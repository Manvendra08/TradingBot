"""
Asynchronous News Sentiment Background Worker & Fast Gate Cache.
================================================================
Decouples news fetching/scoring from the critical scan pipeline tick.

Architecture:
- Background Worker: Periodically polls news (every 15 min by default),
  evaluates sentiment score (-1.0 to 1.0), and atomically persists to
  data/cache/news_sentiment.json.
- Fast Gate: Synchronous, in-memory reader that applies exponential time-decay
  and returns sentiment in < 0.1 ms without blocking the scan pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config.settings import DATA_DIR, WATCH_SYMBOLS, IST

log = logging.getLogger(__name__)

CACHE_DIR = Path(DATA_DIR) / "cache"
NEWS_CACHE_FILE = CACHE_DIR / "news_sentiment.json"
DEFAULT_POLL_INTERVAL_SECONDS = 60 * 60  # 60 minutes
RECENCY_THRESHOLD_SECONDS = 60 * 60  # 60 minutes
OMNIROUTER_NEWS_MODEL = "Claude/Antigravity"
OMNIROUTER_NEWS_TIMEOUT = 35.0

# In-memory fast cache
_LOCK = threading.Lock()
_IN_MEMORY_NEWS: dict[str, dict[str, Any]] = {}
_LAST_DISK_MTIME: float = 0.0
_LAST_EVALUATED_TS: float = 0.0
_WORKER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()


def get_last_news_eval_timestamp() -> float:
    """Return the most recent news evaluation timestamp across all cached entries."""
    global _LAST_EVALUATED_TS
    if _LAST_EVALUATED_TS > 0:
        return _LAST_EVALUATED_TS
    disk = _load_disk_cache()
    if disk:
        ts_values = [float(v.get("evaluated_timestamp", 0.0)) for v in disk.values() if isinstance(v, dict)]
        if ts_values:
            _LAST_EVALUATED_TS = max(ts_values)
            return _LAST_EVALUATED_TS
    return 0.0


def seconds_until_next_minute(target_minute: int = 55) -> float:
    """Calculate the number of seconds until the next occurrence of target_minute past the hour in IST."""
    now_dt = datetime.now(IST)
    target_dt = now_dt.replace(minute=target_minute, second=0, microsecond=0)
    if target_dt <= now_dt:
        # Move to next hour
        target_dt += timedelta(hours=1)
    diff = (target_dt - now_dt).total_seconds()
    return max(1.0, diff)


def _dir_label(score: float) -> str:
    if score >= 0.35:
        return "BULLISH"
    if score <= -0.35:
        return "BEARISH"
    return "MIXED"


def _extract_json_dict(text: str) -> dict | None:
    """Safely extract JSON object from LLM response text."""
    if not text:
        return None
    text = text.strip()
    # Strip <think>...</think> blocks from reasoning models
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return None


# Index symbols whose live price action is authoritative over news sentiment.
# Commodities (NATURALGAS etc.) are intentionally excluded: news legitimately
# drives commodity prices, and a 0.5% move is routine intraday noise there.
_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"})


def _headline_time_ist(published_at: Any) -> str:
    """Render a headline's published_at (ISO UTC) as 'HH:MM IST' for the prompt."""
    if not published_at:
        return "time n/a"
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%H:%M IST")
    except (ValueError, TypeError):
        return "time n/a"


def _get_market_context(symbol: str) -> dict[str, Any] | None:
    """Fetch latest underlying price + change vs previous session close.

    Returns None when no local price history exists. Used to ground the news
    sentiment LLM prompt in live price reality (headlines often lag intraday
    moves) and to power the news/price divergence guard.

    session_fresh=True means the latest price row was written today (IST),
    i.e. it reflects the currently running session.
    """
    try:
        from src.models.schema import get_read_conn

        now_ist = datetime.now(IST)
        if now_ist.weekday() >= 5:  # weekend — no fresh session data
            return None
        day_start_utc = (
            now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
            .isoformat()
        )

        with get_read_conn() as conn:
            latest = conn.execute(
                "SELECT fetched_at, price FROM underlying_price "
                "WHERE symbol = ? ORDER BY fetched_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if not latest:
                return None
            prev_row = conn.execute(
                "SELECT price FROM underlying_price "
                "WHERE symbol = ? AND fetched_at < ? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (symbol, day_start_utc),
            ).fetchone()

        price = float(latest["price"] or 0.0)
        if price <= 0:
            return None

        asof_str = str(latest["fetched_at"] or "")
        # fetched_at is stored as UTC ISO-8601; lexicographic compare is valid.
        session_fresh = bool(asof_str) and asof_str >= day_start_utc
        try:
            asof_dt = datetime.fromisoformat(asof_str.replace("Z", "+00:00"))
            if asof_dt.tzinfo is None:
                asof_dt = asof_dt.replace(tzinfo=timezone.utc)
            asof_ist = asof_dt.astimezone(IST).strftime("%d %b %H:%M IST")
        except (ValueError, TypeError):
            asof_ist = asof_str

        pct: float | None = None
        prev_close = float(prev_row["price"]) if prev_row and prev_row["price"] else None
        if prev_close and prev_close > 0:
            pct = round((price - prev_close) / prev_close * 100.0, 2)

        return {
            "price": price,
            "prev_close": prev_close,
            "pct_change": pct,
            "session_fresh": session_fresh,
            "asof_ist": asof_ist,
        }
    except Exception as exc:
        log.debug("[news_worker] Market context unavailable for %s: %s", symbol, exc)
        return None


def _news_jev_flag(name: str, default: str = "false") -> bool:
    """Read a news-worker feature flag from the environment."""
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _format_prompt_headlines(items: list[dict], limit: int = 6) -> list[str]:
    """Format headlines with IST publish times for sentiment prompts.

    Undated ICICIDirect commentary shows as 'time n/a' and is explicitly
    de-prioritized in the system prompts.
    """
    headlines: list[str] = []
    for idx, item in enumerate(items[:limit], 1):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        provider = (item.get("provider") or "").strip()
        time_str = _headline_time_ist(item.get("published_at"))
        tag = f"{provider}, {time_str}" if provider else time_str
        headlines.append(f"{idx}. [{tag}] {title}")
    return headlines


def _evaluate_sentiment_via_jev(
    symbol: str,
    items: list[dict],
    market_context: dict[str, Any] | None = None,
) -> tuple[float, str, str, str] | None:
    """TypeSafe System-1 (Jev) sentiment — fallback/shadow evaluator.

    Returns (score, direction, evaluator, reason) or None when Jev is
    unavailable (no TYPESAFE_API_KEY, no answer, or malformed response).
    Uses the same headline + live-market state as the OmniRouter prompt so
    shadow comparisons are apples-to-apples. Question schema mirrors the
    proven one in src/engine/jev_gate.py.
    """
    if not items:
        return None
    try:
        from config.settings import TYPESAFE_API_KEY

        if not TYPESAFE_API_KEY:
            return None
        from src.services.typesafe_client import evaluate_system_one

        headlines = _format_prompt_headlines(items)
        if not headlines:
            return None

        state_parts = [
            f"Asset: {symbol}",
            f"Current time: {datetime.now(IST).strftime('%a %d %b %Y %H:%M IST')}",
        ]
        if market_context and market_context.get("pct_change") is not None:
            pct = float(market_context["pct_change"])
            tape = "DOWN" if pct < 0 else ("UP" if pct > 0 else "FLAT")
            freshness = (
                "live session" if market_context.get("session_fresh") else "prior session data"
            )
            state_parts.append(
                f"Live market: {symbol} {float(market_context['price']):.2f} "
                f"({pct:+.2f}% vs prev close, {tape}; {freshness})"
            )
        state_parts.append("Headlines:\n" + "\n".join(headlines))
        state = "\n".join(state_parts)

        questions: dict[str, Any] = {
            "news_direction": {
                "type": "choice",
                "instructions": (
                    "What is the dominant short-term news sentiment for this asset "
                    "given the headlines and any live price action?"
                ),
                "criteria": {
                    "BULLISH": "Recent positive headlines aligned with (or leading) price action",
                    "BEARISH": "Recent negative headlines aligned with (or leading) price action",
                    "NEUTRAL": "Mixed, stale, or headline-vs-price conflicting evidence",
                },
            },
        }
        answers = evaluate_system_one(state, questions, timeout=3.0)
        if not answers:
            return None

        ans = answers.get("news_direction") or {}
        # ChoiceAnswer: {"type": "choice", "choice": "BEARISH", "confidence": 0.99}
        choice = str(ans.get("choice") or ans.get("value") or "").upper().strip()
        if choice not in ("BULLISH", "BEARISH", "NEUTRAL"):
            return None
        try:
            conf = float(ans.get("confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        score = conf if choice == "BULLISH" else (-conf if choice == "BEARISH" else 0.0)
        direction = "MIXED" if choice == "NEUTRAL" else choice
        return (
            round(score, 3),
            direction,
            "jev_system1",
            f"Jev System-1: {choice} (confidence {conf:.2f})",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("[news_worker] Jev sentiment unavailable for %s: %s", symbol, exc)
        return None


def _md_sanitize(text: str) -> str:
    """Strip legacy-Markdown metacharacters so LLM reasons cannot break
    Telegram parse_mode='Markdown' (Telegram has no escape in legacy v1)."""
    if not text:
        return ""
    return re.sub(r"[*_`\[\]]", "", str(text))


def _build_ab_telegram_message(entries: dict[str, dict[str, Any]]) -> str:
    """Build one consolidated OmniRouter-vs-Jev A/B message for Telegram.

    Returns '' when no symbol has a Jev verdict (shadow off / Jev unavailable),
    so callers can skip sending entirely.
    """
    rows: list[str] = []
    for sym in sorted(entries):
        e = entries[sym] or {}
        if e.get("jev_direction") is None:
            continue
        pct = e.get("market_pct_change")
        tape = f"{float(pct):+.2f}%" if isinstance(pct, (int, float)) else "n/a"
        agree = (
            "✅ AGREE"
            if e.get("jev_direction") == e.get("direction")
            else "❌ DISAGREE"
        )
        ev = str(e.get("evaluator") or "")
        if ev.startswith("omnirouter"):
            ev_label = "OmniRouter Claude"
        elif ev == "jev_system1":
            ev_label = "Jev"
        elif ev == "deterministic_regex":
            ev_label = "regex"
        else:
            ev_label = ev or "unknown"
        omni_reason = _md_sanitize(e.get("reason"))[:200]
        jev_reason = _md_sanitize(e.get("jev_reason"))[:200]
        rows.append(
            f"\n**{sym}** | tape {tape} | {agree}\n"
            f"• Omni: {e.get('direction', '?')} ({float(e.get('raw_score') or 0):+.2f}, {ev_label})\n"
            f"   {omni_reason}\n"
            f"• Jev: {e.get('jev_direction')} ({float(e.get('jev_score') or 0):+.2f})\n"
            f"   {jev_reason}"
        )
    if not rows:
        return ""
    now_str = datetime.now(IST).strftime("%d %b %H:%M IST")
    return (
        "**🤖 News AI A/B — OmniRouter vs Jev**\n"
        f"_{now_str}_"
        + "".join(rows)
    )


def _evaluate_sentiment_via_omnirouter(
    symbol: str,
    items: list[dict],
    fallback_score: float,
    market_context: dict[str, Any] | None = None,
) -> tuple[float, str, str, str]:
    """Call OmniRouter with Claude/Free combo to evaluate sentiment.

    Returns:
        (score, direction, evaluator, reason)
    """
    if not items:
        return (0.0, "MIXED", "empty", "No news headlines available")

    # Format the top headlines with IST publish times (shared with the Jev
    # fallback so shadow comparisons use identical state).
    headlines = _format_prompt_headlines(items)

    if not headlines:
        return (0.0, "MIXED", "empty", "No valid headline text")

    headlines_text = "\n".join(headlines)

    base_url = (
        os.environ.get("OMNIROUTER_BASE_URL") or "http://localhost:20128/v1"
    ).strip().rstrip("/").replace(":3000", ":20128")
    if not base_url.endswith("/chat/completions"):
        url = f"{base_url}/chat/completions"
    else:
        url = base_url

    api_key = os.environ.get("OMNIROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OMNIROUTER_API_KEY is not configured")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
        "x-omniroute-compression": "off",
    }

    system_prompt = (
        "You are an expert financial and commodity market sentiment analyst for algorithmic trading.\n"
        "Evaluate the net short-term price sentiment of the provided news headlines for the given asset.\n"
        "Headlines include publish times (IST). Weight recent headlines (last 1-2 hours during market "
        "hours) far more heavily than older ones; treat headlines marked 'time n/a' as undated "
        "commentary and weight them below timestamped headlines.\n"
        "If a 'Live market' line is provided and the headlines conflict with live price action, score "
        "toward the price action or return MIXED — news headlines routinely lag intraday moves, so a "
        "falling index must not be scored bullish on the strength of older positive headlines.\n"
        "Output ONLY a valid JSON object with exactly these fields:\n"
        '{"score": float between -1.0 (strongly bearish) and 1.0 (strongly bullish), '
        '"direction": "BULLISH" | "BEARISH" | "MIXED", '
        '"reason": "brief 1-sentence analytical reason"}'
    )

    prompt_lines = [
        f"Asset: {symbol}",
        f"Current time: {datetime.now(IST).strftime('%a %d %b %Y %H:%M IST')}",
    ]
    if market_context and market_context.get("pct_change") is not None:
        pct = float(market_context["pct_change"])
        tape = "DOWN" if pct < 0 else ("UP" if pct > 0 else "FLAT")
        freshness = (
            "live session" if market_context.get("session_fresh") else "prior session data"
        )
        prompt_lines.append(
            f"Live market: {symbol} {float(market_context['price']):.2f} "
            f"({pct:+.2f}% vs prev close, {tape}; {freshness}, "
            f"as of {market_context.get('asof_ist', 'n/a')})"
        )
    elif market_context and market_context.get("price"):
        prompt_lines.append(
            f"Live market: {symbol} {float(market_context['price']):.2f} "
            f"(no prev-close comparison; as of {market_context.get('asof_ist', 'n/a')})"
        )
    prompt_lines.append(f"Headlines:\n{headlines_text}")
    user_prompt = "\n".join(prompt_lines)

    json_payload = {
        "model": OMNIROUTER_NEWS_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }

    try:
        t0 = time.time()
        resp = requests.post(
            url,
            headers=headers,
            json=json_payload,
            timeout=OMNIROUTER_NEWS_TIMEOUT,
        )
        dt = time.time() - t0

        if resp.status_code == 200:
            resp_data = resp.json()
            choices = resp_data.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content") or ""
                if not content and "reasoning" in msg:
                    content = msg.get("reasoning") or ""
                parsed = _extract_json_dict(content)
                score_val = None
                if parsed:
                    score_val = parsed.get("score")
                    if score_val is None:
                        score_val = parsed.get("sentiment_score") or parsed.get("sentiment")

                if parsed and score_val is not None:
                    raw_score = float(score_val)
                    clamped_score = max(-1.0, min(1.0, raw_score))
                    raw_dir = str(parsed.get("direction", "")).upper().strip()
                    if raw_dir not in {"BULLISH", "BEARISH", "MIXED"}:
                        raw_dir = _dir_label(clamped_score)
                    reason = str(parsed.get("reason", "")).strip() or "OmniRouter Claude/Antigravity assessment"
                    log.info(
                        "[news_worker] OmniRouter %s evaluated %s: score=%.2f direction=%s in %.2fs (%s)",
                        OMNIROUTER_NEWS_MODEL, symbol, clamped_score, raw_dir, dt, reason[:60],
                    )
                    return (round(clamped_score, 3), raw_dir, "omnirouter_claude_antigravity", reason)
                else:
                    log.warning(
                        "[news_worker] OmniRouter %s returned unparseable content for %s (took %.2fs): %s",
                        OMNIROUTER_NEWS_MODEL, symbol, dt, content[:150],
                    )
        else:
            log.warning(
                "[news_worker] OmniRouter returned status %d for %s (took %.2fs). Falling back to regex.",
                resp.status_code, symbol, dt,
            )
    except Exception as exc:
        log.warning(
            "[news_worker] OmniRouter news evaluation failed for %s: %s. Falling back to regex.",
            symbol, exc,
        )

    # OmniRouter failed (HTTP error / timeout / unparseable) → try Jev
    # System-1 before degrading to the crude regex scorer. Flag-gated via
    # NEWS_JEV_FALLBACK (default on — it can only improve on regex, and is
    # skipped automatically when TYPESAFE_API_KEY is unset).
    if _news_jev_flag("NEWS_JEV_FALLBACK", default="true"):
        jev = _evaluate_sentiment_via_jev(symbol, items, market_context)
        if jev:
            log.info(
                "[news_worker] Jev System-1 fallback used for %s: score=%.2f direction=%s",
                symbol, jev[0], jev[1],
            )
            return jev

    return (
        round(fallback_score, 3),
        _dir_label(fallback_score),
        "deterministic_regex",
        "Fallback regex score",
    )


def _load_disk_cache() -> dict[str, dict[str, Any]]:
    """Load cached sentiment from disk if available."""
    if not NEWS_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(NEWS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning("[news_worker] Failed reading news cache file %s: %s", NEWS_CACHE_FILE, exc)
    return {}


def _save_disk_cache(cache_data: dict[str, dict[str, Any]]) -> None:
    """Atomically write cache to disk."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        content = json.dumps(cache_data, indent=2)
        tmp_file = NEWS_CACHE_FILE.with_suffix(f".{os.getpid()}_{threading.get_ident()}.tmp")
        tmp_file.write_text(content, encoding="utf-8")
        os.replace(tmp_file, NEWS_CACHE_FILE)
    except Exception as exc:
        log.error("[news_worker] Failed saving news cache file %s: %s", NEWS_CACHE_FILE, exc)


def evaluate_and_cache_news(
    symbols: list[str] | None = None,
    only_open_markets: bool = True,
) -> dict[str, dict[str, Any]]:
    """Poll news feeds for target symbols, score sentiment via OmniRouter Claude/Free, and update cache.
    Evaluates symbols concurrently via ThreadPoolExecutor to prevent cumulative queue delays.
    If only_open_markets is True, symbols whose respective markets are closed are skipped.
    """
    from concurrent.futures import ThreadPoolExecutor
    from src.fetchers.news_fetcher import fetch_news
    from config.symbol_classes import is_market_open_for_news

    targets = symbols or list(WATCH_SYMBOLS)
    if only_open_markets:
        now_ist = datetime.now(IST)
        open_targets = [s for s in targets if is_market_open_for_news(s, now_ist)]
        closed_targets = [s for s in targets if s not in open_targets]
        if closed_targets:
            log.info(
                "[news_worker] Respective market closed for %s at %s IST — skipping news fetch",
                ", ".join(closed_targets),
                now_ist.strftime("%H:%M:%S"),
            )
        if not open_targets:
            log.info(
                "[news_worker] All target markets closed at %s IST — news evaluation cycle skipped",
                now_ist.strftime("%H:%M:%S"),
            )
            return {}
        targets = open_targets

    updated: dict[str, dict[str, Any]] = {}
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    def _eval_single(sym_clean: str) -> tuple[str, dict[str, Any]] | None:
        try:
            res = fetch_news(sym_clean)
            if not isinstance(res, dict):
                return None

            regex_score = float(res.get("news_score_current", 0.0))
            regex_score = max(-1.0, min(1.0, regex_score))

            items = res.get("items", [])
            market_ctx = _get_market_context(sym_clean)
            score, direction, evaluator, reason = _evaluate_sentiment_via_omnirouter(
                sym_clean, items, regex_score, market_context=market_ctx
            )

            # News/price divergence guard (index symbols, live session only):
            # a verdict materially opposite to live price action is downgraded
            # to MIXED so stale headlines cannot greenlight trades against the
            # tape. Score is clamped inside the MIXED band because readers
            # (get_cached_news_sentiment) re-derive direction from raw_score.
            if (
                sym_clean in _INDEX_SYMBOLS
                and market_ctx
                and market_ctx.get("session_fresh")
                and market_ctx.get("pct_change") is not None
                and abs(float(market_ctx["pct_change"])) > 0.5
            ):
                pct = float(market_ctx["pct_change"])
                opposes = (pct <= -0.5 and direction == "BULLISH") or (
                    pct >= 0.5 and direction == "BEARISH"
                )
                if opposes:
                    log.warning(
                        "[news_worker] News/price divergence for %s: news=%s (score=%.2f) "
                        "vs price=%+.2f%% — downgrading to MIXED",
                        sym_clean, direction, score, pct,
                    )
                    score = max(-0.34, min(0.34, score))
                    direction = "MIXED"
                    reason = (
                        f"{reason} [downgraded to MIXED: live price {pct:+.2f}% "
                        f"contradicts news sentiment]"
                    )

            # Shadow A/B: evaluate Jev on identical state and stash both
            # verdicts in the cache entry so evaluate_and_cache_news() can
            # send one consolidated Telegram alert per cycle (replaces
            # grepping logs for 'Jev shadow').
            jev_score: float | None = None
            jev_direction: str | None = None
            jev_reason: str | None = None
            if _news_jev_flag("NEWS_JEV_SHADOW"):
                jev_res = _evaluate_sentiment_via_jev(sym_clean, items, market_ctx)
                if jev_res:
                    jev_score, jev_direction, _jev_ev, jev_reason = jev_res
                    agree = jev_direction == direction
                    log.info(
                        "[news_worker] Jev shadow %s: omni=%s(%.2f) jev=%s(%.2f) — %s",
                        sym_clean, direction, score, jev_direction, jev_score,
                        "AGREE" if agree else "DISAGREE",
                    )

            entry = {
                "symbol": sym_clean,
                "raw_score": score,
                "direction": direction,
                "evaluator": evaluator,
                "reason": reason,
                "jev_score": jev_score,
                "jev_direction": jev_direction,
                "jev_reason": jev_reason,
                "market_pct_change": market_ctx.get("pct_change") if market_ctx else None,
                "regex_fallback_score": round(regex_score, 3),
                "count_24h": int(res.get("count_24h", 0)),
                "news_score_day": float(res.get("news_score_day", score)),
                "evaluated_at": now_iso,
                "evaluated_timestamp": now_ts,
                "items": items[:10],
            }
            log.info(
                "[news_worker] Evaluated %s via %s: score=%.3f direction=%s articles=%d (%s)",
                sym_clean, evaluator, entry["raw_score"], entry["direction"], entry["count_24h"], reason[:50],
            )
            return sym_clean, entry
        except Exception as exc:
            log.warning("[news_worker] Failed evaluating news for %s: %s", sym_clean, exc)
            return None

    clean_targets = [s.upper().strip().split()[0] for s in targets]
    max_workers = min(4, max(1, len(clean_targets)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_eval_single, s) for s in clean_targets]
        for f in futures:
            res = f.result()
            if res:
                sym_clean, entry = res
                updated[sym_clean] = entry

    if updated:
        global _LAST_DISK_MTIME, _LAST_EVALUATED_TS
        with _LOCK:
            _IN_MEMORY_NEWS.update(updated)
            _LAST_EVALUATED_TS = now_ts
            # Merge with existing on-disk cache
            current_disk = _load_disk_cache()
            current_disk.update(_IN_MEMORY_NEWS)
            _save_disk_cache(current_disk)
            try:
                if NEWS_CACHE_FILE.exists():
                    _LAST_DISK_MTIME = os.path.getmtime(NEWS_CACHE_FILE)
            except Exception:
                pass

    # Consolidated shadow A/B alert: one Telegram message per evaluation cycle
    # showing BOTH OmniRouter and Jev verdicts (active only when
    # NEWS_JEV_SHADOW=true and at least one Jev verdict was produced).
    if updated and _news_jev_flag("NEWS_JEV_SHADOW"):
        try:
            ab_msg = _build_ab_telegram_message(updated)
            if ab_msg:
                from src.alerts.telegram_dispatcher import send_text
                send_text(ab_msg)
        except Exception as exc:
            log.debug("[news_worker] A/B Telegram alert failed: %s", exc)

    return updated


def get_cached_news_sentiment(
    symbol: str,
    half_life_minutes: float = 60.0,
    max_age_minutes: float = 240.0,
) -> dict[str, Any]:
    """Microsecond Fast Gate reader: returns decayed sentiment in < 0.1 ms.

    Computes exponential half-life decay:
        S_effective = S_raw * 0.5 ** (age_minutes / half_life_minutes)
    If age > max_age_minutes (4 hours default), sentiment decays to 0.0 (neutral).
    """
    sym = symbol.upper().strip().split()[0]

    global _LAST_DISK_MTIME
    data: dict[str, Any] | None = None

    # Multi-process sync: check disk cache file modification time
    try:
        if NEWS_CACHE_FILE.exists():
            mtime = os.path.getmtime(NEWS_CACHE_FILE)
            if mtime > _LAST_DISK_MTIME:
                disk_data = _load_disk_cache()
                if disk_data:
                    with _LOCK:
                        _IN_MEMORY_NEWS.update(disk_data)
                        _LAST_DISK_MTIME = mtime
    except Exception as m_exc:
        log.debug("[news_worker] Disk cache mtime check failed: %s", m_exc)

    with _LOCK:
        data = _IN_MEMORY_NEWS.get(sym)

    if data is None:
        disk_data = _load_disk_cache()
        if disk_data and sym in disk_data:
            with _LOCK:
                _IN_MEMORY_NEWS.update(disk_data)
                data = _IN_MEMORY_NEWS.get(sym)

    if not data:
        return {
            "items": [],
            "count_24h": 0,
            "current_news_direction": "MIXED",
            "news_score_current": 0.0,
            "news_score_day": 0.0,
            "evaluator": None,
            "reason": None,
            "age_minutes": None,
            "evaluated_at": None,
            "cached": True,
        }

    now_ts = time.time()
    eval_ts = float(data.get("evaluated_timestamp", now_ts))
    age_minutes = max(0.0, (now_ts - eval_ts) / 60.0)

    raw_score = float(data.get("raw_score", 0.0))
    if age_minutes > max_age_minutes:
        decay_factor = 0.0
    else:
        decay_factor = 0.5 ** (age_minutes / half_life_minutes)

    effective_score = round(raw_score * decay_factor, 3)
    direction = _dir_label(effective_score)

    return {
        "items": data.get("items", []),
        "count_24h": data.get("count_24h", 0),
        "current_news_direction": direction,
        "news_score_current": effective_score,
        "news_score_day": round(float(data.get("news_score_day", raw_score)), 3),
        "evaluator": data.get("evaluator", "omnirouter_claude_antigravity"),
        "reason": data.get("reason", ""),
        "age_minutes": round(age_minutes, 1),
        "evaluated_at": data.get("evaluated_at"),
        "cached": True,
    }


def ensure_news_worker_ready(max_age_seconds: int = RECENCY_THRESHOLD_SECONDS, force: bool = False) -> bool:
    """Pre-scan startup check: Run news worker before Symbols pipeline begins,
    but only if the last News scan completed more than 60 minutes ago.
    If the last scan was within the past 60 minutes, skip and return False.
    Also skips if all target markets are currently closed.
    """
    from config.symbol_classes import is_market_open_for_news
    now_ist = datetime.now(IST)
    if not force and not any(is_market_open_for_news(s, now_ist) for s in WATCH_SYMBOLS):
        log.info(
            "[news_worker] Pre-pipeline news check: all target markets currently closed at %s IST — skipping pre-pipeline news run.",
            now_ist.strftime("%H:%M:%S"),
        )
        return False

    last_eval = get_last_news_eval_timestamp()
    now_ts = time.time()
    age = now_ts - last_eval if last_eval > 0 else float("inf")

    if not force and age <= max_age_seconds:
        log.info(
            "[news_worker] Last news evaluation was %.1fm ago (<= %.0fm threshold) — skipping pre-pipeline news run.",
            age / 60.0, max_age_seconds / 60.0,
        )
        return False

    log.info(
        "[news_worker] Pre-pipeline news evaluation triggered (last run was %.1fm ago > %.0fm threshold)...",
        age / 60.0, max_age_seconds / 60.0,
    )
    res = evaluate_and_cache_news(only_open_markets=True)
    log.info("[news_worker] Pre-pipeline news evaluation finished (%d symbols updated).", len(res))
    return True


def _worker_loop() -> None:
    """Daemon thread loop executing hourly news evaluations at 55 minutes past each hour in IST.
    Enforces a 60-minute recency check before each run so it skips if already ran recently.
    Skips runs completely when all respective markets are closed.
    """
    log.info("[news_worker] Asynchronous News Sentiment Worker started (scheduled at :55 past each hour IST)")
    while not _STOP_EVENT.is_set():
        # Sleep until the next :55 minute boundary in IST
        wait_seconds = seconds_until_next_minute(55)
        next_run_dt = datetime.now(IST) + timedelta(seconds=wait_seconds)
        log.info(
            "[news_worker] Next scheduled news evaluation at %s IST (in %.1f minutes)",
            next_run_dt.strftime("%H:%M:%S"),
            wait_seconds / 60.0,
        )
        if _STOP_EVENT.wait(timeout=wait_seconds):
            break

        # Check recency before running scheduled job
        last_eval = get_last_news_eval_timestamp()
        now_ts = time.time()
        age = now_ts - last_eval if last_eval > 0 else float("inf")
        if age < RECENCY_THRESHOLD_SECONDS - 60:  # Allow 1-minute jitter
            log.info(
                "[news_worker] Scheduled run at :55 IST skipped — news was already evaluated %.1fm ago (< 60m)",
                age / 60.0,
            )
            continue

        try:
            now_ist = datetime.now(IST)
            from config.symbol_classes import is_market_open_for_news
            active_symbols = [s for s in WATCH_SYMBOLS if is_market_open_for_news(s, now_ist)]
            if not active_symbols:
                log.info(
                    "[news_worker] Scheduled run at %s IST skipped — all respective markets are currently closed",
                    now_ist.strftime("%H:%M:%S"),
                )
                continue

            log.info(
                "[news_worker] Scheduled news evaluation triggered at %s IST (:55 past the hour) for active markets: %s...",
                now_ist.strftime("%H:%M:%S"),
                active_symbols,
            )
            evaluate_and_cache_news(only_open_markets=True)
            log.info("[news_worker] Scheduled news evaluation finished.")
        except Exception as exc:
            log.exception("[news_worker] Unexpected error in news worker cycle: %s", exc)

    log.info("[news_worker] News Sentiment Worker stopped")


def start_news_worker() -> None:
    """Start the asynchronous news background worker daemon scheduled at :55 of each hour."""
    global _WORKER_THREAD
    with _LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            log.debug("[news_worker] Worker thread already running")
            return

        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="NewsSentimentWorker",
        )
        _WORKER_THREAD.start()
        log.info("[news_worker] Dispatched NewsSentimentWorker background thread")


def stop_news_worker() -> None:
    """Signal background worker to stop gracefully."""
    _STOP_EVENT.set()
