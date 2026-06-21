import os
from google import genai
from google.genai import types

# Load .env
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    k, v = parts
                    v = v.strip("\"'")
                    os.environ[k] = v

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("No GEMINI_API_KEY found")
else:
    client = genai.Client(api_key=api_key)
    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-pro", "gemini-2.0-pro-exp-02-05"]:
        try:
            print(f"Testing Gemini model: {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents="Hello, respond with one word.",
            )
            print(f"  Success! Response: {response.text.strip()}")
        except Exception as e:
            print(f"  Failed: {e}")
