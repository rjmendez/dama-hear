#!/usr/bin/env python3
"""Run repeatable point-source TDoA benchmark scenarios."""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hear import nodeclass
from hear.sim import SimNode, arrival_times, replay_impulse
from hear.sim.metrics import report as metric_report
from hear.solve.point import solve
SCENARIOS = ("square-4node-center", "square-4node-outside", "mixed-nodeclasses", "real-sample-impulse")

def _position(value: str) -> list[float]:
    try: result = [float(v) for v in value.split(",")]
    except ValueError as exc: raise argparse.ArgumentTypeError("position must be x,y,z") from exc
    if len(result) != 3 or not all(math.isfinite(v) for v in result): raise argparse.ArgumentTypeError("position must be finite x,y,z")
    return result

def _default_nodes(classes=None):
    positions = ((0.,0.,0.), (100.,0.,0.), (100.,100.,0.), (0.,100.,0.))
    classes = classes or ["xiao-s3-pps"] * 4
    return [SimNode(p, classes[i], f"node-{i+1}") for i,p in enumerate(positions)]

def _scenario(name):
    if name == "square-4node-center": return _default_nodes(), [50.,50.,0.]
    if name == "square-4node-outside": return _default_nodes(), [250.,50.,0.]
    if name == "mixed-nodeclasses": return _default_nodes(["xiao-s3-pps", "esp32s3-speaker", "puc-ntp", "xiao-s3-pps"]), [50.,50.,0.]
    return _default_nodes(), [50.,50.,0.]

def _load_nodes(path):
    with open(path, encoding="utf-8") as stream: payload = json.load(stream)
    entries = payload.get("nodes") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not entries: raise ValueError("nodes config must contain a non-empty list or {nodes: [...]}")
    nodes = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict): raise ValueError(f"node {index} must be an object")
        position = entry.get("position", entry.get("pos"))
        if not isinstance(position, list) or len(position) != 3: raise ValueError(f"node {index} position must be [x,y,z]")
        nodes.append(SimNode(tuple(float(v) for v in position), str(entry.get("node_class", entry.get("class", "xiao-s3-pps"))), str(entry.get("id", f"node-{index+1}"))))
    return nodes

def _gate(nodes):
    errors = []
    for node in nodes:
        try: nodeclass.require_arrival(node.node_class, node_id=node.node_id)
        except nodeclass.CapabilityError as exc: errors.append(str(exc))
    return errors

def run(args):
    nodes, source = _scenario(args.scenario)
    if args.nodes_config: nodes = _load_nodes(args.nodes_config)
    if args.source_pos is not None: source = args.source_pos
    gate_errors = _gate(nodes)
    base = {"scenario": args.scenario, "sound_speed_mps": args.c, "snr_db": args.snr_db,
            "nodes": [{"id": n.node_id, "position": list(n.position), "node_class": n.node_class} for n in nodes],
            "source_position": source, "gate": {"admitted": not gate_errors, "errors": gate_errors}}
    if gate_errors:
        base.update(status="refused", refusal="uncalibrated or unsupported node class")
        return base
    if args.scenario == "real-sample-impulse":
        if not args.sample: raise ValueError("--sample is required for real-sample-impulse")
        arrivals, replay = replay_impulse(args.sample, nodes, source, args.c, args.snr_db)
        base["replay"] = replay
    else:
        timing_sigma = 1e-4 * 10.0 ** (-args.snr_db / 20.0)
        arrivals = arrival_times(nodes, source, args.c, emission_time_s=1000., timing_noise_s=timing_sigma, seed=0)
    result = solve([n.position for n in nodes], arrivals.tolist(), "blast", temp_c=(args.c - 331.3) / 0.606, fixed_up_m=source[2])
    base.update(status="ok", arrivals_s=arrivals.tolist(), metrics=metric_report(nodes, source, arrivals, result))
    return base

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", default="square-4node-center", choices=SCENARIOS)
    parser.add_argument("--source-pos", type=_position); parser.add_argument("--nodes-config"); parser.add_argument("--sample")
    parser.add_argument("--snr-db", type=float, default=20.); parser.add_argument("--c", type=float, default=343.); parser.add_argument("--output-report")
    args = parser.parse_args(argv)
    if not math.isfinite(args.c) or args.c <= 0: parser.error("--c must be finite and positive")
    result = run(args); encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output_report: Path(args.output_report).write_text(encoded + "\n", encoding="utf-8")
    else: print(encoded)
    return 0

if __name__ == "__main__": raise SystemExit(main())
