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
WATCH_NSE = ["NIFTY", "BANKNIFTY"]
WATCH_MCX: list[str] = ["NATURALGAS", "CRUDEOIL"]   # MCX commodity futures to watch
WATCH_SYMBOLS = WATCH_NSE + WATCH_MCX   # merged for backward compat

# ── Lot Sizes (for P&L calculation in ₹) ──────────────────────────────────
LOT_SIZES = {
    "NIFTY": 65,           # NIFTY options/futures lot size
    "BANKNIFTY": 30,       # BANKNIFTY options/futures lot size
    "FINNIFTY": 60,        # FINNIFTY options/futures lot size
    "MIDCPNIFTY": 120,      # MIDCPNIFTY options/futures lot size
    "NATURALGAS": 1250,    # NATURALGAS futures lot size (MCX)
    "CRUDEOIL": 100,       # CRUDEOIL futures lot size (MCX)
    "GOLD": 100,           # GOLD futures lot size (MCX)
    "SILVER": 30,          # SILVER futures lot size (MCX)
}

# Default number of lots per trade (can be overridden per symbol)
DEFAULT_LOTS_PER_TRADE = 10

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
MIN_OI_THRESHOLD          = 50      # min absolute current/previous OI to flag spikes/buildup

# ── Severity thresholds (multipliers of base threshold) ───────────────────
SEVERITY_HIGH_MULT        = 2.5     # e.g. OI Δ ≥ 100% = HIGH
SEVERITY_MED_MULT         = 1.5     # e.g. OI Δ ≥ 60%  = MEDIUM
# below SEVERITY_MED_MULT  = LOW

# ── Alert quality ──────────────────────────────────────────────────────────
INDIVIDUAL_ALERT_MIN_SEVERITY = "HIGH"   # only HIGH alerts get individual TG msgs
ALERT_COOLDOWN_HIGH_MINUTES   = 30       # cooldown for HIGH severity
DEDUP_CLUSTER_STRIKES         = 2        # strikes within ±N suppressed in cluster

# ── HTTP / fetchers ────────────────────────────────────────────────────────
FETCHER_PRIORITY = ["nse_public", "dhan", "dhan_headless", "dhan_commodity", "moneycontrol"]
HTTP_TIMEOUT_SECONDS  = 15
HTTP_MAX_RETRIES      = 3
HTTP_BACKOFF_FACTOR   = 2

DHAN_CLIENT_ID    = _optional_env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _optional_env("DHAN_ACCESS_TOKEN")   # Valid for 24 hours
DHAN_API_KEY      = _optional_env("DHAN_API_KEY")
DHAN_API_SECRET   = _optional_env("DHAN_API_SECRET")
DHAN_BASE_URL     = "https://api.dhan.co/v2"

TV_USERNAME  = _optional_env("TV_USERNAME")   # TradingView login (required for MCX data)
TV_PASSWORD  = _optional_env("TV_PASSWORD")   # without these, MCX charts return None
TV_SESSIONID = _optional_env("TV_SESSIONID")  # Optional TradingView sessionid cookie (bypasses CAPTCHA)

DHAN_SECURITY_IDS = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
    "FINNIFTY": 27,
    "MIDCPNIFTY": 442,
    "NATURALGAS": 504265,  # NATURALGAS JUN FUT, Dhan master 2026-05-28
    "CRUDEOIL": 499095,    # CRUDEOIL JUN FUT
    "GOLD": 459277,        # GOLD JUN FUT
    "SILVER": 464150,      # SILVER JUL FUT
}
DHAN_SEGMENTS = {
    "NIFTY": "IDX_I",
    "BANKNIFTY": "IDX_I",
    "FINNIFTY": "IDX_I",
    "MIDCPNIFTY": "IDX_I",
    "NATURALGAS": "MCX_COMM",
    "CRUDEOIL": "MCX_COMM",
    "GOLD": "MCX_COMM",
    "SILVER": "MCX_COMM",
}

NSE_BASE_URL         = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
NSE_EQUITY_OC_URL    = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/option-chain",
}

TELEGRAM_BOT_TOKEN = _optional_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _optional_env("TELEGRAM_CHAT_ID")

# ── Dashboard Authentication ────────────────────────────────────────────────
DASHBOARD_USERNAME = _optional_env("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = _optional_env("DASHBOARD_PASSWORD", "admin")

STRIKES_AROUND_ATM  = 10
LOG_LEVEL           = "INFO"
LOG_ROTATION        = "midnight"
LOG_BACKUP_COUNT    = 30

# ── Per-symbol threshold overrides ────────────────────────────────────────
# MCX commodities have lower absolute OI volumes than NSE indices.
# Use tighter thresholds so the engine fires on meaningful but smaller moves.
SYMBOL_THRESHOLD_OVERRIDES: dict[str, dict] = {
    "NATURALGAS": {
        "oi_threshold":        10.0,
        "ltp_threshold":        4.0,
        "pcr_shift_threshold":  0.10,
        "buildup_oi_min_pct":  10.0,
        "buildup_ltp_min_pct":  3.0,
    },
    "CRUDEOIL": {
        "oi_threshold":        15.0,
        "ltp_threshold":        5.0,
        "pcr_shift_threshold":  0.15,
        "buildup_oi_min_pct":  12.0,
        "buildup_ltp_min_pct":  4.0,
    },
    "GOLD": {
        "oi_threshold":        20.0,
        "ltp_threshold":        5.0,
        "pcr_shift_threshold":  0.20,
        "buildup_oi_min_pct":  15.0,
        "buildup_ltp_min_pct":  5.0,
    },
}


def get_symbol_thresholds(symbol: str) -> dict:
    """Return threshold overrides for the given symbol, or empty dict for defaults."""
    base = symbol.upper().split()[0]
    return SYMBOL_THRESHOLD_OVERRIDES.get(base, {})


# ── Trading System V2.2 ────────────────────────────────────────────────────

# Research mode: True = EXPERIMENTAL trades allowed; False = CORE only
PAPER_RESEARCH_MODE = True

# Trade decision thresholds — CORE (high-quality setups)
MIN_CONFIDENCE_CORE            = 70
MIN_ENTRY_QUALITY_CORE         = 60
MIN_TREND_ALIGNMENT_CORE       = 70
MIN_REGIME_SCORE_CORE          = 60

# Trade decision thresholds — EXPERIMENTAL (research / marginal setups)
MIN_CONFIDENCE_EXPERIMENTAL    = 50
MIN_ENTRY_QUALITY_EXPERIMENTAL = 40

# Reversal trade: higher confidence bar
REVERSAL_MIN_CONFIDENCE        = 75

# Risk engine — applies to paper trading too (overtrading distorts results)
MAX_OPEN_TRADES_PER_SYMBOL     = 2
MAX_OPEN_TRADES_TOTAL          = 5
MAX_TRADES_PER_SYMBOL_PER_DAY  = 4
MAX_DAILY_LOSS_RUPEES          = 200000
LOSS_COOLDOWN_MINUTES          = 30

# ── Trend-Based Trading Logic ──────────────────────────────────────────────
# Mode: "conservative" | "balanced" | "aggressive" | "hybrid"
TREND_FILTER_MODE              = "hybrid"

# Minimum non-fallback scan summaries required before any trend-based trade
# fires for a symbol. Prevents new symbols from getting TRIGGERED_CORE with
# zero trend validation. (#6)
TREND_MIN_SCANS                = 3

# Trend persistence: fraction of last N scans that must agree (0.0-1.0)
TREND_CONSISTENCY_THRESHOLD    = 0.6

# Momentum scoring: 0-100 score threshold to trigger trade
# Used as the momentum fallback gate in hybrid mode (#7)
MOMENTUM_SCORE_THRESHOLD       = 75

# ── Regime Detection ──────────────────────────────────────────────────────
# Thresholds for the explicit RANGE classification branch (#10).
# A session where abs(price_change_pct) < MAX_CHANGE and
# price_range_pct < MAX_RANGE is classified as RANGE rather than NO_TRADE.
REGIME_RANGE_MAX_CHANGE_PCT    = 0.5   # % half-session price drift
REGIME_RANGE_MAX_RANGE_PCT     = 1.5   # % high-low range over session

# ── Trade Plan ────────────────────────────────────────────────────────────
# Maximum strike-steps between current underlying and support/resistance
# before the level is considered "too far" and ATM is used instead (#13)
MAX_LEVEL_DISTANCE_STEPS       = 3

# Timeframe Strategy Settings
TIMEFRAME_OI_MIN_DIFF_PCT      = 0.005  # 0.5% of base side's previous OI
