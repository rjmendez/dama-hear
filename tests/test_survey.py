"""Node survey: what it loads, and the eleven things it refuses to load."""
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.backend import survey as SV       # noqa: E402
from hear.solve import placement as PL      # noqa: E402

SQUARE = [(1, 0.0, 0.0, 0.0), (2, 100.0, 0.0, 0.0), (3, 100.0, 100.0, 0.0), (4, 0.0, 100.0, 0.0)]
LINE = [(1, 0.0, 0.0, 0.0), (2, 50.0, 0.0, 0.0), (3, 100.0, 0.0, 0.0), (4, 150.0, 0.0, 0.0)]


def doc(rows, **over):
    d = {"frame": "enu_local", "units": "m",
         "nodes": [{"node_id": i, "e_m": e, "n_m": n, "u_m": u} for i, e, n, u in rows]}
    d.update(over)
    return d


class TestOrdering:
    def test_ids_sort_but_positions_keep_the_order_asked_for(self):
        """The backend aligns arrivals to node_ids positionally. A method that quietly sorted its
        rows would mislabel every arrival in the array and no residual would show it."""
        sv = SV.from_dict(doc([SQUARE[2], SQUARE[0], SQUARE[3], SQUARE[1]]))
        assert sv.ids == [1, 2, 3, 4]
        P = sv.positions([3, 1, 2])
        assert P.shape == (3, 3)
        assert [float(r[0]) for r in P] == [100.0, 0.0, 100.0]
        assert [float(r[1]) for r in P] == [100.0, 0.0, 0.0]

    def test_position_is_three_float64_metres_of_east_north_up(self):
        sv = SV.from_dict(doc([(1, 3.0, -4.0, 5.0), (2, 100.0, 0.0, 0.0), (3, 0.0, 100.0, 0.0)]))
        p = sv.position(1)
        assert p.shape == (3,) and p.dtype == np.float64
        assert list(p) == [3.0, -4.0, 5.0]
        assert 1 in sv and 99 not in sv and len(sv) == 3

    def test_projecting_to_2d_drops_up_and_only_up(self):
        rows = [(1, 0.0, 0.0, 7.0), (2, 100.0, 0.0, -3.0), (3, 50.0, 80.0, 12.0)]
        sv = SV.from_dict(doc(rows))
        ids = [3, 1, 2]
        assert np.allclose(sv.positions_2d(ids), sv.positions(ids)[:, :2])
        assert sv.positions_2d(ids).shape == (3, 2)
        assert float(np.abs(sv.positions(ids)[:, 2]).max()) > 0.0, "the fixture must have relief"

    def test_an_unsurveyed_node_has_no_position(self):
        sv = SV.from_dict(doc(SQUARE))
        with pytest.raises(SV.SurveyError, match="77"):
            sv.position(77)


class TestRefusals:
    def test_wrong_frame_or_units_are_refused(self):
        """The file that is silently in the wrong frame is the failure this module exists for:
        lat/lon read as metres puts every node ~100 km from where it is, at no residual."""
        with pytest.raises(SV.SurveyError, match="frame"):
            SV.from_dict(doc(SQUARE, frame="wgs84"))
        with pytest.raises(SV.SurveyError, match="units"):
            SV.from_dict(doc(SQUARE, units="ft"))

    def test_duplicate_node_id_is_refused_and_named(self):
        d = doc(SQUARE)
        d["nodes"][3]["node_id"] = 2
        with pytest.raises(SV.SurveyError, match=r"duplicate node_id 2\b"):
            SV.from_dict(d)

    @pytest.mark.parametrize("key", ["e_m", "n_m", "u_m"])
    def test_a_missing_coordinate_is_refused_not_zeroed(self, key):
        """Same reason telemetry.pack sends a sentinel (hear/node/telemetry.py:55-58): a node
        defaulted to up=0 looks exactly like a node that was surveyed at ground level.

        All THREE keys, not just u_m. A guard covering only the one the module's docstring names
        loads a node whose e_m was dropped in transcription at east=0, in silence, which is the
        same silent-wrong survey by a different letter."""
        d = doc([(1, 20.0, 30.0, 4.0), (2, 100.0, 0.0, 0.0), (3, 0.0, 100.0, 0.0)])
        del d["nodes"][0][key]
        with pytest.raises(SV.SurveyError, match="node 1 has no %s" % key):
            SV.from_dict(d)

    @pytest.mark.parametrize("key", ["e_m", "n_m", "u_m"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_coordinates_are_refused(self, bad, key):
        d = doc(SQUARE)
        d["nodes"][1][key] = bad
        with pytest.raises(SV.SurveyError, match="non-finite %s" % key):
            SV.from_dict(d)

    @pytest.mark.parametrize("key", ["e_m", "n_m", "u_m"])
    def test_a_non_numeric_coordinate_is_refused(self, key):
        # 12.5, not 100.0: a value that coerces to a still-legal layout, so a guard that misses
        # this key gets caught here and not incidentally by the coincidence check downstream.
        d = doc(SQUARE)
        d["nodes"][1][key] = "12.5"
        with pytest.raises(SV.SurveyError, match="has %s '12.5', which is not a number" % key):
            SV.from_dict(d)

    def test_two_entries_five_centimetres_apart_are_one_node(self):
        d = doc(SQUARE)
        d["nodes"][2]["e_m"] = 100.05
        d["nodes"][2]["n_m"] = 0.0
        with pytest.raises(SV.SurveyError, match=r"nodes 2 and 3 are 0\.050 m apart"):
            SV.from_dict(d)

    @pytest.mark.parametrize("bad", [70000, -1, True, 1.0, "1", None])
    def test_a_node_id_the_wire_cannot_carry_is_refused(self, bad):
        """node_id is a uint16 on the v2 frame. An id outside that range can be surveyed but can
        never arrive, so the survey is the last place it can be caught."""
        d = doc(SQUARE)
        d["nodes"][1]["node_id"] = bad
        with pytest.raises(SV.SurveyError, match="node_id"):
            SV.from_dict(d)

    def test_min_nodes_names_both_counts(self):
        with pytest.raises(SV.SurveyError, match=r"has 3 nodes, solver needs 4"):
            SV.from_dict(doc(SQUARE[:3]), min_nodes=4)

    def test_an_empty_node_list_is_refused(self):
        with pytest.raises(SV.SurveyError, match="0 nodes"):
            SV.from_dict(doc([]))

    def test_collinear_is_refused_and_placement_agrees_it_is_singular(self):
        """Cross-module: survey's refusal and placement.dop's singularity must fire on the SAME
        layout. If these two ever disagree, MIN_LINEARITY has drifted away from the geometry it
        stands in for. The probe sits ON the node line, which is where dop actually goes infinite;
        off it a collinear array still has a mirror twin dop cannot see, which is the other half
        of why this is a refusal and not a verdict."""
        with pytest.raises(SV.SurveyError, match="collinear"):
            SV.from_dict(doc(LINE))
        assert PL.dop([(e, n) for _, e, n, _ in LINE], (75.0, 0.0))["singular"] is True

        bent = list(LINE)
        bent[2] = (3, 100.0, 20.0, 0.0)
        sv = SV.from_dict(doc(bent))
        assert sv.linearity() >= SV.MIN_LINEARITY
        assert math.isfinite(PL.dop(sv.positions_2d(sv.ids), (75.0, 0.0))["dop"])

    def test_min_linearity_is_placements_number_not_a_second_copy(self):
        assert SV.MIN_LINEARITY is PL.COLLINEAR_LINEARITY

    def test_the_refusal_reads_min_linearity_and_is_not_a_hardcoded_number(self, monkeypatch):
        """The identity assert above pins the CONSTANT; it does not pin the COMPARISON. Measured,
        LINE has linearity 0.0000 and the bent set 0.1494, so a guard hardcoded to any threshold
        in (0.0, 0.1494] satisfies both halves of the cross-check and imports PL for decoration.
        Move the constant and the refusal must move with it, in both directions."""
        bent = list(LINE)
        bent[2] = (3, 100.0, 20.0, 0.0)
        assert SV.from_dict(doc(bent)).linearity() == pytest.approx(0.149422, abs=1e-5)

        monkeypatch.setattr(SV, "MIN_LINEARITY", 0.5)
        with pytest.raises(SV.SurveyError, match="collinear"):
            SV.from_dict(doc(bent))

        monkeypatch.setattr(SV, "MIN_LINEARITY", 0.0)
        assert SV.from_dict(doc(LINE)).linearity() == pytest.approx(0.0, abs=1e-12)


class TestGeometry:
    RELIEF = [(1, 0.0, 0.0, 0.0), (2, 100.0, 0.0, 0.0), (3, 100.0, 100.0, 0.0),
              (4, 0.0, 100.0, 30.0)]

    # RELIEF's widest pair IS its bounding-box corner pair, so it cannot tell a real diameter from
    # a box diagonal. DIAMOND puts the axis extremes on four different nodes: widest real pair
    # 107.703 m, box diagonal 146.969 m.
    DIAMOND = [(1, 0.0, 50.0, 0.0), (2, 50.0, 0.0, 0.0), (3, 100.0, 50.0, 0.0),
               (4, 50.0, 100.0, 40.0)]

    def test_diameter_is_a_pair_of_real_nodes_not_a_bounding_box(self):
        """A box diagonal over-estimates, which widens associate's window on the safe side -- but
        it is not the number this docstring promises and no fixture with the extremes on one pair
        can tell the two apart."""
        sv = SV.from_dict(doc(self.DIAMOND))
        P = sv.positions(sv.ids)
        assert sv.diameter_m() == pytest.approx(math.sqrt(100.0 ** 2 + 40.0 ** 2), abs=1e-9)
        assert float(np.linalg.norm(P.max(0) - P.min(0))) - sv.diameter_m() > 39.0
        assert sv.diameter_m() > 100.0      # still 3D: the widest HORIZONTAL pair is exactly 100 m

    def test_diameter_is_the_3d_distance_not_the_horizontal_one(self):
        """associate turns this into its window bound. Computed horizontally it would be 4 m of
        propagation short on this layout, and the window would be too tight by that much."""
        sv = SV.from_dict(doc(self.RELIEF))
        assert sv.diameter_m() == pytest.approx(math.sqrt(100.0 ** 2 + 100.0 ** 2 + 30.0 ** 2),
                                                abs=1e-9)      # exact arithmetic, no fit involved
        assert sv.diameter_m() > math.sqrt(2) * 100.0

    def test_flat_survey_passes_the_2d_assumption_and_a_hilly_one_costs_its_relief(self):
        flat = SV.from_dict(doc(SQUARE)).validate_2d_assumption()
        assert flat["ok"] is True and flat["note"] is None
        assert flat["vertical_spread_m"] == pytest.approx(0.0, abs=1e-9)

        hilly = SV.from_dict(doc(self.RELIEF)).validate_2d_assumption()
        assert hilly["ok"] is False
        assert hilly["vertical_spread_m"] == pytest.approx(30.0, abs=1e-9)
        assert "30.0" in hilly["note"]

    def test_vertical_spread_is_relief_not_altitude(self):
        """Every fixture above sits with min(u) == 0, where max(u) and max-min are the same number.
        An array on a 100 m ridge has 100 m of altitude and 0 m of relief; reporting the altitude
        tells the operator the 2D projection costs 100 m when it costs nothing."""
        plateau = [(i, e, n, 100.0) for i, e, n, _ in SQUARE]
        flat = SV.from_dict(doc(plateau)).validate_2d_assumption()
        assert flat["vertical_spread_m"] == pytest.approx(0.0, abs=1e-9)
        assert flat["ok"] is True and flat["note"] is None

        ridge = [(i, e, n, u + 100.0) for i, e, n, u in self.RELIEF]
        hilly = SV.from_dict(doc(ridge)).validate_2d_assumption()
        assert hilly["vertical_spread_m"] == pytest.approx(30.0, abs=1e-9)
        assert hilly["ok"] is False and "30.0" in hilly["note"]

    def test_linearity_matches_placement_on_the_same_nodes(self):
        sv = SV.from_dict(doc(SQUARE))
        assert sv.linearity() == PL.linearity(sv.positions(sv.ids))
        assert sv.linearity() > 0.5, "a square is nowhere near a line"


class TestFile:
    def test_load_survey_round_trips_through_to_dict(self, tmp_path):
        d = doc(TestGeometry.RELIEF, origin={"lat_deg": 34.0, "lon_deg": -118.0, "alt_m": 120.0})
        d["nodes"][0]["name"] = "rear"
        d["nodes"][0]["sigma_m"] = 0.05
        p = tmp_path / "survey.json"
        p.write_text(json.dumps(d))

        sv = SV.load_survey(str(p))
        assert sv.names[1] == "rear" and sv.sigma_m[1] == 0.05
        assert sv.names[2] == "" and sv.sigma_m[2] == 0.0
        assert sv.origin == {"lat_deg": 34.0, "lon_deg": -118.0, "alt_m": 120.0}

        again = SV.from_dict(sv.to_dict())
        assert again.ids == sv.ids
        assert np.allclose(again.positions(again.ids), sv.positions(sv.ids))
        assert again.origin == sv.origin

    def test_origin_is_carried_and_never_applied_to_the_coordinates(self):
        """Metadata only. If anyone wires geodesy in here, these coordinates would move."""
        d = doc(SQUARE, origin={"lat_deg": 34.0, "lon_deg": -118.0, "alt_m": 120.0})
        sv = SV.from_dict(d)
        assert list(sv.position(1)) == [0.0, 0.0, 0.0]
        assert sv.diameter_m() == pytest.approx(math.sqrt(2) * 100.0, abs=1e-9)


class TheSurveyStatesWhichNodesCanRange:
    """⚠️A SURVEYED POSITION IS NOT PERMISSION TO USE THE NODE AS AN ARRIVAL.

    hear/nodeclass.py has always known that a PUC timed by NTP is 3 ms -- 1.0 m of range -- and
    must be refused as a TDoA arrival. `require_arrival` was called from tests and from NOWHERE
    else, so nothing in the pipeline ever asked. Adding a non-ranging node to the survey for its
    position then made it indistinguishable from a PPS node at 3.4 cm.
    """

    @staticmethod
    def _d(nodes):
        return {"frame": "enu_local", "units": "m", "nodes": nodes}

    @staticmethod
    def _n(nid, name, e, n, cls=None):
        o = {"node_id": nid, "name": name, "e_m": e, "n_m": n, "u_m": 0.0, "sigma_m": 0.5}
        if cls is not None:
            o["class"] = cls
        return o

    def test_an_ntp_class_node_is_not_an_arrival_source(self):
        s = SV.from_dict(self._d([
            self._n(1, "a", 0.0, 0.0, "xiao-s3-pps"),
            self._n(2, "b", -16.0, 0.0, "xiao-s3-pps"),
            self._n(3, "c", -4.0, 10.0, "xiao-s3-pps"),
            self._n(4, "puc", -22.0, 9.0, "puc-ntp"),
        ]))
        assert s.arrival_ids() == [1, 2, 3], "the NTP-timed node must not be a TDoA arrival"
        assert 4 in s.ids, "but it must still be IN the survey -- it has a position"

    def test_a_pps_puc_would_be_admitted(self):
        # the same hardware, once its 1PPS is wired: 3 ms -> 100 us
        s = SV.from_dict(self._d([
            self._n(1, "a", 0.0, 0.0, "xiao-s3-pps"),
            self._n(2, "b", -16.0, 0.0, "xiao-s3-pps"),
            self._n(3, "puc", -22.0, 9.0, "puc-pps"),
        ]))
        assert s.arrival_ids() == [1, 2, 3]

    def test_an_unstated_class_is_included_not_silently_dropped(self):
        # every survey written before the field existed omits it; dropping those nodes would be a
        # worse failure than the one this guards. Unset means "unstated", not "no".
        s = SV.from_dict(self._d([
            self._n(1, "a", 0.0, 0.0),
            self._n(2, "b", -16.0, 0.0),
            self._n(3, "c", -4.0, 10.0),
        ]))
        assert s.arrival_ids() == [1, 2, 3]

    def test_an_unknown_class_name_is_included_rather_than_refused(self):
        s = SV.from_dict(self._d([
            self._n(1, "a", 0.0, 0.0, "no-such-class"),
            self._n(2, "b", -16.0, 0.0),
            self._n(3, "c", -4.0, 10.0),
        ]))
        assert 1 in s.arrival_ids()

    def test_a_non_string_class_is_refused_at_parse(self):
        import pytest as _p
        with _p.raises(SV.SurveyError):
            SV.from_dict(self._d([
                {"node_id": 1, "name": "a", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0,
                 "sigma_m": 0.5, "class": 7},
                self._n(2, "b", -16.0, 0.0),
                self._n(3, "c", -4.0, 10.0),
            ]))

    def test_the_shipped_survey_refuses_the_puc(self):
        import os
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "survey.json")
        s = SV.load_survey(p)
        by = {s.names[i]: i for i in s.ids}
        assert "puc" in by, "the PUC is in the survey for its position"
        assert by["puc"] not in s.arrival_ids(), \
            "the PUC is PROVISIONAL and NTP-timed -- it must not be a TDoA arrival"
        assert s.sigma_m[by["puc"]] >= 5.0, \
            "its sigma must record that the position came from a 6.5 m rms GPS scatter"
