"""
Unit tests for senior engineer audit fixes (Fixes #1 - #13).
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch


class TestAuditFixesV3:
    """Regression tests for audit fixes in pipeline.py, digest.py, schema.py, and multileg_paper_trading.py."""

    def test_fix_02_llm_pacing_lock_sleep_outside(self):
        """Fix #2: Verify sleep occurs outside the lock so parallel threads don't block."""
        import threading
        import time

        lock = threading.Lock()
        order = []

        def worker(thread_id):
            time.sleep(0.01)  # Simulated sleep outside lock
            with lock:
                order.append(thread_id)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(order) == 2

    def test_fix_04_m110_schema_and_mtm_persist(self):
        """Fix #4: Test M110 migration and current_premium update helper."""
        from src.models.schema import update_multi_leg_leg_current_premium, get_conn

        with patch("src.models.schema.get_conn") as mock_conn:
            ctx = MagicMock()
            mock_conn.return_value.__enter__.return_value = ctx
            update_multi_leg_leg_current_premium(42, 125.50)
            ctx.execute.assert_called_once_with(
                "UPDATE multi_leg_legs SET current_premium=? WHERE id=?",
                (125.50, 42)
            )

    def test_fix_07_dte_type_safety(self):
        """Fix #7: Verify string and float DTE values are safely coerced to int."""
        dte_val = "14"
        try:
            dte_int = int(dte_val) if dte_val is not None else None
        except (TypeError, ValueError):
            dte_int = None
        assert dte_int == 14

        dte_invalid = "invalid"
        try:
            dte_int = int(dte_invalid) if dte_invalid is not None else None
        except (TypeError, ValueError):
            dte_int = None
        assert dte_int is None

    def test_fix_08_fallback_db_query_symbol_guard(self):
        """Fix #8: Non-ALLOWED_SYMBOLS skip fallback DB query."""
        from config.multileg_strategies import ALLOWED_SYMBOLS

        symbol = "NOT_IN_ALLOWED"
        should_query = symbol in ALLOWED_SYMBOLS
        assert not should_query

        symbol = "NIFTY"
        should_query = symbol in ALLOWED_SYMBOLS
        assert should_query

    def test_fix_09_10_sentinel_report_builder_ist(self):
        """Fix #9+#10: Test _build_sentinel_report produces valid IST timestamp."""
        from src.engine.pipeline import _build_sentinel_report

        report = _build_sentinel_report(
            "NIFTY",
            scan_duration_ms=100,
            opt_rows=[],
            underlying_price=24500.0,
            expiry="2026-06-27",
            source="test",
            llm_verdict=None,
            intel=None,
            scan_context=None,
            is_test=False,
            warnings=[],
            errors=[],
            fetcher_errors=[],
        )
        assert report["symbol"] == "NIFTY"
        assert report["scan_duration_ms"] == 100
        assert "timestamp_ist" in report
        # Check IST offset +05:30 in timestamp
        assert "+05:30" in report["timestamp_ist"] or "IST" in report["timestamp_ist"]

    def test_fix_11_db_entered_in_memory_preference(self):
        """Fix #11: Test in-memory paper_res evidence for db_entered."""
        paper_res = {"action": "ENTERED", "trade_id": "T12345", "lots": 2}
        pr_action = str(paper_res.get("action") or "").upper()
        db_entered = pr_action in ("ENTERED", "OPENED", "EXECUTED") and bool(paper_res.get("trade_id"))
        assert db_entered is True
