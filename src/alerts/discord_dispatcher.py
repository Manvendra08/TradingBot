"""
Discord Alert Dispatcher
Uses incoming webhooks to send alert messages to a Discord channel.
"""
import json
import logging
import urllib.request
from config.settings import DISCORD_WEBHOOK_URL

log = logging.getLogger(__name__)


def send_to_discord(text: str, timeout_seconds: int = 10) -> bool:
    """Sends a raw markdown message to Discord via webhook.
    Returns True if sent successfully, False otherwise.
    """
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "your_discord_webhook_url":
        return False

    try:
        # Discord Incoming Webhook expects JSON: {"content": "message text"}
        # We also limit length to 2000 chars (Discord API limit).
        payload = json.dumps({"content": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "NSEBOT/1.0"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            resp.read()
        return True
    except Exception as e:
        log.error("Discord send failed: %s", e)
        return False
