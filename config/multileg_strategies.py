"""
Configuration for Multi-Leg Short Options Strategies.

Strategy types, allowed symbols, book-level risk caps, and constraints.
LLM decides the strategy; this config defines the guardrails.
"""

# ── Strategy Types ──────────────────────────────────────────────────
STRATEGY_TYPES = {
    "IRON_CONDOR": "Iron Condor",
    "SHORT_STRANGLE": "Short Strangle",
    "SHORT_STRADDLE": "Short Straddle",
    "BEAR_CALL_SPREAD": "Bear Call Spread",
    "BULL_PUT_SPREAD": "Bull Put Spread",
    "JADE_LIZARD": "Jade Lizard",
    "TFSS_LEGACY": "Short Strangle (Trend)",
    "CUSTOM": "Custom",
}

# ── Allowed Symbols (NSE indices & MCX commodities) ─────────────────────────────
ALLOWED_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NATURALGAS", "CRUDEOIL"}

# ── Book-Level Risk Caps ────────────────────────────────────────────
MAX_BOOK_MARGIN = 7_500_000.0     # Maximum combined margin per book (₹75L / 7500K)
MAX_NET_DELTA = 0.60              # Maximum absolute net delta per book
MAX_NET_VEGA = 500.0              # Maximum net vega exposure
MAX_LEGS_PER_BOOK = 6             # Maximum legs in a single book
MIN_NET_PREMIUM = 2.0             # Minimum net premium collected (₹)

# ── Strategy-Specific Constraints ───────────────────────────────────
STRATEGY_CONSTRAINTS = {
    "IRON_CONDOR":     {"min_legs": 4, "max_legs": 4, "all_sell": False},
    "SHORT_STRANGLE":  {"min_legs": 2, "max_legs": 2, "all_sell": True},
    "SHORT_STRADDLE":  {"min_legs": 2, "max_legs": 2, "all_sell": True},
    "BEAR_CALL_SPREAD": {"min_legs": 2, "max_legs": 2, "all_sell": False},
    "BULL_PUT_SPREAD": {"min_legs": 2, "max_legs": 2, "all_sell": False},
    "JADE_LIZARD":     {"min_legs": 3, "max_legs": 3, "all_sell": False},
    "TFSS_LEGACY":     {"min_legs": 2, "max_legs": 6, "all_sell": True},
    "CUSTOM":          {"min_legs": 2, "max_legs": 6, "all_sell": False},
}

# ── Conflicting Strategies ──────────────────────────────────────────
# Don't open conflicting books on the same symbol simultaneously.
CONFLICTING_STRATEGIES = {
    "BULL_PUT_SPREAD":  {"conflicts_with": ["BEAR_CALL_SPREAD"]},
    "BEAR_CALL_SPREAD": {"conflicts_with": ["BULL_PUT_SPREAD"]},
}

# ── Exit Defaults ───────────────────────────────────────────────────
DEFAULT_PROFIT_TARGET_PCT = 0.50   # Close book at 50% of max profit
DEFAULT_STOP_LOSS_PCT = 2.0        # Close book at 200% of max loss (premium sold)
DEFAULT_TIME_DECAY_EXIT_DTE = 0    # 0 = hold through expiry day until 15:25 IST market close square-off

# ── IV Thresholds for Strategy Selection ────────────────────────────
IV_HIGH_THRESHOLD = 20.0     # IV above this = high premium environment
IV_LOW_THRESHOLD = 12.0      # IV below this = low premium, avoid selling
