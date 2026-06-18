import difflib

with open("scratch/paper_trading_1b6beca3.py", "r", encoding="utf-8") as f:
    orig_lines = f.readlines()

with open("src/engine/paper_trading.py", "r", encoding="utf-8") as f:
    curr_lines = f.readlines()

# Let's diff only the first 456 lines
d = difflib.unified_diff(orig_lines[:456], curr_lines, fromfile='original', tofile='current')
for line in d:
    clean_line = line.encode('ascii', 'backslashreplace').decode('ascii')
    print(clean_line, end='')
