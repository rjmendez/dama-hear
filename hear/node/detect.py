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

from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

RETRIGGER_S = 0.060          # inside one muzzle blast's decay (median 12.7 ms, p90 119 ms)
GUARD_S = 0.025
REARM_FRAC = 0.35            # envelope must fall to this fraction of threshold before re-firing
ONSET_FRAC = 0.20            # constant fraction of the envelope peak that defines the onset

#: How far before the peak the SKETCH window may start. One hop, NOT the re-trigger guard.
#: The timestamp wants the true onset however slow the rise; the sketch is 8 x 4 ms and an onset
#: 25 ms before the peak slides it off the event. Measured on 228 labelled events, nested grouped
#: CV, varying only the sketch start: peak 0.9634, back<=2 ms 0.9746, back<=4 ms 0.9732,
#: back<=8 ms 0.9685, back<=25 ms (the guard) 0.9443 -- worse than not walking back at all.
#: 2/4/8/12 ms are within noise of each other; one hop is chosen on the mechanism, because past
#: one hop the onset lands in a different sketch frame.
SKETCH_BACK_S = 0.004
# Seconds, and deliberately not rounded: this exact value is what the 2026-09-05 run used, and
# docs/validation-full-captures.md's numbers were measured with it.
# ⚠️The 48000 below is the CAPTURE RIG's rate, not the node's. docs/faketec-pin-budget.md records
# that the nRF52840 cannot produce 48000 Hz at all, and the PDM mic runs at 16 kHz. tau is a TIME,
# so it carries across sample rates unchanged -- the equivalent SAMPLE count does not, and writing
# this as "10 000 samples" in library code would assert a rate the hardware cannot reach.
AMBIENT_TAU_S = 10000.0 / 48000.0   # 0.2083 s -- the LEGACY symmetric constant; see Gate
#: Slow up, quick down. The rise must outlast an event string (a clap burst, a magazine, a
#: firework finale) so the floor cannot be lifted by the very events it is there to catch; the
#: fall only has to outlast one event, so the node recovers when a site genuinely quietens.
#: Mirrored in firmware/hear_node/hear_node.ino as AMB_TAU_RISE_S / AMB_TAU_FALL_S.
AMBIENT_TAU_RISE_S = 30.0
AMBIENT_TAU_FALL_S = 5.0


def envelope(x: np.ndarray, fs: float, ms: float = 1.0) -> np.ndarray:
    """1 ms moving average of |x|.

    Rise and decay MUST come from an envelope. Walking raw |x| from the peak stops at the first
    zero crossing and reports ~0.1 ms for everything, which is how a whole feature set was
    silently wrong once already.
    """
    n = max(1, int(ms * 1e-3 * fs))
    return np.convolve(np.abs(x), np.ones(n) / n, mode="same")


def onset_index(e: np.ndarray, peak: int, frac: float = ONSET_FRAC, back: int = 0) -> float:
    """Sub-sample constant-fraction onset. Index into `e`, fractional, always <= `peak`.

    Walks back from the envelope peak to the last sample below `frac * e[peak]` and linearly
    interpolates the crossing. Constant fraction rather than a fixed threshold because the gate's
    own threshold is an absolute level: a loud round crosses it far earlier in its rise than a
    quiet one, so a threshold-crossing timestamp carries the range-dependent bias this function
    exists to remove.

    ⚠️`e` MUST BE AN ENVELOPE, never raw |x|. The identical walk on raw samples stops at the
    first zero crossing -- see envelope() above. The sibling project shipped exactly that: its
    back-walk moves 0 samples, so its "onset" is its peak, measured across a 100x amplitude sweep.

    Refuses to search more than `back` samples before the peak (0 = back to e[0]). An onset that
    predates the window is reported AT the window edge, not extrapolated: a detection whose rise
    began in the previous block is late, and saying so beats inventing a number.
    """
    idx, _ = onset_index_checked(e, peak, frac, back)
    return idx


def onset_index_checked(e: np.ndarray, peak: int, frac: float = ONSET_FRAC,
                        back: int = 0) -> Tuple[float, bool]:
    """As [onset_index], plus whether the fraction was actually CROSSED.

    ⚠️THE FRACTION IS REFERRED TO THE LOCAL FLOOR, NOT TO ZERO. `target = floor + frac*(peak -
    floor)`, where `floor` is the minimum envelope in the search window.

    Referred to zero it asks the envelope to fall to 20% of the NEW peak, which a round landing
    inside the previous round's decay tail never does -- the old one is still ringing. The walk
    then ran to the clamp edge and returned it, an arbitrary `peak - back` numerically
    indistinguishable from a genuine slow rise. On the 228 hand-labelled 2026-09-05 events that
    was **42 of them (18.4%)**, every one timestamped exactly 25 ms early.

    Measured against known truth (a synthetic previous round still ringing 45 ms later, onset
    truth 45.0 ms, swept by tail level):

        floor/peak   referred to zero        referred to the floor
        0.17         -0.55 ms   11/12 found  +0.45 ms   12/12
        0.23        -21.62 ms    0/12 found  +0.43 ms   12/12

    On the real corpus it takes the never-found count from **42 to 0**, moves those 42 by a
    median of **+22.5 ms** toward the event, and leaves 63% of the rest within 0.05 ms. 27.6% of
    all events shift by more than 1 ms and 19.3% by more than 10 ms; the largest is 24.27 ms,
    which is **8.4 m of range** at 345 m/s.

    ⚠️It is a STRICT GENERALISATION: where the floor is zero the two are identical, so a quiet
    event cannot regress. Nor can a truncated one -- see the interior-trough guard below. Cost on the sketch, whose one-hop window barely sees the floor: nested
    AUC 0.9732 -> 0.9712, inside the noise band, and worth it for one definition of "onset"
    across the node, the phone and the training script.

    ⚠️WHAT IT STILL CANNOT FIX: when the previous round is as loud as or louder than this one,
    `peak` is the WRONG PEAK -- argmax finds the old event -- and no onset rule helps. Measured
    at tail 0 dB and +3 dB the error is -22 ms and -45 ms for both methods. That is a
    peak-picking failure, not an onset failure, and `found` does not catch it.

    `found` is False only when no crossing exists even against the floor (an empty window, or a
    peak at its very edge). Callers must not treat the index as measured when it is False.
    """
    lo = max(0, peak - back) if back > 0 else 0
    if peak <= lo:
        return float(lo), False
    # ⚠️ONLY AN *INTERIOR* TROUGH IS A BASELINE. If the minimum sits at the window's left edge the
    # envelope is still descending out of the window -- the rise predates it -- and referring the
    # fraction to that edge value turns an honest "clamped, cannot see further back" into a
    # confidently LATE onset. So that case keeps the zero reference, which clamps to the edge and
    # reports found=False, exactly as before. The two are cleanly separable in practice: all 228
    # of the 2026-09-05 events have an interior trough (including all 42 that could not be timed),
    # and a block that truncates a rise has its trough at the edge by construction.
    am = int(np.argmin(e[lo:peak]))
    floor = float(e[lo + am]) if am > 0 else 0.0
    target = floor + frac * (float(e[peak]) - floor)
    below = np.nonzero(e[lo:peak] < target)[0]
    if below.size == 0:
        return float(lo), False
    j = lo + int(below[-1])          # last sample below the fraction; crossing is in [j, j+1]
    rise = float(e[j + 1]) - float(e[j])
    return (float(j) if rise <= 0 else j + (target - float(e[j])) / rise), True


class Gate:
    """Streaming level gate with an adaptive floor.

    The threshold is a RATIO over the running ambient, floored: an absolute threshold set for one
    session is wrong for the next, and a shot is tens of dB over ambient in any of them.
    """

    def __init__(self, fs: float, ratio: float = 8.0, floor: float = 800.0,
                 guard_s: float = GUARD_S, ambient_tau_s: Optional[float] = None,
                 rearm_frac: float = REARM_FRAC, onset_frac: float = ONSET_FRAC,
                 sketch_back_s: float = SKETCH_BACK_S,
                 ambient_tau_rise_s: float = AMBIENT_TAU_RISE_S,
                 ambient_tau_fall_s: float = AMBIENT_TAU_FALL_S):
        """⚠️THE AMBIENT ESTIMATE IS ASYMMETRIC IN DIRECTION: slow up, quick down.

        This replaces a single `ambient_tau_s` of 0.21 s that updated only while the envelope sat
        BELOW threshold. That form rested on one claim, stated in this docstring and now known to
        be false: "the floor only ever sees material already below threshold, so nothing it tracks
        is an event". A transient's reverberant tail is below threshold and IS the event, as is
        the noise of whatever is producing a string of them -- so a burst raised its own bar.

        Measured on the node `mach` 2026-09-09, clapping in the same room: ambient 22 -> 143 in
        five seconds, threshold 200 -> 1146, zero of the claps detected, while the co-located
        phone recorded every one. Across the three nodes the one in the occupied room had the
        highest peak envelope (16938, 2.7x its siblings) and the FEWEST detections (294 vs 689
        and 470). The gate turned itself down exactly where there was most to hear.

        Keyed on direction, a burst lifts the floor only at `ambient_tau_rise_s`, so it cannot
        desensitise the detector to itself, while a genuinely louder site is still learned -- over
        half a minute instead of half a second. Falling stays quick so a site that quietens
        recovers its sensitivity. Rising is never frozen, so the disarm deadlock the firmware hit
        outdoors (156 s solid, ambient stuck at 73.2) cannot occur here either.

        ⚠️`ambient_tau_s` IS RETAINED AND SETS BOTH LIMBS EQUAL, because every number in
        docs/validation-full-captures.md (338 raw detections -> 168, 32 impossible clusters -> 1,
        zero false alarms in 68.5 min of quiet) was produced by the symmetric 0.21 s floor. Those
        numbers are reproducible by passing `ambient_tau_s=AMBIENT_TAU_S` -- near enough, since
        the old form also FROZE the estimate above threshold, which mattered only in the disarmed
        state. They do NOT describe the default any more, and this change is expected to raise the
        detection count in bursty or occupied conditions, which is its point.

        The realised constants are pinned by step response in tests/test_node.py, not asserted
        here.
        """
        self.fs = float(fs)
        self.ratio = float(ratio)
        self.floor = float(floor)
        self.guard = max(1, int(guard_s * fs))
        self.rearm_frac = float(rearm_frac)
        self.onset_frac = float(onset_frac)
        self.sketch_back_s = float(sketch_back_s)
        # per SAMPLE, because that is where they are applied
        if ambient_tau_s is not None:          # legacy symmetric form; see the docstring
            ambient_tau_rise_s = ambient_tau_fall_s = float(ambient_tau_s)
        self.alpha_rise = 1.0 / max(1.0, ambient_tau_rise_s * fs)
        self.alpha_fall = 1.0 / max(1.0, ambient_tau_fall_s * fs)
        self.ambient = 0.0
        self.n_seen = 0
        self.last_onset: Optional[float] = None
        # Schmitt state. A fixed guard alone re-fires every guard interval for as long as the
        # envelope stays high, so one shot with a 100 ms tail became 4-5 "rounds" spaced at
        # exactly 25 ms. Measured over 123.7 min of capture before this was added.
        self.armed = True

    def threshold(self) -> float:
        return max(self.ambient * self.ratio, self.floor)

    def process(self, block: np.ndarray, block_start: int) -> List[Dict]:
        """Detections in this block. `block_start` is the absolute sample index of block[0].

        `index`/`t_s` are the ONSET -- sub-sample, constant-fraction. `peak_index`/`env_peak`/
        `peak` are the envelope peak and stay: they are classifier features, and the peak is the
        more amplitude-robust of the two reference points. What the peak is NOT is an arrival
        time. Timestamping it costs the rise time of the round, which grows with range, so the
        bias is per-node and does not cancel in TDoA -- against a 183 us budget (node-hardware.md).
        """
        e = envelope(block, self.fs)
        out: List[Dict] = []
        i = 0
        while i < len(e):
            thr = self.threshold()
            if not self.armed:
                # re-arm only once the envelope has actually fallen back down; a guard that
                # expires on time alone cannot tell a decay tail from a new round
                if e[i] < thr * self.rearm_frac:
                    self.armed = True
                i += 1
                continue
            # Slow up, quick down -- tracked unconditionally. See __init__.
            self.ambient += (self.alpha_rise if e[i] > self.ambient
                             else self.alpha_fall) * (e[i] - self.ambient)
            if e[i] > thr:
                j = min(len(e), i + self.guard)
                k = i + int(np.argmax(e[i:j]))
                # bounded by the guard, which is also the window the peak was found in
                on_rel, on_found = onset_index_checked(e, k, self.onset_frac, back=self.guard)
                on = block_start + on_rel
                # A SECOND, tighter walk for the sketch window only. The timestamp and the
                # feature window want different clamps and used to share one number.
                on_sk = block_start + onset_index(
                    e, k, self.onset_frac, back=max(1, int(self.sketch_back_s * self.fs)))
                since = None if self.last_onset is None else (on - self.last_onset) / self.fs
                out.append({
                    "index": int(on),           # floored onset -- what t_s is anchored on
                    "onset_index": float(on),   # sub-sample; what t_s is made of
                    # ⚠️False => the fraction was never crossed and `onset_index` is the CLAMP
                    # EDGE, not a measurement. 18.4% of the 2026-09-05 events. See
                    # onset_index_checked.
                    "onset_found": bool(on_found),
                    "sketch_index": int(on_sk), # where the SKETCH starts; at most one hop back
                    "t_s": on / self.fs,
                    "peak_index": int(block_start + k),
                    "peak": float(np.abs(block[max(0, k - self.guard):k + self.guard]).max()),
                    "env_peak": float(e[k]),
                    "threshold": self.threshold(),
                    "since_prev_s": since,
                    # NOT suppressed: a reflection is evidence about the site, and the echo
                    # analysis needs it. Flagged so the central side can decide.
                    "retrigger": bool(since is not None and since < RETRIGGER_S),
                })
                self.last_onset = on
                self.armed = False
                i = k + self.guard
                continue
            i += 1
        self.n_seen += len(block)
        return out
