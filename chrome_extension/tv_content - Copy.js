/**
 * tv_content.js — NSEBOT Chart Scraper v7
 * FIXED: Aggressive symbol splitting and stale data persistence.
 */
'use strict';

const SCRAPE_INTERVAL_MS = 15000;
const STORAGE_KEY = 'nsebot_chart_data';

function getSentiment(color) {
  if (!color) return 'NEUTRAL';
  const c = color.toLowerCase();
  if (c.includes('8, 153, 129') || c.includes('38, 166, 154') || c.includes('0, 150, 136')) return 'BULLISH';
  if (c.includes('242, 54, 69') || c.includes('239, 83, 80') || c.includes('255, 82, 82')) return 'BEARISH';
  return 'NEUTRAL';
}

function normTF(t) {
  if (!t) return '—';
  const s = t.toLowerCase().trim();
  if (s.includes('1h') || s.includes('1 h') || s === '60') return '1h';
  if (s.includes('3h') || s.includes('3 h') || s === '180') return '3h';
  if (s.includes('day') || s === 'd') return '1d';
  return s.replace(/\s+/g, ''); 
}

function parseSymbol(text) {
  if (!text || text.includes('TradingView')) return null;
  // 1. Split by TradingView separator or parenthesis change
  let clean = String(text).split(/[·(]/)[0].trim().toUpperCase();
  // 2. Aggressively strip trailing price data (e.g. "NATURALGAS MAY FUT 260.50")
  // Matches a space followed by a number with a decimal at the end of the line.
  clean = clean.replace(/\s\d+\.\d+$/, '').trim();
  return clean.length >= 2 ? clean : null;
}

function scrapeCharts() {
  const charts = document.querySelectorAll('.chart-container, .layout__area--center, .chart-markup-table');
  
  chrome.storage.local.get([STORAGE_KEY], (result) => {
    if (chrome.runtime.lastError) console.warn(chrome.runtime.lastError);
    result = result || {};
    const existingData = result[STORAGE_KEY] || {};
    let lastSymbol = parseSymbol(document.title);
    let updatedCount = 0;

    charts.forEach((chart) => {
      let symbol = lastSymbol;
      let timeframe = '—';
      const indicators = [];

      const seriesItem = chart.querySelector('[data-name="legend-series-item"], [class*="series-"]');
      let priceSentiment = 'NEUTRAL';

      if (seriesItem) {
        const titleEl = seriesItem.querySelector('[class*="title-"]');
        const priceValEl = seriesItem.querySelector('[class*="value-"]');
        
        if (priceValEl) priceSentiment = getSentiment(window.getComputedStyle(priceValEl).color);
        
        if (titleEl) {
          const text = titleEl.textContent.trim();
          if (text.includes('·')) {
            const parts = text.split('·').map(s => s.trim());
            symbol = parseSymbol(parts[0]) || symbol;
            timeframe = normTF(parts[1]);
          } else {
            symbol = parseSymbol(text) || symbol;
          }
        }
      }

      if (!symbol || timeframe === '—') return;

      const legendItems = chart.querySelectorAll('[class*="item-"]:not([class*="series-"])');
      legendItems.forEach(item => {
        const titleEl = item.querySelector('[class*="title-"]');
        const valEl   = item.querySelector('[class*="value-"]');
        if (titleEl && valEl) {
          const name = titleEl.textContent.trim();
          const value = valEl.textContent.trim();
          const sentiment = getSentiment(window.getComputedStyle(valEl).color);
          indicators.push({ name, value, sentiment });
        }
      });

      existingData[symbol] = existingData[symbol] || {};
      existingData[symbol][timeframe] = {
        indicators,
        sentiment: priceSentiment,
        updated_at: new Date().toISOString()
      };
      lastSymbol = symbol;
      updatedCount++;
    });

    if (updatedCount > 0) {
      chrome.storage.local.set({
        [STORAGE_KEY]: existingData,
        'last_chart_symbol': lastSymbol,
        'nsebot_site': 'dhan_chart'
      }, () => {
        const err = chrome.runtime.lastError;
        if (err) console.error(`[NSEBOT] Scraper write error: ${err.message}`);
        else console.log(`[NSEBOT] Updated ${updatedCount} chart panes for ${lastSymbol}`);
      });
    }
  });
}

// Initial triggers
setTimeout(scrapeCharts, 3000);
setInterval(scrapeCharts, SCRAPE_INTERVAL_MS);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'FORCE_SCAN') {
    console.log('[NSEBOT] Force scan signal received');
    scrapeCharts();
  }
  if (msg.type === 'PING') sendResponse({ pong: true });
});

console.log(`[NSEBOT] Scraper v7 active — Context: ${window.location.href}`);
