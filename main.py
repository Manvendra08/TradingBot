"""
NSEBOT entry point.

Usage:
  python main.py                          → start scheduler (blocking)
  python main.py --now                    → single pipeline run (test / manual)
  python main.py --now --symbols NIFTY    → single run for specific symbol(s)
  python main.py --dashboard              → print Streamlit launch command
  python main.py --bridge                 → start Chrome Extension HTTP bridge

Credentials are loaded from .env if present, else from system environment.
"""
import sys
import argparse
import logging
from pathlib import Path

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
    print("[NSEBOT] python-dotenv not installed — reading credentials from system environment only")
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
  python main.py                           # start 15-min scheduler
  python main.py --now                     # one-shot run, all symbols
  python main.py --now --symbols NIFTY     # one-shot for NIFTY only
  python main.py --dashboard               # show Streamlit command
  python main.py --bridge                  # start extension HTTP bridge
        """,
    )
    parser.add_argument("--now",       action="store_true",
                        help="Run pipeline once immediately and exit")
    parser.add_argument("--dashboard", action="store_true",
                        help="Print Streamlit dashboard launch command")
    parser.add_argument("--bridge",    action="store_true",
                        help="Start Chrome Extension HTTP bridge on localhost:8765")
    parser.add_argument("--symbols",   nargs="*", metavar="SYM",
                        help="Override WATCH_SYMBOLS for --now runs")
    args = parser.parse_args()

    configure_logging("bridge" if args.bridge else "main")

    log.info("=" * 60)
    log.info("NSEBOT starting up")
    log.info("=" * 60)

    # Always ensure DB is initialised
    init_db()

    if args.dashboard:
        cmd = "streamlit run src/dashboard/app.py"
        print(f"\n  {cmd}\n")
        log.info("Dashboard command: %s", cmd)
        return

    if args.bridge:
        log.info("Starting Chrome Extension HTTP bridge ...")
        from src.extension_bridge import run
        run()
        return

    if args.now:
        from src.engine.pipeline import run_pipeline
        log.info("One-shot pipeline run%s",
                 f" for {args.symbols}" if args.symbols else " for all configured symbols")
        run_pipeline(symbols=args.symbols or None)
        log.info("One-shot run complete.")
        return

    # Default: blocking scheduler
    log.info("Starting APScheduler — press Ctrl+C to stop")
    from src.scheduler.job_runner import start_scheduler
    start_scheduler()


if __name__ == "__main__":
    main()
