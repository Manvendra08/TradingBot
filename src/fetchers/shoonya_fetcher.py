"""
Shoonya (Finvasia) Option Chain Fetcher
OAuth 2.0 Authentication (from 1st April 2026) using Playwright for browser automation:

  Step 1: Headless browser navigates OAuth authorize page
  Step 2: Auto-fills uid, password, TOTP and submits
  Step 3: Captures auth_code from redirect URL
  Step 4: POST /NorenWClientAPI/GenAcsTok  (auth_code + SHA256(uid+secret+code))  -> access_token
  Step 5: Bearer access_token for all subsequent API calls
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pyotp

from config.settings import _optional_env
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_API_BASE = "https://api.shoonya.com/NorenWClientAPI"
_TOKEN_URL = "https://api.shoonya.com/NorenWClientAPI/GenAcsTok"

_INDEX_SPOT_NAMES = {
    "NIFTY": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Services",
    "MIDCPNIFTY": "Nifty Midcap 100",
    "SENSEX": "S&P BSE SENSEX",
}

# Shoonya exchange codes for index derivatives.
# NFO = NSE F&O (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY)
# BFO = BSE F&O (SENSEX)
# MCX = MCX Commodities
_EXCHANGE_MAP: dict[str, str] = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _post_jdata(
    url: str, payload: dict, access_token: str | None = None
) -> dict | None:
    """POST jData= encoded payload, return parsed JSON or None."""
    body_str = "jData=" + json.dumps(payload, separators=(",", ":"))
    if access_token:
        body_str += f"&jKey={access_token}"
    body = body_str.encode()
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        log.error("[shoonya] POST %s -> HTTP %s: %s", url, e.code, raw[:200])
        try:
            return json.loads(raw)
        except Exception:
            return None
    except Exception as exc:
        log.error("[shoonya] POST %s failed: %s", url, exc)
        return None


class ShoonyaFetcher(BaseFetcher):
    name = "shoonya"
    # Cache path: project_root/scratch/shoonya_token.txt
    # __file__ = src/fetchers/shoonya_fetcher.py → 3 dirname up = project root
    _TOKEN_CACHE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scratch",
        "shoonya_token.txt",
    )

    def __init__(self):
        super().__init__()
        self.access_token: str | None = None

        self.user_id = _optional_env("SHOONYA_USER_ID")
        self.password = _optional_env("SHOONYA_PASSWORD")
        self.totp_key = _optional_env("SHOONYA_TOTP_KEY")
        self.secret_code = _optional_env("SHOONYA_API_SECRET")
        self.vendor_code = _optional_env(
            "SHOONYA_VENDOR_CODE", f"{self.user_id}_U" if self.user_id else ""
        )

        # Cache for resolved MCX futures tokens: symbol -> (token, exchange, expires_at)
        self._futures_token_cache: dict[str, tuple[str, str, float]] = {}

        # Try to load cached token to avoid repeated OAuth browser launches.
        self._load_cached_token()

    def _token_cache_path(self) -> str:
        return self._TOKEN_CACHE

    def _save_token(self) -> None:
        """Persist the current access_token to disk."""
        if not self.access_token:
            return
        try:
            os.makedirs(os.path.dirname(self._TOKEN_CACHE), exist_ok=True)
            with open(self._TOKEN_CACHE, "w") as f:
                f.write(self.access_token.strip())
            log.debug("[shoonya] token cached to %s", self._TOKEN_CACHE)
        except Exception as exc:
            log.warning("[shoonya] failed to cache token: %s", exc)

    def _load_cached_token(self) -> None:
        """Load a previously cached access_token from disk."""
        try:
            if os.path.exists(self._TOKEN_CACHE):
                with open(self._TOKEN_CACHE, "r") as f:
                    token = f.read().strip()
                if token:
                    self.access_token = token
                    log.debug(
                        "[shoonya] loaded cached token from %s", self._TOKEN_CACHE
                    )
        except Exception as exc:
            log.debug("[shoonya] no cached token: %s", exc)
            self.access_token = None

    def _clear_cached_token(self) -> None:
        """Remove the cached token file (e.g. after expiry)."""
        self.access_token = None
        try:
            if os.path.exists(self._TOKEN_CACHE):
                os.remove(self._TOKEN_CACHE)
                log.debug("[shoonya] cleared cached token")
        except Exception as exc:
            log.warning("[shoonya] failed to clear cached token: %s", exc)

    def _verify_token(self) -> bool:
        """Quick lightweight check: does the cached token still work?
        Uses SearchScrip on a known symbol (minimal overhead) instead of
        launching a full OAuth browser."""
        import urllib.parse

        res = self._api_call(
            "SearchScrip",
            {"exch": "NFO", "stext": urllib.parse.quote_plus("NIFTY")},
        )
        if res and res.get("stat") == "Ok":
            log.debug("[shoonya] cached token is still valid")
            return True
        log.info("[shoonya] cached token expired or invalid — will re-authenticate")
        self._clear_cached_token()
        return False

    # ------------------------------------------------------------------
    # Authentication — Playwright OAuth flow
    # ------------------------------------------------------------------

    def _get_auth_code_playwright(self) -> str | None:
        """
        Headless browser OAuth login:
        1. Navigate to authorize URL → auto-redirects to /investor-entry-level/login
        2. Fill: #lgnusrid, #lgnpwd (raw password), #lgnotp (TOTP), click LOGIN button
        3. Capture auth_code from post-login redirect URL
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "[shoonya] playwright not installed. Run: .venv\\Scripts\\pip install playwright && .venv\\Scripts\\playwright install chromium"
            )
            return None

        authorize_url = f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={self.vendor_code}"
        log.info("[shoonya] Launching headless browser for OAuth login...")
        auth_code: str | None = None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                captured_urls: list[str] = []

                # Block only images and fonts to save bandwidth and speed up page load
                def handle_route(route):
                    req_type = route.request.resource_type
                    if req_type in ("image", "font"):
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", handle_route)

                # Listen to requests and responses to capture code= even on failed page navigations (e.g. net::ERR_NAME_NOT_RESOLVED)
                page.on(
                    "request",
                    lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                )
                page.on(
                    "response",
                    lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                )

                # Step 1: Navigate — will redirect to /investor-entry-level/login
                page.goto(authorize_url, wait_until="commit")
                log.debug("[shoonya] Landed on: %s", page.url)

                # Wait for React to render the login form (allow up to 60s for 9.5MB JS load)
                page.wait_for_selector("#lgnusrid", state="visible", timeout=60000)

                # Generate fresh TOTP right before filling (avoids expiry during navigation)
                totp = pyotp.TOTP(self.totp_key).now()

                # Step 2: Fill credentials using confirmed field selectors
                page.locator("#lgnusrid").fill(self.user_id)
                page.locator("#lgnpwd").fill(self.password)
                page.locator("#lgnotp").fill(totp)

                # Step 3: Click Login (wrap in try-except in case the redirect target domain does not resolve)
                try:
                    page.locator("button:has-text('LOGIN')").click()
                    # Step 4: Wait for redirect containing auth_code
                    page.wait_for_url("*code=*", timeout=45000)
                except Exception as click_err:
                    log.debug(
                        "[shoonya] Browser encountered navigation or redirect error: %s",
                        click_err,
                    )

                final_url = page.url
                log.debug("[shoonya] Post-login URL: %s", final_url)
                browser.close()

                # Extract auth_code from URL candidates (both final URL and any intermediate requests)
                for candidate in [final_url] + captured_urls:
                    m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", candidate)
                    if m:
                        auth_code = m.group(1)
                        log.info("[shoonya] auth_code captured successfully")
                        break

                if not auth_code:
                    log.error(
                        "[shoonya] auth_code not found. Final URL: %s, Captured URLs: %s",
                        final_url,
                        captured_urls,
                    )

        except Exception as exc:
            log.exception("[shoonya] Playwright OAuth login failed: %s", exc)

        return auth_code

    def _exchange_for_token(self, auth_code: str) -> str | None:
        """Exchange auth_code for access_token via GenAcsTok."""
        checksum = _sha256(self.vendor_code + self.secret_code + auth_code)
        payload = {
            "uid": self.user_id,
            "code": auth_code,
            "checksum": checksum,
        }
        res = _post_jdata(_TOKEN_URL, payload)
        if not res:
            return None
        if res.get("stat") != "Ok":
            log.error("[shoonya] GenAcsTok failed: %s", res)
            return None
        token = res.get("access_token") or res.get("susertoken")
        if not token:
            log.error("[shoonya] GenAcsTok: no token in response: %s", res)
        else:
            log.debug("[shoonya] GenAcsTok response keys: %s, token length: %d", list(res.keys()), len(token))
        return token

    def login(self) -> bool:
        # If we have a cached token, verify it's still valid with a quick API call.
        if self.access_token:
            if self._verify_token():
                log.debug("[shoonya] reused cached token — skipping OAuth")
                return True
            # Token expired; _verify_token already cleared the cache.

        missing = [
            k
            for k, v in [
                ("SHOONYA_USER_ID", self.user_id),
                ("SHOONYA_PASSWORD", self.password),
                ("SHOONYA_TOTP_KEY", self.totp_key),
                ("SHOONYA_API_SECRET", self.secret_code),
            ]
            if not v
        ]
        if missing:
            log.warning("[shoonya] missing credentials: %s — skipping", missing)
            return False

        try:
            auth_code = self._get_auth_code_playwright()
            if not auth_code:
                log.error("[shoonya] Failed to obtain auth_code")
                return False
            log.info("[shoonya] Exchanging auth_code for access_token...")
            token = self._exchange_for_token(auth_code)
            if not token:
                return False
            self.access_token = token
            self._save_token()
            log.info("[shoonya] OAuth login successful")
            return True
        except Exception as exc:
            log.exception("[shoonya] login exception: %s", exc)
            return False

    # ------------------------------------------------------------------
    # API helpers (Bearer auth)
    # ------------------------------------------------------------------

    def _api_call(self, endpoint: str, payload: dict) -> dict | None:
        payload.setdefault("uid", self.user_id)
        log.debug("[shoonya] API call to %s with uid=%s, token_len=%d", endpoint, self.user_id, len(self.access_token or ""))
        return _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)

    def _search_scrip(self, exchange: str, searchtext: str) -> dict | None:
        import urllib.parse

        quoted = urllib.parse.quote_plus(searchtext)
        return self._api_call("SearchScrip", {"exch": exchange, "stext": quoted})

    def _get_quotes(self, exchange: str, token: str) -> dict | None:
        return self._api_call("GetQuotes", {"exch": exchange, "token": token})

    def _get_option_chain(
        self, exchange: str, tsym: str, strikeprice: float, count: int = 15
    ) -> dict | None:
        return self._api_call(
            "GetOptionChain",
            {
                "exch": exchange,
                "tsym": tsym,
                "strprc": str(strikeprice),
                "cnt": str(count),
            },
        )

    # ------------------------------------------------------------------
    # OHLC candle data — GetTimePriceSeries
    # ------------------------------------------------------------------

    def resolve_futures_token(self, base_symbol: str) -> tuple[str, str] | None:
        """
        Resolve the near-month futures token for an MCX commodity.
        Returns (exchange, token) or None on failure.
        Results are cached for 6 hours to minimise SearchScrip calls.
        """
        import time as _time

        entry = self._futures_token_cache.get(base_symbol)
        if entry:
            token, exchange, expires_at = entry
            if _time.time() < expires_at:
                return (exchange, token)

        if not self.login():
            return None

        try:
            search_res = self._search_scrip("MCX", base_symbol)
            if (
                not search_res
                or search_res.get("stat") != "Ok"
                or not search_res.get("values")
            ):
                log.warning(
                    "[shoonya] resolve_futures_token: SearchScrip failed for %s", base_symbol
                )
                return None

            futures = []
            for val in search_res["values"]:
                tsym = val.get("tsym", "")
                if "CE" in tsym.upper() or "PE" in tsym.upper():
                    continue
                if val.get("instname") != "FUTCOM":
                    continue
                pattern = rf"^{re.escape(base_symbol)}\d{{2}}[A-Z]{{3}}(?:\d{{2}}F?|FUT)?$"
                if re.match(pattern, tsym):
                    futures.append(val)

            if not futures and search_res.get("values"):
                # Fallback: any FUTCOM for this symbol
                futures = [
                    v for v in search_res["values"]
                    if v.get("instname") == "FUTCOM"
                    and "CE" not in v.get("tsym", "").upper()
                    and "PE" not in v.get("tsym", "").upper()
                ]

            if not futures:
                log.warning(
                    "[shoonya] resolve_futures_token: no FUTCOM contracts found for %s", base_symbol
                )
                return None

            target = futures[0]
            token = target.get("token")
            if not token:
                return None

            self._futures_token_cache[base_symbol] = (token, "MCX", _time.time() + 21600)
            log.info(
                "[shoonya] resolved futures token for %s: %s (tsym=%s)",
                base_symbol,
                token,
                target.get("tsym"),
            )
            return ("MCX", token)

        except Exception as exc:
            log.warning("[shoonya] resolve_futures_token failed for %s: %s", base_symbol, exc)
            return None

    def fetch_candles(
        self,
        exchange: str,
        token: str,
        interval_minutes: int,
        start_epoch: int,
        end_epoch: int,
    ) -> list[dict] | None:
        """
        Fetch OHLC candle data via GetTimePriceSeries.
        Returns a list of bar dicts with keys: Open, High, Low, Close, _ts (epoch seconds).
        Returns None if the API call fails or returns no data.
        """
        if not self.login():
            return None

        payload = {
            "uid": self.user_id,
            "exch": exchange,
            "token": str(token),
            "st": str(start_epoch),
            "et": str(end_epoch),
            "intrv": str(interval_minutes),
        }
        url = "https://api.shoonya.com/NorenWClientTP/TPSeries"
        try:
            body_str = "jData=" + json.dumps(payload, separators=(",", ":"))
            body_str += f"&jKey={self.access_token}"
            body = body_str.encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw_body = e.read().decode()
            log.warning(
                "[shoonya] TPSeries HTTP %s for %s %s: %s",
                e.code, exchange, token, raw_body[:200],
            )
            return None
        except Exception as exc:
            log.warning(
                "[shoonya] TPSeries failed %s %s: %s", exchange, token, exc
            )
            return None

        # Handle auth-failure response (single dict with stat != Ok)
        if isinstance(raw, dict):
            emsg = raw.get("emsg", str(raw))
            if "session" in emsg.lower() or "token" in emsg.lower() or "invalid" in emsg.lower():
                log.info("[shoonya] session expired during candle fetch — clearing token cache")
                self._clear_cached_token()
            else:
                log.warning("[shoonya] TPSeries unexpected response: %s", emsg)
            return None

        if not isinstance(raw, list):
            log.warning("[shoonya] TPSeries: unexpected response type %s", type(raw))
            return None

        bars: list[dict] = []
        for item in raw:
            try:
                # Shoonya returns: ssboe (bar start epoch), into/inth/intl/intc (OHLC)
                # Some API versions use 'o'/'h'/'l'/'c' — handle both.
                ts = float(item.get("ssboe") or item.get("ts") or 0)
                o = float(item.get("into") or item.get("o") or 0)
                h = float(item.get("inth") or item.get("h") or 0)
                l = float(item.get("intl") or item.get("l") or 0)
                c = float(item.get("intc") or item.get("c") or 0)
                if ts > 0 and all(x > 0 for x in (o, h, l, c)):
                    bars.append({"Open": o, "High": h, "Low": l, "Close": c, "_ts": ts})
            except Exception:
                continue

        if not bars:
            log.debug(
                "[shoonya] GetTimePriceSeries: zero valid bars for %s token=%s", exchange, token
            )
            return None

        log.debug(
            "[shoonya] GetTimePriceSeries: %d bars for %s token=%s", len(bars), exchange, token
        )
        return bars

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _ensure_mcx_symbols(self) -> str | None:
        """Ensure MCX symbols file is present and up to date."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        dest_dir = os.path.join(project_root, "scratch", "MCX_symbols")
        dest_file = os.path.join(dest_dir, "MCX_symbols.txt")

        import time

        if os.path.exists(dest_file):
            mtime = os.path.getmtime(dest_file)
            if (time.time() - mtime) < 86400:  # 24 hours
                return dest_file

        try:
            os.makedirs(dest_dir, exist_ok=True)
            url = "https://api.shoonya.com/MCX_symbols.txt.zip"
            zip_path = os.path.join(dest_dir, "MCX_symbols.txt.zip")
            log.info("[shoonya] Downloading MCX symbols master from %s...", url)

            import urllib.request

            urllib.request.urlretrieve(url, zip_path)

            import zipfile

            log.info("[shoonya] Extracting MCX symbols...")
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(dest_dir)

            if os.path.exists(zip_path):
                os.remove(zip_path)

            log.info("[shoonya] MCX symbols updated successfully at %s", dest_file)
            return dest_file
        except Exception as e:
            log.exception("[shoonya] Failed to download/extract MCX symbols: %s", e)
            if os.path.exists(dest_file):
                log.warning("[shoonya] Using existing MCX symbols file")
                return dest_file
            return None

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        if not self.login():
            return None

        base = symbol.upper().split()[0]

        try:
            is_index = base in _INDEX_SPOT_NAMES
            if is_index:
                exch = _EXCHANGE_MAP.get(base, "NFO")
                search_text = base
                instname = "FUTIDX"
                option_exch = exch
                # SENSEX derivatives trade on BFO (BSE F&O).
                # SearchScrip on BFO needs "SENSEX FUT" as search text to
                # find regular SENSEX futures (avoids SENSEX50 mini contracts).
                # Futures tsym format: SENSEX26JUNFUT (NFO uses NIFTY25JUN26F).
                if base == "SENSEX":
                    option_exch = "BFO"
                    exch = "BFO"
                    search_text = "SENSEX FUT"
            else:
                exch = "MCX"
                search_text = base
                instname = "FUTCOM"
                option_exch = "MCX"

            # 1. Resolve underlying futures contract
            search_res = self._search_scrip(exch, search_text)
            if (
                not search_res
                or search_res.get("stat") != "Ok"
                or not search_res.get("values")
            ):
                log.warning("[shoonya] could not search scrip for %s", search_text)
                return None

            values = search_res["values"]
            underlying_token = underlying_tsym = None

            # Filter futures contracts
            def _is_not_option(val: dict) -> bool:
                tsym = val.get("tsym", "")
                return "CE" not in tsym.upper() and "PE" not in tsym.upper()

            futures = []
            for val in values:
                if not _is_not_option(val):
                    continue
                if val.get("instname") != instname:
                    continue
                # NFO format: NIFTY25JUN26F  (base + ddMMMyy + optional F)
                # BFO format: SENSEX26JUNFUT  (base + ddMMM + FUT)
                pattern = rf"^{base}\d{{2}}[A-Z]{{3}}(?:\d{{2}}F?|FUT)?$"
                if re.match(pattern, val.get("tsym", "")):
                    futures.append(val)

            if not futures:
                for val in values:
                    if not _is_not_option(val):
                        continue
                    if val.get("instname") == instname:
                        futures.append(val)

            if futures:
                target_item = futures[0]
                # If nearest expires today (e.g. 25JUN26), select next one if available
                if len(futures) > 1 and "25JUN26" in target_item.get("tsym", ""):
                    target_item = futures[1]
                underlying_token = target_item.get("token")
                underlying_tsym = target_item.get("tsym")

            if not underlying_token:
                log.warning("[shoonya] underlying not resolved for %s", base)
                return None

            quote = self._get_quotes(exch, underlying_token)
            if not quote or quote.get("stat") != "Ok":
                log.warning(
                    "[shoonya] failed quotes for underlying %s", underlying_tsym
                )
                return None

            try:
                underlying_price = float(quote.get("lp", 0))
            except (ValueError, TypeError):
                underlying_price = 0.0

            if underlying_price == 0.0:
                log.warning("[shoonya] underlying price is 0 for %s", underlying_tsym)
                return None

            # Handle Non-index (MCX Commodities) using local symbol file fallback
            if not is_index:
                symbol_file = self._ensure_mcx_symbols()
                if not symbol_file:
                    log.warning("[shoonya] MCX symbols file not available")
                    return None

                import csv

                all_options = []
                with open(symbol_file, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if (
                            row.get("Instrument") == "OPTFUT"
                            and row.get("Symbol") == base
                        ):
                            all_options.append(row)

                if not all_options:
                    log.warning(
                        "[shoonya] No option contracts found in master for %s", base
                    )
                    return None

                # Unique expiries sorted chronologically
                try:
                    expiries = sorted(
                        list(set(row["Expiry"] for row in all_options)),
                        key=lambda x: datetime.strptime(x, "%d-%b-%Y"),
                    )
                except Exception as e:
                    log.warning("[shoonya] Failed to parse expiries: %s", e)
                    return None

                if not expiries:
                    log.warning("[shoonya] No expiries found for %s", base)
                    return None

                target_expiry_shoonya = None
                target_expiry_iso = None

                if expiry:
                    for exp in expiries:
                        iso = datetime.strptime(exp, "%d-%b-%Y").strftime("%Y-%m-%d")
                        if iso == expiry:
                            target_expiry_shoonya = exp
                            target_expiry_iso = expiry
                            break
                    if not target_expiry_shoonya:
                        log.warning(
                            "[shoonya] Target expiry %s not found in MCX master for %s",
                            expiry,
                            base,
                        )
                        return None
                else:
                    target_expiry_shoonya = expiries[0]
                    target_expiry_iso = datetime.strptime(
                        target_expiry_shoonya, "%d-%b-%Y"
                    ).strftime("%Y-%m-%d")

                expiry_options = [
                    row for row in all_options if row["Expiry"] == target_expiry_shoonya
                ]
                if not expiry_options:
                    log.warning(
                        "[shoonya] No contracts found for expiry %s",
                        target_expiry_shoonya,
                    )
                    return None

                for row in expiry_options:
                    try:
                        row["strike_val"] = float(row["StrikePrice"])
                    except (ValueError, TypeError):
                        row["strike_val"] = 0.0

                expiry_options = [
                    row for row in expiry_options if row["strike_val"] > 0
                ]
                if not expiry_options:
                    log.warning(
                        "[shoonya] No valid strikes parsed for %s options", base
                    )
                    return None

                unique_strikes = sorted(
                    list(set(row["strike_val"] for row in expiry_options))
                )
                atm_strike = min(
                    unique_strikes, key=lambda s: abs(s - underlying_price)
                )
                atm_idx = unique_strikes.index(atm_strike)

                start_idx = max(0, atm_idx - 15)
                end_idx = min(len(unique_strikes), atm_idx + 16)
                selected_strikes = set(unique_strikes[start_idx:end_idx])

                contracts_to_fetch = [
                    row
                    for row in expiry_options
                    if row["strike_val"] in selected_strikes
                ]

                import time
                from concurrent.futures import ThreadPoolExecutor

                quotes = {}

                def fetch_quote(row):
                    token = row["Token"]
                    q = self._get_quotes(exch, token)
                    if q and q.get("stat") == "Ok":
                        return token, q
                    return token, None

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = []
                    for row in contracts_to_fetch:
                        futures.append(executor.submit(fetch_quote, row))
                        time.sleep(
                            0.12
                        )  # Pace to stay strictly under the 10/sec rate limit

                    for fut in futures:
                        try:
                            token, q = fut.result()
                            if q:
                                quotes[token] = q
                        except Exception:
                            pass

                strikes = []
                for row in contracts_to_fetch:
                    token = row["Token"]
                    q = quotes.get(token)
                    if not q:
                        continue

                    ot = row["OptionType"]
                    if ot not in ("CE", "PE"):
                        continue

                    def _f(key: str, _q: dict = q) -> float:
                        try:
                            return float(_q.get(key) or 0.0)
                        except (ValueError, TypeError):
                            return 0.0

                    def _i(key: str, _q: dict = q) -> int:
                        try:
                            return int(_q.get(key) or 0)
                        except (ValueError, TypeError):
                            return 0

                    strikes.append(
                        {
                            "strike": row["strike_val"],
                            "option_type": ot,
                            "ltp": _f("lp"),
                            "oi": _i("oi"),
                            "oi_change": _i("oichg"),
                            "volume": _i("v"),
                            "iv": _f("iv"),
                            "bid": _f("bp1"),
                            "ask": _f("sp1"),
                        }
                    )

                if not strikes:
                    log.warning("[shoonya] No quotes fetched for %s options", base)
                    return None

                return {
                    "symbol": base,
                    "underlying_price": underlying_price,
                    "expiry": target_expiry_iso,
                    "strikes": strikes,
                    "source": self.name,
                }

            # Handle standard NSE/BSE indices using GetOptionChain
            chain_tsym = underlying_tsym
            chain = self._get_option_chain(
                option_exch, chain_tsym, underlying_price, count=15
            )
            if not chain or chain.get("stat") != "Ok" or not chain.get("values"):
                log.warning("[shoonya] empty option chain for %s", chain_tsym)
                return None

            scrip_list = chain["values"]

            expiry_dates: dict[str, str] = {}
            now = datetime.now()
            for item in scrip_list:
                exp_str = item.get("expiry")
                if not exp_str:
                    tsym = item.get("tsym", "")
                    # Try NFO format: NIFTY25JUN2677100CE → captures "25JUN26"
                    m = re.search(r"(\d{2}[A-Z]{3}\d{2})[CP]", tsym)
                    if m:
                        candidate = m.group(1)
                        try:
                            dt = datetime.strptime(candidate, "%d%b%y")
                            # Sanity check: year should be within ~5 years of current
                            if now.year - 5 <= dt.year <= now.year + 2:
                                exp_str = candidate
                                item["expiry_parsed"] = exp_str
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                    # If NFO format failed, try BFO format:
                    # SENSEX26JUN77100CE → captures "26JUN" (no year digits)
                    if not item.get("expiry_parsed"):
                        m = re.search(r"(\d{2}[A-Z]{3})\d+[CP]", tsym)
                        if m:
                            exp_str = m.group(1)
                            item["expiry_parsed"] = exp_str
                            try:
                                exp_month = datetime.strptime(exp_str[2:], "%b").month
                                year = now.year
                                # Infer year: if month is Dec and current is Jan, use prev year
                                # If month is Jan and current is Dec, use next year
                                if exp_month < now.month - 2:
                                    year += 1
                                dt = datetime(year, exp_month, int(exp_str[:2]))
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                            except ValueError:
                                pass
                else:
                    item["expiry_parsed"] = exp_str
                    if exp_str not in expiry_dates:
                        try:
                            dt = datetime.strptime(exp_str.title(), "%d-%b-%Y")
                            expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass

            all_expiries = sorted(expiry_dates.values())
            if not all_expiries:
                log.warning("[shoonya] no valid expiries for %s", base)
                return None

            target_expiry_iso = expiry
            if not target_expiry_iso:
                today = datetime.now(IST).date()
                future = [
                    e
                    for e in all_expiries
                    if datetime.strptime(e, "%Y-%m-%d").date() >= today
                ]
                target_expiry_iso = future[0] if future else all_expiries[0]

            target_expiry_shoonya = next(
                (sh for sh, iso in expiry_dates.items() if iso == target_expiry_iso),
                None,
            )
            if not target_expiry_shoonya:
                log.warning("[shoonya] target expiry %s not found", target_expiry_iso)
                return None

            target_scrips = [
                s for s in scrip_list if s.get("expiry_parsed") == target_expiry_shoonya
            ]
            if not target_scrips:
                log.warning("[shoonya] no contracts for expiry %s", target_expiry_iso)
                return None

            strikes = []
            for item in target_scrips:
                token = item.get("token")
                if not token:
                    continue
                q = self._get_quotes(option_exch, token)
                if not q or q.get("stat") != "Ok":
                    continue

                ot = item.get("optt")
                if ot not in ("CE", "PE"):
                    continue

                def _f(key: str, _q: dict = q) -> float:
                    try:
                        return float(_q.get(key) or 0.0)
                    except (ValueError, TypeError):
                        return 0.0

                def _i(key: str, _q: dict = q) -> int:
                    try:
                        return int(_q.get(key) or 0)
                    except (ValueError, TypeError):
                        return 0

                try:
                    strike = float(item.get("strprc") or 0)
                except (ValueError, TypeError):
                    continue

                strikes.append(
                    {
                        "strike": strike,
                        "option_type": ot,
                        "ltp": _f("lp"),
                        "oi": _i("oi"),
                        "oi_change": _i("oichg"),
                        "volume": _i("v"),
                        "iv": _f("iv"),
                        "bid": _f("bp1"),
                        "ask": _f("sp1"),
                    }
                )

            if not strikes:
                log.warning("[shoonya] no strikes parsed for %s", base)
                return None

            return {
                "symbol": base,
                "underlying_price": underlying_price,
                "expiry": target_expiry_iso,
                "strikes": strikes,
                "source": self.name,
            }

        except Exception as exc:
            log.exception("[shoonya] option chain fetch failed for %s: %s", symbol, exc)
            return None


# ------------------------------------------------------------------
# Module-level singleton — shared by chart_fetcher and option chain router.
# Using a single instance reuses the cached OAuth token across both callers.
# ------------------------------------------------------------------

_shoonya_instance: ShoonyaFetcher | None = None
_shoonya_lock = threading.Lock()


def get_shoonya_fetcher() -> ShoonyaFetcher:
    """Return (or lazily create) the process-wide ShoonyaFetcher singleton."""
    global _shoonya_instance
    if _shoonya_instance is None:
        with _shoonya_lock:
            if _shoonya_instance is None:
                _shoonya_instance = ShoonyaFetcher()
    return _shoonya_instance
