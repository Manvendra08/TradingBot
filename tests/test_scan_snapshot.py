import pytest
from src.models.scan_snapshot import ScanSnapshot, create_scan_snapshot

def test_scan_snapshot_immutability():
    snap = create_scan_snapshot(
        symbol="NIFTY",
        underlying=24500.0,
        expiry="2026-09-10",
        option_rows=[{"strike": 24500, "option_type": "CE", "ltp": 120.0}],
        engine_verdict="BULLISH_BUILDUP",
        engine_confidence=82
    )
    assert snap.snapshot_id.startswith("snap_NIFTY_")
    assert snap.underlying == 24500.0

    with pytest.raises(AttributeError):
        snap.underlying = 25000.0  # Frozen dataclass check

def test_scan_snapshot_option_rows_hash():
    snap1 = create_scan_snapshot("NIFTY", 24500.0, "2026-09-10", [{"strike": 24500, "ltp": 120.0}])
    snap2 = create_scan_snapshot("NIFTY", 24500.0, "2026-09-10", [{"strike": 24500, "ltp": 120.0}])
    assert snap1.option_rows_hash == snap2.option_rows_hash

def test_scan_snapshot_to_dict():
    snap = create_scan_snapshot(
        symbol="BANKNIFTY",
        underlying=52000.0,
        expiry="2026-09-10",
        option_rows=[{"strike": 52000, "option_type": "PE", "ltp": 250.0}],
        engine_verdict="BEARISH_BUILDUP",
        engine_confidence=75,
        atm_strike=52000.0,
        data_legitimacy_score=95,
        intel={"trend": "BEARISH"}
    )
    d = snap.to_dict()
    assert d["symbol"] == "BANKNIFTY"
    assert d["underlying"] == 52000.0
    assert d["expiry"] == "2026-09-10"
    assert d["option_rows_hash"] == snap.option_rows_hash
    assert d["intel"] == {"trend": "BEARISH"}
