"""Centralized TLS adapter for resilient HTTPS connections.

Solves recurring SSL EOF errors (SSLZeroReturnError) from api.kite.trade
by:
  1. Forcing TLS 1.2+ with OP_IGNORE_UNEXPECTED_EOF
  2. Evicting the dead connection pool before retrying (not just re-sending
     on the same poisoned socket)
  3. Using short backoff (0.1s → 0.3s → 0.9s) — Kite's load-balancer
     recovers on the very next TCP handshake, so long delays are wasteful
  4. Setting Connection: close at the session level to prevent keep-alive
     pool reuse after a failed handshake
"""
import logging
import ssl
import time

from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as ReqConnectionError, SSLError
from urllib3.util import Retry

log = logging.getLogger(__name__)

_SSL_EOF_MARKERS = frozenset((
    "closed (eof)",
    "unexpected eof",
    "sslzeroreturnerror",
    "eof occurred",
    "ssl: eof",
    "connection reset",
    "connection aborted",
))

# urllib3-level retry: ONLY handles 5xx HTTP status codes.
# SSL/connection retries are intentionally disabled here (total=0 for
# connection errors) — our ResilientTLSAdapter.send() owns that logic.
# If urllib3 also retries SSL errors its MaxRetryError fires before our
# send() pool-eviction code can run, making retries useless.
DEFAULT_RETRY = Retry(
    total=0,                       # don't let urllib3 retry anything by default
    connect=0,                     # no urllib3-level connect retries
    read=0,                        # no urllib3-level read retries
    status=3,                      # do retry 5xx HTTP status codes
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)


class ResilientTLSAdapter(HTTPAdapter):
    """HTTPAdapter that handles SSL EOF errors by evicting stale connections
    and retrying with exponential backoff.

    All SSL/connection retry logic lives here in send() — not in urllib3 Retry.
    This prevents the dual-retry conflict where urllib3 exhausts MaxRetryError
    before send() can evict the pool and open a fresh TCP+TLS handshake.

    Usage:
        adapter = ResilientTLSAdapter()
        session.mount("https://", adapter)
    """

    SSL_RETRY_ATTEMPTS = 6
    SSL_BASE_DELAY = 0.1  # seconds — 0.1, 0.3, 0.9, 2.7, 8.1 (attempt 2 always succeeds)

    def __init__(self, *args, ssl_verify: bool = True, **kwargs):
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
            self.ssl_context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
        if not ssl_verify:
            # Must disable check_hostname BEFORE setting CERT_NONE
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

    # ── core retry-on-SSL-EOF logic ──────────────────────────────────────
    def send(self, request, *args, **kwargs):
        last_err = None
        for attempt in range(self.SSL_RETRY_ATTEMPTS):
            try:
                return super().send(request, *args, **kwargs)
            except (SSLError, ReqConnectionError, OSError) as exc:
                last_err = exc
                if not self._is_ssl_eof(exc):
                    raise  # not a transient SSL EOF — propagate immediately

                if attempt < self.SSL_RETRY_ATTEMPTS - 1:
                    delay = self.SSL_BASE_DELAY * (3 ** attempt)
                    log.warning(
                        "SSL EOF on %s (attempt %d/%d), evicting pool & retrying in %.1fs…",
                        request.url, attempt + 1, self.SSL_RETRY_ATTEMPTS, delay,
                    )
                    self._evict_connections()
                    time.sleep(delay)
                    continue
        # all retries exhausted — raise last error
        raise last_err

    def _evict_connections(self):
        """Clear urllib3 connection pools to force fresh TCP+TLS handshake."""
        try:
            pm = getattr(self, "poolmanager", None)
            if pm is not None:
                pm.clear()
        except Exception:
            pass
        # Also clear any proxy pools
        try:
            for proxy_pool in getattr(self, "proxy_manager", {}).values():
                proxy_pool.clear()
        except Exception:
            pass

    @staticmethod
    def _is_ssl_eof(exc) -> bool:
        """Check exc or its inner cause for SSL EOF markers."""
        # Unwrap requests → urllib3 → socket chain
        msgs_to_check = [str(exc).lower()]
        cause = getattr(exc, "__cause__", None) or getattr(exc, "reason", None)
        if cause:
            msgs_to_check.append(str(cause).lower())
            inner = getattr(cause, "reason", None)
            if inner:
                msgs_to_check.append(str(inner).lower())
        return any(m in s for m in _SSL_EOF_MARKERS for s in msgs_to_check)


def mount_resilient_tls(session, max_retries=None, ssl_verify: bool = True):
    """Mount the ResilientTLSAdapter on a requests.Session for https://.

    Critically, overrides the session-level 'Connection: keep-alive' that
    kiteconnect sets by default. Kite's load-balancer silently closes idle
    keep-alive sockets, causing SSL EOF on the next reuse. Forcing
    'Connection: close' at the session level prevents all reuse.

    Args:
        session:    A requests.Session (or kite.reqsession)
        max_retries: Optional urllib3 Retry object. Defaults to DEFAULT_RETRY.
        ssl_verify: Set False for public non-Kite fetchers that use verify=False.
    """
    # Override keep-alive at session level so every prepared request carries
    # Connection: close BEFORE our send() is called. Doing it only inside send()
    # is too late — requests has already merged session headers into the PreparedRequest.
    session.headers["Connection"] = "close"
    adapter = ResilientTLSAdapter(max_retries=max_retries or DEFAULT_RETRY, ssl_verify=ssl_verify)
    session.mount("https://", adapter)
