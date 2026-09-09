"""Array validity gate. Each test is one of the three failures that produced a fake bearing."""
import itertools
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.solve import consistency as CN  # noqa: E402

C = CN.sound_speed(23.0)                      # 345.238 m/s

# The Kinect 4-mic bar, from hugbot5000 perception/array_geometry.KINECT_MIC_Y. Laid along +Y.
# ⚠️these offsets are marked APPROXIMATE, NEVER MEASURED upstream; they are geometry for the
# tests, not a survey.
KINECT_Y = (-0.0952, -0.0381, 0.0190, 0.0714)
KINECT = [(0.0, y, 0.0) for y in KINECT_Y]
FS_KINECT = 16000.0
TOL_KINECT = 0.5 / FS_KINECT                  # 31.25 us -- the field gate rounded this to 30


def _dir_in_yz(deg):
    """Unit arrival direction at `deg` from the +Y array axis, in the Y-Z plane."""
    r = math.radians(deg)
    return (0.0, math.cos(r), math.sin(r))


class TestSpatialNyquist:
    """c/(2d), PER PAIR. Documented for the ESP boards, then missed on the Kinect."""

    @pytest.mark.parametrize("d_mm,hz", [(57.1, 3023), (114.2, 1512), (166.6, 1036), (38.2, 4519)])
    def test_matches_the_field_numbers(self, d_mm, hz):
        assert CN.spatial_nyquist(d_mm / 1000.0, C) == pytest.approx(hz, rel=0.01)

    def test_kinect_pairs_span_three_octaves_of_limit(self):
        lim = {(i, j): CN.spatial_nyquist(CN.spacing(KINECT, i, j), C)
               for i, j in itertools.combinations(range(4), 2)}
        # the widest pair is aliased where the narrowest is still clean: one band cannot serve both
        assert lim[(0, 3)] < 1100.0 < 3000.0 < lim[(0, 1)]

    def test_zero_spacing_has_no_nyquist(self):
        with pytest.raises(ValueError):
            CN.spatial_nyquist(0.0, C)

    def test_clamp_trims_a_wide_band_to_the_pair(self):
        got = CN.clamp_band((200.0, 7000.0), 0.1666, C)
        assert got[0] == 200.0
        assert got[1] == pytest.approx(CN.spatial_nyquist(0.1666, C), rel=1e-9)

    def test_clamp_leaves_a_band_already_inside_alone(self):
        assert CN.clamp_band((200.0, 900.0), 0.1666, C) == (200.0, 900.0)

    def test_clamp_drops_a_pair_whose_whole_band_is_aliased(self):
        # 2-7 kHz on the 167 mm pair: nothing usable is left, so the pair must be DROPPED,
        # not clamped to an empty band that GCC would still return a lag from.
        assert CN.clamp_band((2000.0, 7000.0), 0.1666, C) is None

    def test_the_band_that_produced_the_wrong_kinect_bearings_is_caught(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(40.0), C)
        v = CN.check_array(taus, KINECT, c=C, fs=FS_KINECT, bands=(200.0, 7000.0))
        assert not v.valid, "200-7000 Hz on every pair must be refused"
        assert any("spatial Nyquist" in r for r in v.reasons)
        assert v.axis_cos is None, "must not hand back a direction from an aliased solve"
        # and the same delays pass once each pair is held under its own limit
        bands = {p: CN.clamp_band((200.0, 7000.0), CN.spacing(KINECT, *p), C)
                 for p in itertools.combinations(range(4), 2)}
        assert CN.check_array(taus, KINECT, c=C, fs=FS_KINECT, bands=bands).valid


class TestPhysicalBound:
    def test_a_plane_wave_never_exceeds_d_over_c(self):
        d = CN.spacing(KINECT, 0, 3)
        for deg in range(0, 181, 5):
            taus = CN.plane_wave_taus(KINECT, _dir_in_yz(deg), C)
            assert CN.physically_possible(taus[(0, 3)], d, C)

    def test_endfire_sits_exactly_on_the_bound(self):
        d = CN.spacing(KINECT, 0, 3)
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(0.0), C)
        assert abs(taus[(0, 3)]) == pytest.approx(d / C, rel=1e-12)
        assert CN.physically_possible(taus[(0, 3)], d, C)

    def test_an_impossible_delay_is_rejected(self):
        d = CN.spacing(KINECT, 0, 3)
        assert not CN.physically_possible(1.2 * d / C, d, C)
        assert not CN.physically_possible(-1.2 * d / C, d, C)

    def test_the_esp_cross_board_offsets_are_impossible_by_this_bound(self):
        # ring offsets of 11-88 ms against a 1.5-2.1 ms physical bound: this is the check that
        # would have caught them on the first event instead of after three arrays.
        d = 2.1e-3 * C                              # the widest cross-board baseline, ~0.72 m
        assert not CN.physically_possible(11e-3, d, C)
        assert not CN.physically_possible(88e-3, d, C)

    def test_the_verdict_refuses_on_an_impossible_delay(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(60.0), C)
        taus[(0, 3)] = 1.5 * CN.spacing(KINECT, 0, 3) / C
        v = CN.check_array(taus, KINECT, c=C, fs=FS_KINECT)
        assert not v.valid
        assert any("plane-wave bound" in r for r in v.reasons)


class TestAdditivity:
    """The free check: an identity in the measurement, needing no ground truth."""

    @pytest.mark.parametrize("deg", [0.0, 12.5, 40.0, 90.0, 137.0, 180.0])
    def test_a_synthetic_plane_wave_closes_every_loop_exactly(self, deg):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(deg), C)
        assert CN.additivity_residual(taus, KINECT) < 1e-12

    def test_four_mics_give_three_independent_triangles(self):
        # C(4,2) - (4-1) = 3, not the 4 triangles you can draw
        assert len(CN.independent_triangles(4)) == 3
        assert len(list(itertools.combinations(range(4), 3))) == 4

    def test_one_wrong_correlation_peak_shows_up_as_a_loop_that_does_not_close(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(40.0), C)
        taus[(0, 3)] += 4.0 / FS_KINECT           # GCC off by four samples on the widest pair
        r = CN.additivity_residual(taus, KINECT)
        assert r == pytest.approx(4.0 / FS_KINECT, rel=1e-9)
        assert r > TOL_KINECT

    def test_deliberately_inconsistent_delays_are_rejected(self):
        taus = {(0, 1): 160e-6, (0, 2): 300e-6, (0, 3): 40e-6,
                (1, 2): 20e-6, (1, 3): -900e-6, (2, 3): 111e-6}
        v = CN.check_array(taus, KINECT, c=C, fs=FS_KINECT)
        assert not v.valid
        assert any("consistency" in r for r in v.reasons)
        assert v.axis_cos is None and v.cone_angle_deg is None
        with pytest.raises(CN.ArrayInconsistent):
            v.require_direction()

    def test_sub_tolerance_jitter_still_passes(self):
        rng = np.random.default_rng(7)
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(55.0), C)
        # a per-pair error small enough that no loop can exceed the tolerance
        for k in list(taus):
            taus[k] += float(rng.uniform(-1, 1)) * TOL_KINECT / 3.0
        assert CN.check_array(taus, KINECT, c=C, fs=FS_KINECT).valid

    def test_either_key_order_is_accepted(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(70.0), C)
        flipped = {(j, i): -v for (i, j), v in taus.items()}
        assert CN.additivity_residual(flipped, KINECT) < 1e-12

    def test_the_closure_residual_does_not_depend_on_which_triangles_you_pick(self):
        # ⚠️MEASURED on the 205 Kinect impulses: the field script's basis passed 4 events at
        # 30 us, the library's default basis passed 5, all four triangles passed 0. The maximum
        # over a basis is basis-dependent; the closure fit is not, so the gate uses that.
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(35.0), C)
        taus[(0, 2)] += 60e-6
        taus[(2, 3)] += 60e-6
        field = [(0, 1, 2), (1, 2, 3), (0, 1, 3)]
        a_field = CN.additivity_residual(taus, KINECT, field)
        a_star = CN.additivity_residual(taus, KINECT)
        assert a_field == pytest.approx(60e-6, rel=1e-6)
        assert a_star == pytest.approx(120e-6, rel=1e-6), "the omitted triangle carries 2x"
        # the closure residual is a property of the delays alone
        assert CN.closure_residual(taus, KINECT) == pytest.approx(
            CN.closure_residual({(j, i): -v for (i, j), v in taus.items()}, KINECT), rel=1e-12)

    def test_the_closure_fit_recovers_arrival_times_up_to_the_gauge(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(35.0), C)
        t = CN.arrival_times(taus, KINECT)
        assert t[0] == pytest.approx(0.0, abs=1e-15)
        for (i, j), v in taus.items():
            assert t[j] - t[i] == pytest.approx(v, abs=1e-12)
        assert CN.closure_residual(taus, KINECT) < 1e-12

    def test_a_missing_pair_is_an_error_not_a_pass(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(70.0), C)
        del taus[(1, 2)]
        with pytest.raises(ValueError, match="missing"):
            CN.additivity_residual(taus, KINECT)

    def test_two_mics_cannot_be_checked_at_all(self):
        # a single pair has no loop to close: there is NO free consistency check on 2 mics,
        # which is exactly why the ESP intra-board bearings went unchallenged for so long.
        with pytest.raises(ValueError, match=">= 3 mics"):
            CN.additivity_residual({(0, 1): 50e-6}, [(0.0, 0.0, 0.0), (0.0, 0.0382, 0.0)])


class TestWhatAdditivityCannotSee:
    """Stated in the module docstring; asserted here so the limits are not folklore."""

    def test_a_per_mic_clock_offset_is_invisible(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(40.0), C)
        off = [0.0, 300e-6, -120e-6, 900e-6]        # per-channel skew, way over tolerance
        skewed = {(i, j): t + off[j] - off[i] for (i, j), t in taus.items()}
        assert CN.additivity_residual(skewed, KINECT) < 1e-12, "loops close on a pure skew"
        # it is only caught with a survey in hand, by the affine (plane-wave) check
        v = CN.check_array(skewed, KINECT, c=C, fs=FS_KINECT, plane_wave_tol_s=TOL_KINECT)
        assert not v.valid
        assert any("not affine" in r for r in v.reasons)

    def test_a_wrong_geometry_passes_additivity_and_gives_a_wrong_angle(self):
        # delays from the real bar, read against a bar 20% wider: perfectly consistent, wrong.
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(60.0), C)
        wide = [(0.0, 1.2 * y, 0.0) for y in KINECT_Y]
        v = CN.check_array(taus, wide, c=C, fs=FS_KINECT)
        assert v.valid, "additivity never mentions the geometry"
        true_deg = 60.0
        assert abs(v.cone_angle_deg - true_deg) > 5.0


class TestRecovery:
    @pytest.mark.parametrize("deg", [0.0, 25.0, 60.0, 90.0, 120.0, 180.0])
    def test_a_valid_array_recovers_the_direction_it_was_given(self, deg):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(deg), C)
        v = CN.check_array(taus, KINECT, c=C, fs=FS_KINECT,
                           bands={p: CN.clamp_band((200.0, 7000.0), CN.spacing(KINECT, *p), C)
                                  for p in itertools.combinations(range(4), 2)})
        assert v.valid, v.summary()
        assert v.cone_angle_deg == pytest.approx(deg, abs=0.05)
        assert v.require_direction() == pytest.approx(math.cos(math.radians(deg)), abs=1e-3)
        assert v.axis_cos_spread == pytest.approx(0.0, abs=1e-9)

    def test_a_collinear_array_reports_a_cone_not_an_azimuth(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(60.0), C)
        v = CN.check_array(taus, KINECT, c=C, fs=FS_KINECT)
        assert v.collinear
        assert "cone" in v.note and "azimuth" in v.note

    def test_a_non_collinear_array_gets_the_gate_but_no_direction(self):
        square = [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.1, 0.0), (0.1, 0.1, 0.0)]
        taus = CN.plane_wave_taus(square, (0.3, 0.9, 0.2), C)
        v = CN.check_array(taus, square, c=C, fs=48000.0)
        assert not v.collinear and v.valid
        assert v.axis_cos is None, "this module does not solve 2D direction"

    def test_a_tolerance_is_mandatory(self):
        taus = CN.plane_wave_taus(KINECT, _dir_in_yz(40.0), C)
        with pytest.raises(ValueError, match="tolerance"):
            CN.check_array(taus, KINECT, c=C)


class TestGeometryHelpers:
    def test_the_kinect_bar_is_collinear_and_166_mm_long(self):
        e, off, collinear = CN.array_axis(KINECT)
        assert collinear and off == pytest.approx(0.0, abs=1e-12)
        assert CN.spacing(KINECT, 0, 3) == pytest.approx(0.1666, abs=1e-4)
        assert e == pytest.approx(np.array([0.0, 1.0, 0.0]), abs=1e-12)

    def test_a_scatter_of_mics_is_not_collinear(self):
        assert not CN.array_axis([(0, 0, 0), (0.1, 0, 0), (0.05, 0.04, 0)])[2]


class TestNullControl:
    """⚠️A pass rate without a null is not a result. These pin the control down."""

    def test_a_decoy_obeys_every_bound_and_is_still_not_a_plane_wave(self):
        rng = np.random.default_rng(1)
        c = CN.sound_speed(23.0)
        taus = CN.decoy_taus(KINECT, c, rng)
        # by construction it cannot be rejected by the physical bound -- that is the point
        for (i, j), t in taus.items():
            assert CN.physically_possible(t, CN.spacing(KINECT, i, j), c)
        # and with probability 1 no arrival-time vector explains it
        assert CN.closure_residual(taus, KINECT) > TOL_KINECT

    def test_the_gate_rejects_almost_every_decoy(self):
        c = CN.sound_speed(23.0)
        r = CN.null_pass_rate(KINECT, tol_s=TOL_KINECT, c=c, trials=1000)
        assert r["pass_rate"] < 0.02, "gate accepts decoys at %.3f" % r["pass_rate"]
        # the tolerance has to be far below the typical decoy, or the test is vacuous
        assert r["median_closure_us"] > 4 * r["tol_us"]

    def test_the_measured_kinect_rate_beats_the_null_by_an_order_of_magnitude(self):
        c = CN.sound_speed(23.0)
        d = CN.discrimination(17 / 205, KINECT, tol_s=TOL_KINECT, c=c, trials=1000)
        assert d["ratio"] > 5.0, "gate selects little: ratio %.1f" % d["ratio"]

    def test_a_tolerance_wide_enough_to_admit_anything_shows_up_in_the_null(self):
        """The null is what would have caught a tolerance set too loose to mean anything."""
        c = CN.sound_speed(23.0)
        r = CN.null_pass_rate(KINECT, tol_s=1.0, c=c, trials=200)
        assert r["pass_rate"] == 1.0

    def test_the_null_is_reproducible_from_its_seed(self):
        c = CN.sound_speed(23.0)
        a = CN.null_pass_rate(KINECT, tol_s=TOL_KINECT, c=c, trials=300, seed=7)
        b = CN.null_pass_rate(KINECT, tol_s=TOL_KINECT, c=c, trials=300, seed=7)
        assert a == b


class TestMisassociationNull:
    """The null that matters operationally: three impulses that are real and are not one event."""

    @staticmethod
    def _events(n=60, seed=0):
        rng = np.random.default_rng(seed)
        c = CN.sound_speed(23.0)
        out = []
        for _ in range(n):
            th = rng.uniform(0.0, np.pi)
            d = np.array([0.0, np.cos(th), np.sin(th)])
            t = CN.plane_wave_taus(KINECT, d, c)
            out.append({k: v + rng.normal(0.0, TOL_KINECT / 2) for k, v in t.items()})
        return out

    def test_real_events_pass_their_own_gate(self):
        c = CN.sound_speed(23.0)
        ev = self._events(40, seed=3)
        passed = sum(CN.check_array(e, KINECT, c=c, tol_s=TOL_KINECT).valid for e in ev)
        assert passed >= 35, "%d/40 real plane waves rejected" % (40 - passed)

    def test_mismatching_the_pairs_across_events_is_caught(self):
        c = CN.sound_speed(23.0)
        r = CN.null_from_events(self._events(), KINECT, tol_s=TOL_KINECT, c=c, trials=500)
        assert r["pass_rate"] < 0.05
        assert r["events"] == 60.0

    def test_it_is_a_harder_null_than_a_uniform_draw(self):
        """Real delays carry real structure, so a mismatched set is closer to consistent than a
        random one. Measured on the field's phone data the gap was 17x."""
        c = CN.sound_speed(23.0)
        mis = CN.null_from_events(self._events(), KINECT, tol_s=TOL_KINECT, c=c, trials=3000)
        uni = CN.null_pass_rate(KINECT, tol_s=TOL_KINECT, c=c, trials=3000)
        assert mis["pass_rate"] > uni["pass_rate"]

    def test_it_refuses_when_there_are_too_few_events_to_mismatch(self):
        """6 pairs on a 4-mic array cannot be drawn from fewer than 6 distinct events; silently
        reusing one would put a real event's own pairs back together."""
        c = CN.sound_speed(23.0)
        with pytest.raises(ValueError):
            CN.null_from_events(self._events(3), KINECT, tol_s=TOL_KINECT, c=c, trials=10)

    def test_events_missing_a_pair_are_excluded_not_filled(self):
        c = CN.sound_speed(23.0)
        ev = self._events(20)
        del ev[0][(0, 1)]
        r = CN.null_from_events(ev, KINECT, tol_s=TOL_KINECT, c=c, trials=50)
        assert r["events"] == 19.0
