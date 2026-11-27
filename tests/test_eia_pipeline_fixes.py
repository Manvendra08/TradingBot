from datetime import datetime, timedelta
import sqlite3
import time
from unittest.mock import MagicMock, patch
import pytz

IST = pytz.timezone("Asia/Kolkata")


def test_get_eia_schedule_preserves_ff_12pm_et_thanksgiving_shift():
    """Forex Factory timestamp 12:00:00-05:00 must resolve to 22:30 IST, not hardcoded 21:00 IST."""
    from src.fetchers import eia_consensus_fetcher as ecf
    from src.engine.time_guards import get_eia_schedule

    old_cache = ecf._EIA_CACHE
    try:
        ecf._EIA_CACHE = {
            "report_date": "2026-11-25",
            "release_datetime": "2026-11-25T12:00:00-05:00",
            "consensus_bcf": -19.0,
        }
        now_ist = IST.localize(datetime(2026, 11, 25, 21, 5, 0))
        sched = get_eia_schedule(now_ist)
        assert sched["is_today"] is True
        assert sched["report_date"] == "2026-11-25"
        assert (sched["release_hour"], sched["release_minute"]) == (22, 30)
        assert sched["is_post_release"] is False
        assert sched["is_event_regime"] is False

        post_ist = IST.localize(datetime(2026, 11, 25, 22, 35, 0))
        sched_post = get_eia_schedule(post_ist)
        assert sched_post["is_post_release"] is True
        assert sched_post["is_event_regime"] is True
    finally:
        ecf._EIA_CACHE = old_cache


def test_fetch_complete_eia_dataset_blocks_stale_totals_prerelease_and_allows_backfill(tmp_path):
    """Pre-release must NOT copy prior week's totals/actual; next-day backfill accepts recent official print."""
    from src.fetchers import eia_consensus_fetcher as ecf

    db_file = tmp_path / "eia_test.db"
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE eia_consensus (
            report_date TEXT PRIMARY KEY,
            consensus_bcf REAL,
            actual_bcf REAL,
            surprise_bcf REAL,
            five_year_avg_bcf REAL,
            five_year_total_bcf REAL,
            surplus_vs_5yr_bcf REAL,
            surplus_vs_5yr_pct REAL,
            total_storage_bcf REAL,
            year_ago_bcf REAL,
            pct_change_yrago REAL,
            prior_week_revised_bcf REAL,
            prior_week_revision_flag INTEGER,
            seasonal_regime TEXT,
            macro_stance TEXT,
            stance_summary TEXT,
            key_levels_json TEXT,
            valid_until TEXT,
            fetched_at TEXT,
            source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO eia_consensus (report_date, consensus_bcf, actual_bcf, five_year_total_bcf, macro_stance) VALUES (?, ?, ?, ?, ?)",
        ("2026-11-19", 25.0, 20.0, 3802.0, "NEUTRAL_BALANCED"),
    )
    conn.execute(
        "INSERT INTO eia_consensus (report_date, consensus_bcf, actual_bcf, source) VALUES (?, ?, ?, ?)",
        ("2026-11-25", -19.0, 20.0, "eia_official_plus_forexfactory"),
    )
    conn.commit()
    conn.close()

    def _get_conn():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    # 1. Pre-release at Nov 25 21:00 IST with FF pointing to 22:30 IST & official still on Nov 19
    stale_official = {
        "report_date": "2026-11-19",
        "actual_bcf": 20.0,
        "total_storage_bcf": 3882.0,
        "five_year_total_bcf": 3802.0,
        "surplus_vs_5yr_pct": 2.1,
        "year_ago_bcf": 3948.0,
        "pct_change_yrago": -1.7,
        "prior_week_revision_flag": 0,
    }
    ff_nov25 = {
        "report_date": "2026-11-25",
        "release_datetime": "2026-11-25T12:00:00-05:00",
        "consensus_bcf": -19.0,
        "actual_bcf": None,
    }

    with patch.object(ecf, "get_conn", side_effect=_get_conn), \
         patch.object(ecf, "fetch_eia_weekly_data", return_value=ff_nov25), \
         patch.object(ecf, "fetch_official_eia_wngsr", return_value=stale_official), \
         patch.object(ecf, "_EIA_CACHE", ff_nov25), \
         patch.object(ecf, "datetime") as mock_dt:
        mock_dt.now.return_value = IST.localize(datetime(2026, 11, 25, 21, 0, 0))
        mock_dt.strptime.side_effect = datetime.strptime
        res_pre = ecf.fetch_complete_eia_dataset()

    assert res_pre["actual_bcf"] is None
    assert res_pre["total_storage_bcf"] is None
    assert res_pre["five_year_total_bcf"] is None
    assert res_pre["five_year_avg_bcf"] is None

    # 2. Backfill on Nov 27 (sched already moved to 2026-12-03) when wngsr has 2026-11-25 (-24 Bcf)
    nov25_official = {
        "report_date": "2026-11-25",
        "actual_bcf": -24.0,
        "total_storage_bcf": 3858.0,
        "five_year_total_bcf": 3778.0,
        "surplus_vs_5yr_pct": 2.1,
        "year_ago_bcf": 3937.0,
        "pct_change_yrago": -2.0,
        "prior_week_revision_flag": 0,
    }
    with patch.object(ecf, "get_conn", side_effect=_get_conn), \
         patch.object(ecf, "fetch_eia_weekly_data", return_value={"report_date": "2026-12-03", "consensus_bcf": -45.0}), \
         patch.object(ecf, "fetch_official_eia_wngsr", return_value=nov25_official), \
         patch.object(ecf, "_EIA_CACHE", None), \
         patch.object(ecf, "datetime") as mock_dt:
        mock_dt.now.return_value = IST.localize(datetime(2026, 11, 27, 21, 0, 0))
        mock_dt.strptime.side_effect = datetime.strptime
        res_backfill = ecf.fetch_complete_eia_dataset()

    assert res_backfill["report_date"] == "2026-11-25"
    assert res_backfill["actual_bcf"] == -24.0
    assert res_backfill["consensus_bcf"] == -19.0
    assert res_backfill["surprise_bcf"] == -5.0
    assert res_backfill["total_storage_bcf"] == 3858.0


def test_fetch_eia_actual_fallback_rejects_mismatched_release_date():
    """HTML fallback must NOT return prior week's print when Released date != expected_report_date."""
    from src.engine.ng_eia_strategy import fetch_eia_actual_fallback

    html_nov19 = """
    <html><body>
    Weekly Natural Gas Storage Report | Released: November 19, 2026 at 10:30 a.m.
    <table><tr><td>Total</td><td>3,882</td><td>3,862</td><td>20</td></tr></table>
    </body></html>
    """
    mock_resp = MagicMock(status_code=200, text=html_nov19)
    with patch("requests.get", return_value=mock_resp):
        assert fetch_eia_actual_fallback(expected_report_date="2026-11-25") is None
        assert fetch_eia_actual_fallback(expected_report_date="2026-11-19") == 20.0


def test_should_run_eia_recalibration_retries_until_recalibrated(tmp_path):
    """Scheduler retries EIA recalibration until source='eia_analyzer_recalibration' is persisted."""
    from src.scheduler.job_runner import _should_run_eia_recalibration

    db_file = tmp_path / "sched_test.db"
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE eia_consensus (report_date TEXT PRIMARY KEY, actual_bcf REAL, source TEXT)")
    conn.execute("INSERT INTO eia_consensus VALUES ('2026-11-25', 20.0, 'eia_official_plus_forexfactory')")
    conn.commit()
    conn.close()

    def _get_conn():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    now_wed = IST.localize(datetime(2026, 11, 25, 22, 35, 0))
    sched_wed = {
        "is_today": True,
        "report_date": "2026-11-25",
        "scheduled_release_dt": IST.localize(datetime(2026, 11, 25, 22, 30, 0)),
    }
    with patch("src.models.schema.get_conn", side_effect=_get_conn):
        assert _should_run_eia_recalibration(now_wed, sched_wed, last_attempt_ts=0.0) is True
        assert _should_run_eia_recalibration(now_wed, sched_wed, last_attempt_ts=time.time() - 60.0) is False

        # Friday backfill triggers if 2026-11-25 was never recalibrated by eia_analyzer
        now_fri = IST.localize(datetime(2026, 11, 27, 21, 5, 0))
        sched_next = {"is_today": False, "report_date": "2026-12-03", "scheduled_release_dt": now_fri + timedelta(days=6)}
        assert _should_run_eia_recalibration(now_fri, sched_next, last_attempt_ts=0.0) is True

        # Once eia_analyzer_recalibration is stored, retries stop
        with sqlite3.connect(db_file) as c:
            c.execute("UPDATE eia_consensus SET actual_bcf=-24.0, source='eia_analyzer_recalibration' WHERE report_date='2026-11-25'")
            c.commit()
        assert _should_run_eia_recalibration(now_wed, sched_wed, last_attempt_ts=0.0) is False
        assert _should_run_eia_recalibration(now_fri, sched_next, last_attempt_ts=0.0) is False
