import time
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers.news_fetcher import _fetch_tv_commodity_news, fetch_news


@pytest.fixture(autouse=True)
def clear_news_cache():
    """Clear news cache before each test."""
    from src.fetchers import news_fetcher

    news_fetcher._cache.clear()


def test_fetch_news_tv_dict_provider():
    """Test news fetcher when TradingView returns provider as a dictionary."""
    mock_payload = {
        "items": [
            {
                "published": int(time.time()),
                "title": "Natural Gas Price Rally Continue",
                "provider": {"name": "Reuters"},
                "storyPath": "/news/reuters-natural-gas-rally",
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.content = b"mock content"
    mock_response.json.return_value = mock_payload
    mock_response.status_code = 200

    with (
        patch("requests.Session.get", return_value=mock_response) as mock_get,
        patch("src.fetchers.news_fetcher._fetch_newsapi_news", return_value=[]),
    ):
        data = fetch_news("NATURALGAS")
        mock_get.assert_called_once()
        assert data["count_24h"] == 1
        assert data["items"][0]["provider"] == "Reuters"
        assert data["items"][0]["title"] == "Natural Gas Price Rally Continue"
        assert data["current_news_direction"] == "BULLISH"


def test_fetch_news_tv_str_provider():
    """Test news fetcher when TradingView returns provider as a string."""
    mock_payload = {
        "items": [
            {
                "published": int(time.time()),
                "title": "Crude Oil Falls On High Inventory",
                "provider": "reuters",
                "storyPath": "/news/reuters-crude-oil-falls",
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.content = b"mock content"
    mock_response.json.return_value = mock_payload
    mock_response.status_code = 200

    with (
        patch("requests.Session.get", return_value=mock_response) as mock_get,
        patch("src.fetchers.news_fetcher._fetch_newsapi_news", return_value=[]),
    ):
        data = fetch_news("CRUDEOIL")
        mock_get.assert_called_once()
        assert data["count_24h"] == 1
        assert data["items"][0]["provider"] == "reuters"
        assert data["items"][0]["title"] == "Crude Oil Falls On High Inventory"
        assert data["current_news_direction"] == "BEARISH"


def test_fetch_news_tv_empty_or_missing_provider():
    """Test news fetcher when provider is None or missing entirely."""
    mock_payload = {
        "items": [
            {
                "published": int(time.time()),
                "title": "Crude Oil Intraday Outlook",
                "storyPath": "/news/outlook",
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.content = b"mock content"
    mock_response.json.return_value = mock_payload
    mock_response.status_code = 200

    with (
        patch("requests.Session.get", return_value=mock_response) as mock_get,
        patch("src.fetchers.news_fetcher._fetch_newsapi_news", return_value=[]),
    ):
        data = fetch_news("CRUDEOIL")
        mock_get.assert_called_once()
        assert data["count_24h"] == 1
        assert data["items"][0]["provider"] == ""
        assert data["items"][0]["title"] == "Crude Oil Intraday Outlook"
        assert data["current_news_direction"] == "MIXED"
