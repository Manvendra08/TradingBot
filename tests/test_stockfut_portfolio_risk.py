"""Tests for portfolio risk controls and gap stress checks."""

from src.stockfut.portfolio_risk import (
    check_entry,
    daily_loss_exceeded,
    weekly_loss_exceeded,
    consecutive_losses_exceeded,
    set_halt,
    is_halted,
)


def test_max_positions_limit():
    equity = 10000000.0  # 1 Cr
    candidate = {"symbol": "NEWSTOCK", "side": "LONG", "lots": 2, "lot_size": 250, "entry_price": 1000.0}
    # 6 open positions already
    open_pos = [{"symbol": f"SYM{i}", "side": "LONG", "lots": 2, "lot_size": 250, "entry_price": 1000.0} for i in range(6)]

    ok, reason = check_entry(candidate, open_pos, equity=equity)
    assert ok is False
    assert "MAX_POSITIONS_REACHED" in reason


def test_sector_concentration_limit():
    equity = 10000000.0
    candidate = {"symbol": "INFY", "side": "LONG", "lots": 2, "lot_size": 400, "entry_price": 1800.0}
    # 2 IT stocks already open: TCS, WIPRO
    open_pos = [
        {"symbol": "TCS", "side": "LONG", "lots": 2, "lot_size": 175, "entry_price": 4000.0},
        {"symbol": "WIPRO", "side": "LONG", "lots": 2, "lot_size": 1500, "entry_price": 500.0},
    ]
    sector_map = {"INFY": "IT", "TCS": "IT", "WIPRO": "IT"}

    ok, reason = check_entry(candidate, open_pos, equity=equity, sector_map=sector_map)
    assert ok is False
    assert "SECTOR_LIMIT_REACHED" in reason


def test_gap_stress_blocks_candidate_that_breaches():
    equity = 10000000.0  # 1 Cr, gap stress cap = 6% = ₹600,000
    # Candidate with ₹1.5 Cr notional
    candidate = {"symbol": "BIG", "side": "LONG", "lots": 10, "lot_size": 1000, "entry_price": 1500.0}
    # Stressed gap loss = 5% of 1.5 Cr = ₹750,000 > ₹600,000 cap!
    ok, reason = check_entry(candidate, [], equity=equity)
    assert ok is False
    assert "GAP_STRESS_BREACH" in reason


def test_daily_loss_counts_unrealised():
    equity = 10000000.0  # 1 Cr, 2% limit = ₹200,000
    # Realized -100k, Unrealized -110k -> Total -210k -> Exceeded!
    assert daily_loss_exceeded(equity=equity, realized_pnl=-100000.0, unrealized_pnl=-110000.0, limit_pct=2.0) is True
    # Realized -50k, Unrealized -100k -> Total -150k -> Safe
    assert daily_loss_exceeded(equity=equity, realized_pnl=-50000.0, unrealized_pnl=-100000.0, limit_pct=2.0) is False


def test_consecutive_losses():
    assert consecutive_losses_exceeded([-1000.0, -2000.0, -500.0], limit=3) is True
    assert consecutive_losses_exceeded([-1000.0, 5000.0, -500.0], limit=3) is False


def test_halt_flag_persistence():
    set_halt(True, reason="8% drawdown hit")
    halted, reason = is_halted()
    assert halted is True
    assert "8% drawdown hit" in reason

    # Clear halt
    set_halt(False)
    halted_cleared, _ = is_halted()
    assert halted_cleared is False
