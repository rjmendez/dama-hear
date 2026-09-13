#!/usr/bin/env python3
"""CLI tool: fuse coincident multi-node acoustic tags into spatial-consensus predictions.

Reads the tag store (`clips/tags.jsonl`, see `hear/tags.py`), turns each row's per-class `scores`
dict into `hear.ensemble.NodeObservation`s keyed by node and clip time, and calls
`hear.ensemble.fuse_predictions` to group and fuse them across the array's transit window
(diameter / c + margin -- see `hear/ensemble.py`).

⚠️NOT PART OF ANY deploy/k8s ConfigMap BUNDLE, DELIBERATELY. `hear/tags.py`'s own docstring
refuses to import `hear.pool` because `deploy/k8s/gen_configmap.py`'s `check()` is an `ast.walk`
that resolves a function-local import exactly as well as a top-level one, and would then demand
every transitive dependency in the `hear-tag` bundle for the sake of an analysis-only query the
CronJob never calls. This tool is the same shape of consumer -- an operator's ad-hoc fusion pass
over an already-written tag store -- so it lives outside every bundle's import closure rather
than pulling `hear/ensemble.py` into one.

Examples:
    # Fuse every node's tags for one class into a consensus timeline:
    python3 tools/hear_ensemble.py --root /pool --class "Dog" --diameter-m 24.5

    # Compare the log-odds Bayes pool against the plain SNR-weighted average:
    python3 tools/hear_ensemble.py --root /pool --method linear
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import tags as TAGS
from hear.ensemble import NodeObservation, fuse_predictions


def _snr_db(row: Dict[str, Any]) -> Optional[float]:
    """This row's best SNR/RMS-level estimate. `pre_norm_dbfs` is the level `tools/hear_tag.py`
    stores per clip (see its `tag()` call site) -- read verbatim as the SNR proxy the
    `hear.ensemble` module docstring documents, since it is the only per-row level a tag carries.
    """
    v = row.get("pre_norm_dbfs")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _t_utc_s(row: Dict[str, Any]) -> Optional[float]:
    """This row's clip-window start time, or None when the clip is unanchored (no UTC exists).

    An unanchored row participates in nothing here: `fuse_predictions` groups by `t_utc_s`, and a
    row with no UTC anchor has no time to compare against another node's, so it is dropped rather
    than guessed at.
    """
    window = row.get("window") or {}
    t = window.get("t_start_utc_s")
    try:
        return float(t) if t is not None else None
    except (TypeError, ValueError):
        return None


def load_observations(root: str, store: Optional[str] = None,
                      class_name: Optional[str] = None) -> List[NodeObservation]:
    """Every tag row in `root`'s store, as `NodeObservation`s. `class_name` restricts each row's
    `scores` dict to that one class (dropping the row entirely when it never scored it); None
    keeps every class the row's tagger stored.
    """
    out: List[NodeObservation] = []
    for row in TAGS.read_tags(root, store):
        node = row.get("node")
        t = _t_utc_s(row)
        scores = row.get("scores") or {}
        if not node or t is None or not scores:
            continue
        if class_name is not None:
            if class_name not in scores:
                continue
            scores = {class_name: scores[class_name]}
        out.append(NodeObservation(node=node, t_utc_s=t, scores=dict(scores),
                                   snr_db=_snr_db(row)))
    return out


def fuse_coincident_tags(root: str, *, store: Optional[str] = None,
                         class_name: Optional[str] = None,
                         window_s: Optional[float] = None, diameter_m: Optional[float] = None,
                         temp_c: float = 20.0, method: str = "logodds",
                         prior: float = 0.5) -> List[Dict[str, Any]]:
    """Read `root`'s tag store and fuse coincident cross-node predictions into a consensus
    timeline. -> a list of `hear.ensemble.ConsensusPrediction.to_dict()` dicts, ascending in time.

    This is the one function both this CLI's `main()` and a caller importing the tool directly
    should use -- it is the join `load_observations` + `fuse_predictions` that the CLI's `--root`
    contract commits to.
    """
    obs = load_observations(root, store, class_name)
    fused = fuse_predictions(obs, window_s=window_s, diameter_m=diameter_m, temp_c=temp_c,
                             method=method, prior=prior)
    return [c.to_dict() for c in fused]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fuse coincident multi-node acoustic tag predictions into spatial consensus.")
    parser.add_argument("--root", required=True, help="Pool root holding clips/tags.jsonl.")
    parser.add_argument("--store", default=None, help="Named tag store, default the unstored one.")
    parser.add_argument("--class", dest="class_name", default=None,
                        help="Restrict fusion to one class name; default fuses every class.")
    parser.add_argument("--window-s", type=float, default=None,
                        help="Explicit transit window in seconds; default computed from "
                             "--diameter-m / c + margin.")
    parser.add_argument("--diameter-m", type=float, default=None,
                        help="Array diameter in metres, used when --window-s is not given.")
    parser.add_argument("--temp-c", type=float, default=20.0, help="Air temperature, for c(T).")
    parser.add_argument("--method", choices=("logodds", "linear"), default="logodds",
                        help="Fusion rule: prior-corrected log-odds pool (default) or SNR-"
                             "weighted linear opinion pool.")
    parser.add_argument("--prior", type=float, default=0.5,
                        help="Class base rate for the log-odds method's prior correction.")
    args = parser.parse_args()

    rows = fuse_coincident_tags(args.root, store=args.store, class_name=args.class_name,
                                window_s=args.window_s, diameter_m=args.diameter_m,
                                temp_c=args.temp_c, method=args.method, prior=args.prior)
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
