# Zerodha Broker Integration Plan

## 1. Product Overview & Objectives
To transition NSEBOT from a purely "Paper Trading" environment to a "Live Trading" engine utilizing Zerodha's Kite Connect API. The integration must be robust, asynchronous, and heavily guarded with risk management controls.

### Key Features:
- **Daily Authentication:** Secure handling of Kite API credentials and session tokens, including TOTP-based 2FA automation.
- **Automated Execution:** Instant entry order placement based on the intelligence engine's verdict.
- **GTT Automation:** Immediate placement of OCO (One Cancels the Other) GTT orders for automated Stop-Loss and Target handling, with premium-poll fallback for illiquid strikes.
- **Real-time Sync:** Tracking trade status via Webhooks (Postbacks) or Websockets to update the UI instantly (Pending, Executed, Rejected).
- **Risk & Safety:** Kill switch, daily loss limits, rate-limit handling, and shadow/dry-run mode for pre-production validation.

---

## 2. Phase 1: Database & Data Modeling
Before touching the broker API, the database needs to support live trading and API credentials.

**Tasks:**
1. **`broker_configs` Table:** Store `api_key`, `api_secret`, `access_token`, `request_token`, `last_login_date`, `totp_secret` (AES-encrypted at rest), and `broker_name`.
2. **Update `live_trades` Table:** Create a mirror of `paper_trades` with additional columns:
   - `broker_order_id`
   - `gtt_order_id`
   - `broker_status` (OPEN, REJECTED, COMPLETE, CANCELLED)
   - `broker_message` (To capture rejection reasons like margin shortfall)
3. **Migration Safety:** All schema changes must be implemented as idempotent migrations inside the existing `_MIGRATIONS` list in `schema.py` (pattern already established). No raw `ALTER TABLE` outside the migration runner.

---

## 3. Phase 2: Authentication & Session Management
Zerodha requires a daily manual login with TOTP-based 2FA to generate a request token, which is then exchanged for an access token.

**Tasks:**
1. **Login UI:** Add a "Broker Settings" page to the dashboard.
2. **OAuth Flow:**
   - Provide a "Login to Zerodha" button redirecting to `https://kite.trade/connect/login?v=3&api_key=XXX`.
   - Create a redirect handler endpoint (`/api/zerodha/callback`) to receive the `request_token`.
3. **TOTP / 2FA Automation (Critical):**
   - Zerodha mandates TOTP-based 2FA for all Kite Connect logins. The login redirect alone is insufficient.
   - **Chosen approach:** Store the user's TOTP secret (from Zerodha's 2FA setup QR code) in `broker_configs.totp_secret`, AES-encrypted at rest.
   - At login time, generate the current TOTP code using `pyotp.TOTP(secret).now()` and submit it programmatically to complete the 2FA step before the redirect fires.
   - **Dependency:** `pip install pyotp` — add to `requirements.txt`.
   - **Fallback (semi-automated):** If the user opts out of storing the TOTP secret, provide a UI input field to paste the 6-digit code manually during the daily login flow.
4. **Token Exchange:** Backend exchanges the `request_token` + `api_secret` for the `access_token` and saves it to the DB.
5. **Health Check:** A background job to verify if the token is still valid daily at 9:00 AM IST.

---

## 4. Phase 3: Core Order Execution
Connecting the intelligence engine's entry signals to Kite's Order Placement API.

**Tasks:**
1. **Symbol Translation (`symbol_resolver.py` — dedicated module):**
   - Map NSEBOT internal symbols (e.g., `"NIFTY"`) + expiry date + strike + option type to Zerodha's exact `tradingsymbol` format (e.g., `"NIFTY24JUN22000CE"`).
   - Must handle: weekly vs monthly expiry detection, NFO exchange for index options, MCX exchange for commodity futures/options, and symbol format changes on expiry rollover.
   - Source the instrument list via `kite.instruments("NFO")` / `kite.instruments("MCX")` at session start and cache for the day. Refresh on `TokenException`.
   - This is a standalone, fully-tested module — not a single utility function.

2. **Dynamic Capital Allocation & Symbol-Specific Lot Sizes:**
   Instead of a single lot override, lot sizes will be configurable **per symbol** in the settings. These parameters will be managed via `config/runtime_config.py` and persisted in `data/runtime_config.json`:
   - `LIVE_CAPITAL_PER_TRADE_INR` — fixed ₹ amount allocated per trade entry (default: 50,000)
   - `LIVE_MAX_CAPITAL_UTILISATION_PCT` — max % of available margin to use across all open positions (default: 80)
   - `LIVE_MAX_CONCURRENT_POSITIONS` — hard cap on simultaneous open live trades (default: 2)
   - `LIVE_SYMBOL_LOTS` — mapping of symbol-specific manual lot sizes (e.g. `{"NIFTY": 1, "BANKNIFTY": 2, "NATURALGAS": 1, "CRUDEOIL": 1}`)
   - Order lot size calculation: If `LIVE_SYMBOL_LOTS` contains an override for the symbol, use it. Otherwise, auto-calculate: `floor(LIVE_CAPITAL_PER_TRADE_INR / (entry_premium * instrument_lot_size))`, capped by `LIVE_MAX_CONCURRENT_POSITIONS` and margin checks.

3. **Market/Limit Orders:** Implement a service wrapper around the official `kiteconnect` Python library to place `REGULAR` orders on NFO/MCX.

4. **Shadow / Dry-Run Mode (Pre-Production Validation):**
   - Add `LIVE_SHADOW_MODE = True` to `config/settings.py`.
   - When enabled, signals flow through the entire live execution code path (symbol resolution, order construction, GTT payload building, risk checks) but orders are **suppressed** — logged at `INFO` level with prefix `[SHADOW]` instead of being sent to Kite API.
   - This mode must be active for a minimum of 2 full trading sessions before switching to real capital.
   - Shadow trades are written to `live_trades` with `broker_status = 'SHADOW'` for post-session review.

5. **`TradingInterface` — Order State Machine (Dedicated Phase 3b):**
   This is the most complex component. It must be scoped as a full state machine, not a simple class wrapper.
   - **States:** `PENDING_ENTRY` → `ENTRY_OPEN` → `ENTRY_COMPLETE` / `ENTRY_REJECTED` → `GTT_PLACED` → `GTT_TRIGGERED` / `GTT_CANCELLED` → `CLOSED`
   - **Responsibilities:**
     - Accepts a trade signal and drives it through the full order lifecycle
     - Handles async status updates from postback and fallback polling
     - Owns GTT placement, modification, and cancellation
     - Surfaces state to the dashboard via `live_trades` DB row updates
     - Respects `KILL_SWITCH_ACTIVE` and `LIVE_SHADOW_MODE` flags at every transition
   - **Error boundaries:** Every state transition catches `KiteException` sub-types (`TokenException`, `NetworkException`, `OrderException`) and routes to appropriate recovery or alert path — never silently swallows.
   - Estimated effort: 3–4x a standard service wrapper. Plan a dedicated sprint.

6. **Concurrent Dual Execution (Paper + Live):** Rather than choosing either paper or live mode, the engine must execute both side-by-side to preserve the existing paper trading telemetry.
   - The data pipeline orchestrator (`_process_symbol` in `src/engine/pipeline.py`) will run `run_paper_trading(...)` and `run_live_trading(...)` sequentially.
   - Paper trades continue to log to the `paper_trades` table.
   - Live (or shadow) trades will be routed to the `TradingInterface` and log exclusively to the `live_trades` table. This isolates live account margins/rejections from paper trading statistics.

---

## 5. Phase 4: Automated GTT (Target & SL)
Once an entry order is executed, NSEBOT should instantly protect it using Zerodha's Good Till Triggered (GTT) OCO orders.

**Tasks:**
1. **GTT Payload Construction:**
   - Setup an `OCO` (One Cancels Other) GTT.
   - **Stop Loss Leg:** Set `trigger_values` based on ATR or strict % SL.
   - **Target Leg:** Set `trigger_values` based on risk-reward ratio.

2. **Placement Trigger:** Wait for the primary order to hit `COMPLETE` status before firing the GTT API, or place it concurrently if executing a Market order.

3. **GTT Tracking:** Save the `gtt_trigger_id` to the database to manage or cancel it later if the user manually exits the trade.

4. **GTT Failure Fallback (Options-Specific — Critical):**
   - Zerodha GTT does **not** reliably support all option strikes. Deep OTM and illiquid strikes frequently see GTT trigger failures or silent rejections on NFO/MCX.
   - **Fallback behaviour:** If GTT placement returns an error, or `gtt_trigger_id` is not returned within 5 seconds of `ENTRY_COMPLETE`:
     - Log at `ERROR` level and fire a Telegram alert: `[GTT FAILED] {symbol} — falling back to premium-poll exit`.
     - Activate premium-based polling exit (adapted from the existing `_maybe_close_open_trade()` logic in `paper_trading.py`) for this trade.
     - Set `live_trades.gtt_order_id = NULL` and `live_trades.exit_mode = 'POLL'` so the dashboard surfaces the fallback state clearly.
   - Premium-poll exit checks every 30 seconds (same as fallback polling in Phase 5).

---

## 6. Phase 5: Trade Status & Synchronization
The system needs to know exactly what happens to an order after it is sent to Zerodha.

**Tasks:**
1. **Postback (Webhook) Endpoint — with Checksum Validation:**
   - Create endpoint `POST /api/zerodha/postback`.
   - **Checksum validation (mandatory):** Zerodha signs each postback. Validate using HMAC-SHA256:
     ```python
     import hmac, hashlib
     def validate_postback_checksum(order_id: str, timestamp: str, checksum: str, api_secret: str) -> bool:
         message = f"{order_id}{timestamp}{api_secret}"
         expected = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
         return hmac.compare_digest(expected, checksum)
     ```
   - Reject and log any postback that fails checksum validation. Do not process.
   - The endpoint is HTTP-accessible but not authenticated — checksum is the only security boundary. Ensure it is rate-limited.

2. **Status Reconciliation:**
   - When an order updates from `OPEN` to `COMPLETE`, update `live_trades` and trigger GTT placement.
   - If `REJECTED`, fire a Telegram alert and close the internal trade state. Log `broker_message`.

3. **Fallback Polling:** In case webhooks fail, implement a periodic poll (every 30 seconds) checking `kite.orders()` for pending trades.

---

## 7. Phase 6: UI/UX & Live Broker Dashboard
Instead of just a toggle, a dedicated, fresh broker monitoring page (`src/dashboard/broker.html` served at `/broker`) will be added to distinctively track live broker activities, authentication, and positions.

**Tasks:**
1. **FastAPI Endpoints:** Add a new `@app.get("/broker")` HTML endpoint, and telemetry endpoints (`/api/live_trades`, `/api/broker_status`, `/api/broker_margin`) in `dashboard_server.py`. Add endpoints to read (`GET /api/settings`) and write (`POST /api/settings`) runtime configurations dynamically.
2. **Broker Auth & Token Panel:** Display access token validity, connection status, and daily TOTP integration status.
3. **Active Positions Widget:** Tabulate live/shadow positions from the `live_trades` table showing symbol, side, qty, entry price, current premium, and real-time M2M P&L.
4. **Live GTT Leg Monitor:** Expose active GTT triggers (Target/SL leg trigger prices).
5. **Emergency Kill Switch:** A red button to trigger global square-off, cancel pending orders/GTTs, and halt live scans immediately.
6. **Account Widget:** Show Live Margin Available, Used Margin, and Free cash telemetry pulled from the Kite API.
7. **Shadow Mode Indicator:** If `LIVE_SHADOW_MODE = True`, display a warning banner: `SHADOW MODE — Orders are simulated and logged as 'SHADOW'. No real capital is at risk.`
8. **Settings Management UI Panel:** Add a configuration form on the dashboard to view and modify runtime parameters instantly without restarting the bot:
   - Toggle `LIVE_SHADOW_MODE` (True/False).
   - Edit symbol-specific lot sizes (`LIVE_SYMBOL_LOTS` overrides).
   - Set scan frequencies per market class (NSE index vs MCX commodity).
   - Set risk parameters (Max concurrent positions, max capital utilisation, max daily loss).
   - Adjust trigger thresholds (OI spike %, Price spike %).

---

## 8. Phase 7: Risk Management (Critical)
Safeguards to prevent algorithmic disasters (e.g., infinite loop order placing).

**Tasks:**
1. **The Kill Switch:** A big red button on the UI that:
   - Sets `KILL_SWITCH_ACTIVE = True` in a persistent DB flag (`broker_configs.kill_switch_active`).
   - **Stops the pipeline scanner immediately** — `run_live_trading()` checks `KILL_SWITCH_ACTIVE` at the top of every cycle and exits the loop. The scanner does not place new entries after kill switch activation regardless of signal quality.
   - Cancels all open pending orders via `kite.cancel_order()`.
   - Cancels all active GTTs via `kite.delete_gtt()`.
   - Squares off all open positions at Market via `kite.place_order()` with `order_type=MARKET`.
   - Fires a Telegram alert: `[KILL SWITCH ACTIVATED] All positions squared off. Scanner halted.`

2. **Hard Limits:** Reject trades internally if:
   - Daily Max Loss is hit (check against `LIVE_MAX_DAILY_LOSS_INR` in settings).
   - Max consecutive losses are hit (check against `LIVE_MAX_CONSECUTIVE_LOSSES`).
   - API Rate Limit is nearing exhaustion — implement a **token-bucket rate limiter**: 3 tokens/sec refill rate, max burst 3. All Kite API calls go through the bucket. If a call would exceed the limit, it waits (up to 2 seconds) before firing, then logs a warning. Respect the 200 orders/minute hard cap separately.

3. **Order Retry Circuit Breaker:** Never retry a rejected order automatically without manual intervention. Log rejection reason from `broker_message` and surface in UI.

---

## 9. Next Steps / Approval
To proceed with this implementation, tackle this iteratively.

**Suggested First Step:** Start with **Phase 1 & Phase 2** (DB schema migrations, `broker_configs` table, TOTP secret storage, and OAuth flow with `pyotp` integration).

**Pre-Flight Checklist before Phase 3:**
- [ ] `pyotp` added to `requirements.txt`
- [ ] `broker_configs.totp_secret` encrypted at rest
- [ ] Capital allocation parameters defined in `config/settings.py`
- [ ] `LIVE_SHADOW_MODE = True` set — stay in shadow mode for minimum 2 sessions
- [ ] `symbol_resolver.py` unit-tested for NFO + MCX symbol formats
- [ ] Postback checksum validation tested against Zerodha's test payload

Please review and approve this plan to begin implementation.
