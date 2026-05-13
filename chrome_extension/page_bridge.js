(function () {
  if (window.__NSEBOT_PAGE_BRIDGE__) return;
  window.__NSEBOT_PAGE_BRIDGE__ = true;

  const BRIDGE_SOURCE = 'nsebot-page-bridge';
  const EXT_SOURCE = 'nsebot-extension';
  const NSE_API_PATTERNS = [
    '/api/option-chain-indices',
    '/api/option-chain-equities',
    '/api/option-chain',
  ];

  function post(type, payload) {
    window.postMessage({ source: BRIDGE_SOURCE, type, payload }, '*');
  }

  function isNSEOptionChainUrl(url) {
    return NSE_API_PATTERNS.some((pattern) => String(url || '').includes(pattern));
  }

  async function handleFetchResponse(response, url, channel) {
    try {
      const clone = response.clone();
      const json = await clone.json();
      post('NSEBOT_NSE_API_DATA', { url, json, channel });
    } catch (error) {
      post('NSEBOT_NSE_DIAG', { ok: false, warn: true, msg: `${channel} JSON parse failed: ${error.message}` });
    }
  }

  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const url = (args[0] instanceof Request ? args[0].url : String(args[0])) || '';
    if (isNSEOptionChainUrl(url)) {
      handleFetchResponse(response, url, 'fetch');
    }
    return response;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__nsebotUrl = url;
    return originalOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    if (this.__nsebotUrl && isNSEOptionChainUrl(this.__nsebotUrl)) {
      this.addEventListener('load', function () {
        try {
          const json = JSON.parse(this.responseText);
          post('NSEBOT_NSE_API_DATA', { url: this.__nsebotUrl, json, channel: 'xhr' });
        } catch (error) {
          post('NSEBOT_NSE_DIAG', { ok: false, warn: true, msg: `xhr JSON parse failed: ${error.message}` });
        }
      });
    }
    return originalSend.apply(this, args);
  };

  window.addEventListener('message', async (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== EXT_SOURCE || data.type !== 'NSEBOT_FORCE_NSE_FETCH') return;

    const symbol = String(data.symbol || 'NIFTY').toUpperCase();
    const isEquity = !['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'].includes(symbol);
    const apiPath = isEquity
      ? `/api/option-chain-equities?symbol=${encodeURIComponent(symbol)}`
      : `/api/option-chain-indices?symbol=${encodeURIComponent(symbol)}`;
    const url = `https://www.nseindia.com${apiPath}`;

    try {
      post('NSEBOT_NSE_DIAG', { ok: true, warn: false, msg: `page bridge fetch: ${apiPath}` });
      const response = await originalFetch(url, {
        headers: {
          Accept: 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
          Referer: window.location.href,
        },
        credentials: 'include',
      });
      await handleFetchResponse(response, url, 'force-fetch');
      post('NSEBOT_NSE_FETCH_RESULT', { ok: response.ok, status: response.status, url });
    } catch (error) {
      post('NSEBOT_NSE_FETCH_RESULT', { ok: false, error: error.message, url });
      post('NSEBOT_NSE_DIAG', { ok: false, warn: true, msg: `page bridge fetch failed: ${error.message}` });
    }
  });

  post('NSEBOT_BRIDGE_READY', { href: window.location.href });
})();
