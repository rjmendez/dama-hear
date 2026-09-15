"""Deterministic adversarial arrival mutations for the TDoA association harness."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from .association_stress import AssociationStressTest


def mutate_arrivals(harness: AssociationStressTest,
                    sources: Sequence[Dict[str, Any] | Tuple[Sequence[float], float]], *,
                    clock_jumps_s: Optional[Dict[int, float]] = None,
                    bogus_hyperbola_rate: float = 0.0, multipath_rate: float = 0.0,
                    snr_db_range: Tuple[float, float] = (6.0, 35.0),
                    seed: Optional[int] = 42) -> Dict[str, Any]:
    """Return arrivals tagged with each deliberately hostile condition applied."""
    jumps = clock_jumps_s or {}
    if not 0.0 <= bogus_hyperbola_rate <= 1.0:
        raise ValueError("bogus_hyperbola_rate must be in [0, 1]")
    if snr_db_range[0] > snr_db_range[1] or any(not math.isfinite(float(v)) for v in jumps.values()):
        raise ValueError("invalid adversarial mutation parameters")
    scenario = harness.generate_scenario(sources, multipath_rate=multipath_rate, seed=seed)
    rng = np.random.default_rng(seed)
    for det in scenario["detections"]:
        jump = float(jumps.get(int(det["node_id"]), 0.0))
        det["t_utc_s"] += jump
        det.update(clock_jump_s=jump, snr_db=float(rng.uniform(*snr_db_range)),
                   _is_bogus_hyperbola=False)
    node_ids = sorted({int(d["node_id"]) for d in scenario["detections"]})
    for source in scenario["sources"]:
        if rng.random() < bogus_hyperbola_rate:
            for node_id in node_ids:
                scenario["detections"].append({
                    "node_id": node_id, "t_utc_s": source["t0_s"] + float(rng.uniform(-.2, .2)),
                    "onset_found": True, "utc_trusted": True, "tdoa_capable": True,
                    "timestamp_domain": "utc_gps_pps", "snr_db": float(rng.uniform(*snr_db_range)),
                    "clock_jump_s": 0.0, "_truth_source_id": None, "_is_direct": False,
                    "_is_multipath": False, "_is_clutter": False, "_is_bogus_hyperbola": True})
    per_node = {node_id: [] for node_id in node_ids}
    for det in scenario["detections"]:
        per_node[int(det["node_id"])].append(det)
    for dets in per_node.values():
        dets.sort(key=lambda d: float(d["t_utc_s"]))
        for seq, det in enumerate(dets):
            det["seq"] = seq % 256
    scenario["detections"].sort(key=lambda d: (float(d["t_utc_s"]), int(d["node_id"])))
    scenario["clock_jumps_s"] = dict(jumps)
    return scenario
