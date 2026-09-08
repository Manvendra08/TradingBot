"""
test_omniroute_video.py — Verify AI video generation with veo-free/seedance via OmniRoute.
"""
import os
import sys
import json
import base64
import time
import requests
from pathlib import Path

OMNIROUTE_URL = "http://localhost:20128/v1/videos/generations"
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "2026-09-07"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "test_seedance_clip.mp4"

def test_generate_video():
    prompt = (
        "Cinematic professional shot of an Indian male finance news anchor wearing glasses "
        "and a tailored navy suit seated at a modern studio glass desk, "
        "futuristic trading room background with soft blue glowing charts, "
        "subtle confident nodding, broadcast television 4k quality"
    )

    payload = {
        "model": "veo-free/seedance",
        "prompt": prompt,
        "aspect_ratio": "16:9",
    }

    headers = {
        "Content-Type": "application/json",
    }

    print(f"[*] Dispatching video generation to {OMNIROUTE_URL}...")
    print(f"[*] Model: {payload['model']}")
    print(f"[*] Prompt: {prompt[:80]}...")
    print("[*] Waiting for video generation (polling veoaifree upstream, ~60-180s)...")

    start_time = time.time()
    try:
        resp = requests.post(
            OMNIROUTE_URL,
            json=payload,
            headers=headers,
            timeout=360.0  # 6 minutes timeout
        )
        elapsed = time.time() - start_time
        print(f"[*] Response received in {elapsed:.1f}s, HTTP status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"[!] Request failed with HTTP {resp.status_code}:")
            try:
                print(json.dumps(resp.json(), indent=2))
            except Exception:
                print(resp.text[:500])
            return False

        data = resp.json()
        items = data.get("data", [])
        if not items:
            print(f"[!] No data items in response: {data}")
            return False

        first_item = items[0]
        b64_content = first_item.get("b64_json")
        video_url = first_item.get("url")

        if b64_content:
            video_bytes = base64.b64decode(b64_content)
            OUTPUT_FILE.write_bytes(video_bytes)
            print(f"[+] Successfully saved base64 video to: {OUTPUT_FILE}")
            print(f"[+] File size: {len(video_bytes):,} bytes ({len(video_bytes)/1024/1024:.2f} MB)")
            return True
        elif video_url:
            print(f"[*] Video URL returned: {video_url}")
            print("[*] Downloading video...")
            video_resp = requests.get(video_url, timeout=60.0)
            if video_resp.status_code == 200:
                OUTPUT_FILE.write_bytes(video_resp.content)
                print(f"[+] Downloaded video to: {OUTPUT_FILE}")
                print(f"[+] File size: {len(video_resp.content):,} bytes")
                return True
            else:
                print(f"[!] Failed to download video from {video_url}: HTTP {video_resp.status_code}")
                return False
        else:
            print(f"[!] Neither b64_json nor url found in item: {first_item}")
            return False

    except requests.exceptions.Timeout:
        print(f"[!] Request timed out after {time.time() - start_time:.1f}s")
        return False
    except Exception as exc:
        print(f"[!] Unexpected error: {exc}")
        return False

if __name__ == "__main__":
    success = test_generate_video()
    sys.exit(0 if success else 1)
