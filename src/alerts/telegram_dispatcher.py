"""
Telegram Alert Dispatcher
Uses python-telegram-bot v21 (asyncio-based) via run_coroutine_threadsafe.
One message per alert with full context + signal interpretation.
"""
import asyncio
import json
import logging
import threading
import urllib.parse
import urllib.request
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone, timedelta
from telegram import Bot
from telegram.error import TelegramError
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.utils.formatting import safe_num, fmt_oi, fmt_pct

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Module-level event loop (runs in background thread)
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _start_loop():
    global _loop
    import sys
    if sys.platform == 'win32':
        _loop = asyncio.SelectorEventLoop()
    else:
        _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _ensure_loop():
    global _loop, _loop_thread
    if _loop is None or not _loop.is_running():
        _loop_thread = threading.Thread(target=_start_loop, daemon=True)
        _loop_thread.start()
        import time; time.sleep(0.2)   # let loop start


def _reset_loop():
    global _loop, _loop_thread
    try:
        if _loop and _loop.is_running():
            _loop.call_soon_threadsafe(_loop.stop)
    except Exception:
        pass
    _loop = None
    _loop_thread = None
    _ensure_loop()


# ── Message formatters ────────────────────────────────────────────────────

_EMOJI = {
    "OI_SPIKE":          "📈",
    "OI_UNWIND":         "📉",
    "BUILDUP_CLASSIFY":  "🏗️",
    "LTP_SPIKE":         "⚡",
    "PRICE_SPIKE":       "⚡",
    "PCR_EXTREME":       "🔴",
    "PCR_SHIFT":         "🔄",
    "PCR_VELOCITY":      "📐",
    "IV_SPIKE":          "🌋",
    "IV_CRUSH":          "🫸",
    "ATM_LEG_MOVE":      "🎭",
    "STRADDLE_PREMIUM":  "📦",
    "MAX_PAIN_SHIFT":    "🎯",
    "OI_WALL_SHIFT":     "🧱",
    "VOLUME_AGGRESSION": "💥",
    "OTM_UNUSUAL":       "🎪",
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
    "PCR_SHIFT":   "PCR shifted {pcr_delta:+.3f} in one bar — sentiment flip possible.",
    "PCR_VELOCITY": "PCR trending {direction} — {label}.",
    "IV_SPIKE":    "ATM {option_type} IV jumped {iv_delta:.1f}pts — event pricing / panic hedging.",
    "IV_CRUSH":    "ATM {option_type} IV dropped {iv_delta:.1f}pts — vol crush / event over.",
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
    atype   = alert["alert_type"]
    detail  = json.loads(alert.get("detail_json") or "{}")
    emoji   = _EMOJI.get(atype, "🔔")
    
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
            option_type    = detail.get("option_type", alert.get("option_type", "")),
            strike         = detail.get("strike", alert.get("strike", "")),
            direction      = detail.get("direction", ""),
            pct            = safe_num(detail.get("pct_change")),
            interpretation = detail.get("interpretation", ""),
            pcr_delta      = safe_num(detail.get("pcr_delta")),
            iv_delta       = safe_num(detail.get("iv_delta")),
            shift          = safe_num(detail.get("shift")),
            curr_max_pain  = safe_num(detail.get("curr_max_pain")),
            buildup_type   = detail.get("buildup_type", ""),
            label          = detail.get("label", ""),
            bias           = detail.get("bias", ""),
        )
    except Exception:
        interp = ""

    sev = alert.get("severity", "LOW")
    sev_badge = {"HIGH": "🔥", "MEDIUM": "⚠️", "LOW": "ℹ️"}.get(sev, "")

    lines = [
        f"{emoji} *{atype}* {sev_badge} | {ts}",
        f"Sym: `{alert['symbol']}`",
    ]

    if alert.get("strike"):
        lines[-1] += f" | Strike: `{alert['strike']}` {alert.get('option_type', '')}"

    # Compact body per type
    if atype in ("OI_SPIKE", "OI_UNWIND"):
        prev_oi  = safe_num(detail.get("prev_oi"))
        curr_oi  = safe_num(detail.get("curr_oi"))
        pct_chg  = safe_num(detail.get("pct_change"))
        curr_ltp = safe_num(detail.get("curr_ltp"))
        lines.append(f"OI: `{fmt_oi(prev_oi)}`→`{fmt_oi(curr_oi)}` ({fmt_pct(pct_chg)})")
        lines.append(f"LTP: `{curr_ltp:.2f}`")
    elif atype == "BUILDUP_CLASSIFY":
        btype  = detail.get("buildup_type", "")
        oi_p   = safe_num(detail.get("oi_pct"))
        ltp_p  = safe_num(detail.get("ltp_pct"))
        lines.append(f"Type: *{btype}*")
        lines.append(f"OI: {fmt_pct(oi_p)} | LTP: {fmt_pct(ltp_p)}")
    elif atype == "LTP_SPIKE":
        curr_ltp = safe_num(detail.get("curr_ltp"))
        pct_chg  = safe_num(detail.get("pct_change"))
        lines.append(f"LTP: `{curr_ltp:.2f}` ({fmt_pct(pct_chg)})")
    elif atype == "PRICE_SPIKE":
        curr_pr = safe_num(detail.get("curr_price"))
        pct_chg = safe_num(detail.get("pct_change"))
        lines.append(f"Spot: `{curr_pr:.2f}` ({fmt_pct(pct_chg)}) {detail.get('direction', '')}")
    elif atype in ("PCR_EXTREME", "PCR_SHIFT"):
        pcr   = detail.get("pcr", "N/A")
        delta = safe_num(detail.get("pcr_delta"))
        lines.append(f"PCR: `{pcr}` (Δ {delta:+.3f})")
    elif atype == "PCR_VELOCITY":
        slope = safe_num(detail.get("slope"))
        dire  = detail.get("direction", "")
        lines.append(f"Slope: {slope:+.4f}/scan ({dire})")
    elif atype == "IV_SPIKE":
        curr_iv  = safe_num(detail.get("curr_iv"))
        iv_delta = safe_num(detail.get("iv_delta"))
        lines.append(f"IV: `{curr_iv:.1f}%` (+{iv_delta:.1f}pts)")
    elif atype == "IV_CRUSH":
        curr_iv  = safe_num(detail.get("curr_iv"))
        iv_delta = safe_num(detail.get("iv_delta"))
        lines.append(f"IV: `{curr_iv:.1f}%` ({iv_delta:.1f}pts)")
    elif atype == "ATM_LEG_MOVE":
        ce_p = safe_num(detail.get("ce_pct"))
        pe_p = safe_num(detail.get("pe_pct"))
        bias = detail.get("bias", "")
        lines.append(f"CE: {fmt_pct(ce_p)} | PE: {fmt_pct(pe_p)}")
        lines.append(f"_{bias}_")
    elif atype == "STRADDLE_PREMIUM":
        curr_p = safe_num(detail.get("curr_premium"))
        pct_p  = safe_num(detail.get("pct_change"))
        lines.append(f"Premium: `{curr_p:.1f}` ({fmt_pct(pct_p)})")
    elif atype == "MAX_PAIN_SHIFT":
        curr_mp = safe_num(detail.get("curr_max_pain"))
        shift   = safe_num(detail.get("shift"))
        lines.append(f"MaxPain: `{curr_mp:.0f}` (Δ {shift:+.0f})")
    elif atype == "OI_WALL_SHIFT":
        chg = detail.get("changes", {})
        for side, v in chg.items():
            lines.append(f"{side.capitalize()} wall: `{v['prev']}`→`{v['curr']}`")
    elif atype == "VOLUME_AGGRESSION":
        ratio = safe_num(detail.get("ratio"))
        vol   = safe_num(detail.get("volume"))
        lines.append(f"Vol: `{fmt_oi(vol)}` | Ratio: `{ratio:.1f}`")
    elif atype == "OTM_UNUSUAL":
        pct_chg = safe_num(detail.get("pct_change"))
        curr_oi = safe_num(detail.get("curr_oi"))
        lines.append(f"OI: `{fmt_oi(curr_oi)}` ({fmt_pct(pct_chg)})")

    if interp:
        lines.append(f"_{interp}_")

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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        return "\"ok\":true" in body.replace(" ", "")
    except Exception:
        return False


def send_alert(alert: dict) -> bool:
    """Sync wrapper — safe to call from APScheduler thread. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        log.warning("Telegram not configured — alert suppressed: %s", alert["alert_type"])
        return False

    _ensure_loop()
    message = _format_message(alert)

    try:
        future = asyncio.run_coroutine_threadsafe(_send_async(message), _loop)
        future.result(timeout=35)
        log.info("Telegram sent: %s | %s", alert["symbol"], alert["alert_type"])
        return True
    except TelegramError as exc:
        log.error("Telegram API error: %s", exc)
        return False
    except FuturesTimeoutError:
        log.error("Telegram send_alert timed out; retrying once")
        try:
            future.cancel()
        except Exception:
            pass
        try:
            _reset_loop()
            future = asyncio.run_coroutine_threadsafe(_send_async(message), _loop)
            future.result(timeout=35)
            log.info("Telegram sent on retry: %s | %s", alert["symbol"], alert["alert_type"])
            return True
        except Exception:
            if _send_text_http_fallback(message):
                log.info("Telegram sent via HTTP fallback: %s | %s", alert["symbol"], alert["alert_type"])
                return True
            log.error("Telegram send_alert retry failed")
            return False
    except Exception as exc:
        log.error("Telegram unexpected error submitting alert: %s", exc)
        return False


def send_text(text: str) -> bool:
    """Sends raw text to Telegram."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        return False
    _ensure_loop()
    try:
        future = asyncio.run_coroutine_threadsafe(_send_async(text), _loop)
        future.result(timeout=35)
        first_line = (text or "").splitlines()[0][:90]
        log.info("Telegram sent text: %s", first_line)
        return True
    except TelegramError as exc:
        log.error("Telegram send_text API error: %s", exc)
        return False
    except FuturesTimeoutError:
        log.error("Telegram send_text timed out; retrying once")
        try:
            future.cancel()
        except Exception:
            pass
        try:
            _reset_loop()
            future = asyncio.run_coroutine_threadsafe(_send_async(text), _loop)
            future.result(timeout=35)
            first_line = (text or "").splitlines()[0][:90]
            log.info("Telegram sent text on retry: %s", first_line)
            return True
        except Exception:
            if _send_text_http_fallback(text):
                first_line = (text or "").splitlines()[0][:90]
                log.info("Telegram sent text via HTTP fallback: %s", first_line)
                return True
            log.error("Telegram send_text retry failed")
            return False
    except Exception as exc:
        log.error("Telegram send_text submit error: %s", exc)
        return False
