#!/usr/bin/env python3
"""Run `hear.validate`'s split-honest reports against a real `scene.csv`, and refuse by name.

    python3 tools/validate_scene.py ~/dama-hear-drain/20260907-2030/nyquist/scene.csv
    python3 tools/validate_scene.py .../scene.csv --lag-mult 1.5 --shrinkage 0.05
    python3 tools/validate_scene.py .../scene.csv --json

THIS IS A MEASUREMENT TOOL. It ships no `.npz`, starts no service, and the shrinkage Gaussian it
fits is thrown away at the end of the run -- it exists to be a REFERENCE SCORER, something
concrete for the holdout envelope and the reproducibility check to be computed about. Swap it for
a real detector by passing a different `FitFn`/`ScoreFn` pair to `hear.validate`; nothing here is
the model.

EXIT CODE 0 MEANS THE REPORTS WERE COMPUTED, HOWEVER UGLY THE NUMBERS ARE. Exit 2 means a number
could not be computed at all. Nothing in this tool keys an exit code on an exceedance, a Jaccard
or a spread -- the moment a diagnostic decides an exit status, the pressure to make the
diagnostic pass starts acting on the diagnostic, which is the failure the whole module is
written against.

WHAT IT MEASURED, 2026-09-08, on the 20260907-2030 drain (read-only over ssh, copied to /tmp).

  nyquist/scene.csv -- 1,656,358 B, schema S2, 7077 rows, 0 skipped, 0 undecodable,
  20 bands x 4 slices = 80 dims, 100.0% anchored, span 7412.5 s (2.06 h), 22:26:34Z -> 00:30:07Z,
  median row interval 1.022 s.
      decorrelation  lag 852.95 s (816 rows) at rho=0.50, set by the DIRECTION channel; cosine
                     0.853 at lag 1 row, 0.482 at the crossing (6261 pairs), 0.200 at the
                     1853.1 s search cap. The level channel is already at 0.693 at lag 1 and
                     below rho by lag 18, so on this file the two-channel statistic returns
                     exactly what the direction cosine alone returned. Not censored.
      DEFAULT RUN REFUSES. At lag_mult=3.0 the block is 2558.8 s and a 2.06 h file holds 3 of
      them; the envelope needs 6 (4 fit + 2 holdout) and reproducibility needs 12 (3 x 4). This
      file is roughly half the capture an envelope needs and a quarter of what reproducibility
      needs. THAT IS THE RESULT, not a bug in the tool: at a measured 853 s decorrelation lag a
      two-hour file contains about eight independent observations, and no split of eight
      observations supports a 0.5% tail rate.
      FORCED DOWN TO lag_mult=1.5 (block 1279.4 s, 6 blocks, 2172 of 7077 rows kept) it reads
      2.26% exceedance against a 0.500% design, 5x out, at alpha=0.10. But only 2 of the 5 cut
      points survived and BOTH LANDED ON THE SAME BLOCK BOUNDARY, so there is no spread here at
      all: `holdout_envelope` returns `spread_x=None` with `n_distinct_splits=1` and prints
      `spread n/a (2 cut point(s) produced 1 distinct split)`. It printed `spread 1.0x` in the
      first version of this tool, which is the most reassuring value the metric can take and was
      arithmetic rather than measurement -- six blocks is the smallest split that RUNS, and 20 is
      the smallest split whose spread means anything.

  mach/scene.csv -- REFUSES BEFORE IT GETS THAT FAR, on `mixed node`. 276 of its 5653 rows
  (lines 2610-2885) are labelled `nyquist`: one contiguous run, every row `utc_us == 0`, uptime
  14 -> 298 s, and not one of those lines appears anywhere in `nyquist/scene.csv`. No mechanism
  is asserted here -- what is established is that the file is two nodes' rows, and the reference
  Gaussian is a single mean and covariance. `--only-node mach` keeps 5377 rows and prints the 276
  it dropped. The rest of this paragraph is that filtered read.

  Filtered -- 5653 rows parsed, 276 another node, 5377 kept, 2895 of them (53.8%) carrying
  `utc_us == 0` because the node had no PPS lock yet. That is NOT an error and the rows are not
  malformed; they simply cannot be placed on a clock, so they cannot join a time-blocked split.
  Read naively as a number, those zeros put 53.8% of the file at 1970 and make it report a span
  of 56.68 YEARS. The anchored 2482 rows span 2584.9 s (43.1 min), 23:47:20Z -> 00:30:25Z. This
  tool prints both numbers side by side whenever any row is unanchored, so the reading that would
  be wrong is on the page next to the reading that is right.
      mach's lag is 14.33 s (14 rows), and it is set by the LEVEL channel: its direction cosine
      is 0.178 at lag 1 against nyquist's 0.853, but its row-norm autocorrelation is 0.716 there.
      An earlier version of `decorrelation_lag_s` measured direction only and returned 1.02 s --
      ONE ROW -- for this file, and this docstring said so, adding that a 3.1 s block was
      `modules/supersonic/train_sketch.py`'s `int(utc // 3)` fold arriving by a different road.
      That was an artefact of measuring the wrong quantity: the reference scorer reads Mahalanobis
      DISTANCE, which for scene rows is dominated by level, and cosine divides level out. At the
      corrected 14.33 s the file blocks 59 ways at lag_mult=3 and both reports run:
        envelope        0.00% at fracs 0.5/0.6/0.7/0.8 (4 distinct splits), frac 0.90 REFUSED --
                        140 holdout rows cannot represent a 0.500% rate at all.
        reproducibility rates 0.38% / 1.32% of 532 holdout rows, jaccard 0.286 against a chance
                        jaccard of 0.003 at those rates (excess +0.283), pearson 0.958, closest
                        cross-block gap 15.3 s, 0.25 blocks per fitted dimension against 3240
                        covariance parameters.
      mach's per-band sd is 3.56 dB against nyquist's 7.69 dB. Both are measurements; no
      mechanism is asserted for either here.

THE FIVE REFUSALS THIS TOOL OWNS, all raised as `hear.validate.Refused` and all named in their
message. They are structural: each one means a number does not exist, never that it came out bad.
  1. `no decodable rows`          -- a header and nothing behind it, or every row unparseable.
  2. `mixed geometry` / `mixed node` -- one Gaussian over two firmwares' shapes is not a fit, and
     one over two microphones is worse: nyquist and mach differ 2:1 in per-band sd.
  3. `every row is unanchored`    -- 0 of N rows carry a wall clock.
  4. `rows per block`             -- the block the measured lag sizes cannot hold
     `min_rows_per_block` rows at this file's row interval.
  5. `span too short for an envelope` -- fewer blocks than the smallest report needs, with the
     capture hours required to reach both thresholds.

WHY THE SCORER SHRINKS, AND WHY ITS ALPHA IS A STATED CONSTANT AND NOT A FITTED ONE. An 80x80
covariance has 3240 free parameters. The 7077 rows behind it are not 7077 observations: at a
measured 853 s lag they are about 8. Ledoit-Wolf run on the nyquist drain returns alpha=0.0015 --
essentially no shrinkage -- because its optimality is derived from n, and it is handed n=7077.
Feeding an autocorrelated corpus to an estimator that assumes independence gets you a confident
answer to a question nobody asked, so this tool does not use one: `--shrinkage` defaults to 0.10,
is printed on every run, and the alpha sensitivity above (6.43 -> 3.48 -> 2.26%) is on the record
so nobody reads it as a detail. `blocks_per_dim` from the reproducibility report is the honest
sample count for that covariance.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import scenefile as SF                                    # noqa: E402
from hear import validate as V                                      # noqa: E402

DEFAULT_SHRINKAGE = 0.10
# Mirrors hear.validate's own defaults; stated here so the refusal messages can do the
# arithmetic before make_blocks is reached rather than after it has already failed.
MIN_ROWS_PER_BLOCK = 8
MIN_BLOCKS_ENVELOPE = 6                     # holdout_calibration: 4 fit + 2 holdout
MIN_BLOCKS_REPRODUCIBILITY = 12             # alarm_reproducibility: 3 parts x 4
# ...and the one that is NOT a mirror of a floor in hear.validate, because there is no floor
# there to mirror. MIN_BLOCKS_ENVELOPE is the smallest split that RUNS; at exactly that many
# blocks every surviving frac is clamped to the same cut and the envelope's spread is 1.0x by
# arithmetic on any data. Five distinct cut points first exist at 20 blocks, which is the
# smallest split whose spread MEANS anything, and the refusal below quotes both hour figures so
# the operator is not sent to capture their way into the degenerate case.
MIN_BLOCKS_DISTINCT_CUTS = 20


# --------------------------------------------------------------------------- reading

@dataclass
class Scene:
    """One node's scene.csv as a matrix, its time axis, and everything dropped getting there."""
    path: str
    node: str
    generation: str
    X: np.ndarray
    t: np.ndarray
    rows: List[Dict[str, Any]]
    bands: int
    slices: int
    mode: str
    n_lines: int
    n_skipped: int
    skip_reasons: Dict[str, int] = field(default_factory=dict)
    n_undecodable: int = 0
    undecodable_reasons: Dict[str, int] = field(default_factory=dict)
    n_foreign_node: int = 0
    foreign_nodes: Dict[str, int] = field(default_factory=dict)

    @property
    def n_rows(self) -> int:
        return int(self.X.shape[0])

    @property
    def dims(self) -> int:
        return int(self.X.shape[1])

    @property
    def anchored(self) -> np.ndarray:
        return np.isfinite(self.t) & (self.t > 0.0)

    @property
    def n_anchored(self) -> int:
        return int(self.anchored.sum())

    @property
    def n_unanchored(self) -> int:
        return self.n_rows - self.n_anchored

    @property
    def span_s(self) -> float:
        """Anchored span. The only span that means anything."""
        ta = self.t[self.anchored]
        return float(ta.max() - ta.min()) if ta.size >= 2 else 0.0

    @property
    def naive_span_s(self) -> float:
        """What a reader who took `utc_us` at face value would report.

        Kept and printed BESIDE `span_s` rather than merely avoided. On mach this is 56.68 years
        against a real 43.1 minutes, and a tool that silently does the right thing teaches nobody
        why the wrong number is wrong.
        """
        us = np.array([_utc_us(r) for r in self.rows], dtype=np.float64)
        return float(us.max() - us.min()) / 1e6 if us.size >= 2 else 0.0

    @property
    def dt_s(self) -> float:
        """Median interval between anchored rows, 0.0 if it cannot be formed."""
        ta = np.sort(self.t[self.anchored])
        d = np.diff(ta)
        d = d[d > 0]
        return float(np.median(d)) if d.size else 0.0


def _utc_us(row: Dict[str, Any]) -> int:
    try:
        return int(float(row.get("utc_us") or 0))
    except (TypeError, ValueError):
        return 0


def read_scene(path: str, *, node: Optional[str] = None, mode: str = "db",
               limit: Optional[int] = None, only_node: Optional[str] = None) -> Scene:
    """`scene.csv` -> `Scene`. Geometry comes from the rows; nothing here pins a shape.

    `mode="db"` reproduces `hear.pool.Pool.scene_matrix`: a cell is `q/2 + ref_db`, since `q`
    counts half-decibel steps below the row's OWN `ref_db4` reference. `mode="q"` leaves the raw
    steps, which is a different feature space -- the reference is per row, so `q` alone throws
    away the level the row was measured against.

    The default node name is the parent directory, which is how `tools/hear_drain.py` lays the
    drain out (`<pool>/<utc>/<node>/scene.csv`). S2 files name themselves and ignore it.

    `only_node` KEEPS ONE NODE'S ROWS AND COUNTS THE REST, and it exists because the mixed-node
    refusal below is not hypothetical: the 20260907-2030 drain's `mach/scene.csv` carries 276
    rows (2610-2885) labelled `nyquist`, every one of them `utc_us == 0` over uptime 14 -> 298 s,
    and none of them appears anywhere in `nyquist/scene.csv`. No mechanism is asserted for that
    here. The default stays a refusal because silently fitting across it is the failure; passing
    `only_node` is a decision, and the dropped count is printed.
    """
    if mode not in ("db", "q"):
        raise V.Refused("mode must be 'db' or 'q'; got %r" % (mode,))
    default_node = node or (os.path.basename(os.path.dirname(os.path.abspath(path))) or None)
    read = SF.read_file(path, default_node=default_node)

    xs: List[np.ndarray] = []
    ts: List[float] = []
    kept: List[Dict[str, Any]] = []
    geom: Optional[Tuple[int, int]] = None
    seen_node: Optional[str] = None
    bad: Dict[str, int] = {}
    foreign: Dict[str, int] = {}
    for r in read.rows:
        try:
            d = SF.decode_row(r)
        except ValueError as e:
            reason = str(e).split(";")[0][:60]
            bad[reason] = bad.get(reason, 0) + 1
            continue
        g = (int(d["bands"]), int(d["slices"]))
        if geom is None:
            geom = g
        elif g != geom:
            raise V.Refused(
                "mixed geometry in %s: %dx%d then %dx%d. One Gaussian cannot be fitted across "
                "two descriptor shapes -- bands and slices are firmware's to change, and a "
                "matrix whose width depends on which build was running is not a corpus. Split "
                "the file by firmware, or pass --limit to stay inside one."
                % (os.path.basename(path), geom[0], geom[1], g[0], g[1]))
        n = str(r.get("node") or "")
        if only_node is not None and n != only_node:
            foreign[n] = foreign.get(n, 0) + 1
            continue
        if seen_node is None:
            seen_node = n
        elif n != seen_node:
            raise V.Refused(
                "mixed node in %s: rows from %r and %r. Fitting one reference Gaussian across "
                "two microphones is not a corpus either: measured 2026-09-08, mach's per-band sd "
                "is 3.56 dB against nyquist's 7.69 dB, so the pooled 'normal' is neither node's. "
                "This drain really does contain one: the 20260907-2030 mach/scene.csv carries 276 "
                "rows labelled nyquist. Pass --only-node %s to keep one node's rows and have the "
                "rest counted."
                % (os.path.basename(path), seen_node, n, seen_node))
        q = d["q"].astype(float)
        xs.append((q / 2.0 + d["ref_db"] if mode == "db" else q).reshape(-1))
        us = _utc_us(r)
        ts.append(us / 1e6 if us > 0 else float("nan"))
        kept.append(r)
        if limit and len(xs) >= int(limit):
            break

    n_bad = int(sum(bad.values()))
    if not xs:
        raise V.Refused(
            "no decodable rows in %s: %d line(s) parsed, %d skipped by the reader (%s), %d "
            "undecodable (%s), %d dropped as another node (%s). A file the node created but "
            "never wrote and a file whose content was lost both read like this."
            % (path, len(read.rows), len(read.skips), _counts(read.counts) or "none",
               n_bad, _counts(bad) or "none", int(sum(foreign.values())),
               _counts(foreign) or "none"))

    X = np.vstack(xs)
    t = np.asarray(ts, dtype=float)
    scene = Scene(path=path, node=(seen_node or default_node or "?"),
                  generation=read.generation.name, X=X, t=t, rows=kept,
                  bands=geom[0], slices=geom[1], mode=mode,
                  n_lines=len(read.rows), n_skipped=len(read.skips),
                  skip_reasons=dict(read.counts), n_undecodable=n_bad,
                  undecodable_reasons=bad,
                  n_foreign_node=int(sum(foreign.values())), foreign_nodes=foreign)
    if scene.n_anchored == 0:
        raise V.Refused(
            "every row is unanchored in %s: 0 of %d rows carry a wall clock (utc_us == 0 -- the "
            "node had not locked PPS). These rows are NOT malformed and hear/pool.py keeps them "
            "on purpose, but a row with no time cannot be placed in a time block, so no "
            "time-keyed split exists over this file. Reading those zeros as a number instead "
            "would date them to 1970 and report a span of %.2f years."
            % (path, scene.n_rows, scene.naive_span_s / (365.25 * 86400.0)))
    return scene


def _counts(d: Dict[str, int]) -> str:
    return ", ".join("%s x%d" % (k, v) for k, v in sorted(d.items()))


# --------------------------------------------------------------------------- reference scorer

def shrinkage_gaussian(alpha: float = DEFAULT_SHRINKAGE) -> Callable[..., Dict[str, Any]]:
    """A `FitFn` factory: mean plus a covariance shrunk toward `(trace(S)/d) * I`.

    `C = (1-alpha) * S + alpha * (trace(S)/d) * I`, inverted through `eigh`. In the eigenbasis
    that is exactly `(1-alpha) * lam + alpha * mean(lam)`, so one decomposition yields both the
    raw and the shrunk spectrum and `cond_raw` / `cond` are reported side by side rather than
    asserted.

    ALPHA IS A STATED CONSTANT, NOT A FITTED ONE, and the docstring at the top of this file
    carries the reason: an 80x80 covariance has 3240 free parameters and the nyquist drain's 7077
    rows are worth about 8 independent observations at its measured 853 s lag. Ledoit-Wolf
    returns 0.0015 on that corpus because it is told n=7077. The alpha that matters is therefore
    a choice made in the open -- and it moves the answer: the same corpus and the same split read
    6.43% / 3.48% / 2.26% exceedance at alpha 0 / 0.05 / 0.10.

    Pairs with `hear.validate.mahalanobis_score`, which is the whole seam: the returned dict is
    `{"mu", "inv", ...}` and nothing else is promised.
    """
    a = float(alpha)
    if not (0.0 <= a <= 1.0):
        raise V.Refused("shrinkage alpha must be in [0, 1]; got %r" % (alpha,))

    def fit(X, y=None) -> Dict[str, Any]:
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] < 2:
            raise V.Refused("shrinkage_gaussian needs >= 2 rows of [n, d]; got %r" % (X.shape,))
        mu = X.mean(axis=0)
        S = np.atleast_2d(np.cov(X, rowvar=False))
        lam, vec = np.linalg.eigh(S)
        mean_lam = float(lam.mean())
        lam_s = (1.0 - a) * lam + a * mean_lam
        floored = int((lam_s <= 0.0).sum())
        lam_s = np.maximum(lam_s, 1e-12)
        # `floored` counts the SHRUNK spectrum, and for any alpha > 0 over a real covariance
        # (1-a)*lam + a*mean(lam) is positive whenever mean(lam) is, so at the default alpha=0.10
        # that counter cannot ever be non-zero -- it can only count at alpha=0. `floored_raw`
        # counts the raw spectrum, which is the number that says the corpus was singular before
        # shrinkage hid it.
        # 1e-9 absolute, the same floor hear.validate.mahalanobis_fit uses, so the two report
        # the same quantity: a zeroed dimension lands at 2e-16 here, not at exactly 0.
        raw = int((lam < 1e-9).sum())
        return {"mu": mu, "inv": (vec / lam_s) @ vec.T, "alpha": a,
                "floored_raw": raw, "eps_floor_frac": raw / float(lam.size),
                "n": int(X.shape[0]), "d": int(X.shape[1]),
                "lam_min": float(lam.min()), "lam_max": float(lam.max()),
                "cond_raw": float(lam.max() / lam.min()) if lam.min() > 0 else float("inf"),
                "cond": float(lam_s.max() / lam_s.min()),
                "floored": floored}
    return fit


# --------------------------------------------------------------------------- the run

def run_reports(scene: Scene, *, shrinkage: float = DEFAULT_SHRINKAGE, lag_mult: float = 3.0,
                block_s: Optional[float] = None, max_lag_s: Optional[float] = None,
                pctl: float = 99.5, parts: int = 3,
                min_rows_per_block: int = MIN_ROWS_PER_BLOCK) -> Dict[str, Any]:
    """Measure the lag, build blocks, and run both reports. Refusals propagate; nothing gates.

    The two refusals raised HERE rather than deeper are the ones whose honest message needs the
    file's row interval and span, which `make_blocks` does not have in a form it can phrase. It
    would fail on both of these anyway -- on mach with "every kept row landed in 0 block(s):
    anchored span 0.0s", which is true, arithmetically derived from an already-emptied array, and
    tells the operator nothing about the 1.02 s lag that caused it.
    """
    fit = shrinkage_gaussian(shrinkage)
    lag = V.decorrelation_lag_s(scene.X, scene.t, max_lag_s=max_lag_s)
    lag_s = float(lag["lag_s"])
    want_block_s = float(block_s) if block_s is not None else float(lag_mult) * lag_s
    early = lag_notes(scene, lag)

    dt = scene.dt_s
    # ⚠️GUARD-AWARE. `make_blocks` counts a block's rows only AFTER the guard band has removed
    # `lag_s` from the head of every block, so the raw block length is not what has to hold
    # `min_rows_per_block` rows -- `block_s - lag_s` is. Dividing the raw length by dt left a band
    # of measured lags (2.725-4.088 s at lag_mult=3 and a 1.022 s row) that passed this check and
    # then died inside make_blocks with "every kept row landed in 0 block(s): anchored span 0.0s",
    # which is exactly the uninformative message this refusal exists to replace.
    keeps_s = max(0.0, want_block_s - lag_s)
    rows_per_block = keeps_s / dt if dt > 0 else float("inf")
    if rows_per_block < float(min_rows_per_block):
        raise _with_notes(
            early,
            "rows per block: a %.1fs block keeps about %.1f rows after its %.1fs guard band at "
            "this file's %.3fs row interval, and a block needs %d. It came from a measured lag "
            "of %.2fs (%d row(s), cosine %.3f at lag 1) x lag_mult=%.1f. A lag that short sizes "
            "the same fold modules/supersonic/train_sketch.py cuts with int(utc // 3) -- correct "
            "for a firing string, and it does not make these diagnostics noisy, it makes them "
            "pass. Blocks need >= %.1fs here; measure over a corpus whose rows are actually "
            "correlated, or state --block-s deliberately."
            % (want_block_s, rows_per_block, lag_s, dt, min_rows_per_block, lag_s,
               int(lag["lag_rows"]), lag["cos_lag1"], lag_mult,
               float(min_rows_per_block) * dt + lag_s))

    blocks = V.make_blocks(scene.X, scene.t, block_s=block_s, lag_mult=lag_mult,
                           max_lag_s=max_lag_s, min_rows_per_block=min_rows_per_block)
    if blocks.n_blocks < MIN_BLOCKS_ENVELOPE:
        raise _with_notes(
            early,
            "span too short for an envelope: %.1fs of anchored rows (%.2f h) makes %d block(s) "
            "of %.1fs, and the envelope needs %d (4 fit + 2 holdout) while reproducibility needs "
            "%d (3 parts x 4). At a measured decorrelation lag of %.1fs this file holds about "
            "%.1f independent observations, and no split of that many supports a %.3f%% tail "
            "rate. Capture at least %.1f h for an envelope and %.1f h for reproducibility -- but "
            "%d blocks (%.1f h) is what five DISTINCT cut points need, and at exactly %d blocks "
            "every surviving frac is clamped to the same cut, so the envelope's spread reads "
            "1.0x on any data whatsoever. Or lower --lag-mult (never below 1) knowing the margin "
            "is what it buys."
            % (scene.span_s, scene.span_s / 3600.0, blocks.n_blocks, blocks.block_s,
               MIN_BLOCKS_ENVELOPE, MIN_BLOCKS_REPRODUCIBILITY, lag_s,
               scene.span_s / lag_s if lag_s > 0 else 0.0, 100.0 - pctl,
               MIN_BLOCKS_ENVELOPE * blocks.block_s / 3600.0,
               MIN_BLOCKS_REPRODUCIBILITY * blocks.block_s / 3600.0,
               MIN_BLOCKS_DISTINCT_CUTS,
               MIN_BLOCKS_DISTINCT_CUTS * blocks.block_s / 3600.0, MIN_BLOCKS_ENVELOPE))

    out: Dict[str, Any] = {"lag": lag, "blocks": blocks, "shrinkage": float(shrinkage),
                           "pctl": float(pctl), "envelope": None, "reproducibility": None,
                           "envelope_refused": None, "reproducibility_refused": None}
    try:
        out["envelope"] = V.holdout_envelope(scene.X, blocks, fit=fit,
                                             score=V.mahalanobis_score, pctl=pctl)
    except V.Refused as e:
        out["envelope_refused"] = str(e)
    try:
        out["reproducibility"] = V.alarm_reproducibility(scene.X, blocks, fit=fit,
                                                         score=V.mahalanobis_score,
                                                         pctl=pctl, parts=parts)
    except V.Refused as e:
        out["reproducibility_refused"] = str(e)
    out["notes"] = notes(scene, out)
    return out


def lag_notes(scene: Scene, lag: Dict[str, Any]) -> List[str]:
    """The two captions that depend only on the measured lag, so they survive a REFUSAL.

    `NOTE lag is at the row-interval floor` was unreachable on the default path before: the
    `rows per block` refusal fires first for every `lag_mult <= 2.0` and most of the range above,
    the mach run that motivated the note refused, and both of the note's tests passed an explicit
    `block_s` that bypassed the refusal -- the fixture's own override was the only thing making
    the assertion reachable. So these are computed BEFORE either tool-owned refusal and
    `_with_notes` carries them into the refusal message. A caption that only prints on the runs
    that did not need it is not a caption.
    """
    out: List[str] = []
    dt = scene.dt_s
    if dt > 0 and lag["lag_s"] < 4.0 * dt:
        out.append("lag is at the row-interval floor: %.2fs is %.1f row interval(s), so the "
                   "guard band separates neighbours by about one row and the block split is a "
                   "row-index split wearing a clock. Blocks sized from it would be the 3s "
                   "train_sketch fold. (cosine %.3f at lag 1 row)"
                   % (lag["lag_s"], lag["lag_s"] / dt, lag["cos_lag1"]))
    if lag.get("censored"):
        out.append("decorrelation lag is CENSORED at the %.1fs search cap (cosine still %.3f "
                   "there): the block length rests on a bound, not on a crossing."
                   % (lag["max_lag_s"], lag["cos_max_lag"]))
    return out


def _with_notes(early: List[str], msg: str) -> V.Refused:
    return V.Refused(msg + "".join("\nNOTE %s" % n for n in early))


def notes(scene: Scene, run: Dict[str, Any]) -> List[str]:
    """The reassuring readings that need a caption. Text only -- no gate, no exit code.

    All of these fire on a number that looks GOOD. `spread 1.0x` and a short lag both read as
    stability to anyone skimming, and both mean the split had nothing to measure.
    """
    out: List[str] = lag_notes(scene, run["lag"])
    env = run.get("envelope")
    if env:
        cuts = {(d["blocks_fit"], d["n_fit"]) for d in env["draws"]}
        if len(cuts) < 2 or len(cuts) < len(env["draws"]):
            out.append("cuts collapsed: %d cut point(s) produced %d distinct split(s), so the "
                       "%s spread is measured across %d split(s) and not across cut points. %d "
                       "of %d fracs were refused outright, and %d blocks is what five distinct "
                       "cuts need. The severe form -- ONE surviving draw -- used to slip past "
                       "this note, because its condition was len(cuts) < len(draws) and 1 < 1 "
                       "is False."
                       % (len(env["draws"]), len(cuts),
                          "n/a" if env["spread_x"] is None else "%.1fx" % env["spread_x"],
                          len(cuts), len(env["refused"]),
                          len(env["draws"]) + len(env["refused"]), MIN_BLOCKS_DISTINCT_CUTS))
    rep = run.get("reproducibility")
    if rep and rep["blocks_per_dim"] < 1.0:
        out.append("%.2f blocks per fitted dimension: a %dx%d covariance has %d free "
                   "parameters and this fit has %d independent blocks behind it. --shrinkage "
                   "%.2f is doing the work, not the data."
                   % (rep["blocks_per_dim"], scene.dims, scene.dims,
                      scene.dims * (scene.dims + 1) // 2,
                      min(rep["blocks_a"], rep["blocks_b"]), run["shrinkage"]))
    return out


# --------------------------------------------------------------------------- output

def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_scene(scene: Scene) -> str:
    L = [
        "scene: %s" % scene.path,
        "  node %s, schema %s, %s mode, %d bands x %d slices = %d dims"
        % (scene.node, scene.generation, scene.mode, scene.bands, scene.slices, scene.dims),
        "  rows: %d parsed, %d reader-skipped (%s), %d undecodable (%s), %d another node (%s) "
        "-> %d x %d"
        % (scene.n_lines, scene.n_skipped, _counts(scene.skip_reasons) or "none",
           scene.n_undecodable, _counts(scene.undecodable_reasons) or "none",
           scene.n_foreign_node, _counts(scene.foreign_nodes) or "none",
           scene.n_rows, scene.dims),
    ]
    ta = scene.t[scene.anchored]
    L.append("  time: %d anchored (%.1f%%), %d unanchored (utc_us == 0, pre-PPS)"
             % (scene.n_anchored, 100.0 * scene.n_anchored / max(1, scene.n_rows),
                scene.n_unanchored))
    L.append("  span: %.1fs (%.2f h) %s -> %s, median row interval %.3fs"
             % (scene.span_s, scene.span_s / 3600.0, _iso(float(ta.min())), _iso(float(ta.max())),
                scene.dt_s))
    if scene.n_unanchored:
        L.append("  ⚠️read naively, those %d zeros would date to 1970 and this file would report "
                 "a span of %.2f years instead of %.2f h"
                 % (scene.n_unanchored, scene.naive_span_s / (365.25 * 86400.0),
                    scene.span_s / 3600.0))
    return "\n".join(L)


def format_run(scene: Scene, run: Dict[str, Any]) -> str:
    lag = run["lag"]
    L = [format_scene(scene),
         "decorrelation: lag %.2fs (%d rows) at rho=%.2f%s; cosine %.3f at lag 1 row, %.3f at "
         "the %.1fs cap, %d anchored rows"
         % (lag["lag_s"], lag["lag_rows"], lag["rho"],
            " CENSORED" if lag["censored"] else "", lag["cos_lag1"], lag["cos_max_lag"],
            lag["max_lag_s"], lag["n_anchored"]),
         "scorer: shrinkage Gaussian, alpha=%.3f toward (trace/d)I, threshold at pctl %.2f "
         "(design %.3f%%)" % (run["shrinkage"], run["pctl"], 100.0 - run["pctl"]),
         V.format_report(run["envelope"], run["reproducibility"], run["blocks"])]
    if run["envelope_refused"]:
        L.append("REFUSED holdout envelope: %s" % run["envelope_refused"])
    if run["reproducibility_refused"]:
        L.append("REFUSED alarm reproducibility: %s" % run["reproducibility_refused"])
    for n in run["notes"]:
        L.append("NOTE %s" % n)
    return "\n".join(L)


def _jsonable(o: Any) -> Any:
    if isinstance(o, V.Blocks):
        return {"n_blocks": o.n_blocks, "block_s": o.block_s, "lag_s": o.lag_s,
                "guard_s": o.guard_s, "lag_censored": o.lag_censored, "n_in": o.n_in,
                "n_kept": int(o.row.size), "dropped_unanchored": o.dropped_unanchored,
                "dropped_guard": o.dropped_guard,
                "dropped_short_block": o.dropped_short_block}
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(repr(o))


def as_json(scene: Scene, run: Optional[Dict[str, Any]], refused: Optional[str]) -> str:
    doc = {"path": scene.path, "node": scene.node, "schema": scene.generation,
           "mode": scene.mode, "bands": scene.bands, "slices": scene.slices,
           "dims": scene.dims, "rows": scene.n_rows, "lines": scene.n_lines,
           "skipped": scene.n_skipped, "undecodable": scene.n_undecodable,
           "anchored": scene.n_anchored, "unanchored": scene.n_unanchored,
           "foreign_node": scene.n_foreign_node, "foreign_nodes": scene.foreign_nodes,
           "span_s": scene.span_s, "naive_span_s": scene.naive_span_s, "dt_s": scene.dt_s,
           "refused": refused}
    if run is not None:
        doc.update({k: run[k] for k in ("lag", "blocks", "shrinkage", "pctl", "envelope",
                                        "reproducibility", "envelope_refused",
                                        "reproducibility_refused", "notes")})
    return json.dumps(doc, default=_jsonable, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Split-honest holdout envelope and alarm reproducibility over a scene.csv.")
    p.add_argument("path", help="a node's scene.csv")
    p.add_argument("--node", help="node name for S0/S1 files that do not carry one "
                                  "(default: the parent directory)")
    p.add_argument("--only-node", help="keep only rows labelled this node and count the rest; "
                                       "without it a mixed-node file is refused")
    p.add_argument("--mode", default="db", choices=("db", "q"),
                   help="db = q/2 + ref_db, as hear.pool.scene_matrix builds it (default)")
    p.add_argument("--limit", type=int, help="use only the first N decodable rows")
    p.add_argument("--shrinkage", type=float, default=DEFAULT_SHRINKAGE,
                   help="alpha toward (trace/d)I; a stated constant, not a fitted one "
                        "(default %.2f)" % DEFAULT_SHRINKAGE)
    p.add_argument("--lag-mult", type=float, default=3.0,
                   help="block_s = lag_mult x the measured lag (default 3.0)")
    p.add_argument("--block-s", type=float,
                   help="state the block length instead; still refused below the measured lag")
    p.add_argument("--max-lag-s", type=float, help="cap the lag search (default: span/4)")
    p.add_argument("--pctl", type=float, default=99.5, help="threshold percentile (default 99.5)")
    p.add_argument("--parts", type=int, default=3,
                   help="interleaved parts for reproducibility (default 3)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    scene: Optional[Scene] = None
    try:
        scene = read_scene(args.path, node=args.node, mode=args.mode, limit=args.limit,
                           only_node=args.only_node)
        run = run_reports(scene, shrinkage=args.shrinkage, lag_mult=args.lag_mult,
                          block_s=args.block_s, max_lag_s=args.max_lag_s,
                          pctl=args.pctl, parts=args.parts)
    except V.Refused as e:
        if args.json:
            # A refusal is a result, so --json still emits one rather than dropping to text: a
            # caller parsing stdout must not have to parse two formats to learn it was refused.
            print(as_json(scene, None, str(e)) if scene is not None
                  else json.dumps({"path": args.path, "refused": str(e)}, indent=2,
                                  sort_keys=True))
        else:
            if scene is not None:
                print(format_scene(scene))
            print("REFUSED: %s" % e)
        return 2
    print(as_json(scene, run, None) if args.json else format_run(scene, run))
    return 0 if (run["envelope"] and run["reproducibility"]) else 2


if __name__ == "__main__":
    sys.exit(main())
