import json, os, subprocess, sys
import pytest
from hear.sim import ClapCalibrator
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "calibrate_claps.py")
NODES = {"node-ref": (0.00, 0.00, 0.00), "esp32s3-speaker": (0.62, 0.00, 0.05), "esp32s3-box3": (0.00, 0.58, -0.03), "xiao-s3-pps": (-0.41, 0.28, 0.22), "node-extra": (0.55, 0.52, 0.40)}
BIAS = {"esp32s3-speaker": 180e-6, "esp32s3-box3": -95e-6, "xiao-s3-pps": 0.0}
CLAPS = [(0.40, -0.50, 0.20), (-0.30, 0.40, -0.10), (0.70, 0.30, 0.50), (0.10, 0.10, -0.40), (-0.40, -0.20, 0.30)]

def _inputs(tmp_path, noise=0.0):
    calibrator = ClapCalibrator(NODES, reference_nodes=("xiao-s3-pps",), max_clap_radius_m=1.0)
    observations = calibrator.simulate_claps(CLAPS, capture_biases_s=BIAS, timing_noise_s=noise, seed=4, ingest=False)
    survey = {"nodes": [{"name": key, "position_m": list(value)} for key, value in NODES.items()]}
    arrivals = [{"clap_id": o.clap_id, "node_id": o.node_id, "timestamp_s": o.timestamp_s} for o in observations]
    survey_path, arrivals_path = tmp_path / "survey.json", tmp_path / "arrivals.json"
    survey_path.write_text(json.dumps(survey), encoding="utf-8"); arrivals_path.write_text(json.dumps(arrivals), encoding="utf-8")
    return survey_path, arrivals_path

def test_cli_writes_profile_and_transitions_nodes_to_admissible(tmp_path):
    survey, arrivals = _inputs(tmp_path, noise=1e-7); output = tmp_path / "calibrated.json"
    completed = subprocess.run([sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--output", str(output), "--reference-node", "xiao-s3-pps"], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    profile = json.loads(output.read_text(encoding="utf-8"))
    assert profile["schema"] == "hear.calibrated_node_biases.v1"
    assert profile["nodes"]["esp32s3-speaker"]["path_bias_us"] == pytest.approx(180.0, abs=1.0)
    assert profile["nodes"]["esp32s3-box3"]["path_bias_us"] == pytest.approx(-95.0, abs=1.0)
    assert all(node["status"] == "admissible" for node in profile["nodes"].values())
    assert all(node["sigma_b_us"] <= 30.0 for node in profile["nodes"].values())

def test_cli_refuses_when_confidence_exceeds_gate(tmp_path):
    survey, arrivals = _inputs(tmp_path, noise=2e-3)
    completed = subprocess.run([sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--output", str(tmp_path / "bad.json")], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode != 0
    assert "confidence gate refused" in completed.stderr
    assert not (tmp_path / "bad.json").exists()
