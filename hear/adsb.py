#!/usr/bin/env python3
"""ADS-B ingestion and acoustic-flightpath correlation.

WHY THIS EXISTS. docs/acoustic-stack.md:586-590 already said it: an RTL-SDR feeding dump1090 turns
every overflight into a timestamped, typed, altitude- and range-tagged positive -- and, just as
usefully, a negative for every second a rotor-band detector fires with nothing overhead. Probed at
build time (2026-09-13): no dump1090/readsb/tar1090 on the LAN's usual ports (8080, 8088, 30003,
30005, 30053, 30105, 30154), no RTL-SDR USB device present, no ADS-B pod in the cluster, and no
public-API key configured. **No ADS-B receiver exists in the fleet today.** This module is written
so that the day one shows up (`--source http://host:8080/data/aircraft.json`), or a recorded
`aircraft.json` capture / NDJSON replay is handed to it, the geometry and correlation code needs no
changes -- only `discover_local_source()` needs to find something.

SCOPE. Three things, each usable alone:

  1. INGEST a readsb/dump1090-family `aircraft.json` (live over HTTP, or from a file/replay) into
     `StateVector` records with everything downstream needs: position, kinematics, timestamp.
  2. GEOMETRY: for one state vector against this array's own `survey.json` origin -- the same
     door every other geodesy conversion in this repo uses (hear/geodesy.py, hear/backend/
     survey.py) -- slant range, azimuth, elevation, acoustic propagation delay, a straight-line
     constant-velocity CPA, and the Doppler ratio curve that trajectory implies.
  3. CORRELATE a set of acoustic observations (timestamp, and optionally a measured bearing --
     from a TDoA-derived direction, or bare rotor/low-frequency energy with no bearing at all)
     against a set of candidate state vectors, inside a caller-set time and bearing window.

⚠️ALTITUDE HERE IS TREATED AS HEIGHT ABOVE THE SURVEY ORIGIN'S ELLIPSOID, AND IT IS NOT. ADS-B
`alt_baro` is pressure altitude referenced to the 1013.25 hPa standard datum, not WGS84 nor MSL;
`alt_geom`, where a feed supplies it, is GNSS height and closer to what this module wants but is
frequently absent. Given the platform this repo targets -- ranges of tens of metres to a few
kilometres to a rotor/prop overhead -- a few tens of metres of datum error moves an already-large
elevation angle by a fraction of a degree, which is why the shortcut is taken; it would NOT be
acceptable for grazing-incidence geometry near the horizon, and no caller here computes that.

⚠️NO WIND, NO REFRACTION, NO EARTH CURVATURE BEYOND WHAT hear.geodesy's ENU FRAME ALREADY CARRIES.
The CPA and Doppler models assume straight-line constant-velocity flight and a homogeneous,
still atmosphere at the given speed of sound -- adequate for the minutes around a flyover this
module is built for, not for anything that needs the actual bent ray path.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .backend import survey as SV
from .solve.shockwave import sound_speed

FT_TO_M = 0.3048
KT_TO_MPS = 0.514444
FPM_TO_MPS = FT_TO_M / 60.0

# Ports this project's docs and common feeder stacks (readsb/dump1090/tar1090/ultrafeeder) expose
# an aircraft.json or Beast/SBS socket on. Probed, never assumed live.
KNOWN_PORTS: Tuple[int, ...] = (8080, 8088, 30003, 30005, 30053, 30105, 30154)
AIRCRAFT_JSON_PATHS: Tuple[str, ...] = ("/data/aircraft.json", "/data/aircraft.json/")


# ── ingestion ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StateVector:
    """One aircraft position report. Timestamp is UNIX seconds, UTC -- the same clock base as
    everything else in this repo's pool (hear/backend/pipeline.py)."""
    icao: str
    callsign: Optional[str]
    lat_deg: float
    lon_deg: float
    alt_ft: float
    ground_speed_kts: float
    track_deg: float
    vertical_rate_fpm: float
    ts_s: float

    def alt_m(self) -> float:
        return self.alt_ft * FT_TO_M

    def ground_speed_mps(self) -> float:
        return self.ground_speed_kts * KT_TO_MPS

    def vertical_rate_mps(self) -> float:
        return self.vertical_rate_fpm * FPM_TO_MPS

    def velocity_enu_mps(self) -> Tuple[float, float, float]:
        """(ve, vn, vu), m/s, from track/ground-speed/vertical-rate. Track is degrees clockwise
        from true north, the ADS-B convention -- NOT the math convention this file's atan2 calls
        use for bearing, and the two are converted at every boundary rather than mixed."""
        rad = math.radians(self.track_deg)
        return (self.ground_speed_mps() * math.sin(rad),
                self.ground_speed_mps() * math.cos(rad),
                self.vertical_rate_mps())


def _num(v):
    """A JSON field that is present and numeric, else None. Distinguishes 'no position yet'
    (readsb omits lat/lon until an aircraft has one) from a genuine 0.0 -- the same discipline
    hear/backend/survey.py applies to node coordinates."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


def parse_aircraft_json(doc: Dict) -> List[StateVector]:
    """readsb/dump1090-family `aircraft.json` -> StateVectors with a resolved position.

    Accepts either dialect's field names: `alt_baro`/`altitude`, `gs`/`speed`,
    `baro_rate`/`geom_rate`/`vert_rate`. An entry with no lat/lon (aircraft heard on Mode S but not
    yet positioned) is skipped, not defaulted to 0,0 -- a phantom aircraft at the equator is worse
    than one silently absent. `now` is the feed's own capture time; a per-aircraft `seen` (seconds
    since that aircraft's last message) is subtracted from it so ts_s is that aircraft's own
    report time, not the poll time -- the two differ by up to the poll interval for a fast mover.
    """
    now = _num(doc.get("now"))
    if now is None:
        now = time.time()
    out: List[StateVector] = []
    for a in doc.get("aircraft") or []:
        if not isinstance(a, dict):
            continue
        lat = _num(a.get("lat"))
        lon = _num(a.get("lon"))
        if lat is None or lon is None:
            continue
        alt = _num(a.get("alt_baro"))
        if alt is None:
            alt = _num(a.get("altitude"))
        if alt is None:
            continue
        gs = _num(a.get("gs"))
        if gs is None:
            gs = _num(a.get("speed"))
        track = _num(a.get("track"))
        if track is None:
            track = _num(a.get("true_heading"))
        vrate = _num(a.get("baro_rate"))
        if vrate is None:
            vrate = _num(a.get("geom_rate"))
        if vrate is None:
            vrate = _num(a.get("vert_rate"))
        seen = _num(a.get("seen")) or 0.0
        icao = a.get("hex") or a.get("icao") or a.get("icao24")
        if not icao:
            continue
        flight = a.get("flight") or a.get("callsign")
        out.append(StateVector(
            icao=str(icao).strip().lower(),
            callsign=(str(flight).strip() or None) if flight else None,
            lat_deg=lat, lon_deg=lon, alt_ft=alt,
            ground_speed_kts=gs or 0.0, track_deg=track or 0.0,
            vertical_rate_fpm=vrate or 0.0,
            ts_s=now - seen))
    return out


def load_aircraft_json_file(path: str) -> List[StateVector]:
    """Fallback source #1: a captured `aircraft.json` snapshot, for offline dev and tests."""
    with open(path, "r") as fh:
        return parse_aircraft_json(json.load(fh))


def replay_ndjson(path: str) -> Iterable[List[StateVector]]:
    """Fallback source #2: one `aircraft.json` document per line, oldest first -- a cheap capture
    format (`while true; do curl -s .../aircraft.json; sleep 1; done >>capture.ndjson`) that lets a
    whole flyover be replayed through the exact same parser a live feed uses. Yields one snapshot
    (a list of StateVectors) per non-blank line; a line that fails to parse as JSON is skipped, not
    fatal to the rest of the replay -- a truncated capture should not lose everything after the cut."""
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield parse_aircraft_json(doc)


def fetch_aircraft_json(url: str, timeout: float = 5.0) -> List[StateVector]:
    """Live source: GET a readsb/dump1090 `aircraft.json` over HTTP. Raises on any failure --
    a caller that wants a fallback chain gets to choose it (see `discover_local_source`); silently
    returning an empty list here would make 'no aircraft nearby' indistinguishable from 'the feed
    is down', and those need different responses."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return parse_aircraft_json(json.loads(r.read().decode("utf-8", "replace")))


def discover_local_source(hosts: Optional[Sequence[str]] = None,
                          ports: Sequence[int] = KNOWN_PORTS,
                          paths: Sequence[str] = AIRCRAFT_JSON_PATHS,
                          timeout: float = 0.5) -> Optional[str]:
    """Probe `hosts` (default: localhost plus this repo's LAN convention 172.16.100.1) x `ports`
    x `paths` for a live `aircraft.json`. Returns the first URL that answers with a JSON object
    carrying an `aircraft` list, else None -- never raises, because "nothing is running" is the
    expected answer today and every caller must be able to fall back to a file.
    """
    if hosts is None:
        hosts = ("127.0.0.1", "localhost", "172.16.100.1")
    for host in hosts:
        for port in ports:
            for path in paths:
                url = "http://%s:%d%s" % (host, port, path)
                try:
                    with urllib.request.urlopen(url, timeout=timeout) as r:
                        doc = json.loads(r.read().decode("utf-8", "replace"))
                except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                    continue
                if isinstance(doc, dict) and isinstance(doc.get("aircraft"), list):
                    return url
    return None


# ── geometry ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Geometry:
    """One aircraft position resolved against the array's local ENU frame."""
    enu_m: Tuple[float, float, float]
    slant_range_m: float
    azimuth_deg: float          # clockwise from true north, 0-360, as seen from the origin
    elevation_deg: float        # above the local horizontal; negative is below (never for a flyover)
    acoustic_delay_s: float     # range / c: how much LATER the sound arrives than the light


def _bearing_elevation(e: float, n: float, u: float) -> Tuple[float, float, float]:
    horiz = math.hypot(e, n)
    rng = math.hypot(horiz, u)
    az = math.degrees(math.atan2(e, n)) % 360.0
    el = math.degrees(math.atan2(u, horiz)) if rng > 0 else 0.0
    return rng, az, el


def compute_geometry(sv: StateVector, sy: SV.Survey, temp_c: float = 20.0) -> Geometry:
    """Slant range/azimuth/elevation/acoustic delay of `sv` against `sy`'s origin.

    Elevation is measured from the origin, i.e. from wherever the array's local frame is
    centred -- not from any one node -- which is right for an array whose node spacing (tens of
    metres, tests/test_soundspeed.py) is negligible next to aircraft slant ranges (kilometres).
    """
    e, n, u = sy.enu_of(sv.lat_deg, sv.lon_deg, sv.alt_m())
    rng, az, el = _bearing_elevation(e, n, u)
    c = sound_speed(temp_c)
    return Geometry(enu_m=(e, n, u), slant_range_m=rng, azimuth_deg=az, elevation_deg=el,
                    acoustic_delay_s=rng / c)


@dataclass(frozen=True)
class ClosestApproach:
    """Closest point of a straight-line, constant-velocity extrapolation of `sv` to the array
    origin. `t_s` is seconds after `sv.ts_s`; NEGATIVE means the closest approach was already in
    the past when the state vector was reported (a receding aircraft), which is a legitimate and
    common answer, not an error."""
    t_s: float
    range_m: float
    ts_unix_s: float
    enu_m: Tuple[float, float, float]


def closest_point_of_approach(sv: StateVector, sy: SV.Survey) -> ClosestApproach:
    """t* minimising |p0 + v t|^2 for the straight-line track through `sv`'s reported position and
    velocity: t* = -(p0 . v) / (v . v). Undefined only for a stationary aircraft (v = 0, which an
    airborne ADS-B position essentially never reports); that case returns t*=0 (the reported fix
    itself, which is the only point on a degenerate 'track') rather than raising, since a caller
    asking for CPA on a hovering helicopter still wants an answer.
    """
    p0 = sy.enu_of(sv.lat_deg, sv.lon_deg, sv.alt_m())
    v = sv.velocity_enu_mps()
    vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2
    if vv <= 0.0:
        t_star = 0.0
    else:
        pv = p0[0] * v[0] + p0[1] * v[1] + p0[2] * v[2]
        t_star = -pv / vv
    p_star = (p0[0] + v[0] * t_star, p0[1] + v[1] * t_star, p0[2] + v[2] * t_star)
    rng = math.sqrt(p_star[0] ** 2 + p_star[1] ** 2 + p_star[2] ** 2)
    return ClosestApproach(t_s=t_star, range_m=rng, ts_unix_s=sv.ts_s + t_star, enu_m=p_star)


def doppler_curve(sv: StateVector, sy: SV.Survey, temp_c: float = 20.0,
                  t_start_s: float = -60.0, t_end_s: float = 60.0,
                  dt_s: float = 1.0) -> List[Tuple[float, float, float]]:
    """Acoustic Doppler ratio f_observed/f_source over emission times `sv.ts_s + t_start_s .. +
    t_end_s`, straight-line constant-velocity model, stationary receiver, no wind.

    Returns (t_emit_s, t_arrive_s, ratio) triples -- t_emit_s relative to `sv.ts_s`, t_arrive_s in
    the same relative frame but shifted by that emission's own acoustic delay (so the curve is
    ready to plot against what an array actually recorded, not against the aircraft's clock).

    ratio = c / (c + v_radial), v_radial = d(range)/dt at the emission instant, positive when
    receding. ratio > 1 (pitch raised) while approaching, < 1 while receding, matching the
    classic sign convention for a moving source and stationary observer.
    """
    if t_end_s < t_start_s:
        raise ValueError("t_end_s must be >= t_start_s")
    if dt_s <= 0.0:
        raise ValueError("dt_s must be positive")
    c = sound_speed(temp_c)
    p0 = sy.enu_of(sv.lat_deg, sv.lon_deg, sv.alt_m())
    v = sv.velocity_enu_mps()
    out: List[Tuple[float, float, float]] = []
    n_steps = int(math.floor((t_end_s - t_start_s) / dt_s + 1e-9)) + 1
    for i in range(n_steps):
        t = t_start_s + i * dt_s
        p = (p0[0] + v[0] * t, p0[1] + v[1] * t, p0[2] + v[2] * t)
        rng = math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2)
        if rng <= 0.0:
            v_radial = 0.0
        else:
            v_radial = (p[0] * v[0] + p[1] * v[1] + p[2] * v[2]) / rng
        ratio = c / (c + v_radial) if (c + v_radial) != 0.0 else float("inf")
        out.append((t, t + rng / c, ratio))
    return out


# ── correlation ───────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AcousticObservation:
    """One thing heard: a timestamp, and OPTIONALLY a measured bearing (from a TDoA-derived
    direction fit) and/or an energy level (bare rotor/low-frequency band energy, no direction at
    all). A bearing-less observation still correlates on time alone; a bearing narrows it."""
    ts_s: float
    azimuth_deg: Optional[float] = None
    elevation_deg: Optional[float] = None
    energy: Optional[float] = None


@dataclass(frozen=True)
class Correlation:
    """One acoustic observation matched to one candidate aircraft state vector."""
    observation: AcousticObservation
    state_vector: StateVector
    predicted: Geometry
    dt_s: float                 # observation.ts_s - predicted acoustic arrival time, signed
    bearing_error_deg: Optional[float]
    score: float                 # lower is better; see correlate() for the combination


def _angle_diff_deg(a: float, b: float) -> float:
    """Smallest magnitude difference between two bearings, 0-180 deg, wrap-safe."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


def correlate(observations: Sequence[AcousticObservation], candidates: Sequence[StateVector],
             sy: SV.Survey, temp_c: float = 20.0, time_window_s: float = 30.0,
             bearing_window_deg: Optional[float] = 20.0) -> List[Correlation]:
    """Match each acoustic observation to whichever candidate aircraft's PREDICTED acoustic
    arrival (state-vector position and velocity extrapolated to the moment its sound would reach
    the array origin) lands within `time_window_s` of the observation, and -- if the observation
    carries a bearing and `bearing_window_deg` is not None -- within that bearing window too.

    One entry per (observation, candidate) pair that passes both gates, sorted best score first.
    A caller wanting one match per observation takes the first entry for that observation's id();
    this returns all admissible pairs rather than picking for the caller, because a genuinely
    ambiguous case (two aircraft crossing near the array) should be visible, not resolved by an
    arbitrary tie-break buried in this function.

    SCORE is normalised time error in `time_window_s` units, plus (when bearing is compared)
    normalised bearing error in `bearing_window_deg` units -- both dimensionless and each capped
    at 1.0 by the gate itself, so the two terms are commensurate without a tuned weight.
    """
    if time_window_s <= 0.0:
        raise ValueError("time_window_s must be positive")
    if bearing_window_deg is not None and bearing_window_deg <= 0.0:
        raise ValueError("bearing_window_deg must be positive when given")

    out: List[Correlation] = []
    for cand in candidates:
        cpa = closest_point_of_approach(cand, sy)
        v = cand.velocity_enu_mps()
        for obs in observations:
            # Extrapolate the aircraft to the emission time whose sound would arrive near obs.ts_s.
            # Solved by one fixed-point pass from t_emit = obs.ts_s - cand.ts_s (i.e. ignoring the
            # delay first), then correcting by that estimate's own delay once -- exact for a
            # straight-line track because range varies smoothly and the correction is a fraction
            # of the coarse time window this function gates on, never iterated to convergence
            # because a second correction changes arrival time by microseconds at these ranges.
            t_rel = obs.ts_s - cand.ts_s
            p0 = sy.enu_of(cand.lat_deg, cand.lon_deg, cand.alt_m())
            p = (p0[0] + v[0] * t_rel, p0[1] + v[1] * t_rel, p0[2] + v[2] * t_rel)
            rng, az, el = _bearing_elevation(*p)
            c = sound_speed(temp_c)
            delay = rng / c
            t_emit = t_rel - delay
            p_emit = (p0[0] + v[0] * t_emit, p0[1] + v[1] * t_emit, p0[2] + v[2] * t_emit)
            rng2, az2, el2 = _bearing_elevation(*p_emit)
            predicted = Geometry(enu_m=p_emit, slant_range_m=rng2, azimuth_deg=az2,
                                 elevation_deg=el2, acoustic_delay_s=rng2 / c)
            t_arrival_rel = t_emit + predicted.acoustic_delay_s
            dt = t_rel - t_arrival_rel
            if abs(dt) > time_window_s:
                continue
            bearing_err = None
            if obs.azimuth_deg is not None:
                bearing_err = _angle_diff_deg(obs.azimuth_deg, predicted.azimuth_deg)
                if bearing_window_deg is not None and bearing_err > bearing_window_deg:
                    continue
            score = abs(dt) / time_window_s
            if bearing_err is not None and bearing_window_deg is not None:
                score += bearing_err / bearing_window_deg
            out.append(Correlation(observation=obs, state_vector=cand, predicted=predicted,
                                   dt_s=dt, bearing_error_deg=bearing_err, score=score))
    out.sort(key=lambda c: c.score)
    return out


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
