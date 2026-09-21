"""
TypeSafe AI – System-1 (Jev) client
====================================
Thin wrapper around POST https://api.typesafe.ai/v1/systemone

Question types supported:
  noul   – boolean probability  0.0–1.0  (true/false judgement)
  choice – pick one from options list
  score  – ordered rank

Usage::

    from src.services.typesafe_client import evaluate_system_one

    answers = evaluate_system_one(
        state_text="NIFTY spot 24800, PCR 0.85, BEARISH momentum …",
        questions={
            "tradeable_setup": {
                "type": "noul",
                "question": "Does the market state represent a high-probability tradeable setup?",
            },
            "direction": {
                "type": "choice",
                "question": "What is the dominant market direction right now?",
                "options": ["BULLISH", "BEARISH", "NEUTRAL"],
            },
        },
    )
    # answers → {"tradeable_setup": {"value": True, "probability": 0.82},
    #             "direction": {"value": "BEARISH", "probability": 0.91}}
    # or None on any failure
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

_BASE_URL = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_TIMEOUT = 3.0  # seconds – Jev is fast; 3 s is a generous ceiling


def evaluate_system_one(
    state_text: str,
    questions: dict[str, dict[str, Any]],
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Call TypeSafe AI System-1 (Jev) and return the answers dict.

    Args:
        state_text: Plain-text description of market state fed as the
                    ``state`` field in the request body.
        questions:  Mapping of answer-key → question spec.
                    Each spec must contain at minimum ``type`` and ``question``.
                    For ``choice`` questions also include ``options``.
        timeout:    HTTP read timeout in seconds (default 3 s).

    Returns:
        Dict mapping answer-key → ``{"value": ..., "probability": float}``,
        or ``None`` on auth failure, network error, or malformed response.
    """
    from config.settings import TYPESAFE_API_KEY  # late import — avoids circular at module load

    if not TYPESAFE_API_KEY:
        log.debug("jev: TYPESAFE_API_KEY not set — skipping System-1 call")
        return None

    payload: dict[str, Any] = {
        "state": state_text,
        "questions": questions,
    }
    headers = {
        "Authorization": f"Bearer {TYPESAFE_API_KEY}",
        "Content-Type": "application/json",
    }

    t0 = time.monotonic()
    try:
        resp = requests.post(_BASE_URL, json=payload, headers=headers, timeout=timeout)
        elapsed_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        answers: dict[str, Any] = data.get("answers") or {}
        if not answers:
            log.debug("jev: empty answers in response (%.0f ms)", elapsed_ms)
            return None
        log.debug("jev: System-1 returned %d answers in %.0f ms", len(answers), elapsed_ms)
        return answers
    except requests.exceptions.Timeout:
        log.debug("jev: System-1 timed out after %.1f s", timeout)
        return None
    except requests.exceptions.HTTPError as exc:
        log.debug("jev: HTTP %s — %s", exc.response.status_code if exc.response else "?", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("jev: unexpected error: %s", exc)
        return None
