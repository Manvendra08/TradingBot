# GenTOTP – Trading 2FA Manager

**Chrome Extension** | Manifest V3 | Zero external dependencies

Secure, offline TOTP (Google Authenticator-compatible) auto-fill for **Zerodha Kite** and **Shoonya (Finvasia)** login pages.

---

## Features

| Feature | Detail |
|---|---|
| 🔐 **AES-256-GCM Vault** | Secrets encrypted with master password via PBKDF2 (310k iterations) |
| ⏱ **Live TOTP Countdown** | SVG ring countdown, refreshes every second |
| ⚡ **Auto-Fill** | One-click fills TOTP into broker login forms and optionally auto-submits |
| 📋 **Copy to Clipboard** | Instant clipboard copy per account |
| 🏦 **Multi-Account** | Supports Zerodha, Shoonya, and any RFC 6238 TOTP |
| ⚙️ **Configurable Lock** | Option in Settings to toggle password requirement on extension open |
| 🔒 **Lock Screen** | Vault locks on demand; configurable auto-unlock option |
| 🚫 **No Server** | Everything runs locally — no analytics, no network calls |

---

## Folder Structure

```
GenTOTP/
├── manifest.json               # MV3 manifest
├── background/
│   └── service_worker.js       # Message relay (popup ↔ content script)
├── content/
│   └── autofill.js             # Injected into broker pages; fills TOTP
├── lib/
│   ├── totp.js                 # RFC 6238 TOTP (Web Crypto API, no deps)
│   ├── vault.js                # AES-256-GCM encryption / decryption
│   └── store.js                # chrome.storage.local account CRUD
├── popup/
│   ├── popup.html              # Extension popup UI
│   ├── popup.css               # Dark trading terminal design
│   └── popup.js                # UI controller
└── icons/
    ├── icon16.png
    ├── icon32.png
    ├── icon48.png
    └── icon128.png
```

---

## Installation (Developer Mode)

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select the `GenTOTP/` folder
5. Pin the extension from the puzzle icon in the toolbar

---

## First-Time Setup

1. Click the **GenTOTP** icon in the toolbar
2. Create a **master password** (minimum 1 character) — used to encrypt all TOTP secrets locally
3. Tap **＋** → enter account label, select broker, paste your **Base32 TOTP secret**
   - The Base32 secret is shown alongside the QR code on the broker's 2FA setup page (usually labeled *"manual entry key"*)
4. Your live 6-digit code appears immediately with a 30-second countdown ring

## Finding Your TOTP Secret

### Zerodha
- Login → My Profile → Security → Two-factor Authentication
- Click **Disable** then **Enable** TOTP → the setup page shows the Base32 key

### Shoonya / Finvasia
- Login → Profile → Security Settings → TOTP Authenticator
- Click **Set Up** → the QR setup screen shows the manual key

> **Tip:** The Base32 secret looks like: `JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PX`

---

## Security Model

```
Master Password
      │
      ▼ PBKDF2-SHA256 (310,000 iterations, random 16-byte salt)
  AES-GCM Key
      │
      ▼ AES-256-GCM encrypt (random 12-byte IV)
  Encrypted Blob { salt, iv, ct }
      │
      ▼ chrome.storage.local (device-only, never synced)
```

- Secrets **never leave the device**
- Wrong master password → `DOMException` (AES-GCM authentication tag mismatch)
- Session password held in-memory only; cleared on vault lock

---

## Supported Brokers

| Broker | URL Pattern | TOTP Algorithm |
|---|---|---|
| Zerodha Kite | `kite.zerodha.com` | SHA1 / 6-digit / 30s |
| Shoonya (Finvasia) | `shoonya.finvasia.com`, `trade.shoonya.com` | SHA1 / 6-digit / 30s |
| Any other | Manual copy | RFC 6238 standard |

---

## Development Notes

- **TOTP generation** uses `crypto.subtle` (Web Crypto API) — available in both extension popup and service worker contexts
- **React-aware auto-fill** — `autofill.js` uses native property setter + synthetic `input`/`change` events to trigger React/Vue state updates on broker login forms
- **No `eval`, no remote scripts** — CSP-compliant (`script-src 'self'`)
- **Manifest V3** — uses service worker (not persistent background page)

---

## Roadmap

- [ ] Import/export encrypted vault backup
- [ ] QR code scanner for secret import (via camera)
- [ ] Auto-detect TOTP field on page load and fill without clicking
- [ ] Support for Angel Broking, Fyers, Upstox login pages
- [ ] Optional biometric unlock (Chrome passkey API)
