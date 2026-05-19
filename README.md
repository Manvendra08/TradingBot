# NSEBOT — NSE Option Chain Monitor

Automated 15-min option chain snapshot + anomaly alert system for **NIFTY, BANKNIFTY, FINNIFTY** and F&O stocks. Runs fully local on Windows/Mac/Linux.

---

## Architecture

```
main.py
  └── scheduler/job_runner.py        APScheduler, 15-min interval, market-hours guard
        └── engine/pipeline.py       Fetch → Persist → Detect → Alert
              ├── fetchers/router.py Dhan → NSE Public → Upstox fallback chain
              ├── models/schema.py   SQLite (WAL mode), 4 tables
              ├── engine/anomaly_detector.py  OI spike, PCR, IV, Max Pain
              ├── alerts/dedup.py    30-min cooldown deduplication
              └── alerts/telegram_dispatcher.py  python-telegram-bot v21

src/dashboard/app.py                 Streamlit localhost UI
src/extension_bridge.py             HTTP bridge for Chrome Extension
chrome_extension/                   Manifest V3 extension
  ├── content.js                     Option chain scraper (Dhan, NSE)
  ├── tv_content.js                  TradingView/Dhan chart scraper (1H/3H)
  └── popup.js                       Dashboard with fuzzy symbol matching
```

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
copy .env.example .env
# Edit .env with your Dhan/Upstox/Telegram credentials
```

### 3. Set environment variables (Windows)
```cmd
set DHAN_CLIENT_ID=xxx
set DHAN_ACCESS_TOKEN=xxx
set TELEGRAM_BOT_TOKEN=xxx
set TELEGRAM_CHAT_ID=xxx
```
`main.py` already auto-loads `.env` via `python-dotenv` when the file is present.

### 4. Run
```bash
# Start scheduler (blocks, runs 09:15–15:30 IST Mon–Fri)
python main.py

# One-shot manual run (for testing)
python main.py --now

# Single symbol test
python main.py --now --symbols NIFTY

# Streamlit dashboard (separate terminal)
streamlit run src/dashboard/app.py

# Chrome extension bridge (if using extension)
python main.py --bridge
# or on Windows
start_bridge.bat
```

---

## Anomaly Detection Rules

| Signal | Threshold | Config Key |
|---|---|---|
| OI Spike / Unwind | >25% in 15 min | `OI_SPIKE_THRESHOLD_PCT` |
| Underlying Price Spike | >1.5% in 15 min | `PRICE_SPIKE_THRESHOLD_PCT` |
| PCR Extreme | <0.5 or >1.5 | `PCR_EXTREME_LOW/HIGH` |
| PCR Shift | >0.20 absolute | `PCR_SHIFT_THRESHOLD` |
| ATM IV Spike | >5% jump | `IV_SPIKE_ATM_THRESHOLD` |
| Max Pain Shift | >50 pts | `MAX_PAIN_SHIFT_THRESHOLD` |
| Chart Trend Align | Bullish/Bearish | (1H/3H SuperTrend) |

All thresholds are in `config/settings.py`.

---

## Chrome Extension (v2.8)

The extension now bridges the gap between **Option Chain data** and **Technical Chart trends**.

### Features:
1. **Multi-Site Scraper**: Supports Dhan Advance Option Chain, NSE India, Sensibull, and Opstra.
2. **Chart Telemetry**: Automatically scrapes **SuperTrend** and **RSI** from `tv.dhan.co` or `tradingview.com` panes (1H and 3H timeframes).
3. **Fuzzy Symbol Matching**: Intelligently matches chart symbols (e.g., `MCX:NATURALGAS1!`) with option chain symbols (e.g., `NATURALGAS MAY FUT`) using a normalized lookup engine.
4. **Live Dashboard**: Real-time PCR, Max Pain, and OI Distribution (ATM ±5) in the extension popup.

### Setup:
1. Open `chrome://extensions/` → Enable **Developer mode**.
2. Click **Load unpacked** → select `chrome_extension/` folder.
3. Open both the **Dhan Option Chain** and the **Dhan/TV Chart** in separate tabs.
4. Extension syncs data via `chrome.storage.local`.
5. Start `python main.py --bridge` to pipe this telemetry into the backend for Telegram alerts.

---

## Dhan Security ID Setup

Run once to download and cache Dhan's security master:
```python
import csv, requests
r = requests.get("https://images.dhan.co/api-data/api-scrip-master.csv")
# Parse and find security_id for your F&O stocks
# Update DHAN_SECURITY_IDS in config/settings.py
```

---

## Data Sources Priority

1. **Dhan API v2** `/optionchain` — primary (free with account)
2. **NSE India public JSON** — fallback, no auth, needs cookie warm-up
3. **Upstox API v2** `/option/chain` — tertiary redundancy

Router auto-falls-back on failure with warning log.

---

## Database Schema

```sql
option_chain_snapshots   -- all strike-level data per 15-min tick
underlying_price         -- spot price + % change per tick  
anomaly_alerts           -- every fired alert with full context JSON
alert_dedup              -- deduplication tracker (cooldown)
chart_indicators         -- 1H/3H sentiment synced from extension
```

SQLite file: `data/nsebot.db`

---

## Telegram Alert Format

```
📈 NSEBOT ALERT — OI_SPIKE
🕐 25-Jun 09:30 UTC
📊 Symbol  : BANKNIFTY
📅 Expiry  : 2025-06-26
🎯 Strike  : 52000 CE
📦 OI      : 1,20,000 → 1,58,000
📐 Change  : +31.7%
💰 LTP     : 42.50 → 68.00
📉 Trend   : 1H Bearish | 3H Bearish

💡 Fresh CE build-up at 52000 aligned with Bearish Chart Trend — high confidence bearish signal.
```

---

## VSCode MCP Integration (BrowserMCP)

Added BrowserMCP server for programmatic browser automation via VSCode MCP tools.

**Setup:**
1. Reload VSCode window (Ctrl+Shift+P > "Developer: Reload Window")
2. Command Palette (Ctrl+Shift+P) > "MCP: List Servers" — verify "browsermcp"
3. Use MCP tools like `use_mcp_tool` with server_name="browsermcp" for browser ops (e.g., automate NSE page scraping, synergize with chrome_extension).

Config: `.vscode/mcp.json`
```json
{
  "browsermcp": {
    "command": "npx",
    "args": ["@browsermcp/mcp@latest"]
  }
}
```

## Out of Scope (Phase 1)
- Auto-trading / order execution
- Cloud deployment


### Implementation Alignment Note — Chart Trends

The Chrome extension stores chart telemetry in `chrome.storage.local` and the backend bridge expects the matched symbol's chart telemetry to be included in `/ingest/snapshot` as `chart_indicators`.

Recommended payload shape:

```json
{
  "chart_indicators": {
    "1h": {
      "sentiment": "BULLISH",
      "ohlc": {"open": 100, "high": 110, "low": 95, "close": 105},
      "updated_at": "2026-05-12T00:00:00Z"
    },
    "3h": {
      "sentiment": "BEARISH",
      "ohlc": null,
      "updated_at": "2026-05-12T00:00:00Z"
    }
  }
}
```

Current backend behavior:

- `anomaly_detector.py` passes `chart_indicators` into `scan_context`.
- `intelligence.py` uses `scan_context.chart_indicators` for 1H/3H chart confluence and confidence adjustment.
- Chart confluence is currently an intelligence/confidence input, not a standalone anomaly detector, unless a dedicated chart-confluence rule is added later.
