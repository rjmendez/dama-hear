#!/usr/bin/env python3
"""Calibrate node capture-path delays from near-field clap arrivals."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any, Mapping, Sequence
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from hear.sim import ClapCalibrationError, ClapCalibrator
DEFAULT_TOLERANCE_S = 30e-6

def _read_json(path: str) -> Any:
    if path == "-": return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as stream: return json.load(stream)

def _position(value: Mapping[str, Any]) -> Sequence[float]:
    if "position_m" in value: return tuple(float(item) for item in value["position_m"])
    for fields in (("e_m", "n_m", "u_m"), ("x_m", "y_m", "z_m")):
        if all(field in value for field in fields): return tuple(float(value[field]) for field in fields)
    raise ClapCalibrationError("node survey entry needs position_m or e_m/n_m/u_m")

def load_nodes(payload: Any) -> dict[str, Sequence[float]]:
    entries = payload.get("nodes") if isinstance(payload, Mapping) else payload
    if isinstance(entries, Mapping):
        return {str(node_id): _position(position if isinstance(position, Mapping) else {"position_m": position}) for node_id, position in entries.items()}
    if not isinstance(entries, list): raise ClapCalibrationError("node survey must contain a nodes list or mapping")
    nodes: dict[str, Sequence[float]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping): raise ClapCalibrationError("node survey entries must be objects")
        node_id = entry.get("node_id", entry.get("name", entry.get("id")))
        if node_id is None: raise ClapCalibrationError("node survey entry is missing node_id/name")
        nodes[str(node_id)] = _position(entry)
    return nodes

def load_observations(payload: Any) -> list[dict[str, Any]]:
    entries = payload.get("observations", payload.get("arrivals")) if isinstance(payload, Mapping) else payload
    if not isinstance(entries, list): raise ClapCalibrationError("arrival data must be a list or contain observations")
    observations = []
    for entry in entries:
        if not isinstance(entry, Mapping): raise ClapCalibrationError("arrival entries must be objects")
        clap_id, node_id, timestamp = entry.get("clap_id", entry.get("event_id")), entry.get("node_id", entry.get("node")), entry.get("timestamp_s", entry.get("timestamp"))
        if clap_id is None or node_id is None or timestamp is None: raise ClapCalibrationError("arrival needs clap_id, node_id, and timestamp_s")
        observations.append({"clap_id": clap_id, "node_id": node_id, "timestamp_s": timestamp})
    return observations

def build_profile(result: Any, *, reference_nodes: Sequence[str], tolerance_s: float) -> dict[str, Any]:
    nodes = {}
    for node_id, estimate in sorted(result.biases.items()):
        nodes[node_id] = {"path_bias_s": estimate.bias_s, "path_bias_us": estimate.bias_us, "sigma_b_s": estimate.sigma_b_s, "sigma_b_us": estimate.sigma_b_us, "status": "admissible" if result.is_admissible(node_id, tolerance_s) else "refused", "n_observations": estimate.n_events, "residual_rms_s": estimate.residual_rms_s, "reference": node_id in reference_nodes}
    return {"schema": "hear.calibrated_node_biases.v1", "reference_nodes": list(reference_nodes), "admissibility_tolerance_s": tolerance_s, "solver": result.as_dict(), "nodes": nodes}

def calibrate_claps(survey: Any, arrivals: Any, *, reference_nodes: Sequence[str] = ("xiao-s3-pps",), sound_speed_mps: float = 343.0, max_clap_radius_m: float = 1.0, clap_plane_z_m: float | None = None, tolerance_s: float = DEFAULT_TOLERANCE_S) -> dict[str, Any]:
    calibrator = ClapCalibrator(load_nodes(survey), reference_nodes=reference_nodes, sound_speed_mps=sound_speed_mps, max_clap_radius_m=max_clap_radius_m, clap_plane_z_m=clap_plane_z_m, admissibility_tolerance_s=tolerance_s)
    calibrator.ingest(load_observations(arrivals))
    result = calibrator.solve()
    if not result.converged: raise ClapCalibrationError("clap calibration did not converge: %s" % result.message)
    profile = build_profile(result, reference_nodes=tuple(reference_nodes), tolerance_s=tolerance_s)
    refused = [node_id for node_id, value in profile["nodes"].items() if value["status"] != "admissible"]
    if refused: raise ClapCalibrationError("calibration confidence gate refused: %s" % ", ".join(refused))
    return profile

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", required=True, help="JSON node survey, or - for stdin")
    parser.add_argument("--arrivals", required=True, help="JSON clap arrivals")
    parser.add_argument("--output", default="config/calibrated_node_biases.json")
    parser.add_argument("--reference-node", action="append", default=None)
    parser.add_argument("--sound-speed-mps", type=float, default=343.0)
    parser.add_argument("--max-clap-radius-m", type=float, default=1.0)
    parser.add_argument("--clap-plane-z-m", type=float)
    parser.add_argument("--tolerance-us", type=float, default=30.0)
    args = parser.parse_args(argv)
    references = tuple(args.reference_node or ("xiao-s3-pps",))
    try:
        profile = calibrate_claps(_read_json(args.survey), _read_json(args.arrivals), reference_nodes=references, sound_speed_mps=args.sound_speed_mps, max_clap_radius_m=args.max_clap_radius_m, clap_plane_z_m=args.clap_plane_z_m, tolerance_s=args.tolerance_us * 1e-6)
        output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ClapCalibrationError, ValueError) as exc: parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "nodes": profile["nodes"]}, sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
