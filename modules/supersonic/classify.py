#!/usr/bin/env python3
"""Is this impulse a gunshot? Six weights and a bias -- no sklearn, no GPU, no model file format.

WHY THIS EXISTS. The detector upstream is a LEVEL GATE: amplitude over ambient. It cannot tell a
rifle from a tailgate, it re-fires on a reverb tail, and it counts a reflection as a new event.
Measured on the 2026-09-05 live fire, it emitted 427 events for roughly 60 rounds.

TRAINED ON OPERATOR LABELS, not on a threshold and not on a public corpus: 228 events the operator
listened to and called. Grouped 5-fold CV (events from one string never span folds, because the
same round appears on three boards and inside one burst) gives AUC 0.959 against a 0.561
majority-class baseline. Ungrouped CV leaks and reports a number the field will not reproduce.

⚠️AMPLITUDE IS NOT THE WHOLE MODEL, though it is the largest single term. Ablated: peak alone
0.873, the shape features WITHOUT peak 0.879, together 0.959. The shape features carry
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


DEFAULT_SKETCH_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "model_sketch.json")
#: For a fleet with anything slower than 32 kHz in it. 15 bands, AUC 0.9588 against 0.9634 --
#: within noise, and it is the only one a 16 kHz node's frame can be scored with at all.
FLEET_SKETCH_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "model_sketch_15.json")


class SketchMismatch(Exception):
    """The frame and the model do not describe the same measurement."""


def score_sketch(frame, model: Dict[str, Any]) -> float:
    """P(gunshot) from a wire frame. `frame` is `hear.sketch.unpack`'s dict, or the raw bytes.

    ⚠️REFUSES RATHER THAN PADS. A 16 kHz node's top five bands are empty by construction, and
    feeding those zeros to a 20-band model is not a degraded reading -- it is a spectrum claiming
    the node measured silence above 9.4 kHz when it measured nothing at all. Scoring the same
    audio that way costs 3.3 points of AUC (0.9141 against 0.9473). Use FLEET_SKETCH_MODEL.

    ⚠️LAYOUT MUST MATCH. Under the legacy `nyquist` layout band k is a different frequency at
    every rate, so a model's weight for band k means nothing on a frame from another rate.
    """
    if isinstance(frame, (bytes, bytearray)):
        from hear import sketch as _sk
        frame = _sk.unpack(bytes(frame))
    q, ref = frame["q"], float(frame["ref_db"])
    bands, frames = len(q), len(q[0])
    want_b, want_f = int(model["bands"]), int(model["frames"])
    if frames != want_f:
        raise SketchMismatch("frame has %d time frames, model wants %d" % (frames, want_f))
    if bands < want_b:
        raise SketchMismatch("frame carries %d bands, model wants %d" % (bands, want_b))
    if frame.get("layout") is not None and frame["layout"] != model.get("layout"):
        raise SketchMismatch("frame layout %r, model trained on %r -- band k is not the same "
                             "frequency in the two" % (frame["layout"], model.get("layout")))
    valid = frame.get("valid_bands")
    if valid is not None and valid < want_b:
        raise SketchMismatch(
            "only %d of this frame's bands carry a measurement (fs %s Hz) and the model wants "
            "%d; the rest are empty by construction, not quiet. Score it with a model trained "
            "on %d bands (FLEET_SKETCH_MODEL)." % (valid, frame.get("fs_hz"), want_b, valid))
    w = model["w"]
    if len(w) != want_b * want_f:
        raise SketchMismatch("model has %d weights for a %dx%d sketch" % (len(w), want_b, want_f))
    z = float(model["b"])
    i = 0
    for b in range(want_b):                       # band-major, matching model["order"]
        row = q[b]
        for t in range(want_f):
            z += w[i] * (row[t] / 2.0 + ref)      # absolute dB; the reference is half the signal
            i += 1
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
