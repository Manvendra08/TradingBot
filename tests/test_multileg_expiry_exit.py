"""
Regression tests for 0DTE Expiry Day multi-leg exit discipline and safety guards.
Prevents premature AI exit execution on weekly index option expiry mornings.
"""
from datetime import datetime, time as dt_time, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

from src.engine.multileg_llm_prompt import build_multileg_exit_prompt

IST = pytz.timezone("Asia/Kolkata")


class TestMultilegExpiryExitRules:
    """Tests for exit prompt formatting and safety guards on expiry day."""

    def test_pin_risk_normal_in_morning(self):
        """On expiry day morning (e.g. 11:19 IST), pin risk must evaluate to normal even if spot is within 0.5%."""
        now_morning = IST.localize(datetime(2026, 9, 10, 11, 19, 0))

        book = {
            "book_id": "SENSEX:20260910:IRON_CONDOR:1",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 73.9,
            "total_pnl": 0.0,
            "adjustment_count": 0,
            "entry_reason": "Pin near Max Pain 74800. Invalidation: Spot 75073.9",
        }
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 75000, "entry_premium": 50.0, "lots": 1, "delta": 0.25},
            {"side": "BUY", "option_type": "CE", "strike": 75200, "entry_premium": 20.0, "lots": 1, "delta": 0.10},
            {"side": "SELL", "option_type": "PE", "strike": 74500, "entry_premium": 60.0, "lots": 1, "delta": -0.25},
            {"side": "BUY", "option_type": "PE", "strike": 74300, "entry_premium": 16.1, "lots": 1, "delta": -0.10},
        ]
        ctx = {
            "underlying": 74796.9,
            "dte": 0,
            "days_to_expiry": 0,
            "option_rows": [],
        }
        intel = {"verdict_label": "Neutral", "confidence": 75}

        with patch("src.engine.multileg_llm_prompt.datetime") as mock_dt:
            mock_dt.now.return_value = now_morning
            mock_dt.strftime = datetime.strftime

            prompt = build_multileg_exit_prompt("SENSEX", book, legs, ctx, intel)

        # Pin risk should be normal (not HIGH) in morning
        assert "pin risk: normal" in prompt
        assert "BEFORE 13:00 IST, do NOT close for time decay or pin risk" in prompt

    def test_pin_risk_high_in_late_afternoon(self):
        """On expiry day late afternoon (e.g. 14:30 IST), pin risk evaluates to HIGH if spot is within 0.15% of short strike."""
        now_afternoon = IST.localize(datetime(2026, 9, 10, 14, 30, 0))

        book = {
            "book_id": "SENSEX:20260910:IRON_CONDOR:1",
            "strategy_type": "IRON_CONDOR",
            "net_premium": 73.9,
            "total_pnl": 500.0,
            "adjustment_count": 0,
        }
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": 75000, "entry_premium": 50.0, "lots": 1, "delta": 0.45},
            {"side": "BUY", "option_type": "CE", "strike": 75200, "entry_premium": 20.0, "lots": 1, "delta": 0.10},
        ]
        ctx = {
            "underlying": 74980.0,  # within 0.026% of 75000 (< 0.15%)
            "dte": 0,
            "days_to_expiry": 0,
            "option_rows": [],
        }
        intel = {"verdict_label": "Neutral", "confidence": 75}

        with patch("src.engine.multileg_llm_prompt.datetime") as mock_dt:
            mock_dt.now.return_value = now_afternoon
            mock_dt.strftime = datetime.strftime

            prompt = build_multileg_exit_prompt("SENSEX", book, legs, ctx, intel)

        assert "pin risk: HIGH" in prompt

    def test_paper_trading_suppresses_0dte_morning_ai_close(self):
        """_monitor_open_books must suppress AI exit CLOSE on 0DTE before 13:00 IST if PnL >= 0."""
        from src.engine.multileg_paper_trading import _monitor_open_books

        now_morning = IST.localize(datetime(2026, 9, 10, 11, 19, 0))
        now_iso = now_morning.astimezone(timezone.utc).isoformat()

        open_books = [
            {
                "id": 146,
                "book_id": "SENSEX:20260910:IRON_CONDOR:1",
                "symbol": "SENSEX",
                "strategy_type": "IRON_CONDOR",
                "net_premium": 73.9,
                "total_pnl": 0.0,
                "expiry": "2026-09-10",
                "entry_underlying": 74796.9,
                "adjustment_count": 0,
                "legs": [
                    {"id": 1, "side": "SELL", "option_type": "CE", "strike": 75000, "entry_premium": 50.0, "lots": 1, "delta": 0.25},
                    {"id": 2, "side": "BUY", "option_type": "CE", "strike": 75200, "entry_premium": 20.0, "lots": 1, "delta": 0.10},
                    {"id": 3, "side": "SELL", "option_type": "PE", "strike": 74500, "entry_premium": 60.0, "lots": 1, "delta": -0.25},
                    {"id": 4, "side": "BUY", "option_type": "PE", "strike": 74300, "entry_premium": 16.1, "lots": 1, "delta": -0.10},
                ],
            }
        ]

        mock_advice = {
            "action": "CLOSE",
            "urgency": "MEDIUM",
            "reasoning": "With zero days to expiration at 11:19 IST, underlying SENSEX is near short strike.",
        }

        ctx = {"underlying": 74796.9, "dte": 0, "option_rows": []}
        intel = {"verdict_label": "Neutral", "confidence": 75}

        with patch("src.engine.multileg_paper_trading._dte_from_expiry", return_value=0), \
             patch("src.engine.multileg_paper_trading.datetime") as mock_dt, \
             patch("src.engine.llm_enrichment.get_multileg_exit_advice", return_value=mock_advice), \
             patch("src.models.schema.close_book") as mock_close_book:

            mock_dt.now.side_effect = lambda tz=None: now_morning if tz else now_morning.replace(tzinfo=None)

            res = _monitor_open_books("SENSEX", ctx, "digest_123", intel, open_books, "full", now_iso)

            # close_book should NOT have been called for 0DTE morning AI close
            mock_close_book.assert_not_called()
