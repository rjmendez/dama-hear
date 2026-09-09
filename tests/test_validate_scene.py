"""Falsifiability set for `tools/validate_scene.py`.

The tool's job is to REFUSE by name, so every refusal here is paired with a NULL CONTROL: a
corpus that differs in exactly the property the refusal claims to key on, and which must NOT be
refused. A refusal that fires on every input is worth as much as a check that fires on none.

  - the row-interval floor fires on an iid corpus and stays silent on an AR(1) corpus of the SAME
    length, row interval, dimension and file size, so it keys on the measured lag rather than on
    anything about the file;
  - the `cuts collapsed` note fires at block_s=205 (7 blocks, 3 draws, 2 distinct splits) and
    stays silent at block_s=180 (8 blocks, 3 draws, 3 distinct) on the same rows;
  - the unanchored trap is asserted from both ends: `t` must be NaN, and the naive reading must
    still be computed and reported, because a tool that silently does the right thing teaches
    nobody why the wrong number is wrong;
  - and the exit code must be 0 on a corpus whose exceedance is 83% and 167x its design, because
    an exit code that keys on a diagnostic is how the diagnostic starts being made to pass.

No test here needs the network or the drain. The generators are local on purpose: a null
generator that ships in the tool gets reused as a fixture and stops being a null.
"""
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools"))
from hear import validate as V                                     # noqa: E402
import validate_scene as VS                                        # noqa: E402

T0 = 1788800000.0                # an arbitrary but anchored wall clock
DT = 1.024                       # the real scene row interval
REF_DB = 67.0                    # the drain's own ref_db4=268
HEADER = ("node,utc_us,uptime_s,sample,bands,slices,span_ms,ref_db4,frames,fft_us,mel_hex,"
          "f_lo_hz,f_hi_hz")


# --------------------------------------------------------------------------- generators

def write_scene(path, db, ts, *, bands, slices, node="testnode", ref_db=REF_DB, tail=()):
    """Write an S2 scene.csv encoding `db` at `ts`. `ts` entry None means utc_us == 0.

    The encoding is the file format's, not a convenience: a cell is a half-decibel step BELOW the
    row's own `ref_db4` reference, so `q = (db - ref_db) * 2` and the reader's `q/2 + ref_db`
    must invert it exactly. `tail` appends raw lines, which is how the malformed and mixed cases
    are built without a second writer.
    """
    ref4 = int(round(ref_db * 4))
    lines = [HEADER]
    for i, (row, t) in enumerate(zip(db, ts)):
        q = np.clip(np.round((np.asarray(row, dtype=float) - ref_db) * 2.0), -128, 127)
        us = 0 if (t is None or not np.isfinite(t)) else int(round(float(t) * 1e6))
        lines.append("%s,%d,%d,,%d,%d,1024,%d,64,17000,%s,62.5,7812.5"
                     % (node, us, i, bands, slices, ref4,
                        q.astype(np.int8).tobytes().hex()))
    lines.extend(tail)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def ar1(n, d, phi, *, seed=0, scale=4.0, ramp=0.0):
    """An AR(1) corpus in dB. `phi=0` is the iid null; `phi=0.9727` crosses cos 0.5 near 23 rows."""
    rng = np.random.default_rng(seed)
    x = np.zeros((n, d))
    z = rng.normal(size=d)
    for i in range(n):
        z = phi * z + np.sqrt(max(1.0 - phi * phi, 0.0)) * rng.normal(size=d)
        x[i] = z
    x = x * scale + REF_DB
    if ramp:
        x = x + np.linspace(0.0, ramp, n)[:, None]
    return x


def stamps(n, dt=DT, t0=T0):
    return [t0 + i * dt for i in range(n)]


def corpus(tmp_path, n, d, phi, *, name="scene.csv", node="testnode", ts=None, **kw):
    bands = 2 if d == 4 else 4
    p = os.path.join(str(tmp_path), name)
    write_scene(p, ar1(n, d, phi, **kw), ts if ts is not None else stamps(n),
                bands=bands, slices=d // bands, node=node)
    return p


# --------------------------------------------------------------------------- reading

class TestReadScene:
    def test_the_matrix_inverts_the_encoding_exactly(self, tmp_path):
        db = np.array([[60.0, 67.0, 74.0, 67.5], [70.0, 64.5, 67.0, 61.0]])
        p = write_scene(os.path.join(str(tmp_path), "s.csv"), db, stamps(2),
                        bands=2, slices=2)
        sc = VS.read_scene(p)
        assert sc.X.shape == (2, 4)
        assert np.allclose(sc.X, db.reshape(2, 4))

    def test_q_mode_is_the_same_rows_without_their_reference(self, tmp_path):
        p = corpus(tmp_path, 20, 4, 0.5)
        db = VS.read_scene(p, mode="db").X
        q = VS.read_scene(p, mode="q").X
        assert np.allclose(db - REF_DB, q / 2.0)

    def test_x_and_rows_stay_aligned_row_for_row(self, tmp_path):
        p = corpus(tmp_path, 30, 4, 0.5)
        sc = VS.read_scene(p)
        assert sc.X.shape[0] == len(sc.rows) == sc.t.size == 30
        for i, r in enumerate(sc.rows):
            assert sc.t[i] == pytest.approx(int(r["utc_us"]) / 1e6)

    def test_an_unanchored_row_is_nan_and_not_1970(self, tmp_path):
        ts = stamps(40)
        for i in range(0, 20):
            ts[i] = None
        p = corpus(tmp_path, 40, 4, 0.5, ts=ts)
        sc = VS.read_scene(p)
        assert sc.n_unanchored == 20
        assert np.isnan(sc.t[:20]).all()
        assert (sc.t[20:] > 1.7e9).all()
        assert sc.span_s == pytest.approx(19 * DT)

    def test_the_naive_reading_is_still_computed_so_it_can_be_shown(self, tmp_path):
        """The 1970 trap is REPORTED, not merely avoided: on mach it is 56.68 years vs 43.1 min."""
        ts = stamps(40)
        ts[0] = None
        p = corpus(tmp_path, 40, 4, 0.5, ts=ts)
        sc = VS.read_scene(p)
        assert sc.naive_span_s / (365.25 * 86400.0) > 50.0
        assert sc.span_s < 60.0
        assert sc.naive_span_s / sc.span_s > 1e6

    def test_a_fully_anchored_file_has_no_gap_between_the_two_spans(self, tmp_path):
        p = corpus(tmp_path, 40, 4, 0.5)
        sc = VS.read_scene(p)
        assert sc.n_unanchored == 0
        assert sc.naive_span_s == pytest.approx(sc.span_s)

    def test_every_row_unanchored_is_refused_by_name(self, tmp_path):
        p = corpus(tmp_path, 40, 4, 0.5, ts=[None] * 40)
        with pytest.raises(V.Refused) as e:
            VS.read_scene(p)
        m = str(e.value)
        assert "every row is unanchored" in m
        assert "utc_us == 0" in m and "PPS" in m
        assert "NOT malformed" in m
        assert "1970" in m

    def test_mixed_geometry_is_refused_by_name(self, tmp_path):
        p = os.path.join(str(tmp_path), "s.csv")
        write_scene(p, ar1(10, 4, 0.5), stamps(10), bands=2, slices=2)
        with open(p, "a") as fh:
            fh.write("testnode,%d,99,,4,4,1024,268,64,17000,%s,62.5,7812.5\n"
                     % (int((T0 + 99 * DT) * 1e6), "00" * 16))
        with pytest.raises(V.Refused) as e:
            VS.read_scene(p)
        m = str(e.value)
        assert "mixed geometry" in m and "2x2" in m and "4x4" in m

    def test_mixed_node_is_refused_and_names_the_escape(self, tmp_path):
        p = os.path.join(str(tmp_path), "s.csv")
        write_scene(p, ar1(10, 4, 0.5), stamps(10), bands=2, slices=2, node="mach")
        write_scene(os.path.join(str(tmp_path), "b.csv"), ar1(4, 4, 0.5), stamps(4),
                    bands=2, slices=2, node="nyquist")
        with open(os.path.join(str(tmp_path), "b.csv")) as fh:
            extra = fh.read().splitlines()[1:]
        with open(p, "a") as fh:
            fh.write("\n".join(extra) + "\n")
        with pytest.raises(V.Refused) as e:
            VS.read_scene(p)
        m = str(e.value)
        assert "mixed node" in m and "mach" in m and "nyquist" in m
        assert "--only-node" in m

    def test_only_node_keeps_one_and_counts_the_rest(self, tmp_path):
        p = os.path.join(str(tmp_path), "s.csv")
        write_scene(p, ar1(10, 4, 0.5), stamps(10), bands=2, slices=2, node="mach")
        write_scene(os.path.join(str(tmp_path), "b.csv"), ar1(4, 4, 0.5), stamps(4),
                    bands=2, slices=2, node="nyquist")
        with open(os.path.join(str(tmp_path), "b.csv")) as fh:
            extra = fh.read().splitlines()[1:]
        with open(p, "a") as fh:
            fh.write("\n".join(extra) + "\n")
        sc = VS.read_scene(p, only_node="mach")
        assert sc.n_rows == 10
        assert sc.n_foreign_node == 4
        assert sc.foreign_nodes == {"nyquist": 4}
        assert "nyquist x4" in VS.format_scene(sc)

    def test_an_undecodable_row_is_counted_not_silently_dropped(self, tmp_path):
        p = os.path.join(str(tmp_path), "s.csv")
        write_scene(p, ar1(10, 4, 0.5), stamps(10), bands=2, slices=2,
                    tail=["testnode,%d,50,,2,2,1024,268,64,17000,00cc,62.5,7812.5"
                          % int((T0 + 50 * DT) * 1e6)])
        sc = VS.read_scene(p)
        assert sc.n_rows == 10
        assert sc.n_undecodable == 1
        assert sum(sc.undecodable_reasons.values()) == 1
        assert "1 undecodable" in VS.format_scene(sc)

    def test_a_header_with_nothing_behind_it_is_refused(self, tmp_path):
        p = os.path.join(str(tmp_path), "s.csv")
        with open(p, "w") as fh:
            fh.write(HEADER + "\n")
        with pytest.raises(V.Refused) as e:
            VS.read_scene(p)
        assert "no decodable rows" in str(e.value)

    def test_limit_stops_at_n_rows(self, tmp_path):
        p = corpus(tmp_path, 40, 4, 0.5)
        assert VS.read_scene(p, limit=12).n_rows == 12

    def test_an_unknown_mode_is_refused(self, tmp_path):
        p = corpus(tmp_path, 10, 4, 0.5)
        with pytest.raises(V.Refused):
            VS.read_scene(p, mode="mel")

    def test_the_node_defaults_to_the_parent_directory(self, tmp_path):
        d = tmp_path / "nyquist"
        d.mkdir()
        p = os.path.join(str(d), "scene.csv")
        write_scene(p, ar1(6, 4, 0.5), stamps(6), bands=2, slices=2, node="")
        assert VS.read_scene(p).node == "nyquist"


# --------------------------------------------------------------------------- the scorer

class TestShrinkageGaussian:
    def test_alpha_zero_is_the_plain_inverse_covariance(self):
        X = ar1(400, 6, 0.0, seed=3)
        m = VS.shrinkage_gaussian(0.0)(X)
        assert np.allclose(m["inv"], np.linalg.inv(np.cov(X, rowvar=False)), atol=1e-6)

    def test_alpha_one_is_isotropic(self):
        X = ar1(400, 6, 0.0, seed=4)
        m = VS.shrinkage_gaussian(1.0)(X)
        mean_lam = np.trace(np.cov(X, rowvar=False)) / 6.0
        assert np.allclose(m["inv"], np.eye(6) / mean_lam, atol=1e-8)
        assert m["cond"] == pytest.approx(1.0)

    def test_conditioning_improves_monotonically_with_alpha(self):
        X = ar1(60, 20, 0.0, seed=5)
        conds = [VS.shrinkage_gaussian(a)(X)["cond"] for a in (0.0, 0.05, 0.2, 0.5)]
        assert all(conds[i] > conds[i + 1] for i in range(len(conds) - 1))

    def test_it_scores_a_fit_the_plain_covariance_cannot_invert(self):
        """n < d: the sample covariance is singular, which is the case shrinkage is FOR."""
        X = ar1(12, 20, 0.0, seed=6)
        plain = VS.shrinkage_gaussian(0.0)(X)
        assert not np.isfinite(plain["cond_raw"]) or plain["cond_raw"] > 1e12
        shrunk = VS.shrinkage_gaussian(0.2)(X)
        assert np.isfinite(shrunk["cond"]) and shrunk["cond"] < 1e6
        s = V.mahalanobis_score(shrunk, X)
        assert s.shape == (12,) and np.isfinite(s).all()

    def test_it_pairs_with_mahalanobis_score(self):
        X = ar1(200, 6, 0.0, seed=8)
        m = VS.shrinkage_gaussian()(X)
        s = V.mahalanobis_score(m, X)
        assert s.shape == (200,) and np.isfinite(s).all() and (s >= 0).all()
        far = V.mahalanobis_score(m, X[:1] + 40.0)
        assert far[0] > s.max()

    def test_an_alpha_outside_the_unit_interval_is_refused(self):
        for a in (-0.1, 1.5):
            with pytest.raises(V.Refused):
                VS.shrinkage_gaussian(a)

    def test_one_row_is_refused(self):
        with pytest.raises(V.Refused):
            VS.shrinkage_gaussian()(np.zeros((1, 4)))


# --------------------------------------------------------------------------- the refusals

class TestRowFloorRefusal:
    """The refusal, and the null control that differs only in the corpus's autocorrelation."""

    def test_an_iid_corpus_is_refused_and_names_train_sketch(self, tmp_path):
        p = corpus(tmp_path, 1400, 4, 0.0, name="iid.csv")
        with pytest.raises(V.Refused) as e:
            VS.run_reports(VS.read_scene(p))
        m = str(e.value)
        assert "rows per block" in m
        assert "int(utc // 3)" in m and "train_sketch" in m
        assert "a block needs 8" in m

    def test_a_correlated_corpus_of_the_same_size_is_not_refused(self, tmp_path):
        """Same rows, same interval, same dimension, same file: only the lag differs."""
        iid_p = corpus(tmp_path, 1400, 4, 0.0, name="iid.csv")
        ar_p = corpus(tmp_path, 1400, 4, 0.9727, name="ar.csv")
        assert VS.read_scene(iid_p).X.shape == VS.read_scene(ar_p).X.shape
        run = VS.run_reports(VS.read_scene(ar_p))
        assert run["blocks"].n_blocks >= VS.MIN_BLOCKS_ENVELOPE
        assert run["envelope"] is not None

    def test_a_stated_block_is_the_operator_overriding_it(self, tmp_path):
        """--block-s is a decision, so it is honoured -- and NOTE says the lag was at the floor."""
        p = corpus(tmp_path, 1400, 4, 0.0, name="iid.csv")
        run = VS.run_reports(VS.read_scene(p), block_s=200.0)
        assert run["envelope"] is not None
        assert any("row-interval floor" in n for n in run["notes"])


class TestShortSpanRefusal:
    def test_a_short_file_is_refused_with_the_capture_hours(self, tmp_path):
        p = corpus(tmp_path, 150, 4, 0.9727)
        with pytest.raises(V.Refused) as e:
            VS.run_reports(VS.read_scene(p))
        m = str(e.value)
        assert "span too short for an envelope" in m
        assert "needs 6" in m and "needs 12" in m
        assert "Capture at least" in m and " h for an envelope" in m

    def test_the_same_generator_run_longer_is_not_refused(self, tmp_path):
        p = corpus(tmp_path, 1400, 4, 0.9727)
        run = VS.run_reports(VS.read_scene(p))
        assert run["envelope"] is not None and run["reproducibility"] is not None

    def test_the_hours_it_asks_for_would_actually_reach_the_threshold(self, tmp_path):
        """The arithmetic in the message is checked, not just its wording."""
        sc = VS.read_scene(corpus(tmp_path, 150, 4, 0.9727))
        lag = V.decorrelation_lag_s(sc.X, sc.t)
        block_s = 3.0 * lag["lag_s"]
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)
        want = "%.1f h for an envelope" % (VS.MIN_BLOCKS_ENVELOPE * block_s / 3600.0)
        assert want in str(e.value)


class TestNothingGates:
    def test_an_84_percent_exceedance_is_returned_not_raised(self, tmp_path):
        p = corpus(tmp_path, 1400, 16, 0.9727)
        run = VS.run_reports(VS.read_scene(p))
        assert run["envelope"]["max"] > 0.4
        assert run["envelope"]["max"] > 100.0 * run["envelope"]["designed"]

    def test_the_exit_code_does_not_key_on_how_bad_the_numbers_are(self, tmp_path, capsys):
        p = corpus(tmp_path, 1400, 16, 0.9727)
        assert VS.main([p, "--node", "testnode"]) == 0
        out = capsys.readouterr().out
        assert "WARN max" in out and "x the 0.500% design" in out

    def test_the_exit_code_is_2_when_a_number_could_not_be_computed(self, tmp_path, capsys):
        p = corpus(tmp_path, 150, 4, 0.9727)
        assert VS.main([p, "--node", "testnode"]) == 2
        assert "REFUSED: span too short for an envelope" in capsys.readouterr().out


# --------------------------------------------------------------------------- the notes

class TestCollapsedCuts:
    """`spread 1.0x` off one split reported twice reads as stability. It is the opposite."""

    def test_it_fires_when_two_cut_points_land_on_one_split(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727)), block_s=205.0)
        env = run["envelope"]
        assert len({(d["blocks_fit"], d["n_fit"]) for d in env["draws"]}) < len(env["draws"])
        assert any("cuts collapsed" in n for n in run["notes"])

    def test_it_stays_silent_when_the_cuts_are_distinct(self, tmp_path):
        """Same rows, a different block length: the note must key on the splits, not the corpus."""
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727)), block_s=180.0)
        env = run["envelope"]
        assert len({(d["blocks_fit"], d["n_fit"]) for d in env["draws"]}) == len(env["draws"])
        assert not any("cuts collapsed" in n for n in run["notes"])

    def test_it_counts_the_refused_fracs_too(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727)), block_s=205.0)
        note = [n for n in run["notes"] if "cuts collapsed" in n][0]
        assert "of 5 fracs were refused" in note


class TestRowFloorNote:
    def test_it_fires_on_an_iid_corpus(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.0)), block_s=200.0)
        note = [n for n in run["notes"] if "row-interval floor" in n][0]
        assert "train_sketch" in note and "row interval(s)" in note

    def test_it_stays_silent_on_a_correlated_corpus_at_the_same_block(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727)), block_s=200.0)
        assert not any("row-interval floor" in n for n in run["notes"])


class TestBlocksPerDimNote:
    def test_it_fires_when_the_covariance_is_under_determined(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 16, 0.9727)))
        assert run["reproducibility"]["blocks_per_dim"] < 1.0
        note = [n for n in run["notes"] if "blocks per fitted dimension" in n][0]
        assert "136 free parameters" in note and "--shrinkage 0.10" in note

    def test_it_stays_silent_when_there_are_blocks_to_spare(self, tmp_path):
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727)))
        assert run["reproducibility"]["blocks_per_dim"] >= 1.0
        assert not any("blocks per fitted dimension" in n for n in run["notes"])


# --------------------------------------------------------------------------- output

class TestOutput:
    def test_the_header_prints_the_naive_span_beside_the_real_one(self, tmp_path):
        ts = stamps(40)
        ts[0] = None
        sc = VS.read_scene(corpus(tmp_path, 40, 4, 0.5, ts=ts))
        text = VS.format_scene(sc)
        assert "1 unanchored" in text
        assert "would report a span of" in text and "years" in text

    def test_a_fully_anchored_file_does_not_print_that_line(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 40, 4, 0.5))
        assert "years" not in VS.format_scene(sc)

    def test_the_report_carries_the_lag_the_block_and_the_alpha(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727))
        text = VS.format_run(sc, VS.run_reports(sc))
        assert "decorrelation: lag" in text and "cosine" in text
        assert "shrinkage Gaussian, alpha=0.100" in text
        assert "blocks:" in text and "guard" in text
        assert "holdout exceedance" in text and "reproducibility:" in text

    def test_a_refused_sub_report_is_printed_by_name_not_dropped(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727))
        run = VS.run_reports(sc, block_s=205.0)
        assert run["reproducibility"] is None
        text = VS.format_run(sc, run)
        assert "REFUSED alarm reproducibility:" in text
        assert "blocks available" in text

    def test_json_is_parseable_and_carries_the_numbers(self, tmp_path, capsys):
        p = corpus(tmp_path, 1400, 4, 0.9727)
        assert VS.main([p, "--node", "testnode", "--json"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["refused"] is None
        assert doc["dims"] == 4 and doc["rows"] == 1400
        assert doc["blocks"]["n_blocks"] >= VS.MIN_BLOCKS_ENVELOPE
        assert doc["envelope"]["max"] >= doc["envelope"]["min"]
        assert 0.0 <= doc["reproducibility"]["jaccard"] <= 1.0
        assert doc["shrinkage"] == VS.DEFAULT_SHRINKAGE

    def test_json_on_a_refusal_still_reports_the_file_it_read(self, tmp_path, capsys):
        p = corpus(tmp_path, 150, 4, 0.9727)
        assert VS.main([p, "--node", "testnode", "--json"]) == 2
        doc = json.loads(capsys.readouterr().out)
        assert "span too short" in doc["refused"]
        assert doc["rows"] == 150 and doc["anchored"] == 150

    def test_json_still_emits_json_when_the_file_itself_is_refused(self, tmp_path, capsys):
        """A caller parsing stdout must not have to parse two formats to learn it was refused."""
        p = corpus(tmp_path, 40, 4, 0.5, ts=[None] * 40)
        assert VS.main([p, "--node", "testnode", "--json"]) == 2
        doc = json.loads(capsys.readouterr().out)
        assert "every row is unanchored" in doc["refused"]

    def test_a_run_is_reproducible(self, tmp_path):
        """Same bytes in, same text out -- no seed, no clock and no dict order in the report."""
        sc = VS.read_scene(corpus(tmp_path, 1400, 4, 0.9727))
        assert VS.format_run(sc, VS.run_reports(sc)) == VS.format_run(sc, VS.run_reports(sc))


class TestTheRowFloorIsGuardAware:
    """`make_blocks` counts a block's rows only AFTER the guard band has taken `lag_s` off its
    head, so `block_s / dt` is not the number that has to clear `MIN_ROWS_PER_BLOCK`. Dividing
    the raw block length by dt left a band of measured lags that passed this tool's refusal and
    then died inside `make_blocks` with "every kept row landed in 0 block(s): anchored span 0.0s"
    -- arithmetically true, derived from an already-emptied array, and exactly the uninformative
    message this refusal exists to replace."""

    def test_a_lag_in_the_leaked_band_is_refused_here_and_named(self, tmp_path):
        # phi=0.75 measures a 3.07 s lag on a 1.024 s row: 9.2 / 1.024 = 9.0 rows passes the raw
        # check, and 6.0 rows survive the guard.
        sc = VS.read_scene(corpus(tmp_path, 2000, 4, 0.75))
        with pytest.raises(V.Refused, match="rows per block") as e:
            VS.run_reports(sc)
        assert "after its 3.1s guard band" in str(e.value)

    def test_the_raw_length_would_have_passed_and_make_blocks_would_have_emptied(self, tmp_path):
        # The mutant, run directly: the check the tool used to make, and what happened next.
        sc = VS.read_scene(corpus(tmp_path, 2000, 4, 0.75))
        lag = V.decorrelation_lag_s(sc.X, sc.t)
        want = 3.0 * float(lag["lag_s"])
        assert want / sc.dt_s >= VS.MIN_ROWS_PER_BLOCK          # the old check passed
        with pytest.raises(V.Refused, match="landed in 0 block"):
            V.make_blocks(sc.X, sc.t, lag_mult=3.0)

    def test_a_lag_just_above_the_band_still_runs(self, tmp_path):
        # The null control: phi=0.85 measures 4.1 s, keeps 10 rows a block, and is not refused.
        run = VS.run_reports(VS.read_scene(corpus(tmp_path, 2000, 4, 0.85)))
        assert run["blocks"].n_blocks > VS.MIN_BLOCKS_ENVELOPE

    def test_the_refusal_states_the_block_length_it_would_take(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 2000, 4, 0.75))
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)
        need = VS.MIN_ROWS_PER_BLOCK * sc.dt_s + float(
            V.decorrelation_lag_s(sc.X, sc.t)["lag_s"])
        assert "Blocks need >= %.1fs here" % need in str(e.value)


class TestNotesSurviveARefusal:
    """`NOTE lag is at the row-interval floor` was unreachable on the default path: the `rows per
    block` refusal fires first for every lag_mult <= 2.0 and most of the range above, and both of
    the note's own tests passed an explicit `block_s` that skipped that refusal. The fixture's
    override was the only thing making the assertion reachable."""

    def test_the_floor_note_reaches_the_operator_on_the_default_path(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 1400, 4, 0.0))
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)                                  # no block_s override
        assert "NOTE lag is at the row-interval floor" in str(e.value)

    def test_a_correlated_corpus_refused_for_another_reason_carries_no_such_note(self, tmp_path):
        # The arm: the note keys on the lag, not on there being a refusal.
        sc = VS.read_scene(corpus(tmp_path, 150, 4, 0.9727))
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)
        assert "row-interval floor" not in str(e.value)


class TestCollapsedCutsCatchesTheSevereForm:
    def test_one_surviving_draw_is_still_a_collapse(self, tmp_path):
        # The condition was `len(cuts) < len(draws)`, so the TOTAL collapse -- four fracs refused
        # and one draw left -- failed `1 < 1` and printed `spread 1.0x` with no caption.
        sc = VS.read_scene(corpus(tmp_path, 1200, 4, 0.9727))
        run = VS.run_reports(sc, block_s=200.0)
        assert len(run["envelope"]["draws"]) == 1
        assert any("cuts collapsed" in n for n in run["notes"])

    def test_the_envelope_itself_refuses_to_call_that_a_spread(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 1200, 4, 0.9727))
        run = VS.run_reports(sc, block_s=200.0)
        assert run["envelope"]["spread_x"] is None
        assert run["envelope"]["n_distinct_splits"] == 1

    def test_distinct_cuts_leave_the_note_silent(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 4000, 4, 0.9727))
        run = VS.run_reports(sc, block_s=180.0)
        assert run["envelope"]["n_distinct_splits"] >= 3
        assert not any("cuts collapsed" in n for n in run["notes"])


class TestTheShortSpanRefusalDoesNotSendYouToTheDegenerateCase:
    def test_it_quotes_the_hours_five_distinct_cuts_need(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 150, 4, 0.9727))
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)
        msg = str(e.value)
        assert "%d blocks" % VS.MIN_BLOCKS_DISTINCT_CUTS in msg
        assert "spread reads 1.0x on any data whatsoever" in msg

    def test_those_hours_are_the_arithmetic_they_claim(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 150, 4, 0.9727))
        with pytest.raises(V.Refused) as e:
            VS.run_reports(sc)
        lag = float(V.decorrelation_lag_s(sc.X, sc.t)["lag_s"])
        hours = VS.MIN_BLOCKS_DISTINCT_CUTS * 3.0 * lag / 3600.0
        assert "(%.1f h)" % hours in str(e.value)


class TestShrinkageDiagnosticsCanActuallyCount:
    def test_floored_raw_counts_the_raw_spectrum(self, tmp_path):
        # `floored` counts the SHRUNK eigenvalues, and (1-a)*lam + a*mean(lam) is positive for
        # any alpha > 0 over a real covariance -- so at the default alpha it could never be
        # non-zero. It was a counter that could not count.
        X = np.random.default_rng(4).normal(size=(80, 6))
        X[:, 2] = 0.0
        m = VS.shrinkage_gaussian(0.10)(X)
        assert m["floored"] == 0
        assert m["floored_raw"] >= 1
        assert m["eps_floor_frac"] > 0.0

    def test_a_full_rank_corpus_reports_zero_on_both(self, tmp_path):
        m = VS.shrinkage_gaussian(0.10)(np.random.default_rng(5).normal(size=(400, 6)))
        assert m["floored"] == 0 and m["floored_raw"] == 0 and m["eps_floor_frac"] == 0.0

    def test_the_run_carries_it_into_the_report(self, tmp_path):
        sc = VS.read_scene(corpus(tmp_path, 4000, 4, 0.9727))
        run = VS.run_reports(sc, block_s=180.0)
        assert "eps_floor_frac" in run["envelope"]["draws"][0]["fit_diag"]
