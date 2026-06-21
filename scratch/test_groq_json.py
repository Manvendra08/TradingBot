import os
import requests
import json

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
    print("No GROQ_API_KEY found")
else:
    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    
    # We will test JSON mode with a simple prompt
    test_models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "qwen/qwen3-32b",
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-20b"
    ]
    
    body = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. You MUST respond with a valid JSON object: {\"status\": \"success\", \"message\": \"hello\"}"},
            {"role": "user", "content": "Hello"}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    
    for model in test_models:
        body["model"] = model
        try:
            print(f"Testing model: {model}...")
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=10)
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                print(f"  Success! Response: {content.strip()}")
            else:
                print(f"  Failed: Status {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            print(f"  Failed with exception: {e}")
