#!/usr/bin/env python3
"""Calibrate node capture-path delays from near-field clap arrivals."""
from __future__ import annotations
import argparse, json, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from hear.sim import ClapCalibrationError, ClapCalibrator
from hear.node.telemetry import sound_speed as sound_speed_from_temp
DEFAULT_TOLERANCE_S = 30e-6

def _read_json(path: str) -> Any:
    if path == "-": return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as stream: return json.load(stream)

def _position(value: Mapping[str, Any]) -> Sequence[float]:
    if "position_m" in value: return tuple(float(item) for item in value["position_m"])
    for fields in (("e_m", "n_m", "u_m"), ("x_m", "y_m", "z_m")):
        if all(field in value for field in fields): return tuple(float(value[field]) for field in fields)
    raise ClapCalibrationError("node survey entry needs position_m or e_m/n_m/u_m")

@dataclass(frozen=True)
class _LoadedSurvey:
    nodes: dict[str, Sequence[float]]
    aliases: dict[str, str]

def _node_aliases(entry: Mapping[str, Any], fallback: Any = None) -> tuple[str, ...]:
    aliases = []
    for value in (entry.get("name"), entry.get("node_id"), entry.get("id"), fallback):
        if value is None: continue
        alias = str(value)
        if alias not in aliases: aliases.append(alias)
    return tuple(aliases)

def load_survey(payload: Any) -> _LoadedSurvey:
    entries = payload.get("nodes") if isinstance(payload, Mapping) else payload
    nodes: dict[str, Sequence[float]] = {}
    aliases: dict[str, str] = {}
    if isinstance(entries, Mapping):
        iterable = entries.items()
    elif isinstance(entries, list):
        iterable = enumerate(entries)
    else:
        raise ClapCalibrationError("node survey must contain a nodes list or mapping")
    for key, entry in iterable:
        if isinstance(entries, Mapping):
            if isinstance(entry, Mapping):
                alias_values = _node_aliases(entry, fallback=key)
                position = _position(entry)
            else:
                alias_values = (str(key),)
                position = _position({"position_m": entry})
        else:
            if not isinstance(entry, Mapping): raise ClapCalibrationError("node survey entries must be objects")
            alias_values = _node_aliases(entry)
            if not alias_values: raise ClapCalibrationError("node survey entry is missing node_id/name")
            position = _position(entry)
        canonical = alias_values[0]
        if canonical in nodes: raise ClapCalibrationError("duplicate surveyed node %r" % canonical)
        nodes[canonical] = position
        for alias in alias_values:
            owner = aliases.get(alias)
            if owner is not None and owner != canonical:
                raise ClapCalibrationError("survey alias %r names both %r and %r" % (alias, owner, canonical))
            aliases[alias] = canonical
    return _LoadedSurvey(nodes=nodes, aliases=aliases)

def load_nodes(payload: Any) -> dict[str, Sequence[float]]:
    return load_survey(payload).nodes

def resolve_reference_nodes(payload: Any, requested: Sequence[str]) -> tuple[_LoadedSurvey, tuple[str, ...]]:
    survey = load_survey(payload)
    if not requested: raise ClapCalibrationError("reference_nodes must be non-empty")
    resolved = []
    for token in requested:
        canonical = survey.aliases.get(str(token))
        if canonical is None:
            raise ClapCalibrationError(
                "reference node %r does not match the survey; pass node names/ids explicitly" % token
            )
        if canonical not in resolved: resolved.append(canonical)
    return survey, tuple(resolved)

def load_observations(payload: Any, aliases: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    entries = payload.get("observations", payload.get("arrivals")) if isinstance(payload, Mapping) else payload
    if not isinstance(entries, list): raise ClapCalibrationError("arrival data must be a list or contain observations")
    observations = []
    for entry in entries:
        if not isinstance(entry, Mapping): raise ClapCalibrationError("arrival entries must be objects")
        clap_id, node_id, timestamp = entry.get("clap_id", entry.get("event_id")), entry.get("node_id", entry.get("node")), entry.get("timestamp_s", entry.get("timestamp"))
        if clap_id is None or node_id is None or timestamp is None: raise ClapCalibrationError("arrival needs clap_id, node_id, and timestamp_s")
        node_id = aliases.get(str(node_id), str(node_id)) if aliases is not None else str(node_id)
        observations.append({"clap_id": clap_id, "node_id": node_id, "timestamp_s": timestamp})
    return observations

def build_profile(result: Any, *, reference_nodes: Sequence[str], tolerance_s: float) -> dict[str, Any]:
    nodes = {}
    for node_id, estimate in sorted(result.biases.items()):
        nodes[node_id] = {"path_bias_s": estimate.bias_s, "path_bias_us": estimate.bias_us, "sigma_b_s": estimate.sigma_b_s, "sigma_b_us": estimate.sigma_b_us, "status": "admissible" if result.is_admissible(node_id, tolerance_s) else "refused", "n_observations": estimate.n_events, "residual_rms_s": estimate.residual_rms_s, "reference": node_id in reference_nodes}
    return {"schema": "hear.calibrated_node_biases.v1", "reference_nodes": list(reference_nodes), "admissibility_tolerance_s": tolerance_s, "solver": result.as_dict(), "nodes": nodes}

def calibrate_claps(survey: Any, arrivals: Any, *, reference_nodes: Sequence[str], sound_speed_mps: float, max_clap_radius_m: float = 1.0, clap_plane_z_m: float | None = None, tolerance_s: float = DEFAULT_TOLERANCE_S) -> dict[str, Any]:
    survey_data, resolved_references = resolve_reference_nodes(survey, reference_nodes)
    calibrator = ClapCalibrator(survey_data.nodes, reference_nodes=resolved_references, sound_speed_mps=sound_speed_mps, max_clap_radius_m=max_clap_radius_m, clap_plane_z_m=clap_plane_z_m, admissibility_tolerance_s=tolerance_s)
    calibrator.ingest(load_observations(arrivals, aliases=survey_data.aliases))
    result = calibrator.solve()
    if not result.converged: raise ClapCalibrationError("clap calibration did not converge: %s" % result.message)
    profile = build_profile(result, reference_nodes=resolved_references, tolerance_s=tolerance_s)
    refused = [node_id for node_id, value in profile["nodes"].items() if value["status"] != "admissible"]
    if refused: raise ClapCalibrationError("calibration confidence gate refused: %s" % ", ".join(refused))
    return profile

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", required=True, help="JSON node survey, or - for stdin")
    parser.add_argument("--arrivals", required=True, help="JSON clap arrivals")
    parser.add_argument("--output", default="config/calibrated_node_biases.json")
    parser.add_argument("--reference-node", action="append", default=None, help="Reference node name/id. Repeat for multiple references.")
    sound_speed = parser.add_mutually_exclusive_group(required=True)
    sound_speed.add_argument("--sound-speed-mps", type=float, help="Explicit acoustic propagation speed in m/s.")
    sound_speed.add_argument("--temp-c", type=float, help="Ambient temperature in C; converted with c = 331.3 + 0.606*T.")
    parser.add_argument("--max-clap-radius-m", type=float, default=1.0)
    parser.add_argument("--clap-plane-z-m", type=float)
    parser.add_argument("--tolerance-us", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not args.reference_node: parser.error("at least one --reference-node is required; do not rely on class defaults")
    references = tuple(args.reference_node)
    sound_speed_mps = args.sound_speed_mps if args.sound_speed_mps is not None else sound_speed_from_temp(args.temp_c)
    try:
        profile = calibrate_claps(_read_json(args.survey), _read_json(args.arrivals), reference_nodes=references, sound_speed_mps=sound_speed_mps, max_clap_radius_m=args.max_clap_radius_m, clap_plane_z_m=args.clap_plane_z_m, tolerance_s=args.tolerance_us * 1e-6)
        output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ClapCalibrationError, ValueError) as exc: parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "nodes": profile["nodes"]}, sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
