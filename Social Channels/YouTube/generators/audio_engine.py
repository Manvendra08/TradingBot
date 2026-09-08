"""
Social Channels/YouTube/generators/audio_engine.py
Free TTS engine using Microsoft Edge Neural TTS with Windows SAPI fallback.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import edge_tts

log = logging.getLogger(__name__)


class AudioEngine:
    def __init__(self, voice: str = "en-IN-PrabhatNeural", rate: str = "+25%"):
        self.voice = voice
        self.rate = rate

    async def synthesize(
        self,
        text: str,
        audio_path: Path,
        srt_path: Path,
        script_text_path: Path | None = None,
    ) -> bool:
        """Primary: Microsoft Edge Neural TTS at 1.25x speed ($0, broadcast quality)."""
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save raw audio script to text file if requested
        if script_text_path:
            script_text_path.parent.mkdir(parents=True, exist_ok=True)
            script_text_path.write_text(text, encoding="utf-8")
            log.info(f"Saved audio script to text file: {script_text_path}")

        try:
            log.info(f"Synthesizing audio via edge-tts ({self.voice} at {self.rate} speed)...")
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            sub_maker = edge_tts.SubMaker()
            with open(audio_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                        try:
                            sub_maker.feed(chunk)
                        except Exception:
                            pass
            
            srt_content = sub_maker.get_srt().strip()
            if not srt_content:
                # Fallback non-empty SRT so libass filter never fails
                srt_content = "1\n00:00:00,100 --> 00:03:00,000\n" + text[:80] + "..."
            
            srt_path.write_text(srt_content + "\n", encoding="utf-8")
            log.info(f"Generated free audio ({audio_path}) and SRT ({srt_path}, {len(srt_content)} bytes).")
            return True
        except Exception as exc:
            log.warning(f"edge-tts failed ({exc}). Triggering local pyttsx3 fallback.")
            return self._fallback_sapi(text, audio_path, srt_path)

    def _fallback_sapi(self, text: str, audio_path: Path, srt_path: Path) -> bool:
        """Emergency Fallback: Windows SAPI via pyttsx3 at ~1.25x speed (206 wpm)."""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 206)
            engine.save_to_file(text, str(audio_path))
            engine.runAndWait()
            # Minimal fallback SRT
            srt_path.write_text("1\n00:00:00,000 --> 00:03:00,000\n" + text[:80] + "...", encoding="utf-8")
            log.info(f"Synthesized fallback audio via Windows SAPI: {audio_path}")
            return True
        except Exception as err:
            log.error(f"SAPI fallback failed as well: {err}")
            raise RuntimeError(f"All TTS synthesis options failed: {err}") from err
