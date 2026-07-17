import os
import pyotp
import logging
from pathlib import Path
from cryptography.fernet import Fernet
from src.models.schema import get_broker_config, update_broker_config

log = logging.getLogger("nsebot.zerodha_auth")

KEY_PATH = Path("data/.fernet_key")

def _get_fernet() -> Fernet:
    if not KEY_PATH.exists():
        if not KEY_PATH.parent.exists():
            try:
                KEY_PATH.parent.mkdir(exist_ok=True, parents=True)
            except Exception as e:
                log.warning("[zerodha_auth] Could not create parent directory %s: %s", KEY_PATH.parent, e)
        key = Fernet.generate_key()
        KEY_PATH.write_bytes(key)
    else:
        key = KEY_PATH.read_bytes()
    return Fernet(key)

def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    f = _get_fernet()
    return f.encrypt(secret.encode("utf-8")).decode("utf-8")

def decrypt_secret(encrypted: str) -> str:
    if not encrypted:
        return ""
    f = _get_fernet()
    try:
        return f.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except Exception:
        log.exception("Failed to decrypt TOTP secret")
        return ""

def get_current_totp() -> str:
    config = get_broker_config()
    if not config or not config.get("totp_secret"):
        log.warning("No TOTP secret configured in database")
        return ""
    
    enc_secret = config["totp_secret"]
    secret = decrypt_secret(enc_secret)
    if not secret:
        return ""
    try:
        totp = pyotp.TOTP(secret.strip().replace(" ", ""))
        return totp.now()
    except Exception:
        log.exception("Failed to generate TOTP code")
        return ""

def is_token_valid() -> bool:
    config = get_broker_config()
    if not config or not config.get("access_token") or not config.get("last_login_date"):
        return False
    
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    return config["last_login_date"] == today


def invalidate_token() -> None:
    """Clear access token and last login date to force re-auth on next check."""
    log.warning("[zerodha_auth] Invalidating Kite access token to force re-login.")
    try:
        update_broker_config(access_token="", last_login_date="")
        try:
            from src.engine import live_trading
            live_trading._cached_kite_client = None
            live_trading._cached_access_token = None
        except Exception:
            pass
    except Exception as e:
        log.error("Failed to clear broker config: %s", e)
