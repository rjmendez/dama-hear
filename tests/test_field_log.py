"""tools/field_log.py -- the ground truth a session captures, and the four ways of losing it.

Every test here is about something that cannot be recovered afterwards. A receiver whose height
was never written down cannot enter a survey at all (hear/backend/survey.py refuses a node with
no u_m). A height whose provenance was dropped reads as a measurement. A GNSS ellipsoid height
converted through a placeholder origin puts a receiver hundreds of metres in the air at a perfect residual. A
mark rounded to the second without saying so claims a precision it never had.
"""
import json
import math
import os

import pytest

from hear.backend import survey as SV
from tools import field_log as FL

# A fictional site shaped like survey.json: the origin's h_ell_m is the 0.0 placeholder
# tools/node_survey.py writes, and SITE_H_ELL_M stands in for a real ellipsoid height.
SITE_LAT = 10.292644299999992
SITE_LON = 20.8778792
SITE_H_ELL_M = 312.5

BASE_DOC = {
    "frame": "enu_local", "units": "m",
    "origin": {"lat_deg": SITE_LAT, "lon_deg": SITE_LON, "h_ell_m": 0.0,
               "source": "median of nyquist; height datum is the --heights argument, NOT GNSS"},
    "nodes": [
        {"node_id": 1, "name": "nyquist", "e_m": -0.0, "n_m": 0.0, "u_m": 0.0,
         "sigma_m": 0.717, "u_source": "datum", "sigma_u_m": 0.0},
        {"node_id": 2, "name": "mach", "e_m": -16.602, "n_m": -0.272, "u_m": 3.0,
         "sigma_m": 0.521, "u_source": "nominal storey, +/-1.0 m assumed", "sigma_u_m": 1.0},
    ],
}


@pytest.fixture
def base(tmp_path):
    p = tmp_path / "survey.json"
    p.write_text(json.dumps(BASE_DOC), encoding="utf-8")
    return str(p)


def _place(**kw):
    """A placement with the mandatory fields filled in; every test overrides what it is about."""
    d = dict(name="gauss", node_id=3, height_m=0.90, sigma_m=0.15, ref="nyquist",
             range_m=24.30, bearing_deg=107.6)
    d.update(kw)
    return FL.placement_record(**d)


class TestAMissingHeightIsRefusedNotZeroed:
    """hear/backend/survey.py:_read_node raises "node N has no u_m: a missing coordinate is never
    0.0". This is that rule one layer earlier, where it can still be acted on: in the field."""

    def test_emit_survey_refuses_a_receiver_with_no_height(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(height_m=None))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=base)
        assert "no recorded height" in str(e.value)
        assert "never 0.0" in str(e.value)
        assert "gauss" in str(e.value)

    def test_the_refusal_writes_no_file_and_no_zero_height(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        out = str(tmp_path / "out.json")
        FL.append_record(log, _place(height_m=None))
        rc = FL.main(["emit-survey", "--log", log, "--base", base, "--out", out])
        assert rc == 2
        assert not os.path.exists(out)

    def test_a_height_of_zero_is_a_height_and_is_not_confused_with_absent(self, base, tmp_path):
        """0.0 m above the datum is a real, legitimate measurement. Only ABSENT is refused."""
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(height_m=0.0, u_source="tape, both sills", sigma_u_m=0.02))
        sv, _ = FL.build_survey(FL.read_log(log), base_path=base)
        assert sv.position(3)[2] == pytest.approx(0.0)


class TestProvenanceSurvivesTheRoundTrip:
    def test_u_source_and_sigma_u_reach_to_dict_and_come_back(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(u_source="tape, nyquist sill to gauss mic", sigma_u_m=0.05))
        sv, _ = FL.build_survey(FL.read_log(log), base_path=base)
        d = sv.to_dict()
        got = [n for n in d["nodes"] if n["node_id"] == 3][0]
        assert got["u_source"] == "tape, nyquist sill to gauss mic"
        assert got["sigma_u_m"] == pytest.approx(0.05)

        again = SV.from_dict(json.loads(json.dumps(d)), min_nodes=3)
        assert again.u_source[3] == "tape, nyquist sill to gauss mic"
        assert again.sigma_u_m[3] == pytest.approx(0.05)
        pr = again.height_provenance()
        assert 3 not in pr["unmeasured"]          # 0.05 m is inside HEIGHT_SIGMA_TOL_M
        assert 2 in pr["unmeasured"]              # the base's nominal storey still is not

    def test_an_unstated_sigma_u_is_reported_unmeasured_not_zero(self, base, tmp_path):
        """The one reading the field cannot afford: "we never wrote down how good this height is"
        must not come back as "this height is exact"."""
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(u_source="tape from grade", sigma_u_m=None))
        sv, rep = FL.build_survey(FL.read_log(log), base_path=base)
        assert sv.sigma_u_m[3] is None
        assert "sigma_u_m" not in [n for n in sv.to_dict()["nodes"] if n["node_id"] == 3][0]
        pr = rep["provenance"]
        assert 3 in pr["unmeasured"]
        row = [r for r in pr["nodes"] if r["node_id"] == 3][0]
        assert row["sigma_u_m"] is None and row["sigma_u_m"] != 0.0

    def test_an_unstated_u_source_is_counted_as_unstated(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(u_source=None, sigma_u_m=0.05))
        sv, rep = FL.build_survey(FL.read_log(log), base_path=base)
        assert sv.u_source[3] is None
        assert rep["provenance"]["n_unstated"] == 1
        assert 3 in rep["provenance"]["unmeasured"]


class TestTheEllipsoidHeightTrap:
    """survey.json's origin says h_ell_m 0.0, so Survey.enu_of() on a real fix returns the whole
    ellipsoid height as u. That is a receiver hundreds of metres in the air with a residual nothing
    can see."""

    def test_the_trap_is_real_and_this_is_the_size_of_it(self, base):
        sv = SV.load_survey(base, min_nodes=2)
        u = sv.enu_of(SITE_LAT, SITE_LON, SITE_H_ELL_M)[2]
        assert u == pytest.approx(SITE_H_ELL_M, abs=1e-3)

    def test_emit_refuses_an_ellipsoid_height_against_a_placeholder_origin(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(range_m=None, bearing_deg=None,
                                     lat_deg=10.29353, lon_deg=20.87819,
                                     h_ell_m=SITE_H_ELL_M, height_m=0.9))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=base)
        msg = str(e.value)
        assert "placeholder" in msg
        assert "312.5 m in the air" in msg

    def test_the_placeholder_test_is_on_the_origin_height_not_on_its_prose(self, base, tmp_path):
        assert FL.origin_h_ell_is_placeholder({"lat_deg": 1.0, "lon_deg": 2.0, "h_ell_m": 0.0})
        assert FL.origin_h_ell_is_placeholder({"lat_deg": 1.0, "lon_deg": 2.0})
        assert FL.origin_h_ell_is_placeholder(None)
        assert not FL.origin_h_ell_is_placeholder({"h_ell_m": SITE_H_ELL_M})

    def test_two_stated_heights_that_disagree_are_refused_rather_than_one_being_dropped(
            self, base, tmp_path):
        """Both a tape height and an ellipsoid height, disagreeing by 0.9 m. Picking one and
        discarding the other is the bug class; the survey would look complete."""
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(range_m=None, bearing_deg=None,
                                     lat_deg=10.29353, lon_deg=20.87819,
                                     h_ell_m=SITE_H_ELL_M, height_m=0.9))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=base,
                            origin_h_ell_m=SITE_H_ELL_M)
        assert "disagree by 0.900 m" in str(e.value)

    def test_two_stated_heights_that_agree_inside_the_tolerance_are_accepted(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(range_m=None, bearing_deg=None,
                                     lat_deg=10.29353, lon_deg=20.87819,
                                     h_ell_m=SITE_H_ELL_M + 0.9, height_m=0.9,
                                     u_source="tape, cross-checked against RTK", sigma_u_m=0.05))
        sv, _ = FL.build_survey(FL.read_log(log), base_path=base, origin_h_ell_m=SITE_H_ELL_M)
        assert sv.position(3)[2] == pytest.approx(0.9)

    def test_a_wgs84_only_survey_refuses_to_default_the_origin_height(self, tmp_path):
        log = str(tmp_path / "f.jsonl")
        for i, (nm, lat, lon) in enumerate(
                [("a", 10.29264, 20.87788), ("b", 10.29353, 20.87819),
                 ("c", 10.29272, 20.87904)]):
            FL.append_record(log, FL.placement_record(
                name=nm, node_id=5 + i, height_m=None, sigma_m=0.02,
                lat_deg=lat, lon_deg=lon, h_ell_m=SITE_H_ELL_M + 1.0,
                u_source="RTK, ellipsoid", sigma_u_m=0.02))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log))
        assert "origin ellipsoid height" in str(e.value)

        sv, rep = FL.build_survey(FL.read_log(log), origin_h_ell_m=SITE_H_ELL_M)
        assert rep["mode"] == "wgs84"
        assert sv.position(5)[2] == pytest.approx(1.0, abs=0.01)
        assert sv.u_source[5] == "RTK, ellipsoid"


class TestTheLogIsAppendOnly:
    def test_a_second_run_does_not_rewrite_the_bytes_of_the_first(self, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(name="gauss", node_id=3))
        first = open(log, "rb").read()
        FL.append_record(log, _place(name="euler", node_id=4, bearing_deg=250.0))
        FL.append_record(log, FL.mark_record(1789239847, "shot", note="second run"))
        after = open(log, "rb").read()
        assert after.startswith(first)
        assert len(after) > len(first)
        assert len(FL.read_log(log)) == 3

    def test_a_correction_supersedes_by_appending_and_the_count_is_reported(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(height_m=0.5, u_source="paced", sigma_u_m=0.5))
        FL.append_record(log, _place(height_m=0.9, u_source="tape", sigma_u_m=0.02))
        recs = FL.read_log(log)
        assert len(recs) == 2                    # both rows survive; nothing was edited
        latest, superseded = FL.latest_placements(recs)
        assert latest["gauss"]["height_m"] == 0.9
        assert superseded["gauss"] == 1
        sv, rep = FL.build_survey(recs, base_path=base)
        assert sv.position(3)[2] == pytest.approx(0.9)
        assert rep["superseded"]["gauss"] == 1

    def test_a_line_that_does_not_parse_raises_with_its_number_rather_than_being_skipped(
            self, tmp_path):
        log = tmp_path / "f.jsonl"
        FL.append_record(str(log), _place())
        with open(str(log), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        FL.append_record(str(log), _place(name="euler", node_id=4))
        with pytest.raises(FL.FieldLogError) as e:
            FL.read_log(str(log))
        assert "line 2" in str(e.value)


class TestMarksAreSeconds:
    def test_a_sub_second_stamp_is_refused_not_rounded(self):
        with pytest.raises(FL.FieldLogError) as e:
            FL.parse_utc_second("2026-09-12T19:04:07.9Z")
        assert "sub-second" in str(e.value)
        with pytest.raises(FL.FieldLogError):
            FL.parse_utc_second("1789239847.9")

    def test_truncation_happens_only_when_asked_and_records_what_it_dropped(self):
        sec, trunc = FL.parse_utc_second("2026-09-12T19:04:07.9Z", truncate_subsecond=True)
        assert sec == 1789239847                 # floor, not round: 07.9 does not become 08
        assert trunc == "2026-09-12T19:04:07.9Z"
        rec = FL.mark_record(sec, "shot", truncated_from=trunc)
        assert rec["t_utc_s"] == 1789239847
        assert rec["t_truncated_from"] == "2026-09-12T19:04:07.9Z"

    def test_a_whole_second_carries_no_truncation_note(self):
        sec, trunc = FL.parse_utc_second("2026-09-12T19:04:07Z")
        assert (sec, trunc) == (1789239847, None)
        assert "t_truncated_from" not in FL.mark_record(sec, "shot")
        assert FL.parse_utc_second("1789239847") == (1789239847, None)

    def test_a_naive_stamp_is_refused_because_nothing_records_which_clock_it_was(self):
        with pytest.raises(FL.FieldLogError) as e:
            FL.parse_utc_second("2026-09-12T19:04:07")
        assert "no timezone" in str(e.value)

    def test_the_class_vocabulary_is_closed(self):
        assert FL.MARK_CLASSES == ("shot", "firework", "vehicle", "chirp", "thunder", "other")
        for c in FL.MARK_CLASSES:
            assert FL.mark_record(1789239847, c)["class"] == c
        with pytest.raises(FL.FieldLogError) as e:
            FL.mark_record(1789239847, "gunshot")
        assert "other" in str(e.value)

    def test_a_mark_time_that_is_not_an_integer_second_is_refused(self):
        with pytest.raises(FL.FieldLogError):
            FL.mark_record(1789239847.5, "shot")

    def test_the_cli_refuses_a_sub_second_mark_and_writes_nothing(self, tmp_path):
        log = str(tmp_path / "f.jsonl")
        rc = FL.main(["mark", "--log", log, "--at-time", "2026-09-12T19:04:07.9Z",
                      "--class", "shot"])
        assert rc == 2
        assert not os.path.exists(log)
        rc = FL.main(["mark", "--log", log, "--at-time", "2026-09-12T19:04:07.9Z",
                      "--class", "shot", "--truncate-subsecond"])
        assert rc == 0
        assert FL.read_log(log)[0]["t_truncated_from"] == "2026-09-12T19:04:07.9Z"


class TestPlacementGeometry:
    def test_a_magnetic_bearing_without_a_declination_is_refused(self):
        with pytest.raises(FL.FieldLogError) as e:
            _place(bearing_ref="magnetic")
        assert "declination" in str(e.value)

    def test_a_magnetic_bearing_is_rotated_by_the_stated_declination(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(range_m=24.30, bearing_deg=118.5, bearing_ref="magnetic",
                                     declination_deg=-10.9))
        sv, _ = FL.build_survey(FL.read_log(log), base_path=base)
        e, n = sv.position(3)[0], sv.position(3)[1]
        assert e == pytest.approx(24.30 * math.sin(math.radians(107.6)), abs=1e-6)
        assert n == pytest.approx(24.30 * math.cos(math.radians(107.6)), abs=1e-6)

    def test_a_slope_range_is_reduced_to_horizontal_and_an_impossible_one_is_refused(
            self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(range_m=5.0, bearing_deg=0.0, range_kind="slope",
                                     height_m=3.0))
        sv, _ = FL.build_survey(FL.read_log(log), base_path=base)
        assert sv.position(3)[1] == pytest.approx(4.0)
        log2 = str(tmp_path / "g.jsonl")
        FL.append_record(log2, _place(range_m=2.0, bearing_deg=0.0, range_kind="slope",
                                      height_m=3.0))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log2), base_path=base)
        assert "not longer than" in str(e.value)

    def test_a_node_id_the_base_already_uses_is_refused(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(node_id=2))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=base)
        assert "already gives to 'mach'" in str(e.value)

    def test_an_unknown_reference_node_is_refused(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(ref="planck"))
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=base)
        assert "not in the base survey" in str(e.value)

    def test_an_unstated_horizontal_sigma_is_refused_at_record_time(self):
        with pytest.raises(FL.FieldLogError) as e:
            _place(sigma_m=None)
        assert "sigma-m" in str(e.value)

    def test_a_node_id_is_required_and_never_inferred(self):
        with pytest.raises(FL.FieldLogError) as e:
            _place(node_id=None)
        assert "node-id" in str(e.value)

    def test_the_base_survey_nodes_are_carried_through_unchanged(self, base, tmp_path):
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place(u_source="tape", sigma_u_m=0.05))
        sv, rep = FL.build_survey(FL.read_log(log), base_path=base)
        assert rep["carried"] == ["mach", "nyquist"]
        assert sv.u_source[2] == "nominal storey, +/-1.0 m assumed"
        assert sv.sigma_u_m[2] == pytest.approx(1.0)
        assert sv.position(2).tolist() == pytest.approx([-16.602, -0.272, 3.0])


class TestTemperature:
    def test_the_instrument_and_the_place_are_both_required(self):
        with pytest.raises(FL.FieldLogError):
            FL.temp_record(21.4, "", "nyquist")
        with pytest.raises(FL.FieldLogError):
            FL.temp_record(21.4, "kestrel 3000", "")

    def test_a_fahrenheit_reading_is_refused_rather_than_stored_as_celsius(self):
        with pytest.raises(FL.FieldLogError) as e:
            FL.temp_record(70.5, "kestrel 3000", "nyquist")
        assert "Fahrenheit" in str(e.value)

    def test_the_sound_speed_comes_from_the_repos_one_definition_of_c(self):
        from hear.solve.shockwave import sound_speed
        rec = FL.temp_record(21.4, "kestrel 3000", "nyquist")
        assert rec["c_mps"] == pytest.approx(sound_speed(21.4))


class TestAFictionalOriginPlacesNothing:
    """The public survey.json marks its origin fictional; converting a lat/lon through it misplaces
    a receiver with no error, so it is refused unless HEAR_SITE_ORIGIN supplies the real one."""

    def _fictional_base(self, tmp_path):
        d = json.loads(json.dumps(BASE_DOC))
        d["origin"]["fictional"] = True
        p = tmp_path / "fictional.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        return str(p)

    def _latlon_place(self):
        return _place(ref=None, range_m=None, bearing_deg=None,
                      lat_deg=SITE_LAT + 0.0002, lon_deg=SITE_LON + 0.0002, height_m=0.9)

    def test_a_latlon_placement_against_a_fictional_origin_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SV.SITE_ORIGIN_ENV, raising=False)
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, self._latlon_place())
        with pytest.raises(FL.FieldLogError) as e:
            FL.build_survey(FL.read_log(log), base_path=self._fictional_base(tmp_path))
        assert "fictional" in str(e.value) and SV.SITE_ORIGIN_ENV in str(e.value)

    def test_the_real_origin_from_the_environment_places_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, "%.7f,%.7f,%.1f" % (SITE_LAT, SITE_LON, 0.0))
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, self._latlon_place())
        sv, rep = FL.build_survey(FL.read_log(log), base_path=self._fictional_base(tmp_path))
        assert rep["added"] == ["gauss"] and sv.position(3)[2] == pytest.approx(0.9)

    def test_a_referenced_placement_needs_no_origin_at_all(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SV.SITE_ORIGIN_ENV, raising=False)
        log = str(tmp_path / "f.jsonl")
        FL.append_record(log, _place())
        sv, _ = FL.build_survey(FL.read_log(log), base_path=self._fictional_base(tmp_path))
        assert 3 in sv.ids
