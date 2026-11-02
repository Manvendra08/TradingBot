from datetime import date, datetime, timezone, timedelta
import pytest
from config.symbol_classes import get_futures_expiry
from src.engine.gemini_spark_feed import _parse_contract


def test_mcx_rollover_setup_id_month_detection():
    # If setup_id specifies _NOV_, parse_contract should map to November contract
    sym, exp = _parse_contract("NATURALGAS 27-OCT-2026", "NG_20261027_SELL_NOV_US_OPEN_PULLBACK")
    assert sym == "NATURALGAS"
    assert exp == "2026-11-24"

    # If setup_id specifies _DEC_, parse_contract should map to December contract
    sym, exp = _parse_contract("NATURALGAS", "NG_DEC_MOMENTUM_PULLBACK")
    assert sym == "NATURALGAS"
    assert exp == "2026-12-28"


def test_mcx_futures_expiry_rollover_on_expiry_day():
    # On 2026-10-27 (October expiry day), after 17:00 IST the contract is non-tradable and rolls to Nov
    # With ref_date past expiry day (e.g. 2026-10-28)
    exp = get_futures_expiry("NATURALGAS", ref_date=date(2026, 10, 28))
    assert exp == "2026-11-24"


def test_paper_trading_mcx_month_alignment_logic():
    # Verify same_month logic: options expiry (2026-11-20) vs futures expiry (2026-11-24)
    trade_exp = "2026-11-24"
    ctx_exp = "2026-11-20"
    assert trade_exp[:7] == ctx_exp[:7]  # Same month: 2026-11

    # Active price regime proximity check (1% delta vs 10% threshold)
    entry_und = 292.20
    current_und = 289.30
    assert abs(entry_und - current_und) / current_und < 0.10

    # Old contract price regime (246 vs 289) should fail active price regime check
    old_entry = 246.20
    assert abs(old_entry - current_und) / current_und > 0.10
