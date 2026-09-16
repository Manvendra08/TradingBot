import os
import requests
from dotenv import load_dotenv

from pathlib import Path
dotenv_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=dotenv_path)

def test_openai_image():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY: Not found")
        return False
    print("Testing OpenAI DALL-E 3 / DALL-E 2...")
    url = "https://api.openai.com/v1/images/generations"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": "dall-e-3",
        "prompt": "Minimal financial chart icon, red line down",
        "n": 1,
        "size": "1024x1024"
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=20)
        print("OpenAI Image status:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("OpenAI Image error:", e)
        return False

def test_gemini_imagen():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY: Not found")
        return False
    print("Testing Gemini Imagen 3 via Generative Language API...")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict?key={key}"
    body = {
        "instances": [{"prompt": "A minimalist financial chart icon"}],
        "parameters": {"sampleCount": 1, "aspectRatio": "16:9"}
    }
    try:
        r = requests.post(url, json=body, timeout=20)
        print("Gemini Imagen status:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("Gemini Imagen error:", e)
        return False

def test_huggingface_image():
    token = os.environ.get("HF_API_TOKEN") or os.environ.get("HF_API_KEY")
    if not token:
        print("HF_API_TOKEN: Not found")
        return False
    print("Testing Hugging Face FLUX / SDXL...")
    # HF Serverless Router
    models = [
        "black-forest-labs/FLUX.1-schnell",
        "stabilityai/stable-diffusion-xl-base-1.0"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    for m in models:
        url = f"https://api-inference.huggingface.co/models/{m}"
        try:
            r = requests.post(url, headers=headers, json={"inputs": "Minimalist red stock market chart"}, timeout=20)
            print(f"HF ({m}) status:", r.status_code, r.text[:200] if r.headers.get("content-type", "").startswith("application/json") else f"Binary {len(r.content)} bytes")
            if r.status_code == 200:
                return True
        except Exception as e:
            print(f"HF ({m}) error:", e)
    return False

if __name__ == "__main__":
    print("--- TESTING AVAILABLE IMAGE APIS ---")
    test_openai_image()
    test_gemini_imagen()
    test_huggingface_image()
