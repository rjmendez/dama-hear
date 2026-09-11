#!/usr/bin/env python3
"""Node survey: node_id -> position in one local ENU frame, read from JSON and validated hard.

Every solver in hear/solve/ takes bare positions and none of them can tell where the numbers came
from. This module is the only door, and it either loads clean or raises: a survey that is silently
wrong puts every later answer in the wrong place at a residual that looks perfect.

Frame is local ENU metres -- east=+e, north=+n, up=+u, right-handed, one arbitrary common origin.
Three components on disk even though the solvers are 2D today (hear/solve/shockwave.py:66,76,96,137
and hear/solve/placement.py:83,84,130,158,199,239 all slice [:2]), so the file survives that being
resolved. `positions_2d()` is the ONLY place `up` is dropped.

⚠️REFUSALS ARE LOAD-TIME, NOT VERDICTS. Duplicate ids, a missing coordinate, coincident entries and
collinear layouts all raise. A survey is read once, before any data exists; a degeneracy known then
should stop the load rather than decorate every later answer, and dop() is genuinely singular on a
line (hear/solve/placement.py:90-96) so there is nothing to decorate.

⚠️A MISSING COORDINATE IS NEVER 0.0. Same reasoning as telemetry.pack's sentinel
(hear/node/telemetry.py:55-58): an absent `u_m` defaulted to zero is a plausible-looking node at
ground level and there is no later measurement that can catch it.

`origin` IS NOW USED. It was carried and ignored, which meant every WGS84-to-local conversion got
hand-rolled by whoever needed one -- and the hand-rolled version was a sphere with the height
thrown away. `enu_of()` and `from_wgs84_nodes()` do it through hear.geodesy instead. The origin's
height must be `h_ell_m`, above the ELLIPSOID; an `hmsl_m` is REFUSED rather than quietly accepted,
because the difference is the local geoid undulation and nothing downstream could detect it.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Dict, List, Optional, Sequence

import numpy as np

from .. import geodesy as GEO
from ..solve import placement as PL

# Two entries this close are one point entered twice. JUDGEMENT: it sits below the node survey
# error that placement.py:484 names as the binding term once DOP is good.
COINCIDENT_M: float = 0.10

# The collinearity threshold is placement's, imported so it cannot drift. survey, point and
# placement must fire on the same layout.
MIN_LINEARITY: float = PL.COLLINEAR_LINEARITY

# Vertical spread under which projecting to 2D is benign. JUDGEMENT, not measured -- the
# 2026-09-05 session recorded no node heights at all.
FLAT_TOL_M: float = 2.0

_FRAME = "enu_local"
_UNITS = "m"
_MAX_NODE_ID = 0xFFFF          # the v2 wire field is uint16; an id that does not fit cannot arrive


class SurveyError(ValueError):
    """Every refusal in this module. ValueError subclass so a caller that catches the repo's
    precondition errors catches these too."""


class SiteOriginError(SurveyError):
    """HEAR_SITE_ORIGIN is malformed, or a real origin is required and the survey's is fictional."""


SITE_ORIGIN_ENV = "HEAR_SITE_ORIGIN"
_SITE_FIELD = re.compile(r"-?\d+(?:\.\d+)?")
_H_ELL_RANGE_M = (-1000.0, 10000.0)


def site_origin(environ=None) -> Optional[Dict]:
    """The origin HEAR_SITE_ORIGIN names as 'lat,lon,h_ell_m', or None when it is unset.
    Refusals never echo the value: it is the site."""
    env = os.environ if environ is None else environ
    if SITE_ORIGIN_ENV not in env:
        return None
    fields = [f.strip() for f in env[SITE_ORIGIN_ENV].split(",")]
    if len(fields) != 3 or not all(_SITE_FIELD.fullmatch(f) for f in fields):
        raise SiteOriginError("%s must be three plain decimals 'lat,lon,h_ell_m'; got %d field(s)"
                              % (SITE_ORIGIN_ENV, len(fields)))
    lat, lon, h = (float(f) for f in fields)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
        raise SiteOriginError("%s is not a latitude,longitude on the earth (or is 0,0)"
                              % SITE_ORIGIN_ENV)
    if not (_H_ELL_RANGE_M[0] <= h <= _H_ELL_RANGE_M[1]):
        raise SiteOriginError("%s height is outside %g..%g m above the ellipsoid"
                              % (SITE_ORIGIN_ENV, _H_ELL_RANGE_M[0], _H_ELL_RANGE_M[1]))
    return {"lat_deg": lat, "lon_deg": lon, "h_ell_m": h,
            "source": "environment %s" % SITE_ORIGIN_ENV}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Survey:
    """Node positions in one local ENU frame, metres.

    Does NOT convert coordinates, interpolate a missing node, or guess a node it was not given.
    Construct it through from_dict() or load_survey(); the validation is there, not here.
    """

    def __init__(self, positions: Dict[int, Sequence[float]],
                 names: Optional[Dict[int, str]] = None,
                 sigma_m: Optional[Dict[int, float]] = None,
                 origin: Optional[Dict] = None,
                 classes: Optional[Dict[int, str]] = None) -> None:
        self.ids: List[int] = sorted(int(k) for k in positions)
        self._pos: Dict[int, np.ndarray] = {
            int(k): np.asarray(v, float).reshape(3) for k, v in positions.items()}
        self.names: Dict[int, str] = {i: (names or {}).get(i, "") for i in self.ids}
        self.sigma_m: Dict[int, float] = {i: float((sigma_m or {}).get(i, 0.0)) for i in self.ids}
        self.classes: Dict[int, str] = {i: (classes or {}).get(i, "") for i in self.ids}
        self.origin: Optional[Dict] = origin

    def arrival_ids(self) -> List[int]:
        """The subset whose hardware class admits its timestamps as TDoA arrivals.

        ⚠️BEING IN THE SURVEY IS NOT THE SAME AS BEING SOLVABLE. hear/nodeclass.py already knows
        which classes can produce an arrival and raises saying what the alternative would cost --
        but `require_arrival` was called from tests and from nowhere else, so nothing in the
        pipeline ever asked. A node added to the survey for its position (a PUC on NTP, 3 ms =
        1.0 m of range) was then indistinguishable from a PPS node at 3.4 cm.

        A node with NO stated class is included, because every survey written before the field
        existed omits it and silently dropping those nodes would be a worse failure than the one
        this fixes. State the class to be refused.
        """
        from .. import nodeclass                       # local: keeps survey.py importable alone
        out = []
        for i in self.ids:
            c = self.classes.get(i, "")
            if not c:
                out.append(i)
                continue
            try:
                if nodeclass.get(c).contributes_arrival():
                    out.append(i)
            except Exception:                          # unknown class name: unstated, not "no"
                out.append(i)
        return out

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, node_id) -> bool:
        return node_id in self._pos

    def __repr__(self) -> str:
        # Must not raise. linearity() needs >= 3 nodes, so a 2-node survey -- which is a legal
        # object, just not a solvable array -- made its own repr throw, which is the worst moment
        # for that to happen: you print it precisely when something is already wrong.
        try:
            lin = "%.3f" % self.linearity()
        except (ValueError, SurveyError):
            lin = "n/a (needs >= 3 nodes)"
        return "Survey(%d nodes, diameter %.1f m, linearity %s)" % (
            len(self.ids), self.diameter_m(), lin)

    def position(self, node_id: int) -> np.ndarray:
        """(3,) float64 (east, north, up) in metres. Raises for an id the survey does not have --
        an unsurveyed node has no position and 0,0,0 is not a stand-in."""
        try:
            return self._pos[node_id].copy()
        except KeyError:
            raise SurveyError("node %r is not in the survey (have %s)" % (node_id, self.ids))

    def positions(self, node_ids: Sequence[int]) -> np.ndarray:
        """(N,3) metres, rows IN THE ORDER GIVEN and never re-sorted. Callers align arrivals to
        node_ids positionally; a silent sort here mislabels every arrival in the array."""
        return np.array([self.position(i) for i in node_ids], float).reshape(len(node_ids), 3)

    def positions_2d(self, node_ids: Sequence[int]) -> np.ndarray:
        """(N,2) east/north, rows IN THE ORDER GIVEN.

        ⚠️DROPS `up`, and there is now only one caller left that should: hear/solve/shockwave.py
        is still a planar cone model. hear/solve/point.py is 3D and must be given positions(),
        not this -- feeding it the projection throws away node height that it would otherwise use
        as a real distance, and no residual can see the difference.

        Kept rather than deleted because the cone path genuinely needs a projection, and doing it
        through a named method beats an anonymous [:2] at the call site. Call
        validate_2d_assumption() to get the cost of the projection back as data.
        """
        return self.positions(node_ids)[:, :2]

    def enu_of(self, lat_deg: float, lon_deg: float, h_ell_m: float) -> np.ndarray:
        """A WGS84 point in THIS survey's local frame. Requires an `origin` with lat/lon/h_ell.

        This is what `origin` was always for and never did: a survey whose anchor is only a
        comment cannot place anything measured in the field, so every conversion got hand-rolled
        at the call site -- which is where the spherical-earth formula came from.
        """
        o = self.origin_geodetic()
        return np.asarray(GEO.geodetic_to_enu(lat_deg, lon_deg, h_ell_m, *o), float)

    def origin_geodetic(self):
        """(lat, lon, h_ell) of the frame origin, or raises. HEIGHT IS ABOVE THE ELLIPSOID: a
        survey that anchors itself with hMSL is off by the geoid undulation and nothing downstream
        can detect it, so the key is named h_ell_m and an hmsl_m is refused rather than accepted
        as a synonym."""
        o = self.origin
        if not isinstance(o, dict):
            raise SurveyError("survey has no origin: cannot convert WGS84 into this frame")
        if "hmsl_m" in o and "h_ell_m" not in o:
            raise SurveyError(
                "survey origin gives hmsl_m but not h_ell_m. %s" % GEO.GEOID_NOTE)
        try:
            return (float(o["lat_deg"]), float(o["lon_deg"]), float(o["h_ell_m"]))
        except (KeyError, TypeError, ValueError):
            raise SurveyError("survey origin needs numeric lat_deg, lon_deg and h_ell_m; got %r"
                              % (o,))

    def origin_is_fictional(self) -> bool:
        """Fails closed: a `fictional` key with any value but an explicit false counts."""
        if not isinstance(self.origin, dict) or "fictional" not in self.origin:
            return False
        return self.origin["fictional"] is not False

    def diameter_m(self) -> float:
        """Largest pairwise 3D distance, metres. 3D and not horizontal because it bounds
        inter-node propagation and associate uses it as an upper bound -- the conservative side."""
        P = self.positions(self.ids)
        d = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
        return float(d.max())

    def vertical_spread_m(self) -> float:
        return float(np.ptp(self.positions(self.ids)[:, 2]))

    def linearity(self) -> float:
        """s1/s0 of the centred horizontal positions. placement's formula, not a local copy."""
        return PL.linearity(self.positions(self.ids))

    def validate_2d_assumption(self, tol_m: float = FLAT_TOL_M) -> Dict:
        """The 2D projection as data, so a backend can publish it. A docstring warning is
        invisible to an operator reading a result; a field is not."""
        spread = self.vertical_spread_m()
        ok = spread <= float(tol_m)
        note = None if ok else (
            "vertical spread %.1f m exceeds %.1f m: positions_2d() discards it, so node ranges are "
            "mis-modelled by up to that much and no residual can see it" % (spread, tol_m))
        return {"vertical_spread_m": spread, "tol_m": float(tol_m), "ok": ok, "note": note}

    def to_dict(self) -> Dict:
        """The on-disk JSON shape. Round-trips through from_dict()."""
        d: Dict = {"frame": _FRAME, "units": _UNITS}
        if self.origin is not None:
            d["origin"] = self.origin
        d["nodes"] = [
            {"node_id": i, "name": self.names[i],
             "e_m": float(self._pos[i][0]), "n_m": float(self._pos[i][1]),
             "u_m": float(self._pos[i][2]), "sigma_m": self.sigma_m[i]}
            for i in self.ids]
        return d


def _read_node(entry, index: int, seen: Dict[int, int]) -> Dict:
    if not isinstance(entry, dict):
        raise SurveyError("node at index %d is %s, not an object" % (index, type(entry).__name__))
    nid = entry.get("node_id")
    if nid is None or not isinstance(nid, int) or isinstance(nid, bool):
        raise SurveyError("node at index %d has node_id %r: an int is required" % (index, nid))
    if not (0 <= nid <= _MAX_NODE_ID):
        raise SurveyError("node_id %d outside 0..%d, the uint16 the wire carries"
                          % (nid, _MAX_NODE_ID))
    if nid in seen:
        raise SurveyError("duplicate node_id %d at indices %d and %d" % (nid, seen[nid], index))
    seen[nid] = index
    xyz = []
    for key in ("e_m", "n_m", "u_m"):
        v = entry.get(key)
        if v is None:
            raise SurveyError("node %d has no %s: a missing coordinate is never 0.0" % (nid, key))
        if not _is_number(v):
            raise SurveyError("node %d has %s %r, which is not a number" % (nid, key, v))
        if not math.isfinite(float(v)):
            raise SurveyError("node %d has non-finite %s %r" % (nid, key, v))
        xyz.append(float(v))
    sig = entry.get("sigma_m", 0.0)
    if not _is_number(sig) or not math.isfinite(float(sig)) or float(sig) < 0.0:
        raise SurveyError("node %d has sigma_m %r: need a finite non-negative number" % (nid, sig))
    name = entry.get("name", "")
    if not isinstance(name, str):
        raise SurveyError("node %d has name %r, which is not a string" % (nid, name))
    # ⚠️A SURVEYED POSITION IS NOT PERMISSION TO USE THE NODE AS AN ARRIVAL. `class` names the
    # hardware class from hear/nodeclass.py, which is what says whether the node's timestamps are
    # TDoA arrivals at all -- a PUC timed by NTP is 3 ms, 1.0 m of range, and is refused. Before
    # this field existed the survey carried no such statement, so an entry added for a node that
    # cannot range was indistinguishable from one that can. Unset means "unstated", not "yes".
    cls = entry.get("class")
    if cls is not None and not isinstance(cls, str):
        raise SurveyError("node %d has class %r, which is not a string" % (nid, cls))
    return {"node_id": nid, "xyz": xyz, "name": name, "sigma_m": float(sig),
            "class": cls or ""}


def from_dict(d: Dict, min_nodes: int = 3) -> Survey:
    """Validate and build. Raises SurveyError naming the offending ids and the measured number.

    Refuses: a frame or unit it did not expect, a missing or empty node list, a bad or duplicate
    node_id, a missing/non-numeric/non-finite coordinate, fewer nodes than the solver needs,
    coincident entries, and a collinear layout.
    """
    if not isinstance(d, dict):
        raise SurveyError("survey is %s, not an object" % type(d).__name__)
    frame = d.get("frame")
    if frame != _FRAME:
        raise SurveyError("frame %r is not %r: this module does no geodesy and will not convert it"
                          % (frame, _FRAME))
    units = d.get("units")
    if units != _UNITS:
        raise SurveyError("units %r is not %r" % (units, _UNITS))
    nodes = d.get("nodes")
    if not isinstance(nodes, list):
        raise SurveyError("'nodes' is %s, not a list" % type(nodes).__name__)
    if not nodes:
        raise SurveyError("survey has 0 nodes")

    seen: Dict[int, int] = {}
    rows = [_read_node(e, i, seen) for i, e in enumerate(nodes)]
    if len(rows) < int(min_nodes):
        raise SurveyError("survey has %d nodes, solver needs %d" % (len(rows), int(min_nodes)))

    sv = Survey({r["node_id"]: r["xyz"] for r in rows},
                names={r["node_id"]: r["name"] for r in rows},
                sigma_m={r["node_id"]: r["sigma_m"] for r in rows},
                classes={r["node_id"]: r["class"] for r in rows},
                origin=d.get("origin"))

    P = sv.positions(sv.ids)
    for a in range(len(sv.ids)):
        for b in range(a + 1, len(sv.ids)):
            dist = float(np.linalg.norm(P[a] - P[b]))
            if dist < COINCIDENT_M:
                raise SurveyError(
                    "nodes %d and %d are %.3f m apart, under %.2f m: one point entered twice"
                    % (sv.ids[a], sv.ids[b], dist, COINCIDENT_M))

    # Below 3 nodes there is no line to be on and PL.linearity refuses; min_nodes is the caller's
    # to lower, so do not turn that into a different error message.
    if len(sv) >= 3:
        lin = sv.linearity()
        if lin < MIN_LINEARITY:
            raise SurveyError("nodes are collinear (linearity %.4f): point-source DOP is singular"
                              % lin)
    return sv


def from_wgs84_nodes(entries: Sequence[Dict], origin: Optional[Dict] = None,
                     min_nodes: int = 3) -> Survey:
    """Build a Survey from what the nodes actually report: lat, lon and ELLIPSOID height.

    This is the missing half of the loop. The node firmware measures its own position and every
    solver wants local ENU metres, and until now nothing joined the two -- so the conversion was
    done by hand at the point of use, with a spherical earth and the height dropped.

    ⚠️`h_ell_m`, NOT `hmsl_m`. Both come off the same NAV-PVT and they differ by the local geoid
    undulation (about -33 m in southern Pennsylvania). Passing hMSL puts every node at the wrong
    height by very nearly the same amount, so it largely cancels in a baseline and would not show
    up in a residual -- which is exactly why it is refused here rather than silently accepted.

    With no `origin`, the geodetic centroid of the nodes is used and recorded in the survey, so
    the frame is reproducible and the file says where it came from.
    """
    pts = []
    for i, e in enumerate(entries):
        if "hmsl_m" in e and "h_ell_m" not in e:
            raise SurveyError("node at index %d gives hmsl_m but not h_ell_m. %s"
                              % (i, GEO.GEOID_NOTE))
        try:
            pts.append((float(e["lat_deg"]), float(e["lon_deg"]), float(e["h_ell_m"])))
        except (KeyError, TypeError, ValueError):
            raise SurveyError("node at index %d needs numeric lat_deg, lon_deg and h_ell_m; "
                              "got %r" % (i, e))
    if not pts:
        raise SurveyError("no nodes given")
    if origin is None:
        olat, olon, oh = GEO.centroid(pts)
        origin = {"lat_deg": olat, "lon_deg": olon, "h_ell_m": oh, "source": "node centroid"}
    o = (float(origin["lat_deg"]), float(origin["lon_deg"]), float(origin["h_ell_m"]))
    nodes = []
    for e, p in zip(entries, pts):
        en = GEO.geodetic_to_enu(p[0], p[1], p[2], *o)
        nodes.append({"node_id": e["node_id"], "name": e.get("name", ""),
                      "e_m": en[0], "n_m": en[1], "u_m": en[2],
                      "sigma_m": float(e.get("sigma_m", 0.0))})
    return from_dict({"frame": _FRAME, "units": _UNITS, "origin": origin, "nodes": nodes},
                     min_nodes=min_nodes)


def load_survey(path: str, min_nodes: int = 3, require_real_origin: bool = False) -> Survey:
    """Read JSON from `path` and validate it. Does not search, cache or default a path.

    HEAR_SITE_ORIGIN, when set, replaces the file's origin. `require_real_origin` refuses a survey
    whose origin is still marked fictional, which is what the public repo ships."""
    site = site_origin()
    with open(path, "r") as fh:
        d = json.load(fh)
    if site is not None and isinstance(d, dict):
        d = dict(d, origin=site)
    sv = from_dict(d, min_nodes=min_nodes)
    if require_real_origin and sv.origin_is_fictional():
        raise SiteOriginError(
            "the origin in %s is marked fictional (the public repo does not carry the site) and "
            "%s is unset; every lat/lon converted against it would be misplaced. Set %s="
            "'lat,lon,h_ell_m' to the real frame origin (cluster: Secret hear-site, key origin)"
            % (path, SITE_ORIGIN_ENV, SITE_ORIGIN_ENV))
    return sv
