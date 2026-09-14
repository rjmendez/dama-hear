"""Integration coverage for the standardized TDoA simulation quality gate."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.validate_tdoa_quality import markdown_summary, run_quality_gates


def test_all_quality_gate_scenarios_pass():
    results = run_quality_gates()
    assert [result["name"] for result in results] == [
        "A: center impulse baseline", "B: high jitter stress", "C: near-perimeter source",
        "D: mixed-node class array", "E: low-SNR onset recovery",
    ]
    assert all(result["passed"] for result in results)
    assert results[2]["details"]["coverage"] >= 0.95
    assert results[3]["details"]["rejected"] == results[3]["details"]["uncalibrated"]


def test_quality_gate_cli_emits_markdown_and_success():
    completed = subprocess.run([sys.executable, "tools/validate_tdoa_quality.py"],
                               check=False, capture_output=True, text=True)
    assert completed.returncode == 0
    assert "| Scenario | Result | Measurement | Gate |" in completed.stdout
    assert "**Quality gate: 5/5 scenarios passed.**" in completed.stdout
    assert markdown_summary(run_quality_gates()) in completed.stdout
