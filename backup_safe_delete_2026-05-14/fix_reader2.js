const fs = require('fs');
let code = fs.readFileSync('./chrome_extension/dhan_dom_reader.js', 'utf8');

const newSpotExtract = \
  function resolveDhanMeta() {
    let symbol = 'UNKNOWN';
    let spot = NaN;
    
    // Attempt 1: Search top nav elements for the symbol and spot price.
    // e.g. "NATURALGAS 276.10" or "NIFTY 50 22,100.50"
    const headings = Array.from(document.querySelectorAll('h1, h2, span, p')).filter(isElementVisible);
    
    for (const el of headings) {
        const text = el.textContent.trim().toUpperCase();
        // NIFTY, BANKNIFTY, FINNIFTY, SENSEX, CRUDEOIL, NATURALGAS etc. + possible number
        const match = text.match(/([A-Z]{4,}(?:\s*50)?|\w+)(?:.*?)(?:\\b|_)([0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]{2})?)/);
        if (match && /NIFTY|SENSEX|GAS|OIL|GOLD|SILVER|USD|EUR/.test(match[1])) {
             if (symbol === 'UNKNOWN' || symbol.length < match[1].length) {
                 symbol = match[1].replace(/\\s+/g, '');
             }
             const num = parseFloat(match[2].replace(/,/g, ''));
             if (num > 0) spot = num;
        }
    }
    
    // Attempt 2: Document title fallback
    if (isNaN(spot) || symbol === 'UNKNOWN') {
      const titleMatch = document.title.toUpperCase().match(/([A-Z]+).*?([0-9]{2,}(?:,[0-9]{3})*(?:\\.[0-9]{2})?)/);
      if (titleMatch) {
         if (symbol === 'UNKNOWN') symbol = titleMatch[1];
         if (isNaN(spot)) spot = parseFloat(titleMatch[2].replace(/,/g, ''));
      }
    }
    
    // Fallback based on URL
    if (symbol === 'UNKNOWN') {
      const url = window.location.href.toUpperCase();
      if (url.includes('BANKNIFTY')) symbol = 'BANKNIFTY';
      else if (url.includes('FINNIFTY')) symbol = 'FINNIFTY';
      else if (url.includes('MIDCPNIFTY')) symbol = 'MIDCPNIFTY';
      else if (url.includes('SENSEX')) symbol = 'SENSEX';
      else if (url.includes('NIFTY')) symbol = 'NIFTY';
      else if (url.includes('NATURALGAS')) symbol = 'NATURALGAS';
    }

    return { symbol, spot };
  }
\;

code = code.replace(/function findOptionChainTitleBlock[\s\S]*?function extractSymbolDisplayName[^\}]+}/, newSpotExtract.trim());

const extractPayloadStart = code.indexOf('function extractDhanOptionChainPayload()');
const extractPayloadEnd = code.indexOf('function parseOptionChainRows', extractPayloadStart);
const oldExtract = code.substring(extractPayloadStart, extractPayloadEnd);

const newExtractObj = \
  function extractDhanOptionChainPayload() {
    const meta = resolveDhanMeta();
    const tableInfo = findOptionChainTable();
    if (!tableInfo) {
      return { success: false, reason: 'could_not_find_table' };
    }

    const columnMap = buildColumnIndexMap(tableInfo);
    if (columnMap.strikePriceIdx === -1) {
      return { success: false, reason: 'could_not_find_strike_column' };
    }

    const data = parseOptionChainRows(tableInfo, columnMap);
    if (!data || data.length === 0) {
      return { success: false, reason: 'no_rows_extracted' };
    }

    let minDiff = Infinity;
    let closestStrike = meta.spot;
    if (!isNaN(meta.spot)) {
      for (const row of data) {
        const diff = Math.abs(row.strike - meta.spot);
        if (diff < minDiff) {
           minDiff = diff;
           closestStrike = row.strike;
        }
      }
    }

    return {
      success: true,
      bot_version: "2.4.0",
      source: 'dhan_dom_reader',
      symbol: meta.symbol,
      spot_price: meta.spot,
      closest_strike: closestStrike,
      data: data,
      timestamp: Date.now()
    };
  }
\;

code = code.replace(oldExtract, newExtractObj.trim() + '\\n\\n  ');
code = code.replace(/const titleBlock = findOptionChainTitleBlock(?:[\s\S]*?)const spotPrice = resolveSpotPrice[^\n]+;/, '');

fs.writeFileSync('./chrome_extension/dhan_dom_reader.js', code);
