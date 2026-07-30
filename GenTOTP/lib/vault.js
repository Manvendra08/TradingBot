/**
 * GenTOTP – Crypto Vault
 * AES-256-GCM encryption/decryption of TOTP secrets.
 * Master password → PBKDF2 key → AES-GCM encrypted blob stored in chrome.storage.local
 */

"use strict";

const CryptoVault = (() => {

  const PBKDF2_ITERATIONS = 310_000; // OWASP 2023 recommended minimum
  const SALT_LENGTH       = 16;      // bytes
  const IV_LENGTH         = 12;      // bytes (96-bit for AES-GCM)
  const KEY_BITS          = 256;

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function bufToB64(buf) {
    return btoa(String.fromCharCode(...new Uint8Array(buf)));
  }

  function b64ToBuf(b64) {
    return Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer;
  }

  async function deriveKey(password, salt) {
    const enc = new TextEncoder();
    const rawKey = await crypto.subtle.importKey(
      "raw",
      enc.encode(password),
      "PBKDF2",
      false,
      ["deriveKey"]
    );
    return crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt,
        iterations: PBKDF2_ITERATIONS,
        hash: "SHA-256"
      },
      rawKey,
      { name: "AES-GCM", length: KEY_BITS },
      false,
      ["encrypt", "decrypt"]
    );
  }

  // ---------------------------------------------------------------------------
  // Encrypt plaintext string → opaque blob object { salt, iv, ct }
  // ---------------------------------------------------------------------------
  async function encrypt(plaintext, password) {
    const enc  = new TextEncoder();
    const salt = crypto.getRandomValues(new Uint8Array(SALT_LENGTH));
    const iv   = crypto.getRandomValues(new Uint8Array(IV_LENGTH));
    const key  = await deriveKey(password, salt);

    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      enc.encode(plaintext)
    );

    return {
      salt: bufToB64(salt),
      iv:   bufToB64(iv),
      ct:   bufToB64(ciphertext)
    };
  }

  // ---------------------------------------------------------------------------
  // Decrypt blob → plaintext string (throws if wrong password)
  // ---------------------------------------------------------------------------
  async function decrypt(blob, password) {
    const salt = new Uint8Array(b64ToBuf(blob.salt));
    const iv   = new Uint8Array(b64ToBuf(blob.iv));
    const ct   = b64ToBuf(blob.ct);
    const key  = await deriveKey(password, salt);

    const plainBuf = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv },
      key,
      ct
    );

    return new TextDecoder().decode(plainBuf);
  }

  // ---------------------------------------------------------------------------
  // Session: cache derived key material in memory for the popup session.
  // Cleared when service worker is idle.
  // ---------------------------------------------------------------------------
  let _sessionPassword = null;

  function setSessionPassword(pw) { _sessionPassword = pw; }
  function clearSession()         { _sessionPassword = null; }
  function hasSession()           { return _sessionPassword !== null; }
  function getSessionPassword()   { return _sessionPassword; }

  return { encrypt, decrypt, setSessionPassword, clearSession, hasSession, getSessionPassword };
})();

if (typeof module !== "undefined") module.exports = CryptoVault;
