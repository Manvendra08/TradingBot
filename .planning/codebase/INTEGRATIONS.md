# External Integrations

## Market Data Sources & Fallbacks

### Dhan API v2 (Primary Index Source)
- **URL**: `https://api.dhan.co/v2`
- **Authentication**: Set via `access-token` (`DHAN_ACCESS_TOKEN`) and `client-id` (`DHAN_CLIENT_ID`) request headers.
- **Endpoints**:
  - Expiry list: POST `/optionchain/expirylist`
  - Option chain: POST `/optionchain`
- **Fallback**: Resolves expired MCX contracts dynamically via Dhan's script master CSV (`https://images.dhan.co/api-data/api-scrip-master.csv`) if commodity configuration IDs are missing.

### NSE India Public JSON API (Primary Fallback & Index Alternative)
- **URL**: `https://www.nseindia.com`
- **Endpoints**:
  - Indices Option Chain: `/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}`
  - Equity Option Chain: `/api/option-chain-v3?type=Equity&symbol={symbol}&expiry={expiry}`
  - Expiry Dates Lookup: `/api/option-chain-contract-info?symbol={symbol}`
  - Commodity Spot Rates: `/api/refrates?index=commodityspotrates`
- **Handshake Mechanism**: Requires warm-up hits to the NSE homepage and option-chain landing page to acquire necessary session cookies, utilizing custom headers (`User-Agent`, `Referer`, `Accept`, `Accept-Language`). Rated-limited and only used as a fallback.

### Dhan Headless & Commodity Scraping (Commodity Fallbacks)
- **Dhan Headless Fetcher**: Headless browser scraper fallback to fetch option chain structures via mock/simulated login behaviors.
- **Dhan Commodity Fetcher**: Public commodity page scraper executing fallbacks on MCX symbols (NATURALGAS, CRUDEOIL, etc.) when the API returns empty results or authentication fails.

---

## Fetcher Routing & Fallback Chains

Market data fetching is dynamically routed through `src/fetchers/router.py`, resolving option chains according to target asset classes:

1. **NSE Index & Equity Routing (FETCHER_PRIORITY)**:
   - Primary: `nse_public`
   - Secondary: `dhan`
   - Tertiary: `dhan_headless`
   - Quaternary: `dhan_commodity`
   - Quinary: `moneycontrol`

2. **MCX Commodity Routing**:
   - Primary: `dhan_commodity` (scraped/public tracker)
   - Secondary: `moneycontrol`
   - Tertiary: `dhan` (API-based, dynamic master CSV lookup)
   - Quaternary: `dhan_headless`

*In-place filtering limits output data to the ATM strike $\pm 10$ strikes (scanned engine default) and ATM $\pm 15$ strikes (HTML API output for the frontend).*

---

## Alerting & Telegram Dispatch Configurations

Alerts are sent to users using the Telegram Bot API via `python-telegram-bot` v21:

- **Token**: `TELEGRAM_BOT_TOKEN`
- **Chat ID**: `TELEGRAM_CHAT_ID`
- **Thread-Safety Architecture**:
  - Implements a dedicated background event loop running on `_loop_thread`.
  - Dispatches calls from APScheduler threads asynchronously using `asyncio.run_coroutine_threadsafe()`.
- **Formatting**: Alerts are configured with emojis mapped to anomaly types (e.g. `OI_SPIKE`, `IV_SPIKE`, `PCR_SHIFT`). Time stamps are parsed and explicitly converted to Indian Standard Time (IST).
- **HTTP Fallback Dispatcher**:
  - If the asyncio thread pool or coroutine queue times out (longer than 35s), the bot resets the event loop and executes a synchronous fallback.
  - Falls back to `urllib.request` using standard POST calls to `https://api.telegram.org/bot{token}/sendMessage` with `parse_mode="Markdown"` to bypass loop corruption.

---

## Additional External Services & API Feeds

- **TradingView News Feed (NATURALGAS)**:
  - Fetched from `https://news-mediator.tradingview.com/public/news-flow/v2/news`.
  - Used for 24-hour commodity sentiment analysis, direction scoring, and listing recent news on the dashboard.
- **Dhan ScanX Heatmap API**:
  - Fetched from `https://ow-scanx-analytics.dhan.co/customscan/fetchdt`.
  - Fetches index component advance/decline ratios and weighted change metrics to generate market direction sentiments (e.g., BULLISH / BEARISH / MIXED).
- **yfinance / tvDatafeed Charting**:
  - NIFTY / BANKNIFTY / FINNIFTY utilize `yfinance` to establish 1H/3H chart sentiments.
  - Commodities (e.g., NATURALGAS) prioritize `tvDatafeed` (authenticating using `TV_USERNAME` + `TV_PASSWORD` in `.env`) to get high-accuracy MCX data, falling back to `yfinance`.
  - Synthetic Charting fallback parses local database history in `underlying_price` to determine 1H/3H OHLC trends if both external chart providers fail.
