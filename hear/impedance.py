#!/usr/bin/env python3
"""Acoustic ground surface impedance modeling and classification for the DAMA acoustic fleet.

Implements Delany-Bazley complex surface impedance modeling, spherical wave reflection
coefficients with Chien-Soroka / Chessell boundary loss corrections, ground interference
notch analysis, and ground surface type classification from acoustic spectra.

Surfaces classified:
- `surface.hard_asphalt_concrete`: sigma > 2e7 Pa*s/m2 (+6dB coherent reflection, no LF notch)
- `surface.compacted_dirt_gravel`: sigma ≈ 1e6 - 5e6 Pa*s/m2 (moderate absorption, high-freq notch)
- `surface.porous_grass_turf`: sigma ≈ 1.5e5 - 3e5 Pa*s/m2 (prominent 200-800Hz ground notch)
- `surface.snow_leaf_litter`: sigma < 5e4 Pa*s/m2 (high porous absorption)
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.signal import find_peaks
import scipy.special as sp

# Standard ground surface classification identifiers
SURFACE_HARD_ASPHALT_CONCRETE = "surface.hard_asphalt_concrete"
SURFACE_COMPACTED_DIRT_GRAVEL = "surface.compacted_dirt_gravel"
SURFACE_POROUS_GRASS_TURF = "surface.porous_grass_turf"
SURFACE_SNOW_LEAF_LITTER = "surface.snow_leaf_litter"

ALL_SURFACE_TYPES = [
    SURFACE_HARD_ASPHALT_CONCRETE,
    SURFACE_COMPACTED_DIRT_GRAVEL,
    SURFACE_POROUS_GRASS_TURF,
    SURFACE_SNOW_LEAF_LITTER,
]

# Canonical flow resistivity parameters (Pa * s / m^2)
SURFACE_PROPERTIES: Dict[str, Dict[str, Any]] = {
    SURFACE_HARD_ASPHALT_CONCRETE: {
        "nominal_sigma": 3.0e7,
        "sigma_bounds": (1.0e7, 1.0e9),
        "typical_range": (2.0e7, 1.0e8),
        "description": "Hard asphalt / concrete surface (rigid, +6dB coherent reflection)",
    },
    SURFACE_COMPACTED_DIRT_GRAVEL: {
        "nominal_sigma": 2.5e6,
        "sigma_bounds": (6.0e5, 1.0e7),
        "typical_range": (1.0e6, 5.0e6),
        "description": "Compacted dirt / roadbed / gravel (intermediate flow resistivity)",
    },
    SURFACE_POROUS_GRASS_TURF: {
        "nominal_sigma": 2.0e5,
        "sigma_bounds": (6.0e4, 6.0e5),
        "typical_range": (1.5e5, 3.0e5),
        "description": "Porous grass / pasture / turf (prominent 200-800Hz interference notch)",
    },
    SURFACE_SNOW_LEAF_LITTER: {
        "nominal_sigma": 2.5e4,
        "sigma_bounds": (1.0e3, 6.0e4),
        "typical_range": (5.0e3, 5.0e4),
        "description": "Fresh snow / forest leaf litter / loose mulch (high acoustic absorption)",
    },
}


@dataclass(frozen=True)
class PropagationGeometry:
    """Source-receiver geometry above a planar ground boundary.

    Attributes:
        hs: Source height in meters.
        hr: Receiver height in meters.
        d: Horizontal distance in meters.
    """

    hs: float
    hr: float
    d: float

    @property
    def r1(self) -> float:
        """Direct line-of-sight path length in meters."""
        return math.sqrt(self.d**2 + (self.hs - self.hr) ** 2)

    @property
    def r2(self) -> float:
        """Specular ground-reflected path length in meters."""
        return math.sqrt(self.d**2 + (self.hs + self.hr) ** 2)

    @property
    def delta_r(self) -> float:
        """Path length difference (r2 - r1) in meters."""
        return self.r2 - self.r1

    @property
    def grazing_angle_rad(self) -> float:
        """Specular reflection grazing angle psi in radians."""
        return math.asin((self.hs + self.hr) / self.r2)

    @property
    def grazing_angle_deg(self) -> float:
        """Specular reflection grazing angle psi in degrees."""
        return math.degrees(self.grazing_angle_rad)

    @property
    def incidence_angle_rad(self) -> float:
        """Incidence angle theta from surface normal in radians."""
        return math.acos((self.hs + self.hr) / self.r2)

    @property
    def incidence_angle_deg(self) -> float:
        """Incidence angle theta from surface normal in degrees."""
        return math.degrees(self.incidence_angle_rad)

    def rigid_interference_frequencies(self, c0: float = 343.2, max_harmonics: int = 4) -> List[float]:
        """Calculates destructive interference notch frequencies for an ideal rigid reflector.

        For rigid ground (Q = +1), destructive interference occurs at half-wavelength path differences:
        f_n = (2n - 1) * c0 / (2 * delta_r), for n = 1, 2, ...
        """
        dr = self.delta_r
        if dr <= 1e-6:
            return []
        f0 = c0 / (2.0 * dr)
        return [(2 * n + 1) * f0 for n in range(max_harmonics)]


@dataclass
class SurfaceClassificationResult:
    """Classification outcome for an acoustic ground surface reflection analysis."""

    surface_type: str
    estimated_sigma: float
    confidence: float
    notch_frequencies: List[float]
    notch_depths_db: List[float]
    transmission_loss_curve: np.ndarray
    relative_spl_curve: np.ndarray
    surface_probabilities: Dict[str, float]
    frequencies: np.ndarray
    geometry: PropagationGeometry
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert classification result to a JSON-serializable dictionary."""
        return {
            "surface_type": self.surface_type,
            "estimated_sigma": float(self.estimated_sigma),
            "confidence": float(self.confidence),
            "notch_frequencies": [float(f) for f in self.notch_frequencies],
            "notch_depths_db": [float(d) for d in self.notch_depths_db],
            "surface_probabilities": {k: float(v) for k, v in self.surface_probabilities.items()},
            "frequencies_hz": [float(f) for f in self.frequencies],
            "relative_spl_db": [float(v) for v in self.relative_spl_curve],
            "transmission_loss_db": [float(v) for v in self.transmission_loss_curve],
            "geometry": {
                "hs_m": self.geometry.hs,
                "hr_m": self.geometry.hr,
                "d_m": self.geometry.d,
                "r1_m": self.geometry.r1,
                "r2_m": self.geometry.r2,
                "delta_r_m": self.geometry.delta_r,
                "grazing_angle_deg": self.geometry.grazing_angle_deg,
            },
            "metadata": self.metadata,
        }


def delany_bazley_impedance(
    f: Union[float, np.ndarray, Sequence[float]],
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> Union[complex, np.ndarray]:
    """Computes complex surface acoustic impedance Z_s(f, sigma) using the Delany-Bazley (1970) empirical model.

    The Delany-Bazley model parameterizes acoustic characteristic impedance as a function of the
    dimensionless parameter chi = 1000 * f / sigma (frequency in kHz divided by flow resistivity
    in kPa*s/m^2 = 1000 Pa*s/m^2).

    With time-harmonic convention e^(+j omega t):
        Z_s / Z_0 = 1 + 9.08 * (1000*f / sigma)^(-0.75) - j * 11.9 * (1000*f / sigma)^(-0.73)

    Args:
        f: Frequency in Hertz (scalar or numpy array).
        sigma: Effective airflow resistivity in Pa*s/m^2 (e.g., 2e5 for grass, 3e7 for asphalt).
        rho0: Ambient air density in kg/m^3 (default 1.204).
        c0: Speed of sound in air in m/s (default 343.2).

    Returns:
        Complex acoustic impedance Z_s in Pa*s/m (same shape as f).
    """
    is_scalar = np.isscalar(f)
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    # Protect against zero or negative frequencies
    f_safe = np.maximum(f_arr, 1.0)
    sigma_safe = max(float(sigma), 10.0)

    chi = 1000.0 * f_safe / sigma_safe
    r_norm = 1.0 + 9.08 * (chi ** -0.75)
    x_norm = -11.9 * (chi ** -0.73)

    z0 = rho0 * c0
    zs = (r_norm + 1j * x_norm) * z0

    if is_scalar:
        return complex(zs[0])
    return zs


def delany_bazley_wavenumber(
    f: Union[float, np.ndarray, Sequence[float]],
    sigma: float,
    c0: float = 343.2,
) -> Union[complex, np.ndarray]:
    """Computes complex propagation constant k_c(f, sigma) in the porous ground layer.

    k_c / k_0 = 1 + 10.8 * (1000*f / sigma)^(-0.70) - j * 10.3 * (1000*f / sigma)^(-0.59)
    """
    is_scalar = np.isscalar(f)
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    f_safe = np.maximum(f_arr, 1.0)
    sigma_safe = max(float(sigma), 10.0)

    k0 = 2.0 * np.pi * f_safe / c0
    chi = 1000.0 * f_safe / sigma_safe
    alpha_norm = 1.0 + 10.8 * (chi ** -0.70)
    beta_norm = -10.3 * (chi ** -0.59)

    kc = k0 * (alpha_norm + 1j * beta_norm)

    if is_scalar:
        return complex(kc[0])
    return kc


def plane_wave_reflection_coefficient(
    f: Union[float, np.ndarray, Sequence[float]],
    theta_rad: Union[float, np.ndarray],
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> Union[complex, np.ndarray]:
    """Computes plane-wave reflection coefficient R_p(f, theta) for finite impedance ground.

    R_p = (cos(theta) - beta) / (cos(theta) + beta)
    where beta = Z_0 / Z_s is the normalized specific acoustic admittance.

    Args:
        f: Frequency in Hz.
        theta_rad: Angle of incidence from surface normal in radians (or array).
        sigma: Effective flow resistivity in Pa*s/m^2.
        rho0: Ambient air density in kg/m^3.
        c0: Speed of sound in air in m/s.

    Returns:
        Complex reflection coefficient R_p.
    """
    is_scalar = np.isscalar(f) and np.isscalar(theta_rad)
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    cos_theta = np.asarray(np.cos(theta_rad), dtype=float)

    z0 = rho0 * c0
    zs = delany_bazley_impedance(f_arr, sigma, rho0=rho0, c0=c0)
    beta = z0 / zs

    rp = (cos_theta - beta) / (cos_theta + beta)

    if is_scalar:
        return complex(rp[0])
    return rp


def boundary_loss_factor(rho: Union[complex, np.ndarray]) -> Union[complex, np.ndarray]:
    """Evaluates the boundary loss factor (ground wave function) F(w).

    According to the Chien-Soroka (1975, 1980) and Chessell (1977) formulation:
        w = rho^2 = (1/2) * j * k * r2 * (cos(theta) + beta)^2
        rho = (1 + j)/2 * sqrt(k * r2) * (cos(theta) + beta)
        F(w) = 1 + j * sqrt(pi) * rho * exp(-rho^2) * erfc(-j * rho)
             = 1 + j * sqrt(pi) * rho * wofz(rho)

    where wofz(z) is the Faddeeva (scaled complementary error) function.

    Limits:
        F(w) -> 1 as |w| -> 0 (near grazing / low frequency ground wave dominance)
        F(w) -> 0 as |w| -> inf (classical plane-wave asymptotic limit)
    """
    is_scalar = np.isscalar(rho)
    rho_arr = np.atleast_1d(np.asarray(rho, dtype=complex))

    # Faddeeva function wofz(z) = exp(-z^2) * erfc(-1j * z)
    f_loss = 1.0 + 1j * np.sqrt(np.pi) * rho_arr * sp.wofz(rho_arr)

    if is_scalar:
        return complex(f_loss[0])
    return f_loss


def spherical_wave_reflection_coefficient(
    f: Union[float, np.ndarray, Sequence[float]],
    hs: float,
    hr: float,
    d: float,
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> Union[complex, np.ndarray]:
    """Evaluates the spherical wave reflection coefficient Q(f, hs, hr, d, sigma).

    Q = R_p + (1 - R_p) * F(w)

    Corrects for curved wavefront reflections, finite impedance ground absorption,
    and ground-wave propagation at grazing angles.

    Args:
        f: Frequency in Hz.
        hs: Source height in meters.
        hr: Receiver height in meters.
        d: Horizontal distance in meters.
        sigma: Effective airflow resistivity in Pa*s/m^2.
        rho0: Ambient air density in kg/m^3.
        c0: Speed of sound in air in m/s.

    Returns:
        Complex spherical wave reflection coefficient Q.
    """
    is_scalar = np.isscalar(f)
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    f_safe = np.maximum(f_arr, 1.0)

    geo = PropagationGeometry(hs=hs, hr=hr, d=d)
    r2 = geo.r2
    cos_theta = (hs + hr) / r2  # Equal to sin(grazing_angle)

    z0 = rho0 * c0
    zs = delany_bazley_impedance(f_safe, sigma, rho0=rho0, c0=c0)
    beta = z0 / zs

    # Plane wave reflection coefficient
    rp = (cos_theta - beta) / (cos_theta + beta)

    # Numerical distance parameter rho
    k = 2.0 * np.pi * f_safe / c0
    rho = (1.0 + 1j) * 0.5 * np.sqrt(k * r2) * (cos_theta + beta)

    # Boundary loss factor
    f_loss = boundary_loss_factor(rho)

    q = rp + (1.0 - rp) * f_loss

    if is_scalar:
        return complex(q[0])
    return q


def evaluate_spherical_q(
    f: Union[float, np.ndarray, Sequence[float]],
    theta_rad: float,
    r2: float,
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> Union[complex, np.ndarray]:
    """Evaluates spherical wave reflection coefficient Q given incidence angle and image distance directly."""
    is_scalar = np.isscalar(f)
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    f_safe = np.maximum(f_arr, 1.0)

    cos_theta = math.cos(theta_rad)
    z0 = rho0 * c0
    zs = delany_bazley_impedance(f_safe, sigma, rho0=rho0, c0=c0)
    beta = z0 / zs

    rp = (cos_theta - beta) / (cos_theta + beta)

    k = 2.0 * np.pi * f_safe / c0
    rho = (1.0 + 1j) * 0.5 * np.sqrt(k * r2) * (cos_theta + beta)
    f_loss = boundary_loss_factor(rho)
    q = rp + (1.0 - rp) * f_loss

    if is_scalar:
        return complex(q[0])
    return q


def relative_sound_pressure_level(
    f: Union[float, np.ndarray, Sequence[float]],
    hs: float,
    hr: float,
    d: float,
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> np.ndarray:
    """Computes the relative Sound Pressure Level L_rel(f) (in dB) relative to free field.

    L_rel(f) = 20 * log10 | 1 + Q * (r1 / r2) * exp(-j * k * (r2 - r1)) |

    Constructive interference yields up to +6 dB (for rigid ground at low frequencies).
    Destructive interference creates ground notches where L_rel dips below 0 dB.
    """
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    geo = PropagationGeometry(hs=hs, hr=hr, d=d)
    r1 = geo.r1
    r2 = geo.r2
    delta_r = geo.delta_r

    q = spherical_wave_reflection_coefficient(f_arr, hs, hr, d, sigma, rho0=rho0, c0=c0)
    k = 2.0 * np.pi * np.maximum(f_arr, 1.0) / c0

    # Complex acoustic transfer function relative to direct field
    h = 1.0 + q * (r1 / r2) * np.exp(-1j * k * delta_r)
    mag = np.maximum(np.abs(h), 1e-12)
    return 20.0 * np.log10(mag)


def excess_attenuation(
    f: Union[float, np.ndarray, Sequence[float]],
    hs: float,
    hr: float,
    d: float,
    sigma: float,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> np.ndarray:
    """Computes the excess ground attenuation A_excess(f) in dB.

    A_excess(f) = - L_rel(f) = -20 * log10 | 1 + Q * (r1 / r2) * exp(-j * k * delta_r) |
    """
    return -relative_sound_pressure_level(f, hs, hr, d, sigma, rho0=rho0, c0=c0)


def atmospheric_attenuation_iso9613(
    f: Union[float, np.ndarray, Sequence[float]],
    temp_c: float = 20.0,
    rel_humidity: float = 50.0,
    pressure_kpa: float = 101.325,
) -> np.ndarray:
    """Computes atmospheric absorption coefficient alpha(f) in dB/m per ISO 9613-1.

    Accounts for classical viscosity/thermal losses and molecular relaxation of Oxygen and Nitrogen.
    """
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    f_safe = np.maximum(f_arr, 1.0)

    t_k = temp_c + 273.15
    t_01 = 293.15  # Reference temperature 20 C in Kelvin
    t_0 = 273.16  # Triple point of water in Kelvin
    p_r = pressure_kpa / 101.325  # Relative pressure

    # Saturation vapor pressure
    c_sat = -6.8346 * ((t_0 / t_k) ** 1.261) + 4.6151
    p_sat = 101.325 * (10.0**c_sat)
    h_molar = rel_humidity * (p_sat / pressure_kpa)

    # Relaxation frequencies
    f_r_o = p_r * (24.0 + 4.04e4 * h_molar * ((0.02 + h_molar) / (0.391 + h_molar)))
    f_r_n = p_r * ((t_01 / t_k) ** 0.5) * (9.0 + 280.0 * h_molar * np.exp(-4.170 * (((t_01 / t_k) ** (1.0 / 3.0)) - 1.0)))

    # Attenuation terms
    term_classical = 1.84e-11 * (1.0 / p_r) * np.sqrt(t_k / t_01)
    term_oxygen = 0.01275 * np.exp(-2239.1 / t_k) / (f_r_o + (f_safe**2) / f_r_o)
    term_nitrogen = 0.1068 * np.exp(-3352.0 / t_k) / (f_r_n + (f_safe**2) / f_r_n)

    alpha_np_m = (f_safe**2) * (term_classical + ((t_k / t_01) ** (-2.5)) * (term_oxygen + term_nitrogen))
    alpha_db_m = 8.685889638 * alpha_np_m  # Convert Neper/m to dB/m
    return alpha_db_m


def environmental_transmission_loss(
    f: Union[float, np.ndarray, Sequence[float]],
    hs: float,
    hr: float,
    d: float,
    sigma: float,
    r0: float = 1.0,
    include_atmosphere: bool = True,
    temp_c: float = 20.0,
    rel_humidity: float = 50.0,
    pressure_kpa: float = 101.325,
    rho0: float = 1.204,
    c0: float = 343.2,
) -> np.ndarray:
    """Computes total environmental transmission loss TL(f) in dB relative to 1 meter reference.

    TL(f) = Geometrical Spreading (20*log10(r1/r0)) + Excess Ground Attenuation A_excess(f) + Atmospheric Absorption (alpha * r1)
    """
    f_arr = np.atleast_1d(np.asarray(f, dtype=float))
    geo = PropagationGeometry(hs=hs, hr=hr, d=d)
    r1 = max(geo.r1, 1e-3)

    # 1. Geometrical divergence (spherical spreading from point source)
    tl_geom = 20.0 * np.log10(r1 / r0)

    # 2. Excess ground attenuation
    tl_ground = excess_attenuation(f_arr, hs, hr, d, sigma, rho0=rho0, c0=c0)

    # 3. Atmospheric absorption
    if include_atmosphere:
        alpha = atmospheric_attenuation_iso9613(f_arr, temp_c=temp_c, rel_humidity=rel_humidity, pressure_kpa=pressure_kpa)
        tl_atm = alpha * r1
    else:
        tl_atm = np.zeros_like(f_arr)

    return tl_geom + tl_ground + tl_atm


def find_ground_interference_notches(
    f: np.ndarray,
    relative_spl_db: np.ndarray,
    prominence: float = 1.5,
    min_freq: float = 50.0,
    max_freq: float = 5000.0,
) -> Tuple[List[float], List[float]]:
    """Identifies destructive interference notch frequencies and depths from relative SPL curves.

    Notches appear as local minima in relative SPL (or peaks in excess attenuation).

    Returns:
        Tuple of (notch_frequencies_hz, notch_depths_db).
    """
    # Invert relative SPL so minima become peaks
    inv_spl = -relative_spl_db
    peaks, props = find_peaks(inv_spl, prominence=prominence)

    valid_freqs = []
    valid_depths = []
    for p in peaks:
        freq = float(f[p])
        if min_freq <= freq <= max_freq:
            valid_freqs.append(freq)
            valid_depths.append(float(relative_spl_db[p]))

    return valid_freqs, valid_depths


class GroundSurfaceImpedanceClassifier:
    """Classifier for acoustic ground surface impedance and propagation loss curves.

    Evaluates Delany-Bazley complex surface impedance and spherical wave reflection models,
    fits effective airflow resistivity sigma from measured acoustic spectra between known
    source-receiver positions, and classifies surface type into one of four standard fleet classes.
    """

    def __init__(
        self,
        rho0: float = 1.204,
        c0: float = 343.2,
        temp_c: float = 20.0,
        rel_humidity: float = 50.0,
        pressure_kpa: float = 101.325,
    ) -> None:
        self.rho0 = rho0
        self.c0 = c0
        self.temp_c = temp_c
        self.rel_humidity = rel_humidity
        self.pressure_kpa = pressure_kpa

    def compute_impedance(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        sigma: float,
    ) -> Union[complex, np.ndarray]:
        """Compute Delany-Bazley complex surface impedance Z_s(f, sigma)."""
        return delany_bazley_impedance(f, sigma, rho0=self.rho0, c0=self.c0)

    def evaluate_q(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        theta_rad: float,
        r2: float,
        sigma: float,
    ) -> Union[complex, np.ndarray]:
        """Evaluate spherical wave reflection coefficient Q(f, theta, r)."""
        return evaluate_spherical_q(f, theta_rad, r2, sigma, rho0=self.rho0, c0=self.c0)

    def evaluate_spherical_reflection(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        hs: float,
        hr: float,
        d: float,
        sigma: float,
    ) -> Union[complex, np.ndarray]:
        """Evaluate spherical wave reflection coefficient Q(f, hs, hr, d, sigma)."""
        return spherical_wave_reflection_coefficient(f, hs, hr, d, sigma, rho0=self.rho0, c0=self.c0)

    def predict_relative_spl(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        hs: float,
        hr: float,
        d: float,
        sigma: float,
    ) -> np.ndarray:
        """Predict relative sound pressure level L_rel(f) in dB."""
        return relative_sound_pressure_level(f, hs, hr, d, sigma, rho0=self.rho0, c0=self.c0)

    def predict_excess_attenuation(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        hs: float,
        hr: float,
        d: float,
        sigma: float,
    ) -> np.ndarray:
        """Predict excess ground attenuation A_excess(f) in dB."""
        return excess_attenuation(f, hs, hr, d, sigma, rho0=self.rho0, c0=self.c0)

    def predict_transmission_loss(
        self,
        f: Union[float, np.ndarray, Sequence[float]],
        hs: float,
        hr: float,
        d: float,
        sigma: float,
        r0: float = 1.0,
        include_atmosphere: bool = True,
    ) -> np.ndarray:
        """Predict calibrated environmental transmission loss TL(f) in dB."""
        return environmental_transmission_loss(
            f,
            hs,
            hr,
            d,
            sigma,
            r0=r0,
            include_atmosphere=include_atmosphere,
            temp_c=self.temp_c,
            rel_humidity=self.rel_humidity,
            pressure_kpa=self.pressure_kpa,
            rho0=self.rho0,
            c0=self.c0,
        )

    def find_notches(
        self,
        f: np.ndarray,
        hs: float,
        hr: float,
        d: float,
        sigma: float,
        prominence: float = 1.5,
    ) -> List[Tuple[float, float]]:
        """Find theoretical ground interference notch frequencies and depths."""
        spl = self.predict_relative_spl(f, hs, hr, d, sigma)
        freqs, depths = find_ground_interference_notches(f, spl, prominence=prominence)
        return list(zip(freqs, depths))

    def fit_flow_resistivity(
        self,
        frequencies: np.ndarray,
        measured_spectrum: np.ndarray,
        hs: float,
        hr: float,
        d: float,
        is_relative_spl: bool = True,
        estimate_gain: bool = False,
        log_sigma_bounds: Tuple[float, float] = (3.0, 9.0),
    ) -> Tuple[float, float]:
        """Fits effective airflow resistivity sigma from measured acoustic spectrum.

        Args:
            frequencies: 1D array of frequency bins in Hz (e.g. 100 to 4000 Hz).
            measured_spectrum: 1D array of measured spectrum values in dB (relative SPL or absolute SPL).
            hs: Source height in meters.
            hr: Receiver height in meters.
            d: Horizontal distance in meters.
            is_relative_spl: If True, measured_spectrum represents relative SPL (L_rel) in dB.
            estimate_gain: If True (or if not relative SPL), fits a constant broadband gain/level offset.
            log_sigma_bounds: Log10 bounds for flow resistivity search (default 1e3 to 1e9 Pa*s/m^2).

        Returns:
            Tuple of (estimated_sigma, residual_mean_squared_error).
        """
        freqs = np.asarray(frequencies, dtype=float)
        meas = np.asarray(measured_spectrum, dtype=float)

        if len(freqs) != len(meas):
            raise ValueError(f"Frequencies length {len(freqs)} must match measured spectrum length {len(meas)}")

        def loss_fn(log_sigma: float) -> float:
            sigma_val = 10.0**log_sigma
            pred = relative_sound_pressure_level(freqs, hs, hr, d, sigma_val, rho0=self.rho0, c0=self.c0)
            if estimate_gain or not is_relative_spl:
                # Optimal level offset via mean difference
                offset = np.mean(meas - pred)
                pred_adjusted = pred + offset
                return float(np.mean((meas - pred_adjusted) ** 2))
            else:
                return float(np.mean((meas - pred) ** 2))

        # Optimize log10(sigma)
        res = minimize_scalar(loss_fn, bounds=log_sigma_bounds, method="bounded")
        opt_log_sigma = float(res.x)
        opt_sigma = float(10.0**opt_log_sigma)
        opt_mse = float(res.fun)

        return opt_sigma, opt_mse

    def classify(
        self,
        frequencies: np.ndarray,
        measured_spectrum: np.ndarray,
        hs: float,
        hr: float,
        d: float,
        is_relative_spl: bool = True,
        estimate_gain: bool = False,
    ) -> SurfaceClassificationResult:
        """Classifies ground surface type and computes environmental transmission loss curve.

        Args:
            frequencies: 1D array of frequencies in Hz.
            measured_spectrum: 1D array of measured SPL in dB.
            hs: Source height in meters.
            hr: Receiver height in meters.
            d: Horizontal distance in meters.
            is_relative_spl: True if spectrum is relative SPL (transfer function in dB).
            estimate_gain: True to fit broadband source level / calibration offset.

        Returns:
            SurfaceClassificationResult containing surface type, estimated sigma, confidence,
            detected notch frequencies, and calibrated transmission loss curve.
        """
        freqs = np.asarray(frequencies, dtype=float)
        meas = np.asarray(measured_spectrum, dtype=float)
        geo = PropagationGeometry(hs=hs, hr=hr, d=d)

        # 1. Fit continuous flow resistivity sigma
        est_sigma, fit_mse = self.fit_flow_resistivity(
            freqs,
            meas,
            hs,
            hr,
            d,
            is_relative_spl=is_relative_spl,
            estimate_gain=estimate_gain,
        )

        # 2. Evaluate residual MSE and log-likelihood for each candidate surface class
        class_mses: Dict[str, float] = {}
        for stype, props in SURFACE_PROPERTIES.items():
            nom_sigma = props["nominal_sigma"]
            pred = relative_sound_pressure_level(freqs, hs, hr, d, nom_sigma, rho0=self.rho0, c0=self.c0)
            if estimate_gain or not is_relative_spl:
                offset = np.mean(meas - pred)
                pred = pred + offset
            mse = float(np.mean((meas - pred) ** 2))
            class_mses[stype] = mse

        # Evaluate at bounded optimal sigma per class
        class_opt_mses: Dict[str, float] = {}
        for stype, props in SURFACE_PROPERTIES.items():
            s_min, s_max = props["sigma_bounds"]
            log_min, log_max = math.log10(s_min), math.log10(s_max)
            opt_s, opt_m = self.fit_flow_resistivity(
                freqs,
                meas,
                hs,
                hr,
                d,
                is_relative_spl=is_relative_spl,
                estimate_gain=estimate_gain,
                log_sigma_bounds=(log_min, log_max),
            )
            class_opt_mses[stype] = opt_m

        # Compute softmax probabilities over inverse MSE
        # Using temperature scale calibrated to typical dB spectral variances
        scale = max(0.5, fit_mse * 0.5 + 0.5)
        neg_losses = np.array([-class_opt_mses[st] / scale for st in ALL_SURFACE_TYPES])
        # Numerically stable softmax
        exp_losses = np.exp(neg_losses - np.max(neg_losses))
        probs = exp_losses / np.sum(exp_losses)
        surface_probs = {st: float(probs[i]) for i, st in enumerate(ALL_SURFACE_TYPES)}

        # 3. Determine primary classification based on estimated sigma thresholds and spectral physics
        # Thresholds matching physical acoustics:
        # - Hard asphalt/concrete: sigma > 2e7 Pa*s/m2 (+6dB coherent reflection, no LF notch)
        # - Compacted dirt/gravel: sigma approx 1e6 - 5e6 Pa*s/m2
        # - Porous grass/turf: sigma approx 1.5e5 - 3e5 Pa*s/m2 (prominent 200-800Hz notch)
        # - Snow/leaf litter: sigma < 5e4 Pa*s/m2 (high absorption)
        if est_sigma >= 1.0e7:
            classified_type = SURFACE_HARD_ASPHALT_CONCRETE
        elif 6.0e5 <= est_sigma < 1.0e7:
            classified_type = SURFACE_COMPACTED_DIRT_GRAVEL
        elif 6.0e4 <= est_sigma < 6.0e5:
            classified_type = SURFACE_POROUS_GRASS_TURF
        else:
            classified_type = SURFACE_SNOW_LEAF_LITTER

        # Calculate confidence from probability and fit quality
        base_prob = float(surface_probs.get(classified_type, 0.5))
        mse_factor = math.exp(-fit_mse / 8.0)
        confidence = float(max(0.1, min(0.99, base_prob * (0.3 + 0.7 * mse_factor))))

        # 4. Detect spectral notches
        notch_freqs, notch_depths = find_ground_interference_notches(freqs, meas, prominence=1.0)

        # 5. Compute calibrated curves
        pred_rel_spl = self.predict_relative_spl(freqs, hs, hr, d, est_sigma)
        tl_curve = self.predict_transmission_loss(freqs, hs, hr, d, est_sigma, r0=1.0, include_atmosphere=True)

        return SurfaceClassificationResult(
            surface_type=classified_type,
            estimated_sigma=est_sigma,
            confidence=confidence,
            notch_frequencies=notch_freqs,
            notch_depths_db=notch_depths,
            transmission_loss_curve=tl_curve,
            relative_spl_curve=pred_rel_spl,
            surface_probabilities=surface_probs,
            frequencies=freqs,
            geometry=geo,
            metadata={
                "fit_mse": fit_mse,
                "class_mses": class_mses,
                "class_opt_mses": class_opt_mses,
                "rigid_f0_notch_hz": geo.rigid_interference_frequencies(c0=self.c0, max_harmonics=1)[0]
                if geo.delta_r > 1e-4
                else None,
            },
        )

    def batch_classify(
        self,
        records: List[Dict[str, Any]],
        is_relative_spl: bool = True,
    ) -> List[SurfaceClassificationResult]:
        """Classify a batch of source-receiver acoustic spectral observations."""
        results = []
        for rec in records:
            freqs = np.asarray(rec["frequencies"], dtype=float)
            meas = np.asarray(rec["spectrum"], dtype=float)
            hs = float(rec["hs"])
            hr = float(rec["hr"])
            d = float(rec["d"])
            res = self.classify(freqs, meas, hs, hr, d, is_relative_spl=is_relative_spl)
            results.append(res)
        return results
