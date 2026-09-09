"""Falsifiability set for `hear.validate`.

This repo has shipped several checks that could not fail -- `check_array` published at 8.3% with
no null to compare it against, `interval_consistent` whose 2d/c bound is 58.7-115.9 ms against
~2 ms of onset scatter so every pair passes on every burst, a three-node fit whose residual is
~0 by construction. `hear/validate.py` exists to catch that shape of bug, so every check it makes
needs a NULL CONTROL here proving the check is capable of coming out badly.

Every arm below is paired:
  - a pure-noise scorer must come out at Jaccard 0.023 -- 0.003 BELOW the 0.026 two independent
    flaggers at its own 5% rates would score by chance -- and a stable one at 0.832 against the
    same 0.028 baseline, so the metric is neither always-low nor always-high AND is read against
    its own denominator: the raw number alone reaches 1.000 on white noise at n ~ d;
  - a scorer that flags NOTHING and a scorer that flags EVERYTHING must both be REFUSED rather
    than scoring 1.000;
  - a stationary corpus must land on its design rate at every cut (1.08/0.69/0.59/0.69/0.88%
    against 1.00%, spread 1.8x) and a drifting one must spread (32.2/18.7/6.9/4.0/3.1%, 10.3x),
    so the envelope is neither always-flat nor always-alarming;
  - a corpus that remembers only its LEVEL must not be read as decorrelating in one row, and one
    that remembers only its DIRECTION must still be read off that, so neither channel of the lag
    estimator is allowed to be the whole answer;
  - a correlation curve that DIPS below rho and comes back up must not be read as a crossing;
  - a split whose blocks are not actually separated must be refused wherever it is used, not
    only where it is built;
  - and the mutant: the row-index split that `make_blocks` refuses reports Jaccard 0.857 and
    rates of 0.9%/1.0% against a 1% design on the SAME corpus where the honest block split reads
    0.514 and 29.3%/32.5%. The refused split is the one that looks good. That is the whole
    reason the refusal is a refusal and not a warning.

The synthetic generators live here and not in the module on purpose: a null generator that ships
in the library gets reused as a fixture and stops being a null.
"""
import hashlib
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import validate as V                                    # noqa: E402

T0 = 1.7e9                       # an arbitrary but anchored wall clock
DT = 1.0                         # one row per second; the real scene row is 1.024 s
FIT = dict(fit=V.mahalanobis_fit, score=V.mahalanobis_score)


# --------------------------------------------------------------------------- generators

def iid(n=8000, d=8, seed=7, dt=DT):
    """The stationary control. Lag-1 cosine 0.005: it decorrelates in one row."""
    return np.random.RandomState(seed).normal(size=(n, d)), T0 + dt * np.arange(n)


def ar1(n=6000, d=16, a=0.98, rho0=0.98, seed=1, dt=DT):
    """A corpus with a KNOWN decorrelation lag, shaped like the measured drain.

    row_i = w_i + noise, w an AR(1) with coefficient `a` per dimension and unit variance, the
    iid part sized so the lag-1 cosine is `rho0 * a`. With a=rho0=0.98 that is 0.96, which is the
    lag-1 cosine measured on nyquist's scene.csv. The cosine at lag k is rho0 * a**k, so it
    crosses 0.5 at k = ln(0.5/rho0)/ln(a) = 33.3 rows -- a number this file can assert against
    instead of a number the estimator chose for itself.
    """
    rs = np.random.RandomState(seed)
    w = np.empty((n, d))
    w[0] = rs.normal(size=d)
    e = rs.normal(size=(n, d)) * np.sqrt(1.0 - a * a)
    for i in range(1, n):
        w[i] = a * w[i - 1] + e[i]
    X = w + np.sqrt(1.0 / rho0 - 1.0) * rs.normal(size=(n, d))
    return X, T0 + dt * np.arange(n)


def ar1_lag_rows(a=0.98, rho0=0.98, rho=0.5):
    return float(np.log(rho / rho0) / np.log(a))


def drifting(c=10.0, **kw):
    """The inverted control: the stationary corpus with a linear ramp on one dimension.

    c=10 sigma over the span is the synthetic stand-in for the drain's evening transition
    (22:26->00:30), which produced 78.9 / 5.8 / 2.7 / 0.0 / 0.0 % across the same five cuts.

    ITS MEASURED LAG IS NOT `iid`'s. The ramp is carried in the row NORM, so the level channel of
    `decorrelation_lag_s` sees it and the corpus measures 208 s where the stationary one measures
    1 s -- a monotone trend has no decorrelation lag inside its own span, and the estimator says
    so instead of reporting the direction channel's 1 row. Every drifting arm below therefore
    states `block_s` above that, and `DRIFT_BLOCK_S` is shared so the two arms of each pair are
    compared at the same block length.
    """
    X, t = iid(**kw)
    X = X.copy()
    X[:, 0] += c * np.arange(X.shape[0]) / X.shape[0]
    return X, t


DRIFT_BLOCK_S = 400.0            # above the 208 s the ramp's level channel measures


def settling(frac_spiky=0.5, rate=0.02, amp=8.0, seed=99, **kw):
    """A corpus whose EARLIEST rows carry rare outliers: every fit over-covers its own future, so
    the newest rows exceed NOTHING. That is a real 0.0% rate, which is why `spread_x` is None
    rather than inf.

    The outliers are single rows, 2% of the first half, so the corpus still decorrelates in one
    row and can be blocked 40 ways. The obvious alternative -- a monotonically decaying amplitude
    -- cannot be used here: a global trend is carried in the row norm, so the level channel of
    `decorrelation_lag_s` measures a lag proportional to the span (758 s on 8000 rows), and no
    corpus with a global trend can ever be cut into the 20 blocks five distinct cut points need.
    """
    X, t = iid(**kw)
    X = X.copy()
    k = int(frac_spiky * X.shape[0])
    X[:k][np.random.RandomState(seed).rand(k) < rate] += amp
    return X, t


def level_only(n=6000, d=8, tau=500.0, seed=11, dt=DT):
    """NO direction memory, a LONG level memory: the corpus a cosine-only estimator cannot see.

    Each row is a random unit direction scaled by an AR(1) level with a 500-row time constant.
    The mean cosine between mean-removed rows is ~0 at every lag because the directions are
    independent, while the row NORM -- which is what a Mahalanobis distance mostly reads -- is
    correlated at 0.998 one row apart. An estimator that measured only the cosine returned 1.02 s
    here and sized blocks 300x short, and a block shorter than the true lag does not make these
    diagnostics noisy, it makes them pass.
    """
    rs = np.random.RandomState(seed)
    a = float(np.exp(-1.0 / tau))
    lev = np.zeros(n)
    e = rs.normal(size=n) * np.sqrt(1.0 - a * a)
    for i in range(1, n):
        lev[i] = a * lev[i - 1] + e[i]
    u = rs.normal(size=(n, d))
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    return (3.0 + lev)[:, None] * u, T0 + dt * np.arange(n)


def alternating(n=6000, d=8, a=0.99, amp=2.0, seed=13, dt=DT):
    """A corpus whose correlation curve DIPS below rho at lag 1 and comes back up at lag 2.

    An AR(1) with a fixed vector added with alternating sign, so odd lags are pulled down and
    even lags pushed up: measured cos 0.305 at lag 1, 0.985 at lag 2, 0.294 at 3, 0.971 at 4. A
    first-crossing rule reads 1 row off this; the corpus plainly has not decorrelated. The sketch
    corpus is the real instance -- 0.508 at lag 1 event from 27 pairs, 0.698 at lag 3 -- where a
    0.02 move in rho moved the reported lag 172x.
    """
    rs = np.random.RandomState(seed)
    w = np.zeros((n, d))
    e = rs.normal(size=(n, d)) * np.sqrt(1.0 - a * a)
    for i in range(1, n):
        w[i] = a * w[i - 1] + e[i]
    v = np.zeros(d)
    v[0] = amp
    return w + ((-1) ** np.arange(n))[:, None] * v, T0 + dt * np.arange(n)


def wide(n=900, d=160, seed=3, dt=DT):
    """n ~ d: the regime `modules/supersonic/validate_sketch.py` actually runs in (n_a 45-75 for
    d=160). Pure noise. Two Mahalanobis fits of it put EVERY holdout row over their own 99.5th
    percentile, so the alarm sets are identical by construction and the raw Jaccard is 1.000."""
    return np.random.RandomState(seed).normal(size=(n, d)), T0 + dt * np.arange(n)


def noise_pair():
    """A scorer with no relationship to the audio at all: the fit is a seed, the score is noise.

    Deterministic across processes -- sha256, not `hash()`, whose bytes hashing is randomised by
    PYTHONHASHSEED and would make this null irreproducible.
    """
    def fit(X_fit, y=None):
        return int.from_bytes(hashlib.sha256(np.ascontiguousarray(X_fit).tobytes()).digest()[:4],
                              "big")

    def score(model, X):
        return np.random.RandomState(model).normal(size=X.shape[0])
    return dict(fit=fit, score=score)


def silent_pair():
    """A scorer that never alarms. Its two fits agree on every row, vacuously."""
    return dict(fit=lambda X_fit, y=None: 0.0,
                score=lambda m, X: np.zeros(X.shape[0]))


def row_index_blocks(X, t, lag_s=31.0):
    """THE MUTANT: one block per row, no guard -- what a naive `Blocks` looks like.

    Built by hand precisely because `make_blocks` refuses to produce it. Round-robin over these
    puts each row's immediate neighbours in the other fit and in the holdout, so two 'independent'
    fits see the same autocorrelated seconds.

    It declares the corpus's REAL lag (31 rows on `ar1`), because that is what a naive caller
    would carry in from `decorrelation_lag_s` before ignoring it. Declaring `lag_s=DT` instead
    would make the mutant separated against its own understated yardstick -- the same
    number-kept/meaning-lost move the module is about -- and `separation_check` would have no way
    to see it. Every use below therefore has to pass `allow_unseparated=True`, which is the point:
    the flattering numbers are reachable only by asking for them.
    """
    n = X.shape[0]
    return V.Blocks(t=t, row=np.arange(n), block=np.arange(n), block_s=float(DT),
                    lag_s=float(lag_s), guard_s=0.0, n_blocks=n, lag_censored=False, n_in=n,
                    dropped_unanchored=0, dropped_short_block=0, dropped_guard=0)


# --------------------------------------------------------------------------- the lag

class TestDecorrelationLag:
    def test_it_recovers_a_known_ar1_lag(self):
        # The generator's cosine crosses 0.5 at 33.3 rows; measured 31 rows / 31.0 s.
        X, t = ar1()
        got = V.decorrelation_lag_s(X, t)
        want = ar1_lag_rows()
        assert not got["censored"]
        assert 0.6 * want <= got["lag_rows"] <= 1.6 * want, got["lag_rows"]
        assert got["lag_s"] == pytest.approx(got["lag_rows"] * DT, rel=0.05)
        assert got["cos_lag1"] == pytest.approx(0.96, abs=0.03)

    def test_the_curve_falls(self):
        X, t = ar1()
        cos = [c["cos"] for c in V.decorrelation_lag_s(X, t)["curve"]]
        assert cos[0] > 0.9 and cos[-1] < 0.2

    def test_an_iid_corpus_decorrelates_in_one_row(self):
        # The other arm: an estimator that always returns a long lag would pass the AR test too.
        got = V.decorrelation_lag_s(*iid())
        assert got["lag_rows"] == 1 and abs(got["cos_lag1"]) < 0.05

    def test_it_reports_censored_rather_than_extrapolating(self):
        # The drain's own condition: cosine 0.70 at 10 min, no 0.5 crossing inside the sample.
        X, t = ar1()
        got = V.decorrelation_lag_s(X, t, max_lag_s=10.0)
        assert got["censored"] and got["lag_s"] == 10.0 and got["cos_max_lag"] > 0.5

    def test_the_cap_defaults_to_a_quarter_of_the_span(self):
        X, t = ar1(n=2000)
        got = V.decorrelation_lag_s(X, t)
        assert got["max_lag_s"] == pytest.approx(got["span_s"] / 4.0)

    def test_one_timestamp_is_refused(self):
        X = np.random.RandomState(0).normal(size=(50, 4))
        with pytest.raises(V.Refused, match="span is 0 s"):
            V.decorrelation_lag_s(X, np.full(50, T0))

    def test_it_is_reproducible(self):
        X, t = ar1()
        a, b = V.decorrelation_lag_s(X, t), V.decorrelation_lag_s(X, t)
        assert a["lag_rows"] == b["lag_rows"] and a["curve"] == b["curve"]


# --------------------------------------------------------------------------- blocks

class TestBlocks:
    def test_the_row_accounting_closes(self):
        X, t = ar1()
        b = V.make_blocks(X, t)
        assert b.n_in == b.row.size + b.dropped_unanchored + b.dropped_short_block \
            + b.dropped_guard
        assert b.dropped_guard > 0 and b.n_blocks > 2

    def test_unanchored_rows_are_dropped_and_counted_not_an_error(self):
        # 56.1% of mach is pre-PPS. That is not a bug, and it must not be an exception either.
        X, t = ar1()
        t = t.copy()
        t[::2] = np.nan
        b = V.make_blocks(X, t, lag_s=31.0, block_s=93.0)
        assert b.dropped_unanchored == 3000
        assert b.anchored_frac == pytest.approx(0.5)
        assert b.n_in == b.row.size + b.dropped_unanchored + b.dropped_short_block \
            + b.dropped_guard

    def test_a_zero_utc_is_unanchored_not_1970(self):
        # Reading utc_us naively as a number puts 56.1% of mach at 1970 and reports a 56-year
        # span; the blocks would then be one enormous 1970 block plus the real ones.
        X, t = ar1(n=1000)
        t = t.copy()
        t[:400] = 0.0
        b = V.make_blocks(X, t, lag_s=31.0, block_s=93.0)
        assert b.dropped_unanchored == 400
        assert float(b.t.max() - b.t.min()) < 1000.0

    def test_rows_in_different_blocks_are_at_least_one_lag_apart(self):
        # The guard band's entire job, asserted directly rather than inferred from a metric.
        X, t = ar1()
        b = V.make_blocks(X, t)
        order = np.argsort(b.t)
        ts, bl = b.t[order], b.block[order]
        cross = np.nonzero(np.diff(bl) != 0)[0]
        assert cross.size > 0
        assert float(np.min(ts[cross + 1] - ts[cross])) >= b.lag_s

    def test_without_the_guard_neighbouring_rows_cross_the_boundary(self):
        # The null control for the test above: with guard=False it FAILS, so the assertion is
        # measuring the guard and not the block arithmetic.
        X, t = ar1()
        b = V.make_blocks(X, t, guard=False)
        order = np.argsort(b.t)
        ts, bl = b.t[order], b.block[order]
        cross = np.nonzero(np.diff(bl) != 0)[0]
        assert float(np.min(ts[cross + 1] - ts[cross])) < b.lag_s
        assert b.guard_s == 0.0 and b.dropped_guard == 0

    def test_a_gap_produces_absent_blocks_not_a_stretched_one(self):
        X, t = iid(n=1200, d=4)
        t = t.copy()
        t[600:] += 5000.0                       # an outage in the middle
        b = V.make_blocks(X, t, lag_s=1.0, block_s=100.0)
        ids = b.ids
        assert ids.size == b.n_blocks
        assert np.diff(ids).max() > 1            # ids are simply absent across the outage
        for i in ids:
            span = float(np.ptp(b.t[b.block == i]))
            assert span < b.block_s

    def test_block_shorter_than_the_measured_lag_is_refused(self):
        X, t = ar1()
        with pytest.raises(V.Refused, match="shorter than the measured decorrelation lag"):
            V.make_blocks(X, t, block_s=10.0)

    def test_the_three_second_fold_from_train_sketch_is_exactly_what_it_refuses(self):
        # modules/supersonic/train_sketch.py groups CV folds by int(utc // 3). Correct for a
        # firing string; ~200x too short for scene rows whose measured decorrelation is ~10 min.
        X, t = ar1()
        with pytest.raises(V.Refused, match="pass block_s >= "):
            V.make_blocks(X, t, block_s=3.0)

    def test_a_censored_lag_cannot_size_a_block(self):
        # A lag the sample could not measure cannot size the block that sample is split by. This
        # is the drain's own condition: cosine still 0.70 at 10 min, no crossing inside 2 h.
        X, t = ar1()
        assert V.decorrelation_lag_s(X, t, max_lag_s=10.0)["censored"]
        with pytest.raises(V.Refused, match="CENSORED"):
            V.make_blocks(X, t, max_lag_s=10.0)

    def test_a_censored_lag_still_accepts_an_explicit_block_at_the_bound(self):
        # The escape hatch, and the reason the refusal is not a dead end: the bound is a real
        # lower limit on the lag, so a block at least that long is defensible. `lag_censored`
        # rides along so a downstream reader knows the block was sized against a bound.
        X, t = ar1()
        b = V.make_blocks(X, t, max_lag_s=10.0, block_s=30.0)
        assert b.lag_censored is True and b.lag_s == 10.0 and b.guard_s == 10.0
        assert "CENSORED" in V.format_report(None, None, b)

    def test_the_guard_cannot_eat_the_whole_block(self):
        X, t = ar1()
        with pytest.raises(V.Refused, match="erodes the whole"):
            V.make_blocks(X, t, lag_s=31.0, block_s=31.0)

    def test_one_block_is_refused(self):
        X, t = iid(n=300, d=4)
        with pytest.raises(V.Refused, match="every kept row landed in"):
            V.make_blocks(X, t, lag_s=1.0, block_s=100000.0)

    def test_no_anchored_rows_is_refused(self):
        X, t = iid(n=300, d=4)
        with pytest.raises(V.Refused, match="no anchored rows"):
            V.make_blocks(X, np.full(300, np.nan))
        with pytest.raises(V.Refused, match="no anchored rows"):
            V.make_blocks(X, np.zeros(300))

    def test_one_distinct_anchored_time_is_refused(self):
        X, t = iid(n=300, d=4)
        tt = np.full(300, np.nan)
        tt[:10] = T0
        with pytest.raises(V.Refused, match="distinct anchored time"):
            V.make_blocks(X, tt)

    def test_shape_and_finiteness_are_refused_not_absorbed(self):
        X, t = iid(n=100, d=4)
        with pytest.raises(V.Refused, match="does not match"):
            V.make_blocks(X, t[:50])
        bad = X.copy()
        bad[3, 1] = np.inf
        with pytest.raises(V.Refused, match="non-finite"):
            V.make_blocks(bad, t)
        with pytest.raises(V.Refused, match="0 rows"):
            V.make_blocks(np.zeros((0, 4)), np.zeros(0))

    def test_it_is_reproducible(self):
        X, t = ar1()
        a, b = V.make_blocks(X, t), V.make_blocks(X, t)
        assert a.block_s == b.block_s and a.lag_s == b.lag_s and a.n_blocks == b.n_blocks
        assert np.array_equal(a.row, b.row) and np.array_equal(a.block, b.block)


# --------------------------------------------------------------------------- envelope

class TestHoldoutEnvelope:
    def test_a_stationary_corpus_lands_on_its_design_rate_at_every_cut(self):
        # The decoy analogue. Measured 1.08 / 0.69 / 0.59 / 0.69 / 0.88 % against a 1.00% design,
        # spread 1.8x. If the envelope spread here it would be measuring itself.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["designed"] == pytest.approx(0.01)
        assert len(env["draws"]) == 5 and env["refused"] == []
        assert env["max"] < 4.0 * env["designed"]
        assert env["spread_x"] < 4.0 and not env["min_is_zero"]

    def test_a_drifting_corpus_spreads_across_cut_points(self):
        # The inverted control, proving the envelope can move: measured 32.2 / 18.7 / 6.9 / 4.0 /
        # 3.1 % against the same 1.00% design, spread 10.3x. Same generator, one added ramp, and
        # the same 400 s block the stationary arm reads 1.8x at.
        X, t = drifting()
        b = V.make_blocks(X, t, block_s=DRIFT_BLOCK_S)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["spread_x"] > 8.0
        assert env["max"] > 10.0 * env["designed"]
        assert env["draws"][0]["exceed"] > env["draws"][-1]["exceed"]

    def test_the_two_corpora_differ_by_more_than_the_estimator_noise(self):
        # Neither arm alone proves anything; the gap between them is the measurement.
        b_s = V.make_blocks(*iid(), block_s=DRIFT_BLOCK_S)
        b_d = V.make_blocks(*drifting(), block_s=DRIFT_BLOCK_S)
        s = V.holdout_envelope(iid()[0], b_s, pctl=99.0, **FIT)
        d = V.holdout_envelope(drifting()[0], b_d, pctl=99.0, **FIT)
        assert d["max"] > 20.0 * s["max"]

    def test_spread_is_none_when_the_best_cut_exceeded_nothing(self):
        # 0.0% is a real rate. inf is not a spread, and a stored inf reads downstream as a bug.
        X, t = settling()
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["min"] == 0.0 and env["min_is_zero"] is True and env["spread_x"] is None
        assert "n/a" in V.format_report(env, None, b)

    def test_a_refused_cut_is_counted_not_dropped(self):
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=200.0)          # 10 blocks
        env = V.holdout_envelope(X, b, pctl=99.0, min_blocks_fit=6, min_blocks_holdout=2, **FIT)
        assert [r["frac"] for r in env["refused"]] == [0.5, 0.9]
        assert [d["frac"] for d in env["draws"]] == [0.6, 0.7, 0.8]
        assert len(env["draws"]) + len(env["refused"]) == 5

    def test_it_re_raises_only_when_no_cut_survived(self):
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=200.0)
        with pytest.raises(V.Refused, match="no cut point survived"):
            V.holdout_envelope(X, b, pctl=99.0, min_blocks_fit=50, **FIT)

    def test_the_cut_lands_on_a_block_boundary_not_at_frac_times_total(self):
        # Every block here holds the same number of kept rows, so a boundary cut is divisible by
        # it and a row-count cut is not. frac keeps its row meaning; the unit stays a block.
        X, t = iid(n=8000, d=4)
        b = V.make_blocks(X, t, lag_s=1.0, block_s=200.0)
        per = int((b.block == b.ids[0]).sum())
        cal = V.holdout_calibration(X, b, frac=0.53, pctl=99.0, **FIT)
        assert cal["n_fit"] % per == 0
        assert cal["n_fit"] + cal["n_holdout"] == b.row.size
        assert cal["blocks_fit"] + cal["blocks_holdout"] == b.n_blocks
        assert cal["n_fit"] != int(0.53 * b.row.size)

    def test_calibration_refuses_a_frac_outside_the_open_unit_interval(self):
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=200.0)
        for f in (0.0, 1.0, 1.5):
            with pytest.raises(V.Refused, match="frac must be in"):
                V.holdout_calibration(X, b, frac=f, **FIT)

    def test_it_is_reproducible(self):
        X, t = drifting()
        b = V.make_blocks(X, t, block_s=DRIFT_BLOCK_S)
        a = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        c = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert a == c


# --------------------------------------------------------------------------- reproducibility

class TestAlarmReproducibility:
    def test_a_pure_noise_scorer_almost_never_agrees_with_itself(self):
        # THE LOW ARM. Both fits alarm at very nearly the designed 5% -- a perfectly calibrated
        # non-detector -- and their alarm sets overlap at Jaccard 0.023, which is 0.003 BELOW
        # what two independent flaggers at those rates score by chance. Nothing in the envelope
        # can see this; only reproducibility can.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **noise_pair())
        assert rep["rate_a"] == pytest.approx(0.05, abs=0.01)
        assert rep["rate_b"] == pytest.approx(0.05, abs=0.01)
        assert rep["jaccard"] < 0.15, rep["jaccard"]
        assert abs(rep["pearson"]) < 0.15

    def test_the_envelope_cannot_see_the_noise_scorer_at_all(self):
        # Why the two reports are two. The pure-noise scorer's holdout exceedance sits on its
        # design rate at every cut: calibration is not evidence of detection.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **noise_pair())
        assert env["max"] < 2.0 * env["designed"]

    def test_a_stable_scorer_does_agree_with_itself(self):
        # THE HIGH ARM, without which "low Jaccard" would only prove the metric is always low.
        # Measured 0.832 at pctl=95 on the same corpus and the same block split.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert rep["jaccard"] > 0.7, rep["jaccard"]
        assert rep["pearson"] > 0.9

    def test_a_scorer_that_flags_nothing_is_refused_not_scored_one(self):
        # Diverges from the hugbot source, which returns jaccard=1.0 here. A model that never
        # alarms is perfectly reproducible and perfectly useless; a stored 1.0 travels downstream
        # and reads as perfect agreement. Those must not read alike.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        with pytest.raises(V.Refused, match="agree vacuously"):
            V.alarm_reproducibility(X, b, **silent_pair())

    def test_two_parts_leave_no_shared_holdout(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        with pytest.raises(V.Refused, match="no shared holdout"):
            V.alarm_reproducibility(X, b, parts=2, **FIT)

    def test_too_few_blocks_is_refused_with_both_numbers(self):
        X, t = iid(n=1600, d=4)
        b = V.make_blocks(X, t, block_s=200.0)          # 8 blocks
        with pytest.raises(V.Refused, match="8 blocks available, 12 required"):
            V.alarm_reproducibility(X, b, **FIT)

    def test_blocks_per_dim_is_printed_rather_than_assumed(self):
        # The source's min_part=DIM floor with its observation unit swapped from records to
        # blocks -- not a different rule, which is why `blocks_per_cov_param` rides alongside it.
        # Under this module's defaults a block on the drain is 42.6 min, so a fortnight of one
        # node is 473 blocks against 3240 covariance parameters.
        X, t = iid(n=8000, d=8)
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert rep["blocks_per_dim"] == pytest.approx(min(rep["blocks_a"], rep["blocks_b"]) / 8.0)
        assert rep["blocks_a"] + rep["blocks_b"] + rep["blocks_holdout"] == b.n_blocks
        assert rep["n_a"] + rep["n_b"] + rep["n_holdout"] == b.row.size

    def test_the_two_fits_are_disjoint_and_neither_touches_the_holdout(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        ids = b.ids
        rank = np.arange(ids.size) % 3
        for a_id in ids[rank == 0]:
            assert a_id not in set(ids[rank == 1]) | set(ids[rank >= 2])

    def test_it_is_reproducible(self):
        X, t = ar1()
        b = V.make_blocks(X, t)
        assert V.alarm_reproducibility(X, b, pctl=99.0, **FIT) == \
            V.alarm_reproducibility(X, b, pctl=99.0, **FIT)


# --------------------------------------------------------------------------- the mutant

class TestTheRefusedSplitIsTheFlatteringOne:
    """The refusal in `make_blocks` earns its place only if the split it refuses reports BETTER
    numbers than the honest one. On the AR(1) corpus (lag-1 cosine 0.96, crossing 0.5 at 33 rows,
    measured 31):

        split                 Jaccard   excess   rate_a   rate_b   (design 1.00%)
        block, guarded        0.514    +0.332    29.3%    32.5%
        row index (mutant)    0.857    +0.852     0.9%     1.0%

    Both numbers move the reassuring way. A reader handed the mutant's output would conclude the
    model is well calibrated AND highly reproducible, on a corpus where it is neither. Note that
    the chance correction does NOT rescue the reader here -- the mutant's excess over chance is
    higher too, because its rates are low. What catches this split is separation, measured out of
    the times, which is why every arm below has to ask for `allow_unseparated=True` by name.
    """

    def test_the_row_index_split_reports_a_jaccard_the_block_split_does_not(self):
        X, t = ar1()
        honest = V.alarm_reproducibility(X, V.make_blocks(X, t), pctl=99.0, **FIT)
        mutant = V.alarm_reproducibility(X, row_index_blocks(X, t), pctl=99.0,
                                        allow_unseparated=True, **FIT)
        assert mutant["jaccard"] > 0.85, mutant["jaccard"]
        assert honest["jaccard"] < 0.65, honest["jaccard"]
        assert mutant["jaccard"] - honest["jaccard"] > 0.3

    def test_the_row_index_split_also_looks_perfectly_calibrated(self):
        X, t = ar1()
        honest = V.alarm_reproducibility(X, V.make_blocks(X, t), pctl=99.0, **FIT)
        mutant = V.alarm_reproducibility(X, row_index_blocks(X, t), pctl=99.0,
                                        allow_unseparated=True, **FIT)
        assert mutant["rate_a"] < 3.0 * mutant["designed"]
        assert honest["rate_a"] > 10.0 * honest["designed"]

    def test_make_blocks_refuses_to_build_the_split_that_reports_it(self):
        X, t = ar1()
        with pytest.raises(V.Refused, match="shorter than the measured decorrelation lag"):
            V.make_blocks(X, t, block_s=DT)

    def test_the_refusal_names_both_numbers_so_a_reader_can_argue_with_it(self):
        X, t = ar1()
        with pytest.raises(V.Refused) as e:
            V.make_blocks(X, t, block_s=5.0)
        msg = str(e.value)
        assert "block_s=5.0s" in msg and "cosine" in msg and "pass block_s >=" in msg

    def test_the_honest_jaccard_is_stable_across_legitimate_block_lengths(self):
        # If the honest number were merely a different arbitrary number, it would wander as much
        # as the mutant differs. Measured 0.518 / 0.510 / 0.461 at 62 / 93 / 200 s.
        X, t = ar1()
        js = [V.alarm_reproducibility(X, V.make_blocks(X, t, lag_s=31.0, block_s=bs),
                                      pctl=99.0, **FIT)["jaccard"]
              for bs in (62.0, 93.0, 200.0)]
        assert max(js) - min(js) < 0.2, js
        assert max(js) < 0.7


# --------------------------------------------------------------------------- reference model

class TestMahalanobis:
    def test_a_well_conditioned_corpus_never_touches_the_floor(self):
        # The drain measured rank 80/80, lambda_min +0.186, 0% of directions set by the floor --
        # unlike hugbot, where 43.2% were. If this ever starts flooring, the metric has changed
        # meaning and eps stops being a numerical guard.
        m = V.mahalanobis_fit(iid(n=4000, d=8)[0])
        assert m["eps_floor_frac"] == 0.0 and m["lam_min"] > 0.1

    def test_a_dead_dimension_is_floored_and_says_so(self):
        X = iid(n=4000, d=8)[0].copy()
        X[:, 3] = 0.0
        m = V.mahalanobis_fit(X)
        assert m["eps_floor_frac"] == pytest.approx(1.0 / 8.0)
        assert np.isfinite(V.mahalanobis_score(m, X)).all()

    def test_it_needs_two_rows(self):
        with pytest.raises(V.Refused, match=">= 2 rows"):
            V.mahalanobis_fit(np.zeros((1, 4)))

    def test_the_score_rises_with_distance_from_the_fitted_normal(self):
        X = iid(n=4000, d=8)[0]
        m = V.mahalanobis_fit(X)
        far = np.vstack([X[:1] * 0.0 + 12.0, X[:1] * 0.0])
        s = V.mahalanobis_score(m, far)
        assert s[0] > s[1] and s[0] > 20.0


# --------------------------------------------------------------------------- the pool seam

def _scene_csv(path, n, node="nyquist", utc0=1788813341984000, step=1024000, unanchored=0):
    from hear import scenefile as SF
    import binascii
    gen = SF.S2
    rows = []
    for i in range(n):
        rng = np.random.default_rng(i + 1)
        q = rng.integers(-128, 127, size=(20, 4), dtype=np.int8)
        utc = 0 if i < unanchored else utc0 + i * step
        vals = {"node": node, "utc_us": str(utc), "uptime_s": "5158",
                "sample": str(1000 + i), "bands": "20", "slices": "4", "span_ms": "1024",
                "ref_db4": "251", "frames": "64", "fft_us": "18987",
                "mel_hex": binascii.hexlify(q.tobytes()).decode(),
                "f_lo_hz": "300", "f_hi_hz": "7840"}
        rows.append(",".join(vals[c] for c in gen.written))
    path.write_text("\n".join([",".join(gen.declared)] + rows) + "\n")
    return str(path)


class TestSceneSeam:
    """`scene_xt` is the ONLY contract between this module and `hear/pool.py`; pool.py is not
    modified and does not import validate. The invariant every split rests on is that X and the
    row dicts are the same length in the same order."""

    def test_x_and_the_rows_behind_it_line_up(self, tmp_path):
        from hear import pool as P
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_scene_csv(tmp_path / "scene.csv", 40))
        X, t, rows = V.scene_xt(pl)
        assert X.shape == (40, 80) and len(rows) == 40 and t.size == 40
        assert np.isfinite(t).all()
        assert t[1] - t[0] == pytest.approx(1.024)

    def test_unanchored_rows_come_back_nan_not_1970(self, tmp_path):
        # 56.1% of mach is like this. A naive read of utc_us puts them at epoch 0 and reports a
        # 56-year span; make_blocks would then build one 1970 block and every real one after it.
        from hear import pool as P
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_scene_csv(tmp_path / "scene.csv", 40, unanchored=22))
        X, t, rows = V.scene_xt(pl)
        assert np.isnan(t).sum() == 22
        assert np.nanmax(t) - np.nanmin(t) < 60.0
        assert sum(1 for r in rows if not r["anchored"]) == 22

    def test_a_short_scene_corpus_refuses_rather_than_reporting(self, tmp_path):
        from hear import pool as P
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_scene_csv(tmp_path / "scene.csv", 40))
        X, t, _rows = V.scene_xt(pl)
        with pytest.raises(V.Refused):
            V.make_blocks(X, t)


# --------------------------------------------------------------------------- report

class TestFormatReport:
    def test_it_warns_in_text_and_returns_nothing_that_gates(self):
        X, t = drifting()
        b = V.make_blocks(X, t, block_s=DRIFT_BLOCK_S)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **noise_pair())
        out = V.format_report(env, rep, b)
        assert isinstance(out, str)
        assert "WARN" in out and "spread" in out and "jaccard" in out
        assert "blocks per fitted dimension" in out

    def test_a_clean_corpus_earns_no_warning(self):
        # The other arm: a report that always says WARN is a report nobody reads.
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert "WARN" not in V.format_report(env, rep, b)

    def test_the_row_accounting_is_in_the_report(self):
        X, t = ar1()
        t = t.copy()
        t[::4] = np.nan
        b = V.make_blocks(X, t, lag_s=31.0, block_s=93.0)
        out = V.format_report(None, None, b)
        assert "unanchored %d" % b.dropped_unanchored in out
        assert "guard %d" % b.dropped_guard in out


# --------------------------------------------------------------------------- the second channel

class TestTheLagIsMeasuredOnBothChannels:
    """`decorrelation_lag_s` sizes every block in this module, and it used to measure one thing:
    the mean cosine between mean-removed rows. Cosine divides out the row's magnitude, and
    `mahalanobis_score` reads DISTANCE, which for scene rows is mostly level. The estimator was
    therefore free to return a lag arbitrarily shorter than the one the scorer needs, and short
    is the direction that makes the diagnostics pass rather than the direction that makes them
    noisy. Both arms are here: neither channel alone is allowed to be the whole answer.
    """

    def test_a_corpus_that_remembers_only_its_level_is_not_read_as_one_row(self):
        X, t = level_only()
        lag = V.decorrelation_lag_s(X, t)
        assert lag["cos_dir_lag1"] < 0.1, lag["cos_dir_lag1"]
        assert lag["acf_level_lag1"] > 0.9, lag["acf_level_lag1"]
        assert lag["channel"] == "level"
        assert lag["lag_rows"] > 100, lag["lag_rows"]

    def test_a_corpus_that_remembers_only_its_direction_is_read_off_that(self):
        # The other arm. Without it, "the level channel is consulted" would be satisfied by an
        # estimator that reported the level lag always, including where it is the wrong one.
        X, t = ar1()
        lag = V.decorrelation_lag_s(X, t)
        assert lag["channel"] == "direction"
        assert lag["acf_level_lag1"] < lag["cos_dir_lag1"]
        assert abs(lag["lag_rows"] - ar1_lag_rows()) < 6, lag["lag_rows"]

    def test_the_cosine_only_reading_of_the_level_corpus_would_have_been_one_row(self):
        # The mutant: what the previous estimator returned on the same rows. It is not merely a
        # different number -- 1 row against 316 s is 300x, and `make_blocks` builds the split it
        # refuses out of it.
        X, t = level_only()
        cos_only = [r for r in V.decorrelation_lag_s(X, t)["curve"] if r["cos_dir"] < 0.5]
        assert cos_only and cos_only[0]["lag_rows"] == 1
        assert V.decorrelation_lag_s(X, t)["lag_s"] > 100.0 * cos_only[0]["lag_s"]

    def test_the_level_corpus_blocks_long_instead_of_short(self):
        X, t = level_only()
        b = V.make_blocks(X, t)
        assert b.block_s > 300.0 and b.n_blocks < 40
        assert b.min_cross_block_gap_s >= b.lag_s


class TestTheCrossingMustBeSustained:
    def test_a_dip_that_comes_back_up_is_not_a_crossing(self):
        X, t = alternating()
        lag = V.decorrelation_lag_s(X, t)
        curve = {r["lag_rows"]: r["cos"] for r in lag["curve"]}
        assert curve[1] < 0.5 < curve[2], (curve[1], curve[2])
        assert lag["lag_rows"] > 2, lag["lag_rows"]

    def test_a_curve_that_really_falls_still_crosses_where_it_falls(self):
        # The other arm: the rule must not simply push every answer out to the cap.
        X, t = ar1()
        lag = V.decorrelation_lag_s(X, t)
        assert not lag["censored"]
        assert abs(lag["lag_rows"] - ar1_lag_rows()) < 6

    def test_the_margin_at_the_crossing_is_reported_not_assumed(self):
        lag = V.decorrelation_lag_s(*ar1())
        assert lag["cos_at_lag"] < lag["rho"]
        assert lag["n_pairs_at_lag"] >= V.MIN_PAIRS_PER_LAG

    def test_moving_rho_a_little_no_longer_moves_the_answer_a_lot(self):
        X, t = alternating()
        lags = [V.decorrelation_lag_s(X, t, rho=r)["lag_s"] for r in (0.48, 0.50, 0.52)]
        assert max(lags) / min(lags) < 2.0, lags

    def test_the_pair_floor_is_its_own_constant(self):
        # MIN_PAIRS_PER_LAG, min_rows_per_block and MIN_BLOCKS_ENVELOPE were all 8 in the first
        # draft and mean three different things. This asserts the first one is actually used.
        with pytest.raises(V.Refused, match="usable row pairs"):
            V.decorrelation_lag_s(*iid(n=40, d=4), min_pairs=10**6)


# --------------------------------------------------------------------------- Blocks invariants

class TestBlocksChecksItself:
    """Every refusal the split machinery advertises used to live in `make_blocks`, and both
    reports take a `Blocks` without calling it. A hand-built one carrying nonsense was scored."""

    def _fields(self, **over):
        n = 40
        f = dict(t=T0 + np.arange(n) * 1.0, row=np.arange(n), block=np.arange(n) // 10,
                 block_s=10.0, lag_s=1.0, guard_s=0.0, n_blocks=4, lag_censored=False,
                 n_in=n, dropped_unanchored=0, dropped_short_block=0, dropped_guard=0)
        f.update(over)
        return f

    def test_the_honest_shape_is_accepted(self):
        assert V.Blocks(**self._fields()).n_blocks == 4

    def test_row_accounting_that_does_not_close_is_refused(self):
        with pytest.raises(V.Refused, match="row accounting does not close"):
            V.Blocks(**self._fields(n_in=1))

    def test_a_negative_counter_is_refused(self):
        with pytest.raises(V.Refused, match="dropped_unanchored=-500 is negative"):
            V.Blocks(**self._fields(dropped_unanchored=-500))

    def test_a_block_count_that_does_not_match_the_ids_is_refused(self):
        with pytest.raises(V.Refused, match="n_blocks=999"):
            V.Blocks(**self._fields(n_blocks=999))

    def test_a_guard_wider_than_the_block_is_refused(self):
        with pytest.raises(V.Refused, match="guard_s=1e\\+09 must be in"):
            V.Blocks(**self._fields(guard_s=1e9))

    def test_mismatched_arrays_are_refused(self):
        with pytest.raises(V.Refused, match="index the same rows"):
            V.Blocks(**self._fields(row=np.arange(39), n_in=39))

    def test_the_gap_is_measured_from_the_times_not_read_off_guard_s(self):
        # A partition may declare any guard it likes; the rows say what the separation is.
        b = V.Blocks(**self._fields(guard_s=9.0))
        assert b.min_cross_block_gap_s == pytest.approx(1.0)

    def test_a_single_block_has_no_cross_block_gap(self):
        b = V.Blocks(**self._fields(block=np.zeros(40, dtype=int), n_blocks=1))
        assert b.min_cross_block_gap_s is None


class TestSeparationIsCheckedWhereItIsUsed:
    def test_a_make_blocks_partition_passes(self):
        b = V.make_blocks(*ar1())
        sep = V.separation_check(b)
        assert sep["separated"] and sep["min_cross_block_gap_s"] >= b.lag_s

    def test_the_split_make_blocks_refuses_is_refused_by_the_reports_too(self):
        # THE HOLE. `alarm_reproducibility` re-checked nothing about the object it was handed, so
        # the exact partition `make_blocks` refuses by name could be built by hand and scored.
        X, t = ar1()
        b = row_index_blocks(X, t)
        with pytest.raises(V.Refused, match="blocks are not separated"):
            V.alarm_reproducibility(X, b, pctl=99.0, **FIT)
        with pytest.raises(V.Refused, match="blocks are not separated"):
            V.holdout_calibration(X, b, pctl=99.0, **FIT)

    def test_a_hand_built_block_shorter_than_its_own_lag_is_caught(self):
        # The sketch module's `native` shape: block_s=3.0 against lag_s=22.4, guard 0.
        n = 60
        t = T0 + np.arange(n) * 1.0
        b = V.Blocks(t=t, row=np.arange(n), block=np.arange(n) // 3, block_s=3.0, lag_s=22.4,
                     guard_s=0.0, n_blocks=20, lag_censored=False, n_in=n, dropped_unanchored=0,
                     dropped_short_block=0, dropped_guard=0)
        with pytest.raises(V.Refused, match="1.000s apart against this split's own"):
            V.separation_check(b)

    def test_the_escape_is_explicit_and_says_so_in_the_report(self):
        X, t = ar1()
        b = row_index_blocks(X, t)
        rep = V.alarm_reproducibility(X, b, pctl=99.0, allow_unseparated=True, **FIT)
        assert rep["separated"] is False
        assert "NOT separated" in V.format_report(None, rep, b)

    def test_a_separated_split_earns_no_such_line(self):
        X, t = ar1()
        b = V.make_blocks(X, t)
        rep = V.alarm_reproducibility(X, b, pctl=99.0, **FIT)
        assert rep["separated"] is True
        assert "NOT separated" not in V.format_report(None, rep, b)


# --------------------------------------------------------------------------- vacuous agreement

class TestBothVacuousEndsAreRefused:
    """The source guarded `union == 0` and left `intersection == union == everything` open, where
    the Jaccard is 1.000 by construction. `format_report`'s only Jaccard warning was `< 0.5`, so
    the most reassuring number the metric can produce printed with no caption -- on white noise,
    in the exact n~d regime `validate_sketch.py` runs in."""

    def test_a_scorer_that_flags_everything_is_refused_not_scored_one(self):
        X, t = wide()
        b = V.make_blocks(X, t, block_s=30.0, lag_s=1.0)
        with pytest.raises(V.Refused, match="agree vacuously"):
            V.alarm_reproducibility(X, b, **FIT)

    def test_the_refusal_names_how_many_rows_were_flagged(self):
        X, t = wide()
        b = V.make_blocks(X, t, block_s=30.0, lag_s=1.0)
        with pytest.raises(V.Refused) as e:
            V.alarm_reproducibility(X, b, **FIT)
        msg = str(e.value)
        assert "Jaccard is 1.0 by construction" in msg and "holdout rows" in msg

    def test_the_empty_end_is_still_refused(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        with pytest.raises(V.Refused, match="agree vacuously"):
            V.alarm_reproducibility(X, b, **silent_pair())

    def test_a_real_pair_between_the_two_ends_is_scored(self):
        # The arm that keeps the refusal from being "refuse everything at n ~ d".
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert 0.0 < rep["rate_a"] < 1.0 and 0.0 < rep["jaccard"] < 1.0


class TestJaccardAgainstItsChanceBaseline:
    """Jaccard's null depends on the two rates: two INDEPENDENT flaggers at rate r overlap at
    r/(2-r), which is 0.003 at a 0.5% design and 1.0 when both flag everything. Reading the raw
    number without that denominator is what let white noise score 1.000."""

    def test_the_chance_baseline_matches_the_closed_form(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **noise_pair())
        ra, rb = rep["rate_a"], rep["rate_b"]
        assert rep["jaccard_chance"] == pytest.approx(ra * rb / (ra + rb - ra * rb))
        assert rep["jaccard_excess"] == pytest.approx(rep["jaccard"] - rep["jaccard_chance"])

    def test_a_noise_scorer_sits_on_its_chance_baseline(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **noise_pair())
        assert abs(rep["jaccard_excess"]) < 0.1, rep["jaccard_excess"]
        assert "only" in V.format_report(None, rep, b)

    def test_a_stable_scorer_clears_it_by_a_long_way(self):
        X, t = iid()
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert rep["jaccard_excess"] > 0.5, rep["jaccard_excess"]
        assert "above the" not in V.format_report(None, rep, b)


# --------------------------------------------------------------------------- resolution

class TestTheRowCountsCanRepresentTheRate:
    """`pctl=99.5` came from hugbot with its two companion floors (`min_fit=DIM` records,
    `min_holdout=100` records) left behind. A 1-in-200 tail taken over fewer than 200 fit rows is
    the sample maximum, and a 16-row holdout can only report 0.00% or 12x the design."""

    def test_a_holdout_too_small_to_represent_the_design_rate_is_refused(self):
        # 20 blocks of 99 rows: the FIT part is ample (1782 rows for a 0.5% tail) and only the
        # 198-row holdout is short, so this cannot be passing for the other refusal's reason.
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=100.0)
        with pytest.raises(V.Refused, match="holdout rows cannot measure"):
            V.holdout_calibration(X, b, frac=0.9, pctl=99.5, **FIT)

    def test_a_fit_too_small_to_place_the_percentile_is_refused(self):
        X, t = iid(n=400, d=4)
        b = V.make_blocks(X, t, block_s=40.0, lag_s=1.0)
        with pytest.raises(V.Refused, match="cannot carry a pctl"):
            V.holdout_calibration(X, b, frac=0.5, pctl=99.99, **FIT)

    def test_the_same_split_at_a_pctl_its_rows_can_carry_is_not_refused(self):
        # The null arm: the refusal keys on rows-against-pctl, not on the corpus being small.
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=200.0)
        cal = V.holdout_calibration(X, b, frac=0.8, pctl=99.0, **FIT)
        assert cal["expected_alarms_holdout"] >= 1.0

    def test_the_refusal_states_the_rows_it_needs(self):
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=100.0)
        with pytest.raises(V.Refused) as e:
            V.holdout_calibration(X, b, frac=0.9, pctl=99.5, **FIT)
        assert "Needs >= 200 holdout rows" in str(e.value)

    def test_a_pctl_with_no_tail_at_all_is_refused(self):
        X, t = iid(n=2000, d=4)
        b = V.make_blocks(X, t, block_s=200.0)
        with pytest.raises(V.Refused, match="leaves no tail"):
            V.holdout_calibration(X, b, frac=0.8, pctl=100.0, **FIT)


# --------------------------------------------------------------------------- collapsed envelope

class TestTheEnvelopeSaysWhenItHasOneCut:
    """`min_blocks_fit=4 + min_blocks_holdout=2` is a floor sized for the ONE-cut function and
    reused as the floor for the FIVE-cut one. At exactly 6 blocks every surviving frac clamps to
    the same cut and `spread_x` was 1.0000x by arithmetic on any data -- the most reassuring
    value the metric can take, on the smallest split the tool will run and on the real drain at
    --lag-mult 1.5."""

    def _blocks(self, n):
        X, t = iid(n=n * 200, d=8)
        return X, V.make_blocks(X, t, block_s=200.0)

    def test_six_blocks_report_no_spread_rather_than_a_spread_of_one(self):
        X, b = self._blocks(6)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert b.n_blocks == 6 and env["n_distinct_splits"] == 1
        assert env["spread_x"] is None
        assert "distinct split" in env["spread_undefined_because"]

    def test_the_report_says_so_where_a_reader_would_look(self):
        X, b = self._blocks(6)
        out = V.format_report(V.holdout_envelope(X, b, pctl=99.0, **FIT), None, b)
        assert "collapsed to ONE split" in out and "20 blocks" in out

    def test_twenty_blocks_are_where_five_distinct_cuts_start(self):
        # The arm that keeps the caption from being unconditional.
        X, b = self._blocks(20)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["n_distinct_splits"] == 5 and env["spread_x"] is not None
        assert "collapsed to ONE split" not in V.format_report(env, None, b)

    def test_the_collapse_is_not_hidden_by_a_zero_minimum(self):
        X, b = self._blocks(6)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["min_is_zero"] is False or env["spread_undefined_because"]


# --------------------------------------------------------------------------- fit diagnostics

class TestTheConditioningDiagnosticsAreRead:
    """`eps_floor_frac` was documented as the thing that says so when a corpus starts leaning on
    the floor. It was built inside `_fit_and_flag`, handed to `score`, and dropped -- no report
    path read it, and the conditioning measurement it rests on was taken over the whole 7077-row
    corpus, which nothing ever fits. Every number reported comes from a split PART."""

    def test_a_floored_fit_reaches_the_report(self):
        X, t = iid(n=8000, d=8)
        X = X.copy()
        X[:, 3] = 0.0                       # one dead dimension: 1/8 of the directions
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["max_eps_floor_frac"] == pytest.approx(1.0 / 8.0)
        assert "on the eps floor" in V.format_report(env, None, b)

    def test_a_clean_fit_reports_zero_and_earns_no_line(self):
        X, t = iid(n=8000, d=8)
        b = V.make_blocks(X, t, block_s=200.0)
        env = V.holdout_envelope(X, b, pctl=99.0, **FIT)
        assert env["max_eps_floor_frac"] == 0.0
        assert "eps floor" not in V.format_report(env, None, b)

    def test_reproducibility_carries_it_per_fit(self):
        X, t = iid(n=8000, d=8)
        X = X.copy()
        X[:, 3] = 0.0
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert rep["diag_a"]["eps_floor_frac"] > 0.0 and rep["diag_b"]["eps_floor_frac"] > 0.0
        assert "fit a has" in V.format_report(None, rep, b)

    def test_fit_rows_per_dimension_is_on_the_record(self):
        X, t = iid(n=8000, d=8)
        b = V.make_blocks(X, t, block_s=200.0)
        cal = V.holdout_calibration(X, b, frac=0.8, pctl=99.0, **FIT)
        assert cal["n_fit_over_d"] == pytest.approx(cal["n_fit"] / 8.0)


class TestBlocksPerDimensionIsNotSufficiency:
    def test_the_covariance_parameter_count_rides_alongside(self):
        # The docstring claimed `blocks_per_dim` REPLACED hugbot's min_part=DIM floor. It is that
        # floor with its observation unit swapped; the module's own argument is about d(d+1)/2
        # free parameters, and the ratio against those is 40x smaller at 80 dims.
        X, t = iid(n=8000, d=8)
        b = V.make_blocks(X, t, block_s=200.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        assert rep["cov_params"] == 36
        assert rep["blocks_per_cov_param"] == pytest.approx(
            min(rep["blocks_a"], rep["blocks_b"]) / 36.0)

    def test_the_warning_names_the_parameter_count_rather_than_implying_sufficiency(self):
        X, t = iid(n=4000, d=64)
        b = V.make_blocks(X, t, block_s=200.0, lag_s=1.0)
        rep = V.alarm_reproducibility(X, b, pctl=95.0, **FIT)
        out = V.format_report(None, rep, b)
        assert "under-determined" in out and "2080 of them" in out


# --------------------------------------------------------------------------- deployed geometry

class TestAtTheDeployedGeometry:
    """The paired nulls above run at d=8 with independent rows. The corpus this module is FOR is
    80-dimensional with a lag-1 cosine of 0.96, and the arms behave differently there -- which is
    the whole failure class, so it is measured here rather than argued about.

    Both corpora below are STATIONARY: an AR(1) with the drain's own lag-1 cosine and no drift of
    any kind. At d=8 the reports read sensibly. At d=80 the same generator produces two fits that
    each alarm on 98% of the holdout and overlap at Jaccard 0.969 -- and 0.961 of that is what two
    INDEPENDENT flaggers at those rates score by chance. The old report printed 0.969 with no
    caption at all, because its only Jaccard warning was `< 0.5`.
    """

    def _reports(self, d):
        X, t = ar1(n=12000, d=d, a=0.98, rho0=0.98, seed=5)
        b = V.make_blocks(X, t)
        return (X, b, V.holdout_envelope(X, b, pctl=99.5, **FIT),
                V.alarm_reproducibility(X, b, pctl=99.5, **FIT))

    def test_a_stationary_null_at_eighty_dims_agrees_almost_perfectly_by_chance(self):
        _X, _b, _env, rep = self._reports(80)
        assert rep["jaccard"] > 0.9, rep["jaccard"]
        assert rep["jaccard_excess"] < 0.05, rep["jaccard_excess"]
        assert rep["rate_a"] > 0.9 and rep["rate_b"] > 0.9

    def test_the_old_threshold_would_have_printed_that_without_a_word(self):
        # `jaccard < 0.5` is silent at 0.969. The caption has to come from the chance baseline.
        _X, b, _env, rep = self._reports(80)
        out = V.format_report(None, rep, b)
        assert rep["jaccard"] >= 0.5
        assert "is only +0.007 above" in out and "this is not agreement" in out

    def test_the_same_generator_at_eight_dims_reads_honestly(self):
        # The arm. Without it "the WARN fires at d=80" would only prove the WARN always fires.
        _X, b, env, rep = self._reports(8)
        assert rep["jaccard_excess"] > 0.2, rep["jaccard_excess"]
        assert env["max"] < 6.0 * env["designed"]
        assert "not agreement" not in V.format_report(env, rep, b)

    def test_the_exceedance_is_captioned_with_which_cause_it_is(self):
        # 93.5% against a 0.500% design on a corpus with NO drift: the exceedance warning alone
        # would read as drift. 50.4 fit rows per dimension is the other half of the sentence.
        _X, b, env, _rep = self._reports(80)
        out = V.format_report(env, None, b)
        assert env["max"] > 100.0 * env["designed"]
        assert "50.4 fit rows per dimension" in out

    def test_at_eight_dims_no_such_caption_is_earned(self):
        _X, b, env, _rep = self._reports(8)
        assert env["n_fit_over_d"] > 100.0
        assert "fit rows per dimension: an exceedance" not in V.format_report(env, None, b)
