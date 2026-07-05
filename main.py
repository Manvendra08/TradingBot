"""
NSEBOT entry point.

Usage:
  python main.py                          → start scheduler (blocking, waits for interval boundary)
  python main.py --now                    → start scheduler and trigger immediate scan on launch
  python main.py --once                   → single pipeline run and exit (test / manual)
  python main.py --once --symbols NIFTY   → single run for specific symbol(s) and exit
  python main.py --dashboard              → print FastAPI dashboard launch command
  python main.py --bridge                 → start Chrome Extension HTTP bridge

Credentials are loaded from .env if present, else from system environment.
"""

import argparse
import logging
import socket

# ── Force IPv4 globally ─────────────────────────────────────────────────────
# Zerodha Kite whitelists IPv4 only. When the OS prefers IPv6, requests
# originate from a non-whitelisted address and get rejected.
import sqlite3
import sys
from pathlib import Path

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# ── Disable SSL verification conflict globally ──────────────────────────────
# Recent Python/urllib3 versions raise ValueError when verify=False is used
# without explicitly disabling hostname checking.
#
# NOTE: We patch ONLY urllib3's context creation (used by requests/SDKs)
# rather than ssl._create_default_https_context (which affects ALL Python
# HTTPS including Gemini API, Telegram API, Google Drive). The urllib3-only
# patch limits scope to NSE-specific HTTP clients (Kite, Dhan) that require
# certificate bypass in certain network environments.
import ssl

import urllib3

try:
    _orig_create_context = urllib3.util.ssl_.create_urllib3_context

    def _patched_create_urllib3_context(cert_reqs=None, **kwargs):
        ctx = _orig_create_context(cert_reqs=cert_reqs, **kwargs)
        if cert_reqs == ssl.CERT_NONE:
            ctx.check_hostname = False
        return ctx

    urllib3.util.ssl_.create_urllib3_context = _patched_create_urllib3_context
except AttributeError:
    # Legacy urllib3 without create_urllib3_context
    pass
except Exception as e:
    print(f"[NSEBOT] Warning: Failed to patch urllib3 SSL context: {e}")

# ── Load .env first, before any config import ──────────────────────────────
try:
    from dotenv import load_dotenv

    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)
        print(f"[NSEBOT] Loaded credentials from {env_file}")
    else:
        print("[NSEBOT] No .env found — expecting credentials in system environment")
except ImportError:
    print(
        "[NSEBOT] python-dotenv not installed — reading credentials from system environment only"
    )
    print("         Install with: pip install python-dotenv")

# ── Now safe to import config (reads os.environ) ───────────────────────────
from config.logging_config import configure_logging
from src.models.schema import init_db

log = logging.getLogger("nsebot.main")


def main():
    parser = argparse.ArgumentParser(
        description="NSEBOT — NSE Option Chain Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                           # start scheduler (waits for next interval boundary)
  python main.py --now                     # start scheduler with an immediate scan on launch
  python main.py --once                    # one-shot run, all symbols and exit
  python main.py --once --symbols NIFTY    # one-shot for NIFTY only and exit
  python main.py --dashboard               # show FastAPI dashboard command
  python main.py --bridge                  # start extension HTTP bridge
        """,
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Start scheduler and trigger an immediate scan loop on launch",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run pipeline once immediately and exit"
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Print FastAPI dashboard launch command",
    )
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="Start Chrome Extension HTTP bridge on localhost:8765",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        metavar="SYM",
        help="Override WATCH_SYMBOLS for --once runs",
    )
    args = parser.parse_args()

    configure_logging("bridge" if args.bridge else "main")

    log.info("=" * 60)
    log.info("NSEBOT starting up")
    log.info("=" * 60)

    # Always ensure DB is initialised
    init_db()

    if args.dashboard:
        cmd = "python dashboard_server.py"
        print(f"\n  {cmd}\n")
        log.info("Dashboard command: %s", cmd)
        return

    if args.bridge:
        log.info("Starting Chrome Extension HTTP bridge ...")
        from src.extension_bridge import run

        run()
        return

    if args.once:
        from src.engine.pipeline import run_pipeline

        log.info(
            "One-shot pipeline run (TEST MODE)%s",
            f" for {args.symbols}" if args.symbols else " for all configured symbols",
        )
        run_pipeline(symbols=args.symbols or None, is_test=True)
        log.info("One-shot run complete (no database modifications were made).")
        return

    # Default: blocking scheduler
    log.info("Starting APScheduler — press Ctrl+C to stop")
    from src.scheduler.job_runner import start_scheduler

    start_scheduler(immediate=args.now)


if __name__ == "__main__":
    main()
