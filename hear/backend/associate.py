#!/usr/bin/env python3
"""Group per-node detections into events, bounded by the array's own geometry.

The window is COMPUTED -- array diameter / c plus a stated margin -- and it only bounds the scan.
The actual gate is pairwise: two nodes cannot disagree about one event by more than their own
separation allows. Stating that the other way round is how a window becomes the whole algorithm.

⚠️A FIXED WINDOW MANUFACTURES PHANTOMS. Measured round spacings are 85 ms at 700 rpm
(docs/validation-full-captures.md:35), ~328 ms burst cadence (docs/findings-2026-09-05.md:67-68)
and 522 ms for 19 rounds over 9.4 s (docs/validation-full-captures.md:15). A 600 ms guess contains
all three, so rounds merge into one event that fits at residual 0.00 and is hundreds of metres
wrong. 600 ms is never computed from anything; d/c is.

⚠️EARLIEST WINS, WITH NO REPLACEMENT. One detection per node per group. 60% of raw detections
fired within 60 ms of the previous one, inside a single blast's own decay
(docs/findings-2026-09-05.md:11), so a node latched onto a reflection must not displace the direct
arrival that already joined.

Nothing is dropped silently: n_input == sum(event n_nodes) + len(rejected) + len(duplicates), and
every rejection names its reason and carries the numbers that produced it.

⚠️A REFUSED CANDIDATE IS RETURNED TO THE POOL, AND THAT IS NOT THE SAME AS WIDENING ANYTHING.
A candidate the pairwise gate refuses is not consumed: it stays in the pool, so it can seed a
group of its own. Nothing about the gate moves. Measured on the live pool (74 h, 6,792 node
arrivals, 16.87 m arrival survey, window 78.7 ms): the ground truth is 191 admissible triples
forming 4 three-node episodes, of which 2 survive at zero margin. Consuming the refusal delivered
3 of the 4 and 1 of the 2; returning it delivers 4 of 4 and 2 of 2, and the median delivered
spread falls from 70.37 ms to 55.85 ms. Recovering an episode by widening the window would have
done the opposite: of the 162 distinct within-triple pairs those episodes contain, 64 (39.5%)
already exceed d/c and are admitted only by the margin.

⚠️A DUPLICATE IS NOT A REFUSAL, AND RELEASING IT WHOLESALE COSTS MORE THAN IT PAYS. A second
arrival from a node already in the group is released ONLY when the group can no longer use it at
all -- it already holds every node this batch heard from -- AND it is further from the arrival
holding that slot than that node's own bound (one node, so d = 0 and the bound is the margin).
Both clauses are derived, neither is tuned. Releasing every duplicate instead was measured on the
same pool: 7 deliveries instead of 5, NONE of them admissible at zero margin where the guarded
rule delivers 2, the median spread back up at 70.87 ms, and one episode delivered three times.
It also costs: 11,668 candidate visits against 3,180 for the guarded rule and 3,173 for consuming
every duplicate, so the guard buys the recovery for 0.2% more scan work where releasing
everything costs 3.7x. On the 169.7 m survey the same three numbers are 43,622 / 4,238 / 4,161.

⚠️THE GUARD IS WHAT MAKES A WIDE ARRAY WORK, AND WITHOUT IT A WIDE ARRAY LOSES EVERY ROUND BUT
THE FIRST. On a 169.7 m array (window 520 ms) with three rounds 85 ms apart that all four nodes
heard, consuming the duplicate delivers 1 event of 3 and releasing it delivers 3 of 3. The array
is going from 16.9 m to roughly 170 m, where the window goes 78.7 ms -> 520 ms, so this is the
scale the rule has to survive and tests/test_associate.py TestWideArray now pins the recovery.

⚠️THE SAME MEASUREMENT REFUSES BEST-SPREAD-WINS. Replacing earliest-wins with an exhaustive
search for the tightest admissible set inside the seed window was tried at all three consumption
policies and lost at every one: 0 deliveries admissible at zero margin in each case, against 1
for the shipped scan and 2 for this one. The mechanism is measured, not assumed: at
1789063974.230299 the incremental gate refuses every candidate and forms nothing, while the
exhaustive search finds a jointly admissible three-node set spanning 71.07 ms and consumes the
arrivals that the 55.85 ms and 12.94 ms groups later needed. Minimising spread over sets ANCHORED
AT THE SEED is not minimising spread: the seed is the floor, so the search buys width at the far
end to buy membership. Earliest-wins stays.

⚠️IT ALSO REFUSES POSSIBLE-FIRST TWO-PASS FORMING, AND NOT ON TERMINATION GROUNDS. The proposal
was to form only groups that stay point_source_possible at zero margin, then run this rule over
what is left, on the argument that a two-pass form "can only convert impossible deliveries into
possible ones". MEASURED on the live pool 2026-09-11 (7,067 admitted arrivals, 74 h), at the
margin the DRIVER derives from the array's own pair bounds -- which is what runs in production --
and again at this module's 30 ms default:

  margin 8.608 ms, window 57.740 ms   one-pass  3 events / 3 possible / 3,111 candidate visits
                                      two-pass  5 events / 5 possible / 6,260 candidate visits
  margin 30 ms,    window 79.132 ms   one-pass  5 events / 2 possible / 3,396 candidate visits
                                      two-pass  5 events / 2 possible / 6,863 candidate visits

At 30 ms the two deliveries are IDENTICAL, spread for spread -- and 30 ms is the only setting
measured here that has impossible groups in it. It converts nothing because those groups are
impossible from the SEED PAIR onward: pass one forms two nodes, fails min_nodes, hands every
member back, and pass two re-forms the same group. The 128.1 m and 169.7 m round fixtures are
unchanged at all three measured cadences (85 / 328 / 522 ms), so it buys nothing at the scale the
array is going to either. What it does do at the derived margin is MANUFACTURE two extra triples
out of arrivals pass one released, on a corpus whose shuffle null puts the observed triple count
at 0.63 +- 0.72 (p_emp 0.11) -- deliveries indistinguishable from chance -- for 2.0x the scan
work. The claim fails in both directions: it converts nothing and it is not free.

⚠️point_source_possible IS PAIRWISE AND THEREFORE NECESSARY, NEVER SUFFICIENT. It says no PAIR
in the group violates |dt| <= d/c; it does not say one point exists that fits all of them at
once. Measured on the same pool: the group seeded at 1789063974.287743 is possible at excess
0.0 ms and its three-node fit still runs to the search bound at 10.4 ms residual. So "prefer the
possible group" cannot discriminate between two groups that are both possible, which is exactly
the case this corpus presents. The joint question belongs to the solver, and the driver asks it.

⚠️WHAT THE DUPLICATE RELEASE COSTS, STATED RATHER THAN DISCOVERED LATER. It changes WHICH group
one episode delivers, and the one it picks is wider. Episode 3 of the live pool delivers twice
either way; its second delivery is rankine@1789063974.347535 + mach@.349619 + nyquist@.360475,
spread 12.94 ms, when the duplicate is consumed, and nyquist@.312460 + rankine@.323195 +
mach@.349619, spread 37.16 ms, when it is released -- the released nyquist@.312460 seeds first
and takes mach@.349619. Both are admissible at zero margin; the cost is 24.22 ms = 8.39 m of
spread on one of five deliveries. It is bought with 1 event of 3 -> 3 of 3 at 169.7 m, which is
where the array is going. RETURNING THE GEOMETRY REFUSAL ALONE IS STRICTLY BETTER AT TODAY'S
16.87 m and STRICTLY WORSE AT 169.7 m, and there is no third rule in between that is derived
from anything rather than tuned -- so the wide scale decides it.

⚠️refusals IS A COUNT, NOT ROWS, AND THAT IS DELIBERATE. A returned candidate can be refused by
every seed whose window it falls in, so a per-refusal row list is quadratic in the number of
arrivals one window holds -- on a 520 ms window and a node retriggering every 5 ms that is 4,950
rows per window per node. The per-detection story is still exactly-once and still carries its
numbers: it is in `rejected`, which no candidate reaches twice.

What this refuses to do: it does not classify, does not solve, and does not say whether a residual
is meaningful -- that depends on the model and the dimension and belongs to the solver. It also
does not resolve the genuinely ambiguous case, a node that missed a round on an array whose d/c
exceeds the round cadence; it rejects that detection and reports why.
"""
from __future__ import annotations

import itertools

from typing import Dict, List, Optional, Sequence

import numpy as np

from ..solve import shockwave as SW
from ..solve.consistency import physically_possible

# Slop beyond pure propagation. The gate reports the argmax over a 25 ms guard
# (hear/node/detect.py:22 GUARD_S), plus a few metres of survey error and temperature spread.
# JUDGEMENT, not measured. It must stay well under the tightest measured round spacing -- 85 ms at
# 700 rpm (docs/validation-full-captures.md:35) -- or consecutive rounds merge. 30 ms leaves 55 ms.
MARGIN_S: float = 0.030

REASONS: frozenset = frozenset({
    "unknown_node", "duplicate_seq", "duplicate_node_in_group",
    "pairwise_dt_exceeds_geometry", "too_few_nodes", "unusable_arrival"})

# ---------------------------------------------------------------------------------------------
# ARRIVAL QUALITY. A detection can be perfectly real and still not carry a usable arrival TIME,
# and until now there was no door to refuse one at -- every detection that named a known node was
# admitted, and any defect in its timestamp went straight into the solve.
#
# Two producers already know when their own timestamp is not a measurement and had nowhere to say
# so:
#
#   onset_found   The node gate walks back from the envelope peak to a constant fraction of it.
#                 When the envelope never falls that far the walk reaches the clamp and returns
#                 the CLAMP EDGE, which is numerically indistinguishable from a genuine slow
#                 rise -- 25 ms early, 8.6 m of range at 343 m/s.
#
#                 ⚠️THIS IS NOW A BACKSTOP, NOT THE MITIGATION. When this gate was written the
#                 case was 42 of the 228 reference events (18.4%) -- retriggers sitting inside
#                 the previous round's decay tail, where the envelope never falls to 20% of the
#                 NEW peak because the old one is still ringing. detect.onset_index_checked now
#                 refers the fraction to the LOCAL TROUGH instead of to zero, which takes that
#                 count to ZERO on the same corpus: those events are timed rather than refused,
#                 which is strictly better than discarding 18.4% of them.
#
#                 It still fires, and must stay: the trough reference is deliberately NOT applied
#                 when the window's minimum sits at its left edge, because that means the rise
#                 predates the window and referring to it would report a confidently LATE onset.
#                 Those are still clamp edges, and they are still not measurements.
#
#   utc_trusted   The phone's audio path. False means the HAL supplied no AudioTimestamp or the
#                 GPS anchor was stale, so the stamp still carries the input-buffer and HAL
#                 latency -- tens of milliseconds, constant per handset.
#
#   stamp_admissible
#                 The producer's own `sync_sigma_ns` against that producer's CLASS budget,
#                 resolved by `nodeclass.NodeClass.stamp_admissible`. False means the stamp was
#                 made from an anchor old enough that the free-running crystal has eaten the
#                 arrival budget: a XIAO node latches `time_valid` true at its first NAV-PVT and
#                 NEVER clears it, so when the GPS UART dies it keeps stamping, drifting at a
#                 MEASURED 4.2-11.7 ppm -- about 30 ms per hour, and invisible in every other
#                 counter the node exports.
#
#                 ⚠️A BOOLEAN, NOT A THRESHOLD, AND THAT IS THE POINT. The comparison needs the
#                 receiver's CLASS (the budget is `sqrt(class t_sigma**2 + stated sigma**2)`
#                 against ARRIVAL_T_SIGMA_MAX_S) and this module has never known the class. A
#                 raw `sync_sigma_ns > constant` test here would be a SECOND threshold on a
#                 quantity nodeclass already owns, and two thresholds for one quantity is how
#                 this repo has been wrong before. The caller resolves it -- tools/hear_tdoa.py
#                 does, off the survey's class -- exactly as it already resolves `utc_trusted`
#                 into a real boolean instead of passing the stored field down.
#
#                 ⚠️THE BOOLEAN IS NOT THE ONLY THING THAT QUANTITY IS GOOD FOR. Spending a
#                 stated sigma entirely on a pass/fail throws away everything it says about
#                 receivers that PASS. The caller also resolves it into `t_sigma_s` (seconds),
#                 which this module carries into `event["arrival_sigma_s"]`, index-aligned with
#                 `arrivals`, for a solver to weight on. Carrying is all it does: the conversion
#                 needs the class, for the reason in the paragraph above.
#
# ⚠️THE TIMESTAMP IS NOT MOVED. Rewriting arrival times that are already in a shipped pipeline
# would change every historical answer silently, which is worse than the defect. This refuses the
# detection instead, by the same discipline as nodeclass.require_arrival() and survey's load-time
# raises: a measurement that is known to be wrong is refused at the door, not corrected in place
# and not quietly averaged in.
#
# ⚠️ABSENT MEANS USABLE. A producer that does not publish these fields is not thereby suspect --
# most of them predate the fields. Only an EXPLICIT false is a refusal, so this is a strict no-op
# on every detection recorded before the producers started emitting them.
_QUALITY_FLAGS = ("onset_found", "utc_trusted", "stamp_admissible")


def arrival_is_usable(d: Dict) -> bool:
    """True unless a producer has explicitly said its own timestamp is not a measurement."""
    return all(d.get(k, True) is not False for k in _QUALITY_FLAGS)


def _unusable_reason(d: Dict) -> str:
    bad = [k for k in _QUALITY_FLAGS if d.get(k, True) is False]
    detail = {
        "onset_found": "the constant-fraction onset was never crossed even against the local "
                       "trough, so the timestamp is the clamp edge and not a measurement "
                       "(~25 ms early, ~8.6 m)",
        "utc_trusted": "no HAL audio timestamp or no fresh GPS anchor, so the stamp still "
                       "carries the input-buffer and HAL latency",
        "stamp_admissible": "the producer's own stated clock sigma puts this detection over its "
                            "class's per-node arrival budget -- a stale time anchor free-running "
                            "on the local crystal, not a bad detection",
    }
    return "node %d at %.6f s: %s" % (
        int(d["node_id"]), float(d["t_utc_s"]),
        "; ".join("%s=false -- %s" % (k, detail[k]) for k in bad))


def _xyz(p) -> np.ndarray:
    """3-vector from a survey position. 2-vectors are treated as up=0.

    Distances here are propagation paths, not the solvers' 2D projection, so nothing is sliced:
    the 3D separation is the larger bound and admitting is the conservative direction.
    """
    v = np.asarray(p, float).ravel()
    return v if v.size == 3 else np.array([v[0], v[1], 0.0])


def _row(det: Dict, reason: str, detail: str, seed: Optional[Dict] = None) -> Dict:
    return {
        "node_id": int(det["node_id"]), "seq": int(det["seq"]),
        "t_utc_s": float(det["t_utc_s"]),
        "reason": reason, "detail": detail,
        "seed_node_id": None if seed is None else int(seed["node_id"]),
        "seed_t_utc_s": None if seed is None else float(seed["t_utc_s"]),
    }


def max_window_s(survey, temp_c: float = 20.0, margin_s: float = MARGIN_S) -> float:
    """Widest defensible spread of one event across the array, in seconds.

    diameter / c + margin. NEVER a constant: on a 10 m array this is 59 ms and on a 300 m array
    904 ms, and a single number cannot be right for both.
    """
    return float(survey.diameter_m()) / SW.sound_speed(temp_c) + float(margin_s)



def _point_source_excess_s(group, p_of, c: float) -> float:
    """How far the worst pair in `group` sits OUTSIDE |dt| <= d/c. 0.0 means inside it.

    ⚠️THIS IS THE ZERO-MARGIN BOUND, AND IT IS NOT THE ONE THE GROUPER ADMITS ON. The grouper
    adds MARGIN_S to every pair because onset picking and the survey both carry error. That slop
    is 30 ms = 10.3 m against an array 16.87 m across, so a group can clear the admission test
    and still describe arrivals no single point source anywhere could have produced.

    MEASURED on the live pool 2026-09-11, 7,073 anchored arrivals over 74 h: of the four
    three-node events delivered, THREE span 55.85, 70.37 and 74.70 ms against a largest pair
    bound of 48.7 ms. Not marginal -- past the widest bound the array has. Only the 39.88 ms
    event at 2026-09-09T11:08:41.341Z is possible at zero margin.

    Reported, not refused: whether 30 ms of slop is right is a survey-and-onset question, and
    dropping three quarters of the deliveries is not a decision this function gets to make. It
    makes the number visible so the decision can be taken on evidence.

    `physically_possible` is imported rather than re-derived -- one bound, one definition.
    """
    worst = 0.0
    for a, b in itertools.combinations(group, 2):
        dt = abs(float(a["t_utc_s"]) - float(b["t_utc_s"]))
        d_m = float(np.linalg.norm(p_of(int(a["node_id"])) - p_of(int(b["node_id"]))))
        if not physically_possible(dt, d_m, c, tol_s=0.0):
            worst = max(worst, dt - d_m / c)
    return worst


def associate(detections: Sequence[Dict], survey, temp_c: float = 20.0,
              margin_s: float = MARGIN_S, min_nodes: int = 3,
              window_s: Optional[float] = None) -> Dict:
    """Group detections into events. Reads only `node_id`, `seq` and `t_utc_s`; carries the rest.

    `survey` is duck-typed: `node_id in survey`, `survey.position(node_id)`, `survey.diameter_m()`.
    `window_s` None means computed. An explicit value exists so a test can prove what a too-wide
    one costs; production passes None.

    Refuses to drop anything: every input detection ends in exactly one of `events`, `rejected` or
    `duplicates`. Refuses to admit a second detection from a node already in a group, and refuses
    any candidate whose separation-in-time from a member exceeds their separation-in-space over c.
    Refuses to treat a repeated (node_id, seq) as a duplicate once it is a window away: seq wraps.

    A refused candidate is NOT consumed and can seed a group of its own; a duplicate is consumed
    unless the group already holds every node this batch heard from AND the duplicate is further
    from the arrival holding its slot than that node's own bound. `refusals` counts the
    non-terminal ones by reason and `scan_seeds` / `scan_candidate_visits` are the scan's own
    work, which is what a revisiting implementation would blow out.
    """
    c = SW.sound_speed(temp_c)
    diameter_m = float(survey.diameter_m())
    # One definition of the policy, so max_window_s() cannot drift away from what the scan uses.
    window_s = float(max_window_s(survey, temp_c, margin_s) if window_s is None else window_s)

    rejected: List[Dict] = []
    duplicates: List[Dict] = []

    known = []
    for d in detections:
        if int(d["node_id"]) not in survey:
            rejected.append(_row(d, "unknown_node", "node %d at %.6f s is not in the survey"
                                 % (int(d["node_id"]), float(d["t_utc_s"]))))
        elif not arrival_is_usable(d):
            rejected.append(_row(d, "unusable_arrival", _unusable_reason(d)))
        else:
            known.append(d)
    known.sort(key=lambda d: (float(d["t_utc_s"]), int(d["node_id"]), int(d["seq"])))

    pool: List[Dict] = []
    first_seen: Dict = {}
    for d in known:
        key = (int(d["node_id"]), int(d["seq"]))
        t = float(d["t_utc_s"])
        prior = first_seen.get(key)
        # ⚠️seq is an 8-bit field that wraps (hear/wire.py:113) and a rebooted node restarts its
        # counter, so an unbounded (node_id, seq) dedupe silently eats genuine later events. A
        # repeat is the same frame only if it could still have joined the same group -- one window.
        # `known` is sorted by time, so `prior` is the most recent accepted copy.
        if prior is not None and t - prior <= window_s:
            duplicates.append(_row(d, "duplicate_seq",
                                   "node %d seq %d already accepted at %.6f s; this copy %.6f s"
                                   % (key[0], key[1], prior, t)))
        else:
            first_seen[key] = t
            pool.append(d)

    pos: Dict[int, np.ndarray] = {}

    def _p(node_id: int) -> np.ndarray:
        if node_id not in pos:
            pos[node_id] = _xyz(survey.position(node_id))
        return pos[node_id]

    events: List[Dict] = []
    refusals: Dict[str, int] = {r: 0 for r in REASONS}
    # Every node this batch heard from. `survey` is duck-typed on membership and cannot be
    # enumerated, and a surveyed node that reported nothing can never complete a group anyway.
    reporting_nodes = len({int(d["node_id"]) for d in pool})
    scan_seeds = 0
    scan_visits = 0
    # ⚠️A NON-TERMINAL REFUSAL MUST NOT COST THE DIAGNOSIS. A candidate the geometry gate refuses
    # now goes back to the pool and usually ends as `too_few_nodes` around itself, so the numbers
    # that refused it would otherwise never be printed anywhere. They ride along in the detail.
    last_refusal: Dict[int, str] = {}
    # ⚠️THE SEED IS COMMITTED BEFORE ANY CANDIDATE CAN BE RELEASED, AND `used` ONLY EVER GOES
    # False -> True. That is the termination argument, and mutation says it is the WHOLE of it:
    # replacing this pass with a worklist, scanning from index 0 instead of i + 1, or pushing a
    # released index back on the queue all leave every answer and every counter unchanged,
    # because a released index is already `used` by the time anything reaches it again. Move
    # `used[i] = True` below the release and the same worklist never terminates -- measured, the
    # test file hangs. So this line is not bookkeeping.
    used = [False] * len(pool)
    for i, seed in enumerate(pool):
        if used[i]:
            continue
        scan_seeds += 1
        used[i] = True
        group = [seed]
        group_ix = [i]
        members = {int(seed["node_id"])}
        limit = float(seed["t_utc_s"]) + window_s
        for j in range(i + 1, len(pool)):
            if used[j]:
                continue
            cand = pool[j]
            t_c = float(cand["t_utc_s"])
            if t_c > limit:
                break
            scan_visits += 1
            nid = int(cand["node_id"])
            if nid in members:
                held = next(m for m in group if int(m["node_id"]) == nid)
                gap = t_c - float(held["t_utc_s"])
                # Same node, so d = 0 and the pair's own bound is 0/c + margin. Inside it this is
                # a second reading of the arrival already held -- the retrigger the whole
                # earliest-wins rule exists for -- and it stays terminal. Outside it, AND only
                # once the group holds every reporting node so it can never use the arrival
                # anyway, it goes back to the pool.
                release = (len(members) >= reporting_nodes
                           and abs(gap) > 0.0 / c + float(margin_s))
                if release:
                    refusals["duplicate_node_in_group"] += 1
                    last_refusal[j] = ("node %d was already in the group seeded at %.6f s, "
                                       "%.1f ms earlier, and that group held all %d reporting "
                                       "nodes: returned to the pool"
                                       % (nid, float(seed["t_utc_s"]), gap * 1e3,
                                          reporting_nodes))
                    continue
                used[j] = True
                rejected.append(_row(cand, "duplicate_node_in_group",
                                     "node %d already in event at %.6f s, %.1f ms earlier: "
                                     "earliest wins, no replacement"
                                     % (nid, float(held["t_utc_s"]), gap * 1e3), seed))
                continue
            bad = None
            for m in group:
                dt = abs(t_c - float(m["t_utc_s"]))
                d_m = float(np.linalg.norm(_p(nid) - _p(int(m["node_id"]))))
                bound = d_m / c + float(margin_s)
                if dt > bound:
                    bad = (int(m["node_id"]), dt, bound, d_m)
                    break
            if bad is not None:
                refusals["pairwise_dt_exceeds_geometry"] += 1
                last_refusal[j] = ("node %d vs node %d: dt %.1f ms > %.1f ms "
                                   "(d %.1f m / c %.1f + %.0f ms)"
                                   % (nid, bad[0], bad[1] * 1e3, bad[2] * 1e3, bad[3], c,
                                      float(margin_s) * 1e3))
                continue
            used[j] = True
            members.add(nid)
            group.append(cand)
            group_ix.append(j)

        if len(group) < min_nodes:
            for k, m in zip(group_ix, group):
                why = last_refusal.get(k)
                rejected.append(_row(m, "too_few_nodes",
                                     "group of %d around node %d at %.6f s: min_nodes %d%s"
                                     % (len(group), int(seed["node_id"]),
                                        float(seed["t_utc_s"]), min_nodes,
                                        "" if why is None else "; last refused: " + why), seed))
            continue
        arrivals = [float(m["t_utc_s"]) for m in group]
        excess = _point_source_excess_s(group, _p, c)
        events.append({
            "event_id": len(events),
            "t0_utc_s": arrivals[0],
            "node_ids": [int(m["node_id"]) for m in group],
            "arrivals": arrivals,
            # ⚠️INDEX-ALIGNED WITH `arrivals`, SECONDS, AND None MEANS THE PRODUCER DID NOT STATE
            # ONE. Carried, never computed: turning a stated `sync_sigma_ns` into a total sigma
            # needs the receiver's CLASS, which this module has never known -- the same reason
            # `stamp_admissible` arrives as a resolved boolean. tools/hear_tdoa.py resolves it
            # through nodeclass and puts `t_sigma_s` on the detection; this carries it to the
            # solver so a receiver that states what its stamp is worth can be weighted on that
            # statement instead of voting at par with every other receiver.
            "arrival_sigma_s": [(None if m.get("t_sigma_s") is None
                                 else float(m["t_sigma_s"])) for m in group],
            "detections": list(group),
            "n_nodes": len(group),
            "n_equations": len(group) - 1,      # t0 cancels in TDoA
            "span_s": arrivals[-1] - arrivals[0],
            "point_source_possible": excess <= 0.0,
            "worst_pair_excess_s": excess,
        })

    return {
        "events": events,
        "rejected": rejected,
        "duplicates": duplicates,
        "window_s": window_s,
        "margin_s": float(margin_s),
        "sound_speed_mps": c,
        "diameter_m": diameter_m,
        "n_input": len(detections),
        "refusals": refusals,
        "reporting_nodes": reporting_nodes,
        "scan_seeds": scan_seeds,
        "scan_candidate_visits": scan_visits,
        "events_point_source_possible": sum(1 for e in events if e["point_source_possible"]),
    }
