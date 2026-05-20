"""
Moneycontrol Option Chain Fetcher — Playwright-based (CE+PE stitched).

Moneycontrol returns 403 to plain requests; requires full browser rendering.
Two URLs are fetched concurrently (optyp=CE, optyp=PE) and merged by strike price.

Note: IV and Greeks are NOT available on Moneycontrol — those fields will be None.
This fetcher is a fallback; Dhan headless is primary for full data.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import date, datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Moneycontrol commodity symbol map
_MC_SYMBOL_MAP: dict[str, str] = {
    "NATURALGAS": "naturalgas",
    "CRUDEOIL": "crudeoil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_MC_BASE = "https://www.moneycontrol.com/commodity/option-chain/{sym}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.moneycontrol.com/",
}

_PW_LOCK = threading.Lock()


def _nearest_thursday() -> str:
    """Return next MCX expiry (nearest Thursday) as YYYY-MM-DD."""
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7  # Thursday = weekday 3
    if days_ahead == 0:
        days_ahead = 7
    exp = today + timedelta(days=days_ahead)
    return exp.strftime("%Y-%m-%d")


def _parse_number(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.strip())
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(text: str) -> Optional[int]:
    val = _parse_number(text)
    return int(val) if val is not None else None


async def _fetch_side_async(sym_slug: str, expiry: str, opt_type: str) -> list[dict]:
    """Fetch CE or PE rows from Moneycontrol using Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("[mc] playwright not installed — pip install playwright && playwright install chromium")
        return []

    url = (
        f"{_MC_BASE.format(sym=sym_slug)}"
        f"?exchange=mcx&exp={expiry}&optyp={opt_type}"
    )
    rows: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            extra_http_headers={
                "Accept-Language": _HEADERS["Accept-Language"],
                "Referer": _HEADERS["Referer"],
            },
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            # Wait for the option chain table
            await page.wait_for_selector(
                "table.mctable1, table#opttbldata, .opt-chain-tbl table",
                timeout=20_000,
            )
        except Exception as exc:
            log.warning("[mc] page load failed (%s %s): %s", opt_type, expiry, exc)
            await browser.close()
            return []

        # Grab all table rows
        trs = await page.query_selector_all(
            "table.mctable1 tr, table#opttbldata tr, .opt-chain-tbl table tr"
        )

        for tr in trs:
            cells = await tr.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [await c.inner_text() for c in cells]

            # Moneycontrol CE layout: OI | Chg in OI | Volume | LTP | Strike | ...
            # PE layout (mirrored): Strike | LTP | Volume | Chg in OI | OI
            # We normalise both to extract strike + LTP + OI + volume
            if opt_type == "CE":
                # cols: [OI, ChgOI, Vol, LTP, Strike, ...]
                strike = _parse_number(texts[4] if len(texts) > 4 else "")
                ltp = _parse_number(texts[3] if len(texts) > 3 else "")
                oi = _parse_int(texts[0] if len(texts) > 0 else "")
                oi_chg = _parse_int(texts[1] if len(texts) > 1 else "")
                volume = _parse_int(texts[2] if len(texts) > 2 else "")
                bid = _parse_number(texts[5] if len(texts) > 5 else "")
                ask = _parse_number(texts[6] if len(texts) > 6 else "")
            else:
                # PE: [Strike, LTP, Vol, ChgOI, OI] — reversed order
                strike = _parse_number(texts[0] if len(texts) > 0 else "")
                ltp = _parse_number(texts[1] if len(texts) > 1 else "")
                volume = _parse_int(texts[2] if len(texts) > 2 else "")
                oi_chg = _parse_int(texts[3] if len(texts) > 3 else "")
                oi = _parse_int(texts[4] if len(texts) > 4 else "")
                bid = _parse_number(texts[5] if len(texts) > 5 else "")
                ask = _parse_number(texts[6] if len(texts) > 6 else "")

            if strike is None:
                continue

            rows.append({
                "strike": strike,
                "option_type": opt_type,
                "ltp": ltp or 0.0,
                "oi": oi or 0,
                "oi_change": oi_chg or 0,
                "volume": volume or 0,
                "iv": None,      # MC does not provide IV
                "bid": bid,
                "ask": ask,
            })

        await browser.close()

    log.info("[mc] fetched %d %s rows for expiry %s", len(rows), opt_type, expiry)
    return rows


def _fetch_side_sync(sym_slug: str, expiry: str, opt_type: str) -> list[dict]:
    """Run async fetch in a new event loop (thread-safe)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch_side_async(sym_slug, expiry, opt_type))
    finally:
        loop.close()


def _fetch_underlying_price(sym_slug: str) -> Optional[float]:
    """Try to extract underlying spot/futures price from page title or summary."""
    # Lightweight approach: reuse cached page or skip — router provides fallback
    return None


class MoneycontrolFetcher:
    """Playwright-based Moneycontrol option chain fetcher (MCX commodities)."""

    name = "moneycontrol"

    def fetch_option_chain(self, symbol: str) -> dict | None:
        base = symbol.upper().split()[0]
        slug = _MC_SYMBOL_MAP.get(base)
        if not slug:
            log.warning("[mc] unsupported symbol: %s", symbol)
            return None

        expiry = _nearest_thursday()

        # Fetch CE and PE concurrently using threads (avoids nested event loops)
        ce_rows: list[dict] = []
        pe_rows: list[dict] = []

        def _fetch_ce():
            nonlocal ce_rows
            ce_rows = _fetch_side_sync(slug, expiry, "CE")

        def _fetch_pe():
            nonlocal pe_rows
            pe_rows = _fetch_side_sync(slug, expiry, "PE")

        t_ce = threading.Thread(target=_fetch_ce, daemon=True)
        t_pe = threading.Thread(target=_fetch_pe, daemon=True)
        t_ce.start()
        t_pe.start()
        t_ce.join(timeout=90)
        t_pe.join(timeout=90)

        all_strikes = ce_rows + pe_rows
        if not all_strikes:
            log.error("[mc] no data for %s expiry %s", symbol, expiry)
            return None

        # Deduplicate and sort by (strike, option_type)
        seen: set[tuple] = set()
        unique: list[dict] = []
        for row in all_strikes:
            key = (row["strike"], row["option_type"])
            if key not in seen:
                seen.add(key)
                unique.append(row)

        unique.sort(key=lambda r: (r["strike"], r["option_type"]))

        return {
            "symbol": base,
            "underlying_price": None,  # MC scrape doesn't trivially expose this
            "expiry": expiry,
            "strikes": unique,
            "source": self.name,
            "fetched_at": datetime.now(IST).isoformat(),
        }
