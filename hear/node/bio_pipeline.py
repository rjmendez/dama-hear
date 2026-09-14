#!/usr/bin/env python3
"""The sustained-sound side chain: raw samples -> named TonalGate(s) -> tagged burst events.

    samples -> {name: TonalGate} -> tagged events

⚠️DELIBERATELY NOT PART OF hear/node/pipeline.py's `samples -> Gate -> sketch -> pack -> radio`
CHAIN, AND NOT IMPORTED FROM hear/node/__init__.py. Two reasons, not one:

  1. docs/acoustic-stack.md section 6.2 measured why the impulse gate misses birds, insects and
     aircraft (broadband floor masked by low-frequency rumble; amplitude gate blind to a low
     crest-factor tone) and concluded: "Give bioacoustics its own trigger ... TonalGate is a
     complete ... streaming detector built for exactly this question ... it needs a caller and
     labels, not a rewrite." A caller that folds a sustained-sound trigger into the same object
     the TDoA solve depends on entangles two detectors whose failure domains, timing precision
     and re-arm behaviour are already different by design (see the ⚠️ below).
  2. deploy/k8s/gen_configmap.py ships hear/node/pipeline.py inside TDOA_CODE, whose comment says
     the bundle is deliberately an import CLOSURE and nothing more (see modules_supersonic
     classify's "SMALL ON PURPOSE" note for the same discipline elsewhere in that file). Importing
     modules.bioacoustic.detect from pipeline.py would drag TonalGate's FFT path into a bundle
     whose entry point, tools/hear_tdoa.py, never calls it. This module is not in TDOA_CODE and
     is not yet in any bundle -- see the note below on why it is not wired to a live node.

Run the same way pipeline.py says any node chain must be: "driven from a recorded file, because
the node firmware has to be provable before a wire is cut, and because the only ground truth this
project has is recorded."

⚠️THIS DOES NOT RUN ON A NODE YET, AND SHIPPING IT IS A SEPARATE DECISION. It is the reference an
on-node or central bioacoustic trigger would be measured against -- the same role hear/node/
detect.py and hear/node/pipeline.py played before firmware/night_node/night_node.ino existed.
Two things are unresolved and belong to that later decision, not to this file:
  - WHERE it runs: at full rate against raw samples (this class, ported to firmware, competing
    for the same MCU cycles and RAM as the impulse gate), or against scene.csv's already-shipped
    20-band 1.024 s rows (a coarser, central, redesigned sibling -- and one that cannot see
    periodicity at all: TonalGate's pulse-rate axis needs an envelope sampled far faster than one
    row per second). docs/acoustic-stack.md section 6.2 raises this trade without resolving it.
  - WHICH thresholds: every constant TonalGate ships with is documented as "NOT measured on field
    data ... provisional numbers to be refitted the first night this detector records something a
    person has listened to" (modules/bioacoustic/detect.py). That labelled night has not happened.

⚠️EVERY EVENT IS STAMPED tdoa_capable=False, AND NOT LEFT FOR A CALLER TO INFER. TonalGate's onset
is frame-resolution (+-hop_s/2, ~16 ms at the shipped nfft/hop) -- its own event docstring: "not
sub-sample: an onset that takes 100 ms to rise has no edge to interpolate." hear/node/detect.py's
Gate exists to give the opposite property, a sub-sample constant-fraction onset, because the
cross-node consistency solve runs against a 183 us budget (docs/node-hardware.md). Handing one of
these events to that solve would be handing it timing error two to three orders of magnitude past
what it tolerates. Nothing here computes a cross-node position from these events, and nothing
should, until a detector with that timing property exists.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from modules.bioacoustic import detect as BA


class BioPipeline:
    """One or more band-named TonalGates, run in lockstep over the same samples.

    `bands` maps a name (e.g. "cicada", "katydid") to the kwargs `modules.bioacoustic.detect.
    TonalGate` takes; an empty/`None` dict runs one gate named "cicada" at that class's own
    defaults (CICADA_BAND_HZ). Each name gets its OWN TonalGate instance and therefore its OWN
    adaptive in-band floor -- see TonalGate's class docstring for why a single broadband floor
    cannot see a chorus sitting under a louder band elsewhere in the spectrum. Every event this
    pipeline emits is tagged with the band name that produced it (`burst_kind`), so a consumer
    never has to reconstruct which floor, which threshold set, or which target band produced a
    given row.
    """

    def __init__(self, fs: float, bands: Optional[Dict[str, Dict]] = None):
        self.fs = float(fs)
        bands = bands if bands else {"cicada": {}}
        self.gates: Dict[str, BA.TonalGate] = {
            name: BA.TonalGate(fs, **kw) for name, kw in bands.items()
        }

    @staticmethod
    def _tag(ev: Dict, name: str) -> Dict:
        # See the module docstring: tdoa_capable is stamped here, once, rather than trusted to
        # every caller that ever reads one of these events.
        return {**ev, "burst_kind": name, "detector": "tonal", "tdoa_capable": False}

    def run(self, x: np.ndarray, block: int = 4096) -> List[Dict]:
        """Tagged events whose run closed somewhere inside `x`. Every gate sees every sample.

        `block` only bounds how much of `x` is handed to each gate's `process()` at once; it does
        not change which events fire; TonalGate frames and hops internally regardless of the
        caller's block size, exactly as hear/node/pipeline.py's Gate does.
        """
        x = np.asarray(x, float)
        out: List[Dict] = []
        for s in range(0, len(x), block):
            chunk = x[s:s + block]
            for name, gate in self.gates.items():
                for ev in gate.process(chunk, s):
                    out.append(self._tag(ev, name))
        return out

    def flush(self) -> List[Dict]:
        """Close every gate's still-open run at end of stream (`end_reason` "stream_end")."""
        out: List[Dict] = []
        for name, gate in self.gates.items():
            for ev in gate.flush():
                out.append(self._tag(ev, name))
        return out

    def diagnostics(self) -> Dict[str, Dict]:
        """Per-band counters an operator reads, not a per-event field.

        See TonalGate's own n_short/n_unstructured_segments docstrings for why these are counted
        rather than silently dropped: "saw nothing" and "saw something and threw it away" are
        different facts about a site, and a caller that only reads emitted events cannot tell
        them apart without this.
        """
        return {
            name: {
                "floor_db": g.floor_db,
                "n_frames": g.n_frames,
                "n_short": g.n_short,
                "n_unstructured": g.n_unstructured,
                "n_unstructured_segments": g.n_unstructured_segments,
                "n_gaps": g.n_gaps,
                "n_gap_samples": g.n_gap_samples,
                "n_discarded_samples": g.n_discarded_samples,
            }
            for name, g in self.gates.items()
        }


def summarise(dets: List[Dict]) -> Dict:
    """What this pipeline would report about a run -- counts it can defend, not an identity."""
    by_kind: Dict[str, int] = {}
    for d in dets:
        by_kind[d["burst_kind"]] = by_kind.get(d["burst_kind"], 0) + 1
    return {
        "events": len(dets),
        "by_kind": by_kind,
        "tonal": sum(1 for d in dets if d.get("tonal")),
        "pulsed": sum(1 for d in dets if d.get("pulsed")),
    }
