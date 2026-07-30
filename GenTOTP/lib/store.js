/**
 * GenTOTP – Account Storage
 * Manages account list in chrome.storage.local.
 * Each account: { id, label, broker, encryptedSecret: { salt, iv, ct } }
 * Broker values: "zerodha" | "shoonya" | "other"
 */

"use strict";

const AccountStore = (() => {

  const STORAGE_KEY     = "gentop_accounts";
  const VAULT_INIT_KEY  = "gentop_vault_initialized";

  function generateId() {
    return crypto.randomUUID ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  // ---------------------------------------------------------------------------
  // Folder-level persistent JSON sync (Zero-Config cross-browser loading)
  // ---------------------------------------------------------------------------
  async function seedFromLocalFolder(masterPw = "12345678") {
    try {
      const url = chrome.runtime.getURL("vault_data.json");
      const resp = await fetch(url);
      if (!resp.ok) return false;

      const data = await resp.json();
      if (!data || !data.accounts || data.accounts.length === 0) return false;

      const accountsToStore = [];
      for (const acc of data.accounts) {
        let encSecret = acc.encryptedSecret;
        if (!encSecret && acc.secret && typeof CryptoVault !== "undefined") {
          encSecret = await CryptoVault.encrypt(acc.secret, masterPw);
        }
        if (encSecret) {
          accountsToStore.push({
            id: acc.id || generateId(),
            label: acc.label,
            broker: acc.broker,
            encryptedSecret: encSecret,
            createdAt: Date.now()
          });
        }
      }

      if (accountsToStore.length > 0) {
        await writeAll(accountsToStore);
        await setVaultInitialized(true);
        await new Promise(r => chrome.storage.local.set({
          gentop_require_pw: data.require_pw !== undefined ? data.require_pw : false,
          gentop_auto_fill_detect: data.auto_fill_detect !== undefined ? data.auto_fill_detect : true,
          gentop_cached_pw: data.cached_pw || masterPw
        }, r));
        if (typeof CryptoVault !== "undefined") {
          CryptoVault.setSessionPassword(data.cached_pw || masterPw);
        }
        return true;
      }
    } catch (err) {
      console.warn("[GenTOTP] Could not seed from local vault_data.json:", err);
    }
    return false;
  }

  // ---------------------------------------------------------------------------
  // Raw read/write
  // ---------------------------------------------------------------------------
  async function readAll() {
    return new Promise(resolve => {
      chrome.storage.local.get([STORAGE_KEY], async result => {
        let accounts = result[STORAGE_KEY] || [];
        if (accounts.length === 0) {
          const seeded = await seedFromLocalFolder();
          if (seeded) {
            accounts = await new Promise(r => chrome.storage.local.get([STORAGE_KEY], res => r(res[STORAGE_KEY] || [])));
          }
        }
        resolve(accounts);
      });
    });
  }

  async function writeAll(accounts) {
    return new Promise(resolve => {
      chrome.storage.local.set({ [STORAGE_KEY]: accounts }, resolve);
    });
  }

  // ---------------------------------------------------------------------------
  // Vault state
  // ---------------------------------------------------------------------------
  async function isVaultInitialized() {
    return new Promise(resolve => {
      chrome.storage.local.get([VAULT_INIT_KEY], async result => {
        let init = !!result[VAULT_INIT_KEY];
        if (!init) {
          const seeded = await seedFromLocalFolder();
          if (seeded) init = true;
        }
        resolve(init);
      });
    });
  }

  async function setVaultInitialized(flag = true) {
    return new Promise(resolve => {
      chrome.storage.local.set({ [VAULT_INIT_KEY]: flag }, resolve);
    });
  }

  // ---------------------------------------------------------------------------
  // Account CRUD
  // ---------------------------------------------------------------------------
  async function addAccount({ label, broker, encryptedSecret }) {
    const accounts = await readAll();
    const account = { id: generateId(), label, broker, encryptedSecret, createdAt: Date.now() };
    accounts.push(account);
    await writeAll(accounts);
    return account;
  }

  async function removeAccount(id) {
    const accounts = await readAll();
    await writeAll(accounts.filter(a => a.id !== id));
  }

  async function updateAccount(id, updates) {
    const accounts = await readAll();
    const idx = accounts.findIndex(a => a.id === id);
    if (idx === -1) throw new Error("Account not found");
    accounts[idx] = { ...accounts[idx], ...updates };
    await writeAll(accounts);
    return accounts[idx];
  }

  async function getAccount(id) {
    const accounts = await readAll();
    return accounts.find(a => a.id === id) || null;
  }

  return {
    readAll,
    addAccount,
    removeAccount,
    updateAccount,
    getAccount,
    isVaultInitialized,
    setVaultInitialized,
    seedFromLocalFolder
  };
})();

if (typeof module !== "undefined") module.exports = AccountStore;
