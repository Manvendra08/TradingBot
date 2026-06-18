# 1. Update tests/conftest.py
with open("tests/conftest.py", "r", encoding="utf-8") as f:
    conftest_content = f.read()

fixture_code = """

@pytest.fixture(autouse=True)
def mock_runtime_config_frequencies():
    \"\"\"Mock scan frequencies to default 5 min to ensure tests are isolated from host configuration file.\"\"\"
    with patch("src.engine.paper_trading.get_scan_frequency_nse", return_value=5), \\
         patch("src.engine.paper_trading.get_scan_frequency_mcx", return_value=5), \\
         patch("src.engine.paper_trading.get_scan_frequency_minutes", return_value=5):
        yield
"""

if "mock_runtime_config_frequencies" not in conftest_content:
    with open("tests/conftest.py", "a", encoding="utf-8") as f:
        f.write(fixture_code)
    print("Added mock_runtime_config_frequencies to tests/conftest.py")

# 2. Update src/engine/risk_engine.py to comment out daily loss limit
with open("src/engine/risk_engine.py", "r", encoding="utf-8") as f:
    risk_content = f.read()

target_loss_cap = """        # 4. Daily loss cap (also IST-aligned)
        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?",
            (today_start,),
        ).fetchone()["total"]
        if float(today_pnl) < -abs(MAX_DAILY_LOSS_RUPEES):
            return False, f"Daily loss limit hit (\\u20b9{float(today_pnl):,.0f})\""""

# Let's replace it with commented-out version
replacement_loss_cap = """        # 4. Daily loss cap (Removed per user request)
        # today_pnl = conn.execute(
        #     "SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?",
        #     (today_start,),
        # ).fetchone()["total"]
        # if float(today_pnl) < -abs(MAX_DAILY_LOSS_RUPEES):
        #     return False, f"Daily loss limit hit (\\u20b9{float(today_pnl):,.0f})\""""

if "Removed per user request" not in risk_content:
    # Let's find and replace it
    risk_content = risk_content.replace(
        "        # 4. Daily loss cap (also IST-aligned)\n        today_pnl = conn.execute(\n            \"SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?\",\n            (today_start,),\n        ).fetchone()[\"total\"]\n        if float(today_pnl) < -abs(MAX_DAILY_LOSS_RUPEES):\n            return False, f\"Daily loss limit hit (\\u20b9{float(today_pnl):,.0f})\"",
        "        # 4. Daily loss cap (Removed per user request)\n        # today_pnl = conn.execute(\n        #     \"SELECT COALESCE(SUM(pnl_rupees), 0) AS total FROM paper_trades WHERE closed_at >= ?\",\n        #     (today_start,),\n        # ).fetchone()[\"total\"]\n        # if float(today_pnl) < -abs(MAX_DAILY_LOSS_RUPEES):\n        #     return False, f\"Daily loss limit hit (\\u20b9{float(today_pnl):,.0f})\""
    )
    with open("src/engine/risk_engine.py", "w", encoding="utf-8") as f:
        f.write(risk_content)
    print("Removed daily loss limit check in src/engine/risk_engine.py")

# 3. Update tests/test_core_engine_coverage.py
with open("tests/test_core_engine_coverage.py", "r", encoding="utf-8") as f:
    cov_content = f.read()

# Replace get_conn patch with _db_insert_scan_summary patch
cov_content = cov_content.replace(
    '        with patch("src.engine.scan_summary.get_conn") as mock_conn:\n            mock_conn.side_effect = Exception("Simulated DB Error")',
    '        with patch("src.engine.scan_summary._db_insert_scan_summary") as mock_insert:\n            mock_insert.side_effect = Exception("Simulated DB Error")'
)

# Also update the daily loss test
cov_content = cov_content.replace(
    '                (today_str, today_str, "TEST_SYM", "CE", 100.0, -250000.0, "CLOSED_SL")\n            )\n        allowed, reason = check_risk_limits("TEST_SYM")\n        assert not allowed\n        assert "Daily loss limit hit" in reason',
    '                (today_str, today_str, "TEST_SYM", "CE", 100.0, -250000.0, "CLOSED_TARGET")\n            )\n        allowed, reason = check_risk_limits("TEST_SYM")\n        assert allowed\n        assert "Risk checks passed" in reason'
)

with open("tests/test_core_engine_coverage.py", "w", encoding="utf-8") as f:
    f.write(cov_content)
print("Updated tests/test_core_engine_coverage.py")
