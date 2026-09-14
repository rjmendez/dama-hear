"""Association stress-testing harness for multi-source acoustic scenarios.

Simulates multi-source bursts (simultaneous or rapid-fire impulses with dt in [5 ms, 100 ms]),
injects false positive clutter, dropped arrivals, and multipath echoes, feeds arrivals into
hear.backend.associate.associate(), and measures grouping precision, recall, split-event rate,
merged-event rate, and downstream solver success rate under varying clutter density.
"""
from __future__ import annotations

import itertools
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from hear.backend import associate as backend_associate
from hear.solve import point as point_solve
from hear.solve import shockwave as SW


def _to_xyz(p) -> np.ndarray:
    """Ensure a 2D or 3D position vector becomes a 3D float64 numpy array."""
    v = np.asarray(p, dtype=float).ravel()
    if v.size == 2:
        return np.array([v[0], v[1], 0.0], dtype=float)
    if v.size == 3:
        return v.astype(float)
    raise ValueError(f"position must be 2D or 3D, got shape {v.shape}")


class _SimpleSurvey:
    """Minimal duck-typed Survey wrapper for node positions."""

    def __init__(self, nodes: Any):
        if isinstance(nodes, dict):
            self._pos = {int(k): _to_xyz(v) for k, v in nodes.items()}
        elif isinstance(nodes, (list, tuple)):
            self._pos = {}
            for i, item in enumerate(nodes):
                if hasattr(item, "node_id") and hasattr(item, "position"):
                    self._pos[int(item.node_id)] = _to_xyz(item.position)
                elif hasattr(item, "position"):
                    self._pos[i + 1] = _to_xyz(item.position)
                else:
                    self._pos[i + 1] = _to_xyz(item)
        else:
            raise TypeError(f"unsupported nodes format: {type(nodes)}")

        self.ids = sorted(self._pos.keys())

    def __contains__(self, node_id: Any) -> bool:
        return int(node_id) in self._pos

    def position(self, node_id: int) -> np.ndarray:
        if int(node_id) not in self._pos:
            raise KeyError(f"node {node_id} not in survey")
        return self._pos[int(node_id)].copy()

    def diameter_m(self) -> float:
        if len(self._pos) < 2:
            return 0.0
        pts = list(self._pos.values())
        return max(float(np.linalg.norm(a - b)) for a, b in itertools.combinations(pts, 2))


def _normalize_survey(survey: Any) -> Any:
    if (
        hasattr(survey, "position")
        and hasattr(survey, "diameter_m")
        and callable(getattr(survey, "position"))
        and callable(getattr(survey, "diameter_m"))
    ):
        return survey
    return _SimpleSurvey(survey)


def simulate_multi_source_burst(
    num_sources: int = 3,
    dt_range: Tuple[float, float] = (0.005, 0.100),
    spatial_bounds: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]] = (
        (-100.0, 100.0),
        (-100.0, 100.0),
        (0.0, 10.0),
    ),
    t0_start: float = 100.0,
    seed: Optional[int] = 42,
) -> List[Dict[str, Any]]:
    """Generate multi-source burst configurations with delta_t in dt_range."""
    rng = np.random.default_rng(seed)
    sources = []
    curr_t = float(t0_start)

    (x_min, x_max), (y_min, y_max), (z_min, z_max) = spatial_bounds

    for i in range(num_sources):
        pos = np.array([
            float(rng.uniform(x_min, x_max)),
            float(rng.uniform(y_min, y_max)),
            float(rng.uniform(z_min, z_max)),
        ])
        sources.append({"source_id": i, "pos": pos, "t0_s": curr_t})

        if i < num_sources - 1:
            dt = float(rng.uniform(dt_range[0], dt_range[1]))
            curr_t += dt

    return sources


class AssociationStressTest:
    """Association stress-testing harness for multi-source acoustic scenarios."""

    def __init__(
        self,
        survey: Any,
        temp_c: float = 20.0,
        margin_s: float = 0.030,
        min_nodes: int = 3,
        source_class: str = "blast",
        fixed_up_m: Optional[float] = None,
        window_s: Optional[float] = None,
    ):
        self.survey = _normalize_survey(survey)
        self.temp_c = float(temp_c)
        self.margin_s = float(margin_s)
        self.min_nodes = int(min_nodes)
        self.source_class = source_class
        self.fixed_up_m = fixed_up_m
        self.window_s = window_s

    def generate_scenario(
        self,
        sources: Sequence[Dict[str, Any] | Tuple[Sequence[float], float]],
        clutter_rate_hz: float = 0.0,
        drop_rate: float = 0.0,
        multipath_rate: float = 0.0,
        multipath_delay_range: Tuple[float, float] = (0.010, 0.050),
        timing_noise_s: float = 0.0,
        seed: Optional[int] = 42,
    ) -> Dict[str, Any]:
        """Generate synthetic detection stream from multiple sources with injected noise/clutter."""
        rng = np.random.default_rng(seed)
        c = SW.sound_speed(self.temp_c)

        parsed_sources = []
        for idx, src in enumerate(sources):
            if isinstance(src, dict):
                pos = np.asarray(src["pos"] if "pos" in src else src["position"], dtype=float)
                t0 = float(src["t0"] if "t0" in src else src["t0_s"])
            else:
                pos = np.asarray(src[0], dtype=float)
                t0 = float(src[1])
            if pos.shape not in ((2,), (3,)):
                raise ValueError(f"source {idx} position must be a 2D or 3D vector")
            if pos.shape == (2,):
                pos = np.array([pos[0], pos[1], 0.0], dtype=float)
            parsed_sources.append({"source_id": idx, "pos": pos, "t0_s": t0})

        node_ids = (
            list(self.survey.ids)
            if hasattr(self.survey, "ids")
            else sorted(int(k) for k in self.survey._pos.keys())
        )
        raw_detections = []
        direct_arrival_count = 0

        min_t = min((s["t0_s"] for s in parsed_sources), default=100.0)
        max_t = max((s["t0_s"] for s in parsed_sources), default=101.0)

        for src in parsed_sources:
            s_id = src["source_id"]
            s_pos = src["pos"]
            s_t0 = src["t0_s"]

            for nid in node_ids:
                n_pos = _to_xyz(self.survey.position(nid))
                dist = float(np.linalg.norm(n_pos - s_pos))
                prop_delay = dist / c
                nominal_t = s_t0 + prop_delay

                if timing_noise_s > 0:
                    nominal_t += float(rng.normal(0.0, timing_noise_s))

                if nominal_t > max_t:
                    max_t = nominal_t

                # Drop check
                if drop_rate > 0 and rng.random() < drop_rate:
                    continue

                direct_arrival_count += 1
                raw_detections.append({
                    "node_id": int(nid),
                    "t_utc_s": nominal_t,
                    "onset_found": True,
                    "utc_trusted": True,
                    "tdoa_capable": True,
                    "timestamp_domain": "utc_gps_pps",
                    "_truth_source_id": s_id,
                    "_is_direct": True,
                    "_is_multipath": False,
                    "_is_clutter": False,
                })

                # Multipath check
                if multipath_rate > 0 and rng.random() < multipath_rate:
                    echo_delay = float(rng.uniform(multipath_delay_range[0], multipath_delay_range[1]))
                    raw_detections.append({
                        "node_id": int(nid),
                        "t_utc_s": nominal_t + echo_delay,
                        "onset_found": True,
                        "utc_trusted": True,
                        "tdoa_capable": True,
                        "timestamp_domain": "utc_gps_pps",
                        "_truth_source_id": s_id,
                        "_is_direct": False,
                        "_is_multipath": True,
                        "_is_clutter": False,
                    })

        # Inject false positive clutter onsets
        if clutter_rate_hz > 0:
            margin = 0.2
            span_s = max(0.5, (max_t - min_t) + 2 * margin)
            t_start = min_t - margin
            t_end = min_t - margin + span_s

            for nid in node_ids:
                n_clutter = rng.poisson(clutter_rate_hz * span_s)
                if n_clutter > 0:
                    clutter_times = rng.uniform(t_start, t_end, size=n_clutter)
                    for t_c in clutter_times:
                        raw_detections.append({
                            "node_id": int(nid),
                            "t_utc_s": float(t_c),
                            "onset_found": True,
                            "utc_trusted": True,
                            "tdoa_capable": True,
                            "timestamp_domain": "utc_gps_pps",
                            "_truth_source_id": None,
                            "_is_direct": False,
                            "_is_multipath": False,
                            "_is_clutter": True,
                        })

        # Assign sequence numbers per node
        node_dets: Dict[int, List[Dict]] = {int(nid): [] for nid in node_ids}
        for det in raw_detections:
            node_dets[int(det["node_id"])].append(det)

        final_detections = []
        for nid in node_ids:
            dets = node_dets[int(nid)]
            dets.sort(key=lambda d: float(d["t_utc_s"]))
            for seq, d in enumerate(dets):
                d["seq"] = seq % 256
                final_detections.append(d)

        final_detections.sort(key=lambda d: (float(d["t_utc_s"]), int(d["node_id"])))

        return {
            "detections": final_detections,
            "sources": parsed_sources,
            "direct_arrival_count": direct_arrival_count,
        }

    def run_association(self, detections: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Feed arrivals into backend_associate.associate()."""
        return backend_associate.associate(
            detections,
            self.survey,
            temp_c=self.temp_c,
            margin_s=self.margin_s,
            min_nodes=self.min_nodes,
            window_s=self.window_s,
        )

    def evaluate_grouping(
        self,
        assoc_result: Dict[str, Any],
        scenario_data: Dict[str, Any],
    ) -> Dict[str, float]:
        """Compute grouping precision, recall, split-event rate, and merged-event rate."""
        events = assoc_result.get("events", [])
        sources = scenario_data.get("sources", [])
        num_sources = len(sources)

        total_event_arrivals = 0
        total_correct_arrivals = 0

        source_direct_total: Dict[int, int] = {s["source_id"]: 0 for s in sources}
        source_max_in_event: Dict[int, int] = {s["source_id"]: 0 for s in sources}
        source_event_counts: Dict[int, int] = {s["source_id"]: 0 for s in sources}

        for det in scenario_data.get("detections", []):
            if det.get("_is_direct") and det.get("_truth_source_id") is not None:
                sid = det["_truth_source_id"]
                source_direct_total[sid] = source_direct_total.get(sid, 0) + 1

        merged_events_count = 0

        for ev in events:
            dets = ev.get("detections", [])
            total_event_arrivals += len(dets)

            direct_counts: Dict[int, int] = {}
            for d in dets:
                if d.get("_is_direct") and d.get("_truth_source_id") is not None:
                    sid = d["_truth_source_id"]
                    direct_counts[sid] = direct_counts.get(sid, 0) + 1

            distinct_sources = len(direct_counts)
            if distinct_sources >= 2:
                merged_events_count += 1

            if direct_counts:
                majority_sid, majority_count = max(direct_counts.items(), key=lambda x: x[1])
                total_correct_arrivals += majority_count

            for sid, count in direct_counts.items():
                source_event_counts[sid] += 1
                if count > source_max_in_event.get(sid, 0):
                    source_max_in_event[sid] = count

        precision = (
            float(total_correct_arrivals) / total_event_arrivals
            if total_event_arrivals > 0
            else 1.0
        )

        total_direct_all_sources = sum(source_direct_total.values())
        total_matched_in_primary = sum(source_max_in_event.values())
        recall = (
            float(total_matched_in_primary) / total_direct_all_sources
            if total_direct_all_sources > 0
            else 1.0
        )

        split_sources_count = sum(1 for sid, count in source_event_counts.items() if count > 1)
        split_event_rate = (
            float(split_sources_count) / num_sources if num_sources > 0 else 0.0
        )

        num_events = len(events)
        merged_event_rate = (
            float(merged_events_count) / num_events if num_events > 0 else 0.0
        )

        return {
            "grouping_precision": precision,
            "grouping_recall": recall,
            "split_event_rate": split_event_rate,
            "merged_event_rate": merged_event_rate,
            "num_events": num_events,
            "num_sources": num_sources,
        }

    def evaluate_solver(
        self,
        assoc_result: Dict[str, Any],
        scenario_data: Dict[str, Any],
        max_solver_error_m: float = 5.0,
    ) -> Dict[str, float]:
        """Evaluate downstream solver success rate for associated events."""
        events = assoc_result.get("events", [])
        sources_by_id = {s["source_id"]: s for s in scenario_data.get("sources", [])}

        if not events:
            return {
                "solver_success_rate": 0.0,
                "mean_position_error_m": np.nan,
                "solved_events_count": 0,
                "total_events_count": 0,
            }

        successful_solves = 0
        errors = []

        node_ids = (
            list(self.survey.ids)
            if hasattr(self.survey, "ids")
            else sorted(int(k) for k in self.survey._pos.keys())
        )
        node_positions = np.array([self.survey.position(nid) for nid in node_ids])
        vertical_spread = (
            float(np.ptp(node_positions[:, 2])) if node_positions.shape[1] == 3 else 0.0
        )
        use_fixed_up = (
            self.fixed_up_m if self.fixed_up_m is not None else (0.0 if vertical_spread < 0.5 else None)
        )

        for ev in events:
            dets = ev.get("detections", [])
            positions = [self.survey.position(int(d["node_id"])) for d in dets]
            arrivals = [float(d["t_utc_s"]) for d in dets]

            direct_counts: Dict[int, int] = {}
            for d in dets:
                if d.get("_is_direct") and d.get("_truth_source_id") is not None:
                    sid = d["_truth_source_id"]
                    direct_counts[sid] = direct_counts.get(sid, 0) + 1

            true_pos = None
            if direct_counts:
                majority_sid = max(direct_counts.items(), key=lambda x: x[1])[0]
                if majority_sid in sources_by_id:
                    true_pos = sources_by_id[majority_sid]["pos"]

            try:
                res = point_solve.solve(
                    positions,
                    arrivals,
                    source_class=self.source_class,
                    temp_c=self.temp_c,
                    fixed_up_m=use_fixed_up,
                )
                if res.get("position_observable") and res.get("east_m") is not None:
                    est_pos = np.array([res["east_m"], res["north_m"], res["up_m"] or 0.0])
                    if true_pos is not None:
                        err_m = float(np.linalg.norm(est_pos - true_pos))
                        errors.append(err_m)
                        if err_m <= max_solver_error_m:
                            successful_solves += 1
            except Exception:
                pass

        success_rate = float(successful_solves) / len(events)
        mean_err = float(np.mean(errors)) if errors else np.nan

        return {
            "solver_success_rate": success_rate,
            "mean_position_error_m": mean_err,
            "solved_events_count": successful_solves,
            "total_events_count": len(events),
        }

    def run_test(
        self,
        sources: Sequence[Dict[str, Any] | Tuple[Sequence[float], float]],
        clutter_rate_hz: float = 0.0,
        drop_rate: float = 0.0,
        multipath_rate: float = 0.0,
        multipath_delay_range: Tuple[float, float] = (0.010, 0.050),
        timing_noise_s: float = 0.001,
        seed: Optional[int] = 42,
        max_solver_error_m: float = 5.0,
    ) -> Dict[str, Any]:
        """Run complete scenario simulation, association, and evaluation."""
        scenario = self.generate_scenario(
            sources,
            clutter_rate_hz=clutter_rate_hz,
            drop_rate=drop_rate,
            multipath_rate=multipath_rate,
            multipath_delay_range=multipath_delay_range,
            timing_noise_s=timing_noise_s,
            seed=seed,
        )

        assoc = self.run_association(scenario["detections"])
        grouping = self.evaluate_grouping(assoc, scenario)
        solver = self.evaluate_solver(assoc, scenario, max_solver_error_m=max_solver_error_m)

        return {
            "scenario": scenario,
            "association": assoc,
            "metrics": {**grouping, **solver},
        }

    def benchmark_clutter_density(
        self,
        sources: Sequence[Dict[str, Any] | Tuple[Sequence[float], float]],
        clutter_rates: Sequence[float] = (0.0, 5.0, 20.0, 50.0, 100.0),
        num_trials: int = 5,
        drop_rate: float = 0.0,
        multipath_rate: float = 0.0,
        seed: int = 42,
        max_solver_error_m: float = 5.0,
    ) -> Dict[str, Any]:
        """Benchmark metrics across varying clutter densities."""
        results_by_rate = {}

        for rate in clutter_rates:
            trial_metrics = []
            for trial in range(num_trials):
                trial_seed = seed + int(rate * 100) + trial
                res = self.run_test(
                    sources,
                    clutter_rate_hz=rate,
                    drop_rate=drop_rate,
                    multipath_rate=multipath_rate,
                    seed=trial_seed,
                    max_solver_error_m=max_solver_error_m,
                )
                trial_metrics.append(res["metrics"])

            aggregated = {}
            keys = [
                "grouping_precision",
                "grouping_recall",
                "split_event_rate",
                "merged_event_rate",
                "solver_success_rate",
            ]
            for k in keys:
                vals = [m[k] for m in trial_metrics if not np.isnan(m[k])]
                aggregated[k + "_mean"] = float(np.mean(vals)) if vals else 0.0
                aggregated[k + "_std"] = float(np.std(vals)) if vals else 0.0

            results_by_rate[rate] = aggregated

        return {
            "clutter_rates": list(clutter_rates),
            "results_by_rate": results_by_rate,
        }


def format_benchmark_summary(benchmark_results: Dict[str, Any]) -> str:
    """Format clutter benchmark results into a readable table string."""
    headers = (
        "Clutter (Hz)",
        "Precision",
        "Recall",
        "Split Rate",
        "Merged Rate",
        "Solver Success",
    )
    rows = []

    for rate, metrics in benchmark_results.get("results_by_rate", {}).items():
        rows.append((
            f"{rate:g}",
            f"{metrics.get('grouping_precision_mean', 0.0):.3f}",
            f"{metrics.get('grouping_recall_mean', 0.0):.3f}",
            f"{metrics.get('split_event_rate_mean', 0.0):.3f}",
            f"{metrics.get('merged_event_rate_mean', 0.0):.3f}",
            f"{metrics.get('solver_success_rate_mean', 0.0):.3f}",
        ))

    widths = [
        max(len(str(h)), *(len(str(r[i])) for r in rows))
        for i, h in enumerate(headers)
    ]
    header_line = " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * w for w in widths)
    data_lines = [" | ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)) for r in rows]

    return "\n".join([header_line, sep_line] + data_lines)


__all__ = [
    "AssociationStressTest",
    "simulate_multi_source_burst",
    "format_benchmark_summary",
]
