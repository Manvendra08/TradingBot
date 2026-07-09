"""
Tests for OPS Agent v2.0 — playbook state machine, health reading, actions.
Run: pytest tests/test_ops_agent.py -v
"""

import json
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# ── Fixtures ─────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary bot DB with full production schema.

    Uses init_db() from src.models.schema so the schema matches the real
    database. This is required because dashboard_server.py patches
    sqlite3.connect globally at import time with a PatchedConnection that
    rewrites SQL to reference 40+ columns — the test fixture must have
    those columns or the rewrite raises OperationalError.
    """
    db_path = tmp_path / "nsebot.db"
    from src.models.schema import DB_PATH as _REAL_DB_PATH
    from src.models.schema import init_db

    with patch("src.models.schema.DB_PATH", str(db_path)):
        init_db()
    return db_path


@pytest.fixture
def agent_db(tmp_path):
    """Create a temporary agent incidents DB."""
    db_path = tmp_path / "ops_agent.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            playbook_id TEXT NOT NULL,
            trigger_state TEXT,
            action TEXT,
            result TEXT,
            acked INTEGER DEFAULT 0
        );
    """)
    conn.close()
    return db_path


def _stamp_health(db_path, key, status, detail="", minutes_ago=0):
    """Stamp a health row in the given DB."""
    now = datetime.now(IST) - timedelta(minutes=minutes_ago)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO health_state (key, status, detail, updated_at) VALUES (?,?,?,?)",
        (key, status, detail, now.isoformat()),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def suppress_ops_escalation(request, monkeypatch):
    """Prevent tests from sending real Telegram messages via Ops Agent."""
    if request.cls and request.cls.__name__ == "TestEscalation":
        return
    monkeypatch.setattr("ops_agent._send_telegram", lambda text, fallback=False: True)
    monkeypatch.setattr("ops_agent._escalate", lambda p, m, critical=False: None)


# ── State Machine Tests ──────────────────────────────────────────────────────


class TestStateMachine:
    def test_ok_to_ok(self):
        from ops_agent import StateMachine

        sm = StateMachine()
        row = {"status": "OK", "detail": "test"}
        result = sm.evaluate("scheduler_loop", row)
        assert result == "OK"
        assert sm.components["scheduler_loop"].consecutive_down == 0

    def test_ok_to_degraded_to_down(self):
        from ops_agent import StateMachine

        sm = StateMachine()
        # First bad read → DEGRADED
        sm.evaluate("shoonya_session", {"status": "DOWN", "detail": "fail"})
        assert sm.components["shoonya_session"].status == "DEGRADED"
        assert sm.components["shoonya_session"].consecutive_down == 1
        # Second bad read → DOWN
        sm.evaluate("shoonya_session", {"status": "DOWN", "detail": "fail"})
        assert sm.components["shoonya_session"].status == "DOWN"
        assert sm.components["shoonya_session"].consecutive_down == 2

    def test_degraded_resets_on_ok(self):
        from ops_agent import StateMachine

        sm = StateMachine()
        sm.evaluate("scheduler_loop", {"status": "DOWN", "detail": ""})
        assert sm.components["scheduler_loop"].status == "DEGRADED"
        sm.evaluate("scheduler_loop", {"status": "OK", "detail": ""})
        assert sm.components["scheduler_loop"].status == "OK"
        assert sm.components["scheduler_loop"].consecutive_down == 0

    def test_flapping_guard(self):
        """OK/DOWN alternating reads → no action until 2 consecutive DOWN."""
        from ops_agent import StateMachine

        sm = StateMachine()
        sm.evaluate_stale(
            "shoonya_session",
            {"status": "OK", "updated_at": datetime.now(IST).isoformat()},
            10,
        )
        assert sm.components["shoonya_session"].status == "OK"
        # Stale read
        old_time = (datetime.now(IST) - timedelta(minutes=15)).isoformat()
        sm.evaluate_stale(
            "shoonya_session", {"status": "OK", "updated_at": old_time}, 10
        )
        assert sm.components["shoonya_session"].status == "DEGRADED"
        # Back to OK
        sm.evaluate_stale(
            "shoonya_session",
            {"status": "OK", "updated_at": datetime.now(IST).isoformat()},
            10,
        )
        assert sm.components["shoonya_session"].status == "OK"
        assert sm.components["shoonya_session"].consecutive_down == 0

    def test_staleness_evaluation(self):
        from ops_agent import StateMachine

        sm = StateMachine()
        # Fresh → OK
        fresh = {"status": "OK", "updated_at": datetime.now(IST).isoformat()}
        sm.evaluate_stale("scheduler_loop", fresh, stale_min=3)
        assert sm.components["scheduler_loop"].status == "OK"
        # Stale → DEGRADED then DOWN
        stale = {
            "status": "OK",
            "updated_at": (datetime.now(IST) - timedelta(minutes=5)).isoformat(),
        }
        sm.evaluate_stale("scheduler_loop", stale, stale_min=3)
        assert sm.components["scheduler_loop"].status == "DEGRADED"
        sm.evaluate_stale("scheduler_loop", stale, stale_min=3)
        assert sm.components["scheduler_loop"].status == "DOWN"

    def test_none_health_row(self):
        from ops_agent import StateMachine

        sm = StateMachine()
        result = sm.evaluate("scheduler_loop", None)
        assert result == "DOWN"


# ── Health Reading Tests ─────────────────────────────────────────────────────


class TestHealthReading:
    def test_heartbeat_age(self, tmp_path):
        from ops_agent import HealthSnapshot, _read_heartbeat_age

        # No heartbeat file
        with patch("ops_agent.HEARTBEAT_PATH", tmp_path / "missing"):
            age = _read_heartbeat_age()
            assert age is None
        # Fresh heartbeat
        hb_file = tmp_path / "nsebot.heartbeat"
        hb_file.write_text(str(int(time.time())))
        with patch("ops_agent.HEARTBEAT_PATH", hb_file):
            age = _read_heartbeat_age()
            assert age is not None
            assert age < 1.0
        # Stale heartbeat
        hb_file.write_text(str(int(time.time()) - 300))
        import os

        stale_time = time.time() - 300
        os.utime(str(hb_file), (stale_time, stale_time))
        with patch("ops_agent.HEARTBEAT_PATH", hb_file):
            age = _read_heartbeat_age()
            assert age is not None
            assert age > 200

    def test_read_health_via_sqlite(self, tmp_db):
        from ops_agent import _read_health_via_sqlite

        _stamp_health(tmp_db, "scheduler_loop", "OK", "test")
        with patch("ops_agent.BOT_DB_PATH", str(tmp_db)):
            rows = _read_health_via_sqlite()
            assert len(rows) == 1
            assert rows[0]["key"] == "scheduler_loop"
            assert rows[0]["status"] == "OK"

    def test_read_open_positions_count(self, tmp_db):
        from ops_agent import _read_open_positions_count

        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO paper_trades (opened_at, status, symbol, option_type, entry_underlying) VALUES (?, 'OPEN', 'NIFTY', 'CE', 0.0)",
            (datetime.now(IST).isoformat(),),
        )
        conn.execute(
            "INSERT INTO live_trades (opened_at, status, symbol, option_type, entry_underlying) VALUES (?, 'OPEN', 'BANKNIFTY', 'CE', 0.0)",
            (datetime.now(IST).isoformat(),),
        )
        conn.commit()
        conn.close()
        with patch("ops_agent.BOT_DB_PATH", str(tmp_db)):
            count = _read_open_positions_count()
            assert count == 2


# ── Playbook Tests ───────────────────────────────────────────────────────────


class TestPlaybooks:
    def test_p01_restart_when_dead_no_positions(self):
        """P01: Bot dead + no open positions → restart issued."""
        from ops_agent import ROLLOUT_LEVEL, HealthSnapshot, StateMachine, run_playbooks

        snap = HealthSnapshot(
            heartbeat_age_s=300,
            heartbeat_ok=False,
            open_positions=0,
            read_source="sqlite",
        )
        sm = StateMachine()
        results = run_playbooks(snap, sm)
        # Should attempt restart (P01)
        assert any(r.action_taken == "restart_nsebot" for r in results)

    def test_p02_crash_loop_no_restart(self):
        """P01 bound exceeded → P02 state, no third restart."""
        from ops_agent import HealthSnapshot, StateMachine, run_playbooks

        snap = HealthSnapshot(
            heartbeat_age_s=300,
            heartbeat_ok=False,
            open_positions=0,
            read_source="sqlite",
        )
        sm = StateMachine()
        # Simulate 2 prior restarts
        sm.components["_restart_count"] = type(
            "C", (), {"consecutive_down": 2, "detail": ""}
        )()
        results = run_playbooks(snap, sm)
        # Should NOT restart, should escalate P02
        assert not any(r.action_taken == "restart_nsebot" for r in results)
        assert any(r.action_taken == "P02_crash_loop" for r in results)

    def test_p02_open_position_emergency_flat(self):
        """P02: Bot dead + open positions → emergency flat."""
        from ops_agent import HealthSnapshot, StateMachine, run_playbooks

        snap = HealthSnapshot(
            heartbeat_age_s=300,
            heartbeat_ok=False,
            open_positions=1,
            read_source="sqlite",
        )
        sm = StateMachine()
        with patch("ops_agent._run_emergency_flat", return_value=True):
            results = run_playbooks(snap, sm)
        assert any(r.action_taken == "emergency_flat" for r in results)

    def test_p03_reauth_on_auth_fail(self):
        """P03: Shoonya auth-fail → re-auth hook called."""
        from ops_agent import (
            ComponentState,
            HealthSnapshot,
            StateMachine,
            run_playbooks,
        )

        snap = HealthSnapshot(
            heartbeat_ok=True,
            heartbeat_age_s=5,
            open_positions=0,
            read_source="dashboard",
        )
        sm = StateMachine()
        sm.components["shoonya_session"] = ComponentState(
            status="DOWN",
            consecutive_down=2,
            detail="auth-fail: 401 Invalid Token",
        )
        with patch("ops_agent._reauth_shoonya", return_value=True):
            results = run_playbooks(snap, sm)
        assert any(r.action_taken == "reauth_shoonya" for r in results)

    def test_p04_trading_paused_on_reauth_failure(self):
        """P04: Re-auth failed ×3 → trading_paused=true."""
        from ops_agent import (
            ComponentState,
            HealthSnapshot,
            StateMachine,
            run_playbooks,
        )

        snap = HealthSnapshot(
            heartbeat_ok=True,
            heartbeat_age_s=5,
            open_positions=0,
            read_source="dashboard",
        )
        sm = StateMachine()
        sm.components["shoonya_session"] = ComponentState(
            status="DOWN",
            consecutive_down=2,
            detail="auth-fail: 401 Invalid Token",
        )
        sm.components["_reauth_count"] = ComponentState(consecutive_down=3)
        with patch("ops_agent._set_trading_paused", return_value=True):
            results = run_playbooks(snap, sm)
        assert any(r.action_taken == "P04_trading_paused" for r in results)

    def test_p04_broker_down_pause(self):
        """P04: Broker down (502/Timeout) → pause trading."""
        from ops_agent import (
            ComponentState,
            HealthSnapshot,
            StateMachine,
            run_playbooks,
        )

        snap = HealthSnapshot(
            heartbeat_ok=True,
            heartbeat_age_s=5,
            open_positions=0,
            read_source="dashboard",
        )
        sm = StateMachine()
        sm.components["shoonya_session"] = ComponentState(
            status="DOWN",
            consecutive_down=2,
            detail="502 Bad Gateway timeout",
        )
        with patch("ops_agent._set_trading_paused", return_value=True):
            results = run_playbooks(snap, sm)
        assert any(r.action_taken == "P04_broker_down" for r in results)

    def test_p12_unknown_state(self):
        """P12: Unknown component state → notification only."""
        from ops_agent import (
            ComponentState,
            HealthSnapshot,
            StateMachine,
            run_playbooks,
        )

        snap = HealthSnapshot(
            heartbeat_ok=True,
            heartbeat_age_s=5,
            open_positions=0,
            read_source="dashboard",
        )
        sm = StateMachine()
        sm.components["scheduler_loop"] = ComponentState(status="UNKNOWN")
        with patch("ops_agent._escalate"):
            results = run_playbooks(snap, sm)
        assert any(r.action_taken == "P12_unknown" for r in results)

    def test_observe_only_no_actions(self):
        """In observe-only mode, no T1/T2 actions are taken."""
        import ops_agent
        from ops_agent import HealthSnapshot, StateMachine, run_playbooks

        snap = HealthSnapshot(
            heartbeat_age_s=300,
            heartbeat_ok=False,
            open_positions=0,
            read_source="sqlite",
        )
        sm = StateMachine()
        original_level = ops_agent.ROLLOUT_LEVEL
        try:
            ops_agent.ROLLOUT_LEVEL = 0
            results = run_playbooks(snap, sm)
            # No actions in observe-only mode
            assert not any(r.action_taken for r in results)
        finally:
            ops_agent.ROLLOUT_LEVEL = original_level


# ── Incident Logging Tests ───────────────────────────────────────────────────


class TestIncidents:
    def test_log_incident(self, agent_db):
        from ops_agent import _get_incidents_conn

        # Patch the incidents DB path
        with patch("ops_agent.AGENT_DB", agent_db):
            from ops_agent import _log_incident

            iid = _log_incident("P01", "heartbeat stale", "restart_nsebot", "success")
            assert iid is not None
            # Verify it was written
            conn = sqlite3.connect(str(agent_db))
            row = conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
            conn.close()
            assert row is not None
            assert row[2] == "P01"  # playbook_id


# ── Telegram Escalation Tests ────────────────────────────────────────────────


class TestEscalation:
    def test_send_telegram_no_token(self):
        from ops_agent import _send_telegram

        with (
            patch("ops_agent.TELEGRAM_BOT_TOKEN", ""),
            patch("ops_agent.TELEGRAM_CHAT_ID", ""),
        ):
            result = _send_telegram("test")
            assert result is False

    def test_send_telegram_success(self):
        from ops_agent import _send_telegram

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with (
            patch("ops_agent.TELEGRAM_BOT_TOKEN", "test_token"),
            patch("ops_agent.TELEGRAM_CHAT_ID", "123"),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = _send_telegram("test message")
            assert result is True


# ── Set Trading Paused Tests ─────────────────────────────────────────────────


class TestTradingPaused:
    def test_set_trading_paused(self, tmp_path):
        from ops_agent import _set_trading_paused

        config_path = tmp_path / "runtime_config.json"
        config_path.write_text(json.dumps({"trading_paused": False}))
        with patch("ops_agent.DATA_DIR", tmp_path):
            result = _set_trading_paused()
            assert result is True
            config = json.loads(config_path.read_text())
            assert config["trading_paused"] is True

    def test_set_trading_paused_no_file(self, tmp_path):
        from ops_agent import _set_trading_paused

        with patch("ops_agent.DATA_DIR", tmp_path):
            result = _set_trading_paused()
            assert result is True
            config = json.loads((tmp_path / "runtime_config.json").read_text())
            assert config["trading_paused"] is True


# ── Schema Health Stamp Tests ────────────────────────────────────────────────


class TestSchemaHealthStamps:
    def test_stamp_health(self, tmp_db):
        from src.models.schema import stamp_health

        with patch("src.models.schema.DB_PATH", str(tmp_db)):
            stamp_health("scheduler_loop", "OK", "test detail")
            # Verify
            conn = sqlite3.connect(str(tmp_db))
            row = conn.execute(
                "SELECT * FROM health_state WHERE key='scheduler_loop'"
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[1] == "OK"
            assert row[2] == "test detail"

    def test_stamp_health_upsert(self, tmp_db):
        from src.models.schema import stamp_health

        with patch("src.models.schema.DB_PATH", str(tmp_db)):
            stamp_health("scheduler_loop", "OK", "first")
            stamp_health("scheduler_loop", "DOWN", "second")
            conn = sqlite3.connect(str(tmp_db))
            rows = conn.execute(
                "SELECT * FROM health_state WHERE key='scheduler_loop'"
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][1] == "DOWN"

    def test_read_health_state(self, tmp_db):
        from src.models.schema import read_health_state, stamp_health

        with patch("src.models.schema.DB_PATH", str(tmp_db)):
            stamp_health("scheduler_loop", "OK")
            stamp_health("shoonya_session", "DOWN")
            rows = read_health_state()
            assert len(rows) == 2
            keys = {r["key"] for r in rows}
            assert keys == {"scheduler_loop", "shoonya_session"}

    def test_get_open_positions_count(self, tmp_db):
        from src.models.schema import get_open_positions_count

        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO paper_trades (opened_at, status, symbol, option_type, entry_underlying) VALUES (?, 'OPEN', 'NIFTY', 'CE', 0.0)",
            (datetime.now(IST).isoformat(),),
        )
        conn.commit()
        conn.close()
        with patch("src.models.schema.DB_PATH", str(tmp_db)):
            count = get_open_positions_count()
            assert count == 1

    def test_stamp_open_positions(self, tmp_db):
        from src.models.schema import stamp_open_positions

        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO paper_trades (opened_at, status, symbol, option_type, entry_underlying) VALUES (?, 'OPEN', 'NIFTY', 'CE', 0.0)",
            (datetime.now(IST).isoformat(),),
        )
        conn.commit()
        conn.close()
        with patch("src.models.schema.DB_PATH", str(tmp_db)):
            stamp_open_positions()
            conn = sqlite3.connect(str(tmp_db))
            row = conn.execute(
                "SELECT * FROM health_state WHERE key='open_positions'"
            ).fetchone()
            conn.close()
            assert row is not None
            assert "count=1" in row[2]
