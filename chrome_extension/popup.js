/**
 * popup.js — NSEBOT v2.6.0
 * Changes: removed Underlying metric, IST timestamps, no expiry in alerts,
 *          ATM LTP_SPIKE alert type, simplified UI.
 */
'use strict';

const SK = {
  ALERTS: 'nsebot_alerts', SNAPSHOT: 'nsebot_snapshot',
  SITE: 'nsebot_site', SCAN_LOG: 'nsebot_scan_log',
  LAST_TS: 'nsebot_last_scan_ts', COUNT: 'nsebot_scan_count',
  SETTINGS: 'nsebot_settings', BE_OK: 'nsebot_backend_ok',
  BE_RUN: 'nsebot_backend_running',
  CHART_DATA: 'nsebot_chart_data',
  LAST_CHART_SYM: 'last_chart_symbol',
  CHART_DEBUG: 'nsebot_chart_debug',
};
const DFLT = { oiThreshold: 25, ltpSpikeThreshPct: 5, intervalMin: 5, strikeRange: 15, notifications: true, forwardBackend: true, backendUrl: 'http://localhost:8765' };
const EMOJIS = {
  OI_SPIKE: '📈', OI_UNWIND: '📉', LTP_SPIKE: '⚡',
  PCR_EXTREME: '🔴', PCR_SHIFT: '🔄', PCR_VELOCITY: '📊',
  IV_SPIKE: '🌋', IV_CRUSH: '❄️',
  BUILDUP_CLASSIFY: '🏗️', OI_WALL_SHIFT: '🧱',
  VOLUME_AGGRESSION: '⚡', STRADDLE_PREMIUM: '🎯',
  ATM_LEG_MOVE: '🦵', OTM_UNUSUAL: '🔭',
  PRICE_SPIKE: '💥', MAX_PAIN_SHIFT: '😰',
  SCAN_SUMMARY: 'ℹ️',
};
const MAX_ALERTS = 50, MAX_LOG = 30, TICK_MS = 1000, HEALTH_EVERY = 15;

let S = {
  site: null,
  snap: null,
  alerts: [],
  log: [],
  lastTs: null,
  count: 0,
  settings: { ...DFLT },
  beOk: false,
  iSec: 300,
  backendAlerts: []
};
let activeTab = 'dashboard', rTimer = null, hCount = 0, pollTimer = null, beAlertTick = 0, stateTick = 0, stuckTick = 0;

// ── IST time formatter ────────────────────────────────────────────────────────
function fmtIST(d) {
  if (!d || isNaN(d)) return '';
  return d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
function fmtISTFull(d) {
  if (!d || isNaN(d)) return '';
  return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
function ago(ts) {
  const t = new Date(ts || 0).getTime();
  if (!t) return '—';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const el = id => document.getElementById(id);
const tog = (id, on) => { const e = el(id); if (e) e.classList[on ? 'add' : 'remove']('on'); };
function fmtNum(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e7) return (n / 1e7).toFixed(1) + 'Cr';
  if (a >= 1e5) return (n / 1e5).toFixed(1) + 'L';
  if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString('en-IN');
}
function normBase(u) { return (u || 'http://localhost:8765').trim().replace(/\/+$/, ''); }
function beUrls(url, path) {
  const b = normBase(url);
  const a = b.includes('localhost') ? [b, b.replace('localhost', '127.0.0.1')] : b.includes('127.0.0.1') ? [b, b.replace('127.0.0.1', 'localhost')] : [b];
  return [...new Set(a)].map(u => `${u}${path}`);
}
function resolveUnderlying(snap) {
  const v = Number(snap?.underlying);
  if (Number.isFinite(v) && v > 0) return v;
  return 0;
}

// ── Symbol Normalization ──────────────────────────────────────────────────────
function normSym(s) {
  if (!s) return '';
  return s.toUpperCase()
    .replace(/MCX:|NSE:|NFO:|CDS:/g, '') // Strip exchange prefixes
    .replace(/\s+/g, '')               // Strip whitespace
    .replace(/[!]/g, '')               // Strip TradingView continuous markers
    .replace(/(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?/gi, '') // Strip contract months
    .replace(/[^A-Z0-9]/g, '');         // Strip special chars
}

function findChartMatch(chartData, ...symbols) {
  if (!chartData) return { key: null, data: null };

  const keys = Object.keys(chartData);
  const candidates = symbols.filter(Boolean);

  for (const s of candidates) {
    if (chartData[s]) return { key: s, data: chartData[s] };
  }

  const targets = candidates.map(normSym).filter(Boolean);
  for (const target of targets) {
    const exactKey = keys.find(k => normSym(k) === target);
    if (exactKey) return { key: exactKey, data: chartData[exactKey] };
  }

  for (const target of targets) {
    const looseKey = keys.find(k => {
      const nk = normSym(k);
      if (!nk) return false;
      const longer = nk.length >= target.length ? nk : target;
      const shorter = nk.length < target.length ? nk : target;
      return longer.startsWith(shorter) && (longer.length - shorter.length <= 2 || /\d+$/.test(longer));
    });
    if (looseKey) return { key: looseKey, data: chartData[looseKey] };
  }

  return { key: null, data: null };
}

function pickChartRef(chartData, lastSym, snapSym) {
  const last = findChartMatch(chartData, lastSym);
  if (last.data) return last;

  const snap = findChartMatch(chartData, snapSym);
  if (snap.data) return snap;

  return { key: lastSym || null, data: null };
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  validateSite();
  setVersion();
  initTabs();
  initButtons();
  injectLaunchOverlay();
  loadSettingsUI();
  loadState(() => refreshUI());
  startLoop();
  checkBE();

  // Instant UI refresh on storage update
  chrome.storage.onChanged.addListener((changes) => {
    const relevant = Object.values(SK).some(k => changes[k]);
    if (relevant) {
      loadState(() => refreshUI());
    }
  });
});

function validateSite() {
  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    const url = tabs[0]?.url || '';
    const isOptionChain = url.includes('options-trader.dhan.co/advanceoptionchain');
    const isChart =
			url.includes('tv.dhan.co') ||
			url.includes('tradingview.com');
    if (!isOptionChain && !isChart) {
      if (el('blocker')) el('blocker').style.display = 'flex';
    }
  });
}

function setVersion() {
  try {
    const v = chrome.runtime.getManifest?.()?.version || '2.5';
    el('bv') && (el('bv').textContent = `v${v}`);
    el('fv') && (el('fv').textContent = `NSEBOT v${v} © 2025`);
  } catch (_) { }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
}
function switchTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
  if (name === 'oi-table') renderOI();
  if (name === 'alerts') { fetchBackendAlerts(); renderAlerts(); }
  if (name === 'log') renderLog();
  if (name === 'dashboard') renderDash();
}

// ── Buttons ───────────────────────────────────────────────────────────────────
function initButtons() {
  document.querySelectorAll('.toggle').forEach(t => t.addEventListener('click', () => t.classList.toggle('on')));
  el('btn-save').addEventListener('click', saveSettings);
  el('btn-test').addEventListener('click', testBE);
  el('btn-force').addEventListener('click', forceScan);
  el('btn-clear').addEventListener('click', clearAll);
  el('btn-start').addEventListener('click', startBE);
  el('btn-stop').addEventListener('click', stopBE);
}

// ── Launch overlay ────────────────────────────────────────────────────────────
function injectLaunchOverlay() {
  if (el('ov')) return;
  const div = document.createElement('div');
  div.id = 'ov';
  div.style.cssText = 'display:none;position:absolute;left:0;right:0;top:0;bottom:0;background:rgba(8,12,20,.97);z-index:100;padding:18px;flex-direction:column;gap:9px;';
  div.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:13px;font-weight:700;color:#00d4aa">🚀 Start Backend Bridge</span>
      <span id="ov-close" style="cursor:pointer;color:#4a6a8a;font-size:16px;padding:2px 8px">✕</span>
    </div>
    <div style="font-size:11px;color:#8fa8c8;line-height:1.6">Extensions cannot spawn processes.<br>Run once in a terminal — stays alive.</div>
    <div style="font-size:9px;color:#4a6a8a;text-transform:uppercase;letter-spacing:.4px">Step 1 — Open terminal in NSEBOT folder</div>
    <div id="ov-cmd" style="background:#060a12;border:1px solid #1a3050;border-radius:5px;padding:7px 10px;font-family:monospace;font-size:11px;color:#4fc3f7;cursor:pointer;position:relative">
      python src/extension_bridge.py
      <span id="ov-copy" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:9px;color:#2a4a6a">click to copy</span>
    </div>
    <div style="font-size:9px;color:#4a6a8a;text-transform:uppercase;letter-spacing:.4px">Step 2 — Bridge starts → click ▶ Start</div>
    <div id="ov-status" style="font-size:10px;color:#ffa726">Checking...</div>
    <button id="ov-retry" style="padding:6px;background:#003d30;color:#00d4aa;border:1px solid #00d4aa44;border-radius:4px;cursor:pointer;font-size:11px;font-weight:700">🔄 Check Now</button>
  `;
  document.body.style.position = 'relative';
  document.body.appendChild(div);
  el('ov-close').addEventListener('click', hideOv);
  el('ov-cmd').addEventListener('click', () => {
    navigator.clipboard.writeText('python src/extension_bridge.py').then(() => {
      const h = el('ov-copy');
      if (h) { h.textContent = '✓ copied!'; h.style.color = '#00d4aa'; setTimeout(() => { h.textContent = 'click to copy'; h.style.color = '#2a4a6a' }, 2000); }
    }).catch(() => { });
  });
  el('ov-retry').addEventListener('click', () => { const s = el('ov-status'); if (s) { s.textContent = 'Checking...'; s.style.color = '#ffa726'; } pollUntilReady(); });
}
function showOv() { const o = el('ov'); if (o) o.style.display = 'flex'; pollUntilReady(); }
function hideOv() { const o = el('ov'); if (o) o.style.display = 'none'; if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
function pollUntilReady() {
  if (pollTimer) clearInterval(pollTimer);
  let n = 0;
  pollTimer = setInterval(() => {
    n++;
    chrome.runtime.sendMessage({ type: 'CHECK_BACKEND', backendUrl: S.settings.backendUrl }, resp => {
      if (chrome.runtime.lastError) return;
      const st = el('ov-status');
      if (resp?.ok) {
        if (st) { st.textContent = '✅ Bridge running! Closing...'; st.style.color = '#00d4aa'; }
        clearInterval(pollTimer); pollTimer = null;
        chrome.storage.local.set({ [SK.BE_OK]: true, [SK.BE_RUN]: true }, () => { void chrome.runtime.lastError; });
        S.beOk = true; setTimeout(hideOv, 800); updateBE(); updateSvr();
      } else { if (st) { st.textContent = `Waiting... (${n})`; st.style.color = '#ffa726'; } }
    });
  }, 3000);
}

// ── Start / Stop backend ──────────────────────────────────────────────────────
function startBE() {
  const url = S.settings.backendUrl; el('btn-start').disabled = true;
  el('svr-dot').className = 'svr-dot starting'; el('svr-txt').textContent = 'Starting...';
  chrome.runtime.sendMessage({ type: 'START_BACKEND', backendUrl: url }, resp => {
    if (chrome.runtime.lastError) return;
    if (resp?.ok) {
      chrome.storage.local.set({ [SK.BE_OK]: true, [SK.BE_RUN]: true }, () => { void chrome.runtime.lastError; }); S.beOk = true; updateBE(); updateSvr(); el('btn-start').disabled = false;
    } else {
      el('btn-start').disabled = false; el('svr-dot').className = 'svr-dot stopped';
      el('svr-txt').textContent = 'Not running'; el('svr-hint').textContent = 'See instructions ↓'; showOv();
    }
  });
}
function stopBE() {
  el('btn-stop').disabled = true; el('svr-dot').className = 'svr-dot starting'; el('svr-txt').textContent = 'Stopping...';
  chrome.runtime.sendMessage({ type: 'STOP_BACKEND', backendUrl: S.settings.backendUrl }, () => {
    if (chrome.runtime.lastError) return;
    chrome.storage.local.set({ [SK.BE_RUN]: false, [SK.BE_OK]: false }, () => { void chrome.runtime.lastError; }); S.beOk = false; updateBE(); updateSvr(); el('btn-stop').disabled = false;
  });
}
function updateSvr() {
  const dot = el('svr-dot'); if (!dot || dot.className.includes('starting')) return;
  if (S.beOk) {
    dot.className = 'svr-dot running'; el('svr-txt').textContent = 'Running';
    el('svr-hint').textContent = normBase(S.settings.backendUrl); el('btn-start').disabled = true; el('btn-stop').disabled = false;
  } else {
    dot.className = 'svr-dot stopped'; el('svr-txt').textContent = 'Stopped';
    el('svr-hint').textContent = 'Click ▶ Start'; el('btn-start').disabled = false; el('btn-stop').disabled = true;
  }
}

// ── Refresh loop ──────────────────────────────────────────────────────────────
function startLoop() { if (rTimer) clearInterval(rTimer); rTimer = setInterval(tick, TICK_MS); tick(); }
function tick() {
  updateCD();
  if (++hCount >= HEALTH_EVERY) { hCount = 0; checkBE(); }
  if (++beAlertTick >= 10) { beAlertTick = 0; fetchBackendAlerts(); }
  // Reload state every 5s to pick up fresh lastTs written by content script
  if (++stateTick >= 5) { stateTick = 0; loadState(() => refreshUI()); }
}
function refreshUI() {
  updateStatus(); updateMetrics(); updateAlertBadge(); updateBE(); updateSvr();
  if (activeTab === 'dashboard') renderDash();
  if (activeTab === 'oi-table') renderOI();
  if (activeTab === 'alerts') renderAlerts();
  if (activeTab === 'log') renderLog();
}
function loadState(cb) {
  chrome.storage.local.get(Object.values(SK), r => {
    if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError);
    r = r || {};
    S.site = r[SK.SITE] || null;
    S.snap = r[SK.SNAPSHOT] || null;
    S.alerts = r[SK.ALERTS] || [];
    S.log = r[SK.SCAN_LOG] || [];
    S.lastTs = r[SK.LAST_TS] || null;
    S.count = r[SK.COUNT] || 0;
    S.beOk = r[SK.BE_OK] || false;
    S.chartData = r[SK.CHART_DATA] || {};
    S.lastChartSym = r[SK.LAST_CHART_SYM] || null;
    S.chartDebug = r[SK.CHART_DEBUG] || null;
    const s = r[SK.SETTINGS]; if (s) S.settings = { ...DFLT, ...s };
    S.iSec = (S.settings.intervalMin || 5) * 60;
    if (cb) cb();
  });
}
function loadSettingsUI() {
  chrome.storage.local.get([SK.SETTINGS], r => {
    if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError);
    r = r || {};
    const s = { ...DFLT, ...(r[SK.SETTINGS] || {}) };
    el('s-oi').value = s.oiThreshold;
    el('s-ltp').value = s.ltpSpikeThreshPct || 5;
    el('s-int').value = s.intervalMin;
    el('s-strike').value = s.strikeRange || 15;
    el('s-url').value = s.backendUrl;
    tog('t-notif', s.notifications);
    tog('t-be', s.forwardBackend);
  });
}
function checkBE() {
  const url = S.settings.backendUrl || 'http://localhost:8765';
  chrome.runtime.sendMessage({ type: 'CHECK_BACKEND', backendUrl: url }, resp => {
    if (chrome.runtime.lastError) return;
    const ok = !!(resp?.ok);
    if (ok !== S.beOk) { chrome.storage.local.set({ [SK.BE_OK]: ok }, () => { void chrome.runtime.lastError; }); S.beOk = ok; updateBE(); updateSvr(); }
  });
}

// ── Countdown ─────────────────────────────────────────────────────────────────
function updateCD() {
  let rem = S.iSec;
  const elapsed = S.lastTs ? Math.floor((Date.now() - new Date(S.lastTs).getTime()) / 1000) : Infinity;
  if (S.lastTs) rem = Math.max(0, S.iSec - elapsed);
  const m = Math.floor(rem / 60), s = rem % 60;
  el('cd').textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  const urgCls = rem <= 60 ? ' urgent' : rem <= 90 ? ' warning' : '';
  el('cd').className = 'cd-timer' + urgCls;
  el('pb').className = 'pb' + urgCls;
  el('pb').style.width = ((rem / S.iSec) * 100).toFixed(1) + '%';
  el('scan-no').textContent = `Scan #${S.count}`;

  // ── Watchdog: if stuck at 00:00 for >30s, auto force-scan ───────────────
  if (rem === 0 && elapsed > S.iSec + 30) {
    if (++stuckTick >= 30) {
      stuckTick = 0;
      console.warn('[NSEBOT] Watchdog: scan overdue, forcing scan');
      forceScan();
    }
  } else {
    stuckTick = 0;
  }
}
function updateStatus() {
  const LABELS = { nse: 'NSE India', dhan: 'Dhan', sensibull: 'Sensibull', opstra: 'Opstra', dhan_chart: 'Dhan Chart' };
  if (S.site) {
    el('sdot').className = 'dot active'; const lbl = LABELS[S.site] || S.site;
    const type = S.site === 'dhan_chart' ? 'chart' : 'option chain';
    el('stxt').innerHTML = `Monitoring <strong>${lbl}</strong> ${type}`;
    el('header-site').textContent = lbl; el('header-site').style.color = '#00d4aa';
  } else {
    el('sdot').className = 'dot inactive'; el('stxt').innerHTML = '⚠ No supported page open';
    el('header-site').textContent = 'No page'; el('header-site').style.color = '#ef5350';
  }
}
function updateBE() {
  const ok = S.beOk; const e = el('be-badge');
  e.className = `be-badge ${ok ? 'ok' : 'err'}`; e.textContent = ok ? 'Backend ✓' : 'Backend ✗';
}
function updateMetrics() {
  const snap = S.snap; if (!snap) return;
  const m = getMetrics(snap);
  el('m-pcr').textContent = Number.isFinite(m.pcr) ? m.pcr.toFixed(2) : '—';
  el('m-pcr').className = 'mv ' + (Number.isFinite(m.pcr) ? (m.pcr >= 1.3 ? 'green' : m.pcr <= 0.7 ? 'red' : 'blue') : 'blue');
  el('m-maxpain').textContent = Number.isFinite(m.maxPain) ? m.maxPain.toLocaleString('en-IN') : '—';
  el('m-alerts').textContent = symAlerts().length;
}
// Returns alerts for the active symbol — prefers backend (SQLite) over local storage
function symAlerts() {
  const sym = S.snap?.symbol;
  const src = S.backendAlerts.length ? S.backendAlerts : S.alerts;
  return sym ? src.filter(a => a.symbol === sym) : src;
}

// Fetch recent alerts from Python backend bridge and store in S.backendAlerts
function fetchBackendAlerts() {
  if (!S.beOk) return;
  const sym = S.snap?.symbol;
  const path = '/alerts?limit=40' + (sym ? `&symbol=${encodeURIComponent(sym)}` : '');
  const urls = beUrls(S.settings.backendUrl, path);
  fetch(urls[0])
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(data => {
      if (Array.isArray(data.alerts)) {
        S.backendAlerts = data.alerts;
        if (activeTab === 'alerts') renderAlerts();
        updateAlertBadge(); updateMetrics();
      }
    })
    .catch(() => {
      // fallback: try alternate URL (localhost vs 127.0.0.1)
      if (urls[1]) fetch(urls[1])
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(data => { if (Array.isArray(data.alerts)) { S.backendAlerts = data.alerts; } })
        .catch(() => { });
    });
}

function updateAlertBadge() {
  const c = symAlerts().length, b = el('ab');
  b.textContent = c > 99 ? '99+' : c; b.style.display = c > 0 ? 'inline-block' : 'none';
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function renderDash() {
  const snap = S.snap; if (!snap) return;
  const m = getMetrics(snap);
  const full = snap.strikes || [], filt = snap.filteredStrikes || full;
  el('d-sym').textContent = snap.symbol || '—';
  el('d-src').textContent = `page: ${snap.site || '—'}`;
  el('d-scan').textContent = snap.lastScan ? fmtIST(new Date(snap.lastScan)) : '—';
  el('d-status').textContent = snap.scanOk ? '✓ success' : '✗ failed';

  // Timeframe Bias (1H & 3H)
  const trends = { '1h': '—', '3h': '—' };

  const snapSym = snap.symbol;
  const lastSym = S.lastChartSym;
  const chartMatch = pickChartRef(S.chartData, lastSym, snapSym);
  const symData = chartMatch.data;

  if (symData) {
    ['1h', '3h'].forEach(tf => {
      const data = symData[tf] || symData[tf.toUpperCase()];
      if (data && data.sentiment) {
        const icon = data.sentiment === 'BULLISH' ? '🟢' : data.sentiment === 'BEARISH' ? '🔴' : '⚪';
        trends[tf] = `${tf.toUpperCase()}: ${icon} ${data.sentiment}`;
      }
    });
  }
  const e1 = el('d-trend-1h'), e3 = el('d-trend-3h');

  // Helper to format OHLC
  const getOhlcHtml = (tf) => {
    const ohlc = symData?.[tf]?.ohlc || symData?.[tf.toUpperCase()]?.ohlc;
    if (!ohlc) return '';
    if (ohlc.close && !ohlc.open) return `<br><span style="font-size:10px;color:#8fa8c8;font-family:monospace">LTP: ${ohlc.close}</span>`;
    return `<br><span style="font-size:10px;color:#8fa8c8;font-family:monospace">O:${ohlc.open} H:${ohlc.high} L:${ohlc.low} C:${ohlc.close}</span>`;
  };

  if (e1) e1.innerHTML = `${trends['1h']}${getOhlcHtml('1h')}`;
  if (e3) e3.innerHTML = `${trends['3h']}${getOhlcHtml('3h')}`;

  // Chart Reference
  el('d-chart').textContent = chartMatch.key || S.lastChartSym || '—';
  if (symData) {
    const tfs = Object.keys(symData);
    const latest = tfs.reduce((max, tf) => {
      const ts = new Date(symData[tf].updated_at).getTime();
      return ts > max ? ts : max;
    }, 0);
    const diff = Math.floor((Date.now() - latest) / 1000);
    el('d-chart-ts').textContent = latest === 0 ? '—' : (diff < 60 ? 'just now' : `${Math.floor(diff / 60)}m ago`);
  } else {
    const dbg = S.chartDebug;
    el('d-chart-ts').textContent = dbg?.stage ? `${dbg.stage}` : '—';
  }

  let chartStale = false;
  if (symData) {
    const tfs2 = Object.keys(symData);
    const latestSeen = tfs2.reduce((max, tf) => {
      const ts = new Date(symData[tf].seen_at || symData[tf].updated_at).getTime();
      return ts > max ? ts : max;
    }, 0);
    const latestChanged = tfs2.reduce((max, tf) => {
      const ts = new Date(symData[tf].changed_at || symData[tf].updated_at).getTime();
      return ts > max ? ts : max;
    }, 0);
    chartStale = latestSeen > 0 && Date.now() - latestSeen > 120000;
    el('d-chart-ts').textContent = latestSeen === 0 ? '—' : `seen ${ago(latestSeen)} | trend same ${ago(latestChanged)}`;
  }

  // Symbol Mismatch Check
  const mDiv = el('sym-mismatch');
  const mMsg = el('sym-mismatch-msg');
  const chartRef = chartMatch.key || S.lastChartSym;
  if (mDiv && mMsg && chartRef && snap.symbol && normSym(chartRef) !== normSym(snap.symbol)) {
    mDiv.style.display = 'flex';
    mMsg.textContent = `Symbol Mismatch: Chart (${chartRef}) vs Chain (${snap.symbol})`;
  } else if (mDiv && mMsg && chartStale) {
    mDiv.style.display = 'flex';
    mMsg.textContent = `Chart scanner stale: ${el('d-chart-ts')?.textContent || 'not seen recently'}`;
  } else if (mDiv) {
    mDiv.style.display = 'none';
  }

  // PCR bars
  const tot = Math.max(m.ceTotal + m.peTotal, 1);
  el('ce-tot').textContent = fmtNum(m.ceTotal); el('pe-tot').textContent = fmtNum(m.peTotal);
  el('ce-bar').style.width = ((m.ceTotal / tot) * 100).toFixed(0) + '%';
  el('pe-bar').style.width = ((m.peTotal / tot) * 100).toFixed(0) + '%';
  el('ce-int').textContent = '';
  el('pe-int').textContent = Number.isFinite(m.pcr)
    ? (m.pcr >= 1.5 ? '🐂 Strong Bull' : m.pcr >= 1.2 ? '📗 Bullish' : m.pcr >= 0.8 ? '⚖ Neutral' : m.pcr >= 0.6 ? '📕 Bearish' : '🐻 Strong Bear') : '—';

  renderChart(full, resolveUnderlying(snap));
}

function renderChart(strikes, underlying) {
  const canvas = el('oi-chart'); if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 456, H = 90;
  canvas.width = W * devicePixelRatio; canvas.height = H * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio); ctx.clearRect(0, 0, W, H);
  if (!strikes.length) return;

  const allS = [...new Set(strikes.map(r => r.strike))].sort((a, b) => a - b);
  const atmI = allS.reduce((b, s, i) => Math.abs(s - underlying) < Math.abs(allS[b] - underlying) ? i : b, 0);
  const lo = Math.max(0, atmI - 5), hi = Math.min(allS.length - 1, atmI + 5);
  const viewS = allS.slice(lo, hi + 1);
  const ceMap = {}, peMap = {};
  strikes.forEach(r => { if (viewS.includes(r.strike)) { if (r.option_type === 'CE') ceMap[r.strike] = r.oi || 0; else peMap[r.strike] = r.oi || 0; } });
  const maxOI = Math.max(...Object.values(ceMap), ...Object.values(peMap), 1);
  const n = viewS.length, bw = Math.floor((W - 20) / (n * 2 + n + 1));
  const padX = (W - (n * (2 * bw + 2))) / 2, atm = allS[atmI];

  viewS.forEach((s, i) => {
    const x = padX + i * (2 * bw + 2);
    const ceH = Math.floor((Math.sqrt(ceMap[s] || 0) / Math.sqrt(maxOI)) * (H - 20));
    const peH = Math.floor((Math.sqrt(peMap[s] || 0) / Math.sqrt(maxOI)) * (H - 20));
    const isATM = s === atm;
    ctx.fillStyle = isATM ? '#ff5252' : '#b71c1c'; ctx.fillRect(x, H - 17 - ceH, bw, ceH);
    ctx.fillStyle = isATM ? '#00e5c3' : '#00695c'; ctx.fillRect(x + bw + 1, H - 17 - peH, bw, peH);
    ctx.fillStyle = isATM ? '#ffd54f' : '#3a5a7a'; ctx.font = `${isATM ? 700 : 400} 8px 'Segoe UI',sans-serif`; ctx.textAlign = 'center';
    const lbl = s >= 1000 ? String(Math.round(s / 100) * 100).slice(0, -2) : s;
    ctx.fillText(lbl, x + bw, H - 3);
  });
  ctx.font = '9px Segoe UI'; ctx.textAlign = 'left';
  ctx.fillStyle = '#ef5350'; ctx.fillRect(0, 2, 8, 8); ctx.fillStyle = '#8fa8c8'; ctx.fillText('CE OI', 10, 10);
  ctx.fillStyle = '#00d4aa'; ctx.fillRect(50, 2, 8, 8); ctx.fillStyle = '#8fa8c8'; ctx.fillText('PE OI', 62, 10);
  ctx.fillStyle = '#ffd54f'; ctx.fillRect(100, 2, 8, 8); ctx.fillStyle = '#8fa8c8'; ctx.fillText('ATM', 112, 10);
}

// ── OI Table ──────────────────────────────────────────────────────────────────
// Removed const ATM_TABLE_WINDOW=15;
function renderOI() {
  const c = el('oi-tc'), snap = S.snap;
  const sourceRows = snap?.filteredStrikes?.length ? snap.filteredStrikes : (snap?.strikes || []);
  if (!sourceRows.length) { c.innerHTML = '<div class="empty"><div class="ei">📋</div><p>No data yet.</p></div>'; return; }
  const underlying = resolveUnderlying(snap);

  // Find ATM from all unique strikes
  const allUniq = [...new Set(sourceRows.map(r => +r.strike))].sort((a, b) => a - b);
  let atmIdx = Math.floor(allUniq.length / 2);
  if (underlying > 0) {
    let best = Infinity;
    allUniq.forEach((s, i) => { const d = Math.abs(s - underlying); if (d < best) { best = d; atmIdx = i; } });
  }
  const atm = allUniq[atmIdx];

  // Slice to ATM ± window strikes
  const win = S.settings.strikeRange || 15;
  const lo = Math.max(0, atmIdx - win);
  const hi = Math.min(allUniq.length - 1, atmIdx + win);
  const keepS = new Set(allUniq.slice(lo, hi + 1));
  const rows = sourceRows.filter(r => keepS.has(+r.strike));
  const allS = [...keepS].sort((a, b) => a - b);

  const maxOI = Math.max(...rows.map(r => r.oi || 0), 1);
  const ceM = {}, peM = {};
  rows.forEach(r => { if (r.option_type === 'CE') ceM[r.strike] = r; else peM[r.strike] = r; });
  const prev = snap?.prevStrikes || {};
  const delt = (s, ot) => { const k = `${ot}_${s}`, curr = ot === 'CE' ? ceM[s]?.oi : peM[s]?.oi, p = prev[k]?.oi; return (curr && p) ? Math.round(curr - p) : null; };
  const fmtD = (d, pc, nc) => d !== null ? `<span class="dp ${d >= 0 ? pc : nc}">${d >= 0 ? '+' : ''}${fmtNum(d)}</span>` : '<span style="color:#2a3a50">—</span>';

  let html = `<table class="oi-table"><thead><tr>
    <th class="hce">CE OI</th><th class="hce">CE Δ</th><th class="hce" style="width:38px">Bar</th><th class="hce">LTP</th>
    <th class="hst">Strike</th>
    <th class="hpe">LTP</th><th class="hpe" style="width:38px">Bar</th><th class="hpe">PE Δ</th><th class="hpe">PE OI</th>
  </tr></thead><tbody>`;

  allS.forEach(s => {
    const ce = ceM[s] || {}, pe = peM[s] || {};
    const isATM = s === atm;
    const cp = (Math.sqrt(ce.oi || 0) / Math.sqrt(maxOI) * 100).toFixed(0);
    const pp = (Math.sqrt(pe.oi || 0) / Math.sqrt(maxOI) * 100).toFixed(0);
    const cd = delt(s, 'CE'), pd = delt(s, 'PE');
    html += `<tr${isATM ? ' class="atm-row"' : ''}>
      <td class="ce">${fmtNum(ce.oi || 0)}</td>
      <td>${fmtD(cd, 'cp', 'cn')}</td>
      <td style="width:38px"><div style="display:flex;justify-content:flex-end"><div class="ob ce" style="width:${cp}%"></div></div></td>
      <td class="ce" style="font-size:10px">${ce.ltp ? ce.ltp.toFixed(1) : '—'}</td>
      <td class="sc${isATM ? ' atm' : ''}">${s.toLocaleString('en-IN')}${isATM ? ' ★' : ''}</td>
      <td class="pe" style="font-size:10px">${pe.ltp ? pe.ltp.toFixed(1) : '—'}</td>
      <td style="width:38px"><div class="ob pe" style="width:${pp}%"></div></td>
      <td>${fmtD(pd, 'pp', 'pn')}</td>
      <td class="pe">${fmtNum(pe.oi || 0)}</td>
    </tr>`;
  });
  el('oi-tc').innerHTML = html + '</tbody></table>';
}

// ── Alerts — IST time, no expiry ──────────────────────────────────────────────
function renderAlerts() {
  // Update symbol badge in the section header
  const activeSym = S.snap?.symbol;
  const badge = el('alerts-sym-badge');
  if (badge) badge.textContent = activeSym || '';

  updateAIInterpretation();
  // Sort by fired_at DESC (newest first) regardless of source.
  // Backend returns DESC already; local storage may be ASC — explicit sort handles both.
  const sorted = [...symAlerts()].sort((a, b) => {
    const ta = a.fired_at ? Date.parse(a.fired_at) : 0;
    const tb = b.fired_at ? Date.parse(b.fired_at) : 0;
    return tb - ta;
  });
  const c = el('alerts-c'), alerts = sorted.slice(0, MAX_ALERTS);
  if (!alerts.length) {
    const msg = activeSym ? `No anomalies for ${activeSym} yet.` : 'No anomalies detected.';
    c.innerHTML = `<div class="empty"><div class="ei">🔔</div><p>${msg}</p></div>`; return;
  }
  const SEV_COLOR = { HIGH: '#ef5350', MEDIUM: '#ffa726', MED: '#ffa726', LOW: '#4a6a8a' };
  c.innerHTML = alerts.map(a => {
    const pct = parseFloat(a.pct_change || a.pct || 0);
    const ts = a.fired_at ? fmtISTFull(new Date(a.fired_at.replace(/\+05:30$/, ''))) : '';
    const emoji = EMOJIS[a.alert_type] || '🔔';
    const sevColor = SEV_COLOR[a.severity] || '';
    const sevBadge = a.severity ? `<span style="font-size:9px;font-weight:700;color:${sevColor};margin-left:4px">${a.severity}</span>` : '';

    let body = `<strong>${a.symbol || ''}</strong>`;

    if (a.alert_type === 'BUILDUP_CLASSIFY') {
      if (a.strike) body += ` | ${a.option_type} <strong>${Number(a.strike).toLocaleString('en-IN')}</strong>`;
      if (a.buildup_type) body += ` | ${a.buildup_type}`;
      if (a.oi_pct != null) body += ` (OI ${a.oi_pct > 0 ? '+' : ''}${(+a.oi_pct).toFixed(1)}%)`;
    } else if (a.alert_type === 'ATM_LEG_MOVE') {
      if (a.bias) body += ` | ${a.bias}`;
      if (a.ce_pct != null) body += ` CE ${a.ce_pct > 0 ? '+' : ''}${(+a.ce_pct).toFixed(1)}%`;
      if (a.pe_pct != null) body += ` PE ${a.pe_pct > 0 ? '+' : ''}${(+a.pe_pct).toFixed(1)}%`;
    } else if (a.alert_type === 'OI_WALL_SHIFT') {
      if (a.resistance_from != null) body += ` | Resistance ${a.resistance_from}→${a.resistance_to}`;
      if (a.support_from != null) body += ` | Support ${a.support_from}→${a.support_to}`;
    } else if (a.alert_type === 'SCAN_SUMMARY') {
      body += ` | scan complete`;
      body += ` | alerts ${a.generated_alerts ?? 0} (${a.new_alerts ?? 0} new)`;
      if (a.max_oi_delta_pct != null) body += ` | max OI Δ ${(+a.max_oi_delta_pct).toFixed(2)}%`;
      if (a.max_atm_ltp_delta_pct != null) body += ` | ATM LTP Δ ${(+a.max_atm_ltp_delta_pct).toFixed(2)}%`;
    } else if (a.alert_type === 'LTP_SPIKE') {
      if (a.strike) body += ` | ATM ${a.option_type} ${Number(a.strike).toLocaleString('en-IN')}`;
      if (a.prev_ltp != null) body += ` | LTP: ${(+a.prev_ltp).toFixed(1)}→${(+a.curr_ltp).toFixed(1)}`;
    } else {
      if (a.strike) body += ` | ${a.option_type} <strong>${Number(a.strike).toLocaleString('en-IN')}</strong>`;
      if (a.prev_oi) body += ` | OI: ${fmtNum(a.prev_oi)}→${fmtNum(a.curr_oi)}`;
      if (!a.prev_oi && a.curr_oi) body += ` | OI: ${fmtNum(a.curr_oi)}`;
    }

    let detail = '';
    if (pct) detail += `<span class="ap ${pct > 0 ? 'pos' : 'neg'}">${pct > 0 ? '+' : ''}${pct.toFixed(1)}%</span>`;
    if (a.underlying) detail += ` | Spot: ${Number(a.underlying).toLocaleString('en-IN')}`;

    return `<div class="alert-item ${a.alert_type}">
      <div class="al-top">
        <span class="ab ${a.alert_type}">${emoji} ${a.alert_type}${sevBadge}</span>
        <span class="al-time">${ts} IST</span>
      </div>
      <div class="al-body">${body}</div>
      ${detail ? `<div class="al-detail">${detail}</div>` : ''}
    </div>`;
  }).join('');
}

function updateAIInterpretation() {
  const c = el('ai-interp');
  const filteredAlerts = symAlerts();
  if (!filteredAlerts.length) {
    if (c) c.innerHTML = '<div class="empty" style="padding:15px 0; border:none; margin:0;"><div class="ei">🧠</div><p>Awaiting scan data...</p></div>';
    return;
  }

  const scans = [];
  filteredAlerts.forEach(a => {
    let lastScan = scans[scans.length - 1];
    if (!lastScan || Math.abs(new Date(lastScan.ts) - new Date(a.fired_at)) > 60000) {
      scans.push({ ts: a.fired_at, alerts: [a] });
    } else {
      lastScan.alerts.push(a);
    }
  });

  const latestScan = scans[scans.length - 1];
  const last5Scans = scans.slice(-5);
  let html = '';

  if (latestScan && latestScan.alerts.length > 0) {
    let ceS = 0, peS = 0, ceU = 0, peU = 0, cLtp = 0, pLtp = 0;
    latestScan.alerts.forEach(a => {
      if (a.alert_type === 'OI_SPIKE') { if (a.option_type === 'CE') ceS++; else peS++; }
      else if (a.alert_type === 'OI_UNWIND') { if (a.option_type === 'CE') ceU++; else peU++; }
      else if (a.alert_type === 'LTP_SPIKE') {
        if (a.option_type === 'CE' && parseFloat(a.pct_change) > 0) cLtp++;
        if (a.option_type === 'PE' && parseFloat(a.pct_change) > 0) pLtp++;
      }
    });

    let sent = 'Neutral', move = 'Rangebound / Sideways', adv = 'Wait & Watch', cls = 'neutral';

    const bearScore = ceS + peU + (pLtp > cLtp ? 1 : 0);
    const bullScore = peS + ceU + (cLtp > pLtp ? 1 : 0);

    if (bearScore > bullScore) { sent = 'Bearish'; move = 'Downward / Facing Resistance'; adv = 'Look for Sell on Rise'; cls = 'bear'; }
    if (bullScore > bearScore) { sent = 'Bullish'; move = 'Upward / Finding Support'; adv = 'Look for Buy on Dip'; cls = 'bull'; }

    if (ceS > 0 && peU > 0) { sent = 'Strong Bearish'; move = 'Sharp Downward'; adv = 'Aggressive Short / Avoid Calls'; cls = 'bear'; }
    else if (peS > 0 && ceU > 0) { sent = 'Strong Bullish'; move = 'Sharp Upward'; adv = 'Aggressive Long / Avoid Puts'; cls = 'bull'; }

    html += `
      <div class="ai-hdr">Latest Scan Insight</div>
      <div class="ai-b">
        <span style="color:#64748b">Sentiment:</span> <strong style="color:#1e293b">${sent}</strong><br>
        <span style="color:#64748b">Expected Move:</span> <strong style="color:#1e293b">${move}</strong><br>
        <div class="ai-adv ${cls}">💡 ${adv}</div>
      </div>
    `;
  }

  if (last5Scans.length > 1) {
    let tCeS = 0, tPeS = 0, tCeU = 0, tPeU = 0;
    last5Scans.forEach(s => s.alerts.forEach(a => {
      if (a.alert_type === 'OI_SPIKE') { if (a.option_type === 'CE') tCeS++; else tPeS++; }
      if (a.alert_type === 'OI_UNWIND') { if (a.option_type === 'CE') tCeU++; else tPeU++; }
    }));

    let tr = 'Mixed / Indecisive', tCls = 'neutral';
    const netBear = tCeS + tPeU;
    const netBull = tPeS + tCeU;

    if (tCeS >= 2 && tPeU >= 1) { tr = 'Aggressive Call Writing (Bearish Trend)'; tCls = 'bear'; }
    else if (tPeS >= 2 && tCeU >= 1) { tr = 'Aggressive Put Writing (Bullish Trend)'; tCls = 'bull'; }
    else if (netBear > netBull * 1.5) { tr = 'Gradual Resistance Build-up (Mild Bearish)'; tCls = 'bear'; }
    else if (netBull > netBear * 1.5) { tr = 'Gradual Support Build-up (Mild Bullish)'; tCls = 'bull'; }
    else if (tCeS > 0 && tPeS > 0 && Math.abs(tCeS - tPeS) <= 1) { tr = 'Straddle / Strangle Build-up (Rangebound)'; tCls = 'neutral'; }

    html += `
      <div class="ai-hdr" style="margin-top:10px;">Broader Shift (Last ${last5Scans.length} Scans)</div>
      <div class="ai-b">
        <span class="ai-adv ${tCls}" style="margin-left:0;">🎯 ${tr}</span>
      </div>
    `;
  }

  if (c) c.innerHTML = html || '<div class="empty" style="padding:15px 0; border:none; margin:0;"><div class="ei">🧠</div><p>No actionable insights yet.</p></div>';
}

// ── Scan Log — IST time ────────────────────────────────────────────────────────
function renderLog() {
  const c = el('log-c'), log = [...S.log].reverse().slice(0, MAX_LOG);
  if (!log.length) { c.innerHTML = '<div class="empty"><div class="ei">📝</div><p>Scan log will appear here.</p></div>'; return; }
  c.innerHTML = log.map(e => {
    const cls = e.ok ? (e.warn ? 'warn' : 'ok') : 'err', icon = e.ok ? (e.warn ? '⚠' : '✓') : '✗';
    const ts = e.ts ? fmtIST(new Date(e.ts)) : '';
    return `<div class="sli"><span class="sl-ts">${ts}</span><span class="sl-ic">${icon}</span><span class="sl-msg ${cls}">${e.msg}</span></div>`;
  }).join('');
}

// ── Settings ──────────────────────────────────────────────────────────────────
function saveSettings() {
  const newStrike = parseInt(el('s-strike').value) || 15;
  const s = {
    oiThreshold: parseInt(el('s-oi').value) || 25,
    ltpSpikeThreshPct: parseInt(el('s-ltp').value) || 5,
    intervalMin: parseInt(el('s-int').value) || 5,
    strikeRange: newStrike,
    notifications: el('t-notif').classList.contains('on'),
    forwardBackend: el('t-be').classList.contains('on'),
    backendUrl: el('s-url').value.trim() || 'http://localhost:8765',
  };
  const strikeChanged = S.settings && S.settings.strikeRange !== newStrike;
  chrome.storage.local.set({ [SK.SETTINGS]: s }, () => {
    S.settings = s;
    chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
      if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: 'SETTINGS_UPDATED', settings: s }, () => { void chrome.runtime.lastError; });
    });
    const b = el('btn-save'), orig = b.textContent;
    b.textContent = '✅ Saved!'; setTimeout(() => b.textContent = orig, 1500);
    if (strikeChanged) {
      alert('Strike range changed to ±' + newStrike + '.\nForcing a rescan...\n\nNote: Backend service does NOT need to be restarted.');
      forceScan();
      renderOI();
    }
  });
}
function testBE() {
  const url = el('s-url').value.trim() || 'http://localhost:8765', b = el('btn-test');
  b.textContent = '⏳ Testing...';
  chrome.runtime.sendMessage({ type: 'CHECK_BACKEND', backendUrl: url }, resp => {
    if (chrome.runtime.lastError) return;
    const ok = !!(resp?.ok); chrome.storage.local.set({ [SK.BE_OK]: ok }, () => { void chrome.runtime.lastError; }); S.beOk = ok;
    b.textContent = ok ? '✅ Connected!' : '❌ Unreachable';
    setTimeout(() => b.textContent = '🔌 Test Backend', 2000); updateBE(); updateSvr();
  });
}
function forceScan() {
  chrome.tabs.query({}, tabs => {
    tabs.forEach(t => {
      const isSupported = t.url && (
        t.url.includes('dhan.co') ||
        t.url.includes('tradingview.com') ||
        t.url.includes('sensibull.com') ||
        t.url.includes('nseindia.com')
      );
      if (isSupported) {
        const send = () => chrome.tabs.sendMessage(t.id, { type: 'FORCE_SCAN' }, () => {
          const err = chrome.runtime.lastError;
          if (err) console.debug(`[NSEBOT] Message skip for tab ${t.id}: ${err.message}`);
        });
        if (t.url.includes('tv.dhan.co') || t.url.includes('tradingview.com')) {
          chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ['tv_content.js'] }, send);
        } else {
          send();
        }
      }
    });
  });
  const b = el('btn-force'); if (b) { const orig = b.textContent; b.textContent = '⏳ Scanning...'; setTimeout(() => b.textContent = orig, 3000); }
}
function clearAll() {
  const sym = S.snap?.symbol;
  let scope = null;   // 'all' | 'symbol' | null (cancel)

  if (!sym) {
    // No active symbol → only "all" makes sense
    if (confirm('Clear ALL alerts and scan log?')) scope = 'all';
  } else {
    // Two-step: ask whether to wipe everything or just current symbol
    const wipeAll = confirm(
      `Clear alerts for ALL symbols?\n\n` +
      `[ OK ]    → Clear ALL symbols' alerts + scan log\n` +
      `[Cancel]  → Choose to clear only ${sym}`
    );
    if (wipeAll) {
      scope = 'all';
    } else if (confirm(`Clear alerts only for ${sym}?\n\n[ OK ] = Yes, [Cancel] = Abort`)) {
      scope = 'symbol';
    }
  }
  if (!scope) return;

  // 1. Clear backend (Python SQLite)
  if (S.beOk) {
    const path = scope === 'all' ? '/alerts/clear' : `/alerts/clear?symbol=${encodeURIComponent(sym)}`;
    const urls = beUrls(S.settings.backendUrl, path);
    fetch(urls[0], { method: 'POST' }).catch(() => {
      if (urls[1]) fetch(urls[1], { method: 'POST' }).catch(() => { });
    });
  }

  // 2. Clear local extension storage
  if (scope === 'all') {
    chrome.storage.local.set({ [SK.ALERTS]: [], [SK.SCAN_LOG]: [], [SK.COUNT]: 0 }, () => {
      S.alerts = []; S.log = []; S.count = 0; S.backendAlerts = [];
      renderAlerts(); renderLog(); updateAlertBadge(); updateMetrics();
    });
  } else {
    // Symbol-scoped: keep alerts/log for other symbols
    const remaining = S.alerts.filter(a => a.symbol !== sym);
    chrome.storage.local.set({ [SK.ALERTS]: remaining }, () => {
      S.alerts = remaining;
      S.backendAlerts = S.backendAlerts.filter(a => a.symbol !== sym);
      renderAlerts(); updateAlertBadge(); updateMetrics();
    });
  }

  // 3. Re-fetch backend after a brief delay to confirm the deletion stuck
  setTimeout(fetchBackendAlerts, 400);
}

// ── Computations ──────────────────────────────────────────────────────────────
function getMetrics(snap) {
  const strikes = snap?.strikes || [], s = snap?.summary || {};
  const comp = computePCR(strikes);
  const ceTotal = Number.isFinite(Number(s.ceOi)) ? Number(s.ceOi) : comp.ceTotal;
  const peTotal = Number.isFinite(Number(s.peOi)) ? Number(s.peOi) : comp.peTotal;
  const pcr = Number.isFinite(Number(s.pcr)) ? Number(s.pcr) : (ceTotal > 0 ? peTotal / ceTotal : null);
  const maxPain = Number.isFinite(Number(s.maxPain)) ? Number(s.maxPain) : computeMaxPain(strikes);
  return { pcr, ceTotal, peTotal, maxPain };
}
function computePCR(strikes) {
  let ce = 0, pe = 0;
  strikes.forEach(r => { if (r.option_type === 'CE') ce += r.oi || 0; else pe += r.oi || 0; });
  return { pcr: ce > 0 ? pe / ce : null, ceTotal: ce, peTotal: pe };
}
function computeMaxPain(strikes) {
  const ceM = {}, peM = {};
  strikes.forEach(r => { if (r.option_type === 'CE') ceM[r.strike] = r.oi || 0; else peM[r.strike] = r.oi || 0; });
  const allS = [...new Set([...Object.keys(ceM), ...Object.keys(peM)].map(Number))].sort((a, b) => a - b);
  if (!allS.length) return null;
  let minP = Infinity, mp = null;
  allS.forEach(c => { let p = 0; allS.forEach(s => { if (c > s) p += (c - s) * (ceM[s] || 0); if (c < s) p += (s - c) * (peM[s] || 0); }); if (p < minP) { minP = p; mp = c; } });
  return mp;
}
