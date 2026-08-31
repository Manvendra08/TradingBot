/**
 * GenTOTP – Content Script (Auto-fill)
 * Injected into Zerodha and Shoonya login pages.
 */

"use strict";

// ─── Broker Detection ─────────────────────────────────────────────────────────
function detectBroker() {
  const host = window.location.hostname;
  if (host.includes("zerodha.com")) return "zerodha";
  if (host.includes("shoonya.com") || host.includes("finvasia.com")) return "shoonya";
  return "unknown";
}

// ─── Strict Login Page Guard ──────────────────────────────────────────────────
// Prevents GenTOTP from filling TOTP into order quantity/price fields after login!
function isLoginPage(broker) {
  const b = (broker || "").toLowerCase();

  if (b.includes("zerodha")) {
    // 1. If explicit 2FA container/field exists, it is definitely a login/2FA page!
    if (
      document.querySelector("#totp, #pin, input[name='totp'], input[name='pin'], form.twofa-form, .twofa-form, .container-login, form.login-form, .login-box")
    ) {
      return true;
    }

    // 2. Definite logged-in indicators in DOM (trading navigation, order tabs, marketwatch)
    if (
      document.querySelector(".app-nav") ||
      document.querySelector(".marketwatch-sidebar") ||
      document.querySelector('a[href*="/orders"], a[href*="/positions"], a[href*="/holdings"], a[href*="/funds"]')
    ) {
      return false;
    }

    // 3. DO NOT run on logged-in dashboard/trading paths
    const path = window.location.pathname.toLowerCase();
    if (
      path.includes("/dashboard") ||
      path.includes("/orders") ||
      path.includes("/positions") ||
      path.includes("/holdings") ||
      path.includes("/funds") ||
      path.includes("/marketwatch") ||
      path.includes("/apps") ||
      path.includes("/chart")
    ) {
      return false;
    }

    const bodyText = (document.body ? document.body.innerText || "" : "").toLowerCase();
    const has2FAText = bodyText.includes("two-factor") || bodyText.includes("totp") || bodyText.includes("authenticator") || bodyText.includes("app code");

    return has2FAText;
  }

  if (b.includes("shoonya") || b.includes("finvasia")) {
    const bodyText = (document.body ? document.body.innerText || "" : "").toLowerCase();
    if (
      bodyText.includes("watchlist") ||
      bodyText.includes("orderbook") ||
      bodyText.includes("net position") ||
      bodyText.includes("portfolio") ||
      document.querySelector(".header-profile, .user-info, .dashboard-container")
    ) {
      return false;
    }
    return true;
  }

  return false;
}

// ─── Visibility Check ────────────────────────────────────────────────────────
function isVisible(el) {
  if (!el || el.disabled || el.readOnly) return false;
  try {
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  } catch (_) { return el.offsetParent !== null; }
}

// ─── Explicit Priority Selectors ─────────────────────────────────────────────
const ZERODHA_SELECTORS = [
  '#totp',
  '#pin',
  'input[name="totp"]',
  'input[name="pin"]',
  'input[name="twofa"]',
  'input[name="2fa"]',
  'input[id*="totp" i]',
  'input[id*="pin" i]',
  'input[id*="twofa" i]',
  'input[id="twofa_value"]',
  '.twofa-form input[type="number"]',
  '.twofa-form input[type="text"]',
  '.twofa-form input[type="password"]',
  '.twofa-form input[type="tel"]',
  '.twofa-form input',
  'form.twofa-form input',
  'form.login-form input[type="number"]',
  'form.login-form input[type="password"]',
  'form.login-form input[type="tel"]',
  'input[type="number"][placeholder*="TOTP" i]',
  'input[type="tel"][placeholder*="TOTP" i]',
  'input[placeholder*="TOTP" i]',
  'input[placeholder*="App code" i]',
  'input[placeholder*="Mobile App Code" i]',
  'input[placeholder*="Authenticator" i]',
  'input[placeholder*="2FA" i]',
  'input[placeholder*="OTP" i]',
  'input[placeholder*="code" i]',
  'input[placeholder="••••••"]',
  'input[autocomplete="one-time-code"]',
  'input[type="number"][maxlength="6"]',
  'input[type="text"][maxlength="6"]',
  'input[type="password"][maxlength="6"]',
  'input[type="tel"][maxlength="6"]',
];

const SHOONYA_SELECTORS = [
  'input[placeholder="OTP/TOTP"]',
  'input[placeholder*="OTP" i]',
  'input[placeholder*="TOTP" i]',
  'input[placeholder*="2FA" i]',
  'input[name="totp"]',
  'input[name="otp"]',
  'input[name="twofa"]',
  'input[name="code"]',
  'input[id*="totp" i]',
  'input[id*="otp" i]',
  'input[formcontrolname*="otp" i]',
  'input[formcontrolname*="totp" i]',
  'input[placeholder*="Authenticator" i]',
  'input[type="number"][maxlength="6"]',
  'input[type="text"][maxlength="6"]',
  'input[type="password"][maxlength="6"]',
  '.p-inputtext',
  '.otp-input',
  '#twofa',
];

// ─── Shadow DOM / Flutter Helper ─────────────────────────────────────────────
function getShoonyaFlutterInput() {
  const pane = document.querySelector('flt-glass-pane');
  if (pane && pane.shadowRoot) {
    const inputs = Array.from(pane.shadowRoot.querySelectorAll('input.flt-text-editing')).filter(isVisible);
    if (inputs.length >= 3) return inputs[2];
    if (inputs.length === 1) return inputs[0];
  }
  return null;
}

// ─── TOTP Input Finder ───────────────────────────────────────────────────────
function findInput(broker, isManual = false) {
  if (!isManual && !isLoginPage(broker)) return null;

  // For Shoonya, Flutter Web inputs are inside a Shadow DOM!
  if (broker === "shoonya") {
    const flutterEl = getShoonyaFlutterInput();
    if (flutterEl) return flutterEl;
  }

  const selectors = broker === "zerodha"
    ? [...ZERODHA_SELECTORS, ...SHOONYA_SELECTORS]
    : [...SHOONYA_SELECTORS, ...ZERODHA_SELECTORS];

  // 1. Explicit broker selectors (Main DOM)
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    } catch (_) {}
  }

  // 2. Keyword scan across visible inputs on login page
  const allVisible = Array.from(document.querySelectorAll("input"))
    .filter(el => {
      if (!isVisible(el)) return false;
      const t = (el.type || "").toLowerCase();
      return !["hidden","submit","button","checkbox","radio","image","file"].includes(t);
    });

  for (const el of allVisible) {
    const sig = [
      el.name, el.id, el.placeholder,
      el.getAttribute("formcontrolname") || "",
      el.getAttribute("aria-label") || "",
      el.getAttribute("autocomplete") || "",
    ].join(" ").toLowerCase();
    if (/(totp|otp|2fa|twofa|one.?time|auth.*code|security.?code|app.?code)/.test(sig)) return el;
    if (el.getAttribute("autocomplete") === "one-time-code") return el;
  }

  // 3. Label matching
  const labels = Array.from(document.querySelectorAll("label"));
  for (const lbl of labels) {
    const txt = (lbl.innerText || "").toLowerCase();
    if (/(totp|otp|2fa|twofa|authenticator|app code)/.test(txt)) {
      const forId = lbl.getAttribute("for");
      if (forId) {
        const inp = document.getElementById(forId);
        if (inp && isVisible(inp)) return inp;
      }
      const childInp = lbl.querySelector("input");
      if (childInp && isVisible(childInp)) return childInp;
      // Sibling input
      const siblingInp = lbl.parentElement?.querySelector("input");
      if (siblingInp && isVisible(siblingInp)) return siblingInp;
    }
  }

  // 4. Structural heuristics ONLY on login pages
  const userInputs = allVisible.filter(el => {
    const t = (el.type || "").toLowerCase();
    return ["text","number","password","tel",""].includes(t);
  });

  // Zerodha 2FA screen: exactly 1 input visible inside login/2FA container or on page
  if (broker === "zerodha" && (userInputs.length === 1 || userInputs.length === 2)) {
    for (const el of userInputs) {
      const sig = (el.name + " " + el.id + " " + el.placeholder).toLowerCase();
      if (!/username|userid|user_id|password|passwd|search|filter/.test(sig)) {
        return el;
      }
    }
  }

  if (userInputs.length === 3 && broker === "shoonya") {
    const third = userInputs[2];
    const sig = (third.name + third.id + third.placeholder).toLowerCase();
    if (!/user|login/.test(sig)) return third;
  }

  return null;
}

// ─── Multi-Box OTP ───────────────────────────────────────────────────────────
function findMultiBoxInputs() {
  const inputs = Array.from(document.querySelectorAll('input[maxlength="1"], input[data-index]'))
    .filter(isVisible);
  return inputs.length === 6 ? inputs : null;
}

// ─── Universal Value Setter (React, Angular, Vue, Flutter Web) ───────────────
function setValueUniversal(el, value) {
  if (!el) return;
  el.focus();

  const isFlutter = el.classList.contains("flt-text-editing") || el.classList.contains("transparentTextEditing");

  if (isFlutter) {
    // 1. For Flutter Web: Select existing text first to overwrite instead of append
    try { document.execCommand("selectAll", false, null); } catch (_) {}
    
    // 2. Insert text (Flutter's TextController listens to this)
    try {
      const inserted = document.execCommand("insertText", false, value);
      if (!inserted) {
        // Fallback if execCommand failed
        el.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: value }));
      }
    } catch (_) {}
    
    // 3. Trigger blur so Flutter finalizes the input
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return;
  }

  // Native Property Setter for React, Angular, Vue, Vanilla
  const nativeSetter =
    Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")?.set ||
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (nativeSetter) nativeSetter.call(el, value);
  else el.value = value;

  // React / Standard DOM events
  el.dispatchEvent(new Event("input",  { bubbles: true, cancelable: true }));
  el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
  el.dispatchEvent(new Event("blur", { bubbles: true }));
}

// ─── Submit Clicker ──────────────────────────────────────────────────────────
function clickSubmit() {
  for (const sel of ['button[type="submit"]', 'button.btn-primary', 'button.btn', 'input[type="submit"]']) {
    const btn = document.querySelector(sel);
    if (btn && isVisible(btn)) { setTimeout(() => btn.click(), 350); return true; }
  }
  return false;
}

// ─── Toast ───────────────────────────────────────────────────────────────────
function showToast(msg, type = "success") {
  const ex = document.getElementById("gentop-toast");
  if (ex) ex.remove();
  const t = document.createElement("div");
  t.id = "gentop-toast";
  t.textContent = msg;
  Object.assign(t.style, {
    position: "fixed", top: "16px", right: "16px", zIndex: "2147483647",
    background: type === "success" ? "#1a7c4e" : "#b91c1c",
    color: "#fff", padding: "10px 18px", borderRadius: "8px",
    fontSize: "13px", fontFamily: "system-ui, sans-serif",
    boxShadow: "0 4px 16px rgba(0,0,0,.35)", opacity: "0", transition: "opacity .2s ease"
  });
  document.body.appendChild(t);
  requestAnimationFrame(() => { t.style.opacity = "1"; });
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 2800);
}

// ─── Core Fill ───────────────────────────────────────────────────────────────
function performFill(code, broker, autoSubmit, silent = false) {
  const multiBox = findMultiBoxInputs();
  if (multiBox) {
    code.split("").forEach((ch, i) => {
      setValueUniversal(multiBox[i], ch);
    });
    showToast(`✅ GenTOTP: ${broker} TOTP filled`);
    if (autoSubmit) clickSubmit();
    return true;
  }

  const isManual = !silent;
  const input = findInput(broker, isManual);
  if (!input) {
    if (!silent) {
      showToast("❌ TOTP field not found on this page", "error");
    }
    return false;
  }

  setValueUniversal(input, code);
  showToast(`✅ GenTOTP: ${broker} TOTP filled`);
  if (autoSubmit) clickSubmit();
  return true;
}

// ─── Message Listener ────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "FILL_TOTP") return;
  const broker = detectBroker();
  const ok = performFill(message.code, broker, message.autoSubmit !== false, false);
  sendResponse({ ok, broker });
});

// ─── Instant Auto-Fill on Detection ─────────────────────────────────────────
let _lastAutoFilledEl = null;

function checkAndAutoFillOnDetect() {
  const broker = detectBroker();
  if (broker === "unknown" || !isLoginPage(broker)) return;

  const multiBox = findMultiBoxInputs();
  const input = multiBox ? multiBox[0] : findInput(broker);
  if (!input) return;
  if (_lastAutoFilledEl === input) return;
  if ((input.value || "").trim().length >= 6) return;

  chrome.runtime.sendMessage({ type: "GET_AUTO_TOTP", broker }, (resp) => {
    if (chrome.runtime.lastError || !resp?.ok || !resp.code) return;
    const ok = performFill(resp.code, broker, true, true);
    if (ok) _lastAutoFilledEl = input;
  });
}

setInterval(checkAndAutoFillOnDetect, 800);
console.log(`[GenTOTP] content script on ${detectBroker()} (${window.location.hostname})`);
