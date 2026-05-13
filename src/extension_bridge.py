"""
Extension bridge — localhost:8765
Endpoints:
    GET  /health           → liveness check (200 when running, 503 when paused)
  POST /ingest           → single anomaly alert from extension
  POST /ingest/snapshot  → full option chain snapshot from extension
    POST /control/start    → resume bridge processing
    POST /control/stop     → pause bridge processing (process stays alive)
"""
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# Support direct script execution: `python src/extension_bridge.py`
if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

# Load .env BEFORE config imports — os.getenv is called at settings.py import time
try:
    from dotenv import load_dotenv as _ldenv
    _ldenv(Path(__file__).resolve().parents[1] / '.env')
except ImportError:
    pass

from config.logging_config import configure_logging
from src.models.schema import init_db, insert_snapshots, insert_underlying_price, get_conn, get_alert_history, delete_alerts
from src.engine.anomaly_detector import detect_anomalies
from src.alerts.dedup import is_duplicate, record_alert
from src.alerts.telegram_dispatcher import send_alert, send_text
from src.models.schema import insert_alert, mark_telegram_sent
from src.alerts.digest import build_digest
from config.settings import INDIVIDUAL_ALERT_MIN_SEVERITY

configure_logging(name="bridge")
log = logging.getLogger("extension_bridge")
BRIDGE_CODE_VERSION = "snapshot-flow-v2"

HOST = "localhost"
PORT = 8765
_server: HTTPServer | None = None
_bridge_enabled = True


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        s = str(v).replace(",", "").strip()
        if not s or s in {"—", "-", "--", "NA", "N/A", "null", "None"}:
            return default
        return float(s)
    except Exception:
        return default


def _norm_symbol(s: str | None) -> str:
    """Normalize symbols for chart/option-chain matching."""
    import re
    if not s:
        return ""
    x = str(s).upper().strip()
    x = re.sub(r"^(NSE|NFO|BSE|MCX|CDS):", "", x)
    x = x.replace("!", "")
    x = re.sub(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$", "", x)
    return re.sub(r"[^A-Z0-9]", "", x)


def _normalize_chart_indicators(raw, symbol: str | None = None) -> dict | None:
    """Normalize extension chart telemetry before passing to the intelligence layer.

    Accepts either:
      1) direct timeframe dict: {"1h": {...}, "3h": {...}}
      2) symbol-keyed chart cache: {"NIFTY": {"1h": {...}}}

    Returns direct timeframe dict compatible with intelligence.py.
    """
    if not isinstance(raw, dict) or not raw:
        return None

    tf_keys = {"1h", "3h", "4h", "1d", "15m", "30m", "5m"}

    # Already direct timeframe mapping.
    if any(str(k).lower() in tf_keys for k in raw.keys()):
        selected = raw
    else:
        # Symbol-keyed cache from chrome.storage.local; pick closest symbol match.
        target = _norm_symbol(symbol)
        selected = None
        for key, value in raw.items():
            if _norm_symbol(key) == target and isinstance(value, dict):
                selected = value
                break
        if selected is None and raw:
            # Conservative fallback: use first dict-like value rather than dropping chart telemetry.
            selected = next((v for v in raw.values() if isinstance(v, dict)), None)

    if not isinstance(selected, dict):
        return None

    out: dict = {}
    for tf, item in selected.items():
        tf_norm = str(tf).lower()
        if tf_norm not in tf_keys or not isinstance(item, dict):
            continue
        sentiment = str(item.get("sentiment") or "NEUTRAL").upper()
        if sentiment not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            sentiment = "NEUTRAL"
        out[tf_norm] = {
            "sentiment": sentiment,
            "indicators": item.get("indicators", []),
            "ohlc": item.get("ohlc"),
            "updated_at": item.get("updated_at") or item.get("seen_at") or item.get("changed_at"),
            "seen_at": item.get("seen_at"),
            "changed_at": item.get("changed_at"),
        }

    return out or None


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # suppress access log

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _respond(self, code: int, body: bytes = b'{"ok":true}'):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError) as e:
            # Client closed connection prematurely (common when popup is closed during poll)
            log.debug("[bridge] response failed (client disconnected): %s", e)
        except Exception as e:
            log.warning("[bridge] response failed: %s", e)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        global _bridge_enabled
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            status = "ok" if _bridge_enabled else "stopped"
            body = json.dumps({
                "status": status,
                "enabled": _bridge_enabled,
                "service": "nsebot_bridge",
                "code_version": BRIDGE_CODE_VERSION,
            }).encode("utf-8")
            self._respond(200 if _bridge_enabled else 503, body)

        elif parsed.path == "/alerts":
            # Popup polls this to show backend alerts in the extension UI
            qs     = parse_qs(parsed.query)
            symbol = qs.get("symbol", [None])[0]
            limit  = int(qs.get("limit", ["30"])[0])
            rows   = get_alert_history(symbol or None, limit=limit)
            alerts = []
            for r in rows:
                flat = {k: v for k, v in r.items() if k != "detail_json"}
                try:
                    detail = json.loads(r.get("detail_json") or "{}")
                    flat.update(detail)   # merge detail fields so popup can use them directly
                except Exception:
                    pass
                alerts.append(flat)
            body = json.dumps({"alerts": alerts}).encode("utf-8")
            self._respond(200, body)

        else:
            self._respond(404, b'{"error":"not found"}')

    def do_POST(self):
        global _bridge_enabled
        try:
            data = self._read_json()
        except Exception:
            self._respond(400, b'{"error":"bad json"}'); return

        fetched_at = datetime.now(timezone.utc).isoformat()

        # ── Control endpoints ──────────────────────────────────────────────
        if self.path == "/control/start":
            _bridge_enabled = True
            self._respond(200, b'{"ok":true,"status":"running"}')
            return

        if self.path == "/control/stop":
            _bridge_enabled = False
            self._respond(200, b'{"ok":true,"status":"stopped"}')
            log.info("Stop requested via /control/stop — bridge paused")
            return

        if not _bridge_enabled:
            self._respond(503, b'{"ok":false,"error":"bridge_stopped"}')
            return

        # ── Clear alerts (all or per-symbol) ───────────────────────────────
        if self.path.startswith("/alerts/clear"):
            from urllib.parse import urlparse, parse_qs
            qs     = parse_qs(urlparse(self.path).query)
            symbol = qs.get("symbol", [None])[0]
            n = delete_alerts(symbol or None)
            log.info("[bridge] cleared %d alerts (scope: %s)", n, symbol or "ALL")
            self._respond(200, json.dumps({"ok": True, "deleted": n, "scope": symbol or "ALL"}).encode("utf-8"))
            return

        # ── Ingest single alert ────────────────────────────────────────────
        if self.path == "/ingest":
            # Extension sends this just before snapshot. We handle Telegram in snapshot
            # to ensure Bot Intelligence summary is sent BEFORE individual anomalies.
            log.info("[bridge] client alert: %s %s", data.get("alert_type"), data.get("symbol"))
            self._respond(200); return

        # ── Ingest full snapshot ───────────────────────────────────────────
        if self.path == "/ingest/snapshot":
            symbol     = data.get("symbol", "UNKNOWN")
            underlying = float(data.get("underlying", 0))
            expiry     = data.get("expiry", fetched_at[:10])
            forced     = bool(data.get("force", False))
            is_baseline = bool(data.get("is_baseline", False))

            rows = [{
                "fetched_at": fetched_at, "symbol": symbol, "expiry": expiry,
                "strike": r.get("strike"), "option_type": r.get("option_type"),
                "ltp": r.get("ltp"), "oi": r.get("oi"), "oi_change": None,
                "volume": r.get("volume"), "iv": r.get("iv"), "bid": None, "ask": None,
                "delta": r.get("delta"), "underlying_price": underlying,
                "fetcher_source": "chrome_extension",
            } for r in data.get("strikes", [])]

            if rows:
                if is_baseline:
                    # First scan after symbol switch — persist only, no detection
                    insert_snapshots(rows)
                    insert_underlying_price(symbol, underlying, None, fetched_at)
                    log.info("[bridge] baseline snapshot %s | %d strikes | skip detect",
                             symbol, len(rows))
                    self._respond(200); return

                oc = {
                    "symbol": symbol,
                    "underlying_price": underlying,
                    "expiry": expiry,
                    "strikes": data.get("strikes", []),
                    "source": "chrome_extension",
                }
                chart_indicators = _normalize_chart_indicators(data.get("chart_indicators"), symbol)
                alerts, scan_context = detect_anomalies(oc, fetched_at, chart_indicators=chart_indicators)
                new_alerts = []
                suppressed = 0
                for a in alerts:
                    if is_duplicate(a):
                        suppressed += 1
                    else:
                        new_alerts.append(a)

                digest_id, digest_msg = build_digest(
                    symbol, new_alerts, fetched_at,
                    scan_context=scan_context,
                )
                for a in new_alerts:
                    a["digest_id"] = digest_id
                sent_digest = send_text(digest_msg)
                if not sent_digest:
                    log.warning("[bridge] digest send failed | %s | %d alerts",
                                symbol, len(new_alerts))

                for a in new_alerts:
                    aid = insert_alert(a)
                    record_alert(a)
                    # Individual message only for HIGH when digest failed
                    if a.get("severity") == INDIVIDUAL_ALERT_MIN_SEVERITY and not sent_digest:
                        if send_alert(a):
                            mark_telegram_sent(aid)
                    elif sent_digest:
                        mark_telegram_sent(aid)

                diag = (scan_context or {}).get("diagnostics", {})
                summary_alert = {
                    "fired_at": fetched_at,
                    "symbol": symbol,
                    "alert_type": "SCAN_SUMMARY",
                    "strike": None,
                    "option_type": None,
                    "expiry": expiry,
                    "detail_json": json.dumps({
                        "generated_alerts": len(alerts),
                        "new_alerts": len(new_alerts),
                        "dedup_suppressed": suppressed,
                        **diag,
                    }),
                    "telegram_sent": 1 if sent_digest else 0,
                    "severity": "LOW",
                    "digest_id": digest_id,
                }
                insert_alert(summary_alert)

                # Persist after detection so deltas are correct on next scan
                insert_snapshots(rows)
                insert_underlying_price(symbol, underlying, None, fetched_at)

                src = "FORCE" if forced else "scheduled"
                log.info(
                    "[bridge] %s snapshot %s | %d strikes | generated=%d new=%d dedup=%d | maxOI=%.2f%% maxATM_LTP=%.2f%% PCR=%s prevPCR=%s dPCR=%s",
                    src, symbol, len(rows), len(alerts), len(new_alerts), suppressed,
                    float(diag.get("max_oi_delta_pct") or 0),
                    float(diag.get("max_atm_ltp_delta_pct") or 0),
                    f"{diag.get('pcr'):.3f}" if diag.get("pcr") is not None else "n/a",
                    f"{diag.get('prev_pcr'):.3f}" if diag.get("prev_pcr") is not None else "n/a",
                    f"{diag.get('pcr_delta'):+.3f}" if diag.get("pcr_delta") is not None else "n/a",
                )
                if not alerts:
                    log.info(
                        "[bridge] no alert reason | %s | max OI %.2f%% < 40.00, ATM LTP %.2f%% < 8.00, PCR delta %s < 0.25",
                        symbol,
                        float(diag.get("max_oi_delta_pct") or 0),
                        float(diag.get("max_atm_ltp_delta_pct") or 0),
                        "n/a" if diag.get("pcr_delta") is None else f"{abs(diag.get('pcr_delta')):.3f}",
                    )

            self._respond(200); return

        self._respond(404, b'{"error":"unknown endpoint"}')


def _shutdown():
    global _server
    if _server:
        _server.shutdown()


def run():
    global _server
    init_db()
    _server = HTTPServer((HOST, PORT), BridgeHandler)
    log.info("Extension bridge  http://%s:%d  (Ctrl+C to stop) | code=%s",
             HOST, PORT, BRIDGE_CODE_VERSION)

    def _sig(sig, frame):
        log.info("Signal received — shutting down")
        threading.Thread(target=_shutdown, daemon=True).start()

    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    _server.serve_forever()
    log.info("Bridge stopped.")


if __name__ == "__main__":
    run()
