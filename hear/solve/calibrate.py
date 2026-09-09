#!/usr/bin/env python3
"""Joint multi-event solve that ESTIMATES a per-node constant clock bias instead of assuming zero.

WHY THIS EXISTS. A node whose timestamps carry a fixed offset is not a noisy node -- it is a lying
node, and the two fail differently. Random scatter averages down over events and shows up in the
residual; a constant offset does neither. It moves that node's range by c*bias for every event
alike, and in an exactly-determined fit it relocates the answer without disturbing the residual at
all. Nothing downstream can see it.

The dama-gotchi phones are exactly this case. Their clock is fine -- GPSTimingSync anchors
(UTC, CLOCK_BOOTTIME) per GPS fix at 1-5 ms -- but the audio path stamps System.nanoTime() after
AudioRecord.read() returns, which adds the input-buffer and HAL latency of that particular handset:
tens of milliseconds, constant per device, never measured. At 343 m/s a 70 ms offset is 24 m.

THE WAY OUT IS REDUNDANCY ACROSS EVENTS, NOT ACROSS NODES. One event cannot separate "the source
was over there" from "this node's clock is late" -- both stretch the same range. But the source
MOVES between events and the bias does not, so over K events there are 2K+B unknowns (with the
source height declared) against K*(N-1) equations, and the biases become observable. Measured on
the 2-PPS-plus-3-phone layout: rank-deficient by one at K=1, full rank from K=2, condition number
around 10-100 thereafter. So this is not a regularisation trick or a prior -- the information is
genuinely in the data as soon as there is more than one event.

⚠️AT LEAST ONE NODE MUST BE TRUSTED. Bias is only defined relative to something. Adding a constant
to every node's clock is indistinguishable from shifting t0, which the mean-subtraction already
removes -- so if every node is allowed a bias the problem is rank-deficient by exactly one, for
ever, at any K. `biased` names which nodes float; the rest are the reference. Passing every node
raises rather than returning a confident answer off a singular fit.

⚠️THIS BUYS ACCURACY, NOT PRECISION. Removing a 70 ms bias does not make a 25 ms-scatter node into
a good one. It stops that node from dragging every answer in one direction; it does not stop it
from being noisy. Read `bias_sigma_ms` next to the estimate.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix

from . import placement as PL
from . import point as PT
from . import shockwave as SW


class CalibrationError(ValueError):
    """The layout or the data cannot support a bias estimate. Raised at the door, as everywhere
    else in this package, rather than decorating an answer nobody can check."""


def known_source_offset_budget(path_m: float, sigma_pos_m: float, sigma_node_t_s: float,
                               sigma_pick_t_s: float, sigma_temp_c: float = 1.0,
                               temp_c: float = 20.0, n_chirps: int = 1,
                               source_moves: bool = False) -> Dict:
    """What a KNOWN-SOURCE event buys for one node's constant audio-stamp offset.

    ⚠️THIS IS WHY THE PHONE CHIRP IS INSTRUMENTATION AND NOT A SEVENTH LABEL. `solve_multi` above
    separates bias from position only because the source MOVES between events, and it needs
    K >= 2 events plus a trusted node to do it. A dama-gotchi chirp is different in kind: its
    source POSITION and its emission INSTANT are both known, so one event gives the offset
    directly,

        beta = t_heard - t_emitted - d/c

    with no solve, no rank argument and no second event. The unknowns collapse the way
    `soundspeed.determines_speed(..., n_known_sources=n_events)` counts them.

    The four terms and how each behaves over N chirps:

      survey     sigma_pos / c        random only if the phone MOVES between chirps
      picking    sigma_pick           random, averages as 1/sqrt(N)
      node clock sigma_node_t         random, averages as 1/sqrt(N)
      sound speed (path/c)*0.176%/degC  COMMON MODE at a fixed path: never averages

    ⚠️SO STAND CLOSE. The sound-speed term is the only one proportional to path length, and it is
    the only one repetition cannot remove. At 5 m an unknown 1 degC is worth 25 us; at 36 m it is
    183 us. `soundspeed.separation_for_temperature` wants the OPPOSITE geometry -- a long
    separation -- which is why measuring c and measuring beta are two different standoffs and not
    one experiment.

    `source_moves=True` says the phone was re-surveyed at a different spot for each chirp, which
    is what turns the survey term from a bias into noise.
    """
    c = SW.sound_speed(temp_c)
    if path_m <= 0.0:
        raise CalibrationError("path must be > 0 m (got %r)" % path_m)
    if n_chirps < 1:
        raise CalibrationError("need >= 1 chirp (got %d)" % n_chirps)
    rt = math.sqrt(float(n_chirps))
    survey = float(sigma_pos_m) / c
    pick = float(sigma_pick_t_s)
    node = float(sigma_node_t_s)
    speed = (float(path_m) / c) * abs(float(sigma_temp_c)) * 0.606 / c
    single = math.sqrt(survey ** 2 + pick ** 2 + node ** 2 + speed ** 2)
    averaged = math.sqrt((survey / rt if source_moves else survey) ** 2
                         + (pick / rt) ** 2 + (node / rt) ** 2 + speed ** 2)
    return {
        "terms_s": {"survey": survey, "onset_pick": pick, "node_clock": node,
                    "sound_speed": speed},
        "sigma_single_s": single,
        "sigma_after_n_s": averaged,
        "n_chirps": int(n_chirps),
        "irreducible_s": speed if not source_moves else math.hypot(speed, 0.0),
        "path_m": float(path_m),
        "sound_speed_mps": c,
        "limiting_term": max({"survey": survey, "onset_pick": pick, "node_clock": node,
                              "sound_speed": speed}.items(), key=lambda kv: kv[1])[0],
    }


def _seed_grid(P: np.ndarray, margin_m: float, step_m: float, up_m: float) -> np.ndarray:
    lo, hi = P.min(axis=0) - margin_m, P.max(axis=0) + margin_m
    xs = np.arange(lo[0], hi[0] + step_m * 0.5, step_m)
    ys = np.arange(lo[1], hi[1] + step_m * 0.5, step_m)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    return np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, up_m)])


def solve_multi(positions: Sequence, arrivals: Sequence[Sequence[float]],
                biased: Sequence[bool], source_class: str, temp_c: float = 20.0,
                fixed_up_m: float = 0.0, search_margin_m: float = 500.0,
                grid_step_m: float = 10.0) -> Dict:
    """Fit K source positions and one constant clock bias per flagged node, together.

    `positions`  (N,3) east/north/up metres, one row per node, in one local frame.
    `arrivals`   (K,N) absolute seconds. NaN marks a node that did not hear that event.
    `biased`     (N,) bool. True = this node's clock offset is unknown and will be estimated.
                 At least one node must be False; that set defines the time reference.
    `fixed_up_m` the source height, DECLARED. Solving for it as well is possible in principle but
                 needs a non-coplanar array, and every array this was written for is flat.

    Returns the per-event positions, the estimated biases in milliseconds with their formal
    sigmas, and the usual observability verdicts.
    """
    if source_class in PT.CONE_CLASSES:
        raise ValueError("source class %r radiates off a Mach cone, not from a point"
                         % (source_class,))
    if source_class not in PT.POINT_CLASSES:
        raise ValueError("unknown source class %r" % (source_class,))

    P = PL._as3(positions)
    T = np.asarray(arrivals, float)
    if T.ndim != 2 or T.shape[1] != len(P):
        raise CalibrationError("arrivals must be (K, N) to match %d nodes; got %r"
                               % (len(P), (T.shape,)))
    b_mask = np.asarray(biased, bool)
    if b_mask.shape != (len(P),):
        raise CalibrationError("biased must be one flag per node")
    if b_mask.all():
        raise CalibrationError(
            "every node is flagged biased, which is rank-deficient by exactly one at any number "
            "of events: adding the same constant to every clock is indistinguishable from moving "
            "t0, and t0 is already marginalised out. At least one node must be the reference.")

    K, N = T.shape
    nb = int(b_mask.sum())
    heard = np.isfinite(T)
    per_event = heard.sum(axis=1)
    if np.any(per_event < 3):
        raise CalibrationError("every event needs >= 3 nodes that heard it; worst has %d"
                               % int(per_event.min()))
    n_eq = int((per_event - 1).sum())
    n_unk = 2 * K + nb
    if n_eq < n_unk:
        raise CalibrationError(
            "%d equations for %d unknowns (%d events x 2 + %d biases): add events, not nodes -- "
            "a bias is only separable from position because the source MOVES and it does not"
            % (n_eq, n_unk, K, nb))

    c = SW.sound_speed(temp_c)
    # Recentre per event. Arrivals are absolute epoch seconds where float64 spacing is 2.4e-7 s,
    # which is the same trap point.solve documents: differences survive it, absolutes do not.
    T0 = np.nanmin(T, axis=1, keepdims=True)
    Tc = T - T0

    G = _seed_grid(P, search_margin_m, grid_step_m, fixed_up_m)
    D = np.linalg.norm(G[:, None, :] - P[None, :, :], axis=2) / c
    seeds = np.empty((K, 2))
    for k in range(K):
        m = heard[k]
        rr = Tc[k][None, m] - D[:, m]
        rr = rr - rr.mean(axis=1, keepdims=True)
        seeds[k] = G[int(np.argmin((rr * rr).sum(axis=1)))][:2]

    bi = np.flatnonzero(b_mask)

    def _split(x):
        return x[:2 * K].reshape(K, 2), x[2 * K:]

    # Both the residual and the Jacobian are evaluated once per optimiser iteration, so a Python
    # loop over events inside them costs K times more than it looks. Measured with the loop in
    # place: 0.3 s at K=10, 2.0 s at K=20, 27.4 s at K=40 -- superlinear, and the whole point of
    # this routine is to use many events. Both are vectorised over events below.
    M = heard                                   # (K, N) bool
    NM = M.sum(axis=1, keepdims=True)           # nodes heard per event
    Tm = np.where(M, Tc, 0.0)

    def _masked_centre(R):
        """Subtract the per-event mean over the nodes that heard it, leaving absentees at zero.
        This IS the marginalisation of t0 -- exactly point._residual's mean subtraction, done per
        row and skipping the nodes that were not there."""
        R = np.where(M, R, 0.0)
        return np.where(M, R - R.sum(axis=1, keepdims=True) / NM, 0.0)

    def _ranges(S):
        s3 = np.column_stack([S, np.full(len(S), fixed_up_m)])       # (K,3)
        d = s3[:, None, :] - P[None, :, :]                            # (K,N,3)
        return d, np.maximum(np.linalg.norm(d, axis=2), 1e-3)         # floor: see jac()

    def resid(x):
        S, b = _split(x)
        full_b = np.zeros(N)
        full_b[bi] = b * 1e-3
        _, n = _ranges(S)
        return _masked_centre(Tm - full_b[None, :] - n / c).ravel()

    # Sparsity pattern, built ONCE. Event k's residuals touch only its own two position columns
    # plus the bias columns, so the Jacobian is mostly zeros; handing scipy a dense one makes it
    # SVD the lot every step. (An lil_matrix built element-by-element in a Python loop was tried
    # and was slower than dense -- the assignment cost more than the arithmetic it saved.)
    KK, II = np.nonzero(M)                       # heard (event, node) pairs, row-major
    ROW = KK * N + II
    rows_p = np.repeat(ROW, 2)
    cols_p = np.empty(2 * ROW.size, dtype=int)
    cols_p[0::2] = 2 * KK
    cols_p[1::2] = 2 * KK + 1
    rows_b, cols_b, sel_b = [], [], []
    for col, node in enumerate(bi):
        sel = M[KK, node]        # this bias only enters events where that node was actually heard
        rows_b.append(ROW[sel])
        cols_b.append(np.full(int(sel.sum()), 2 * K + col))
        sel_b.append(sel)
    ROWS = np.concatenate([rows_p] + rows_b)
    COLS = np.concatenate([cols_p] + cols_b)

    # The bias block does not depend on the parameters at all -- it is linear in b -- so it is
    # computed once here rather than on every iteration.
    inv_nm = (1.0 / NM[:, 0])[KK]
    bias_vals = []
    for col, node in enumerate(bi):
        v = (inv_nm - (II == node).astype(float)) * 1e-3
        bias_vals.append(v[sel_b[col]])

    def jac(x):
        S, _ = _split(x)
        d, n = _ranges(S)
        # d|s-P|/ds is the unit vector from P to s; the mean subtraction carries through linearly.
        Jp = -d[:, :, :2] / (n[:, :, None] * c)
        Jp = np.where(M[:, :, None], Jp, 0.0)
        Jp = np.where(M[:, :, None],
                      Jp - Jp.sum(axis=1, keepdims=True) / NM[:, :, None], 0.0)
        vals = np.concatenate([Jp[KK, II, :].ravel()] + bias_vals)
        return coo_matrix((vals, (ROWS, COLS)), shape=(K * N, n_unk)).tocsr()

    # Bound the positions to the region the seed grid scanned, for the reason point.solve
    # documents: an unbounded TDoA fit slides along a flat direction and returns a confident
    # coordinate from outside the searched box. Biases are bounded generously -- a second of
    # offset is far past anything an audio path can produce and still stops a runaway.
    plo = np.tile(P.min(axis=0)[:2] - search_margin_m, K)
    phi = np.tile(P.max(axis=0)[:2] + search_margin_m, K)
    lo = np.concatenate([plo, np.full(nb, -1000.0)])
    hi = np.concatenate([phi, np.full(nb, 1000.0)])
    x0 = np.clip(np.concatenate([seeds.ravel(), np.zeros(nb)]), lo, hi)
    fit = least_squares(resid, x0, jac=jac, bounds=(lo, hi), tr_solver="lsmr",
                        xtol=1e-14, ftol=1e-14, gtol=1e-14)
    S, b_ms = _split(fit.x)

    # Formal sigma from the Jacobian at the solution, scaled by the observed residual. This is the
    # fit's own opinion of its precision and it assumes the model is right; it is reported so a
    # caller can see when a bias is merely poorly determined rather than genuinely small.
    J = fit.jac
    J = J.toarray() if hasattr(J, "toarray") else np.asarray(J)
    dof = max(n_eq - n_unk, 1)
    s2 = float(fit.fun @ fit.fun) / dof
    try:
        cov = np.linalg.inv(J.T @ J) * s2
        sig = np.sqrt(np.clip(np.diag(cov)[2 * K:], 0, None))
        rank_ok = np.linalg.matrix_rank(J, tol=1e-9) == n_unk
    except np.linalg.LinAlgError:
        sig = np.full(nb, float("inf"))
        rank_ok = False

    bias_full = np.zeros(N)
    bias_full[bi] = b_ms
    sig_full = np.zeros(N)
    sig_full[bi] = sig
    return {
        "positions_m": [(float(S[k, 0]), float(S[k, 1]), float(fixed_up_m)) for k in range(K)],
        "bias_ms": [float(v) for v in bias_full],
        "bias_sigma_ms": [float(v) for v in sig_full],
        "bias_range_m": [float(v * 1e-3 * c) for v in bias_full],
        "biased_nodes": [int(i) for i in bi],
        "observable": bool(rank_ok),
        "n_events": K, "n_nodes": N, "n_equations": n_eq, "n_unknowns": n_unk,
        "rms_residual_ms": math.sqrt(float(fit.fun @ fit.fun) / n_eq) * 1000.0,
        "up_assumed_m": float(fixed_up_m),
        "sound_speed_mps": c,
        "source_class": source_class,
        "note": None if rank_ok else
                "the joint fit is rank deficient: the biases are not separable from the positions "
                "for this layout and event set. More EVENTS, spread over different bearings, is "
                "the fix -- more nodes is not.",
    }
