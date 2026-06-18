import logging
import re
from datetime import datetime, timezone
import pytz
from kiteconnect import KiteConnect
from src.models.schema import (
    get_open_live_trade,
    get_open_live_timeframe_trades,
    insert_live_trade,
    update_live_trade_entry,
    close_live_trade,
    get_broker_config,
    get_latest_snapshots_for_symbol,
)
from src.engine.symbol_resolver import resolve_instrument
from src.engine.capital_allocator import calculate_trade_lots
from src.engine.paper_plan import (
    build_paper_trade_plan,
    is_bearish_verdict,
    is_bullish_verdict,
)
from src.engine.trade_decision import make_trade_decision
from config.settings import LOT_SIZES
from config.symbol_classes import get_symbol_class, market_window
from config.runtime_config import load_runtime_config

log = logging.getLogger("nsebot.live_trading")
IST = pytz.timezone("Asia/Kolkata")

def _is_market_open(symbol: str) -> bool:
    now = datetime.now(IST)
    open_t, close_t, days = market_window(symbol)
    if now.weekday() not in days:
        return False
    from config.holidays import is_market_holiday
    if is_market_holiday(symbol, now):
        return False
    t = now.strftime("%H:%M")
    return open_t <= t <= close_t

# ---------------------------------------------------------------------------
# FIX #11: Harden MCX exchange routing.
# Original code only recognised 4 MCX symbols — new commodities added to
# WATCH_SYMBOLS (COPPER, ZINC, ALUMINIUM, etc.) would silently route to NFO
# and be rejected by Kite with an invalid-exchange error.
# Named frozenset makes additions explicit and lookup O(1).
# ---------------------------------------------------------------------------
_MCX_SYMBOLS: frozenset[str] = frozenset({
    "NATURALGAS",
    "NATURALGAS_MINI",
    "CRUDEOIL",
    "CRUDEOILM",
    "GOLD",
    "GOLDM",
    "SILVER",
    "SILVERM",
    "COPPER",
    "ZINC",
    "ALUMINIUM",
    "NICKEL",
    "LEAD",
})

def _get_exchange(symbol: str) -> str:
    """Return the correct Kite exchange segment for *symbol*.

    MCX commodities route to ``MCX``; all equity/index derivatives route to
    ``NFO``.  The MCX set is maintained as a module-level frozenset so that
    adding a new commodity requires a single-line change here rather than a
    buried tuple update.
    """
    return "MCX" if symbol.upper() in _MCX_SYMBOLS else "NFO"

_cached_kite_client = None
_cached_access_token = None


def clear_kite_client_cache() -> None:
    global _cached_kite_client, _cached_access_token
    _cached_kite_client = None
    _cached_access_token = None

def _get_public_ip() -> str:
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as response:
            return response.read().decode("utf-8").strip()
    except Exception:
        try:
            with urllib.request.urlopen("https://ifconfig.me/ip", timeout=3) as response:
                return response.read().decode("utf-8").strip()
        except Exception:
            return "unknown"

def _handle_kite_ip_error(e: Exception) -> None:
    msg = str(e)
    if any(keyword in msg for keyword in ["No IPs configured", "Add allowed IPs", "static-ip", "IP whitelist", "unauthorized IP"]):
        try:
            public_ip = _get_public_ip()
            log.error(
                "\n============================================================\n"
                "\U0001f6a8 ZERODHA KITE IP ERROR DETECTED \U0001f6a8\n"
                "Your public IP: %s\n"
                "Zerodha requires you to whitelist this IP on the Kite developer console.\n"
                "To resolve this for FREE ($0 cost):\n"
                "1. Log in to your Zerodha Developer Console (https://developers.kite.trade)\n"
                "2. Go to Profile -> IP Whitelist (top right menu)\n"
                "3. Add your current public IP: %s\n"
                "4. Click 'Update' (usually allows 1 change per week)\n"
                "============================================================\n",
                public_ip, public_ip
            )
        except Exception as err:
            log.warning("Failed to auto-resolve public IP for Kite error helper: %s", err)

def get_kite_client() -> KiteConnect | None:
    global _cached_kite_client, _cached_access_token
    config = get_broker_config()
    if not config or not config.get("api_key") or not config.get("access_token"):
        _cached_kite_client = None
        _cached_access_token = None
        return None
    
    # Reuse cached client if access token matches
    if _cached_kite_client and _cached_access_token == config["access_token"]:
        return _cached_kite_client
        
    try:
        kite = KiteConnect(api_key=config["api_key"])
        kite.set_access_token(config["access_token"])
        
        # Mount resilient TLS adapter with pool-eviction retry logic
        try:
            from src.utils.tls_adapter import mount_resilient_tls
            mount_resilient_tls(kite.reqsession)
        except Exception as e:
            log.warning("Failed to configure TLS adapter: %s", e)
            
        _cached_kite_client = kite
        _cached_access_token = config["access_token"]
        
        # Asynchronously populate instrument cache during Kite client init if not ready
        try:
            from src.engine.symbol_resolver import _instrument_cache_is_ready, fetch_and_cache_instruments
            import threading
            if not _instrument_cache_is_ready():
                log.info("Instrument cache not ready. Spawning background thread to fetch instruments...")
                threading.Thread(
                    target=fetch_and_cache_instruments,
                    args=(kite,),
                    daemon=True
                ).start()
        except Exception as e:
            log.warning("Failed to spawn background thread for instrument cache: %s", e)
            
        return kite
    except Exception as e:
        _handle_kite_ip_error(e)
        log.exception("Failed to initialize Kite client")
        _cached_kite_client = None
        _cached_access_token = None
        return None

def _get_option_premium(
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    option_rows: list[dict] | None = None,
) -> float | None:
    for row in option_rows or []:
        try:
            if (
                abs(float(row.get("strike") or 0) - strike) < 0.01
                and str(row.get("option_type") or "").upper() == option_type
            ):
                premium = float(row.get("ltp") or 0.0)
                return premium if premium > 0 else None
        except Exception:
            continue
    try:
        snapshots = get_latest_snapshots_for_symbol(symbol, expiry)
        for snap in snapshots:
            if (abs(snap.get("strike", 0) - strike) < 0.01 and
                snap.get("option_type") == option_type):
                return float(snap.get("ltp") or 0.0)
    except Exception:
        pass
    return None

def _is_reversal_against_open_trade(open_trade: dict, verdict: str, confidence: int) -> bool:
    if confidence < 70:
        return False
    ot = str(open_trade.get("option_type") or "").upper()
    side = open_trade.get("side") or "BUY"
    if ot == "CE" and side == "BUY" and is_bearish_verdict(verdict):
        return True
    if ot == "PE" and side == "BUY" and is_bullish_verdict(verdict):
        return True
    if ot == "CE" and side == "SELL" and is_bullish_verdict(verdict):
        return True
    if ot == "PE" and side == "SELL" and is_bearish_verdict(verdict):
        return True
    return False

# ---------------------------------------------------------------------------
# FIX #10: Structured LLMVerdict parsing
# Accept optional ai_verdict (LLMVerdict dataclass). When present, read
# .verdict and .confidence directly instead of regex-scanning telegram_text.
# Regex fallback retained so legacy callers without ai_verdict still work.
# ---------------------------------------------------------------------------
def _parse_verdict_and_confidence(
    intel_text: str,
    ai_verdict=None,  # LLMVerdict dataclass or None
) -> tuple[str, int]:
    # Prefer structured object — immune to telegram formatting changes
    if ai_verdict is not None:
        try:
            v = (getattr(ai_verdict, "verdict", None) or "").strip()
            c = int(getattr(ai_verdict, "confidence", 0) or 0)
            if v:
                return v, c
        except Exception:
            pass
    # Fallback: regex over telegram_text
    verdict = ""
    confidence = 0
    m_v = re.search(r"\*Verdict:\s*([^\*]+)\*", intel_text or "")
    if m_v:
        verdict = m_v.group(1).strip()
    m_c = re.search(r"Confidence:\s*(\d+)%", intel_text or "")
    if m_c:
        confidence = int(m_c.group(1))
    return verdict, confidence


# ---------------------------------------------------------------------------
# FIX #5: ATR-based dynamic SL/Target for option entries.
# Reads atr_14 from chart_indicators in ctx (3h preferred, 1h fallback).
# Maps ATR to an implied option premium volatility percentage, then applies
# directional multipliers. Falls back to fixed-pct when ATR is unavailable.
# This aligns the live engine with the dynamic logic already used in
# sync_direct_kite_positions for manually-adopted positions.
# ---------------------------------------------------------------------------
def _compute_option_sl_target(
    entry_premium: float,
    side: str,
    ctx: dict,
) -> tuple[float, float]:
    """
    Returns (sl_premium, target_premium) for an option BUY or SELL leg.
    Attempts ATR-based dynamic sizing first; falls back to fixed percentages.
    """
    # --- attempt ATR-based sizing ---
    try:
        underlying_price = float(ctx.get("underlying") or ctx.get("entry_underlying") or 0.0)
        chart_indicators = ctx.get("chart_indicators") or {}
        symbol = ctx.get("symbol", "")
        sym_chart = chart_indicators.get(symbol, chart_indicators)
        # prefer 3h ATR; fall back to 1h
        atr = (
            (sym_chart.get("3h") or {}).get("atr_14")
            or (sym_chart.get("1h") or {}).get("atr_14")
        )
        if atr and underlying_price > 0:
            atr = float(atr)
            # Map ATR as % of underlying to a premium volatility multiplier.
            # A 1% ATR move in the underlying implies ~10-15% option premium swing
            # (rough vega-weighted estimate for near-ATM options).
            atr_pct = atr / underlying_price
            premium_vol_pct = min(atr_pct * 12, 0.60)  # cap at 60% of premium

            if side == "SELL":
                sl_premium = round(entry_premium * (1 + premium_vol_pct * 1.5), 2)
                target_premium = round(entry_premium * (1 - premium_vol_pct), 2)
            else:
                sl_premium = round(entry_premium * (1 - premium_vol_pct * 1.5), 2)
                target_premium = round(entry_premium * (1 + premium_vol_pct * 2), 2)

            # Sanity bounds: SL must be < entry for BUY, > entry for SELL
            if side == "SELL":
                sl_premium = max(sl_premium, entry_premium * 1.10)
                target_premium = min(target_premium, entry_premium * 0.90)
            else:
                sl_premium = min(sl_premium, entry_premium * 0.90)
                target_premium = max(target_premium, entry_premium * 1.20)

            log.debug(
                "ATR-based SL/Target: atr=%.4f atr_pct=%.4f vol_pct=%.4f "
                "entry=%.2f sl=%.2f target=%.2f side=%s",
                atr, atr_pct, premium_vol_pct,
                entry_premium, sl_premium, target_premium, side,
            )
            return sl_premium, target_premium
    except Exception as exc:
        log.debug("ATR-based SL/Target failed (%s) — falling back to fixed pct", exc)

    # --- fixed-percentage fallback ---
    if side == "SELL":
        return round(entry_premium * 1.50, 2), round(entry_premium * 0.60, 2)
    return round(entry_premium * 0.70, 2), round(entry_premium * 1.50, 2)
