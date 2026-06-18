"""
Test to show the separator line length change
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Show the difference
print("SEPARATOR LINE LENGTH COMPARISON")
print("=" * 80)
print()

print("OLD FORMAT (50 characters):")
print("=" * 50)
print("📊 NIFTY — TRADING SIGNAL")
print("=" * 50)
print()

print("NEW FORMAT (25 characters - HALF LENGTH):")
print("=" * 25)
print("📊 NIFTY — TRADING SIGNAL")
print("=" * 25)
print()

print("=" * 80)
print("MOBILE VIEW COMPARISON")
print("=" * 80)
print()

print("OLD (50 chars) - Takes up full mobile width:")
print("-" * 50)
print("This line is 50 characters long")
print("-" * 50)
print()

print("NEW (25 chars) - Better for mobile:")
print("-" * 25)
print("This line is 25 characters long")
print("-" * 25)
print()

print("=" * 80)
print("FULL MESSAGE EXAMPLE (NEW FORMAT)")
print("=" * 80)
print()

message = """
=========================
📊 NIFTY — TRADING SIGNAL
=========================

🟢 BUY SIGNAL
Verdict: Long Buildup
Confidence: 85% 🔥

📝 WHAT'S HAPPENING:
  Buyers are accumulating positions. Price likely to go up.

✅ BOT DECISION:
  Status: ✅ GO AHEAD (High Quality)
  Type: 📈 Trend Trade (Following trend)

  Score Breakdown:
    Confidence: 🟢 ████████░░ 85%
    Entry Quality: 🟢 █████████░ 90/100
    Trend Alignment: 🟢 ███████░░░ 78%
    Market Regime: 🟢 ███████░░░ 72%
    Momentum: 🟢 ████████░░ 82%

⚠️ RISK CHECK:
  Open Trades: 1/4
  Daily Loss: ₹2,000/10,000

🎯 WHAT TO DO:
  ✅ Bot approved this trade
  → You can enter if you agree with the setup

  IF YOU TRADE BULLISH:
    • Buy Call (CE) at ATM or slightly OTM
    • Set Stop Loss below support
    • Target: Resistance level

📊 MARKET CONTEXT:
  🟢 Strong Bullish Trend

=========================
⏰ Check back in 5 minutes for next scan
=========================
"""

print(message)

print()
print("=" * 80)
print("BENEFITS OF SHORTER SEPARATOR LINES")
print("=" * 80)
print()
print("✅ Better mobile display (no horizontal scroll)")
print("✅ Cleaner appearance on small screens")
print("✅ Easier to read on Telegram mobile app")
print("✅ Still provides visual separation")
print("✅ Matches screenshot requirements")
print()
print("=" * 80)
print("✓ Separator line length reduced to 25 characters (half of 50)")
print("=" * 80)
