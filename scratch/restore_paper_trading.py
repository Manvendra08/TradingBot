import subprocess

# 1. Get version from 1b6beca3
orig_content = subprocess.check_output(["git", "show", "1b6beca3:src/engine/paper_trading.py"]).decode("utf-8")

# 2. Get current version
with open("src/engine/paper_trading.py", "r", encoding="utf-8") as f:
    curr_content = f.read()

print(f"Original content length: {len(orig_content.splitlines())}")
print(f"Current content length: {len(curr_content.splitlines())}")

# Let's save the original to a temp file first
with open("scratch/paper_trading_1b6beca3.py", "w", encoding="utf-8") as f:
    f.write(orig_content)
