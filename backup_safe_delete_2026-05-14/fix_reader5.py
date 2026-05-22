import re

with open('./chrome_extension/dhan_dom_reader.js', 'r', encoding='utf-8') as f:
    code = f.read()

spot_logic = """
  function resolveDhanMeta() {
    let symbol = 'UNKNOWN';
    let spot = NaN;
    
    const headings = Array.from(document.querySelectorAll('h1, h2, span, p'));
    
    for (const el of headings) {
        if (!el.isConnected) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;

        const text = el.textContent.trim().toUpperCase();
        const match = text.match(/([A-Z]{3,}(?:\s*50)?|\w+)(?:.*?)(?:\b|_)([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)/);
        if (match && /NIFTY|SENSEX|GAS|OIL|GOLD|SILVER|USD|EUR|TCS|RELIANCE|HDFC|INFY/.test(match[1])) {
             if (symbol === 'UNKNOWN' || symbol.length < match[1].length) {
                 symbol = match[1].replace(/\s+/g, '');
             }
             const num = parseFloat(match[2].replace(/,/g, ''));
             if (num > 0) spot = num;
        }
    }
    
    if (isNaN(spot) || symbol === 'UNKNOWN') {
      const titleMatch = document.title.toUpperCase().match(/([A-Z]+).*?([0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]{2})?)/);
      if (titleMatch) {
         if (symbol === 'UNKNOWN') symbol = titleMatch[1];
         if (isNaN(spot)) spot = parseFloat(titleMatch[2].replace(/,/g, ''));
      }
    }
    
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
"""

if 'function resolveDhanMeta' not in code:
    code = code.replace('function findOptionChainTable()', spot_logic + '\n  function findOptionChainTable()')

extract_logic = """
  function extractDhanOptionChainPayload() {
    const meta = typeof resolveDhanMeta === 'function' ? resolveDhanMeta() : {symbol: 'UNKNOWN', spot: NaN};
    const tableInfo = typeof findOptionChainTable === 'function' ? findOptionChainTable() : null;
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
        if (!row.strike) continue;
        const diff = Math.abs(row.strike - meta.spot);
        if (diff < minDiff) {
           minDiff = diff;
           closestStrike = row.strike;
        }
      }
    } else {
      // Find ATM by minimum premium difference
      let minPremiumDiff = Infinity;
      let atmStrike = null;
      for (const row of data) {
        if (row.ce && row.pe && typeof row.ce.ltp === 'number' && typeof row.pe.ltp === 'number') {
           const pDiff = Math.abs(row.ce.ltp - row.pe.ltp);
           if (pDiff < minPremiumDiff) {
               minPremiumDiff = pDiff;
               atmStrike = row.strike;
           }
        }
      }
      if (atmStrike !== null) closestStrike = atmStrike;
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
"""

start_idx = code.find('function extractDhanOptionChainPayload()')
end_idx = code.find('function parseOptionChainRows', start_idx)

if start_idx != -1 and end_idx != -1:
    old_extract = code[start_idx:end_idx]
    code = code.replace(old_extract, extract_logic + '\n\n  ')

with open('./chrome_extension/dhan_dom_reader.js', 'w', encoding='utf-8') as f:
    f.write(code)
