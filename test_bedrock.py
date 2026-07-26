import os, sys, json
from dotenv import load_dotenv
load_dotenv()
import boto3

client = boto3.client('bedrock-runtime', region_name='us-east-1',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    config=boto3.session.Config(connect_timeout=10, read_timeout=15))

# Test Converse vs InvokeModel for a known ACTIVE model
mid = "meta.llama3-3-70b-instruct-v1:0"

print("=== Converse API ===")
try:
    resp = client.converse(
        modelId=mid,
        messages=[{"role":"user","content":[{"text":"Say OK"}]}],
        inferenceConfig={"temperature":0.1,"maxTokens":10})
    print("OK:", resp["output"]["message"]["content"][0]["text"].strip()[:60])
except Exception as e:
    print(f"FAIL: {str(e)[:200]}")

print("\n=== InvokeModel API (Llama format) ===")
try:
    resp = client.invoke_model(
        modelId=mid,
        body=json.dumps({"prompt":"Say OK","max_gen_len":5,"temperature":0.1}),
        contentType="application/json",
        accept="application/json")
    body = json.loads(resp["body"].read())
    print("OK:", body)
except Exception as e:
    print(f"FAIL: {str(e)[:200]}")

print("\n=== InvokeModel API (Nova format) ===")
try:
    resp = client.invoke_model(
        modelId="amazon.nova-pro-v1:0",
        body=json.dumps({"messages":[{"role":"user","content":[{"text":"Say OK"}]}],"inferenceConfig":{"maxTokens":10,"temperature":0.1}}),
        contentType="application/json",
        accept="application/json")
    body = json.loads(resp["body"].read())
    print("OK:", body)
except Exception as e:
    print(f"FAIL: {str(e)[:200]}")

print("\n=== InvokeModel API (Claude format) ===")
try:
    resp = client.invoke_model(
        modelId="anthropic.claude-sonnet-4-20250514-v1:0",
        body=json.dumps({"anthropic_version":"bedrock-2023-05-31","max_tokens":10,"messages":[{"role":"user","content":"Say OK"}]}),
        contentType="application/json",
        accept="application/json")
    body = json.loads(resp["body"].read())
    print("OK:", body)
except Exception as e:
    print(f"FAIL: {str(e)[:200]}")

print("\n=== InvokeModel API (GLM 5 format) ===")
try:
    resp = client.invoke_model(
        modelId="zai.glm-5",
        body=json.dumps({"messages":[{"role":"user","content":"Say OK"}]}),
        contentType="application/json",
        accept="application/json")
    body = json.loads(resp["body"].read())
    print("OK:", body)
except Exception as e:
    print(f"FAIL: {str(e)[:200]}")
