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
    completed = subprocess.run([sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--output", str(output), "--reference-node", "xiao-s3-pps", "--sound-speed-mps", "343.0"], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    profile = json.loads(output.read_text(encoding="utf-8"))
    assert profile["schema"] == "hear.calibrated_node_biases.v1"
    assert profile["nodes"]["esp32s3-speaker"]["path_bias_us"] == pytest.approx(180.0, abs=1.0)
    assert profile["nodes"]["esp32s3-box3"]["path_bias_us"] == pytest.approx(-95.0, abs=1.0)
    assert all(node["status"] == "admissible" for node in profile["nodes"].values())
    assert all(node["sigma_b_us"] <= 30.0 for node in profile["nodes"].values())

def test_cli_refuses_when_confidence_exceeds_gate(tmp_path):
    survey, arrivals = _inputs(tmp_path, noise=2e-3)
    completed = subprocess.run([sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--output", str(tmp_path / "bad.json"), "--reference-node", "xiao-s3-pps", "--sound-speed-mps", "343.0"], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode != 0
    assert "confidence gate refused" in completed.stderr
    assert not (tmp_path / "bad.json").exists()


def test_cli_requires_explicit_reference_and_sound_speed(tmp_path):
    survey, arrivals = _inputs(tmp_path, noise=1e-7)

    missing_reference = subprocess.run(
        [sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--sound-speed-mps", "343.0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert missing_reference.returncode != 0
    assert "at least one --reference-node is required" in missing_reference.stderr

    missing_sound_speed = subprocess.run(
        [sys.executable, SCRIPT, "--survey", str(survey), "--arrivals", str(arrivals), "--reference-node", "xiao-s3-pps"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert missing_sound_speed.returncode != 0
    assert "--sound-speed-mps" in missing_sound_speed.stderr or "--temp-c" in missing_sound_speed.stderr


def test_cli_uses_survey_names_and_accepts_numeric_arrival_ids(tmp_path):
    nodes = {
        "nyquist": (0.00, 0.00, 0.00),
        "mach": (0.52, 0.00, 0.02),
        "rankine": (0.00, 0.49, -0.01),
        "gold": (0.47, 0.43, 0.03),
    }
    calibrator = ClapCalibrator(nodes, reference_nodes=("nyquist", "mach", "rankine"), max_clap_radius_m=1.0)
    observations = calibrator.simulate_claps(
        [(0.10, 0.00, 0.00), (-0.20, 0.15, 0.00), (0.20, 0.20, 0.00), (0.05, -0.15, 0.00)],
        capture_biases_s={"gold": 120e-6},
        timing_noise_s=0.0,
        seed=6,
        ingest=False,
    )
    survey = {
        "nodes": [
            {"node_id": 1, "name": "nyquist", "e_m": 0.00, "n_m": 0.00, "u_m": 0.00},
            {"node_id": 2, "name": "mach", "e_m": 0.52, "n_m": 0.00, "u_m": 0.02},
            {"node_id": 3, "name": "rankine", "e_m": 0.00, "n_m": 0.49, "u_m": -0.01},
            {"node_id": 5, "name": "gold", "class": "esp32s3-i2s-gps", "e_m": 0.47, "n_m": 0.43, "u_m": 0.03},
        ]
    }
    arrivals = []
    node_ids = {"nyquist": 1, "mach": 2, "rankine": 3, "gold": 5}
    for observation in observations:
        arrival = {"clap_id": observation.clap_id, "timestamp_s": observation.timestamp_s}
        arrival["node_id" if observation.node_id == "gold" else "node"] = node_ids[observation.node_id] if observation.node_id == "gold" else observation.node_id
        arrivals.append(arrival)
    survey_path, arrivals_path = tmp_path / "survey.json", tmp_path / "arrivals.json"
    survey_path.write_text(json.dumps(survey), encoding="utf-8")
    arrivals_path.write_text(json.dumps(arrivals), encoding="utf-8")

    output = tmp_path / "calibrated.json"
    completed = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--survey",
            str(survey_path),
            "--arrivals",
            str(arrivals_path),
            "--output",
            str(output),
            "--reference-node",
            "nyquist",
            "--reference-node",
            "2",
            "--reference-node",
            "rankine",
            "--sound-speed-mps",
            "343.0",
            "--clap-plane-z-m",
            "0.0",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    profile = json.loads(output.read_text(encoding="utf-8"))
    assert profile["reference_nodes"] == ["nyquist", "mach", "rankine"]
    assert profile["nodes"]["gold"]["path_bias_us"] == pytest.approx(120.0, abs=1.0)
