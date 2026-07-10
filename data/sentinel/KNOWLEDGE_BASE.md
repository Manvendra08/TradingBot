# NSEBOT Architecture — Scan Sentinel Knowledge Base

## Pipeline Flow
1. `fetch_option_chain(symbol)` → fetches from Shoonya/Dhan/Paytm.
2. `detect_anomalies()` → OI analysis, PCR, max pain, alerts.
3. `get_llm_verdict()` → AI trade recommendation.
4. `_sanitize_llm_verdict()` → validates/corrects LLM output.
5. Paper/Live trading execution.
6. `build_digest()` → Telegram/Discord alert.

## Known Failure Modes

### F1: Premium == Underlying (CRITICAL)
- **Symptom:** Target premium (target_1 or target_2) within 5% of underlying_price.
- **Root Cause:** BFO weekly options have zero volume. Shoonya returns spot price as LTP for untraded options. Sanitizer uses this fake LTP.
- **Self-Heal:** Flag the verdict as INVALID, skip trade execution (block trade_decision).

### F2: yfinance Constituent Fetch Failures
- **Symptom:** "possibly delisted" errors for .NS tickers.
- **Root Cause:** Yahoo Finance rate limiting or data gaps.
- **Impact:** Index weight calculation falls back to static defaults.
- **Self-Heal:** None needed (graceful fallback exists).

### F3: Option Type Mismatch (CE vs PE)
- **Symptom:** GO_SHORT action with CE instrument, or GO_LONG with PE.
- **Root Cause:** LLM outputs wrong option type (e.g. buying CE on a bearish trigger).
- **Self-Heal:** _sanitize_llm_verdict should auto-correct; if it fails, the sentinel flags it as CRITICAL and blocks the trade.

### F4: Fetcher Source Degradation
- **Symptom:** Multiple symbols falling back to secondary fetchers.
- **Root Cause:** Primary fetcher (Shoonya) auth failure or rate limit.
- **Self-Heal:** Trigger Shoonya re-auth via ops_agent.

### F5: Scan Duration Anomaly
- **Symptom:** Single symbol scan takes >120 seconds.
- **Root Cause:** LLM provider timeout, chart fetcher hanging.
- **Self-Heal:** None (informational alert, but log for visibility).

### F6: Zero OI Option Chain
- **Symptom:** >80% of strikes have oi=0 AND volume=0.
- **Root Cause:** After-hours scan, illiquid contract, or fetcher bug.
- **Impact:** OI-based signals (PCR, max pain) are unreliable.
- **Self-Heal:** Flag scan as LOW_CONFIDENCE, downgrade confidence levels.

### F7: Trend Alignment Dilution
- **Symptom:** Directional trades are blocked by trend alignment score < 70, even when the overall trend is strongly aligned.
- **Root Cause:** The trend alignment score formula counted non-directional scans ("Low Conviction", "Sideways") in the denominator, diluting the score.
- **Fix**: Modified `get_trend_alignment_score` to ignore neutral scans and only calculate the score based on directional ones.

### F8: Settings Cockpit Option Mismatches
- **Symptom:** Settings cockpit unsaved changes banner doesn't disappear after clicking "Save Now".
- **Root Cause:** Discrepancy between HTML options (missing `boost_only` option) and the default backend `runtimeConfig` settings, causing a perpetual dirty check mismatch.
- **Fix**: Added the missing option `boost_only` in settings UI and resolved the JS handler visibility checks.

## Architecture Notes
- Pipeline logs go to `logs/main.log` (RotatingFileHandler, 10MB).
- Health state stored in SQLite `health_state` table via `stamp_health()`.
- Ops Agent runs as separate process, reads health_state every 60s.
- LLM providers: OpenRouter, Groq, Gemini, GitHub Models (cascading fallback).
- Symbols: NIFTY (NFO), BANKNIFTY (NFO), SENSEX (BFO), NATURALGAS (MCX), CRUDEOIL (MCX).
