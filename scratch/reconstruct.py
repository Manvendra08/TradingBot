with open("src/engine/paper_trading.py", "r", encoding="utf-8") as f:
    curr_lines = f.readlines()

with open("scratch/paper_trading_1b6beca3.py", "r", encoding="utf-8") as f:
    orig_lines = f.readlines()

# The current file is 456 lines long.
# Original file lines 457 to the end (index 456 onwards) need to be appended.
merged_lines = curr_lines + orig_lines[456:]

with open("src/engine/paper_trading.py", "w", encoding="utf-8") as f:
    f.writelines(merged_lines)

print(f"Reconstructed paper_trading.py to {len(merged_lines)} lines.")
