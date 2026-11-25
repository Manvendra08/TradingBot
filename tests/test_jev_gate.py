from unittest.mock import patch

from src.engine.jev_gate import _build_state_text


def test_build_state_text_passes_regime_session_move_and_cached_news():
    scan_context = {
        "underlying": 342.5,
        "pcr": 1.08,
        "market_regime": "TRENDING_UP",
        "price_change_pct": 2.45,
        "trend_1h": "SHOULD_BE_IGNORED",
        "trend_3h": "SHOULD_BE_IGNORED",
    }
    intel = {
        "verdict_label": "Long Buildup",
        "confidence": 65,
    }
    cached_news = {
        "news_score_current": 0.42,
        "current_news_direction": "BULLISH",
        "items": [{"title": "LNG feedgas demand rebounds"}],
    }

    with patch("src.services.news_worker.get_cached_news_sentiment", return_value=cached_news):
        text = _build_state_text("NATURALGAS", scan_context, intel, news_data=None)

    assert "1H trend" not in text
    assert "3H trend" not in text
    assert "Market regime: TRENDING_UP" in text
    assert "Session move: +2.45%" in text
    assert "MCX news score: +0.420 (BULLISH)" in text
    assert "Top news: LNG feedgas demand rebounds" in text


def test_build_state_text_uses_provided_news_data_without_bypassed():
    scan_context = {"underlying": 340.0, "market_regime": "RANGE", "price_change_pct": -1.1}
    news_data = {
        "news_score_current": -0.35,
        "current_news_direction": "BEARISH",
        "items": [{"title": "Milder US weather forecast"}],
    }

    text = _build_state_text("NATURALGAS", scan_context, {"verdict_label": "Low Conviction", "confidence": 20}, news_data)

    assert "Market regime: RANGE" in text
    assert "Session move: -1.10%" in text
    assert "MCX news score: -0.350 (BEARISH)" in text
    assert "Top news: Milder US weather forecast" in text
