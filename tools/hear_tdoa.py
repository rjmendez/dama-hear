#!/usr/bin/env python3
"""Drive the shipped TDoA solvers over the pool, and account for every record that did not solve.

    python3 tools/hear_tdoa.py --pool ~/hear-pool --source-class blast          # solve
    python3 tools/hear_tdoa.py --pool ~/hear-pool --source-class blast --census # read-only
    python3 tools/hear_tdoa.py --pool ~/hear-pool --plan-only                   # survey only
    python3 tools/hear_tdoa.py --pool ~/hear-pool --check                       # gate, exit 0/1

WHY THIS EXISTS. `hear/solve/{point,shockwave,placement,consistency,soundspeed,calibrate}.py` and
`hear/backend/{survey,associate,pipeline}.py` are written, tested and have ZERO callers outside
tests/. The pool holds 11k sketches with absolute timestamps. Nothing joined the two. This file is
the join and it reimplements none of them: geometry, association, sound speed, DOP and the
determinacy counting all reach it from those modules.

⚠️THE PRODUCT IS THE FUNNEL, NOT THE EVENTS. Every pool row reaches exactly one terminal verdict
and the run succeeds when it can explain why zero solved. `n_solved == 0` is exit 0. A run that
cannot account for its own inputs is exit 2. On the corpus this was written against the honest
answer is zero: across 30 one-minute bins in which all three surveyed nodes were simultaneously
detecting, there were 0 three-node coincidences that satisfy their own pairwise geometry at
zero margin. A tool whose only output is `events.jsonl` reports that as an empty directory.

WHAT THIS CONTRIBUTES that no shipped module has, and nothing else:

  1. the pool -> arrival adapter (`admit`), including the four traps the pool schema sets
  2. the ARRIVAL SUB-SURVEY (`arrival_survey`), which routes around two verified library defects
  3. an UNGATED coincidence scan (`scan_coincidences`), so "coincident in time" and "consistent
     with a point source" are two separately countable populations -- `associate()` conflates
     them by design, and the two need opposite field responses
  4. an arrival-stream shuffle null (`shuffle_null`), so a coincidence count has a referent
  5. the attribution ledger

⚠️`Backend.ingest()` IS NOT USABLE HERE AND `Backend.flush()` IS MIRRORED, NOT CALLED.
`ingest()` takes raw wire bytes and calls `wire.decode()`, which requires a v2 frame carrying
node_id/seq/us_of_day. Pool records store v1 SKETCH bytes (`hear.sketch.unpack`, no node id and no
seq in the frame at all -- which is exactly why the pool stores `node` and `ts_utc_s` as separate
JSON columns beside the frame), so feeding a pool frame to `ingest()` gets `v1_frame_has_no_node_id`.
This driver therefore composes at the `associate()` layer and reproduces ~15 lines of `flush()`'s
model dispatch. `TestTheModelChoiceMatchesPipeline` pins the two together. The clean fix upstream
is to extract `pipeline.solve_event(...)` from `flush()` and have both call it; that is a change to
pipeline.py and is deliberately not made here.

⚠️TWO VERIFIED LIBRARY DEFECTS ARE ROUTED AROUND, NOT FIXED, AND THE DISCREPANCY IS REPORTED.
  (a) `associate.max_window_s` sizes the scan window off `survey.diameter_m()`, which maxes over
      ALL surveyed ids -- including `puc`, which `Survey.arrival_ids()` refuses and which
      survey.json marks PROVISIONAL at sigma 6.5 m. On this survey that is 99.6 ms where the
      arrival nodes alone give 79.1 ms: 26% too wide. It is not merely conservative. Measured on
      the live corpus: `associate()` returns 3 events at 79.1 ms and 2 at 99.6 ms, because the
      wider seed absorbs a wrong partner and the true third member is then lost to
      `duplicate_node_in_group`. A node that cannot contribute an arrival must not set the window.
  (b) `associate()`'s membership test is `int(d["node_id"]) in survey` -- plain position-dict
      membership. It never consults `arrival_ids()`, so a `puc` detection would enter the geometry
      beside sigma 0.5 m nodes. This driver pre-filters; `nodeclass.require_arrival` supplies the
      refusal message so the wording cannot drift from the module that owns it.
Both are closed by building a SUB-SURVEY of the arrival nodes and passing that to `associate()`.
`manifest.json` carries `window_s`, `window_s_full_survey` and `window_inflation_frac` so the
library defect stays visible instead of being silently masked -- those numbers are the evidence
for the eventual PR against associate.py.

⚠️A THIRD DEFECT IS CLOSED BEFORE THE GATE, NOT AT IT. `associate.arrival_is_usable` reads
`d.get(k, True) is not False`, which admits None. `utc_trusted` is null on ALL 5,505 phone rows in
this pool, so the gate written specifically for the phone audio path passes 100% of it -- the
docstring's "ABSENT MEANS USABLE / only an EXPLICIT false is a refusal" rule has a third state it
does not handle. `admit()` resolves that third state through `corpus.utc_trusted_of` (the derived
rung, NOT the raw stored field) and hands `associate()` a real boolean, so its refusal path is live
rather than decorative.

⚠️MARGIN_S IS A GROUPING HEURISTIC, AND ITS FLOOR IS NOT THE CLOCK. `associate.MARGIN_S = 0.030`
was dimensioned against 85 ms round spacing and never against aperture; on this 16.87 m array it
is 61% of the widest pair bound and 87% of the tightest, so the pairwise gate admits groups that
cannot be one point source. This tool derives a margin from the array's own pair bounds. But the
margin cannot simply be shrunk to the timing budget: PPS is 25-40 ns and
`nodeclass.get("xiao-s3-pps").range_sigma_m(c)` is about 0.034 m -- roughly 20x SMALLER than the
0.49-0.717 m survey sigma -- while the binding term, INTER-NODE ONSET-DETECTION JITTER, has never
been measured on this fleet. So admissibility is judged separately, at tol_s = 0, and every excess
is reported in metres. This file does not claim the derived margin is correct; it claims only that
it is dimensioned to the aperture and that the binding uncertainty is unmeasured.

WHAT IT WRITES, all under `--out` (default `<pool>/tdoa`) and NEVER inside the checkout:

    <out>/runs/<run_id>/manifest.json     policy, survey, window, funnel, onset_quality,
                                          conservation, not_composed, could_not_do, timings
    <out>/runs/<run_id>/coactivity.json
    <out>/runs/<run_id>/candidates.jsonl  ungated scan + bound_check + reached_associate
    <out>/runs/<run_id>/attempts.jsonl    one row per candidate, ALWAYS a verdict
    <out>/runs/<run_id>/events.jsonl      solved AND strictly admissible only
    <out>/runs/<run_id>/null.json
    <out>/runs/<run_id>/plan.json
    <out>/arrivals/<day>/<source>.jsonl   the per-record ledger (append, deduped)
    <out>/latest.json                     {"run_id","at"} -- a FILE, not a symlink (k8s-safe)
    <out>/model_card.json
    <out>/state/tdoa_heartbeat.json       ring of the last 64 runs

⚠️ASSOCIATION IS NOT A PER-RECORD FUNCTION, so there is no per-record resume for it. The whole
`--lookback-h` window is re-associated every run and emission is deduped on

    event_key = sha256("\\x1f".join(sorted(member pool_keys)))[:32]

a content address of the member SET, and the keys it is deduped AGAINST are read from
`runs/*/events.jsonl` -- the emission log -- and from nowhere else. ⚠️NOT from the arrival
ledger, which carries an event_key for every arrival of every CANDIDATE whatever its verdict:
reading it as an emission log let a run that REFUSED a group burn that member set's key
permanently, so the next run solved it, filtered it against its own refusal, and wrote a 0-byte
events.jsonl under a manifest claiming an emission. See `read_emitted_events`.

Consequences, published rather than hidden: a backfill that adds a member produces a NEW
event_key and the superseded row stays -- counted `event_membership_changed` and NAMED in
`superseded_event_keys`, which is a count of new events reusing an arrival already published
under a different key, not a count of new events; a backfill older than the lookback can never be associated (counted
`outside_lookback_unassociated`, which is a hard `--check` failure -- the one thing a lookback
costs, made loud). The per-record ledger resumes on sha256(pool_key|verdict|reason|event_key), so a
record is written once per DISTINCT verdict and a verdict that later changes appends a transition
row rather than rewriting history. A file/offset/timestamp watermark would be wrong for the same
measured reason hear_score.py:64 gives: a later drain was observed appending 513 rows INTO an
already-scanned partition.

⚠️EVERY ARRIVAL IN THIS POOL CARRIES AN UNKNOWN ONSET QUALITY, and the run says so in a number
rather than passing them in silence. `onset_quality` in the manifest is the distribution over the
three states -- crossed, not crossed, and never measured -- per source. Today the third bucket is
100% of the admitted stream, because no producer in the chain measures the quantity at all: see
_ONSET_UNSTATED_WHY for the four places that was checked. `--onset-unstated refuse` prices it.

⚠️--census WRITES NOTHING AT ALL, which is a deliberate deviation from "writes only the run
summary". The check CronJob runs `--census || true` after the gate, and a read-only gate job that
writes is a second writer nobody expects. `--no-write` is the flag for "compute everything, write
nothing".

WHAT A ZERO-EVENT RUN IS NOT. It is not evidence of no gunshots. The referent is the co-activity
number, which is printed FIRST and short-circuits the whole run when it is zero: a coincidence
rate over zero opportunity is a number with no referent. See `<out>/model_card.json`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import corpus as C                                              # noqa: E402
from hear import nodeclass as NC                                          # noqa: E402
from hear import pool as POOL                                             # noqa: E402
from hear.backend import associate as AS                                  # noqa: E402
from hear.backend import pipeline as BP                                   # noqa: E402
from hear.backend import survey as SV                                     # noqa: E402
from hear.solve import calibrate as CAL                                   # noqa: E402
from hear.solve import consistency as CO                                  # noqa: E402
from hear.solve import placement as PL                                    # noqa: E402
from hear.solve import point as PT                                        # noqa: E402
from hear.solve import shockwave as SW                                    # noqa: E402
from hear.solve import soundspeed as SS                                   # noqa: E402

TDOA_SCHEMA = "hear.tdoa_attempt.v1"
LEDGER_SCHEMA = "hear.tdoa_arrival.v1"

#: Runs kept in the heartbeat ring. hear_score.py:133 uses the same number for the same reason:
#: the check job runs a quarter as often as the solver, so a ring is what a gate can sum over.
RUN_RING = 64
DEFAULT_RUN_WINDOW_S = 7200.0
DEFAULT_MAX_STALE_S = 7200.0

#: How far back every run re-associates. Association is not a per-record function, so this is a
#: real cost and it is measured rather than hidden: a record that arrives older than this can
#: never be associated, and `--check` fails on it.
DEFAULT_LOOKBACK_H = 72.0
#: A record newer than this is not yet associated, because its partners may still be draining.
#: Live hear-drain job durations have been measured from 3m58s to 67m against a 15-minute
#: schedule (deploy/k8s/hear-score.yaml), so an hour is not enough slack.
DEFAULT_SETTLE_S = 7200.0

#: The derived margin is capped at this fraction of the TIGHTEST pair bound. JUDGEMENT, and the
#: judgement is about the aperture, not about the clock -- see the module docstring.
DEFAULT_MARGIN_FRAC = 0.25
#: Refuse an operator-supplied margin above this fraction of the tightest bound without --force.
HARD_MARGIN_FRAC = 0.5

DEFAULT_BIN_S = 60.0
DEFAULT_NULL_TRIALS = 200
DEFAULT_NULL_SEED = 20260910
DEFAULT_TARGET_EVENTS = 20

#: Stated per-arrival sync sigma above which the arrival is refused, as a fraction of the
#: tightest pair bound. A node whose own stated uncertainty is a tenth of the bound it has to
#: resolve is not contributing information to that pair.
DEFAULT_SYNC_SIGMA_FRAC = 0.10

# ---------------------------------------------------------------- drop reasons (admit())
D_UNANCHORED = "unanchored"
D_OUTSIDE_WINDOW = "outside_window"
D_OUTSIDE_LOOKBACK_EMITTED = "outside_lookback_emitted"
D_OUTSIDE_LOOKBACK_UNASSOC = "outside_lookback_unassociated"
D_PENDING_SETTLE = "pending_settle"
D_UNSURVEYED = "unsurveyed_node"
D_NOT_ARRIVAL = "not_arrival_class"
D_CLOCK_UNSTATED = "clock_unstated"
D_CLOCK_UNTRUSTED = "clock_untrusted"
D_SYNC_SIGMA = "sync_sigma_exceeds"
D_ONSET = "onset_not_found"
D_ONSET_UNSTATED = "onset_unstated"
D_LATENCY = "latency_uncorrected"
D_UNPARSEABLE = "line_unparseable"
D_ADMITTED = "admitted"

DROP_REASONS = (D_UNANCHORED, D_OUTSIDE_WINDOW, D_OUTSIDE_LOOKBACK_EMITTED,
                D_OUTSIDE_LOOKBACK_UNASSOC, D_PENDING_SETTLE, D_UNSURVEYED, D_NOT_ARRIVAL,
                D_CLOCK_UNSTATED, D_CLOCK_UNTRUSTED, D_SYNC_SIGMA, D_ONSET, D_ONSET_UNSTATED,
                D_LATENCY, D_UNPARSEABLE)

#: Why an admitted arrival's onset quality is UNSTATED, stated once so the ledger detail, the
#: manifest and the CLI help cannot drift apart. Every clause was checked on 2026-09-10 against
#: the running system, not read off a comment:
#:
#:   * hear/pool.py `_record_from_node_row` builds a source=node record from the dets.csv row
#:     plus the decoded sketch frame and writes no onset field at all. `row.get("onset_found")`
#:     is therefore None on every node row ever ingested -- not sometimes, always. The gate this
#:     replaces read that None as "not False" and passed 100% of the node corpus in silence.
#:   * The three deployed nodes (nyquist .105, mach .116, rankine .50) each answer
#:     GET /sd?file=/dets.csv with the G5 header
#:     node_id,utc_us,uptime_s,sample,pps_n,us_since_pps,trigger,flags,fs_hz,sketch_back,
#:     frame_hex,clip,clip_why -- no onset column, and hear/detsfile.py declares none in any
#:     generation G1..G5.
#:   * The sketch frame cannot carry it either. hear/sketch.py spends flags bits 0-7 on event
#:     flags (bit 0 retrigger, bit 1 no-context), 8-11 on the sample-rate code and 12 on layout;
#:     hear/node/pipeline.py packs `flags=(1 if d["retrigger"] else 0)` and nothing else. The
#:     Python detector DOES compute the crossing verdict -- hear/node/detect.py's
#:     `onset_index_checked` returns it and the gate puts it in the detection dict -- and it
#:     dies at the wire boundary, a 172-byte format shared byte-for-byte with the Android
#:     producer.
#:   * The node firmware never computes it at all: firmware/night_node/night_node.ino derives
#:     the stamp with `sk_onset_acq_at(acq_base, i, DECIM, DECIM_DELAY)`, index arithmetic on
#:     the gate-crossing sample back-dated by the decimator group delay. There is no
#:     constant-fraction search on the node, so there is no verdict to carry.
#:
#: SO "MAKE THE PRODUCER STATE IT" IS NOT A PLUMBING FIX. It is a new dets.csv generation or a
#: wire-format bit, a firmware change on three field nodes, a matching change in the Android
#: producer and a pool schema bump -- and even done perfectly it would state nothing about the
#: records already in the pool, which is the entire corpus every result here rests on. What the
#: driver can do is refuse to call an unmeasured quantity a passing measurement, and publish the
#: count.
_ONSET_UNSTATED_WHY = (
    "onset quality is UNSTATED: nothing in this chain measures it. dets.csv (G5) has no onset "
    "column, the 172-byte sketch frame has no bit for one, and the node firmware picks the "
    "stamp from the gate crossing with no constant-fraction test. Admitted under "
    "--onset-unstated admit and counted in the run's onset_quality block; that is NOT evidence "
    "the stamp is good")

# ---------------------------------------------------------------- verdicts (attempts.jsonl)
V_SOLVED = "solved"
V_SOLVER_REFUSED = "solver_refused"
V_POSITION_UNOBSERVABLE = "position_unobservable"
V_AT_SEARCH_BOUND = "at_search_bound"
V_MARGIN_DEPENDENT = "margin_dependent"
V_INADMISSIBLE = "inadmissible"
V_LOST_TO_GATE = "lost_to_gate"

VERDICTS = (V_SOLVED, V_SOLVER_REFUSED, V_POSITION_UNOBSERVABLE, V_AT_SEARCH_BOUND,
            V_MARGIN_DEPENDENT, V_INADMISSIBLE, V_LOST_TO_GATE)

# ---------------------------------------------------------------- binding constraint
B_NO_RECORDS = "no_records"
B_CO_ACTIVITY = "co_activity"
B_ADMISSIBILITY = "admissibility"
B_SOLVED = "solved"


class Refusal(Exception):
    """Exit 1. The tool will not run at all, and NOTHING is written.

    Every one of these is arithmetic that no amount of future data changes -- a survey that does
    not load, fewer than three arrival-class receivers, a 3D fit with three receivers, a source
    class the model gate does not know, an --out inside the checkout. Same discipline as
    SurveyError raising at load time rather than decorating every later answer.
    """


class NotInterpretable(Exception):
    """Exit 2. The run happened but could not account for its own inputs, so its output is not
    evidence of anything. Distinct from Refusal: this one means something is WRONG, where a
    Refusal means the question was malformed."""


# ================================================================= small shared helpers

def _jsonable(o: Any) -> Any:
    """numpy scalars/arrays and tuples into JSON. Solver dicts are copied VERBATIM through this
    and never retyped: a hand-written copy of a result field is a second definition of what the
    solver said, and the two drift."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_jsonable(v) for v in o.tolist()]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o


def _write_json_atomic(path: str, obj: Any) -> None:
    """tmp + os.replace. A torn heartbeat is a check that reads nothing and passes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(_jsonable(obj), fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _day_of(ts_utc_s: Optional[float]) -> str:
    """Same partition rule as hear.pool._day, so the arrival ledger and the record store agree
    about which day a row belongs to. Unanchored rows never reach here -- they are dropped in
    admit() -- but the branch is kept so a future caller cannot get a guessed day."""
    if not ts_utc_s:
        return "unanchored"
    return _dt.datetime.fromtimestamp(ts_utc_s, _dt.timezone.utc).strftime("%Y-%m-%d")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def records_dir(root: str) -> str:
    return os.path.join(root, "records")


def out_dir_default(root: str) -> str:
    return os.path.join(root, "tdoa")


def runs_dir(out: str) -> str:
    return os.path.join(out, "runs")


def arrivals_dir(out: str) -> str:
    return os.path.join(out, "arrivals")


def state_dir(out: str) -> str:
    return os.path.join(out, "state")


def heartbeat_path(out: str) -> str:
    return os.path.join(state_dir(out), "tdoa_heartbeat.json")


def latest_path(out: str) -> str:
    return os.path.join(out, "latest.json")


def model_card_path(out: str) -> str:
    return os.path.join(out, "model_card.json")


def _partitions(d: str) -> Iterator[Tuple[str, str]]:
    """(day partition, absolute jsonl path), oldest partition first."""
    if not os.path.isdir(d):
        return
    for day in sorted(os.listdir(d)):
        p = os.path.join(d, day)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if fn.endswith(".jsonl"):
                yield day, os.path.join(p, fn)


def count_lines(root: str, sources: Sequence[str]) -> int:
    """How many stored records the selected sources hold, counted WITHOUT decoding any of them.

    ⚠️A SECOND, INDEPENDENT PASS, AND THAT IS THE ENTIRE POINT. If `lines_read` were incremented
    by the same loop that files each record into a bucket, `lines_read == sum(buckets)` would
    restate the loop rather than test it -- every path lands in a bucket by construction, so the
    assertion could not fail. Counting here, from the bytes, gives the two sides of the invariant
    different provenance, which is what makes it a check. Copied in shape, deliberately, from
    hear_score.count_lines.
    """
    want = {"%s.jsonl" % s for s in sources}
    n = 0
    for _day, path in _partitions(records_dir(root)):
        if os.path.basename(path) not in want:
            continue
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    n += 1
    return n


def _iter_raw(root: str, sources: Sequence[str]) -> Iterator[Tuple[str, Optional[Dict[str, Any]]]]:
    """(day partition, stored row) for the selected sources; (day, None) for a torn line.

    ⚠️WALKED HERE RATHER THAN THROUGH `Pool.raw()`, for the reasons hear_score._partitions gives
    and one more. `raw()` discards WHICH day partition a line came from and the day is not
    recoverable from `ts_utc_s` for unanchored rows; and `raw()`'s `json.loads` carries no
    try/except, so one torn line aborts a whole scan instead of becoming a counted refusal.

    ⚠️`raw()`'s SHAPE, NOT `records()`. `records()` runs `SK.unpack` on every frame, and timing
    needs nothing spectral; worse, `records(usable_only=True)` would drop the 955 legacy
    `nyquist`-axis rows for a SPECTRAL reason that has no bearing on timing at all.
    """
    want = {"%s.jsonl" % s for s in sources}
    for day, path in _partitions(records_dir(root)):
        if os.path.basename(path) not in want:
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield day, json.loads(line)
                except Exception:
                    yield day, None


# ================================================================= geometry (composed, not written)

def arrival_survey(sv: SV.Survey, min_nodes: int = 3) -> SV.Survey:
    """The sub-survey of receivers whose hardware class admits their timestamps as arrivals.

    ⚠️BUILT WITH THE DIRECT CONSTRUCTOR, NOT `from_dict(to_dict())`. `Survey.to_dict()` emits
    node_id/name/e_m/n_m/u_m/sigma_m and NOT `class` (survey.py:207-219), so a round trip through
    it silently drops every node class -- and `arrival_ids()` on the result then returns ALL ids,
    because an unstated class is admitted by design (survey.py:96-100). The round trip would
    therefore produce a sub-survey that re-admits exactly the node it was built to exclude.

    ⚠️THE DIRECT CONSTRUCTOR BYPASSES from_dict's VALIDATION (survey.py:291-308), so the coincident
    and collinear checks are re-run here by hand. This is not belt-and-braces: a 4-node
    non-collinear survey can have a collinear 3-node ARRIVAL subset, and from_dict is where that
    check lives. Refusing here is the same load-time discipline, one level down.
    """
    keep = list(sv.arrival_ids())
    s = SV.Survey({i: sv.position(i) for i in keep},
                  names={i: sv.names[i] for i in keep},
                  sigma_m={i: sv.sigma_m[i] for i in keep},
                  classes={i: sv.classes[i] for i in keep},
                  origin=sv.origin)
    if len(s) < int(min_nodes):
        raise Refusal("survey has %d arrival-class receiver(s) (%s), solver needs %d. A 2-sensor "
                      "TDoA Fisher matrix is rank-1 and singular for ANY geometry, so this is a "
                      "refusal and not a degraded mode."
                      % (len(s), ", ".join(sorted(s.names[i] or str(i) for i in s.ids)),
                         int(min_nodes)))
    P = s.positions(s.ids)
    for a in range(len(s.ids)):
        for b in range(a + 1, len(s.ids)):
            d = float(np.linalg.norm(P[a] - P[b]))
            if d < SV.COINCIDENT_M:
                raise Refusal("arrival nodes %d and %d are %.3f m apart, under %.2f m: one point "
                              "entered twice" % (s.ids[a], s.ids[b], d, SV.COINCIDENT_M))
    lin = s.linearity()
    if lin < SV.MIN_LINEARITY:
        raise Refusal(
            "the ARRIVAL subset %s is collinear (linearity %.4f) even though the full survey is "
            "not: point-source DOP is singular on a line, and the source's mirror across that "
            "line fits identically at zero residual."
            % (sorted(s.names[i] or str(i) for i in s.ids), lin))
    return s


def pair_bounds(sv: SV.Survey, c: float) -> Dict[Tuple[int, int], Dict[str, float]]:
    """Per receiver pair, the largest arrival-time difference one point source can produce.

    ⚠️FULL 3D SEPARATION, never positions_2d. This is the propagation path, and the horizontal
    projection is shorter -- which is the ADMITTING direction, so projecting here would widen
    every gate silently. `associate._xyz` makes the same call for the same reason.
    """
    ids = list(sv.ids)
    P = sv.positions(ids)
    out: Dict[Tuple[int, int], Dict[str, float]] = {}
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            d = CO.spacing(P, a, b)                      # composed: consistency owns "spacing"
            out[(ids[a], ids[b])] = {"d_m": d, "bound_s": d / float(c)}
    return out


def derive_margin_s(bounds: Dict[Tuple[int, int], Dict[str, float]], margin_frac: float,
                    override_s: Optional[float] = None, force: bool = False) -> Dict[str, Any]:
    """The grouping margin, dimensioned against THIS array's pair bounds.

    `AS.MARGIN_S` is 30 ms, reasoned about against 85 ms round spacing and never against aperture.
    On a 16.87 m array that is 61% of the widest pair bound and 87% of the tightest, so the
    pairwise gate admits groups that cannot be one point source -- the transplanted-constant
    pattern: the number survived the move, the dimensioning did not.

    ⚠️THIS DOES NOT MAKE THE MARGIN CORRECT. Its floor is not the clock. PPS timing is about
    0.034 m of range against a 0.49-0.717 m survey sigma; the term that actually binds,
    inter-node onset-detection jitter, is UNMEASURED on this fleet. Admissibility is therefore
    judged separately at tol_s = 0 (see bound_check) and every excess is reported in metres.
    """
    tightest = min(v["bound_s"] for v in bounds.values())
    widest = max(v["bound_s"] for v in bounds.values())
    tight_d = min(v["d_m"] for v in bounds.values())
    derived = min(AS.MARGIN_S, float(margin_frac) * tightest)
    src = "derived"
    if override_s is not None:
        if float(override_s) > HARD_MARGIN_FRAC * tightest and not force:
            # ⚠️MEASURED AGAINST THE TIGHTEST PAIR, NOT THE APERTURE. margin_frac is a fraction
            # of the SMALLEST d/c in the array, because that is the pair the gate has the least
            # room on; quoting the required geometry against the widest separation would name a
            # number the array already meets while the gate is still useless.
            raise Refusal(
                "--margin-s %.4f s is %.0f%% of the tightest pair bound (%.4f s over %.2f m). "
                "For it to be %.0f%% of d/c the CLOSEST pair would have to be at least %.1f m "
                "apart; it is %.2f m. Pass --force-margin to run it anyway."
                % (float(override_s), 100.0 * float(override_s) / tightest, tightest,
                   tight_d, 100.0 * HARD_MARGIN_FRAC,
                   float(override_s) * SW.sound_speed(20.0) / HARD_MARGIN_FRAC, tight_d))
        derived = float(override_s)
        src = "operator" + ("_forced" if force else "")
    return {
        "margin_s": float(derived),
        "margin_source": src,
        "margin_frac": float(margin_frac),
        "library_default_margin_s": float(AS.MARGIN_S),
        "tightest_pair_bound_s": float(tightest),
        "widest_pair_bound_s": float(widest),
        "margin_over_tightest_bound": float(derived) / tightest,
        "library_default_over_tightest_bound": float(AS.MARGIN_S) / tightest,
        "library_default_over_widest_bound": float(AS.MARGIN_S) / widest,
        "tightest_pair_separation_m": float(tight_d),
        # the separation the CLOSEST pair would need for this margin to be a fifth of its own
        # d/c. Named for the closest pair and not the aperture: see the refusal above.
        "closest_pair_separation_for_margin_at_20pct_m":
            float(derived) * SW.sound_speed(20.0) / 0.20,
        "note": "the floor on this number is inter-node onset-detection jitter, which is "
                "UNMEASURED; it is not the PPS clock, which is ~20x smaller than the survey sigma",
    }


# ================================================================= pool -> arrivals

def _detail(row: Dict[str, Any], msg: str) -> str:
    return "%s %s: %s" % (row.get("source"), row.get("node"), msg)


def admit(root: str, sv: SV.Survey, arr_sv: SV.Survey, policy: Dict[str, Any],
          ledger_keys: Optional[set] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Every stored record -> exactly one terminal state, and the detections `associate()` wants.

    ⚠️FOUR TRAPS IN THE POOL SCHEMA, EACH OF WHICH SILENTLY PRODUCES A WRONG ANSWER RATHER THAN
    AN ERROR, AND ALL FOUR ARE HANDLED HERE RATHER THAN IN associate():

      1. `ts_utc_s`, NOT `t_utc_s`. Different key name, and it can be None (a node before PPS
         lock, a phone payload with no ts_utc_ms). `associate()` calls `float(d["t_utc_s"])`
         UNCONDITIONALLY on every detection that passes its survey-membership test -- including
         while building the `unusable_arrival` detail string -- so an unanchored row does not get
         refused there, it crashes there. About 9% of this pool is unanchored.

      2. `node` is a STRING name ("nyquist"), and `associate()` does `int(d["node_id"])`. The
         name -> int map is inverted out of the ARRIVAL sub-survey, so a name that is surveyed but
         not arrival-class cannot be coerced by accident.

      3. There is NO `seq` in the pool. The wire's 8-bit field does not survive sketch decoding
         (pool rows are decoded with `hear.sketch.unpack`, not `hear.wire`), and the pool dedups
         on a content hash at ingest instead. See _assign_seq for what is synthesised and why the
         two obvious alternatives are wrong.

      4. `utc_trusted` is resolved through `corpus.utc_trusted_of`, the DERIVED rung, not read
         off the stored field. The raw field needs the same ordering -- onset_dated refusal
         first, then a stated bool, then clock_tier membership -- and reading it naively
         mis-trusts a wall-clock stamp. For a node row it must resolve to None (node clock trust
         is a different measurement: PPS lock), which `utc_trusted_of`'s `source != "phone"`
         guard already guarantees.

    ⚠️THE THIRD STATE. `utc_trusted` is null on ALL phone rows in this pool, and
    `arrival_is_usable` reads `d.get(k, True) is not False`, which admits None. So the gate
    written for the phone audio path passes 100% of it. That third state is resolved HERE, under
    an explicit policy (`--clock-unstated`), and a real boolean is handed down -- which is what
    makes `associate()`'s refusal path live rather than decorative.
    """
    now = time.time() if now is None else now
    ledger_keys = set() if ledger_keys is None else ledger_keys
    sources = list(policy["sources"])
    name_to_id = {arr_sv.names[i]: i for i in arr_sv.ids if arr_sv.names[i]}
    surveyed_names = {sv.names[i]: i for i in sv.ids if sv.names[i]}
    days = set(policy.get("days") or ())
    since, until = policy.get("since"), policy.get("until")
    lookback_cut = now - float(policy["lookback_h"]) * 3600.0
    settle_cut = now - float(policy["settle_s"])
    cal = policy.get("latency_cal") or {}
    max_sync_ns = policy.get("max_sync_sigma_ns")

    dets: List[Dict[str, Any]] = []
    ledger: List[Dict[str, Any]] = []
    by_reason: Dict[str, int] = {}
    by_node_day_reason: Dict[str, int] = {}
    onset_unstated: Dict[str, int] = {}       # admitted with an unknown onset, by source
    onset_stated: Dict[str, int] = {}         # admitted and the producer said it crossed
    n_rows = 0

    def _drop(day: str, row: Optional[Dict[str, Any]], reason: str, detail: str) -> None:
        by_reason[reason] = by_reason.get(reason, 0) + 1
        node = (row or {}).get("node") or "(unparseable)"
        b = "%s|%s|%s" % (node, day, reason)
        by_node_day_reason[b] = by_node_day_reason.get(b, 0) + 1
        ledger.append({
            "schema": LEDGER_SCHEMA,
            "key": (row or {}).get("key"),
            "node": node, "node_id": None,
            "source": (row or {}).get("source"), "day": day,
            "ts_utc_s": (row or {}).get("ts_utc_s"),
            "admitted": False, "drop_reason": reason, "detail": detail,
            "terminal": reason, "event_key": None,
        })

    for day, row in _iter_raw(root, sources):
        n_rows += 1
        if row is None:
            _drop(day, None, D_UNPARSEABLE, "a stored line did not parse as JSON")
            continue
        ts = row.get("ts_utc_s")
        # (1) An unanchored row must never reach associate(): float(None) is not a refusal.
        if ts is None or not row.get("anchored"):
            _drop(day, row, D_UNANCHORED,
                  _detail(row, "no UTC anchor (node before PPS lock, or a payload with no "
                               "ts_utc_ms); associate() would call float(None)"))
            continue
        ts = float(ts)
        if days and day not in days:
            _drop(day, row, D_OUTSIDE_WINDOW, _detail(row, "partition %s not selected" % day))
            continue
        if (since is not None and ts < since) or (until is not None and ts > until):
            _drop(day, row, D_OUTSIDE_WINDOW,
                  _detail(row, "%.3f s outside --since/--until" % ts))
            continue
        if ts > settle_cut:
            _drop(day, row, D_PENDING_SETTLE,
                  _detail(row, "newer than the %.0f s settle window: its partners may still be "
                               "draining, and associating it now would fix a membership set that "
                               "a later drain changes" % float(policy["settle_s"])))
            continue
        if ts < lookback_cut:
            # ⚠️THE ONE THING A LOOKBACK COSTS, MADE LOUD. A row this old can never be
            # associated by any future run. If it was already emitted that is fine; if it never
            # was, the lookback silently ate it, and --check fails on the count.
            seen = row.get("key") in ledger_keys
            _drop(day, row,
                  D_OUTSIDE_LOOKBACK_EMITTED if seen else D_OUTSIDE_LOOKBACK_UNASSOC,
                  _detail(row, "older than --lookback-h %.1f h and %s"
                          % (float(policy["lookback_h"]),
                             "already in the arrival ledger" if seen else
                             "NEVER associated -- the lookback lost it")))
            continue
        name = row.get("node")
        if name not in name_to_id:
            if name in surveyed_names:
                nid = surveyed_names[name]
                cls = sv.classes.get(nid) or ""
                try:
                    NC.require_arrival(cls, nid)         # composed: the message is nodeclass's
                    why = ("class %r admits arrivals but %r is not in arrival_ids()" % (cls, name))
                except NC.CapabilityError as exc:
                    why = str(exc)
                except Exception as exc:                 # unknown class name, etc.
                    why = "%s: %s" % (type(exc).__name__, exc)
                _drop(day, row, D_NOT_ARRIVAL, _detail(row, why))
            else:
                _drop(day, row, D_UNSURVEYED,
                      _detail(row, "no survey entry, so no position and no int node_id; "
                                   "associate() would call int(%r)" % (name,)))
            continue
        nid = name_to_id[name]
        # (4) the DERIVED rung, and the third state resolved under an explicit policy.
        trusted = C.utc_trusted_of(row)
        if trusted is None and row.get("source") == "phone":
            if policy["clock_unstated"] == "refuse":
                _drop(day, row, D_CLOCK_UNSTATED,
                      _detail(row, "clock trust is UNSTATED (utc_trusted null, clock_tier %r). "
                                   "associate.arrival_is_usable admits None, so this is the state "
                                   "its gate cannot see; --clock-unstated admit overrides"
                              % (row.get("clock_tier"),)))
                continue
            trusted = None                               # admitted, and recorded as unstated
        if trusted is False:
            _drop(day, row, D_CLOCK_UNTRUSTED,
                  _detail(row, "clock_tier %r is not a UTC measurement (trusted tiers %s)"
                          % (row.get("clock_tier"), sorted(C.TRUSTED_CLOCK_TIERS))))
            continue
        ssig = row.get("sync_sigma_ns")
        if max_sync_ns is not None and ssig is not None and float(ssig) > float(max_sync_ns):
            _drop(day, row, D_SYNC_SIGMA,
                  _detail(row, "stated sync_sigma %.0f ns exceeds --max-sync-sigma-ns %.0f"
                          % (float(ssig), float(max_sync_ns))))
            continue
        # ⚠️THREE-STATE, AND THE THIRD STATE IS THE ONE THAT ACTUALLY OCCURS. See the
        # `onset_quality` block returned below for what this pool is made of.
        onset = row.get("onset_found")
        if onset is False:
            _drop(day, row, D_ONSET,
                  _detail(row, "the producer states the constant-fraction onset was never "
                               "crossed, so the stamp is the clamp edge, not a measurement"))
            continue
        if onset is None:
            # NOT "the producer measured it and the field went missing". For source=node the
            # quantity is NEVER MEASURED ANYWHERE IN THE CHAIN -- see _ONSET_UNSTATED_WHY.
            if policy["onset_unstated"] == "refuse":
                _drop(day, row, D_ONSET_UNSTATED, _detail(row, _ONSET_UNSTATED_WHY))
                continue
            onset_unstated[row.get("source") or "unknown"] = (
                onset_unstated.get(row.get("source") or "unknown", 0) + 1)
        lat_ms = None
        if row.get("source") == "phone":
            # ⚠️REACHABLE ONLY ONCE A PHONE IS SURVEYED. Today no phone has a survey entry, so
            # every phone row is already gone as unsurveyed_node above and this branch is dead.
            # It is written anyway because adding a phone to survey.json is a one-line change,
            # and the failure it would otherwise cause is silent: ~13 ms of uncorrected
            # audio-path latency is 4.5 m, which alone exceeds the entire 34.4 ms
            # nyquist-rankine budget. 2 of 3 handsets have no calibration entry at all.
            off_ns = (cal.get("by_node_id") or {}).get(name)
            if off_ns is None:
                _drop(day, row, D_LATENCY,
                      _detail(row, "no --latency-cal entry; the phone audio path's per-handset "
                                   "bias is constant, invisible in a residual, and is NOT "
                                   "something this tool will fit for itself"))
                continue
            lat_ms = float(off_ns) / 1e6
            ts = ts + float(off_ns) / 1e9
        d = {
            "node_id": int(nid),
            "t_utc_s": float(ts),
            # carried provenance -- associate() reads none of it and carries all of it
            "pool_key": row.get("key"), "node_name": name, "source": row.get("source"),
            "day": day, "peak": row.get("peak"), "ref_db": row.get("ref_db"),
            "retrigger": row.get("retrigger"), "layout": row.get("layout"),
            "fs_hz": row.get("fs_hz"), "clock_tier": row.get("clock_tier"),
            "sync_sigma_ns": ssig, "ts_utc_s_raw": float(row["ts_utc_s"]),
            "latency_applied_ms": lat_ms,
            # ⚠️REAL BOOLEANS, not the stored fields. None here means "not stated and admitted
            # under policy", which associate()'s `is not False` reads as usable -- the same
            # answer, but now it is a decision on the record instead of an accident.
            "utc_trusted": trusted,
            "onset_found": row.get("onset_found"),
        }
        dets.append(d)
        if onset is True:
            src = d["source"] or "unknown"
            onset_stated[src] = onset_stated.get(src, 0) + 1
        by_reason[D_ADMITTED] = by_reason.get(D_ADMITTED, 0) + 1

    _assign_seq(dets)
    return {"dets": dets, "ledger": ledger, "by_reason": by_reason,
            "by_node_day_reason": by_node_day_reason, "rows_seen": n_rows,
            "n_admitted": len(dets), "n_dropped": len(ledger),
            "onset_quality": _onset_quality(policy, onset_stated, onset_unstated, by_reason)}


def _onset_quality(policy: Dict[str, Any], stated: Dict[str, int],
                   unstated: Dict[str, int], by_reason: Dict[str, int]) -> Dict[str, Any]:
    """A DISTRIBUTION over the three onset states of the admitted stream, per source.

    Reported rather than summarised to one number because the three states are not degrees of
    the same thing: `stated_crossed` is a measurement that passed, `stated_not_crossed` is a
    measurement that failed (and those rows are already gone, counted under the drop reason),
    and `unstated` is NO MEASUREMENT -- see _ONSET_UNSTATED_WHY. Averaging the last into a
    "quality score" with the first two is exactly the move this block exists to prevent.

    `unstated_frac` is over the ADMITTED stream, which is the population every downstream count
    in this run is computed from. It is None when nothing was admitted, not 0.0: a fraction of
    an empty set has no referent.
    """
    n_stated = sum(stated.values())
    n_unstated = sum(unstated.values())
    n_admitted = n_stated + n_unstated
    return {
        "policy": policy["onset_unstated"],
        "stated_crossed": dict(sorted(stated.items())),
        "unstated_admitted": dict(sorted(unstated.items())),
        "n_stated_crossed": n_stated,
        "n_unstated_admitted": n_unstated,
        "n_stated_not_crossed_refused": by_reason.get(D_ONSET, 0),
        "n_unstated_refused": by_reason.get(D_ONSET_UNSTATED, 0),
        "unstated_frac_of_admitted": (None if not n_admitted else n_unstated / float(n_admitted)),
        # The load-bearing sentence, carried in the artefact so a reader of manifest.json alone
        # cannot mistake a passed row for a timed one.
        "why_unstated": _ONSET_UNSTATED_WHY,
    }


def _assign_seq(dets: List[Dict[str, Any]]) -> None:
    """`seq` = the per-node ordinal in the time-sorted admitted stream. Assigned in place.

    ⚠️THE POOL HAS NO seq AND THE TWO OBVIOUS SUBSTITUTES ARE BOTH WRONG.
      - A CONSTANT (seq=0 everywhere) makes `associate()`'s (node_id, seq) dedupe fire on two
        genuinely distinct rounds from one node inside one window: they become `duplicate_seq`
        and one is silently discarded. That is a different failure from the one the dedupe
        exists for, wearing its name.
      - `sample` is the node's own monotonic counter and RESTARTS on reboot; phones do not
        carry it at all.
    An ordinal is unique per node by construction, so `duplicate_seq` can never fire on this
    input -- which is correct: the pool already deduplicated by content hash at ingest, so a
    repeat here would be a pool defect, not a transport repeat. It is also deterministic for a
    fixed input, so two runs over the same pool are byte-comparable. `pool_key` is the real
    identity and travels beside it.
    """
    dets.sort(key=lambda d: (d["t_utc_s"], d["node_id"], d["pool_key"] or ""))
    n: Dict[int, int] = {}
    for d in dets:
        i = d["node_id"]
        d["seq"] = n.get(i, 0)
        n[i] = d["seq"] + 1


# ================================================================= co-activity (printed FIRST)

def coactivity(dets: Sequence[Dict[str, Any]], ids: Sequence[int], bin_s: float) -> Dict[str, Any]:
    """How much OPPORTUNITY the array had, before anything association-shaped runs.

    ⚠️THIS IS THE BINDING CONSTRAINT ON THIS CORPUS AND IT IS NOT CLOSE. Measured: all three
    surveyed nodes were simultaneously detecting in 30 one-minute bins out of 951 bins in which
    at least one was. A coincidence rate quoted over zero three-way opportunity is a number with
    no referent, so `bins_with_all == 0` short-circuits the run rather than reporting one.

    Also reports each node's median inter-detection interval, because the detectors are measurably
    not triggering on the same thing: 0.043 s (nyquist), 0.149 s (rankine), 1.066 s (mach), a ~25x
    spread. A shared, characterised threshold is a PRECONDITION for co-detection.
    """
    ids = list(ids)
    per: Dict[int, List[float]] = {i: [] for i in ids}
    for d in dets:
        if d["node_id"] in per:
            per[d["node_id"]].append(float(d["t_utc_s"]))
    bins: Dict[int, set] = {}
    for i, ts in per.items():
        for t in ts:
            bins.setdefault(int(math.floor(t / float(bin_s))), set()).add(i)
    by_count: Dict[str, int] = {}
    for members in bins.values():
        by_count[str(len(members))] = by_count.get(str(len(members)), 0) + 1
    pairs: Dict[str, int] = {}
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            k = "%d|%d" % (ids[a], ids[b])
            pairs[k] = sum(1 for m in bins.values() if ids[a] in m and ids[b] in m)
    per_node = {}
    for i, ts in per.items():
        ts = sorted(ts)
        gaps = np.diff(np.asarray(ts)) if len(ts) > 1 else np.zeros(0)
        per_node[str(i)] = {
            "n": len(ts),
            "first_utc_s": ts[0] if ts else None,
            "last_utc_s": ts[-1] if ts else None,
            "median_interval_s": float(np.median(gaps)) if gaps.size else None,
        }
    return {
        "bin_s": float(bin_s),
        "bins_total": len(bins),
        "bins_by_node_count": by_count,
        "bins_with_all": sum(1 for m in bins.values() if len(m) == len(ids)),
        "bins_pairwise": pairs,
        "per_node": per_node,
        "n_arrival_nodes": len(ids),
    }


# ================================================================= the two scans

def scan_coincidences(dets: Sequence[Dict[str, Any]], window_s: float,
                      min_nodes: int) -> List[List[Dict[str, Any]]]:
    """Greedy earliest-wins grouping with NO geometry gate. The driver's own; nothing does this.

    ⚠️IT EXISTS TO SEPARATE TWO POPULATIONS `associate()` CONFLATES BY DESIGN. `associate()`
    applies the pairwise geometry gate while it groups, so its output cannot distinguish "the
    array never co-detected" from "co-detections exist and every one of them is physically
    impossible". Those need opposite field responses -- the first is an uptime and threshold
    problem, the second is a siting or clock problem -- so both are counted.

    Semantics are otherwise associate()'s exactly: one detection per node per group, earliest
    wins with no replacement, a consumed candidate is terminal. Same scan, minus one gate.
    """
    pool = sorted(dets, key=lambda d: (float(d["t_utc_s"]), int(d["node_id"]), int(d["seq"])))
    used = [False] * len(pool)
    out: List[List[Dict[str, Any]]] = []
    for i, seed in enumerate(pool):
        if used[i]:
            continue
        used[i] = True
        group = [seed]
        members = {int(seed["node_id"])}
        limit = float(seed["t_utc_s"]) + float(window_s)
        for j in range(i + 1, len(pool)):
            if used[j]:
                continue
            cand = pool[j]
            if float(cand["t_utc_s"]) > limit:
                break
            used[j] = True                     # consumed either way -- earliest wins, no replace
            nid = int(cand["node_id"])
            if nid in members:
                continue
            members.add(nid)
            group.append(cand)
        if len(group) >= int(min_nodes):
            out.append(group)
    return out


def bound_check(group: Sequence[Dict[str, Any]], sv: SV.Survey, c: float,
                margin_s: float) -> Dict[str, Any]:
    """Is this group consistent with ONE point source, judged at zero margin and again at margin.

    ⚠️`margin_dependent` IS A FIRST-CLASS LABEL AND IS NEVER SILENTLY SOLVED. A group that fails
    its own pairwise geometry at tol_s = 0 and passes only because of the 30 ms slop is not a
    weak event; it is a group the aperture cannot vouch for. On the live corpus every one of the
    six surviving three-node coincidences is exactly that, and labelling them is the whole
    diagnosis -- so they are emitted with the verdict and kept out of events.jsonl.

    The gate itself is `consistency.physically_possible`, not a local |dt| <= d/c.
    """
    ids = [int(d["node_id"]) for d in group]
    P = sv.positions(ids)
    pairs = []
    strict_ok = True
    margin_ok = True
    worst = None
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            dt = float(group[b]["t_utc_s"]) - float(group[a]["t_utc_s"])
            d_m = CO.spacing(P, a, b)
            bound_s = d_m / float(c)
            ok_s = CO.physically_possible(dt, d_m, c, tol_s=0.0)
            ok_m = CO.physically_possible(dt, d_m, c, tol_s=float(margin_s))
            excess_s = max(0.0, abs(dt) - bound_s)
            # ⚠️tol_s = 0 IS HOSTILE TO EXACT ENDFIRE, AND THAT IS REPORTED RATHER THAN PAPERED
            # OVER. `physically_possible`'s own docstring says its tol_s exists so "a true
            # endfire arrival, which sits exactly on the bound, is not rejected for a rounding" --
            # a source on the line through two receivers gives |dt| == d/c to the last digit, and
            # at tol_s = 0 float64 decides the verdict. Measured while writing the tests: a
            # planted source collinear with two nodes came back inadmissible at excess 0.00 m.
            # The strict gate stays at zero, because that is the physical statement; what is
            # added is `frac_of_bound` and `near_endfire`, so a refusal at excess ~0 is legible
            # as geometry rather than read as a violation.
            frac = abs(dt) / bound_s if bound_s > 0 else float("inf")
            row = {"i": ids[a], "j": ids[b], "dt_ms": dt * 1e3, "bound_ms": bound_s * 1e3,
                   "d_m": d_m, "ok_strict": bool(ok_s), "ok_margin": bool(ok_m),
                   "excess_ms": excess_s * 1e3, "excess_m": excess_s * float(c),
                   "frac_of_bound": frac, "near_endfire": bool(abs(frac - 1.0) < 1e-6)}
            pairs.append(row)
            strict_ok = strict_ok and ok_s
            margin_ok = margin_ok and ok_m
            if worst is None or row["excess_m"] > worst["excess_m"]:
                worst = row
    return {
        "pairs": pairs,
        "admissible": bool(strict_ok),
        "margin_dependent": bool(margin_ok and not strict_ok),
        "violating_pairs": ["%d|%d" % (r["i"], r["j"]) for r in pairs if not r["ok_strict"]],
        "worst_pair": None if worst is None else "%d|%d" % (worst["i"], worst["j"]),
        "worst_excess_m": 0.0 if worst is None else worst["excess_m"],
        "near_endfire_pairs": ["%d|%d" % (r["i"], r["j"]) for r in pairs if r["near_endfire"]],
        "tol_s_strict": 0.0, "tol_s_margin": float(margin_s),
    }


def pair_coincidences(dets: Sequence[Dict[str, Any]],
                      bounds: Dict[Tuple[int, int], Dict[str, float]]) -> Dict[str, int]:
    """Per receiver pair, how many detections on the earlier node have a partner on the other
    within that pair's OWN physical bound. One-to-one, greedy, earliest first.

    Kept separate from the triple scan because a pair coincidence is real evidence the array
    hears common events and is STILL unsolvable: a 2-sensor TDoA Fisher matrix is rank-1 and
    singular for any geometry. Counting them is how "the nodes never hear the same thing" gets
    ruled out without implying the pairs can be solved.
    """
    by_node: Dict[int, List[float]] = {}
    for d in dets:
        by_node.setdefault(int(d["node_id"]), []).append(float(d["t_utc_s"]))
    for v in by_node.values():
        v.sort()
    out: Dict[str, int] = {}
    for (i, j), b in sorted(bounds.items()):
        a_ts, b_ts = by_node.get(i, []), by_node.get(j, [])
        bound = b["bound_s"]
        n = 0
        pos = 0
        taken = [False] * len(b_ts)
        for t in a_ts:
            while pos < len(b_ts) and b_ts[pos] < t - bound:
                pos += 1
            k = pos
            while k < len(b_ts) and b_ts[k] <= t + bound:
                if not taken[k]:
                    taken[k] = True
                    n += 1
                    break
                k += 1
        out["%d|%d" % (i, j)] = n
    return out


def shuffle_null(dets: Sequence[Dict[str, Any]], ids: Sequence[int],
                 bounds: Dict[Tuple[int, int], Dict[str, float]], window_s: float,
                 min_nodes: int, bin_s: float, trials: int,
                 seed: int) -> Dict[str, Any]:
    """What the same scan finds when only the sub-second alignment is destroyed.

    Each node's stamps are circularly shifted WITHIN their own `bin_s` bin, so every node's rate
    and burst structure survive exactly and only the cross-node phase is randomised. The shifted
    stream goes through the SAME `scan_coincidences` and the same `pair_coincidences`, which is
    what makes the two numbers comparable at all.

    ⚠️NULL-FIRST IS AN ORDERING RULE, NOT A DECORATION. An observed coincidence count is never
    printed without its null beside it; over a 3-day corpus at these rates the accidental triple
    rate at an 80 ms window is well under one, so an unpaired count of 6 reads as a discovery
    when it is not.

    `p_emp = (1 + #(null >= obs)) / (1 + trials)` -- the add-one form, so a null that never
    reaches the observation reports 1/(1+trials) rather than an unearned zero.
    """
    ids = list(ids)
    if trials <= 0 or not dets:
        return {"trials": int(trials), "skipped": True,
                "reason": "no trials requested" if trials <= 0 else "no admitted arrivals"}
    rng = np.random.default_rng(int(seed))
    base = [{"node_id": int(d["node_id"]), "t_utc_s": float(d["t_utc_s"]), "seq": i}
            for i, d in enumerate(dets)]
    t0 = min(d["t_utc_s"] for d in base)
    obs_triples = len(scan_coincidences(base, window_s, min_nodes))
    obs_pairs = pair_coincidences(base, bounds)

    trip: List[int] = []
    pr: Dict[str, List[int]] = {k: [] for k in obs_pairs}
    started = time.time()
    for _ in range(int(trials)):
        shifted = []
        for k, d in enumerate(base):
            rel = d["t_utc_s"] - t0
            b = math.floor(rel / float(bin_s))
            off = rel - b * float(bin_s)
            off = (off + float(rng.random()) * float(bin_s)) % float(bin_s)
            shifted.append({"node_id": d["node_id"], "seq": k,
                            "t_utc_s": t0 + b * float(bin_s) + off})
        trip.append(len(scan_coincidences(shifted, window_s, min_nodes)))
        p = pair_coincidences(shifted, bounds)
        for k in pr:
            pr[k].append(p.get(k, 0))

    def _stat(vals: List[int], obs: int) -> Dict[str, Any]:
        a = np.asarray(vals, float)
        ge = int((a >= obs).sum())
        return {"observed": int(obs), "null_mean": float(a.mean()), "null_sd": float(a.std()),
                "null_p50": float(np.percentile(a, 50)), "null_p95": float(np.percentile(a, 95)),
                "null_max": float(a.max()),
                "z": (float((obs - a.mean()) / a.std()) if a.std() > 0 else None),
                "p_emp": (1.0 + ge) / (1.0 + len(vals))}

    return {
        "trials": int(trials), "seed": int(seed), "bin_s": float(bin_s),
        "window_s": float(window_s), "min_nodes": int(min_nodes),
        "triples": _stat(trip, obs_triples),
        "pairs": {k: _stat(v, obs_pairs[k]) for k, v in sorted(pr.items())},
        "wall_s": time.time() - started,
        "skipped": False,
    }


# ================================================================= solve + attribution

def event_key(member_pool_keys: Sequence[str]) -> str:
    """A content address of the MEMBER SET, so emission dedupes without a watermark.

    ⚠️A BACKFILL THAT ADDS A MEMBER PRODUCES A NEW KEY, AND THE SUPERSEDED ROW STAYS. That is
    published (`event_membership_changed`) rather than hidden: the alternative is rewriting
    history in an append-only store, and the whole reason there is no offset watermark here is
    that a later drain has been observed appending rows INTO an already-scanned partition.
    """
    h = hashlib.sha256()
    h.update("\x1f".join(sorted(str(k) for k in member_pool_keys)).encode())
    return h.hexdigest()[:32]


def geometry_report(sv: SV.Survey, node_ids: Sequence[int], source: Sequence[float],
                    temp_c: float, fixed_up_m: Optional[float]) -> Dict[str, Any]:
    """What the geometry alone says, computed WITHOUT reference to whether anything solved.

    Run twice per attempt -- once at the array centroid, which is available before solving, and
    once at the fitted position. A failed solve therefore still gets a geometry verdict, which is
    the point: "it did not solve" and "it could not have solved from here" are different answers.

    ⚠️`dop3` IS ABSENT, NOT None, BELOW FOUR NODES. `PL.dop3` refuses under 4 (3D has three
    unknowns) and a null in its place reads as "computed, no answer". An absent key reads as
    "not asked", which is the truth.
    """
    P = sv.positions(list(node_ids))
    n = len(P)
    out: Dict[str, Any] = {
        "n_nodes": n,
        "node_ids": [int(i) for i in node_ids],
        "source_probe": [float(v) for v in np.asarray(source, float).ravel()[:3]],
        "linearity": PL.linearity(P) if n >= 3 else None,
        "coplanarity": PL.coplanarity(P) if n >= 3 else None,
        "dop": PL.dop(P, source) if n >= 3 else None,
        "vertical_observability": (PL.vertical_observability(P, source, temp_c=temp_c)
                                   if n >= 3 else None),
        "node_counts": PL.node_counts("point", 2 if fixed_up_m is not None else 3),
        "vertical_assumption": sv.validate_2d_assumption(tol_m=SV.FLAT_TOL_M),
        "sigma_m": {str(int(i)): sv.sigma_m[int(i)] for i in node_ids},
    }
    if n >= 4:
        out["dop3"] = PL.dop3(P, source)
    return out


def solve_event(sv: SV.Survey, node_ids: Sequence[int], arrivals: Sequence[float],
                source_class: str, temp_c: float, v_mps: float,
                fixed_up_m: Optional[float]) -> Tuple[str, Optional[Dict], Optional[str]]:
    """(model, solution, solver_error). ~15 lines mirrored from `pipeline.Backend.flush()`.

    ⚠️MIRRORED, NOT COPIED-AND-EMBELLISHED, and pinned equal by
    TestTheModelChoiceMatchesPipeline. The rows are taken IN THE ORDER GIVEN so they pair with
    `arrivals` by index; `positions()` promises never to re-sort and a sort here would mislabel
    every arrival. The cone lane gets the 2D projection explicitly and by name, because
    shockwave.py is still planar; the point lane gets full 3D positions, because point.py
    consumes node height as a real distance.

    ⚠️`fixed_up_m is not None`, NEVER TRUTHINESS. point.solve tests `fixed_up_m is not None`
    (point.py:236), so `fixed_up_m = 0.0` -- the most likely declared height there is -- means
    DECLARED, two unknowns, three nodes. A driver that tested truthiness would compute a minimum
    of 4 for a declared height and 3 for a 3D fit: exactly backwards.
    """
    model = "cone" if source_class in PT.CONE_CLASSES else "point"
    P = sv.positions(list(node_ids))
    try:
        if model == "cone":
            return model, SW.solve(P[:, :2], list(arrivals), v_mps=v_mps, temp_c=temp_c), None
        return model, PT.solve(P, list(arrivals), source_class, temp_c=temp_c,
                               fixed_up_m=fixed_up_m), None
    except ValueError as exc:
        return model, None, str(exc)


def solver_verdict(model: str, sol: Optional[Dict], err: Optional[str]) -> str:
    """One verdict per attempt, and a RETURNED refusal is never flattened into a raise.

    point.solve raises on a caller error and RETURNS a verdict on a degenerate geometry --
    `position_observable: False` for collinear nodes, `at_search_bound: True` for a fit pinned on
    the edge of the searched box. Those are different failures with different remedies (break the
    line; widen the box or accept the model is wrong), so they get their own verdicts.
    `up_observable: False` is NOT a failure here: with the height declared it is False by
    construction, and calling that a refusal would refuse every 3-node fit this tool exists for.
    """
    if err is not None:
        return V_SOLVER_REFUSED
    if not sol:
        return V_SOLVER_REFUSED
    if sol.get("position_observable") is False:
        return V_POSITION_UNOBSERVABLE
    if sol.get("at_search_bound"):
        return V_AT_SEARCH_BOUND
    return V_SOLVED


def leave_one_out(sv: SV.Survey, node_ids: Sequence[int], arrivals: Sequence[float],
                  source_class: str, temp_c: float, v_mps: float,
                  fixed_up_m: Optional[float], full: Optional[Dict]) -> Optional[Dict[str, Any]]:
    """Receiver-level attribution: drop each node in turn and report what moved.

    ⚠️SKIPPED, LOUDLY, BELOW `meaningful_n`. At the exactly-determined node count the residual is
    ~0 BY CONSTRUCTION, so a leave-one-out drops to n-1 = the rank-1 case and every "improvement"
    it reports is arithmetic rather than evidence. `PL.node_counts` owns that number.
    """
    n = len(node_ids)
    counts = PL.node_counts("point" if source_class in PT.POINT_CLASSES else "trajectory",
                            2 if fixed_up_m is not None else 3)
    if n < counts["meaningful_n"]:
        return {"skipped": True, "n_nodes": n, "meaningful_n": counts["meaningful_n"],
                "reason": "residual_is_meaningful is false at n=%d (meaningful at %d); a "
                          "leave-one-out at n=%d is rank-1 and singular for any geometry"
                          % (n, counts["meaningful_n"], n - 1)}
    rows = []
    for k in range(n):
        keep = [i for j, i in enumerate(node_ids) if j != k]
        ta = [t for j, t in enumerate(arrivals) if j != k]
        _m, sol, err = solve_event(sv, keep, ta, source_class, temp_c, v_mps, fixed_up_m)
        dpos = None
        if sol and full and sol.get("east_m") is not None and full.get("east_m") is not None:
            dpos = float(math.hypot(sol["east_m"] - full["east_m"],
                                    sol["north_m"] - full["north_m"]))
        rows.append({
            "dropped_node_id": int(node_ids[k]),
            "rms_residual_ms": None if not sol else sol.get("rms_residual_ms"),
            "delta_ms": (None if not (sol and full) else
                         float(full.get("rms_residual_ms") or 0.0)
                         - float(sol.get("rms_residual_ms") or 0.0)),
            "delta_position_m": dpos,
            "solver_error": err,
        })
    scored = [r for r in rows if r["delta_ms"] is not None]
    worst = max(scored, key=lambda r: r["delta_ms"]) if scored else None
    return {"skipped": False, "n_nodes": n, "per_node": rows,
            "worst_node_id": None if worst is None else worst["dropped_node_id"],
            "worst_delta_ms": None if worst is None else worst["delta_ms"],
            "note": "delta_ms is how much the fit's rms residual IMPROVES when that node is "
                    "dropped; the largest is the node the rest of the array disagrees with"}


# ================================================================= the plan (what would change it)

def _box(sv: SV.Survey, site_box: Optional[str], margin_m: float = 100.0):
    if site_box:
        v = [float(x) for x in site_box.split(",")]
        if len(v) != 4:
            raise Refusal("--site-box wants x_min,y_min,x_max,y_max in local metres, got %r"
                          % (site_box,))
        return (v[0], v[1], v[2], v[3])
    P = sv.positions(sv.ids)
    return (float(P[:, 0].min() - margin_m), float(P[:, 1].min() - margin_m),
            float(P[:, 0].max() + margin_m), float(P[:, 1].max() + margin_m))


def build_plan(sv: SV.Survey, temp_c: float, v_mps: float, candidates: Sequence[str],
               site_box: Optional[str], target_events: int,
               co: Optional[Dict[str, Any]] = None,
               n_admissible: int = 0) -> Dict[str, Any]:
    """What would have to change, as arithmetic rather than advice. Survey-only; reads no pool.

    ⚠️`PL.render`'s ASCII MAP GOES IN THE TEXT REPORT ONLY. Serialising an array of characters
    into JSON gives a consumer something that looks like data and is a picture.
    """
    P = sv.positions(sv.ids)
    b = _box(sv, site_box)
    g = PL.dop_grid(P, b, step=max(2.0, (b[2] - b[0]) / 60.0))
    cands = []
    if candidates:
        # ⚠️EVERY CANDIDATE MUST HAVE THE SAME ARITY. np.asarray on a ragged list builds an object
        # array silently and every downstream slice then lies -- placement._parse_points says so
        # in as many words, so the check is repeated rather than the parse being hand-rolled.
        pts = [tuple(_parse_point(cnd)) for cnd in candidates]
        if len({len(p) for p in pts}) != 1:
            raise Refusal("mixed 2D and 3D --candidate coordinates: %r" % (candidates,))
        cands = PL.best_addition(P, pts, b, step=max(5.0, (b[2] - b[0]) / 30.0))
    out: Dict[str, Any] = {
        "box": list(b),
        "dop_grid": {"median": g["median"], "usable_frac": g["usable_frac"],
                     "dop_usable_threshold": PL.DOP_USABLE},
        "worst_bearing": PL.worst_bearing(P, v_mps=v_mps, temp_c=temp_c),
        "observable_span_at_worst": None,
        "plan_3d": PL.plan_3d(P, temp_c=temp_c),
        "best_addition": cands,
        "target_events": int(target_events),
    }
    out["observable_span_at_worst"] = PL.observable_span(P, out["worst_bearing"]["bearing_deg"])
    # ⚠️THE UPTIME ARITHMETIC IS THE PART THAT ACTUALLY CHANGES THE ANSWER. With zero admissible
    # events over B co-active bins the rule of three gives a 95% Poisson upper bound of 3/B per
    # bin, and even at that CEILING the required uptime is the number an operator needs.
    if co is not None:
        bins = int(co.get("bins_with_all") or 0)
        if bins <= 0:
            out["uptime"] = {"co_active_bins": 0, "bin_s": co.get("bin_s"),
                             "rate_per_bin": None, "rate_upper_95_per_bin": None,
                             "bins_needed_for_target": None,
                             "note": "no bin had every arrival node detecting, so there is no "
                                     "opportunity to quote a rate over"}
        else:
            rate = float(n_admissible) / bins
            upper = rate if n_admissible else 3.0 / bins
            out["uptime"] = {
                "co_active_bins": bins, "bin_s": co.get("bin_s"),
                "admissible_events": int(n_admissible),
                "rate_per_bin": rate,
                "rate_upper_95_per_bin": upper,
                "bins_needed_for_target": (int(math.ceil(target_events / upper))
                                           if upper > 0 else None),
                "note": ("rate_upper_95 is the rule-of-three Poisson ceiling on ZERO observed "
                         "events; the required uptime below is what that ceiling implies, and "
                         "the true requirement is larger" if not n_admissible else
                         "rate measured on observed admissible events"),
            }
    return out


def determinacy_block(n_arrival: int, n_events: int, fixed_up_m: Optional[float],
                      known_source: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Can `c` be recovered here at all? Stamped EVERY run, whatever the outcome.

    Three ground receivers with an unknown source is 2 equations against 3 unknowns at ANY number
    of events -- `soundspeed.determines_speed`'s own docstring says it is "not a conditioning
    problem that better data fixes". This tool assumes `c` from `--temp-c` and says so; the block
    is what stops a reader assuming it was measured.
    """
    dim = 2 if fixed_up_m is not None else 3
    d = SS.determines_speed(n_receivers=max(2, int(n_arrival)),
                            n_events=max(1, int(n_events)), dim=dim,
                            n_known_sources=(1 if known_source is not None else 0))
    return {"equations": d.equations, "unknowns": d.unknowns, "determined": bool(d.determined),
            "deficit": d.deficit, "reason": d.reason, "dim": dim,
            "n_receivers": max(2, int(n_arrival)), "n_events": max(1, int(n_events))}


# ================================================================= the model card

def model_card(sv: SV.Survey, arr_sv: SV.Survey, policy: Dict[str, Any],
               bounds: Dict[Tuple[int, int], Dict[str, float]],
               margin: Dict[str, Any], c: float) -> Dict[str, Any]:
    """What a solution from this tool is NOT. Prose written once; every NUMBER computed here.

    A card whose figures are retyped constants stops describing the survey the moment the survey
    changes, and this project has already shipped a docstring naming a filename the tool does not
    write.
    """
    ids = list(arr_sv.ids)
    P = arr_sv.positions(ids)
    aperture = float(arr_sv.diameter_m())
    pps = NC.get("xiao-s3-pps")
    timing_m = pps.range_sigma_m(c)
    sigmas = {arr_sv.names[i] or str(i): arr_sv.sigma_m[i] for i in ids}
    worst_sigma = max(sigmas.values()) if sigmas else 0.0
    refused = [{"node_id": i, "name": sv.names[i], "class": sv.classes.get(i) or "(unstated)",
                "sigma_m": sv.sigma_m[i], "why": _why_refused(sv, i)}
               for i in sv.ids if i not in set(ids)]
    counts_2d = PL.node_counts("point", 2)
    counts_3d = PL.node_counts("point", 3)
    return {
        "schema": "hear.tdoa_model_card.v1",
        "array": {
            "arrival_node_ids": ids,
            "arrival_names": [arr_sv.names[i] for i in ids],
            "aperture_m": aperture,
            "pair_bounds_ms": {"%d|%d" % k: v["bound_s"] * 1e3 for k, v in sorted(bounds.items())},
            "pair_separations_m": {"%d|%d" % k: v["d_m"] for k, v in sorted(bounds.items())},
            "linearity": arr_sv.linearity(),
            "vertical_spread_m": arr_sv.vertical_spread_m(),
            "flat_tol_m": SV.FLAT_TOL_M,
            "vertical_assumption": arr_sv.validate_2d_assumption(tol_m=SV.FLAT_TOL_M),
            "surveyed_but_refused_as_arrivals": refused,
        },
        "what_a_solution_is_not": [
            "2 receivers is rank-1 and singular for ANY geometry -- not a hard case, an "
            "impossible one.",
            "%d receivers with the height DECLARED is exactly determined (%d unknowns, %d "
            "equations), so its residual is ~0 BY CONSTRUCTION and is not evidence of agreement "
            "-- including for a badly wrong --fixed-up-m. %d non-coplanar receivers is where "
            "both the height and the residual become real."
            % (counts_2d["exact_n"], counts_2d["unknowns"], counts_2d["exact_n"] - 1,
               counts_3d["meaningful_n"]),
            "`up` is DECLARED, not solved, unless --fixed-up-m none is passed. This array's "
            "vertical spread is %.1f m against a %.1f m flatness tolerance, so the cone lane's "
            "2D projection mis-models node ranges by up to that and no residual can see it."
            % (arr_sv.vertical_spread_m(), SV.FLAT_TOL_M),
            "`c` is ASSUMED from --temp-c %.1f (%.2f m/s). determines_speed(%d receivers, dim 2, "
            "unknown source) says it can never be recovered here at any number of events. A 10 "
            "degC error is %.3f%% of c = %.3f m over this %.2f m aperture."
            % (policy["temp_c"], c, len(ids), 100.0 * SS.fractional_speed_error(10.0),
               SS.fractional_speed_error(10.0) * aperture, aperture),
            "Timing is NOT the binding error term. %r is %.1f us = %.3f m of range against a "
            "worst node survey sigma of %.3f m -- position error is ~%.0fx the timing error, so "
            "the next accuracy gain is a RE-SURVEY, not a better clock."
            % (pps.name, pps.t_sigma_s * 1e6, timing_m, worst_sigma,
               (worst_sigma / timing_m) if timing_m else float("nan")),
            "margin_s %.4f s is %.0f%% of the tightest pair bound. For it to be <=20%% of that "
            "pair's own d/c the two closest receivers would have to be at least %.1f m apart; "
            "they are %.2f m apart (the full aperture is %.2f m). And the floor on the margin is "
            "INTER-NODE ONSET JITTER, which has never been measured on this fleet."
            % (margin["margin_s"], 100.0 * margin["margin_over_tightest_bound"],
               margin["closest_pair_separation_for_margin_at_20pct_m"],
               margin["tightest_pair_separation_m"], aperture),
            "Phones are unsurveyed: no position, so no int node_id, so they cannot enter a TDoA "
            "solve at all. Even surveyed they carry ~13 ms (4.5 m) of UNCORRECTED audio-path "
            "latency with 1 of 3 handsets calibrated, and `clock_tier: gnss` is a statement "
            "about the GPS anchor, NOT about that latency.",
            "A run with zero events is NOT evidence of no gunshots. The referent is the "
            "co-activity number in coactivity.json, not the event count.",
            "--source-class is an OPERATOR ASSERTION and is never inferred. Explicitly refused "
            "design: classifying cone-vs-point from hear-score's P. That model is not a general "
            "classifier, is not comparable between nodes (28.4 dB of measured inter-node level "
            "offset = 16.9 logits against a 7.37 dB p10-p90 range), and has no calibrated field "
            "prior.",
        ],
        "policy": {k: policy[k] for k in sorted(policy) if k != "latency_cal"},
        "sound_speed_mps": c,
        "timing_range_sigma_m": timing_m,
        "survey_sigma_m": sigmas,
    }


def _why_refused(sv: SV.Survey, node_id: int) -> str:
    cls = sv.classes.get(node_id) or ""
    if not cls:
        return "in arrival_ids(): an unstated class is admitted by design"
    try:
        NC.require_arrival(cls, node_id)
        return "class admits arrivals"
    except Exception as exc:
        return str(exc)


# ================================================================= the run

def _refuse_out_dir_inside_checkout(out: str) -> None:
    """⚠️A TEST THAT REGENERATED A FIXTURE IN PLACE HAS DESTROYED REAL DATA IN THIS ORG.

    So this refuses before writing anything, and it refuses on the SHAPE of a checkout rather
    than on one hardcoded path: any ancestor holding both a `hear/` package and a `.git` is a
    working tree, whoever cloned it and wherever they put it.
    """
    p = os.path.abspath(os.path.expanduser(out))
    cur = p
    while True:
        if (os.path.isdir(os.path.join(cur, "hear"))
                and os.path.exists(os.path.join(cur, ".git"))):
            raise Refusal("--out %s is inside the checkout at %s. This tool writes into the "
                          "pool, never into a working tree." % (p, cur))
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def ledger_hash(key: Optional[str], terminal: str, reason: Optional[str],
                ev_key: Optional[str]) -> str:
    h = hashlib.sha256()
    h.update("\x1f".join([str(key), str(terminal), str(reason), str(ev_key)]).encode())
    return h.hexdigest()[:32]


def read_ledger(out: str) -> Tuple[set, set]:
    """(pool keys ever written, ledger hashes ever written). THE resume token, and it is a key
    set rather than a watermark for the reason hear_score.py:70 measured: a later drain was seen
    appending 513 rows INTO an already-scanned partition, which every offset mark loses."""
    keys, hashes = set(), set()
    for _day, path in _partitions(arrivals_dir(out)):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue                       # a torn ledger row costs one re-emission
                if r.get("key"):
                    keys.add(r["key"])
                if r.get("ledger_hash"):
                    hashes.add(r["ledger_hash"])
    return keys, hashes


def read_emitted_events(out: str) -> Tuple[set, Dict[str, str]]:
    """What has actually been EMITTED before: (event_keys, pool_key -> the key that carried it).

    Read from `runs/*/events.jsonl` -- the emission log -- and from nothing else.

    ⚠️THIS USED TO READ THE ARRIVAL LEDGER, WHICH IS A DIFFERENT SET AND A STRICTLY LARGER ONE.
    The ledger gets an `event_key` for every arrival in every CANDIDATE group, written from
    `ev_key_of_det`, which is populated before the verdict is known -- so a candidate that was
    REFUSED (inadmissible, margin-dependent, solver-refused, lost to the associator) still put
    its key in the ledger. The next run would solve that same group, compute the same key, find
    it "already emitted", and filter it out: events.jsonl came out 0 bytes while manifest.json
    said an event had been emitted. One refusal burned that member set forever, and the only
    way back was to delete the ledger partition -- i.e. the append-only store's own history.
    Reproduced by execution; TestEmissionDedupeReadsEmissions is the regression.

    A missing or empty `runs/` is an empty set: nothing has been emitted, so nothing is deduped.
    A torn line costs one re-emission, which is the same trade `read_ledger` makes and the safe
    direction -- a duplicate row is visible, a silently dropped event is not.
    """
    ks: set = set()
    member_of: Dict[str, str] = {}
    for _run_id, path in _partitions(runs_dir(out)):
        if os.path.basename(path) != "events.jsonl":
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = r.get("event_key")
                if not k:
                    continue
                ks.add(k)
                for pk in (r.get("pool_keys") or ()):
                    member_of[str(pk)] = k
    return ks, member_of


def run(pool_root: str, survey_path: str, policy: Dict[str, Any], out: Optional[str] = None,
        write: bool = True, now: Optional[float] = None, plan_only: bool = False,
        census: bool = False) -> Dict[str, Any]:
    """One pass. Returns the run report; raises Refusal (exit 1) or NotInterpretable (exit 2).

    ⚠️EVERY STARTUP REFUSAL FIRES BEFORE A BYTE OF POOL I/O, and TestStartupRefusalsArePreIO
    patches the reader to raise if that is not true. A tool that reads 11k records and then says
    "the survey has two arrival nodes" has burned the read to tell you something it knew from a
    53-line JSON file.
    """
    now = time.time() if now is None else now
    started = time.time()
    out = out or out_dir_default(pool_root)

    # ---------------------------------------------------------- startup refusals, pre-I/O
    _refuse_out_dir_inside_checkout(out)
    try:
        sv = SV.load_survey(survey_path, min_nodes=3)
    except SV.SurveyError as exc:
        raise Refusal("survey %s did not load: %s" % (survey_path, exc))
    except OSError as exc:
        raise Refusal("survey %s is unreadable: %s" % (survey_path, exc))
    arr_sv = arrival_survey(sv, min_nodes=3)
    fixed_up = policy["fixed_up_m"]
    if fixed_up is None and len(arr_sv) < 4:
        raise Refusal(
            "--fixed-up-m none asks for a 3D fit from %d arrival-class receivers: t0 cancels, so "
            "N-1 = %d equations against n_unknowns = 3. Pass --fixed-up-m to DECLARE the height "
            "(two unknowns, three receivers), or add a 4th arrival-class receiver. No amount of "
            "further data changes this." % (len(arr_sv), len(arr_sv) - 1))
    if not plan_only:
        PT.is_point_source(policy["source_class"])           # its own ValueError, verbatim

    c = SW.sound_speed(policy["temp_c"])
    bounds = pair_bounds(arr_sv, c)
    margin = derive_margin_s(bounds, policy["margin_frac"], policy.get("margin_s_override"),
                             bool(policy.get("force_margin")))
    margin_s = margin["margin_s"]
    if policy.get("max_sync_sigma_ns") is None:
        policy["max_sync_sigma_ns"] = (DEFAULT_SYNC_SIGMA_FRAC
                                       * margin["tightest_pair_bound_s"] * 1e9)

    window_s = AS.max_window_s(arr_sv, policy["temp_c"], margin_s)
    window_full = AS.max_window_s(sv, policy["temp_c"], margin_s)
    # the point solver's own minimum, computed the way point.py:236 computes it -- never from
    # the docstring's truthiness form, which is inverted (see solve_event).
    n_unk = 2 if fixed_up is not None else 3
    solver_min_nodes = n_unk + 1 if policy.get("source_class") in PT.POINT_CLASSES else 3
    min_nodes = max(int(policy["min_nodes"]), solver_min_nodes)

    report: Dict[str, Any] = {
        "schema": TDOA_SCHEMA,
        "run_id": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "at": now,
        "pool": pool_root, "out": out, "survey": survey_path,
        "policy": {k: v for k, v in sorted(policy.items()) if k != "latency_cal"},
        "survey_block": {
            "survey_ok": True,
            "all_ids": list(sv.ids), "all_names": [sv.names[i] for i in sv.ids],
            "arrival_ids": list(arr_sv.ids),
            "arrival_names": [arr_sv.names[i] for i in arr_sv.ids],
            "refused_as_arrivals": [{"node_id": i, "name": sv.names[i],
                                     "class": sv.classes.get(i) or "(unstated)",
                                     "sigma_m": sv.sigma_m[i], "why": _why_refused(sv, i)}
                                    for i in sv.ids if i not in set(arr_sv.ids)],
            "linearity": arr_sv.linearity(),
            "vertical_assumption": arr_sv.validate_2d_assumption(tol_m=SV.FLAT_TOL_M),
        },
        "association": {
            "window_s": window_s,
            "window_s_full_survey": window_full,
            "window_inflation_frac": (window_full / window_s - 1.0) if window_s else None,
            "diameter_m": arr_sv.diameter_m(),
            "diameter_m_full_survey": sv.diameter_m(),
            "min_nodes": min_nodes,
            "solver_min_nodes": solver_min_nodes,
            "sound_speed_mps": c,
            "pair_bounds_ms": {"%d|%d" % k: v["bound_s"] * 1e3 for k, v in sorted(bounds.items())},
        },
        "margin": margin,
        "c": {"sound_speed_mps": c, "source": "assumed_temp_c", "temp_c": policy["temp_c"],
              "frac_error_per_10c": SS.fractional_speed_error(10.0),
              "metres_per_10c_on_aperture":
                  SS.fractional_speed_error(10.0) * arr_sv.diameter_m()},
        "not_composed": NOT_COMPOSED,
        "could_not_do": COULD_NOT_DO,
    }

    if plan_only:
        report["mode"] = "plan_only"
        report["plan"] = build_plan(arr_sv, policy["temp_c"], policy["v_mps"],
                                    policy.get("candidates") or [], policy.get("site_box"),
                                    policy.get("target_events") or DEFAULT_TARGET_EVENTS)
        report["binding_constraint"] = None
        report["wall_s"] = time.time() - started
        return report

    # ---------------------------------------------------------- read
    ledger_keys, ledger_hashes = read_ledger(out)
    prior_event_keys, prior_member_event = read_emitted_events(out)
    lines_read = count_lines(pool_root, policy["sources"])        # INDEPENDENT byte pass
    a = admit(pool_root, sv, arr_sv, policy, ledger_keys=ledger_keys, now=now)
    dets, ledger = a["dets"], a["ledger"]

    co = coactivity(dets, arr_sv.ids, policy["bin_s"])
    report["coactivity"] = co
    report["funnel"] = {
        "lines_read": lines_read, "rows_seen": a["rows_seen"],
        "admitted": a["n_admitted"], "dropped": a["n_dropped"],
        "by_reason": a["by_reason"], "by_node_day_reason": a["by_node_day_reason"],
    }
    report["onset_quality"] = a["onset_quality"]

    if census:
        report["mode"] = "census"
        report["binding_constraint"] = (B_NO_RECORDS if not dets else
                                        B_CO_ACTIVITY if not co["bins_with_all"] else None)
        report["conservation"] = {
            "one_terminal_state_per_row": lines_read == a["n_admitted"] + a["n_dropped"],
            "lines_read": lines_read, "accounted": a["n_admitted"] + a["n_dropped"],
        }
        report["wall_s"] = time.time() - started
        return report

    # ---------------------------------------------------------- associate (composed)
    grouped = AS.associate(dets, arr_sv, temp_c=policy["temp_c"], margin_s=margin_s,
                           min_nodes=min_nodes, window_s=None)
    if abs(float(grouped["window_s"]) - window_s) > 1e-12:
        raise NotInterpretable(
            "associate() used window %.6f s where max_window_s(arrival_survey) is %.6f s: the "
            "driver's scan and the associator's would be measuring different things"
            % (grouped["window_s"], window_s))
    assoc_accounted = (sum(e["n_nodes"] for e in grouped["events"])
                       + len(grouped["rejected"]) + len(grouped["duplicates"]))
    if assoc_accounted != len(dets):
        raise NotInterpretable(
            "associate() did not conserve its own input: %d in, %d accounted for. Nothing is "
            "written -- a funnel that loses rows is not evidence of anything."
            % (len(dets), assoc_accounted))

    # ---------------------------------------------------------- the two scans
    candidates = scan_coincidences(dets, window_s, min_nodes)
    by_key = {d["pool_key"]: d for d in dets}
    ev_by_seed = {}
    for ev in grouped["events"]:
        seed = min(ev["detections"], key=lambda d: (float(d["t_utc_s"]), int(d["node_id"])))
        ev_by_seed[seed["pool_key"]] = ev
    rej_by_key = {}
    for r in grouped["rejected"]:
        rej_by_key.setdefault((r["node_id"], r["seq"]), r)

    cand_rows: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    events_out: List[Dict[str, Any]] = []
    key_of_det: Dict[str, str] = {}          # pool_key -> terminal verdict
    ev_key_of_det: Dict[str, str] = {}
    solved_for_cal: List[Dict[str, Any]] = []

    for gi, group in enumerate(candidates):
        seed = group[0]
        ids = [int(d["node_id"]) for d in group]
        arrivals = [float(d["t_utc_s"]) for d in group]
        pkeys = [d["pool_key"] for d in group]
        ek = event_key(pkeys)
        bc = bound_check(group, arr_sv, c, margin_s)
        ev = ev_by_seed.get(seed["pool_key"])
        reached = ev is not None
        lost_reason = None
        if not reached:
            for d in group:
                r = rej_by_key.get((int(d["node_id"]), int(d["seq"])))
                if r is not None:
                    lost_reason = r["reason"]
                    break
        cand_rows.append({
            "schema": TDOA_SCHEMA, "candidate_id": gi, "event_key": ek,
            "t0_utc_s": arrivals[0], "span_s": arrivals[-1] - arrivals[0],
            "node_ids": ids, "node_names": [d["node_name"] for d in group],
            "arrivals": arrivals, "pool_keys": pkeys,
            "n_nodes": len(group), "n_equations": len(group) - 1,
            "bound_check": bc, "reached_associate": reached,
            "associate_reason": lost_reason,
            "retrigger": [bool(d.get("retrigger")) for d in group],
            "layout": [d.get("layout") for d in group],
            "fs_hz": [d.get("fs_hz") for d in group],
        })

        # exactly one verdict per candidate, in this order and no other
        sol = err = model = None
        geom_centroid = geometry_report(
            arr_sv, ids, arr_sv.positions(ids).mean(axis=0), policy["temp_c"], fixed_up)
        geom_fit = None
        loo = None
        if not bc["admissible"]:
            verdict = V_MARGIN_DEPENDENT if bc["margin_dependent"] else V_INADMISSIBLE
        elif not reached:
            verdict = V_LOST_TO_GATE
        else:
            model, sol, err = solve_event(arr_sv, ev["node_ids"], ev["arrivals"],
                                          policy["source_class"], policy["temp_c"],
                                          policy["v_mps"], fixed_up)
            verdict = solver_verdict(model, sol, err)
            if sol and sol.get("east_m") is not None:
                geom_fit = geometry_report(
                    arr_sv, ev["node_ids"],
                    (sol["east_m"], sol["north_m"], sol.get("up_m") or 0.0),
                    policy["temp_c"], fixed_up)
                loo = leave_one_out(arr_sv, ev["node_ids"], ev["arrivals"],
                                    policy["source_class"], policy["temp_c"], policy["v_mps"],
                                    fixed_up, sol)
        row = {
            "schema": TDOA_SCHEMA, "candidate_id": gi, "event_key": ek, "verdict": verdict,
            "t0_utc_s": arrivals[0], "node_ids": ids, "arrivals": arrivals,
            "pool_keys": pkeys,
            "node_names": [d["node_name"] for d in group],
            "n_nodes": len(group), "n_equations": len(group) - 1,
            "model": model, "source_class": policy["source_class"],
            "solution": sol, "solver_error": err,
            "associate_reason": lost_reason,
            "bound_check": bc,
            "geometry_at_centroid": geom_centroid,
            "geometry_at_fit": geom_fit,
            "leave_one_out": loo,
            "claim": {
                "residual_is_meaningful": (bool(sol.get("residual_is_meaningful"))
                                           if sol else False),
                "up_declared": fixed_up is not None,
                "position_is_2d_only": fixed_up is not None,
                "clock_bias_removed": False,
                "phone_latency_corrected": any(d.get("latency_applied_ms") is not None
                                               for d in group),
                "margin_dependent": bool(bc["margin_dependent"]),
                "evidence_of_absence": False,
            },
        }
        attempts.append(row)
        for d in group:
            key_of_det[d["pool_key"]] = verdict
            ev_key_of_det[d["pool_key"]] = ek
        if verdict == V_SOLVED and bc["admissible"]:
            pub = BP.to_dama_event(
                {"event_id": gi, "model": model, "source_class": policy["source_class"],
                 "n_nodes": len(ev["node_ids"]), "n_equations": len(ev["node_ids"]) - 1,
                 "node_ids": list(ev["node_ids"]), "t0_utc_s": ev["t0_utc_s"],
                 "solution": sol}, array_id=policy.get("array_id") or "hear")
            events_out.append(dict(row, published_payload=pub))
            solved_for_cal.append({"node_ids": list(ev["node_ids"]),
                                   "arrivals": list(ev["arrivals"])})

    # ---------------------------------------------------------- finalise the arrival ledger
    # ⚠️ONE TERMINAL STATE PER ADMITTED ROW, from the DRIVER's own scan and not from associate's
    # bookkeeping. A row that no candidate group contains is `singleton_unpaired` -- which is the
    # overwhelmingly common outcome on this corpus and is a measurement, not an error.
    n_singleton = 0
    for d in dets:
        v = key_of_det.get(d["pool_key"])
        if v is None:
            v, n_singleton = "singleton_unpaired", n_singleton + 1
        ledger.append({
            "schema": LEDGER_SCHEMA, "key": d["pool_key"], "node": d["node_name"],
            "node_id": d["node_id"], "source": d["source"], "day": d["day"],
            "ts_utc_s": d["ts_utc_s_raw"], "admitted": True, "drop_reason": None,
            "detail": None, "terminal": v, "event_key": ev_key_of_det.get(d["pool_key"]),
            "latency_applied_ms": d.get("latency_applied_ms"),
            "utc_trusted": d.get("utc_trusted"), "layout": d.get("layout"),
            "fs_hz": d.get("fs_hz"),
        })
    for r in ledger:
        r["run_id"] = report["run_id"]
        r["at"] = now
        r["ledger_hash"] = ledger_hash(r.get("key"), r["terminal"], r.get("drop_reason"),
                                       r.get("event_key"))

    by_terminal: Dict[str, int] = {}
    for r in ledger:
        if r["admitted"]:
            by_terminal[r["terminal"]] = by_terminal.get(r["terminal"], 0) + 1

    # ---------------------------------------------------------- clock bias (composed, optional)
    calib = None
    if policy.get("calibrate"):
        calib = _calibrate(arr_sv, solved_for_cal, policy, c)

    # ---------------------------------------------------------- null control
    if not co["bins_with_all"]:
        # ⚠️NO RATE OVER ZERO OPPORTUNITY. The scan above still ran -- refusing to run it would
        # hide candidates that straddle a bin edge, and conservation needs one verdict per
        # candidate either way -- but a coincidence RATE and its null are not reported, because
        # the denominator does not exist.
        null = {"skipped": True, "reason": "no bin had every arrival node detecting: a "
                                           "coincidence rate over zero opportunity is a number "
                                           "with no referent",
                "bins_with_all": 0, "trials": 0}
    else:
        null = shuffle_null(dets, arr_sv.ids, bounds, window_s, min_nodes,
                            policy["bin_s"], int(policy["null_trials"]), int(policy["null_seed"]))

    n_admissible = sum(1 for r in attempts if r["bound_check"]["admissible"])
    n_solved = sum(1 for r in attempts if r["verdict"] == V_SOLVED)
    plan = build_plan(arr_sv, policy["temp_c"], policy["v_mps"],
                      policy.get("candidates") or [], policy.get("site_box"),
                      policy.get("target_events") or DEFAULT_TARGET_EVENTS,
                      co=co, n_admissible=n_admissible)

    # ---------------------------------------------------------- conservation, three ways
    cons = {
        # I1 -- the byte pass against the admit loop. Different provenance is what makes it a
        # check rather than a restatement of the loop.
        "one_terminal_state_per_row": lines_read == a["n_admitted"] + a["n_dropped"],
        "lines_read": lines_read, "accounted": a["n_admitted"] + a["n_dropped"],
        # I2 -- every admitted arrival lands in exactly one terminal state of the DRIVER's scan.
        "admitted_accounted": a["n_admitted"] == sum(by_terminal.values()),
        "admitted": a["n_admitted"], "by_terminal": by_terminal,
        # I2b -- associate's own invariant, re-asserted on its return rather than trusted.
        "associate_conserved": assoc_accounted == len(dets),
        # I3 -- one verdict per candidate, no candidate silently dropped.
        "one_verdict_per_candidate": len(candidates) == len(attempts),
        "n_candidates": len(candidates), "n_attempts": len(attempts),
    }
    cons["ok"] = all(bool(cons[k]) for k in ("one_terminal_state_per_row", "admitted_accounted",
                                             "associate_conserved", "one_verdict_per_candidate"))

    report.update({
        "mode": "run",
        "candidates": len(candidates),
        "attempts": len(attempts),
        "by_verdict": {v: sum(1 for r in attempts if r["verdict"] == v) for v in VERDICTS},
        "events_admissible": n_admissible,
        "events_solved": n_solved,
        # ⚠️SOLVED AND ADMISSIBLE THIS RUN -- NOT what was written. `events_emitted` below is
        # the number of rows that actually reached events.jsonl, and the two differ by exactly
        # the events a previous run already published. Reporting the first under the second's
        # name is how a manifest came to claim an emission against a 0-byte file.
        "events_solved_admissible": len(events_out),
        "events_emitted": 0,
        "singleton_unpaired": n_singleton,
        "associate": {
            "n_events": len(grouped["events"]), "n_rejected": len(grouped["rejected"]),
            "n_duplicates": len(grouped["duplicates"]),
            "by_reason": {r: sum(1 for x in grouped["rejected"] if x["reason"] == r)
                          for r in sorted(AS.REASONS)},
        },
        "determinacy": determinacy_block(len(arr_sv), max(1, n_solved), fixed_up,
                                         policy.get("known_source")),
        "null": null,
        "plan": plan,
        "calibration": calib,
        "conservation": cons,
        "binding_constraint": (B_NO_RECORDS if not dets else
                               B_CO_ACTIVITY if not co["bins_with_all"] else
                               B_ADMISSIBILITY if not n_admissible else
                               B_SOLVED if n_solved else B_ADMISSIBILITY),
        "observation_not_health": {
            "pair_coincidences": pair_coincidences(dets, bounds),
            "by_node": {str(i): co["per_node"][str(i)]["n"] for i in arr_sv.ids},
            "n_solved": n_solved, "n_admissible": n_admissible,
        },
        "wall_s": time.time() - started,
    })

    if not cons["ok"]:
        # ⚠️NOTHING IS WRITTEN. A funnel that cannot account for its own inputs is not evidence,
        # and writing events off it would put unaccountable rows in an append-only store.
        raise NotInterpretable(
            "conservation broke: %s. Nothing written."
            % json.dumps({k: cons[k] for k in ("one_terminal_state_per_row", "admitted_accounted",
                                               "associate_conserved",
                                               "one_verdict_per_candidate")}, sort_keys=True))

    if policy.get("known_source") is not None:
        report["sound_speed_recovery"] = _recover_c(arr_sv, attempts, policy, c)

    # ---------------------------------------------------------- write
    if write:
        rd = os.path.join(runs_dir(out), report["run_id"])
        os.makedirs(rd, exist_ok=True)
        _write_json_atomic(os.path.join(rd, "coactivity.json"), co)
        _write_json_atomic(os.path.join(rd, "null.json"), null)
        _write_json_atomic(os.path.join(rd, "plan.json"), plan)
        _append_jsonl(os.path.join(rd, "candidates.jsonl"), cand_rows)
        _append_jsonl(os.path.join(rd, "attempts.jsonl"), attempts)
        new_events = [e for e in events_out if e["event_key"] not in prior_event_keys]
        _append_jsonl(os.path.join(rd, "events.jsonl"), new_events)
        report["events_new"] = len(new_events)
        report["events_emitted"] = len(new_events)     # what reached the file, not what solved
        # ⚠️MEMBERSHIP CHANGED = A MEMBER SET THAT SUPERSEDES A PUBLISHED ONE, which is what
        # event_key()'s docstring promises this number is. It used to be
        # `sum(1 for e in events_out if e[key] not in prior and prior)` -- algebraically
        # len(new_events) whenever anything had ever been emitted, so a first-ever backfill of
        # ten unrelated events reported ten "membership changes" and a genuine supersession on
        # an empty store reported none. The real question is whether a new key reuses an
        # arrival that was ALREADY PUBLISHED under a DIFFERENT key, which is exactly the
        # backfill-adds-a-member case, and it needs the prior member map to answer.
        superseded = set()
        n_changed = 0
        for e in new_events:
            prior = {prior_member_event[k] for k in (e.get("pool_keys") or ())
                     if k in prior_member_event and prior_member_event[k] != e["event_key"]}
            if prior:
                n_changed += 1
                superseded |= prior
        report["event_membership_changed"] = n_changed
        report["superseded_event_keys"] = sorted(superseded)
        # the per-record ledger: append-only, one row per DISTINCT verdict, deduped by hash
        fresh = [r for r in ledger if r["ledger_hash"] not in ledger_hashes]
        report["ledger_rows_written"] = len(fresh)
        parts: Dict[Tuple[str, str], List[str]] = {}
        for r in fresh:
            parts.setdefault((r.get("day") or "unanchored", r.get("source") or "unknown"),
                             []).append(json.dumps(_jsonable(r), sort_keys=True))
        for (day, source), lines in sorted(parts.items()):
            d = os.path.join(arrivals_dir(out), day)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s.jsonl" % source), "a") as fh:
                fh.write("\n".join(lines) + "\n")
        _write_json_atomic(os.path.join(rd, "manifest.json"),
                           {k: v for k, v in report.items() if k != "plan"})
        _write_json_atomic(model_card_path(out),
                           model_card(sv, arr_sv, policy, bounds, margin, c))
        _write_json_atomic(latest_path(out), {"run_id": report["run_id"], "at": now})
        write_heartbeat(out, report, now=now)
    return report


def _append_jsonl(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(_jsonable(r), sort_keys=True) + "\n")


def _calibrate(arr_sv: SV.Survey, solved: Sequence[Dict[str, Any]], policy: Dict[str, Any],
               c: float) -> Dict[str, Any]:
    """`calibrate.solve_multi` over the solved set. Its counting refusal is surfaced VERBATIM.

    ⚠️ITS REFUSAL IS THE MOST USEFUL SENTENCE THIS TOOL CAN PRINT ON A THIN CORPUS: "%d equations
    for %d unknowns ... add events, not nodes -- a bias is only separable from position because
    the source MOVES and it does not". Swallowing that and reporting "calibration unavailable"
    would delete the one line that says what to go and do.

    At least one node must be the reference; the PPS three are it. A phone or a provisional node
    is what floats.
    """
    ids = list(arr_sv.ids)
    if len(solved) < 2:
        return {"attempted": False,
                "reason": "solve_multi needs K >= 2 solved events to separate a constant bias "
                          "from position; %d available" % len(solved)}
    T = np.full((len(solved), len(ids)), np.nan)
    for k, ev in enumerate(solved):
        for nid, t in zip(ev["node_ids"], ev["arrivals"]):
            T[k, ids.index(int(nid))] = float(t)
    biased = [bool((arr_sv.classes.get(i) or "").startswith("puc")
                   or (arr_sv.classes.get(i) or "") == "gotchi-phone") for i in ids]
    if not any(biased):
        return {"attempted": False,
                "reason": "no arrival node is flagged biased: every receiver here is PPS-timed, "
                          "and flagging them all is rank-deficient by exactly one at any K"}
    try:
        r = CAL.solve_multi(arr_sv.positions(ids), T, biased, policy["source_class"],
                            temp_c=policy["temp_c"],
                            fixed_up_m=(policy["fixed_up_m"] if policy["fixed_up_m"] is not None
                                        else 0.0))
        return {"attempted": True, "result": r, "biased": biased, "node_ids": ids}
    except (CAL.CalibrationError, ValueError) as exc:
        return {"attempted": True, "refused": str(exc), "biased": biased, "node_ids": ids}


def _recover_c(arr_sv: SV.Survey, attempts: Sequence[Dict[str, Any]], policy: Dict[str, Any],
               c: float) -> Dict[str, Any]:
    """`soundspeed.recover_from_delays` against an operator-supplied surveyed source.

    Without `--known-source` this is never attempted, because `determines_speed` already says the
    answer: 3 ground receivers and an unknown source is 2 equations against 3 unknowns at any
    number of events. The verdict is reported whether or not it is valid -- a SpeedVerdict
    carrying `c_upper_mps` on a refusal is more informative than a missing field.
    """
    src = np.asarray(policy["known_source"], float)
    ids = list(arr_sv.ids)
    best = next((r for r in attempts if r["verdict"] == V_SOLVED), None)
    if best is None:
        return {"attempted": False, "reason": "no solved event to take delays from"}
    node_ids, arrivals = best["node_ids"], [None] * len(best["node_ids"])
    arrivals = [a for a in best.get("arrivals", [])] or None
    if arrivals is None:
        arrivals = [best["t0_utc_s"]] * len(node_ids)
    taus = {}
    for i in range(len(node_ids)):
        for j in range(i + 1, len(node_ids)):
            taus[(ids.index(int(node_ids[j])), ids.index(int(node_ids[i])))] = \
                float(arrivals[i]) - float(arrivals[j])
    v = SS.recover_from_delays(taus, arr_sv.positions(ids), source_position=src)
    return {"attempted": True, "valid": v.valid, "reasons": v.reasons, "c_mps": v.c_mps,
            "c_upper_mps": v.c_upper_mps, "temp_c": v.temp_c, "note": v.note,
            "assumed_c_mps": c}


# ================================================================= heartbeat and the gate

def write_heartbeat(out: str, report: Dict[str, Any], now: Optional[float] = None) -> Dict:
    """Merge this run into the ring, and record which node|day|reason buckets are NEW.

    ⚠️A REFUSAL BUCKET APPEARING WHERE IT NEVER HAS IS AN EVENT, NOT A RATE. A firmware or
    producer change flips a whole node's rows from admitted to refused in one step, so a
    threshold on a refusal PERCENTAGE reads 0% right up until it reads 100%. Same shape, and the
    same first-run exemption, as hear_score.write_heartbeat -- failing the very first check
    trains an operator to ignore the gate, which is worse than not having it.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(out)
    hb: Dict[str, Any] = {"runs": [], "buckets_ever": []}
    if os.path.exists(p):
        try:
            hb = json.load(open(p))
            hb.setdefault("runs", [])
            hb.setdefault("buckets_ever", [])
        except Exception:
            hb = {"runs": [], "buckets_ever": []}
    ever = set(hb.get("buckets_ever") or [])
    first_run = not hb.get("runs")
    seen_now = set((report.get("funnel") or {}).get("by_node_day_reason") or {})
    new_buckets = [] if first_run else sorted(seen_now - ever)
    hb["last_run_s"] = now
    hb["buckets_ever"] = sorted(ever | seen_now)
    f = report.get("funnel") or {}
    assoc = report.get("association") or {}
    cons = report.get("conservation") or {}
    hb["runs"] = (hb["runs"] + [{
        "at": now, "run_id": report.get("run_id"),
        "records_seen": f.get("lines_read"), "admitted": f.get("admitted"),
        "dropped": f.get("dropped"),
        "unparseable": (f.get("by_reason") or {}).get(D_UNPARSEABLE, 0),
        "outside_lookback_unassociated":
            (f.get("by_reason") or {}).get(D_OUTSIDE_LOOKBACK_UNASSOC, 0),
        "candidates": report.get("candidates"), "attempts": report.get("attempts"),
        "events_admissible": report.get("events_admissible"),
        "events_solved": report.get("events_solved"),
        "events_emitted": report.get("events_emitted"),
        "ledger_rows_written": report.get("ledger_rows_written"),
        "window_s": assoc.get("window_s"),
        "window_s_expected": assoc.get("window_s"),
        "n_arrival_ids": len((report.get("survey_block") or {}).get("arrival_ids") or []),
        "survey_ok": bool((report.get("survey_block") or {}).get("survey_ok")),
        "conservation_ok": bool(cons.get("ok")),
        "conservation": {k: bool(v) for k, v in cons.items() if isinstance(v, bool)},
        "by_reason": f.get("by_reason") or {},
        "by_verdict": report.get("by_verdict") or {},
        "binding_constraint": report.get("binding_constraint"),
        "new_buckets": new_buckets, "first_run": first_run,
        "bins_with_all": (report.get("coactivity") or {}).get("bins_with_all"),
        "observation_not_health": report.get("observation_not_health"),
    }])[-RUN_RING:]
    _write_json_atomic(p, hb)
    return hb


def check(out: str, max_stale_s: float = DEFAULT_MAX_STALE_S,
          window_s: float = DEFAULT_RUN_WINDOW_S, expect_window_s: Optional[float] = None,
          now: Optional[float] = None) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero when the SOLVER is not flowing -- never when nothing solved.

    ⚠️NOT ONE GATE HERE IS ABOUT EVENTS. On this corpus the correct number of solved events is
    zero and it will stay zero until three-way uptime changes, so a gate keyed on the event count
    would fire on a correct result forever and go quiet the day the tool broke. The gates are
    throughput, refusal-bucket novelty, torn lines, the accounting invariants, the survey, the
    window, the lookback, and staleness. Everything about events is published under
    `observation_not_health`.

    ⚠️SEVERAL GATES ARE ABSOLUTE, NOT GROWTH-CONDITIONED. Point --pool at /pool instead of
    /pool/corpus and every growth-conditioned gate passes vacuously on an empty read.
    `hear_drain.check()`'s `if not sensors: return 1` is the shape being copied.

    ⚠️IT READS A WINDOW OF THE RING, NOT THE LAST RUN. The solver runs 4x as often as this check.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(out)
    if not os.path.exists(p):
        return 1, ["no heartbeat at %s -- hear-tdoa has never completed a run" % p]
    try:
        hb = json.load(open(p))
    except Exception as exc:
        return 1, ["heartbeat at %s is unreadable (%s)" % (p, exc)]
    runs = hb.get("runs") or []
    if not runs:
        return 1, ["heartbeat records no runs"]
    lines: List[str] = []
    bad = 0
    last = runs[-1]

    age = now - float(hb.get("last_run_s") or last.get("at") or 0.0)
    if age > max_stale_s:
        lines.append("solver   STALE     last run %.0f s ago (max %.0f)" % (age, max_stale_s))
        bad += 1
    else:
        lines.append("solver   ok        last run %.0f s ago" % age)

    window = [r for r in runs if now - float(r.get("at") or 0.0) <= window_s] or [last]
    lines.append("window   %d run(s) in the last %.0f s" % (len(window), window_s))

    seen = last.get("records_seen") or 0
    if not seen:
        lines.append("pool     EMPTY     the newest run read 0 records -- an empty read is a "
                     "failure, not a quiet night (wrong --pool root?)")
        bad += 1
    else:
        lines.append("pool     ok        %d record(s) in the store" % seen)

    torn = sum(int(r.get("unparseable") or 0) for r in window)
    if torn:
        lines.append("parse    TORN      %d unparseable pool line(s) in the window" % torn)
        bad += 1

    broken = [r for r in window if not r.get("conservation_ok", True)]
    if broken:
        lines.append("ledger   BROKEN    %d run(s) could not account for every stored line"
                     % len(broken))
        bad += 1

    nsv = [r for r in window if not r.get("survey_ok", True)
           or int(r.get("n_arrival_ids") or 0) < 3]
    if nsv:
        lines.append("survey   REFUSED   %d run(s) had a bad survey or < 3 arrival-class "
                     "receivers -- 2 receivers is rank-1 and singular for any geometry"
                     % len(nsv))
        bad += 1

    lost = sum(int(r.get("outside_lookback_unassociated") or 0) for r in window)
    if lost:
        lines.append("lookback LOST      %d record(s) fell out of --lookback-h without ever "
                     "being associated; they can never be" % lost)
        bad += 1

    if expect_window_s is not None:
        drift = [r for r in window
                 if r.get("window_s") is not None
                 and abs(float(r["window_s"]) - float(expect_window_s)) > 1e-6]
        if drift:
            lines.append("window   DRIFT     %d run(s) used a scan window other than %.6f s -- "
                         "the puc-sized-window regression is back" % (len(drift), expect_window_s))
            bad += 1

    newb = sorted({b for r in window for b in (r.get("new_buckets") or [])})
    if newb:
        lines.append("refusals NEW       %d node|day|reason bucket(s) never seen before: %s"
                     % (len(newb), ", ".join(newb[:6]) + (" ..." if len(newb) > 6 else "")))
        bad += 1

    grew = (window[-1].get("records_seen") or 0) - (window[0].get("records_seen") or 0)
    examined = sum(int(r.get("admitted") or 0) + int(r.get("dropped") or 0) for r in window)
    if grew > 0 and examined == 0:
        lines.append("through  STUCK     the store grew by %d record(s) across the window and "
                     "the driver examined 0" % grew)
        bad += 1
    else:
        lines.append("through  ok        store +%d, %d record(s) examined across the window"
                     % (grew, examined))

    lines.append("binding  %s   (OBSERVATION, not a gate)" % (last.get("binding_constraint"),))
    lines.append("events   solved %s  admissible %s  co-active bins %s  (OBSERVATION, not a gate)"
                 % (last.get("events_solved"), last.get("events_admissible"),
                    last.get("bins_with_all")))
    lines.append("verdicts %s  (OBSERVATION, not a gate)"
                 % json.dumps(last.get("by_verdict") or {}, sort_keys=True))
    return (1 if bad else 0), lines


# ================================================================= decisions on the record

#: Modules deliberately NOT composed, each with the reason, so the omission is a decision rather
#: than an oversight. This ships in every manifest.
NOT_COMPOSED = {
    "hear.solve.burstassoc": (
        "answers a different question -- one shared delay tau fitted across two sensors' own "
        "burst series -- has zero callers outside its tests, and is validated only on synthetic "
        "ground truth because real field data has too little co-activity between any two nodes "
        "to test it. It is not on the path that turns detections into solvable events."),
    "hear.solve.consistency.check_array": (
        "used for its primitives (physically_possible, spacing) and NOT as a gate. Its closure "
        "and additivity residuals are identically zero BY CONSTRUCTION when every tau is derived "
        "from absolute arrival times, so thresholding them is a check that cannot fail. Any "
        "future use of its PairCheck table for reporting must carry "
        "closure_is_vacuous_for_derived_taus: true beside the number."),
    "hear.solve.consistency.null_pass_rate/null_from_events/decoy_taus": (
        "they null the ARRAY GATE given a tau dict from a co-located mic array. The question "
        "here is the accidental-coincidence rate across surveyed nodes on their own clocks, "
        "which is what shuffle_null measures."),
    "hear.solve.crackblast": (
        "single-mic range from the crack-to-blast interval -- the one method that works with ONE "
        "receiver and would otherwise rescue this corpus -- needs a raw audio buffer. The pool "
        "holds 172-byte sketches. Wiring it needs clips (hear/clips.py) and a --clips-dir; until "
        "then the honest answer is no_waveform_in_pool, not a fallback."),
    "hear.backend.pipeline.Backend.ingest": (
        "takes raw wire bytes and calls wire.decode(), which requires a v2 frame carrying "
        "node_id/seq/us_of_day. Pool records store v1 SKETCH bytes and would be refused "
        "v1_frame_has_no_node_id. flush()'s model dispatch is mirrored instead and pinned equal "
        "by test; the clean fix is to extract pipeline.solve_event() upstream."),
    "hear.solve.placement.dop3": (
        "refused by PL below 4 nodes, so the key is ABSENT rather than None at n=3: a null reads "
        "as 'computed, no answer' where the truth is 'not asked'."),
}

#: What a run does NOT establish. Ships in every manifest and is repeated in the model card.
COULD_NOT_DO = [
    "It does not make an unsolvable corpus solvable. Zero physically-admissible triples over the "
    "measured three-way co-active time puts a rule-of-three Poisson ceiling on the natural rate; "
    "plan.json carries the arithmetic. The first fix is three-way uptime, then a shared "
    "characterised detector threshold, then a controlled impulsive source.",
    "It does not fix associate.py, MARGIN_S or max_window_s. It routes around all three by "
    "construction and REPORTS the discrepancy (window_s vs window_s_full_survey, margin_s vs "
    "library_default_margin_s), so the library defect stays visible instead of silently masked.",
    "It never corrects an arrival time except from a hear_latency_cal table that already passed "
    "that tool's own verdict(), and it records the correction per arrival. It does not fit one.",
    "No waveform path. The pool holds sketches, so crackblast's single-mic range has no input.",
    "The band-axis seam does not touch this path -- associate() reads only node_id, seq and "
    "t_utc_s. layout and fs_hz are carried on every admitted arrival anyway, so a timing/axis "
    "correlation would already be in the ledger if one appeared.",
    "Why one particular receiver pair breaks its bound is NOT established here. Per-pair excess "
    "is reported so the question stays measurable; a siting error and ordinary accidental "
    "pairing are not distinguishable at these counts, and no clips were examined.",
]


# ================================================================= report and CLI

def format_report(t: Dict[str, Any]) -> str:
    out: List[str] = []
    sb = t.get("survey_block") or {}
    out.append("survey   %d node(s), %d admitted as arrivals: %s"
               % (len(sb.get("all_ids") or []), len(sb.get("arrival_ids") or []),
                  ", ".join(sb.get("arrival_names") or [])))
    for r in sb.get("refused_as_arrivals") or []:
        out.append("         REFUSED %s (sigma %.2f m): %s" % (r["name"], r["sigma_m"], r["why"]))
    a = t.get("association") or {}
    m = t.get("margin") or {}
    if a:
        out.append("window   %.4f s from the arrival nodes alone; the FULL survey would give "
                   "%.4f s (+%.0f%%)"
                   % (a["window_s"], a["window_s_full_survey"],
                      100.0 * (a["window_inflation_frac"] or 0.0)))
        out.append("margin   %.4f s (%s) = %.0f%% of the tightest pair bound; the library "
                   "default %.4f s would be %.0f%%"
                   % (m["margin_s"], m["margin_source"],
                      100.0 * m["margin_over_tightest_bound"], m["library_default_margin_s"],
                      100.0 * m["library_default_over_tightest_bound"]))
    if t.get("mode") == "plan_only":
        return "\n".join(out + _plan_lines(t.get("plan") or {}))

    # ⚠️CO-ACTIVITY FIRST. It is the binding constraint by an order of magnitude on this corpus,
    # and a coincidence count printed above it invites the reader to interpret a number whose
    # denominator has not been stated yet.
    co = t.get("coactivity") or {}
    out.append("")
    out.append("co-activity (bin %.0f s): %d bin(s) with any node, %d with ALL %d arrival nodes"
               % (co.get("bin_s") or 0, co.get("bins_total") or 0,
                  co.get("bins_with_all") or 0, co.get("n_arrival_nodes") or 0))
    for k in sorted(co.get("per_node") or {}):
        pn = co["per_node"][k]
        out.append("  node %-3s n %-6d median inter-detection %s"
                   % (k, pn["n"],
                      "n/a" if pn["median_interval_s"] is None
                      else "%.3f s" % pn["median_interval_s"]))
    for k, v in sorted((co.get("bins_pairwise") or {}).items()):
        out.append("  pair %-8s co-active bins %d" % (k, v))

    f = t.get("funnel") or {}
    out.append("")
    out.append("funnel   read %d, admitted %d, dropped %d"
               % (f.get("lines_read") or 0, f.get("admitted") or 0, f.get("dropped") or 0))
    for k in sorted(f.get("by_reason") or {}):
        out.append("  %-32s %d" % (k, f["by_reason"][k]))
    if t.get("mode") == "census":
        return "\n".join(out)

    n = t.get("null") or {}
    out.append("")
    if n.get("skipped"):
        out.append("null     SKIPPED -- %s" % n.get("reason"))
        out.append("coincidences NOT reported as a rate: there is no denominator")
    else:
        tr = n["triples"]
        out.append("null     %d trial(s), circular shift within %.0f s bins"
                   % (n["trials"], n["bin_s"]))
        out.append("triples  observed %d   null %.2f +- %.2f (p95 %.0f, max %.0f)  p_emp %.4f"
                   % (tr["observed"], tr["null_mean"], tr["null_sd"], tr["null_p95"],
                      tr["null_max"], tr["p_emp"]))
        for k in sorted(n.get("pairs") or {}):
            p = n["pairs"][k]
            out.append("  pair %-8s observed %-5d null %.2f +- %.2f   p_emp %.4f"
                       % (k, p["observed"], p["null_mean"], p["null_sd"], p["p_emp"]))
    out.append("")
    out.append("candidates %d (UNGATED scan)   attempts %d   admissible at tol_s=0: %d   "
               "solved %d   emitted %d"
               % (t.get("candidates") or 0, t.get("attempts") or 0,
                  t.get("events_admissible") or 0, t.get("events_solved") or 0,
                  t.get("events_emitted") or 0))
    out.append("verdicts " + json.dumps(t.get("by_verdict") or {}, sort_keys=True))
    ass = t.get("associate") or {}
    out.append("associate events %d  rejected %d  duplicates %d   %s"
               % (ass.get("n_events") or 0, ass.get("n_rejected") or 0,
                  ass.get("n_duplicates") or 0,
                  json.dumps({k: v for k, v in (ass.get("by_reason") or {}).items() if v},
                             sort_keys=True)))
    d = t.get("determinacy") or {}
    out.append("c        ASSUMED %.2f m/s from temp %.1f C; determines_speed: %s"
               % ((t.get("c") or {}).get("sound_speed_mps") or 0.0,
                  (t.get("policy") or {}).get("temp_c") or 0.0, d.get("reason")))
    cons = t.get("conservation") or {}
    out.append("conservation %s  (read %s == accounted %s; admitted %s in %s terminal state(s); "
               "%s candidate(s) == %s verdict(s))"
               % ("OK" if cons.get("ok") else "BROKEN", cons.get("lines_read"),
                  cons.get("accounted"), cons.get("admitted"),
                  len(cons.get("by_terminal") or {}), cons.get("n_candidates"),
                  cons.get("n_attempts")))
    out.append("terminal states: " + json.dumps(cons.get("by_terminal") or {}, sort_keys=True))
    out.append("")
    out.append("BINDING CONSTRAINT: %s" % t.get("binding_constraint"))
    out += _plan_lines(t.get("plan") or {})
    out.append("")
    if not t.get("events_solved"):
        out.append("⚠️zero events is NOT evidence of no gunshots. The referent is the "
                   "co-activity number above, not the event count. See tdoa/model_card.json.")
    else:
        out.append("⚠️a solved position is conditional on --source-class, on the DECLARED height "
                   "and on the ASSUMED c, and at the exactly-determined node count its residual "
                   "is ~0 by construction. See tdoa/model_card.json.")
    return "\n".join(out)


def _plan_lines(plan: Dict[str, Any]) -> List[str]:
    if not plan:
        return []
    out = ["", "plan"]
    g = plan.get("dop_grid") or {}
    out.append("  median DOP %.2f over the box; %.0f%% of it at DOP <= %.0f (geometry only -- a "
               "node must still HEAR it)"
               % (g.get("median") or float("nan"), 100.0 * (g.get("usable_frac") or 0.0),
                  g.get("dop_usable_threshold") or 0.0))
    w = plan.get("worst_bearing") or {}
    out.append("  thinnest bearing %.0f deg: track offset observable only in a %.1f m band"
               % (w.get("bearing_deg") or 0.0, w.get("span_m") or 0.0))
    u = plan.get("uptime") or {}
    if u:
        if not u.get("co_active_bins"):
            out.append("  uptime  %s" % u.get("note"))
        else:
            out.append("  uptime  %d co-active bin(s) of %.0f s, %d admissible; ceiling "
                       "%.4f/bin -> >= %s bin(s) for %d event(s)"
                       % (u["co_active_bins"], u["bin_s"] or 0.0, u["admissible_events"],
                          u["rate_upper_95_per_bin"] or 0.0,
                          u.get("bins_needed_for_target"), plan.get("target_events") or 0))
    for r in (plan.get("best_addition") or [])[:5]:
        out.append("  candidate (%7.1f,%7.1f)  median DOP %6.2f (%+.2f)  thinnest band %6.1f m "
                   "(%+.1f)" % (r["position"][0], r["position"][1], r["median_dop"],
                                -r["dop_gain"], r["worst_span_m"], r["worst_span_gain_m"]))
    return out


def _parse_point(s: str) -> List[float]:
    v = [float(x) for x in s.split(",")]
    if len(v) not in (2, 3):
        raise Refusal("a point wants E,N or E,N,U in local metres, got %r" % (s,))
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", default="~/hear-pool", help="pool root; records/ is read")
    ap.add_argument("--survey", default=os.path.join(repo_root(), "survey.json"),
                    help="node survey JSON; the ONLY door to geometry")
    ap.add_argument("--out", default=None, help="write root (default <pool>/tdoa). Refused if "
                                                "it resolves inside a checkout")
    ap.add_argument("--day", action="append", default=[], help="restrict to a day partition")
    ap.add_argument("--since", type=float, default=None, metavar="UTC_S")
    ap.add_argument("--until", type=float, default=None, metavar="UTC_S")
    ap.add_argument("--source", action="append", default=[],
                    help="pool source to read (repeatable; default node)")
    ap.add_argument("--lookback-h", type=float, default=DEFAULT_LOOKBACK_H)
    ap.add_argument("--settle-s", type=float, default=DEFAULT_SETTLE_S)
    # ⚠️NO DEFAULT. The class decides point-vs-cone and nothing in this repo classifies; a
    # default would be this tool asserting what the array heard.
    ap.add_argument("--source-class", default=None,
                    help="REQUIRED for a solve: %s (point) or %s (cone). An OPERATOR ASSERTION"
                         % (sorted(PT.POINT_CLASSES), sorted(PT.CONE_CLASSES)))
    ap.add_argument("--fixed-up-m", default="0.0",
                    help="DECLARE the source height (two unknowns, three receivers), or the "
                         "literal 'none' to attempt a 3D fit (three unknowns, four receivers)")
    ap.add_argument("--temp-c", type=float, default=20.0)
    ap.add_argument("--v-mps", type=float, default=900.0, help="cone lane only")
    ap.add_argument("--min-nodes", type=int, default=3)
    ap.add_argument("--margin-frac", type=float, default=DEFAULT_MARGIN_FRAC)
    ap.add_argument("--margin-s", type=float, default=None,
                    help="override the derived margin; refused above %.0f%% of the tightest "
                         "pair bound without --force-margin" % (100.0 * HARD_MARGIN_FRAC))
    ap.add_argument("--force-margin", action="store_true")
    ap.add_argument("--bin-s", type=float, default=DEFAULT_BIN_S)
    ap.add_argument("--max-sync-sigma-ns", type=float, default=None,
                    help="default %.0f%% of the tightest pair bound"
                         % (100.0 * DEFAULT_SYNC_SIGMA_FRAC))
    ap.add_argument("--allow-phones", action="store_true",
                    help="also read the phone source. They are unsurveyed, so today every row "
                         "still lands in unsurveyed_node -- this makes that a counted number")
    ap.add_argument("--clock-unstated", choices=("admit", "refuse"), default="refuse")
    # ⚠️DEFAULT admit, WHERE --clock-unstated DEFAULTS refuse, AND THE ASYMMETRY IS THE POINT.
    # An unstated CLOCK is a producer that measures trust and did not say; refusing it costs a
    # phone row. An unstated ONSET is a quantity no producer in this chain measures at all
    # (_ONSET_UNSTATED_WHY), so refusing it by default would empty the corpus and report a
    # missing FIELD as an absence of EVENTS. `refuse` is offered so the cost is measurable:
    # run it and the admitted count is what a stated-onset chain would have left.
    ap.add_argument("--onset-unstated", choices=("admit", "refuse"), default="admit",
                    help="what to do with an arrival whose producer never measured onset "
                         "quality. Today that is every source=node row: see the run's "
                         "onset_quality block")
    ap.add_argument("--latency-cal", default=None,
                    help="acoustic_latency_calibration.json from tools/hear_latency_cal.py")
    ap.add_argument("--null-trials", type=int, default=DEFAULT_NULL_TRIALS)
    ap.add_argument("--null-seed", type=int, default=DEFAULT_NULL_SEED)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--known-source", default=None, metavar="E,N[,U]")
    ap.add_argument("--candidate", action="append", default=[], metavar="E,N[,U]")
    ap.add_argument("--site-box", default=None, metavar="XMIN,YMIN,XMAX,YMAX")
    ap.add_argument("--target-events", type=int, default=DEFAULT_TARGET_EVENTS)
    ap.add_argument("--array-id", default="hear")
    ap.add_argument("--census", action="store_true", help="stages 1-2 only; writes NOTHING")
    ap.add_argument("--plan-only", action="store_true", help="survey only; reads no pool")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-stale-s", type=float, default=DEFAULT_MAX_STALE_S)
    ap.add_argument("--window-s", type=float, default=DEFAULT_RUN_WINDOW_S,
                    help="--check sums the heartbeat ring over this many seconds")
    a = ap.parse_args(argv)

    root = os.path.expanduser(a.pool)
    out = os.path.expanduser(a.out) if a.out else out_dir_default(root)

    if a.check:
        expect = None
        try:
            sv = SV.load_survey(os.path.expanduser(a.survey), min_nodes=3)
            expect = AS.max_window_s(
                arrival_survey(sv), a.temp_c,
                derive_margin_s(pair_bounds(arrival_survey(sv), SW.sound_speed(a.temp_c)),
                                a.margin_frac, a.margin_s, a.force_margin)["margin_s"])
        except Exception:
            expect = None            # the survey gate below is what reports a bad survey
        code, lines = check(out, a.max_stale_s, window_s=a.window_s, expect_window_s=expect)
        print("\n".join(lines))
        return code

    if not a.plan_only and not a.source_class:
        print("refused: --source-class is required and has no default. Point classes: %s; cone "
              "classes: %s. It decides the model and nothing in this repo classifies, so a "
              "default would be this tool asserting what the array heard."
              % (sorted(PT.POINT_CLASSES), sorted(PT.CONE_CLASSES)), file=sys.stderr)
        return 1

    try:
        fixed_up = None if str(a.fixed_up_m).strip().lower() == "none" else float(a.fixed_up_m)
    except ValueError:
        print("refused: --fixed-up-m wants metres or the literal 'none', got %r" % (a.fixed_up_m,),
              file=sys.stderr)
        return 1

    cal = None
    if a.latency_cal:
        try:
            with open(os.path.expanduser(a.latency_cal)) as fh:
                cal = json.load(fh)
        except OSError as exc:
            print("refused: --latency-cal %s is unreadable: %s" % (a.latency_cal, exc),
                  file=sys.stderr)
            return 1

    sources = list(dict.fromkeys(a.source or ["node"]))
    if a.allow_phones and "phone" not in sources:
        sources.append("phone")

    policy: Dict[str, Any] = {
        "sources": sources, "days": list(a.day), "since": a.since, "until": a.until,
        "lookback_h": a.lookback_h, "settle_s": a.settle_s,
        "source_class": a.source_class, "fixed_up_m": fixed_up, "temp_c": a.temp_c,
        "v_mps": a.v_mps, "min_nodes": a.min_nodes, "margin_frac": a.margin_frac,
        "margin_s_override": a.margin_s, "force_margin": bool(a.force_margin),
        "bin_s": a.bin_s, "max_sync_sigma_ns": a.max_sync_sigma_ns,
        "clock_unstated": a.clock_unstated, "onset_unstated": a.onset_unstated,
        "latency_cal": cal,
        "null_trials": a.null_trials, "null_seed": a.null_seed,
        "calibrate": bool(a.calibrate),
        "known_source": _parse_point(a.known_source) if a.known_source else None,
        "candidates": list(a.candidate), "site_box": a.site_box,
        "target_events": a.target_events, "array_id": a.array_id,
        "allow_phones": bool(a.allow_phones),
    }

    try:
        t = run(root, os.path.expanduser(a.survey), policy, out=out,
                write=not (a.census or a.no_write or a.plan_only),
                plan_only=bool(a.plan_only), census=bool(a.census))
    except Refusal as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 1
    except NotInterpretable as exc:
        print("NOT INTERPRETABLE: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(_jsonable(t), indent=2, sort_keys=True) if a.json else format_report(t))
    # ⚠️ZERO SOLVED EVENTS IS EXIT 0. The product is the funnel. What fails is losing track of a
    # record, not failing to find a gunshot in a corpus that does not contain one.
    return 0 if (t.get("conservation") or {}).get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
