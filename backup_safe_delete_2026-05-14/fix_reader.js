const fs = require('fs');
let code = fs.readFileSync('./chrome_extension/dhan_dom_reader.js', 'utf8');

code = code.replace(/return element\.getClientRects\(\)\.length > 0;/g, 'const rect = element.getBoundingClientRect(); return rect.width > 0 || rect.height > 0;');

const newFindTable = \
  function findOptionChainTable() {
    const tables = Array.from(document.querySelectorAll('table'));
    let best = null;

    for (const table of tables) {
      if (!isElementVisible(table)) continue;
      const dataRows = Array.from(table.querySelectorAll('tbody tr'));
      if (dataRows.length < 5) continue;
      
      const cellCount = dataRows[0].querySelectorAll('td').length;
      if (cellCount < 7) continue;

      const score = dataRows.length * cellCount;
      if (!best || score > best.score) {
        best = { table, score, cellCount, dataRows };
      }
    }

    return best ? { table: best.table, headers: best.table.querySelectorAll('th'), cellCount: best.cellCount, _dhanRows: best.dataRows } : null;
  }
\;
code = code.replace(/function findOptionChainTable\\(\\) \\{[\\s\\S]*?return best;\\s*\\}/, newFindTable.trim());

const newBuildMap = \
  function buildColumnIndexMap(tableInfo) {
    const cellCount = tableInfo.cellCount;
    let strikeCol = Math.floor(cellCount / 2);
    let pcrCol = -1;
    let ceLtpCol = -1, ceOiCol = -1, ceVolCol = -1, ceChgCol = -1, ceDeltaCol = -1, ceThetaCol = -1;
    let peLtpCol = -1, peOiCol = -1, peVolCol = -1, peChgCol = -1, peDeltaCol = -1, peThetaCol = -1;

    const rows = tableInfo._dhanRows.slice(0, 20);
    let bestScore = -Infinity;
    
    for (let c = 0; c < cellCount; c++) {
      const vals = rows.map(r => toNumberOrNaN(readCellText(Array.from(r.querySelectorAll('td')), c))).filter(v => Number.isFinite(v) && v > 0);
      if (vals.length < 3) continue;
      
      let nonDec = 0;
      for (let i = 1; i < vals.length; i++) if (vals[i] >= vals[i - 1]) nonDec++;
      const trend = vals.length > 1 ? (nonDec / (vals.length - 1)) : 0;
      if (trend < 0.6) continue;
      
      const unique = new Set(vals).size;
      const spread = Math.max(...vals) - Math.min(...vals);
      const score = trend * 2 + (unique / vals.length) + Math.min(1, spread / 100) - Math.abs(c - Math.floor(cellCount/2));
      if (score > bestScore) {
        bestScore = score;
        strikeCol = c;
      }
    }
    
    try {
        const pcrLookahead = readCellText(Array.from(rows[0].querySelectorAll('td')), strikeCol + 1);
        const hasPcr = (cellCount >= 14 && toNumberOrNaN(pcrLookahead) >= 0 && toNumberOrNaN(pcrLookahead) < 10) || false;
        pcrCol = hasPcr ? strikeCol + 1 : -1;
        
        let ceCursor = strikeCol - 1;
        if (ceCursor >= 0 && !rows[0].querySelectorAll('td')[ceCursor].textContent.trim().match(/\\d/)) {
            ceCursor--;
        }
        ceLtpCol = ceCursor--;
        if (ceCursor >= 0 && cellCount >= 12) ceThetaCol = ceCursor--;
        if (ceCursor >= 0 && cellCount >= 12) ceDeltaCol = ceCursor--;
        if (ceCursor >= 0) ceVolCol = ceCursor--;
        if (ceCursor >= 0) ceOiCol = ceCursor--;
        if (ceCursor >= 0) ceChgCol = ceCursor--;
        
        let peCursor = pcrCol !== -1 ? pcrCol + 1 : strikeCol + 1;
        if (peCursor < cellCount && !rows[0].querySelectorAll('td')[peCursor].textContent.trim().match(/\\d/)) {
            peCursor++;
        }
        peLtpCol = peCursor++;
        if (peCursor < cellCount && cellCount >= 12) peThetaCol = peCursor++;
        if (peCursor < cellCount && cellCount >= 12) peDeltaCol = peCursor++;
        if (peCursor < cellCount) peVolCol = peCursor++;
        if (peCursor < cellCount) peOiCol = peCursor++;
        if (peCursor < cellCount) peChgCol = peCursor++;
    } catch(e) {}

    return {
      strikePriceIdx: strikeCol, pcrIdx: pcrCol,
      ceLtpIdx: Math.max(0, ceLtpCol), ceOiIdx: Math.max(0, ceOiCol), ceVolumeIdx: Math.max(0, ceVolCol),
      ceLtpChangeIdx: Math.max(0, ceChgCol), ceDeltaIdx: ceDeltaCol, ceThetaIdx: ceThetaCol,
      peLtpIdx: Math.max(0, peLtpCol), peOiIdx: peOiCol !== -1 ? peOiCol : cellCount - 1, peVolumeIdx: peVolCol !== -1 ? peVolCol : cellCount - 2,
      peLtpChangeIdx: peChgCol !== -1 ? peChgCol : cellCount - 1, peDeltaIdx: peDeltaCol, peThetaIdx: peThetaCol
    };
  }
\;

code = code.replace(/function buildColumnIndexMap\\(headers\\) \\{[\\s\\S]*?return map;\\s*\\}/, newBuildMap.trim());
code = code.replace(/buildColumnIndexMap\\(tableInfo\\.headers\\)/g, 'buildColumnIndexMap(tableInfo)');

fs.writeFileSync('./chrome_extension/dhan_dom_reader.js', code);
