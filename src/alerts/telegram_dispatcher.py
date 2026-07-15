"""
Telegram Alert Dispatcher
Uses python-telegram-bot v21 (asyncio-based) via run_coroutine_threadsafe.
One message per alert with full context + signal interpretation.
"""

import asyncio
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

from telegram import Bot
from telegram.error import TelegramError

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.utils.formatting import fmt_oi, fmt_pct, safe_num

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Module-level event loop (runs in background thread)
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


# ── Markdown escaping ────────────────────────────────────────────────────────

_MD_SPECIAL_CHARS = r"[_*[\]()~`>#+\-=|{}.!]"


def _escape_md(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    if not text:
        return ""
    return re.sub(_MD_SPECIAL_CHARS, r"\\\1", text)


def _escape_md_v1(text: str) -> str:
    """Escape Telegram Markdown (legacy) special characters: _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !"""
    if not text:
        return ""
    # Markdown (legacy) special chars: _ * [ ] ( ) ~ ` > # + - = | { } . !
    return re.sub(r"([_*\[\]()~`>#\+\-=|{}.!])", r"\\\1", text)


def _start_loop():
    global _loop
    import sys

    if sys.platform == "win32":
        _loop = asyncio.SelectorEventLoop()
    else:
        _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _ensure_loop():
    """BUG-M01 FIX: Ensure the dedicated background event loop is running.
    
    Uses a lock to prevent race conditions when multiple threads call this
    simultaneously. The loop runs in a dedicated daemon thread, isolated from
    the main thread's asyncio context.
    """
    global _loop, _loop_thread
    if not hasattr(_ensure_loop, '_lock'):
        _ensure_loop._lock = threading.Lock()
    with _ensure_loop._lock:
        if _loop is None or not _loop.is_running():
            _loop_thread = threading.Thread(target=_start_loop, daemon=True)
            _loop_thread.start()
            import time
            time.sleep(0.2)  # let loop start


async def _cleanup_loop() -> None:
    """Cancels all pending tasks in the event loop and stops the loop."""
    try:
        current_task = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current_task]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass
    finally:
        try:
            asyncio.get_running_loop().stop()
        except Exception:
            pass


def _reset_loop():
    global _loop, _loop_thread
    if _loop and _loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(_cleanup_loop(), _loop)
            future.result(timeout=3.0)
        except Exception as e:
            log.warning(
                "Loop cleanup failed or timed out: %s. Stopping loop directly.", e
            )
            try:
                _loop.call_soon_threadsafe(_loop.stop)
            except Exception:
                pass

        if _loop_thread and _loop_thread.is_alive():
            _loop_thread.join(timeout=3.0)

        try:
            _loop.close()
        except Exception:
            pass

    _loop = None
    _loop_thread = None
    _ensure_loop()


# ── Message formatters ────────────────────────────────────────────────────

_EMOJI = {
    "OI_SPIKE": "📈",
    "OI_UNWIND": "📉",
    "BUILDUP_CLASSIFY": "🏗️",
    "LTP_SPIKE": "⚡",
    "PRICE_SPIKE": "⚡",
    "PCR_EXTREME": "🔴",
    "PCR_SHIFT": "🔄",
    "PCR_VELOCITY": "📐",
    "IV_SPIKE": "🌋",
    "IV_CRUSH": "🫸",
    "ATM_LEG_MOVE": "🎭",
    "STRADDLE_PREMIUM": "📦",
    "MAX_PAIN_SHIFT": "🎯",
    "OI_WALL_SHIFT": "🧱",
    "VOLUME_AGGRESSION": "💥",
    "OTM_UNUSUAL": "🎪",
}

_INTERPRETATIONS = {
    "OI_SPIKE": (
        "Fresh {option_type} build-up at {strike} — "
        "suggests {direction} positioning by smart money."
    ),
    "OI_UNWIND": (
        "{option_type} short-covering / long exit at {strike} — "
        "possible directional reversal or expiry pressure."
    ),
    "BUILDUP_CLASSIFY": "{buildup_type} at {strike} {option_type} — key directional signal.",
    "LTP_SPIKE": "ATM {option_type} LTP moved {pct:.1f}% in one scan — directional momentum signal.",
    "PRICE_SPIKE": (
        "Underlying moved {pct:.2f}% ({direction}) in one bar — "
        "watch for momentum continuation or reversal."
    ),
    "PCR_EXTREME": "{interpretation}",
    "PCR_SHIFT": "PCR shifted {pcr_delta:+.3f} in one bar — sentiment flip possible.",
    "PCR_VELOCITY": "PCR trending {direction} — {label}.",
    "IV_SPIKE": "ATM {option_type} IV jumped {iv_delta:.1f}pts — event pricing / panic hedging.",
    "IV_CRUSH": "ATM {option_type} IV dropped {iv_delta:.1f}pts — vol crush / event over.",
    "ATM_LEG_MOVE": "{bias}.",
    "STRADDLE_PREMIUM": "Straddle premium {direction} — {label}.",
    "MAX_PAIN_SHIFT": (
        "Max Pain moved {shift:+.0f} pts → {curr_max_pain:.0f}. "
        "Writers defending new level."
    ),
    "OI_WALL_SHIFT": "Major OI wall moved — support/resistance levels changed.",
    "VOLUME_AGGRESSION": "{label} at {strike} {option_type}.",
    "OTM_UNUSUAL": "Far-OTM {option_type} activity at {strike} — watch for tail hedge / speculation.",
}


def _format_message(alert: dict) -> str:
    atype = alert["alert_type"]
    detail = json.loads(alert.get("detail_json") or "{}")
    emoji = _EMOJI.get(atype, "🔔")

    # Force IST Timezone
    try:
        fired = alert.get("fired_at", "")
        if not fired:
            ts = datetime.now(IST).strftime("%H:%M") + " IST"
        else:
            # fromisoformat handles "2026-04-01T23:13:16+05:30" correctly
            ts_dt = datetime.fromisoformat(fired)
            # If naive, assume it was UTC if it came from fetched_at, or IST if from extension
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc).astimezone(IST)
            else:
                ts_dt = ts_dt.astimezone(IST)
            ts = ts_dt.strftime("%H:%M") + " IST"
    except Exception as e:
        log.warning("Timestamp parse fail: %s | %s", e, alert.get("fired_at"))
        ts = datetime.now(IST).strftime("%H:%M") + " IST"

    # Build interpretation string
    try:
        interp_template = _INTERPRETATIONS.get(atype, "")
        interp = interp_template.format(
            option_type=_escape_md_v1(
                str(detail.get("option_type", alert.get("option_type", "")))
            ),
            strike=_escape_md_v1(str(detail.get("strike", alert.get("strike", "")))),
            direction=_escape_md_v1(str(detail.get("direction", ""))),
            pct=safe_num(detail.get("pct_change")),
            interpretation=_escape_md_v1(str(detail.get("interpretation", ""))),
            pcr_delta=safe_num(detail.get("pcr_delta")),
            iv_delta=safe_num(detail.get("iv_delta")),
            shift=safe_num(detail.get("shift")),
            curr_max_pain=safe_num(detail.get("curr_max_pain")),
            buildup_type=_escape_md_v1(str(detail.get("buildup_type", ""))),
            label=_escape_md_v1(str(detail.get("label", ""))),
            bias=_escape_md_v1(str(detail.get("bias", ""))),
        )
    except Exception as exc:
        log.warning(
            "%s: interpretation format failed for alert_type=%s: %s",
            alert.get("symbol", "?"),
            atype,
            exc,
        )
        interp = ""

    sev = alert.get("severity", "LOW")
    sev_badge = {"HIGH": "🔥", "MEDIUM": "⚠️", "LOW": "ℹ️"}.get(sev, "")

    safe_symbol = _escape_md_v1(str(alert["symbol"]))
    safe_strike = _escape_md_v1(str(alert.get("strike", "")))
    safe_opt_type = _escape_md_v1(str(alert.get("option_type", "")))

    lines = [
        f"{emoji} *{_escape_md_v1(atype)}* {sev_badge} | {ts}",
        f"Sym: `{safe_symbol}`",
    ]

    if alert.get("strike"):
        lines[-1] += f" | Strike: `{safe_strike}` {safe_opt_type}"

    # Compact body per type
    if atype in ("OI_SPIKE", "OI_UNWIND"):
        prev_oi = safe_num(detail.get("prev_oi"))
        curr_oi = safe_num(detail.get("curr_oi"))
        pct_chg = safe_num(detail.get("pct_change"))
        curr_ltp = safe_num(detail.get("curr_ltp"))
        lines.append(
            f"OI: `{fmt_oi(prev_oi)}`→`{fmt_oi(curr_oi)}` ({fmt_pct(pct_chg)})"
        )
        lines.append(f"LTP: `{curr_ltp:.2f}`")
    elif atype == "BUILDUP_CLASSIFY":
        btype = _escape_md_v1(str(detail.get("buildup_type", "")))
        oi_p = safe_num(detail.get("oi_pct"))
        ltp_p = safe_num(detail.get("ltp_pct"))
        lines.append(f"Type: *{btype}*")
        lines.append(f"OI: {fmt_pct(oi_p)} | LTP: {fmt_pct(ltp_p)}")
    elif atype == "LTP_SPIKE":
        curr_ltp = safe_num(detail.get("curr_ltp"))
        pct_chg = safe_num(detail.get("pct_change"))
        lines.append(f"LTP: `{curr_ltp:.2f}` ({fmt_pct(pct_chg)})")
    elif atype == "PRICE_SPIKE":
        curr_pr = safe_num(detail.get("curr_price"))
        pct_chg = safe_num(detail.get("pct_change"))
        is_commodity = str(alert.get("symbol", "")).upper().split()[0] in {
            "NATURALGAS",
            "CRUDEOIL",
            "GOLD",
            "SILVER",
        }
        label = "Future" if is_commodity else "Spot"
        dire = _escape_md_v1(str(detail.get("direction", "")))
        lines.append(f"{label}: `{curr_pr:.2f}` ({fmt_pct(pct_chg)}) {dire}")
    elif atype in ("PCR_EXTREME", "PCR_SHIFT"):
        pcr = _escape_md_v1(str(detail.get("pcr", "N/A")))
        delta = safe_num(detail.get("pcr_delta"))
        lines.append(f"PCR: `{pcr}` (Δ {delta:+.3f})")
    elif atype == "PCR_VELOCITY":
        slope = safe_num(detail.get("slope"))
        dire = _escape_md_v1(str(detail.get("direction", "")))
        lines.append(f"Slope: {slope:+.4f}/scan ({dire})")
    elif atype == "IV_SPIKE":
        curr_iv = safe_num(detail.get("curr_iv"))
        iv_delta = safe_num(detail.get("iv_delta"))
        lines.append(f"IV: `{curr_iv:.1f}%` (+{iv_delta:.1f}pts)")
    elif atype == "IV_CRUSH":
        curr_iv = safe_num(detail.get("curr_iv"))
        iv_delta = safe_num(detail.get("iv_delta"))
        lines.append(f"IV: `{curr_iv:.1f}%` ({iv_delta:.1f}pts)")
    elif atype == "ATM_LEG_MOVE":
        ce_p = safe_num(detail.get("ce_pct"))
        pe_p = safe_num(detail.get("pe_pct"))
        bias = _escape_md_v1(str(detail.get("bias", "")))
        lines.append(f"CE: {fmt_pct(ce_p)} | PE: {fmt_pct(pe_p)}")
        lines.append(f"_{bias}_")
    elif atype == "STRADDLE_PREMIUM":
        curr_p = safe_num(detail.get("curr_premium"))
        pct_p = safe_num(detail.get("pct_change"))
        lines.append(f"Premium: `{curr_p:.1f}` ({fmt_pct(pct_p)})")
    elif atype == "MAX_PAIN_SHIFT":
        curr_mp = safe_num(detail.get("curr_max_pain"))
        shift = safe_num(detail.get("shift"))
        lines.append(f"MaxPain: `{curr_mp:.0f}` (Δ {shift:+.0f})")
    elif atype == "OI_WALL_SHIFT":
        chg = detail.get("changes", {})
        for side, v in chg.items():
            prev_v = _escape_md_v1(str(v.get("prev", "")))
            curr_v = _escape_md_v1(str(v.get("curr", "")))
            lines.append(f"{side.capitalize()} wall: `{prev_v}`→`{curr_v}`")
    elif atype == "VOLUME_AGGRESSION":
        ratio = safe_num(detail.get("ratio"))
        vol = safe_num(detail.get("volume"))
        lines.append(f"Vol: `{fmt_oi(vol)}` | Ratio: `{ratio:.1f}`")
    elif atype == "OTM_UNUSUAL":
        pct_chg = safe_num(detail.get("pct_change"))
        curr_oi = safe_num(detail.get("curr_oi"))
        lines.append(f"OI: `{fmt_oi(curr_oi)}` ({fmt_pct(pct_chg)})")

    if interp:
        lines.append(f"_{_escape_md_v1(interp)}_")

    return "\n".join(lines)


# ── Dispatcher ────────────────────────────────────────────────────────────


async def _send_async(message: str) -> None:
    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )


def _send_text_http_fallback(text: str, timeout_seconds: int = 15) -> bool:
    """Direct Telegram HTTP fallback if asyncio loop path times out."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    def _try_request(parse_mode: str = "Markdown") -> tuple[bool, str]:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data_dict = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true",
            }
            if parse_mode:
                data_dict["parse_mode"] = parse_mode
            payload = urllib.parse.urlencode(data_dict).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            is_ok = '"ok":true' in body.replace(" ", "")
            return is_ok, body
        except Exception as e:
            return False, str(e)

    success, body = _try_request("Markdown")
    if not success:
        # If it failed due to markdown entity parsing, retry without parsing
        if "parse" in body.lower() or "entity" in body.lower() or "bad request" in body.lower():
            log.warning("HTTP fallback Markdown failed: %s. Retrying in plain text...", body)
            success, body = _try_request(None)
    return success


_tg_bot: Bot | None = None


def _get_tg_bot() -> Bot:
    """Return a reused Bot instance to avoid repeated getMe calls on every send."""
    global _tg_bot
    if _tg_bot is None:
        _tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _tg_bot


async def _send_async_safe(message: str, symbol: str = None, atype: str = None) -> None:
    try:
        bot = _get_tg_bot()
        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode="Markdown",
                ),
                timeout=10.0,
            )
        except TelegramError as tg_err:
            err_msg = str(tg_err).lower()
            if "can't parse" in err_msg or "entity" in err_msg or "markdown" in err_msg:
                log.warning("Telegram Markdown parse failed: %s. Retrying in plain text...", tg_err)
                await asyncio.wait_for(
                    bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=message,
                    ),
                    timeout=10.0,
                )
            else:
                raise

        # Successful send (direct path) -> stamp OK
        try:
            from src.models.schema import stamp_health
            stamp_health("telegram_send", "OK", "sent successfully")
        except Exception:
            pass

        if symbol and atype:
            log.info("Telegram sent (bg): %s | %s", symbol, atype)
        else:
            first_line = (message or "").splitlines()[0][:90] if message else ""
            log.info("Telegram sent text (bg): %s", first_line)
    except Exception as exc:
        log.warning(
            "Telegram async send failed: %s; trying HTTP fallback in background", exc
        )
        try:
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(
                None,
                _send_text_http_fallback,
                message,
                10,  # timeout_seconds
            )
            if success:
                # Fallback succeeded -> stamp OK
                try:
                    from src.models.schema import stamp_health
                    stamp_health("telegram_send", "OK", "sent via HTTP fallback")
                except Exception:
                    pass

                if symbol and atype:
                    log.info(
                        "Telegram sent via HTTP fallback (bg): %s | %s", symbol, atype
                    )
                else:
                    first_line = (message or "").splitlines()[0][:90] if message else ""
                    log.info(
                        "Telegram sent text via HTTP fallback (bg): %s", first_line
                    )
            else:
                log.error("Telegram HTTP fallback failed in bg")
                # Both direct and fallback failed -> stamp DOWN
                try:
                    from src.models.schema import stamp_health
                    stamp_health("telegram_send", "DOWN", f"async+http failed: {str(exc)[:80]}")
                except Exception:
                    pass
        except Exception as e:
            log.error("Telegram HTTP fallback exception in bg: %s", e)
            try:
                from src.models.schema import stamp_health
                stamp_health("telegram_send", "DOWN", f"exception: {str(e)[:80]}")
            except Exception:
                pass


def send_alert(alert: dict) -> bool:
    """Sync wrapper — safe to call from APScheduler thread. Returns True on success."""
    message = _format_message(alert)
    tg_queued = False

    # Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN":
        _ensure_loop()
        try:
            asyncio.run_coroutine_threadsafe(
                _send_async_safe(message, alert.get("symbol"), alert.get("alert_type")),
                _loop,
            )
            tg_queued = True
        except Exception as exc:
            log.error("Telegram unexpected error queueing alert: %s", exc)

    if not tg_queued:
        log.warning(
            "Telegram not configured — alert suppressed: %s",
            alert.get("alert_type"),
        )
        return False

    return True


def send_text(text: str) -> bool:
    """Sends raw text to Telegram in background."""
    tg_queued = False

    # Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN":
        _ensure_loop()
        try:
            asyncio.run_coroutine_threadsafe(_send_async_safe(text), _loop)
            tg_queued = True
        except Exception as exc:
            log.error("Telegram unexpected error queueing text: %s", exc)

    return tg_queued


# ── ADR-007 §3 A3: Async LLM enrichment — message editing ──────────────────

async def _edit_message_text_async(message_id: int, text: str) -> bool:
    """Edit an existing Telegram message by message_id."""
    try:
        bot = _get_tg_bot()
        await asyncio.wait_for(
            bot.edit_message_text(
                chat_id=TELEGRAM_CHAT_ID,
                message_id=message_id,
                text=text,
                parse_mode="Markdown",
            ),
            timeout=10.0,
        )
        return True
    except TelegramError as tg_err:
        err_msg = str(tg_err).lower()
        if "can't parse" in err_msg or "entity" in err_msg or "markdown" in err_msg:
            try:
                await asyncio.wait_for(
                    bot.edit_message_text(
                        chat_id=TELEGRAM_CHAT_ID,
                        message_id=message_id,
                        text=text,
                    ),
                    timeout=10.0,
                )
                return True
            except Exception:
                pass
        log.debug("Telegram edit_message failed: %s", tg_err)
        return False
    except Exception as exc:
        log.debug("Telegram edit_message unexpected error: %s", exc)
        return False


def send_text_and_return_id(text: str) -> int | None:
    """Send a message and return its message_id for later editing. Returns None on failure."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        return None
    _ensure_loop()
    try:
        async def _send_and_get_id():
            bot = _get_tg_bot()
            try:
                msg = await asyncio.wait_for(
                    bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode="Markdown",
                    ),
                    timeout=10.0,
                )
                return msg.message_id
            except TelegramError as tg_err:
                err_msg = str(tg_err).lower()
                if "can't parse" in err_msg or "entity" in err_msg or "markdown" in err_msg:
                    log.warning("Telegram send_and_return_id Markdown parse failed: %s. Retrying in plain text...", tg_err)
                    msg = await asyncio.wait_for(
                        bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=text,
                        ),
                        timeout=10.0,
                    )
                    return msg.message_id
                else:
                    raise

        future = asyncio.run_coroutine_threadsafe(_send_and_get_id(), _loop)
        return future.result(timeout=15.0)
    except Exception as exc:
        log.warning("Telegram send_and_return_id failed: %s | type: %s", exc, type(exc).__name__)
        return None


def edit_message_text(message_id: int, text: str) -> bool:
    """Edit an existing Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        return False
    _ensure_loop()
    try:
        future = asyncio.run_coroutine_threadsafe(
            _edit_message_text_async(message_id, text), _loop
        )
        return future.result(timeout=15.0)
    except Exception as exc:
        log.warning("Telegram edit_message_text failed: %s | type: %s", exc, type(exc).__name__)
        return False
