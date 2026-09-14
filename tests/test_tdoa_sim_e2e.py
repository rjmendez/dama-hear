"""End-to-end tests for the TDoA simulation CLI."""
import json
import struct
import subprocess
import sys
import wave


def run_cli(tmp_path, *args):
    report = tmp_path / "report.json"
    command = [sys.executable, "tools/simulate_tdoa.py", *args, "--output-report", str(report)]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    assert completed.stderr == ""
    return json.loads(report.read_text())


def test_center_benchmark_recovers_source(tmp_path):
    result = run_cli(tmp_path, "square-4node-center", "--snr-db", "40")
    assert result["status"] == "ok"
    assert result["gate"]["admitted"] is True
    assert result["metrics"]["position_error_m"] < 0.1


def test_outside_benchmark_reports_gdop_degradation(tmp_path):
    center = run_cli(tmp_path, "square-4node-center", "--snr-db", "40")
    outside = run_cli(tmp_path, "square-4node-outside", "--snr-db", "40")
    assert outside["status"] == "ok"
    assert outside["metrics"]["gdop"] > center["metrics"]["gdop"]
    assert outside["metrics"]["position_error_m"] < 1.0


def test_mixed_nodeclasses_refuses_uncalibrated_arrivals(tmp_path):
    result = run_cli(tmp_path, "mixed-nodeclasses")
    assert result["status"] == "refused"
    assert result["gate"]["admitted"] is False
    assert any("ntp" in error.lower() or "unknown node class" in error.lower()
               for error in result["gate"]["errors"])


def test_recorded_impulse_replay_runs_through_cli(tmp_path):
    sample = tmp_path / "impulse.wav"
    values = [0] * 8000
    values[1600] = 30000
    with wave.open(str(sample), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(struct.pack("<h", value) for value in values))
    result = run_cli(tmp_path, "real-sample-impulse", "--sample", str(sample), "--snr-db", "30")
    assert result["status"] == "ok"
    assert result["replay"]["impulse_index"] == 1600
    assert result["metrics"]["position_error_m"] < 0.1
