import os
import requests

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

groq_key = os.environ.get("GROQ_API_KEY")
if not groq_key:
    print("No GROQ_API_KEY found in environment or .env file")
else:
    headers = {"Authorization": f"Bearer {groq_key}"}
    r = requests.get("https://api.groq.com/openai/v1/models", headers=headers)
    if r.status_code == 200:
        data = r.json()
        models = [m.get("id") for m in data.get("data", [])]
        print("Active Groq models:")
        for m in sorted(models):
            print(f"  - {m}")
    else:
        print(f"Error fetching models: {r.status_code} - {r.text}")
