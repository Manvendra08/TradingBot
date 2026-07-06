import subprocess
import sys

result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_core_engine_coverage.py::TestTradeDecisionDetailed::test_decision_mode_conservative_success_and_fail",
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

print("STDOUT:")
print(result.stdout[-5000:])
print("\nSTDERR:")
print(result.stderr[-5000:])
print("\nReturn code:", result.returncode)
