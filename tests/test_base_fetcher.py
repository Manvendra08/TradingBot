"""
Unit tests for src/fetchers/base_fetcher.py.
"""
import pytest
from unittest.mock import patch, MagicMock
import requests
from src.fetchers.base_fetcher import BaseFetcher

class MockFetcher(BaseFetcher):
    name = "mock"
    def fetch_option_chain(self, symbol: str):
        return None

def test_base_fetcher_get_success():
    f = MockFetcher()
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}
    with patch.object(f.session, "get", return_value=mock_response):
        res = f._get("https://example.com")
        assert res == {"ok": True}

def test_base_fetcher_get_failure_retry():
    f = MockFetcher()
    with patch.object(f.session, "get", side_effect=requests.RequestException("conn error")), \
         patch("src.fetchers.base_fetcher.time.sleep") as mock_sleep:
        res = f._get("https://example.com")
        assert res is None
        # HTTP_MAX_RETRIES is 3 in the default configuration
        assert mock_sleep.call_count == 3
