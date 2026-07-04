import logging
import tempfile
import zipfile
from pathlib import Path

import requests
from config.settings import DB_PATH, DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("nsebot.backup")

TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024       # 50 MB
CHUNK_THRESHOLD = 48 * 1024 * 1024            # 48 MB — compress above this
CHUNK_SIZE = 40 * 1024 * 1024                 # 40 MB per chunk when splitting


def _tg_send_file(file_path: Path, caption: str, session: requests.Session) -> dict:
    """Post a single file to Telegram. Returns the API response dict."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    mime = "application/zip" if file_path.suffix == ".zip" else "application/x-sqlite3"
    with open(file_path, "rb") as f:
        files = {"document": (file_path.name, f, mime)}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
        resp = session.post(url, files=files, data=data, timeout=120)
    return resp.json()


def backup_db_to_telegram() -> bool:
    """Sends the local database file to Telegram with compression & chunking."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram token or Chat ID not configured. Backup skipped.")
        return False

    if not DB_PATH.exists():
        log.error(f"Local database not found at {DB_PATH}. Cannot backup.")
        return False

    from src.utils.tls_adapter import mount_resilient_tls
    session = requests.Session()
    mount_resilient_tls(session)

    file_size = DB_PATH.stat().st_size
    log.info("Database size: %.1f MB", file_size / (1024 * 1024))

    # ── Determine which file(s) to send ──────────────────────────────────
    if file_size < TELEGRAM_FILE_LIMIT:
        # Small enough to send directly
        log.info("Sending database directly (%.1f MB < 50 MB)...", file_size / (1024 * 1024))
        res = _tg_send_file(DB_PATH, f"Database backup: {DB_PATH.name}", session)
        if res.get("ok"):
            log.info("Database backup sent to Telegram successfully.")
            return True
        if res.get("error_code") == 413:
            log.warning("413 on direct send — will retry with compression.")
        else:
            log.error("Telegram backup API error: %s", res)
            return False

    # ── Compress with zip ────────────────────────────────────────────────
    tmp = tempfile.TemporaryDirectory()
    zip_path = Path(tmp.name) / f"{DB_PATH.stem}.zip"
    log.info("Compressing database to %s ...", zip_path.name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(DB_PATH, arcname=DB_PATH.name)

    zipped_size = zip_path.stat().st_size
    log.info("Compressed size: %.1f MB", zipped_size / (1024 * 1024))

    if zipped_size < CHUNK_THRESHOLD:
        res = _tg_send_file(zip_path, f"Compressed DB backup: {zip_path.name}", session)
        if res.get("ok"):
            log.info("Compressed database backup sent to Telegram.")
            tmp.cleanup()
            return True
        log.error("Telegram backup API error after compression: %s", res)
        tmp.cleanup()
        return False

    # ── Split into chunks ────────────────────────────────────────────────
    log.warning("Zipped file (%.1f MB) exceeds Telegram limit — splitting into chunks.",
                zipped_size / (1024 * 1024))
    part_num = 0
    with open(zip_path, "rb") as src:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            part_num += 1
            part_path = Path(tmp.name) / f"{DB_PATH.stem}.zip.{part_num:03d}"
            part_path.write_bytes(chunk)
            caption = f"DB backup part {part_num} ({len(chunk) / (1024 * 1024):.1f} MB)"
            res = _tg_send_file(part_path, caption, session)
            if not res.get("ok"):
                log.error("Failed to send chunk %d: %s", part_num, res)
                _send_discord_notification(
                    f"❌ Telegram backup failed — chunk {part_num} rejected: {res.get('description', 'unknown')}"
                )
                tmp.cleanup()
                return False
            log.info("Sent chunk %d", part_num)

    log.info("All %d chunks sent successfully.", part_num)
    _send_discord_notification(
        f"✅ DB backup sent to Telegram ({part_num} chunks, {file_size / (1024 * 1024):.1f} MB compressed to {zipped_size / (1024 * 1024):.1f} MB)"
    )
    tmp.cleanup()
    return True


def _send_discord_notification(text: str):
    """Fallback notification when Telegram backup encounters issues."""
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "your_discord_webhook_url":
        return
    try:
        import json
        import urllib.request
        payload = json.dumps({"content": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "NSEBOT/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        log.warning("Discord fallback notification failed: %s", e)
