# NSEBOT Trading Strategy Overview

> **Purpose:** This document outlines the trading strategy, risk management framework, and execution logic for NSEBOT. Read this after `ARCHITECTURE.md` to understand how trades are generated and executed.

---

## Strategy Philosophy

NSEBOT employs a systematic, rule-based approach to NSE equity trading with:
- **Quantitative signal generation** using technical and fundamental indicators
- **AI-enhanced analysis** for pattern recognition and sentiment validation
- **Strict risk controls** to protect capital
- **Hybrid execution** supporting both paper trading and live trading modes

---

## Core Components

### 1. Signal Generation
**Location:** `src/services/` and `src/fetchers/`

**Data Inputs:**
- Price action (OHLCV from Yahoo Finance and broker APIs)
- Technical indicators (moving averages, RSI, MACD, Bollinger Bands)
- Volume analysis
- Market sentiment (AI-enhanced)
- Fundamental metrics (P/E, sector performance)

**Signal Types:**
- **Entry Signals:** Buy/short opportunities based on confluence of indicators
- **Exit Signals:** Profit targets, stop losses, or trend reversals
- **Validation Signals:** AI confirmation of trade thesis

### 2. Entry Logic

**Entry Criteria (Must meet minimum threshold):**
- Technical indicator alignment (trend + momentum + volume)
- Risk-reward ratio ≥ 2:1
- Position sizing within risk limits
- AI validation (if enabled)
- Market regime filter (avoid trading in unfavorable conditions)

**Entry Process:**
```
1. Fetcher retrieves market data
2. Services layer calculates indicators
3. Signal generation identifies opportunity
4. Risk validation checks constraints
5. Engine executes entry order
6. Dashboard logs trade
```

**Entry Quality Scoring:**
- Based on `test_entry_quality.py` and related tests
- Evaluates signal strength, timing, and risk metrics
- Only high-quality entries are executed

### 3. Exit Logic

**Exit Triggers:**
- **Profit Target:** Predefined percentage gain (configurable)
- **Stop Loss:** Maximum acceptable loss per trade
- **Trailing Stop:** Dynamic stop based on price movement
- **Time-based Exit:** Maximum holding period exceeded
- **Signal Reversal:** Opposite signal generated
- **Risk Breach:** Position violates risk limits

**Exit Process:**
```
1. Monitor active positions continuously
2. Check exit conditions on each update
3. Execute exit order when triggered
4. Log trade result to database
5. Update performance metrics
```

---

## Risk Management Framework

### 1. Position Sizing

**Risk-Based Sizing:**
- Maximum risk per trade: 1-2% of total capital
- Position size = (Risk Amount) / (Entry - Stop Loss)
- Example: ₹10L capital, 1% risk = ₹10K max loss
  - Entry: ₹100, Stop: ₹95 → Risk per share = ₹5
  - Position size = ₹10K / ₹5 = 2,000 shares

**Portfolio Constraints:**
- Maximum concurrent positions (configurable)
- Maximum sector exposure (avoid concentration)
- Maximum total deployed capital (keep cash reserve)

### 2. Risk Limits

**Per-Trade Limits:**
- Maximum loss per trade: 1-2% of capital
- Maximum position size: 10-20% of portfolio
- Minimum risk-reward ratio: 2:1

**Portfolio Limits:**
- Maximum total exposure: 80-90% of capital
- Maximum correlated positions: 3-5 in same sector
- Maximum daily drawdown: 5% (stop trading for day)
- Maximum weekly drawdown: 10% (review strategy)

**Reference:** `test_risk_metrics.py` validates these limits

### 3. Risk Metrics

**Key Metrics Monitored:**
- **Sharpe Ratio:** Risk-adjusted returns
- **Maximum Drawdown:** Largest peak-to-trough decline
- **Win Rate:** Percentage of profitable trades
- **Profit Factor:** Gross profit / Gross loss
- **Average Win/Loss:** Mean trade outcomes
- **Sortino Ratio:** Downside risk-adjusted returns

**Alerts:**
- Drawdown exceeds threshold → Pause trading
- Win rate drops below 40% → Review strategy
- Consecutive losses > 5 → Investigate market regime

---

## Execution Modes

### 1. Paper Trading (Simulation)

**Purpose:** Test strategy without real capital
**Reference:** `test_paper_trading.py`, `PAPER_TRADING_TELEGRAM_REDESIGN.md`

**Behavior:**
- Simulates order execution at market prices
- Tracks virtual positions and P&L
- Applies all risk rules and constraints
- Generates realistic performance metrics

**Use Cases:**
- Strategy development and testing
- Backtesting new parameters
- Validating signal quality
- Training and learning

**Activation:**
```bash
python src/main.py --mode paper
```

### 2. Live Trading

**Purpose:** Execute real trades with actual capital
**Reference:** `test_live_trading_p0.py`

**Behavior:**
- Connects to broker APIs (Zerodha/Dhan)
- Places real orders on NSE
- Manages actual positions and capital
- Full risk management enforcement

**Safety Features:**
- Requires explicit activation
- Additional confirmation prompts
- Enhanced logging and monitoring
- Emergency stop capabilities

**Activation:**
```bash
python src/main.py --mode live
```

**Prerequisites:**
- Broker API credentials configured
- Sufficient margin/capital available
- Risk limits reviewed and approved
- All tests passing

---

## AI/LLM Enhancement

**Integration:** AI_INTELLIGENCE_ROADMAP_v3.0.md
**Schema:** test_llm_schema_v2.py

### AI Capabilities

**1. Sentiment Analysis:**
- Analyzes news and social media sentiment
- Validates entry signals against market mood
- Flags potential reversals or regime changes

**2. Pattern Recognition:**
- Identifies complex chart patterns
- Detects support/resistance levels
- Recognizes market structure (trending/ranging)

**3. Trade Validation:**
- Reviews trade thesis before execution
- Identifies potential risks not captured by rules
- Suggests position sizing adjustments

**4. Performance Explanation:**
- Explains winning/losing trades
- Identifies strategy weaknesses
- Suggests parameter optimizations

### AI Integration Flow
```
Signal Generated
    ↓
AI Validation (Optional)
    ↓
    ├─→ Approved → Proceed to Execution
    ├─→ Modified → Adjust parameters
    └─→ Rejected → Skip trade
```

---

## Performance Monitoring

### Real-Time Metrics (Dashboard)
- Current P&L (day/week/month)
- Active positions and their status
- Capital deployed vs available
- Recent trade history
- Risk limit utilization

### Historical Analysis
- Cumulative returns chart
- Drawdown periods
- Monthly/quarterly performance
- Sector-wise performance
- Strategy parameter effectiveness

### Reporting
- Daily trade summary (Telegram integration)
- Weekly performance report
- Monthly strategy review
- Annual backtesting report

**Reference:** `TELEGRAM_REDESIGN_SUMMARY.md`, `TELEGRAM_REDESIGN_IMPLEMENTATION.md`

---

## Market Data Sources

### Primary Sources
1. **Yahoo Finance** (via fetchers)
   - Historical OHLCV data
   - Real-time quotes (with delay)
   - Cached in `data/yf-cache*` directories

2. **Broker APIs** (Zerodha/Dhan)
   - Real-time streaming quotes
   - Market depth (Level 2 data)
   - Official exchange data

3. **Chrome Extension Bridge** (Fallback)
   - TradingView chart data (`tv_content.js`)
   - Dhan web interface data (`dhan_dom_reader.js`)
   - Used when APIs are unavailable

### Data Processing
- Fetchers retrieve raw data
- Services layer normalizes and validates
- Engine consumes processed data
- Models store historical data

---

## Configuration Parameters

### Strategy Parameters (Configurable)
- Entry threshold scores
- Exit target percentages
- Stop loss percentages
- Position sizing rules
- Maximum concurrent positions
- Sector exposure limits

### Risk Parameters (Critical)
- Maximum risk per trade (%)
- Maximum portfolio exposure (%)
- Daily/weekly drawdown limits
- Minimum risk-reward ratio

### Execution Parameters
- Order type (market/limit)
- Slippage tolerance
- Order timeout
- Retry attempts

**Location:** Configuration files in `src/` (check for `config.py`, `settings.py`)

---

## Testing Strategy

### Test Coverage
**Location:** `tests/` directory (25+ files)

**Critical Tests:**
- `test_live_trading_p0.py` - Priority 0 live trading validation
- `test_risk_metrics.py` - Risk management compliance
- `test_entry_quality.py` - Signal quality assessment
- `test_paper_trading.py` - Simulation accuracy
- `test_llm_schema_v2.py` - AI integration correctness

### Test Workflow
1. Run full test suite before any deployment
2. Validate risk metrics calculations
3. Verify paper trading accuracy
4. Test live trading safety features
5. Confirm AI integration (if enabled)

### Continuous Testing
- Automated tests on code changes
- Daily backtesting with live data
- Weekly strategy parameter validation
- Monthly performance review

---

## Operational Workflow

### Daily Operations
1. **Pre-Market (9:00 AM IST):**
   - Fetch overnight market data
   - Update watchlists
   - Check risk limits
   - Prepare for market open

2. **Market Hours (9:15 AM - 3:30 PM IST):**
   - Continuous signal monitoring
   - Real-time position management
   - Automated order execution
   - Risk limit enforcement

3. **Post-Market (3:30 PM IST):**
   - Reconcile positions
   - Update performance metrics
   - Generate daily report
   - Backup database

### Weekly Operations
- Performance review and analysis
- Strategy parameter assessment
- Risk limit review
- Database maintenance (`tools/db_maintenance.py`)

### Monthly Operations
- Comprehensive backtesting
- Strategy optimization
- Capital allocation review
- Broker API key rotation

---

## Emergency Procedures

### Circuit Breakers
- **Daily Drawdown > 5%:** Pause trading for rest of day
- **Weekly Drawdown > 10%:** Pause trading, review strategy
- **System Error:** Halt all new orders, maintain existing positions
- **Broker API Failure:** Switch to paper mode, alert operator

### Manual Override
- Emergency stop command to halt all trading
- Manual position closure capability
- Override risk limits (with logging)
- Force reconciliation

### Recovery Procedures
1. Database backup restoration
2. Position reconciliation with broker
3. Signal regeneration from last checkpoint
4. Gradual resumption of trading

---

## Future Enhancements

### Planned Features
- Options strategy support (covered calls, protective puts)
- Multi-asset trading (futures, commodities)
- Advanced ML models for signal generation
- Real-time collaboration (multi-user dashboard)
- Mobile app for monitoring

### Optimization Opportunities
- Reduce reliance on DOM scraping
- Migrate fully to broker APIs
- Implement streaming data architecture
- Add more sophisticated risk models
- Enhance AI integration depth

---

## Quick Reference

### Start Trading
```bash
# Paper mode (safe)
python src/main.py --mode paper

# Live mode (requires setup)
python src/main.py --mode live
```

### Run Tests
```bash
pytest tests/
```

### Check Performance
```bash
python tools/performance_report.py
```

### Emergency Stop
```bash
python src/main.py --stop
```

---

## Related Documentation

- `ARCHITECTURE.md` - System architecture and module overview
- `AI_INTELLIGENCE_ROADMAP_v3.0.md` - AI integration details
- `ZERODHA-BROKER-INTEGRATION-PLAN.md` - Zerodha API setup
- `PHASE_2_IMPLEMENTATION_SUMMARY.md` - Phase 2 features
- `PHASE4_IMPLEMENTATION_COMPLETE.md` - Phase 4 features

---

**Last Updated:** June 22, 2026
**Strategy Version:** 3.0
**Maintainer:** NSEBOT Development Team
