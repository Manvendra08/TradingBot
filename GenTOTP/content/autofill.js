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
  '.twofa-form input[type="number"]',
  '.twofa-form input[type="text"]',
  '.twofa-form input',
  'input[type="number"][placeholder]',
  'input[name="totp"]',
  'input[id*="totp" i]',
  'input[placeholder*="TOTP" i]',
  'input[placeholder*="Authenticator" i]',
  'input[placeholder*="2FA" i]',
];

// Shoonya uses Flutter Web — the active editable input carries class .flt-text-editing
// The 3rd visible flt-text-editing input on the login page is the OTP/TOTP field
const SHOONYA_SELECTORS = [
  'input.flt-text-editing.transparentTextEditing',   // Flutter Web active TOTP field (exact user-reported class)
  'input.flt-text-editing',                          // Flutter Web generic editable input
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

// ─── TOTP Input Finder ───────────────────────────────────────────────────────
function findInput(broker) {
  const selectors = broker === "zerodha"
    ? [...ZERODHA_SELECTORS, ...SHOONYA_SELECTORS]
    : [...SHOONYA_SELECTORS, ...ZERODHA_SELECTORS];

  // 1. Explicit broker selectors
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    } catch (_) {}
  }

  // 2. Keyword scan across all visible inputs
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
    if (/(totp|otp|2fa|twofa|one.?time|auth.*code|security.?code)/.test(sig)) return el;
    if (el.getAttribute("autocomplete") === "one-time-code") return el;
    if (el.getAttribute("maxlength") === "6") return el;
  }

  // 3. Structural last-resort
  const userInputs = allVisible.filter(el => {
    const t = (el.type || "").toLowerCase();
    return ["text","number","password","tel",""].includes(t);
  });

  if (userInputs.length === 1) {
    const el = userInputs[0];
    const sig = (el.name + el.id + el.placeholder).toLowerCase();
    if (!/username|userid|user_id|password|passwd/.test(sig)) return el;
  }

  // Shoonya Flutter: 3 flt-text-editing fields (User ID, Password, OTP) → 3rd is TOTP
  const flutterInputs = Array.from(document.querySelectorAll("input.flt-text-editing")).filter(isVisible);
  if (flutterInputs.length === 3) return flutterInputs[2];
  if (flutterInputs.length === 1) return flutterInputs[0];

  if (userInputs.length === 3) {
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

// ─── React/Angular Value Setter ──────────────────────────────────────────────
function setNativeValue(el, value) {
  const nativeSetter =
    Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")?.set ||
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (nativeSetter) nativeSetter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input",  { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur",   { bubbles: true }));
}

// ─── Flutter Web Value Setter ─────────────────────────────────────────────────
// Flutter Web ignores el.value assignments and generic DOM events.
// It only responds to execCommand('insertText') or compositionend + InputEvent with insertText.
function setFlutterValue(el, value) {
  el.focus();

  // Step 1: Select all existing text (so we replace rather than append)
  el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "a", keyCode: 65, ctrlKey: true }));
  el.select && el.select();

  // Step 2: Use execCommand insertText — Flutter's text controller listens to this
  const inserted = document.execCommand("insertText", false, value);

  if (!inserted) {
    // Fallback: dispatch InputEvent with insertText type (works in some Flutter versions)
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      inputType: "insertText",
      data: value,
    }));
  }

  // Step 3: Also fire compositionend in case Flutter is in composition mode
  el.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: value }));
}

// ─── Is Flutter Input? ────────────────────────────────────────────────────────
function isFlutterInput(el) {
  return el && (
    el.classList.contains("flt-text-editing") ||
    el.classList.contains("transparentTextEditing")
  );
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
function performFill(code, broker, autoSubmit) {
  const multiBox = findMultiBoxInputs();
  if (multiBox) {
    code.split("").forEach((ch, i) => {
      multiBox[i].focus();
      isFlutterInput(multiBox[i]) ? setFlutterValue(multiBox[i], ch) : setNativeValue(multiBox[i], ch);
    });
    showToast(`✅ GenTOTP: ${broker} TOTP filled`);
    if (autoSubmit) clickSubmit();
    return true;
  }

  const input = findInput(broker);
  if (!input) {
    showToast("❌ TOTP field not found on this page", "error");
    return false;
  }

  input.focus();
  if (isFlutterInput(input)) {
    setFlutterValue(input, code);
  } else {
    setNativeValue(input, code);
  }
  showToast(`✅ GenTOTP: ${broker} TOTP filled`);
  if (autoSubmit) clickSubmit();
  return true;
}

// ─── Message Listener ────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "FILL_TOTP") return;
  const broker = detectBroker();
  const ok = performFill(message.code, broker, message.autoSubmit !== false);
  sendResponse({ ok, broker });
});

// ─── Instant Auto-Fill on Detection ─────────────────────────────────────────
let _lastAutoFilledEl = null;

function checkAndAutoFillOnDetect() {
  const broker = detectBroker();
  if (broker === "unknown") return;
  const multiBox = findMultiBoxInputs();
  const input = multiBox ? multiBox[0] : findInput(broker);
  if (!input) return;
  if (_lastAutoFilledEl === input) return;
  if ((input.value || "").trim().length >= 6) return;

  chrome.runtime.sendMessage({ type: "GET_AUTO_TOTP", broker }, (resp) => {
    if (chrome.runtime.lastError || !resp?.ok || !resp.code) return;
    const ok = performFill(resp.code, resp.label || broker, true);
    if (ok) _lastAutoFilledEl = input;
  });
}

setInterval(checkAndAutoFillOnDetect, 800);
console.log(`[GenTOTP] content script on ${detectBroker()} (${window.location.hostname})`);
