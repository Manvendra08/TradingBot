import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.symbol_resolver import resolve_instrument, _INSTRUMENT_CACHE

# Seed _INSTRUMENT_CACHE with mock data
_INSTRUMENT_CACHE.clear()
_INSTRUMENT_CACHE[("NATURALGAS", "2026-06-23", 0.0, "FUT")] = {
    "tradingsymbol": "NATURALGAS26JUNFUT",
    "instrument_token": 123456,
    "lot_size": 1250
}
_INSTRUMENT_CACHE[("NATURALGAS", "2026-07-28", 0.0, "FUT")] = {
    "tradingsymbol": "NATURALGAS26JULFUT",
    "instrument_token": 789012,
    "lot_size": 1250
}
_INSTRUMENT_CACHE[("BANKNIFTY", "2026-06-25", 58000.0, "CE")] = {
    "tradingsymbol": "BANKNIFTY2662558000CE",
    "instrument_token": 555555,
    "lot_size": 15
}

print("Mock instrument cache seeded.")

# Test case 1: exact match
res1 = resolve_instrument("NATURALGAS", "2026-06-23", 0.0, "FUT")
print("Test 1 (exact match):", res1)
assert res1 and res1["instrument_token"] == 123456

# Test case 2: cache search fallback for FUT with wrong strike and missing expiry
res2 = resolve_instrument("NATURALGAS", "", 290.0, "FUT")
print("Test 2 (fallback match FUT):", res2)
assert res2 and res2["instrument_token"] == 123456

# Test case 3: cache search fallback for option with missing expiry
res3 = resolve_instrument("BANKNIFTY", "", 58000.0, "CE")
print("Test 3 (fallback match Option):", res3)
assert res3 and res3["instrument_token"] == 555555

# Test case 4: no match at all
res4 = resolve_instrument("CRUDEOIL", "", 7600.0, "FUT")
print("Test 4 (no match):", res4)
assert res4 and res4["instrument_token"] is None
