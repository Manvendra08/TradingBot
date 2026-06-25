"""
Integration tests — full pipeline with a mocked fetcher.
Tests the complete Fetch→Persist→Detect→Alert→Dedup cycle.
Run: pytest tests/test_integration.py -v
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone


FETCHED_AT = datetime.now(timezone.utc).isoformat()


class TestPipelineIntegration:
    """
    Exercises pipeline.run_pipeline() end-to-end with:
      - mocked fetcher router (no real API calls)
      - real SQLite (isolated temp DB via conftest)
      - mocked Telegram (no real sends)
    """

    def _run_with_oc(self, oc_data: dict, symbol: str = "NIFTY"):
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc_data):
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=[symbol])

    def test_pipeline_persists_snapshots(self, sample_oc_nifty):
        from src.models.schema import get_latest_snapshots_for_symbol
        self._run_with_oc(sample_oc_nifty, "NIFTY")
        rows = get_latest_snapshots_for_symbol("NIFTY", "2030-06-26")
        assert len(rows) > 0
        assert all(r["symbol"] == "NIFTY" for r in rows)

    def test_pipeline_persists_underlying(self, sample_oc_nifty):
        from src.models.schema import get_previous_underlying
        self._run_with_oc(sample_oc_nifty, "NIFTY")
        row = get_previous_underlying("NIFTY")
        assert row is not None
        assert row["price"] == pytest.approx(22000.0)

    def test_pipeline_detects_oi_spike_and_stores_alert(self, sample_oc_nifty):
        from src.models.schema import get_alert_history, get_latest_snapshots_for_symbol

        # First run — establishes baseline
        self._run_with_oc(sample_oc_nifty, "NIFTY")

        # Second run — inject a 40% OI spike on ATM CE
        import copy
        oc2 = copy.deepcopy(sample_oc_nifty)
        for row in oc2["strikes"]:
            if row["strike"] == 22000.0 and row["option_type"] == "CE":
                row["oi"] = int(row["oi"] * 1.40)   # +40%

        self._run_with_oc(oc2, "NIFTY")

        alerts = get_alert_history("NIFTY", limit=50)
        oi_alerts = [a for a in alerts if a["alert_type"] == "OI_SPIKE"]
        assert len(oi_alerts) >= 1

    def test_pipeline_dedup_suppresses_repeat_alert(self, sample_oc_nifty):
        from src.models.schema import get_alert_history
        import copy

        def count_alerts():
            return len([a for a in get_alert_history("NIFTY", limit=200)
                        if a["alert_type"] == "OI_SPIKE"])

        # Run 1: baseline
        self._run_with_oc(sample_oc_nifty, "NIFTY")
        # Run 2: spike
        oc2 = copy.deepcopy(sample_oc_nifty)
        for row in oc2["strikes"]:
            if row["strike"] == 22000.0 and row["option_type"] == "CE":
                row["oi"] = int(row["oi"] * 1.40)
        self._run_with_oc(oc2, "NIFTY")
        after_first_spike = count_alerts()

        # Run 3: same spike again — dedup should suppress
        self._run_with_oc(oc2, "NIFTY")
        after_second_spike = count_alerts()

        assert after_second_spike == after_first_spike, \
            "Dedup should prevent re-alerting same strike within cooldown"

    def test_pipeline_no_data_doesnt_crash(self):
        with patch("src.engine.pipeline.fetch_option_chain", return_value=None):
            from src.engine.pipeline import run_pipeline
            # Should not raise
            run_pipeline(symbols=["NIFTY"])

    def test_pipeline_continues_when_chart_fetch_fails(self, sample_oc_nifty):
        from src.models.schema import get_latest_snapshots_for_symbol

        with patch("src.engine.pipeline.fetch_option_chain", return_value=sample_oc_nifty), \
             patch("src.engine.pipeline.get_chart_fetcher") as mock_chart:
            mock_chart.return_value.fetch.return_value = {}
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])

        rows = get_latest_snapshots_for_symbol("NIFTY", "2030-06-26")
        assert len(rows) > 0

    def test_pipeline_banknifty_independent(self, sample_oc_banknifty):
        from src.models.schema import get_latest_snapshots_for_symbol
        self._run_with_oc(sample_oc_banknifty, "BANKNIFTY")
        rows = get_latest_snapshots_for_symbol("BANKNIFTY", "2030-06-26")
        assert len(rows) > 0

    def test_alert_json_is_valid(self, sample_oc_nifty):
        from src.models.schema import get_alert_history
        import copy

        oc2 = copy.deepcopy(sample_oc_nifty)
        for row in oc2["strikes"]:
            if row["strike"] == 22000.0 and row["option_type"] == "PE":
                row["oi"] = int(row["oi"] * 1.50)   # +50% on PE

        self._run_with_oc(sample_oc_nifty, "NIFTY")
        self._run_with_oc(oc2, "NIFTY")

        alerts = get_alert_history("NIFTY", limit=10)
        for alert in alerts:
            detail = json.loads(alert["detail_json"])
            assert isinstance(detail, dict)

    def test_pipeline_continues_when_chart_fetch_crashes(self, sample_oc_nifty):
        from src.models.schema import get_latest_snapshots_for_symbol
        with patch("src.engine.pipeline.fetch_option_chain", return_value=sample_oc_nifty), \
             patch("src.engine.pipeline.get_chart_fetcher") as mock_chart:
            mock_chart.return_value.fetch.side_effect = Exception("Chart fetch crash")
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])
        rows = get_latest_snapshots_for_symbol("NIFTY", "2030-06-26")
        assert len(rows) > 0

    def test_pipeline_continues_when_process_symbol_crashes(self):
        with patch("src.engine.pipeline._process_symbol", side_effect=Exception("Symbol process crash")):
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])

    def test_pipeline_continues_when_save_scan_summary_fails(self, sample_oc_nifty):
        with patch("src.engine.pipeline.fetch_option_chain", return_value=sample_oc_nifty), \
             patch("src.engine.pipeline.save_scan_summary", side_effect=Exception("Save summary failed")):
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])

    def test_pipeline_continues_when_paper_trading_fails(self, sample_oc_nifty):
        with patch("src.engine.pipeline.fetch_option_chain", return_value=sample_oc_nifty), \
             patch("src.engine.pipeline.run_paper_trading", side_effect=Exception("Paper trading failed")):
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])

    def test_pipeline_zero_alerts_send_scenarios(self, sample_oc_nifty):
        import copy
        oc = copy.deepcopy(sample_oc_nifty)
        # Scenario 1: max_oi >= 1.0 (should_send = True)
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc), \
             patch("src.engine.pipeline.detect_anomalies") as mock_detect, \
             patch("src.engine.pipeline.send_text") as mock_send:
            mock_detect.return_value = ([], {"diagnostics": {"max_oi_delta_pct": 1.5}})
            from src.engine.pipeline import run_pipeline
            run_pipeline(symbols=["NIFTY"])
            mock_send.assert_called_once()

        # Scenario 2: duplicate alerts and should_send_zero_signal is False (should_send = False)
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc), \
             patch("src.engine.pipeline.detect_anomalies") as mock_detect, \
             patch("src.engine.pipeline.is_duplicate", return_value=True), \
             patch("src.engine.pipeline.should_send_zero_signal", return_value=False), \
             patch("src.engine.pipeline.send_text") as mock_send:
            mock_detect.return_value = ([{"alert_type": "OI_SPIKE"}], {"diagnostics": {"max_oi_delta_pct": 0.5}})
            run_pipeline(symbols=["NIFTY"])
            mock_send.assert_not_called()

        # Scenario 3: duplicate alerts and should_send_zero_signal is True (should_send = True) -> covers line 138
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc), \
             patch("src.engine.pipeline.detect_anomalies") as mock_detect, \
             patch("src.engine.pipeline.is_duplicate", return_value=True), \
             patch("src.engine.pipeline.should_send_zero_signal", return_value=True), \
             patch("src.engine.pipeline.send_text") as mock_send:
            mock_detect.return_value = ([{"alert_type": "OI_SPIKE"}], {"diagnostics": {"max_oi_delta_pct": 0.5}})
            run_pipeline(symbols=["NIFTY"])
            mock_send.assert_called_once()

        # Scenario 4: underlying price is None -> covers line 70-72
        oc_none = copy.deepcopy(sample_oc_nifty)
        oc_none["underlying_price"] = None
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc_none), \
             patch("src.engine.pipeline.send_text"):
            run_pipeline(symbols=["NIFTY"])

        # Scenario 5: sent_digest is True with new alerts -> covers line 165
        with patch("src.engine.pipeline.fetch_option_chain", return_value=oc), \
             patch("src.engine.pipeline.detect_anomalies") as mock_detect, \
             patch("src.engine.pipeline.is_duplicate", return_value=False), \
             patch("src.engine.pipeline.record_alert"), \
             patch("src.engine.pipeline.send_text", return_value=True) as mock_send, \
             patch("src.engine.pipeline.insert_alert", return_value=123) as mock_insert, \
             patch("src.engine.pipeline.mark_telegram_sent") as mock_mark:
            mock_detect.return_value = ([{"symbol": "NIFTY", "alert_type": "OI_SPIKE", "strike": 22000.0, "option_type": "CE", "severity": "HIGH", "detail_json": '{"pct_change": 40.0}'}], {"diagnostics": {"max_oi_delta_pct": 0.5}})
            run_pipeline(symbols=["NIFTY"])
            mock_mark.assert_called_once_with(123)



class TestFetcherRouter:
    """Tests fetcher fallback chain logic."""

    def test_router_returns_first_success(self):
        from src.fetchers.router import fetch_option_chain
        mock_data = {
            "symbol": "NIFTY", "underlying_price": 22000.0,
            "expiry": "2030-06-26", "strikes": [
                {"strike": 22000, "option_type": "CE", "oi": 100000,
                 "ltp": 120, "iv": 15, "oi_change": 0, "volume": 0, "bid": 119, "ask": 121}
            ], "source": "dhan"
        }
        with patch("src.fetchers.router._get_fetcher") as mock_get:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_option_chain.return_value = mock_data
            mock_get.return_value = mock_fetcher
            result = fetch_option_chain("NIFTY")
        assert result is not None
        assert result["symbol"] == "NIFTY"

    def test_router_falls_back_on_none(self):
        from src.fetchers.router import fetch_option_chain

        call_order = []
        mock_data = {
            "symbol": "NIFTY", "underlying_price": 22000.0,
            "expiry": "2030-06-26", "strikes": [
                {"strike": 22000, "option_type": "CE", "oi": 1, "ltp": 1,
                 "iv": 0, "oi_change": 0, "volume": 0, "bid": 0, "ask": 0}
            ], "source": "nse_public"
        }

        def side_effect(name):
            m = MagicMock()
            if name == "dhan":
                m.fetch_option_chain.return_value = None    # dhan fails
            elif name == "nse_public":
                m.fetch_option_chain.return_value = mock_data  # fallback succeeds
            else:
                m.fetch_option_chain.return_value = None
            call_order.append(name)
            return m

        with patch("src.fetchers.router.FETCHER_PRIORITY", ["dhan", "nse_public"]):
            with patch("src.fetchers.router._get_fetcher", side_effect=side_effect):
                result = fetch_option_chain("NIFTY")

        assert result is not None
        assert "dhan" in call_order
        assert "nse_public" in call_order

    def test_router_returns_none_if_all_fail(self):
        from src.fetchers.router import fetch_option_chain
        with patch("src.fetchers.router._get_fetcher") as mock_get:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_option_chain.return_value = None
            mock_get.return_value = mock_fetcher
            result = fetch_option_chain("NIFTY")
        assert result is None


class TestTelegramFormatter:
    """Tests alert message formatting — no actual Telegram send."""

    def _make_alert(self, alert_type, detail):
        return {
            "fired_at":    datetime.now(timezone.utc).isoformat(),
            "symbol":      "NIFTY",
            "alert_type":  alert_type,
            "strike":      22000.0,
            "option_type": "CE",
            "expiry":      "2030-06-26",
            "detail_json": json.dumps(detail),
            "telegram_sent": 0,
        }

    def test_oi_spike_message_contains_key_fields(self):
        from src.alerts.telegram_dispatcher import _format_message
        alert = self._make_alert("OI_SPIKE", {
            "strike": 22000, "option_type": "CE",
            "prev_oi": 100000, "curr_oi": 140000,
            "pct_change": 40.0, "prev_ltp": 100.0, "curr_ltp": 145.0,
            "underlying": 22000.0,
        })
        msg = _format_message(alert)
        assert "OI_SPIKE" in msg
        assert "NIFTY" in msg
        assert "22000" in msg
        assert any(x in msg for x in ("1.0L", "1.00L", "1.4L", "1.40L", "100000", "1,00,000"))

    def test_price_spike_message(self):
        from src.alerts.telegram_dispatcher import _format_message
        alert = self._make_alert("PRICE_SPIKE", {
            "prev_price": 22000.0, "curr_price": 22350.0,
            "pct_change": 1.59, "direction": "UP",
        })
        msg = _format_message(alert)
        assert "PRICE_SPIKE" in msg
        assert "UP" in msg

    def test_max_pain_message(self):
        from src.alerts.telegram_dispatcher import _format_message
        alert = self._make_alert("MAX_PAIN_SHIFT", {
            "prev_max_pain": 21900.0, "curr_max_pain": 22100.0,
            "shift": 200.0, "underlying": 22050.0,
        })
        msg = _format_message(alert)
        assert "MAX_PAIN_SHIFT" in msg
        assert "22100" in msg


class TestSchedulerMarketHours:
    """Tests IST market-hours guard."""

    def test_outside_hours_skipped(self):
        from src.scheduler.job_runner import _is_open_for
        import pytz
        from datetime import datetime as dt

        IST = pytz.timezone("Asia/Kolkata")
        # Saturday 10:00 IST — weekend
        saturday = IST.localize(dt(2025, 6, 28, 10, 0, 0))
        with patch("src.scheduler.job_runner.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            assert _is_open_for("NIFTY") is False

    def test_inside_hours_open(self):
        from src.scheduler.job_runner import _is_open_for
        import pytz
        from datetime import datetime as dt

        IST = pytz.timezone("Asia/Kolkata")
        # Monday 10:30 IST
        monday = IST.localize(dt(2025, 6, 23, 10, 30, 0))
        with patch("src.scheduler.job_runner.datetime") as mock_dt:
            mock_dt.now.return_value = monday
            assert _is_open_for("NIFTY") is True

    def test_before_open_skipped(self):
        from src.scheduler.job_runner import _is_open_for
        import pytz
        from datetime import datetime as dt

        IST = pytz.timezone("Asia/Kolkata")
        # Monday 08:00 IST — before NSE open
        early = IST.localize(dt(2025, 6, 23, 8, 0, 0))
        with patch("src.scheduler.job_runner.datetime") as mock_dt:
            mock_dt.now.return_value = early
            assert _is_open_for("NIFTY") is False

    def test_after_close_skipped(self):
        from src.scheduler.job_runner import _is_open_for
        import pytz
        from datetime import datetime as dt

        IST = pytz.timezone("Asia/Kolkata")
        # Friday 16:00 IST — after NSE close
        late = IST.localize(dt(2025, 6, 20, 16, 0, 0))
        with patch("src.scheduler.job_runner.datetime") as mock_dt:
            mock_dt.now.return_value = late
            assert _is_open_for("NIFTY") is False

    def test_mcx_open_evening(self):
        """MCX commodity stays open until 23:30 IST."""
        from src.scheduler.job_runner import _is_open_for
        import pytz
        from datetime import datetime as dt

        IST = pytz.timezone("Asia/Kolkata")
        # Monday 21:00 IST — NSE closed, MCX open
        evening = IST.localize(dt(2025, 6, 23, 21, 0, 0))
        with patch("src.scheduler.job_runner.datetime") as mock_dt:
            mock_dt.now.return_value = evening
            assert _is_open_for("NIFTY") is False
            assert _is_open_for("NATURALGAS") is True

    def test_guarded_run_all_closed(self):
        from src.scheduler.job_runner import _guarded_run
        with patch("src.scheduler.job_runner._is_open_for", return_value=False), \
             patch("src.scheduler.job_runner.run_pipeline") as mock_pipeline:
            _guarded_run()
            mock_pipeline.assert_not_called()

    def test_guarded_run_some_open(self):
        from src.scheduler.job_runner import _guarded_run
        def side_effect(sym):
            return sym == "NATURALGAS"
        with patch("src.scheduler.job_runner._is_open_for", side_effect=side_effect), \
             patch("src.scheduler.job_runner.run_pipeline") as mock_pipeline:
            _guarded_run()
            mock_pipeline.assert_called_once_with(symbols=["NATURALGAS"])

    def test_start_scheduler(self):
        from src.scheduler.job_runner import start_scheduler
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                dt = datetime(2026, 6, 17, 10, 0, 0)
                if tz is not None:
                    if hasattr(tz, "localize"):
                        return tz.localize(dt)
                    return dt.replace(tzinfo=tz)
                return dt
            @classmethod
            def fromtimestamp(cls, ts, tz=None):
                return datetime.fromtimestamp(ts, tz)
        t2 = ist.localize(datetime(2026, 6, 17, 10, 15, 0)).timestamp()
        with patch("src.scheduler.job_runner.datetime", FakeDatetime), \
             patch("time.time", return_value=t2), \
             patch("src.scheduler.job_runner._guarded_run") as mock_run, \
             patch("src.scheduler.job_runner._run_dhan_naturalgas_scrape") as mock_scrape, \
             patch("time.sleep", side_effect=SystemExit) as mock_sleep:
            try:
                start_scheduler()
            except SystemExit:
                pass
            from unittest.mock import call
            # Scheduler now uses market classes (NSE_INDEX, MCX_COMMODITY) in its loop
            calls = [c for c in mock_run.call_args_list]
            assert len(calls) >= 2
            args = {c[0][0] for c in calls if c[0]}
            assert 'NSE_INDEX' in args
            assert 'MCX_COMMODITY' in args
            mock_scrape.assert_called_once()

    def test_start_scheduler_keyboard_interrupt(self):
        from src.scheduler.job_runner import start_scheduler
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                dt = datetime(2026, 6, 17, 10, 0, 0)
                if tz is not None:
                    if hasattr(tz, "localize"):
                        return tz.localize(dt)
                    return dt.replace(tzinfo=tz)
                return dt
            @classmethod
            def fromtimestamp(cls, ts, tz=None):
                return datetime.fromtimestamp(ts, tz)
        t2 = ist.localize(datetime(2026, 6, 17, 10, 15, 0)).timestamp()
        with patch("src.scheduler.job_runner.datetime", FakeDatetime), \
             patch("time.time", return_value=t2), \
             patch("src.scheduler.job_runner._guarded_run") as mock_run, \
             patch("time.sleep", side_effect=KeyboardInterrupt) as mock_sleep:
            start_scheduler()  # Handles KeyboardInterrupt without throwing
            from unittest.mock import call
            # Scheduler now uses market classes (NSE_INDEX, MCX_COMMODITY) in its loop
            calls = [c for c in mock_run.call_args_list]
            assert len(calls) >= 2
            args = {c[0][0] for c in calls if c[0]}
            assert 'NSE_INDEX' in args
            assert 'MCX_COMMODITY' in args

    def test_run_dhan_naturalgas_scrape_runner_missing(self):
        from src.scheduler.job_runner import _run_dhan_naturalgas_scrape
        with patch("src.scheduler.job_runner.SCRAPE_RUNNER") as mock_runner:
            mock_runner.exists.return_value = False
            _run_dhan_naturalgas_scrape()

    def test_run_dhan_naturalgas_scrape_success(self):
        from src.scheduler.job_runner import _run_dhan_naturalgas_scrape
        with patch("src.scheduler.job_runner.SCRAPE_RUNNER") as mock_runner, \
             patch("subprocess.run") as mock_run:
            mock_runner.exists.return_value = True
            mock_run.return_value.stdout = "Dhan scrape output"
            _run_dhan_naturalgas_scrape()

    def test_run_dhan_naturalgas_scrape_called_process_error(self):
        from src.scheduler.job_runner import _run_dhan_naturalgas_scrape
        import subprocess
        with patch("src.scheduler.job_runner.SCRAPE_RUNNER") as mock_runner, \
             patch("subprocess.run") as mock_run:
            mock_runner.exists.return_value = True
            mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="Process failed")
            _run_dhan_naturalgas_scrape()

    def test_run_dhan_naturalgas_scrape_general_exception(self):
        from src.scheduler.job_runner import _run_dhan_naturalgas_scrape
        with patch("src.scheduler.job_runner.SCRAPE_RUNNER") as mock_runner, \
             patch("subprocess.run") as mock_run:
            mock_runner.exists.return_value = True
            mock_run.side_effect = Exception("General error")
            _run_dhan_naturalgas_scrape()

