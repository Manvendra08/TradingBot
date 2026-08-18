import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from config.settings import DB_PATH, DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("nsebot.backup")

TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024       # 50 MB
CHUNK_THRESHOLD = 48 * 1024 * 1024            # 48 MB — compress above this
CHUNK_SIZE = 40 * 1024 * 1024                 # 40 MB per chunk when splitting
IST = timezone(timedelta(hours=5, minutes=30))


def _tg_send_file(file_path: Path, caption: str, session: requests.Session) -> dict:
    """Post a single file to Telegram. Returns the API response dict."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    mime = "application/zip" if file_path.suffix == ".zip" else "application/x-sqlite3"
    with open(file_path, "rb") as f:
        files = {"document": (file_path.name, f, mime)}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
        resp = session.post(url, files=files, data=data, timeout=120)
    return resp.json()


def backup_db_to_telegram() -> bool:
    """Creates a point-in-time online SQLite backup, compresses to ZIP, and pushes to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram token or Chat ID not configured. Backup skipped.")
        return False

    if not DB_PATH.exists():
        log.error(f"Local database not found at {DB_PATH}. Cannot backup.")
        return False

    now_ist = datetime.now(IST)
    now_str = now_ist.strftime("%Y%m%d_%H%M%S")
    now_human = now_ist.strftime("%d-%b-%Y %I:%M:%S %p IST")

    from src.utils.tls_adapter import mount_resilient_tls
    session = requests.Session()
    mount_resilient_tls(session)

    # 1. Point-in-time online SQLite backup
    tmp_dir = tempfile.TemporaryDirectory()
    backup_db_path = Path(tmp_dir.name) / f"nsebot_prod_{now_str}.db"
    log.info("Creating online SQLite backup at %s...", backup_db_path.name)

    stats = []
    tables = []
    try:
        src_conn = sqlite3.connect(str(DB_PATH))
        dst_conn = sqlite3.connect(str(backup_db_path))
        src_conn.backup(dst_conn)

        # Collect table stats
        c = dst_conn.cursor()
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for t in sorted(tables):
            try:
                cnt = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                stats.append((t, cnt))
            except Exception:
                pass

        src_conn.close()
        dst_conn.close()
    except Exception as e:
        log.error("SQLite backup failed: %s", e)
        tmp_dir.cleanup()
        return False

    raw_size = backup_db_path.stat().st_size
    raw_size_mb = raw_size / (1024 * 1024)
    log.info("Database snapshot size: %.2f MB across %d tables", raw_size_mb, len(tables))

    # 2. Compress into ZIP
    zip_path = Path(tmp_dir.name) / f"nsebot_prod_backup_{now_str}.zip"
    log.info("Compressing snapshot to %s ...", zip_path.name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(backup_db_path, arcname=backup_db_path.name)

    zipped_size = zip_path.stat().st_size
    zipped_size_mb = zipped_size / (1024 * 1024)
    log.info("Compressed ZIP size: %.2f MB", zipped_size_mb)

    # Format Telegram caption
    top_tables = "\n".join([f"• `{t}`: {cnt:,}" for t, cnt in stats if cnt > 0][:10])
    caption = (
        f"🗄️ *NSEBOT Production Database Backup*\n"
        f"📅 *Timestamp:* {now_human}\n"
        f"📦 *Uncompressed:* {raw_size_mb:.2f} MB\n"
        f"🗜️ *Compressed:* {zipped_size_mb:.2f} MB\n"
        f"📊 *Tables:* {len(tables)}\n\n"
        f"*Key Table Record Counts:*\n{top_tables}"
    )

    # 3. Deliver via Telegram
    if zipped_size < CHUNK_THRESHOLD:
        log.info("Sending compressed backup to Telegram...")
        res = _tg_send_file(zip_path, caption, session)
        if res.get("ok"):
            log.info("Production database backup pushed to Telegram successfully.")
            tmp_dir.cleanup()
            return True
        log.error("Telegram backup API error: %s", res)
        tmp_dir.cleanup()
        return False

    # 4. Split into chunks if exceeds limit
    log.warning("Zipped file (%.1f MB) exceeds Telegram single file limit — splitting into chunks.", zipped_size_mb)
    part_num = 0
    with open(zip_path, "rb") as src:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            part_num += 1
            part_path = Path(tmp_dir.name) / f"{backup_db_path.stem}.zip.{part_num:03d}"
            part_path.write_bytes(chunk)
            part_caption = f"DB backup part {part_num} ({len(chunk) / (1024 * 1024):.1f} MB)"
            res = _tg_send_file(part_path, part_caption, session)
            if not res.get("ok"):
                log.error("Failed to send chunk %d: %s", part_num, res)
                _send_discord_notification(f"❌ Telegram backup failed — chunk {part_num} rejected: {res.get('description', 'unknown')}")
                tmp_dir.cleanup()
                return False
            log.info("Sent chunk %d", part_num)

    log.info("All %d chunks sent successfully.", part_num)
    tmp_dir.cleanup()
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    success = backup_db_to_telegram()
    print(f"\nDB Backup & Telegram Delivery: {'SUCCESS' if success else 'FAILED'}\n")
