const fs = require('fs');
let code = fs.readFileSync('./chrome_extension/dhan_dom_reader.js', 'utf8');

const spotLogic = \
  function resolveDhanMeta() {
    let symbol = 'UNKNOWN';
    let spot = NaN;
    
    // Attempt 1: Search top nav elements for the symbol and spot price.
    // e.g. "NATURALGAS 276.10" or "NIFTY 50 22,100.50"
    const headings = Array.from(document.querySelectorAll('h1, h2, span, p'));
    
    for (const el of headings) {
        if (!el.isConnected) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;

        const text = el.textContent.trim().toUpperCase();
        // NIFTY, BANKNIFTY, FINNIFTY, SENSEX, CRUDEOIL, NATURALGAS etc. + possible number
        const match = text.match(/([A-Z]{3,}(?:\\s*50)?|\\w+)(?:.*?)(?:\\b|_)([0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]{2})?)/);
        if (match && /NIFTY|SENSEX|GAS|OIL|GOLD|SILVER|USD|EUR|TCS|RELIANCE|HDFC|INFY/.test(match[1])) {
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

if (!code.includes('resolveDhanMeta')) {
    code = code.replace('function findOptionChainTable()', spotLogic + '\\n\\n  function findOptionChainTable()');
}

fs.writeFileSync('./chrome_extension/dhan_dom_reader.js', code);
