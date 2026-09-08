"""
Social Channels/YouTube/publishers/telegram_alert.py
Dispatches review alert with direct YouTube Studio link using NSEBOT's telegram_dispatcher.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add project root for telegram_dispatcher
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger(__name__)


def send_youtube_review_alert(video_id: str, title: str) -> None:
    """Sends 1-tap review notification to Telegram with YouTube Studio link."""
    studio_url = f"https://studio.youtube.com/video/{video_id}/edit"
    preview_url = f"https://youtu.be/{video_id}"
    msg = (
        f"🎬 *DAILY YOUTUBE MARKET WRAP READY*\n\n"
        f"📌 *Title:* {title}\n"
        f"🔒 *Status:* Private Draft\n\n"
        f"👉 [Review in YouTube Studio]({studio_url})\n"
        f"👁️ [Watch Preview]({preview_url})\n\n"
        f"_Tap link to verify and flip to Public._"
    )
    try:
        from src.alerts.telegram_dispatcher import send_telegram_alert_sync
        send_telegram_alert_sync(msg)
        log.info("Telegram review alert dispatched successfully.")
    except Exception as exc:
        log.warning(f"Failed to dispatch Telegram review alert: {exc}")
