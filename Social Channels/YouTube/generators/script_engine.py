"""
Social Channels/YouTube/generators/script_engine.py
Synthesizes high-retention video script using OmniRouter with fallback to Gemini 2.5 Flash.
"""
from __future__ import annotations

import json
import logging
import os
import requests
from typing import Any
from generators.script_schema import YouTubeScriptModel, SceneItem

log = logging.getLogger(__name__)

SEBI_MANDATORY_DISCLAIMER = (
    "Disclaimer: This video is for educational purposes only and does not constitute "
    "financial, investment, or trading advice. We are not SEBI registered advisors. "
    "Options trading involves substantial risk of loss."
)


class ScriptEngine:
    def __init__(self):
        # 1. Check os.environ first, fallback to root config.settings (.env loaded by NSEBOT)
        try:
            import sys
            from pathlib import Path
            root_dir = Path(__file__).resolve().parents[3]
            if str(root_dir) not in sys.path:
                sys.path.append(str(root_dir))
            from config import settings as root_settings
            root_omni_base = getattr(root_settings, "OMNIROUTER_BASE_URL", "")
            root_omni_key = getattr(root_settings, "OMNIROUTER_API_KEY", "")
            root_gemini_key = getattr(root_settings, "GEMINI_API_KEY", "")
        except Exception:
            root_omni_base = ""
            root_omni_key = ""
            root_gemini_key = ""

        self.omnirouter_base = (
            os.environ.get("OMNIROUTER_BASE_URL") or root_omni_base or "http://localhost:20128/v1"
        ).strip().rstrip("/").replace(":3000", ":20128")
        
        self.omnirouter_key = os.environ.get("OMNIROUTER_API_KEY") or root_omni_key
        self.gemini_key = os.environ.get("GEMINI_API_KEY") or root_gemini_key

    def _build_prompt(self, metrics: Any) -> str:
        ce_res = metrics.top_ce_oi_strikes[0] if getattr(metrics, "top_ce_oi_strikes", None) else 23700.0
        pe_sup = metrics.top_pe_oi_strikes[0] if getattr(metrics, "top_pe_oi_strikes", None) else 23600.0
        return f"""
You are an expert Indian stock market derivatives trader and lead YouTube financial director.
Review today's finalized market closing figures:
• NIFTY 50: {metrics.nifty_close:.2f} ({metrics.nifty_change:+.2f}, {metrics.nifty_pchange:+.2f}%)
• BANK NIFTY: {metrics.banknifty_close:.2f} ({metrics.banknifty_change:+.2f}, {metrics.banknifty_pchange:+.2f}%)
• INDIA VIX: {metrics.vix:.2f}
• FII Net Cash: ₹{metrics.fii_net_cash:+.2f} Cr | DII Net Cash: ₹{metrics.dii_net_cash:+.2f} Cr
• Put-Call Ratio (PCR): {metrics.nifty_pcr:.2f} | Max Pain: {metrics.nifty_max_pain:.0f}
• Resistance Strikes (Heavy CE Writing): {metrics.top_ce_oi_strikes}
• Support Strikes (Heavy PE Writing): {metrics.top_pe_oi_strikes}

Write a tight, high-retention 1.5 to 2-minute YouTube script (target 200-240 spoken words total across all scenes).
CRITICAL RULES:
1. Provide REAL, ACTIONABLE DERIVATIVE INSIGHTS (gamma exposure, pin-risk at max pain, institutional absorption, smart money divergence). No generic filler, no robotic clichés.
2. DO NOT REPEAT metrics or phrases across scenes. Each scene delivers distinct, fresh quantitative insight.
3. Keep spoken sentences natural, punchy, and conversational for an elite Indian derivatives trader audience.
4. Strictly use today's actual numbers: Nifty {metrics.nifty_close:,.2f}, Max Pain {metrics.nifty_max_pain:,.0f}, FII ₹{metrics.fii_net_cash:+.1f} Cr, DII ₹{metrics.dii_net_cash:+.1f} Cr, PCR {metrics.nifty_pcr:.2f}, Call Wall {ce_res:,.0f}, Put Floor {pe_sup:,.0f}.

Divide into exactly 4 scenes:
Scene 1: THE HOOK & CONSOLIDATION (0:00 - 0:25) - Context of today's price action at {metrics.nifty_close:,.2f} ({metrics.nifty_change:+.2f} pts), and why Max Pain at {metrics.nifty_max_pain:,.0f} acted as the exact gravitational pin.
Scene 2: INSTITUTIONAL FLOWS (0:25 - 0:50) - Real cash vs derivatives divergence. FII Net Cash ({metrics.fii_net_cash:+.1f} Cr) vs DII absorption (+{metrics.dii_net_cash:+.1f} Cr). Why domestic liquidity cushioned the market.
Scene 3: DERIVATIVE BATTLEGROUND (0:50 - 1:20) - PCR {metrics.nifty_pcr:.2f} analysis, the {ce_res:,.0f} call wall defense, and put writing floor at {pe_sup:,.0f}.
Scene 4: TOMORROW'S ROADMAP (1:20 - 1:50) - Precise trade levels to defend ({pe_sup:,.0f} support), downside risk below this floor, and {ce_res:,.0f} upside recovery hurdle.

Output MUST be strictly valid JSON adhering to this exact schema:
{{
  "video_title": "High CTR Title under 90 chars (e.g. NIFTY Pins 23650 Max Pain! DIIs Absorb Selling | Next Trade Levels)",
  "thumbnail_hook": "3-word bold punchline (e.g. 23,650 PIN!)",
  "description_summary": "Comprehensive SEO description covering today's analysis and key levels.",
  "chapters": ["00:00 The Trap & Pin", "00:25 FII/DII Footprint", "00:50 Options OI & PCR", "01:20 Key Levels for Tomorrow"],
  "scenes": [
    {{
      "scene_id": 1,
      "segment_title": "THE TRAP & PIN",
      "headline_text": "NIFTY PINS 23,650 MAX PAIN",
      "metric_highlight": "Close: {metrics.nifty_close:,.2f} | Pain: {metrics.nifty_max_pain:,.0f}",
      "spoken_text": "Spoken sentence without filler..."
    }},
    {{
      "scene_id": 2,
      "segment_title": "INSTITUTIONAL FLOWS",
      "headline_text": "DIIS BUY ₹{int(metrics.dii_net_cash)} CR CASH",
      "metric_highlight": "DII: +₹{int(metrics.dii_net_cash)} Cr | FII: ₹{int(metrics.fii_net_cash)} Cr",
      "spoken_text": "Spoken sentence..."
    }},
    {{
      "scene_id": 3,
      "segment_title": "DERIVATIVES BATTLEGROUND",
      "headline_text": "{int(ce_res)} CALL WALL DEFENSE",
      "metric_highlight": "PCR: {metrics.nifty_pcr:.2f} | CE Wall: {int(ce_res)}",
      "spoken_text": "Spoken sentence..."
    }},
    {{
      "scene_id": 4,
      "segment_title": "TOMORROW'S WATCH OUT",
      "headline_text": "DEFEND {int(pe_sup)} SUPPORT",
      "metric_highlight": "Resistance: {int(ce_res)} | Support: {int(pe_sup)}",
      "spoken_text": "Spoken sentence..."
    }}
  ],
  "spoken_sebi_disclaimer": "This analysis is for educational purposes only and not investment advice."
}}
"""

    def _call_omnirouter(self, prompt: str) -> str | None:
        if not self.omnirouter_key:
            return None
        url = f"{self.omnirouter_base}/chat/completions"
        payload = {
            "model": "Claude-Models",
            "messages": [
                {"role": "system", "content": "You are a professional financial YouTube script director. Output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            log.info(f"Calling OmniRouter endpoint: {url}")
            res = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.omnirouter_key}", "Content-Type": "application/json"},
                timeout=5,
            )
            if res.status_code == 200:
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                log.info("OmniRouter returned script successfully.")
                return content
            log.warning(f"OmniRouter HTTP {res.status_code}: {res.text[:200]}")
        except Exception as exc:
            log.warning(f"OmniRouter request error: {exc}. Falling back to Gemini.")
        return None

    def _call_gemini(self, prompt: str) -> str:
        if not self.gemini_key:
            raise ValueError("Neither OMNIROUTER_API_KEY nor GEMINI_API_KEY is configured.")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.gemini_key)
        for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
            try:
                log.info(f"Calling Gemini ({model_name}) as LLM provider...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                )
                if response.text and response.text.strip():
                    return response.text
            except Exception as e:
                log.warning(f"Gemini model {model_name} failed: {e}. Trying fallback...")
        return "{}"

    def generate_script(self, metrics: Any) -> YouTubeScriptModel:
        prompt = self._build_prompt(metrics)
        raw_json = self._call_omnirouter(prompt)
        if not raw_json:
            raw_json = self._call_gemini(prompt)

        try:
            data = json.loads(raw_json)
            data["spoken_sebi_disclaimer"] = SEBI_MANDATORY_DISCLAIMER
            return YouTubeScriptModel.model_validate(data)
        except Exception as exc:
            log.error(f"Pydantic validation failed: {exc}. Retrying once with Gemini...")
            raw_json = self._call_gemini(prompt)
            data = json.loads(raw_json)
            data["spoken_sebi_disclaimer"] = SEBI_MANDATORY_DISCLAIMER
            return YouTubeScriptModel.model_validate(data)
