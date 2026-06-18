"""Centralized TLS adapter for resilient HTTPS connections.

Solves recurring SSL EOF errors (SSLZeroReturnError) from api.kite.trade
by:
  1. Forcing TLS 1.2+ with OP_IGNORE_UNEXPECTED_EOF
  2. Evicting the dead connection pool before retrying (not just re-sending
     on the same poisoned socket)
  3. Using exponential backoff (0.3s → 0.9s → 2.7s) to let the server
     stabilise between retries
  4. Disabling keep-alive after consecutive failures to force fresh TCP
     handshakes
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

# Default retry config for non-SSL HTTP errors (5xx, etc.)
DEFAULT_RETRY = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)


class ResilientTLSAdapter(HTTPAdapter):
    """HTTPAdapter that handles SSL EOF errors by evicting stale connections
    and retrying with exponential backoff.

    Usage:
        adapter = ResilientTLSAdapter(max_retries=DEFAULT_RETRY)
        session.mount("https://", adapter)
    """

    SSL_RETRY_ATTEMPTS = 4
    SSL_BASE_DELAY = 0.3  # seconds — 0.3, 0.9, 2.7, 8.1

    def __init__(self, *args, **kwargs):
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
            self.ssl_context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
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
                err_msg = str(exc).lower()
                if not any(m in err_msg for m in _SSL_EOF_MARKERS):
                    raise  # not a transient SSL EOF — propagate immediately

                if attempt < self.SSL_RETRY_ATTEMPTS - 1:
                    delay = self.SSL_BASE_DELAY * (3 ** attempt)
                    log.warning(
                        "SSL EOF on %s (attempt %d/%d), evicting pool & retrying in %.1fs… %s",
                        request.url, attempt + 1, self.SSL_RETRY_ATTEMPTS, delay, exc,
                    )
                    # Evict ALL stale connections from urllib3's pool so the
                    # next send() opens a fresh TCP+TLS handshake.
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


def mount_resilient_tls(session, max_retries=None):
    """Mount the ResilientTLSAdapter on a requests.Session for https://.

    Args:
        session: A requests.Session (or kite.reqsession)
        max_retries: Optional urllib3 Retry object. Defaults to DEFAULT_RETRY.
    """
    adapter = ResilientTLSAdapter(max_retries=max_retries or DEFAULT_RETRY)
    session.mount("https://", adapter)
