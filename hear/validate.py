#!/usr/bin/env python3
"""Split-honest holdout and reproducibility reports for any fit over the scene corpus.

A port of `holdout_calibration` / `holdout_envelope` / `alarm_reproducibility` from the hugbot
NPU audio trainer, re-dimensioned for dama-hear. Everything here REPORTS. Nothing gates a fit,
nothing raises because a diagnostic came out ugly, and no function returns a pass/fail. The only
exceptions are `Refused`, which means a number could not honestly be computed at all.

WHAT THE THREE REPORTS ANSWER, AND WHY THEY ARE THREE.
  - `holdout_calibration` asks whether the THRESHOLD means what it says. Fit on the older part,
    threshold at that fit's own `pctl`, count what fraction of the newer part exceeds. Designed
    rate is `(100-pctl)/100`.
  - `holdout_envelope` asks the same question at five cut points, because one cut point is not
    the rate. On the measured drain (see below) the same corpus reads 78.9% at one cut and 0.0%
    at another.
  - `alarm_reproducibility` asks the prior question: does WHICH row alarms depend on the audio,
    or on which blocks happened to land in the fit? A model can miss its designed rate purely
    because the corpus drifted and still be a fine detector at a re-derived threshold. A model
    whose two independent fits alarm on different rows cannot be repaired by moving any
    threshold.

WHAT WAS RE-DIMENSIONED, WHICH IS THE WHOLE REASON THIS IS A PORT AND NOT A COPY.

The hugbot corpus is a directory of ROTATIONS, roughly an hour of audio each, and both source
functions split on rotation boundaries. dama-hear's scene corpus has no rotations: it is one
growing file of 1.024 s rows (20 bands x 4 slices = 80 dims, ungated). Splitting it by record
index would put the same ten minutes on both sides of the split, and interleaving at row
granularity would make two supposedly independent fits share autocorrelated neighbours, which
inflates the Jaccard toward 1 and turns the check into one that cannot fail. So the split unit
here is a TIME BLOCK whose length is at least the corpus's own MEASURED decorrelation lag, and
`decorrelation_lag_s` measures that lag rather than hardcoding a number.

The failure the source was written against does NOT transfer, and the one that binds here is a
different one. Measured 2026-09-08 on ~/dama-hear-drain/20260907-2030/{nyquist,mach}/scene.csv:
  - CONDITIONING IS CLEAN. Rank 80/80, zero zero-variance dimensions, 0.00% exact zeros,
    lambda_min +0.186, condition number 1.5e4, and 0% of directions set by an eps floor. The
    hugbot fit was rank 654 of 1024 with 43.2% of its directions pinned by the floor, and its
    `EPS` carries pages of measurement sizing that floor. Here the covariance is invertible on
    its own and `mahalanobis_fit`'s eps is a numerical guard, not a fitted value.
  - AUTOCORRELATION BINDS INSTEAD. Lag-1 cosine 0.96 (hugbot 0.859), still 0.70 at lag 600 rows
    (~10 min). It does not decorrelate inside a 2 h sample. `decorrelation_lag_s` measures 853 s
    on that file, so under this module's own defaults (`lag_mult=3`) ONE BLOCK IS 42.6 MIN and a
    fortnight of one node is 473 blocks against the 3240 free parameters of an 80x80 covariance
    -- so a full-rank fit is still under-determined, for a completely different reason than
    hugbot's. (An earlier draft of this paragraph said "~2016 blocks", which is 14*24*6: a count
    of TEN-MINUTE units, i.e. the 600-row lag figure used as if it were the block length. 4.3x
    out, in the reassuring direction, inside the docstring arguing against exactly that error.)
    `alarm_reproducibility` prints `blocks_per_dim` AND `blocks_per_cov_param` so both ratios are
    on the record instead of being assumed.
  - DRIFT REPRODUCES THE SPLIT-DEPENDENCE, WORSE. Holdout exceedance across fracs
    0.5/0.6/0.7/0.8/0.9 was 78.9 / 5.8 / 2.7 / 0.0 / 0.0 % against a 0.50% design -- a 158x
    spread, against the 42x this machinery was written to expose on hugbot. The window was
    22:26->00:30, the evening transition, so the diurnal cycle IS the signal being measured.
    THOSE FIVE NUMBERS ARE THE MOTIVATION, NOT THIS MODULE'S OUTPUT, and the distinction matters
    because they were taken with a ROW-INDEX cut -- the split this module refuses. A 2.06 h file
    at an 853 s lag holds 3 blocks under the defaults and 6 at `--lag-mult 1.5`, so what this
    code returns on that file is a refusal or a 2-cut envelope; see `tools/validate_scene.py`,
    which prints what it actually produced. Re-deriving the 158x under the honest split would
    take the ~14 h capture the envelope needs for five distinct cuts.

UNANCHORED ROWS ARE NOT AN ERROR. 56.1% of mach's rows carry `utc_us == 0` because the node had
not yet locked PPS; `hear/pool.py` already handles that correctly (`anchored` flag, `ts_utc_s`
None, mel bytes in the dedup key). They cannot join a time-keyed split, so they are dropped and
COUNTED. Reading `utc_us` naively as a number instead puts those rows at 1970 and reports a
56-year span.

    from hear import validate as V
    X, t, rows = V.scene_xt(pool, node="nyquist")
    b = V.make_blocks(X, t)
    env = V.holdout_envelope(X, b, fit=V.mahalanobis_fit, score=V.mahalanobis_score)
    rep = V.alarm_reproducibility(X, b, fit=V.mahalanobis_fit, score=V.mahalanobis_score)
    print(V.format_report(env, rep, b))
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

FitFn = Callable[[np.ndarray, Optional[np.ndarray]], Any]
ScoreFn = Callable[[Any, np.ndarray], np.ndarray]

HOLDOUT_FRACS = (0.5, 0.6, 0.7, 0.8, 0.9)

#: Row pairs required before a lag's correlation estimate is used at all. Its own quantity: a
#: cosine averaged over fewer pairs than this is noise, and the sketch corpus made the point --
#: its lag-1 point came from 27 pairs and sat 0.0083 below rho, close enough that moving rho by
#: 0.02 moved the reported lag 172x. Deliberately NOT the same constant as `min_rows_per_block`
#: (rows a block needs to support a covariance) or `MIN_BLOCKS_*` (blocks a report needs); all
#: three were 8 in the first draft and none of them means the same thing as the others.
MIN_PAIRS_PER_LAG = 8

#: Multiple of the design rate at which `format_report` calls an exceedance out. A CHOSEN
#: constant, not a derived one: it is this repo's `median_closure_us > 4 * tol_us` idiom ("or the
#: test is vacuous") reused as a reporting threshold. It has no sampling theory behind it and
#: `holdout_envelope` returns `max_over_designed` so a caller can pick its own.
EXCEED_WARN_X = 4.0


class Refused(ValueError):
    """A number could not be computed honestly. NEVER raised because a diagnostic looked bad.

    The distinction is the point of the module. A holdout exceedance of 78.9% against a 0.50%
    design is a result and is returned; a corpus that cannot be cut into two separated halves at
    all has no exceedance to return, and the message names the shortfall.
    """


# --------------------------------------------------------------------------- input checks

def _as_xt(X, t) -> Tuple[np.ndarray, np.ndarray]:
    X = np.asarray(X, dtype=float)
    if X.ndim != 2:
        raise Refused("X must be [n, d]; got shape %r" % (X.shape,))
    t = np.asarray(t, dtype=float).ravel()
    if X.shape[0] == 0:
        raise Refused("X has 0 rows")
    if t.size != X.shape[0]:
        raise Refused("len(t)=%d does not match X.shape[0]=%d" % (t.size, X.shape[0]))
    bad = int((~np.isfinite(X)).sum())
    if bad:
        raise Refused("X carries %d non-finite values; a covariance over them is not a number"
                      % bad)
    return X, t


def _as_x(X) -> np.ndarray:
    """X alone, for the functions that take their time axis from an already-built `Blocks`."""
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] == 0:
        raise Refused("X must be a non-empty [n, d]; got shape %r" % (X.shape,))
    bad = int((~np.isfinite(X)).sum())
    if bad:
        raise Refused("X carries %d non-finite values; a covariance over them is not a number"
                      % bad)
    return X


def _anchored(t: np.ndarray) -> np.ndarray:
    """Anchored == a real wall clock. `pool.scene_xt` writes NaN for `ts_utc_s is None`, and a
    naive reader of the same rows writes 0.0 from `utc_us`; both are rejected here so the 1970
    reading cannot creep back in through either door."""
    return np.isfinite(t) & (t > 0.0)


# --------------------------------------------------------------------------- decorrelation

def decorrelation_lag_s(X, t, *, rho: float = 0.5, max_lag_s: Optional[float] = None,
                        n_lags: int = 48, min_pairs: int = MIN_PAIRS_PER_LAG) -> Dict[str, Any]:
    """Measure the lag at which rows stop looking like each other. Seconds, from the data.

    TWO CHANNELS, AND THE STATISTIC IS THEIR MAX. Rows are mean-removed across the corpus and for
    each log-spaced row lag k two numbers are computed over the pairs k apart:
      - `cos_dir`, the mean cosine between the two rows -- SHAPE, scale-free per row;
      - `acf_level`, the autocorrelation of the row NORM -- LEVEL, which cosine divides out.
    `cos = max(cos_dir, acf_level)`, and the lag is read off that. A single-channel cosine is the
    wrong quantity to size a block for a scorer that reads DISTANCE: a corpus whose per-row
    direction is random but whose level is an AR(1) with a 512 s time constant measures a 1.02 s
    cosine lag and a 0.997 level autocorrelation at lag 1, and blocks sized from the cosine alone
    come out 500x short. Neither channel is a guarantee for an arbitrary `ScoreFn` -- a scorer
    reading some third statistic can autocorrelate longer than both -- so `curve` carries both
    columns and `channel` names the one that held the statistic above `rho` longest.

    RHO IS 0.5, NOT 1/e. On the measured drain the cosine is 0.96 at lag 1 and still 0.70 at 600
    rows (~10 min), so the 1/e crossing does not occur anywhere inside a 2 h sample. An estimator
    that has to extrapolate to answer is worse than one that reports it cannot: if the curve
    never crosses `rho` inside `max_lag_s` the result carries `lag_s = max_lag_s` and
    `censored=True`, and `make_blocks` refuses to derive a block length from it.

    `max_lag_s` defaults to span/4. A lag estimated from one realisation of itself is not a
    measurement -- at lag = span there is exactly one pair.

    THE CROSSING MUST BE SUSTAINED, not merely first. The returned lag is the grid point after
    the LAST measured lag at or above `rho`, so a single dip below `rho` on a curve that comes
    back up is not read as decorrelation. That was not a hypothetical: on the sketch corpus the
    lag-1 point sat at 0.5083 from 27 pairs and lag 3 was back at 0.6977, so a first-crossing
    rule would have answered from whichever side of `rho` a 27-pair estimate happened to land --
    moving `rho` from 0.50 to 0.52 moved the answer 172x. `cos_at_lag` and `n_pairs_at_lag` are
    returned so the margin at the crossing is visible instead of assumed.

    The lag is not interpolated back toward the crossing either. The log grid at n_lags=48 steps
    ~17% at a time, so the grid alone can only place the answer LONG; interpolating would
    systematically shorten it, and short is the dangerous direction -- a block shorter than the
    true lag does not make the diagnostics noisy, it makes them pass. Noise in the curve can
    still land the estimate under the true crossing, which is what `make_blocks`'s `lag_mult`
    margin is for.
    """
    X, t = _as_xt(X, t)
    ok = _anchored(t)
    if int(ok.sum()) < 3:
        raise Refused("decorrelation needs >= 3 anchored rows; %d of %d are anchored"
                      % (int(ok.sum()), t.size))
    order = np.argsort(t[ok], kind="stable")
    ts = t[ok][order]
    Z = X[ok][order]
    span = float(ts[-1] - ts[0])
    if span <= 0.0:
        raise Refused("all %d anchored rows share one timestamp; span is 0 s" % ts.size)
    dts = np.diff(ts)
    dt = float(np.median(dts[dts > 0])) if np.any(dts > 0) else 0.0
    if dt <= 0.0:
        raise Refused("median row interval is 0 s over %d anchored rows" % ts.size)

    cap = float(max_lag_s) if max_lag_s is not None else span / 4.0
    max_k = int(cap / dt)
    if max_k < 1:
        raise Refused("max_lag_s=%.3fs is under one row interval (%.3fs)" % (cap, dt))
    max_k = min(max_k, ts.size - 2)
    if max_k < 1:
        raise Refused("only %d anchored rows: no lag can be measured" % ts.size)

    Z = Z - Z.mean(axis=0)
    norms = np.linalg.norm(Z, axis=1)
    lev = norms - norms.mean()
    var_lev = float(np.mean(lev * lev))

    lags = np.unique(np.round(np.geomspace(1, max_k, num=max(2, n_lags))).astype(int))
    curve: List[Dict[str, Any]] = []
    for k in lags:
        k = int(k)
        i = np.arange(ts.size - k)
        sep = ts[i + k] - ts[i]
        good = (norms[i] > 0) & (norms[i + k] > 0) & \
               (sep >= 0.5 * k * dt) & (sep <= 1.5 * k * dt)
        n_pairs = int(good.sum())
        if n_pairs < int(min_pairs):
            continue
        a, b = i[good], i[good] + k
        cos_dir = float(np.mean(np.einsum("ij,ij->i", Z[a], Z[b]) / (norms[a] * norms[b])))
        acf_lev = float(np.mean(lev[a] * lev[b]) / var_lev) if var_lev > 0 else 0.0
        curve.append({"lag_rows": k, "lag_s": float(np.median(sep[good])),
                      "cos": max(cos_dir, acf_lev), "cos_dir": cos_dir, "acf_level": acf_lev,
                      "n_pairs": n_pairs})
    if not curve:
        raise Refused("no lag had >= %d usable row pairs inside max_lag_s=%.1fs"
                      % (int(min_pairs), cap))

    above = [j for j, r in enumerate(curve) if r["cos"] >= rho]
    if not above:
        hit: Optional[Dict[str, Any]] = curve[0]
    elif above[-1] >= len(curve) - 1:
        hit = None
    else:
        hit = curve[above[-1] + 1]
    last_above = curve[above[-1]] if above else None

    censored = hit is None
    return {"lag_s": (cap if censored else hit["lag_s"]),
            "lag_rows": (max_k if censored else hit["lag_rows"]),
            "censored": censored, "rho": float(rho), "dt_s": dt, "span_s": span,
            "max_lag_s": cap, "n_anchored": int(ts.size), "curve": curve,
            "cos_lag1": curve[0]["cos"], "cos_max_lag": curve[-1]["cos"],
            "cos_dir_lag1": curve[0]["cos_dir"], "acf_level_lag1": curve[0]["acf_level"],
            "cos_at_lag": (None if censored else hit["cos"]),
            "n_pairs_at_lag": (None if censored else hit["n_pairs"]),
            "channel": (None if last_above is None else
                        ("level" if last_above["acf_level"] >= last_above["cos_dir"]
                         else "direction")),
            "min_pairs": int(min_pairs)}


# --------------------------------------------------------------------------- blocks

@dataclass(frozen=True, eq=False)
class Blocks:
    """A time-block partition of the anchored rows, plus the full row accounting.

    `eq=False` because the fields are ndarrays and the generated __eq__ would return an array
    whose truth value is ambiguous -- comparing two Blocks would raise instead of answering.

    CHECKED AT CONSTRUCTION, not only in `make_blocks`. `alarm_reproducibility` and
    `holdout_calibration` take a `Blocks` and nothing forces it to have come from `make_blocks`
    -- `modules/supersonic/validate_sketch.py` builds one by hand for a legitimate reason, and a
    hand-built one carrying `n_in=1, dropped_unanchored=-500, n_blocks=999` was accepted and
    scored without complaint. Every refusal the split machinery advertises lived in `make_blocks`
    and none of it re-checked the object, so these move here:
        n_in == len(row) + dropped_unanchored + dropped_short_block + dropped_guard
        len(t) == len(row) == len(block);  n_blocks == unique(block).size
        block_s > 0;  lag_s > 0;  0 <= guard_s < block_s;  no negative counters
    The one property that CANNOT be checked here is separation between blocks, because it depends
    on the times and is what `min_cross_block_gap_s` measures -- see `separation_check`.
    """
    t: np.ndarray
    row: np.ndarray
    block: np.ndarray
    block_s: float
    lag_s: float
    guard_s: float
    n_blocks: int
    lag_censored: bool
    n_in: int
    dropped_unanchored: int
    dropped_short_block: int
    dropped_guard: int

    def __post_init__(self) -> None:
        n = int(np.asarray(self.row).size)
        if not (np.asarray(self.t).size == n == np.asarray(self.block).size):
            raise Refused("Blocks: t/row/block are %d/%d/%d long; they index the same rows"
                          % (np.asarray(self.t).size, n, np.asarray(self.block).size))
        for name in ("n_in", "dropped_unanchored", "dropped_short_block", "dropped_guard"):
            if int(getattr(self, name)) < 0:
                raise Refused("Blocks: %s=%d is negative" % (name, int(getattr(self, name))))
        acc = n + self.dropped_unanchored + self.dropped_short_block + self.dropped_guard
        if int(self.n_in) != acc:
            raise Refused("Blocks: row accounting does not close: n_in=%d against %d kept + %d "
                          "unanchored + %d short block + %d guard = %d"
                          % (self.n_in, n, self.dropped_unanchored, self.dropped_short_block,
                             self.dropped_guard, acc))
        seen = int(np.unique(self.block).size)
        if int(self.n_blocks) != seen:
            raise Refused("Blocks: n_blocks=%d against %d distinct block ids"
                          % (self.n_blocks, seen))
        if not (float(self.block_s) > 0.0 and float(self.lag_s) > 0.0):
            raise Refused("Blocks: block_s=%.6g and lag_s=%.6g must both be > 0"
                          % (self.block_s, self.lag_s))
        if not (0.0 <= float(self.guard_s) < float(self.block_s)):
            raise Refused("Blocks: guard_s=%.6g must be in [0, block_s=%.6g)"
                          % (self.guard_s, self.block_s))

    @property
    def anchored_frac(self) -> float:
        return (self.n_in - self.dropped_unanchored) / self.n_in if self.n_in else 0.0

    @property
    def ids(self) -> np.ndarray:
        """Distinct block ids, in time order."""
        return np.unique(self.block)

    @property
    def min_cross_block_gap_s(self) -> Optional[float]:
        """Smallest wall-clock gap between two kept rows in DIFFERENT blocks. None below 2 blocks.

        This is the realised separation, measured, as against `guard_s`, which is the separation
        `make_blocks` intended. They are the same number only for a partition `make_blocks`
        actually built: a hand-built `Blocks` can assert any `guard_s` it likes, and a scheme that
        merges groups on their CENTRES can leave two events either side of a kept boundary closer
        than the lag even though the centres are further apart than it.

        The minimum is always attained by a time-adjacent pair: if two rows of different blocks
        are k apart in time order, the block id changes at some adjacent step between them, and
        that step's gap is no larger.
        """
        t = np.asarray(self.t, dtype=float)
        if t.size < 2 or self.n_blocks < 2:
            return None
        o = np.argsort(t, kind="stable")
        ts, bs = t[o], np.asarray(self.block)[o]
        d = np.diff(ts)
        cross = bs[1:] != bs[:-1]
        return float(d[cross].min()) if np.any(cross) else None


def make_blocks(X, t, *, block_s: Optional[float] = None, lag_s: Optional[float] = None,
                lag_mult: float = 3.0, guard: bool = True, max_lag_s: Optional[float] = None,
                min_rows_per_block: int = 8) -> Blocks:
    """Partition anchored rows into half-open time blocks, eroded by a guard band.

    `block_id = floor((t - t0) / block_s)`. Gaps in the corpus produce ABSENT ids rather than
    stretched blocks, so a node that was offline for an hour does not silently glue the rows
    either side of the outage into one block.

    THE GUARD BAND. Adjacent blocks touch: two rows either side of a boundary are seconds apart
    even though the blocks land in different parts of a split, and that alone would smuggle
    autocorrelated neighbours across every split this module makes. So each block is ERODED --
    rows within `guard_s = lag_s` of the block's start are dropped and counted in
    `dropped_guard`. Any two surviving rows in different blocks are then >= `lag_s` apart by
    construction. At `lag_mult=3` the guard costs a third of the rows. That is what the
    separation costs, and it is reported rather than hidden.

    THE BLOCK LENGTH. Default `block_s = lag_mult * lag_s` with `lag_mult=3.0`, `lag_s` measured
    by `decorrelation_lag_s` unless passed. The margin is the point, and it is this repo's own
    idiom: `median_closure_us > 4 * tol_us`, "or the test is vacuous". Both numbers ride in the
    returned `Blocks` so the margin is auditable rather than folklore.

    A BLOCK SHORTER THAN THE MEASURED LAG IS A REFUSAL, NOT A WARNING. A too-short block does not
    make the diagnostics noisy -- it makes them PASS. Jaccard climbs toward 1 and holdout
    exceedance collapses onto the design rate, so the wrong answer is the reassuring one, printed
    next to a warning nobody reads. This is exactly `modules/supersonic/train_sketch.py` grouping
    its CV folds by `int(utc // 3)`: correct at 3 s for a firing string, wrong by ~200x for scene
    rows whose measured decorrelation is ~10 min. A constant copied between subsystems keeps its
    number and loses its units-of-meaning.

    A CENSORED LAG CANNOT SIZE A BLOCK EITHER. If the cosine never fell below `rho` inside the
    sample, `block_s` must be passed explicitly and must be >= the censoring bound -- which is
    the drain's own condition, where the cosine was still 0.70 at 10 min. `lag_censored` rides in
    the returned `Blocks` either way, so a split built on a bound rather than a crossing says so.

    `max_lag_s` is forwarded to `decorrelation_lag_s`; a caller who already knows how far their
    corpus is worth searching should not have to reimplement the measurement to say so.

    `min_rows_per_block=8` IS NOT DIMENSION-AWARE AND CANNOT BE MADE SO HERE -- `make_blocks`
    never sees the fit. Eight rows a block times `min_blocks_fit=4` is 32 rows for an 80-dim
    covariance, which is rank-deficient by a factor of two and a half. What guards that is
    downstream, in `_resolution_check` (can these row counts represent the rate at all) and in
    the `n_fit_over_d` and `eps_floor_frac` figures every report now carries. The 8 here is the
    smallest block that can hold a spread of rows at all; it is not the same 8 as
    `MIN_PAIRS_PER_LAG`, and neither is a claim about sufficiency.
    """
    X, t = _as_xt(X, t)
    n_in = int(X.shape[0])
    ok = _anchored(t)
    n_anch = int(ok.sum())
    dropped_unanchored = n_in - n_anch
    if n_anch == 0:
        raise Refused("no anchored rows: 0 of %d carry a wall clock (utc_us == 0 / ts_utc_s "
                      "None). 56.1%% of mach is like this and it is not a bug -- but a "
                      "time-keyed split needs at least two anchored times" % n_in)
    if np.unique(t[ok]).size < 2:
        raise Refused("only %d distinct anchored time(s) among %d anchored rows of %d: no time "
                      "split exists" % (int(np.unique(t[ok]).size), n_anch, n_in))

    lag_censored = False
    lag_info: Optional[Dict[str, Any]] = None
    if lag_s is None:
        lag_info = decorrelation_lag_s(X, t, max_lag_s=max_lag_s)
        lag_s = float(lag_info["lag_s"])
        lag_censored = bool(lag_info["censored"])
    lag_s = float(lag_s)
    if lag_s <= 0:
        raise Refused("lag_s must be > 0; got %.6f" % lag_s)

    if block_s is None:
        if lag_censored:
            raise Refused(
                "cannot derive block_s from a CENSORED decorrelation lag: the cosine never fell "
                "below rho=%.2f inside max_lag_s=%.1fs (still %.2f there, %.2f at lag 1 row). "
                "Pass block_s >= %.1f explicitly, or measure over a longer span"
                % (lag_info["rho"], lag_info["max_lag_s"], lag_info["cos_max_lag"],
                   lag_info["cos_lag1"], lag_s))
        block_s = float(lag_mult) * lag_s
    block_s = float(block_s)
    if block_s < lag_s:
        detail = ""
        if lag_info is not None:
            detail = " (cosine %.2f at lag 1 row, %.2f at %d rows)" % (
                lag_info["cos_lag1"], lag_info["cos_max_lag"], lag_info["curve"][-1]["lag_rows"])
        raise Refused("block_s=%.1fs is shorter than the measured decorrelation lag %.1fs%s; "
                      "pass block_s >= %.1f or measure again" % (block_s, lag_s, detail, lag_s))

    guard_s = lag_s if guard else 0.0
    if guard_s >= block_s:
        raise Refused("the guard band (%.1fs, one decorrelation lag) erodes the whole "
                      "%.1fs block, leaving no rows anywhere; block_s must exceed lag_s, "
                      "which is what lag_mult=%.1f is for" % (guard_s, block_s, lag_mult))

    idx = np.nonzero(ok)[0]
    ta = t[idx]
    order = np.argsort(ta, kind="stable")
    idx, ta = idx[order], ta[order]
    t0 = float(ta[0])
    bid = np.floor((ta - t0) / block_s).astype(np.int64)

    keep_guard = (ta - (t0 + bid * block_s)) >= guard_s
    dropped_guard = int((~keep_guard).sum())
    idx, ta, bid = idx[keep_guard], ta[keep_guard], bid[keep_guard]

    uniq, counts = np.unique(bid, return_counts=True)
    fat = uniq[counts >= int(min_rows_per_block)]
    keep_block = np.isin(bid, fat)
    dropped_short_block = int((~keep_block).sum())
    idx, ta, bid = idx[keep_block], ta[keep_block], bid[keep_block]

    n_blocks = int(np.unique(bid).size)
    if n_blocks < 2:
        span = float(ta[-1] - ta[0]) if ta.size else 0.0
        raise Refused("every kept row landed in %d block(s): anchored span %.1fs against "
                      "block_s=%.1fs (%d rows kept of %d, guard dropped %d, short blocks %d)"
                      % (n_blocks, span, block_s, ta.size, n_in, dropped_guard,
                         dropped_short_block))

    b = Blocks(t=ta, row=idx, block=bid, block_s=block_s, lag_s=lag_s, guard_s=guard_s,
               n_blocks=n_blocks, lag_censored=lag_censored, n_in=n_in,
               dropped_unanchored=dropped_unanchored,
               dropped_short_block=dropped_short_block, dropped_guard=dropped_guard)
    return b


def separation_check(blocks: Blocks, *, allow_unseparated: bool = False) -> Dict[str, Any]:
    """Are two rows in different blocks actually a decorrelation lag apart? MEASURED, then gated.

    THE GAP IS MEASURED, NOT DECLARED. It comes out of the times (`min_cross_block_gap_s`) and
    not out of `guard_s`, so a hand-built partition cannot assert separation it does not have --
    which is the whole hole: `block_s=3.0` against `lag_s=22.4` with `guard_s=0.0` was accepted
    and scored. The lag it is compared against is still the one the `Blocks` DECLARES, because a
    lag re-derived from the kept rows would be measured on a periodically-eroded sample whose
    row interval no longer matches its wall clock; a partition that understates its corpus's lag
    is beyond what this check can see, and `make_blocks` is where that number is measured.

    `make_blocks` buys this with its guard band and its `block_s >= lag_s` refusal, and every
    argument the two reports make rests on it -- "what makes interleaving legitimate here rather
    than self-defeating is the guard band". But neither report was built from `make_blocks`; both
    take a `Blocks`, and every refusal that guarantees the separation lived in the builder. A
    hand-built partition could therefore ship `block_s=3.0` against `lag_s=22.4` with `guard_s=0`
    -- the exact split `make_blocks` refuses by name -- and be scored without objection.

    So the reports call this instead of trusting `guard_s`, and it reads the realised gap out of
    the times. `allow_unseparated=True` is the deliberate escape, for a caller measuring what the
    naive split reports (`modules/supersonic/validate_sketch.py`'s `native` scheme is exactly
    that, and `tests/test_validate.py`'s row-index mutant is the same thing as a control). It is
    not a silent one: `separated=False` rides in the report and `format_report` says so.
    """
    gap = blocks.min_cross_block_gap_s
    lag_ref = float(blocks.lag_s)
    ok = gap is not None and gap >= lag_ref * (1.0 - 1e-9)
    if not ok and not allow_unseparated:
        raise Refused(
            "blocks are not separated: the closest two rows in different blocks are %s apart "
            "against this split's own decorrelation lag of %.3fs (block_s=%.3fs, guard_s=%.3fs)."
            " Two 'independent' fits over it share autocorrelated neighbours, which drives the "
            "Jaccard toward 1 and the holdout exceedance onto the design rate -- the wrong "
            "answer is the reassuring one. Build the split with make_blocks, or pass "
            "allow_unseparated=True to measure what the naive split reports."
            % ("nothing (one block)" if gap is None else "%.3fs" % gap, lag_ref,
               blocks.block_s, blocks.guard_s))
    return {"min_cross_block_gap_s": gap, "separated": bool(ok),
            "lag_required_s": lag_ref, "unseparated_allowed": bool(not ok)}


# --------------------------------------------------------------------------- holdout

#: Conditioning fields a dict-returning `FitFn` may expose. `mahalanobis_fit` documents
#: `eps_floor_frac` as the thing that "says so" when a corpus starts leaning on the floor -- it
#: said so to nobody, because the model built here was scored and dropped and no report path ever
#: read it. Every report now carries whichever of these its `FitFn` returned, measured on the
#: rows actually fitted rather than on the whole corpus, which is the only place a split part can
#: be seen to be rank-deficient.
_DIAG_KEYS = ("eps_floor_frac", "cond", "cond_raw", "lam_min", "floored", "alpha")


def _diag(model: Any) -> Dict[str, Any]:
    if not isinstance(model, dict):
        return {}
    return {k: model[k] for k in _DIAG_KEYS if k in model}


def _fit_and_flag(X: np.ndarray, rows_fit: np.ndarray, rows_score: np.ndarray,
                  fit: FitFn, score: ScoreFn, pctl: float,
                  y: Optional[np.ndarray]) -> Tuple[float, np.ndarray, Dict[str, Any]]:
    yf = None if y is None else np.asarray(y)[rows_fit]
    model = fit(X[rows_fit], yf)
    s_fit = np.asarray(score(model, X[rows_fit]), dtype=float).ravel()
    if s_fit.size != rows_fit.size:
        raise Refused("score() returned %d values for %d fit rows" % (s_fit.size, rows_fit.size))
    thr = float(np.percentile(s_fit, pctl))
    s_out = np.asarray(score(model, X[rows_score]), dtype=float).ravel()
    if s_out.size != rows_score.size:
        raise Refused("score() returned %d values for %d scored rows"
                      % (s_out.size, rows_score.size))
    return thr, s_out, _diag(model)


def _resolution_check(n_fit: int, n_holdout: int, pctl: float, where: str) -> Dict[str, Any]:
    """Can the two row counts even represent the rate `pctl` names? Arithmetic, not judgement.

    `pctl=99.5` came over from hugbot intact and its two companion floors -- `min_fit = DIM`
    records and `min_holdout = 100` records -- did not; the port's floors are in BLOCKS, and
    nothing anywhere related the percentile to a row count. Two things then stop meaning what
    they say:
      - `np.percentile(s_fit, 99.5)` on fewer than 200 fit rows lies inside the gap between the
        top two order statistics, i.e. it IS the sample maximum. "99.5% of normal is below it" is
        then untestable, not merely imprecise.
      - a holdout of 16 rows can only report 0.00% or >= 6.25%, so against a 0.50% design a
        perfectly calibrated model reads exactly 0.00% with probability 0.92, and the envelope
        then prints `spread n/a (best cut exceeded nothing)`.
    Both are refusals rather than warnings for the module's usual reason: the degenerate reading
    is the flattering one.
    """
    tail = (100.0 - float(pctl)) / 100.0
    if tail <= 0.0:
        raise Refused("pctl=%.4f leaves no tail: (100-pctl)/100 = %.6g" % (pctl, tail))
    exp_fit = float(n_fit) * tail
    exp_hold = float(n_holdout) * tail
    if exp_fit < 1.0:
        raise Refused("%s: %d fit rows cannot carry a pctl=%.2f threshold -- its tail is %.2f "
                      "rows, so np.percentile returns the sample MAXIMUM and 'pctl of normal is "
                      "below it' is untestable. Needs >= %d fit rows at this pctl"
                      % (where, n_fit, pctl, exp_fit, int(math.ceil(1.0 / tail))))
    if exp_hold < 1.0:
        raise Refused("%s: %d holdout rows cannot measure a %.3f%% rate -- a perfectly calibrated "
                      "model is expected to alarm on %.2f of them, and the finest non-zero rate "
                      "representable is %.3f%% (%.1fx the design). Needs >= %d holdout rows"
                      % (where, n_holdout, 100.0 * tail, exp_hold, 100.0 / max(1, n_holdout),
                         (1.0 / max(1, n_holdout)) / tail, int(math.ceil(1.0 / tail))))
    return {"expected_alarms_fit": exp_fit, "expected_alarms_holdout": exp_hold,
            "holdout_resolution": 1.0 / max(1, n_holdout)}


def holdout_calibration(X, blocks: Blocks, *, fit: FitFn, score: ScoreFn, pctl: float = 99.5,
                        y=None, frac: float = 0.8, min_blocks_fit: int = 4,
                        min_blocks_holdout: int = 2,
                        allow_unseparated: bool = False) -> Dict[str, Any]:
    """Fit on the OLDEST blocks, threshold at that fit's own `pctl`, count what the newest exceeds.

    The threshold ships with a stated meaning -- "pctl of normal is below it", so
    `(100-pctl)/100` of normal should alarm. That is a percentile of the SAME rows the model was
    fitted on, and it is the number every downstream reader takes at face value. Out of sample it
    is not that number: on the nyquist drain (80 dims, 1.024 s rows, 22:26->00:30) a row-index
    cut read 0.0% against a 0.50% design at frac=0.8 and 78.9% at frac=0.5 -- same corpus, same
    model. Under the block split this function insists on, that file supports at most two cut
    points; the motivating spread is quoted in the module docstring with its provenance.

    Two arithmetic refusals guard the result's meaning, both in `_resolution_check`: a fit part
    too small to place `pctl` anywhere but its own maximum, and a holdout too small to represent
    the design rate at all.

    READ THE RESULT AS ONE DRAW, NOT AS THE RATE -- see `holdout_envelope`, which is the reason
    this function is rarely the one to call.

    THE CUT IS AT A BLOCK BOUNDARY. Blocks are ordered by time, cumulative kept-row count is
    walked, and the cut falls at the FIRST block boundary where that count crosses
    `frac * total`. So `frac` keeps its row-count meaning while the unit of the split stays a
    block: a row-index cut would put the same ten minutes on both sides, which is the failure
    this whole module is re-dimensioned against.

    Not a gate: the model fitted here is thrown away, and nothing about the return value is a
    pass or a fail.
    """
    X = _as_x(X)
    if blocks.row.size and int(blocks.row.max()) >= X.shape[0]:
        raise Refused("blocks index row %d of an X with %d rows: they are not the same corpus"
                      % (int(blocks.row.max()), X.shape[0]))
    if not (0.0 < float(frac) < 1.0):
        raise Refused("frac must be in (0, 1); got %r" % (frac,))
    sep = separation_check(blocks, allow_unseparated=allow_unseparated)
    ids = blocks.ids
    if ids.size < int(min_blocks_fit) + int(min_blocks_holdout):
        raise Refused("%d blocks available, %d required (min_blocks_fit=%d + "
                      "min_blocks_holdout=%d) at block_s=%.1fs"
                      % (ids.size, int(min_blocks_fit) + int(min_blocks_holdout),
                         min_blocks_fit, min_blocks_holdout, blocks.block_s))

    counts = np.array([int((blocks.block == b).sum()) for b in ids])
    total = int(counts.sum())
    cum = np.cumsum(counts)
    cut = int(np.searchsorted(cum, float(frac) * total, side="left")) + 1
    cut = max(1, min(cut, ids.size - 1))
    ids_fit, ids_hold = ids[:cut], ids[cut:]
    if ids_fit.size < int(min_blocks_fit) or ids_hold.size < int(min_blocks_holdout):
        raise Refused("frac=%.2f cuts %d blocks into %d fit / %d holdout; %d / %d required"
                      % (frac, ids.size, ids_fit.size, ids_hold.size,
                         min_blocks_fit, min_blocks_holdout))

    m_fit = np.isin(blocks.block, ids_fit)
    rows_fit, rows_hold = blocks.row[m_fit], blocks.row[~m_fit]
    res = _resolution_check(int(rows_fit.size), int(rows_hold.size), pctl,
                            "frac=%.2f" % float(frac))
    thr, s_hold, diag = _fit_and_flag(X, rows_fit, rows_hold, fit, score, pctl, y)
    out = {"frac": float(frac), "n_fit": int(rows_fit.size), "n_holdout": int(rows_hold.size),
           "blocks_fit": int(ids_fit.size), "blocks_holdout": int(ids_hold.size),
           "threshold": thr, "exceed": float((s_hold > thr).mean()),
           "designed": (100.0 - float(pctl)) / 100.0,
           "n_fit_over_d": int(rows_fit.size) / float(X.shape[1]),
           "fit_diag": diag,
           "block_s": blocks.block_s, "lag_s": blocks.lag_s}
    out.update(res)
    out.update(sep)
    return out


def holdout_envelope(X, blocks: Blocks, *, fracs: Sequence[float] = HOLDOUT_FRACS,
                     **kw) -> Dict[str, Any]:
    """`holdout_calibration` at several cut points, because ONE cut point is not the rate.

    Measured 2026-09-08 on the nyquist drain, 80 dims, against a 0.50% design:
    78.9 / 5.8 / 2.7 / 0.0 / 0.0 % at fracs 0.5 / 0.6 / 0.7 / 0.8 / 0.9 -- a 158x spread, worse
    than the 42x this machinery was written to expose on hugbot. A reader handed the frac=0.8
    figure alone would conclude the threshold means exactly what it says. It means that at one
    cut and is 158x out at another. The window was 22:26->00:30, so what is being measured is the
    evening transition: the diurnal cycle is the signal, not a fault.

    The fracs stay `(0.5, 0.6, 0.7, 0.8, 0.9)` -- generic cut points, and the drain measurement
    above was taken at exactly these, so the port stays comparable to the number it was
    re-dimensioned against.

    `spread_x` is max/min, and is None -- with `spread_undefined_because` saying which -- in two
    cases. The first is `min_is_zero`: 0.0 is a real rate, `inf` is not a spread, and a stored
    `inf` reads downstream as a bug. The second is A SPREAD OF ONE SPLIT AGAINST ITSELF, which is
    the version of this that cannot fail. `min_blocks_fit=4` + `min_blocks_holdout=2` is a floor
    sized for `holdout_calibration`, which makes ONE cut, and at exactly 6 blocks every surviving
    frac is clamped to the same cut: `spread_x` was then 1.0000x by arithmetic, on any data
    whatsoever, and 1.0x is the most reassuring value the metric can take. Six blocks is not a
    corner -- it is what `tools/validate_scene.py` tells the operator to capture for, and it is
    the real nyquist drain at `--lag-mult 1.5`. Five DISTINCT cuts first exist at 20 blocks.
    `n_distinct_splits` is returned so the collapse is visible in the numbers and not only in a
    caller's note.

    A per-frac `Refused` is CAUGHT and recorded in `refused` rather than dropped, because a cut
    that could not be made is information about the corpus. The envelope re-raises only if no
    draw survived at all. It reports; sizing an alarm budget off `max` is the caller's job.
    """
    draws: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for f in fracs:
        try:
            draws.append(holdout_calibration(X, blocks, frac=float(f), **kw))
        except Refused as e:
            refused.append({"frac": float(f), "reason": str(e)})
    if not draws:
        raise Refused("no cut point survived: " + "; ".join(
            "frac=%.2f: %s" % (r["frac"], r["reason"]) for r in refused))

    ex = sorted(d["exceed"] for d in draws)
    lo, hi = ex[0], ex[-1]
    designed = draws[0]["designed"]
    n_distinct = len({(d["blocks_fit"], d["n_fit"]) for d in draws})
    if n_distinct < 2:
        spread: Optional[float] = None
        why = ("%d cut point(s) produced 1 distinct split" % len(draws))
    elif lo <= 0.0:
        spread, why = None, "best cut exceeded nothing"
    else:
        spread, why = hi / lo, None
    return {"draws": draws, "refused": refused,
            "min": lo, "median": float(np.median(ex)), "max": hi, "designed": designed,
            "max_over_designed": (hi / designed if designed > 0 else None),
            "spread_x": spread, "spread_undefined_because": why,
            "min_is_zero": bool(lo <= 0.0), "n_distinct_splits": n_distinct,
            "n_fit_over_d": min(d["n_fit_over_d"] for d in draws),
            "max_eps_floor_frac": max((d["fit_diag"].get("eps_floor_frac", 0.0) for d in draws),
                                      default=0.0),
            "max_cond": max((d["fit_diag"].get("cond", 0.0) for d in draws), default=0.0),
            "separated": all(bool(d["separated"]) for d in draws),
            "block_s": blocks.block_s, "lag_s": blocks.lag_s, "n_blocks": blocks.n_blocks}


# --------------------------------------------------------------------------- reproducibility

def alarm_reproducibility(X, blocks: Blocks, *, fit: FitFn, score: ScoreFn, pctl: float = 99.5,
                          y=None, parts: int = 3, min_blocks_part: int = 4,
                          allow_unseparated: bool = False) -> Dict[str, Any]:
    """Do two independent fits of the SAME corpus alarm on the same rows? Jaccard, or a refusal.

    This is separate from every calibration figure above and it is the prior question.
    `holdout_calibration` asks whether the threshold means 0.5%; a model can fail that purely
    because the corpus drifted and still be a good detector at a re-derived threshold. This asks
    whether WHICH row alarms is a property of the audio at all -- and a model that fails it
    cannot be repaired by moving the threshold, because there is no threshold at which its alarms
    mean anything. On hugbot: two fits of one corpus flagged 5.47% and 1.13% of the same held-out
    rows, overlapping at Jaccard 0.155.

    Blocks in time order are dealt round-robin, `part = rank % parts`. Parts 0 and 1 are fitted
    independently, each thresholded at its OWN in-sample `pctl`, and both score the rest.
    INTERLEAVING IS INHERITED DELIBERATELY AND FOR THE SOURCE'S REASON: both fits then span the
    same hours, so drift is largely cancelled and what remains is estimation noise -- this is the
    generous test, and a sequential split can only be worse. What makes interleaving legitimate
    here rather than self-defeating is the guard band: at row granularity the two fits would
    share autocorrelated neighbours and the Jaccard would be driven toward 1 by construction. The
    same interleaving over eroded time blocks separates every fit row from every holdout row by
    at least the measured lag.

    Separation is MEASURED, not assumed: `separation_check` reads the realised gap between blocks
    out of the times, because this function is handed a `Blocks` and every guarantee about how
    one is built lives in `make_blocks`, which it does not call.

    `parts < 3` is refused: two parts leave no shared holdout to score.

    JACCARD ALONE IS NOT AN AGREEMENT MEASURE, because its null depends on the two alarm rates.
    Two INDEPENDENT flaggers at rates ra and rb overlap at ra*rb / (ra + rb - ra*rb) in
    expectation, which is 0.003 at a 0.5% design and 1.0 when both flag everything. So a Jaccard
    of 1.000 on pure white noise is not a bug in the corpus, it is the metric being read without
    its denominator: at n ~ d both fits put every holdout row over their own 99.5th percentile
    and the intersection equals the union by construction. `jaccard_chance` and `jaccard_excess`
    are returned and `format_report` warns on the excess, not the raw number. The saturated end
    is refused outright, matching the empty end -- the source guarded only `union == 0`.

    `blocks_per_dim = min(blocks_a, blocks_b) / d` is the source's `min_part = DIM` floor with
    its OBSERVATION UNIT changed from records to blocks, not a different rule: 1024 was hugbot's
    embedding width and a floor phrased in RECORDS is meaningless when the records are
    autocorrelated, but "d observations per part" is the same rule underneath. Clearing it is not
    sufficiency and the report says so, because the argument this module makes is about
    `cov_params = d(d+1)/2` free parameters -- 3240 at 80 dims -- and `blocks_per_dim >= 1` is
    reached at 80 blocks, i.e. 40x short of one block per parameter. `blocks_per_cov_param` is
    returned alongside for that reason. Both are ratios on the record, neither is a gate.

    VACUOUS AGREEMENT IS REFUSED AT BOTH ENDS, diverging from the source, which returns
    `jaccard=1.0` for the empty case and calls it out in `main()`. A stored 1.0 travels
    downstream and reads as perfect agreement. A model that never alarms and one that alarms on
    every row are both perfectly reproducible and perfectly useless.
    """
    X = _as_x(X)
    if blocks.row.size and int(blocks.row.max()) >= X.shape[0]:
        raise Refused("blocks index row %d of an X with %d rows: they are not the same corpus"
                      % (int(blocks.row.max()), X.shape[0]))
    sep = separation_check(blocks, allow_unseparated=allow_unseparated)
    parts = int(parts)
    if parts < 3:
        raise Refused("parts=%d: two parts leave no shared holdout to score, so there is nothing "
                      "to compare the two fits on" % parts)
    ids = blocks.ids
    need = parts * int(min_blocks_part)
    if ids.size < need:
        raise Refused("%d blocks available, %d required (parts=%d x min_blocks_part=%d) at "
                      "block_s=%.1fs" % (ids.size, need, parts, min_blocks_part, blocks.block_s))

    rank = np.arange(ids.size) % parts
    ids_a, ids_b = ids[rank == 0], ids[rank == 1]
    ids_h = ids[rank >= 2]
    for name, got in (("a", ids_a.size), ("b", ids_b.size), ("holdout", ids_h.size)):
        if got < int(min_blocks_part):
            raise Refused("part %s got %d blocks, %d required" % (name, got, min_blocks_part))

    rows_a = blocks.row[np.isin(blocks.block, ids_a)]
    rows_b = blocks.row[np.isin(blocks.block, ids_b)]
    rows_h = blocks.row[np.isin(blocks.block, ids_h)]

    _resolution_check(int(rows_a.size), int(rows_h.size), pctl, "part a")
    _resolution_check(int(rows_b.size), int(rows_h.size), pctl, "part b")
    thr_a, s_a, diag_a = _fit_and_flag(X, rows_a, rows_h, fit, score, pctl, y)
    thr_b, s_b, diag_b = _fit_and_flag(X, rows_b, rows_h, fit, score, pctl, y)
    flag_a, flag_b = s_a > thr_a, s_b > thr_b

    union = int((flag_a | flag_b).sum())
    n_h = int(rows_h.size)
    if union == 0 or union == n_h == int((flag_a & flag_b).sum()):
        raise Refused("both fits flagged %d of %d holdout rows (thresholds %.6g and %.6g): the "
                      "alarm sets agree vacuously and the Jaccard is %.1f by construction, not "
                      "by measurement. A model that never alarms and a model that alarms on "
                      "everything are both perfectly reproducible and perfectly useless"
                      % (union, n_h, thr_a, thr_b, 0.0 if union == 0 else 1.0))

    ra, rb = float(flag_a.mean()), float(flag_b.mean())
    chance = (ra * rb) / (ra + rb - ra * rb) if (ra + rb - ra * rb) > 0 else None
    with np.errstate(invalid="ignore", divide="ignore"):
        pear = (float(np.corrcoef(s_a, s_b)[0, 1])
                if (s_a.std() > 0 and s_b.std() > 0) else float("nan"))
    jac = float((flag_a & flag_b).sum()) / union
    d = float(X.shape[1])
    out = {"n_a": int(rows_a.size), "n_b": int(rows_b.size), "n_holdout": n_h,
           "blocks_a": int(ids_a.size), "blocks_b": int(ids_b.size),
           "blocks_holdout": int(ids_h.size),
           "rate_a": ra, "rate_b": rb,
           "designed": (100.0 - float(pctl)) / 100.0,
           "jaccard": jac, "jaccard_chance": chance,
           "jaccard_excess": (None if chance is None else jac - chance),
           "pearson": (None if not math.isfinite(pear) else pear),
           "threshold_a": thr_a, "threshold_b": thr_b, "diag_a": diag_a, "diag_b": diag_b,
           "block_s": blocks.block_s, "lag_s": blocks.lag_s, "guard_s": blocks.guard_s,
           "blocks_per_dim": min(int(ids_a.size), int(ids_b.size)) / d,
           "cov_params": int(d * (d + 1) // 2),
           "blocks_per_cov_param": min(int(ids_a.size), int(ids_b.size)) / (d * (d + 1) / 2.0)}
    out.update(sep)
    return out


# --------------------------------------------------------------------------- reference pair

def mahalanobis_fit(X, y=None, *, eps: float = 1e-9) -> Dict[str, Any]:
    """The reference unsupervised `FitFn`: mean plus a floored inverse covariance.

    THE EPS HERE IS NOT THE HUGBOT EPS AND IS NOT SIZED THE SAME WAY. That trainer's floor exists
    because 361 of 1024 YAMNet dimensions hold exactly zero variance and 43.2% of directions end
    up pinned by it; pages of its source measure what floor to use. The scene corpus does not
    have that problem: measured 2026-09-08 on the nyquist drain, rank 80/80, zero zero-variance
    dimensions, 0.00% exact zeros, lambda_min +0.186, condition number 1.5e4, and 0% of
    directions set by the floor at eps=1e-9. So eps is a numerical guard against a degenerate
    caller, not a fitted value, and `eps_floor_frac` is returned so a corpus that DOES start
    leaning on it says so instead of quietly changing what the metric means.

    THAT MEASUREMENT IS OF THE WHOLE CORPUS AND NOTHING EVER FITS THE WHOLE CORPUS. Every number
    the reports return comes from a split PART, and a part is small: at the module's minimum
    legal split, 32 rows for 80 dims, over half the directions land on the floor. So
    `eps_floor_frac` is now read -- `_fit_and_flag` keeps it, both reports carry it, and
    `format_report` warns on it. It was returned and dropped before, which is a safeguard that
    reports to nobody.

    Floored via `eigh` rather than `inv(cov + eps*I)` so the floored fraction is observable and
    a singular input cannot raise mid-report.
    """
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] < 2:
        raise Refused("mahalanobis_fit needs >= 2 rows of [n, d]; got %r" % (X.shape,))
    mu = X.mean(axis=0)
    cov = np.cov(X, rowvar=False)
    cov = np.atleast_2d(cov)
    lam, vec = np.linalg.eigh(cov)
    floored = int((lam < eps).sum())
    lam_f = np.maximum(lam, eps)
    inv = (vec / lam_f) @ vec.T
    return {"mu": mu, "inv": inv, "eps": float(eps), "n": int(X.shape[0]), "d": int(X.shape[1]),
            "lam_min": float(lam.min()), "lam_max": float(lam.max()),
            "cond": float(lam_f.max() / lam_f.min()),
            "eps_floor_frac": floored / float(lam.size)}


def mahalanobis_score(model: Dict[str, Any], X) -> np.ndarray:
    """The matching `ScoreFn`: Mahalanobis distance, higher = more unlike the fitted normal."""
    X = np.asarray(X, dtype=float)
    diff = X - model["mu"]
    return np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", diff, model["inv"], diff), 0.0))


# --------------------------------------------------------------------------- corpus adapter

def scene_xt(pool, *, node: Optional[str] = None, day: Optional[str] = None, mode: str = "db",
             limit: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray, List[Dict[str, Any]]]:
    """`(X, t, rows)` from a `hear.pool.Pool`. The ONLY contract between this module and the pool.

    `pool.py` is not modified and does not import this module. `Pool.scene_matrix` already
    refuses to stack rows of differing geometry, so X's width cannot silently depend on which
    firmware was running; what is added here is `t`, and the assertion that X and rows are the
    same length in the same order, which is the invariant every split below rests on.

    UNANCHORED ROWS COME BACK AS NaN, NOT 0. `ts_utc_s` is None for a pre-PPS row and a naive
    reader takes `utc_us` at face value, which places 56.1% of mach at 1970 and reports a 56-year
    span. `make_blocks` drops NaN and 0 alike and counts them in `dropped_unanchored`.
    """
    X, rows = pool.scene_matrix(node=node, day=day, mode=mode, limit=limit)
    X = np.asarray(X, dtype=float)
    if X.shape[0] != len(rows):
        raise Refused("scene_matrix returned %d rows of X against %d row dicts"
                      % (X.shape[0], len(rows)))
    t = np.array([(r.get("ts_utc_s") if r.get("anchored") and r.get("ts_utc_s") else np.nan)
                  for r in rows], dtype=float)
    return X, t, rows


# --------------------------------------------------------------------------- report

def format_report(cal: Optional[Dict[str, Any]], rep: Optional[Dict[str, Any]],
                  blocks: Blocks) -> str:
    """Text. `WARN` lines are text and nothing else -- no return value, no exit code, no gate.

    A caller that wants a gate keys on the returned fields itself. This is the same separation
    the source keeps between its model-quality fields and its `healthy` boolean: the moment a
    diagnostic decides an exit code, the pressure to make the diagnostic pass starts acting on
    the diagnostic.
    """
    L: List[str] = []
    L.append("blocks: %d x %.1fs  lag %.1fs%s  guard %.1fs" % (
        blocks.n_blocks, blocks.block_s, blocks.lag_s,
        " (CENSORED)" if blocks.lag_censored else "", blocks.guard_s))
    L.append("rows: %d in -> %d kept  (unanchored %d, guard %d, short block %d, anchored %.1f%%)"
             % (blocks.n_in, blocks.row.size, blocks.dropped_unanchored, blocks.dropped_guard,
                blocks.dropped_short_block, 100.0 * blocks.anchored_frac))
    if cal:
        L.append("holdout exceedance (design %.3f%%): %s" % (
            100.0 * cal["designed"],
            " / ".join("%.2f%%@%.1f" % (100.0 * d["exceed"], d["frac"]) for d in cal["draws"])))
        L.append("  min %.3f%%  median %.3f%%  max %.3f%%  spread %s  (%d distinct split(s) "
                 "from %d cut point(s), %.1f fit rows per dimension)" % (
                     100.0 * cal["min"], 100.0 * cal["median"], 100.0 * cal["max"],
                     ("n/a (%s)" % cal["spread_undefined_because"])
                     if cal["spread_x"] is None else "%.1fx" % cal["spread_x"],
                     cal["n_distinct_splits"], len(cal["draws"]), cal["n_fit_over_d"]))
        for r in cal.get("refused", []):
            L.append("  REFUSED frac=%.2f: %s" % (r["frac"], r["reason"]))
        if cal["n_distinct_splits"] < 2:
            L.append("  WARN the envelope collapsed to ONE split: there is no spread here to "
                     "read, whatever it prints. Five distinct cuts need 20 blocks")
        if cal["max"] > EXCEED_WARN_X * cal["designed"]:
            L.append("  WARN max %.3f%% is %.0fx the %.3f%% design"
                     % (100.0 * cal["max"], cal["max"] / cal["designed"],
                        100.0 * cal["designed"]))
        if cal["spread_x"] is not None and cal["spread_x"] > 10.0:
            L.append("  WARN %.0fx spread across cut points: one cut is not the rate"
                     % cal["spread_x"])
        if cal["max_eps_floor_frac"] > 0.0:
            L.append("  WARN %.1f%% of the fitted covariance's directions are on the eps floor: "
                     "the metric's units are set by the floor, not by the corpus"
                     % (100.0 * cal["max_eps_floor_frac"]))
        if cal["n_fit_over_d"] < 10.0:
            L.append("  WARN %.1f fit rows per dimension: an exceedance this far out is "
                     "estimation error in the covariance before it is drift in the corpus"
                     % cal["n_fit_over_d"])
        if not cal["separated"]:
            L.append("  WARN blocks are NOT separated by the measured lag (allow_unseparated): "
                     "these cut points share autocorrelated rows")
    if rep:
        L.append("reproducibility: rates %.2f%% / %.2f%% of %d holdout rows (design %.3f%%), "
                 "jaccard %.3f, pearson %s" % (
                     100.0 * rep["rate_a"], 100.0 * rep["rate_b"], rep["n_holdout"],
                     100.0 * rep["designed"], rep["jaccard"],
                     "n/a" if rep["pearson"] is None else "%.3f" % rep["pearson"]))
        L.append("  chance jaccard at these rates %s, excess %s"
                 % ("n/a" if rep["jaccard_chance"] is None else "%.3f" % rep["jaccard_chance"],
                    "n/a" if rep["jaccard_excess"] is None else "%+.3f" % rep["jaccard_excess"]))
        L.append("  blocks %d / %d / %d, %.2f blocks per fitted dimension, %.4f per covariance "
                 "parameter (%d of them), closest cross-block gap %s"
                 % (rep["blocks_a"], rep["blocks_b"], rep["blocks_holdout"],
                    rep["blocks_per_dim"], rep["blocks_per_cov_param"], rep["cov_params"],
                    "n/a" if rep["min_cross_block_gap_s"] is None
                    else "%.1fs" % rep["min_cross_block_gap_s"]))
        if rep["jaccard_excess"] is not None and rep["jaccard_excess"] < 0.1:
            L.append("  WARN jaccard %.3f is only %+.3f above the %.3f two INDEPENDENT flaggers "
                     "at rates %.1f%% / %.1f%% would score by chance: this is not agreement"
                     % (rep["jaccard"], rep["jaccard_excess"], rep["jaccard_chance"],
                        100.0 * rep["rate_a"], 100.0 * rep["rate_b"]))
        if rep["jaccard"] < 0.5:
            L.append("  WARN jaccard %.3f: which row alarms is mostly decided by which blocks "
                     "landed in the fit" % rep["jaccard"])
        if rep["blocks_per_dim"] < 1.0:
            L.append("  WARN %.2f blocks per dimension: the covariance is under-determined. "
                     "Clearing this is not sufficiency -- one block per covariance parameter "
                     "needs %d of them" % (rep["blocks_per_dim"], rep["cov_params"]))
        for part in ("a", "b"):
            frac = rep["diag_%s" % part].get("eps_floor_frac", 0.0)
            if frac and frac > 0.0:
                L.append("  WARN fit %s has %.1f%% of its directions on the eps floor: the two "
                         "alarm sets are being compared in units the floor set" % (part,
                                                                                   100.0 * frac))
        if not rep["separated"]:
            L.append("  WARN blocks are NOT separated by the measured lag (allow_unseparated): "
                     "closest cross-block gap %s against lag %.1fs, so the two fits share "
                     "autocorrelated rows and this jaccard is inflated by construction"
                     % ("n/a" if rep["min_cross_block_gap_s"] is None
                        else "%.1fs" % rep["min_cross_block_gap_s"], rep["lag_s"]))
    return "\n".join(L)
