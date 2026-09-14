#!/usr/bin/env python3
"""Experimental clap-TDOA self-calibration for relative node geometry.

This is exploratory tooling, not a replacement for a surveyed array. It tries
to recover relative node positions, clap positions, and per-node clock bias
from clap arrival times alone. The result is only identifiable up to rigid
motion (translation, rotation, mirror reflection); if the speed of sound were
unknown too, scale would be ambiguous as well.

The solver is a gauge-fixed nonlinear least-squares fit. It is useful for
feasibility experiments and synthetic rehearsals, but measured geometry remains
the primary calibration path for production use.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import least_squares

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class GeometryEstimateError(RuntimeError):
    """The experimental self-calibration problem is malformed or unsolved."""


@dataclass(frozen=True)
class Observation:
    clap_id: str
    node_id: str
    timestamp_s: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Observation":
        clap_id = value.get("clap_id", value.get("event_id"))
        node_id = value.get("node_id", value.get("node"))
        timestamp_s = value.get("timestamp_s", value.get("timestamp"))
        if clap_id is None or node_id is None or timestamp_s is None:
            raise GeometryEstimateError("arrival needs clap_id, node_id and timestamp_s")
        try:
            return cls(str(clap_id), str(node_id), float(timestamp_s))
        except (TypeError, ValueError) as exc:
            raise GeometryEstimateError("arrival timestamp must be finite") from exc


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def load_observations(payload: Any) -> list[Observation]:
    entries = payload.get("observations", payload.get("arrivals")) if isinstance(payload, Mapping) else payload
    if not isinstance(entries, list):
        raise GeometryEstimateError("arrival data must be a list or contain observations")
    observations = [Observation.from_mapping(entry) for entry in entries]
    if not observations:
        raise GeometryEstimateError("arrival data is empty")
    if any(not math.isfinite(obs.timestamp_s) for obs in observations):
        raise GeometryEstimateError("arrival timestamps must be finite")
    return observations


def _grouped(observations: Iterable[Observation]) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for observation in observations:
        bucket = grouped.setdefault(observation.clap_id, {})
        if observation.node_id in bucket:
            raise GeometryEstimateError(
                "duplicate observation for clap %r node %r" % (observation.clap_id, observation.node_id)
            )
        bucket[observation.node_id] = observation.timestamp_s
    return grouped


def _minimum_full_coverage_claps(n_nodes: int, dimension: int) -> int | None:
    denom = n_nodes - (dimension + 1)
    if denom <= 0:
        return None
    rigid = dimension * (dimension + 1) // 2
    unknown = dimension * n_nodes - rigid + (n_nodes - 1)
    # Need K*(M-1) > dimension*K + unknown  ->  K*(M-1-dimension) > unknown.
    required = unknown / float(denom)
    return int(math.floor(required)) + 1


def problem_summary(observations: Sequence[Observation], *, dimension: int, sound_speed_mps: float) -> dict[str, Any]:
    if dimension not in (2, 3):
        raise GeometryEstimateError("dimension must be 2 or 3")
    grouped = _grouped(observations)
    node_ids = tuple(sorted({obs.node_id for obs in observations}))
    clap_ids = tuple(sorted(grouped))
    members_per_clap = {clap_id: len(grouped[clap_id]) for clap_id in clap_ids}
    if len(node_ids) < dimension + 1:
        raise GeometryEstimateError(
            "%dD self-calibration needs at least %d nodes to fix rigid-motion gauge"
            % (dimension, dimension + 1)
        )
    if any(count < 2 for count in members_per_clap.values()):
        bad = sorted(clap_id for clap_id, count in members_per_clap.items() if count < 2)
        raise GeometryEstimateError("every clap needs at least two node arrivals; bad claps: %s" % ", ".join(bad))

    rigid = dimension * (dimension + 1) // 2
    tdoa_equations = int(sum(count - 1 for count in members_per_clap.values()))
    node_geometry_dof = int(dimension * len(node_ids) - rigid)
    clap_geometry_dof = int(dimension * len(clap_ids))
    bias_dof = int(len(node_ids) - 1)
    unknown_dof = node_geometry_dof + clap_geometry_dof + bias_dof
    dof = tdoa_equations - unknown_dof
    min_claps = _minimum_full_coverage_claps(len(node_ids), dimension)
    notes = []
    if dof <= 0:
        notes.append("underdetermined by TDOA degrees of freedom")
    elif dof <= max(2, dimension):
        notes.append("only marginally overdetermined; expect poor conditioning")
    if min_claps is None:
        notes.append("%dD full self-calibration is impossible with only %d nodes, regardless of clap count" % (dimension, len(node_ids)))
    elif len(clap_ids) < min_claps and all(count == len(node_ids) for count in members_per_clap.values()):
        notes.append(
            "full-coverage %dD solve with %d nodes typically needs at least %d claps by count"
            % (dimension, len(node_ids), min_claps)
        )
    notes.append("sufficient spatial diversity is still required; count alone is not enough")
    return {
        "dimension": int(dimension),
        "sound_speed_mps": float(sound_speed_mps),
        "node_ids": list(node_ids),
        "clap_ids": list(clap_ids),
        "n_nodes": len(node_ids),
        "n_claps": len(clap_ids),
        "n_arrivals": len(observations),
        "members_per_clap": members_per_clap,
        "tdoa_equations": tdoa_equations,
        "rigid_motion_gauge_dof": rigid,
        "node_geometry_dof": node_geometry_dof,
        "clap_geometry_dof": clap_geometry_dof,
        "bias_dof": bias_dof,
        "unknown_dof": unknown_dof,
        "degrees_of_freedom": dof,
        "underdetermined": bool(dof <= 0),
        "minimum_full_coverage_claps": min_claps,
        "notes": notes,
    }


@dataclass(frozen=True)
class _Gauge:
    node_ids: tuple[str, ...]
    clap_ids: tuple[str, ...]
    node_layout: tuple[tuple[int, ...], ...]
    node_slices: tuple[slice, ...]
    clap_slice: slice
    t0_slice: slice
    bias_slice: slice
    n_params: int
    dimension: int
    node0: str

    @classmethod
    def build(cls, node_ids: Sequence[str], clap_ids: Sequence[str], dimension: int) -> "_Gauge":
        layout: list[tuple[int, ...]] = []
        slices: list[slice] = []
        offset = 0
        for node_index in range(len(node_ids)):
            if node_index == 0:
                coords: tuple[int, ...] = ()
            elif node_index <= dimension:
                coords = tuple(range(node_index))
            else:
                coords = tuple(range(dimension))
            layout.append(coords)
            slices.append(slice(offset, offset + len(coords)))
            offset += len(coords)
        clap_slice = slice(offset, offset + len(clap_ids) * dimension)
        offset = clap_slice.stop
        t0_slice = slice(offset, offset + len(clap_ids))
        offset = t0_slice.stop
        bias_slice = slice(offset, offset + len(node_ids) - 1)
        offset = bias_slice.stop
        return cls(
            node_ids=tuple(node_ids),
            clap_ids=tuple(clap_ids),
            node_layout=tuple(layout),
            node_slices=tuple(slices),
            clap_slice=clap_slice,
            t0_slice=t0_slice,
            bias_slice=bias_slice,
            n_params=offset,
            dimension=int(dimension),
            node0=str(node_ids[0]),
        )

    def unpack(self, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        nodes = np.zeros((len(self.node_ids), self.dimension), dtype=float)
        for node_index in range(1, len(self.node_ids)):
            coords = self.node_layout[node_index]
            if coords:
                nodes[node_index, list(coords)] = theta[self.node_slices[node_index]]
        claps = theta[self.clap_slice].reshape(len(self.clap_ids), self.dimension)
        biases = np.zeros(len(self.node_ids), dtype=float)
        biases[1:] = theta[self.bias_slice]
        return nodes, claps, biases

    def bounds(self, coordinate_limit_m: float, time_slack_s: float) -> tuple[np.ndarray, np.ndarray]:
        lower = np.full(self.n_params, -np.inf, dtype=float)
        upper = np.full(self.n_params, np.inf, dtype=float)
        coord = float(abs(coordinate_limit_m))
        lower[self.clap_slice] = -coord
        upper[self.clap_slice] = coord
        for node_index in range(1, len(self.node_ids)):
            sl = self.node_slices[node_index]
            if sl.stop == sl.start:
                continue
            lower[sl] = -coord
            upper[sl] = coord
            diag = sl.stop - 1
            lower[diag] = 1e-3
        lower[self.t0_slice] = -abs(time_slack_s)
        upper[self.t0_slice] = abs(time_slack_s)
        lower[self.bias_slice] = -abs(time_slack_s)
        upper[self.bias_slice] = abs(time_slack_s)
        return lower, upper


def _simplex_seed(scale_m: float, dimension: int, count: int) -> np.ndarray:
    nodes = np.zeros((count, dimension), dtype=float)
    for i in range(1, min(count, dimension + 1)):
        nodes[i, :i] = 0.35 * float(scale_m)
        nodes[i, i - 1] = float(scale_m)
    for i in range(dimension + 1, count):
        nodes[i] = 0.25 * float(scale_m)
    return nodes


def _common_bias_seed(
    grouped: Mapping[str, Mapping[str, float]], node_ids: Sequence[str], clap_ids: Sequence[str]
) -> np.ndarray:
    ref = str(node_ids[0])
    seed = np.zeros(len(node_ids), dtype=float)
    for node_index, node_id in enumerate(node_ids[1:], start=1):
        diffs = [
            float(grouped[clap_id][node_id] - grouped[clap_id][ref])
            for clap_id in clap_ids
            if ref in grouped[clap_id] and node_id in grouped[clap_id]
        ]
        seed[node_index] = float(np.median(diffs)) if diffs else 0.0
    return seed


def _pairwise_distance_summary(node_ids: Sequence[str], nodes: np.ndarray) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for i in range(len(node_ids)):
        for j in range(i + 1, len(node_ids)):
            pairs.append(
                {
                    "a": node_ids[i],
                    "b": node_ids[j],
                    "distance_m": float(np.linalg.norm(nodes[i] - nodes[j])),
                }
            )
    return pairs


def estimate_geometry(
    observations: Sequence[Observation],
    *,
    dimension: int = 2,
    sound_speed_mps: float = 343.0,
    restarts: int = 32,
    seed: int = 0,
    max_nfev: int = 5000,
    coordinate_limit_m: float = 100.0,
    initial_scale_m: float = 1.0,
    loss: str = "linear",
) -> dict[str, Any]:
    summary = problem_summary(observations, dimension=dimension, sound_speed_mps=sound_speed_mps)
    if summary["underdetermined"]:
        raise GeometryEstimateError(
            "under-determined by TDOA count in %dD: %d equations for %d unknown DoF"
            % (dimension, summary["tdoa_equations"], summary["unknown_dof"])
        )

    grouped = _grouped(observations)
    node_ids = tuple(summary["node_ids"])
    clap_ids = tuple(summary["clap_ids"])
    gauge = _Gauge.build(node_ids, clap_ids, dimension)
    node_index = {node_id: idx for idx, node_id in enumerate(node_ids)}
    clap_index = {clap_id: idx for idx, clap_id in enumerate(clap_ids)}
    rows = np.array(
        [
            (clap_index[obs.clap_id], node_index[obs.node_id], float(obs.timestamp_s))
            for obs in observations
        ],
        dtype=[("clap", int), ("node", int), ("time", float)],
    )
    time_origin = float(rows["time"].min())
    centered_times = rows["time"] - time_origin
    time_slack_s = max(
        2.0,
        float(np.max(centered_times) - np.min(centered_times)) + 1.0,
        float(abs(coordinate_limit_m)) / float(sound_speed_mps) + 1.0,
    )
    lower, upper = gauge.bounds(coordinate_limit_m=coordinate_limit_m, time_slack_s=time_slack_s)
    bias_seed = _common_bias_seed(grouped, node_ids, clap_ids)
    simplex = _simplex_seed(initial_scale_m, dimension, len(node_ids))
    rng = np.random.default_rng(seed)

    def residual(theta: np.ndarray) -> np.ndarray:
        nodes, claps, biases = gauge.unpack(theta)
        propagation = np.linalg.norm(claps[rows["clap"]] - nodes[rows["node"]], axis=1) / sound_speed_mps
        model = theta[gauge.t0_slice][rows["clap"]] + propagation + biases[rows["node"]]
        return centered_times - model

    restart_summaries: list[dict[str, Any]] = []
    candidates: list[tuple[float, Any, np.ndarray, np.ndarray, np.ndarray]] = []
    for restart in range(max(1, int(restarts))):
        theta0 = np.zeros(gauge.n_params, dtype=float)
        seed_nodes = simplex.copy()
        if len(node_ids) > dimension + 1:
            seed_nodes[dimension + 1 :] += rng.normal(0.0, 0.25 * initial_scale_m, seed_nodes[dimension + 1 :].shape)
        theta_nodes: list[float] = []
        for node_index_ in range(1, len(node_ids)):
            coords = gauge.node_layout[node_index_]
            if coords:
                theta_nodes.extend(float(seed_nodes[node_index_, axis]) for axis in coords)
        theta0[: gauge.clap_slice.start] = np.asarray(theta_nodes, dtype=float)
        clap_seed = rng.uniform(-0.75 * initial_scale_m, 0.75 * initial_scale_m, size=(len(clap_ids), dimension))
        theta0[gauge.clap_slice] = clap_seed.ravel()
        for clap_id in clap_ids:
            k = clap_index[clap_id]
            member_times = [grouped[clap_id][node_id] - bias_seed[node_index[node_id]] for node_id in grouped[clap_id]]
            theta0[gauge.t0_slice.start + k] = float(min(member_times) - time_origin - initial_scale_m / sound_speed_mps)
        theta0[gauge.bias_slice] = bias_seed[1:] + rng.normal(0.0, 0.01, size=len(node_ids) - 1)
        theta0 = np.clip(theta0, lower + 1e-9, upper - 1e-9)
        result = least_squares(
            residual,
            theta0,
            bounds=(lower, upper),
            method="trf",
            max_nfev=int(max_nfev),
            loss=str(loss),
        )
        nodes, claps, biases = gauge.unpack(np.asarray(result.x, dtype=float))
        rms = float(np.sqrt(np.mean(result.fun ** 2)))
        span = float(np.max(np.linalg.norm(nodes[:, None, :] - nodes[None, :, :], axis=2)))
        restart_summaries.append(
            {
                "restart": restart,
                "success": bool(result.success),
                "residual_rms_s": rms,
                "max_node_span_m": span,
                "message": str(getattr(result, "message", "")),
            }
        )
        candidates.append((rms, result, nodes, claps, biases))

    candidates.sort(key=lambda item: item[0])
    best_rms, best_fit, best_nodes, best_claps, best_biases = candidates[0]
    near_best = [item for item in candidates if item[0] <= best_rms + max(1e-6, 0.05 * max(best_rms, 1e-6))]
    pair_spread_m = 0.0
    if len(near_best) > 1:
        pair_vectors = []
        for _rms, _fit, nodes, _claps, _biases in near_best:
            pair_vectors.append(np.asarray([pair["distance_m"] for pair in _pairwise_distance_summary(node_ids, nodes)]))
        pair_spread_m = float(np.max(np.ptp(np.stack(pair_vectors), axis=0)))
    x_best = np.asarray(best_fit.x, dtype=float)
    at_lower = np.isfinite(lower) & np.isclose(x_best, lower, atol=1e-6, rtol=0.0)
    at_upper = np.isfinite(upper) & np.isclose(x_best, upper, atol=1e-6, rtol=0.0)

    output = {
        "schema": "hear.experimental_clap_geometry_selfcal.v1",
        "experimental": True,
        "caveats": [
            "Exploratory only: surveyed geometry remains the production calibration source of truth.",
            "Solutions are only unique up to translation, rotation and mirror reflection.",
            "Sufficiently diverse clap positions are required; near-degenerate layouts are poorly conditioned.",
        ],
        "input_summary": {
            "dimension": dimension,
            "sound_speed_mps": sound_speed_mps,
            "n_arrivals": len(observations),
            "n_nodes": len(node_ids),
            "n_claps": len(clap_ids),
            "reference_bias_node": gauge.node0,
        },
        "identifiability": summary,
        "solver": {
            "converged": bool(best_fit.success),
            "message": str(getattr(best_fit, "message", "")),
            "residual_rms_s": best_rms,
            "n_parameters": int(gauge.n_params),
            "n_observations": int(len(observations)),
            "absolute_time_degrees_of_freedom": int(len(observations) - gauge.n_params),
            "restarts": int(restarts),
            "best_restart": int(next(item["restart"] for item in restart_summaries if item["residual_rms_s"] == best_rms)),
            "near_best_solution_count": int(len(near_best)),
            "near_best_pairwise_distance_spread_m": pair_spread_m,
            "coordinate_limit_m": float(coordinate_limit_m),
            "parameters_at_bounds": int(np.count_nonzero(at_lower | at_upper)),
            "restart_summary": restart_summaries,
        },
        "nodes": {
            node_id: {
                "position_m": [float(v) for v in best_nodes[node_index[node_id]]],
                "bias_s": float(best_biases[node_index[node_id]]),
                "bias_us": float(best_biases[node_index[node_id]] * 1e6),
            }
            for node_id in node_ids
        },
        "claps": {
            clap_id: {
                "position_m": [float(v) for v in best_claps[clap_index[clap_id]]],
                "emission_time_s": float(best_fit.x[gauge.t0_slice.start + clap_index[clap_id]] + time_origin),
                "n_nodes": int(summary["members_per_clap"][clap_id]),
            }
            for clap_id in clap_ids
        },
        "node_pair_distances_m": _pairwise_distance_summary(node_ids, best_nodes),
    }
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arrivals", required=True, help="JSON arrivals file, or - for stdin")
    parser.add_argument("--output", default="-", help="Output JSON file, or - for stdout")
    parser.add_argument("--dimension", type=int, choices=(2, 3), default=2)
    parser.add_argument("--sound-speed-mps", type=float, default=343.0)
    parser.add_argument("--restarts", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-nfev", type=int, default=5000)
    parser.add_argument("--coordinate-limit-m", type=float, default=100.0)
    parser.add_argument("--initial-scale-m", type=float, default=1.0)
    parser.add_argument("--loss", choices=("linear", "soft_l1", "huber", "cauchy"), default="linear")
    args = parser.parse_args(argv)

    try:
        result = estimate_geometry(
            load_observations(_read_json(args.arrivals)),
            dimension=int(args.dimension),
            sound_speed_mps=float(args.sound_speed_mps),
            restarts=int(args.restarts),
            seed=int(args.seed),
            max_nfev=int(args.max_nfev),
            coordinate_limit_m=float(args.coordinate_limit_m),
            initial_scale_m=float(args.initial_scale_m),
            loss=str(args.loss),
        )
    except (GeometryEstimateError, OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(body)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")
        print(json.dumps({"output": str(output), "residual_rms_s": result["solver"]["residual_rms_s"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
