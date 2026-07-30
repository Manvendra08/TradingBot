/**
 * GenTOTP – Background Service Worker
 * Handles messages from popup and content scripts.
 * Acts as the bridge: popup → service worker → content script.
 */

"use strict";

importScripts("../lib/totp.js", "../lib/vault.js", "../lib/store.js");

// ─── Message Handler ─────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "PING") {
    sendResponse({ ok: true });
    return;
  }

  if (message.type === "GET_AUTO_TOTP") {
    (async () => {
      try {
        const settings = await new Promise(r => chrome.storage.local.get(["gentop_auto_fill_detect", "gentop_cached_pw"], r));
        if (!settings.gentop_auto_fill_detect) {
          sendResponse({ ok: false, error: "Auto-fill on detect disabled" });
          return;
        }

        const pw = CryptoVault.getSessionPassword() || settings.gentop_cached_pw;
        if (!pw) {
          sendResponse({ ok: false, error: "Vault locked" });
          return;
        }

        const accounts = await AccountStore.readAll();
        if (!accounts || accounts.length === 0) {
          sendResponse({ ok: false, error: "No accounts saved" });
          return;
        }

        const targetBroker = message.broker || "zerodha";
        let targetAcc = accounts.find(a => a.broker === targetBroker) || accounts.find(a => a.broker === "other") || accounts[0];

        const secret = await CryptoVault.decrypt(targetAcc.encryptedSecret, pw);
        const code = await TOTP.generate(secret);
        sendResponse({ ok: true, code, broker: targetAcc.broker, label: targetAcc.label });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (message.type === "AUTOFILL_TOTP") {
    // Forward TOTP code to active tab content script
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs || tabs.length === 0) {
        sendResponse({ ok: false, error: "No active tab" });
        return;
      }
      chrome.tabs.sendMessage(
        tabs[0].id,
        { type: "FILL_TOTP", code: message.code, broker: message.broker },
        (response) => {
          if (chrome.runtime.lastError) {
            sendResponse({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            sendResponse(response);
          }
        }
      );
    });
    return true; // keep channel open for async response
  }
});

// ─── Install / Update Listener ───────────────────────────────────────────────
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    console.log("[GenTOTP] Extension installed. Open the popup to set up your vault.");
  }
});
