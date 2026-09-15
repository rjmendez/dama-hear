#!/usr/bin/env python3
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "estimate_geometry_from_claps.py")
SPEED_MPS = 343.0
NODES = {
    "node-a": (0.00, 0.00),
    "node-b": (0.72, 0.00),
    "node-c": (0.21, 0.64),
    "node-d": (0.91, 0.48),
    "node-e": (0.15, 0.93),
}
CLAPS = [
    (0.12, 0.18),
    (0.80, 0.14),
    (0.34, 0.71),
    (0.67, 0.82),
    (0.49, 0.29),
    (0.05, 0.76),
    (0.95, 0.74),
    (0.42, 0.06),
    (0.28, 0.49),
    (0.74, 0.58),
]
EMISSIONS = [50.0 + 0.7 * i for i in range(len(CLAPS))]
BIASES = {"node-b": 180e-6, "node-c": -95e-6, "node-d": 40e-6, "node-e": -210e-6}


def _arrivals():
    rows = []
    for clap_id, (position, t0) in enumerate(zip(CLAPS, EMISSIONS)):
        for node_id, node_xy in NODES.items():
            delay = math.dist(position, node_xy) / SPEED_MPS
            rows.append(
                {
                    "clap_id": "clap-%d" % clap_id,
                    "node_id": node_id,
                    "timestamp_s": t0 + delay + BIASES.get(node_id, 0.0),
                }
            )
    return {"observations": rows}


def _pairwise(points):
    keys = list(points)
    return {
        (keys[i], keys[j]): float(np.linalg.norm(np.asarray(points[keys[i]]) - np.asarray(points[keys[j]])))
        for i in range(len(keys))
        for j in range(i + 1, len(keys))
    }


def test_cli_recovers_synthetic_relative_geometry_and_biases():
    completed = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--arrivals",
            "-",
            "--output",
            "-",
            "--dimension",
            "2",
            "--restarts",
            "1",
            "--seed",
            "4",
            "--max-nfev",
            "2000",
            "--coordinate-limit-m",
            "5",
            "--initial-scale-m",
            "1",
        ],
        cwd=ROOT,
        input=json.dumps(_arrivals()),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    recovered = {
        node_id: tuple(result["nodes"][node_id]["position_m"])
        for node_id in sorted(result["nodes"])
    }
    truth_pairs = _pairwise(NODES)
    got_pairs = _pairwise(recovered)
    for pair, truth in truth_pairs.items():
        assert got_pairs[pair] == pytest.approx(truth, abs=1e-3)

    for node_id, truth in BIASES.items():
        assert result["nodes"][node_id]["bias_s"] == pytest.approx(truth, abs=1e-6)
    assert result["solver"]["residual_rms_s"] < 1e-9
    assert result["identifiability"]["degrees_of_freedom"] > 0


def test_cli_refuses_underdetermined_3d_count_before_solving():
    completed = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--arrivals",
            "-",
            "--output",
            "-",
            "--dimension",
            "3",
        ],
        cwd=ROOT,
        input=json.dumps(_arrivals()),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "under-determined by TDOA count in 3D" in completed.stderr
