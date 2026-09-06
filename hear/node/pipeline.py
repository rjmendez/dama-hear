#!/usr/bin/env python3
"""The whole node chain: audio in, Meshtastic frames out. No hardware required to run it.

    samples -> Gate -> sketch -> pack -> radio

Written so it can be driven from a recorded file, because the node firmware has to be provable
before a wire is cut, and because the only ground truth this project has is recorded.
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional

import numpy as np

from .. import sketch as SK
from . import detect as DT

PRE_S = 0.02          # a little context before the onset; the sketch starts AT it
POST_S = 0.30


class Pipeline:
    def __init__(self, fs: float, node_us_of=None, **gate_kw):
        self.fs = float(fs)
        self.gate = DT.Gate(fs, **gate_kw)
        # PPS-relative microseconds. Injected so a test can be deterministic and so the node
        # can hand in its own PPS counter without this module knowing anything about hardware.
        self.node_us_of = node_us_of or (lambda idx: int((idx / fs) * 1e6) % 1_000_000)

    def run(self, x: np.ndarray, block: int = 4096) -> List[Dict]:
        """Detections with their packed frames. `x` is one channel, int16-scaled float."""
        x = np.asarray(x, float)
        out: List[Dict] = []
        for s in range(0, len(x), block):
            for d in self.gate.process(x[s:s + block], s):
                i = d["index"]
                lo = max(0, i - int(PRE_S * self.fs))
                hi = min(len(x), i + int(POST_S * self.fs))
                seg = x[lo:hi]
                if len(seg) < int(0.01 * self.fs):
                    continue
                # sketch from the onset, not from the padded window start
                q, ref = SK.sketch(seg[i - lo:], self.fs)
                frame = SK.pack(self.node_us_of(i), ref, int(min(d["peak"], 65535)), q,
                                flags=(1 if d["retrigger"] else 0))
                out.append({**d, "ref_db": ref, "frame": frame, "frame_len": len(frame)})
        return out


def summarise(dets: List[Dict]) -> Dict:
    """What the node would report about a run -- counts it can defend, not counts it invents."""
    n = len(dets)
    rt = sum(1 for d in dets if d["retrigger"])
    return {"detections": n, "retriggers": rt, "distinct_candidates": n - rt,
            "bytes_if_all_sent": sum(d["frame_len"] for d in dets)}
