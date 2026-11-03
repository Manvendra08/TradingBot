"""
Decision Matrix Engine for Indian Markets (Indices & MCX).
Deterministic structure selection, vol bucketing, strike delta bounds,
MCX liquidity evaluation, and NYMEX 1H trend alignment.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config.decision_matrix_loader import load_decision_matrix
from src.models.schema import get_conn
from src.engine.verdict_sets import is_bullish, is_bearish

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MCX_SYMBOLS = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "COPPER"}

_NYMEX_CACHE: dict[str, tuple[float, str]] = {}
_NYMEX_LOCK = threading.Lock()


def atm_iv_percentile(
    symbol: str,
    lookback_days: int = 30,
    min_samples: int = 10,
    current_atm_iv: float | None = None,
) -> float | None:
    """
    Calculate the implied volatility percentile (IVP, 0-100) of current ATM IV
    relative to historical ATM snapshots over the lookback window.
    Fail-closed: Returns None if insufficient history or database error.
    """
    try:
        from config.symbol_classes import get_strike_step
        step = float(get_strike_step(symbol) or 50)
        tol = step * 0.60

        with get_conn() as conn:
            # Query historical ATM IVs (strikes within 0.6 * strike_step of underlying_price)
            # Filter out extreme unphysical model artifacts (< 3.0 or > 250.0)
            rows = conn.execute(
                """
                SELECT iv FROM option_chain_snapshots
                WHERE symbol = ?
                  AND iv IS NOT NULL AND iv >= 3.0 AND iv <= 250.0
                  AND underlying_price > 0
                  AND abs(strike - underlying_price) <= ?
                  AND fetched_at >= datetime('now', ?)
                ORDER BY fetched_at DESC
                LIMIT 3000
                """,
                (symbol, tol, f"-{int(lookback_days)} days"),
            ).fetchall()

            # If insufficient ATM rows with underlying_price, fall back to general snapshots
            if len(rows) < min_samples:
                rows = conn.execute(
                    """
                    SELECT iv FROM option_chain_snapshots
                    WHERE symbol = ?
                      AND iv IS NOT NULL AND iv >= 3.0 AND iv <= 250.0
                      AND fetched_at >= datetime('now', ?)
                    ORDER BY fetched_at DESC
                    LIMIT 3000
                    """,
                    (symbol, f"-{int(lookback_days)} days"),
                ).fetchall()

            if len(rows) < min_samples:
                return None

            all_ivs = sorted(float(r["iv"]) for r in rows)

            current_iv = current_atm_iv
            if current_iv is None or current_iv <= 0:
                cur_row = conn.execute(
                    """
                    SELECT iv FROM option_chain_snapshots
                    WHERE symbol = ?
                      AND iv IS NOT NULL AND iv >= 3.0 AND iv <= 250.0
                      AND underlying_price > 0
                      AND abs(strike - underlying_price) <= ?
                    ORDER BY fetched_at DESC, abs(strike - underlying_price) ASC
                    LIMIT 1
                    """,
                    (symbol, tol),
                ).fetchone()

                if not cur_row:
                    cur_row = conn.execute(
                        """
                        SELECT iv FROM option_chain_snapshots
                        WHERE symbol = ? AND iv IS NOT NULL AND iv >= 3.0 AND iv <= 250.0
                        ORDER BY fetched_at DESC
                        LIMIT 1
                        """,
                        (symbol,),
                    ).fetchone()

                if not cur_row:
                    return None
                current_iv = float(cur_row["iv"])

            rank = sum(1 for iv in all_ivs if iv <= current_iv)
            percentile = (rank / len(all_ivs)) * 100.0
            return round(percentile, 2)
    except Exception as exc:
        log.warning("[decision_matrix] Failed to calculate IVP for %s: %s", symbol, exc)
        return None


def vol_bucket(
    symbol: str,
    ivp: float | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """
    Classify volatility regime based on IV percentile:
    - LOW: IVP < low_max (default < 25)
    - MID: mid_min <= IVP <= mid_max (default 25 - 70)
    - HIGH: IVP > high_min (default > 70)
    Fail-closed: If ivp is None, returns fallback_bucket (default 'HIGH' -> defined-risk only).
    """
    if cfg is None:
        cfg = load_decision_matrix()

    vb_cfg = cfg.get("vol_buckets", {})
    fallback = str(vb_cfg.get("fallback_bucket", "HIGH")).upper()

    if ivp is None:
        return fallback

    low_max = float(vb_cfg.get("low_max", 25.0))
    mid_max = float(vb_cfg.get("mid_max", 70.0))

    if ivp < low_max:
        return "LOW"
    elif ivp <= mid_max:
        return "MID"
    else:
        return "HIGH"


def is_mcx_strike_liquid(
    strike_data: dict[str, Any],
    now_ist: datetime | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Two-tier liquidity gate for MCX option strikes (sold and hedge legs).
    Checks in order:
    1. OI floor (default 1500)
    2. Quote truth (bid > 0, ask > 0, spread within absolute or percentage limit)
    3. Time-tiered cumulative daily volume
    Returns (True, 'OK') or (False, reason_string).
    """
    if cfg is None:
        cfg = load_decision_matrix()

    liq_cfg = cfg.get("liquidity", {}).get("mcx", {})
    min_oi = int(liq_cfg.get("min_oi", 1500))

    # 1. Open Interest Floor
    oi = int(strike_data.get("oi") or 0)
    if oi < min_oi:
        return False, f"LIQ: Low OI ({oi} < {min_oi})"

    # 2. Quote Truth & Spread
    require_quote = bool(liq_cfg.get("require_quote", True))
    bid = float(strike_data.get("bid") or 0.0)
    ask = float(strike_data.get("ask") or 0.0)

    if require_quote:
        if bid <= 0.0 or ask <= 0.0:
            return False, f"LIQ: Zero quote (bid={bid:.2f}, ask={ask:.2f})"

        abs_spread = ask - bid
        if abs_spread < 0:
            return False, f"LIQ: Crossed book (bid={bid:.2f} > ask={ask:.2f})"

        spread_pct = (abs_spread / ask) * 100.0 if ask > 0 else 999.0
        hard_max_spread_pct = float(liq_cfg.get("hard_max_spread_pct", 15.0))
        if hard_max_spread_pct > 0 and spread_pct > hard_max_spread_pct:
            return False, f"LIQ: Extreme spread ({spread_pct:.1f}% > {hard_max_spread_pct:.1f}%)"

        max_abs = float(liq_cfg.get("max_abs_spread", 0.30))
        max_pct = float(liq_cfg.get("max_spread_pct", 2.0))
        pass_abs = abs_spread <= max_abs
        pass_pct = spread_pct <= max_pct

        spread_mode = str(liq_cfg.get("spread_mode", "either")).lower()
        if spread_mode == "either":
            if not (pass_abs or pass_pct):
                return False, f"LIQ: Wide spread ({abs_spread:.2f} pts / {spread_pct:.1f}%)"
        else:
            if not (pass_abs and pass_pct):
                return False, f"LIQ: Wide spread ({abs_spread:.2f} pts / {spread_pct:.1f}%)"

    # 3. Time-tiered Volume
    if now_ist is None:
        now_ist = datetime.now(_IST)

    current_hour = now_ist.hour
    time_tiers = liq_cfg.get("time_tiers", [
        {"before_hour": 12, "min_vol": 50},
        {"before_hour": 17, "min_vol": 150},
        {"default": 400},
    ])

    required_vol = 400
    for tier in time_tiers:
        if "before_hour" in tier and current_hour < int(tier["before_hour"]):
            required_vol = int(tier["min_vol"])
            break
        elif "default" in tier:
            required_vol = int(tier["default"])

    vol = int(strike_data.get("volume") or 0)
    if vol < required_vol:
        return False, f"LIQ: Low volume ({vol} < {required_vol} at {now_ist.strftime('%H:%M')})"

    return True, "OK"


def nymex_trend(
    symbol: str,
    now_utc: datetime | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """
    Check 20-EMA trend on NYMEX 1H candles for MCX commodities.
    Uses in-memory cache to prevent blocking or rate limits.
    Returns 'BULLISH', 'BEARISH', or 'NEUTRAL'. Never raises.
    """
    if cfg is None:
        cfg = load_decision_matrix()

    nymex_cfg = cfg.get("nymex", {})
    mode = str(nymex_cfg.get("mode", "shadow")).lower()
    if mode == "off":
        return "NEUTRAL"

    symbol_map = nymex_cfg.get("symbol_map", {
        "NATURALGAS": "NG=F",
        "CRUDEOIL": "CL=F",
        "GOLD": "GC=F",
        "SILVER": "SI=F",
    })

    nymex_symbol = symbol_map.get(symbol.upper())
    if not nymex_symbol:
        return "NEUTRAL"

    cache_secs = int(nymex_cfg.get("cache_secs", 300))
    current_time = time.time()

    with _NYMEX_LOCK:
        if nymex_symbol in _NYMEX_CACHE:
            ts, trend = _NYMEX_CACHE[nymex_symbol]
            if current_time - ts < cache_secs:
                return trend

    try:
        import yfinance as yf
        df = yf.download(nymex_symbol, period="5d", interval="1h", progress=False)
        if len(df) < 21:
            trend = "NEUTRAL"
        else:
            closes = df["Close"].squeeze()
            ema20 = closes.ewm(span=20, adjust=False).mean()
            last_close = float(closes.iloc[-1].item() if hasattr(closes.iloc[-1], "item") else closes.iloc[-1])
            last_ema = float(ema20.iloc[-1].item() if hasattr(ema20.iloc[-1], "item") else ema20.iloc[-1])

            if last_close > last_ema:
                trend = "BULLISH"
            elif last_close < last_ema:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"
    except Exception as exc:
        log.warning("[decision_matrix] Failed to fetch NYMEX trend for %s (%s): %s", symbol, nymex_symbol, exc)
        trend = "NEUTRAL"

    with _NYMEX_LOCK:
        _NYMEX_CACHE[nymex_symbol] = (current_time, trend)

    return trend


def select_structure(
    direction: str,
    vol: str,
    dte: int | None = None,
    is_event: bool = False,
    symbol: str = "NIFTY",
    now_ist: datetime | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[str | None, list[str], str]:
    """
    Select recommended structure and allowed set from regime matrix + overrides:
    - Direction: BULLISH | BEARISH | NEUTRAL
    - Vol: LOW | MID | HIGH
    - Overrides: 0-DTE window, MCX no-naked, event-window defined-risk only, blocked structures.
    Returns (recommended_structure, allowed_structures, reason).
    """
    if cfg is None:
        cfg = load_decision_matrix()

    dir_key = direction.upper() if direction else "NEUTRAL"
    vol_key = vol.upper() if vol else "HIGH"

    regime_table = cfg.get("regime_table", {})
    dir_table = regime_table.get(dir_key, regime_table.get("NEUTRAL", {}))
    raw_allowed = list(dir_table.get(vol_key, []))

    if not raw_allowed or "NO_TRADE" in raw_allowed:
        return None, [], f"REGIME: {dir_key} in {vol_key} vol is NO_TRADE"

    overrides = cfg.get("overrides", {})

    # 1. 0-DTE entry cutoff
    if dte == 0 and overrides.get("zero_dte", {}).get("enabled", True):
        cutoff_time_str = overrides.get("zero_dte", {}).get("no_entry_after_ist", "13:00")
        if now_ist is None:
            now_ist = datetime.now(_IST)
        cutoff_hour, cutoff_min = [int(p) for p in cutoff_time_str.split(":")]
        if (now_ist.hour > cutoff_hour) or (now_ist.hour == cutoff_hour and now_ist.minute >= cutoff_min):
            return None, [], f"OVERRIDE: 0-DTE entry closed after {cutoff_time_str} IST"

    # 2. MCX No-Naked Override
    symbol_upper = symbol.upper()
    is_mcx = symbol_upper in MCX_SYMBOLS
    if is_mcx and overrides.get("mcx_no_naked", {}).get("enabled", True):
        replacements = overrides.get("mcx_no_naked", {}).get("replacement", {"SHORT_STRANGLE": "IRON_CONDOR"})
        transformed: list[str] = []
        for s in raw_allowed:
            rep = replacements.get(s, s)
            if rep not in transformed:
                transformed.append(rep)
        raw_allowed = transformed

    # 3. Event Window Override (Defined risk only, half size)
    if is_event and overrides.get("event_window", {}).get("enabled", True):
        defined_risk = {"IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"}
        raw_allowed = [s for s in raw_allowed if s in defined_risk]
        if not raw_allowed:
            return None, [], "OVERRIDE: Event window permits defined-risk structures only"

    # 4. Filter blocked structures
    blocked = set(overrides.get("blocked_structures", []))
    allowed = [s for s in raw_allowed if s not in blocked]

    if not allowed:
        return None, [], "OVERRIDE: All structures in regime cell are blocked"

    return allowed[0], allowed, "OK"


def delta_band(
    structure: str,
    leg_type: str,
    symbol: str = "NIFTY",
    cfg: dict[str, Any] | None = None,
) -> tuple[float, float, float]:
    """
    Get (min_delta, max_delta, danger_ceiling) for a structure leg.
    Leg types: 'SHORT', 'LONG', 'WING', 'HEDGE'.
    """
    if cfg is None:
        cfg = load_decision_matrix()

    profile = cfg.get("delta_profile", "aggressive")
    is_mcx = symbol.upper() in MCX_SYMBOLS
    asset_class = "mcx" if is_mcx else "indices"

    bands = cfg.get("delta_bands", {}).get(profile, {}).get(asset_class, {})

    struct_upper = structure.upper()
    leg_upper = leg_type.upper()

    if struct_upper in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
        key = "CREDIT_SPREAD_SHORT" if leg_upper == "SHORT" else "CREDIT_SPREAD_LONG"
    elif struct_upper in ("IRON_CONDOR", "JADE_LIZARD"):
        key = "IRON_CONDOR_SHORT" if leg_upper == "SHORT" else "IRON_CONDOR_LONG"
    elif struct_upper == "SHORT_STRANGLE":
        key = "SHORT_STRANGLE_SHORT"
    else:
        key = "CREDIT_SPREAD_SHORT" if leg_upper == "SHORT" else "CREDIT_SPREAD_LONG"

    band = bands.get(key, {"min": 0.20, "max": 0.30, "danger_ceiling": 0.35})
    return float(band["min"]), float(band["max"]), float(band["danger_ceiling"])


def evaluate_matrix_decision(
    symbol: str,
    intel: dict[str, Any],
    scan_context: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Produce complete deterministic decision matrix shadow assessment.
    Runs non-blocking, fail-closed, with complete telemetry.
    """
    if cfg is None:
        cfg = load_decision_matrix()

    symbol_upper = symbol.upper()
    is_mcx = symbol_upper in MCX_SYMBOLS

    # 1. Vol regime
    current_atm_iv = None
    if isinstance(scan_context, dict):
        current_atm_iv = scan_context.get("atm_iv")
        if not current_atm_iv:
            atm_strike = float(scan_context.get("atm_strike") or 0.0)
            option_rows = scan_context.get("option_rows") or []
            if atm_strike > 0 and option_rows:
                cand_ivs = [
                    float(r.get("iv") or 0.0)
                    for r in option_rows
                    if abs(float(r.get("strike") or 0.0) - atm_strike) < 0.01 and float(r.get("iv") or 0.0) > 0
                ]
                if cand_ivs:
                    current_atm_iv = sum(cand_ivs) / len(cand_ivs)
    if not current_atm_iv and isinstance(intel, dict):
        current_atm_iv = intel.get("atm_iv")

    ivp = atm_iv_percentile(symbol_upper, current_atm_iv=current_atm_iv)
    vol_regime = vol_bucket(symbol_upper, ivp=ivp, cfg=cfg)

    # 2. Market direction from engine verdict (uses canonical verdict_sets)
    raw_direction = (
        intel.get("direction")
        or intel.get("bias")
        or intel.get("verdict_label")
        or scan_context.get("verdict_label")
        or "NEUTRAL"
    )
    raw_str = str(raw_direction).strip()
    raw_upper = raw_str.upper()
    if is_bullish(raw_str) or "BULL" in raw_upper or "LONG BUILD" in raw_upper or "PUT WRIT" in raw_upper:
        direction = "BULLISH"
    elif is_bearish(raw_str) or "BEAR" in raw_upper or "SHORT BUILD" in raw_upper or "CALL WRIT" in raw_upper:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # 2b. Heavyweight Confluence Veto (for NSE/BSE equity indices)
    hw_veto_applied = False
    hw_confluence_breadth = None
    if symbol_upper in ("NIFTY", "BANKNIFTY", "SENSEX"):
        try:
            from src.alerts.heavyweight_digest import get_heavyweights_confluence
            hw_conf = get_heavyweights_confluence() or {}
            if hw_conf:
                hw_confluence_breadth = hw_conf.get("breadth")
                if direction == "BEARISH" and hw_conf.get("veto_bear_calls"):
                    direction = "NEUTRAL"
                    hw_veto_applied = True
                    log.info("[decision_matrix] %s: BEARISH verdict overridden to NEUTRAL by Heavyweights confluence (%s)", symbol_upper, hw_confluence_breadth)
                elif direction == "BULLISH" and hw_conf.get("veto_bull_puts"):
                    direction = "NEUTRAL"
                    hw_veto_applied = True
                    log.info("[decision_matrix] %s: BULLISH verdict overridden to NEUTRAL by Heavyweights confluence (%s)", symbol_upper, hw_confluence_breadth)
        except Exception as hw_exc:
            log.debug("[decision_matrix] Heavyweights confluence check failed gracefully: %s", hw_exc)

    # 3. Event check
    now_ist = datetime.now(_IST)
    is_event = False
    events = cfg.get("events", [])
    for ev in events:
        if symbol_upper in ev.get("symbols", []):
            try:
                ev_dt = datetime.fromisoformat(ev["datetime_ist"]).replace(tzinfo=_IST)
                diff = abs((now_ist - ev_dt).total_seconds())
                if diff <= 3600.0 * 2.0:  # Within 2 hours
                    is_event = True
                    break
            except Exception:
                pass

    # 4. DTE check
    dte = None
    expiry_str = intel.get("expiry") or scan_context.get("expiry")
    if expiry_str:
        try:
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte = (exp_date - now_ist.date()).days
        except Exception:
            pass

    # 5. Structure recommendation
    rec_structure, allowed_structures, reason = select_structure(
        direction=direction,
        vol=vol_regime,
        dte=dte,
        is_event=is_event,
        symbol=symbol_upper,
        now_ist=now_ist,
        cfg=cfg,
    )

    # 6. NYMEX trend for MCX
    nymex_sym = cfg.get("nymex", {}).get("symbol_map", {}).get(symbol_upper)
    nymex_tr = nymex_trend(symbol_upper, cfg=cfg) if is_mcx else None
    nymex_agrees = None
    if is_mcx and nymex_tr:
        if direction == "BULLISH":
            nymex_agrees = nymex_tr == "BULLISH"
        elif direction == "BEARISH":
            nymex_agrees = nymex_tr == "BEARISH"
        else:
            nymex_agrees = True

    # 7. Actual decisions
    actual_llm = intel.get("multileg_verdict")
    actual_llm_structure = getattr(actual_llm, "strategy", None) if actual_llm else None
    actual_core_verdict = intel.get("verdict_label") or scan_context.get("verdict_label")

    return {
        "mode": cfg.get("mode", "shadow"),
        "config_version": cfg.get("config_version", "1.0.0"),
        "delta_profile": cfg.get("delta_profile", "aggressive"),
        "symbol": symbol_upper,
        "direction": direction,
        "ivp": ivp,
        "vol_bucket": vol_regime,
        "nymex_symbol": nymex_sym,
        "nymex_trend": nymex_tr,
        "nymex_agrees": nymex_agrees,
        "recommended_structure": rec_structure,
        "allowed_structures": allowed_structures,
        "actual_llm_structure": str(actual_llm_structure) if actual_llm_structure else None,
        "actual_core_verdict": str(actual_core_verdict) if actual_core_verdict else None,
        "reason": reason,
        "is_event": is_event,
        "heavyweight_veto_applied": hw_veto_applied,
        "heavyweight_confluence": hw_confluence_breadth,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

def check_churn_guards(
    symbol: str,
    open_books: list[dict],
    trade_mode: str = "PAPER",
    now_dt: datetime | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Check churn guards defined in decision_matrix.json:
    1. Max open books per symbol (default 2)
    2. Cooldown minutes after a stop loss / closed trade (default 30)
    3. Max entries per day (default 3)
    
    Returns:
        (True, "OK") if allowed to enter.
        (False, reason_str) if entry must be blocked.
    """
    if cfg is None:
        cfg = load_decision_matrix(symbol)

    churn_cfg = cfg.get("churn_guards", {})
    max_open_books = int(churn_cfg.get("max_open_books_per_symbol", 2))
    cooldown_min = int(churn_cfg.get("cooldown_minutes", 30))
    max_entries_day = int(churn_cfg.get("max_entries_per_day", 3))

    # 1. Open books cap
    if len(open_books) >= max_open_books:
        return False, f"Max open books cap reached ({len(open_books)} >= {max_open_books})"

    # 2. Daily entries & Cooldown queries
    try:
        from src.models.schema import get_read_conn
        with get_read_conn() as conn:
            # Check entries created today
            c_today = conn.execute(
                "SELECT COUNT(*) as cnt FROM multi_leg_trades WHERE UPPER(symbol)=UPPER(?) AND trade_mode=? AND DATE(created_at) = DATE('now')",
                (symbol, trade_mode),
            ).fetchone()
            entries_today = int(c_today["cnt"] if c_today else 0)
            if entries_today >= max_entries_day:
                return False, f"Daily entry cap reached ({entries_today} >= {max_entries_day})"

            # Check cooldown after a stopped-out or closed trade
            if cooldown_min > 0:
                row_last = conn.execute(
                    "SELECT closed_at FROM multi_leg_trades WHERE UPPER(symbol)=UPPER(?) AND trade_mode=? AND status IN ('CLOSED', 'SL_HIT', 'STOP_LOSS') ORDER BY id DESC LIMIT 1",
                    (symbol, trade_mode),
                ).fetchone()
                if row_last and row_last["closed_at"]:
                    try:
                        closed_str = str(row_last["closed_at"]).strip()
                        closed_ts = closed_str[:19]
                        last_closed_dt = datetime.fromisoformat(closed_ts)
                        if now_dt is None:
                            now_dt = datetime.now()
                        else:
                            now_dt = now_dt.replace(tzinfo=None)
                        diff_sec = (now_dt - last_closed_dt).total_seconds()
                        if 0 <= diff_sec < (cooldown_min * 60):
                            rem_m = int(round((cooldown_min * 60 - diff_sec) / 60))
                            return False, f"Cooldown active after recent exit ({rem_m}m remaining < {cooldown_min}m)"
                    except Exception as err:
                        log.debug("[decision_matrix] cooldown parse error: %s", err)
    except Exception as e:
        log.debug("[decision_matrix] check_churn_guards DB error: %s", e)

    return True, "OK"