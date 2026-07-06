import subprocess

result = subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "tests/test_core_engine_coverage.py::TestTradeDecisionDetailed::test_decision_mode_hybrid_all_branches",
        "-v",
        "--tb=short",
        "-p",
        "no:cov",
        "--override-ini=addopts=",
    ],
    capture_output=True,
    text=True,
    timeout=120,
    cwd=r"C:\Users\manve\Downloads\NSEBOT",
)
print("STDOUT:", result.stdout[-3000:])
print("STDERR:", result.stderr[-3000:])
print("Return code:", result.returncode)
