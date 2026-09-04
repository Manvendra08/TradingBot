# NSEBOT

Autonomous options trading system, real-time option-chain monitor, signal engine, Telegram digest bot, multi-leg paper trading simulator, and Zerodha Kite live trading executor.

---

## Capabilities & Architecture

- **Real-Time Option Chain Monitoring:** Ingests live option chain data for watched symbols (NSE indices: `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`; MCX commodities: `NATURALGAS`, `CRUDEOIL`, `GOLD`, `SILVER`). Dynamically filters strikes to **ATM +/- 10**.
- **Anomaly & Regime Detection:** Identifies Open Interest (OI) buildup, PCR shifts, IV percentiles, volume spikes, support/resistance walls, and underlying price divergence.
- **Engine-Aligned LLM Enrichment (v3.0):** Multi-tiered LLM routing chain provides structural execution detail without flipping underlying OI direction. Direction flips are deterministically blocked or corrected to `NO_TRADE`.
- **Strategy Registry:** Plugs strategies dynamically per symbol and regime via [strategy_registry.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/strategy_registry.py):
  - **CORE:** Directional long premium momentum entries (BUY CE/PE).
  - **TIMEFRAME:** 3H breakout confirmation entries with 1H crossover exits.
  - **TFSS (Trend-Following Short Strangle):** Multi-tranche premium selling (up to 6 legs) grouped via `leg_group_id`.
  - **MULTILEG:** Advanced multi-leg premium selling (Iron Condor, Short Strangle, Short Straddle, Bear Call Spread, Bull Put Spread, Jade Lizard, Custom).
  - **NG Parity / Momentum / Event:** Tailored MCX commodity strategies.
- **Fail-Closed Broker Authorization Gate:** All live Kite executions (entry, exit, adjustment, reconciliation) are strictly gated through [broker_gate.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/broker_gate.py) (`authorize_broker_execution`), verifying shadow mode, broker disabled switches, pause flags, enabled symbol lists, and market hours. Paper trading remains fully operational even when broker mode is disabled.
- **Fail-Closed Runtime Configuration:** Persisted in `data/runtime_config.json` via [runtime_config.py](file:///c:/Users/manve/VibeProjects/NSEBOT/config/runtime_config.py) with atomic file writes and fail-closed fallback defaults (`get_fail_closed_defaults`).
- **Immutable Scan Snapshots:** Context frozen at scan time in [scan_snapshot.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/scan_snapshot.py) with SHA-256 data hashing and `MappingProxyType` immutability. Provenance tracked via `snapshot_id` across `paper_trades`, `live_trades`, and `multi_leg_trades`.
- **Sequential Leg Execution & Atomic Rollback:** Multi-leg live orders verify fills sequentially via `confirm_order_fill()`. Upon partial rejection or broker failure, `_rollback_placed_legs()` instantly flattens all open positions.
- **Bloomberg / TradingView Dashboard:** High-performance FastAPI server ([dashboard_server.py](file:///c:/Users/manve/VibeProjects/NSEBOT/dashboard_server.py)) serving:
  - Main Cockpit & Recent Intelligence (TradingView 24h news, ScanX heatmaps, combined sentiment)
  - Paper Trading Console (6 KPIs, equity curve, trade duration, win rates, MTM P&L)
  - Live Broker Console (Zerodha Kite position reconciliation, GTT order tracking)
  - Ops Agent & Scan Sentinel Diagnostics (health checks, self-healing, anomaly rules)

---

## Timeframe Separation Rules

Strict separation of roles between timeframes:
- **3H Candles:** Entry timing **ONLY** (breakout/breakdown confirmation with OI classification). Never used for exit or trend generation.
- **1H Candles:** Exit timing **ONLY** (strategy-level exit trigger). Never used for entries or trend generation.
- 3H and 1H candles are **never** cross-checked against each other.

---

## LLM Provider Stack

Purpose-based failover pipelines assembled in [llm_enrichment.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/llm_enrichment.py):

- **MCX Commodities Chain:** OmniRouter (Claude-Models Combo → Claude/Antigravity → claude/Free → GPT 5.5 CX → KR Models) → GitHub Models → Groq → Gemini SDK → OpenCode Zen → AnyAPI Free → Bedrock Mantle → NVIDIA NIM → Bedrock SDK → OpenRouter → SambaNova.
- **NSE/BSE Indices Chain:** OmniRouter → GitHub Models → Groq → Gemini SDK → OpenCode Zen → NVIDIA NIM → Bedrock SDK → OpenRouter.
- **EOD Strategy Review:** OmniRouter → GitHub Models → Groq → Gemini SDK → OpenCode Zen → Bedrock Mantle → NVIDIA NIM → OpenRouter.
- **Scan Sentinel Diagnostic:** OmniRouter Sentinel → Groq → Gemini SDK → GitHub Models → OpenCode Zen.

---

## Quick Start

### 1. Installation

```bash
# Clone and enter workspace
cd NSEBOT

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Credentials

Create a `.env` file in the root directory:

```env
# Zerodha Kite Connect
KITE_API_KEY=your_kite_api_key
KITE_ACCESS_TOKEN=your_kite_access_token

# Telegram Alerts
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# LLM Providers (Configure as needed)
OMNIROUTER_API_KEY=your_omnirouter_key
GROQ_API_KEY=your_groq_key
GITHUB_TOKEN=your_github_token
GEMINI_API_KEY=your_gemini_key
OPENCODE_API_KEY=your_opencode_key
```

### 3. Run the Bot

**Continuous Scheduler Loop:**
```bash
python main.py
```

**One-Shot Scan (Immediate execution):**
```bash
python main.py --now
python main.py --now --symbols NIFTY BANKNIFTY NATURALGAS
```

### 4. Run the Dashboard

```bash
python dashboard_server.py
```

Open in your browser:
- **Main Dashboard & Cockpit:** [http://localhost:8080/](http://localhost:8080/)
- **Paper Trading:** [http://localhost:8080/paper](http://localhost:8080/paper)
- **Broker Live Trades:** [http://localhost:8080/broker](http://localhost:8080/broker)
- **Settings & Strategy Cockpit:** [http://localhost:8080/settings](http://localhost:8080/settings)
- **Scan Sentinel Diagnostics:** [http://localhost:8080/sentinel](http://localhost:8080/sentinel)
- **Ops Agent Safety Monitor:** [http://localhost:8080/ops](http://localhost:8080/ops)

---

## Scan Frequencies

Active scan intervals can be configured via the dashboard Cockpit or `runtime_config.json`:
`5m`, `10m`, `15m` (default), `30m`, `1H`, `3H`, `1D`.

---

## Key Project Structure

```
NSEBOT/
├── config/
│   ├── settings.py              # Environment variables & static configuration
│   ├── runtime_config.py         # Fail-closed runtime settings loader
│   ├── multileg_strategies.py    # Multi-leg strategy structures & caps
│   └── holidays.py              # 2026 Indian market holidays (NSE & MCX)
├── src/
│   ├── fetchers/
│   │   ├── router.py            # Multi-source option chain router
│   │   ├── chart_fetcher.py     # 1H and 3H candle aggregation
│   │   ├── news_fetcher.py      # ICICIDirect, NewsAPI & TradingView news
│   │   └── shoonya_fetcher.py   # Shoonya Noren API connector
│   ├── engine/
│   │   ├── pipeline.py          # Central scan execution pipeline
│   │   ├── broker_gate.py       # Centralized fail-closed broker authorization
│   │   ├── strategy_registry.py # Strategy × Symbol dynamic dispatcher
│   │   ├── execution_parser.py  # Strict LLM execution proposal parser
│   │   ├── multileg_validator.py# Pre-flight engine alignment & margin validator
│   │   ├── multileg_live_trading.py  # Live multi-leg executor & atomic rollback
│   │   ├── multileg_paper_trading.py # Paper multi-leg tracker & MTM accounting
│   │   ├── live_trading.py      # Core & timeframe live Kite executor
│   │   ├── paper_trading.py     # Core & timeframe paper trading runner
│   │   ├── llm_enrichment.py    # Engine-aligned LLM enrichment pipeline
│   │   ├── trade_plan.py        # Single source of truth for SL/Target calculations
│   │   ├── intelligence.py      # OI analysis, regime, and verdict scoring
│   │   ├── capital_allocator.py # Position sizing & SPAN margin calculations
│   │   └── scan_sentinel.py     # Agentic self-healing diagnostics
│   ├── models/
│   │   ├── schema.py            # SQLite schema, WAL mode, migrations M101–M113
│   │   └── scan_snapshot.py     # Immutable scan context snapshots & SHA-256
│   └── alerts/
│       ├── digest.py            # Clean trader-facing Telegram digest builder
│       └── telegram_dispatcher.py # Reliable Telegram delivery with HTTP fallback
├── dashboard_server.py          # FastAPI dashboard server
├── main.py                      # Bot daemon entry point
└── tests/                       # Unit and integration test suites
```

---

## Exit Precedents & Risk Guardrails

1. **AI Exit Auto-Exits:** Autonomous AI book exits (`CLOSED_AI_EXIT`) and leg rolls (`ADJUST`) are enabled under `live_ai_exit_advisor_enabled`.
2. **Mechanical Exits:** SL, profit targets, and trailing stops are evaluated continuously on every scan tick.
3. **Friday Square-Off:** Mandatory risk square-off executed between 15:35–15:40 IST (NSE) and 23:25–23:30 IST (MCX).
4. **Daily Loss Cap:** Natural Gas halts new entries after 5 daily SL hits.
5. **Margin & Delta Caps:** Multi-leg books enforce combined margin cap (₹500K) and combined net delta cap (0.60).
