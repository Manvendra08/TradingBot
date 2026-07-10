import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (works whether you run from root or a subdirectory)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Global safeguard: Automatically redirect DB_PATH to test database when executing tests
import sys
_is_testing = (
    "pytest" in sys.modules or
    any("pytest" in arg or arg.startswith("test_") for arg in sys.argv) or
    (len(sys.argv) > 0 and (sys.argv[0].endswith("test_manual.py") or "test_" in os.path.basename(sys.argv[0])))
)
DB_PATH = DATA_DIR / "nsebot_test.db" if _is_testing else DATA_DIR / "nsebot.db"

LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

WATCH_NSE = ["NIFTY", "BANKNIFTY"]
WATCH_BSE = ["SENSEX"]
WATCH_MCX = ["NATURALGAS", "CRUDEOIL"]
WATCH_SYMBOLS = WATCH_NSE + WATCH_BSE + WATCH_MCX

FETCH_INTERVAL_MINUTES = 5

HTTP_TIMEOUT_SECONDS = 15
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_FACTOR = 2


def _optional_env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


# ── Broker Selection ────────────────────────────────────────────────────────────────────────────────────────
ACTIVE_BROKER = os.environ.get("ACTIVE_BROKER", "zerodha").lower()

# ── Dhan Credentials ────────────────────────────────────────────────────────────────────────────────────
DHAN_CLIENT_ID = _optional_env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _optional_env("DHAN_ACCESS_TOKEN")
DHAN_BASE_URL = _optional_env("DHAN_BASE_URL", "https://api.dhan.co/v2")

# ── Shoonya / Finvasia Credentials ───────────────────────────────────────────────────────────────────────
SHOONYA_USER_ID = _optional_env("SHOONYA_USER_ID")
SHOONYA_PASSWORD = _optional_env("SHOONYA_PASSWORD")
SHOONYA_TOTP_KEY = _optional_env("SHOONYA_TOTP_KEY")
SHOONYA_VENDOR_CODE = _optional_env("SHOONYA_VENDOR_CODE")
SHOONYA_API_SECRET = _optional_env("SHOONYA_API_SECRET")
SHOONYA_IMEI = _optional_env("SHOONYA_IMEI", "abc1234")

# ── Zerodha Credentials ─────────────────────────────────────────────────────────────────────────────────────────
ZERODHA_API_KEY = _optional_env("ZERODHA_API_KEY")
ZERODHA_API_SECRET = _optional_env("ZERODHA_API_SECRET")
ZERODHA_ACCESS_TOKEN = _optional_env("ZERODHA_ACCESS_TOKEN")

# ── ICICIDirect Credentials ────────────────────────────────────────────────────────────────────────────────────────
ICICI_API_KEY = _optional_env("ICICI_API_KEY")
ICICI_API_SECRET = _optional_env("ICICI_API_SECRET")
ICICI_SESSION_TOKEN = _optional_env("ICICI_SESSION_TOKEN")

# ── TradingView Credentials ───────────────────────────────────────────────────────────────────────────────────────
TV_USERNAME = _optional_env("TV_USERNAME")
TV_PASSWORD = _optional_env("TV_PASSWORD")
TV_SESSIONID = _optional_env("TV_SESSIONID")
# Set TV_DISABLE=true to skip all tvDatafeed auth attempts.
# Use this when TradingView credentials are stale or unavailable — chart data
# will come from Shoonya GetTimePriceSeries (primary) or Yahoo Finance (fallback).
TV_DISABLE: bool = os.environ.get("TV_DISABLE", "false").lower() == "true"

# ── NewsAPI.org ─────────────────────────────────────────────────────────────────────────────────────
NEWSAPI_KEY = _optional_env("NEWSAPI_KEY")

# ── Google Drive Backup ─────────────────────────────────────────────────────────────────────────────
GOOGLE_DRIVE_FOLDER_ID = _optional_env("GOOGLE_DRIVE_FOLDER_ID")


# ── Market Windows ─────────────────────────────────────────────────────────────────────────────────────────
# Format: (open_time, close_time, weekdays)  — weekdays: 0=Mon … 6=Sun
MARKET_WINDOWS = {
    "NSE_INDEX": ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "BSE_INDEX": ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NSE_EQUITY": ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "NFO": ("09:15", "15:30", [0, 1, 2, 3, 4]),
    "MCX_COMMODITY": (
        "09:00",
        "23:30",
        [0, 1, 2, 3, 4, 5],
    ),  # Saturday MCX session included
}

# ── Symbol → Market Window mapping ───────────────────────────────────────────────────────────────────────
SYMBOL_MARKET = {
    "NIFTY": "NSE_INDEX",
    "BANKNIFTY": "NSE_INDEX",
    "FINNIFTY": "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "SENSEX": "BSE_INDEX",
    "NATURALGAS": "MCX_COMMODITY",
    "CRUDEOIL": "MCX_COMMODITY",
    "GOLD": "MCX_COMMODITY",
    "SILVER": "MCX_COMMODITY",
}

# ── Lot Sizes ────────────────────────────────────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 75,
    "SENSEX": 20,
    "NATURALGAS": 1250,
    "CRUDEOIL": 100,
    "GOLD": 100,
    "SILVER": 30,
}

# Default lots per trade (used as fallback when capital allocator is not active)
DEFAULT_LOTS_PER_TRADE = 10

# ── Strike Step Sizes ────────────────────────────────────────────────────────────────────────────────────────────
STRIKE_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
    "NATURALGAS": 5,
    "CRUDEOIL": 50,
    "GOLD": 100,
    "SILVER": 100,
}

# ── Dhan Security IDs ────────────────────────────────────────────────────────────────────────────────────────────
DHAN_SECURITY_IDS = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
    "FINNIFTY": 27,
    "MIDCPNIFTY": 442,
    "SENSEX": 51,
    # FIX #15: MCX contract IDs expire at month-end.  These IDs MUST be updated
    # manually before each monthly rollover (or automated via Dhan instrument dump).
    # Update procedure: download https://images.dhan.co/api-data/api-scrip-master.csv,
    # filter SEM_SMST_SECURITY_ID where SEM_TRADING_SYMBOL matches the near-month
    # continuous contract (e.g. NATURALGAS25JUNFUT), and replace the values below.
    "NATURALGAS": 434817,  # NATURALGAS JUN 2026 FUT  <-- update on rollover
    "CRUDEOIL": 435021,  # CRUDEOIL   JUN 2026 FUT  <-- update on rollover
    "GOLD": 459277,  # GOLD       JUN 2026 FUT  <-- update on rollover
    "SILVER": 464150,  # SILVER     JUL 2026 FUT  <-- update on rollover
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

NSE_BASE_URL = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL = (
    "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
)
NSE_EQUITY_OC_URL = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

TELEGRAM_BOT_TOKEN = _optional_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _optional_env("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = _optional_env("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = _optional_env("GEMINI_API_KEY")
SAMBANOVA_API_KEY = _optional_env("SAMBANOVA_API_KEY")
OPENCODE_API_KEY = _optional_env("OPENCODE_API_KEY")
NVIDIA_API_KEY = _optional_env("NVIDIA_API_KEY")

# ── Dashboard Authentication ────────────────────────────────────────────────────────────────────────────
# FIX #13: Removed insecure admin/admin defaults.
# Both vars MUST be set in .env — the dashboard will refuse to start if either
# is absent.  Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in your .env file.
DASHBOARD_USERNAME = _optional_env("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = _optional_env("DASHBOARD_PASSWORD")

STRIKES_AROUND_ATM = 10

# ── Fetcher Priority ────────────────────────────────────────────────────────────────────────────────────────────
# Order in which fetchers are tried for NSE indices. MCX commodities have
# their own priority defined in router.py.
FETCHER_PRIORITY = ["shoonya", "paytm", "nse_public"]

LOG_LEVEL = "INFO"
LOG_ROTATION = "midnight"
LOG_BACKUP_COUNT = 30

# ── Per-symbol threshold overrides ──────────────────────────────────────────────────────────────────────────────
# MCX commodities have lower absolute OI volumes than NSE indices.
# Use tighter thresholds so the engine fires on meaningful but smaller moves.
SYMBOL_THRESHOLD_OVERRIDES: dict[str, dict] = {
    # ── NSE Indices (high liquidity, huge OI — need HIGHER thresholds) ──
    "NIFTY": {
        "oi_threshold": 30.0,        # most liquid, huge OI buffer
        "ltp_threshold": 4.0,
        "pcr_shift_threshold": 0.30,
        "buildup_oi_min_pct": 25.0,
        "buildup_ltp_min_pct": 6.0,
        "otm_oi_spike_pct": 40.0,
        "max_pain_shift_pct": 0.5,
    },
    "BANKNIFTY": {
        "oi_threshold": 25.0,        # very liquid but more volatile
        "ltp_threshold": 3.5,
        "pcr_shift_threshold": 0.25,
        "buildup_oi_min_pct": 20.0,
        "buildup_ltp_min_pct": 5.0,
        "otm_oi_spike_pct": 35.0,
        "max_pain_shift_pct": 0.5,
    },
    "FINNIFTY": {
        "oi_threshold": 20.0,        # less liquid than NIFTY/BANKNIFTY
        "ltp_threshold": 3.0,
        "pcr_shift_threshold": 0.20,
        "buildup_oi_min_pct": 18.0,
        "buildup_ltp_min_pct": 5.0,
        "otm_oi_spike_pct": 30.0,
        "max_pain_shift_pct": 1.0,
    },
    "MIDCPNIFTY": {
        "oi_threshold": 18.0,        # lower liquidity, higher volatility
        "ltp_threshold": 3.0,
        "pcr_shift_threshold": 0.20,
        "buildup_oi_min_pct": 15.0,
        "buildup_ltp_min_pct": 4.0,
        "otm_oi_spike_pct": 25.0,
        "max_pain_shift_pct": 1.0,
    },
    "SENSEX": {
        "oi_threshold": 28.0,        # similar to NIFTY, slightly less liquid
        "ltp_threshold": 4.0,
        "pcr_shift_threshold": 0.28,
        "buildup_oi_min_pct": 22.0,
        "buildup_ltp_min_pct": 6.0,
        "otm_oi_spike_pct": 38.0,
        "max_pain_shift_pct": 0.5,
    },
    # ── MCX Commodities (lower liquidity, different volatility profiles) ──
    "NATURALGAS": {
        "oi_threshold": 10.0,        # very volatile, low OI — tightest
        "ltp_threshold": 4.0,
        "pcr_shift_threshold": 0.10,
        "buildup_oi_min_pct": 10.0,
        "buildup_ltp_min_pct": 3.0,
        "otm_oi_spike_pct": 15.0,
        "max_pain_shift_pct": 2.0,
    },
    "CRUDEOIL": {
        "oi_threshold": 15.0,        # high volatility, moderate OI
        "ltp_threshold": 5.0,
        "pcr_shift_threshold": 0.15,
        "buildup_oi_min_pct": 12.0,
        "buildup_ltp_min_pct": 4.0,
        "otm_oi_spike_pct": 20.0,
        "max_pain_shift_pct": 1.0,
    },
    "GOLD": {
        "oi_threshold": 20.0,        # moderate volatility, decent OI
        "ltp_threshold": 5.0,
        "pcr_shift_threshold": 0.20,
        "buildup_oi_min_pct": 15.0,
        "buildup_ltp_min_pct": 5.0,
        "otm_oi_spike_pct": 25.0,
        "max_pain_shift_pct": 1.0,
    },
    "SILVER": {
        "oi_threshold": 18.0,        # moderate volatility, moderate OI
        "ltp_threshold": 4.5,
        "pcr_shift_threshold": 0.18,
        "buildup_oi_min_pct": 14.0,
        "buildup_ltp_min_pct": 4.5,
        "otm_oi_spike_pct": 22.0,
        "max_pain_shift_pct": 1.0,
    },
}


def get_symbol_thresholds(symbol: str) -> dict:
    """Return threshold overrides for the given symbol, or empty dict for defaults."""
    base = symbol.upper().split()[0]
    return SYMBOL_THRESHOLD_OVERRIDES.get(base, {})


# ── Anomaly Detection Thresholds ────────────────────────────────────────────────────────────────────────────────────
OI_SPIKE_THRESHOLD_PCT = 15.0  # % change in OI to trigger spike alert
PRICE_SPIKE_THRESHOLD_PCT = 2.0  # % change in LTP to trigger price spike
PCR_EXTREME_LOW = 0.5  # PCR below this is extreme bearish
PCR_EXTREME_HIGH = 1.8  # PCR above this is extreme bullish
PCR_SHIFT_THRESHOLD = 0.3  # min PCR change to trigger alert
PCR_EXTREME_SEVERITY_BAND = 0.1  # band around extremes for severity bump
IV_SPIKE_ATM_THRESHOLD = 20.0  # % IV change at ATM to trigger alert
MAX_PAIN_SHIFT_PCT = 1.0  # % of underlying price — max pain shift to trigger alert
SEVERITY_HIGH_MULT = 1.5  # multiplier for HIGH severity thresholds
SEVERITY_MED_MULT = 1.0  # multiplier for MEDIUM severity thresholds
BUILDUP_OI_MIN_PCT = 10.0  # min OI change % for buildup detection
BUILDUP_LTP_MIN_PCT = 3.0  # min LTP change % for buildup detection
OTM_STRIKE_RANGE = 3  # strikes to check for OTM unusual moves
OTM_OI_SPIKE_PCT = 20.0  # OI spike % threshold for OTM unusual
VOLUME_AGGRESSION_HIGH = 2.5  # volume multiplier for high aggression
VOLUME_AGGRESSION_LOW = 1.5  # volume multiplier for low aggression
IV_CRUSH_THRESHOLD = 15.0  # % IV drop to trigger crush alert
STRADDLE_DELTA_PCT = 5.0  # delta move % for straddle premium alert
ATM_LEG_MOVE_PCT = 2.0  # ATM premium move % for straddle
PCR_VELOCITY_WINDOW = 3  # number of snapshots to track PCR velocity
MIN_OI_THRESHOLD = 1000  # min OI for a strike to be considered
ALERT_COOLDOWN_MINUTES = 60  # don't re-alert same type within N minutes
ALERT_COOLDOWN_HIGH_MINUTES = 30  # shorter cooldown for HIGH severity
INDIVIDUAL_ALERT_MIN_SEVERITY = "LOW"  # min severity to send individual alerts
DEDUP_CLUSTER_STRIKES = 2  # cluster strikes within N steps of fired key
MAX_ANOMALIES_PER_SYMBOL = 25  # cap raw anomalies per symbol to prevent noise floods
ANOMALY_MIN_SEVERITY = "MEDIUM"  # drop LOW severity anomalies before digest

# Research mode: True = EXPERIMENTAL trades allowed; False = CORE only
PAPER_RESEARCH_MODE = os.environ.get("PAPER_RESEARCH_MODE", "true").lower() == "true"

# Trade decision thresholds — CORE (high-quality setups)
MIN_CONFIDENCE_CORE = 70
MIN_ENTRY_QUALITY_CORE = 60
MIN_TREND_ALIGNMENT_CORE = 70
MIN_REGIME_SCORE_CORE = 60
# High-confidence bypass: when OI confidence >= this, trend alignment check is relaxed
# to MIN_TREND_ALIGNMENT_CORE * 0.6 (allows entries in choppy markets with strong OI conviction)
HIGH_CONFIDENCE_BYPASS_THRESHOLD = 90
HEAVYWEIGHT_THRESHOLDS = {
    "NIFTY": 0.30,
    "BANKNIFTY": 0.60,
}


# Trade decision thresholds — EXPERIMENTAL (research / marginal setups)
MIN_CONFIDENCE_EXPERIMENTAL = 50
MIN_ENTRY_QUALITY_EXPERIMENTAL = 40

# Reversal trade: higher confidence bar
REVERSAL_MIN_CONFIDENCE = 75

# Risk engine — applies to paper trading too (overtrading distorts results)
MAX_OPEN_TRADES_PER_SYMBOL = 2
MAX_OPEN_TRADES_TOTAL = 5
MAX_TRADES_PER_SYMBOL_PER_DAY = 4
MAX_DAILY_LOSS_RUPEES = 200000
LOSS_COOLDOWN_MINUTES = 30

# Natural Gas Risk Settings
NG_MAX_POSITIONS = 1
NG_RISK_PCT_PER_TRADE = 2.0  # 2% capital risk per trade


# ── Trend-Based Trading Logic ──────────────────────────────────────────────────────────────────────────────────────
# Mode: "conservative" | "balanced" | "aggressive" | "hybrid"
TREND_FILTER_MODE = "hybrid"

# Minimum non-fallback scan summaries required before any trend-based trade
# fires for a symbol. Prevents new symbols from getting TRIGGERED_CORE with
# zero trend validation. (#6)
TREND_MIN_SCANS = 3

# Trend persistence: fraction of last N scans that must agree (0.0-1.0)
TREND_CONSISTENCY_THRESHOLD = 0.6

# Momentum scoring: 0-100 score threshold to trigger trade
# Used as the momentum fallback gate in hybrid mode (#7)
MOMENTUM_SCORE_THRESHOLD = 75

# ── Regime Detection ──────────────────────────────────────────────────────────────────────────────────────────────
# Thresholds for the explicit RANGE classification branch (#10).
# A session where abs(price_change_pct) < MAX_CHANGE and
# price_range_pct < MAX_RANGE is classified as RANGE rather than NO_TRADE.
REGIME_RANGE_MAX_CHANGE_PCT = 0.5  # % half-session price drift
REGIME_RANGE_MAX_RANGE_PCT = 1.5  # % high-low range over session

# ── Trade Plan ──────────────────────────────────────────────────────────────────────────────────────────────────
# Maximum strike-steps between current underlying and support/resistance
# before the level is considered "too far" and ATM is used instead (#13)
MAX_LEVEL_DISTANCE_STEPS = 3

# Timeframe Strategy Settings
TIMEFRAME_OI_MIN_DIFF_PCT = 0.005  # 0.5% of base side's previous OI

TF_CANDLE_BODY_MIN_RATIO = 0.45
TF_CANDLE_CLOSE_POSITION_LONG = 0.65  # close must be in top 35%
TF_CANDLE_CLOSE_POSITION_SHORT = 0.35  # close must be in bottom 35%
TF_BREAKOUT_RANGE_PCT = 0.25  # 25% of prev candle range
TF_BREAKOUT_CMP_CAP_PCT = 0.002  # 0.2% of CMP
TF_EXHAUSTION_HARD_BLOCK = 4  # block at 4+ with weak OI
TF_REENTRY_COOLDOWN_BARS = 1  # wait 1 3H bar after SL
TF_CONTINUATION_OI_MULTIPLIER = 2.0  # 2x OI threshold for non-reversal entries


# ── Transaction Cost Model ──────────────────────────────────────────────────────────────────────────────────────────
# Per-trade round-trip costs in rupees (both legs combined).
# STT rates are approximate based on NSE/MCX exchange circulars.
# Options STT: 0.0625% of sell-side premium turnover (NSE)
# Futures STT: 0.01% of turnover (NSE/MCX)
# Brokerage: flat ₹20 per trade (Zerodha/Dhan/Shoonya typical)
TRANSACTION_COSTS = {
    "OPTIONS": {
        "flat_brokerage": 20.0,
        "stt_pct_turnover": 0.000625,  # 0.0625% of sell-side premium turnover
    },
    "FUTURES": {
        "flat_brokerage": 20.0,
        "stt_pct_turnover": 0.0001,  # 0.01% of futures turnover
    },
}


# ── AI Brain Settings ─────────────────────────────────────────────────────────────────────────────────────────────────
# Controls how the AI verdict influences trade decisions.
#   advisory   — AI verdict logged and displayed, but does NOT change trade outcomes
#   empirical  — Empirical boost based on pattern history (ADR-007 v2)
#   full       — AI can both promote (empirical) and veto trade decisions (post-Tier-2 only)
AI_DECISION_MODE = os.environ.get("AI_DECISION_MODE", "empirical")

# Minimum AI confidence to influence trade decisions (boost/veto) - kept for backward compatibility
AI_MIN_CONFIDENCE_BOOST = int(os.environ.get("AI_MIN_CONFIDENCE_BOOST", "80"))
AI_MIN_CONFIDENCE_VETO = int(os.environ.get("AI_MIN_CONFIDENCE_VETO", "85"))

# ── ADR-007: AI role redesign ──
EMP_BOOST_MIN_TRADES = int(os.environ.get("EMP_BOOST_MIN_TRADES", "20"))
EMP_BOOST_MIN_WINRATE = float(os.environ.get("EMP_BOOST_MIN_WINRATE", "0.60"))
ML_PREDICTOR_MODE = os.environ.get("ML_PREDICTOR_MODE", "shadow")          # off | shadow | live (live gated by §7)
LLM_ENRICHMENT_ASYNC = os.environ.get("LLM_ENRICHMENT_ASYNC", "true").lower() == "true"
LLM_ENRICH_TIMEOUT_S = int(os.environ.get("LLM_ENRICH_TIMEOUT_S", "120"))
AUTOPSY_ENABLED = os.environ.get("AUTOPSY_ENABLED", "true").lower() == "true"
AUTOPSY_TIME_IST = os.environ.get("AUTOPSY_TIME_IST", "23:45")

# Whether to call AI exit advisor for open trades during each scan
AI_EXIT_ADVISOR_ENABLED = (
    os.environ.get("AI_EXIT_ADVISOR_ENABLED", "false").lower() == "true"
)

# Disable LLM enrichment entirely when quota is exhausted or to reduce API calls
# Set to True to skip all Gemini/Groq/OpenRouter calls
DISABLE_LLM_ENRICHMENT = (
    os.environ.get("DISABLE_LLM_ENRICHMENT", "false").lower() == "true"
)

# Cap completion tokens — prevents OpenRouter 402 "can only afford N" on default 65k max_tokens
LLM_MAX_TOKENS_LIVE = int(os.environ.get("LLM_MAX_TOKENS_LIVE", "2048"))
LLM_MAX_TOKENS_FORMATTING = int(os.environ.get("LLM_MAX_TOKENS_FORMATTING", "1024"))
LLM_MAX_TOKENS_EOD = int(os.environ.get("LLM_MAX_TOKENS_EOD", "4096"))

# ── MCX Commodity Confidence Floor ───────────────────────────────────────────────────────────────────────────────────
# MCX OI data is thinner than NSE index — a 10-contract CE spike can look
# significant on a percentage basis but carries little actual market conviction.
# Set a higher minimum confidence for MCX trades to filter out low-signal setups.
# 72 chosen: above the NSE core floor (70) but below reversal threshold (75),
# ensuring MCX entries require meaningful OI confluence without being too restrictive.
MCX_MIN_CONFIDENCE = int(os.environ.get("MCX_MIN_CONFIDENCE", "72"))
MCX_SYMBOLS = frozenset({"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"})

# ── Decision Pipeline Settings ────────────────────────────────────────────────────────────────────────────────────────
PIPELINE_SHORT_CIRCUIT = os.environ.get("PIPELINE_SHORT_CIRCUIT", "false").lower() == "true"
ENTRY_QUALITY_MIN_SCORE_TF = int(os.environ.get("ENTRY_QUALITY_MIN_SCORE_TF", "40"))
TREND_ALIGNMENT_MIN_SCORE_TF = int(os.environ.get("TREND_ALIGNMENT_MIN_SCORE_TF", "35"))
DECISION_AUDIT_ENABLED = os.environ.get("DECISION_AUDIT_ENABLED", "true").lower() == "true"
DECISION_AUDIT_RETENTION_DAYS = int(os.environ.get("DECISION_AUDIT_RETENTION_DAYS", "90"))

# ── NATURALGAS strategy ──
NG_STRATEGY_ENABLED = _optional_env("NG_STRATEGY_ENABLED", "false").lower() == "true"
NG_FUT_ONLY = True                                             # hard, not configurable
PARITY_MAX_STALENESS_SEC = 300
PARITY_DEV_ENTRY_PCT = 0.45        # placeholder; overwritten by §5 calibration
PARITY_DEV_STOP_MULT = 2.0         # stop = entry deviation × this
PARITY_FORCE_FLAT_IST = "17:30"
MOMENTUM_ENTRY_START_IST = "18:00"
MOMENTUM_NO_ENTRY_AFTER_IST = "23:00"
NG_WEEKEND_FLAT = True             # no NG position past Fri 23:00
EIA_MIN_SURPRISE_BCF = 15
EIA_NO_TRADE_BAND_BCF = 8
EIA_TIME_STOP_IST = "21:30"
NG_MAX_POSITIONS = 1               # one NG position at a time, all regimes
NG_RISK_PCT_PER_TRADE = 0.5        # % of capital
