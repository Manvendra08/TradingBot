"""
TradingView Real-Time NYMEX Henry Hub Natural Gas WebSocket Streamer.
Zero-cost, sub-second live streaming for prompt Henry Hub futures (NYMEX:NG1!).
Provides atomic in-memory cache and fallback to yfinance on network disconnects.
"""

import json
import logging
import random
import re
import string
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket"
_DEFAULT_SYMBOL = "NYMEX:NG1!"

# Thread-safe global tick cache
_TICK_LOCK = threading.Lock()
_NYMEX_TICK_CACHE: dict[str, dict[str, Any]] = {}

_STREAMER_THREAD: threading.Thread | None = None
_STOP_STREAMER = threading.Event()


def _generate_session_id() -> str:
    return "qs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def _format_ws_msg(func: str, args: list) -> str:
    content = json.dumps({"m": func, "p": args})
    return f"~m~{len(content)}~m~{content}"


def _worker_loop() -> None:
    """Continuous background worker maintaining WebSocket connection to TradingView."""
    import websocket

    symbol = _DEFAULT_SYMBOL
    session_id = _generate_session_id()

    while not _STOP_STREAMER.is_set():
        ws = None
        try:
            log.info("[tv_nymex_streamer] Connecting to TradingView WebSocket: %s", _TV_WS_URL)
            ws = websocket.create_connection(
                _TV_WS_URL,
                header={"Origin": "https://www.tradingview.com"},
                timeout=12.0,
            )

            # 1. Authorize as public user (100% free, no API key required)
            ws.send(_format_ws_msg("set_auth_token", ["unauthorized_user_token"]))

            # 2. Register quote session
            ws.send(_format_ws_msg("quote_create_session", [session_id]))
            ws.send(
                _format_ws_msg(
                    "quote_set_fields",
                    [
                        session_id,
                        "lp",
                        "ch",
                        "chp",
                        "ask",
                        "bid",
                        "volume",
                        "open_price",
                        "high_price",
                        "low_price",
                    ],
                )
            )

            # 3. Subscribe to prompt Henry Hub symbol
            ws.send(_format_ws_msg("quote_add_symbols", [session_id, symbol]))
            log.info("[tv_nymex_streamer] Subscribed to %s streaming quotes", symbol)

            # 4. Message receive loop
            while not _STOP_STREAMER.is_set():
                raw = ws.recv()
                if not raw:
                    break

                # Parse TradingView ~m~len~m~ framed packets
                parts = re.split(r"~m~\d+~m~", raw)
                for part in parts:
                    chunk = part.strip()
                    if not chunk:
                        continue
                    if chunk.startswith("~h~"):
                        # Heartbeat reply
                        try:
                            ws.send(chunk)
                        except Exception:
                            pass
                        continue

                    try:
                        pkt = json.loads(chunk)
                        if pkt.get("m") == "qsd":
                            payload = pkt.get("p", [None, {}])
                            if len(payload) >= 2 and isinstance(payload[1], dict):
                                sym_name = payload[1].get("n", symbol)
                                values = payload[1].get("v", {})
                                if "lp" in values and values["lp"] is not None:
                                    lp_val = float(values["lp"])
                                    now_ts = time.time()
                                    with _TICK_LOCK:
                                        prev = _NYMEX_TICK_CACHE.get(sym_name, {})
                                        _NYMEX_TICK_CACHE[sym_name] = {
                                            "symbol": sym_name,
                                            "price": lp_val,
                                            "change": float(values.get("ch") or prev.get("change") or 0.0),
                                            "change_pct": float(values.get("chp") or prev.get("change_pct") or 0.0),
                                            "bid": float(values.get("bid") or prev.get("bid") or 0.0),
                                            "ask": float(values.get("ask") or prev.get("ask") or 0.0),
                                            "high": float(values.get("high_price") or prev.get("high") or 0.0),
                                            "low": float(values.get("low_price") or prev.get("low") or 0.0),
                                            "volume": float(values.get("volume") or prev.get("volume") or 0.0),
                                            "updated_at": now_ts,
                                            "source": "tradingview_ws",
                                        }
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass

        except Exception as exc:
            log.warning("[tv_nymex_streamer] WebSocket stream exception: %s. Reconnecting in 3s...", exc)
            time.sleep(3.0)
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            time.sleep(1.0)


def start_nymex_streamer() -> None:
    """Start the background TradingView NYMEX streaming daemon."""
    global _STREAMER_THREAD
    if _STREAMER_THREAD is not None and _STREAMER_THREAD.is_alive():
        return
    _STOP_STREAMER.clear()
    _STREAMER_THREAD = threading.Thread(
        target=_worker_loop,
        name="tv_nymex_streamer",
        daemon=True,
    )
    _STREAMER_THREAD.start()
    log.info("[tv_nymex_streamer] Daemon thread started")


def stop_nymex_streamer() -> None:
    """Stop the background TradingView NYMEX streaming daemon."""
    _STOP_STREAMER.set()


def get_live_nymex_quote(symbol: str = _DEFAULT_SYMBOL, max_age_sec: float = 30.0) -> dict[str, Any] | None:
    """
    Retrieve the latest sub-second live Henry Hub quote from cache.
    Auto-starts the streaming daemon if not already running.
    Falls back to yfinance if cache is missing or stale.
    """
    start_nymex_streamer()

    now_ts = time.time()
    with _TICK_LOCK:
        tick = _NYMEX_TICK_CACHE.get(symbol)
        if tick and (now_ts - tick.get("updated_at", 0.0) <= max_age_sec):
            return dict(tick)

    # Fallback to yfinance if stream is warming up or dropped
    try:
        import yfinance as yf
        ticker = yf.Ticker("NG=F")
        fi = getattr(ticker, "fast_info", None)
        last_price = getattr(fi, "last_price", None) if fi else None
        if last_price and float(last_price) > 0:
            fb_quote = {
                "symbol": symbol,
                "price": round(float(last_price), 4),
                "change": 0.0,
                "change_pct": 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0.0,
                "updated_at": now_ts,
                "source": "yfinance_fallback",
            }
            with _TICK_LOCK:
                _NYMEX_TICK_CACHE[symbol] = fb_quote
            return fb_quote
    except Exception as exc:
        log.warning("[tv_nymex_streamer] yfinance fallback failed for NG=F: %s", exc)

    return None
