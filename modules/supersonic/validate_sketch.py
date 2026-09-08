#!/usr/bin/env python3
"""Alarm reproducibility for the SUPERVISED sketch classifier -- `hear/validate.py` applied to
`modules/supersonic/train_sketch.py`.

`train_sketch.py` reports AUC 0.9732 from nested grouped CV over 228 events in 69 groups (events
grouped `int(utc // 3)`, one group per firing string). That number says the classifier separates
shot/not-shot across FOLDS of this corpus. It has never said whether two classifiers fit on
genuinely disjoint halves of the data would FLAG THE SAME held-out items -- alarm reproducibility,
`hear.validate.alarm_reproducibility`, asked here of a supervised scorer instead of an
unsupervised one.

REUSE, NOT REIMPLEMENTATION. `train_sketch.load()` does the feature extraction (onset detection,
the sketch bank, the absolute-dB construction) and is called here UNCHANGED; this module reads
`it["utc"]` a second time, directly from the same items file `load()` already parsed, to recover
the per-event wall-clock time that `load()` folds into `int(utc // 3)` and then discards. Nothing
in `hear/sketch.py`, `hear/node/detect.py`, or `train_sketch.py` is touched or reimplemented, and
the 3 s grouping constant is not changed.

IS `int(utc // 3)` A SUFFICIENT SPLIT UNIT? MEASURED 2026-09-08 -- NO.
`hear.validate.decorrelation_lag_s`, run on the 228 labelled sketch vectors ordered by event
time, finds the sketch features do not fall below cosine 0.5 until **22.4 s** apart (lag_rows=80).
That is ~7.5x the 3 s bucket `train_sketch.load` groups by. Concretely: 91.7% of the 228 events
(209/228) have a DIFFERENT-group neighbour within that 22.4 s, and 77.9% of consecutive groups
(53/68), ordered by time, are themselves closer together than the lag. A `GroupKFold` split that
keeps every event of one 3 s bucket on one side of a fold does nothing to stop the NEXT bucket,
4 s later and still the same firing string, landing on the other side. This is `make_blocks`'s
point (a constant copied between subsystems keeps its number and loses its units-of-meaning)
applied to a different corpus: `int(utc // 3)` was sized for the thing it is legitimately used
for -- never split one string's rounds across folds -- and reused, silently, as an independence
guarantee it was never sized to provide.

HOW MUCH THAT 22.4 s LEANS ON `rho`, since one number carrying a whole conclusion should be
asked. The measured cosine curve here is not monotone -- 0.508 at lag 1 event (27 pairs), back up
to 0.698 at lag 3 -- so under a FIRST-crossing rule the answer was decided by which side of 0.50
a 27-pair estimate landed on: rho=0.52 gave 0.13 s and "int(utc // 3) is 23x MORE than
sufficient", a 172x swing on unchanged data. `decorrelation_lag_s` now requires the crossing to
be SUSTAINED (the grid point after the LAST lag at or above rho), which takes the swing to 1.4x
-- 22.37 s at rho 0.40-0.50, 15.82 s at 0.52-0.60 -- and both bracket values plus the pair count
at the crossing (10) are printed on every run by `group_leakage`. The conclusion does not turn
on rho any more; it did.

TWO BLOCK SCHEMES, REPORTED SIDE BY SIDE, NEITHER PICKED AS "the" ANSWER.
  - `native_group_blocks` -- blocks = `train_sketch`'s own `int(utc // 3)` groups, unmodified,
    69 of them on the real corpus. Its closest two events in different blocks are 0.64 s apart
    against a 22.4 s lag, so `hear.validate.separation_check` REFUSES it and it is scored only
    under an explicit `allow_unseparated=True`, with `UNSEPARATED` on its report line. That is
    the naive scheme being labelled, not disqualified.
  - `lag_merged_blocks` -- groups are folded together until the next group's first event is more
    than the measured lag after the last event already in the block; 69 groups collapse to 16 and
    the realised closest cross-block gap is 24.79 s, clear of the 22.4 s lag.
Both go through `hear.validate.alarm_reproducibility` UNMODIFIED, and this module builds `Blocks`
by hand rather than reusing `make_blocks`'s regular-cadence boundary algorithm, which does not
apply to 228 sparse, bursty events. What that hand-building used to lose was every refusal in
`make_blocks`: `alarm_reproducibility` re-checked nothing about the object it was handed, so the
`native` scheme shipped `block_s=3.0` against `lag_s=22.4` with `guard_s=0.0` -- the exact split
`make_blocks` refuses by name -- and was scored without objection. `Blocks.__post_init__` and
`separation_check` now live in `hear/validate.py` for that reason.

MEASURED (fixed C=3.0, the shipped `model_sketch.json`'s own chosen C; parts=3;
pctl=100*(1-prevalence)=56.1, prevalence 43.9% over 100 shot / 128 not-shot, d=160):

    scheme      blocks a/b/hold  n_a n_b  jaccard  chance  excess  rate_a  rate_b  pearson
    native         69  23/23/23   75  71   0.617    0.280  +0.337   57.3%   35.4%   0.968
    lag_merged     16   6/ 5/ 5   45  58   0.667    0.030  +0.637    4.8%    7.2%   0.787
                                                                    (design 43.9%)

THE RAW JACCARD DOES NOT MOVE THE WAY THE SCENE MODULE'S DID; THE CHANCE-CORRECTED ONE DOES. On
the scene corpus (`TestTheRefusedSplitIsTheFlatteringOne` in `tests/test_validate.py`) the naive
split's Jaccard was the flattering number and the honest split dropped it. Here the raw Jaccard
goes the OTHER way (0.617 -> 0.667). But Jaccard's null depends on the two alarm rates -- two
INDEPENDENT flaggers at 57.3% and 35.4% overlap at 0.280 by chance, and at 4.8% and 7.2% they
overlap at 0.030 -- so most of `native`'s 0.617 is its rates, not its agreement. Against chance
the two schemes read +0.337 and +0.637, and with only 16 lag-respecting blocks split three ways
(`blocks_holdout` is 5) this corpus is still too small to call that direction real either way.
What DOES move sharply and in the direction the leak predicts is CALIBRATION: both `native` rates
bracket the 43.9% design (25% mean deviation); both `lag_merged` rates collapse to single digits
(86% mean deviation). Read as: the honest split does not show the two fits disagreeing about
WHICH events are risky so much as it shows both of them losing confidence that ANY held-out event
is risky, once "held out" means "not within 22 s of anything the classifier trained on."

`d=160 >> n_a, n_b` IS A SEPARATE, LARGER PROBLEM AND IS NOT THIS MODULE'S HEADLINE FINDING. Each
independent fit sees 45-75 events for a 160-dimensional feature vector -- `n/d` is printed
because it is the number that actually bears on a fixed-C logistic fit, where `blocks_per_dim`
(inherited from `alarm_reproducibility`, sized for a Mahalanobis covariance's O(d^2) free
parameters) does not apply and reads a meaningless 0.03-0.14 here. Halving `bands` to reduce `d`
(10/5/2 bands, tried on `lag_merged`) did NOT raise Jaccard -- 0.667 -> 0.400 -> 0.375 -> 0.357 --
so this is recorded as a MEASUREMENT, not asserted as a fix: `blocks_holdout` (5) is too small at
this corpus size for the comparison to be more than noise, and nothing here establishes why the
direction is what it is.

WHAT `main()` DOES WHEN THE LABEL CORPUS IS UNAVAILABLE. `~/analysis_20260905/label_items.json`
is a workstation-local export, not part of this repo and not guaranteed present on every box. If
it is missing, this module says so on stderr, in those words, and switches to a SYNTHETIC corpus
built with the same coarse shape (bursty group timing, 160-dim features, a real decorrelation
lag measured on THAT data) so the machinery itself stays exercised. It never substitutes a
made-up number for the 22.4s / 91.7% / 77.9% / Jaccard figures above -- those are reported only
when the real corpus was actually loaded.

    python3 modules/supersonic/validate_sketch.py \\
        --items ~/analysis_20260905/label_items.json \\
        --labels '~/analysis_20260905/labels/labels/*.json'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from hear import validate as V                       # noqa: E402
from modules.supersonic import train_sketch as TS     # noqa: E402

#: The shipped `modules/supersonic/model_sketch.json`'s own chosen `C`. Not re-derived here --
#: see `_sketch_fit`'s docstring for why a per-fit inner-CV choice is not available to it.
DEFAULT_C = 3.0
#: `train_sketch.load`'s own grouping width, `int(utc // 3)`. A literal, not a re-derivation --
#: this module reports against it, and is instructed not to change it.
GROUP_S = 3.0


@dataclass(frozen=True, eq=False)
class Corpus:
    """A labelled sketch corpus: `train_sketch.load`'s `(X, y, g, ids)` plus `t`.

    `eq=False` for the same reason as `hear.validate.Blocks`: the fields are ndarrays, and the
    generated `__eq__` would return an array whose truth value is ambiguous.
    """
    X: np.ndarray
    y: np.ndarray
    g: np.ndarray
    t: np.ndarray
    ids: List[str]


def _utc_for_ids(items_path: str, ids: List[str]) -> np.ndarray:
    """Per-event utc, in `ids` order -- read directly from the items file `train_sketch.load`
    already parsed, using the identical `{i["id"]: i for i in json.load(...)}` indexing it uses
    internally. Not a second feature-loading path: `load()` computed `g = int(utc // 3)` from
    this exact field and threw the float away, and this recovers only that.
    """
    items = {i["id"]: i for i in json.load(open(os.path.expanduser(items_path)))}
    missing = [k for k in ids if k not in items]
    if missing:
        raise V.Refused("%d id(s) load() returned are not in %s: %r"
                        % (len(missing), items_path, missing[:5]))
    return np.array([float(items[k]["utc"]) for k in ids], dtype=float)


def load_sketch_corpus(items_path: str, labels_glob: str,
                       layout: str = TS.SK.LAYOUT_FIXED) -> Corpus:
    """`Corpus` via `train_sketch.load`, unmodified, plus `t`. See module docstring."""
    X, y, g, ids = TS.load(labels_glob, items_path, layout)
    t = _utc_for_ids(items_path, ids)
    return Corpus(X=X, y=y, g=np.asarray(g), t=t, ids=ids)


# --------------------------------------------------------------------------- leakage diagnostic

def group_leakage(X: np.ndarray, t: np.ndarray, g: np.ndarray, *, group_s: float = GROUP_S,
                  max_lag_s: Optional[float] = None) -> Dict[str, Any]:
    """Is `group_s` wide enough to keep two DIFFERENT groups' sketch vectors independent?
    Measures, rather than assumes -- see the module docstring's "MEASURED" section for the
    22.4 s / 91.7% / 77.9% numbers this returns on the real corpus.

    Runs `hear.validate.decorrelation_lag_s` on the labelled sketch vectors themselves, ordered
    by event time -- the same estimator `hear/validate.py` runs on scene.csv rows, because the
    question is the same one: how far apart in time do two rows have to be before they stop
    looking like each other. `sufficient = lag_s <= group_s`.

    HOW HARD THE LAG LEANS ON `rho` IS REPORTED, not assumed away. The measured curve here is
    not monotone -- 0.508 at lag 1 event (27 pairs), 0.698 at lag 3 -- so under a FIRST-crossing
    rule the answer was decided by which side of 0.50 a 27-pair estimate happened to land on, and
    moving rho to 0.52 moved it from 22.4 s to 0.13 s: a 172x swing on unchanged data, flipping
    `sufficient` with it. `hear.validate.decorrelation_lag_s` now requires the crossing to be
    SUSTAINED, which takes that swing to 1.4x (22.37 s at rho 0.40-0.50, 15.82 s at 0.52-0.60),
    and `lag_swing_x` publishes it so the next corpus does not have to be argued about.

    Two further numbers, computed directly rather than inferred from `lag_s` alone:
      - `frac_events_with_cross_group_neighbor`: of all events, the fraction with at least one
        event from a DIFFERENT group within the measured lag.
      - `frac_adjacent_groups_within_lag`: of consecutive groups ordered by time, the fraction
        whose centres are closer together than the lag. `None` if fewer than 2 groups.
    Both near-unanimous on the real corpus means a `GroupKFold` split on `g` alone routinely puts
    two groups seconds apart -- still inside the corpus's own measured correlation lag -- on
    opposite sides of a fold.
    """
    lag_info = V.decorrelation_lag_s(X, t, max_lag_s=max_lag_s)
    lag_s = float(lag_info["lag_s"])
    sens = {}
    for r in (0.45, 0.55):
        try:
            sens[r] = float(V.decorrelation_lag_s(X, t, rho=r, max_lag_s=max_lag_s)["lag_s"])
        except V.Refused:
            sens[r] = float("nan")
    lag_swing = (max(sens.values()) / min(sens.values())
                 if sens and min(sens.values()) > 0 else None)

    order = np.argsort(t, kind="stable")
    ts, gs = t[order], np.asarray(g)[order]
    n = int(ts.size)
    lo = np.searchsorted(ts, ts - lag_s, side="left")
    hi = np.searchsorted(ts, ts + lag_s, side="right")
    cross = np.zeros(n, dtype=bool)
    for i in range(n):
        window = np.arange(lo[i], hi[i])
        window = window[window != i]
        if window.size and np.any(gs[window] != gs[i]):
            cross[i] = True
    frac_cross = float(cross.mean()) if n else 0.0

    uniq_g = np.unique(gs)
    if uniq_g.size >= 2:
        centers = np.array([ts[gs == gg].mean() for gg in uniq_g])
        gaps = np.diff(centers)
        frac_adj: Optional[float] = float((gaps < lag_s).mean())
    else:
        frac_adj = None

    return {"lag_s": lag_s, "censored": bool(lag_info["censored"]), "group_s": float(group_s),
            "lag_at_rho_045": sens[0.45], "lag_at_rho_055": sens[0.55],
            "lag_swing_x": lag_swing, "n_pairs_at_lag": lag_info["n_pairs_at_lag"],
            "sufficient": lag_s <= float(group_s),
            "frac_events_with_cross_group_neighbor": frac_cross,
            "frac_adjacent_groups_within_lag": frac_adj,
            "n_events": n, "n_groups": int(uniq_g.size), "lag_info": lag_info}


# --------------------------------------------------------------------------- block schemes

def native_group_blocks(t: np.ndarray, g: np.ndarray, *, lag_s: float,
                        group_s: float = GROUP_S,
                        lag_censored: bool = False) -> "V.Blocks":
    """Blocks = `train_sketch`'s own `int(utc // 3)` groups, untouched -- THE NAIVE SCHEME. It
    keeps one firing string from splitting across a fold (`g`'s own job) but does nothing about
    two DIFFERENT, adjacent groups landing on opposite sides of a fold; see `group_leakage`.

    Built by hand rather than through `hear.validate.make_blocks`: that function derives block
    BOUNDARIES from a roughly-regular row cadence (scene.csv's 1.024 s rows), and 228 sparse,
    bursty events have no such cadence -- reusing it here would silently re-derive different
    boundaries than `g` already provides. `alarm_reproducibility` only needs `Blocks`'s fields,
    not `make_blocks`'s boundary algorithm, so nothing about building it directly loses the
    guard/interleave/refusal machinery downstream.
    """
    g = np.asarray(g)
    row = np.arange(t.size)
    return V.Blocks(t=t, row=row, block=g.astype(np.int64), block_s=float(group_s),
                    lag_s=float(lag_s), guard_s=0.0, n_blocks=int(np.unique(g).size),
                    lag_censored=bool(lag_censored), n_in=int(t.size), dropped_unanchored=0,
                    dropped_short_block=0, dropped_guard=0)


def lag_merged_blocks(t: np.ndarray, g: np.ndarray, *, lag_s: float,
                      lag_censored: bool = False) -> "V.Blocks":
    """Blocks = `g`'s groups, MERGED whenever the next group's FIRST event is within the measured
    `lag_s` of the last event kept in the current block -- THE HONEST SCHEME, built to answer the
    question `group_leakage` raises rather than to look better. Every surviving block boundary is
    then at least one measured decorrelation lag from its neighbours: the same property
    `make_blocks`'s guard band buys for scene.csv rows, reached here by MERGING rather than
    eroding, because labelled clips cannot be trimmed the way a continuum of rows can -- there is
    no partial event to drop. On the real 228-event corpus this collapses 69 groups to 16.

    THE COMPARISON IS BETWEEN EVENTS, NOT BETWEEN GROUP CENTRES, which is what it was. The
    leakage is event-to-event and an event sits up to `group_s`/2 off its bucket's centre, so two
    blocks kept apart on a centre gap just over `lag_s` could have their nearest events up to 3 s
    closer than that -- the guarantee in the paragraph above was asserted rather than enforced.
    It held on this corpus (closest kept boundary 24.79 s against a 22.4 s lag) and does not hold
    in general. `hear.validate.separation_check` now measures the realised gap on every report,
    so the claim is checked wherever it is used rather than argued here.
    """
    g = np.asarray(g)
    uniq = np.unique(g)
    centers = np.array([t[g == gg].mean() for gg in uniq])
    first = np.array([t[g == gg].min() for gg in uniq])
    last = np.array([t[g == gg].max() for gg in uniq])
    order = np.argsort(centers, kind="stable")
    uniq, first, last = uniq[order], first[order], last[order]
    merged = np.zeros(uniq.size, dtype=np.int64)
    run_last = last[0]
    for i in range(1, uniq.size):
        if first[i] - run_last > lag_s:
            merged[i] = merged[i - 1] + 1
            run_last = last[i]
        else:
            merged[i] = merged[i - 1]
            run_last = max(run_last, last[i])
    remap = dict(zip(uniq.tolist(), merged.tolist()))
    block = np.array([remap[gg] for gg in g], dtype=np.int64)
    row = np.arange(t.size)
    return V.Blocks(t=t, row=row, block=block, block_s=float(lag_s), lag_s=float(lag_s),
                    guard_s=0.0, n_blocks=int(np.unique(block).size),
                    lag_censored=bool(lag_censored),
                    n_in=int(t.size), dropped_unanchored=0, dropped_short_block=0,
                    dropped_guard=0)


# --------------------------------------------------------------------------- the classifier pair

def _sketch_fit(C: float = DEFAULT_C) -> "V.FitFn":
    """A `hear.validate.FitFn` bound to a FIXED `C`. Deliberately not tuned per fit:
    `alarm_reproducibility` calls `fit(X_fit, y_fit)` with only the fit rows, never the group
    ids `train_sketch.nested_auc`'s inner CV needs to choose `C` honestly, and re-deriving that
    selection here would be its own, separate re-fit machinery layered on top of the question
    this module asks. `C=3.0` is the shipped `model_sketch.json`'s own chosen value, so this
    measures reproducibility of the representation and the split, not of a hyperparameter search
    this function has no way to run correctly.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def fit(Xf: np.ndarray, yf: Optional[np.ndarray]) -> Any:
        if yf is None:
            raise V.Refused("the sketch classifier is supervised; fit rows carry no labels")
        yf = np.asarray(yf)
        classes = np.unique(yf)
        if classes.size < 2:
            raise V.Refused("%d fit rows are all one class (%r); a classifier fit on them "
                            "distinguishes nothing" % (yf.size, classes.tolist()))
        m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000))
        m.fit(Xf, yf)
        return m
    return fit


def _sketch_score(model: Any, X: np.ndarray) -> np.ndarray:
    """`hear.validate.ScoreFn`: probability of the positive (shot) class."""
    return np.asarray(model.predict_proba(X)[:, 1], dtype=float)


def sketch_alarm_reproducibility(X: np.ndarray, y: np.ndarray, blocks: "V.Blocks", *,
                                 C: float = DEFAULT_C, parts: int = 3,
                                 min_blocks_part: int = 4, pctl: Optional[float] = None,
                                 allow_unseparated: bool = False,
                                 fit: Optional["V.FitFn"] = None,
                                 score: Optional["V.ScoreFn"] = None) -> Dict[str, Any]:
    """`hear.validate.alarm_reproducibility`, applied UNMODIFIED, to a supervised scorer.

    `pctl` defaults to `100 * (1 - prevalence)` with `prevalence = y.mean()` over every labelled
    event passed in (43.9% on the real 228-event corpus, so pctl=56.1) -- NOT
    `hear.validate`'s anomaly-tuned default of 99.5. An anomaly detector's `pctl` asks "what
    fraction of NORMAL should alarm"; a classifier has no normal class to calibrate against, so
    the natural question instead is "does each independent fit's own top-`(1-prevalence)`
    fraction of its probabilities land on the same held-out events."

    `fit`/`score` default to a fixed-`C` logistic pipeline (`_sketch_fit`/`_sketch_score`);
    passing both lets a caller (or a test) exercise this function's wiring around
    `alarm_reproducibility` -- the pctl derivation, the `d` bookkeeping below -- without paying
    for a real `sklearn` fit every time.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    if pctl is None:
        prevalence = float(y.mean())
        if not (0.0 < prevalence < 1.0):
            raise V.Refused("prevalence %.4f is not in (0, 1): %d of %d events are the "
                            "positive class" % (prevalence, int(y.sum()), y.size))
        pctl = 100.0 * (1.0 - prevalence)
    fit_fn = fit if fit is not None else _sketch_fit(C)
    score_fn = score if score is not None else _sketch_score
    rep = dict(V.alarm_reproducibility(X, blocks, fit=fit_fn, score=score_fn, pctl=float(pctl),
                                       y=y, parts=parts, min_blocks_part=min_blocks_part,
                                       allow_unseparated=allow_unseparated))
    rep["pctl"] = float(pctl)
    rep["d"] = int(X.shape[1])
    rep["n_a_over_d"] = rep["n_a"] / float(rep["d"])
    rep["n_b_over_d"] = rep["n_b"] / float(rep["d"])
    return rep


# --------------------------------------------------------------------------- report

def format_report(leak: Dict[str, Any], reps: Dict[str, Dict[str, Any]]) -> str:
    """Text. Mirrors `hear.validate.format_report`'s separation: `WARN` lines are text only --
    no return value, no exit code, no gate. `reps` maps a scheme name ("native", "lag_merged",
    ...) to `sketch_alarm_reproducibility`'s return.
    """
    L: List[str] = []
    L.append("corpus: %d events, %d groups, group_s=%.1fs, measured decorrelation lag %.1fs%s"
             % (leak["n_events"], leak["n_groups"], leak["group_s"], leak["lag_s"],
                " (CENSORED)" if leak["censored"] else ""))
    L.append("  %.1f%% of events have a different-group neighbour within the lag; "
             "%.1f%% of consecutive groups are themselves closer than it"
             % (100.0 * leak["frac_events_with_cross_group_neighbor"],
                100.0 * (leak["frac_adjacent_groups_within_lag"] or 0.0)))
    L.append("  lag at rho 0.45 / 0.55: %.1fs / %.1fs (%s swing), %s pairs at the crossing"
             % (leak["lag_at_rho_045"], leak["lag_at_rho_055"],
                "n/a" if leak["lag_swing_x"] is None else "%.2fx" % leak["lag_swing_x"],
                leak["n_pairs_at_lag"]))
    if not leak["sufficient"]:
        L.append("  WARN group_s=%.1fs is %.1fx SHORTER than the measured lag %.1fs: two "
                 "different groups this close are not independent"
                 % (leak["group_s"], leak["lag_s"] / leak["group_s"], leak["lag_s"]))
    for name, rep in reps.items():
        L.append("%s: %d blocks (%d/%d/%d a/b/holdout)%s, n_a=%d n_b=%d of d=%d (n/d %.2f / "
                 "%.2f), jaccard %.3f, rates %.1f%% / %.1f%% (design %.1f%%), pearson %s"
                 % (name, rep["blocks_a"] + rep["blocks_b"] + rep["blocks_holdout"],
                    rep["blocks_a"], rep["blocks_b"], rep["blocks_holdout"],
                    "" if rep["separated"] else " UNSEPARATED",
                    rep["n_a"], rep["n_b"], rep["d"], rep["n_a_over_d"], rep["n_b_over_d"],
                    rep["jaccard"], 100.0 * rep["rate_a"], 100.0 * rep["rate_b"],
                    100.0 * rep["designed"],
                    "n/a" if rep["pearson"] is None else "%.3f" % rep["pearson"]))
        L.append("  chance jaccard at those rates %s, excess %s; closest cross-block gap %s "
                 "against lag %.1fs"
                 % ("n/a" if rep["jaccard_chance"] is None else "%.3f" % rep["jaccard_chance"],
                    "n/a" if rep["jaccard_excess"] is None else "%+.3f" % rep["jaccard_excess"],
                    "n/a" if rep["min_cross_block_gap_s"] is None
                    else "%.2fs" % rep["min_cross_block_gap_s"], rep["lag_s"]))
        if not rep["separated"]:
            L.append("  WARN this split is NOT separated: its closest two events in different "
                     "blocks are %.2fs apart against a %.1fs lag, so its two 'independent' fits "
                     "share correlated events and every number on the line above is inflated "
                     "toward agreement by construction"
                     % (rep["min_cross_block_gap_s"] or 0.0, rep["lag_s"]))
        if rep["jaccard_excess"] is not None and rep["jaccard_excess"] < 0.1:
            L.append("  WARN jaccard %.3f is only %+.3f above chance at these rates: two "
                     "independent flaggers would score %.3f, so this is not agreement"
                     % (rep["jaccard"], rep["jaccard_excess"], rep["jaccard_chance"]))
        if rep["n_a_over_d"] < 1.0 or rep["n_b_over_d"] < 1.0:
            L.append("  WARN fewer fit rows than dimensions (n/d %.2f, %.2f): the classifier "
                     "is underdetermined before any split question is asked"
                     % (rep["n_a_over_d"], rep["n_b_over_d"]))
        if rep["designed"] > 0:
            dev = (abs(rep["rate_a"] - rep["designed"]) + abs(rep["rate_b"] - rep["designed"])
                  ) / (2.0 * rep["designed"])
            if dev > 0.5:
                L.append("  WARN mean deviation from design %.0f%%: this split's threshold "
                         "does not generalise" % (100.0 * dev))
        if rep["jaccard"] < 0.5:
            L.append("  WARN jaccard %.3f: which event alarms is mostly decided by which "
                     "blocks landed in the fit" % rep["jaccard"])
    return "\n".join(L)


# --------------------------------------------------------------------------- synthetic fallback

def synthetic_corpus(n_events: int = 228, n_groups: int = 69, d: int = 160,
                     prevalence: float = 0.44, seed: int = 0) -> Corpus:
    """A corpus with the REAL data's coarse shape (bursty group timing, comparable n and d) but
    every number INVENTED -- used ONLY when `~/analysis_20260905/label_items.json` is not on
    this box. `main()` prints SYNTHETIC on every line derived from this; none of the 22.4s /
    91.7% / 77.9% / Jaccard figures in the module docstring were measured on it.
    """
    rng = np.random.default_rng(seed)
    t0 = 1_788_000_000.0
    centers: List[float] = []
    cursor = t0
    while len(centers) < n_groups:
        cursor += float(rng.uniform(30.0, 400.0))
        for _ in range(int(rng.integers(1, 5))):
            if len(centers) >= n_groups:
                break
            centers.append(cursor)
            cursor += float(rng.uniform(3.0, 12.0))
    centers = np.array(centers[:n_groups])

    ids: List[str] = []
    t: List[float] = []
    g: List[int] = []
    y: List[int] = []
    per_group = max(1, n_events // n_groups)
    for c in centers:
        for _ in range(per_group):
            if len(ids) >= n_events:
                break
            ids.append("syn_%04d" % len(ids))
            t.append(float(c + rng.uniform(-1.0, 1.0)))
            g.append(int(c // GROUP_S))
            y.append(int(rng.random() < prevalence))
    cursor = float(centers[-1])
    while len(ids) < n_events:
        cursor += float(rng.uniform(1.0, 400.0))
        ids.append("syn_%04d" % len(ids))
        t.append(cursor)
        g.append(int(cursor // GROUP_S))
        y.append(int(rng.random() < prevalence))

    y_arr = np.array(y)
    mu = rng.normal(size=d) * 0.3      # a weak, genuine signal: not a pure-noise fixture
    X = rng.normal(size=(len(ids), d))
    X[y_arr == 1] += mu
    return Corpus(X=X, y=y_arr, g=np.array(g), t=np.array(t), ids=ids)


# --------------------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--items", default="~/analysis_20260905/label_items.json")
    ap.add_argument("--labels", default="~/analysis_20260905/labels/labels/*.json")
    ap.add_argument("--layout", default=TS.SK.LAYOUT_FIXED,
                    choices=[TS.SK.LAYOUT_NYQUIST, TS.SK.LAYOUT_FIXED])
    ap.add_argument("--C", type=float, default=DEFAULT_C)
    ap.add_argument("--parts", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0, help="synthetic-fallback seed only")
    a = ap.parse_args(argv)

    items_path = os.path.expanduser(a.items)
    if os.path.exists(items_path):
        corpus = load_sketch_corpus(a.items, a.labels, a.layout)
        print("loaded %d labelled events (%d shot / %d not) from %s"
             % (corpus.y.size, int(corpus.y.sum()), int((1 - corpus.y).sum()), items_path))
    else:
        print("%s not found -- REAL LABEL CORPUS UNAVAILABLE. Falling back to a SYNTHETIC "
             "corpus of the same coarse shape; every number this run prints is invented, not "
             "measured." % items_path, file=sys.stderr)
        corpus = synthetic_corpus(seed=a.seed)
        print("SYNTHETIC corpus: %d events, %d groups"
             % (corpus.y.size, int(np.unique(corpus.g).size)))

    leak = group_leakage(corpus.X, corpus.t, corpus.g)
    cens = bool(leak["censored"])
    blocks = {"native": native_group_blocks(corpus.t, corpus.g, lag_s=leak["lag_s"],
                                            lag_censored=cens),
             "lag_merged": lag_merged_blocks(corpus.t, corpus.g, lag_s=leak["lag_s"],
                                             lag_censored=cens)}
    reps: Dict[str, Dict[str, Any]] = {}
    for name, b in blocks.items():
        try:
            reps[name] = sketch_alarm_reproducibility(
                corpus.X, corpus.y, b, C=a.C, parts=a.parts,
                # The naive scheme IS an unseparated split -- that is what it is here to show --
                # so it says so out loud rather than being scored as if it were not.
                allow_unseparated=(name == "native"))
        except V.Refused as e:
            print("%s: REFUSED: %s" % (name, e))
    print(format_report(leak, reps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
