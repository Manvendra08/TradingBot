import subprocess

diff_output = subprocess.check_output(["git", "diff", "1b6beca3", "HEAD", "--", "src/engine/paper_trading.py"]).decode("utf-8", errors="ignore")
for line in diff_output.splitlines():
    clean_line = line.encode('ascii', 'backslashreplace').decode('ascii')
    if line.startswith("@@"):
        print(clean_line)
    elif line.startswith("+") and not line.startswith("+++"):
        print(f"ADDED: {clean_line}")
    elif line.startswith("-") and not line.startswith("---"):
        if len(line) < 150 and "is_long_trigger" not in line:
            print(f"REMOVED: {clean_line}")
