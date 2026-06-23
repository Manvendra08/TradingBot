# NSEBOT Architecture — Adaptive Graph of Thoughts Analysis

> **Generated:** June 22, 2026 | **Method:** AGoT Reasoning Graph
> **Purpose:** Decompose system architecture into a reasoning graph with annotated trade-offs.
> Read this FIRST in any new session to understand the system at a structural level.

---

## 1. System Overview

NSEBOT is a **multi-asset algorithmic trading system** for NSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) and MCX commodities (NATURALGAS, CRUDEOIL, GOLD, SILVER). It operates on a **scan-analyze-decide-execute** pipeline running every 5 minutes during market hours.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        NSEBOT REASONING GRAPH                        │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  FETCH   │───▶│  DETECT  │───▶│  DECIDE  │───▶│  EXECUTE │      │
│  │  (router)│    │(anomaly) │    │ (trade_  │    │ (paper/  │      │
│  │          │    │          │    │ decision) │    │  live)   │      │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
│       │               │               │               │             │
│       ▼               ▼               ▼               ▼             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  CHART   │    │   LLM    │    │   RISK   │    │ MONITOR  │      │
│  │(chart_   │    │(llm_     │    │ (risk_   │    │(monitor_ │      │
│  │ fetcher) │    │enrich)   │    │ engine)  │    │ paper)   │      │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
│       │               │               │               │             │
│       ▼               ▼               ▼               ▼             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  NEWS    │    │   EXIT   │    │  TREND   │    │  ALERT   │      │
│  │(news_    │    │ ADVISOR  │    │ ANALYSIS │    │(telegram)│      │
│  │ fetcher) │    │ (AI)     │    │          │    │          │      │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Module Responsibilities (Reasoning Nodes)

### Node 1: Data Ingestion Layer
**Files:** `src/fetchers/router.py`, `src/fetchers/*.py`

| Fetcher | Source | Reliability | Data Quality | Use Case |
|---------|--------|-------------|-------------|----------|
| `NSEPublicFetcher` | nseindia.com | HIGH | Medium | Primary NSE indices |
| `DhanFetcher` | Dhan API | HIGH | High | Authenticated NSE |
| `DhanCommodityFetcher` | Dhan MCX API | HIGH | High | MCX commodities |
| `MoneycontrolFetcher` | moneycontrol.com | MEDIUM | Medium | Fallback |
| `ScrapeGraphFetcher` | ScrapeGraph AI | MEDIUM | Variable | AI-assisted |
| `DhanHeadlessFetcher` | Headless browser | LOW | High | Last resort |
| Chrome Extension | DOM scraping | LOW | High | Emergency only |

**Priority Chain:**
- NSE: `nse_public` → (fallbacks if configured)
- MCX: `dhan_commodity` → `moneycontrol` → `dhan` → `dhan_headless`

**Trade-off:** Multi-source fallback provides resilience but increases maintenance surface. Each fetcher is a potential failure point that must be tested independently.

### Node 2: Anomaly Detection
**Files:** `src/engine/anomaly_detector.py`

Detects 10+ anomaly types from option chain data:
- OI spikes (>15% change)
- Price spikes (>2% change)
- PCR extremes (0.5 or 1.8)
- PCR shifts (>0.3 change)
- IV spikes at ATM (>20%)
- Max pain shifts (>50 rupees)
- OTM unusual activity
- Volume aggression (2.5x normal)
- IV crush (>15% drop)
- Straddle premium changes

**Output:** Verdict label (e.g., "Long Buildup", "Short Buildup", "Put Writing") + confidence score.

### Node 3: Intelligence & LLM Enrichment
**Files:** `src/engine/intelligence.py`, `src/engine/llm_enrichment.py`

**Intelligence Layer:**
- Aggregates anomalies into structured intelligence
- Generates verdict label and confidence
- Calculates scan context (support, resistance, ATM strike, total OI)

**LLM Enrichment Layer (Gemini/Groq/OpenRouter):**
- Action-oriented verdict: `GO_LONG` / `GO_SHORT` / `NO_TRADE`
- Specific instrument recommendation (strike + option type)
- Entry trigger, premium range, SL, T1, T2
- Risk-reward ratio, risk rating, thesis, invalidation
- Catalyst identification

**AI Integration Modes:**
| Mode | Behavior | Risk |
|------|----------|------|
| `advisory` (default) | Log + display only | LOW |
| `boost_only` | Promotes BLOCKED → EXPERIMENTAL | MEDIUM |
| `full` | Can boost AND veto trades | HIGH |

### Node 4: Trade Decision Engine
**Files:** `src/engine/trade_decision.py`

**Multi-Layer Scoring:**
```
┌─────────────────────────────────────────────────────────┐
│                  TRADE DECISION GRAPH                     │
│                                                         │
│  Hard Blocks ──▶ Scan Count Gate ──▶ Entry Quality     │
│       │                                   │             │
│       ▼                                   ▼             │
│  Verdict Check ──▶ Trend Alignment ──▶ Regime Score    │
│       │                                   │             │
│       ▼                                   ▼             │
│  Momentum Score ──▶ AI Verdict ──▶ Final Decision      │
│                                                         │
│  Output: TRIGGERED_CORE / TRIGGERED_EXPERIMENTAL       │
│          / BLOCKED                                       │
└─────────────────────────────────────────────────────────┘
```

**Decision Modes:**
| Mode | Priority Chain | Use Case |
|------|---------------|----------|
| `conservative` | Persistence only | Stable markets |
| `balanced` | Momentum scoring | Normal markets |
| `aggressive` | Reversal detection | Volatile markets |
| `hybrid` (default) | Persistence → Reversal → Momentum → Experimental → AI Boost | All conditions |

**Key Thresholds (configurable in `config/settings.py`):**
| Parameter | CORE | EXPERIMENTAL |
|-----------|------|-------------|
| Min Confidence | 70% | 50% |
| Min Entry Quality | 60/100 | 40/100 |
| Min Trend Alignment | 70/100 | — |
| Min Regime Score | 60/100 | — |
| Reversal Min Confidence | 75% | — |
| Momentum Score Threshold | 75/100 | — |

### Node 5: Risk Engine
**Files:** `src/engine/risk_engine.py`

**6 Risk Checks (identical for paper and live):**
1. Max open trades per symbol (default: 2)
2. Max total open trades (default: 5)
3. Max trades per symbol per day (default: 4)
4. Daily loss cap — sums ONLY negative P&L (default: ₹200,000)
5. Loss cooldown — wait N minutes after a loss (default: 30 min)
6. Consecutive-loss circuit breaker — 3 losses in 30 min halts all trading

**IST-Aligned Day Boundaries:** Daily counters reset at IST midnight, not UTC.

### Node 6: Execution Layer
**Files:** `src/engine/paper_trading.py`, `src/engine/live_trading.py`

**Paper Trading:**
- Simulates orders at market prices
- Tracks virtual positions and P&L
- All risk rules enforced
- Two strategies: Core OI-based + Timeframe (3H/1H)

**Live Trading:**
- Connects to Zerodha Kite Connect (primary) or Dhan API
- Real order placement with lot sizing
- Position sync: Kite direct positions synced to SQLite
- TLS-resilient adapter with pool-eviction retry

---

## 3. Data Flow (Annotated)

```
SCHEDULER (every 5 min)
    │
    ▼
run_pipeline(symbols)
    │
    ├──▶ fetch_option_chain(symbol)          [Node 1: Data Ingestion]
    │    │   tries: nse_public → dhan → moneycontrol → ...
    │    │   filters: ATM ± STRIKES_AROUND_ATM strikes
    │    │   normalizes: {underlying, expiry, strikes[], source}
    │    │
    │    ▼
    ├──▶ get_chart_fetcher().fetch()          [Chart Data]
    │    │   server-side chart indicators (no Chrome dependency)
    │    │   injects: {1h: {ohlc, atr_14}, 3h: {ohlc, atr_14}}
    │    │
    │    ▼
    ├──▶ detect_anomalies()                   [Node 2: Anomaly Detection]
    │    │   input: option chain + chart indicators
    │    │   output: alerts[] + scan_context{}
    │    │
    │    ▼
    ├──▶ is_duplicate() filter                [Dedup]
    │    │   suppresses repeated alerts within cooldown
    │    │
    │    ▼
    ├──▶ generate_intelligence_structured()   [Node 3: Intelligence]
    │    │   input: alerts + scan_context
    │    │   output: intel{verdict_label, confidence, telegram_text}
    │    │
    │    ▼
    ├──▶ get_llm_verdict()                    [Node 3: LLM Enrichment]
    │    │   input: intel + scan_context + news + open_trade
    │    │   output: llm_verdict{action, instrument, entry, SL, T1, T2, ...}
    │    │   ⚠️ can be disabled via DISABLE_LLM_ENRICHMENT
    │    │
    │    ▼
    ├──▶ get_exit_advice()                    [AI Exit Advisor]
    │    │   evaluates open paper trades for early close or SL trail
    │    │   ⚠️ gated by live_ai_exit_advisor_enabled
    │    │
    │    ▼
    ├──▶ run_paper_trading()                  [Node 6: Paper Execution]
    │    │   ├── monitor_paper_trades() — SL/Target/premium checks
    │    │   ├── build_paper_trade_plan() — strike selection
    │    │   └── execute_paper_trade() — reversal check → insert
    │    │
    │    ▼
    ├──▶ run_timeframe_strategy()             [Timeframe Strategy]
    │    │   3H candle breakout + 1H candle exit
    │    │   OI bias confirmation required
    │    │   pyramid up to 3 levels
    │    │
    │    ▼
    ├──▶ run_live_trading()                   [Node 6: Live Execution]
    │    │   same logic as paper but with real broker orders
    │    │   gated by live_trading_enabled in runtime_config
    │    │
    │    ▼
    ├──▶ build_digest()                       [Formatting]
    │    │   combines all outputs into Telegram message
    │    │
    │    ▼
    ├──▶ send_text()                          [Alerting]
    │    │   Telegram/Discord dispatch
    │    │
    │    ▼
    └──▶ save_scan_summary()                  [Persistence]
         stores to SQLite for regime detection
         ⚠️ is_fallback=True when price is stale
```

---

## 4. Configuration Matrix

### Environment Variables (`.env`)
| Variable | Purpose | Default |
|----------|---------|---------|
| `ACTIVE_BROKER` | Primary broker | `zerodha` |
| `ZERODHA_API_KEY` | Zerodha credentials | — |
| `ZERODHA_ACCESS_TOKEN` | Zerodha session token | — |
| `DHAN_CLIENT_ID` | Dhan credentials | — |
| `TELEGRAM_BOT_TOKEN` | Alert delivery | — |
| `AI_DECISION_MODE` | AI influence level | `advisory` |
| `PAPER_RESEARCH_MODE` | Allow experimental trades | `true` |
| `DISABLE_LLM_ENRICHMENT` | Skip AI calls | `false` |

### Runtime Configuration (`data/runtime_config.json`)
| Key | Purpose | Default |
|-----|---------|---------|
| `live_trading_enabled` | Enable real orders | `false` |
| `live_ai_decision_mode` | Live AI mode | `advisory` |
| `live_ai_exit_advisor_enabled` | AI exit advice | `false` |
| `scan_frequency_nse` | NSE scan interval (min) | `5` |
| `scan_frequency_mcx` | MCX scan interval (min) | `5` |

### Key Settings (`config/settings.py`)
| Setting | Value | Impact |
|---------|-------|--------|
| `FETCHER_PRIORITY` | `["nse_public"]` | Data source order |
| `STRIKES_AROUND_ATM` | `10` | Option chain width (21 strikes) |
| `TREND_FILTER_MODE` | `"hybrid"` | Decision complexity |
| `MAX_OPEN_TRADES_TOTAL` | `5` | Portfolio exposure |
| `MAX_DAILY_LOSS_RUPEES` | `200000` | Daily loss limit |
| `LOSS_COOLDOWN_MINUTES` | `30` | Post-loss wait time |

---

## 5. Broker Integrations

### Zerodha (Kite Connect) — PRIMARY
- **Auth:** API key + access token (daily refresh)
- **Orders:** Market/Limit/SL orders via `kite.place_order()`
- **Positions:** Real-time via `kite.positions()`
- **Instruments:** Cached via `kite.instruments()` with background thread
- **Resilience:** TLS adapter with pool-eviction retry on connection errors
- **IP Whitelist:** Auto-detects public IP and logs whitelist instructions

### Dhan — SECONDARY
- **Auth:** Client ID + access token
- **Orders:** Via Dhan HQ API
- **Data:** Option chain for NSE + MCX
- **Monthly Rollover:** MCX security IDs expire monthly — must update `DHAN_SECURITY_IDS`

### Chrome Extension — EMERGENCY ONLY
- **Files:** `chrome_extension/dhan_dom_reader.js`, `tv_content.js`
- **Purpose:** DOM scraping when APIs fail
- **Risk:** UI changes break immediately
- **Recommendation:** Deprecate in favor of official APIs

---

## 6. Technical Debt Assessment

### High Priority
1. **Multiple Virtual Environments:** `.venv`, `.venv-1`, `.venv_new` — consolidate to single env
2. **Database Proliferation:** `bot.db` + `nsebot.db` — audit and merge
3. **Cache Bloat:** 6 `yf-cache*` directories — implement cleanup routine
4. **Scratch Directory:** 164+ debug files — archive or delete

### Medium Priority
5. **DOM Scraping Dependency:** Chrome extension is fragile — migrate to APIs
6. **MCX Contract Rollover:** Manual ID updates required monthly — automate
7. **Backup Folders:** `backup_safe_delete_*` in root — move to external storage

### Low Priority
8. **Test Coverage:** 25+ test files exist but coverage unknown — add coverage reporting
9. **Documentation Drift:** Multiple MD files in `MD Files/` — consolidate into `docs/`
10. **Environment Clutter:** `.planning/`, `_agent/` directories — review necessity

---

## 7. Dependency Graph

```
config/settings.py ─────────────┐
config/symbol_classes.py ───────┤
config/runtime_config.py ───────┤
config/holidays.py ─────────────┤
                                ▼
src/fetchers/router.py ────▶ src/engine/pipeline.py
src/fetchers/*_fetcher.py ──┘         │
                                      ├──▶ src/engine/anomaly_detector.py
                                      ├──▶ src/engine/intelligence.py
                                      ├──▶ src/engine/llm_enrichment.py
                                      ├──▶ src/engine/trade_decision.py ──▶ src/engine/entry_quality.py
                                      │                                   ──▶ src/engine/regime_detector.py
                                      │                                   ──▶ src/engine/trend_analysis.py
                                      ├──▶ src/engine/risk_engine.py
                                      ├──▶ src/engine/paper_trading.py ──▶ src/engine/paper_plan.py
                                      │                                 ──▶ src/engine/trade_plan.py
                                      │                                 ──▶ src/engine/capital_allocator.py
                                      ├──▶ src/engine/live_trading.py ──▶ src/engine/symbol_resolver.py
                                      │                                ──▶ kiteconnect (external)
                                      ├──▶ src/alerts/dedup.py
                                      ├──▶ src/alerts/digest.py
                                      ├──▶ src/alerts/telegram_dispatcher.py
                                      └──▶ src/models/schema.py ──▶ SQLite (data/nsebot.db)
```

---

## 8. Quick Reference for Future Sessions

### Entry Point
```python
# Main pipeline entry
from src.engine.pipeline import run_pipeline
run_pipeline(symbols=["NIFTY", "BANKNIFTY", "NATURALGAS"])
```

### Test Suite
```bash
pytest tests/                          # All tests
pytest tests/test_live_trading_p0.py   # Critical live trading tests
pytest tests/test_risk_metrics.py      # Risk engine validation
pytest tests/test_llm_schema_v2.py     # AI integration tests
```

### Runtime Control
```bash
# Enable/disable live trading via runtime config
# Edit data/runtime_config.json
```

### Key Files to Read First
1. `docs/AGoT-playbook.md` — Reasoning framework
2. `docs/order-flow.md` — Signal to execution path
3. `docs/strategies/options-engine.md` — Options-specific logic
4. `config/settings.py` — All configurable thresholds
5. `src/engine/pipeline.py` — Main orchestration

---

**Last Updated:** June 22, 2026
**Analysis Method:** Adaptive Graph of Thoughts (AGoT)
