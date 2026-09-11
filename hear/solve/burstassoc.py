#!/usr/bin/env python3
"""Associate a BURST across two sensors by the one delay it must share.

⚠️WHY PER-EVENT MATCHING IS NOT ENOUGH, MEASURED. Inside the physical bound |tau| <= d/c a burst
offers several partners per event: on the 2026-09-08 node pair (16.873 m, bound 49.1 ms) 13 of
55 coincident nyquist events had more than one mach candidate, and comparing the events' 172-byte
sketches did NOT break a single one of those 13 -- within a burst the candidates are echoes of
the same event and their spectra all look alike (shape correlation 0.87-0.93 across the whole
candidate set, against an AUC of 0.939 for true-vs-random pairs).

The constraint per-event matching throws away is that a burst comes from ONE place. The source
does not move between events milliseconds apart, so every event in the burst shares ONE tau.
Matching each event independently is free to pick a different tau for each, which is how a set of
individually-plausible pairings becomes a jointly-impossible one.

So this fits a single tau to the whole burst and reports the assignment it implies.

⚠️CANDIDATE TAUS ARE EXACT, NOT GRIDDED. The best tau always aligns at least one pair exactly, so
the candidates are precisely {b_j - a_i} within the bound. A grid search instead makes the answer
depend on the step -- and a step coarser than the tolerance silently misses the optimum.

⚠️IT REFUSES. A burst whose best tau is not clearly better than the runner-up is reported as
ambiguous rather than resolved, because that is exactly the 13-of-55 case above and picking one
of two equally-good answers is how a plausible wrong association enters a solve. `margin` is the
separation, and `MIN_MARGIN` the default demand.

⚠️A CONSTANT ECHO DELAY IS AN UNRESOLVABLE TIE IN THE TIMING, AND IT IS THE REALISTIC CASE. A
stationary source in a fixed room echoes at a near-constant delay -- same paths, same geometry,
every event -- so the echo train IS the direct train shifted, and tau and tau+echo explain the
arrivals equally well.

Three things break it, in order of authority. The geometric bound settles it for free when
tau+echo exceeds d/c: such a tau is never proposed. A varying echo delay settles it when the
variation exceeds the tolerance. Otherwise pass `peak_a`/`peak_b` -- the per-event amplitude the
frame already carries -- and the louder pairing wins, because a direct arrival is louder than its
own reflection.

⚠️AMPLITUDE IS A PRIOR AND IS RANKED BELOW TIMING ACCORDINGLY. It only ever breaks a tie the pair
COUNT could not; a hypothesis explaining more pairs wins however quiet it is. A shadowed or
off-axis node genuinely can receive a reflection stronger than the direct arrival, so a winner
that is quieter on any differing side is refused outright rather than inverted, the margin is
taken on the MEDIAN of the matched set, and a zero or missing peak disables the tiebreaker
instead of counting as silence. `decided_by` records which rule settled it.

⚠️VALIDATED ON SYNTHETIC GROUND TRUTH ONLY. The field data cannot test it: over a 15.0 h overlap
those two nodes shared only 3 bursts with 3+ events on both sides, and 30% of one node's events
sit in a burst the other never heard at all -- its two largest bursts (12 and 23 events) drew
zero co-activity. Two nodes 16.9 m apart hear mostly different near-field sources. What
would settle it is a loud impulsive source both nodes hear; ambient sound is not that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

#: How much better the best tau must be than the next distinct one, in matched pairs.
MIN_MARGIN: int = 1

#: Two taus closer than this count as the same hypothesis when measuring the margin.
TAU_SEPARATION_FACTOR: float = 2.0

#: How much louder, in dB of median matched peak, one hypothesis must be before amplitude is
#: allowed to break a tie the pair COUNT could not.
#:
#: ⚠️NOT MEASURED FROM FIELD DATA, because the field data has no usable bursts to measure it on.
#: It is sized against what IS measured: event-to-event peak on these nodes spans 23.6 dB
#: (nyquist) and 25.1 dB (mach) from p10 to p90, so a small threshold would fire on ordinary
#: variation. 6 dB is a factor of two in amplitude, taken on the MEDIAN of the matched set rather
#: than any single event, so ordinary spread averages down while a systematic direct-vs-echo
#: offset does not. Revisit it the first time a burst both nodes heard is recorded.
MIN_AMPLITUDE_MARGIN_DB: float = 6.0


@dataclass
class BurstMatch:
    """What a burst can be said to be. `ok` False means: do not use this association."""
    ok: bool
    reason: str = ""
    tau_s: Optional[float] = None
    pairs: List[Tuple[int, int]] = field(default_factory=list)
    n_pairs: int = 0
    margin: int = 0
    tau_spread_s: Optional[float] = None
    runner_up_tau_s: Optional[float] = None
    #: "count" when the pair count settled it, "amplitude" when peaks broke a count tie.
    decided_by: str = "count"
    #: dB by which the winner's median matched peak beat the runner-up's, when amplitude was used.
    amplitude_margin_db: Optional[float] = None

    def __bool__(self) -> bool:
        return self.ok


def _median_db(peaks: Sequence[float], idx: Sequence[int]) -> Optional[float]:
    """Median of the selected peaks in dB, or None if any is missing or non-positive.

    None rather than a substitute: a zero peak is an absent measurement, and treating it as
    -inf dB would let one missing value decide the tie.
    """
    if not idx:
        return None
    vals = []
    for k in idx:
        if k >= len(peaks):
            return None
        v = float(peaks[k])
        if not np.isfinite(v) or v <= 0:
            return None
        vals.append(20.0 * np.log10(v))
    return float(np.median(vals))


def _amplitude_prefers(a_pk, b_pk, cand_a, cand_b, margin_db):
    """Is `cand_a` louder than `cand_b` on every side that differs, by at least `margin_db`?

    ⚠️THE DIRECT PATH BEING LOUDER THAN ITS ECHO IS A PRIOR, NOT A LAW. A shadowed or
    off-axis node can receive a reflection stronger than the direct arrival. So this demands a
    real margin, demands the winner is not QUIETER on either side, and reports the margin it
    used so the decision can be second-guessed.

    Returns (prefers, margin_db) with margin None when amplitude cannot decide.
    """
    if a_pk is None or b_pk is None:
        return False, None
    best = None
    for peaks, ia, ib in ((a_pk, [i for i, _ in cand_a], [i for i, _ in cand_b]),
                          (b_pk, [j for _, j in cand_a], [j for _, j in cand_b])):
        if sorted(ia) == sorted(ib):
            continue                      # this side is the same set: it says nothing
        da, db_ = _median_db(peaks, ia), _median_db(peaks, ib)
        if da is None or db_ is None:
            return False, None
        d = da - db_
        if d < 0:
            return False, None            # quieter on a side that differs: refuse outright
        best = d if best is None else min(best, d)
    if best is None or best < margin_db:
        return False, best
    return True, best

def _monotone_pairs(a: np.ndarray, b: np.ndarray, tau: float, tol: float) -> List[Tuple[int, int]]:
    """Order-preserving greedy matching of `a + tau` against `b`, within `tol`.

    Order-preserving because both sensors hear one source's events in the same order: a matching
    that crosses (event 2 on A paired with an earlier B event than event 1 was) describes a
    reordering of time, which no geometry produces.
    """
    out: List[Tuple[int, int]] = []
    j = 0
    for i, t in enumerate(a):
        want = t + tau
        # advance past b-events that can no longer match this or any later a-event
        while j < len(b) and b[j] < want - tol:
            j += 1
        if j < len(b) and abs(b[j] - want) <= tol:
            out.append((i, j))
            j += 1
    return out


def associate_burst(t_a: Sequence[float], t_b: Sequence[float], max_tau_s: float,
                    tol_s: float, min_pairs: int = 3,
                    min_margin: int = MIN_MARGIN,
                    peak_a: Optional[Sequence[float]] = None,
                    peak_b: Optional[Sequence[float]] = None,
                    min_amp_margin_db: float = MIN_AMPLITUDE_MARGIN_DB) -> BurstMatch:
    """Fit one delay to a whole burst.

    `max_tau_s` is the geometry's bound, d/c. `tol_s` is what a pairing may be off by -- the
    onset noise, NOT the bound. Passing the bound as the tolerance admits everything.

    `peak_a`/`peak_b` are the per-event peak amplitudes the 172-byte frame already carries, one
    per event, in the same order as the times. When given they break a tie the pair COUNT could
    not -- the constant-echo case, where tau and tau+echo explain the arrivals equally well. The
    direct path is louder than its reflection, so the louder pairing is preferred.

    ⚠️AMPLITUDE ONLY EVER BREAKS A TIE. It cannot overturn a count: a hypothesis explaining more
    pairs wins regardless of how loud the loser is. Timing is the measurement; loudness is a
    prior, and a shadowed node really can hear a reflection louder than the direct arrival. So it
    also refuses when the winner is QUIETER on any side whose matched set differs, and demands
    `min_amp_margin_db` of separation on the MEDIAN of the matched set rather than any single
    event. `decided_by` says which rule settled it.
    """
    a = np.asarray(sorted(t_a), dtype=float)
    b = np.asarray(sorted(t_b), dtype=float)
    if len(a) < min_pairs or len(b) < min_pairs:
        return BurstMatch(False, "too few events: %d and %d, need %d each"
                          % (len(a), len(b), min_pairs))
    if tol_s >= max_tau_s:
        raise ValueError("tolerance %.4g s is not tighter than the bound %.4g s: every pairing "
                         "would match and the fit would mean nothing" % (tol_s, max_tau_s))

    cands = np.unique(np.round((b[None, :] - a[:, None]).ravel(), 9))
    cands = cands[np.abs(cands) <= max_tau_s]
    if cands.size == 0:
        return BurstMatch(False, "no pairing is inside the %.1f ms bound" % (max_tau_s * 1e3))

    scored = []
    for tau in cands:
        pairs = _monotone_pairs(a, b, float(tau), tol_s)
        if pairs:
            # re-centre on the pairs it actually made, so tau is their consensus and not the
            # single difference that happened to seed it
            refined = float(np.median([b[j] - a[i] for i, j in pairs]))
            scored.append((len(pairs), refined, pairs))
    if not scored:
        return BurstMatch(False, "no tau matched any pair within %.1f ms" % (tol_s * 1e3))
    scored.sort(key=lambda s: (-s[0], abs(s[1])))
    n, tau, pairs = scored[0]

    sep = TAU_SEPARATION_FACTOR * tol_s
    runner = next((s for s in scored if abs(s[1] - tau) > sep), None)
    margin = n - (runner[0] if runner else 0)

    if n < min_pairs:
        return BurstMatch(False, "best tau explains only %d pairs, need %d" % (n, min_pairs),
                          tau_s=tau, pairs=pairs, n_pairs=n, margin=margin)
    if margin < min_margin:
        prefers, amp_db = _amplitude_prefers(peak_a, peak_b, pairs, runner[2], min_amp_margin_db)
        if prefers:
            spread = float(np.ptp([b[j] - a[i] for i, j in pairs])) if len(pairs) > 1 else 0.0
            return BurstMatch(True, "", tau_s=tau, pairs=pairs, n_pairs=n, margin=margin,
                              tau_spread_s=spread, runner_up_tau_s=runner[1],
                              decided_by="amplitude", amplitude_margin_db=amp_db)
        extra = ""
        if peak_a is not None and peak_b is not None:
            extra = ("; amplitude did not settle it either (%s)"
                     % ("margin %.1f dB, need %.1f" % (amp_db, min_amp_margin_db)
                        if amp_db is not None else "peaks missing, equal, or favouring the runner-up"))
        return BurstMatch(False,
                          "ambiguous: tau %+.2f ms explains %d pairs and %+.2f ms explains %d -- "
                          "margin %d, need %d%s"
                          % (tau * 1e3, n, runner[1] * 1e3, runner[0], margin, min_margin, extra),
                          tau_s=tau, pairs=pairs, n_pairs=n, margin=margin,
                          runner_up_tau_s=runner[1], amplitude_margin_db=amp_db)
    spread = float(np.ptp([b[j] - a[i] for i, j in pairs])) if len(pairs) > 1 else 0.0
    return BurstMatch(True, "", tau_s=tau, pairs=pairs, n_pairs=n, margin=margin,
                      tau_spread_s=spread,
                      runner_up_tau_s=(runner[1] if runner else None))


def split_bursts(t: Sequence[float], gap_s: float = 1.0) -> List[List[int]]:
    """Indices grouped into bursts, split wherever the gap exceeds `gap_s`."""
    t = list(t)
    if not t:
        return []
    out = [[0]]
    for i in range(1, len(t)):
        if t[i] - t[i - 1] > gap_s:
            out.append([])
        out[-1].append(i)
    return out
