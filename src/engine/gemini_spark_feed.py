"""
Gemini Spark JSON Drive Feed & Paper Execution Subsystem.
============================================================
Independent, parallel subsystem that polls the Google Gemini Spark trade feed
from the Google Apps Script bridge and executes/manages paper trades strictly
within NSEBOT's existing paper trading execution framework.

Multi-Symbol Support:
- MCX Commodities (e.g. NATURALGAS via mcx_ng_live_signal.json)
- NSE Index Futures (e.g. NIFTY / NIFTY_FUT via nifty_fut_live_signal.json)

Security & Safety Constraints:
- PAPER TRADES ONLY: Live Kite broker orders must NEVER be placed by this subsystem.
- Provenance & Marking: All trades are recorded with setup_type="GeminiAI" and ai_model_name="GeminiAI".
- Schema Compatibility: Conforms to paper_trades schema, lot sizing rules, and transaction cost accounting.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import pytz

from config.runtime_config import (
    is_gemini_spark_feed_enabled,
    get_gemini_spark_poll_interval_sec,
    load_runtime_config,
)
from config.settings import DATA_DIR, LOT_SIZES, IST
from src.models.schema import (
    get_conn,
    insert_paper_trade,
    close_paper_trade,
)
from src.engine.ng_risk_manager import calculate_ng_lot_size

log = logging.getLogger(__name__)

STATE_FILE = Path(DATA_DIR) / "gemini_spark_state.json"
APPS_SCRIPT_FEED_URL = "https://script.google.com/macros/s/AKfycbydlEELF1vn_cd0sypzkEAfgkH2uAIZ61ypH1NaxTviGwEP9PDLyrVfrE7iKMtWSqRp/exec"

SUPPORTED_FEED_SYMBOLS = ("NATURALGAS", "NIFTY")

_WORKER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_FEED_SESSION: requests.Session | None = None
_FEED_SESSION_LOCK = threading.Lock()


def _get_feed_session() -> requests.Session:
    """Provide a persistent, connection-pooled HTTP session with retries for Google Apps Script requests."""
    global _FEED_SESSION
    if _FEED_SESSION is not None:
        return _FEED_SESSION
    with _FEED_SESSION_LOCK:
        if _FEED_SESSION is not None:
            return _FEED_SESSION
        sess = requests.Session()
        sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NSEBOT/3.0"
        sess.headers["Accept"] = "application/json"
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        # Silence internal urllib3 connection retry warnings on transient drops
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        sess.mount("https://", adapter)
        _FEED_SESSION = sess
        return sess


def _is_symbol_market_open(symbol: str) -> bool:
    """Determine if continuous regular trading is active for symbol."""
    try:
        from config.symbol_classes import market_window
        from config.holidays import is_market_holiday
        now_ist = datetime.now(IST)
        open_t, close_t, days = market_window(symbol)
        if now_ist.weekday() not in days:
            return False
        if is_market_holiday(symbol, now_ist):
            return False
        t = now_ist.strftime("%H:%M")
        return open_t <= t <= close_t
    except Exception:
        return True


def _parse_contract(contract_str: str) -> tuple[str, str]:
    """
    Parse contract string like 'NATURALGAS 27-OCT-2026', 'NIFTY_FUT', 'NIFTY 30OCT2026'.
    Returns canonical (symbol, expiry_iso_str).
    """
    parts = contract_str.strip().split()
    symbol = "NATURALGAS"
    if parts:
        raw = parts[0].upper()
        if "NIFTY" in raw and "BANK" not in raw and "FIN" not in raw and "MID" not in raw:
            symbol = "NIFTY"
        elif "BANKNIFTY" in raw:
            symbol = "BANKNIFTY"
        else:
            symbol = raw

    expiry_str = ""
    date_part = " ".join(parts[1:]) if len(parts) > 1 else ""
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d%b%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_part.strip(), fmt)
            expiry_str = dt.strftime("%Y-%m-%d")
            break
        except ValueError:
            pass

    if not expiry_str:
        try:
            from config.symbol_classes import get_futures_expiry
            expiry_str = get_futures_expiry(symbol) or datetime.now(IST).strftime("%Y-%m-%d")
        except Exception:
            expiry_str = datetime.now(IST).strftime("%Y-%m-%d")

    return symbol, expiry_str


def calculate_spark_lot_size(symbol: str, capital: float, stop_dist: float) -> int:
    """
    Calculate risk-adjusted lot sizing for GeminiAI paper trades.
    - NATURALGAS: Delegate to calculate_ng_lot_size (cap 5)
    - NIFTY: 1.5% capital risk, lot_size=65, cap 10 lots
    """
    if symbol == "NATURALGAS":
        return calculate_ng_lot_size(capital, stop_dist)

    lot_size = LOT_SIZES.get(symbol, 65)
    if stop_dist <= 0:
        return 1

    risk_cap = capital * 0.015  # 1.5% max risk per trade
    lots = int(risk_cap // (stop_dist * lot_size))
    max_lots = 10 if symbol == "NIFTY" else 5
    return max(1, min(lots, max_lots))


def _load_state() -> dict[str, Any]:
    """Load state file containing last processed timestamps."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[gemini_spark] Failed to read state file: %s", e)
    return {"last_processed_timestamp": None, "symbols": {}}


def _save_state(state: dict[str, Any]) -> None:
    """Save state file atomically."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.warning("[gemini_spark] Failed to save state file: %s", e)


def _get_symbol_last_ts(state: dict[str, Any], symbol: str) -> str | None:
    """Retrieve last processed timestamp for a specific symbol."""
    symbols_map = state.get("symbols", {})
    if isinstance(symbols_map, dict) and symbol in symbols_map:
        return symbols_map[symbol].get("last_processed_timestamp")
    if symbol == "NATURALGAS":
        return state.get("last_processed_timestamp")
    return None


def _set_symbol_last_ts(state: dict[str, Any], symbol: str, ts: str, payload: dict[str, Any]) -> None:
    """Update state for a specific symbol while maintaining top-level backward compatibility."""
    if "symbols" not in state or not isinstance(state["symbols"], dict):
        state["symbols"] = {}
    state["symbols"][symbol] = {
        "last_processed_timestamp": ts,
        "last_processed_payload": payload,
    }
    if symbol == "NATURALGAS":
        state["last_processed_timestamp"] = ts
        state["last_processed_payload"] = payload


def fetch_latest_signal(symbol: str = "NATURALGAS") -> dict[str, Any]:
    """
    Fetch the latest JSON payload from the Google Apps Script bridge for a given symbol.
    Uses resilient session pooling and backoff retry to transparently absorb transient TLS/EOF drops.
    """
    sess = _get_feed_session()
    for attempt in range(2):
        try:
            resp = sess.get(
                APPS_SCRIPT_FEED_URL,
                params={"symbol": symbol},
                timeout=25.0,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and not data.get("error"):
                    return data
                elif isinstance(data, dict) and data.get("error"):
                    log.debug("[gemini_spark] Bridge returned notice for %s: %s", symbol, data.get("error"))
            else:
                log.debug("[gemini_spark] Failed to fetch feed for %s. Status: %s", symbol, resp.status_code)
            return {}
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as ce:
            if attempt == 0:
                time.sleep(1.0)
                continue
            log.debug("[gemini_spark] Transient network/TLS drop fetching %s: %s", symbol, ce)
        except Exception as e:
            log.debug("[gemini_spark] Feed fetch error for %s: %s", symbol, e)
            break
    return {}


def get_open_gemini_paper_trade(symbol: str = "NATURALGAS") -> dict[str, Any] | None:
    """Retrieve the most recent open paper trade created by GeminiAI for a symbol (backward compatible)."""
    sql = """
        SELECT * FROM paper_trades
        WHERE symbol = ? AND status = 'OPEN' AND setup_type = 'GeminiAI'
        ORDER BY opened_at DESC
        LIMIT 1
    """
    with get_conn(read_only=True) as conn:
        row = conn.execute(sql, (symbol,)).fetchone()
        return dict(row) if row else None


def _extract_setup_id_from_trade(trade: dict[str, Any]) -> str:
    """
    Extract the canonical Gemini setup ID from a trade dictionary.
    Handles legacy trades where signal_key was suffixed with a timestamp (e.g. SETUP_123456789).
    """
    sig_key = str(trade.get("signal_key") or "")
    if sig_key:
        # If suffixed with _<digits>, strip trailing unix timestamp
        import re
        m = re.match(r"^(.*?)(?:_\d{9,11})?$", sig_key)
        if m and m.group(1):
            return m.group(1)
        return sig_key
    
    # Fallback to reason: e.g. "GeminiAI Setup: NG_20261006_BUY_BREAKOUT"
    reason = str(trade.get("reason") or "")
    if "GeminiAI Setup:" in reason:
        raw_part = reason.split("GeminiAI Setup:", 1)[1].strip()
        return raw_part.split()[0].split("|")[0].strip()
    return str(trade.get("id"))


def _is_setup_inactive(setup: dict[str, Any]) -> bool:
    """
    Determine if a setup in the feed payload is completed, closed, invalidated, or missed.
    Inactive setups must NEVER trigger new trade entries.
    """
    status_upper = str(setup.get("status", "")).upper()
    invalidation_ist = str(setup.get("invalidation_time_ist", "")).upper()
    
    inactive_keywords = (
        "CLOSED", "HIT", "TARGET", "MISSED", "CANCELLED", "INVALID", "EXPIRED", "STOP_LOSS_HIT", "SL_HIT"
    )
    if any(k in status_upper for k in inactive_keywords):
        return True
    if invalidation_ist == "CLOSED" or "CLOSED" in invalidation_ist:
        return True
    if setup.get("remaining_lots") == 0:
        return True
    return False


def _extract_reconciled_trade_exit(payload: dict[str, Any], setup_id: str) -> dict[str, Any] | None:
    """
    Search payload for reconciled trade records (e.g. daily_reconciled_trades, reconciled_trades, closed_trades)
    matching the given setup_id.
    Returns a dict with exit details (exit_price, status, reason, pnl_pts) or None if not found.
    """
    reconciled_keys = ("daily_reconciled_trades", "reconciled_trades", "closed_trades", "settled_trades")
    reconciled_list: list[dict[str, Any]] = []

    for k in reconciled_keys:
        cand = payload.get(k)
        if isinstance(cand, list):
            reconciled_list.extend([x for x in cand if isinstance(x, dict)])

    # Also check if payload has symbols map
    symbols_map = payload.get("symbols")
    if isinstance(symbols_map, dict):
        for sym_data in symbols_map.values():
            if isinstance(sym_data, dict):
                inner_payload = sym_data.get("last_processed_payload") or sym_data
                if isinstance(inner_payload, dict):
                    for k in reconciled_keys:
                        cand = inner_payload.get(k)
                        if isinstance(cand, list):
                            reconciled_list.extend([x for x in cand if isinstance(x, dict)])

    for rec in reconciled_list:
        rec_id = str(rec.get("id") or "").strip()
        if not rec_id:
            continue
        # Exact match or prefix match
        if rec_id == setup_id or setup_id.startswith(rec_id) or rec_id.startswith(setup_id):
            # Prioritize explicit exit prices
            exit_price = None
            for p_key in (
                "trailed_sl_exit",
                "exit_price",
                "target_2_hit",
                "target_1_hit",
                "target_hit",
                "close_price",
                "fill_exit",
            ):
                if p_key in rec and rec[p_key] is not None:
                    try:
                        val = float(rec[p_key] or 0.0)
                        if val > 0:
                            exit_price = val
                            break
                    except (ValueError, TypeError):
                        pass

            raw_status = str(rec.get("status") or "CLOSED_PROFIT").upper()
            if "PROFIT" in raw_status or "TARGET" in raw_status:
                status = "CLOSED_TARGET"
            elif "SL" in raw_status or "LOSS" in raw_status or "STOP" in raw_status:
                status = "CLOSED_SL"
            else:
                status = "CLOSED_AI_EXIT"

            notes = str(rec.get("notes") or rec.get("reason") or raw_status)
            pnl_pts = rec.get("blended_pnl_pts") or rec.get("pnl_pts") or rec.get("pnl")

            return {
                "exit_price": exit_price,
                "status": status,
                "reason": f"GeminiAI Reconciled: {notes}",
                "raw_status": raw_status,
                "pnl_pts": pnl_pts,
                "rec_id": rec_id,
            }

    return None


def get_open_gemini_paper_trades_map(symbol: str = "NATURALGAS") -> dict[str, dict[str, Any]]:
    """
    Retrieve all currently open paper trades created by GeminiAI for a symbol,
    indexed by canonical setup_id.
    """
    sql = """
        SELECT * FROM paper_trades
        WHERE symbol = ? AND status = 'OPEN' AND setup_type = 'GeminiAI'
        ORDER BY opened_at ASC
    """
    with get_conn(read_only=True) as conn:
        rows = conn.execute(sql, (symbol,)).fetchall()
        trades_map = {}
        for r in rows:
            d = dict(r)
            canonical_id = _extract_setup_id_from_trade(d)
            trades_map[canonical_id] = d
        return trades_map


def push_execution_feedback_to_ledger(event: str, data: dict[str, Any]) -> bool:
    """
    Push trade execution events (TRADE_OPENED, SL_TRAILED, TRADE_CLOSED) back to Google Apps Script
    to maintain a live 2-way synchronized ledger in Google Sheets / Drive.
    Dynamically routes to dedicated sheet ID (e.g. NIFTY vs NATURALGAS).
    """
    try:
        now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        sym = str(data.get("symbol", "")).upper()
        from config.runtime_config import get_gemini_spark_sheet_id
        target_sheet_id = get_gemini_spark_sheet_id(sym)

        payload = {
            "action": "log_event",
            "event": event,
            "timestamp_ist": now_ist,
            "sheet_id": target_sheet_id,
            **data,
        }
        resp = _get_feed_session().post(
            APPS_SCRIPT_FEED_URL,
            json=payload,
            timeout=25.0,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            log.info("[gemini_spark] 2-Way Ledger updated: %s for %s #%s (sheet: %s)", event, data.get("symbol"), data.get("trade_id"), target_sheet_id[:8])
            return True
        else:
            log.debug("[gemini_spark] 2-Way Ledger post status %s: %s", resp.status_code, resp.text[:200])
    except requests.exceptions.Timeout:
        log.debug("[gemini_spark] Ledger feedback timed out (25s) for %s #%s", event, data.get("trade_id"))
    except Exception as e:
        log.warning("[gemini_spark] Failed to push feedback to 2-way ledger: %s", e)
    return False


def get_all_active_gemini_paper_trades() -> list[dict[str, Any]]:
    """Retrieve all currently open paper trades created by GeminiAI across all symbols."""
    sql = """
        SELECT id, symbol, side, lots, entry_underlying, sl_underlying, target_underlying, status, trade_status, signal_key, opened_at
        FROM paper_trades
        WHERE status = 'OPEN' AND setup_type = 'GeminiAI'
        ORDER BY opened_at ASC
    """
    try:
        with get_conn(read_only=True) as conn:
            rows = conn.execute(sql).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning("[gemini_spark] Failed to query active GeminiAI trades: %s", e)
        return []


def sync_active_positions_snapshot() -> bool:
    """
    Push current active positions snapshot to Google Apps Script to update
    the NSEBOT_ACTIVE_POSITIONS sheet tab and nsebot_live_status.json in Google Drive.
    Supplies dedicated sheet IDs for both NIFTY and NATURALGAS.
    """
    try:
        active_trades = get_all_active_gemini_paper_trades()
        now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        from config.runtime_config import get_gemini_spark_sheet_id
        payload = {
            "action": "sync_snapshot",
            "timestamp_ist": now_ist,
            "total_open_trades": len(active_trades),
            "active_positions": active_trades,
            "ng_sheet_id": get_gemini_spark_sheet_id("NATURALGAS"),
            "nifty_sheet_id": get_gemini_spark_sheet_id("NIFTY"),
        }
        resp = _get_feed_session().post(
            APPS_SCRIPT_FEED_URL,
            json=payload,
            timeout=25.0,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            log.debug("[gemini_spark] 2-Way Positions Snapshot synced (%d active)", len(active_trades))
            return True
    except requests.exceptions.Timeout:
        log.debug("[gemini_spark] Positions snapshot sync timed out (25s) - will retry on next cycle")
    except Exception as e:
        log.warning("[gemini_spark] Failed to sync active positions snapshot: %s", e)
    return False


def execute_gemini_setups(payload: dict[str, Any]) -> None:
    """
    Process incoming Gemini Spark JSON payload and execute purely in PAPER TRADING.
    Supports both MCX Commodity and NSE Index Futures.
    Autonomous execution: Not blocked by generic NSEBOT open position caps (>5) or scan blockers.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    market_state_raw = payload.get("market_state", "OPEN")
    if isinstance(market_state_raw, dict):
        market_state = str(market_state_raw.get("trend") or market_state_raw.get("status") or "OPEN").upper()
    else:
        market_state = str(market_state_raw).upper()

    contract_str = payload.get("symbol", "NATURALGAS")
    symbol, parsed_expiry = _parse_contract(contract_str)
    expiry = payload.get("contract_expiry") or payload.get("expiry") or parsed_expiry

    # Extract real-time CMP for accurate entries/exits
    cmp_val = payload.get("cmp_inr")
    if cmp_val is None:
        cmp_val = payload.get("cmp")

    raw_setups = payload.get("setups")
    setups = raw_setups if isinstance(raw_setups, list) else []

    # If market state is explicitly closed AND there are no actionable setups in payload, exit early
    if market_state in ("CLOSED_WEEKEND", "CLOSED", "CLOSED_SESSION", "POST_CLOSE") and not setups:
        log.info("[gemini_spark] Market State is %s for %s and no setups provided. Queued for next session.", market_state, payload.get("symbol"))
        return

    open_trades_map = get_open_gemini_paper_trades_map(symbol)
    active_setup_ids = {str(s.get("id")) for s in setups if isinstance(s, dict) and s.get("id")}

    # Close any open GeminiAI trade for this symbol whose setup ID is no longer active in the feed
    for open_signal_key, open_trade in list(open_trades_map.items()):
        if open_signal_key not in active_setup_ids:
            trade_id = int(open_trade["id"])

            # Check if this setup was recorded in daily_reconciled_trades / reconciled_trades
            reconciled = _extract_reconciled_trade_exit(payload, open_signal_key)

            if reconciled and reconciled.get("exit_price"):
                close_price = float(reconciled["exit_price"])
                close_status = reconciled["status"]
                close_reason = reconciled["reason"]
                log.info(
                    "[gemini_spark] Setup %s resolved via reconciled records. Closing trade #%s at %.2f (%s)",
                    open_signal_key, trade_id, close_price, close_status,
                )
            else:
                close_price = float(cmp_val if cmp_val is not None else (open_trade.get("entry_underlying") or 0.0))
                close_status = "CLOSED_AI_EXIT"
                close_reason = f"GeminiAI: Setup {open_signal_key} no longer active in feed"
                log.info("[gemini_spark] Setup %s removed from feed without reconciled price. Closing trade #%s at CMP %.2f", open_signal_key, trade_id, close_price)

            close_paper_trade(
                trade_id=trade_id,
                closed_at=now_iso,
                exit_underlying=close_price,
                exit_premium=close_price,
                status=close_status,
                reason=close_reason,
            )
            push_execution_feedback_to_ledger("TRADE_CLOSED", {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": open_trade.get("side"),
                "fill_price": open_trade.get("entry_underlying"),
                "exit_price": close_price,
                "sl_price": open_trade.get("sl_underlying"),
                "target_price": open_trade.get("target_underlying"),
                "lots": open_trade.get("lots"),
                "setup_id": open_signal_key,
                "reason": close_reason,
                "pnl": (reconciled.get("pnl_pts") if reconciled else 0.0),
            })
            _send_telegram_notification(
                f"🏁 *[GeminiAI] Paper Trade CLOSED*\n"
                f"• Symbol: `{symbol}`\n"
                f"• Trade ID: `#{trade_id}`\n"
                f"• Setup ID: `{open_signal_key}`\n"
                f"• Exit Status: `{close_status}`\n"
                f"• Exit Price: ₹{close_price:.2f}\n"
                f"• Reason: {close_reason}"
            )
            open_trades_map.pop(open_signal_key, None)

    for setup in setups:
        setup_id = setup.get("id")

        # 1. Action / Direction Resolution
        raw_direction = str(
            setup.get("direction") or setup.get("type") or setup.get("action") or setup.get("action_bias") or ""
        ).upper()
        status_upper = str(setup.get("status", "")).upper()

        # Guard: Check for non-actionable or neutral waiting states
        if "NEUTRAL" in raw_direction or "WAITING" in raw_direction or "FLAT" in raw_direction or "NO_TRADE" in raw_direction:
            log.info("[gemini_spark] Setup %s is %s (status=%s). Skipping order execution.", setup_id, raw_direction, status_upper)
            continue

        if "SHORT" in raw_direction or "SELL" in raw_direction or "BEARISH" in raw_direction:
            action_upper = "SELL"
        elif "LONG" in raw_direction or "BUY" in raw_direction or "BULLISH" in raw_direction:
            action_upper = "BUY"
        else:
            log.info("[gemini_spark] Setup %s has unhandled direction '%s'. Skipping.", setup_id, raw_direction)
            continue

        # 2. Entry Zone / Trigger Price Resolution
        entry_target = 0.0
        entry_zone = setup.get("entry_zone")
        if "entry_trigger" in setup and setup["entry_trigger"] is not None:
            try:
                entry_target = float(setup["entry_trigger"] or 0.0)
            except (ValueError, TypeError):
                entry_target = 0.0
        elif entry_zone is not None:
            ez = entry_zone
            if isinstance(ez, dict):
                try:
                    entry_target = float(ez.get("recommended") or ez.get("min") or 0.0)
                except (ValueError, TypeError):
                    entry_target = 0.0
            else:
                try:
                    entry_target = float(ez or 0.0)
                except (ValueError, TypeError):
                    entry_target = 0.0
        elif "trigger_zone" in setup and setup["trigger_zone"] is not None:
            try:
                entry_target = float(setup["trigger_zone"] or 0.0)
            except (ValueError, TypeError):
                entry_target = 0.0

        # 3. Stop-Loss Resolution
        stop_loss = 0.0
        for sl_key in (
            "active_stop_loss",
            "trailed_sl",
            "stop_loss",
            "sl",
            "stoploss",
            "invalidation",
            "sl_price",
            "stop",
            "active_sl",
        ):
            if sl_key in setup and setup[sl_key] is not None:
                try:
                    val = float(setup[sl_key] or 0.0)
                    if val > 0:
                        stop_loss = val
                        break
                except (ValueError, TypeError):
                    pass

        # 4. Target 1 and Target 2 Resolution
        target_1 = 0.0
        target_2 = 0.0
        for t1_key in ("active_target", "target_1", "target1", "t1", "target", "take_profit_1", "tp1", "t1_price"):
            if t1_key in setup and setup[t1_key] is not None:
                try:
                    val = float(setup[t1_key] or 0.0)
                    if val > 0:
                        target_1 = val
                        break
                except (ValueError, TypeError):
                    pass

        for t2_key in ("target_2", "target2", "t2", "take_profit_2", "tp2", "t2_price"):
            if t2_key in setup and setup[t2_key] is not None:
                try:
                    val = float(setup[t2_key] or 0.0)
                    if val > 0:
                        target_2 = val
                        break
                except (ValueError, TypeError):
                    pass

        if target_1 == 0.0 and "targets" in setup and isinstance(setup["targets"], list):
            targets_list = setup["targets"]
            if len(targets_list) > 0 and isinstance(targets_list[0], dict):
                target_1 = float(targets_list[0].get("price") or 0.0)
            if len(targets_list) > 1 and isinstance(targets_list[1], dict):
                target_2 = float(targets_list[1].get("price") or 0.0)

        # 5. Determine actual fill price / current price utilizing top-level CMP
        fill_price = float(cmp_val if cmp_val is not None else (entry_target or stop_loss))

        log.info(
            "[gemini_spark] Processing setup: %s | %s %s | CMP/Fill: %.2f (Zone/Trigger: %.2f) | SL: %.2f | T1: %.2f",
            setup_id, action_upper, symbol, fill_price, entry_target, stop_loss, target_1,
        )

        # Check if the setup is completed, closed, missed, or invalidated
        if _is_setup_inactive(setup):
            existing_trade = open_trades_map.get(str(setup_id))
            if existing_trade:
                existing_id = int(existing_trade["id"])
                
                # Check setup first, then reconciled records, then cmp_val
                reconciled = _extract_reconciled_trade_exit(payload, str(setup_id))
                rec_price = float(reconciled["exit_price"]) if (reconciled and reconciled.get("exit_price")) else None

                close_price = float(setup.get("exit_price") or (rec_price if rec_price is not None else (cmp_val if cmp_val is not None else (existing_trade.get("entry_underlying") or 0.0))))
                
                if reconciled and rec_price is not None:
                    close_status = reconciled["status"]
                    close_reason = reconciled["reason"]
                else:
                    if "TARGET" in status_upper or "PROFIT" in status_upper:
                        close_status = "CLOSED_TARGET"
                    elif "TRAILED" in status_upper:
                        # For trailed exits, if exit is better than entry for the side, it's CLOSED_TARGET
                        entry_u = float(existing_trade.get("entry_underlying") or 0.0)
                        trade_side = str(existing_trade.get("side", "")).upper()
                        is_profit = (close_price > entry_u) if trade_side == "BUY" else (close_price < entry_u)
                        close_status = "CLOSED_TARGET" if is_profit else "CLOSED_SL"
                    elif "SL" in status_upper or "STOP" in status_upper:
                        close_status = "CLOSED_SL"
                    else:
                        close_status = "CLOSED_AI_EXIT"
                    close_reason = f"GeminiAI Setup: {status_upper}"

                log.info("[gemini_spark] Closing open trade #%s (%s) due to setup status '%s' at %.2f (%s)", existing_id, setup_id, status_upper, close_price, close_status)
                close_paper_trade(
                    trade_id=existing_id,
                    closed_at=now_iso,
                    exit_underlying=close_price,
                    exit_premium=close_price,
                    status=close_status,
                    reason=close_reason,
                )
                push_execution_feedback_to_ledger("TRADE_CLOSED", {
                    "trade_id": existing_id,
                    "symbol": symbol,
                    "side": existing_trade.get("side"),
                    "fill_price": existing_trade.get("entry_underlying"),
                    "exit_price": close_price,
                    "sl_price": existing_trade.get("sl_underlying"),
                    "target_price": existing_trade.get("target_underlying"),
                    "lots": existing_trade.get("lots"),
                    "setup_id": setup_id,
                    "reason": close_reason,
                    "pnl": setup.get("total_realized_pnl_inr", 0.0),
                })
                sync_active_positions_snapshot()
                _send_telegram_notification(
                    f"🏁 *[GeminiAI] Paper Trade CLOSED*\n"
                    f"• Symbol: `{symbol}`\n"
                    f"• Trade ID: `#{existing_id}` (`{setup_id}`)\n"
                    f"• Exit Status: `{close_status}`\n"
                    f"• Exit Price: ₹{close_price:.2f}\n"
                    f"• Reason: {status_upper}"
                )
                open_trades_map.pop(str(setup_id), None)
            else:
                log.info("[gemini_spark] Setup %s is inactive/closed in feed (status=%s). Skipping execution.", setup_id, status_upper)
            continue

        existing_trade = open_trades_map.get(str(setup_id))
        if existing_trade:
            existing_id = int(existing_trade["id"])
            existing_signal_key = existing_trade.get("signal_key")
            trade_side = str(existing_trade.get("side", "")).upper()
            current_sl = float(existing_trade.get("sl_underlying") or 0.0)
            current_tgt = float(existing_trade.get("target_underlying") or 0.0)

            # Fix missing SL if database had 0.0 and feed has a valid stop loss
            if current_sl == 0.0 and stop_loss > 0:
                with get_conn() as conn:
                    conn.execute("UPDATE paper_trades SET sl_underlying = ? WHERE id = ?", (stop_loss, existing_id))
                current_sl = stop_loss
                existing_trade["sl_underlying"] = stop_loss
                log.info("[gemini_spark] Set missing SL for open trade #%d (%s) -> ₹%.2f", existing_id, setup_id, stop_loss)

            # Fix missing Target if database had 0.0
            if current_tgt == 0.0 and target_1 > 0:
                with get_conn() as conn:
                    conn.execute("UPDATE paper_trades SET target_underlying = ? WHERE id = ?", (target_1, existing_id))
                current_tgt = target_1
                existing_trade["target_underlying"] = target_1

            effective_sl = stop_loss if stop_loss > 0 else current_sl
            effective_tgt = target_1 if target_1 > 0 else current_tgt

            # Mechanical SL / Target breach check against live market price (fill_price / cmp_val)
            hit_sl = False
            hit_tgt = False
            if fill_price > 0:
                if trade_side == "BUY":
                    hit_sl = (effective_sl > 0 and fill_price <= effective_sl)
                    hit_tgt = (effective_tgt > 0 and fill_price >= effective_tgt)
                elif trade_side == "SELL":
                    hit_sl = (effective_sl > 0 and fill_price >= effective_sl)
                    hit_tgt = (effective_tgt > 0 and fill_price <= effective_tgt)

            if hit_sl or hit_tgt:
                close_status = "CLOSED_SL" if hit_sl else "CLOSED_TARGET"
                exit_price = effective_sl if hit_sl else effective_tgt
                if hit_sl and trade_side == "BUY" and fill_price < effective_sl:
                    exit_price = fill_price
                elif hit_sl and trade_side == "SELL" and fill_price > effective_sl:
                    exit_price = fill_price

                close_reason = f"GeminiAI: Mechanical {'SL' if hit_sl else 'Target'} hit at ₹{fill_price:.2f} (SL: ₹{effective_sl:.2f}, Tgt: ₹{effective_tgt:.2f})"
                log.info("[gemini_spark] Closing trade #%d (%s) due to mechanical exit at %.2f (%s)", existing_id, setup_id, exit_price, close_status)
                close_paper_trade(
                    trade_id=existing_id,
                    closed_at=now_iso,
                    exit_underlying=exit_price,
                    exit_premium=exit_price,
                    status=close_status,
                    reason=close_reason,
                )
                push_execution_feedback_to_ledger("TRADE_CLOSED", {
                    "trade_id": existing_id,
                    "symbol": symbol,
                    "side": trade_side,
                    "fill_price": existing_trade.get("entry_underlying"),
                    "exit_price": exit_price,
                    "sl_price": effective_sl,
                    "target_price": effective_tgt,
                    "lots": existing_trade.get("lots"),
                    "setup_id": setup_id,
                    "reason": close_reason,
                })
                sync_active_positions_snapshot()
                _send_telegram_notification(
                    f"🏁 *[GeminiAI] Paper Trade CLOSED (Mechanical {('SL' if hit_sl else 'Target')})*\n"
                    f"• Symbol: `{symbol}`\n"
                    f"• Trade ID: `#{existing_id}` (`{setup_id}`)\n"
                    f"• Exit Status: `{close_status}`\n"
                    f"• Exit Price: ₹{exit_price:.2f}\n"
                    f"• Reason: {close_reason}"
                )
                open_trades_map.pop(str(setup_id), None)
                continue

            # Trailed Stop-Loss update (when trade is not breached)
            sl_delta_threshold = 0.05 if symbol == "NATURALGAS" else 2.0
            is_valid_trail = False
            if trade_side == "BUY" and stop_loss > current_sl and stop_loss < fill_price:
                is_valid_trail = True
            elif trade_side == "SELL" and (current_sl == 0.0 or stop_loss < current_sl) and stop_loss > fill_price:
                is_valid_trail = True

            if is_valid_trail and abs(stop_loss - current_sl) > sl_delta_threshold:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE paper_trades SET sl_underlying = ?, reason = reason || ' | Trailed SL to ' || ? WHERE id = ?",
                        (stop_loss, str(stop_loss), existing_id),
                    )
                existing_trade["sl_underlying"] = stop_loss
                log.info("[gemini_spark] Trailed SL for %s trade #%d (%s) to ₹%.2f", symbol, existing_id, setup_id, stop_loss)
                push_execution_feedback_to_ledger("SL_TRAILED", {
                    "trade_id": existing_id,
                    "symbol": symbol,
                    "side": trade_side,
                    "fill_price": existing_trade.get("entry_underlying"),
                    "sl_price": stop_loss,
                    "target_price": existing_trade.get("target_underlying"),
                    "lots": existing_trade.get("lots"),
                    "setup_id": setup_id,
                    "reason": f"Trailed stop-loss updated by GeminiAI {setup_id}",
                })
                sync_active_positions_snapshot()
                _send_telegram_notification(
                    f"🛡️ *[GeminiAI] Trailed Stop-Loss Updated*\n"
                    f"• Symbol: `{symbol}`\n"
                    f"• Trade ID: `#{existing_id}` (`{setup_id}`)\n"
                    f"• Trailed SL: ₹{stop_loss:.2f}"
                )
            continue

        # Check if this setup was already executed in history (prevent repeat re-entry)
        sig_key = str(setup_id)
        with get_conn(read_only=True) as conn:
            prior_trade = conn.execute(
                "SELECT id, status FROM paper_trades WHERE signal_key LIKE ? OR reason LIKE ? LIMIT 1",
                (f"{sig_key}%", f"%{sig_key}%"),
            ).fetchone()
            if prior_trade:
                log.info(
                    "[gemini_spark] Setup %s was already executed in paper_trades (#%s, status=%s). Skipping duplicate re-entry.",
                    setup_id, prior_trade["id"], prior_trade["status"],
                )
                continue

        # 6. Directional Stop-Loss Validation Guard (Applies to NEW Entries ONLY)
        if stop_loss > 0:
            if action_upper == "BUY" and stop_loss >= fill_price:
                log.error(
                    "[gemini_spark] REJECTED %s setup %s: Inverted Stop-Loss for BUY! SL (%.2f) >= Fill (%.2f)",
                    symbol, setup_id, stop_loss, fill_price,
                )
                _send_telegram_notification(
                    f"⚠️ *[GeminiAI] REJECTED Setup `{setup_id}`*\n"
                    f"• Symbol: `{symbol}` (BUY)\n"
                    f"• Invalid SL: ₹{stop_loss:.2f} >= Fill ₹{fill_price:.2f}\n"
                    f"• Action: Execution aborted by engine safety guard."
                )
                continue
            elif action_upper == "SELL" and stop_loss <= fill_price:
                log.error(
                    "[gemini_spark] REJECTED %s setup %s: Inverted Stop-Loss for SELL! SL (%.2f) <= Fill (%.2f)",
                    symbol, setup_id, stop_loss, fill_price,
                )
                _send_telegram_notification(
                    f"⚠️ *[GeminiAI] REJECTED Setup `{setup_id}`*\n"
                    f"• Symbol: `{symbol}` (SELL)\n"
                    f"• Invalid SL: ₹{stop_loss:.2f} <= Fill ₹{fill_price:.2f}\n"
                    f"• Action: Execution aborted by engine safety guard."
                )
                continue

        # 7. Breakout / Trigger Condition Check for pending states (Applies to NEW Entries ONLY)
        if "WAITING" in status_upper or "PENDING" in status_upper:
            if entry_target > 0:
                if action_upper == "BUY" and fill_price < entry_target:
                    log.info("[gemini_spark] %s setup %s pending breakout (CMP %.2f < Trigger %.2f). Awaiting trigger.", symbol, setup_id, fill_price, entry_target)
                    continue
                elif action_upper == "SELL" and fill_price > entry_target:
                    log.info("[gemini_spark] %s setup %s pending breakdown (CMP %.2f > Trigger %.2f). Awaiting trigger.", symbol, setup_id, fill_price, entry_target)
                    continue

        # New Setup Execution: Autonomous paper trading, never blocked by open position caps (>5) or scan blockers
        capital = float(load_runtime_config().get("live_capital_per_trade_inr", 1000000))
        stop_dist = abs(fill_price - stop_loss) if stop_loss > 0 else (2.0 if symbol == "NATURALGAS" else 40.0)
        lots = calculate_spark_lot_size(symbol, capital, stop_dist)
        lot_size = LOT_SIZES.get(symbol, 65 if symbol == "NIFTY" else 1250)

        trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "expiry": expiry,
            "verdict_label": "GeminiAI",
            "side": action_upper,
            "option_type": "FUT",
            "strike": 0.0,
            "entry_underlying": fill_price,
            "entry_premium": fill_price,
            "sl_underlying": stop_loss,
            "target_underlying": target_1,
            "lots": lots,
            "lot_size": lot_size,
            "status": "OPEN",
            "reason": f"GeminiAI Setup: {setup_id}",
            "digest_id": f"gemini_{int(time.time())}",
            "trade_status": setup.get("status", "ACTIVE_RUNNER"),
            "setup_type": "GeminiAI",
            "decision_reason": f"GeminiAI Feed: Setup {setup_id}",
            "confidence_score": 90,
            "entry_quality_score": 90,
            "trend_alignment_score": 90,
            "regime_score": 90,
            "signal_key": sig_key,
            "regime": "GEMINI_SPARK",
            "underlying": fill_price,
            "entry_dev_pct": 0.0,
            "ai_model_name": "GeminiAI",
        }

        new_trade_id = insert_paper_trade(trade_data)
        if new_trade_id:
            log.info(
                "[gemini_spark] Successfully placed GeminiAI paper trade #%d | %s %s FUT at ₹%.2f (Zone: %.2f) | SL: ₹%.2f, Tgt: ₹%.2f (setup: %s)",
                new_trade_id, action_upper, symbol, fill_price, entry_target, stop_loss, target_1, setup_id,
            )
            push_execution_feedback_to_ledger("TRADE_OPENED", {
                "trade_id": new_trade_id,
                "symbol": symbol,
                "side": action_upper,
                "fill_price": fill_price,
                "sl_price": stop_loss,
                "target_price": target_1,
                "target_2": target_2,
                "lots": lots,
                "setup_id": setup_id,
                "reason": f"GeminiAI Setup {setup_id} executed",
            })
            sync_active_positions_snapshot()
            zone_desc = ""
            if isinstance(entry_zone, dict):
                zone_desc = f"• Zone: ₹{entry_zone.get('min', entry_target):.2f} - ₹{entry_zone.get('max', entry_target):.2f}\n"

            _send_telegram_notification(
                f"🤖 *[GeminiAI] Paper Trade OPENED*\n"
                f"• Contract: `{contract_str}`\n"
                f"• Setup ID: `{setup_id}`\n"
                f"• Side: *{action_upper}* FUT ({lots} lots @ ₹{fill_price:.2f})\n"
                f"{zone_desc}"
                f"• SL: ₹{stop_loss:.2f} | T1: ₹{target_1:.2f} | T2: ₹{target_2:.2f}"
            )
            trade_data["id"] = new_trade_id
            open_trades_map[str(setup_id)] = trade_data


def _send_telegram_notification(msg: str) -> None:
    """Helper to dispatch telegram alert safely without throwing exceptions."""
    try:
        from src.alerts.telegram_dispatcher import send_text
        send_text(msg)
    except Exception as e:
        log.debug("[gemini_spark] Telegram alert dispatch failed: %s", e)


def poll_and_process_feed(target_symbol: str = "NATURALGAS") -> bool:
    """
    Polling cycle for a specific symbol:
    1. Fetches JSON from Apps Script bridge
    2. Validates symbol integrity
    3. Compares timestamp
    4. If new timestamp, processes setups and persists state
    Returns True if a new signal was processed, False otherwise.
    """
    if not is_gemini_spark_feed_enabled():
        return False

    payload = fetch_latest_signal(target_symbol)
    if not payload:
        return False

    # Defensive symbol guard: Ensure bridge returned the requested asset
    raw_sym = str(payload.get("symbol", "")).upper()
    if target_symbol == "NIFTY" and "NIFTY" not in raw_sym:
        # Apps Script did not return NIFTY (e.g. script not updated yet)
        return False
    if target_symbol == "NATURALGAS" and "NATURALGAS" not in raw_sym:
        return False

    state = _load_state()
    last_ts = _get_symbol_last_ts(state, target_symbol)
    current_ts = payload.get("timestamp")

    has_open_trades = len(get_open_gemini_paper_trades_map(target_symbol)) > 0
    if (current_ts and current_ts != last_ts) or has_open_trades:
        if current_ts and current_ts != last_ts:
            log.info("[gemini_spark] New Gemini Spark scan detected for %s at %s!", target_symbol, current_ts)
        execute_gemini_setups(payload)
        if current_ts and current_ts != last_ts:
            _set_symbol_last_ts(state, target_symbol, current_ts, payload)
            _save_state(state)
        return True

    return False


def _worker_loop() -> None:
    """Background daemon worker polling all supported symbols periodically with adaptive cadence."""
    log.info("[gemini_spark] Multi-symbol feed worker loop started.")
    last_snapshot_sync = 0.0
    last_symbol_poll: dict[str, float] = {}

    while not _STOP_EVENT.is_set():
        now_time = time.time()
        for sym in SUPPORTED_FEED_SYMBOLS:
            try:
                poll_and_process_feed(sym)
                last_symbol_poll[sym] = now_time
            except Exception as e:
                log.error("[gemini_spark] Polling error in worker loop for %s: %s", sym, e)

        # Heartbeat: sync positions snapshot to 2-way ledger every 60 seconds
        if now_time - last_snapshot_sync >= 60.0:
            try:
                sync_active_positions_snapshot()
                last_snapshot_sync = now_time
            except Exception as e:
                log.debug("[gemini_spark] Heartbeat snapshot sync error: %s", e)

        interval = max(5, get_gemini_spark_poll_interval_sec())
        _STOP_EVENT.wait(timeout=float(interval))
    log.info("[gemini_spark] Feed worker loop stopped.")


def start_gemini_spark_worker() -> None:
    """Start the background daemon worker if not already running."""
    global _WORKER_THREAD, _STOP_EVENT
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        log.debug("[gemini_spark] Worker thread already active.")
        return

    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True, name="gemini-spark-feed-worker")
    _WORKER_THREAD.start()
    log.info("[gemini_spark] Background Gemini Spark Multi-Symbol Feed Worker dispatched.")


def stop_gemini_spark_worker() -> None:
    """Stop the background daemon worker."""
    global _WORKER_THREAD, _STOP_EVENT
    _STOP_EVENT.set()
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        _WORKER_THREAD.join(timeout=3.0)
    _WORKER_THREAD = None
    log.info("[gemini_spark] Gemini Spark Feed Worker stopped.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    print("Testing Gemini Spark Multi-Symbol Feed Ingestion...")
    for sym in SUPPORTED_FEED_SYMBOLS:
        res = poll_and_process_feed(sym)
        print(f"[{sym}] Poll completed. Processed new payload: {res}")
