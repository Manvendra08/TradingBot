# External Integrations

## Market Data Sources
- **Dhan API v2**: Primary data source (`/optionchain` endpoint). Requires `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`.
- **NSE Public JSON**: Fallback source. Scrapes indices/equities option chains via public endpoints. Requires cookie warm-up and `User-Agent` headers.
- **Upstox API v2**: Tertiary data source (currently commented out in requirements, but logic exists).

## Notifications
- **Telegram**: Integration via Telegram Bot API. 
  - **Bot Token**: `TELEGRAM_BOT_TOKEN`
  - **Chat ID**: `TELEGRAM_CHAT_ID`
  - **Format**: Digest-based alerts for multiple anomalies, individual messages for HIGH severity.

## Local Services
- **Chrome Extension Bridge**: Local HTTP server on port 8765. Receives JSON webhooks from the NSEBOT Chrome extension.
- **SQLite Database**: Local file-based persistence at `data/nsebot.db`.
