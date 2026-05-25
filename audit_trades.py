import sqlite3
from pathlib import Path
import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")
conn = sqlite3.connect("data/nsebot.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, opened_at, closed_at, symbol, option_type, entry_underlying, exit_underlying, "
    "entry_premium, exit_premium, sl_premium, target_premium, status, pnl_rupees, verdict_label, reason "
    "FROM paper_trades ORDER BY opened_at DESC"
).fetchall()

MARKET_WINDOWS = {
    "NIFTY":       ("09:15", "15:30"),
    "BANKNIFTY":   ("09:15", "15:30"),
    "NATURALGAS":  ("09:00", "23:30"),
    "CRUDEOIL":    ("09:00", "23:30"),
}

issues_total = []

print("=== TRADE LEGITIMACY AUDIT ===\n")
for r in rows:
    r = dict(r)
    tid = r["id"]
    sym = r["symbol"]
    sym_upper = sym.upper()
    window = MARKET_WINDOWS.get(sym_upper, ("09:15", "15:30"))
    ot_type = r.get("option_type", "")
    status = r.get("status", "")
    pnl = float(r.get("pnl_rupees") or 0)
    ep = r.get("entry_premium")
    xp = r.get("exit_premium")
    sl = r.get("sl_premium")
    tgt = r.get("target_premium")

    opened_utc = r["opened_at"]
    closed_utc = r["closed_at"]

    try:
        opened_ist = datetime.fromisoformat(opened_utc.replace("Z", "+00:00")).astimezone(IST)
        opened_str = opened_ist.strftime("%d %b %H:%M IST")
    except Exception:
        opened_ist = None
        opened_str = opened_utc

    closed_ist = None
    closed_str = "OPEN"
    if closed_utc:
        try:
            closed_ist = datetime.fromisoformat(closed_utc.replace("Z", "+00:00")).astimezone(IST)
            closed_str = closed_ist.strftime("%d %b %H:%M IST")
        except Exception:
            closed_str = closed_utc

    trade_issues = []

    # --- Market hours check: opened_at ---
    if opened_ist:
        t = opened_ist.strftime("%H:%M")
        day = opened_ist.weekday()
        if day >= 5:
            trade_issues.append(f"OPENED on weekend ({opened_ist.strftime('%A')})")
        elif not (window[0] <= t <= window[1]):
            trade_issues.append(f"OPENED outside market hours ({t} IST, window {window[0]}-{window[1]})")

    # --- Market hours check: closed_at ---
    if closed_ist:
        t = closed_ist.strftime("%H:%M")
        day = closed_ist.weekday()
        if day >= 5:
            trade_issues.append(f"CLOSED on weekend ({closed_ist.strftime('%A')})")
        elif not (window[0] <= t <= window[1]):
            trade_issues.append(f"CLOSED outside market hours ({t} IST, window {window[0]}-{window[1]})")

    # --- P&L vs status consistency ---
    if status == "CLOSED_TARGET" and pnl < -500:
        trade_issues.append(f"TARGET HIT but P&L is negative (Rs {pnl:,.0f})")
    if status == "CLOSED_SL" and pnl > 500:
        trade_issues.append(f"SL HIT but P&L is positive (Rs {pnl:,.0f})")

    # --- Premium exit logic check ---
    if status == "CLOSED_TARGET" and ep and xp and tgt:
        if ot_type == "PE":
            # Bearish PE short: target = premium falls to tgt (exit <= tgt)
            if xp > tgt + 10:
                trade_issues.append(
                    f"PE TARGET logic: exit_prem {xp:.2f} should be <= target_prem {tgt:.2f}"
                )
        elif ot_type == "CE":
            # Bullish CE long: target = premium rises to tgt (exit >= tgt)
            if xp < tgt - 10:
                trade_issues.append(
                    f"CE TARGET logic: exit_prem {xp:.2f} should be >= target_prem {tgt:.2f}"
                )

    # --- Underlying frozen (stale data) ---
    eu = r.get("entry_underlying")
    xu = r.get("exit_underlying")
    if eu and xu and eu == xu and status != "OPEN":
        trade_issues.append(f"Underlying FROZEN: entry={eu} == exit={xu} (stale price data?)")

    flag = "FAIL" if trade_issues else "OK  "
    print(f"[{flag}] ID={tid:2d} | {sym_upper:12s} {ot_type} | {opened_str} -> {closed_str} | {status:16s} | PnL Rs {pnl:>10,.0f}")
    for issue in trade_issues:
        print(f"         !! {issue}")
        issues_total.append({"id": tid, "issue": issue})

print(f"\n=== SUMMARY: {len(issues_total)} issues across {len(rows)} trades ===")
for i in issues_total:
    print(f"  Trade #{i['id']}: {i['issue']}")

conn.close()
