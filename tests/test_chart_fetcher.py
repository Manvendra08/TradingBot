import sys
import pytest
from unittest.mock import patch, MagicMock
import threading

# Ensure src is in the import path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.fetchers.chart_fetcher import _get_tv_client, _tv_local, _tv_record_failure, _tv_record_success

@pytest.fixture(autouse=True)
def clean_tv_local():
    """Reset the thread-local state before and after each test."""
    if hasattr(_tv_local, "client"):
        delattr(_tv_local, "client")
    if hasattr(_tv_local, "fail_count"):
        delattr(_tv_local, "fail_count")
    if hasattr(_tv_local, "backoff_until"):
        delattr(_tv_local, "backoff_until")
    yield
    if hasattr(_tv_local, "client"):
        delattr(_tv_local, "client")
    if hasattr(_tv_local, "fail_count"):
        delattr(_tv_local, "fail_count")
    if hasattr(_tv_local, "backoff_until"):
        delattr(_tv_local, "backoff_until")

@patch("tvDatafeed.TvDatafeed")
def test_get_tv_client_with_sessionid(mock_tvdatafeed):
    """Test that TV_SESSIONID is preferred for authentication."""
    with patch("config.settings.TV_SESSIONID", "fake_session_123"), \
         patch("config.settings.TV_USERNAME", "user@test.com"), \
         patch("config.settings.TV_PASSWORD", "pwd123"):
        
        client = _get_tv_client()
        
        assert client is not None
        mock_tvdatafeed.assert_called_once_with(sessionid="fake_session_123")

@patch("tvDatafeed.TvDatafeed")
def test_get_tv_client_with_credentials_only(mock_tvdatafeed):
    """Test that credentials are used if TV_SESSIONID is missing."""
    with patch("config.settings.TV_SESSIONID", None), \
         patch("config.settings.TV_USERNAME", "user@test.com"), \
         patch("config.settings.TV_PASSWORD", "pwd123"):
        
        client = _get_tv_client()
        
        assert client is not None
        mock_tvdatafeed.assert_called_once_with(username="user@test.com", password="pwd123")

@patch("tvDatafeed.TvDatafeed")
def test_get_tv_client_unauthenticated(mock_tvdatafeed):
    """Test that TvDatafeed initializes without auth if no config is present."""
    with patch("config.settings.TV_SESSIONID", None), \
         patch("config.settings.TV_USERNAME", None), \
         patch("config.settings.TV_PASSWORD", None):
        
        client = _get_tv_client()
        
        assert client is not None
        mock_tvdatafeed.assert_called_once_with()

def test_tv_circuit_breaker():
    """Test that consecutive failures trigger the circuit breaker."""
    import time
    with patch("tvDatafeed.TvDatafeed") as mock_tv:
        # Simulate 3 failures
        for _ in range(3):
            _tv_record_failure()
            
        # Client should now return None (circuit breaker open)
        client = _get_tv_client()
        assert client is None
        
        # Test success reset
        _tv_record_success()
        client = _get_tv_client()
        assert client is not None
