"""
NSEBOT Central Configuration
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH  = DATA_DIR / "nsebot.db"
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


def _require_env(key: str) -> str:
    """Return env var value or raise ValueError if missing/placeholder."""
    val = os.getenv(key, "")
    if not val or val.startswith("YOUR_"):
        raise ValueError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in real values."
        )
    return val


def _optional_env(key: str, default: str = "") -> str:
    """Return env var value or default; never raises."""
    val = os.getenv(key, default)
    return val if val and not val.startswith("YOUR_") else default


# ── Symbols ────────────────────────────────────────────────────────────────
WATCH_NSE = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
WATCH_MCX: list[str] = []           # e.g. ["NATURALGAS", "CRUDEOIL", "GOLD"]
WATCH_SYMBOLS = WATCH_NSE + WATCH_MCX   # merged for backward compat

# ── Per-class market windows: (open, close, weekdays) ─────────────────────
MARKET_WINDOWS: dict[str, tuple[str, str, list[int]]] = {
    "NSE_INDEX":     ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NSE_STOCK":     ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "MCX_COMMODITY": ("09:00", "23:30", [0, 1, 2, 3, 4]),
    "MCX_AGRI":      ("09:00", "21:00", [0, 1, 2, 3, 4]),
}

# Legacy single-window kept for backward compat / fallback
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"
FETCH_INTERVAL_MINUTES = 5
MARKET_DAYS  = [0, 1, 2, 3, 4]

# ── Anomaly thresholds ─────────────────────────────────────────────────────
OI_SPIKE_THRESHOLD_PCT    = 40.0
PRICE_SPIKE_THRESHOLD_PCT = 2.5
PCR_EXTREME_LOW           = 0.4
PCR_EXTREME_HIGH          = 1.8
PCR_SHIFT_THRESHOLD       = 0.25
PCR_EXTREME_SEVERITY_BAND = 0.2   # denominator for PCR extreme severity scoring
IV_SPIKE_ATM_THRESHOLD    = 7.0
MAX_PAIN_SHIFT_THRESHOLD  = 100
ALERT_COOLDOWN_MINUTES    = 60

# ── New v2.6 thresholds ────────────────────────────────────────────────────
BUILDUP_OI_MIN_PCT        = 25.0    # min OI Δ to classify buildup type
BUILDUP_LTP_MIN_PCT       = 10.0    # min LTP Δ to classify buildup type
OTM_STRIKE_RANGE          = 5       # strikes beyond ATM±N = "OTM unusual" zone
OTM_OI_SPIKE_PCT          = 50.0    # OI spike threshold for far-OTM activity
VOLUME_AGGRESSION_HIGH    = 2.0     # vol/oi-delta > this = aggressive flow
VOLUME_AGGRESSION_LOW     = 0.3     # vol/oi-delta < this = passive positioning
IV_CRUSH_THRESHOLD        = -5.0    # IV drop of this magnitude = IV crush
STRADDLE_DELTA_PCT        = 10.0    # straddle premium Δ % to fire
ATM_LEG_MOVE_PCT          = 8.0     # per-leg ATM LTP Δ% to fire
PCR_VELOCITY_WINDOW       = 3       # scans for PCR rate-of-change calc

# ── Severity thresholds (multipliers of base threshold) ───────────────────
SEVERITY_HIGH_MULT        = 2.5     # e.g. OI Δ ≥ 100% = HIGH
SEVERITY_MED_MULT         = 1.5     # e.g. OI Δ ≥ 60%  = MEDIUM
# below SEVERITY_MED_MULT  = LOW

# ── Alert quality ──────────────────────────────────────────────────────────
INDIVIDUAL_ALERT_MIN_SEVERITY = "HIGH"   # only HIGH alerts get individual TG msgs
ALERT_COOLDOWN_HIGH_MINUTES   = 30       # cooldown for HIGH severity
DEDUP_CLUSTER_STRIKES         = 2        # strikes within ±N suppressed in cluster

# ── HTTP / fetchers ────────────────────────────────────────────────────────
FETCHER_PRIORITY = ["dhan", "nse_public", "scrapegraph", "upstox"]
HTTP_TIMEOUT_SECONDS  = 15
HTTP_MAX_RETRIES      = 3
HTTP_BACKOFF_FACTOR   = 2

DHAN_CLIENT_ID    = _optional_env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _optional_env("DHAN_ACCESS_TOKEN")   # validated at fetcher init
DHAN_BASE_URL     = "https://api.dhan.co/v2"
DHAN_SECURITY_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27}

NSE_BASE_URL         = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
NSE_EQUITY_OC_URL    = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/option-chain",
}

UPSTOX_ACCESS_TOKEN = _optional_env("UPSTOX_ACCESS_TOKEN")
UPSTOX_BASE_URL     = "https://api.upstox.com/v2"

TELEGRAM_BOT_TOKEN = _optional_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _optional_env("TELEGRAM_CHAT_ID")

STRIKES_AROUND_ATM  = 15
LOG_LEVEL           = "INFO"
LOG_ROTATION        = "midnight"
LOG_BACKUP_COUNT    = 30
