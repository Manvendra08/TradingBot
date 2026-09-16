"""Regression tests for the mandatory daily market-close scan in _maybe_market_close_scan()."""
from datetime import datetime, time as dt_time, timedelta
from unittest.mock import patch

import pytest
import pytz

from src.scheduler import job_runner as jr

IST = pytz.timezone("Asia/Kolkata")


def _ist(dt: datetime) -> datetime:
    return IST.localize(dt.replace(tzinfo=None))


def _run_close_scan(now_ist: datetime, watch_symbols: list[str], immediate: bool = False):
    """Run _maybe_market_close_scan with controlled state."""
    tracker: set[tuple[str, str]] = set()
    current_date = now_ist.strftime("%Y-%m-%d")

    guarded_calls = []

    original_symbols = jr.WATCH_SYMBOLS
    jr.WATCH_SYMBOLS = watch_symbols

    try:
        with patch.object(jr, "_guarded_run", side_effect=lambda ck, force: guarded_calls.append((ck, force))):
            triggered = jr._maybe_market_close_scan(
                now_ist=now_ist,
                immediate=immediate,
                close_tracker=tracker,
                current_date=current_date,
            )
    finally:
        jr.WATCH_SYMBOLS = original_symbols

    return triggered, guarded_calls


# ── NSE/BSE 15:38 trigger ─────────────────────────────────────────────────────


class TestNseMarketCloseTrigger:
    """NSE/BSE F&O final scan must fire at 15:38 IST (2 min before 15:40 close)."""

    def test_triggers_at_exact_close(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 38, 0))  # Wednesday
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        assert "NSE_INDEX" in triggered, f"Expected NSE_INDEX in triggered list, got: {triggered}"
        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) >= 1
        assert nse_calls[0][1] is True

    def test_triggers_within_window(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 43, 0))  # +5 min
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        assert "NSE_INDEX" in triggered, f"Expected NSE_INDEX within 10-min window, got: {triggered}"

    def test_does_not_trigger_before_close(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 30, 0))  # 8 min before
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) == 0, f"NSE_INDEX must NOT trigger before 15:38 IST, got: {calls}"
        assert "NSE_INDEX" not in triggered

    def test_does_not_trigger_after_window(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 49, 0))  # +11 min
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) == 0, f"NSE_INDEX must NOT trigger after +10 min window, got: {calls}"


# ── MCX 23:30 trigger ─────────────────────────────────────────────────────────


class TestMcxMarketCloseTrigger:
    """MCX final scan must fire at 23:30 IST."""

    def test_triggers_at_mcx_close(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 23, 30, 0))
        triggered, calls = _run_close_scan(now, ["NATURALGAS"])

        assert "MCX_COMMODITY" in triggered, f"Expected MCX_COMMODITY, got: {triggered}"
        mcx_calls = [c for c in calls if c[0] == "MCX_COMMODITY"]
        assert len(mcx_calls) >= 1
        assert mcx_calls[0][1] is True


# ── once-per-day dedup ────────────────────────────────────────────────────────


class TestMarketCloseDedup:
    """Market-close scan must run at most once per class per day."""

    def test_second_call_same_day_is_skipped(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 40, 0))
        current_date = now.strftime("%Y-%m-%d")

        # Shared tracker — simulates the module-level set across scheduler ticks
        tracker: set[tuple[str, str]] = set()
        original_symbols = jr.WATCH_SYMBOLS
        jr.WATCH_SYMBOLS = ["NIFTY"]

        # First call — should trigger
        guarded_calls1 = []
        try:
            with patch.object(jr, "_guarded_run", side_effect=lambda ck, force: guarded_calls1.append((ck, force))):
                triggered1 = jr._maybe_market_close_scan(
                    now_ist=now,
                    immediate=False,
                    close_tracker=tracker,
                    current_date=current_date,
                )
        finally:
            pass

        assert "NSE_INDEX" in triggered1, "First invocation should trigger NSE_INDEX"
        assert ("NSE_INDEX", current_date) in tracker, "Tracker must hold the key after first call"

        # Second call with the SAME tracker — must be skipped
        guarded_calls2 = []
        try:
            with patch.object(jr, "_guarded_run", side_effect=lambda ck, force: guarded_calls2.append((ck, force))):
                triggered2 = jr._maybe_market_close_scan(
                    now_ist=now,
                    immediate=False,
                    close_tracker=tracker,
                    current_date=current_date,
                )
        finally:
            jr.WATCH_SYMBOLS = original_symbols

        assert "NSE_INDEX" not in triggered2
        nse_calls2 = [c for c in guarded_calls2 if c[0] == "NSE_INDEX"]
        assert len(nse_calls2) == 0, f"Second same-day invocation must be deduplicated, got: {guarded_calls2}"


# ── weekend / holiday skip ────────────────────────────────────────────────────


class TestMarketCloseHolidaySkip:
    """Market-close scan must skip weekends and holidays."""

    def test_weekend_skipped(self, monkeypatch):
        now = _ist(datetime(2026, 9, 12, 15, 40, 0))  # Saturday
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) == 0, f"NSE_INDEX must NOT trigger on Saturday, got: {calls}"
        assert "NSE_INDEX" not in triggered

    def test_sunday_skipped(self, monkeypatch):
        now = _ist(datetime(2026, 9, 13, 15, 40, 0))  # Sunday
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) == 0, f"NSE_INDEX must NOT trigger on Sunday, got: {calls}"


# ── force=True semantics ──────────────────────────────────────────────────────


class TestMarketCloseForceFlag:
    """Force scan must pass force=True to _guarded_run."""

    def test_force_flag_passed(self, monkeypatch):
        now = _ist(datetime(2026, 9, 9, 15, 40, 0))
        triggered, calls = _run_close_scan(now, ["NIFTY"])

        nse_calls = [c for c in calls if c[0] == "NSE_INDEX"]
        assert len(nse_calls) >= 1, "Expected at least one NSE_INDEX call"
        assert nse_calls[0][1] is True, "_guarded_run must receive force=True"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
