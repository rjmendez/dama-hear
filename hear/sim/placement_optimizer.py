"""Constrained array placement search and GDOP surface mapping."""
from __future__ import annotations
import math
from typing import Any, Dict, Optional, Sequence, Tuple
import numpy as np
from hear.solve import placement

class PlacementOptimizer:
    """Map and optimize TDoA node geometry over (x_min, x_max, y_min, y_max)."""
    def __init__(self, perimeter: Sequence[float], *, grid_shape: Tuple[int, int] = (25, 25),
                 min_separation: float = 0.0, z_bounds: Optional[Tuple[float, float]] = None) -> None:
        if len(perimeter) != 4: raise ValueError("perimeter must be (x_min, x_max, y_min, y_max)")
        self.perimeter = tuple(float(v) for v in perimeter)
        xmin, xmax, ymin, ymax = self.perimeter
        if not (xmin < xmax and ymin < ymax): raise ValueError("perimeter bounds must be increasing")
        if len(grid_shape) != 2 or min(grid_shape) < 2: raise ValueError("grid_shape needs at least 2 samples per axis")
        self.grid_shape = tuple(int(v) for v in grid_shape)
        self.min_separation = float(min_separation)
        if self.min_separation < 0: raise ValueError("min_separation must be non-negative")
        self.z_bounds = None if z_bounds is None else tuple(float(v) for v in z_bounds)
        if self.z_bounds is not None and self.z_bounds[0] > self.z_bounds[1]: raise ValueError("z_bounds must be increasing")

    @staticmethod
    def _nodes(nodes):
        a = np.asarray(nodes, float)
        if a.ndim != 2 or a.shape[1] not in (2, 3): raise ValueError("nodes must have shape (N, 2) or (N, 3)")
        if len(a) < 3: raise ValueError("at least 3 nodes are required")
        if not np.all(np.isfinite(a)): raise ValueError("node positions must be finite")
        return a.copy()

    def _valid(self, a):
        xmin, xmax, ymin, ymax = self.perimeter
        if np.any(a[:, 0] < xmin) or np.any(a[:, 0] > xmax) or np.any(a[:, 1] < ymin) or np.any(a[:, 1] > ymax): return False
        if a.shape[1] == 3 and self.z_bounds is not None and (np.any(a[:, 2] < self.z_bounds[0]) or np.any(a[:, 2] > self.z_bounds[1])): return False
        if self.min_separation:
            d = np.linalg.norm(a[:, None, :] - a[None, :, :], axis=2)
            if not np.all(d[np.triu_indices(len(a), 1)] >= self.min_separation): return False
        return True

    def _check(self, a, dims):
        if dims not in (2, 3): raise ValueError("dimensions must be 2 or 3")
        if dims == 3 and len(a) < 4: raise ValueError("3D GDOP requires at least 4 nodes")
        if not self._valid(a): raise ValueError("nodes violate perimeter or minimum-separation constraints")

    def _grid(self):
        xmin, xmax, ymin, ymax = self.perimeter
        nx, ny = self.grid_shape
        return np.linspace(xmin, xmax, nx), np.linspace(ymin, ymax, ny)

    def heatmap(self, nodes, *, dimensions=None, source_z=0.0) -> Dict[str, Any]:
        """Return GDOP/HDOP surfaces, singular zones, and mean/p95 coverage statistics."""
        a = self._nodes(nodes); dims = int(dimensions or a.shape[1]); self._check(a, dims)
        xs, ys = self._grid(); shape = (len(ys), len(xs))
        gdop = np.full(shape, np.inf); hdop = np.full(shape, np.inf)
        vdop = np.full(shape, np.nan) if dims == 3 else None; singular = np.zeros(shape, bool)
        geom = a if dims == 3 else a[:, :2]
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                try:
                    r = placement.dop(geom, (x, y)) if dims == 2 else placement.dop3(geom, (x, y, float(source_z)))
                    gdop[j, i] = r["dop"] if dims == 2 else r["pdop"]
                    hdop[j, i] = r["dop"] if dims == 2 else r["hdop"]
                    if dims == 3: vdop[j, i] = r["vdop"]
                    singular[j, i] = not math.isfinite(gdop[j, i])
                except (ValueError, np.linalg.LinAlgError): singular[j, i] = True
        finite = gdop[np.isfinite(gdop)]
        stats = {"mean": float(np.mean(finite)) if finite.size else np.inf, "p95": float(np.percentile(finite, 95)) if finite.size else np.inf,
                 "median": float(np.median(finite)) if finite.size else np.inf, "finite_fraction": float(np.mean(np.isfinite(gdop))),
                 "singular_fraction": float(np.mean(singular))}
        out = {"x": xs, "y": ys, "gdop": gdop, "hdop": hdop, "singular": singular, "stats": stats, "mean_gdop": stats["mean"], "p95_gdop": stats["p95"], "dimensions": dims, "nodes": a}
        if vdop is not None: out["vdop"] = vdop
        return out

    map = heatmap

    def objective(self, nodes, *, dimensions=None):
        s = self.heatmap(nodes, dimensions=dimensions)["stats"]
        return {"mean_gdop": s["mean"], "p95_gdop": s["p95"], "objective": s["mean"] + s["p95"]}

    def optimize(self, initial_nodes, *, dimensions=None, iterations=100, step_fraction=0.2):
        """Minimize mean plus p95 GDOP with bounded coordinate search."""
        if iterations < 0 or step_fraction <= 0: raise ValueError("iterations must be non-negative and step_fraction positive")
        a = self._nodes(initial_nodes); dims = int(dimensions or a.shape[1]); self._check(a, dims)
        if dims == 2: a = a[:, :2]
        elif a.shape[1] == 2: a = np.column_stack((a, np.zeros(len(a))))
        best = a.copy(); score = self.objective(best, dimensions=dims)
        spans = [self.perimeter[1]-self.perimeter[0], self.perimeter[3]-self.perimeter[2]]
        spans += [(self.z_bounds[1]-self.z_bounds[0]) if self.z_bounds else max(spans)] if dims == 3 else []
        for k in range(int(iterations)):
            scale = step_fraction * (1.0-k/max(1, iterations))
            for n in range(len(best)):
                for axis, span in enumerate(spans):
                    for sign in (-1.0, 1.0):
                        trial = best.copy(); trial[n, axis] += sign*scale*span
                        if not self._valid(trial): continue
                        trial_score = self.objective(trial, dimensions=dims)
                        if trial_score["objective"] < score["objective"]: best, score = trial, trial_score
        return {"nodes": best, "initial_nodes": a, "objective": score, "iterations": int(iterations), "dimensions": dims}

    def vertical_observability(self, nodes, *, source_z=0.0, delta_z=1.0):
        """Flag coplanar/singular 3D zones and recommend symmetric height offsets."""
        a = self._nodes(nodes); a3 = a if a.shape[1] == 3 else np.column_stack((a, np.zeros(len(a))))
        if len(a3) < 4: raise ValueError("vertical observability requires at least 4 nodes")
        source = (float(a3[:, 0].mean()), float(a3[:, 1].mean()), float(source_z))
        plane = placement.coplanarity(a3); vertical = placement.vertical_observability(a3, source, delta_m=float(delta_z))
        span = max(self.perimeter[1]-self.perimeter[0], self.perimeter[3]-self.perimeter[2]); offset = max(self.min_separation, 0.1*span)
        if self.z_bounds is not None: offset = min(offset, (self.z_bounds[1]-self.z_bounds[0])/2.0)
        zones = self.heatmap(a3, dimensions=3)["singular"]
        return {"coplanar": bool(plane["coplanar"]), "planarity_rms_m": plane["planarity_rms_m"], "singular_zones": zones,
                "singular_fraction": float(np.mean(zones)), "observable": bool(vertical["observable"]),
                "mirror_ambiguous": bool(vertical["mirror_ambiguous"]), "vertical": vertical,
                "recommended_z_offsets_m": (-offset, offset) if offset else (0.0,),
                "recommendation": "raise and lower nodes by the recommended offsets" if plane["coplanar"] else "existing relief provides vertical geometry"}

__all__ = ["PlacementOptimizer"]
