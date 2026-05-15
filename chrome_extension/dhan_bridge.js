/**
 * dhan_bridge.js — runs in MAIN world (injected via <script> tag)
 * Intercepts fetch/XHR for Dhan's option chain API calls.
 * Posts results back to content script via window.postMessage.
 *
 * Dhan advanceoptionchain API endpoint pattern:
 *   POST https://api.dhan.co/v2/optionchain
 *   POST https://options-trader.dhan.co/...
 *
 * Response shape:
 * {
 *   data: {
 *     last_price: 24500,
 *     oc_data: {
 *       "2025-06-26": [
 *         { strike_price: 24000, CE: { last_price, oi, oi_change, volume, implied_volatility, bid_price, ask_price }, PE: {...} },
 *         ...
 *       ],
 *       "2025-07-03": [...]
 *     }
 *   }
 * }
 */

(function() {
  'use strict';

  const BRIDGE_SOURCE = 'nsebot-dhan-bridge';
  const DHAN_OC_PATTERNS = [
    '/optionchain',
    '/v2/optionchain',
    '/option-chain',
    'optionchain',
    '/option_chain',
    'advoptionchain',
    'advanceoptionchain',
  ];

  function isDhanOCUrl(url) {
    const u = (url || '').toLowerCase();
    if (u.includes('.svg') || u.includes('.png') || u.includes('.js') || u.includes('.css')) return false;
    return DHAN_OC_PATTERNS.some(p => u.includes(p));
  }

  function post(type, payload) {
    window.postMessage({ source: BRIDGE_SOURCE, type, payload }, '*');
  }

  function parseExpiry(ocDataKeys) {
    if (!ocDataKeys || !ocDataKeys.length) return '';
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const valid = ocDataKeys
      .filter(k => /^\d{4}-\d{2}-\d{2}$/.test(k))
      .sort();
    return valid.find(k => new Date(k) >= today) || valid[0] || ocDataKeys[0] || '';
  }

  function toPositiveNumber(v) {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }

  function firstPositive(...vals) {
    for (const v of vals) {
      const n = toPositiveNumber(v);
      if (n > 0) return n;
    }
    return 0;
  }

  function sanitizeSymbol(raw) {
    if (!raw) return '';
    const s = String(raw)
      .toUpperCase()
      .replace(/OPTION\s*CHAIN/gi, ' ')
      .replace(/\b(ADVANCE|ADVANCED|VIEW|PAGE|EXPIRY|CALL|PUT|CE|PE)\b/gi, ' ')
      .replace(/[^A-Z0-9& \-]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    if (!s || /^\d+$/.test(s)) return '';
    if (s.length < 2 || s.length > 24) return '';
    if (/^(UNKNOWN|NA|N\/A|NULL|UNDEFINED)$/.test(s)) return '';
    return s;
  }

  function extractSymbolFromResponse(payload) {
    if (!payload || typeof payload !== 'object') return '';

    const direct = [
      payload.symbol,
      payload.underlying,
      payload.underlyingSymbol,
      payload.underlying_name,
      payload.underlyingName,
      payload.scrip,
      payload.scripName,
      payload.security,
      payload.securityName,
      payload.instrument,
      payload.instrumentName,
      payload.tradingSymbol,
      payload.tradingsymbol,
      payload.displaySymbol,
      payload.displayName,
      payload.name,
    ];
    for (const d of direct) {
      const sym = sanitizeSymbol(d);
      if (sym) return sym;
    }

    // Light recursive scan for symbol-like string values in nested objects.
    const queue = [payload];
    let depth = 0;
    while (queue.length && depth < 5) {
      const next = [];
      for (const node of queue) {
        if (!node || typeof node !== 'object') continue;
        for (const [k, v] of Object.entries(node)) {
          if (typeof v === 'string' && /(symbol|underlying|scrip|security|instrument|trading|name)/i.test(k)) {
            const sym = sanitizeSymbol(v);
            if (sym) return sym;
          } else if (v && typeof v === 'object') {
            next.push(v);
          }
        }
      }
      queue.splice(0, queue.length, ...next);
      depth += 1;
    }

    return '';
  }

  function inferUnderlyingFromRows(strikes) {
    if (!Array.isArray(strikes) || !strikes.length) return 0;
    const byStrike = new Map();
    strikes.forEach(r => {
      const s = Number(r?.strike);
      if (!Number.isFinite(s) || s <= 0) return;
      const oi = Math.max(0, Number(r?.oi) || 0);
      byStrike.set(s, (byStrike.get(s) || 0) + oi);
    });
    if (!byStrike.size) return 0;

    let bestStrike = 0;
    let bestOi = -1;
    byStrike.forEach((oi, strike) => {
      if (oi > bestOi) {
        bestOi = oi;
        bestStrike = strike;
      }
    });
    return bestStrike > 0 ? bestStrike : 0;
  }

  function extractSymbolFromBody(body) {
    if (!body) return '';
    try {
      const b = typeof body === 'string' ? JSON.parse(body) : body;
      // Dhan API may send numeric UnderlyingScrip plus optional text symbol.
      const raw =
        b.symbol || b.scrip || b.underlying || b.underlyingSymbol || b.securityName ||
        b.tradingSymbol || b.tradingsymbol || b.UnderlyingScrip || b.underlyingScrip || '';
      return sanitizeSymbol(raw);
    } catch (_) { return ''; }
  }

  function parseOCResponse(json, requestBody) {
    try {
      const bodySymbol = extractSymbolFromBody(requestBody);

      // Dhan wraps data inside .data
      const data = json.data || json;
      if (!data) return null;

      let underlying = firstPositive(
        data.last_price,
        data.lastPrice,
        data.underlyingValue,
        data.underlying_price,
        data.underlyingPrice,
        data.spotPrice,
        data.spot_price,
        data.ltp,
        data.indexLtp,
        data.underlying_data?.last_price,
        data.underlying_data?.lastPrice,
        data.underlying_data?.ltp,
        data.underlying?.last_price,
        data.underlying?.lastPrice,
        data.underlying?.ltp,
        data.underlying?.price,
        json.last_price,
        json.lastPrice,
        json.ltp,
      );

      // Detect nearest expiry from oc_data keys
      const ocData = data.oc_data;
      let expiry = '';
      let rawRows = [];

      if (ocData && typeof ocData === 'object' && !Array.isArray(ocData)) {
        // Object keyed by expiry date: { "2025-04-23": [...] }
        expiry  = parseExpiry(Object.keys(ocData));
        const val = ocData[expiry];
        rawRows = Array.isArray(val) ? val : [];
        // Some Dhan formats: { "2025-04-23": { CE: [...], PE: [...] } }
        if (!rawRows.length && val && typeof val === 'object') {
          const ceArr = val.CE || val.ce || [];
          const peArr = val.PE || val.pe || [];
          // Merge CE/PE arrays into rows with option type tag
          if (Array.isArray(ceArr) || Array.isArray(peArr)) {
            rawRows = [
              ...ceArr.map(r => ({ ...r, _ot: 'CE' })),
              ...peArr.map(r => ({ ...r, _ot: 'PE' })),
            ];
          }
        }
        post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge: oc_data expiries=[${Object.keys(ocData).join(',')}] using expiry=${expiry} rows=${rawRows.length}`, ok: true });
      } else if (Array.isArray(ocData)) {
        // oc_data is flat array: [{strike_price, CE:{}, PE:{}}, ...]
        rawRows = ocData;
        expiry  = parseExpiry(rawRows.map(r => r.expiry || r.expiryDate || '').filter(Boolean)) ||
                  parseExpiry(Object.keys(data).filter(k => /^\d{4}-\d{2}-\d{2}$/.test(k)));
        post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge: oc_data array rows=${rawRows.length}`, ok: true });
      } else if (Array.isArray(data.strikes)) {
        rawRows = data.strikes;
      } else if (Array.isArray(data)) {
        rawRows = data;
      } else {
        // Try to find the array with CE/PE structure
        for (const key of Object.keys(json)) {
          const val = json[key];
          if (Array.isArray(val) && val.length > 0) {
            const s = val[0];
            if (s && (s.CE || s.PE || s.strike_price || s.strikePrice)) {
              rawRows = val;
              break;
            }
          }
        }
      }

      if (!rawRows.length) {
        post('NSEBOT_DHAN_DIAG', { msg: 'Dhan bridge: no rawRows found in response', ok: false, warn: true });
        return null;
      }

      const strikes = [];
      rawRows.forEach(item => {
        const strikeVal = parseFloat(
          item.strike_price || item.strikePrice || item.strike || item.strikeValue || item.sp || 0
        );
        if (!strikeVal || isNaN(strikeVal)) return;

        // Flat row with _ot tag (from CE/PE array merge)
        if (item._ot) {
          const ot = item._ot;
          const oi = parseInt(item.oi || item.openInterest || item.open_interest || 0) || 0;
          strikes.push({
            strike:      strikeVal,
            option_type: ot,
            ltp:         parseFloat(item.last_price || item.lastPrice || item.ltp || 0) || 0,
            oi:          Math.abs(oi),
            oi_change:   parseInt(item.oi_change || item.changeinOpenInterest || 0) || 0,
            volume:      parseInt(item.volume || item.totalTradedVolume || 0) || 0,
            iv:          parseFloat(item.implied_volatility || item.impliedVolatility || item.iv || 0) || 0,
            bid:         parseFloat(item.bid_price || item.bidPrice || 0) || 0,
            ask:         parseFloat(item.ask_price || item.askPrice || 0) || 0,
          });
          return;
        }

        ['CE', 'PE'].forEach(ot => {
          const opt = item[ot] || item[ot.toLowerCase()];
          if (!opt || typeof opt !== 'object') return;

          const oi = parseInt(opt.oi || opt.openInterest || 0) || 0;
          const oiChange = parseInt(opt.oi_change || opt.changeinOpenInterest || 0) || 0;

          strikes.push({
            strike:      strikeVal,
            option_type: ot,
            ltp:         parseFloat(opt.last_price || opt.lastPrice || opt.ltp || 0) || 0,
            oi:          Math.abs(oi),   // always positive
            oi_change:   oiChange,
            volume:      parseInt(opt.volume || opt.totalTradedVolume || 0) || 0,
            iv:          parseFloat(opt.implied_volatility || opt.impliedVolatility || opt.iv || 0) || 0,
            bid:         parseFloat(opt.bid_price || opt.bidPrice || 0) || 0,
            ask:         parseFloat(opt.ask_price || opt.askPrice || 0) || 0,
          });
        });
      });

      if (!strikes.length) {
        post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge: rawRows=${rawRows.length} but 0 parsed strikes`, ok: false, warn: true });
        return null;
      }

      if (!(underlying > 0)) {
        underlying = firstPositive(inferUnderlyingFromRows(strikes));
      }

      const symbol = extractSymbolFromResponse(data) || extractSymbolFromResponse(json) || bodySymbol || '';

      // Compute PCR and max pain from full chain
      let ceOiTotal = 0, peOiTotal = 0;
      strikes.forEach(r => {
        if (r.option_type === 'CE') ceOiTotal += r.oi;
        else peOiTotal += r.oi;
      });
      const pcr = ceOiTotal > 0 ? peOiTotal / ceOiTotal : null;

      post('NSEBOT_DHAN_DIAG', {
        msg: `Dhan bridge parsed: symbol=${symbol || 'NA'} spot=${underlying || 0} rows=${strikes.length}`,
        ok: true,
      });

      return {
        underlying,
        expiry,
        symbol,
        bodySymbol,
        strikes,
        summary: {
          source:   'dhan_api',
          ceOi:     ceOiTotal,
          peOi:     peOiTotal,
          pcr:      pcr,
          maxPain:  null,   // computed on content-script side for perf
        },
      };
    } catch (e) {
      post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge parseOCResponse error: ${e.message}`, ok: false });
      return null;
    }
  }

  // ── Patch window.fetch ───────────────────────────────────────────────────
  const _origFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await _origFetch.apply(this, args);
    try {
      const url = args[0] instanceof Request ? args[0].url : String(args[0] || '');
      if (isDhanOCUrl(url)) {
        // Capture request body
        let reqBody = null;
        if (args[0] instanceof Request) {
          try { reqBody = await args[0].clone().text(); } catch (_) {}
        } else if (args[1]?.body) {
          reqBody = typeof args[1].body === 'string'
            ? args[1].body
            : (args[1].body instanceof FormData ? null : String(args[1].body));
        }

        response.clone().json().then(json => {
          post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge fetch intercepted: ${url.split('?')[0].split('/').pop()}`, ok: true });
          const parsed = parseOCResponse(json, reqBody);
          if (parsed) {
            post('NSEBOT_DHAN_API_DATA', { json, parsed, url, reqBody });
          }
        }).catch(e => {
          if (!e.message.includes('Unexpected')) {
            post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge fetch JSON parse error: ${e.message}`, ok: false, warn: true });
          }
        });
      }
    } catch (_) {}
    return response;
  };

  // ── Patch XMLHttpRequest ─────────────────────────────────────────────────
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._nsebot_dhan_url = String(url || '');
    this._nsebot_dhan_method = String(method || '').toUpperCase();
    return _origOpen.apply(this, [method, url, ...rest]);
  };

  XMLHttpRequest.prototype.send = function (body) {
    if (this._nsebot_dhan_url && isDhanOCUrl(this._nsebot_dhan_url)) {
      this._nsebot_dhan_body = body;
      this.addEventListener('load', function () {
        try {
          const json = JSON.parse(this.responseText);
          post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge XHR intercepted: ${(this._nsebot_dhan_url || '').split('/').pop()}`, ok: true });
          const parsed = parseOCResponse(json, this._nsebot_dhan_body);
          if (parsed) {
            post('NSEBOT_DHAN_API_DATA', { json, parsed, url: this._nsebot_dhan_url, reqBody: this._nsebot_dhan_body });
          }
        } catch (e) {
          if (!e.message.includes('Unexpected')) {
            post('NSEBOT_DHAN_DIAG', { msg: `Dhan bridge XHR parse error: ${e.message}`, ok: false });
          }
        }
      });
    }
    return _origSend.apply(this, [body]);
  };

  post('NSEBOT_DHAN_BRIDGE_READY', { ts: Date.now() });
  console.log('[NSEBOT] Dhan bridge active (MAIN world)');
})();
