"""
Unit tests for the Shoonya IP-change guard (src/fetchers/shoonya_ip_guard.py).

Covers: first-run baseline adoption, unchanged IP, rotation detection
(skip + alert once), same-day caching, fail-open on detection failure,
fast-path skip, date-scoped skip window, and repeated rotation next day.
"""

import json

import pytest

from src.fetchers import shoonya_ip_guard as guard


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point the guard at a throwaway state file, pin the IST date, and mock portal updater."""
    monkeypatch.setattr(guard, "STATE_PATH", tmp_path / "shoonya_ip_state.json")
    monkeypatch.setattr(guard, "_today", lambda: "2026-08-10")
    # Mock update_shoonya_portal_ip to fail by default so tests cover skip behavior without real Playwright execution
    monkeypatch.setattr(
        "src.fetchers.shoonya_ip_updater.update_shoonya_portal_ip",
        lambda **kw: (False, "mocked portal update failure"),
    )
    return tmp_path


def _write_state(tmp_path, **fields):
    data = {"baseline_ip": None, "checked_date": None, "skip_date": None}
    data.update(fields)
    p = tmp_path / "shoonya_ip_state.json"
    p.write_text(json.dumps(data), encoding="utf-8")


# ── run_daily_ip_check ─────────────────────────────────────────────────────


def test_first_run_adopts_baseline_no_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "1.2.3.4")

    res = guard.run_daily_ip_check()

    assert res["skip"] is False
    assert res["reason"] == "initial_adopt"
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["baseline_ip"] == "1.2.3.4"
    assert state["checked_date"] == "2026-08-10"
    assert state["skip_date"] is None


def test_unchanged_ip_no_skip(tmp_path, monkeypatch):
    _write_state(tmp_path, baseline_ip="1.2.3.4")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "1.2.3.4")

    res = guard.run_daily_ip_check()

    assert res["skip"] is False
    assert res["reason"] == "unchanged"
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["skip_date"] is None
    assert state["checked_date"] == "2026-08-10"


def test_rotation_skips_alerts_and_adopts_new_baseline(tmp_path, monkeypatch):
    _write_state(tmp_path, baseline_ip="1.2.3.4")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "5.6.7.8")
    sent = []
    monkeypatch.setattr(
        "src.alerts.telegram_dispatcher.send_text",
        lambda msg: sent.append(msg),
    )

    res = guard.run_daily_ip_check()

    assert res["skip"] is True
    assert res["reason"].startswith("ip_auto_update_failed")
    assert res["old_ip"] == "1.2.3.4"
    assert res["new_ip"] == "5.6.7.8"
    assert len(sent) == 1
    assert "1.2.3.4" in sent[0] and "5.6.7.8" in sent[0]
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["baseline_ip"] == "5.6.7.8"
    assert state["skip_date"] == "2026-08-10"


def test_same_day_second_call_cached_no_alert(tmp_path, monkeypatch):
    _write_state(
        tmp_path,
        baseline_ip="5.6.7.8",
        checked_date="2026-08-10",
        skip_date="2026-08-10",
    )
    # Fetch must NOT be called again on the same day.
    def _should_not_fetch(**kw):
        raise AssertionError("_fetch_public_ip should not run on cached day")

    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", _should_not_fetch)
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    res = guard.run_daily_ip_check()

    assert res["skip"] is True
    assert res["reason"] == "cached"
    assert sent == []


def test_fail_open_when_detection_fails(tmp_path, monkeypatch):
    _write_state(tmp_path, baseline_ip="1.2.3.4")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: None)
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    res = guard.run_daily_ip_check()

    assert res["skip"] is False
    assert res["reason"] == "ip_detect_failed"
    assert sent == []
    # checked_date left unset so the check is retried later in the day.
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["checked_date"] != "2026-08-10"


# ── shoonya_should_skip ────────────────────────────────────────────────────


def test_should_skip_fast_path_reads_state_without_fetch(tmp_path, monkeypatch):
    _write_state(
        tmp_path,
        baseline_ip="5.6.7.8",
        checked_date="2026-08-10",
        skip_date="2026-08-10",
    )

    def _should_not_fetch(**kw):
        raise AssertionError("_fetch_public_ip should not run on fast path")

    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", _should_not_fetch)

    assert guard.shoonya_should_skip() is True


def test_should_skip_lazy_runs_daily_check_when_not_checked_today(tmp_path, monkeypatch):
    # State says baseline 1.2.3.4 but nothing checked today -> lazy check runs.
    _write_state(tmp_path, baseline_ip="1.2.3.4", checked_date="2026-08-09")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "5.6.7.8")
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    assert guard.shoonya_should_skip() is True
    assert len(sent) == 1


def test_skip_window_is_date_scoped_next_day_clears(tmp_path, monkeypatch):
    # Yesterday a rotation was detected (baseline adopted to 5.6.7.8, skip that day).
    _write_state(
        tmp_path,
        baseline_ip="5.6.7.8",
        checked_date="2026-08-09",
        skip_date="2026-08-09",
    )
    monkeypatch.setattr(guard, "_today", lambda: "2026-08-10")
    # IP still 5.6.7.8 -> unchanged -> no skip.
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "5.6.7.8")
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    assert guard.shoonya_should_skip() is False
    assert sent == []
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["skip_date"] is None
    assert state["checked_date"] == "2026-08-10"


def test_next_day_new_rotation_skips_again(tmp_path, monkeypatch):
    # Baseline from previous rotation; ISP rotates again today.
    _write_state(
        tmp_path,
        baseline_ip="5.6.7.8",
        checked_date="2026-08-09",
        skip_date="2026-08-09",
    )
    monkeypatch.setattr(guard, "_today", lambda: "2026-08-10")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "9.9.9.9")
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    res = guard.run_daily_ip_check()

    assert res["skip"] is True
    assert res["reason"].startswith("ip_auto_update_failed")
    assert res["old_ip"] == "5.6.7.8"
    assert res["new_ip"] == "9.9.9.9"
    assert len(sent) == 1


def test_rotation_successful_portal_update_resumes_fetching(tmp_path, monkeypatch):
    _write_state(tmp_path, baseline_ip="1.2.3.4")
    monkeypatch.setattr("src.utils.ip_monitor._fetch_public_ip", lambda **kw: "5.6.7.8")
    monkeypatch.setattr(
        "src.fetchers.shoonya_ip_updater.update_shoonya_portal_ip",
        lambda **kw: (True, "success"),
    )
    sent = []
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda msg: sent.append(msg))

    res = guard.run_daily_ip_check()

    assert res["skip"] is False
    assert res["reason"] == "ip_auto_updated"
    assert res["old_ip"] == "1.2.3.4"
    assert res["new_ip"] == "5.6.7.8"
    assert len(sent) == 1
    assert "Shoonya IP Auto-Updated" in sent[0]
    state = json.loads((tmp_path / "shoonya_ip_state.json").read_text())
    assert state["baseline_ip"] == "5.6.7.8"
    assert state["skip_date"] is None
