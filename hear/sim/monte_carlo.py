"""Monte Carlo bounds and failure-mode characterization for point-source TDoA."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from hear.sim.forward_model import simulate_forward_model
from hear.sim.metrics import TDoAEvaluationMetrics
from hear.solve import point


@dataclass(frozen=True)
class MonteCarloSweep:
    """Run reproducible SNR/timing/geometry sweeps over a 2D array."""

    node_positions: Sequence[Sequence[float]]
    snr_db: Sequence[float] = tuple(range(0, 41, 5))
    timing_sigma_s: Sequence[float] = (1e-5, 3e-5, 1e-4, 3e-4, 1e-3)
    grid_step_m: float = 5.0
    outside_margin_m: float = 10.0
    trials_per_position: int = 1
    seed: int = 0
    sound_speed_mps: float = 343.42
    fixed_up_m: float = 0.0

    def __post_init__(self) -> None:
        nodes = np.asarray(self.node_positions, dtype=float)
        if nodes.ndim != 2 or nodes.shape[1] != 2 or len(nodes) < 3:
            raise ValueError("node_positions must contain at least three 2D positions")
        if not np.all(np.isfinite(nodes)):
            raise ValueError("node_positions must be finite")
        if self.grid_step_m <= 0 or self.outside_margin_m < 0 or self.trials_per_position < 1:
            raise ValueError("grid_step_m and trials_per_position must be positive and margin non-negative")
        if any(float(v) < 0 or not np.isfinite(v) for v in self.timing_sigma_s):
            raise ValueError("timing_sigma_s must be finite and non-negative")

    @property
    def positions(self) -> np.ndarray:
        nodes = np.asarray(self.node_positions, dtype=float)
        lo = nodes.min(axis=0) - self.outside_margin_m
        hi = nodes.max(axis=0) + self.outside_margin_m
        xs = np.arange(lo[0], hi[0] + self.grid_step_m * 0.5, self.grid_step_m)
        ys = np.arange(lo[1], hi[1] + self.grid_step_m * 0.5, self.grid_step_m)
        return np.asarray([(x, y) for y in ys for x in xs], dtype=float)

    def _inside(self, position: np.ndarray) -> bool:
        nodes = np.asarray(self.node_positions, dtype=float)
        lo, hi = nodes.min(axis=0), nodes.max(axis=0)
        return bool(np.all(position >= lo - 1e-9) and np.all(position <= hi + 1e-9))

    @staticmethod
    def _percentile(values: Iterable[float], percentile: float) -> float | None:
        values = np.asarray(list(values), dtype=float)
        return float(np.percentile(values, percentile)) if values.size else None

    def _extract_arrivals(self, audio: Sequence[np.ndarray], sample_rates: Sequence[float], rng: np.random.Generator,
                          snr_db: float, timing_sigma_s: float) -> np.ndarray:
        arrivals = []
        for stream, rate in zip(audio, sample_rates):
            signal = np.asarray(stream, dtype=float).reshape(-1)
            rms = float(np.sqrt(np.mean(signal * signal)))
            noise_std = rms / (10.0 ** (float(snr_db) / 20.0))
            noisy = signal + rng.normal(0.0, noise_std, signal.size)
            arrivals.append(float(np.argmax(np.abs(noisy))) / float(rate))
        arrivals = np.asarray(arrivals)
        if timing_sigma_s:
            arrivals += rng.normal(0.0, timing_sigma_s, len(arrivals))
        return arrivals

    def _run_trial(self, source: np.ndarray, snr_db: float, timing_sigma_s: float,
                   rng: np.random.Generator) -> dict:
        signal = np.zeros(64, dtype=float)
        signal[0] = 1.0
        forward = simulate_forward_model(source, self.node_positions, signal,
                                         sound_speed_mps=self.sound_speed_mps,
                                         add_clock_noise=False, rng=rng, t0_s=1.0)
        arrivals = self._extract_arrivals(forward.audio, forward.sample_rates_hz, rng, snr_db, timing_sigma_s)
        try:
            if np.min(np.linalg.norm(np.asarray(self.node_positions) - source, axis=1)) < 1e-9:
                raise ValueError("source coincides with a node")
            solved = point.solve(self.node_positions, arrivals, "blast", temp_c=20.0,
                                 fixed_up_m=self.fixed_up_m,
                                 search_margin_m=max(self.outside_margin_m + 20.0, 50.0),
                                 grid_step_m=self.grid_step_m)
            estimate = np.array([solved["east_m"], solved["north_m"]], dtype=float)
            if not np.all(np.isfinite(estimate)) or solved.get("position_observable") is False:
                raise ValueError("solver returned an unobservable position")
            error_m, failure = float(np.linalg.norm(estimate - source)), False
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            solved, error_m, failure = {}, None, True
        nodes3 = np.c_[np.asarray(self.node_positions), np.zeros(len(self.node_positions))]
        geometry = TDoAEvaluationMetrics(sound_speed_mps=self.sound_speed_mps).gdop(
            np.r_[source, self.fixed_up_m], nodes3)
        return {"source_position_m": source.tolist(),
                "region": "inside" if self._inside(source) else "outside",
                "snr_db": float(snr_db), "timing_sigma_s": float(timing_sigma_s),
                "error_m": error_m, "gdop": geometry["gdop"],
                "solver_failed": failure, "solver": solved}

    def run(self) -> dict:
        """Execute the sweep and return structured metrics plus calibration curves."""
        rng = np.random.default_rng(self.seed)
        trials = []
        for snr in self.snr_db:
            for sigma in self.timing_sigma_s:
                for source in self.positions:
                    for _ in range(self.trials_per_position):
                        trials.append(self._run_trial(source, float(snr), float(sigma), rng))
        groups = []
        for snr in self.snr_db:
            for sigma in self.timing_sigma_s:
                selected = [r for r in trials if r["snr_db"] == float(snr) and r["timing_sigma_s"] == float(sigma)]
                valid = [r for r in selected if r["error_m"] is not None]
                errors = [r["error_m"] for r in valid]
                gdops = [r["gdop"] for r in valid if np.isfinite(r["gdop"])]
                error_arr, gdop_arr = np.asarray(errors), np.asarray(gdops)
                correlation = None
                if len(error_arr) > 1 and len(error_arr) == len(gdop_arr) and np.std(error_arr) and np.std(gdop_arr):
                    correlation = float(np.corrcoef(error_arr, gdop_arr)[0, 1])
                median_gdop = float(np.median(gdop_arr)) if gdop_arr.size else None
                groups.append({"snr_db": float(snr), "timing_sigma_s": float(sigma),
                               "samples": len(selected),
                               "rmse_m": float(np.sqrt(np.mean(error_arr ** 2))) if error_arr.size else None,
                               "p50_m": self._percentile(errors, 50),
                               "p90_m": self._percentile(errors, 90),
                               "p99_m": self._percentile(errors, 99),
                               "gdop_error_correlation": correlation,
                               "solver_failure_rate": float(sum(r["solver_failed"] for r in selected) / len(selected)) if selected else 1.0,
                               "median_gdop": median_gdop,
                               "predicted_1sigma_m": median_gdop * self.sound_speed_mps * float(sigma) if median_gdop is not None else None})
        return {"config": self.to_dict(), "metrics": groups,
                "calibration_curves": groups, "trials": trials}

    def to_dict(self) -> dict:
        return {"node_positions": np.asarray(self.node_positions, dtype=float).tolist(),
                "snr_db": [float(v) for v in self.snr_db],
                "timing_sigma_s": [float(v) for v in self.timing_sigma_s],
                "grid_step_m": self.grid_step_m, "outside_margin_m": self.outside_margin_m,
                "trials_per_position": self.trials_per_position, "seed": self.seed,
                "sound_speed_mps": self.sound_speed_mps, "fixed_up_m": self.fixed_up_m}

    def run_json(self, **kwargs) -> str:
        return json.dumps(self.run(), **kwargs)


__all__ = ["MonteCarloSweep"]
