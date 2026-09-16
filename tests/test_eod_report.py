"""
Unit tests for Firecrawl EOD Fetcher and EOD Report Generator.
"""

from unittest.mock import patch, MagicMock
from src.fetchers.firecrawl_eod import fetch_firecrawl_macro_news, fetch_firecrawl_eod_data
from src.engine.eod_report_generator import generate_eod_macro_report, EODMarketReport


def test_firecrawl_missing_key():
    """Test that missing FIRECRAWL_API_KEY handles gracefully without crashing."""
    with patch.dict("os.environ", {}, clear=True):
        res = fetch_firecrawl_macro_news()
        assert res["ok"] is False
        assert res["count"] == 0
        assert "FIRECRAWL_API_KEY missing" in res["error"]


def test_firecrawl_v2_web_shape():
    """Test Firecrawl API v2 nested response shape {"success": true, "data": {"web": [...]}}."""
    mock_payload = {
        "success": True,
        "data": {
            "web": [
                {
                    "title": "Nifty breaks 23500",
                    "url": "https://example.com/2",
                    "description": "Nifty slips on crude oil surge",
                    "snippet": "Crude oil near 100",
                }
            ]
        },
    }
    with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-key"}):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = mock_payload

            res = fetch_firecrawl_macro_news(country="in")
            assert res["ok"] is True
            assert res["count"] == 1
            assert res["items"][0]["title"] == "Nifty breaks 23500"

            # Verify country param was sent
            called_kwargs = mock_post.call_args.kwargs
            json_payload = called_kwargs.get("json", {})
            assert json_payload.get("country") == "in"


def test_eod_report_generation():
    """Test EOD report generation pipeline end-to-end with real-schema mocks."""
    mock_llm_response = EODMarketReport(
        title="Indian Market Close EOD Report — 09 Sep 2026",
        index_summary="Nifty closed at 23,431 (Long Unwinding, 50% conviction). Sensex at 74,764 (Low Conviction). BankNifty at 56,295 with PCR 1.52 (Short Buildup, 60%).",
        oi_derivatives_analysis=(
            "NIFTY: CE OI change -8,580,910 (call short-covering/unwinding), PE OI change -26,384,020 (massive put unwinding — bullish unwind). "
            "PCR 0.88 signals mild bearish tilt. Max pain 23,500 vs spot 23,431 — pin risk at 23,500. "
            "BANKNIFTY: CE -2,189,430, PE -1,381,290. PCR 1.52 — put writing dominance, bullish institutional positioning. "
            "SENSEX: CE -7,451,220, PE -13,335,240. Both sides unwinding — directional ambiguity, avoid naked structures."
        ),
        bot_performance="3 Iron Condor books opened today. SENSEX IC closed +Rs 630 (AI exit, DTE-1 time decay rule). NIFTY IC open at +Rs 32.5 unrealized. BANKNIFTY IC open at Rs 0 unrealized. Total realized P&L: +Rs 630.",
        key_levels_tomorrow=(
            "NIFTY: Support 23,400 (max pain 23,500 acts as magnet, DTE 6). Resistance 23,700 (CE wall). "
            "Bias: Neutral-to-bearish given PCR 0.88 and put unwinding. Watch 23,400 — break below triggers bearish acceleration. "
            "BANKNIFTY: Support 55,900 (invalidation level per IC). Resistance 56,800. PCR 1.52 bullish but Short Buildup OI pattern = fade rallies."
        ),
        macro_and_watchlist="Global cues mixed with crude oil elevated near $80. Watch: (1) Nifty 23,400 support at open — if gaps below, ICs at risk. (2) BankNifty PCR decay trend at 09:15. (3) SENSEX 0 DTE expiry — no new positions.",
    )

    with patch("src.engine.eod_report_generator.fetch_firecrawl_eod_data") as mock_fc, \
         patch("src.engine.eod_report_generator._get_today_scan_data") as mock_summary, \
         patch("src.engine.eod_report_generator._get_today_trade_performance") as mock_trades, \
         patch("src.engine.llm_enrichment._call_llm_api") as mock_llm:

        mock_fc.return_value = {
            "ok": True,
            "closing_bell": {
                "items": [{"title": "Nifty closes at 23431", "description": "Markets closed mixed", "url": "http://x.com"}],
                "count": 1, "error": None,
            },
            "fii_dii_flows": {
                "items": [{"title": "FII DII activity", "description": "FII net sellers", "url": "http://x.com"}],
                "count": 1, "error": None,
            },
            "macro_news": {
                "items": [{"title": "Nifty News", "description": "Markets closed mixed", "url": "http://x.com"}],
                "count": 1, "error": None,
            },
        }
        mock_summary.return_value = [
            {
                "symbol": "NIFTY", "underlying": 23431.5, "pcr": 0.882,
                "max_pain": 23500.0, "verdict_label": "Long Unwinding", "confidence": 50,
                "ce_oi_change": -8580910, "pe_oi_change": -26384020,
                "total_ce_oi": 54065180, "total_pe_oi": 47687380,
                "atm_strike": 23450.0, "support": 23400.0, "resistance": 23700.0,
                "top_signal_type": None, "top_signal_strike": None,
                "top_signal_option_type": None, "top_signal_severity": None,
                "trend_bias": None, "market_regime": None,
                "candle_1h": None, "candle_3h": None, "fetched_at": "2026-09-09T10:10:00+00:00",
            }
        ]
        mock_trades.return_value = {
            "ml_opened": 3, "ml_closed": 1, "ml_open": 2,
            "ml_total_pnl": 630.0, "ml_closed_pnl": 630.0,
            "ml_books": [],
            "pt_opened": 0, "pt_closed": 0, "pt_total_pnl": 0.0,
        }
        mock_llm.return_value = mock_llm_response

        report_path = generate_eod_macro_report()
        assert report_path is not None
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "Index Summary" in content
        assert "OI & Derivatives Analysis" in content
        assert "Bot Performance" in content
        assert "Key Levels" in content
        assert "Macro" in content
        # Verify no test placeholder data
        assert "Test Date" not in content
        assert "Nifty ended flat" not in content


def test_firecrawl_eod_data_missing_key():
    """Test that missing FIRECRAWL_API_KEY returns all sections as failed."""
    with patch.dict("os.environ", {}, clear=True):
        res = fetch_firecrawl_eod_data()
        assert res["ok"] is False
        assert res["closing_bell"]["error"] == "FIRECRAWL_API_KEY missing"
        assert res["fii_dii_flows"]["error"] == "FIRECRAWL_API_KEY missing"
        assert res["macro_news"]["error"] == "FIRECRAWL_API_KEY missing"


def test_firecrawl_eod_data_success():
    """Test fetch_firecrawl_eod_data makes 3 searches and aggregates results."""
    mock_response = {
        "success": True,
        "data": {
            "web": [
                {"title": "Nifty closes at 23431", "description": "Market ended flat", "url": "http://x.com"}
            ]
        },
    }
    with patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-key"}):
        with patch("requests.post") as mock_post, patch("time.sleep"):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = mock_response

            res = fetch_firecrawl_eod_data()
            assert res["ok"] is True
            assert res["closing_bell"]["count"] == 1
            assert res["fii_dii_flows"]["count"] == 1
            assert res["macro_news"]["count"] == 1
            # 3 separate Firecrawl API calls
            assert mock_post.call_count == 3
