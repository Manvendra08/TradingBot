"""
LLM Card Engine for NSEBOT YouTube Pipeline
Generates cinematic 3D 16:9 broadcast infographic cards using
LLM image generation (antigravity/gemini-3.1-flash-image via OmniRouter).
"""

import os
import time
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

log = logging.getLogger("nsebot.youtube.llm_card_engine")

# Load environment from project root .env
dotenv_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(dotenv_path=dotenv_path)

BASE_URL = os.environ.get("OMNIROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
API_KEY = os.environ.get("OMNIROUTER_API_KEY", "")
MODEL_NAME = "antigravity/gemini-3.1-flash-image"
DEFAULT_SIZE = "1792x1024"  # 16:9 aspect ratio


class LLMCardEngine:
    def __init__(self, base_url: str = BASE_URL, api_key: str = API_KEY, model: str = MODEL_NAME):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def generate_image_with_retry(self, prompt: str, output_path: Path, max_retries: int = 2) -> Path:
        if output_path.exists() and output_path.stat().st_size > 100_000:
            log.info(f"[LLMCardEngine] Reusing existing image: {output_path.name} ({output_path.stat().st_size:,} bytes)")
            return output_path

        url = f"{self.base_url}/images/generations"
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": DEFAULT_SIZE
        }

        for attempt in range(1, max_retries + 1):
            try:
                log.info(f"[LLMCardEngine] Generating image (attempt {attempt}/{max_retries}): {output_path.name}")
                res = requests.post(url, headers=self.headers, json=body, timeout=60)
                if res.status_code == 200:
                    data = res.json()["data"][0]
                    b64 = data.get("b64_json")
                    if b64:
                        raw = base64.b64decode(b64)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(output_path, "wb") as f:
                            f.write(raw)
                        log.info(f"[LLMCardEngine] Saved {output_path.name} ({len(raw):,} bytes)")
                        return output_path
                log.warning(f"[LLMCardEngine] API returned {res.status_code}: {res.text[:200]}")
                if res.status_code == 429:
                    break  # Quota exhausted, fail immediately to fallback
            except Exception as exc:
                log.warning(f"[LLMCardEngine] Error on attempt {attempt}: {exc}")
            time.sleep(1.5 * attempt)

        raise RuntimeError(f"Failed to generate LLM card {output_path.name} via {self.model}")

    def render_intro_card(self, output_path: Path, trade_date: str, metrics: dict, fallback_fn=None) -> Path:
        if output_path.exists() and output_path.stat().st_size > 100_000:
            return output_path
        nifty_close = metrics.get("nifty_close", 23635.10)
        nifty_change = metrics.get("nifty_change", -144.05)
        nifty_pchange = metrics.get("nifty_pchange", -0.61)
        bn_close = metrics.get("banknifty_close", 56792.45)
        pain = metrics.get("nifty_max_pain", 23650.0)

        prompt = (
            f"Cinematic 3D financial broadcast intro card, 16:9 widescreen, 8k resolution. "
            f"Deep navy blue high-tech trading floor background with volumetric cyan lighting and glowing cybernetic grid. "
            f"Large glowing 3D metallic gold and chrome typography: 'NIFTY 50 MARKET WRAP'. "
            f"Prominent glowing crimson red pill badge: 'CLOSE: {nifty_close:,.2f} ({nifty_change:+.2f} / {nifty_pchange:+.2f}%)'. "
            f"Three glassmorphic pedestals displaying: NIFTY 50 {nifty_close:,.2f}, BANK NIFTY {bn_close:,.2f}, and MAX PAIN {pain:,.0f} PIVOT in bright glowing amber. "
            f"Top holographic badge: 'TRADE DATE: {trade_date}'. "
            f"Professional CNBC / Bloomberg terminal broadcast television quality, photorealistic 3D render."
        )
        try:
            return self.generate_image_with_retry(prompt, output_path)
        except Exception as e:
            if fallback_fn:
                log.info(f"[LLMCardEngine] Falling back to alternative renderer for intro: {e}")
                return fallback_fn()
            raise

    def render_beat_card(self, output_path: Path, segment: str, headline: str, metric_label: str, metric_val: str, visual_concept: str, fallback_fn=None) -> Path:
        if output_path.exists() and output_path.stat().st_size > 100_000:
            return output_path
        prompt = (
            f"Cinematic 3D financial infographic card, 16:9 widescreen, 8k resolution. "
            f"Top segment badge: '[ {segment.upper()} ]'. "
            f"Giant glowing 3D metallic gold headline: '{headline.upper()}'. "
            f"Central holographic metric pill: '{metric_label}: {metric_val}'. "
            f"Center visual stage: {visual_concept}. "
            f"Dark institutional trading room background, volumetric lighting, photorealistic, ultra-high contrast, CNBC / Bloomberg broadcast TV standard."
        )
        try:
            return self.generate_image_with_retry(prompt, output_path)
        except Exception as e:
            if fallback_fn:
                log.info(f"[LLMCardEngine] Falling back to HTML/Matplotlib renderer for {output_path.name}: {e}")
                return fallback_fn()
            raise

    def render_disclaimer_card(self, output_path: Path, fallback_fn=None) -> Path:
        if output_path.exists() and output_path.stat().st_size > 100_000:
            return output_path
        prompt = (
            "Cinematic 3D financial compliance card, 16:9 widescreen, 8k resolution. "
            "Deep navy blue sleek background with glowing amber border and subtle security shield watermark. "
            "Top golden warning badge: 'IMPORTANT REGULATORY NOTICE'. "
            "Bold white metallic headline: 'SEBI DISCLAIMER & RISK DISCLOSURE'. "
            "Large ultra-clean high-contrast typography: "
            "'This analysis is strictly for educational and quantitative research purposes only. "
            "We are NOT SEBI registered advisors. "
            "Options trading involves substantial risk of loss. 9 out of 10 individual traders incur net financial losses in F&O.' "
            "Sleek broadcast TV standard, professional institutional finish."
        )
        try:
            return self.generate_image_with_retry(prompt, output_path)
        except Exception as e:
            if fallback_fn:
                log.info(f"[LLMCardEngine] Falling back to HTML disclaimer for {output_path.name}")
                return fallback_fn()
            raise

    def generate_thumbnail(self, output_path: Path, hook_text: str, nifty_change: float, key_support: str, fallback_fn=None) -> Path:
        if output_path.exists() and output_path.stat().st_size > 100_000:
            return output_path
        delta_str = f"{nifty_change:+.1f} PTS" if nifty_change != 0 else "-144.1 PTS"
        prompt = (
            f"Cinematic 3D YouTube thumbnail, 16:9 widescreen, 8k resolution. "
            f"High-impact bold glowing 3D metallic typography: '{hook_text}'. "
            f"Large bright red glowing pill badge: '{delta_str} PLUNGE!'. "
            f"Amber glowing laser callout: 'KEY PIVOT: {key_support}'. "
            f"Background: Dark futuristic derivatives command center with high-tech red neon candlesticks and holographic order books. "
            f"Extremely punchy, high-CTR YouTube thumbnail aesthetic, photorealistic 3D render."
        )
        try:
            return self.generate_image_with_retry(prompt, output_path)
        except Exception as e:
            if fallback_fn:
                log.info(f"[LLMCardEngine] Falling back to VisualCardEngine for thumbnail: {e}")
                return fallback_fn()
            raise

