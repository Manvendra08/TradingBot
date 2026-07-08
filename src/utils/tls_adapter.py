"""Centralized TLS adapter for resilient HTTPS connections.

Solves recurring SSL EOF errors (SSLZeroReturnError) from api.kite.trade
by:
  1. Patching ssl.create_default_context() to inject OP_IGNORE_UNEXPECTED_EOF
     into every SSL context — including ones urllib3 creates when kiteconnect
     passes verify=True
  2. Evicting the dead connection pool before retrying (not just re-sending
     on the same poisoned socket)
  3. Using short backoff (0.1s → 0.3s → 0.9s)
  4. Setting Connection: close at the session level
  5. Serialising send() calls via threading.Lock to prevent concurrent
     threads from corrupting the urllib3 connection pool

ROOT CAUSE: kiteconnect passes verify=True to every HTTP request. urllib3
creates a fresh ssl.create_default_context() internally, which LACKS the
OP_IGNORE_UNEXPECTED_EOF flag. Kite's CDN sends TLS close_notify on idle
connections, and without that flag Python's ssl module raises SSLZeroReturnError.
"""

import logging
import socket
import ssl
import threading
import time

from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import SSLError
from urllib3.util import Retry

log = logging.getLogger(__name__)

_KITE_HOST = "api.kite.trade"
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_getaddrinfo_patched = False
_GLOBAL_SEND_LOCK = threading.RLock()

_SSL_EOF_MARKERS = frozenset(
    (
        "closed (eof)",
        "unexpected eof",
        "sslzeroreturnerror",
        "eof occurred",
        "ssl: eof",
        "connection reset",
        "connection aborted",
    )
)

# Timeout markers for retry on connection/read timeouts
_TIMEOUT_MARKERS = frozenset(
    (
        "read timed out",
        "read timeout",
        "connect timed out",
        "connect timeout",
        "timed out",
        "winerror 10060",
        "connection timed out",
    )
)

# DNS resolution failure markers
_DNS_MARKERS = frozenset(
    (
        "getaddrinfo failed",
        "name or service not known",
        "nodename nor servname provided",
        "name resolution",
        "dns resolution",
    )
)


# ── Process-wide SSL EOF fix ────────────────────────────────────────────
# Patch ssl.create_default_context to inject OP_IGNORE_UNEXPECTED_EOF.
# This is the safest hook because urllib3 calls create_default_context()
# when verify=True, and our context is still fully secure (TLS 1.2+,
# certificate verification, hostname checking all stay enabled).
_original_create_default_context = ssl.create_default_context
_ssl_patched = False


def _patched_create_default_context(*args, **kwargs):
    ctx = _original_create_default_context(*args, **kwargs)
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
    return ctx


def _ensure_ssl_patched():
    global _ssl_patched
    if _ssl_patched:
        return
    ssl.create_default_context = _patched_create_default_context
    _ssl_patched = True
    log.debug("Patched ssl.create_default_context to inject OP_IGNORE_UNEXPECTED_EOF")


def _kite_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if str(host).lower() == _KITE_HOST:
        family = socket.AF_INET
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _ensure_kite_ipv4_patched() -> None:
    """Force Kite REST traffic to IPv4 without affecting other hosts."""
    global _getaddrinfo_patched
    if _getaddrinfo_patched:
        return
    socket.getaddrinfo = _kite_ipv4_getaddrinfo
    _getaddrinfo_patched = True
    log.debug("Patched socket.getaddrinfo to force IPv4 for %s", _KITE_HOST)


# Apply patches at import time.
_ensure_ssl_patched()
_ensure_kite_ipv4_patched()


# urllib3-level retry: ONLY handles 5xx HTTP status codes.
# SSL/connection retries are intentionally disabled here (total=0 for
# connection errors) — our ResilientTLSAdapter.send() owns that logic.
DEFAULT_RETRY = Retry(
    total=0,
    connect=0,
    read=0,
    status=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)


class ResilientTLSAdapter(HTTPAdapter):
    """HTTPAdapter that handles SSL EOF errors by evicting stale connections
    and retrying with exponential backoff.

    All SSL/connection retry logic lives here in send() — not in urllib3 Retry.

    Usage:
        adapter = ResilientTLSAdapter()
        session.mount("https://", adapter)
    """

    SSL_RETRY_ATTEMPTS = 6
    SSL_BASE_DELAY = 0.1

    def __init__(self, *args, ssl_verify: bool = True, **kwargs):
        _ensure_ssl_patched()
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not ssl_verify:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        # Kite's REST edge is sensitive to concurrent reuse from multiple
        # dashboard/scheduler threads. Use one process-wide lock, not one lock
        # per adapter/client, so all Kite HTTPS requests are serialized.
        self._send_lock = _GLOBAL_SEND_LOCK
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

    # ── core retry-on-SSL-EOF/timeout logic ─────────────────────────────────
    def send(self, request, *args, **kwargs):
        last_err = None
        request.headers["Connection"] = "close"
        for attempt in range(self.SSL_RETRY_ATTEMPTS):
            try:
                with self._send_lock:
                    log.debug(
                        "[tls] Attempting request to %s (attempt %d/%d)",
                        request.url,
                        attempt + 1,
                        self.SSL_RETRY_ATTEMPTS,
                    )
                    return super().send(request, *args, **kwargs)
            except (SSLError, ReqConnectionError, OSError, NameError) as exc:
                last_err = exc
                log.debug("[tls] Exception on %s: %s", request.url, exc)
                is_ssl_eof = self._is_ssl_eof(exc)
                is_timeout = self._is_timeout(exc)
                is_dns = self._is_dns_failure(exc)
                is_name_err = isinstance(exc, NameError)
                if not is_ssl_eof and not is_timeout and not is_dns and not is_name_err:
                    log.debug("[tls] Non-retryable exception, re-raising")
                    raise

                if attempt < self.SSL_RETRY_ATTEMPTS - 1:
                    delay = self.SSL_BASE_DELAY * (3**attempt)
                    if is_ssl_eof:
                        reason = "SSL EOF"
                    elif is_timeout:
                        reason = "timeout"
                    elif is_dns:
                        reason = "DNS failure"
                    else:
                        reason = "response parse error"
                    log.warning(
                        "%s on %s (attempt %d/%d), evicting pool & retrying in %.1fs…",
                        reason,
                        request.url,
                        attempt + 1,
                        self.SSL_RETRY_ATTEMPTS,
                        delay,
                    )
                    self._evict_connections()
                    time.sleep(delay)
                    continue
                else:
                    self._evict_connections()
                    log.warning(
                        "[tls] retries exhausted for %s after %d attempts",
                        request.url,
                        self.SSL_RETRY_ATTEMPTS,
                    )
        if last_err is None:
            raise RuntimeError(f"TLS retries exhausted for {request.url}")
        raise last_err

    def _evict_connections(self):
        """Clear urllib3 connection pools to force fresh TCP+TLS handshake."""
        try:
            pm = getattr(self, "poolmanager", None)
            if pm is not None:
                pm.clear()
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass
        try:
            for proxy_pool in getattr(self, "proxy_manager", {}).values():
                proxy_pool.clear()
        except Exception:
            pass

    @staticmethod
    def _is_ssl_eof(exc) -> bool:
        """Check exc or its inner cause for SSL EOF markers."""
        msgs_to_check = [str(exc).lower()]
        cause = getattr(exc, "__cause__", None) or getattr(exc, "reason", None)
        if cause:
            msgs_to_check.append(str(cause).lower())
            inner = getattr(cause, "reason", None)
            if inner:
                msgs_to_check.append(str(inner).lower())
        return any(m in s for m in _SSL_EOF_MARKERS for s in msgs_to_check)

    @staticmethod
    def _is_timeout(exc) -> bool:
        """Check exc or its inner cause for timeout markers."""
        msgs_to_check = [str(exc).lower()]
        cause = getattr(exc, "__cause__", None) or getattr(exc, "reason", None)
        if cause:
            msgs_to_check.append(str(cause).lower())
            inner = getattr(cause, "reason", None)
            if inner:
                msgs_to_check.append(str(inner).lower())
        return any(m in s for m in _TIMEOUT_MARKERS for s in msgs_to_check)

    @staticmethod
    def _is_dns_failure(exc) -> bool:
        """Check exc or its inner cause for DNS resolution failure markers."""
        msgs_to_check = [str(exc).lower()]
        cause = getattr(exc, "__cause__", None) or getattr(exc, "reason", None)
        if cause:
            msgs_to_check.append(str(cause).lower())
            inner = getattr(cause, "reason", None)
            if inner:
                msgs_to_check.append(str(inner).lower())
        return any(m in s for m in _DNS_MARKERS for s in msgs_to_check)


def is_retryable_transport_error(exc: Exception) -> bool:
    """Return True for transient Kite transport failures handled by this module."""
    return (
        ResilientTLSAdapter._is_ssl_eof(exc)
        or ResilientTLSAdapter._is_timeout(exc)
        or ResilientTLSAdapter._is_dns_failure(exc)
    )


def mount_resilient_tls(session, max_retries=None, ssl_verify: bool = True):
    """Mount the ResilientTLSAdapter on a requests.Session for https://.

    Args:
        session:    A requests.Session (or kite.reqsession)
        max_retries: Optional urllib3 Retry object. Defaults to DEFAULT_RETRY.
        ssl_verify: Set False for public non-Kite fetchers that use verify=False.
    """
    _ensure_ssl_patched()
    _ensure_kite_ipv4_patched()
    session.headers["Connection"] = "close"
    adapter = ResilientTLSAdapter(
        max_retries=max_retries or DEFAULT_RETRY, ssl_verify=ssl_verify
    )
    session.mount("https://", adapter)
