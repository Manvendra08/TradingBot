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

  function inferSentimentFromLegend(symbol) {
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
      const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 30);

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
    const s = String(text || '');

    // Common compact form: O 100 H 110 L 95 C 105
    const re1 =
      /(?:O|OPEN)\s*[: ]\s*([\d,.]+).*?(?:H|HIGH)\s*[: ]\s*([\d,.]+).*?(?:L|LOW)\s*[: ]\s*([\d,.]+).*?(?:C|CLOSE)\s*[: ]\s*([\d,.]+)/i;

    const m1 = s.match(re1);
    if (m1) {
      return {
        open: parseNumber(m1[1]),
        high: parseNumber(m1[2]),
        low: parseNumber(m1[3]),
        close: parseNumber(m1[4])
      };
    }

    // TradingView sometimes exposes O/H/L/C as separated labels.
    const o = s.match(/\bO(?:PEN)?\s*[: ]\s*([\d,.]+)/i);
    const h = s.match(/\bH(?:IGH)?\s*[: ]\s*([\d,.]+)/i);
    const l = s.match(/\bL(?:OW)?\s*[: ]\s*([\d,.]+)/i);
    const c = s.match(/\bC(?:LOSE)?\s*[: ]\s*([\d,.]+)/i);

    if (o || h || l || c) {
      return {
        open: o ? parseNumber(o[1]) : null,
        high: h ? parseNumber(h[1]) : null,
        low: l ? parseNumber(l[1]) : null,
        close: c ? parseNumber(c[1]) : null
      };
    }

    return null;
  }

  function extractOHLC() {
    const selectors = [
      '[data-name="legend-series-item"]',
      '[class*="seriesValues"]',
      '[class*="valuesWrapper"]',
      '[class*="legend"]'
    ];

    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 30);

      for (const node of nodes) {
        const text = safeText(node);
        const ohlc = extractOHLCFromText(text);
        if (ohlc) return ohlc;
      }
    }

    return extractOHLCFromText(getBodyText());
  }

  // ---------------------------------------------------------------------------
  // Storage writer
  // ---------------------------------------------------------------------------

  function saveTrend(symbol, timeframe, sentiment, ohlc = null) {
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

    const safeSentiment = ['BULLISH', 'BEARISH', 'NEUTRAL'].includes(sentiment)
      ? sentiment
      : 'NEUTRAL';

    const current = {
      symbol: cleanSymbol,
      timeframe: tf,
      sentiment: safeSentiment,
      ohlc: ohlc || null
    };

    const serialized = JSON.stringify(current);

    chrome.storage.local.get([STORAGE_KEY], (r) => {
      const chartData = r[STORAGE_KEY] || {};
      const existing = chartData?.[cleanSymbol]?.[tf] || {};

      const existingComparable = JSON.stringify({
        sentiment: existing.sentiment || null,
        ohlc: existing.ohlc || null
      });

      const newComparable = JSON.stringify({
        sentiment: safeSentiment,
        ohlc: ohlc || null
      });

      const changed = existingComparable !== newComparable;
      const timestamp = nowIso();

      chartData[cleanSymbol] = chartData[cleanSymbol] || {};
      chartData[cleanSymbol][tf] = {
        ...existing,
        sentiment: safeSentiment,
        ohlc: ohlc || existing.ohlc || null,
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
          lastSavedSerialized = serialized;
          debug('trend_saved', {
            symbol: cleanSymbol,
            timeframe: tf,
            sentiment: safeSentiment,
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

  function scrape() {
    try {
      if (!chrome.runtime?.id) {
        if (scrapeInterval) clearInterval(scrapeInterval);
        return;
      }

      const symbol = extractSymbol();

      if (!symbol) {
        debug('no_symbol_found');
        return;
      }

      const bodyText = getBodyText();

      let timeframe = findTimeframe(bodyText);

      // If timeframe cannot be read from UI/URL, default to 1h.
      // This avoids popup showing blank trend forever.
      if (timeframe === '—') {
        timeframe = '1h';
      }

      const legendSentiment = inferSentimentFromLegend(symbol);
      const bodySentiment = inferSentimentFromText(bodyText);
      const sentiment = legendSentiment || bodySentiment || 'NEUTRAL';

      const ohlc = extractOHLC();

      const nextSerialized = JSON.stringify({
        symbol: cleanSymbolText(symbol),
        timeframe,
        sentiment,
        ohlc
      });

      // Avoid excessive writes, but still update seen_at periodically.
      if (nextSerialized !== lastSavedSerialized) {
        saveTrend(symbol, timeframe, sentiment, ohlc);
      } else {
        saveTrend(symbol, timeframe, sentiment, ohlc);
      }
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