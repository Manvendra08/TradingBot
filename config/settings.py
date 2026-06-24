import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (works whether you run from root or a subdirectory)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH  = DATA_DIR / "nsebot.db"
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

WATCH_NSE = ["NIFTY", "BANKNIFTY"]
WATCH_BSE = ["SENSEX"]
WATCH_MCX = ["NATURALGAS", "CRUDEOIL"]
WATCH_SYMBOLS = WATCH_NSE + WATCH_BSE + WATCH_MCX

FETCH_INTERVAL_MINUTES = 5

HTTP_TIMEOUT_SECONDS  = 15
HTTP_MAX_RETRIES      = 3
HTTP_BACKOFF_FACTOR   = 2



def _optional_env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


# ── Broker Selection ────────────────────────────────────────────────────────────────────────────────────────
ACTIVE_BROKER = os.environ.get("ACTIVE_BROKER", "zerodha").lower()

# ── Dhan Credentials ────────────────────────────────────────────────────────────────────────────────────
DHAN_CLIENT_ID   = _optional_env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _optional_env("DHAN_ACCESS_TOKEN")
DHAN_BASE_URL    = _optional_env("DHAN_BASE_URL", "https://api.dhan.co/v2")

# ── Shoonya / Finvasia Credentials ───────────────────────────────────────────────────────────────────────
SHOONYA_USER_ID   = _optional_env("SHOONYA_USER_ID")
SHOONYA_PASSWORD  = _optional_env("SHOONYA_PASSWORD")
SHOONYA_TOTP_KEY  = _optional_env("SHOONYA_TOTP_KEY")
SHOONYA_VENDOR_CODE = _optional_env("SHOONYA_VENDOR_CODE")
SHOONYA_API_SECRET  = _optional_env("SHOONYA_API_SECRET")
SHOONYA_IMEI        = _optional_env("SHOONYA_IMEI", "abc1234")

# ── Zerodha Credentials ─────────────────────────────────────────────────────────────────────────────────────────
ZERODHA_API_KEY    = _optional_env("ZERODHA_API_KEY")
ZERODHA_API_SECRET = _optional_env("ZERODHA_API_SECRET")
ZERODHA_ACCESS_TOKEN = _optional_env("ZERODHA_ACCESS_TOKEN")

# ── ICICIDirect Credentials ────────────────────────────────────────────────────────────────────────────────────────
ICICI_API_KEY     = _optional_env("ICICI_API_KEY")
ICICI_API_SECRET  = _optional_env("ICICI_API_SECRET")
ICICI_SESSION_TOKEN = _optional_env("ICICI_SESSION_TOKEN")

# ── TradingView Credentials ───────────────────────────────────────────────────────────────────────────────────────
TV_USERNAME          = _optional_env("TV_USERNAME")
TV_PASSWORD          = _optional_env("TV_PASSWORD")
TV_SESSIONID         = _optional_env("TV_SESSIONID")

# ── Market Windows ─────────────────────────────────────────────────────────────────────────────────────────
# Format: (open_time, close_time, weekdays)  — weekdays: 0=Mon … 6=Sun
MARKET_WINDOWS = {
    "NSE_INDEX":     ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "BSE_INDEX":     ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NSE_EQUITY":    ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NFO":           ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "MCX_COMMODITY": ("09:00", "23:30", [0, 1, 2, 3, 4, 5]),  # Saturday MCX session included
}

# ── Symbol → Market Window mapping ───────────────────────────────────────────────────────────────────────
SYMBOL_MARKET = {
    "NIFTY":      "NSE_INDEX",
    "BANKNIFTY":  "NSE_INDEX",
    "FINNIFTY":   "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "SENSEX":     "BSE_INDEX",
    "NATURALGAS": "MCX_COMMODITY",
    "CRUDEOIL":   "MCX_COMMODITY",
    "GOLD":       "MCX_COMMODITY",
    "SILVER":     "MCX_COMMODITY",
}

# ── Lot Sizes ────────────────────────────────────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   60,
    "MIDCPNIFTY": 75,
    "SENSEX":     20,
    "NATURALGAS": 1250,
    "CRUDEOIL":   100,
    "GOLD":       100,
    "SILVER":     30,
}

# Default lots per trade (used as fallback when capital allocator is not active)
DEFAULT_LOTS_PER_TRADE = 10

# ── Strike Step Sizes ────────────────────────────────────────────────────────────────────────────────────────────
STRIKE_STEPS = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,
    "NATURALGAS": 5,
    "CRUDEOIL":   50,
    "GOLD":       100,
    "SILVER":     100,
}

# ── Dhan Security IDs ────────────────────────────────────────────────────────────────────────────────────────────
DHAN_SECURITY_IDS = {
    "NIFTY":      13,
    "BANKNIFTY":  25,
    "FINNIFTY":   27,
    "MIDCPNIFTY": 442,
    "SENSEX":     51,
    # FIX #15: MCX contract IDs expire at month-end.  These IDs MUST be updated
    # manually before each monthly rollover (or automated via Dhan instrument dump).
    # Update procedure: download https://images.dhan.co/api-data/api-scrip-master.csv,
    # filter SEM_SMST_SECURITY_ID where SEM_TRADING_SYMBOL matches the near-month
    # continuous contract (e.g. NATURALGAS25JUNFUT), and replace the values below.
    "NATURALGAS": 434817,      # NATURALGAS JUN 2026 FUT  <-- update on rollover
    "CRUDEOIL":   435021,      # CRUDEOIL   JUN 2026 FUT  <-- update on rollover
    "GOLD":       459277,      # GOLD       JUN 2026 FUT  <-- update on rollover
    "SILVER":     464150,      # SILVER     JUL 2026 FUT  <-- update on rollover
}
DHAN_FALLBACK_EXPIRIES = {
    "NATURALGAS": "2026-06",
    "CRUDEOIL": "2026-06",
    "GOLD": "2026-06",
    "SILVER": "2026-07",
}
DHAN_SEGMENTS = {
    "NIFTY": "IDX_I",
    "BANKNIFTY": "IDX_I",
    "FINNIFTY": "IDX_I",
    "MIDCPNIFTY": "IDX_I",
    "SENSEX": "BSE_IND",
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
DISCORD_WEBHOOK_URL = _optional_env("DISCORD_WEBHOOK_URL")

# ── Dashboard Authentication ────────────────────────────────────────────────────────────────────────────
# FIX #13: Removed insecure admin/admin defaults.
# Both vars MUST be set in .env — the dashboard will refuse to start if either
# is absent.  Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in your .env file.
DASHBOARD_USERNAME = _optional_env("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = _optional_env("DASHBOARD_PASSWORD")

STRIKES_AROUND_ATM  = 10

# ── Fetcher Priority ────────────────────────────────────────────────────────────────────────────────────────────
# Order in which fetchers are tried for NSE indices. MCX commodities have
# their own priority: ["dhan_commodity", "moneycontrol", "dhan", "dhan_headless"]
FETCHER_PRIORITY    = ["nse_public"]

LOG_LEVEL           = "INFO"
LOG_ROTATION        = "midnight"
LOG_BACKUP_COUNT    = 30

# ── Per-symbol threshold overrides ──────────────────────────────────────────────────────────────────────────────
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


# ── Anomaly Detection Thresholds ────────────────────────────────────────────────────────────────────────────────────
OI_SPIKE_THRESHOLD_PCT         = 15.0    # % change in OI to trigger spike alert
PRICE_SPIKE_THRESHOLD_PCT      = 2.0     # % change in LTP to trigger price spike
PCR_EXTREME_LOW                = 0.5     # PCR below this is extreme bearish
PCR_EXTREME_HIGH               = 1.8     # PCR above this is extreme bullish
PCR_SHIFT_THRESHOLD            = 0.3     # min PCR change to trigger alert
PCR_EXTREME_SEVERITY_BAND      = 0.1     # band around extremes for severity bump
IV_SPIKE_ATM_THRESHOLD         = 20.0    # % IV change at ATM to trigger alert
MAX_PAIN_SHIFT_THRESHOLD       = 50      # rupees max pain shift to trigger alert
SEVERITY_HIGH_MULT             = 1.5     # multiplier for HIGH severity thresholds
SEVERITY_MED_MULT              = 1.0     # multiplier for MEDIUM severity thresholds
BUILDUP_OI_MIN_PCT             = 10.0    # min OI change % for buildup detection
BUILDUP_LTP_MIN_PCT            = 3.0     # min LTP change % for buildup detection
OTM_STRIKE_RANGE               = 3       # strikes to check for OTM unusual moves
OTM_OI_SPIKE_PCT               = 20.0    # OI spike % threshold for OTM unusual
VOLUME_AGGRESSION_HIGH         = 2.5     # volume multiplier for high aggression
VOLUME_AGGRESSION_LOW          = 1.5     # volume multiplier for low aggression
IV_CRUSH_THRESHOLD             = 15.0    # % IV drop to trigger crush alert
STRADDLE_DELTA_PCT             = 5.0     # delta move % for straddle premium alert
ATM_LEG_MOVE_PCT               = 2.0     # ATM premium move % for straddle
PCR_VELOCITY_WINDOW            = 3       # number of snapshots to track PCR velocity
MIN_OI_THRESHOLD               = 1000    # min OI for a strike to be considered
ALERT_COOLDOWN_MINUTES         = 60      # don't re-alert same type within N minutes
ALERT_COOLDOWN_HIGH_MINUTES    = 30      # shorter cooldown for HIGH severity
INDIVIDUAL_ALERT_MIN_SEVERITY  = "LOW"   # min severity to send individual alerts
DEDUP_CLUSTER_STRIKES          = 2       # cluster strikes within N steps of fired key

# Research mode: True = EXPERIMENTAL trades allowed; False = CORE only
PAPER_RESEARCH_MODE = os.environ.get("PAPER_RESEARCH_MODE", "true").lower() == "true"

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

# ── Trend-Based Trading Logic ──────────────────────────────────────────────────────────────────────────────────────
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

# ── Regime Detection ──────────────────────────────────────────────────────────────────────────────────────────────
# Thresholds for the explicit RANGE classification branch (#10).
# A session where abs(price_change_pct) < MAX_CHANGE and
# price_range_pct < MAX_RANGE is classified as RANGE rather than NO_TRADE.
REGIME_RANGE_MAX_CHANGE_PCT    = 0.5   # % half-session price drift
REGIME_RANGE_MAX_RANGE_PCT     = 1.5   # % high-low range over session

# ── Trade Plan ──────────────────────────────────────────────────────────────────────────────────────────────────
# Maximum strike-steps between current underlying and support/resistance
# before the level is considered "too far" and ATM is used instead (#13)
MAX_LEVEL_DISTANCE_STEPS       = 3

# Timeframe Strategy Settings
TIMEFRAME_OI_MIN_DIFF_PCT      = 0.005  # 0.5% of base side's previous OI

TF_CANDLE_BODY_MIN_RATIO      = 0.45
TF_CANDLE_CLOSE_POSITION_LONG  = 0.65   # close must be in top 35%
TF_CANDLE_CLOSE_POSITION_SHORT = 0.35   # close must be in bottom 35%
TF_BREAKOUT_RANGE_PCT          = 0.25   # 25% of prev candle range
TF_BREAKOUT_CMP_CAP_PCT        = 0.002  # 0.2% of CMP
TF_EXHAUSTION_HARD_BLOCK       = 4      # block at 4+ with weak OI
TF_REENTRY_COOLDOWN_BARS       = 1      # wait 1 3H bar after SL
TF_CONTINUATION_OI_MULTIPLIER  = 2.0    # 2x OI threshold for non-reversal entries


# ── Transaction Cost Model ──────────────────────────────────────────────────────────────────────────────────────────
# Per-trade round-trip costs in rupees (both legs combined).
# STT rates are approximate based on NSE/MCX exchange circulars.
# Options STT: 0.0625% of sell-side premium turnover (NSE)
# Futures STT: 0.01% of turnover (NSE/MCX)
# Brokerage: flat ₹20 per trade (Zerodha/Dhan/Shoonya typical)
TRANSACTION_COSTS = {
    "OPTIONS": {
        "flat_brokerage": 20.0,
        "stt_pct_turnover": 0.000625,   # 0.0625% of sell-side premium turnover
    },
    "FUTURES": {
        "flat_brokerage": 20.0,
        "stt_pct_turnover": 0.0001,     # 0.01% of futures turnover
    },
}


# ── AI Brain Settings ─────────────────────────────────────────────────────────────────────────────────────────────────
# Controls how the AI verdict influences trade decisions.
#   advisory   — AI verdict logged and displayed, but does NOT change trade outcomes
#   boost_only — AI can promote BLOCKED → TRIGGERED_EXPERIMENTAL (never veto)
#   full       — AI can both promote and veto trade decisions
AI_DECISION_MODE               = os.environ.get("AI_DECISION_MODE", "boost_only")

# Minimum AI confidence to influence trade decisions (boost/veto)
AI_MIN_CONFIDENCE_BOOST        = int(os.environ.get("AI_MIN_CONFIDENCE_BOOST", "80"))
AI_MIN_CONFIDENCE_VETO         = int(os.environ.get("AI_MIN_CONFIDENCE_VETO", "85"))

# Whether to call AI exit advisor for open trades during each scan
AI_EXIT_ADVISOR_ENABLED        = os.environ.get("AI_EXIT_ADVISOR_ENABLED", "false").lower() == "true"

# Disable LLM enrichment entirely when quota is exhausted or to reduce API calls
# Set to True to skip all Gemini/Groq/OpenRouter calls
DISABLE_LLM_ENRICHMENT         = os.environ.get("DISABLE_LLM_ENRICHMENT", "false").lower() == "true"

# ── MCX Commodity Confidence Floor ───────────────────────────────────────────────────────────────────────────────────
# MCX OI data is thinner than NSE index — a 10-contract CE spike can look
# significant on a percentage basis but carries little actual market conviction.
# Set a higher minimum confidence for MCX trades to filter out low-signal setups.
# 72 chosen: above the NSE core floor (70) but below reversal threshold (75),
# ensuring MCX entries require meaningful OI confluence without being too restrictive.
MCX_MIN_CONFIDENCE             = int(os.environ.get("MCX_MIN_CONFIDENCE", "72"))
MCX_SYMBOLS                    = frozenset({"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"})
