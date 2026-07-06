import os
import json
import pytest
from unittest.mock import patch, MagicMock
from src.engine.index_weights import (
    get_index_weights_state,
    calculate_index_momentum,
    refresh_index_weights,
    get_live_constituent_changes
)

def test_get_index_weights_state_fallback():
    """Verify that get_index_weights_state returns fallback static weights when cache is missing."""
    # Ensure cache file path does not exist by patching it
    with patch("src.engine.index_weights.CACHE_FILE", "nonexistent_file_path.json"):
        state = get_index_weights_state()
        assert "weights" in state
        assert "NIFTY" in state["weights"]
        assert "BANKNIFTY" in state["weights"]
        assert "SENSEX" in state["weights"]
        assert abs(sum(state["weights"]["NIFTY"].values()) - 1.0) < 1e-4
        assert abs(sum(state["weights"]["BANKNIFTY"].values()) - 1.0) < 1e-4
        assert abs(sum(state["weights"]["SENSEX"].values()) - 1.0) < 1e-4

def test_calculate_index_momentum_neutral():
    """Test momentum calculation when changes are zero (Neutral)."""
    with patch("src.engine.index_weights.get_live_constituent_changes") as mock_changes:
        mock_changes.return_value = {c: 0.0 for c in ["RELIANCE", "TCS", "HDFCBANK"]}
        result = calculate_index_momentum("NIFTY")
        assert result["weighted_momentum"] == 0.0
        assert result["direction"] == "NEUTRAL"
        assert len(result["constituents"]) > 0

def test_calculate_index_momentum_bullish():
    """Test momentum calculation when changes are positive (Bullish)."""
    with patch("src.engine.index_weights.get_live_constituent_changes") as mock_changes:
        # All constituents up by 2%
        mock_changes.return_value = {c: 2.0 for c in ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC"]}
        result = calculate_index_momentum("NIFTY")
        assert result["weighted_momentum"] > 0.50
        assert result["direction"] == "BULLISH"

def test_calculate_index_momentum_bearish():
    """Test momentum calculation when changes are negative (Bearish)."""
    with patch("src.engine.index_weights.get_live_constituent_changes") as mock_changes:
        # All constituents down by 2%
        mock_changes.return_value = {c: -2.0 for c in ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC"]}
        result = calculate_index_momentum("NIFTY")
        assert result["weighted_momentum"] < -0.50
        assert result["direction"] == "BEARISH"

@patch("yfinance.download")
def test_get_live_constituent_changes_cached(mock_yf_download):
    """Test caching of constituent changes works and doesn't query yfinance twice."""
    mock_yf_download.return_value = MagicMock()
    
    # Pre-populate cache for RELIANCE.NS
    from src.engine.index_weights import _LIVE_CHANGES_CACHE
    import time
    _LIVE_CHANGES_CACHE["RELIANCE.NS"] = (1.50, time.time())
    
    changes = get_live_constituent_changes("NIFTY")
    assert changes.get("RELIANCE") == 1.50
    # yfinance should not have been called for RELIANCE.NS since it is cached
