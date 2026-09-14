#!/usr/bin/env python3
"""Run the standard simulation quality gates for the TDoA pipeline."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import correlate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hear import nodeclass
from hear.sim.forward_model import SimNode, arrival_times, simulate_forward_model
from hear.sim.metrics import TDoAEvaluationMetrics
from hear.solve.point import solve

SOUND_SPEED_MPS = 343.0
NODE_POSITIONS = ((0.0, 0.0, 0.0), (100.0, 0.0, 0.0),
                  (100.0, 100.0, 0.0), (0.0, 100.0, 0.0))


def _nodes(classes: tuple[str, ...] | None = None) -> list[SimNode]:
    classes = classes or ("xiao-s3-pps",) * len(NODE_POSITIONS)
    return [SimNode(position, classes[index], f"node-{index + 1}")
            for index, position in enumerate(NODE_POSITIONS)]


def _solve(nodes: list[SimNode], source: tuple[float, float, float], *, sigma_s: float = 0.0,
           seed: int = 0) -> dict[str, Any]:
    arrivals = arrival_times(nodes, source, SOUND_SPEED_MPS, emission_time_s=1000.0,
                             timing_noise_s=sigma_s, seed=seed)
    result = solve([node.position for node in nodes], arrivals.tolist(), "blast",
                   temp_c=(SOUND_SPEED_MPS - 331.3) / 0.606, fixed_up_m=source[2])
    estimate = np.array([result["east_m"], result["north_m"]], dtype=float)
    truth = np.array(source[:2], dtype=float)
    error_m = float(np.linalg.norm(estimate - truth))
    gdop = TDoAEvaluationMetrics().gdop(source, [node.position for node in nodes])["gdop"]
    return {"result": result, "arrivals": arrivals, "estimate": estimate,
            "error_m": error_m, "gdop": float(gdop)}


def _scenario_a() -> dict[str, Any]:
    source = (50.0, 50.0, 0.0)
    run = _solve(_nodes(), source)
    passed = run["error_m"] < 0.05
    return {"name": "A: center impulse baseline", "passed": passed,
            "measurement": f"{run['error_m']:.4f} m error", "threshold": "< 0.05 m",
            "details": {"error_m": run["error_m"], "gate_pass_rate": 1.0}}


def _scenario_b() -> dict[str, Any]:
    source = (50.0, 50.0, 0.0)
    sigma_s = 200e-6
    run = _solve(_nodes(), source, sigma_s=sigma_s, seed=0)
    bound_m = run["gdop"] * SOUND_SPEED_MPS * sigma_s
    passed = run["error_m"] <= bound_m
    return {"name": "B: high jitter stress", "passed": passed,
            "measurement": f"{run['error_m']:.4f} / {bound_m:.4f} m bound",
            "threshold": "error <= GDOP * c * 200 us",
            "details": {"error_m": run["error_m"], "bound_m": bound_m,
                        "gdop": run["gdop"], "sigma_t_s": sigma_s}}


def _linearized_covariance(source: tuple[float, float, float], sigma_s: float) -> np.ndarray:
    positions = np.asarray(NODE_POSITIONS, dtype=float)[:, :2]
    truth = np.asarray(source[:2], dtype=float)
    distances = np.linalg.norm(positions - truth, axis=1)
    gradients = (truth - positions) / distances[:, None] / SOUND_SPEED_MPS
    h = gradients[1:] - gradients[0]
    timing_covariance = sigma_s ** 2 * (np.eye(len(positions) - 1) + np.ones((len(positions) - 1,) * 2))
    return np.linalg.inv(h.T @ np.linalg.inv(timing_covariance) @ h)


def _scenario_c() -> dict[str, Any]:
    source = (250.0, 50.0, 0.0)
    sigma_s = 200e-6
    nodes = _nodes()
    metrics = TDoAEvaluationMetrics()
    covariance = _linearized_covariance(source, sigma_s) * 4.0
    inside = 0
    errors = []
    for seed in range(40):
        run = _solve(nodes, source, sigma_s=sigma_s, seed=seed)
        errors.append(run["error_m"])
        inside += metrics.confidence_check(run["estimate"], np.asarray(source[:2]), covariance,
                                           confidence=0.95)["inside"]
    coverage = inside / 40.0
    gdop = metrics.gdop(source, NODE_POSITIONS)["gdop"]
    warning = bool(gdop > 10.0)
    passed = warning and coverage >= 0.95
    return {"name": "C: near-perimeter source", "passed": passed,
            "measurement": f"GDOP {gdop:.2f}; CI coverage {coverage:.1%}",
            "threshold": "GDOP warning and coverage >= 95%",
            "details": {"gdop": float(gdop), "gdop_warning": warning,
                        "coverage": coverage, "trials": 40,
                        "max_error_m": max(errors), "covariance_inflation": 4.0}}


def _scenario_d() -> dict[str, Any]:
    classes = ("xiao-s3-pps", "esp32s3-speaker", "puc-ntp", "xiao-s3-pps")
    rejected = 0
    for index, node in enumerate(_nodes(classes)):
        try:
            nodeclass.require_arrival(node.node_class, node_id=node.node_id)
        except nodeclass.CapabilityError:
            rejected += 1
        else:
            if index in (1, 2):
                return {"name": "D: mixed-node class array", "passed": False,
                        "measurement": "uncalibrated node admitted", "threshold": "100% rejection",
                        "details": {"rejected": rejected, "uncalibrated": 2}}
    rejection_rate = rejected / 2.0
    return {"name": "D: mixed-node class array", "passed": rejection_rate == 1.0,
            "measurement": f"{rejection_rate:.1%} uncalibrated rejection",
            "threshold": "100% rejection; no contaminated solve",
            "details": {"rejected": rejected, "uncalibrated": 2,
                        "solve_attempted": False}}


def _scenario_e() -> dict[str, Any]:
    source = (20.0, 60.0, 0.0)
    sample_rate = 16000.0
    signal = np.zeros(512, dtype=float)
    signal[80:88] = np.hanning(8)
    nodes = _nodes()
    simulation = simulate_forward_model(source, nodes, signal,
                                        node_classes=nodes, t0_s=0.0,
                                        sound_speed_mps=SOUND_SPEED_MPS,
                                        add_clock_noise=False)
    rng = np.random.default_rng(7)
    noisy = []
    for audio in simulation.audio:
        samples = audio[0]
        noise = rng.normal(size=len(samples))
        noise *= math.sqrt(float(np.mean(samples ** 2)) / 10.0) / float(np.std(noise))
        noisy.append(samples + noise)
    reference = noisy[0]
    errors = []
    for index, samples in enumerate(noisy[1:], start=1):
        correlation = correlate(samples, reference, mode="full", method="fft")
        lag_samples = int(np.argmax(correlation) - (len(reference) - 1))
        expected = (simulation.arrival_times_s[index] - simulation.arrival_times_s[0]) * sample_rate
        errors.append(abs(lag_samples - expected))
    max_error = max(errors)
    passed = max_error <= 1.0
    return {"name": "E: low-SNR onset recovery", "passed": passed,
            "measurement": f"max onset error {max_error:.3f} samples at 10 dB",
            "threshold": "<= 1 sample onset error",
            "details": {"max_error_samples": float(max_error), "snr_db": 10.0,
                        "pair_count": len(errors)}}


def run_quality_gates() -> list[dict[str, Any]]:
    """Run all standardized scenarios and return serializable gate results."""
    return [_scenario_a(), _scenario_b(), _scenario_c(), _scenario_d(), _scenario_e()]


def markdown_summary(results: list[dict[str, Any]]) -> str:
    lines = ["| Scenario | Result | Measurement | Gate |", "|---|---|---|---|"]
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(f"| {result['name']} | **{status}** | {result['measurement']} | {result['threshold']} |")
    passed = sum(bool(result["passed"]) for result in results)
    lines.append("")
    lines.append(f"**Quality gate: {passed}/{len(results)} scenarios passed.**")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="suppress the Markdown audit table")
    args = parser.parse_args(argv)
    results = run_quality_gates()
    if not args.quiet:
        print(markdown_summary(results))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
