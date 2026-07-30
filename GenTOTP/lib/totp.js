/**
 * GenTOTP – TOTP Core Library
 * RFC 6238 compliant TOTP using the native Web Crypto API.
 * No external dependencies. Works in both popup and service worker contexts.
 */

"use strict";

const TOTP = (() => {

  // ---------------------------------------------------------------------------
  // Base32 Decoder (RFC 4648)
  // ---------------------------------------------------------------------------
  const BASE32_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

  function base32Decode(base32) {
    const cleaned = base32.toUpperCase().replace(/=+$/, "").replace(/\s/g, "");
    let bits = 0;
    let value = 0;
    let index = 0;
    const output = new Uint8Array(Math.ceil((cleaned.length * 5) / 8));

    for (let i = 0; i < cleaned.length; i++) {
      const charIdx = BASE32_CHARS.indexOf(cleaned[i]);
      if (charIdx === -1) throw new Error(`Invalid Base32 character: '${cleaned[i]}'`);
      value = (value << 5) | charIdx;
      bits += 5;
      if (bits >= 8) {
        output[index++] = (value >>> (bits - 8)) & 0xff;
        bits -= 8;
      }
    }

    return output.slice(0, index);
  }

  // ---------------------------------------------------------------------------
  // HOTP (HMAC-based OTP) – RFC 4226
  // ---------------------------------------------------------------------------
  async function generateHOTP(keyBytes, counter, digits = 6) {
    // Pack counter as big-endian 8-byte integer
    const counterBytes = new Uint8Array(8);
    let remaining = counter;
    for (let i = 7; i >= 0; i--) {
      counterBytes[i] = remaining & 0xff;
      remaining = Math.floor(remaining / 256);
    }

    // Import key for HMAC-SHA1
    const cryptoKey = await crypto.subtle.importKey(
      "raw",
      keyBytes,
      { name: "HMAC", hash: "SHA-1" },
      false,
      ["sign"]
    );

    const signature = await crypto.subtle.sign("HMAC", cryptoKey, counterBytes);
    const hmac = new Uint8Array(signature);

    // Dynamic truncation
    const offset = hmac[hmac.length - 1] & 0x0f;
    const code =
      ((hmac[offset]     & 0x7f) << 24) |
      ((hmac[offset + 1] & 0xff) << 16) |
      ((hmac[offset + 2] & 0xff) << 8)  |
       (hmac[offset + 3] & 0xff);

    const otp = code % Math.pow(10, digits);
    return String(otp).padStart(digits, "0");
  }

  // ---------------------------------------------------------------------------
  // TOTP (Time-based OTP) – RFC 6238
  // ---------------------------------------------------------------------------
  async function generate(base32Secret, { digits = 6, period = 30 } = {}) {
    const normalised = base32Secret.toUpperCase().replace(/[\s\-_]/g, "").replace(/=+$/, "");
    const keyBytes = base32Decode(normalised);
    const counter = Math.floor(Date.now() / 1000 / period);
    return generateHOTP(keyBytes, counter, digits);
  }

  /**
   * Returns seconds remaining in the current 30-second window.
   */
  function secondsRemaining(period = 30) {
    return period - (Math.floor(Date.now() / 1000) % period);
  }

  /**
   * Validates that a Base32 secret looks correct (length, charset).
   * Handles spaces, hyphens, lowercase, and trailing padding.
   */
  function validateSecret(base32) {
    // Strip whitespace, hyphens, underscores (some authenticator apps add them)
    const cleaned = base32.toUpperCase()
      .replace(/[\s\-_]/g, "")
      .replace(/=+$/, "");

    if (cleaned.length === 0) return { valid: false, error: "Secret cannot be empty" };
    if (cleaned.length < 8)  return { valid: false, error: "Secret too short (min 8 chars)" };

    // Detect Fernet-encrypted blobs (start with 'gAAAAA')
    if (cleaned.startsWith("GAAAAA")) {
      return {
        valid: false,
        error: "This looks like an encrypted secret from NSEBOT. Paste the raw Base32 key from Zerodha/Shoonya 2FA setup page instead."
      };
    }

    // Detect JWT / bearer tokens
    if (base32.trim().startsWith("eyJ")) {
      return { valid: false, error: "This looks like a JWT token, not a TOTP secret" };
    }

    if (!/^[A-Z2-7]+$/.test(cleaned)) {
      // Find the first bad char to help the user
      const bad = [...cleaned].find(c => !/[A-Z2-7]/.test(c));
      return {
        valid: false,
        error: `Invalid character '${bad}' — Base32 uses only letters A-Z and digits 2-7`
      };
    }
    return { valid: true, cleaned };
  }

  /**
   * Normalises a secret before use (same stripping as validateSecret).
   */
  function normaliseSecret(base32) {
    return base32.toUpperCase().replace(/[\s\-_]/g, "").replace(/=+$/, "");
  }

  return { generate, secondsRemaining, validateSecret, normaliseSecret, base32Decode };
})();

// Export for use as module (service worker) or global (popup via <script>)
if (typeof module !== "undefined") module.exports = TOTP;
