/**
 * tv_content.js — NSEBOT Chart Scraper
 * Fixed resilient version for TradingView / Dhan TV chart pages
 *
 * Writes data to:
 *   - nsebot_chart_data
 *   - last_chart_symbol
 *   - nsebot_chart_debug
 *
 * Expected by popup.js:
 * {
 *   SYMBOL: {
 *     "1h": {
 *       sentiment: "BULLISH" | "BEARISH" | "NEUTRAL",
 *       ohlc: { open, high, low, close } | null,
 *       updated_at,
 *       seen_at,
 *       changed_at
 *     },
 *     "3h": { ... }
 *   }
 * }
 */

'use strict';

(function () {
  if (window.__NSEBOT_TV_CONTENT_FIXED__) return;
  window.__NSEBOT_TV_CONTENT_FIXED__ = true;

  const SCRAPE_INTERVAL_MS = 5000;
  const STORAGE_KEY = 'nsebot_chart_data';
  const DEBUG_KEY = 'nsebot_chart_debug';
  const LAST_SYMBOL_KEY = 'last_chart_symbol';

  let scrapeInterval = null;
  let lastSavedSerialized = '';

  // ---------------------------------------------------------------------------
  // Utility helpers
  // ---------------------------------------------------------------------------

  function nowIso() {
    return new Date().toISOString();
  }

  function safeText(el) {
    return (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function getBodyText() {
    return safeText(document.body);
  }

  function debug(stage, extra = {}) {
    try {
      chrome.storage.local.set({
        [DEBUG_KEY]: {
          stage,
          url: window.location.href,
          title: document.title || '',
          host: window.location.hostname,
          at: nowIso(),
          ...extra
        }
      });
    } catch (_) {
      // Ignore debug failures
    }
  }

  function parseNumber(raw) {
    if (raw == null) return null;

    const cleaned = String(raw)
      .replace(/,/g, '')
      .replace(/[^\d.+-]/g, '');

    const n = Number.parseFloat(cleaned);
    return Number.isFinite(n) ? n : null;
  }

  function escapeRe(s) {
    return String(s || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // ---------------------------------------------------------------------------
  // Timeframe normalization
  // ---------------------------------------------------------------------------

  function normTF(t) {
    if (!t) return '—';

    const s = String(t)
      .toLowerCase()
      .replace(/\s+/g, '')
      .replace(/^interval=/, '');

    const map = {
      '5': '5m',
      '5m': '5m',
      '15': '15m',
      '15m': '15m',
      '30': '30m',
      '30m': '30m',
      '60': '1h',
      '60m': '1h',
      '1h': '1h',
      '180': '3h',
      '180m': '3h',
      '3h': '3h',
      '240': '4h',
      '240m': '4h',
      '4h': '4h',
      'd': '1d',
      '1d': '1d',
      '1day': '1d'
    };

    return map[s] || '—';
  }

  function findTimeframeFromUrl() {
    try {
      const url = new URL(window.location.href);

      const keys = [
        'interval',
        'timeframe',
        'tf',
        'resolution'
      ];

      for (const key of keys) {
        const value = url.searchParams.get(key);
        const tf = normTF(value);
        if (tf !== '—') return tf;
      }
    } catch (_) {
      // Ignore URL errors
    }

    return '—';
  }

  function findTimeframeFromPageText(text) {
    const s = String(text || '').toUpperCase();

    // Common TradingView / Dhan visible timeframe patterns.
    const patterns = [
      /\b(5M|15M|30M|60M|180M|240M|1H|3H|4H|1D|D)\b/,
      /\b(5|15|30|60|180|240)\s*(MIN|MINS|MINUTE|MINUTES)\b/,
      /\b(1|3|4)\s*(H|HR|HOUR|HOURS)\b/,
      /\b(1)\s*(D|DAY)\b/
    ];

    for (const re of patterns) {
      const m = s.match(re);
      if (!m) continue;

      if (m[2]) {
        const unit = m[2].toUpperCase();

        if (unit.startsWith('MIN') || unit === 'MINS') {
          return normTF(`${m[1]}m`);
        }

        if (unit === 'H' || unit === 'HR' || unit.startsWith('HOUR')) {
          return normTF(`${m[1]}h`);
        }

        if (unit === 'D' || unit === 'DAY') {
          return normTF(`${m[1]}d`);
        }
      }

      return normTF(m[1]);
    }

    return '—';
  }

  function findTimeframe(text) {
    const fromUrl = findTimeframeFromUrl();
    if (fromUrl !== '—') return fromUrl;

    const fromText = findTimeframeFromPageText(text);
    if (fromText !== '—') return fromText;

    return '—';
  }

  // ---------------------------------------------------------------------------
  // Symbol extraction
  // ---------------------------------------------------------------------------

  function cleanSymbolText(raw) {
    let text = String(raw || '').toUpperCase().trim();
    if (!text) return null;

    try {
      text = decodeURIComponent(text);
    } catch (_) {
      // Ignore decode errors
    }

    text = text
      .replace(/\s+/g, '')
      .replace(/^NSE:/, '')
      .replace(/^NFO:/, '')
      .replace(/^BSE:/, '')
      .replace(/^MCX:/, '')
      .replace(/^CDS:/, '')
      .replace(/[!]/g, '')
      .replace(/\|.*/, '')
      .replace(/[,;].*/, '');

    // Remove common futures month suffixes if present.
    text = text.replace(
      /(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4}(FUT)?$/i,
      ''
    );

    text = text.replace(/[^A-Z0-9]/g, '');

    if (!text || text.length < 2) return null;

    return text;
  }

  function extractSymbolFromUrl() {
    try {
      const url = new URL(window.location.href);

      const keys = [
        'symbol',
        'tvwidgetsymbol',
        's',
        'ticker'
      ];

      for (const key of keys) {
        const val = url.searchParams.get(key);
        const clean = cleanSymbolText(val);
        if (clean) return clean;
      }

      // Sometimes symbol appears in hash.
      const hash = url.hash || '';
      const hashMatch = hash.match(/(?:symbol|ticker)=([^&]+)/i);
      if (hashMatch) {
        const clean = cleanSymbolText(hashMatch[1]);
        if (clean) return clean;
      }
    } catch (_) {
      // Ignore URL parsing errors
    }

    return null;
  }

  function extractSymbolFromTitle() {
    const title = String(document.title || '').toUpperCase();

    // Examples:
    // "NIFTY1! Chart"
    // "BANKNIFTY - NSE"
    // "NSE:NIFTY"
    const patterns = [
      /\b(?:NSE|NFO|BSE):([A-Z0-9!]{2,30})\b/,
      /\b([A-Z]{3,20}\d*!?)\s+(?:CHART|PRICE|INDEX)\b/,
      /^([A-Z]{3,20}\d*!?)\b/
    ];

    for (const re of patterns) {
      const m = title.match(re);
      if (m?.[1]) {
        const clean = cleanSymbolText(m[1]);
        if (clean) return clean;
      }
    }

    return null;
  }

  function extractSymbolFromDom() {
    const selectors = [
      '[data-name="legend-source-title"]',
      '[data-name="legend-series-item"]',
      '[class*="legendMainSourceWrapper"]',
      '[class*="mainSourceWrapper"]',
      '[class*="ticker"]',
      '[class*="symbol"]',
      '[class*="instrument"]'
    ];

    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 20);

      for (const node of nodes) {
        const text = safeText(node);
        if (!text) continue;

        const prefixed = text.match(/\b(?:NSE|NFO|BSE):([A-Z0-9!]{2,30})\b/i);
        if (prefixed?.[1]) {
          const clean = cleanSymbolText(prefixed[1]);
          if (clean) return clean;
        }

        const generic = text.match(/\b([A-Z]{3,20}\d*!?)\b/);
        if (generic?.[1]) {
          const clean = cleanSymbolText(generic[1]);
          if (clean) return clean;
        }
      }
    }

    return null;
  }

  function extractSymbol() {
    return (
      extractSymbolFromUrl() ||
      extractSymbolFromTitle() ||
      extractSymbolFromDom()
    );
  }

  // ---------------------------------------------------------------------------
  // Sentiment extraction
  // ---------------------------------------------------------------------------

  function sentimentFromSignedChange(text) {
    const s = String(text || '');

    // Matches values like:
    // +123.45 (+0.55%)
    // −55.20 −0.21%
    // -23.5 (-0.10%)
    const re = /([+\-−]\s*\d[\d,]*(?:\.\d+)?)\s*(?:\(|\s)?([+\-−]\s*\d[\d,]*(?:\.\d+)?)?\s*%?/;
    const m = s.match(re);

    if (!m) return null;

    const change = parseNumber(String(m[1]).replace('−', '-'));
    const pct = m[2] != null ? parseNumber(String(m[2]).replace('−', '-')) : null;

    const n = Number.isFinite(pct) && pct !== 0 ? pct : change;

    if (n > 0) return { sentiment: 'BULLISH', change, pct };
    if (n < 0) return { sentiment: 'BEARISH', change, pct };

    return { sentiment: 'NEUTRAL', change, pct };
  }

  function inferSentimentFromText(text) {
    const s = String(text || '').toUpperCase();

    const signed = sentimentFromSignedChange(s);
    if (signed?.sentiment) return signed.sentiment;

    // Conservative keyword fallback.
    if (/\b(STRONG BUY|BUY|BULLISH|UPTREND|LONG)\b/.test(s)) return 'BULLISH';
    if (/\b(STRONG SELL|SELL|BEARISH|DOWNTREND|SHORT)\b/.test(s)) return 'BEARISH';

    return 'NEUTRAL';
  }

  function inferSentimentFromLegend(symbol, root = document) {
    const selectors = [
      '[data-name="legend-series-item"]',
      '[data-name="legend-source-item"]',
      '[class*="legendMainSourceWrapper"]',
      '[class*="mainSourceWrapper"]',
      '[class*="seriesValues"]',
      '[class*="valuesWrapper"]'
    ];

    const sym = cleanSymbolText(symbol);
    const candidates = [];

    for (const selector of selectors) {
      const nodes = Array.from(root.querySelectorAll(selector)).slice(0, 30);

      for (const node of nodes) {
        const text = safeText(node);
        if (!text) continue;

        if (!sym || cleanSymbolText(text)?.includes(sym) || text.length < 400) {
          candidates.push(text);
        }
      }
    }

    for (const text of candidates) {
      const signed = sentimentFromSignedChange(text);
      if (signed?.sentiment && signed.sentiment !== 'NEUTRAL') {
        return signed.sentiment;
      }
    }

    return null;
  }

  // ---------------------------------------------------------------------------
  // OHLC extraction
  // ---------------------------------------------------------------------------

  function extractOHLCFromText(text) {
    const s = String(text || '').toUpperCase();

    // 1. Strict sequence: O H L C
    const re1 = /(?:\bO(?:PEN)?\s*[:=]?\s*([\d,.]+)).*?(?:\bH(?:IGH)?\s*[:=]?\s*([\d,.]+)).*?(?:\bL(?:OW)?\s*[:=]?\s*([\d,.]+)).*?(?:\bC(?:LOSE)?\s*[:=]?\s*([\d,.]+))/;
    const m1 = s.match(re1);
    if (m1) {
      return {
        open: parseNumber(m1[1]),
        high: parseNumber(m1[2]),
        low: parseNumber(m1[3]),
        close: parseNumber(m1[4])
      };
    }

    // 2. Loose extraction if they are present in any order, but require at least 3 to prevent false positives
    const o = s.match(/\bO(?:PEN)?\s*[:=]?\s*([\d,.]+)/);
    const h = s.match(/\bH(?:IGH)?\s*[:=]?\s*([\d,.]+)/);
    const l = s.match(/\bL(?:OW)?\s*[:=]?\s*([\d,.]+)/);
    const c = s.match(/\bC(?:LOSE)?\s*[:=]?\s*([\d,.]+)/);

    const matched = [o, h, l, c].filter(Boolean);
    if (matched.length >= 3) {
      return {
        open: o ? parseNumber(o[1]) : null,
        high: h ? parseNumber(h[1]) : null,
        low: l ? parseNumber(l[1]) : null,
        close: c ? parseNumber(c[1]) : null
      };
    }
    
    // 3. Extracted purely from a sequence of numbers (e.g. 100.00 101.00 99.00 100.50)
    const nums = s.split(/\s+/).map(x => parseNumber(x)).filter(n => n !== null);
    if (nums.length >= 4) {
      // Must look like prices and have OHLC labels nearby to avoid random lists
      const hasLabels = /[OHLC]\s*[:\s]?\s*\d/i.test(s) || /Open|High|Low|Close/i.test(s);
      const valid = nums.slice(0, 4).every(n => n > 1.0);
      if (valid && hasLabels) {
        return {
          open: nums[0],
          high: nums[1],
          low: nums[2],
          close: nums[3]
        };
      }
    }

    return null;
  }

  function extractOHLC(root = document) {
    const selectors = [
      '[data-name="legend-series-item"]',
      '[data-name="legend-source-item"]',
      '[class*="seriesValues"]',
      '[class*="valuesWrapper"]',
      '[class*="itemValues"]',
      '[class*="legend"]',
      '[class*="chart-toolbar"]'
    ];

    for (const selector of selectors) {
      const nodes = Array.from(root.querySelectorAll(selector)).slice(0, 30);

      for (const node of nodes) {
        const text = safeText(node);
        const ohlc = extractOHLCFromText(text);
        if (ohlc) return ohlc;
      }
    }

    // Fallback: try just getting all valueItem-like elements individually
    const valItems = root.querySelectorAll('[class*="valueItem"], [class*="itemValue"]');
    if (valItems.length) {
      const joined = Array.from(valItems).map(el => safeText(el)).join(' ');
      const ohlc = extractOHLCFromText(joined);
      if (ohlc) return ohlc;
      
      const nums = Array.from(valItems).map(el => parseNumber(safeText(el))).filter(n => n !== null && n > 10);
      if (nums.length >= 4) {
        return {
          open: nums[0],
          high: nums[1],
          low: nums[2],
          close: nums[3]
        };
      }
    }

    return extractOHLCFromText(safeText(root));
  }

  // ---------------------------------------------------------------------------
  // Storage writer
  // ---------------------------------------------------------------------------

  function saveTrend(symbol, timeframe, fallbackSentiment, ohlc = null) {
    if (!symbol) {
      debug('save_skipped_no_symbol');
      return;
    }

    const tf = normTF(timeframe);
    if (tf === '—') {
      debug('save_skipped_no_timeframe', { symbol, timeframe });
      return;
    }

    const cleanSymbol = cleanSymbolText(symbol);
    if (!cleanSymbol) {
      debug('save_skipped_bad_symbol', { symbol });
      return;
    }

    chrome.storage.local.get([STORAGE_KEY], (r) => {
      if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError);
      r = r || {};
      const chartData = r[STORAGE_KEY] || {};
      const existing = chartData?.[cleanSymbol]?.[tf] || {};

      let lastClosedOhlc = existing.last_closed_ohlc || null;
      let currentOhlc = ohlc || existing.ohlc || null;
      
      // Rollover detection: if Open changes, previous candle is closed
      if (existing.ohlc && ohlc && existing.ohlc.open !== ohlc.open) {
        lastClosedOhlc = existing.ohlc;
      }

      // 1. Determine base sentiment from legend/text
      let finalSentiment = fallbackSentiment;

      // 2. Override/Refine using OHLC (prioritize current candle for live bias)
      const refOhlc = ohlc || existing.ohlc || lastClosedOhlc;
      
      if (refOhlc && Number.isFinite(refOhlc.open) && Number.isFinite(refOhlc.close)) {
        if (refOhlc.close > refOhlc.open + 0.01) finalSentiment = 'BULLISH';
        else if (refOhlc.close < refOhlc.open - 0.01) finalSentiment = 'BEARISH';
        else finalSentiment = 'NEUTRAL';
      }

      // 3. Last resort fallback to legend-based sentiment if OHLC is missing
      if (!refOhlc && (!finalSentiment || finalSentiment === 'NEUTRAL')) {
        finalSentiment = fallbackSentiment;
      }

      if (!['BULLISH', 'BEARISH', 'NEUTRAL'].includes(finalSentiment)) {
        finalSentiment = 'NEUTRAL';
      }

      const existingComparable = JSON.stringify({
        sentiment: existing.sentiment || null,
        ohlc: existing.ohlc || null
      });

      const newComparable = JSON.stringify({
        sentiment: finalSentiment,
        ohlc: ohlc || null
      });

      const changed = existingComparable !== newComparable;
      const timestamp = nowIso();

      chartData[cleanSymbol] = chartData[cleanSymbol] || {};
      chartData[cleanSymbol][tf] = {
        ...existing,
        sentiment: finalSentiment,
        ohlc: ohlc || existing.ohlc || null,
        last_closed_ohlc: lastClosedOhlc,
        updated_at: timestamp,
        seen_at: timestamp,
        changed_at: changed ? timestamp : existing.changed_at || timestamp
      };

      chrome.storage.local.set(
        {
          [STORAGE_KEY]: chartData,
          [LAST_SYMBOL_KEY]: cleanSymbol
        },
        () => {
          lastSavedSerialized = JSON.stringify(chartData[cleanSymbol][tf]);
          debug('trend_saved', {
            symbol: cleanSymbol,
            timeframe: tf,
            sentiment: finalSentiment,
            changed,
            hasOhlc: !!ohlc
          });
        }
      );
    });
  }

  // ---------------------------------------------------------------------------
  // Core scraper
  // ---------------------------------------------------------------------------

  function getChartContainers() {
    let containers = Array.from(document.querySelectorAll('.chart-container'));
    if (containers.length > 0) return containers;
    
    containers = Array.from(document.querySelectorAll('td.chart-cell'));
    if (containers.length > 0) return containers;

    containers = Array.from(document.querySelectorAll('.chart-gui-wrapper'));
    if (containers.length > 0) return containers;

    return [document];
  }

  function scrape() {
    try {
      if (!chrome.runtime?.id) {
        if (scrapeInterval) clearInterval(scrapeInterval);
        return;
      }

      const containers = getChartContainers();
      let anySaved = false;

      for (const node of containers) {
        const text = safeText(node);
        
        const symbol = extractSymbol();
        if (!symbol) continue;

        let timeframe = findTimeframeFromPageText(text);
        if (timeframe === '—') timeframe = findTimeframeFromUrl() || '1h';
        if (timeframe === '—') timeframe = '1h';

        const legendSentiment = inferSentimentFromLegend(symbol, node);
        const bodySentiment = inferSentimentFromText(text);
        let sentiment = legendSentiment || bodySentiment || 'NEUTRAL';

        const ohlc = extractOHLC(node);

        saveTrend(symbol, timeframe, sentiment, ohlc);
        anySaved = true;
      }
      
      if (!anySaved) debug('no_symbol_found');

    } catch (e) {
      debug('scrape_error', {
        error: String(e?.message || e),
        stack: e?.stack ? String(e.stack).slice(0, 500) : ''
      });
    }
  }

  function forceScan(sendResponse) {
    scrape();
    if (typeof sendResponse === 'function') {
      sendResponse({ ok: true });
    }
  }

  // ---------------------------------------------------------------------------
  // Message listener
  // ---------------------------------------------------------------------------

  try {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (!chrome.runtime?.id) return false;

      if (msg?.type === 'FORCE_SCAN') {
        forceScan(sendResponse);
        return true;
      }

      if (msg?.type === 'PING') {
        sendResponse({ ok: true, script: 'tv_content.js' });
        return true;
      }

      return false;
    });
  } catch (_) {
    // Ignore listener registration errors
  }

  // ---------------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------------

  debug('init', {
    href: window.location.href,
    readyState: document.readyState
  });

  console.log('[NSEBOT] Fixed TV scraper initialized:', window.location.href);

  [1000, 3000, 6000].forEach((ms) => {
    setTimeout(() => {
      if (chrome.runtime?.id) scrape();
    }, ms);
  });

  scrapeInterval = setInterval(scrape, SCRAPE_INTERVAL_MS);
})();