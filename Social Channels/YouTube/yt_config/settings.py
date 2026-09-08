"""
Social Channels/YouTube/config/settings.py
Configuration settings for the YouTube content pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parents[1]
OUTPUT_DIR = BASE_DIR / "output"
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
MUSIC_DIR = ASSETS_DIR / "music"
TEMPLATES_DIR = ASSETS_DIR / "templates"

NSEBOT_DB_PATH = PROJECT_ROOT / "data" / "trading_bot.db"
YOUTUBE_TOKEN_PATH = BASE_DIR / "config" / "youtube_token.json"
CLIENT_SECRETS_PATH = BASE_DIR / "config" / "client_secrets.json"

# LLM Providers
OMNIROUTER_BASE_URL = os.environ.get("OMNIROUTER_BASE_URL", "http://localhost:20128/v1").strip().rstrip("/")
OMNIROUTER_API_KEY = os.environ.get("OMNIROUTER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# TTS Settings
DEFAULT_TTS_VOICE = os.environ.get("YOUTUBE_TTS_VOICE", "en-IN-PrabhatNeural")
DEFAULT_TTS_RATE = os.environ.get("YOUTUBE_TTS_RATE", "+25%")  # 1.25x speed
FALLBACK_TTS_RATE = int(os.environ.get("YOUTUBE_FALLBACK_TTS_RATE", "206"))  # 1.25x of 165

# Video Settings
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 24
AUDIO_DUCKING_VOLUME = float(os.environ.get("YOUTUBE_MUSIC_VOLUME", "0.07"))  # ~ -23dB

# YouTube Upload
DEFAULT_PRIVACY_STATUS = os.environ.get("YOUTUBE_PRIVACY_STATUS", "private")
DEFAULT_CATEGORY_ID = "27"  # Education
