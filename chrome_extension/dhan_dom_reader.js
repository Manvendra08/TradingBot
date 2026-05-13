/**
 * Read-only DOM extractor for Dhan Advance Option Chain.
 *
 * This module never clicks, types, scrolls, mutates DOM nodes, or changes page state.
 * It only reads visible text and returns a normalized payload.
 */
(function () {
  'use strict';

  const DEFAULT_WINDOW_SIZE = 15;
  const DEFAULT_DEBOUNCE_MS = 350;

  const MONTH_RE = /\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\b/i;
  const NUM_TOKEN_RE = /[-+]?\d[\d,]*\.?\d*/g;

  const HEADER_PATTERNS = {
    strike: [/^strike\s*price$/i, /^strike$/i],
    pcr: [/^pcr$/i, /\bpcr\b/i],
    ltpChange: [/^ltp\s*change$/i, /^change$/i, /^chg$/i, /ltp\s*chg/i, /chg\s*ltp/i],
    oi: [/^oi$/i, /open\s*interest/i, /\boi\b/i],
    volume: [/^vol(?:ume)?$/i, /\bvolume\b/i, /\bvol\b/i],
    delta: [/\bdelta\b/i],
    theta: [/\btheta\b/i],
    iv: [/^\biv\b$/i, /^impl.*vol/i, /\biv\b/i],
    ltp: [/^ltp$/i, /last\s*price/i, /\bltp\b/i],
  };

  function normalizeWhitespace(value) {
    return String(value || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[\t\r\n]+/g, ' ')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  function cleanText(value) {
    return normalizeWhitespace(value);
  }

  function isElementVisible(element) {
    if (!element) return false;
    if (element.closest('script,style,noscript,template')) return false;
    if (!element.isConnected) return false;
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return element.getClientRects().length > 0;
  }

  function getElementText(element) {
    if (!element) return '';
    return cleanText(element.innerText || element.textContent || '');
  }

  function asUniqueList(items) {
    const out = [];
    const seen = new Set();
    for (const item of items) {
      const value = cleanText(item);
      if (!value) continue;
      if (seen.has(value)) continue;
      seen.add(value);
      out.push(value);
    }
    return out;
  }

  function toNumberOrNaN(raw) {
    const text = cleanText(raw).replace(/,/g, '');
    const match = text.match(/[-+]?\d*\.?\d+/);
    if (!match) return Number.NaN;
    const value = Number(match[0]);
    return Number.isFinite(value) ? value : Number.NaN;
  }

  function isLikelyExpiryText(text) {
    const t = cleanText(text);
    if (!t || t.length > 30) return false;
    if (/option\s*chain/i.test(t)) return false;
    if (!MONTH_RE.test(t) && !/\d{1,2}[-/ ]\d{1,2}(?:[-/ ]\d{2,4})?/.test(t) && !/\d{1,2}\s+[A-Za-z]{3,9}/.test(t)) {
      return false;
    }
    return true;
  }

  function getNearestContainer(element, levels) {
    let node = element || null;
    for (let i = 0; i < levels && node && node.parentElement; i += 1) {
      node = node.parentElement;
    }
    return node || document.body;
  }

  function findOptionChainTitleBlock() {
    const searchInput = document.querySelector('input[aria-label="Search underlying"]');
    const candidates = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p,strong,label'));
    let best = null;

    for (const el of candidates) {
      if (!isElementVisible(el)) continue;
      const text = getElementText(el);
      if (!text || text.length > 140) continue;
      if (!/option\s*chain/i.test(text)) continue;

      let score = 0;
      if (/^H[1-4]$/.test(el.tagName)) score += 30;
      if (searchInput && getNearestContainer(el, 3).contains(searchInput)) score += 20;
      score += Math.max(0, 120 - text.length) * 0.1;

      if (!best || score > best.score) {
        best = { element: el, text, score };
      }
    }

    if (best) return best;

    return {
      element: null,
      text: cleanText(document.title || ''),
      score: 0,
    };
  }

  function extractSymbolDisplayName(pageTitle) {
    const title = cleanText(pageTitle);
    if (!title) return '';

    const withoutSuffix = cleanText(title.replace(/\boption\s*chain\b/i, ''));
    return withoutSuffix || title;
  }

  function collectExpiryElements(root) {
    const scope = root || document.body;
    const selector = 'button,[role="tab"],option,[aria-selected],[aria-pressed],li,span,div';
    const nodes = Array.from(scope.querySelectorAll(selector));

    const out = [];
    for (const el of nodes) {
      if (!isElementVisible(el)) continue;
      const text = getElementText(el);
      if (!isLikelyExpiryText(text)) continue;
      out.push({ element: el, text });
    }
    return out;
  }

  function extractExpiryInfo(titleBlockElement) {
    const localRoot = titleBlockElement ? getNearestContainer(titleBlockElement, 3) : document.body;
    const local = collectExpiryElements(localRoot);
    const global = collectExpiryElements(document.body);
    const all = [...local, ...global];

    const selected = all
      .filter(({ element }) => {
        if (!element) return false;
        const ariaSelected = String(element.getAttribute('aria-selected') || '').toLowerCase() === 'true';
        const ariaPressed = String(element.getAttribute('aria-pressed') || '').toLowerCase() === 'true';
        const selectedOption = element.matches('option[selected]');
        return ariaSelected || ariaPressed || selectedOption;
      })
      .map(({ text }) => text);

    const available = asUniqueList(all.map(({ text }) => text));

    return {
      selectedExpiry: asUniqueList(selected)[0] || available[0] || '',
      availableExpiries: available,
    };
  }

  function containsText(pattern) {
    if (!document.body) return false;
    const bodyText = cleanText(document.body.innerText || document.body.textContent || '');
    return pattern.test(bodyText);
  }

  function isDhanOptionChainPage() {
    const titleBlock = findOptionChainTitleBlock();
    const titleOk = document.title.toLowerCase().includes('option chain') || true;
    const strikeOk = containsText(/\bstrike\s*price\b/i);
    const pcrOk = containsText(/\bpcr\b/i);
    const searchOk = !!document.querySelector('input[aria-label*="Search underlying" i], input[placeholder*="Search underlying" i], [class*="search" i]');
    return titleOk && strikeOk && pcrOk && searchOk;
  }

  function extractInlineValue(text, labels) {
    const normalized = cleanText(text);
    if (!normalized) return '';

    for (const label of labels) {
      const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const inlineRe = new RegExp(`^${escaped}\\s*[:\\-]?\\s*(.+)$`, 'i');
      const match = normalized.match(inlineRe);
      if (match && cleanText(match[1])) {
        return cleanText(match[1]);
      }
    }

    return '';
  }

  function isMetricValue(text, labels) {
    const value = cleanText(text);
    if (!value) return false;

    const lower = value.toLowerCase();
    if (labels.some((label) => lower === label.toLowerCase())) return false;
    if (value.length > 42) return false;

    if (/[-+]?\d/.test(value)) return true;
    if (/%|\b(?:L|CR|K|M)\b/i.test(value)) return true;
    if (/^na$|^n\/a$|^--$|^-$|^nil$/i.test(value)) return true;

    return false;
  }

  function extractSiblingValue(labelElement, labels) {
    if (!labelElement || !labelElement.parentElement) return '';

    const children = Array.from(labelElement.parentElement.children);
    const idx = children.indexOf(labelElement);
    if (idx < 0) return '';

    const offsets = [1, -1, 2, -2, 3, -3];
    for (const offset of offsets) {
      const candidate = children[idx + offset];
      if (!candidate || !isElementVisible(candidate)) continue;
      const text = getElementText(candidate);
      if (!isMetricValue(text, labels)) continue;
      return text;
    }

    return '';
  }

  function findLabeledValue(labels, options) {
    const opts = options || {};
    const includeInTables = opts.includeInTables === true;
    const roots = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p,strong,label,li,td,th,button'));

    for (const element of roots) {
      if (!isElementVisible(element)) continue;
      if (!includeInTables && element.closest('table')) continue;

      const text = getElementText(element);
      if (!text || text.length > 90) continue;

      const matchesLabel = labels.some((label) => {
        const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp(`^${escaped}$`, 'i').test(text) || new RegExp(`^${escaped}\\b`, 'i').test(text);
      });

      if (!matchesLabel) continue;

      const inline = extractInlineValue(text, labels);
      if (inline) return inline;

      const siblingValue = extractSiblingValue(element, labels);
      if (siblingValue) return siblingValue;
    }

    return '';
  }

  
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
        const match = text.match(/([A-Z]{3,}(?:\s*50)?|\w+)(?:.*?)(?:|_)([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)/);
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

  function findOptionChainTable() {
    const tables = Array.from(document.querySelectorAll('table'));
    let best = null;

    for (const table of tables) {
      const headerRows = Array.from(table.querySelectorAll('thead tr, tr'));
      for (const row of headerRows) {
        const cells = Array.from(row.querySelectorAll('th,td'));
        if (!cells.length) continue;

        const headers = cells.map((cell, index) => ({
          index,
          text: cleanText(cell.innerText || cell.textContent || ''),
        }));

        const hasStrike = headers.some(({ text }) => /\bstrike\s*price\b/i.test(text));
        const hasPcr = headers.some(({ text }) => /\bpcr\b/i.test(text));
        if (!hasStrike || !hasPcr) continue;

        const bodyRows = table.querySelectorAll('tbody tr').length || table.querySelectorAll('tr').length;
        const score = headers.length * 3 + bodyRows;

        if (!best || score > best.score) {
          best = { table, headerRow: row, headers, score };
        }
      }
    }

    return best;
  }

  function headerMatches(patterns, text) {
    const value = cleanText(text);
    return patterns.some((re) => re.test(value));
  }

  function buildColumnIndexMap(headers) {
    const map = {
      strikePriceIdx: -1,
      pcrIdx: -1,
      ceLtpChangeIdx: -1,
      ceOiIdx: -1,
      ceVolumeIdx: -1,
      ceDeltaIdx: -1,
      ceThetaIdx: -1,
      ceIvIdx: -1,
      ceLtpIdx: -1,
      peLtpIdx: -1,
      peThetaIdx: -1,
      peDeltaIdx: -1,
      peVolumeIdx: -1,
      peIvIdx: -1,
      peOiIdx: -1,
      peLtpChangeIdx: -1,
    };

    const strike = headers.find((h) => headerMatches(HEADER_PATTERNS.strike, h.text));
    const pcr = headers.find((h) => headerMatches(HEADER_PATTERNS.pcr, h.text));

    map.strikePriceIdx = strike ? strike.index : -1;
    map.pcrIdx = pcr ? pcr.index : -1;

    if (map.strikePriceIdx < 0) return map;

    const left = headers.filter((h) => h.index < map.strikePriceIdx);
    const right = headers.filter((h) => h.index > map.strikePriceIdx && h.index !== map.pcrIdx);

    function assignByPattern(sideHeaders, patterns, excludeFn) {
      for (const item of sideHeaders) {
        const text = cleanText(item.text);
        if (excludeFn && excludeFn(text)) continue;
        if (headerMatches(patterns, text)) return item.index;
      }
      return -1;
    }

    map.ceLtpChangeIdx = assignByPattern(left, HEADER_PATTERNS.ltpChange);
    map.ceOiIdx = assignByPattern(left, HEADER_PATTERNS.oi);
    map.ceVolumeIdx = assignByPattern(left, HEADER_PATTERNS.volume);
    map.ceDeltaIdx = assignByPattern(left, HEADER_PATTERNS.delta);
    map.ceThetaIdx = assignByPattern(left, HEADER_PATTERNS.theta);
    map.ceIvIdx = assignByPattern(left, HEADER_PATTERNS.iv);
    map.ceLtpIdx = assignByPattern(left, HEADER_PATTERNS.ltp, (text) => /change|chg/i.test(text));

    map.peLtpChangeIdx = assignByPattern(right, HEADER_PATTERNS.ltpChange);
    map.peOiIdx = assignByPattern(right, HEADER_PATTERNS.oi);
    map.peVolumeIdx = assignByPattern(right, HEADER_PATTERNS.volume);
    map.peDeltaIdx = assignByPattern(right, HEADER_PATTERNS.delta);
    map.peThetaIdx = assignByPattern(right, HEADER_PATTERNS.theta);
    map.peIvIdx = assignByPattern(right, HEADER_PATTERNS.iv);
    map.peLtpIdx = assignByPattern(right, HEADER_PATTERNS.ltp, (text) => /change|chg/i.test(text));

    // Positional fallback only when header text mapping is incomplete.
    const leftIdx = left.map((h) => h.index);
    if (leftIdx.length > 0) {
      if (map.ceLtpIdx < 0) map.ceLtpIdx = leftIdx[leftIdx.length - 1];
      if (map.ceThetaIdx < 0 && leftIdx.length > 1) map.ceThetaIdx = leftIdx[leftIdx.length - 2];
      if (map.ceDeltaIdx < 0 && leftIdx.length > 2) map.ceDeltaIdx = leftIdx[leftIdx.length - 3];
      if (map.ceVolumeIdx < 0 && leftIdx.length > 3) map.ceVolumeIdx = leftIdx[leftIdx.length - 4];
      if (map.ceOiIdx < 0 && leftIdx.length > 4) map.ceOiIdx = leftIdx[leftIdx.length - 5];
      if (map.ceLtpChangeIdx < 0) map.ceLtpChangeIdx = leftIdx[0];
    }

    const rightIdx = right.map((h) => h.index);
    if (rightIdx.length > 0) {
      if (map.peLtpIdx < 0) map.peLtpIdx = rightIdx[0];
      if (map.peThetaIdx < 0 && rightIdx.length > 1) map.peThetaIdx = rightIdx[1];
      if (map.peDeltaIdx < 0 && rightIdx.length > 2) map.peDeltaIdx = rightIdx[2];
      if (map.peVolumeIdx < 0 && rightIdx.length > 3) map.peVolumeIdx = rightIdx[3];
      if (map.peOiIdx < 0 && rightIdx.length > 4) map.peOiIdx = rightIdx[4];
      if (map.peLtpChangeIdx < 0) map.peLtpChangeIdx = rightIdx[rightIdx.length - 1];
    }

    return map;
  }

  function readCellText(cells, idx) {
    if (!Array.isArray(cells)) return '';
    if (!Number.isInteger(idx) || idx < 0 || idx >= cells.length) return '';
    return cleanText(cells[idx].innerText || cells[idx].textContent || '');
  }

  function parseOptionChainRows(table, columnMap, headerRow) {
    if (!table || !columnMap || columnMap.strikePriceIdx < 0) return [];

    const allRows = Array.from(table.querySelectorAll('tbody tr')).length
      ? Array.from(table.querySelectorAll('tbody tr'))
      : Array.from(table.querySelectorAll('tr'));

    const parsed = [];

    for (const row of allRows) {
      if (headerRow && row === headerRow) continue;
      if (row.querySelectorAll('th').length) continue;

      const cells = Array.from(row.querySelectorAll('td'));
      if (cells.length < 3) continue;

      const strikePrice = readCellText(cells, columnMap.strikePriceIdx);
      const strikeNumber = toNumberOrNaN(strikePrice);
      if (!Number.isFinite(strikeNumber)) continue;

      parsed.push({
        strikePrice,
        ceLtpChangeText: readCellText(cells, columnMap.ceLtpChangeIdx),
        ceOi: readCellText(cells, columnMap.ceOiIdx),
        ceVolume: readCellText(cells, columnMap.ceVolumeIdx),
        ceDelta: readCellText(cells, columnMap.ceDeltaIdx),
        ceTheta: readCellText(cells, columnMap.ceThetaIdx),
        ceIv: readCellText(cells, columnMap.ceIvIdx),
        ceLtp: readCellText(cells, columnMap.ceLtpIdx),
        pcr: readCellText(cells, columnMap.pcrIdx),
        peLtp: readCellText(cells, columnMap.peLtpIdx),
        peTheta: readCellText(cells, columnMap.peThetaIdx),
        peDelta: readCellText(cells, columnMap.peDeltaIdx),
        peVolume: readCellText(cells, columnMap.peVolumeIdx),
        peIv: readCellText(cells, columnMap.peIvIdx),
        peOi: readCellText(cells, columnMap.peOiIdx),
        peLtpChangeText: readCellText(cells, columnMap.peLtpChangeIdx),
        _strikeNumber: strikeNumber,
      });
    }

    return parsed;
  }

  function medianStep(strikeNumbers) {
    if (!strikeNumbers || strikeNumbers.length < 2) return 0;
    const sorted = [...new Set(strikeNumbers)].sort((a, b) => a - b);
    const diffs = [];
    for (let i = 1; i < sorted.length; i += 1) {
      const diff = sorted[i] - sorted[i - 1];
      if (Number.isFinite(diff) && diff > 0) diffs.push(diff);
    }
    if (!diffs.length) return 0;
    diffs.sort((a, b) => a - b);
    return diffs[Math.floor(diffs.length / 2)] || 0;
  }

  function extractSpotPriceNearStrikeLadder(table, parsedRows) {
    const strikes = parsedRows
      .map((row) => row._strikeNumber)
      .filter((value) => Number.isFinite(value));

    if (!strikes.length) return '';

    const minStrike = Math.min(...strikes);
    const maxStrike = Math.max(...strikes);
    const center = (minStrike + maxStrike) / 2;
    const step = medianStep(strikes);
    const tolerance = Math.max(25, step * 5, (maxStrike - minStrike) * 0.35);

    const root = getNearestContainer(table, 3);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);

    let best = null;

    while (walker.nextNode()) {
      const textNode = walker.currentNode;
      const parent = textNode && textNode.parentElement;
      if (!parent || !isElementVisible(parent)) continue;
      if (parent.closest('script,style,noscript,template')) continue;
      if (table && parent.closest('table') === table) continue;

      const rawText = cleanText(textNode.nodeValue || '');
      if (!rawText || rawText.length > 80) continue;

      const tokens = rawText.match(NUM_TOKEN_RE) || [];
      for (const token of tokens) {
        const num = toNumberOrNaN(token);
        if (!Number.isFinite(num)) continue;
        if (num < (minStrike - tolerance) || num > (maxStrike + tolerance)) continue;

        let score = 0;
        if (/\./.test(token)) score += 2;
        if (cleanText(parent.innerText || parent.textContent || '').match(/\b(?:spot|ltp|underlying|price)\b/i)) score += 4;
        if (cleanText(rawText) === cleanText(token)) score += 1;

        const dist = Math.abs(num - center);
        const denom = Math.max(1, maxStrike - minStrike);
        score += (1 - Math.min(1, dist / denom));

        if (!best || score > best.score) {
          best = { token: cleanText(token), score };
        }
      }
    }

    return best ? best.token : '';
  }

  function resolveSpotPrice(table, parsedRows) {
    const fromLabel = findLabeledValue(['Spot Price', 'Spot', 'LTP', 'Underlying'], { includeInTables: false });
    if (fromLabel) return fromLabel;
    return extractSpotPriceNearStrikeLadder(table, parsedRows);
  }

  function extractSummaryMetrics(table, parsedRows) {
    const spotPrice = resolveSpotPrice(table, parsedRows);
    const atmIv = findLabeledValue(['ATM IV'], { includeInTables: false });
    const ivChangePct = findLabeledValue(['IV Change %', 'IV Change'], { includeInTables: false });
    const daysForExpiry = findLabeledValue(['Days for Expiry'], { includeInTables: false });
    const marketLot = findLabeledValue(['Market Lot'], { includeInTables: false });
    const pcr = findLabeledValue(['PCR'], { includeInTables: false });
    const maxPain = findLabeledValue(['Max Pain'], { includeInTables: false });

    let resolvedPcr = pcr;
    if (!resolvedPcr && parsedRows.length > 0) {
      const pcrFromRows = parsedRows.map((row) => row.pcr).find((value) => cleanText(value));
      resolvedPcr = cleanText(pcrFromRows || '');
    }

    return {
      spotPrice: cleanText(spotPrice),
      atmIv: cleanText(atmIv),
      ivChangePct: cleanText(ivChangePct),
      daysForExpiry: cleanText(daysForExpiry),
      marketLot: cleanText(marketLot),
      pcr: cleanText(resolvedPcr),
      maxPain: cleanText(maxPain),
    };
  }

  function determineAtmIndex(parsedRows, spotPriceRaw) {
    if (!parsedRows.length) return -1;

    const spot = toNumberOrNaN(spotPriceRaw);
    if (!Number.isFinite(spot)) {
      return Math.floor(parsedRows.length / 2);
    }

    let bestIdx = 0;
    let bestDiff = Number.POSITIVE_INFINITY;

    for (let i = 0; i < parsedRows.length; i += 1) {
      const strike = parsedRows[i]._strikeNumber;
      if (!Number.isFinite(strike)) continue;
      const diff = Math.abs(strike - spot);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = i;
      }
    }

    return bestIdx;
  }

  function applyAtmWindow(parsedRows, atmIndex, windowSize) {
    if (!parsedRows.length || atmIndex < 0) return [];

    const from = Math.max(0, atmIndex - windowSize);
    const to = Math.min(parsedRows.length - 1, atmIndex + windowSize);

    return parsedRows.slice(from, to + 1).map((row) => ({
      strikePrice: row.strikePrice,
      ceLtpChangeText: row.ceLtpChangeText,
      ceOi: row.ceOi,
      ceVolume: row.ceVolume,
      ceDelta: row.ceDelta,
      ceTheta: row.ceTheta,
      ceIv: row.ceIv,
      ceLtp: row.ceLtp,
      pcr: row.pcr,
      peLtp: row.peLtp,
      peTheta: row.peTheta,
      peDelta: row.peDelta,
      peVolume: row.peVolume,
      peIv: row.peIv,
      peOi: row.peOi,
      peLtpChangeText: row.peLtpChangeText,
    }));
  }

  function buildEmptyPayload() {
    return {
      capturedAt: new Date().toISOString(),
      pageTitle: '',
      symbolDisplayName: '',
      selectedExpiry: '',
      availableExpiries: [],
      summary: {
        spotPrice: '',
        atmIv: '',
        ivChangePct: '',
        daysForExpiry: '',
        marketLot: '',
        pcr: '',
        maxPain: '',
      },
      atmStrike: '',
      atmIndex: -1,
      windowSize: DEFAULT_WINDOW_SIZE,
      rows: [],
    };
  }

  function extractDhanOptionChainPayload(options) {
    const opts = options || {};
    const windowSize = Number.isInteger(opts.windowSize) && opts.windowSize >= 0
      ? opts.windowSize
      : DEFAULT_WINDOW_SIZE;

    const payload = buildEmptyPayload();
    payload.windowSize = windowSize;

    if (!isDhanOptionChainPage()) {
      return payload;
    }

    const titleBlock = findOptionChainTitleBlock();
    const pageTitle = cleanText(titleBlock.text || document.title || '');

    const expiryInfo = extractExpiryInfo(titleBlock.element);

    const tableInfo = findOptionChainTable();
    const columnMap = tableInfo ? buildColumnIndexMap(tableInfo.headers) : null;
    const parsedRows = tableInfo && columnMap
      ? parseOptionChainRows(tableInfo.table, columnMap, tableInfo.headerRow)
      : [];

    const summary = extractSummaryMetrics(tableInfo ? tableInfo.table : null, parsedRows);
    const atmIndex = determineAtmIndex(parsedRows, summary.spotPrice);
    const atmStrike = atmIndex >= 0 && parsedRows[atmIndex] ? cleanText(parsedRows[atmIndex].strikePrice) : '';
    const rows = applyAtmWindow(parsedRows, atmIndex, windowSize);

    return {
      capturedAt: new Date().toISOString(),
      pageTitle,
      symbolDisplayName: extractSymbolDisplayName(pageTitle),
      selectedExpiry: expiryInfo.selectedExpiry,
      availableExpiries: expiryInfo.availableExpiries,
      summary,
      atmStrike,
      atmIndex,
      windowSize,
      rows,
    };
  }

  function clampDebounceMs(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return DEFAULT_DEBOUNCE_MS;
    return Math.max(300, Math.min(500, Math.round(num)));
  }

  function createReadOnlyMonitor(config) {
    const cfg = config || {};
    const debounceMs = clampDebounceMs(cfg.debounceMs);
    const onPayload = typeof cfg.onPayload === 'function' ? cfg.onPayload : null;
    const extractionOptions = cfg.extractionOptions || {};

    let observer = null;
    let timerId = null;
    let lastPayload = null;

    function readNow() {
      lastPayload = extractDhanOptionChainPayload(extractionOptions);
      if (onPayload) onPayload(lastPayload);
      return lastPayload;
    }

    function scheduleRead() {
      if (timerId) {
        clearTimeout(timerId);
      }
      timerId = setTimeout(() => {
        timerId = null;
        readNow();
      }, debounceMs);
    }

    function start() {
      if (observer) return;
      const root = document.documentElement || document.body;
      if (!root) return;

      observer = new MutationObserver(() => {
        scheduleRead();
      });

      observer.observe(root, {
        childList: true,
        subtree: true,
        characterData: true,
      });

      readNow();
    }

    function stop() {
      if (observer) {
        observer.disconnect();
        observer = null;
      }
      if (timerId) {
        clearTimeout(timerId);
        timerId = null;
      }
    }

    function getLastPayload() {
      return lastPayload;
    }

    return {
      start,
      stop,
      readNow,
      getLastPayload,
      debounceMs,
    };
  }

  window.NSEBOT_DHAN_DOM_READER = Object.freeze({
    isDhanOptionChainPage,
    extractDhanOptionChainPayload,
    createReadOnlyMonitor,
  });
})();
