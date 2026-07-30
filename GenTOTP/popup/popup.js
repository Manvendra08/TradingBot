/**
 * GenTOTP – Popup Controller
 * Manages: lock/unlock flow, account CRUD, live TOTP display, auto-fill dispatch.
 */

"use strict";

// ─── State ────────────────────────────────────────────────────────────────────
let _accounts       = [];      // decrypted account list (label, broker, id, secret)
let _activeTimers   = [];      // interval handles for cleanup
let _currentBroker  = null;    // broker detected on active tab ("zerodha"|"shoonya"|null)

// ─── DOM References ───────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const screens = {
  lock:     $("screen-lock"),
  main:     $("screen-main"),
  add:      $("screen-add"),
  changePw: $("screen-change-pw")
};

// ─── Utilities ────────────────────────────────────────────────────────────────
function showScreen(name) {
  Object.entries(screens).forEach(([k, el]) => {
    el.classList.toggle("hidden", k !== name);
    el.classList.toggle("active", k === name);
  });
}

function showError(elId, msg) {
  const el = $(elId);
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3500);
}

function clearErrors() {
  ["lock-error", "add-error"].forEach(id => $(id)?.classList.add("hidden"));
}

// ─── Settings Storage Helpers ────────────────────────────────────────────────
async function isRequirePwEnabled() {
  return new Promise(r => {
    chrome.storage.local.get(["gentop_require_pw"], res => {
      r(res.gentop_require_pw !== false); // default true
    });
  });
}

async function syncCachedPassword(pw) {
  const reqPw = await isRequirePwEnabled();
  if (!reqPw && pw) {
    await new Promise(r => chrome.storage.local.set({ gentop_cached_pw: pw }, r));
  } else {
    await new Promise(r => chrome.storage.local.remove(["gentop_cached_pw"], r));
  }
}

// ─── Lock / Vault ─────────────────────────────────────────────────────────────
async function initLockScreen() {
  const initialized = await AccountStore.isVaultInitialized();
  if (initialized) {
    $("lock-setup").classList.add("hidden");
    $("lock-unlock").classList.remove("hidden");
    $("unlock-pw").focus();
  } else {
    $("lock-setup").classList.remove("hidden");
    $("lock-unlock").classList.add("hidden");
    $("setup-pw").focus();
  }
  showScreen("lock");
}

$("btn-setup").addEventListener("click", async () => {
  const pw  = $("setup-pw").value;
  const cnf = $("setup-pw-cnf").value;
  if (!pw)        return showError("lock-error", "Password cannot be empty");
  if (pw !== cnf) return showError("lock-error", "Passwords do not match");

  CryptoVault.setSessionPassword(pw);
  await AccountStore.setVaultInitialized(true);
  await syncCachedPassword(pw);
  $("setup-pw").value = "";
  $("setup-pw-cnf").value = "";
  await loadMainScreen();
});

$("unlock-pw").addEventListener("keydown", e => { if (e.key === "Enter") $("btn-unlock").click(); });
$("btn-unlock").addEventListener("click", async () => {
  const pw = $("unlock-pw").value;
  if (!pw) return showError("lock-error", "Enter your master password");

  // Try decrypting first account to verify password correctness
  const allAccounts = await AccountStore.readAll();
  if (allAccounts.length > 0) {
    try {
      await CryptoVault.decrypt(allAccounts[0].encryptedSecret, pw);
    } catch (_) {
      return showError("lock-error", "Wrong password");
    }
  }

  CryptoVault.setSessionPassword(pw);
  await syncCachedPassword(pw);
  $("unlock-pw").value = "";
  await loadMainScreen();
});

$('btn-lock').addEventListener('click', async () => {
  CryptoVault.clearSession();
  await new Promise(r => chrome.storage.local.remove(["gentop_cached_pw"], r));
  _accounts = [];
  clearTimers();
  closeSettingsMenu();
  initLockScreen();
});

// ─── Settings Menu ────────────────────────────────────────────────────────────
function closeSettingsMenu() {
  $("settings-menu").classList.add("hidden");
}

$("btn-settings").addEventListener("click", (e) => {
  e.stopPropagation();
  $("settings-menu").classList.toggle("hidden");
});

$("chk-require-pw").addEventListener("change", async (e) => {
  const requirePw = e.target.checked;
  await new Promise(r => chrome.storage.local.set({ gentop_require_pw: requirePw }, r));
  if (requirePw) {
    await new Promise(r => chrome.storage.local.remove(["gentop_cached_pw"], r));
  } else {
    const sessionPw = CryptoVault.getSessionPassword();
    if (sessionPw) {
      await new Promise(r => chrome.storage.local.set({ gentop_cached_pw: sessionPw }, r));
    }
  }
});

$("chk-auto-fill-detect").addEventListener("change", async (e) => {
  const autoFillDetect = e.target.checked;
  await new Promise(r => chrome.storage.local.set({ gentop_auto_fill_detect: autoFillDetect }, r));
});

document.addEventListener("click", () => closeSettingsMenu());

// ─── Logout (wipe vault) ─────────────────────────────────────────────────────
$("btn-logout").addEventListener("click", async () => {
  closeSettingsMenu();
  const confirmed = confirm(
    "⚠️ This will DELETE all saved accounts and reset the vault.\n\nAre you absolutely sure?"
  );
  if (!confirmed) return;

  await new Promise(resolve => {
    chrome.storage.local.clear(resolve);
  });
  CryptoVault.clearSession();
  _accounts = [];
  clearTimers();
  await initLockScreen();
});

// ─── Change Password Screen ───────────────────────────────────────────────────
$("btn-change-pw").addEventListener("click", () => {
  closeSettingsMenu();
  $("cpw-current").value = "";
  $("cpw-new").value     = "";
  $("cpw-confirm").value = "";
  $("cpw-error").classList.add("hidden");
  $("cpw-success").classList.add("hidden");
  showScreen("changePw");
  $("cpw-current").focus();
});

$("btn-back-from-pw").addEventListener("click", async () => {
  await loadMainScreen();
});

$("btn-save-pw").addEventListener("click", async () => {
  const current = $("cpw-current").value;
  const newPw   = $("cpw-new").value.trim();
  const confirm = $("cpw-confirm").value.trim();

  const showCpwError = (msg) => {
    const el = $("cpw-error");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 4000);
  };

  if (!current) return showCpwError("Enter your current password");
  if (!newPw)   return showCpwError("New password cannot be empty");
  if (newPw !== confirm) return showCpwError("Passwords do not match");

  // Verify current password
  const sessionPw = CryptoVault.getSessionPassword();
  if (current !== sessionPw) {
    // Try decrypting a real account to verify (in case session was somehow mismatched)
    const rawAccounts = await AccountStore.readAll();
    if (rawAccounts.length > 0) {
      try {
        await CryptoVault.decrypt(rawAccounts[0].encryptedSecret, current);
      } catch (_) {
        return showCpwError("Current password is incorrect");
      }
    } else if (current !== sessionPw) {
      return showCpwError("Current password is incorrect");
    }
  }

  // Re-encrypt all account secrets with new password
  const rawAccounts = await AccountStore.readAll();
  const updatedAccounts = [];
  for (const acc of rawAccounts) {
    const plainSecret = await CryptoVault.decrypt(acc.encryptedSecret, current);
    const newEncSecret = await CryptoVault.encrypt(plainSecret, newPw);
    updatedAccounts.push({ ...acc, encryptedSecret: newEncSecret });
  }

  await new Promise(resolve => {
    chrome.storage.local.set({ gentop_accounts: updatedAccounts }, resolve);
  });

  CryptoVault.setSessionPassword(newPw);
  await syncCachedPassword(newPw);
  $("cpw-current").value = "";
  $("cpw-new").value     = "";
  $("cpw-confirm").value = "";

  const succ = $("cpw-success");
  succ.classList.remove("hidden");
  setTimeout(() => { succ.classList.add("hidden"); loadMainScreen(); }, 1800);
});

// ─── Main Screen ──────────────────────────────────────────────────────────────
async function loadMainScreen() {
  clearTimers();
  $("account-list").innerHTML = "";

  const rawAccounts = await AccountStore.readAll();
  _accounts = [];

  const pw = CryptoVault.getSessionPassword();
  for (const acc of rawAccounts) {
    try {
      const secret = await CryptoVault.decrypt(acc.encryptedSecret, pw);
      _accounts.push({ ...acc, secret });
    } catch (_) {
      console.warn(`[GenTOTP] Could not decrypt account ${acc.id}`);
    }
  }

  await detectActiveBroker();
  renderAccountList();
  startTimers();
  $("chk-require-pw").checked = await isRequirePwEnabled();
  $("chk-auto-fill-detect").checked = await new Promise(r => chrome.storage.local.get(["gentop_auto_fill_detect"], res => r(!!res.gentop_auto_fill_detect)));
  showScreen("main");
}

function clearTimers() {
  _activeTimers.forEach(t => clearInterval(t));
  _activeTimers = [];
}

// ─── Broker Detection ─────────────────────────────────────────────────────────
async function detectActiveBroker() {
  return new Promise(resolve => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs || tabs.length === 0) { _currentBroker = null; return resolve(); }
      const url = tabs[0].url || "";
      if (url.includes("zerodha.com"))  _currentBroker = "zerodha";
      else if (url.includes("shoonya.com") || url.includes("finvasia.com")) _currentBroker = "shoonya";
      else _currentBroker = null;

      updateAutofillBanner();
      resolve();
    });
  });
}

function updateAutofillBanner() {
  const banner = $("autofill-banner");
  if (_currentBroker && _accounts.some(a => a.broker === _currentBroker || a.broker === "other")) {
    $("autofill-broker-name").textContent =
      _currentBroker === "zerodha" ? "Zerodha Kite" : "Shoonya";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

// ─── Auto-fill Banner Action ─────────────────────────────────────────────────
$("btn-autofill").addEventListener("click", async () => {
  const account = _accounts.find(a => a.broker === _currentBroker) || _accounts[0];
  if (!account) return;
  await sendAutofill(account);
});

async function sendAutofill(account) {
  const code = await TOTP.generate(account.secret);
  chrome.runtime.sendMessage(
    { type: "AUTOFILL_TOTP", code, broker: account.broker },
    (resp) => {
      if (chrome.runtime.lastError) {
        console.warn("[GenTOTP] Autofill error:", chrome.runtime.lastError.message);
      }
    }
  );
}

// ─── Account List Rendering ───────────────────────────────────────────────────
function renderAccountList() {
  const list = $("account-list");
  list.innerHTML = "";

  const empty = $("empty-state") || createEmptyState();
  if (_accounts.length === 0) {
    list.appendChild(empty);
    return;
  }

  _accounts.forEach(acc => {
    const card = createAccountCard(acc);
    list.appendChild(card);
  });
}

function createEmptyState() {
  const div = document.createElement("div");
  div.id = "empty-state";
  div.className = "empty-state";
  div.innerHTML = `<div class="empty-icon">🔑</div><p>No accounts yet.<br/>Tap <strong>＋</strong> to add one.</p>`;
  return div;
}

const SVG_R = 14; // circle radius for countdown ring
const SVG_CIRCUMFERENCE = 2 * Math.PI * SVG_R;

function createAccountCard(account) {
  const card = document.createElement("div");
  card.className = `account-card broker-${account.broker}`;
  card.dataset.id = account.id;

  const brokerName = { zerodha: "Zerodha", shoonya: "Shoonya", other: "Other" }[account.broker] || account.broker;

  card.innerHTML = `
    <div class="card-header">
      <span class="card-label">${escHtml(account.label)}</span>
      <div style="display:flex;align-items:center;gap:6px;">
        <span class="broker-badge ${account.broker}">${brokerName}</span>
        <button class="card-delete" data-id="${account.id}" title="Remove account">✕</button>
      </div>
    </div>
    <div class="totp-row">
      <span class="totp-code placeholder" id="code-${account.id}">• • • • • •</span>
      <div class="countdown-wrap">
        <svg class="countdown-svg" viewBox="0 0 34 34">
          <circle class="countdown-bg" cx="17" cy="17" r="${SVG_R}"/>
          <circle class="countdown-arc" id="arc-${account.id}" cx="17" cy="17" r="${SVG_R}"
            stroke-dasharray="${SVG_CIRCUMFERENCE}"
            stroke-dashoffset="${SVG_CIRCUMFERENCE}"/>
        </svg>
        <div class="countdown-text" id="timer-${account.id}">—</div>
      </div>
    </div>
    <div class="card-actions">
      <button class="card-btn copy-btn" data-id="${account.id}">📋 Copy</button>
      <button class="card-btn fill-btn" data-id="${account.id}">⚡ Auto-Fill</button>
    </div>
  `;

  // Delete handler
  card.querySelector(".card-delete").addEventListener("click", async (e) => {
    e.stopPropagation();
    await AccountStore.removeAccount(account.id);
    _accounts = _accounts.filter(a => a.id !== account.id);
    renderAccountList();
    startTimers();
    updateAutofillBanner();
  });

  // Copy handler
  card.querySelector(".copy-btn").addEventListener("click", async () => {
    const code = await TOTP.generate(account.secret);
    await navigator.clipboard.writeText(code);
    const codeEl = $(`code-${account.id}`);
    codeEl.classList.add("copied");
    setTimeout(() => codeEl.classList.remove("copied"), 600);
  });

  // Fill handler
  card.querySelector(".fill-btn").addEventListener("click", async () => {
    await sendAutofill(account);
  });

  return card;
}

function escHtml(str) {
  return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ─── Live TOTP Ticker ─────────────────────────────────────────────────────────
function startTimers() {
  clearTimers();
  _accounts.forEach(acc => startAccountTimer(acc));
}

function startAccountTimer(account) {
  const tick = async () => {
    const codeEl  = $(`code-${account.id}`);
    const arcEl   = $(`arc-${account.id}`);
    const timerEl = $(`timer-${account.id}`);
    if (!codeEl) return;

    const sec      = TOTP.secondsRemaining(30);
    const expiring = sec <= 7;
    const fraction = sec / 30;
    const offset   = SVG_CIRCUMFERENCE * (1 - fraction);

    try {
      const code = await TOTP.generate(account.secret);
      codeEl.textContent = code.slice(0, 3) + " " + code.slice(3);
      codeEl.classList.remove("placeholder");
      codeEl.classList.toggle("expiring", expiring);
    } catch (err) {
      codeEl.textContent = "Error";
    }

    if (arcEl)   { arcEl.style.strokeDashoffset = offset; arcEl.classList.toggle("expiring", expiring); }
    if (timerEl) { timerEl.textContent = sec; }
  };

  tick();
  const handle = setInterval(tick, 1000);
  _activeTimers.push(handle);
}

// ─── Add Account Screen ───────────────────────────────────────────────────────
$("btn-add-account").addEventListener("click", () => {
  clearErrors();
  $("add-label").value  = "";
  $("add-secret").value = "";
  $("add-broker").value = _currentBroker || "zerodha";
  showScreen("add");
  $("add-label").focus();
});

$("btn-back").addEventListener("click", async () => {
  await loadMainScreen();
});

$("btn-toggle-secret").addEventListener("click", () => {
  const inp = $("add-secret");
  inp.type  = inp.type === "password" ? "text" : "password";
});

$("btn-save-account").addEventListener("click", async () => {
  const label  = $("add-label").value.trim();
  const broker = $("add-broker").value;
  const secret = $("add-secret").value.trim().replace(/\s/g, "");

  if (!label)  return showError("add-error", "Label is required");
  if (!secret) return showError("add-error", "TOTP secret is required");

  const { valid, error } = TOTP.validateSecret(secret);
  if (!valid) return showError("add-error", error);

  // Verify it actually generates a code
  try {
    await TOTP.generate(secret);
  } catch (err) {
    return showError("add-error", `Invalid secret: ${err.message}`);
  }

  const pw = CryptoVault.getSessionPassword();
  const encryptedSecret = await CryptoVault.encrypt(secret, pw);

  await AccountStore.addAccount({ label, broker, encryptedSecret });
  await loadMainScreen();
});

// ─── Keyboard Shortcuts ───────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const main = screens.main;
    if (!main.classList.contains("hidden")) return;
    loadMainScreen();
  }
});

// ─── Boot ─────────────────────────────────────────────────────────────────────
(async () => {
  const reqPw = await isRequirePwEnabled();
  if (!reqPw) {
    const cachedPw = await new Promise(r => chrome.storage.local.get(["gentop_cached_pw"], res => r(res.gentop_cached_pw)));
    if (cachedPw) {
      CryptoVault.setSessionPassword(cachedPw);
    }
  }
  if (CryptoVault.hasSession()) {
    await loadMainScreen();
  } else {
    await initLockScreen();
  }
})().catch(console.error);

// Start timers after DOM fully loaded
document.addEventListener("DOMContentLoaded", () => {
  if (CryptoVault.hasSession()) startTimers();
});
