import os
import sqlite3
import json
import base64
import shutil
from pathlib import Path
import ctypes
from ctypes import wintypes

# Windows DPAPI structures and functions
class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte))
    ]

def decrypt_data(encrypted_data):
    try:
        # Load crypt32.dll
        crypt32 = ctypes.windll.crypt32
        
        # Prepare inputs
        in_blob = DATA_BLOB()
        in_blob.cbData = len(encrypted_data)
        in_blob.pbData = ctypes.cast(ctypes.create_string_buffer(encrypted_data), ctypes.POINTER(ctypes.c_byte))
        
        out_blob = DATA_BLOB()
        
        # Call CryptUnprotectData
        # CryptUnprotectData(pDataIn, ppszDataDescr, pOptionalEntropy, pvReserved, pPromptStruct, dwFlags, pDataOut)
        if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            # Read output data
            size = out_blob.cbData
            address = out_blob.pbData
            decrypted = ctypes.string_at(address, size)
            
            # Free memory (LocalFree)
            ctypes.windll.kernel32.LocalFree(address)
            return decrypted
        else:
            return None
    except Exception as e:
        print(f"DPAPI decryption failed: {e}")
        return None

def decrypt_v10_or_higher(encrypted_value, key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        # AES-GCM decryption
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as e:
        # Fallback to DPAPI if encryption prefix is different
        return decrypt_data(encrypted_value)

def get_encryption_key(local_state_path):
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        # Strip DPAPI prefix "DPAPI"
        encrypted_key = encrypted_key[5:]
        # Decrypt key using DPAPI
        return decrypt_data(encrypted_key)
    except Exception as e:
        print(f"Failed to get key from {local_state_path}: {e}")
        return None

def extract_cookies(profile_dir, browser_name):
    print(f"\nChecking {browser_name} in {profile_dir}...")
    profile_path = Path(profile_dir)
    if not profile_path.exists():
        print(f"{browser_name} profile directory not found.")
        return None
        
    cookies_path = profile_path / "Default" / "Network" / "Cookies"
    if not cookies_path.exists():
        cookies_path = profile_path / "Default" / "Cookies"
        if not cookies_path.exists():
            # Try other profile directories if any, or check profile_path directly
            cookies_path = profile_path / "Network" / "Cookies"
            if not cookies_path.exists():
                print(f"Cookies file not found in standard paths.")
                return None
                
    local_state_path = profile_path.parent / "Local State"
    if not local_state_path.exists():
        local_state_path = profile_path / "Local State"
        if not local_state_path.exists():
            print(f"Local State file not found.")
            return None
            
    key = get_encryption_key(local_state_path)
    if not key:
        print("Could not retrieve decryption key.")
        return None
        
    # Copy cookies file to temp path to avoid lock
    temp_cookies = Path("scratch") / f"{browser_name}_cookies.db"
    try:
        shutil.copy(cookies_path, temp_cookies)
    except Exception as e:
        print(f"Failed to copy cookies database: {e}")
        return None
        
    conn = sqlite3.connect(temp_cookies)
    cursor = conn.cursor()
    
    session_id = None
    try:
        # Query for tradingview.com cookies
        cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%tradingview.com%'")
        rows = cursor.fetchall()
        print(f"Found {len(rows)} tradingview.com cookies.")
        for host, name, enc_val in rows:
            decrypted = None
            if enc_val.startswith(b"v10") or enc_val.startswith(b"v11"):
                decrypted = decrypt_v10_or_higher(enc_val, key)
            else:
                decrypted = decrypt_data(enc_val)
                
            if decrypted:
                val = decrypted.decode("utf-8", errors="ignore")
                if name == "sessionid":
                    print(f"FOUND cookie 'sessionid': {val[:8]}...{val[-8:] if len(val) > 8 else ''}")
                    session_id = val
    except Exception as e:
        print(f"Error querying/decrypting cookies: {e}")
    finally:
        conn.close()
        try:
            temp_cookies.unlink()
        except:
            pass
            
    return session_id

def main():
    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        print("USERPROFILE environment variable not found.")
        return
        
    # Chrome
    chrome_path = Path(user_profile) / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    session_id = extract_cookies(chrome_path, "Chrome")
    if session_id:
        print(f"SUCCESS: Extracted sessionid: {session_id}")
        return
        
    # Edge
    edge_path = Path(user_profile) / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
    session_id = extract_cookies(edge_path, "Edge")
    if session_id:
        print(f"SUCCESS: Extracted sessionid: {session_id}")
        return

    print("Could not find tradingview.com sessionid in Chrome or Edge.")

if __name__ == "__main__":
    main()
