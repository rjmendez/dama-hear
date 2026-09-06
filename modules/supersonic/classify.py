#!/usr/bin/env python3
"""Is this impulse a gunshot? Seven weights and a bias -- no sklearn, no GPU, no model file format.

WHY THIS EXISTS. The detector upstream is a LEVEL GATE: amplitude over ambient. It cannot tell a
rifle from a tailgate, it re-fires on a reverb tail, and it counts a reflection as a new event.
Measured on the 2026-09-05 live fire, it emitted 427 events for roughly 60 rounds.

TRAINED ON OPERATOR LABELS, not on a threshold and not on a public corpus: 228 events the operator
listened to and called. Grouped 5-fold CV (events from one string never span folds, because the
same round appears on three boards and inside one burst) gives AUC 0.960 against a 0.561
majority-class baseline. Ungrouped CV leaks and reports a number the field will not reproduce.

⚠️AMPLITUDE IS NOT THE WHOLE MODEL, though it is the largest single term. Ablated: peak alone
0.873, the shape features WITHOUT peak 0.879, together 0.948-0.960. The shape features carry
independent information -- permutation importance says otherwise only because it hands shared
signal to one correlated feature.

⚠️THE SITE TAUGHT IT. Every label came from one afternoon at one range with one rifle, and the
operator labelled essentially every real shot as a supersonic CRACK, never a clean muzzle blast --
which the spectrum independently agrees with (centroid ~7.9 kHz, 1% of energy under 500 Hz). A
shot heard from behind, or a subsonic round, is NOT represented here. Retrain before trusting it
somewhere else.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Optional

DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.json")


def load_model(path: str = DEFAULT_MODEL) -> Dict[str, Any]:
    with open(os.path.abspath(path)) as fh:
        return json.load(fh)


def score(feat: Dict[str, float], model: Dict[str, Any]) -> Optional[float]:
    """P(gunshot) in [0,1], or None if a feature is missing.

    None rather than a default: a missing feature substituted with 0 scores a silent event as a
    confident shot, and the caller cannot tell that from a real one.
    """
    z = float(model["b"])
    logs = set(model.get("log10", ()))
    for name, w in zip(model["features"], model["w"]):
        if name not in feat or feat[name] is None:
            return None
        v = float(feat[name])
        if name in logs:
            v = math.log10(max(v, 1e-3))
        z += float(w) * v
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


def merge_rounds(events, window_s: float = 0.060):
    """Collapse per-board detections of ONE round into one round.

    The three boards sit up to 40 ms apart in the ring, so the same shot lands three times at
    different instants. Counting board-events as rounds is what turned ~60 rounds into 427.
    """
    out = []
    for e in sorted(events, key=lambda x: x["utc"]):
        if out and e["utc"] - out[-1][-1]["utc"] <= window_s:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def needs_label(p: float, lo: float = 0.35, hi: float = 0.65) -> bool:
    """Active learning: only the band the model cannot call is worth an operator's time.

    Measured over 1014 events, this band holds 14% of them -- so continuous labelling costs about
    a seventh of labelling everything, which is what makes it sustainable in the field.
    """
    return lo <= p <= hi
