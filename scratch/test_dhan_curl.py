import subprocess
import json

print("Testing request via curl...")
try:
    # Use curl to request Dhan API
    res = subprocess.run(
        ["curl", "-s", "-X", "GET", "https://api.dhan.co/v2"],
        capture_output=True,
        text=True,
        timeout=10
    )
    print(f"Exit code: {res.returncode}")
    print(f"Stdout: {res.stdout[:500]}")
    print(f"Stderr: {res.stderr[:500]}")
except Exception as e:
    print(f"Failed to run curl: {e}")
