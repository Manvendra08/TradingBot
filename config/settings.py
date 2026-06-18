import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (works whether you run from root or a subdirectory)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _optional_env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


# ── Broker Selection ────────────────────────────────────────────────────────
# Set ACTIVE_BROKER to one of: "dhan", "shoonya", "zerodha", "icicidirect"
ACTIVE_BROKER = os.environ.get("ACTIVE_BROKER", "dhan").lower()

# ── Dhan Credentials ────────────────────────────────────────────────────────
DHAN_CLIENT_ID   = _optional_env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _optional_env("DHAN_ACCESS_TOKEN")

# ── Shoonya / Finvasia Credentials ─────────────────────────────────────────
SHOONYA_USER_ID   = _optional_env("SHOONYA_USER_ID")
SHOONYA_PASSWORD  = _optional_env("SHOONYA_PASSWORD")
SHOONYA_TOTP_KEY  = _optional_env("SHOONYA_TOTP_KEY")
SHOONYA_VENDOR_CODE = _optional_env("SHOONYA_VENDOR_CODE")
SHOONYA_API_SECRET  = _optional_env("SHOONYA_API_SECRET")
SHOONYA_IMEI        = _optional_env("SHOONYA_IMEI", "abc1234")

# ── Zerodha Credentials ─────────────────────────────────────────────────────
ZERODHA_API_KEY    = _optional_env("ZERODHA_API_KEY")
ZERODHA_API_SECRET = _optional_env("ZERODHA_API_SECRET")
ZERODHA_ACCESS_TOKEN = _optional_env("ZERODHA_ACCESS_TOKEN")

# ── ICICIDirect Credentials ─────────────────────────────────────────────────
ICICI_API_KEY     = _optional_env("ICICI_API_KEY")
ICICI_API_SECRET  = _optional_env("ICICI_API_SECRET")
ICICI_SESSION_TOKEN = _optional_env("ICICI_SESSION_TOKEN")

# ── Market Windows ─────────────────────────────────────────────────────────
# Format: (open_time, close_time, weekdays)  — weekdays: 0=Mon … 6=Sun
MARKET_WINDOWS = {
    "NSE_INDEX":     ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NSE_EQUITY":    ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NFO":           ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "MCX_COMMODITY": ("09:00", "23:30", [0, 1, 2, 3, 4, 5]),  # Saturday MCX session included
}

# ── Symbol → Market Window mapping ─────────────────────────────────────────
SYMBOL_MARKET = {
    "NIFTY":      "NSE_INDEX",
    "BANKNIFTY":  "NSE_INDEX",
    "FINNIFTY":   "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "NATURALGAS": "MCX_COMMODITY",
    "CRUDEOIL":   "MCX_COMMODITY",
    "GOLD":       "MCX_COMMODITY",
    "SILVER":     "MCX_COMMODITY",
}

# ── Lot Sizes ────────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY":      50,
    "BANKNIFTY":  15,
    "FINNIFTY":   40,
    "MIDCPNIFTY": 75,
    "NATURALGAS": 1250,
    "CRUDEOIL":   100,
    "GOLD":       100,
    "SILVER":     30,
}

# Default lots per trade (used as fallback when capital allocator is not active)
DEFAULT_LOTS_PER_TRADE = 1

# ── Strike Step Sizes ────────────────────────────────────────────────────────
STRIKE_STEPS = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "NATURALGAS": 5,
    "CRUDEOIL":   50,
    "GOLD":       100,
    "SILVER":     100,
}

# ── Dhan Security IDs ────────────────────────────────────────────────────────
DHAN_SECURITY_IDS = {
    "NIFTY":      13,
    "BANKNIFTY":  25,
    "FINNIFTY":   27,
    "MIDCPNIFTY": 442,
    "NATURALGAS": 434817,      # NATURALGAS JUN FUT
    "CRUDEOIL":   435021,      # CRUDEOIL JUN FUT
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

TF_CANDLE_BODY_MIN_RATIO      = 0.45
TF_CANDLE_CLOSE_POSITION_LONG  = 0.65   # close must be in top 35%
TF_CANDLE_CLOSE_POSITION_SHORT = 0.35   # close must be in bottom 35%
TF_BREAKOUT_RANGE_PCT          = 0.25   # 25% of prev candle range
TF_BREAKOUT_CMP_CAP_PCT        = 0.002  # 0.2% of CMP
TF_EXHAUSTION_HARD_BLOCK       = 4      # block at 4+ with weak OI
TF_REENTRY_COOLDOWN_BARS       = 1      # wait 1 3H bar after SL
TF_CONTINUATION_OI_MULTIPLIER  = 2.0    # 2x OI threshold for non-reversal entries


# ── Transaction Cost Model ─────────────────────────────────────────────────
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


# ── AI Brain Settings ─────────────────────────────────────────────────────
# Controls how the AI verdict influences trade decisions.
#   advisory   — AI verdict logged and displayed, but does NOT change trade outcomes (default, safe)
#   boost_only — AI can promote BLOCKED → TRIGGERED_EXPERIMENTAL (never veto)
#   full       — AI can both promote and veto trade decisions
AI_DECISION_MODE               = os.environ.get("AI_DECISION_MODE", "advisory")

# Minimum AI confidence to influence trade decisions (boost/veto)
AI_MIN_CONFIDENCE_BOOST        = int(os.environ.get("AI_MIN_CONFIDENCE_BOOST", "80"))
AI_MIN_CONFIDENCE_VETO         = int(os.environ.get("AI_MIN_CONFIDENCE_VETO", "85"))

# Whether to call AI exit advisor for open trades during each scan
AI_EXIT_ADVISOR_ENABLED        = os.environ.get("AI_EXIT_ADVISOR_ENABLED", "false").lower() == "true"
