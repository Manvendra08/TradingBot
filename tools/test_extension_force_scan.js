const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const puppeteer = require('puppeteer-core');

const repoRoot = path.resolve(__dirname, '..');
const extensionPath = path.join(repoRoot, 'chrome_extension');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

async function getExtensionId(browser) {
  const target = await browser.waitForTarget(
    (candidate) =>
      candidate.type() === 'service_worker' &&
      candidate.url().startsWith('chrome-extension://'),
    { timeout: 30000 }
  );
  return target.url().split('/')[2];
}

async function getStorage(debugPage) {
  return debugPage.evaluate(
    () =>
      new Promise((resolve) => {
        chrome.storage.local.get(null, resolve);
      })
  );
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getBrowserWSEndpoint() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch('http://127.0.0.1:9222/json/version');
      const data = await response.json();
      if (data.webSocketDebuggerUrl) return data.webSocketDebuggerUrl;
    } catch (_) {
      // Chrome not ready yet.
    }
    await sleep(1000);
  }
  throw new Error('Chrome remote debugging endpoint did not come up');
}

async function main() {
  if (!fs.existsSync(chromePath)) {
    throw new Error(`Chrome not found at ${chromePath}`);
  }

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nsebot-ext-'));
  const chrome = spawn(chromePath, [
    '--remote-debugging-port=9222',
    `--user-data-dir=${userDataDir}`,
    `--disable-extensions-except=${extensionPath}`,
    `--load-extension=${extensionPath}`,
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank',
  ], { stdio: 'ignore' });

  try {
    const browserWSEndpoint = await getBrowserWSEndpoint();
    const browser = await puppeteer.connect({
      browserWSEndpoint,
      defaultViewport: { width: 1440, height: 1100 },
    });

    const extensionId = await getExtensionId(browser);
    console.log(`Extension ID: ${extensionId}`);

    const page = await browser.newPage();
    page.on('console', (msg) => console.log(`[page] ${msg.text()}`));

    await page.goto('https://www.nseindia.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(5000);
    await page.goto('https://www.nseindia.com/option-chain', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(12000);

    await page.evaluate(() => {
      window.postMessage(
        { source: 'nsebot-extension', type: 'NSEBOT_FORCE_NSE_FETCH', symbol: 'NIFTY' },
        '*'
      );
    });

    const debugPage = await browser.newPage();
    await debugPage.goto(`chrome-extension://${extensionId}/debug.html`, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });

    let storage = null;
    let success = false;

    for (let attempt = 0; attempt < 12; attempt += 1) {
      await sleep(3000);
      storage = await getStorage(debugPage);
      const scanCount = storage.nsebot_scan_count || 0;
      const strikeRows = storage.nsebot_snapshot?.strikes?.length || 0;
      console.log(`Poll ${attempt + 1}: scanCount=${scanCount} strikeRows=${strikeRows}`);
      if (scanCount > 0 && strikeRows > 0) {
        success = true;
        break;
      }
    }

    console.log(JSON.stringify({
      success,
      site: storage?.nsebot_site || null,
      backendOk: storage?.nsebot_backend_ok || false,
      scanCount: storage?.nsebot_scan_count || 0,
      strikeRows: storage?.nsebot_snapshot?.strikes?.length || 0,
      lastScan: storage?.nsebot_last_scan_ts || null,
      scanLogTail: (storage?.nsebot_scan_log || []).slice(-8),
    }, null, 2));

    if (!success) {
      throw new Error('Force scan did not populate snapshot data');
    }

    await browser.close();
  } finally {
    chrome.kill('SIGKILL');
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
