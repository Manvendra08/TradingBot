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

// ─── Precise TOTP Input Inspector ───────────────────────────────────────────
function isTotpInput(el) {
  if (!isElementVisible(el)) return false;
  if (el.tagName !== "INPUT") return false;

  const type = (el.type || "").toLowerCase();
  if (["hidden", "submit", "button", "checkbox", "radio", "image", "file"].includes(type)) return false;

  const name = (el.name || "").toLowerCase();
  const id = (el.id || "").toLowerCase();
  const placeholder = (el.placeholder || "").toLowerCase();
  const className = (el.className || "").toLowerCase();
  const formControl = (el.getAttribute("formcontrolname") || "").toLowerCase();
  const ariaLabel = (el.getAttribute("aria-label") || "").toLowerCase();
  const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();

  const fullText = `${name} ${id} ${placeholder} ${className} ${formControl} ${ariaLabel} ${autocomplete}`;

  // 1. Never match primary username or password inputs
  if (fullText.includes("password") || fullText.includes("passwd") || fullText.includes("username") || name === "userid" || id === "userid") {
    // Exception: if placeholder specifically says OTP/TOTP alongside password type
    if (!placeholder.includes("otp") && !placeholder.includes("totp") && !id.includes("otp") && !name.includes("otp")) {
      return false;
    }
  }

  // 2. Positive 2FA / TOTP attribute indicators
  if (
    fullText.includes("totp") ||
    fullText.includes("otp") ||
    fullText.includes("2fa") ||
    fullText.includes("authenticator") ||
    fullText.includes("auth_code") ||
    fullText.includes("security_code") ||
    fullText.includes("twofa") ||
    autocomplete === "one-time-code"
  ) {
    return true;
  }

  // 3. Length or type constraints (e.g. 6-digit numeric input)
  if (el.getAttribute("maxlength") === "6" || el.getAttribute("maxlength") === "8") {
    return true;
  }

  return false;
}

// ─── Input Field Discovery ───────────────────────────────────────────────────
function findInput(broker) {
  // 1. Search all inputs on page using isTotpInput inspector
  const allInputs = Array.from(document.querySelectorAll("input"));
  for (const input of allInputs) {
    if (isTotpInput(input)) return input;
  }

  // 2. Active/focused element check
  const active = document.activeElement;
  if (active && active.tagName === "INPUT" && isElementVisible(active) && isTotpInput(active)) {
    return active;
  }

  // 3. Form-based structural heuristics:
  // If page has 3 inputs (e.g. Shoonya: User ID, Password, OTP/TOTP) or 1 input on a 2FA screen
  const visibleInputs = allInputs.filter(el => {
    if (!isElementVisible(el)) return false;
    const t = (el.type || "").toLowerCase();
    return !["hidden", "submit", "button", "checkbox", "radio"].includes(t);
  });

  if (visibleInputs.length === 1) {
    // On dedicated 2FA screen (like Zerodha Step 2)
    const single = visibleInputs[0];
    const t = (single.name + " " + single.id + " " + single.placeholder).toLowerCase();
    if (!t.includes("username") && !t.includes("userid")) {
      return single;
    }
  } else if (visibleInputs.length === 3) {
    // Single-page 3-field login (like Shoonya: User ID, Password, OTP)
    const third = visibleInputs[2];
    const name = (third.name || "").toLowerCase();
    const id = (third.id || "").toLowerCase();
    if (!name.includes("user") && !id.includes("user")) {
      return third;
    }
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
  el.dispatchEvent(new Event("blur",   { bubbles: true }));
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
      setTimeout(() => btn.click(), 300);
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
let _autoFilledInput = null;

function checkAndAutoFillOnDetect() {
  const broker = detectBroker();
  if (broker === "unknown") return;

  const multiBox = findMultiBoxInputs();
  const input = multiBox ? multiBox[0] : findInput(broker);

  if (!input) return;
  if (_autoFilledInput === input) return;
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

    _autoFilledInput = input;
    showToast(`⚡ GenTOTP: Auto-filled ${resp.label || broker} TOTP on detection`);
    clickSubmit(broker);
  });
}

// Run periodic check to detect 2FA input as soon as it appears
setInterval(checkAndAutoFillOnDetect, 800);

console.log(`[GenTOTP] Content script active on ${detectBroker()} (${window.location.hostname})`);
