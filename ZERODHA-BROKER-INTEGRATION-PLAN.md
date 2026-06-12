# Zerodha Broker Integration Plan

## 1. Product Overview & Objectives
To transition NSEBOT from a purely "Paper Trading" environment to a "Live Trading" engine utilizing Zerodha's Kite Connect API. The integration must be robust, asynchronous, and heavily guarded with risk management controls. 

### Key Features:
- **Daily Authentication:** Secure handling of Kite API credentials and session tokens.
- **Automated Execution:** Instant entry order placement based on the intelligence engine's verdict.
- **GTT Automation:** Immediate placement of OCO (One Cancels the Other) GTT orders for automated Stop-Loss and Target handling.
- **Real-time Sync:** Tracking trade status via Webhooks (Postbacks) or Websockets to update the UI instantly (Pending, Executed, Rejected).
- **Risk & Safety:** Kill switch, daily loss limits, and rate-limit handling.

---

## 2. Phase 1: Database & Data Modeling
Before touching the broker API, the database needs to support live trading and API credentials.

**Tasks:**
1. **`broker_configs` Table:** Store `api_key`, `api_secret`, `access_token`, `request_token`, `last_login_date`, and `broker_name`.
2. **Update `live_trades` Table:** Create a mirror of `paper_trades` with additional columns: 
   - `broker_order_id`
   - `gtt_order_id`
   - `broker_status` (OPEN, REJECTED, COMPLETE, CANCELLED)
   - `broker_message` (To capture rejection reasons like margin shortfall)

---

## 3. Phase 2: Authentication & Session Management
Zerodha requires a daily manual login to generate a request token, which is then exchanged for an access token.

**Tasks:**
1. **Login UI:** Add a "Broker Settings" page to the dashboard.
2. **OAuth Flow:** 
   - Provide a "Login to Zerodha" button redirecting to `https://kite.trade/connect/login?v=3&api_key=XXX`.
   - Create a redirect handler endpoint (`/api/zerodha/callback`) to receive the `request_token`.
3. **Token Exchange:** Backend exchanges the `request_token` + `api_secret` for the `access_token` and saves it to the DB.
4. **Health Check:** A background job to verify if the token is still valid daily at 9:00 AM.

---

## 4. Phase 3: Core Order Execution
Connecting the intelligence engine's entry signals to Kite's Order Placement API.

**Tasks:**
1. **Symbol Translation:** Map NSEBOT symbols (e.g., "NIFTY") and expiry dates to Zerodha's exact `tradingsymbol` (e.g., "NIFTY24JUN22000CE").
2. **Market/Limit Orders:** Implement a service wrapper around the official `kiteconnect` python library to place `REGULAR` orders.
3. **Lot Size Calculation:** Ensure the number of lots is dynamically calculated based on capital allocation settings.
4. **Execution Abstraction:** Introduce a `TradingInterface` class so the engine doesn't care if it's trading on paper or live.

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

---

## 6. Phase 5: Trade Status & Synchronization
The system needs to know exactly what happens to an order after it is sent to Zerodha.

**Tasks:**
1. **Postback (Webhook) Endpoint:** Create an unauthenticated, but checksum-validated endpoint (`/api/zerodha/postback`) to receive real-time order updates from Zerodha.
2. **Status Reconciliation:** 
   - When an order updates from `OPEN` to `COMPLETE`, update `live_trades`.
   - If `REJECTED`, fire an alert (Telegram/UI) and close the internal trade state.
3. **Fallback Polling:** In case webhooks fail, implement a periodic poll (every 30 seconds) checking `kite.orders()` for pending trades.

---

## 7. Phase 6: UI/UX & Live Dashboard
The frontend must distinctly separate Paper Trades from Live Trades and provide manual overrides.

**Tasks:**
1. **Live vs Paper Toggle:** A global toggle on the dashboard to view either Paper tracking or Live tracking.
2. **Action Controls:** Buttons on active live trades to "Exit Market" (which cancels the GTT and fires a market exit order) or "Modify SL".
3. **Account Widget:** Display Live Margin Available, Used Margin, and Live M2M (Mark to Market) pulled from the Kite API.

---

## 8. Phase 7: Risk Management (Critical)
Safeguards to prevent algorithmic disasters (e.g., infinite loop order placing).

**Tasks:**
1. **The Kill Switch:** A big red button on the UI that:
   - Disables further entries.
   - Cancels all open pending orders.
   - Cancels all GTTs.
   - Squares off all open positions at Market.
2. **Hard Limits:** Reject trades internally if:
   - Daily Max Loss is hit.
   - Max consecutive losses are hit.
   - API Rate Limit (e.g., 3 requests/sec) is nearing exhaustion.
3. **Order Retry Circuit Breaker:** Never retry a rejected order automatically without manual intervention.

---

## 9. Next Steps / Approval
To proceed with this implementation, we will tackle this iteratively.

**Suggested First Step:** Start with **Phase 1 & Phase 2** (Setting up the DB, UI for credentials, and OAuth flow). 

Please review and approve this plan to begin implementation.