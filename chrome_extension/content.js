/**
 * content.js — NSEBOT v2.6.0
 * Fixes:
 *  - Dhan: bridge properly injected (was disabled in v2.4 boot)
 *  - Dhan: expiry from active button only, not all buttons
 *  - Dhan: underlying from bridge API data, not DOM
 *  - Added ATM LTP spike detection (CE + PE)
 *  - All fired_at timestamps in IST
 */
'use strict';

// ── Security & Target check ──────────────────────────────────────────────────
const isSupported =
  window.location.href.includes('options-trader.dhan.co/advanceoptionchain') ||
  window.location.href.includes('nseindia.com/option-chain') ||
  window.location.href.includes('sensibull.com') ||
  window.location.href.includes('opstra.definedge.com');

if (!isSupported) {
  console.log('[NSEBOT] Inactive: valid only on supported trading sites');
} else {
  console.log('[NSEBOT] Active on supported trading site');
}

// ── Config ──────────────────────────────────────────────────────────────────
let CFG = {
  oiThreshold: 25,   // % OI change to trigger OI_SPIKE alert
  ltpSpikeThreshPct: 5,    // % ATM LTP change to trigger LTP_SPIKE alert
  intervalMin: 5,
  strikeRange: 15,
  notifications: true,
  forwardBackend: true,
  backendUrl: 'http://localhost:8765',
};

const STORAGE = {
  ALERTS: 'nsebot_alerts',
  SNAPSHOT: 'nsebot_snapshot',
  SITE: 'nsebot_site',
  SCAN_LOG: 'nsebot_scan_log',
  LAST_SCAN_TS: 'nsebot_last_scan_ts',
  SCAN_COUNT: 'nsebot_scan_count',
  SETTINGS: 'nsebot_settings',
  BACKEND_OK: 'nsebot_backend_ok',
};

const MAX_ALERTS = 50;
const MAX_LOG = 80;
const DEFAULT_ATM_STRIKE_WINDOW = 15;
const DHAN_BRIDGE_SRC = 'nsebot-dhan-bridge';
const NSE_BRIDGE_SRC = 'nsebot-page-bridge';
const EXT_SRC = 'nsebot-extension';

function safeStorageGet(keys, cb) { try { chrome.storage.local.get(keys, (res) => { if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError); if (cb) cb(res || {}); }); } catch (e) { if (cb) cb({}); } }
function safeStorageSet(obj, cb) { try { chrome.storage.local.set(obj, () => { if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError); if (cb) cb(); }); } catch (e) { if (cb) cb(); } }
function safeSendMessage(msg, cb) { try { chrome.runtime.sendMessage(msg, (res) => { if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError); if (cb) cb(res || {}); }); } catch (e) {} }

// ── State ────────────────────────────────────────────────────────────────────
let prevStrikeMap = {};   // { "CE_22000": { oi, ltp } }
let prevAtmLtp = {};      // { CE: number, PE: number } — for LTP spike
let scanIntervalId = null;
let lastGoodData = null;
let bridgeReady = false;
let dhanBridgeReady = false;
let lastSnapshotSymbol = '';
let currentDhanSymbol = '';
let _pendingForceScan = false;   // next scan will be tagged force:true
let _nextIsBaseline = false;     // next scan after symbol switch is baseline only

// ── Logging ──────────────────────────────────────────────────────────────────
function diag(msg, ok = true, warn = false) {
  const entry = { ts: new Date().toISOString(), msg, ok, warn };
  console.log(`[NSEBOT] ${ok ? (warn ? '⚠' : '✓') : '✗'} ${msg}`);
  addScanLog(entry);
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function nowIST() {
  return new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Kolkata' }).replace(' ', 'T') + '+05:30';
}
function normalizeBase(url) {
  return (url || 'http://localhost:8765').trim().replace(/\/+$/, '');
}
function candidateUrls(path) {
  const b = normalizeBase(CFG.backendUrl);
  const alts = b.includes('localhost')
    ? [b, b.replace('localhost', '127.0.0.1')]
    : b.includes('127.0.0.1')
      ? [b, b.replace('127.0.0.1', 'localhost')]
      : [b];
  return [...new Set(alts)].map(u => `${u}${path}`);
}
function toNum(s) {
  let val = String(s || '').toUpperCase().replace(/,/g, '').trim();
  if (!val) return 0;
  let multiplier = 1;
  if (val.endsWith('K')) { multiplier = 1000; val = val.slice(0, -1); }
  else if (val.endsWith('L')) { multiplier = 100000; val = val.slice(0, -1); }
  else if (val.endsWith('M')) { multiplier = 1000000; val = val.slice(0, -1); }
  else if (val.endsWith('CR')) { multiplier = 10000000; val = val.slice(0, -2); }
  return (parseFloat(val) * multiplier) || 0;
}
function toInt(s) { return Math.round(toNum(s)); }
// Strip Dhan OI rank badge text from a cell's textContent.
// Dhan injects orange rank badges ("OI 1"–"OI 5") inside OI cells, either as
// child <span> elements or as inline text appended to the value.
// Uses a clone so the live DOM is never mutated.
function cellText(cell) {
  if (!cell) return '';
  const clone = cell.cloneNode(true);
  // Remove any descendant element whose ENTIRE text is the badge pattern "OI N"
  clone.querySelectorAll('span, div, label, i, em, b').forEach(child => {
    if (/^OI\s*\d{1,2}$/i.test(child.textContent.trim())) child.remove();
  });
  // Also strip any residual badge text left as a direct text node (e.g. "50.65LOI 3")
  return clone.textContent.replace(/OI\s*\d{1,2}/gi, '').trim();
}
function firstFinite(...vals) {
  for (const v of vals) { const n = Number(v); if (Number.isFinite(n)) return n; }
  return null;
}
function firstPositive(...vals) {
  for (const v of vals) { const n = Number(v); if (Number.isFinite(n) && n > 0) return n; }
  return 0;
}

function normChartSym(s) {
  if (!s) return '';
  return String(s).toUpperCase()
    .replace(/MCX:|NSE:|NFO:|CDS:/g, '')
    .replace(/\s+/g, '')
    .replace(/[!]/g, '')
    .replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?/gi, '')
    .replace(/[^A-Z0-9]/g, '');
}

function findChartDataForSymbol(chartData, symbol) {
  if (!chartData || !symbol) return null;
  if (chartData[symbol]) return chartData[symbol];

  const target = normChartSym(symbol);
  if (!target) return null;

  const keys = Object.keys(chartData);
  const exactKey = keys.find(k => normChartSym(k) === target);
  if (exactKey) return chartData[exactKey];

  const looseKey = keys.find(k => {
    const nk = normChartSym(k);
    if (!nk) return false;
    const longer = nk.length >= target.length ? nk : target;
    const shorter = nk.length < target.length ? nk : target;
    return longer.startsWith(shorter) && (longer.length - shorter.length <= 2 || /\d+$/.test(longer));
  });
  return looseKey ? chartData[looseKey] : null;
}

function getMetricValue(label) {
  const elements = Array.from(document.querySelectorAll('div, span, p, label, b, strong'));
  const target = elements.find(el => {
    const t = el.textContent.trim();
    return t === label || t === `${label}:`;
  });
  if (!target) return null;
  const parentText = (target.parentElement.textContent || '').trim();
  const valuePart = parentText.replace(label, '').replace(':', '').trim();
  if (!valuePart && target.nextElementSibling) return target.nextElementSibling.textContent.trim();
  return valuePart || null;
}

// ── Settings ─────────────────────────────────────────────────────────────────
function loadSettings(cb) {
  safeStorageGet([STORAGE.SETTINGS], r => {
    if (r[STORAGE.SETTINGS]) CFG = { ...CFG, ...r[STORAGE.SETTINGS] };
    if (cb) cb();
  });
}
function scheduleScans() {
  if (scanIntervalId) clearInterval(scanIntervalId);
  const ms = (CFG.intervalMin || 5) * 60 * 1000;
  scanIntervalId = setInterval(runPeriodicScan, ms);
  diag(`Scan every ${CFG.intervalMin} min`, true);
}
function runPeriodicScan() {
  const site = detectSite();
  diag(`Periodic scan (${site})`, true);
  if (site === 'nse') triggerNSEApiFetch();
  else if (site === 'dhan') triggerDhanBridgeFetch();
  else domScrapeWithRetry(site, 0);
}

// ── Messages from popup ───────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'FORCE_SCAN') {
    diag('Force scan from popup', true);
    _pendingForceScan = true;
    const site = detectSite();
    if (site === 'nse') triggerNSEApiFetch();
    else if (site === 'dhan') triggerDhanBridgeFetch();
    else domScrapeWithRetry(site, 0);
    sendResponse({ ok: true });
  }
  if (msg.type === 'SETTINGS_UPDATED') {
    CFG = { ...CFG, ...msg.settings };
    scheduleScans();
    diag(`Settings: interval=${CFG.intervalMin}m range=±${CFG.strikeRange} OI%=${CFG.oiThreshold} LTP%=${CFG.ltpSpikeThreshPct}`, true);
    sendResponse({ ok: true });
  }
});

// ── Site detector ─────────────────────────────────────────────────────────────
function detectSite() {
  const h = window.location.hostname;
  if (h.includes('nseindia')) return 'nse';
  if (h.includes('options-trader.dhan.co')) return 'dhan';
  if (h.includes('sensibull')) return 'sensibull';
  if (h.includes('opstra')) return 'opstra';
  return null;
}

// ══════════════════════════════════════════════════════════════════════════════
// DHAN — bridge (MAIN world) + DOM fallback
// ══════════════════════════════════════════════════════════════════════════════

function injectDhanBridge() {
  if (document.getElementById('nsebot-dhan-bridge')) return;
  const s = document.createElement('script');
  s.id = 'nsebot-dhan-bridge';
  s.src = chrome.runtime.getURL('dhan_bridge.js');
  s.onload = () => { s.remove(); diag('Dhan bridge loaded', true); };
  s.onerror = () => diag('Dhan bridge load FAILED — reload page', false);
  (document.head || document.documentElement).appendChild(s);
}

function handleDhanBridgeMessage(event) {
  if (event.source !== window) return;
  const d = event.data;
  if (!d || d.source !== DHAN_BRIDGE_SRC) return;

  if (d.type === 'NSEBOT_DHAN_BRIDGE_READY') {
    dhanBridgeReady = true;
    diag('Dhan bridge ready (MAIN world)', true);
    return;
  }
  if (d.type === 'NSEBOT_DHAN_DIAG') {
    diag(d.payload?.msg || '?', !!d.payload?.ok, !!d.payload?.warn);
    return;
  }
  if (d.type === 'NSEBOT_DHAN_API_DATA') {
    const { parsed } = d.payload || {};
    if (!parsed?.strikes?.length) {
      diag('Dhan bridge: API data — no strikes', false, true);
      return;
    }

    // Prefer DOM symbol (human-readable) over numeric scrip from API body
    const domSym = detectDhanSymbol();
    const symbol = (domSym && domSym !== 'UNKNOWN')
      ? domSym
      : (parsed.symbol || parsed.bodySymbol || 'UNKNOWN');

    // Underlying: trust bridge API value first, then DOM, then infer from strikes
    const underlying = firstPositive(
      parsed.underlying,
      detectDhanUnderlying(),
      inferUnderlyingFromStrikes(parsed.strikes)
    );

    // Expiry: from bridge (parsed from oc_data keys) + active DOM button
    const expiry = parsed.expiry || detectActiveExpiry();

    const oc = { symbol, underlying, expiry, strikes: parsed.strikes, summary: parsed.summary, site: 'dhan' };
    diag(`Dhan bridge API: ${symbol} | ${oc.strikes.length} rows | spot:${underlying} | expiry:${expiry}`, true);
    lastGoodData = oc;
    processSnapshot(oc, 'dhan');
  }
}

function detectDhanSymbol() {
  // 1. Text-based header detection (e.g. "NIFTY Option Chain")
  const all = Array.from(document.querySelectorAll('div, span, h1, h2, h3'));
  const header = all.find(el => {
    const t = (el.textContent || '').trim().toUpperCase();
    return t.endsWith('OPTION CHAIN') && t.length > 12 && t.length < 50;
  });
  if (header) {
    const s = header.textContent.trim().toUpperCase().replace(/OPTION CHAIN$/i, '').trim();
    if (s && !s.includes('\n')) { currentDhanSymbol = s; return s; }
  }

  // 2. Class-based fallback
  const symLabel = document.querySelector('.symbol-name, [class*="symbolName"], [class*="instrument-name"]');
  if (symLabel) {
    const s = symLabel.textContent.trim().toUpperCase().replace(/OPTION CHAIN$/i, '').trim();
    if (s) { currentDhanSymbol = s; return s; }
  }

  // 3. Page title
  const title = document.title || '';
  const tm = title.match(/^([A-Z][A-Z0-9 &-]{1,18})\s+(?:OPTION|OC|OPTION CHAIN)/i);
  if (tm) { const s = tm[1].trim(); if (s) { currentDhanSymbol = s; return s; } }

  // H1/H2 containing "option chain"
  for (const el of document.querySelectorAll('h1,h2,h3')) {
    const txt = (el.textContent || '').trim();
    if (/option\s*chain/i.test(txt)) {
      const cleaned = txt
        .replace(/option\s*chain/gi, '')
        .replace(/[^A-Z0-9 &-]/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .toUpperCase();
      if (cleaned && cleaned.length >= 2 && cleaned.length <= 20) {
        currentDhanSymbol = cleaned;
        return cleaned;
      }
    }
  }

  // Selected dropdown value
  for (const el of document.querySelectorAll('select option:checked, [class*="selected"]')) {
    const txt = ((el.value || el.textContent || '')).trim().toUpperCase();
    if (/^[A-Z][A-Z0-9 &-]{1,18}$/.test(txt) && !/^(SELECT|SYMBOL|CHOOSE|DHAN)/.test(txt)) {
      currentDhanSymbol = txt;
      return txt;
    }
  }

  return currentDhanSymbol || 'UNKNOWN';
}

// ── Active expiry from DOM buttons ───────────────────────────────────────────
function detectActiveExpiry() {
  const expirySelectors = [
    '[class*="expiry"] [class*="active"]',
    '[class*="expiry"] [aria-selected="true"]',
    '[class*="expiryTab"].active',
    '[class*="expiry-tab"].active',
    'button[class*="expiry"].active',
    '[data-testid*="expiry"][aria-selected="true"]',
  ];
  for (const sel of expirySelectors) {
    const el = document.querySelector(sel);
    if (el) {
      const txt = (el.textContent || '').trim();
      if (txt) return txt;
    }
  }
  // Fallback: first button in an expiry container
  const container = document.querySelector('[class*="expiry"],[class*="Expiry"]');
  if (container) {
    const btns = container.querySelectorAll('button, [role="tab"]');
    const active = Array.from(btns).find(b =>
      b.classList.contains('active') || b.getAttribute('aria-selected') === 'true'
    );
    if (active) return (active.textContent || '').trim();
  }
  return '';
}

function detectDhanUnderlying() {
  // 1. Look for ATM highlight / Floating row (usually a green box between strikes)
  // Search for bg-green-500, atm-price-label, spot-price, etc.
  const atmCell = document.querySelector(
    '.bg-green-500, .atm-price-label, [class*="bg-green"], [class*="spot-box"], ' +
    '[class*="liveSpot"], [class*="spotPrice"], [class*="activeStrike"]'
  );
  if (atmCell) {
    const raw = atmCell.textContent.replace(/[^\d.]/g, '');
    const v = parseFloat(raw);
    // Spot price can be anything from 1 to 1M (commodities like NG are low, Nifty high)
    if (v > 1) return v;
  }

  // 2. Specific selectors for non-table elements (header)
  const sels = [
    '[data-testid="spot-price"]',
    '[data-testid="ltp"]',
    '[class*="spotPrice"]',
    '[class*="spot-price"]',
    '[class*="underlyingPrice"]',
    '[class*="ocSpot"]',
    '[class*="currentPrice"]',
    '[class*="livePrice"]',
    '[class*="HeaderLtp"]',
    '[class*="indexPrice"]',
  ];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const raw = el.textContent.replace(/[^\d.]/g, '');
      const v = parseFloat(raw);
      // Sanity: price must be > 1 and < 1000000
      if (v > 1 && v < 1000000 && /^\d{1,7}\.?\d{0,2}$/.test(raw)) {
        // If it's in a table, only pick it if it has highlight class
        if (el.closest('table') && !el.closest('[class*="green"], [class*="active"], [class*="spot"]')) continue;
        return v;
      }
    }
  }
  return 0;
}

// ── Infer underlying from max OI strike ─────────────────────────────────────
function inferUnderlyingFromStrikes(strikes) {
  if (!Array.isArray(strikes) || !strikes.length) return 0;

  // 1. Try to find the visual ATM marker among elements on the page first
  const active = document.querySelector('[class*="bg-green"], [class*="activeStrike"], [class*="spot-box"]');
  if (active) {
    const v = toNum(active.textContent);
    if (v > 1) return v;
  }

  const byStrike = new Map();
  for (const r of strikes) {
    const s = +r?.strike;
    if (!Number.isFinite(s) || s <= 0) continue;
    byStrike.set(s, (byStrike.get(s) || 0) + (r?.oi || 0));
  }
  if (!byStrike.size) return 0;

  // 2. Return median strike as last resort fallback
  const sorted = [...byStrike.keys()].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)] || 0;
}

// ── Trigger Dhan data fetch ──────────────────────────────────────────────────
function triggerDhanBridgeFetch() {
  if (!dhanBridgeReady) {
    diag('Dhan bridge not ready — re-injecting', false, true);
    injectDhanBridge();
  }

  // Always run DOM scrape as immediate fallback
  const domData = scrapeDhan();
  if (domData?.strikes?.length > 0) {
    diag(`Dhan DOM: ${domData.symbol} | ${domData.strikes.length} rows`, true, true);
    lastGoodData = domData;
    processSnapshot(domData, 'dhan');
  } else {
    diag('Dhan DOM empty — bridge will fire on next API call', false, true);
  }

  // Click active expiry tab to force page to re-fetch from its API (bridge will intercept)
  const activeExpiry = document.querySelector(
    '[class*="expiry"] .active, [class*="expiry"] [aria-selected="true"], ' +
    'button[class*="expiry"].active, [class*="expiryTab"].active'
  );
  if (activeExpiry) {
    try { activeExpiry.click(); } catch (_) { }
  }
}

// ── MutationObserver for symbol changes ─────────────────────────────────────
let dhanObserver = null;
let _symbolChangeTimer = null;
function installDhanMutationObserver() {
  if (dhanObserver) return;
  let lastSym = detectDhanSymbol();
  dhanObserver = new MutationObserver(() => {
    // Debounce 1500ms — avoid mid-render misreads
    if (_symbolChangeTimer) clearTimeout(_symbolChangeTimer);
    _symbolChangeTimer = setTimeout(() => {
      _symbolChangeTimer = null;
      const sym = detectDhanSymbol();
      if (sym && sym !== lastSym && sym !== 'UNKNOWN') {
        diag(`Dhan symbol ${lastSym}→${sym}: resetting snapshot cache`, true, true);
        lastSym = sym;
        // Flush cross-symbol state
        prevStrikeMap = {};
        prevAtmLtp = {};
        lastSnapshotSymbol = sym;
        // Mark next scan as baseline so server skips delta detection
        _nextIsBaseline = true;
        setTimeout(() => triggerDhanBridgeFetch(), 500);
      }
    }, 1500);
  });
  dhanObserver.observe(document.body, { childList: true, subtree: true });
}

// ══════════════════════════════════════════════════════════════════════════════
// DHAN DOM SCRAPER — column-map based, correct OI vs OI-change
//
// Dhan's advanceoptionchain 9-column layout:
//   [CE Chg | CE Vol | CE OI | CE LTP | Strike | PE LTP | PE OI | PE Vol | PE Chg]
//      0        1       2       3         4        5        6       7        8
// OI is always positive. OI Change (Chg) can be negative. Never use col 0 or 8.
// ══════════════════════════════════════════════════════════════════════════════

const COL_PATTERNS = {
  ce_oi:  [/\bce\s*oi\b/i, /\bcall\s*oi\b/i, /\bcall.*open.*int/i],
  ce_ltp: [/\bce\s*ltp\b/i, /\bcall\s*ltp\b/i, /\bce\s*price\b/i],
  ce_chg: [/\bce.*chg\b/i, /\bcall.*chg\b/i, /\bce.*oi.*ch/i],
  ce_vol: [/\bce.*vol\b/i, /\bcall.*vol\b/i],
  ce_iv:  [/\bce\s*iv\b/i, /\bcall\s*iv\b/i, /\bce.*impl/i],
  strike: [/^strike$/i, /^sp$/i, /^strike.*price$/i, /^strike.*val/i, /^\d+\.?\d*$/i],
  pe_oi:  [/\bpe\s*oi\b/i, /\bput\s*oi\b/i, /\bput.*open.*int/i],
  pe_ltp: [/\bpe\s*ltp\b/i, /\bput\s*ltp\b/i, /\bpe\s*price\b/i, /\bpe\s*last\b/i],
  pe_chg: [/\bpe.*chg\b/i, /\bput.*chg\b/i, /\bpe.*oi.*ch/i],
  pe_vol: [/\bpe.*vol\b/i, /\bput.*vol\b/i],
  pe_iv:  [/\bpe\s*iv\b/i, /\bput\s*iv\b/i, /\bpe.*impl/i],
  // Generic — resolved by position relative to strike
  gen_oi:  [/^oi$/i, /^open\s*interest$/i],
  gen_ltp: [/^ltp$/i, /^price$/i, /^last\s*price$/i, /^last\s*traded\s*price$/i],
  gen_vol: [/^vol$/i, /^volume$/i, /^qty$/i],
  gen_chg: [/^chg$/i, /^change$/i, /%.*chg/i, /\bltp\s*%?/i],
  gen_iv:  [/^\biv\b$/i, /^impl.*vol/i],
};

function matchCol(text) {
  const t = text.trim();
  for (const [k, patterns] of Object.entries(COL_PATTERNS)) {
    if (patterns.some(p => p.test(t))) return k;
  }
  return null;
}

function buildHeaderColMap(thEls) {
  const map = {};
  let strikeIdx = -1;

  // First pass: find strike price column index & clear-cut CE/PE columns
  thEls.forEach((el, i) => {
    const type = matchCol(el.textContent.trim());
    if (type === 'strike') strikeIdx = i;
    if (type && !type.startsWith('gen_') && !(type in map)) map[type] = i;
  });

  // Second pass: resolve generic labels (gen_oi, gen_ltp, gen_vol, gen_iv) by position
  thEls.forEach((el, i) => {
    const type = matchCol(el.textContent.trim());
    if (!type || !type.startsWith('gen_') || strikeIdx === -1) return;

    const base = type.replace('gen_', ''); // 'oi', 'ltp', 'vol', 'iv', etc.
    const side = i < strikeIdx ? 'ce' : 'pe';
    const key = `${side}_${base}`;
    if (!(key in map)) map[key] = i;
  });

  if (strikeIdx !== -1) map.strike = strikeIdx;
  return map;
}

// Positional fallback — Dhan standard layouts
const POSITIONAL = {
  16: { ce_oi: 1, ce_ltp: 5, strike: 7, pe_ltp: 10, pe_oi: 14 }, // Dhan Advanced (Checkbox inclusive)
  14: { ce_oi: 1, ce_ltp: 5, strike: 6, pe_ltp: 8, pe_oi: 12 },  // Dhan Advanced (Data only)
  9: { ce_chg: 0, ce_vol: 1, ce_oi: 2, ce_ltp: 3, strike: 4, pe_ltp: 5, pe_oi: 6, pe_vol: 7, pe_chg: 8 },
  8: { ce_vol: 0, ce_oi: 1, ce_ltp: 2, ce_chg: 3, strike: 4, pe_chg: 5, pe_ltp: 6, pe_oi: 7 },
  7: { ce_oi: 0, ce_ltp: 1, ce_chg: 2, strike: 3, pe_chg: 4, pe_ltp: 5, pe_oi: 6 },
};

function scrapeTable(table) {
  const thEls = Array.from(table.querySelectorAll('thead th, thead td'));
  let colMap = buildHeaderColMap(thEls);
  const numCols = thEls.length;
  const site = detectSite();

  if (site === 'dhan') {
    if (numCols === 16) colMap = POSITIONAL[16];
    else if (numCols === 14) colMap = POSITIONAL[14];
  }

  if (!Object.keys(colMap).length && (!('strike' in colMap) || (!('ce_oi' in colMap) && !('pe_oi' in colMap)))) {
    colMap = POSITIONAL[numCols] || {};
    if (numCols >= 3 && !Object.keys(colMap).length) {
      // Ultra-generic: strike = middle, CE OI = 2nd from left, PE OI = 2nd from right
      const mid = Math.floor(numCols / 2);
      const samples = Array.from(table.querySelectorAll('tbody tr')).slice(0, 4);
      // Find first left col with all non-negative values (OI, not change)
      let ceOiIdx = 1, peOiIdx = numCols - 2;
      for (let c = 0; c < mid; c++) {
        const vals = samples.map(r => toInt(cellText(r.querySelectorAll('td')[c]) || '0'));
        if (vals.every(v => v >= 0) && vals.some(v => v > 0)) { ceOiIdx = c; break; }
      }
      for (let c = numCols - 1; c > mid; c--) {
        const vals = samples.map(r => toInt(cellText(r.querySelectorAll('td')[c]) || '0'));
        if (vals.every(v => v >= 0) && vals.some(v => v > 0)) { peOiIdx = c; break; }
      }
      colMap = { strike: mid, ce_oi: ceOiIdx, pe_oi: peOiIdx };
    }
  }

  if (!('strike' in colMap)) return [];

  const rows = Array.from(table.querySelectorAll('tbody tr'));
  const strikes = [];
  rows.forEach(row => {
    const cells = Array.from(row.querySelectorAll('td'));
    if (cells.length < 3) return;

    // Skip "Floating" Spot Row: if row has highlight/spot classes, skip parsing as strike
    if (row.querySelector('[class*="bg-green"], [class*="spot"], [class*="active"]')) {
      // Small optimization: if we find it here, it confirms underlying detection
      return;
    }

    const rawStrike = toNum(cells[colMap.strike]?.textContent || '0');
    // Strike price must be a whole number (or integer-valued float) ≥ 1
    // Reject decimals like 0.01, 0.02 (LTP), 0.48 (PCR), etc.
    const strike = Number.isFinite(rawStrike) && rawStrike >= 1 && Math.abs(rawStrike - Math.round(rawStrike)) < 0.5 ? Math.round(rawStrike) : 0;
    if (!strike || strike <= 0) return;

    // OI must be non-negative — reject negative values (those are OI change)
    // Use cellText() to strip Dhan's orange OI rank badge spans (e.g. "OI 3") from cell text
    const rawCeOi = 'ce_oi' in colMap ? toInt(cellText(cells[colMap.ce_oi]) || '0') : 0;
    const rawPeOi = 'pe_oi' in colMap ? toInt(cellText(cells[colMap.pe_oi]) || '0') : 0;
    const ce_oi  = rawCeOi < 0 ? 0 : rawCeOi;
    const pe_oi  = rawPeOi < 0 ? 0 : rawPeOi;
    const ce_ltp = 'ce_ltp' in colMap ? toNum(cells[colMap.ce_ltp]?.textContent || '0') : 0;
    const pe_ltp = 'pe_ltp' in colMap ? toNum(cells[colMap.pe_ltp]?.textContent || '0') : 0;
    const ce_vol = 'ce_vol' in colMap ? toInt(cells[colMap.ce_vol]?.textContent || '0') : 0;
    const pe_vol = 'pe_vol' in colMap ? toInt(cells[colMap.pe_vol]?.textContent || '0') : 0;
    // IV: some layouts show it as %; strip the % sign
    const rawCeIv = 'ce_iv' in colMap ? (cells[colMap.ce_iv]?.textContent || '').replace('%', '') : '';
    const rawPeIv = 'pe_iv' in colMap ? (cells[colMap.pe_iv]?.textContent || '').replace('%', '') : '';
    const ce_iv = rawCeIv ? toNum(rawCeIv) : 0;
    const pe_iv = rawPeIv ? toNum(rawPeIv) : 0;

    if (ce_oi > 0 || pe_oi > 0 || ce_ltp > 0 || pe_ltp > 0) {
      strikes.push({ strike, option_type: 'CE', oi: ce_oi, ltp: ce_ltp, iv: ce_iv, volume: ce_vol });
      strikes.push({ strike, option_type: 'PE', oi: pe_oi, ltp: pe_ltp, iv: pe_iv, volume: pe_vol });
    }
  });
  return strikes;
}

function scrapeDhan() {
  const symbol = detectDhanSymbol();
  const underlying = detectDhanUnderlying();
  const expiry = detectActiveExpiry();

  // Try all tables — pick the one with most valid rows
  let best = [];
  for (const tbl of document.querySelectorAll('table')) {
    const s = scrapeTable(tbl);
    if (s.length > best.length) best = s;
  }

  if (!best.length) {
    // Class-based strike cell fallback
    document.querySelectorAll('[class*="strikePrice"],[class*="strikeprice"],[class*="StrikePrice"]').forEach(sc => {
      const row = sc.closest('tr,[role="row"],[class*="row"]') || sc.parentElement;
      if (!row) return;
      const strike = toNum(sc.textContent);
      if (!strike || strike <= 0) return;
      const cells = Array.from(row.querySelectorAll('td,[class*="cell"]'));
      const sciIdx = cells.indexOf(sc.closest('td,[class*="cell"]') || sc);
      const left = sciIdx >= 0 ? cells.slice(0, sciIdx) : cells.slice(0, Math.floor(cells.length / 2));
      const right = sciIdx >= 0 ? cells.slice(sciIdx + 1) : cells.slice(Math.floor(cells.length / 2) + 1);
      const posMax = arr => arr.map(c => toInt(cellText(c))).filter(v => v > 0).reduce((mx, v) => Math.max(mx, v), 0);
      const ce_oi = posMax(left);
      const pe_oi = posMax(right);
      if (ce_oi > 0 || pe_oi > 0) {
        best.push({ strike, option_type: 'CE', oi: ce_oi, ltp: 0, iv: 0 });
        best.push({ strike, option_type: 'PE', oi: pe_oi, ltp: 0, iv: 0 });
      }
    });
  }

  if (!best.length) return { symbol, strikes: [], underlying, expiry, site: 'dhan' };

  let ceOi = 0, peOi = 0;
  best.forEach(r => { if (r.option_type === 'CE') ceOi += r.oi; else peOi += r.oi; });

  return {
    symbol,
    strikes: best,
    underlying: firstPositive(underlying, inferUnderlyingFromStrikes(best)),
    expiry,
    summary: {
      source: 'dhan_dom',
      ceOi,
      peOi,
      pcr: firstFinite(toNum(getMetricValue("PCR")), ceOi > 0 ? peOi / ceOi : null),
      maxPain: firstFinite(toInt(getMetricValue("Max Pain")), null),
    },
    site: 'dhan',
  };
}

// ══════════════════════════════════════════════════════════════════════════════
// NSE bridge
// ══════════════════════════════════════════════════════════════════════════════
function installNSEPageBridge() {
  if (document.getElementById('nsebot-page-bridge')) return;
  const s = document.createElement('script');
  s.id = 'nsebot-page-bridge';
  s.src = chrome.runtime.getURL('page_bridge.js');
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
}

function handleNSEBridgeMessage(event) {
  if (event.source !== window) return;
  const d = event.data;
  if (!d || d.source !== NSE_BRIDGE_SRC) return;
  if (d.type === 'NSEBOT_BRIDGE_READY') { bridgeReady = true; diag('NSE bridge ready', true); return; }
  if (d.type === 'NSEBOT_NSE_DIAG') { diag(d.payload?.msg, !!d.payload?.ok, !!d.payload?.warn); return; }
  if (d.type === 'NSEBOT_NSE_API_DATA') {
    const p = parseNSEApiResponse(d.payload?.json, d.payload?.url || window.location.href);
    if (p?.strikes?.length > 0) { lastGoodData = p; processSnapshot(p, 'nse'); }
  }
}

function triggerNSEApiFetch() {
  let symbol = 'NIFTY';
  const m = window.location.href.match(/[?&]symbol=([A-Z0-9]+)/i);
  if (m) symbol = m[1].toUpperCase();
  if (!bridgeReady) installNSEPageBridge();
  window.postMessage({ source: EXT_SRC, type: 'NSEBOT_FORCE_NSE_FETCH', symbol }, '*');
}

function parseNSEApiResponse(json, url) {
  try {
    const records = json?.records || {};
    const filtered = json?.filtered || {};
    const underlying = parseFloat(records.underlyingValue || 0);
    const urlObj = new URL(url, window.location.href);
    const symbol = (urlObj.searchParams.get('symbol') || '').toUpperCase();
    const expDates = records.expiryDates || [];
    const today = new Date(); today.setHours(0, 0, 0, 0);
    let expiry = '';
    for (const e of expDates) { if (new Date(e) >= today) { expiry = e; break; } }
    if (!expiry && expDates.length) expiry = expDates[0];
    const rawStrikes = filtered.data?.length > 0 ? filtered.data : records.data || [];
    const strikes = [];
    rawStrikes.forEach(item => {
      const strike = parseFloat(item.strikePrice || 0);
      if (!strike) return;
      if (expiry && item.expiryDate && item.expiryDate !== expiry) return;
      ['CE', 'PE'].forEach(ot => {
        const opt = item[ot];
        if (!opt) return;
        strikes.push({
          strike, option_type: ot,
          ltp: parseFloat(opt.lastPrice || 0), oi: parseInt(opt.openInterest || 0),
          oi_change: parseInt(opt.changeinOpenInterest || 0),
          volume: parseInt(opt.totalTradedVolume || 0),
          iv: parseFloat(opt.impliedVolatility || 0),
        });
      });
    });
    return { symbol, underlying, expiry, strikes, site: 'nse' };
  } catch (e) { diag(`parseNSEApiResponse: ${e.message}`, false); return null; }
}

// ══════════════════════════════════════════════════════════════════════════════
// Other DOM scrapers
// ══════════════════════════════════════════════════════════════════════════════
const DOM_RETRY_MS = [2000, 3000, 5000, 8000, 15000];
function domScrapeWithRetry(site, attempt) {
  const data = site === 'sensibull' ? scrapeSensibull() : site === 'opstra' ? scrapeOpstra() : scrapeDhan();
  if (data?.strikes?.length > 0) { lastGoodData = data; processSnapshot(data, site); return; }
  if (attempt < DOM_RETRY_MS.length) setTimeout(() => domScrapeWithRetry(site, attempt + 1), DOM_RETRY_MS[attempt]);
  else diag(`DOM gave up after ${DOM_RETRY_MS.length + 1} attempts for ${site}`, false);
}

function scrapeSensibull() {
  const data = { symbol: '', strikes: [], underlying: 0, expiry: '' };
  document.querySelectorAll('table tbody tr').forEach(row => {
    const cells = Array.from(row.querySelectorAll('td'));
    if (cells.length < 3) return;
    const mid = Math.floor(cells.length / 2);
    const strike = parseFloat(cells[mid]?.textContent?.replace(/[^0-9.]/g, ''));
    if (!strike || isNaN(strike)) return;
    data.strikes.push({ strike, option_type: 'CE', oi: toInt(cells[0]?.textContent) || 0, ltp: 0, iv: 0 });
    data.strikes.push({ strike, option_type: 'PE', oi: toInt(cells[cells.length - 1]?.textContent) || 0, ltp: 0, iv: 0 });
  });
  return data;
}

function scrapeOpstra() {
  for (const src of [window.__APP_STATE__, window.__INITIAL_STATE__, window.__NEXT_DATA__?.props?.pageProps]) {
    if (!src) continue;
    const oc = src.optionChain || src.oc;
    if (oc?.data && Array.isArray(oc.data)) {
      const strikes = [];
      oc.data.forEach(r => ['CE', 'PE'].forEach(ot => {
        if (r[ot]) strikes.push({ strike: r.strike || 0, option_type: ot, oi: r[ot].oi || 0, ltp: r[ot].ltp || 0, iv: 0 });
      }));
      if (strikes.length) return { symbol: oc.symbol || '', strikes, underlying: oc.underlyingPrice || 0, expiry: oc.expiry || '', site: 'opstra' };
    }
  }
  return scrapeSensibull();
}

// ══════════════════════════════════════════════════════════════════════════════
// ATM filter: ±15 strikes
// ══════════════════════════════════════════════════════════════════════════════
function filterAtmWindow(strikes, underlying) {
  if (!strikes?.length) return [];
  const uniq = [...new Set(strikes.map(r => +r.strike).filter(v => v > 0))].sort((a, b) => a - b);
  if (!uniq.length) return strikes;
  let atmIdx = Math.floor(uniq.length / 2);
  if (underlying > 0) {
    let best = Infinity;
    uniq.forEach((s, i) => { const d = Math.abs(s - underlying); if (d < best) { best = d; atmIdx = i; } });
  }
  const windowSize = Number.isFinite(Number(CFG.strikeRange))
    ? Number(CFG.strikeRange)
    : DEFAULT_ATM_STRIKE_WINDOW;
  const lo = Math.max(0, atmIdx - windowSize);
  const hi = Math.min(uniq.length - 1, atmIdx + windowSize);
  const keep = new Set(uniq.slice(lo, hi + 1));
  return strikes.filter(r => keep.has(+r.strike));
}

// ══════════════════════════════════════════════════════════════════════════════
// Max Pain
// ══════════════════════════════════════════════════════════════════════════════
function computeMaxPain(strikes) {
  const ceM = {}, peM = {};
  (strikes || []).forEach(r => {
    if (r.option_type === 'CE') ceM[+r.strike] = r.oi || 0;
    else peM[+r.strike] = r.oi || 0;
  });
  const allS = [...new Set([...Object.keys(ceM), ...Object.keys(peM)].map(Number))].sort((a, b) => a - b);
  if (!allS.length) return null;
  let minPain = Infinity, mp = null;
  allS.forEach(c => {
    let p = 0;
    allS.forEach(s => { if (c > s) p += (c - s) * (ceM[s] || 0); if (c < s) p += (s - c) * (peM[s] || 0); });
    if (p < minPain) { minPain = p; mp = c; }
  });
  return mp;
}

// ══════════════════════════════════════════════════════════════════════════════
// Snapshot processor
// ══════════════════════════════════════════════════════════════════════════════
function processSnapshot(data, site) {
  if (!data?.strikes?.length) return;

  const symbol = data.symbol || site.toUpperCase();
  const underlying = firstPositive(data.underlying, inferUnderlyingFromStrikes(data.strikes));
  const allStrikes = data.strikes;
  const filtered = filterAtmWindow(allStrikes, underlying);
  
  if (!filtered.length) {
    diag(`ATM filter: 0 rows for ${symbol}`, false, true);
    return;
  }

  // Handle symbol switch
  if (lastSnapshotSymbol && symbol && lastSnapshotSymbol !== symbol) {
    prevStrikeMap = {};
    prevAtmLtp = {};
    diag(`Symbol switch ${lastSnapshotSymbol}→${symbol}: reset`, true, true);
  }

  const now = nowIST();
  const { anomalies, nextMap, maxOiPct, maxAtmLtpPct } = analyzeAnomalies(filtered, underlying, symbol, now);
  
  prevStrikeMap = nextMap;
  lastSnapshotSymbol = symbol;

  const summary = getScanSummary(allStrikes, filtered, data.summary);
  const pcrDelta = calculatePcrDelta(summary.pcr, filtered);

  // 1. Persist locally
  persistSnapshot(symbol, underlying, data.expiry, filtered, summary, site, now);

  // 2. Dispatch Alerts
  if (anomalies.length) {
    persistAlerts(anomalies);
    anomalies.forEach(a => {
      if (CFG.notifications) showNotification(a);
      if (CFG.forwardBackend) postToBackend('/ingest', a);
    });
  }

  // 3. Log diagnostics
  reportDiagnostics(symbol, allStrikes.length, filtered.length, underlying, summary, anomalies, maxOiPct, maxAtmLtpPct, pcrDelta);

  // 4. Forward full snapshot to backend
  if (CFG.forwardBackend && filtered.length >= 5 && Number.isFinite(underlying)) {
    forwardSnapshot(data, filtered, symbol, summary);
  } else if (CFG.forwardBackend) {
    diag(`${symbol}: validation failed (${filtered.length} strikes, underlying=${underlying}) — skip POST`, false, true);
  }
}

function analyzeAnomalies(filtered, underlying, symbol, now) {
  const nextMap = {};
  const anomalies = [];
  let maxOiPct = 0;
  let maxAtmLtpPct = 0;

  const sortedStrikes = [...new Set(filtered.map(s => +s.strike))].sort((a, b) => a - b);
  let interval = 100;
  if (sortedStrikes.length > 1) {
    const diffs = [];
    for (let i = 1; i < sortedStrikes.length; i++) {
      const d = sortedStrikes[i] - sortedStrikes[i-1];
      if (d > 0) diffs.push(d);
    }
    interval = diffs.length ? Math.min(...diffs) : 100;
  }
  const itmLimit = 5 * interval;

  // OI Spikes
  filtered.forEach(({ strike, option_type, oi, ltp }) => {
    const key = `${option_type}_${strike}`;
    const p = prevStrikeMap[key];
    nextMap[key] = { oi, ltp };
    if (!p?.oi) return;

    const isDeepITM = (option_type === 'CE' && strike < underlying - itmLimit) ||
                      (option_type === 'PE' && strike > underlying + itmLimit);
    if (isDeepITM) return;

    const pct = ((oi - p.oi) / Math.abs(p.oi)) * 100;
    maxOiPct = Math.max(maxOiPct, Math.abs(pct));
    if (Math.abs(pct) >= CFG.oiThreshold) {
      anomalies.push({
        symbol, alert_type: pct > 0 ? 'OI_SPIKE' : 'OI_UNWIND',
        strike, option_type, prev_oi: p.oi, curr_oi: oi,
        pct_change: pct.toFixed(1), underlying, fired_at: now
      });
    }
  });

  // ATM LTP Spikes
  const atmStrike = sortedStrikes.reduce((best, s) => Math.abs(s - underlying) < Math.abs(best - underlying) ? s : best, sortedStrikes[0] || 0);
  ['CE', 'PE'].forEach(ot => {
    const atmRow = filtered.find(r => +r.strike === atmStrike && r.option_type === ot);
    if (!atmRow) return;
    const currLtp = atmRow.ltp || 0, prevLtp = prevAtmLtp[ot] || 0;
    if (prevLtp > 0 && currLtp > 0) {
      const ltpPct = ((currLtp - prevLtp) / prevLtp) * 100;
      maxAtmLtpPct = Math.max(maxAtmLtpPct, Math.abs(ltpPct));
      if (Math.abs(ltpPct) >= (CFG.ltpSpikeThreshPct || 5)) {
        anomalies.push({
          symbol, alert_type: 'LTP_SPIKE', strike: atmStrike, option_type: ot,
          prev_ltp: prevLtp, curr_ltp: currLtp, pct_change: ltpPct.toFixed(1),
          underlying, fired_at: now
        });
      }
    }
    if (atmRow.ltp) prevAtmLtp[ot] = atmRow.ltp;
  });

  return { anomalies, nextMap, maxOiPct, maxAtmLtpPct };
}

function getScanSummary(allStrikes, filtered, apiSummary = {}) {
  let ceOiTotal = 0, peOiTotal = 0;
  allStrikes.forEach(r => { if (r.option_type === 'CE') ceOiTotal += r.oi || 0; else peOiTotal += r.oi || 0; });
  return {
    source: apiSummary.source || 'computed',
    ceOi: firstFinite(apiSummary.ceOi, ceOiTotal),
    peOi: firstFinite(apiSummary.peOi, peOiTotal),
    pcr: firstFinite(apiSummary.pcr, ceOiTotal > 0 ? peOiTotal / ceOiTotal : null),
    maxPain: firstFinite(apiSummary.maxPain, computeMaxPain(allStrikes)),
  };
}

function calculatePcrDelta(currPcr, filtered) {
  let prevCeOi = 0, prevPeOi = 0;
  filtered.forEach(r => {
    const p = prevStrikeMap[`${r.option_type}_${r.strike}`];
    if (p?.oi) { if (r.option_type === 'CE') prevCeOi += p.oi; else prevPeOi += p.oi; }
  });
  const prevPcr = prevCeOi > 0 ? prevPeOi / prevCeOi : null;
  return (Number.isFinite(prevPcr) && Number.isFinite(currPcr)) ? currPcr - prevPcr : null;
}

function persistSnapshot(symbol, underlying, expiry, filtered, summary, site, now) {
  const snap = {
    symbol, underlying, expiry: expiry || '',
    strikes: filtered, filteredStrikes: filtered,
    summary, site, lastScan: now, scanOk: true
  };
  safeStorageGet([STORAGE.SCAN_COUNT], r => {
    safeStorageSet({
      [STORAGE.SNAPSHOT]: snap,
      [STORAGE.LAST_SCAN_TS]: now,
      [STORAGE.SCAN_COUNT]: (r[STORAGE.SCAN_COUNT] || 0) + 1,
      [STORAGE.SITE]: site,
    });
  });
}

function persistAlerts(anomalies) {
  safeStorageGet([STORAGE.ALERTS], r => {
    safeStorageSet({ [STORAGE.ALERTS]: [...(r[STORAGE.ALERTS] || []), ...anomalies].slice(-MAX_ALERTS) });
  });
}

function reportDiagnostics(symbol, totalRows, filteredRows, underlying, summary, anomalies, maxOiPct, maxAtmLtpPct, pcrDelta) {
  const pcrNote = Number.isFinite(summary.pcr) ? ` pcr:${summary.pcr.toFixed(2)}` : '';
  const mpNote = Number.isFinite(summary.maxPain) ? ` mp:${summary.maxPain}` : '';
  const range = CFG.strikeRange || 15;
  
  diag(`${symbol} ✓ ${totalRows} rows (${filteredRows} ATM±${range}) spot:${underlying}${pcrNote}${mpNote}${anomalies.length ? ` ⚠${anomalies.length}` : ''}`, true, anomalies.length > 0);

  if (!anomalies.length && Object.keys(prevStrikeMap).length) {
    const pcrPart = pcrDelta == null ? 'PCR delta n/a' : `PCR delta ${Math.abs(pcrDelta).toFixed(3)} < 0.25`;
    diag(`No alert: max OI delta ${maxOiPct.toFixed(2)}% < ${CFG.oiThreshold}, ATM LTP delta ${maxAtmLtpPct.toFixed(2)}% < ${CFG.ltpSpikeThreshPct}, ${pcrPart}`, true, true);
  }
}

function forwardSnapshot(data, filtered, symbol, summary) {
  const isBase = _nextIsBaseline;
  const isForce = _pendingForceScan;
  _nextIsBaseline = false;
  _pendingForceScan = false;
  if (isBase) diag('Baseline scan: backend will persist only, no alert detection', true, true);

  safeStorageGet(['nsebot_chart_data'], r => {
    const allChartData = r.nsebot_chart_data || {};
    const chartData = findChartDataForSymbol(allChartData, symbol);

    postToBackend('/ingest/snapshot', {
      ...data,
      strikes: filtered,
      symbol,
      force: isForce,
      is_baseline: isBase,
      chart_indicators: chartData,
      oi_threshold: CFG.oiThreshold,
      ltp_threshold: CFG.ltpSpikeThreshPct
    });
  });
}

// ── Notify / Backend ──────────────────────────────────────────────────────────
function showNotification(a) {
  const emoji = a.alert_type === 'OI_SPIKE' ? '📈' : a.alert_type === 'OI_UNWIND' ? '📉' : a.alert_type === 'LTP_SPIKE' ? '⚡' : '🔔';
  // Compact one-line body for Windows toast
  let body = `${a.option_type} ${a.strike} | `;
  if (a.alert_type === 'LTP_SPIKE') {
    body += `LTP: ${a.curr_ltp?.toFixed(1)} (${a.pct_change}%)`;
  } else {
    body += `OI: ${fmtNum(a.curr_oi)} (${a.pct_change}%)`;
  }
  safeSendMessage({
    type: 'SHOW_NOTIFICATION',
    title: `${emoji} ${a.symbol}: ${a.alert_type}`,
    body
  });
}
function postToBackend(path, body) {
  safeSendMessage({
    type: 'FETCH_BACKEND', urls: candidateUrls(path),
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, resp => safeStorageSet({ [STORAGE.BACKEND_OK]: !!resp?.ok }));
}
let logQueue = [];
let logFlushTimer = null;

function addScanLog(entry) {
  logQueue.push(entry);
  if (logFlushTimer) clearTimeout(logFlushTimer);
  logFlushTimer = setTimeout(flushLogs, 150);
}

function flushLogs() {
  const batch = [...logQueue];
  logQueue = [];
  safeStorageGet([STORAGE.SCAN_LOG], r => {
    const existing = r[STORAGE.SCAN_LOG] || [];
    const merged = [...existing, ...batch].slice(-MAX_LOG);
    safeStorageSet({ [STORAGE.SCAN_LOG]: merged });
  });
}
function fmtNum(n) {
  if (!n) return '0';
  if (n >= 1e7) return (n / 1e7).toFixed(1) + 'Cr';
  if (n >= 1e5) return (n / 1e5).toFixed(1) + 'L';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadSettings(() => {
  const site = detectSite();
  if (!site) { diag('Not a supported page', false); return; }

  safeStorageSet({ [STORAGE.SITE]: site });
  diag(`NSEBOT v2.6.0 active: ${site}`, true);

  if (site === 'nse') {
    window.addEventListener('message', handleNSEBridgeMessage);
    installNSEPageBridge();
    setTimeout(() => {
      if (!lastGoodData) {
        const d = scrapeSensibull(); // NSE DOM fallback
        if (d?.strikes?.length > 0) { processSnapshot(d, 'nse'); lastGoodData = d; }
      }
    }, 45000);

  } else if (site === 'dhan') {
    // Register listener BEFORE injecting bridge (avoid race)
    window.addEventListener('message', handleDhanBridgeMessage);
    injectDhanBridge();
    installDhanMutationObserver();
    diag('Dhan: bridge injected, observer active', true);
    // DOM fallback after SPA renders (~3s)
    setTimeout(() => { if (!lastGoodData) triggerDhanBridgeFetch(); }, 3000);

  } else {
    domScrapeWithRetry(site, 0);
  }

  scheduleScans();

  // ── Visibility watchdog: re-arm scan if tab was hidden/re-shown ──────────
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (!scanIntervalId) {
        diag('Tab visible: re-scheduling scans (interval was dead)', true, true);
        scheduleScans();
      }
      // Immediately re-scan if overdue
      safeStorageGet(['nsebot_last_scan_ts', 'nsebot_settings'], r => {
        const last = r['nsebot_last_scan_ts'];
        const iMin = r['nsebot_settings']?.intervalMin || CFG.intervalMin || 5;
        const overdueSec = iMin * 60 + 30;
        if (!last || (Date.now() - new Date(last).getTime()) / 1000 > overdueSec) {
          diag('Overdue scan detected on tab focus, triggering now', true, true);
          runPeriodicScan();
        }
      });
    }
  });
});
