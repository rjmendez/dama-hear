"""Array geometry analysis and placement planning."""
from __future__ import annotations
import math
from itertools import product
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
import numpy as np
from .solve import placement as PL

class ArrayGeometryOptimizer:
    """Analyze and optimize 2-D/3-D TDoA receiver geometries in local metres."""
    def __init__(self, timing_sigma_s=0.001, sound_speed_mps=343.0,
                 target_side_resolution_m=1.0, placement_resolution_m=5.0):
        if timing_sigma_s <= 0 or sound_speed_mps <= 0 or target_side_resolution_m <= 0 or placement_resolution_m <= 0:
            raise ValueError("optimizer parameters must be positive")
        self.timing_sigma_s = float(timing_sigma_s)
        self.sound_speed_mps = float(sound_speed_mps)
        self.target_side_resolution_m = float(target_side_resolution_m)
        self.placement_resolution_m = float(placement_resolution_m)

    @staticmethod
    def _point(point: Any) -> np.ndarray:
        if isinstance(point, Mapping):
            return np.array([float(next((point[k] for k in keys if k in point), 0.0))
                             for keys in (("e_m", "x"), ("n_m", "y"), ("u_m", "z"))])
        values = np.asarray(point, dtype=float).ravel()
        if values.size not in (2, 3):
            raise ValueError("coordinates need two or three values")
        return np.pad(values, (0, 3 - values.size))

    @classmethod
    def _receivers(cls, receivers):
        points = np.asarray([cls._point(p) for p in receivers], dtype=float)
        if points.ndim != 2 or points.shape[0] < 3 or not np.isfinite(points).all():
            raise ValueError("need at least three finite receivers")
        return points

    @staticmethod
    def _bounds(bounds):
        b = tuple(float(v) for v in bounds)
        if len(b) == 4:
            x0, y0, x1, y1 = b; z = None
        elif len(b) == 6:
            x0, y0, z0, x1, y1, z1 = b; z = (z0, z1)
        else:
            raise ValueError("bounds must have four or six values")
        if x1 <= x0 or y1 <= y0 or (z is not None and z1 <= z0):
            raise ValueError("bounds must have positive extent")
        return x0, y0, x1, y1, z

    def _covariance_2d(self, receivers, source):
        d = source[None, :2] - receivers[:, :2]
        r = np.linalg.norm(d, axis=1)
        if r.min() < 1e-9:
            return None
        G = d / r[:, None]
        M = np.eye(len(receivers)) - np.ones((len(receivers), len(receivers))) / len(receivers)
        F = G.T @ M @ G
        if np.linalg.matrix_rank(F, tol=1e-12) < 2 or np.linalg.cond(F) > 1e12:
            return None
        try:
            return np.linalg.inv(F) * (self.sound_speed_mps * self.timing_sigma_s) ** 2
        except np.linalg.LinAlgError:
            return None

    def classify_geometry(self, receivers):
        P = self._receivers(receivers)
        C = P[:, :2] - P[:, :2].mean(axis=0)
        sv = np.linalg.svd(C, compute_uv=False)
        linearity = float(sv[1] / sv[0]) if sv[0] else 0.0
        if linearity < 1e-9:
            label = "collinear"
        elif len(P) == 3:
            label = "triangle"
        else:
            xmin, ymin = P[:, :2].min(axis=0); xmax, ymax = P[:, :2].max(axis=0)
            edge = np.isclose(P[:, 0], xmin) | np.isclose(P[:, 0], xmax) | np.isclose(P[:, 1], ymin) | np.isclose(P[:, 1], ymax)
            radii = np.linalg.norm(C, axis=1)
            label = "star" if radii.min() < 0.35 * radii.max() and edge.sum() < len(P) else ("L-shape" if edge.sum() >= 3 and np.isclose(P[:, 0], xmin).any() and np.isclose(P[:, 1], ymin).any() else "general")
        return {"classification": label, "linearity": linearity, "n_receivers": int(len(P)),
                "horizontal_extent_m": (float(np.ptp(P[:, 0])), float(np.ptp(P[:, 1]))),
                "vertical_extent_m": float(np.ptp(P[:, 2]))}

    def evaluate_geometry(self, receivers, test_points):
        P = self._receivers(receivers); points = [self._point(p) for p in test_points]
        if not points: raise ValueError("test_points must not be empty")
        rows = []
        for point in points:
            d2 = PL.dop(P[:, :2], point[:2])
            d3 = PL.dop3(P, point) if len(P) >= 4 else {"hdop": np.inf, "vdop": np.inf, "pdop": np.inf, "singular": True}
            rows.append({"point": tuple(point), "gdop": float(d2["dop"]),
                         "hdop": float(d3["hdop"]) if np.isfinite(d3["hdop"]) else None,
                         "vdop": float(d3["vdop"]) if np.isfinite(d3["vdop"]) else None,
                         "pdop": float(d3["pdop"]) if np.isfinite(d3["pdop"]) else None,
                         "singular_2d": bool(d2["singular"]), "singular_3d": bool(d3["singular"])})
        finite = [r["gdop"] for r in rows if np.isfinite(r["gdop"])]
        return {**self.classify_geometry(P), "points": rows, "mean_gdop": float(np.mean(finite)) if finite else np.inf}

    def compute_gdop_grid(self, receivers, bounds, resolution=5.0):
        if resolution <= 0: raise ValueError("resolution must be positive")
        P = self._receivers(receivers); x0, y0, x1, y1, zb = self._bounds(bounds)
        xs = np.arange(x0, x1 + resolution * .5, resolution); ys = np.arange(y0, y1 + resolution * .5, resolution)
        z = float(np.mean(zb)) if zb else 0.0
        gdop = np.full((len(ys), len(xs)), np.inf); hdop = np.full_like(gdop, np.inf); vdop = np.full_like(gdop, np.inf); area = np.full_like(gdop, np.inf)
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                q = np.array([x, y, z]); d2 = PL.dop(P[:, :2], q[:2]); gdop[j, i] = d2["dop"]
                cov = self._covariance_2d(P, q)
                if cov is not None: area[j, i] = math.sqrt(max(float(np.linalg.det(cov)), 0.0))
                if len(P) >= 4:
                    d3 = PL.dop3(P, q); hdop[j, i], vdop[j, i] = d3["hdop"], d3["vdop"]
        fg, fa = gdop[np.isfinite(gdop)], area[np.isfinite(area)]
        return {"grid": gdop, "gdop": gdop, "hdop": hdop, "vdop": vdop, "area_gdop": area, "xs": xs, "ys": ys,
                "mean_gdop": float(np.mean(fg)) if fg.size else np.inf, "mean_area_gdop": float(np.mean(fa)) if fa.size else np.inf,
                "usable_fraction": float(np.mean(np.isfinite(gdop)))}

    def optimize_placement(self, existing, n_new, bounds):
        if n_new < 1: raise ValueError("n_new must be positive")
        x0, y0, x1, y1, zb = self._bounds(bounds); step = self.placement_resolution_m; z = float(np.mean(zb)) if zb else 0.0
        candidates = [np.array([x, y, z]) for x, y in product(np.arange(x0, x1 + step * .5, step), np.arange(y0, y1 + step * .5, step))]
        chosen = [self._point(p) for p in existing]
        for _ in range(n_new):
            best = None
            for candidate in candidates:
                if any(np.linalg.norm(candidate - p) < step * .5 for p in chosen): continue
                score = self.compute_gdop_grid(chosen + [candidate], bounds, step)["mean_area_gdop"]
                if best is None or score < best[0]: best = (score, candidate)
            if best is None: raise ValueError("bounds do not contain enough placement candidates")
            chosen.append(best[1])
        result = self.compute_gdop_grid(chosen, bounds, step)
        return {"placements": [tuple(float(v) for v in p) for p in chosen[len(existing):]], "receivers": [tuple(float(v) for v in p) for p in chosen],
                "mean_area_gdop": result["mean_area_gdop"], "grid": result}

    def minimum_off_axis_baseline(self, receivers, test_points, max_distance_m=100.0):
        P = self._receivers(receivers); center = P[:, :2].mean(axis=0); _, _, vt = np.linalg.svd(P[:, :2] - center, full_matrices=False); axis = vt[0]; normal = np.array([-axis[1], axis[0]])
        points = [self._point(p) for p in test_points]
        def meets(distance):
            added = np.r_[center + distance * normal, 0.0]
            if distance <= 1e-9:
                return False
            for p in points:
                augmented = np.vstack([P, added])
                cov = self._covariance_2d(augmented, p)
                if cov is None or math.sqrt(max(float(normal @ cov @ normal), 0.0)) > self.target_side_resolution_m:
                    return False
                mirror = p.copy(); off = float(np.dot(p[:2] - center, normal)); mirror[:2] = p[:2] - 2.0 * off * normal
                t1 = np.linalg.norm(p[None, :] - augmented, axis=1) / self.sound_speed_mps
                t2 = np.linalg.norm(mirror[None, :] - augmented, axis=1) / self.sound_speed_mps
                if np.max(np.abs((t1 - t1[0]) - (t2 - t2[0]))) < 1.96 * self.timing_sigma_s:
                    return False
            return True
        if not meets(max_distance_m): required = np.inf
        elif meets(0.0): required = 0.0
        else:
            lo, hi = 0.0, max_distance_m
            for _ in range(45):
                mid = (lo + hi) / 2.0
                if meets(mid): hi = mid
                else: lo = mid
            required = hi
        return {"minimum_off_axis_baseline_m": required, "recommended_sensor": tuple(float(v) for v in np.r_[center + required * normal, 0.0]) if np.isfinite(required) else None,
                "target_side_resolution_m": self.target_side_resolution_m, "axis_unit": tuple(axis), "normal_unit": tuple(normal)}

    def evaluate_mirror_resolvability(self, receivers, test_points):
        P = self._receivers(receivers); center = P[:, :2].mean(axis=0); _, _, vt = np.linalg.svd(P[:, :2] - center, full_matrices=False); axis = vt[0]; normal = np.array([-axis[1], axis[0]])
        rows = []
        for point in [self._point(p) for p in test_points]:
            offset = float(np.dot(point[:2] - center, normal)); mirror = point.copy(); mirror[:2] = point[:2] - 2 * offset * normal
            t1 = np.linalg.norm(point[None, :] - P, axis=1) / self.sound_speed_mps; t2 = np.linalg.norm(mirror[None, :] - P, axis=1) / self.sound_speed_mps; delta = (t1 - t1[0]) - (t2 - t2[0]); cov = self._covariance_2d(P, point)
            sigma = np.inf if cov is None else math.sqrt(max(float(normal @ cov @ normal), 0.0))
            rows.append({"point": tuple(point), "mirror_point": tuple(mirror), "mirror_tdoa_rms_s": float(np.sqrt(np.mean(delta ** 2))), "mirror_tdoa_max_ms": float(np.max(np.abs(delta)) * 1000), "side_crlb_sigma_m": sigma, "mirror_separation_m": abs(2 * offset), "resolvable": bool(np.isfinite(sigma) and sigma <= self.target_side_resolution_m and np.max(np.abs(delta)) >= 1.96 * self.timing_sigma_s)})
        baseline = self.minimum_off_axis_baseline(P, test_points)
        return {"classification": self.classify_geometry(P)["classification"], "off_axis_spread_m": float(np.ptp((P[:, :2] - center) @ normal)), "points": rows, "resolvable": all(r["resolvable"] for r in rows), "minimum_off_axis_baseline_m": baseline["minimum_off_axis_baseline_m"], "recommended_sensor": baseline["recommended_sensor"]}
