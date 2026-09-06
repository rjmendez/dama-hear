#!/usr/bin/env python3
"""Event detection on the node. A level gate, but one that knows what it gets wrong.

The gate itself is unavoidable: a node cannot afford to classify continuously, so something cheap
has to decide when to look. What it MUST NOT do is pretend its output is a count of events.

Measured on 2026-09-05 with a naive gate: 427 events for ~60 rounds. Of 1014 board-events, 60%
fired within 60 ms of the previous one -- inside a single blast's own decay -- and a guard shorter
than the delay of any reflector past ~4 m counts reflections as new events BY CONSTRUCTION.

So this gate reports `retrigger` and `since_prev_s` on every detection and leaves the decision to
the classifier and the central side. Suppressing them here would destroy the evidence that a
reflection existed, which the echo analysis needs.
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional

import numpy as np

RETRIGGER_S = 0.060          # inside one muzzle blast's decay (median 12.7 ms, p90 119 ms)
GUARD_S = 0.025


def envelope(x: np.ndarray, fs: float, ms: float = 1.0) -> np.ndarray:
    """1 ms moving average of |x|.

    Rise and decay MUST come from an envelope. Walking raw |x| from the peak stops at the first
    zero crossing and reports ~0.1 ms for everything, which is how a whole feature set was
    silently wrong once already.
    """
    n = max(1, int(ms * 1e-3 * fs))
    return np.convolve(np.abs(x), np.ones(n) / n, mode="same")


class Gate:
    """Streaming level gate with an adaptive floor.

    The threshold is a RATIO over the running ambient, floored: an absolute threshold set for one
    session is wrong for the next, and a shot is tens of dB over ambient in any of them.
    """

    def __init__(self, fs: float, ratio: float = 8.0, floor: float = 800.0,
                 guard_s: float = GUARD_S, ambient_tau_s: float = 10.0):
        self.fs = float(fs)
        self.ratio = float(ratio)
        self.floor = float(floor)
        self.guard = max(1, int(guard_s * fs))
        self.alpha = 1.0 / max(1.0, ambient_tau_s * fs / max(1, int(0.001 * fs)))
        self.ambient = 0.0
        self.n_seen = 0
        self.last_idx: Optional[int] = None

    def threshold(self) -> float:
        return max(self.ambient * self.ratio, self.floor)

    def process(self, block: np.ndarray, block_start: int) -> List[Dict]:
        """Detections in this block. `block_start` is the absolute sample index of block[0]."""
        e = envelope(block, self.fs)
        out: List[Dict] = []
        i = 0
        while i < len(e):
            # ambient tracks the quiet material only, so one loud event cannot raise the floor
            if e[i] <= self.threshold():
                self.ambient = (1 - self.alpha) * self.ambient + self.alpha * e[i]
            else:
                j = min(len(e), i + self.guard)
                k = i + int(np.argmax(e[i:j]))
                idx = block_start + k
                since = None if self.last_idx is None else (idx - self.last_idx) / self.fs
                out.append({
                    "index": int(idx),
                    "t_s": idx / self.fs,
                    "peak": float(np.abs(block[max(0, k - self.guard):k + self.guard]).max()),
                    "env_peak": float(e[k]),
                    "threshold": self.threshold(),
                    "since_prev_s": since,
                    # NOT suppressed: a reflection is evidence about the site, and the echo
                    # analysis needs it. Flagged so the central side can decide.
                    "retrigger": bool(since is not None and since < RETRIGGER_S),
                })
                self.last_idx = idx
                i = k + self.guard
                continue
            i += 1
        self.n_seen += len(block)
        return out
