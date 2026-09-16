import os
import sys
import json
import base64
import requests
from pathlib import Path
from dotenv import load_dotenv

dotenv_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=dotenv_path)

BASE_URL = os.environ.get("OMNIROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
API_KEY = os.environ.get("OMNIROUTER_API_KEY", "")
MODEL_NAME = "antigravity/gemini-3.1-flash-image"

def generate_ai_card(prompt: str, output_path: Path, size: str = "1792x1024") -> Path:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "size": size
    }
    url = f"{BASE_URL}/images/generations"
    print(f"Generating AI card via OmniRouter ({MODEL_NAME})...")
    res = requests.post(url, headers=headers, json=body, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"OmniRouter image generation failed ({res.status_code}): {res.text[:300]}")
    
    data = res.json()["data"][0]
    b64 = data.get("b64_json")
    if not b64:
        raise RuntimeError(f"No b64_json in response: {res.text[:200]}")
    
    raw = base64.b64decode(b64)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(raw)
    print(f"Successfully generated AI image: {output_path} ({len(raw):,} bytes)")
    return output_path

if __name__ == "__main__":
    test_out = Path("c:/Users/manve/VibeProjects/NSEBOT/test_ai_card_01.jpg")
    p = (
        "Cinematic 3D financial infographic, 16:9 widescreen, 8k resolution. "
        "Top segment badge: '[ THE PLUNGE ]'. "
        "Giant glowing 3D metallic gold headline: 'NIFTY SLIDES 144 POINTS'. "
        "Central holographic metric pill glowing vivid crimson red: 'CLOSE: 23,635.10 (-144.05 PTS / -0.61%)'. "
        "Center visual stage: High-tech 3D candlestick chart with sharp falling red Japanese candlesticks sliding from 23,780 down to 23,635.10, glowing red support breach lines, and laser grid. "
        "Dark institutional trading floor background, volumetric lighting, photorealistic."
    )
    generate_ai_card(p, test_out)
