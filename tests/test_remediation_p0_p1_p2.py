"""
Regression tests for prioritized remediation changes.

Covers:
- P0: update_broker_config column whitelist
- P0: Zerodha callback client_id validation
- P1: live-monitor ADJUST risk-limit gate
- P2: OMNIROUTER_API_KEY missing-key failure
- P2: LLM budget hard stop in call_llm_api
- P3: dashboard auth startup warning on non-loopback bind
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from dashboard_server import app
from src.models.schema import DB_PATH, get_conn, init_db, update_broker_config
from src.engine import llm_enrichment as llm_mod
from src.engine.llm_enrichment import (
    LLMTradeVerdict,
    call_llm_api,
    get_cost_tracker,
    record_call_cost,
)
from src.engine.multileg_live_trading import _monitor_open_books_live

_REMEDIATION_DB = Path(__file__).with_suffix(".py").parent / ".tmp" / "remediation.db"


def _init_remediation_db(monkeypatch):
    _REMEDIATION_DB.parent.mkdir(parents=True, exist_ok=True)
    if _REMEDIATION_DB.exists():
        _REMEDIATION_DB.unlink()
    monkeypatch.setattr("src.models.schema.DB_PATH", _REMEDIATION_DB)
    monkeypatch.setattr("config.settings.DB_PATH", _REMEDIATION_DB)
    init_db()


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    _init_remediation_db(monkeypatch)
    yield _REMEDIATION_DB
    import gc
    gc.collect()
    for _ in range(5):
        try:
            _REMEDIATION_DB.unlink()
            _REMEDIATION_DB.with_suffix(".db-wal").unlink()
            _REMEDIATION_DB.with_suffix(".db-shm").unlink()
            break
        except FileNotFoundError:
            break
        except PermissionError:
            import time
            time.sleep(0.05)


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_alert", lambda alert: False)
    monkeypatch.setattr("src.alerts.telegram_dispatcher.send_text", lambda text: False)
    monkeypatch.setattr(
        "src.alerts.discord_dispatcher.send_to_discord",
        lambda text, timeout_seconds=10: False,
    )


@pytest.fixture(autouse=True)
def _reset_llm_state():
    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()
    llm_mod._GLOBAL_COST_TRACKER = llm_mod.CostTracker(budget_limit=10.0)
    yield
    llm_mod._CONSECUTIVE_FAILURES = 0
    llm_mod._CIRCUIT_OPEN_UNTIL = 0.0
    llm_mod._API_QUOTA_EXHAUSTED_UNTIL = 0.0
    llm_mod._PROVIDER_COOLDOWN_UNTIL.clear()
    llm_mod._GLOBAL_COST_TRACKER = llm_mod.CostTracker(budget_limit=10.0)


# ---------------------------------------------------------------------------
# P0: update_broker_config column whitelist
# ---------------------------------------------------------------------------


class TestUpdateBrokerConfigWhitelist:
    def test_allowed_columns_are_saved(self):
        update_broker_config(api_key="k", api_secret="s", access_token="t")
        with get_conn() as conn:
            row = conn.execute("SELECT api_key, api_secret, access_token FROM broker_configs ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row["api_key"] == "k"
        assert row["api_secret"] != "s"
        from src.services.zerodha_auth import decrypt_secret
        assert decrypt_secret(row["access_token"]) == "t"

    def test_unknown_columns_are_ignored(self):
        update_broker_config(api_key="k", evil_column="drop")
        with get_conn() as conn:
            row = conn.execute("PRAGMA table_info(broker_configs)").fetchall()
        columns = [r["name"] for r in row]
        assert "evil_column" not in columns

    def test_empty_kwargs_does_not_insert_or_update(self):
        update_broker_config()
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM broker_configs").fetchone()
        assert int(row["c"]) == 0


# ---------------------------------------------------------------------------
# P0: Zerodha callback client_id validation
# ---------------------------------------------------------------------------


class TestZerodhaCallbackValidation:
    def test_missing_request_token_returns_400(self):
        client = TestClient(app)
        resp = client.get("/api/zerodha/callback")
        assert resp.status_code == 400

    def test_mismatched_client_id_returns_400(self):
        client = TestClient(app)
        with patch("src.models.schema.get_broker_config", return_value={"api_key": "stored_key", "api_secret": "s"}):
            resp = client.get("/api/zerodha/callback?client_id=other_key&request_token=rt")
        assert resp.status_code == 400
        assert "Invalid client_id" in resp.text

    def test_valid_client_id_passes_initial_guard(self):
        fake_kite = MagicMock()
        fake_kite.login_url.return_value = "https://example.com/login"
        fake_kite.generate_session.return_value = MagicMock(access_token="at", public_token="pt")
        fake_module = MagicMock()
        fake_module.KiteConnect.return_value = fake_kite
        with patch.dict("sys.modules", {"kiteconnect": fake_module}), \
             patch("src.models.schema.get_broker_config", return_value={"api_key": "stored_key", "api_secret": "s"}):
            client = TestClient(app)
            resp = client.get("/api/zerodha/callback?client_id=stored_key&request_token=rt")
        assert resp.status_code != 400 or "Invalid client_id" not in resp.text


# ---------------------------------------------------------------------------
# P2: OMNIROUTER_API_KEY missing-key failure
# ---------------------------------------------------------------------------


class TestNewsWorkerApiKeyRequirement:
    def test_missing_omnirouter_api_key_raises(self):
        from src.services.news_worker import _evaluate_sentiment_via_omnirouter

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="OMNIROUTER_API_KEY is not configured"):
                _evaluate_sentiment_via_omnirouter("NIFTY", [{"title": "Headline 1"}], 0.0)


# ---------------------------------------------------------------------------
# P2: LLM budget hard stop
# ---------------------------------------------------------------------------


class TestLLMBudgetHardStop:
    def test_call_llm_api_returns_none_when_over_budget(self):
        llm_mod._GLOBAL_COST_TRACKER = llm_mod.CostTracker(
            budget_limit=0.0,
            records=(llm_mod.CostRecord(model="gpt-4o", input_tokens=1000, output_tokens=1000, cost_usd=0.0125),),
        )
        assert get_cost_tracker().over_budget is True

        with patch("config.settings.DISABLE_LLM_ENRICHMENT", False):
            result = call_llm_api("NIFTY", "prompt", LLMTradeVerdict, deadline=None, purpose="live_verdict")
        assert result is None


# ---------------------------------------------------------------------------
# P1: live-monitor ADJUST risk-limit gate
# ---------------------------------------------------------------------------


class TestLiveMonitorAdjustRiskGate:
    def test_adjust_blocked_when_risk_limits_fail(self):
        book = {
            "book_id": "BOOK-ADJUST-1",
            "id": 1,
            "symbol": "NIFTY",
            "strategy_type": "SHORT_STRANGLE",
            "net_premium": 100.0,
            "total_pnl": -5000.0,
            "profit_target_pct": 0.5,
            "stop_loss_pct": 1.0,
            "time_decay_exit_dte": 2,
            "adjustment_count": 0,
            "entry_underlying": 24500.0,
            "expiry": "2126-08-14",
            "legs": [
                {
                    "id": 10,
                    "strike": 24600.0,
                    "option_type": "PE",
                    "side": "SELL",
                    "lots": 1,
                    "entry_premium": 55.0,
                    "current_premium": 20.0,
                    "delta": 0.25,
                }
            ],
        }
        scan_context = {"underlying": 24500.0, "option_rows": []}

        with patch("config.runtime_config.load_runtime_config", return_value={"live_ai_exit_advisor_enabled": True}), \
             patch("src.engine.llm_enrichment.get_multileg_exit_advice", return_value={
                 "action": "ADJUST",
                 "reasoning": "risk limit test",
                 "adjustment": {"close_strike": 24600.0, "close_option_type": "PE", "new_strike": 24700.0, "new_option_type": "PE", "new_side": "SELL", "rationale": "roll"},
             }), \
             patch("src.models.schema.increment_adjustment_count") as mock_inc, \
             patch("src.engine.risk_engine.check_live_risk_limits", return_value=(False, "delta cap exceeded")):
            result = _monitor_open_books_live("NIFTY", scan_context, "digest", {}, [book], "full", "2026-01-01T00:00:00+00:00")
            mock_inc.assert_not_called()
            assert result["action"] == "HOLD"


# ---------------------------------------------------------------------------
# P3: dashboard auth startup warning on non-loopback bind
# ---------------------------------------------------------------------------


class TestDashboardAuthStartupWarning:
    def test_lifespan_warns_when_auth_disabled_on_non_loopback(self, caplog):
        with patch("config.runtime_config.load_runtime_config", return_value={"dashboard_auth_enabled": False}), \
             patch.dict(os.environ, {"HOST": "0.0.0.0"}):
            with caplog.at_level(logging.WARNING, logger="nsebot.dashboard"):
                with TestClient(app) as client:
                    client.get("/health")
        assert any("dashboard_auth_enabled=false" in m for m in caplog.messages)
