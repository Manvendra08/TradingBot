# NSEBOT

Autonomous options trading system, real-time option-chain monitor, signal engine, Telegram digest bot, multi-leg paper trading simulator, and Zerodha Kite live trading executor.

---

## Capabilities & Architecture

- **Real-Time Option Chain Monitoring:** Ingests live option chain data for watched symbols (NSE indices: `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`; MCX commodities: `NATURALGAS`, `CRUDEOIL`, `GOLD`, `SILVER`). Dynamically filters strikes to **ATM +/- 10**.
- **Deterministic Decision Matrix (v2.0 — Enforce Mode):** Fully decoupled from LLM stochastic variance via [decision_matrix.json](file:///c:/Users/manve/VibeProjects/NSEBOT/config/decision_matrix.json) and [decision_matrix.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/decision_matrix.py).
  - Direction × IV Percentile Volatility Bucket (`Low`, `Mid`, `High`) deterministically maps to permitted structures (`IRON_CONDOR`, `SHORT_STRANGLE`, `BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`, `JADE_LIZARD`).
  - LLM authority is strictly limited to execution enrichment and tightening/veto only; proposals outside the regime cell are auto-rejected by [multileg_validator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_validator.py).
- **Aggressive Delta Profile & Strike Pre-Filtering:**
  - Configurable delta bands (Aggressive default vs. Defensive) with hard danger ceilings (`max_delta`).
  - Pre-filters candidate strikes in [multileg_strategy.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_strategy.py) (`get_candidate_strikes`), discarding any strike breaching short delta danger ceilings before offering choices to the LLM.
  - Hedge drag ceiling calibrated to **60%** for indices (55% for Natural Gas), allowing viable credit spreads when net credit $\ge 25\%$ of wing width.
- **Directional Engine Alignment & Confidence Blending:**
  - Directional setups (Bull Put / Bear Call) dynamically align engine conviction (`is_bullish_engine` / `is_bearish_engine`).
  - When engine conviction is $\ge 60\%$ and LLM execution quality is high ($\ge \text{conf\_floor}$), confidence is blended (`0.35 * engine + 0.65 * llm`) in [multileg_paper_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_paper_trading.py) and [multileg_live_trading.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/multileg_live_trading.py) to prevent false rejections of 60–69% signals.
- **Exit Priority Evaluator & Leg Ordering:**
  - Priority engine in [exit_evaluator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/exit_evaluator.py) (`HARD_STOP` $\to$ `TESTED_DELTA` $\to$ `PROFIT_TARGET` $\to$ `TIME_DECAY` $\to$ `CAS_CLOSE` $\to$ `FRIDAY_CLOSE` $\to$ `AI_TIGHTEN`).
  - AI exits only permitted on true tightening conditions ($\ge 30\%$ credit profit or $\le -50\%$ stop loss ratio).
  - Safe exit leg ordering closes short legs first to immediately release SPAN margin and eliminate naked risk before closing long wings.
- **Top Heavyweights Net Impact Digest & Contributors Page:**
  - Consolidated 3-index Telegram alert (`NIFTY`, `BANKNIFTY`, `SENSEX`) dispatched once per scan cycle via [heavyweight_digest.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/alerts/heavyweight_digest.py) during market hours (09:15–15:40 IST).
  - Web dashboard Contributors cockpit ([contributors.html](file:///c:/Users/manve/VibeProjects/NSEBOT/src/dashboard/contributors.html)) displaying real-time heavyweight point contributions, 5-scan velocity shifts, and options strategy takeaways.
- **Fail-Closed Broker Authorization Gate:** All live Kite executions (entry, exit, adjustment, reconciliation) are strictly gated through [broker_gate.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/broker_gate.py) (`authorize_broker_execution`), verifying shadow mode, broker disabled switches, pause flags, enabled symbol lists, and market hours. Paper trading remains fully operational even when broker mode is disabled.
- **Fail-Closed Runtime Configuration:** Persisted in `data/runtime_config.json` via [runtime_config.py](file:///c:/Users/manve/VibeProjects/NSEBOT/config/runtime_config.py) with atomic file writes and fail-closed fallback defaults (`get_fail_closed_defaults`).
- **Immutable Scan Snapshots:** Context frozen at scan time in [scan_snapshot.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/models/scan_snapshot.py) with SHA-256 data hashing and `MappingProxyType` immutability. Provenance tracked via `snapshot_id` across `paper_trades`, `live_trades`, and `multi_leg_trades`.
- **Sequential Leg Execution & Atomic Rollback:** Multi-leg live orders verify fills sequentially via `confirm_order_fill()`. Upon partial rejection or broker failure, `_rollback_placed_legs()` instantly flattens all open positions.
- **Bloomberg / TradingView Dashboard:** High-performance FastAPI server ([dashboard_server.py](file:///c:/Users/manve/VibeProjects/NSEBOT/dashboard_server.py)) serving:
  - Main Cockpit & Recent Intelligence (TradingView 24h news, ScanX heatmaps, combined sentiment)
  - Paper Trading Console (6 KPIs, collapsible equity curve with smooth animations, trade duration, win rates, MTM P&L)
  - Live Broker Console (Zerodha Kite position reconciliation, GTT order tracking)
  - Contributors Cockpit (Top heavyweights net impact, 5-scan velocity tracking)
  - Settings & Strategy Cockpit (Hot-reloadable runtime config and decision matrix)
  - Ops Agent & Scan Sentinel Diagnostics (Health checks, self-healing, anomaly rules)

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
- **EOD Strategy Review & Autopsy:** OmniRouter → GitHub Models → Groq → Gemini SDK → OpenCode Zen → Bedrock Mantle → NVIDIA NIM → OpenRouter.
- **Scan Sentinel Diagnostic:** OmniRouter Sentinel → Groq → Gemini SDK → GitHub Models → OpenCode Zen.

---

## Quick Start (Local Development)

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

### 3. Run the Bot Locally

**Continuous Scheduler Loop:**
```bash
python main.py
```

**One-Shot Scan (Immediate execution):**
```bash
python main.py --now
python main.py --now --symbols NIFTY BANKNIFTY NATURALGAS
```

### 4. Run the Dashboard Locally

```bash
python dashboard_server.py
```

Open in your browser:
- **Main Dashboard & Cockpit:** [http://localhost:8080/](http://localhost:8080/)
- **Paper Trading:** [http://localhost:8080/paper](http://localhost:8080/paper)
- **Broker Live Trades:** [http://localhost:8080/broker](http://localhost:8080/broker)
- **Contributors & Heavyweights:** [http://localhost:8080/contributors](http://localhost:8080/contributors)
- **Settings & Strategy Cockpit:** [http://localhost:8080/settings](http://localhost:8080/settings)
- **Scan Sentinel Diagnostics:** [http://localhost:8080/sentinel](http://localhost:8080/sentinel)
- **Ops Agent Safety Monitor:** [http://localhost:8080/ops](http://localhost:8080/ops)

---

## Production Deployment (Oracle Cloud Infrastructure — OCI)

The bot runs 24/7 in production on an **Oracle Cloud Infrastructure (OCI)** Ubuntu instance managed via `systemd`.

### Server Specifications & Access
- **Host IP:** `137.23.58.212` (Hostname: `nsebot`)
- **User:** `ubuntu`
- **SSH Key:** `~/.ssh/id_ed25519`
- **Application Directory:** `/home/ubuntu/nsebot`
- **Python Environment:** `/home/ubuntu/nsebot/.venv`

### Systemd Services

| Service Name | Description | Command |
|---|---|---|
| `nsebot.service` | Trading scheduler, option-chain fetcher & execution engine | `/home/ubuntu/nsebot/.venv/bin/python3 main.py` |
| `nsebot-dashboard.service` | FastAPI Web Dashboard server (Port 8080) | `/home/ubuntu/nsebot/.venv/bin/python3 dashboard_server.py` |

### Production Management Commands

**Connect via SSH:**
```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212
```

**Deploy Latest Changes (from local or GitHub):**
```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@137.23.58.212 "cd /home/ubuntu/nsebot && git pull origin dev && sudo systemctl restart nsebot.service nsebot-dashboard.service"
```

**Check Service Status:**
```bash
sudo systemctl status nsebot.service nsebot-dashboard.service --no-pager
```

**Restart Services:**
```bash
sudo systemctl restart nsebot.service nsebot-dashboard.service
```

**Tail Real-Time Engine Logs:**
```bash
sudo journalctl -u nsebot.service -f
```

**Tail Real-Time Dashboard Logs:**
```bash
sudo journalctl -u nsebot-dashboard.service -f
```

---

## Scan Frequencies

Active scan intervals can be configured via the dashboard Cockpit or `runtime_config.json`:
`5m`, `10m`, `15m` (default), `30m`, `1H`, `3H`, `1D`.

---

## Key Project Structure

```
NSEBOT/
├── config/
│   ├── decision_matrix.json     # Deterministic regime rules, delta bands, stop/target configs
│   ├── decision_matrix_loader.py# Thread-safe loader with validation & fail-closed defaults
│   ├── multileg_strategies.py   # Multi-leg strategy structures, caps & hedge cost ratios
│   ├── runtime_config.py        # Fail-closed runtime settings loader
│   ├── settings.py              # Environment variables & static configuration
│   └── holidays.py              # 2026 Indian market holidays (NSE & MCX)
├── docs/
│   └── autopsies/               # Automated EOD post-mortem reports
├── src/
│   ├── fetchers/
│   │   ├── router.py            # Multi-source option chain router
│   │   ├── chart_fetcher.py     # 1H and 3H candle aggregation
│   │   ├── news_fetcher.py      # ICICIDirect, NewsAPI & TradingView news
│   │   └── shoonya_fetcher.py   # Shoonya Noren API connector
│   ├── engine/
│   │   ├── pipeline.py          # Central scan execution pipeline
│   │   ├── decision_matrix.py   # Volatility bucketing, structure selection & delta bands
│   │   ├── exit_evaluator.py    # Priority-based exit engine & safe leg ordering
│   │   ├── broker_gate.py       # Centralized fail-closed broker authorization
│   │   ├── strategy_registry.py # Strategy × Symbol dynamic dispatcher
│   │   ├── execution_parser.py  # Strict LLM execution proposal parser
│   │   ├── multileg_validator.py# Pre-flight engine alignment, delta & margin validator
│   │   ├── multileg_live_trading.py  # Live multi-leg executor & atomic rollback
│   │   ├── multileg_paper_trading.py # Paper multi-leg tracker & MTM accounting
│   │   ├── multileg_strategy.py # Strike selection, candidate filtering & risk profile
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
│   ├── alerts/
│   │   ├── digest.py            # Clean trader-facing Telegram digest builder
│   │   ├── heavyweight_digest.py# Consolidated 3-index heavyweights impact alert
│   │   └── telegram_dispatcher.py # Reliable Telegram delivery with HTTP fallback
│   └── dashboard/
│       ├── contributors.html    # Top heavyweights net movement cockpit
│       ├── paper.html           # Paper trading console with collapsible equity curve
│       └── settings.html        # Settings, runtime config & decision matrix editor
├── dashboard_server.py          # FastAPI dashboard server
├── main.py                      # Bot daemon entry point
└── tests/                       # Unit and integration test suites
```

---

## Exit Precedents & Risk Guardrails

1. **AI Exit Auto-Exits:** Autonomous AI book exits (`CLOSED_AI_EXIT`) and leg rolls (`ADJUST`) are gated by [exit_evaluator.py](file:///c:/Users/manve/VibeProjects/NSEBOT/src/engine/exit_evaluator.py). AI cannot close prematurely; it only acts on tightening conditions ($\ge 30\%$ credit profit or $\le -50\%$ stop loss ratio).
2. **Mechanical Exits:** SL, profit targets, tested-side delta triggers ($\ge 0.48-0.50$ ATM breach for aggressive profile; $\ge 0.35$ for naked strangles), and trailing stops are evaluated continuously on every scan tick.
3. **Safe Leg Exit Ordering:** Closes short sold legs first to immediately release SPAN margin and eliminate naked market risk, before closing long protective wings.
4. **Friday Square-Off:** Mandatory risk square-off executed between 15:35–15:40 IST (NSE) and 23:25–23:30 IST (MCX).
5. **Daily Loss Cap:** Natural Gas halts new entries after 5 daily SL hits.
6. **Margin & Delta Caps:** Multi-leg books enforce combined margin cap (₹600K) and combined net delta cap (0.60).
