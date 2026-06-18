import sys

try:
    with open("logs/main.log", "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        print(f"Total lines: {len(lines)}")
        for line in lines[-150:]:
            # Clean up line output for Windows console
            encoded = line.rstrip().encode('ascii', errors='replace').decode('ascii')
            print(encoded)
except Exception as e:
    print(f"Error: {e}")
