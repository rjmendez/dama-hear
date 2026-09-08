#!/usr/bin/env python3
"""WGS84 <-> ECEF <-> local ENU. The one place in this repo that converts degrees into metres.

WHY THIS EXISTS. The nodes report lat/lon/height and every solver works in local ENU metres, and
the conversion between them was being done ad hoc. The ad hoc version, written while working out
where a dog was standing, was:

    x = (lon2 - lon1) * cos((lat1 + lat2) / 2) * R
    y = (lat2 - lat1) * R

with R = 6371000 -- a SPHERE. That is wrong three ways at once, and only one of them is small:

  1. The earth is an ellipsoid. At 40 deg latitude one degree of latitude is 111.03 km, not the
     111.19 km a mean-radius sphere gives: 0.14% low, so a 17.9 m baseline came out ~2.5 cm short.
     Small here. NOT small at array scale, and it is a bias, not noise -- it does not average out.
  2. It ignores height entirely. Two nodes at the same lat/lon and different heights come out as
     the same point. Worked on one snapshot of the nyquist/mach pair -- 11.6 m of height against
     17.9 m horizontal -- dropping the height understates the separation by 18%. (That particular
     11.6 m was NOT real: it was single-epoch GNSS vertical noise and it changed sign on the next
     reading. The arithmetic is the point, and it does not depend on the example being true.)
  3. It silently accepts height above mean sea level where the transform is defined on the
     ellipsoid, which injects the local geoid undulation (about -33 m in southern Pennsylvania).

None of these announce themselves. Each one returns a plausible number.

FRAME. ENU is right-handed with +e east, +n north, +u along the ellipsoid normal at the ORIGIN --
not at the point being converted. Over an array a few hundred metres across the two differ by
microradians and the flat local frame the solvers assume is exact to well under a millimetre; over
tens of kilometres it is not, and `enu_frame_error_m` says how much so a caller can find out
rather than assume.

HEIGHT IS ALWAYS ABOVE THE ELLIPSOID here. There is no geoid model in this repo, so hMSL cannot be
converted and is not accepted: the node firmware reports both (`hell_m` and `hmsl_m`) and the
geometry takes `hell_m`. `GEOID_NOTE` says why a caller who only has hMSL cannot simply pass it.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

# WGS84 defining constants (NIMA TR8350.2). a and f are definitional; everything else derives.
WGS84_A = 6378137.0                 # semi-major axis, metres, EXACT by definition
WGS84_F = 1.0 / 298.257223563       # flattening, EXACT by definition
WGS84_B = WGS84_A * (1.0 - WGS84_F)             # semi-minor axis
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)            # first eccentricity squared
WGS84_EP2 = WGS84_E2 / (1.0 - WGS84_E2)         # second eccentricity squared

GEOID_NOTE = (
    "heights here are above the WGS84 ELLIPSOID. Height above mean sea level (hMSL) differs by "
    "the local geoid undulation -- about -33 m in southern Pennsylvania -- and converting between "
    "them needs a geoid model this repo does not carry. The node firmware reports hell_m "
    "alongside hmsl_m precisely so this conversion never has to be guessed."
)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_ell_m: float) -> Tuple[float, float, float]:
    """WGS84 geodetic -> earth-centred earth-fixed metres. Closed form, no iteration."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    # Radius of curvature in the prime vertical. This is the ellipsoid entering the maths; a
    # sphere would use a constant here and that is precisely the 0.14% error described above.
    N = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    return ((N + h_ell_m) * cl * math.cos(lon),
            (N + h_ell_m) * cl * math.sin(lon),
            (N * (1.0 - WGS84_E2) + h_ell_m) * sl)


def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """ECEF -> WGS84 geodetic. Bowring's method, which is closed-form and good to well under a
    micrometre for any height a microphone will ever be at -- so this is an exact inverse of
    geodetic_to_ecef for our purposes, and the round-trip is asserted in the tests."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:                       # on the spin axis: lon is undefined, latitude is +-90
        return (90.0 if z >= 0 else -90.0, 0.0, abs(z) - WGS84_B)
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    st, ct = math.sin(theta), math.cos(theta)
    lat = math.atan2(z + WGS84_EP2 * WGS84_B * st ** 3,
                     p - WGS84_E2 * WGS84_A * ct ** 3)
    sl = math.sin(lat)
    N = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    h = p / math.cos(lat) - N
    return math.degrees(lat), math.degrees(lon), h


def _enu_basis(lat_deg: float, lon_deg: float):
    """Rows of the ECEF->ENU rotation at the origin: east, north, up unit vectors."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sla, cla, slo, clo = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    return ((-slo, clo, 0.0),
            (-sla * clo, -sla * slo, cla),
            (cla * clo, cla * slo, sla))


def geodetic_to_enu(lat_deg: float, lon_deg: float, h_ell_m: float,
                    olat_deg: float, olon_deg: float, oh_ell_m: float) -> Tuple[float, float, float]:
    """WGS84 geodetic -> local ENU metres about an origin. Height is above the ELLIPSOID."""
    x, y, z = geodetic_to_ecef(lat_deg, lon_deg, h_ell_m)
    ox, oy, oz = geodetic_to_ecef(olat_deg, olon_deg, oh_ell_m)
    d = (x - ox, y - oy, z - oz)
    e, n, u = _enu_basis(olat_deg, olon_deg)
    return (sum(a * b for a, b in zip(e, d)),
            sum(a * b for a, b in zip(n, d)),
            sum(a * b for a, b in zip(u, d)))


def enu_to_geodetic(e_m: float, n_m: float, u_m: float,
                    olat_deg: float, olon_deg: float, oh_ell_m: float) -> Tuple[float, float, float]:
    """Local ENU metres -> WGS84 geodetic. Exact inverse of geodetic_to_enu."""
    ex, nx, ux = _enu_basis(olat_deg, olon_deg)
    ox, oy, oz = geodetic_to_ecef(olat_deg, olon_deg, oh_ell_m)
    x = ox + ex[0] * e_m + nx[0] * n_m + ux[0] * u_m
    y = oy + ex[1] * e_m + nx[1] * n_m + ux[1] * u_m
    z = oz + ex[2] * e_m + nx[2] * n_m + ux[2] * u_m
    return ecef_to_geodetic(x, y, z)


def ecef_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """True 3D separation of two geodetic points (lat, lon, h_ell), via ECEF.

    This is the number a baseline should be quoted as. The horizontal-only version is not a
    shorter approximation of it -- it is a different quantity, and for two nodes 17.9 m apart
    horizontally with 11.6 m of height difference it is 35% smaller.
    """
    ax, ay, az = geodetic_to_ecef(*a)
    bx, by, bz = geodetic_to_ecef(*b)
    return math.dist((ax, ay, az), (bx, by, bz))


def enu_frame_error_m(span_m: float, lat_deg: float = 45.0) -> float:
    """Worst-case metres by which the flat local ENU frame misplaces a point `span_m` from the
    origin, because 'up' is taken at the origin rather than at the point.

    The surface curves away from the tangent plane by about d^2 / 2R. Callers working over a few
    hundred metres can ignore it; callers thinking about a 10 km array should not have to
    rediscover that. Returned rather than asserted -- this module does not decide what is
    acceptable for someone else's array.
    """
    sl = math.sin(math.radians(lat_deg))
    # Gaussian mean radius of curvature at this latitude: sqrt(M * N).
    w = math.sqrt(1.0 - WGS84_E2 * sl * sl)
    M = WGS84_A * (1.0 - WGS84_E2) / w ** 3
    N = WGS84_A / w
    return span_m * span_m / (2.0 * math.sqrt(M * N))


def centroid(points: Iterable[Sequence[float]]) -> Tuple[float, float, float]:
    """Geodetic centroid of (lat, lon, h_ell) points, averaged in ECEF rather than in degrees.

    Averaging longitudes directly is wrong across the antimeridian and subtly wrong everywhere
    else; averaging the Cartesian points and converting back is right everywhere. Used to pick a
    survey origin when the caller has not named one.
    """
    pts = [geodetic_to_ecef(*p) for p in points]
    if not pts:
        raise ValueError("no points")
    n = len(pts)
    return ecef_to_geodetic(sum(p[0] for p in pts) / n,
                            sum(p[1] for p in pts) / n,
                            sum(p[2] for p in pts) / n)
