"""Metrics emitted by the TDoA simulation harness."""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from typing import Sequence
import numpy as np
from hear.solve import placement

@dataclass(frozen=True)
class GateCompliance:
    non_admissible_rejection_rate: float
    non_admissible_false_accept_rate: float

class TDoAEvaluationMetrics:
    def __init__(self, sound_speed_mps=343.0): self.sound_speed_mps = float(sound_speed_mps)
    def spatial_errors(self, estimated, truth, nodes=None):
        a, b = np.asarray(estimated, float), np.asarray(truth, float)
        a3, b3 = np.pad(a, (0, 3-len(a))), np.pad(b, (0, 3-len(b)))
        horizontal = float(np.linalg.norm(a3[:2]-b3[:2]))
        result = {"error_2d_m": horizontal, "error_3d_m": float(np.linalg.norm(a3-b3)),
                  "radial_error_m": float(np.linalg.norm(a3)-np.linalg.norm(b3))}
        result["bearing_error_deg"] = float(abs(math.degrees(math.atan2(a3[1], a3[0])-math.atan2(b3[1], b3[0])))) if np.linalg.norm(a3[:2]) and np.linalg.norm(b3[:2]) else 0.0
        return result
    def timing_residuals(self, arrivals, estimate, nodes):
        nodes = np.asarray(nodes, float); estimate = np.asarray(estimate, float)
        estimate = np.pad(estimate, (0, nodes.shape[1]-len(estimate)))
        expected = np.linalg.norm(nodes-estimate, axis=1)/self.sound_speed_mps
        observed = np.asarray(arrivals, float); residual = (observed-observed[0])-(expected-expected[0])
        return {"residuals_s": residual}
    def gdop(self, source, nodes):
        nodes = np.asarray(nodes, float); source = np.asarray(source, float)
        if len(nodes) < 3: return {"gdop": np.inf, "hdop": np.inf}
        try: horizontal = placement.dop(nodes, source[:2])['dop']
        except (ValueError, IndexError): horizontal = np.inf
        if nodes.shape[1] < 3 or len(nodes) < 4: return {"gdop": float(horizontal), "hdop": float(horizontal)}
        value = placement.dop3(nodes, source)["pdop"]
        if not np.isfinite(value): value = horizontal
        return {"gdop": float(value), "hdop": float(horizontal)}
    def confidence_check(self, estimate, truth, covariance, sigma=2.0):
        delta = np.asarray(estimate, float)-np.asarray(truth, float); cov=np.asarray(covariance,float)
        return {"inside": bool(delta @ np.linalg.pinv(cov) @ delta <= sigma*sigma)}
    @staticmethod
    def node_gate_compliance(arrivals):
        rejected = [x for x in arrivals if not x["admissible"]]
        return GateCompliance(sum(not x["accepted"] for x in rejected)/len(rejected) if rejected else 1.0,
                              sum(x["accepted"] for x in rejected)/len(rejected) if rejected else 0.0)
    def evaluate(self, estimated_position, true_position, node_positions, arrivals=None):
        result = self.spatial_errors(estimated_position, true_position, node_positions)
        result.update(self.gdop(np.asarray(true_position), node_positions)); result["scenario"] = "simulation"
        return result

class TDoAReport:
    def __init__(self, runs): self.runs = runs
    def to_json(self): return json.dumps({"runs": self.runs})
    def summary_table(self): return "scenario\n" + "\n".join(str(name) for name in self.runs)

def position_error(estimate, truth):
    if estimate is None or truth is None: return None
    return float(np.linalg.norm(np.asarray(estimate,float)-np.asarray(truth,float)))

def _json_safe(value):
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def report(nodes, source_pos, arrivals, solve_result):
    estimate = [solve_result.get("east_m"), solve_result.get("north_m"), solve_result.get("up_m")]
    if any(value is None for value in estimate): estimate = None
    geometry = TDoAEvaluationMetrics().gdop(source_pos, [node.position for node in nodes])
    payload = {"position_error_m": position_error(estimate, source_pos), "estimate_position": estimate,
               "truth_position": [float(v) for v in source_pos], "arrival_count": len(arrivals),
               "residual_rms_ms": solve_result.get("rms_residual_ms"), **geometry, "solver": solve_result}
    return _json_safe(payload)


@dataclass(frozen=True)
class Evaluation:
    metrics: dict

NodeGateMetrics = GateCompliance

def format_report(runs):
    return TDoAReport(runs).summary_table()

# Structured evaluation/report API.
from dataclasses import asdict, field
from typing import Any, Iterable, Mapping

@dataclass(frozen=True)
class NodeGateMetrics:
    admissible_true_arrivals: int
    admissible_accepted: int
    admissible_rejected: int
    non_admissible_true_arrivals: int
    non_admissible_accepted: int
    non_admissible_rejected: int
    non_admissible_rejection_rate: float
    non_admissible_false_accept_rate: float

@dataclass(frozen=True)
class Evaluation:
    spatial: Mapping[str, Any]
    timing: Mapping[str, Any]
    geometry: Mapping[str, Any]
    confidence: Mapping[str, Any]
    node_gate: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    def to_dict(self):
        def safe(v):
            if isinstance(v, np.ndarray): return [safe(x) for x in v.tolist()]
            if isinstance(v, (np.floating, np.integer, np.bool_)): return v.item()
            if isinstance(v, Mapping): return {str(k): safe(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)): return [safe(x) for x in v]
            if isinstance(v, float) and not math.isfinite(v): return None
            return v
        return safe(asdict(self))


def _metrics_evaluate(self, *, estimated_position, true_position, node_positions, arrivals=None, covariance=None, gate_arrivals=None, confidence=0.95, metadata=None):
    timing = self.timing_residuals(arrivals, estimated_position, node_positions) if arrivals is not None else {}
    ci = self.confidence_check(estimated_position, true_position, covariance, confidence) if covariance is not None else {}
    gate = asdict(self.node_gate_compliance(gate_arrivals)) if gate_arrivals is not None else {}
    return Evaluation(self.spatial_errors(estimated_position, true_position, node_positions), timing, self.gdop(true_position, node_positions), ci, gate, metadata or {})
TDoAEvaluationMetrics.evaluate = _metrics_evaluate

class TDoAReport:
    def __init__(self, runs): self.runs = dict(runs)
    def to_dict(self): return {"runs": {k: v.to_dict() if isinstance(v, Evaluation) else v for k, v in self.runs.items()}}
    def to_json(self, **kwargs): return json.dumps(self.to_dict(), **kwargs)
    def summary_table(self):
        headers = ("scenario", "2D err (m)", "3D err (m)", "bearing (deg)", "RMS residual (ms)", "GDOP", "CI")
        rows = []
        for name, run in self.runs.items():
            d = run.to_dict() if isinstance(run, Evaluation) else run
            s, t, g, c = (d.get(k, {}) for k in ("spatial", "timing", "geometry", "confidence"))
            rows.append((name, s.get("error_2d_m", "-"), s.get("error_3d_m", "-"), s.get("bearing_error_deg", "-"), t.get("rms_s", 0) * 1000 if "rms_s" in t else "-", g.get("gdop", "-"), c.get("inside", "-")))
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
        return "\n".join([" | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)), "-+-".join("-" * w for w in widths)] + [" | ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)) for r in rows])

def format_report(runs, *, json_output=False):
    report_obj = TDoAReport(runs)
    return report_obj.to_json(indent=2, sort_keys=True) if json_output else report_obj.summary_table()

# Numerical implementations below keep the public compatibility helpers above while providing
# the centroid-based and covariance-aware validation semantics used by the simulation harness.
def _spatial_errors(self, estimated_position, true_position, node_positions):
    estimate, truth, nodes = np.asarray(estimated_position, float), np.asarray(true_position, float), np.asarray(node_positions, float)
    if estimate.ndim != 1 or truth.shape != estimate.shape or nodes.ndim != 2 or nodes.shape[1] != estimate.size:
        raise ValueError("positions must have matching 2D or 3D shapes")
    centroid = nodes.mean(axis=0); delta = estimate - truth
    er, tr = np.linalg.norm(estimate - centroid), np.linalg.norm(truth - centroid)
    bearing = 0.0 if er == 0 or tr == 0 else float(np.arccos(np.clip(np.dot(estimate-centroid, truth-centroid)/(er*tr), -1, 1)))
    return {"error_2d_m": float(np.linalg.norm(delta[:2])), "error_3d_m": float(np.linalg.norm(delta)), "radial_error_m": float(er-tr), "radial_error_abs_m": float(abs(er-tr)), "bearing_error_rad": bearing, "bearing_error_deg": math.degrees(bearing)}

def _timing_residuals(self, arrivals, estimated_position, node_positions):
    times, nodes, estimate = np.asarray(arrivals, float), np.asarray(node_positions, float), np.asarray(estimated_position, float)
    if times.ndim != 1 or nodes.ndim != 2 or len(times) != len(nodes) or estimate.shape != (nodes.shape[1],):
        raise ValueError("arrivals and geometry have incompatible shapes")
    distances = np.linalg.norm(nodes-estimate, axis=1)
    values = np.asarray([(times[i]-times[0])-(distances[i]-distances[0])/self.sound_speed_mps for i in range(1, len(times))])
    return {"reference_node": 0, "residuals_s": values, "residuals_ms": values*1000, "rms_s": float(np.sqrt(np.mean(values**2))) if len(values) else 0.0, "max_abs_s": float(np.max(np.abs(values))) if len(values) else 0.0}

def _gdop(self, true_position, node_positions):
    truth, nodes = np.asarray(true_position, float), np.asarray(node_positions, float)
    if nodes.ndim != 2 or nodes.shape[0] < 2:
        raise ValueError("need >= 2 nodes")
    if nodes.shape[0] < 3:
        return {"gdop": float("inf"), "hdop": float("inf"), "geometry_matrix": None}
    if placement.linearity(nodes) < placement.COLLINEAR_LINEARITY:
        return {"gdop": float("inf"), "hdop": float("inf"), "geometry_matrix": None}
    if nodes.shape[1] < 3:
        try:
            dop_value = float(placement.dop(nodes, truth[:2])["dop"])
        except (ValueError, IndexError, np.linalg.LinAlgError):
            dop_value = float("inf")
        return {"gdop": dop_value, "hdop": dop_value, "geometry_matrix": None}
    if np.allclose(nodes[:, 2], nodes[0, 2]):
        try:
            dop_value = float(placement.dop(nodes, truth[:2])["dop"])
        except (ValueError, IndexError, np.linalg.LinAlgError):
            dop_value = float("inf")
        return {"gdop": dop_value, "hdop": dop_value, "geometry_matrix": None}
    distances = np.linalg.norm(nodes-truth, axis=1)
    if np.any(distances == 0): raise ValueError("true position cannot coincide with a node")
    H = ((truth-nodes)/distances[:, None])[1:] - ((truth-nodes)/distances[:, None])[0]
    def dop(matrix):
        try: return float(np.sqrt(np.trace(np.linalg.inv(matrix.T @ matrix))))
        except np.linalg.LinAlgError: return float("inf")
    return {"gdop": dop(H), "hdop": dop(H[:, :2]), "geometry_matrix": H}

def _chi_square_quantile(confidence, dimensions):
    from scipy.stats import chi2
    return float(chi2.ppf(confidence, dimensions))

def _confidence_check(self, true_position, estimated_position, covariance, confidence=0.95, sigma=None):
    truth, estimate, cov = np.asarray(true_position,float), np.asarray(estimated_position,float), np.asarray(covariance,float)
    if cov.shape != (len(truth), len(truth)): raise ValueError("covariance shape does not match position")
    try: mahal = float((truth-estimate) @ np.linalg.solve(cov, truth-estimate))
    except np.linalg.LinAlgError as exc: raise ValueError("covariance must be invertible") from exc
    threshold = float(sigma*sigma) if sigma is not None else _chi_square_quantile(confidence, len(truth))
    return {"inside": bool(mahal <= threshold), "mahalanobis_sq": mahal, "threshold": threshold, "confidence": confidence, "dimensions": len(truth)}

def _node_gate_compliance(arrivals):
    counts = {(True,True):0,(True,False):0,(False,True):0,(False,False):0}
    for row in arrivals: counts[(bool(row["admissible"]), bool(row["accepted"]))] += 1
    adm, non = counts[(True,True)]+counts[(True,False)], counts[(False,True)]+counts[(False,False)]
    return NodeGateMetrics(adm, counts[(True,True)], counts[(True,False)], non, counts[(False,True)], counts[(False,False)], counts[(False,False)]/non if non else 0.0, counts[(False,True)]/non if non else 0.0)
TDoAEvaluationMetrics.spatial_errors = _spatial_errors
TDoAEvaluationMetrics.timing_residuals = _timing_residuals
TDoAEvaluationMetrics.gdop = _gdop
TDoAEvaluationMetrics.confidence_check = _confidence_check
TDoAEvaluationMetrics.node_gate_compliance = staticmethod(_node_gate_compliance)
