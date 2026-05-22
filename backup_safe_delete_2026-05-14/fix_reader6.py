import re

with open('./chrome_extension/dhan_dom_reader.js', 'r', encoding='utf-8') as f:
    code = f.read()

new_is_page = """
  function isDhanOptionChainPage() {
    const meta = typeof resolveDhanMeta === 'function' ? resolveDhanMeta() : {symbol: 'UNKNOWN'};
    if (meta.symbol !== 'UNKNOWN') return true;
    
    // Fallback logic
    const strikeOk = containsText(/\bstrike\s*price\b/i);
    const pcrOk = containsText(/\bpcr\b/i);
    return strikeOk || pcrOk;
  }
"""

code = re.sub(r'function isDhanOptionChainPage\(\)\s*\{[\s\S]*?return[^}]+;[^}]+\}', new_is_page.strip(), code, count=1)

new_payload = """
  function extractDhanOptionChainPayload(options) {
    const opts = options || {};
    const windowSize = Number.isInteger(opts.windowSize) && opts.windowSize >= 0 ? opts.windowSize : 15;

    if (!isDhanOptionChainPage()) {
      return buildEmptyPayload();
    }

    const meta = typeof resolveDhanMeta === 'function' ? resolveDhanMeta() : {symbol: 'UNKNOWN', spot: NaN};
    const tableInfo = findOptionChainTable();
    const columnMap = tableInfo ? buildColumnIndexMap(tableInfo) : null;
    let parsedRows = [];
    if (tableInfo && columnMap && columnMap.strikePriceIdx >= 0) {
      // our modified parseOptionChainRows takes table
      parsedRows = parseOptionChainRows(tableInfo.table, columnMap, tableInfo.headerRow);
    }
    
    const summary = extractSummaryMetrics(tableInfo ? tableInfo.table : null, parsedRows);
    summary.spotPrice = meta.spot; // override with parsed meta
    
    const atmIndex = determineAtmIndex(parsedRows, summary.spotPrice);
    const atmStrike = atmIndex >= 0 && parsedRows[atmIndex] ? cleanText(parsedRows[atmIndex].strikePrice) : '';
    const rows = applyAtmWindow(parsedRows, atmIndex, windowSize);

    return {
      success: true, 
      bot_version: "2.4.0",
      source: 'dhan_dom_reader',
      capturedAt: new Date().toISOString(),
      pageTitle: document.title || '',
      symbolDisplayName: meta.symbol,
      symbol: meta.symbol, // also raw symbol
      selectedExpiry: '',
      availableExpiries: [],
      summary,
      atmStrike,
      atmIndex,
      windowSize,
      rows,
    };
  }
"""

code = re.sub(r'function extractDhanOptionChainPayload\(options\)\s*\{[\s\S]*?return\s*\{[\s\S]*?\};\s*\}', new_payload.strip(), code, count=1)

with open('./chrome_extension/dhan_dom_reader.js', 'w', encoding='utf-8') as f:
    f.write(code)
