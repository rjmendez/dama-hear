#!/usr/bin/env python3
"""Comprehensive test suite for acoustic ground surface impedance and reflection modeling.

Tests:
- Delany-Bazley complex surface impedance Z_s(f, sigma) and wavenumber k_c(f, sigma)
- Plane wave and spherical wave reflection coefficients Q(f, theta, r)
- Boundary loss factor F(w) asymptotics
- Ground interference notch frequencies across geometries and surface types
- Surface classification across the four standard fleet classes:
    * surface.hard_asphalt_concrete (sigma > 2e7 Pa*s/m2, +6dB coherent reflection, no LF notch)
    * surface.compacted_dirt_gravel (sigma ≈ 1e6 - 5e6 Pa*s/m2)
    * surface.porous_grass_turf (sigma ≈ 1.5e5 - 3e5 Pa*s/m2, prominent 200-800Hz notch)
    * surface.snow_leaf_litter (sigma < 5e4 Pa*s/m2, high absorption)
- Calibrated environmental transmission loss curves including ISO 9613-1 atmospheric absorption
- Parameter estimation under noise, gain offsets, and batch processing
"""
from __future__ import annotations

import math
import pathlib
import sys
from typing import Dict, List, Tuple

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear import impedance as imp  # noqa: E402


class TestDelanyBazleyImpedance:
    """Tests for Delany-Bazley complex surface impedance and propagation wavenumber."""

    def test_delany_bazley_impedance_scalar_and_vector(self):
        """Scalar and vector calls must produce identical complex impedance values."""
        f_scalar = 1000.0
        sigma = 2.0e5
        z_scalar = imp.delany_bazley_impedance(f_scalar, sigma)
        assert isinstance(z_scalar, complex)

        f_vec = np.array([500.0, 1000.0, 2000.0])
        z_vec = imp.delany_bazley_impedance(f_vec, sigma)
        assert isinstance(z_vec, np.ndarray)
        assert len(z_vec) == 3
        assert z_vec[1] == pytest.approx(z_scalar)

    def test_delany_bazley_physical_bounds(self):
        """Physical properties: Resistance > Z0 (air impedance) and Reactance < 0 (capacitive/spring-like)."""
        rho0, c0 = 1.204, 343.2
        z0 = rho0 * c0
        freqs = np.linspace(100.0, 4000.0, 40)

        for sigma in [2.5e4, 2.0e5, 2.5e6, 3.0e7]:
            zs = imp.delany_bazley_impedance(freqs, sigma, rho0=rho0, c0=c0)
            # Real part (acoustic resistance) must be strictly positive and > Z0
            assert np.all(zs.real > z0)
            # Imaginary part (acoustic reactance) must be negative in e^(+j omega t) convention
            assert np.all(zs.imag < 0.0)

    def test_impedance_scales_with_flow_resistivity(self):
        """High flow resistivity (asphalt) should yield high impedance; low (snow) should yield lower impedance."""
        f = 1000.0
        z_asphalt = imp.delany_bazley_impedance(f, 3.0e7)
        z_dirt = imp.delany_bazley_impedance(f, 2.5e6)
        z_grass = imp.delany_bazley_impedance(f, 2.0e5)
        z_snow = imp.delany_bazley_impedance(f, 2.5e4)

        assert abs(z_asphalt) > abs(z_dirt) > abs(z_grass) > abs(z_snow)

    def test_delany_bazley_wavenumber(self):
        """Wavenumber k_c must have positive real part and negative imaginary part (attenuation inside medium)."""
        f = 1000.0
        c0 = 343.2
        k0 = 2.0 * math.pi * f / c0
        kc = imp.delany_bazley_wavenumber(f, 2.0e5, c0=c0)

        assert kc.real > k0
        assert kc.imag < 0.0


class TestReflectionCoefficients:
    """Tests for Plane Wave and Spherical Wave reflection coefficients and boundary loss."""

    def test_plane_wave_reflection_rigid_limit(self):
        """For highly resistive ground (sigma -> inf), Rp approaches +1.0 for all angles."""
        freqs = np.array([200.0, 1000.0, 3000.0])
        theta = math.radians(45.0)
        rp_rigid = imp.plane_wave_reflection_coefficient(freqs, theta, 1.0e9)

        for val in rp_rigid:
            assert abs(val - 1.0) < 0.05

    def test_boundary_loss_factor_limits(self):
        """Boundary loss factor F(w) -> 1 as |w| -> 0, and F(w) -> 0 as |w| -> inf."""
        # Near zero numerical distance
        f_zero = imp.boundary_loss_factor(1e-7 * (1.0 + 1j))
        assert abs(f_zero - 1.0) < 1e-3

        # Large numerical distance
        f_large = imp.boundary_loss_factor(100.0 * (1.0 + 1j))
        assert abs(f_large) < 0.05

    def test_spherical_q_asphalt_coherent_reflection(self):
        """Hard asphalt exhibits near-unity spherical reflection coefficient |Q| approx 1.0."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        freqs = np.linspace(100.0, 3000.0, 30)
        q = clf.evaluate_spherical_reflection(freqs, hs=1.5, hr=1.5, d=20.0, sigma=3.0e7)

        assert np.all(np.abs(q) > 0.88)

    def test_spherical_q_vs_evaluate_q_consistency(self):
        """Direct evaluate_q with geometry parameters must match evaluate_spherical_reflection."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        hs, hr, d = 1.2, 1.5, 25.0
        geo = imp.PropagationGeometry(hs=hs, hr=hr, d=d)
        f = 800.0
        sigma = 2.0e5

        q1 = clf.evaluate_spherical_reflection(f, hs, hr, d, sigma)
        q2 = clf.evaluate_q(f, geo.incidence_angle_rad, geo.r2, sigma)

        assert q1 == pytest.approx(q2, rel=1e-6)


class TestGeometryAndInterferenceNotches:
    """Tests for propagation geometry, destructive interference, and ground notches."""

    def test_geometry_properties(self):
        """Geometry direct and reflected path lengths must satisfy Pythagorean relations."""
        geo = imp.PropagationGeometry(hs=1.5, hr=1.5, d=20.0)
        assert geo.r1 == pytest.approx(20.0)
        assert geo.r2 == pytest.approx(math.sqrt(20.0**2 + 3.0**2))
        assert geo.delta_r == pytest.approx(geo.r2 - 20.0)
        assert 0.0 < geo.grazing_angle_deg < 45.0
        assert geo.grazing_angle_deg + geo.incidence_angle_deg == pytest.approx(90.0)

    def test_rigid_destructive_interference_frequencies(self):
        """Analytical rigid interference frequencies match half-wavelength path differences."""
        geo = imp.PropagationGeometry(hs=1.2, hr=1.2, d=30.0)
        c0 = 343.2
        dr = geo.delta_r
        f0 = c0 / (2.0 * dr)

        f_notches = geo.rigid_interference_frequencies(c0=c0, max_harmonics=3)
        assert len(f_notches) == 3
        assert f_notches[0] == pytest.approx(f0)
        assert f_notches[1] == pytest.approx(3.0 * f0)
        assert f_notches[2] == pytest.approx(5.0 * f0)

    def test_asphalt_low_frequency_plus_6db_coherent_reflection(self):
        """On hard asphalt, low-frequency (50-100 Hz) relative SPL is +6 dB (coherent pressure doubling)."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        spl_lf = clf.predict_relative_spl(np.array([50.0, 80.0, 100.0]), hs=1.5, hr=1.5, d=25.0, sigma=3.0e7)
        # Should be between +5.8 and +6.0 dB
        assert np.all(spl_lf > 5.8)
        assert np.all(spl_lf <= 6.02)

    def test_porous_grass_prominent_notch_in_200_800hz(self):
        """Porous grass (sigma approx 2e5) produces a prominent ground notch in the 200-800 Hz band."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        freqs = np.linspace(50.0, 2000.0, 400)
        spl_grass = clf.predict_relative_spl(freqs, hs=1.2, hr=1.2, d=25.0, sigma=2.0e5)

        # Minimum SPL should be a deep dip below 0 dB
        min_idx = np.argmin(spl_grass)
        notch_freq = freqs[min_idx]
        assert 200.0 <= notch_freq <= 800.0
        assert spl_grass[min_idx] < 0.0


class TestSurfaceClassification:
    """Tests for the GroundSurfaceImpedanceClassifier across all four standard fleet classes."""

    @pytest.fixture
    def classifier(self) -> imp.GroundSurfaceImpedanceClassifier:
        return imp.GroundSurfaceImpedanceClassifier()

    def test_classify_hard_asphalt_concrete(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifies hard asphalt / concrete (sigma > 2e7 Pa*s/m2)."""
        freqs = np.linspace(100.0, 3500.0, 80)
        hs, hr, d = 1.0, 1.2, 20.0
        spl_meas = classifier.predict_relative_spl(freqs, hs, hr, d, sigma=3.0e7)

        res = classifier.classify(freqs, spl_meas, hs, hr, d)
        assert res.surface_type == imp.SURFACE_HARD_ASPHALT_CONCRETE
        assert res.estimated_sigma >= 1.0e7
        assert res.confidence > 0.80
        assert res.surface_probabilities[imp.SURFACE_HARD_ASPHALT_CONCRETE] > 0.70

    def test_classify_compacted_dirt_gravel(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifies compacted dirt / gravel (sigma ≈ 1e6 - 5e6 Pa*s/m2)."""
        freqs = np.linspace(100.0, 3500.0, 80)
        hs, hr, d = 1.0, 1.2, 20.0
        spl_meas = classifier.predict_relative_spl(freqs, hs, hr, d, sigma=2.5e6)

        res = classifier.classify(freqs, spl_meas, hs, hr, d)
        assert res.surface_type == imp.SURFACE_COMPACTED_DIRT_GRAVEL
        assert 6.0e5 <= res.estimated_sigma < 1.0e7
        assert res.confidence > 0.80

    def test_classify_porous_grass_turf(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifies porous grass / turf (sigma ≈ 1.5e5 - 3e5 Pa*s/m2)."""
        freqs = np.linspace(100.0, 3500.0, 80)
        hs, hr, d = 1.0, 1.2, 20.0
        spl_meas = classifier.predict_relative_spl(freqs, hs, hr, d, sigma=2.0e5)

        res = classifier.classify(freqs, spl_meas, hs, hr, d)
        assert res.surface_type == imp.SURFACE_POROUS_GRASS_TURF
        assert 6.0e4 <= res.estimated_sigma < 6.0e5
        assert res.confidence > 0.70

    def test_classify_snow_leaf_litter(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifies snow / leaf litter (sigma < 5e4 Pa*s/m2)."""
        freqs = np.linspace(100.0, 3500.0, 80)
        hs, hr, d = 1.0, 1.2, 20.0
        spl_meas = classifier.predict_relative_spl(freqs, hs, hr, d, sigma=2.5e4)

        res = classifier.classify(freqs, spl_meas, hs, hr, d)
        assert res.surface_type == imp.SURFACE_SNOW_LEAF_LITTER
        assert res.estimated_sigma < 6.0e4
        assert res.confidence > 0.50

    def test_classification_robust_to_gaussian_noise(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifier correctly identifies surface type under +/- 1.0 dB measurement noise."""
        np.random.seed(12345)
        freqs = np.linspace(100.0, 3500.0, 70)
        hs, hr, d = 1.2, 1.5, 30.0

        for stype, true_sigma in [
            (imp.SURFACE_HARD_ASPHALT_CONCRETE, 4.0e7),
            (imp.SURFACE_COMPACTED_DIRT_GRAVEL, 2.0e6),
            (imp.SURFACE_POROUS_GRASS_TURF, 2.2e5),
            (imp.SURFACE_SNOW_LEAF_LITTER, 3.0e4),
        ]:
            clean_spl = classifier.predict_relative_spl(freqs, hs, hr, d, true_sigma)
            noisy_spl = clean_spl + np.random.normal(0, 0.75, size=clean_spl.shape)
            res = classifier.classify(freqs, noisy_spl, hs, hr, d)
            assert res.surface_type == stype

    def test_classification_with_uncalibrated_source_gain(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Classifier fits unknown source level offset when estimate_gain=True."""
        freqs = np.linspace(100.0, 3000.0, 60)
        hs, hr, d = 1.2, 1.2, 25.0
        true_sigma = 2.0e5  # grass

        clean_spl = classifier.predict_relative_spl(freqs, hs, hr, d, true_sigma)
        uncal_spl = clean_spl + 23.5  # 23.5 dB unknown source power offset

        res = classifier.classify(freqs, uncal_spl, hs, hr, d, is_relative_spl=False, estimate_gain=True)
        assert res.surface_type == imp.SURFACE_POROUS_GRASS_TURF
        assert 1.0e5 <= res.estimated_sigma <= 4.0e5

    def test_batch_classify(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Batch classification runs on multiple observations and preserves order."""
        freqs = np.linspace(100.0, 3000.0, 50)
        records = [
            {
                "frequencies": freqs,
                "spectrum": classifier.predict_relative_spl(freqs, 1.0, 1.0, 20.0, 3.0e7),
                "hs": 1.0,
                "hr": 1.0,
                "d": 20.0,
            },
            {
                "frequencies": freqs,
                "spectrum": classifier.predict_relative_spl(freqs, 1.5, 1.5, 30.0, 2.0e5),
                "hs": 1.5,
                "hr": 1.5,
                "d": 30.0,
            },
        ]

        batch_res = classifier.batch_classify(records)
        assert len(batch_res) == 2
        assert batch_res[0].surface_type == imp.SURFACE_HARD_ASPHALT_CONCRETE
        assert batch_res[1].surface_type == imp.SURFACE_POROUS_GRASS_TURF

    def test_result_to_dict_serialization(self, classifier: imp.GroundSurfaceImpedanceClassifier):
        """Result object serializes cleanly to dict with JSON-native primitive types."""
        freqs = np.linspace(100.0, 2000.0, 20)
        spl = classifier.predict_relative_spl(freqs, 1.0, 1.0, 20.0, 2.0e5)
        res = classifier.classify(freqs, spl, 1.0, 1.0, 20.0)

        data = res.to_dict()
        assert isinstance(data, dict)
        assert data["surface_type"] == imp.SURFACE_POROUS_GRASS_TURF
        assert isinstance(data["estimated_sigma"], float)
        assert isinstance(data["frequencies_hz"], list)
        assert isinstance(data["transmission_loss_db"], list)
        assert len(data["transmission_loss_db"]) == len(freqs)


class TestEnvironmentalTransmissionLoss:
    """Tests for calibrated environmental transmission loss curves."""

    def test_transmission_loss_increases_with_distance(self):
        """Transmission loss increases monotonically with distance due to spherical spreading."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        f = np.array([1000.0])
        hs, hr = 1.5, 1.5
        sigma = 2.0e5

        tl_10m = clf.predict_transmission_loss(f, hs, hr, d=10.0, sigma=sigma)[0]
        tl_50m = clf.predict_transmission_loss(f, hs, hr, d=50.0, sigma=sigma)[0]
        tl_100m = clf.predict_transmission_loss(f, hs, hr, d=100.0, sigma=sigma)[0]

        assert tl_10m < tl_50m < tl_100m
        # Spherical spreading from 10m to 100m is at least 20 dB
        assert (tl_100m - tl_10m) > 15.0

    def test_atmospheric_absorption_iso9613(self):
        """Atmospheric absorption is higher at high frequencies (4kHz > 1kHz > 100Hz)."""
        alpha_100 = imp.atmospheric_attenuation_iso9613(100.0)[0]
        alpha_1k = imp.atmospheric_attenuation_iso9613(1000.0)[0]
        alpha_4k = imp.atmospheric_attenuation_iso9613(4000.0)[0]

        assert 0.0 < alpha_100 < alpha_1k < alpha_4k
        # At 4 kHz, absorption is typically ~0.02 - 0.04 dB/m (20-40 dB/km)
        assert 0.015 < alpha_4k < 0.050

    def test_calibrated_transmission_loss_curve_in_classification_result(self):
        """Classification result carries populated transmission loss curve matching frequencies."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        freqs = np.linspace(100.0, 4000.0, 50)
        hs, hr, d = 1.5, 1.5, 40.0
        spl_meas = clf.predict_relative_spl(freqs, hs, hr, d, sigma=2.0e5)

        res = clf.classify(freqs, spl_meas, hs, hr, d)
        assert len(res.transmission_loss_curve) == len(freqs)
        assert np.all(res.transmission_loss_curve > 0.0)

    def test_environmental_conditions_affect_sound_speed_and_absorption(self):
        """Classifier respects custom temperature and relative humidity settings."""
        clf_cold_dry = imp.GroundSurfaceImpedanceClassifier(temp_c=-10.0, rel_humidity=20.0, c0=325.0)
        clf_hot_humid = imp.GroundSurfaceImpedanceClassifier(temp_c=35.0, rel_humidity=85.0, c0=352.0)

        f = np.array([2000.0, 4000.0])
        tl_cold = clf_cold_dry.predict_transmission_loss(f, hs=1.5, hr=1.5, d=50.0, sigma=2.0e5)
        tl_hot = clf_hot_humid.predict_transmission_loss(f, hs=1.5, hr=1.5, d=50.0, sigma=2.0e5)

        # Absorption and speed differences produce differing transmission loss
        assert not np.allclose(tl_cold, tl_hot)


class TestEdgeCasesAndValidation:
    """Tests for edge cases, error handling, and parameter bounds."""

    def test_frequency_spectrum_length_mismatch_raises(self):
        """Mismatched frequency and measured spectrum array lengths must raise ValueError."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        freqs = np.array([100.0, 200.0, 300.0])
        spectrum = np.array([0.0, 1.0])

        with pytest.raises(ValueError, match="must match"):
            clf.fit_flow_resistivity(freqs, spectrum, hs=1.0, hr=1.0, d=20.0)

    def test_very_low_frequencies_handled_gracefully(self):
        """Near-zero frequencies (< 10 Hz) do not produce NaN or ZeroDivisionError."""
        zs = imp.delany_bazley_impedance(np.array([0.0, 1.0, 5.0]), sigma=2.0e5)
        assert not np.any(np.isnan(zs))
        assert not np.any(np.isinf(zs))

    def test_extreme_grazing_angle(self):
        """Long range grazing propagation (d=200m, hs=0.2m, hr=0.2m) evaluates stably."""
        clf = imp.GroundSurfaceImpedanceClassifier()
        freqs = np.linspace(100.0, 2000.0, 30)
        q = clf.evaluate_spherical_reflection(freqs, hs=0.2, hr=0.2, d=200.0, sigma=2.0e5)
        assert not np.any(np.isnan(q))
        assert not np.any(np.isinf(q))
        spl = clf.predict_relative_spl(freqs, hs=0.2, hr=0.2, d=200.0, sigma=2.0e5)
        assert not np.any(np.isnan(spl))

