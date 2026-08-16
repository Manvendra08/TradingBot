"""
Direct Google Antigravity / Cloud Code API Client for NSEBOT.

Faithfully mirrors OmniRoute's AntigravityExecutor (open-sse/executors/antigravity.ts) to
guarantee identical API behaviour and 429 handling:

  * Uses the SSE streaming endpoint  /v1internal:streamGenerateContent?alt=sse
    (the non-streaming :generateContent endpoint is served from a separate quota bucket
    that is subject to stricter rate limits — OmniRoute never hits it for LLM calls)

  * Sends the full request envelope that Google's Cloud Code API expects:
      project, model, userAgent, requestType, requestId, request.sessionId,
      generationConfig.topK=40, generationConfig.topP=1.0

  * Primary token source: reads live access_token from OmniRoute's storage.sqlite
    (OmniRoute keeps it refreshed as long as it is running).
    Falls back to google-auth / raw HTTP refresh if the stored token is missing or expired.

  * 429 classification mirrors antigravity429Engine.ts:
      unknown / soft_rate_limit  → exponential backoff 2s → 4s → 8s (MAX 3 retries)
      quota_exhausted            → skip account, move to next token
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from urllib3.util.retry import Retry

log = logging.getLogger("nsebot.antigravity_client")

# ─── OAuth / API constants ─────────────────────────────────────────────────────

ANTIGRAVITY_CLIENT_ID = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID", "")
ANTIGRAVITY_CLIENT_SECRET = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "")
ANTIGRAVITY_TOKEN_URI = "https://oauth2.googleapis.com/token"
ANTIGRAVITY_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# OmniRoute uses the SSE streaming endpoint exclusively (buildUrl → streamGenerateContent)
ANTIGRAVITY_BASE_URL = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_SSE_ENDPOINT = f"{ANTIGRAVITY_BASE_URL}/v1internal:streamGenerateContent?alt=sse"

# Headers matching antigravityHeaders.ts
_ANTIGRAVITY_IDE_VERSION = "2.0.0"
_ANTIGRAVITY_OS_TYPE = "darwin"
_ANTIGRAVITY_ARCH = "arm64"

# 429 retry constants (mirrors antigravity.ts)
_MAX_AUTO_RETRIES = 3
_MAX_RETRY_AFTER_MS = 60_000

# Treat a token as expired if it has less than this many seconds remaining
_TOKEN_EXPIRY_BUFFER_SECS = 120

# 429 classification keywords (mirrors antigravity429Engine.ts)
_QUOTA_EXHAUSTED_KEYWORDS = [
    "quota_exhausted", "quota exhausted", "quota reached",
    "enable overages", "individual quota",
    "free tier", "daily limit", "exhausted your capacity",
]
# Google's generic RESOURCE_EXHAUSTED string — treated as credits-eligible quota
_RESOURCE_EXHAUSTED_PATTERN = "resource has been exhausted"

# Per-process credits-exhausted tracker {email -> expiry_epoch}
_credits_exhausted_until: Dict[str, float] = {}
_CREDITS_EXHAUSTED_TTL_SECS = 5 * 3600  # 5 hours

try:
    import google.auth
    import google.auth.transport.requests
    import google.oauth2.credentials
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False


# ─── Encryption helpers ────────────────────────────────────────────────────────

def _get_encryption_key_from_env() -> Optional[str]:
    key = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if key:
        return key
    omniroute_env = os.path.expanduser(r"C:\Users\manve\OmniRoute\.env")
    if os.path.exists(omniroute_env):
        try:
            with open(omniroute_env, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("STORAGE_ENCRYPTION_KEY="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return None


def _decrypt_omniroute_field(ciphertext: str, secret_key: Optional[str]) -> Optional[str]:
    if not secret_key or not ciphertext.startswith("enc:v1:"):
        return None
    try:
        from hashlib import scrypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        parts = ciphertext[7:].split(":")
        if len(parts) != 3:
            return None
        iv = bytes.fromhex(parts[0])
        cipher_bytes = bytes.fromhex(parts[1])
        auth_tag = bytes.fromhex(parts[2])
        derived_key = scrypt(
            secret_key.encode("utf-8"),
            salt=b"omniroute-field-encryption-v1",
            dklen=32, n=16384, r=8, p=1
        )
        aesgcm = AESGCM(derived_key)
        decrypted = aesgcm.decrypt(iv, cipher_bytes + auth_tag, None)
        return decrypted.decode("utf-8")
    except Exception:
        return None


def _parse_iso_utc(ts: Optional[str]) -> Optional[float]:
    """Parse ISO-8601 UTC timestamp string to epoch seconds."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return None


# ─── Token discovery from OmniRoute storage.sqlite ────────────────────────────

def _discover_refresh_tokens() -> List[Dict[str, Any]]:
    """
    Discover all active Antigravity accounts from OmniRoute's DB.
    Returns list of dicts with keys: email, refresh_token, access_token,
    token_expires_at (epoch float or None), project_id.
    """
    tokens: List[Dict[str, Any]] = []

    env_token = os.environ.get("ANTIGRAVITY_REFRESH_TOKEN") or os.environ.get("GOOGLE_REFRESH_TOKEN")
    if env_token:
        tokens.append({
            "email": "env@override",
            "refresh_token": env_token,
            "access_token": None,
            "token_expires_at": None,
            "project_id": os.environ.get("ANTIGRAVITY_PROJECT_ID") or "",
        })

    db_path = os.path.expanduser(r"C:\Users\manve\.omniroute\storage.sqlite")
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                "SELECT email, name, access_token, refresh_token, token_expires_at, project_id "
                "FROM provider_connections "
                "WHERE provider='antigravity' AND is_active=1"
            )
            rows = c.fetchall()
            conn.close()
            enc_key = _get_encryption_key_from_env()
            for email, name, at, rt, expires_at_str, project_id in rows:
                label = email or name or "omniroute@db"
                # Decrypt access token
                if at and at.startswith("enc:v1:"):
                    dec_at = _decrypt_omniroute_field(at, enc_key)
                else:
                    dec_at = at
                # Decrypt refresh token
                if rt and rt.startswith("enc:v1:"):
                    dec_rt = _decrypt_omniroute_field(rt, enc_key)
                else:
                    dec_rt = rt
                if not dec_rt and not dec_at:
                    continue
                tokens.append({
                    "email": label,
                    "refresh_token": dec_rt,
                    "access_token": dec_at,
                    "token_expires_at": _parse_iso_utc(expires_at_str),
                    "project_id": project_id or "",
                })
        except Exception as exc:
            log.debug("Failed to auto-discover tokens from OmniRoute DB: %s", exc)

    return tokens


# ─── 429 classification (mirrors antigravity429Engine.ts) ─────────────────────

def _classify_429(error_message: str) -> str:
    """Returns 'quota_exhausted', 'rate_limited', 'soft_rate_limit', or 'credits_eligible', or 'unknown'."""
    lower = (error_message or "").lower()
    for kw in _QUOTA_EXHAUSTED_KEYWORDS:
        if kw in lower:
            return "quota_exhausted"
    # Google's generic RESOURCE_EXHAUSTED — treat as credits-eligible (OmniRoute retries with credits)
    if _RESOURCE_EXHAUSTED_PATTERN in lower:
        return "credits_eligible"
    if any(k in lower for k in ("per minute", "rpm", "rate limit", "rate_limit", "too many requests")):
        return "rate_limited"
    if any(k in lower for k in ("try again", "temporarily")):
        return "soft_rate_limit"
    return "unknown"


def _is_credits_exhausted(email: str) -> bool:
    until = _credits_exhausted_until.get(email)
    if not until:
        return False
    if time.time() >= until:
        del _credits_exhausted_until[email]
        return False
    return True


def _mark_credits_exhausted(email: str) -> None:
    _credits_exhausted_until[email] = time.time() + _CREDITS_EXHAUSTED_TTL_SECS


def _decide_backoff_ms(category: str, retry_attempt: int) -> int:
    """
    Mirrors decide429() + the backoff loop in handleAntigravityRateLimit():
      soft_rate_limit  → 2 000 ms
      rate_limited     → try next token immediately (but if only 1 token, backoff)
      unknown          → exponential 2^(attempt+1) seconds, capped at 60s
      quota_exhausted  → -1 (sentinel: skip this account)
    """
    if category == "quota_exhausted":
        return -1
    if category == "soft_rate_limit":
        return 2_000
    return min(1000 * (2 ** (retry_attempt + 1)), _MAX_RETRY_AFTER_MS)


# ─── Request identity (mirrors antigravityIdentity.ts) ────────────────────────

def _generate_request_id() -> str:
    return f"agent/{int(time.time() * 1000)}/{uuid.uuid4().hex[:4]}"


def _generate_session_id() -> str:
    import random
    return f"-{random.randint(1_000_000_000_000_000, 9_000_000_000_000_000_000)}"


def _ide_user_agent() -> str:
    return f"antigravity/ide/{_ANTIGRAVITY_IDE_VERSION} {_ANTIGRAVITY_OS_TYPE}/{_ANTIGRAVITY_ARCH}"


# ─── SSE response parsing (mirrors sseCollect.ts) ─────────────────────────────

def _parse_sse_text(sse_body: str) -> str:
    """
    Parse SSE `data: {...}` chunks and extract concatenated text candidates.
    """
    parts: List[str] = []
    for line in sse_body.splitlines():
        if not line.startswith("data:"):
            continue
        json_str = line[5:].strip()
        if not json_str or json_str == "[DONE]":
            continue
        try:
            chunk = json.loads(json_str)
            gemini_resp = chunk.get("response") or chunk
            for cand in (gemini_resp.get("candidates") or []):
                for part in (cand.get("content", {}).get("parts") or []):
                    if "text" in part:
                        parts.append(part["text"])
        except (json.JSONDecodeError, AttributeError):
            continue
    return "".join(parts)


# ─── Project ID bootstrap (mirrors antigravityProjectBootstrap.ts) ────────────

def _load_code_assist_project_id(access_token: str, timeout: float = 8.0) -> Optional[str]:
    url = f"{ANTIGRAVITY_BASE_URL}/v1internal:loadCodeAssist"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": _ide_user_agent(),
    }
    body = {"metadata": {"ideType": "ANTIGRAVITY"}}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            raw = data.get("cloudaicompanionProject")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            if isinstance(raw, dict):
                pid = raw.get("id")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()
        log.debug("[ antigravity_client ] loadCodeAssist HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.debug("[ antigravity_client ] loadCodeAssist exception: %s", exc)
    return None


# ─── Main client ──────────────────────────────────────────────────────────────

class AntigravityClient:
    """
    Direct Antigravity Cloud Code API client matching OmniRoute's AntigravityExecutor.

    Token strategy (priority order):
      1. Live access_token from OmniRoute's storage.sqlite (kept fresh by OmniRoute while running)
      2. google-auth refresh using stored refresh_token + client credentials
      3. Raw HTTP POST to token endpoint
      4. Per-token projectId from DB; falls back to loadCodeAssist discovery
    """

    def __init__(self, refresh_token: Optional[str] = None, project_id: str = ""):
        self.client_id = ANTIGRAVITY_CLIENT_ID
        self.client_secret = ANTIGRAVITY_CLIENT_SECRET
        self.token_uri = ANTIGRAVITY_TOKEN_URI
        self.project_id = project_id
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self._creds = None
        self._project_cache: Dict[str, str] = {}
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=Retry(total=1, backoff_factor=0.3,
                              status_forcelist=[502, 503, 504],
                              allowed_methods=["POST"])
        )
        self.session.mount("https://", adapter)

    # ── Token management ──────────────────────────────────────────────────────

    def _init_credentials(self) -> bool:
        if not self.refresh_token:
            discovered = _discover_refresh_tokens()
            if discovered:
                d = discovered[0]
                self.refresh_token = d.get("refresh_token")
                self.project_id = d.get("project_id") or self.project_id
                log.info("[ antigravity_client ] Auto-discovered token for %s", d["email"])
        if not self.refresh_token:
            log.error("[ antigravity_client ] No refresh token provided or discovered.")
            return False
        if HAS_GOOGLE_AUTH:
            try:
                self._creds = google.oauth2.credentials.Credentials(
                    token=self.access_token,
                    refresh_token=self.refresh_token,
                    token_uri=self.token_uri,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=ANTIGRAVITY_SCOPES,
                )
                return True
            except Exception as exc:
                log.warning("[ antigravity_client ] Failed to construct google.oauth2 credentials: %s", exc)
        return bool(self.refresh_token)

    def _try_refresh_token(self, refresh_token: str) -> Optional[str]:
        """Try to get a fresh access token from the given refresh token."""
        # Try google-auth first
        if HAS_GOOGLE_AUTH:
            try:
                creds = google.oauth2.credentials.Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    token_uri=self.token_uri,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=ANTIGRAVITY_SCOPES,
                )
                request = google.auth.transport.requests.Request(session=self.session)
                creds.refresh(request)
                if creds.valid and creds.token:
                    return creds.token
            except Exception as exc:
                log.debug("[ antigravity_client ] google-auth refresh failed: %s", exc)

        # Raw HTTP fallback
        try:
            resp = self.session.post(
                self.token_uri,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("access_token")
            log.debug("[ antigravity_client ] Raw token refresh HTTP %d: %s", resp.status_code, resp.text[:150])
        except Exception as exc:
            log.debug("[ antigravity_client ] Raw token refresh exception: %s", exc)
        return None

    def get_valid_access_token(self) -> Optional[str]:
        now = time.time()
        if self.access_token and (self.token_expires_at - now) > _TOKEN_EXPIRY_BUFFER_SECS:
            return self.access_token
        if not self._creds and not self._init_credentials():
            return None
        if HAS_GOOGLE_AUTH and self._creds:
            try:
                request = google.auth.transport.requests.Request(session=self.session)
                self._creds.refresh(request)
                if self._creds.valid and self._creds.token:
                    self.access_token = self._creds.token
                    self.token_expires_at = now + 3500
                    return self.access_token
            except Exception:
                pass
        if self.refresh_token:
            token = self._try_refresh_token(self.refresh_token)
            if token:
                self.access_token = token
                self.token_expires_at = now + 3500
                return token
        return None

    def _get_token_for_entry(self, entry: Dict[str, Any]) -> Optional[str]:
        """
        Get a valid access token for a token entry dict.
        Priority:
          1. stored access_token if not expired
          2. refresh via refresh_token
        """
        now = time.time()
        stored_at = entry.get("access_token")
        expires_at = entry.get("token_expires_at")
        rt = entry.get("refresh_token")

        # Use stored access token if fresh
        if stored_at and expires_at and (expires_at - now) > _TOKEN_EXPIRY_BUFFER_SECS:
            return stored_at

        # If stored token is expired or missing, try to re-read from DB (OmniRoute may have refreshed it)
        if stored_at and expires_at and (expires_at - now) < 0:
            fresh_tokens = _discover_refresh_tokens()
            for fresh in fresh_tokens:
                if fresh.get("email") == entry.get("email"):
                    fresh_at = fresh.get("access_token")
                    fresh_exp = fresh.get("token_expires_at")
                    if fresh_at and fresh_exp and (fresh_exp - now) > _TOKEN_EXPIRY_BUFFER_SECS:
                        log.info(
                            "[ antigravity_client ] Re-read fresh token from OmniRoute DB for %s",
                            entry.get("email")
                        )
                        entry["access_token"] = fresh_at
                        entry["token_expires_at"] = fresh_exp
                        entry["refresh_token"] = fresh.get("refresh_token", rt)
                        return fresh_at
                    break

        # Fall back to token refresh
        if rt:
            log.debug("[ antigravity_client ] Refreshing token for %s via OAuth", entry.get("email"))
            new_at = self._try_refresh_token(rt)
            if new_at:
                entry["access_token"] = new_at
                entry["token_expires_at"] = now + 3500
                log.info("[ antigravity_client ] Refreshed OAuth token for %s", entry.get("email"))
                return new_at

        # Last resort: use stored token even if expired (worth trying)
        if stored_at:
            log.warning(
                "[ antigravity_client ] Using potentially expired token for %s (exp=%s)",
                entry.get("email"), expires_at
            )
            return stored_at

        return None

    def _resolve_project_id(self, token: str, stored_project_id: str) -> str:
        pid = (stored_project_id or "").strip()
        if pid:
            return pid
        cache_key = token[:32]
        if cache_key in self._project_cache:
            return self._project_cache[cache_key]
        discovered = _load_code_assist_project_id(token)
        if discovered:
            self._project_cache[cache_key] = discovered
            log.info("[ antigravity_client ] Discovered project ID via loadCodeAssist: %s", discovered)
            return discovered
        return "cloudcode-pa"

    # ── Request builder ───────────────────────────────────────────────────────

    def _build_envelope(
        self,
        model: str,
        prompt: str,
        system_instruction: Optional[str],
        temperature: float,
        max_output_tokens: int,
        project_id: str,
    ) -> Dict[str, Any]:
        """Build the full OmniRoute-compatible request envelope."""
        max_out = min(max_output_tokens, 16_384)
        if system_instruction:
            contents = [{
                "role": "user",
                "parts": [{"text": f"System Instruction:\n{system_instruction}\n\nUser Prompt:\n{prompt}"}]
            }]
        else:
            contents = [{"role": "user", "parts": [{"text": prompt}]}]
        return {
            "project": project_id,
            "model": model,
            "userAgent": "antigravity",
            "requestType": "agent",
            "requestId": _generate_request_id(),
            "request": {
                "contents": contents,
                "sessionId": _generate_session_id(),
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_out,
                    "topK": 40,
                    "topP": 1.0,
                },
            },
        }

    def _build_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _ide_user_agent(),
            "Accept": "text/event-stream",
        }

    # ── Core API call with OmniRoute-identical 429 handling ───────────────────

    def generate_content_with_status(
        self,
        prompt: str,
        model: str = "gemini-2.5-flash",
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
        max_output_tokens: int = 4096,
        timeout: float = 20.0,
    ) -> Tuple[Optional[str], int, str]:
        if model and model.startswith("antigravity/"):
            model = model.split("antigravity/", 1)[1]

        tokens_info = _discover_refresh_tokens()
        if not tokens_info and self.refresh_token:
            tokens_info = [{
                "email": "manual", "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "token_expires_at": self.token_expires_at or None,
                "project_id": self.project_id
            }]
        if not tokens_info:
            return None, 401, "No refresh tokens available"

        last_status = 429
        last_err = "Rate limit hit across all tokens"

        for entry in tokens_info:
            token = self._get_token_for_entry(entry)
            if not token:
                log.warning(
                    "[ antigravity_client ] Could not get valid token for %s, skipping",
                    entry.get("email")
                )
                continue

            project_id = self._resolve_project_id(token, entry.get("project_id") or "")
            envelope = self._build_envelope(
                model, prompt, system_instruction, temperature, max_output_tokens, project_id
            )
            headers = self._build_headers(token)

            retry_count = 0
            while retry_count <= _MAX_AUTO_RETRIES:
                try:
                    started = time.time()
                    resp = self.session.post(
                        ANTIGRAVITY_SSE_ENDPOINT,
                        headers=headers,
                        json=envelope,
                        timeout=timeout,
                    )
                    duration_ms = int((time.time() - started) * 1000)

                    if resp.status_code == 200:
                        text = _parse_sse_text(resp.text)
                        if not text:
                            # Sometimes the response is plain JSON, not SSE
                            try:
                                data = resp.json()
                                for cand in (data.get("candidates") or []):
                                    for p in (cand.get("content", {}).get("parts") or []):
                                        if "text" in p:
                                            text += p["text"]
                            except Exception:
                                pass
                        log.info(
                            "[ antigravity_client ] SUCCESS (%s, %dms, len=%d, token=%s, attempt=%d)",
                            model, duration_ms, len(text), entry.get("email"), retry_count,
                        )
                        return text, 200, "OK"

                    elif resp.status_code == 429:
                        error_body = resp.text or ""
                        try:
                            err_json = json.loads(error_body)
                            error_message = str((err_json.get("error") or {}).get("message", "")) + " " + error_body
                        except Exception:
                            error_message = error_body

                        category = _classify_429(error_message)
                        email = entry.get("email", "")

                        # ── Google One AI credits retry (mirrors tryCreditsRetry()) ──
                        # When we get RESOURCE_EXHAUSTED and haven't tried credits yet,
                        # inject enabledCreditTypes: ["GOOGLE_ONE_AI"] and retry once.
                        if (category == "credits_eligible"
                                and "enabledCreditTypes" not in envelope
                                and not _is_credits_exhausted(email)):
                            log.info(
                                "[ antigravity_client ] RESOURCE_EXHAUSTED on %s (%s) — retrying with Google One AI credits",
                                model, email,
                            )
                            credits_envelope = {**envelope, "enabledCreditTypes": ["GOOGLE_ONE_AI"]}
                            credits_envelope["requestId"] = _generate_request_id()
                            try:
                                cr_start = time.time()
                                cr_resp = self.session.post(
                                    ANTIGRAVITY_SSE_ENDPOINT, headers=headers,
                                    json=credits_envelope, timeout=timeout,
                                )
                                cr_ms = int((time.time() - cr_start) * 1000)
                                if cr_resp.status_code == 200:
                                    text = _parse_sse_text(cr_resp.text)
                                    log.info(
                                        "[ antigravity_client ] Credits retry SUCCESS (%s, %dms, len=%d, token=%s)",
                                        model, cr_ms, len(text), email,
                                    )
                                    return text, 200, "OK"
                                elif cr_resp.status_code == 429:
                                    log.warning(
                                        "[ antigravity_client ] Credits retry also 429 on %s (%s) — marking credits exhausted",
                                        model, email,
                                    )
                                    _mark_credits_exhausted(email)
                                else:
                                    log.warning(
                                        "[ antigravity_client ] Credits retry HTTP %d on %s (%s)",
                                        cr_resp.status_code, model, email,
                                    )
                            except Exception as cr_exc:
                                log.warning("[ antigravity_client ] Credits retry exception: %s", cr_exc)
                            # Fall through to normal backoff / next account
                            last_status = 429
                            last_err = error_body[:300]
                            break

                        backoff_ms = _decide_backoff_ms(category, retry_count)

                        if backoff_ms == -1:
                            log.warning(
                                "[ antigravity_client ] QUOTA EXHAUSTED on %s (%s) — skipping token",
                                model, email,
                            )
                            last_status = 429
                            last_err = error_body[:300]
                            break

                        if retry_count < _MAX_AUTO_RETRIES:
                            backoff_s = backoff_ms / 1000.0
                            log.warning(
                                "[ antigravity_client ] 429 [%s] on %s (%s) — retry %d/%d after %.1fs",
                                category, model, entry.get("email"),
                                retry_count + 1, _MAX_AUTO_RETRIES, backoff_s,
                            )
                            time.sleep(backoff_s)
                            retry_count += 1
                            envelope["requestId"] = _generate_request_id()
                            continue
                        else:
                            log.warning(
                                "[ antigravity_client ] 429 max retries (%d) exhausted on %s (%s)",
                                _MAX_AUTO_RETRIES, model, entry.get("email"),
                            )
                            last_status = 429
                            last_err = error_body[:300]
                            break

                    else:
                        last_status = resp.status_code
                        last_err = resp.text[:300] or f"HTTP {resp.status_code}"
                        log.error(
                            "[ antigravity_client ] HTTP %d (%dms) for %s on %s: %s",
                            resp.status_code, duration_ms, model, entry.get("email"), last_err[:200],
                        )
                        break

                except requests.Timeout:
                    last_status = 504
                    last_err = f"Timeout ({timeout}s) calling model {model}"
                    log.warning("[ antigravity_client ] Timeout (%.0fs) on %s", timeout, model)
                    break
                except Exception as exc:
                    last_status = 500
                    last_err = str(exc)
                    log.error("[ antigravity_client ] Request exception: %s", exc)
                    break

        return None, last_status, last_err

    def _extract_candidate_text(self, data: Dict[str, Any]) -> str:
        try:
            candidates = data.get("response", {}).get("candidates") or data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or []
                return "".join(p.get("text", "") for p in parts if "text" in p)
        except Exception:
            pass
        return ""


# ─── Singleton accessor ───────────────────────────────────────────────────────

_client_instance: Optional[AntigravityClient] = None


def get_antigravity_client() -> AntigravityClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AntigravityClient()
    return _client_instance