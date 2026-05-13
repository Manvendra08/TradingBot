"""
One-time utility: download Dhan security master CSV and print security_ids
for NIFTY, BANKNIFTY, FINNIFTY and any F&O stock you specify.

Usage:
    python tools/fetch_dhan_master.py
    python tools/fetch_dhan_master.py --symbols RELIANCE TCS HDFCBANK
"""
import argparse
import csv
import io
import requests

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

KNOWN = {
    "NIFTY":      {"security_id": 13,  "segment": "IDX_I"},
    "BANKNIFTY":  {"security_id": 25,  "segment": "IDX_I"},
    "FINNIFTY":   {"security_id": 27,  "segment": "IDX_I"},
    "MIDCPNIFTY": {"security_id": 442, "segment": "IDX_I"},
}


def fetch_master() -> list[dict]:
    print(f"Downloading Dhan scrip master ...")
    r = requests.get(MASTER_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def search(rows: list[dict], symbols: list[str]) -> None:
    sym_upper = {s.upper() for s in symbols}
    found = {}
    for row in rows:
        name   = (row.get("SEM_TRADING_SYMBOL") or "").upper()
        seg    = row.get("SEM_EXM_EXCH_ID", "")
        instru = row.get("SEM_INSTRUMENT_NAME", "")
        sec_id = row.get("SEM_SMST_SECURITY_ID", "")
        if name in sym_upper and "NSE" in seg and instru in ("FUTSTK", "OPTSTK", "FUTIDX", "OPTIDX"):
            found[name] = {"security_id": sec_id, "segment": seg, "instrument": instru}

    print("\n# Paste into config/settings.py → DHAN_SECURITY_IDS\nDHAN_SECURITY_IDS = {")
    for k, v in KNOWN.items():
        print(f'    "{k}": {v["security_id"]},   # index')
    for sym in symbols:
        sym = sym.upper()
        if sym in found:
            print(f'    "{sym}": {found[sym]["security_id"]},   # {found[sym]["instrument"]}')
        else:
            print(f'    # "{sym}": ???  (not found)')
    print("}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=[])
    args = parser.parse_args()
    if not args.symbols:
        print("Indices (hardcoded):")
        for k, v in KNOWN.items():
            print(f"  {k}: {v['security_id']}")
        print("\nPass --symbols RELIANCE TCS ... to look up F&O stocks.")
        return
    rows = fetch_master()
    search(rows, args.symbols)


if __name__ == "__main__":
    main()
