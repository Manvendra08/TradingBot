"""
Dhan public commodity option chain scraper (HTML, no auth).

This fetcher first attempts to parse embedded Next.js `__NEXT_DATA__` JSON
from the public Dhan commodity option-chain page and converts it into the
standard strike list. If that fails, it falls back to an HTML table parser.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from config.settings import HTTP_TIMEOUT_SECONDS, HTTP_MAX_RETRIES, HTTP_BACKOFF_FACTOR
from src.utils.dhan_resolver import get_dhan_security_id
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_BASE_URL = "https://dhan.co/commodity/{slug}-option-chain/"
_SCANX_OPTCHAIN_URL = "https://open-web-scanx.dhan.co/scanx/optchainactive"
_DHAN_BUILTUP_URL = "https://openweb-ticks.dhan.co/builtup"
_JULIAN_1980_BASE = datetime(1980, 1, 1, tzinfo=timezone.utc)

_SYMBOL_SLUGS: dict[str, str] = {
    "NATURALGAS": "natural-gas",
    "CRUDEOIL": "crude-oil",
    "GOLD": "gold",
    "SILVER": "silver",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_JSON_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://dhan.co",
    "Referer": "https://dhan.co/",
}

_BUILTUP_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://dhan.co",
    "Referer": "https://dhan.co/",
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def _parse_float(value: str) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"[-+]?\d*\.?\d+", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _parse_percent(value: str) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"\((-?\d*\.?\d+)\s*%\)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _strip_percent(value: str) -> str:
    return re.sub(r"\([^)]*%\)", "", str(value) or "").strip()


def _parse_compact_number(value: str) -> Optional[int]:
    if not value:
        return None
    cleaned = _clean_text(str(value)).upper().replace(",", "")
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)([A-Z]{0,3})$", cleaned)
    if not m:
        try:
            return int(float(cleaned))
        except Exception:
            return None
    num = float(m.group(1))
    suffix = m.group(2)
    factor = 1
    if suffix == "K":
        factor = 1_000
    elif suffix in {"M"}:
        factor = 1_000_000
    elif suffix in {"B"}:
        factor = 1_000_000_000
    elif suffix in {"L", "LAC", "LAKH"}:
        factor = 100_000
    elif suffix in {"CR", "CRORE"}:
        factor = 10_000_000
    return int(num * factor)


def _parse_int(value: Any) -> Optional[int]:
    f = _parse_float(str(value) if value is not None else "")
    if f is None:
        return None
    try:
        return int(round(f))
    except Exception:
        return None


def _extract_expiry_iso(page_text: str) -> str:
    m = re.search(r"Expiry\s+on\s+(\d{1,2})\s+([A-Za-z]{3,9})", page_text, re.I)
    if not m:
        return ""
    day = int(m.group(1))
    mon = m.group(2)[:3].title()
    try:
        month = datetime.strptime(mon, "%b").month
    except Exception:
        return ""
    today = datetime.now(IST).date()
    year = today.year
    if month < today.month - 1:
        year += 1
    try:
        expiry = datetime(year, month, day).date()
    except Exception:
        return ""
    return expiry.strftime("%Y-%m-%d")


def _extract_underlying(page_text: str, symbol: str) -> Optional[float]:
    if not page_text:
        return None
    pattern = rf"{re.escape(symbol)}\s+.*?(\d{{1,6}}\.?\d{{0,2}})"
    m = re.search(pattern, page_text, re.I | re.S)
    if not m:
        return None
    return _parse_float(m.group(1))


def _extract_next_data(html: str) -> dict | None:
    if not html:
        return None
    m = re.search(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", html, re.I | re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            return None
    m2 = re.search(r"window\.__NEXT_DATA__\s*=\s*({.*?})\s*;", html, re.I | re.S)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            return None
    return None


def _extract_page_props(next_data: dict | None) -> dict:
    if not isinstance(next_data, dict):
        return {}
    props = next_data.get("props")
    if not isinstance(props, dict):
        return {}
    page_props = props.get("pageProps")
    return page_props if isinstance(page_props, dict) else {}


def _now_julian_1980_seconds() -> int:
    now_utc = datetime.now(timezone.utc)
    return int((now_utc - _JULIAN_1980_BASE).total_seconds())


def _julian_1980_to_expiry_iso(expj: int) -> str:
    try:
        dt_utc = _JULIAN_1980_BASE + timedelta(seconds=int(expj))
        return dt_utc.astimezone(IST).date().strftime("%Y-%m-%d")
    except Exception:
        return ""


def _pick_scrip_id(page_props: dict) -> Optional[int]:
    stock_data = page_props.get("stock_data")
    if isinstance(stock_data, list):
        for row in stock_data:
            if not isinstance(row, dict):
                continue
            sid = _parse_int(row.get("SCRIP_CODE"))
            if sid:
                return sid
    fno_data = page_props.get("fnoData")
    if isinstance(fno_data, dict):
        sid = _parse_int(fno_data.get("s_sid") or fno_data.get("u_id"))
        if sid:
            return sid
    return None


def _pick_option_expj(page_props: dict) -> Optional[int]:
    fno_data = page_props.get("fnoData")
    if not isinstance(fno_data, dict):
        return None
    opsum = fno_data.get("opsum")
    if not isinstance(opsum, dict) or not opsum:
        return None
    expjs: list[int] = []
    for k in opsum.keys():
        v = _parse_int(k)
        if v:
            expjs.append(v)
    if not expjs:
        return None
    expjs.sort()
    today_ist = datetime.now(IST).date()
    future = []
    for v in expjs:
        d = _JULIAN_1980_BASE + timedelta(seconds=int(v))
        d_ist = d.astimezone(IST).date()
        if d_ist >= today_ist:
            future.append(v)
    if not future:
        future = expjs
        
    from collections import defaultdict
    month_groups = defaultdict(list)
    for v in future:
        d = _JULIAN_1980_BASE + timedelta(seconds=int(v))
        d_ist = d.astimezone(IST).date()
        month_groups[(d_ist.year, d_ist.month)].append(v)
        
    monthly_expjs = []
    for (y, m), v_list in month_groups.items():
        monthly_expjs.append(max(v_list))
        
    monthly_expjs.sort()
    return monthly_expjs[0] if monthly_expjs else future[0]


def _extract_underlying_from_page_props(page_props: dict, symbol: str) -> Optional[float]:
    # For MCX commodities we trade futures, so prefer futures scrip LTP (Ltp) over options index sltp
    scrip = page_props.get("scripData")
    if isinstance(scrip, dict):
        v = _parse_float(str(scrip.get("Ltp") or ""))
        if v is not None and v > 0:
            return v
    fno_data = page_props.get("fnoData")
    if isinstance(fno_data, dict):
        v = _parse_float(str(fno_data.get("sltp") or ""))
        if v is not None and v > 0:
            return v
    return _extract_underlying(json.dumps(page_props), symbol)


def _extract_live_fut_from_builtup(raw: dict) -> Optional[float]:
    rows = (raw or {}).get("data")
    if not isinstance(rows, list) or not rows:
        return None
    latest = None
    latest_et = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        et = _parse_int(row.get("et") or row.get("st")) or 0
        if latest is None or et >= (latest_et or 0):
            latest = row
            latest_et = et
    if not isinstance(latest, dict):
        return None
    for key in ("c", "ltp", "close"):
        px = _parse_float(str(latest.get(key) or ""))
        if px is not None and px > 0:
            return px
    return None


def _normalise_scanx_oc(raw: dict) -> list[dict]:
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return []
    oc = data.get("oc")
    if not isinstance(oc, dict):
        return []

    out: list[dict] = []
    for strike_key, row in oc.items():
        if not isinstance(row, dict):
            continue
        strike = _parse_float(str(strike_key))
        if strike is None or strike <= 0:
            continue
        for side_key, side in (("ce", "CE"), ("pe", "PE")):
            leg = row.get(side_key)
            if not isinstance(leg, dict) or not leg:
                continue
            oi = _parse_int(leg.get("OI")) or 0
            oi_chg = _parse_int(leg.get("oichng")) or 0
            out.append({
                "strike": strike,
                "option_type": side,
                "ltp": _parse_float(str(leg.get("ltp") or "")) or 0.0,
                "ltp_change_pct": _parse_float(str(leg.get("p_pchng") or "")),
                "oi": oi,
                "oi_change_pct": _parse_float(str(leg.get("oiperchnge") or "")),
                "oi_change": oi_chg,
                "volume": _parse_int(leg.get("vol")) or 0,
                "iv": _parse_float(str(leg.get("iv") or "")),
                "bid": _parse_float(str(leg.get("bid") or "")),
                "ask": _parse_float(str(leg.get("ask") or "")),
                "delta": _parse_float(str((leg.get("optgeeks") or {}).get("delta") or "")),
            })
    return out


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _find_strike_idx(cells: list[str]) -> Optional[int]:
    candidates: list[tuple[int, float]] = []
    for idx, cell in enumerate(cells):
        cleaned = _clean_text(cell)
        if not cleaned:
            continue
        val = _parse_float(cleaned)
        if val is None:
            continue
        if 10 <= val <= 100_000:
            candidates.append((idx, val))
    if not candidates:
        return None
    mid = len(cells) // 2
    candidates.sort(key=lambda it: abs(it[0] - mid))
    return candidates[0][0]


def _extract_strikes(html: str) -> list[dict]:
    # Try to extract structured JSON from Next.js __NEXT_DATA__ first
    try:
        m = re.search(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", html, re.I | re.S)
        data = None
        if m:
            txt = m.group(1).strip()
            try:
                data = json.loads(txt)
            except Exception:
                data = None
        else:
            m2 = re.search(r"window\.__NEXT_DATA__\s*=\s*({.*?})\s*;", html, re.I | re.S)
            if m2:
                try:
                    data = json.loads(m2.group(1))
                except Exception:
                    data = None

        if data:
            # dump debug copy to scratch for inspection
            try:
                root = Path(__file__).resolve().parents[2]
                dbg = root / "scratch" / "dhan_next_data_debug.json"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

            page_props = (data.get("props") or {}).get("pageProps") if isinstance(data.get("props"), dict) else data.get("pageProps") or data.get("props")
            if not isinstance(page_props, dict):
                page_props = {}

            fno = page_props.get("fnoData") or page_props.get("fno_data") or page_props.get("opsum") or page_props.get("data")
            rows = None
            if isinstance(fno, dict):
                rows = fno.get("flst") or fno.get("rows") or fno.get("list")
            if not rows:
                rows = page_props.get("flst") or page_props.get("rows") or page_props.get("rowsList")

            if isinstance(rows, list) and rows:
                strikes: list[dict] = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    strike_val = r.get("strike") or r.get("st") or r.get("s") or r.get("strike_price")
                    if strike_val is None:
                        continue
                    strike = _parse_float(strike_val)
                    if strike is None:
                        continue

                    ce = r.get("ce") or r.get("CE") or r.get("call") or r.get("c")
                    pe = r.get("pe") or r.get("PE") or r.get("put") or r.get("p")

                    ce_ltp = _parse_float((ce or {}).get("ltp") if isinstance(ce, dict) else None)
                    ce_oi = _parse_compact_number((ce or {}).get("oi") if isinstance(ce, dict) else None)
                    ce_vol = _parse_compact_number((ce or {}).get("volume") if isinstance(ce, dict) else None)
                    pe_ltp = _parse_float((pe or {}).get("ltp") if isinstance(pe, dict) else None)
                    pe_oi = _parse_compact_number((pe or {}).get("oi") if isinstance(pe, dict) else None)
                    pe_vol = _parse_compact_number((pe or {}).get("volume") if isinstance(pe, dict) else None)

                    if ce_ltp is None and pe_ltp is None:
                        continue

                    strikes.append({
                        "strike": strike,
                        "option_type": "CE",
                        "ltp": ce_ltp or 0.0,
                        "ltp_change_pct": None,
                        "oi": ce_oi or 0,
                        "oi_change_pct": None,
                        "oi_change": 0,
                        "volume": ce_vol or 0,
                        "iv": None,
                        "bid": None,
                        "ask": None,
                    })
                    strikes.append({
                        "strike": strike,
                        "option_type": "PE",
                        "ltp": pe_ltp or 0.0,
                        "ltp_change_pct": None,
                        "oi": pe_oi or 0,
                        "oi_change_pct": None,
                        "oi_change": 0,
                        "volume": pe_vol or 0,
                        "iv": None,
                        "bid": None,
                        "ask": None,
                    })

                if strikes:
                    log.debug("[dhan_commodity] parsed %d strikes from __NEXT_DATA__", len(strikes))
                    return strikes
    except Exception:
        log.exception("[dhan_commodity] error parsing __NEXT_DATA__ JSON")

    # HTML table fallback
    parsed: list[dict] = []
    table_blocks = re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    log.debug("[dhan_commodity] found %d table blocks in HTML", len(table_blocks))
    for block in table_blocks:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.I | re.S)
        try:
            sample = []
            for r in rows[:5]:
                cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", r, re.I | re.S)
                sample.append([_clean_text(_strip_tags(c)) for c in cells])
            log.debug("[dhan_commodity] sample rows: %s", sample)
        except Exception:
            pass

        strikes: list[dict] = []
        for row_html in rows:
            cell_html = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row_html, re.I | re.S)
            cells = [_clean_text(_strip_tags(c)) for c in cell_html]
            if len(cells) < 7:
                continue
            strike_idx = _find_strike_idx(cells)
            if strike_idx is None:
                continue
            if strike_idx - 3 < 0 or strike_idx + 3 >= len(cells):
                continue
            strike = _parse_float(cells[strike_idx])
            if strike is None:
                continue

            ce_oi_text = cells[strike_idx - 3]
            ce_vol_text = cells[strike_idx - 2]
            ce_ltp_text = cells[strike_idx - 1]
            pe_ltp_text = cells[strike_idx + 1]
            pe_vol_text = cells[strike_idx + 2]
            pe_oi_text = cells[strike_idx + 3]

            ce_ltp = _parse_float(_strip_percent(ce_ltp_text))
            pe_ltp = _parse_float(_strip_percent(pe_ltp_text))
            ce_oi = _parse_compact_number(_strip_percent(ce_oi_text))
            pe_oi = _parse_compact_number(_strip_percent(pe_oi_text))
            ce_vol = _parse_compact_number(ce_vol_text)
            pe_vol = _parse_compact_number(pe_vol_text)
            ce_ltp_pct = _parse_percent(ce_ltp_text)
            pe_ltp_pct = _parse_percent(pe_ltp_text)
            ce_oi_pct = _parse_percent(ce_oi_text)
            pe_oi_pct = _parse_percent(pe_oi_text)

            if ce_ltp is None and pe_ltp is None:
                continue

            strikes.append({
                "strike": strike,
                "option_type": "CE",
                "ltp": ce_ltp or 0.0,
                "ltp_change_pct": ce_ltp_pct,
                "oi": ce_oi or 0,
                "oi_change_pct": ce_oi_pct,
                "oi_change": 0,
                "volume": ce_vol or 0,
                "iv": None,
                "bid": None,
                "ask": None,
            })
            strikes.append({
                "strike": strike,
                "option_type": "PE",
                "ltp": pe_ltp or 0.0,
                "ltp_change_pct": pe_ltp_pct,
                "oi": pe_oi or 0,
                "oi_change_pct": pe_oi_pct,
                "oi_change": 0,
                "volume": pe_vol or 0,
                "iv": None,
                "bid": None,
                "ask": None,
            })

        if len(strikes) > len(parsed):
            parsed = strikes

    return parsed

class DhanCommodityFetcher(BaseFetcher):
    name = "dhan_commodity"

    def _fetch_html(self, url: str) -> Optional[str]:
        last_exc = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, headers=_HEADERS, timeout=HTTP_TIMEOUT_SECONDS)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if "nameresolutionerror" in exc_str or "getaddrinfo failed" in exc_str or "failed to resolve" in exc_str:
                    log.warning("[dhan_commodity] Name resolution failed — network offline. Skipping retries.")
                    break
                if "timeout" in type(exc).__name__.lower() or "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                    log.warning("[dhan_commodity] HTML request timed out — skipping retries: %s", exc)
                    break
                wait = HTTP_BACKOFF_FACTOR ** attempt
                log.warning("[dhan_commodity] attempt %d/%d failed: %s — retry in %ds",
                            attempt, HTTP_MAX_RETRIES, exc, wait)
                time.sleep(wait)
        log.error("[dhan_commodity] all %d retries exhausted: %s", HTTP_MAX_RETRIES, last_exc)
        return None

    def _fetch_scanx_option_chain(self, sid: int, expj: int) -> dict | None:
        payload = {"Data": {"Seg": 5, "Sid": int(sid), "Exp": int(expj)}}
        last_exc = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    _SCANX_OPTCHAIN_URL,
                    headers=_JSON_HEADERS,
                    json=payload,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if "nameresolutionerror" in exc_str or "getaddrinfo failed" in exc_str or "failed to resolve" in exc_str:
                    log.warning("[dhan_commodity] Name resolution failed — network offline. Skipping retries.")
                    break
                wait = HTTP_BACKOFF_FACTOR ** attempt
                log.warning(
                    "[dhan_commodity] optchainactive attempt %d/%d failed: %s - retry in %ds",
                    attempt,
                    HTTP_MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
        log.error("[dhan_commodity] optchainactive exhausted after %d retries: %s", HTTP_MAX_RETRIES, last_exc)
        return None

    def _fetch_builtup_live_price(self, secid: int) -> Optional[float]:
        payload = {
            "Data": {
                "Exch": "MCX",
                "Seg": "M",
                "Inst": "FUTCOM",
                "Timeinterval": "15",
                "Secid": int(secid),
            }
        }
        last_exc = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    _DHAN_BUILTUP_URL,
                    headers=_BUILTUP_HEADERS,
                    json=payload,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                return _extract_live_fut_from_builtup(resp.json())
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if "nameresolutionerror" in exc_str or "getaddrinfo failed" in exc_str or "failed to resolve" in exc_str:
                    log.warning("[dhan_commodity] Name resolution failed — network offline. Skipping retries.")
                    break
                wait = HTTP_BACKOFF_FACTOR ** attempt
                log.warning(
                    "[dhan_commodity] builtup live-price attempt %d/%d failed: %s - retry in %ds",
                    attempt,
                    HTTP_MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
        log.warning("[dhan_commodity] builtup live-price unavailable: %s", last_exc)
        return None

    def fetch_option_chain(self, symbol: str) -> dict | None:
        base = symbol.upper().split()[0]
        slug = _SYMBOL_SLUGS.get(base)
        if not slug:
            log.warning("[dhan_commodity] unsupported symbol: %s", base)
            return None

        url = _BASE_URL.format(slug=slug)
        html = self._fetch_html(url)
        
        sid = None
        expj = None
        expiry = ""
        underlying = None
        strikes = []

        if html:
            page_text = _clean_text(_strip_tags(html))
            next_data = _extract_next_data(html)
            page_props = _extract_page_props(next_data)

            sid = _pick_scrip_id(page_props)
            expj = _pick_option_expj(page_props)
            expiry = _julian_1980_to_expiry_iso(expj) if expj else ""
            if not expiry:
                expiry = _extract_expiry_iso(page_text)

            underlying = _extract_underlying_from_page_props(page_props, base)
            if underlying is None:
                underlying = _extract_underlying(page_text, base)

            if sid and expj:
                raw = self._fetch_scanx_option_chain(sid, expj)
                strikes = _normalise_scanx_oc(raw or {})
                if strikes:
                    api_underlying = _parse_float(str(((raw or {}).get("data") or {}).get("sltp") or ""))
                    if api_underlying is not None and api_underlying > 0:
                        # Prefer futures contract LTP from page props if already resolved
                        if not underlying:
                            underlying = api_underlying
                    log.debug(
                        "[dhan_commodity] parsed %d strikes via optchainactive for %s (sid=%s expj=%s)",
                        len({r["strike"] for r in strikes}),
                        base,
                        sid,
                        expj,
                    )
                else:
                    log.warning(
                        "[dhan_commodity] optchainactive returned empty chain for %s (sid=%s expj=%s)",
                        base,
                        sid,
                        expj,
                    )

            if not strikes:
                strikes = _extract_strikes(html)

        # ── API-ONLY FALLBACK ──────────────────────────────────────────────────
        if not strikes:
            log.info("[dhan_commodity] HTML parsing failed or empty. Falling back to ScanX API scan for %s", base)
            secid = get_dhan_security_id(base)
            if secid:
                fl_payload = {"Data": {"Seg": 5, "Sid": int(secid), "Exp": 0}}
                try:
                    resp = self.session.post(
                        _SCANX_OPTCHAIN_URL,
                        headers=_JSON_HEADERS,
                        json=fl_payload,
                        timeout=10.0,
                    )
                    resp.raise_for_status()
                    data = resp.json().get("data", {})
                    fl = data.get("fl", {})
                    expjs_list = sorted([int(k) for k in fl.keys()])
                    
                    if expjs_list:
                        fut_exp = expjs_list[0]
                        seconds_per_day = 86400
                        for days_before in range(0, 8):
                            test_exp = fut_exp - (days_before * seconds_per_day)
                            payload_oc = {"Data": {"Seg": 5, "Sid": int(secid), "Exp": test_exp}}
                            try:
                                log.debug("[dhan_commodity] ScanX scan: trying Exp=%d", test_exp)
                                resp_oc = self.session.post(
                                    _SCANX_OPTCHAIN_URL,
                                    headers=_JSON_HEADERS,
                                    json=payload_oc,
                                    timeout=15.0,
                                )
                                if resp_oc.status_code == 200:
                                    raw_oc = resp_oc.json()
                                    candidate_strikes = _normalise_scanx_oc(raw_oc)
                                    if candidate_strikes:
                                        strikes = candidate_strikes
                                        expj = test_exp
                                        expiry = _julian_1980_to_expiry_iso(expj)
                                        api_underlying = _parse_float(str((raw_oc.get("data") or {}).get("sltp") or ""))
                                        if api_underlying is not None:
                                            underlying = api_underlying
                                        log.info(
                                            "[dhan_commodity] ScanX API scan success: parsed %d strikes for %s at Exp=%d (offset=%d days)",
                                            len({r["strike"] for r in strikes}),
                                            base,
                                            expj,
                                            days_before,
                                        )
                                        break
                            except Exception as exc:
                                log.debug("[dhan_commodity] ScanX scan Exp=%d timed out or failed: %s", test_exp, exc)
                except Exception as exc:
                    log.error("[dhan_commodity] API scan fallback failed: %s", exc)

        if not strikes:
            log.warning("[dhan_commodity] no strikes parsed for %s", base)
            return None

        secid = get_dhan_security_id(base)
        if secid:
            live_fut = self._fetch_builtup_live_price(secid)
            if live_fut is not None and live_fut > 0:
                underlying = live_fut

        return {
            "symbol": base,
            "underlying_price": underlying,
            "expiry": expiry,
            "strikes": strikes,
            "source": self.name,
        }
