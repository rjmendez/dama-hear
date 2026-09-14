"""tools/node_survey.py -- the statistics that decide what a surveyed position is worth.

The tool exists because averaging node fixes into a survey does not work, and the reason is
subtle: the errors are correlated, so the number of epochs is not the number of independent
measurements. These tests pin the three ways of quoting an uncertainty apart, because picking the
wrong one is silent -- every one of them returns a plausible metre value.
"""
import math
import statistics as st

import pytest

from tools import node_survey as NS


def _series(t0, n, dt_s, xy, drift_period_s=3600.0, drift_m=2.0, jitter_m=0.0, seed=1):
    """Epochs with a SLOW correlated wander plus optional white jitter -- the shape multipath has.
    Positions are converted from a local offset back to lat/lon so the tool's own path is used."""
    import random
    rng = random.Random(seed)
    from hear import geodesy as G
    o = (40.0, -76.0, 0.0)
    out = []
    for i in range(n):
        t = t0 + i * dt_s * 1_000_000
        ph = 2 * math.pi * (i * dt_s) / drift_period_s
        e = xy[0] + drift_m * math.sin(ph) + rng.gauss(0, jitter_m)
        nn = xy[1] + drift_m * math.cos(ph) + rng.gauss(0, jitter_m)
        lat, lon, h = G.enu_to_geodetic(e, nn, 0.0, *o)
        out.append({"utc_us": t, "lat": lat, "lon": lon, "hell": h,
                    "hacc": 1.0, "vacc": 1.5, "sats": 20})
    return out, o


class TestEpochFiltering:
    def test_non_3d_fixes_and_blank_positions_are_dropped_not_zeroed(self):
        csv = ("utc_us,fix,sats,lat,lon,hell_m,hmsl_m,hacc_m,vacc_m\n"
               "1,3,20,40.1,-76.1,200,234,1.0,1.5\n"
               "2,2,20,40.1,-76.1,200,234,1.0,1.5\n"      # 2D fix
               "3,3,20,,,,,,\n"                            # blank
               "4,3,20,0.0000000,-76.1,200,234,1.0,1.5\n"  # null island sentinel
               "5,3,20,40.1,-76.1,200,234,99.0,1.5\n")     # hAcc past the reject floor
        assert len(NS.epochs(csv)) == 1

    def test_a_row_that_cannot_be_parsed_is_skipped_rather_than_raising(self):
        csv = ("utc_us,fix,sats,lat,lon,hell_m,hmsl_m,hacc_m,vacc_m\n"
               "x,3,20,40.1,-76.1,200,234,1.0,1.5\n"
               "2,3,20,40.1,-76.1,200,234,1.0,1.5\n")
        assert len(NS.epochs(csv)) == 1


class TestSigmaIsNotTheEpochScatter:
    """The heart of it. 8 hours of 30 s epochs with a slow 2 m wander and no white noise.

    The wander period is 5.7 h, NOT a whole number of blocks -- see
    test_a_wander_commensurate_with_the_block_length_is_invisible for why that matters.
    """

    def _summary(self):
        rows, o = _series(1_788_000_000_000_000, 960, 30.0, (0.0, 0.0),
                          drift_period_s=5.7 * 3600.0, drift_m=2.0)
        return NS.summarise(rows, o), rows

    def test_position_sigma_is_far_below_the_per_epoch_scatter(self):
        s, _ = self._summary()
        assert s["epoch_sigma_m"] > 1.0
        assert s["horiz_sigma_m"] < s["epoch_sigma_m"] / 2.0

    def test_position_sigma_is_far_above_the_naive_root_n(self):
        """sqrt(N) over epochs assumes independence, which is exactly what a slow wander is not.
        With 960 correlated epochs it would claim ~5 cm; the truth is nearer a metre."""
        s, rows = self._summary()
        naive = s["epoch_sigma_m"] / math.sqrt(len(rows))
        assert naive < 0.1
        assert s["horiz_sigma_m"] > 5.0 * naive

    def test_it_says_which_estimator_it_used(self):
        s, _ = self._summary()
        assert "hourly blocks" in s["sigma_source"]
        assert s["n_blocks"] >= 3

    def test_a_wander_commensurate_with_the_block_length_is_invisible(self):
        """A LIMIT of the estimator, pinned so nobody rediscovers it as a mystery.

        Blocking to an hour measures error that varies BETWEEN hours. An error whose period is
        exactly one hour has the same shape in every block, so every block median is identical and
        the estimator reports sigma 0 -- perfect confidence in a position that is wandering by
        metres. Found by writing this fixture with a 3600 s period by accident.

        It is not a defect for real data: multipath tracks the GPS ground-track repeat of about
        11 h 58 m, which is nowhere near an hour. But if the block length is ever tuned, it must
        stay well clear of the period of whatever is actually moving.
        """
        rows, o = _series(1_788_000_000_000_000, 960, 30.0, (0.0, 0.0),
                          drift_period_s=3600.0, drift_m=2.0)
        s = NS.summarise(rows, o)
        assert s["epoch_sigma_m"] > 1.0, "the position really is wandering by metres"
        assert s["horiz_sigma_m"] < 0.01, "and the hourly-block estimator cannot see it"

    def test_a_short_series_falls_back_and_admits_it(self):
        rows, o = _series(1_788_000_000_000_000, 20, 30.0, (0.0, 0.0))
        s = NS.summarise(rows, o)
        assert s["n_blocks"] < 3
        assert "too few blocks" in s["sigma_source"]
        assert s["horiz_sigma_m"] == pytest.approx(s["epoch_sigma_m"], rel=1e-9)


class TestMedianNotMean:
    def test_a_wild_outlier_does_not_move_the_surveyed_position(self):
        """The firmware keeps a running MEAN because it cannot store the series. This data has
        60 m outliers, and a mean carries them into the survey; a median does not."""
        rows, o = _series(1_788_000_000_000_000, 400, 30.0, (0.0, 0.0), drift_m=0.5)
        clean = NS.summarise(rows, o)
        from hear import geodesy as G
        lat, lon, h = G.enu_to_geodetic(500.0, 500.0, 0.0, *o)
        rows.append({"utc_us": rows[-1]["utc_us"] + 30_000_000, "lat": lat, "lon": lon,
                     "hell": h, "hacc": 1.0, "vacc": 1.5, "sats": 20})
        dirty = NS.summarise(rows, o)
        assert abs(dirty["e_m"] - clean["e_m"]) < 0.2
        assert abs(dirty["n_m"] - clean["n_m"]) < 0.2
        # ...whereas the mean would have moved by more than a metre.
        assert abs(500.0 / len(rows)) > 1.0


class TestBlockMedians:
    def test_blocks_shorter_than_three_epochs_are_dropped(self):
        rows, o = _series(1_788_000_000_000_000, 100, 30.0, (0.0, 0.0))
        rows.append({"utc_us": rows[-1]["utc_us"] + 10 * 3600 * 1_000_000,
                     "lat": 40.0, "lon": -76.0, "hell": 0.0,
                     "hacc": 1.0, "vacc": 1.5, "sats": 20})
        from hear import geodesy as G
        enu = [G.geodetic_to_enu(r["lat"], r["lon"], r["hell"], *o) for r in rows]
        blocks = NS._block_medians(rows, enu)
        # the lone far-future epoch forms a block of one and must not count as an observation
        assert all(True for _ in blocks)
        assert len(blocks) == 1


class TestOutputLoadsBack:
    """The tool must not emit a survey the loader refuses. It did: `frame` was written as the
    literal "enu" while from_dict() requires "enu_local", so node_survey.py produced a file it
    could not itself read and nothing noticed until someone ran both halves in one sitting.
    """

    def test_the_frame_string_comes_from_the_loader_not_a_literal(self):
        from hear.backend import survey as SV
        assert NS._FRAME == SV._FRAME
        assert NS._UNITS == SV._UNITS

    def test_a_written_survey_round_trips_through_the_loader(self, tmp_path):
        import json
        from hear.backend import survey as SV
        doc = {"frame": NS._FRAME, "units": NS._UNITS,
               "origin": {"lat_deg": 40.29, "lon_deg": -79.12, "h_ell_m": 0.0},
               "nodes": [{"node_id": 1, "name": "a", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0,
                          "sigma_m": 0.7},
                         {"node_id": 2, "name": "b", "e_m": -16.6, "n_m": -0.3, "u_m": 3.0,
                          "sigma_m": 0.5},
                         {"node_id": 3, "name": "c", "e_m": 4.0, "n_m": 12.0, "u_m": 0.0,
                          "sigma_m": 0.6}]}
        p = tmp_path / "s.json"
        p.write_text(json.dumps(doc))
        s = SV.load_survey(str(p))
        assert len(s) == 3
        assert s.vertical_spread_m() == pytest.approx(3.0, abs=1e-9)
        assert s.origin_geodetic()[0] == pytest.approx(40.29, abs=1e-9)


class TestReprIsTotal:
    def test_a_two_node_survey_can_still_be_printed(self):
        """A repr that raises fails at the exact moment you reach for it. linearity() needs three
        nodes; a two-node survey is a legal object that simply cannot be solved from."""
        from hear.backend import survey as SV
        s = SV.Survey({1: (0.0, 0.0, 0.0), 2: (-16.6, -0.3, 3.0)})
        assert "2 nodes" in repr(s)
        assert "n/a" in repr(s)

    def test_three_nodes_still_report_a_number(self):
        from hear.backend import survey as SV
        s = SV.Survey({1: (0.0, 0.0, 0.0), 2: (-16.6, -0.3, 3.0), 3: (4.0, 12.0, 0.0)})
        assert "n/a" not in repr(s)


class TestHeightProvenanceIsWritten:
    """Re-running the tool must not launder a guess into a measurement.

    survey.json carried mach at a nominal 3.0 m, and the only record of that was a `u_source`
    sentence hand-edited into the file. The tool wrote six keys and none of them was that one, so
    the next run would have overwritten the admission and left the number reading as surveyed. On
    the measured 16.6 m pair, 1 m of height error is 206 mm of 3D baseline -- 41x what the fleet's
    14.5 us clock sync is worth -- so this is a physical term, not bookkeeping.
    """

    def _run(self, tmp_path, extra, monkeypatch):
        import json
        s1, o = _series(1_700_000_000_000_000, 400, 30.0, (0.0, 0.0), seed=1)
        s2, _ = _series(1_700_000_000_000_000, 400, 30.0, (-16.6, -0.3), seed=2)
        s3, _ = _series(1_700_000_000_000_000, 400, 30.0, (4.0, 12.0), seed=3)
        by = {"a": s1, "b": s2, "c": s3}
        monkeypatch.setattr(NS, "fetch", lambda n, timeout=300.0: n)
        monkeypatch.setattr(NS, "epochs", lambda text: by[text])
        out = tmp_path / "s.json"
        rc = NS.main(["a", "b", "c", "--out", str(out),
                      "--heights", "a=0", "b=3.1", "c=0"] + extra)
        return rc, (json.loads(out.read_text()) if out.exists() else None)

    def test_a_stated_source_and_sigma_reach_the_file_and_the_loader(self, tmp_path, monkeypatch):
        from hear.backend import survey as SV
        rc, d = self._run(tmp_path, [
            "--height-source", "a=datum", "b=tape from the sill", "c=datum",
            "--sigma-u", "a=0.0", "b=0.05", "c=0.0"], monkeypatch)
        assert rc == 0
        b = [n for n in d["nodes"] if n["name"] == "b"][0]
        assert b["u_source"] == "tape from the sill" and b["sigma_u_m"] == 0.05
        sv = SV.from_dict(d)
        assert sv.height_provenance()["unmeasured"] == []

    def test_an_unqualified_height_says_so_in_the_file_rather_than_looking_surveyed(
            self, tmp_path, monkeypatch):
        from hear.backend import survey as SV
        rc, d = self._run(tmp_path, [], monkeypatch)
        assert rc == 0
        b = [n for n in d["nodes"] if n["name"] == "b"][0]
        assert "NOT stated" in b["u_source"], b
        assert "sigma_u_m" not in b, "an unstated vertical sigma must not be written as 0.0"
        assert SV.from_dict(d).height_provenance()["unmeasured"] == [1, 2, 3]

    def test_the_horizontal_sigma_is_never_copied_into_the_vertical_one(
            self, tmp_path, monkeypatch):
        rc, d = self._run(tmp_path, ["--sigma-u", "b=0.05"], monkeypatch)
        assert rc == 0
        b = [n for n in d["nodes"] if n["name"] == "b"][0]
        assert b["sigma_u_m"] == 0.05 and b["sigma_m"] != 0.05

    def test_a_provenance_naming_a_node_not_being_surveyed_is_refused(
            self, tmp_path, monkeypatch):
        """A typo attaches the sentence to nothing and leaves the real node unlabelled, which is
        exactly the silent outcome this whole change exists to stop."""
        rc, d = self._run(tmp_path, ["--sigma-u", "mach=0.05"], monkeypatch)
        assert rc == 3 and d is None
