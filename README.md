# NSEBOT

Local NSE option-chain monitor, signal engine, Telegram digest bot, and paper-trading tracker.

## What it does

- Fetches option-chain data for watched symbols
- Filters strikes to ATM +/- 10 only
- Detects OI, PCR, IV, price, and max-pain anomalies
- Builds a trader-facing Telegram digest
- Auto-opens and manages paper trades from bot intelligence
- Stores scans, alerts, and paper trades in SQLite
- Serves a local dashboard with a dedicated paper-trading page

## Run

```bash
pip install -r requirements.txt
python main.py
```

One-shot scan:

```bash
python main.py --now
python main.py --now --symbols NIFTY BANKNIFTY
```

Dashboard:

```bash
python dashboard_server.py
```

Open:

- Main dashboard: `http://localhost:8080/`
- Paper trading page: `http://localhost:8080/paper`

## Scan frequency

The dashboard lets you set the scheduler interval. Available options:

- `5 min`
- `15 min`
- `30 min`
- `1H`
- `3H`
- `1D`

The scheduler runs only at the selected interval.

## Live data flow

`main.py -> scheduler -> pipeline -> fetch -> detect -> digest -> telegram -> paper trade`

## Current source routing

- NSE indices and equities use the active fetchers in `src/fetchers/router.py`
- MCX commodities use the commodity path already in the repo plus Moneycontrol fallback
- Upstox and Paytm are not part of the active live route
- NSE commodity chain is not part of the active live route

## Telegram digest

The Telegram message now includes:

- verdict and confidence
- action plan
- key levels
- candles (1H / 3H)
- OI pulse with add/reduce colors
- top signals
- balance and trend

The duplicate chart block was removed.

## Paper trading

The bot now creates paper trades from signal intelligence when the verdict is strong enough.

Tracked fields include:

- open and close time
- symbol
- option type
- strike
- entry, stop loss, target
- PnL in points
- status and close reason

## Files to know

- `main.py`
- `dashboard_server.py`
- `src/engine/pipeline.py`
- `src/engine/intelligence.py`
- `src/engine/paper_trading.py`
- `src/alerts/digest.py`
- `src/models/schema.py`

## Notes

- Keep the strike window tight around ATM.
- Keep Telegram text clean and trader-friendly.
- Prefer the current dashboard server over old Streamlit references.
