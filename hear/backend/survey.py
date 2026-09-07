#!/usr/bin/env python3
"""Node survey: node_id -> position in one local ENU frame, read from JSON and validated hard.

Every solver in hear/solve/ takes bare positions and none of them can tell where the numbers came
from. This module is the only door, and it either loads clean or raises: a survey that is silently
wrong puts every later answer in the wrong place at a residual that looks perfect.

Frame is local ENU metres -- east=+e, north=+n, up=+u, right-handed, one arbitrary common origin.
Three components on disk even though the solvers are 2D today (hear/solve/shockwave.py:58,68,88,129
and hear/solve/placement.py:61,62,108,136,177 all slice [:2]), so the file survives that being
resolved. `positions_2d()` is the ONLY place `up` is dropped.

⚠️REFUSALS ARE LOAD-TIME, NOT VERDICTS. Duplicate ids, a missing coordinate, coincident entries and
collinear layouts all raise. A survey is read once, before any data exists; a degeneracy known then
should stop the load rather than decorate every later answer, and dop() is genuinely singular on a
line (hear/solve/placement.py:73-77) so there is nothing to decorate.

⚠️A MISSING COORDINATE IS NEVER 0.0. Same reasoning as telemetry.pack's sentinel
(hear/node/telemetry.py:55-58): an absent `u_m` defaulted to zero is a plausible-looking node at
ground level and there is no later measurement that can catch it.

⚠️`origin` IS CARRIED AND NEVER USED. Naming a lat/lon anchor is what makes a survey reproducible
between sessions; this module does no geodesy and will not convert it into the local frame.
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Sequence

import numpy as np

from ..solve import placement as PL

# Two entries this close are one point entered twice. JUDGEMENT: it sits below the node survey
# error that placement.py:260 names as the binding term once DOP is good.
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
                 origin: Optional[Dict] = None) -> None:
        self.ids: List[int] = sorted(int(k) for k in positions)
        self._pos: Dict[int, np.ndarray] = {
            int(k): np.asarray(v, float).reshape(3) for k, v in positions.items()}
        self.names: Dict[int, str] = {i: (names or {}).get(i, "") for i in self.ids}
        self.sigma_m: Dict[int, float] = {i: float((sigma_m or {}).get(i, 0.0)) for i in self.ids}
        self.origin: Optional[Dict] = origin

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, node_id) -> bool:
        return node_id in self._pos

    def __repr__(self) -> str:
        return "Survey(%d nodes, diameter %.1f m, linearity %.3f)" % (
            len(self.ids), self.diameter_m(), self.linearity())

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
        """(N,2) east/north, rows IN THE ORDER GIVEN. This is what hear/solve/*.py consumes.

        ⚠️DROPS `up`. Valid only while the solvers are 2D (hear/solve/shockwave.py:58,68,88,129 and
        hear/solve/placement.py:61,62,108,136,177). This is the one site that projects; call
        validate_2d_assumption() to get the assumption back as data instead of as a comment.
        """
        return self.positions(node_ids)[:, :2]

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
    return {"node_id": nid, "xyz": xyz, "name": name, "sigma_m": float(sig)}


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


def load_survey(path: str, min_nodes: int = 3) -> Survey:
    """Read JSON from `path` and validate it. Does not search, cache or default a path."""
    with open(path, "r") as fh:
        return from_dict(json.load(fh), min_nodes=min_nodes)
