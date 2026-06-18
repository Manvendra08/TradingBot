import os
import ctypes
from ctypes import wintypes
import sqlite3
import shutil
import base64
import json
from pathlib import Path

# Win32 Constants
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = -1

# Types
LPDWORD = ctypes.POINTER(wintypes.DWORD)
LPVOID = ctypes.c_void_p

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte))
    ]

def decrypt_data(encrypted_data):
    try:
        crypt32 = ctypes.windll.crypt32
        in_blob = DATA_BLOB()
        in_blob.cbData = len(encrypted_data)
        in_blob.pbData = ctypes.cast(ctypes.create_string_buffer(encrypted_data), ctypes.POINTER(ctypes.c_byte))
        
        out_blob = DATA_BLOB()
        
        if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            size = out_blob.cbData
            address = out_blob.pbData
            decrypted = ctypes.string_at(address, size)
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
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as e:
        return decrypt_data(encrypted_value)

def get_encryption_key(local_state_path):
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        encrypted_key = encrypted_key[5:]
        return decrypt_data(encrypted_key)
    except Exception as e:
        print(f"Failed to get key: {e}")
        return None

def copy_locked_file(src, dst):
    """Copy a locked file by opening it with FILE_SHARE_READ and FILE_SHARE_WRITE."""
    src_path = os.path.abspath(src)
    dst_path = os.path.abspath(dst)
    
    # Open source file with sharing
    handle = ctypes.windll.kernel32.CreateFileW(
        src_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None
    )
    
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.windll.kernel32.GetLastError()
        print(f"CreateFileW failed for {src}. Error: {err}")
        return False
        
    try:
        # Read from source and write to destination
        chunk_size = 4096
        buffer = ctypes.create_string_buffer(chunk_size)
        bytes_read = wintypes.DWORD(0)
        
        with open(dst_path, "wb") as f_out:
            while True:
                ret = ctypes.windll.kernel32.ReadFile(
                    handle,
                    buffer,
                    chunk_size,
                    ctypes.byref(bytes_read),
                    None
                )
                if not ret or bytes_read.value == 0:
                    break
                f_out.write(buffer.raw[:bytes_read.value])
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

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
        
    temp_cookies = Path("scratch") / f"locked_{browser_name}_cookies.db"
    
    print(f"Attempting to copy locked cookies file...")
    if not copy_locked_file(cookies_path, temp_cookies):
        print("Failed to copy locked cookies database.")
        return None
        
    conn = sqlite3.connect(temp_cookies)
    cursor = conn.cursor()
    
    session_id = None
    try:
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
        print(f"\nSUCCESS: Extracted sessionid: {session_id}")
        return
        
    # Edge
    edge_path = Path(user_profile) / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
    session_id = extract_cookies(edge_path, "Edge")
    if session_id:
        print(f"\nSUCCESS: Extracted sessionid: {session_id}")
        return

    print("Could not find tradingview.com sessionid in Chrome or Edge.")

if __name__ == "__main__":
    main()
