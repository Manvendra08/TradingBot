"""
Text Sanitizer & Mojibake Repair Utility.

Fixes double-encoded UTF-8, mis-decoded Windows-1252/ISO-8859-1 strings,
and junk characters in Telegram alerts, LLM prompt responses, and logs.
(e.g., 'Î”' -> 'Δ', 'â†’' -> '→', 'â‚¹' -> '₹', 'â€”' -> '—').
"""
from __future__ import annotations

import re


# Explicit mapping for known double-encoded UTF-8 / Mojibake sequences
_MOJIBAKE_MAP: dict[str, str] = {
    # Greek / Math
    "Î”": "Δ",
    "Ã—": "×",
    "â‰≥": "≥",
    "â‰≤": "≤",
    "â‰": "≤",
    # Arrows
    "â†’": "→",
    "â†‘": "↑",
    "â†“": "↓",
    "âžA": "➔",
    # Punctuation / Formatting
    "â€”": "—",
    "â€“": "–",
    "â€¢": "•",
    "â€™": "'",
    "â€œ": '"',
    "â€": '"',
    # Currency / Emojis
    "â‚¹": "₹",
    "âœ…": "✅",
    "âœ✔": "✔",
    "âšA": "⚠️",
    "Ã©": "é",
}


def sanitize_mojibake(text: str) -> str:
    """Repair double-encoded UTF-8 or Mojibake characters in text."""
    if not text or not isinstance(text, str):
        return text if text is not None else ""

    # 1. Apply explicit replacements first for common corrupt patterns
    for bad, good in _MOJIBAKE_MAP.items():
        if bad in text:
            text = text.replace(bad, good)

    # 2. Try automated byte re-decoding for residual double-encoded UTF-8
    if any(k in text for k in ("Ã", "â", "Î")):
        try:
            # Re-encode as latin1 bytes, then decode correctly as UTF-8
            reencoded = text.encode("latin-1").decode("utf-8")
            text = reencoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            try:
                reencoded = text.encode("cp1252").decode("utf-8")
                text = reencoded
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    return text
