/**
 * GenTOTP – Content Script (Auto-fill)
 * Injected into Zerodha and Shoonya login pages.
 * Listens for FILL_TOTP messages and handles Instant Auto-Fill on Detection.
 */

"use strict";

// ─── Broker Detection ─────────────────────────────────────────────────────────
function detectBroker() {
  const host = window.location.hostname;
  if (host.includes("zerodha.com")) return "zerodha";
  if (host.includes("shoonya.com") || host.includes("finvasia.com")) return "shoonya";
  return "unknown";
}

// ─── Input Field Selectors ───────────────────────────────────────────────────
const SELECTORS = {
  zerodha: [
    'input[type="number"][placeholder]',   // Kite TOTP field (numeric)
    'input[placeholder*="TOTP" i]',
    'input[placeholder*="totp" i]',
    'input[placeholder*="authenticator" i]',
    'input[name="totp"]',
    'input[id*="totp"]',
    '.twofa-form input[type="number"]',
    '.twofa-form input[type="text"]'
  ],
  shoonya: [
    'input[placeholder*="OTP/TOTP" i]',
    'input[placeholder*="OTP" i]',
    'input[placeholder*="TOTP" i]',
    'input[placeholder*="2FA" i]',
    'input[name="totp"]',
    'input[name="otp"]',
    'input[name="twofa"]',
    'input[name="code"]',
    'input[id="totp"]',
    'input[id="otp"]',
    'input[id="twofa"]',
    'input[id="otp_input"]',
    'input[id*="totp" i]',
    'input[id*="otp" i]',
    'input[formcontrolname*="otp" i]',
    'input[formcontrolname*="totp" i]',
    'input[placeholder*="authenticator" i]',
    'input[type="number"][maxlength="6"]',
    'input[type="text"][maxlength="6"]',
    'input[type="password"][maxlength="6"]',
    '.otp-input',
    '#twofa',
    '.p-inputtext'
  ]
};

// ─── Visibility Check ────────────────────────────────────────────────────────
function isElementVisible(el) {
  if (!el || el.disabled || el.readOnly) return false;
  try {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  } catch (_) {
    return el.offsetParent !== null;
  }
}

// ─── Utilities ────────────────────────────────────────────────────────────────
function findInput(broker) {
  const selectorList = SELECTORS[broker] ? [...SELECTORS[broker], ...SELECTORS.zerodha] : [...SELECTORS.zerodha, ...SELECTORS.shoonya];
  
  // 1. Try explicit selectors first
  for (const sel of selectorList) {
    try {
      const el = document.querySelector(sel);
      if (el && isElementVisible(el)) return el;
    } catch (_) {}
  }

  // 2. Check for active/focused element if it's an input
  const active = document.activeElement;
  if (active && active.tagName === "INPUT" && active.type !== "hidden" && active.type !== "submit") {
    return active;
  }

  // 3. Fallback: Search all visible inputs for 2FA-like attributes
  const allInputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])'));
  const visibleInputs = allInputs.filter(isElementVisible);

  for (const input of visibleInputs) {
    const attr = (input.name + " " + input.id + " " + input.className + " " + (input.placeholder || "") + " " + (input.getAttribute("aria-label") || "") + " " + (input.getAttribute("formcontrolname") || "")).toLowerCase();
    if (attr.includes("totp") || attr.includes("otp") || attr.includes("2fa") || attr.includes("code") || attr.includes("auth") || input.getAttribute("maxlength") === "6") {
      return input;
    }
  }

  // 4. Last resort: If 2 or 3 visible fields on page (e.g. User ID + Password + OTP/TOTP), the OTP/TOTP is the LAST input!
  if (visibleInputs.length >= 2 && visibleInputs.length <= 4) {
    return visibleInputs[visibleInputs.length - 1];
  }

  return null;
}

// ─── Multi-Box OTP Handler (e.g. 6 separate 1-digit inputs) ──────────────────
function findMultiBoxInputs() {
  const inputs = Array.from(document.querySelectorAll('input[maxlength="1"], input[data-index]'));
  const visible = inputs.filter(isElementVisible);
  if (visible.length === 6) return visible;
  return null;
}

/**
 * React/Angular-aware value setter – triggers synthetic events so framework
 * state updates pick up the new value.
 */
function setNativeValue(el, value) {
  const nativeSetter =
    Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")?.set ||
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;

  if (nativeSetter) {
    nativeSetter.call(el, value);
  } else {
    el.value = value;
  }

  el.dispatchEvent(new Event("input",  { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function clickSubmit(broker) {
  const submitSelectors = [
    'button[type="submit"]',
    'button.btn-primary',
    'button.btn',
    '.form-action button',
    'input[type="submit"]'
  ];
  for (const sel of submitSelectors) {
    const btn = document.querySelector(sel);
    if (btn && isElementVisible(btn)) {
      setTimeout(() => btn.click(), 250);
      return true;
    }
  }
  return false;
}

// ─── Toast Notification ───────────────────────────────────────────────────────
function showToast(message, type = "success") {
  const existing = document.getElementById("gentop-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.id = "gentop-toast";
  toast.textContent = message;
  Object.assign(toast.style, {
    position:     "fixed",
    top:          "16px",
    right:        "16px",
    zIndex:       "2147483647",
    background:   type === "success" ? "#1a7c4e" : "#b91c1c",
    color:        "#fff",
    padding:      "10px 18px",
    borderRadius: "8px",
    fontSize:     "13px",
    fontFamily:   "system-ui, sans-serif",
    boxShadow:    "0 4px 16px rgba(0,0,0,.35)",
    opacity:      "0",
    transition:   "opacity .2s ease"
  });
  document.body.appendChild(toast);
  requestAnimationFrame(() => { toast.style.opacity = "1"; });
  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 300);
  }, 2800);
}

// ─── Message Listener ─────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "FILL_TOTP") return;

  const broker = detectBroker();
  const multiBox = findMultiBoxInputs();

  if (multiBox && multiBox.length === 6) {
    const chars = message.code.split("");
    multiBox.forEach((box, i) => {
      box.focus();
      setNativeValue(box, chars[i]);
    });
    showToast(`✅ GenTOTP: ${broker === "zerodha" ? "Zerodha" : "Shoonya"} TOTP filled`);
    const didSubmit = message.autoSubmit !== false ? clickSubmit(broker) : false;
    sendResponse({ ok: true, broker, didSubmit });
    return;
  }

  const input = findInput(broker);

  if (!input) {
    showToast("❌ TOTP field not found on this page", "error");
    sendResponse({ ok: false, error: "No TOTP input found" });
    return;
  }

  input.focus();
  setNativeValue(input, message.code);
  showToast(`✅ GenTOTP: ${broker === "zerodha" ? "Zerodha" : "Shoonya"} TOTP filled`);

  const didSubmit = message.autoSubmit !== false ? clickSubmit(broker) : false;
  sendResponse({ ok: true, broker, didSubmit });
});

// ─── Auto-Fill on Detection ──────────────────────────────────────────────────
let _autoFilled = false;

function checkAndAutoFillOnDetect() {
  if (_autoFilled) return;
  const broker = detectBroker();
  if (broker === "unknown") return;

  const multiBox = findMultiBoxInputs();
  const input = multiBox ? multiBox[0] : findInput(broker);

  if (!input) return;
  if (input.value && input.value.trim().length >= 6) return;

  chrome.runtime.sendMessage({ type: "GET_AUTO_TOTP", broker }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.ok || !resp.code) return;

    if (multiBox && multiBox.length === 6) {
      const chars = resp.code.split("");
      multiBox.forEach((box, i) => {
        box.focus();
        setNativeValue(box, chars[i]);
      });
    } else if (input) {
      input.focus();
      setNativeValue(input, resp.code);
    }

    _autoFilled = true;
    showToast(`⚡ GenTOTP: Auto-filled ${resp.label || broker} TOTP on detection`);
    clickSubmit(broker);
  });
}

// Run periodic check to detect 2FA input as soon as it appears
setInterval(checkAndAutoFillOnDetect, 800);

console.log(`[GenTOTP] Content script active on ${detectBroker()} (${window.location.hostname})`);
