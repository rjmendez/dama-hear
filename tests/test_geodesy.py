"""hear/geodesy.py -- the degrees-to-metres conversion, checked against closed-form truth.

These are not regression tests against whatever the code happened to print. Each one pins a value
that is fixed by the WGS84 definition or by a symmetry, so the module cannot be quietly wrong in
the way its predecessor was: a spherical approximation returns plausible numbers everywhere and
announces nothing.
"""
import math

import pytest

from hear import geodesy as G


class TestDefiningConstants:
    def test_semi_minor_axis(self):
        # b = a(1-f) is definitional, and the published value is 6356752.314245 m.
        assert G.WGS84_B == pytest.approx(6356752.314245, abs=1e-6)

    def test_eccentricity_squared(self):
        assert G.WGS84_E2 == pytest.approx(0.00669437999014, rel=1e-12)


class TestEcefAnchors:
    """Points where the answer is forced by the definition, not by a reference implementation."""

    def test_equator_prime_meridian_is_semi_major_axis(self):
        x, y, z = G.geodetic_to_ecef(0.0, 0.0, 0.0)
        assert x == pytest.approx(G.WGS84_A, abs=1e-6)
        assert y == pytest.approx(0.0, abs=1e-9)
        assert z == pytest.approx(0.0, abs=1e-9)

    def test_north_pole_is_semi_minor_axis(self):
        x, y, z = G.geodetic_to_ecef(90.0, 0.0, 0.0)
        assert math.hypot(x, y) == pytest.approx(0.0, abs=1e-6)
        assert z == pytest.approx(G.WGS84_B, abs=1e-6)

    def test_equator_90_east_is_on_the_y_axis(self):
        x, y, z = G.geodetic_to_ecef(0.0, 90.0, 0.0)
        assert x == pytest.approx(0.0, abs=1e-6)
        assert y == pytest.approx(G.WGS84_A, abs=1e-6)

    def test_height_at_the_pole_adds_along_z(self):
        _, _, z0 = G.geodetic_to_ecef(90.0, 0.0, 0.0)
        _, _, z1 = G.geodetic_to_ecef(90.0, 0.0, 100.0)
        assert z1 - z0 == pytest.approx(100.0, abs=1e-9)


class TestRoundTrips:
    CASES = [(0.0, 0.0, 0.0), (40.2925513, -76.1221659, 234.0), (-33.8688, 151.2093, 58.0),
             (89.9, 179.9, -420.0), (-89.9, -179.9, 8848.0), (51.4778, 0.0, 0.0)]

    @pytest.mark.parametrize("lat,lon,h", CASES)
    def test_ecef_round_trip_is_exact(self, lat, lon, h):
        la, lo, hh = G.ecef_to_geodetic(*G.geodetic_to_ecef(lat, lon, h))
        assert la == pytest.approx(lat, abs=1e-9)
        assert ((lo - lon + 180) % 360) - 180 == pytest.approx(0.0, abs=1e-9)
        assert hh == pytest.approx(h, abs=1e-6)

    @pytest.mark.parametrize("lat,lon,h", CASES)
    def test_enu_round_trip_is_exact(self, lat, lon, h):
        o = (40.0, -76.0, 200.0)
        e, n, u = G.geodetic_to_enu(lat, lon, h, *o)
        la, lo, hh = G.enu_to_geodetic(e, n, u, *o)
        assert la == pytest.approx(lat, abs=1e-9)
        assert ((lo - lon + 180) % 360) - 180 == pytest.approx(0.0, abs=1e-9)
        assert hh == pytest.approx(h, abs=1e-6)


class TestEnuFrame:
    ORIGIN = (40.2925513, -76.1221659, 234.0)

    def test_origin_maps_to_zero(self):
        e, n, u = G.geodetic_to_enu(*self.ORIGIN, *self.ORIGIN)
        assert (abs(e), abs(n), abs(u)) == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)

    def test_pure_height_change_is_pure_up(self):
        lat, lon, h = self.ORIGIN
        e, n, u = G.geodetic_to_enu(lat, lon, h + 25.0, *self.ORIGIN)
        assert u == pytest.approx(25.0, abs=1e-6)
        assert e == pytest.approx(0.0, abs=1e-9)
        assert n == pytest.approx(0.0, abs=1e-9)

    def test_due_north_has_no_east_component(self):
        lat, lon, h = self.ORIGIN
        e, n, u = G.geodetic_to_enu(lat + 0.001, lon, h, *self.ORIGIN)
        assert e == pytest.approx(0.0, abs=1e-9)
        assert n > 100.0

    def test_due_east_has_no_north_component(self):
        # Only true to first order -- a due-east step on the ellipsoid follows a parallel, which
        # curves poleward of the great circle. At 0.001 deg the deviation is sub-millimetre.
        lat, lon, h = self.ORIGIN
        e, n, u = G.geodetic_to_enu(lat, lon + 0.001, h, *self.ORIGIN)
        assert e > 80.0
        assert abs(n) < 1e-3

    def test_basis_is_right_handed_and_orthonormal(self):
        e, n, u = G._enu_basis(40.0, -76.0)
        for v in (e, n, u):
            assert math.sqrt(sum(c * c for c in v)) == pytest.approx(1.0, abs=1e-12)
        for a, b in ((e, n), (n, u), (u, e)):
            assert sum(x * y for x, y in zip(a, b)) == pytest.approx(0.0, abs=1e-12)
        cross = (e[1] * n[2] - e[2] * n[1], e[2] * n[0] - e[0] * n[2], e[0] * n[1] - e[1] * n[0])
        assert sum(x * y for x, y in zip(cross, u)) == pytest.approx(1.0, abs=1e-12)


class TestTheBugThisReplaces:
    """The ad hoc spherical formula, pinned so the difference is a number and not an opinion."""

    R_SPHERE = 6371000.0

    def test_a_degree_of_latitude_is_not_the_spherical_value(self):
        # Ellipsoidal: one degree of latitude at 40 deg N is 111.03 km, not 111.19 km.
        a = G.geodetic_to_enu(40.5, -76.0, 0.0, 40.0, -76.0, 0.0)
        true_m = math.hypot(a[0], a[1]) / 0.5
        sphere_m = math.radians(1.0) * self.R_SPHERE
        assert true_m == pytest.approx(111030.0, abs=60.0)
        assert sphere_m == pytest.approx(111194.0, abs=60.0)
        # The sphere is high by ~0.15%: a bias, not noise, so it does not average away.
        assert (sphere_m - true_m) / true_m == pytest.approx(0.00148, abs=2e-4)

    def test_dropping_height_understates_a_baseline(self):
        # Shaped on one snapshot of the nyquist/mach pair: 17.9 m apart horizontally, 11.6 m in
        # height. Treat the numbers as a FIXTURE, not as surveyed truth -- that height difference
        # was single-epoch GNSS noise (vAcc 2.9 m) and reversed sign on the next reading. What is
        # being pinned is the arithmetic, which holds whatever the real geometry turns out to be.
        nyq = (40.2925513, -76.1221659, 234.0)
        mach = (40.2925364, -76.1223763, 222.4)
        e, n, u = G.geodetic_to_enu(*mach, *nyq)
        horiz = math.hypot(e, n)
        full = G.ecef_distance(nyq, mach)
        assert horiz == pytest.approx(17.97, abs=0.05)
        assert full == pytest.approx(21.39, abs=0.05)
        # 19% larger, which is 10 ms of admissible TDoA that a 2D baseline would have refused.
        assert full / horiz == pytest.approx(1.19, abs=0.01)
        assert (full - horiz) / 343.0 * 1000.0 == pytest.approx(9.97, abs=0.2)


class TestFrameError:
    def test_small_arrays_are_flat_enough_to_ignore(self):
        assert G.enu_frame_error_m(200.0, 40.0) < 0.005

    def test_large_arrays_are_not(self):
        assert G.enu_frame_error_m(10000.0, 40.0) > 5.0

    def test_error_is_quadratic_in_span(self):
        a = G.enu_frame_error_m(1000.0, 40.0)
        b = G.enu_frame_error_m(2000.0, 40.0)
        assert b / a == pytest.approx(4.0, rel=1e-9)


class TestCentroid:
    def test_centroid_of_one_point_is_that_point(self):
        p = (40.2925513, -76.1221659, 234.0)
        la, lo, h = G.centroid([p])
        assert (la, lo) == pytest.approx(p[:2], abs=1e-9)
        assert h == pytest.approx(p[2], abs=1e-6)

    def test_centroid_survives_the_antimeridian(self):
        # Averaging longitudes directly gives 0 deg -- the opposite side of the planet.
        la, lo, _ = G.centroid([(0.0, 179.9, 0.0), (0.0, -179.9, 0.0)])
        assert abs(lo) == pytest.approx(180.0, abs=1e-6)
        assert la == pytest.approx(0.0, abs=1e-9)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            G.centroid([])


class TestSurveyBridge:
    """hear/backend/survey.py's WGS84 door -- the half of the loop that did not exist.

    The nodes measure their own lat/lon/height and the solvers want local ENU metres. With nothing
    joining the two, the conversion got hand-rolled wherever it was needed, and the hand-rolled
    version was a sphere with the height discarded.
    """

    NODES = [
        {"node_id": 1, "name": "nyquist", "lat_deg": 40.2925513, "lon_deg": -76.1221659,
         "h_ell_m": 234.0},
        {"node_id": 2, "name": "mach", "lat_deg": 40.2925364, "lon_deg": -76.1223763,
         "h_ell_m": 222.4},
        {"node_id": 3, "name": "third", "lat_deg": 40.2927000, "lon_deg": -76.1222500,
         "h_ell_m": 230.0},
    ]

    def _survey(self):
        from hear.backend import survey as SV
        return SV, SV.from_wgs84_nodes(self.NODES)

    def test_separation_survives_the_round_trip(self):
        """The ENU frame must preserve the distance the ellipsoid says is there.

        The coordinates are a fixture taken from one reading, not a survey: the 11.6 m of height
        in them is GNSS vertical noise that later reversed sign. This asserts that the frame is
        distance-preserving, which is true of any input.
        """
        _, s = self._survey()
        P = s.positions([1, 2])
        enu = float(math.dist(P[0], P[1]))
        direct = G.ecef_distance((40.2925513, -76.1221659, 234.0),
                                 (40.2925364, -76.1223763, 222.4))
        assert enu == pytest.approx(direct, abs=1e-6)
        assert enu == pytest.approx(21.39, abs=0.05)

    def test_height_reaches_the_frame_rather_than_being_dropped(self):
        _, s = self._survey()
        assert s.vertical_spread_m() == pytest.approx(11.6, abs=0.01)

    def test_diameter_is_3d_and_exceeds_the_horizontal(self):
        import numpy as np
        _, s = self._survey()
        P = s.positions(s.ids)
        horiz = max(float(math.dist(a[:2], b[:2])) for a in P for b in P)
        assert s.diameter_m() > horiz
        assert s.diameter_m() == pytest.approx(22.43, abs=0.05)

    def test_origin_defaults_to_the_node_centroid_and_is_recorded(self):
        _, s = self._survey()
        assert s.origin["source"] == "node centroid"
        # A survey that cannot say where its frame is anchored is not reproducible.
        assert s.origin_geodetic()[0] == pytest.approx(40.29259, abs=1e-4)

    def test_hmsl_is_refused_rather_than_taken_as_a_synonym(self):
        """The failure this guards is invisible downstream: every node is wrong by very nearly
        the same geoid undulation, so it cancels in baselines and no residual can see it."""
        SV, _ = self._survey()
        bad = [dict(n) for n in self.NODES]
        for n in bad:
            n["hmsl_m"] = n.pop("h_ell_m")
        with pytest.raises(SV.SurveyError, match="hmsl_m"):
            SV.from_wgs84_nodes(bad)

    def test_an_origin_with_hmsl_is_refused_too(self):
        SV, _ = self._survey()
        s = SV.from_wgs84_nodes(self.NODES)
        s.origin = {"lat_deg": 40.0, "lon_deg": -76.0, "hmsl_m": 200.0}
        with pytest.raises(SV.SurveyError, match="hmsl_m"):
            s.origin_geodetic()

    def test_enu_of_places_a_measured_point_in_the_frame(self):
        _, s = self._survey()
        # A node's own coordinates must map back onto its surveyed position.
        got = s.enu_of(40.2925513, -76.1221659, 234.0)
        assert got == pytest.approx(s.position(1), abs=1e-6)

    def test_a_frame_with_no_origin_refuses_rather_than_assuming_one(self):
        from hear.backend import survey as SV
        s = SV.Survey({1: (0, 0, 0), 2: (10, 0, 0), 3: (0, 10, 0)})
        with pytest.raises(SV.SurveyError, match="no origin"):
            s.origin_geodetic()
