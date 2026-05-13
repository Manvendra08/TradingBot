/**
 * background.js — NSEBOT
 * Added: START_BACKEND / STOP_BACKEND via fetch to bridge control endpoints.
 * Backend is started by running `python src/extension_bridge.py` on the host.
 * The popup can start/stop it via the /start and /stop control endpoints.
 */
'use strict';

const EXT_VERSION = String(chrome.runtime.getManifest().version || '');

async function fetchBackendWithFallback(message) {
  const urls = Array.isArray(message.urls) && message.urls.length ? message.urls : [message.url];
  const opts = { method: message.method || 'GET', headers: message.headers || {} };
  if (message.method && message.method !== 'GET' && message.body) opts.body = message.body;
  let lastErr = null;
  for (const url of urls.filter(Boolean)) {
    try {
      const r = await fetch(url, opts);
      if (r.ok) return { ok: true, status: r.status, url };
      lastErr = { ok: false, status: r.status, url };
    } catch(e) { lastErr = { ok: false, error: e.toString(), url }; }
  }
  return lastErr || { ok: false, error: 'No URLs provided' };
}

// ── Install ────────────────────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  console.log(`[NSEBOT] v${EXT_VERSION} installed`);
  chrome.storage.local.set({ nsebot_alerts: [], nsebot_scan_log: [], nsebot_scan_count: 0, nsebot_backend_running: false });
});

// ── Message handler ────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  if (msg.type === 'SHOW_NOTIFICATION') {
    chrome.notifications.create(`nsebot_${Date.now()}`, {
      type: 'basic', iconUrl: 'icons/icon48.png',
      title: msg.title, message: msg.body, priority: 2,
    });
    chrome.storage.local.get(['nsebot_alerts'], r => {
      const count = (r.nsebot_alerts || []).length;
      chrome.action.setBadgeText({ text: count > 0 ? String(count) : '' });
      chrome.action.setBadgeBackgroundColor({ color: '#ef5350' });
    });
  }

  if (msg.type === 'PING') { sendResponse({ pong: true }); return true; }

  if (msg.type === 'FETCH_BACKEND') {
    fetchBackendWithFallback(msg).then(sendResponse);
    return true;
  }

  // ── START backend bridge ────────────────────────────────────────────────
  // Calls POST /control/start on the bridge — if bridge is already running this succeeds.
  // If bridge is NOT running, the extension cannot spawn a subprocess directly (MV3 restriction).
  // Instead we call a lightweight "launcher" endpoint that the user can pre-start once.
  // The popup shows instructions if the bridge is unreachable.
  if (msg.type === 'START_BACKEND') {
    const baseUrl = (msg.backendUrl || 'http://localhost:8765').replace(/\/+$/, '');
    const candidates = [baseUrl, baseUrl.replace('localhost','127.0.0.1')].map(u => `${u}/control/start`);
    fetchBackendWithFallback({ urls: candidates, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
      .then(result => {
        chrome.storage.local.set({ nsebot_backend_running: result.ok });
        sendResponse(result);
      });
    return true;
  }

  // ── STOP backend bridge ─────────────────────────────────────────────────
  if (msg.type === 'STOP_BACKEND') {
    const baseUrl = (msg.backendUrl || 'http://localhost:8765').replace(/\/+$/, '');
    const candidates = [baseUrl, baseUrl.replace('localhost','127.0.0.1')].map(u => `${u}/control/stop`);
    fetchBackendWithFallback({ urls: candidates, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
      .then(result => {
        chrome.storage.local.set({ nsebot_backend_running: false });
        sendResponse(result);
      });
    return true;
  }

  // ── CHECK backend status ────────────────────────────────────────────────
  if (msg.type === 'CHECK_BACKEND') {
    const baseUrl = (msg.backendUrl || 'http://localhost:8765').replace(/\/+$/, '');
    const candidates = [baseUrl, baseUrl.replace('localhost','127.0.0.1')].map(u => `${u}/health`);
    fetchBackendWithFallback({ urls: candidates, method: 'GET' })
      .then(result => {
        chrome.storage.local.set({ nsebot_backend_ok: result.ok });
        sendResponse(result);
      });
    return true;
  }

  return true;
});

chrome.notifications.onClicked.addListener(() => chrome.action.openPopup().catch(() => {}));

chrome.storage.onChanged.addListener(changes => {
  if (changes.nsebot_alerts) {
    const count = (changes.nsebot_alerts.newValue || []).length;
    chrome.action.setBadgeText({ text: count > 0 ? String(count) : '' });
    chrome.action.setBadgeBackgroundColor({ color: '#ef5350' });
  }
});
