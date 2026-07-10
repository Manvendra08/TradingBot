import pytest
import logging
from unittest.mock import MagicMock, patch
from src.engine.scan_sentinel import (
    ScanRunRecorder,
    SentinelFlag,
    ScanDiagnostic,
    _check_rules,
    run_sentinel,
    _execute_self_healing
)

@pytest.fixture
def clean_report():
    return {
        "symbol": "NIFTY",
        "timestamp_ist": "2026-07-09T20:00:00+05:30",
        "scan_duration_ms": 15000,
        "underlying_price": 24000.0,
        "expiry": "2026-07-16",
        "source": "shoonya",
        "total_strikes": 40,
        "zero_ltp_strikes": 2,
        "zero_oi_strikes": 5,
        "llm_action": "GO_SHORT",
        "llm_instrument": "NIFTY 24000 PE",
        "llm_entry_premium": 150.0,
        "llm_target_1": 250.0,
        "llm_target_2": 350.0,
        "llm_stop_loss": 80.0,
        "trade_decision_status": "TRIGGERED",
        "trade_decision_reason": "AI Signal",
        "warnings": [],
        "errors": [],
        "log_lines": ["Info message 1", "Info message 2"],
        "is_test": False,
        "status": "COMPLETED"
    }

def test_check_rules_clean(clean_report):
    flags = _check_rules(clean_report)
    assert len(flags) == 0

def test_check_rules_premium_is_underlying(clean_report):
    # Set target premium within 5% of underlying index spot (24000)
    clean_report["llm_target_1"] = 24100.0
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R1_PREMIUM_IS_UNDERLYING"
    assert flags[0].severity == "CRITICAL"

def test_check_rules_high_error_rate(clean_report):
    clean_report["errors"] = ["Error 1", "Error 2", "Error 3"]
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R2_HIGH_ERROR_RATE"
    assert flags[0].severity == "WARNING"

def test_check_rules_dead_option_chain(clean_report):
    clean_report["zero_ltp_strikes"] = 35  # 35 out of 40 strikes are zero
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R3_DEAD_OPTION_CHAIN"
    assert flags[0].severity == "WARNING"

def test_check_rules_slow_scan(clean_report):
    clean_report["scan_duration_ms"] = 125000  # 125s
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R4_SLOW_SCAN"
    assert flags[0].severity == "WARNING"

def test_check_rules_option_type_mismatch(clean_report):
    # Action GO_SHORT but instrument is CE
    clean_report["llm_action"] = "GO_SHORT"
    clean_report["llm_instrument"] = "NIFTY 24000 CE"
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R5_OPTION_TYPE_MISMATCH"
    assert flags[0].severity == "CRITICAL"

def test_check_rules_premium_out_of_bounds(clean_report):
    clean_report["llm_entry_premium"] = 6000.0
    flags = _check_rules(clean_report)
    assert len(flags) == 1
    assert flags[0].rule == "R6_PREMIUM_OUT_OF_BOUNDS"
    assert flags[0].severity == "CRITICAL"

@patch("src.engine.llm_enrichment._call_llm_api")
@patch("src.engine.scan_sentinel._persist_sentinel_incident")
def test_run_sentinel_diagnostics(mock_persist, mock_llm, clean_report):
    # Trigger R1 flag
    clean_report["llm_target_1"] = 24100.0
    
    mock_diag = ScanDiagnostic(
        anomaly_summary="SENSEX target premium equals spot price",
        root_cause="Illiquid contract returning spot LTP",
        impact="Order placed at ₹24,100 instead of ₹250",
        severity="CRITICAL",
        recommended_action="SKIP_TRADE",
        reasoning="Target 1 is equal to underlying spot index"
    )
    mock_llm.return_value = mock_diag
    
    with patch("src.engine.scan_sentinel.SENTINEL_HEAL_ENABLED", True), \
         patch("src.engine.scan_sentinel.stamp_health") as mock_stamp:
        diag = run_sentinel(clean_report)
        
        assert diag is not None
        assert diag.anomaly_summary == "SENSEX target premium equals spot price"
        assert diag.recommended_action == "SKIP_TRADE"
        
        # Verify persistence and stamp health calls
        mock_persist.assert_called_once()
        mock_stamp.assert_called_once_with("last_scan_NIFTY", "DEGRADED", "sentinel_blocked: SENSEX target premium equals spot price")

def test_execute_self_healing_clear_cache(clean_report):
    diag = ScanDiagnostic(
        anomaly_summary="Poisoned LLM cache",
        root_cause="Transient error",
        impact="None",
        severity="WARNING",
        recommended_action="CLEAR_CACHE",
        reasoning="Test"
    )
    
    with patch("src.engine.llm_enrichment._VERDICT_CACHE", {"NIFTY": {"entry_premium": 100.0}}):
        from src.engine.llm_enrichment import _VERDICT_CACHE
        assert "NIFTY" in _VERDICT_CACHE
        _execute_self_healing("NIFTY", diag, clean_report)
        assert "NIFTY" not in _VERDICT_CACHE

def test_scan_run_recorder_captured_logs():
    recorder = ScanRunRecorder("NIFTY")
    with recorder:
        logging.getLogger("nsebot").warning("Test log statement 1")
        logging.getLogger("nsebot.sub").warning("Test log statement 2")
        
    assert len(recorder.captured_logs) == 2
    assert "Test log statement 1" in recorder.captured_logs[0]
    assert "Test log statement 2" in recorder.captured_logs[1]
