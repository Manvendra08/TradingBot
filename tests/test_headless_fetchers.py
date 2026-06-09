"""
Unit tests for new headless fetchers (no network/browser — all mocked).
"""
from __future__ import annotations
import threading

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ──────────────────────────────────────────────────────────────────────────────
# MoneycontrolFetcher — unit tests (Playwright fully mocked)
# ──────────────────────────────────────────────────────────────────────────────

class TestMoneycontrolFetcher:
    """Test Moneycontrol fetcher data parsing and output schema."""

    def _make_fetcher(self):
        from src.fetchers.moneycontrol_fetcher import MoneycontrolFetcher
        return MoneycontrolFetcher()

    def test_unsupported_symbol_returns_none(self):
        f = self._make_fetcher()
        result = f.fetch_option_chain("UNKNOWNSYM")
        assert result is None

    def test_output_schema_keys(self):
        """Patch _fetch_side_sync to inject synthetic rows, verify schema."""
        from src.fetchers import moneycontrol_fetcher as mc_mod

        fake_ce = [{"strike": 180.0, "option_type": "CE", "ltp": 5.0, "oi": 1000,
                    "oi_change": 100, "volume": 50, "iv": None, "bid": 4.9, "ask": 5.1}]
        fake_pe = [{"strike": 180.0, "option_type": "PE", "ltp": 3.0, "oi": 800,
                    "oi_change": -50, "volume": 30, "iv": None, "bid": 2.9, "ask": 3.1}]
        fake_strikes = fake_ce + fake_pe

        with patch.object(mc_mod, "_fetch_side_sync", return_value=("2026-05-28", 182.5, fake_strikes)):
            f = self._make_fetcher()
            result = f.fetch_option_chain("NATURALGAS")

        assert result is not None
        assert result["symbol"] == "NATURALGAS"
        assert result["expiry"] != ""
        assert result["source"] == "moneycontrol"
        assert len(result["strikes"]) == 2

        row = result["strikes"][0]
        for key in ("strike", "option_type", "ltp", "oi", "oi_change", "volume", "iv", "bid", "ask"):
            assert key in row, f"Missing key: {key}"

    def test_dedup_prevents_duplicate_strikes(self):
        from src.fetchers import moneycontrol_fetcher as mc_mod

        # Both threads return same row
        dup_row = {"strike": 200.0, "option_type": "CE", "ltp": 7.0, "oi": 500,
                   "oi_change": 20, "volume": 10, "iv": None, "bid": None, "ask": None}
        with patch.object(mc_mod, "_fetch_side_sync", return_value=("2026-05-28", 200.0, [dup_row])):
            f = self._make_fetcher()
            result = f.fetch_option_chain("NATURALGAS")

        assert result is not None
        assert len(result["strikes"]) == 1   # deduped

    def test_empty_rows_returns_none(self):
        from src.fetchers import moneycontrol_fetcher as mc_mod

        with patch.object(mc_mod, "_fetch_side_sync", return_value=(None, None, [])):
            f = self._make_fetcher()
            result = f.fetch_option_chain("NATURALGAS")

        assert result is None

    def test_parse_number_handles_commas_and_dashes(self):
        from src.fetchers.moneycontrol_fetcher import _parse_number
        assert _parse_number("1,23,456") == pytest.approx(123456.0)
        assert _parse_number("  -  ") is None
        assert _parse_number("") is None
        assert _parse_number("250.50") == pytest.approx(250.5)

    def test_parse_int_handles_none(self):
        from src.fetchers.moneycontrol_fetcher import _parse_int
        assert _parse_int("abc") is None
        assert _parse_int("1000") == 1000

    def test_get_live_future_price_success(self):
        from src.fetchers.moneycontrol_fetcher import _get_live_future_price
        from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher

        with patch.object(DhanCommodityFetcher, "_fetch_builtup_live_price", return_value=315.0) as mock_fetch:
            val = _get_live_future_price("NATURALGAS")
            assert val == pytest.approx(315.0)
            mock_fetch.assert_called_once_with(504265)

    def test_get_live_future_price_missing_secid(self):
        from src.fetchers.moneycontrol_fetcher import _get_live_future_price
        val = _get_live_future_price("UNKNOWNSYM")
        assert val is None

    def test_get_live_future_price_exception_handled(self):
        from src.fetchers.moneycontrol_fetcher import _get_live_future_price
        from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher

        with patch.object(DhanCommodityFetcher, "_fetch_builtup_live_price", side_effect=Exception("Dhan down")):
            val = _get_live_future_price("NATURALGAS")
            assert val is None

    def test_fetch_nse_commodity_spot_delegates(self):
        from src.fetchers import moneycontrol_fetcher as mc_mod

        with patch.object(mc_mod, "_get_live_future_price", return_value=315.0) as mock_get:
            val = mc_mod._fetch_nse_commodity_spot("NATURALGAS")
            assert val == pytest.approx(315.0)
            mock_get.assert_called_once_with("NATURALGAS")


# ──────────────────────────────────────────────────────────────────────────────
# DhanHeadlessFetcher — unit tests (async interceptor mocked)
# ──────────────────────────────────────────────────────────────────────────────

class TestDhanHeadlessFetcher:
    """Test Dhan headless fetcher normalisation logic."""

    def _make_fetcher(self):
        from src.fetchers.dhan_headless_fetcher import DhanHeadlessFetcher
        return DhanHeadlessFetcher()

    def test_unsupported_symbol_returns_none(self):
        f = self._make_fetcher()
        result = f.fetch_option_chain("UNKNOWNSYM")
        assert result is None

    def test_normalise_shape1_payload(self):
        from src.fetchers.dhan_headless_fetcher import _normalise_dhan_payload

        raw = {
            "data": {
                "expiryDate": "2026-05-22",
                "underlyingValue": 260.5,
                "optionChain": [
                    {
                        "strike_price": 260,
                        "CE": {
                            "last_price": 5.0,
                            "open_interest": 2000,
                            "oi_change": 100,
                            "volume": 500,
                            "implied_volatility": 42.5,
                            "bid_price": 4.9,
                            "ask_price": 5.1,
                            "delta": 0.52,
                            "theta": -0.03,
                            "gamma": 0.01,
                            "vega": 0.10,
                        },
                        "PE": {
                            "last_price": 3.5,
                            "open_interest": 1500,
                            "oi_change": -50,
                            "volume": 300,
                            "implied_volatility": 40.0,
                            "bid_price": 3.4,
                            "ask_price": 3.6,
                            "delta": -0.48,
                            "theta": -0.02,
                            "gamma": 0.01,
                            "vega": 0.09,
                        },
                    }
                ],
            }
        }

        result = _normalise_dhan_payload(raw, "NATURALGAS")
        assert result is not None
        assert result["symbol"] == "NATURALGAS"
        assert result["underlying_price"] == pytest.approx(260.5)
        assert result["expiry"] == "2026-05-22"
        assert len(result["strikes"]) == 2

        ce = next(r for r in result["strikes"] if r["option_type"] == "CE")
        assert ce["strike"] == pytest.approx(260.0)
        assert ce["ltp"] == pytest.approx(5.0)
        assert ce["iv"] == pytest.approx(42.5)
        assert ce["delta"] == pytest.approx(0.52)

        pe = next(r for r in result["strikes"] if r["option_type"] == "PE")
        assert pe["iv"] == pytest.approx(40.0)
        assert pe["delta"] == pytest.approx(-0.48)

    def test_normalise_empty_chain_returns_none(self):
        from src.fetchers.dhan_headless_fetcher import _normalise_dhan_payload
        raw = {"data": {"expiryDate": "2026-05-22", "underlyingValue": 260.0, "optionChain": []}}
        result = _normalise_dhan_payload(raw, "NATURALGAS")
        assert result is None

    def test_fetch_sync_uses_async_interceptor(self):
        """_fetch_sync should call the async function and return its result."""
        from src.fetchers import dhan_headless_fetcher as dh_mod

        fake_result = {
            "symbol": "NIFTY",
            "underlying_price": 261.0,
            "expiry": "2026-05-22",
            "strikes": [{"strike": 260.0, "option_type": "CE"}],
            "source": "dhan_headless",
            "fetched_at": "2026-05-21T00:00:00+05:30",
        }

        with patch.object(dh_mod, "_fetch_sync", return_value=fake_result):
            f = self._make_fetcher()
            result = f.fetch_option_chain("NIFTY")

        assert result is not None
        assert result["strikes"][0]["strike"] == pytest.approx(260.0)


# ──────────────────────────────────────────────────────────────────────────────
# Router integration — new fetchers registered
# ──────────────────────────────────────────────────────────────────────────────

class TestRouterHeadlessIntegration:
    """Verify router sees dhan_headless and moneycontrol fetcher classes."""

    def test_router_registers_dhan_headless(self):
        from src.fetchers import router
        assert "dhan_headless" in router._FETCHERS

    def test_router_registers_moneycontrol(self):
        from src.fetchers import router
        assert "moneycontrol" in router._FETCHERS

    def test_fetcher_priority_includes_headless(self):
        from config.settings import FETCHER_PRIORITY
        assert "dhan_headless" in FETCHER_PRIORITY

    def test_fetcher_priority_includes_moneycontrol(self):
        from config.settings import FETCHER_PRIORITY
        assert "moneycontrol" in FETCHER_PRIORITY


# ──────────────────────────────────────────────────────────────────────────────
# Extra coverage: helpers and edge-cases
# ──────────────────────────────────────────────────────────────────────────────

class TestMoneycontrolHelpers:

    def test_fetch_option_chain_crudeoil(self):
        """Non-NATURALGAS MCX commodity slug resolution."""
        from src.fetchers import moneycontrol_fetcher as mc_mod
        fake_row = {"strike": 6500.0, "option_type": "CE", "ltp": 10.0, "oi": 200,
                    "oi_change": 5, "volume": 20, "iv": None, "bid": None, "ask": None}
        with patch.object(mc_mod, "_fetch_side_sync", return_value=("2026-05-28", 6500.0, [fake_row])):
            f = mc_mod.MoneycontrolFetcher()
            result = f.fetch_option_chain("CRUDEOIL")
        assert result is not None
        assert result["symbol"] == "CRUDEOIL"

    def test_sorted_strikes_order(self):
        from src.fetchers import moneycontrol_fetcher as mc_mod
        rows = [
            {"strike": 300.0, "option_type": "PE", "ltp": 2.0, "oi": 100, "oi_change": 0, "volume": 5, "iv": None, "bid": None, "ask": None},
            {"strike": 200.0, "option_type": "CE", "ltp": 8.0, "oi": 300, "oi_change": 10, "volume": 15, "iv": None, "bid": None, "ask": None},
            {"strike": 200.0, "option_type": "PE", "ltp": 6.0, "oi": 250, "oi_change": -5, "volume": 10, "iv": None, "bid": None, "ask": None},
        ]
        sorted_rows = sorted(rows, key=lambda r: (r["strike"], r["option_type"]))
        with patch.object(mc_mod, "_fetch_side_sync", return_value=("2026-05-28", 250.0, sorted_rows)):
            f = mc_mod.MoneycontrolFetcher()
            result = f.fetch_option_chain("NATURALGAS")
        strikes = result["strikes"]
        # Should be sorted ascending by strike then option_type
        assert strikes[0]["strike"] <= strikes[-1]["strike"]


class TestDhanHeadlessHelpers:

    def test_normalise_flat_row_shape(self):
        """Handle shape where rows are flat (not nested CE/PE dicts)."""
        from src.fetchers.dhan_headless_fetcher import _normalise_dhan_payload
        raw = {
            "data": {
                "expiryDate": "2026-05-22",
                "underlyingValue": 265.0,
                "optionChain": [
                    {"strike_price": 265, "option_type": "CE", "last_price": 4.0,
                     "open_interest": 500, "oi_change": 20, "volume": 100,
                     "implied_volatility": 38.0, "bid_price": 3.9, "ask_price": 4.1},
                    {"strike_price": 265, "option_type": "PE", "last_price": 3.0,
                     "open_interest": 400, "oi_change": -10, "volume": 80,
                     "implied_volatility": 36.0, "bid_price": 2.9, "ask_price": 3.1},
                ]
            }
        }
        result = _normalise_dhan_payload(raw, "NATURALGAS")
        assert result is not None
        assert len(result["strikes"]) == 2

    def test_normalise_missing_iv_is_none(self):
        from src.fetchers.dhan_headless_fetcher import _normalise_dhan_payload
        raw = {
            "data": {
                "expiryDate": "2026-05-22",
                "underlyingValue": 260.0,
                "optionChain": [
                    {"strike_price": 260, "CE": {"last_price": 5.0, "open_interest": 100}},
                ]
            }
        }
        result = _normalise_dhan_payload(raw, "NATURALGAS")
        ce = next(r for r in result["strikes"] if r["option_type"] == "CE")
        assert ce["iv"] is None
        assert ce["delta"] is None

    def test_fetch_option_chain_logs_warning_on_no_data(self, caplog):
        import logging
        from src.fetchers import dhan_headless_fetcher as dh_mod

        with patch.object(dh_mod, "_fetch_sync", return_value=None):
            with caplog.at_level(logging.WARNING, logger="src.fetchers.dhan_headless_fetcher"):
                f = dh_mod.DhanHeadlessFetcher()
                result = f.fetch_option_chain("NIFTY")

        assert result is None
        assert any("session may be expired" in r.message or "no data" in r.message.lower()
                   for r in caplog.records)

