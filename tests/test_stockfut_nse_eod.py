"""Tests for NSE EOD fetcher using mocks and fixtures (no network calls)."""

from unittest.mock import MagicMock
import pytest
from src.fetchers.nse_eod_fetcher import NseEodFetcher, FetchError


def test_parses_ban_list_fixture():
    fetcher = NseEodFetcher()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "secBanList": ["IDEA", "SAIL", "pnb"]
    }
    mock_session.get.return_value = mock_resp
    fetcher._session = mock_session

    ban_set = fetcher.fetch_ban_list()
    assert ban_set == {"IDEA", "SAIL", "PNB"}


def test_mwpl_percent_as_float():
    fetcher = NseEodFetcher()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"symbol": "RELIANCE", "mwplPercentage": 45.5},
            {"symbol": "IDEA", "mwplPercentage": "86.2"},
        ]
    }
    mock_session.get.return_value = mock_resp
    fetcher._session = mock_session

    mwpl = fetcher.fetch_mwpl()
    assert mwpl["RELIANCE"] == 45.5
    assert mwpl["IDEA"] == 86.2


def test_fetch_failure_raises():
    fetcher = NseEodFetcher()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_session.get.return_value = mock_resp
    fetcher._session = mock_session

    with pytest.raises(FetchError):
        fetcher.fetch_ban_list()
