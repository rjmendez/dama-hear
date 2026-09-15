"""Spatial checks backed by durable Loci map and anchor memory.

Loci is deliberately represented as data, rather than imported as a runtime
dependency. A worker recalls a map/anchor record from Loci and passes its JSON
payload to :meth:`LociSpatialMemory.from_dict`; localization remains available
when the memory service is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np

from .solve import shockwave as SW


@dataclass(frozen=True)
class MapBounds:
    """Inclusive ENU map limits recalled from a Loci site-memory record."""

    east_m: tuple[float, float]
    north_m: tuple[float, float]
    up_m: Optional[tuple[float, float]] = None

    def contains(self, position: Sequence[float]) -> bool:
        p = np.asarray(position, dtype=float)
        if p.shape != (3,) or not np.all(np.isfinite(p)):
            return False
        if not (self.east_m[0] <= p[0] <= self.east_m[1]
                and self.north_m[0] <= p[1] <= self.north_m[1]):
            return False
        return self.up_m is None or self.up_m[0] <= p[2] <= self.up_m[1]


@dataclass
class LociSpatialMemory:
    """Validated subset of a recalled Loci map-memory record."""

    bounds: Optional[MapBounds] = None
    anchors: Mapping[Any, Sequence[float]] = field(default_factory=dict)
    anchor_tolerance_m: float = 5.0
    arrival_slack_s: float = 0.005

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> "LociSpatialMemory":
        bounds_record = record.get("bounds")
        bounds = None
        if bounds_record is not None:
            try:
                east = tuple(float(v) for v in bounds_record["east_m"])
                north = tuple(float(v) for v in bounds_record["north_m"])
                up_value = bounds_record.get("up_m")
                up = None if up_value is None else tuple(float(v) for v in up_value)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Loci bounds need east_m/north_m [min, max] pairs") from exc
            if len(east) != 2 or len(north) != 2 or (up is not None and len(up) != 2):
                raise ValueError("Loci bounds must contain exactly two limits per axis")
            if east[0] > east[1] or north[0] > north[1] or (up is not None and up[0] > up[1]):
                raise ValueError("Loci bounds minimum exceeds maximum")
            bounds = MapBounds(east, north, up)
        tolerance = float(record.get("anchor_tolerance_m", 5.0))
        slack = float(record.get("arrival_slack_s", 0.005))
        if not math.isfinite(tolerance) or tolerance < 0 or not math.isfinite(slack) or slack < 0:
            raise ValueError("Loci anchor_tolerance_m and arrival_slack_s must be non-negative")
        return cls(bounds=bounds, anchors=record.get("anchors", {}),
                   anchor_tolerance_m=tolerance, arrival_slack_s=slack)

    def rogue_nodes(self, positions: Mapping[Any, Sequence[float]]) -> Dict[Any, Dict[str, float]]:
        """Return surveyed nodes that materially disagree with durable anchors."""
        rogues: Dict[Any, Dict[str, float]] = {}
        for node_id, actual in positions.items():
            expected = self.anchors.get(node_id, self.anchors.get(str(node_id)))
            if expected is None:
                continue
            actual_xyz = np.asarray(actual, dtype=float)
            expected_xyz = np.asarray(expected, dtype=float)
            if actual_xyz.shape != (3,) or expected_xyz.shape != (3,):
                raise ValueError("Loci anchor positions must be finite ENU triples")
            drift = float(np.linalg.norm(actual_xyz - expected_xyz))
            if not math.isfinite(drift):
                raise ValueError("Loci anchor positions must be finite ENU triples")
            if drift > self.anchor_tolerance_m:
                rogues[node_id] = {"drift_m": drift, "limit_m": self.anchor_tolerance_m}
        return rogues

    def filter_arrivals(self, node_ids: Sequence[Any], arrivals: Sequence[float],
                        positions: Sequence[Sequence[float]], temp_c: float) -> Dict[str, Any]:
        """Isolate timestamps that violate pairwise speed-of-sound limits.

        A one-second PPS-label jump disagrees with every healthy receiver, while
        healthy-to-healthy pairs remain feasible. Repeatedly removing the node
        with the most incompatible pairs preserves the largest feasible set.
        """
        if not (len(node_ids) == len(arrivals) == len(positions)):
            raise ValueError("node_ids, arrivals, and positions must have equal length")
        c = SW.sound_speed(temp_c)
        values = np.asarray(arrivals, dtype=float)
        xyz = np.asarray(positions, dtype=float)
        if values.ndim != 1 or xyz.shape != (len(node_ids), 3) or not np.all(np.isfinite(values)):
            raise ValueError("arrivals must be finite and positions must be ENU triples")

        remaining = list(range(len(node_ids)))
        rejected = []
        while len(remaining) >= 3:
            counts = {i: 0 for i in remaining}
            for offset, left in enumerate(remaining):
                for right in remaining[offset + 1:]:
                    limit = float(np.linalg.norm(xyz[left] - xyz[right])) / c + self.arrival_slack_s
                    if abs(values[left] - values[right]) > limit:
                        counts[left] += 1
                        counts[right] += 1
            worst = max(counts.values(), default=0)
            if worst == 0:
                break
            candidates = [i for i, count in counts.items() if count == worst]
            # Timing alone cannot identify a rogue node when the maximum is tied.
            if len(candidates) != 1:
                break
            bad = candidates[0]
            rejected.append({"node_id": node_ids[bad], "reason": "loci_speed_of_sound_incompatible",
                             "violating_pairs": worst})
            remaining.remove(bad)
        return {"indices": remaining, "rejected": rejected}

    def accepts_position(self, position: Sequence[float]) -> bool:
        return self.bounds is None or self.bounds.contains(position)
