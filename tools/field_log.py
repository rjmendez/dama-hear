#!/usr/bin/env python3
"""Field log: write the ground truth down while it is still true, then turn it into a survey.

    python3 tools/field_log.py place --log field.jsonl --name gauss --node-id 3 \
        --ref nyquist --range 24.30 --bearing 118.5 --bearing-ref magnetic \
        --declination -10.9 --height 0.90 --sigma-m 0.15 \
        --u-source 'tape, nyquist sill to gauss mic' --sigma-u 0.05
    python3 tools/field_log.py temp  --log field.jsonl --temp-c 21.4 \
        --instrument 'kestrel 3000' --at nyquist --at-time 2026-09-12T19:04:07Z
    python3 tools/field_log.py mark  --log field.jsonl --at-time 2026-09-12T19:04:07Z \
        --class shot --lat 10.29358 --lon 20.87902 --note '.22 from the treeline'
    python3 tools/field_log.py emit-survey --log field.jsonl --base survey.json \
        --out survey.new.json

WHY THIS EXISTS. A receiver placed in the field and not written down cannot enter a survey at all:
hear/backend/survey.py:_read_node raises "node N has no u_m: a missing coordinate is never 0.0",
so an unrecorded height is not a degraded node, it is no node. The extra receivers are the whole
point of a session -- two receivers leave the position problem rank-deficient -- and their geometry
is the one thing no later reprocessing can recover. Audio is recoverable; a tape measure that was
never read is not.

THREE RECORD TYPES, one append-only JSONL, no schema migration and no database.

  placement  where a receiver was put, how its height was obtained, and what both are worth
  temp       air temperature with the instrument named -- c = 331.3 + 0.606*T, so 1 degC is
             0.606 m/s is 0.18% of range, and the BMP280 series on the nodes has never been
             checked against a thermometer
  mark       a wall-clock SECOND and a class, for the events that have no labels anywhere
             (fireworks, thunder, vehicles, birds, insects, people: zero labelled examples)

⚠️MARKS ARE SECONDS BY DESIGN. The audio carries the onset to the sample; the mark only has to say
which second to look in and what it was. A phone's wall clock is sufficient for that, which is why
this needs no firmware, no network and no clock work in the field. A sub-second input is therefore
REFUSED rather than rounded, unless --truncate-subsecond says out loud to drop it: a mark that
silently became 19:04:07 from 19:04:07.9 would read later as a precision it never had.

⚠️A GNSS/RTK ELLIPSOID HEIGHT IS NOT A HEIGHT IN THIS FRAME. survey.json's origin says h_ell_m 0.0,
a placeholder, so Survey.enu_of() on a real fix returns u equal to the site's whole ellipsoid
height -- a receiver hundreds of metres in the air, at a residual nothing downstream can see.
`--h-ell` is therefore REFUSED while the origin height is that placeholder, and the refusal names
both numbers. Give heights with --height instead: metres above the reference node, from a tape.
(For scale in the other direction, the origin height's effect on the HORIZONTAL is the frame-scale
term h/R: a few millimetres at 140 m for an origin a few hundred metres up.)

⚠️A FICTIONAL ORIGIN PLACES NOTHING. The public repo's survey.json marks its origin fictional (the
real one comes from HEAR_SITE_ORIGIN at runtime), so a lat/lon placement converted through it lands
in the wrong place with no error. It is refused unless HEAR_SITE_ORIGIN supplies the real origin.

⚠️THE LOG IS APPEND-ONLY AND IS NEVER REWRITTEN. Records are only ever appended and fsynced; a
correction is a new placement for the same name, and emit-survey uses the last one and says how
many it superseded. Nothing edits or deletes a line, because the value of a field log is that it
is the thing that was written at the time.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import geodesy as GEO                                    # noqa: E402
from hear.backend import survey as SV                              # noqa: E402
from hear.solve.shockwave import sound_speed                       # noqa: E402

RECORD_VERSION = 1

# Closed vocabulary. Free text here would give one class per operator per session and nothing to
# train on; "other" plus --note carries anything that does not fit without growing the list in
# the field, where a new class cannot be reconciled with the ones already written.
MARK_CLASSES: Tuple[str, ...] = ("shot", "firework", "vehicle", "chirp", "thunder", "other")

# An origin ellipsoid height this close to zero is the literal placeholder tools/node_survey.py
# writes (node_survey.py:275 emits "h_ell_m": 0.0 because its heights come from --heights, not
# from GNSS). It is not a plausible measurement here either: h_ell 0.0 would put the array
# hundreds of metres underground. Treated as "unknown", never as "sea level".
ORIGIN_H_ELL_PLACEHOLDER_TOL_M: float = 1e-9


class FieldLogError(ValueError):
    """Every refusal in this module. ValueError subclass, matching hear.backend.survey."""


# ---------------------------------------------------------------------------- time

def parse_utc_second(text, truncate_subsecond: bool = False) -> Tuple[int, Optional[str]]:
    """A wall-clock UTC SECOND from an ISO-8601 stamp or an integer epoch second.

    Returns (epoch_second, truncated_from) where truncated_from is None unless a sub-second input
    was explicitly dropped. Raises on a sub-second input when `truncate_subsecond` is False --
    see the module docstring: a silent round is a precision claim the mark cannot support.
    """
    s = str(text).strip()
    if not s:
        raise FieldLogError("empty timestamp")
    frac_src = None
    bare = s.lstrip("+-")
    if bare.replace(".", "", 1).isdigit() and bare.count(".") <= 1:
        v = float(s)
        if v != math.floor(v):
            frac_src = s
        sec = int(math.floor(v))
    else:
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            dt = _dt.datetime.fromisoformat(iso)
        except ValueError:
            raise FieldLogError(
                "cannot read %r as a UTC second: give an ISO-8601 stamp such as "
                "2026-09-12T19:04:07Z, or an integer epoch second" % (text,))
        if dt.tzinfo is None:
            raise FieldLogError(
                "timestamp %r has no timezone. A naive stamp is the field's own clock and there "
                "is no record of which one; write it as UTC with a trailing Z." % (text,))
        dt = dt.astimezone(_dt.timezone.utc)
        if dt.microsecond:
            frac_src = s
        sec = int(math.floor(dt.timestamp()))
    if frac_src is not None and not truncate_subsecond:
        raise FieldLogError(
            "timestamp %r carries sub-second precision. A mark is a SECOND -- the audio carries "
            "the onset -- so this is refused rather than rounded. Pass --truncate-subsecond to "
            "drop it on the record, which stores what was truncated." % (text,))
    return sec, frac_src


def now_utc_second() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


# ---------------------------------------------------------------------------- the log

def append_record(path: str, rec: Dict) -> Dict:
    """Append one record as one JSON line and fsync it.

    The file is opened in append mode only: existing bytes are never truncated, rewritten or
    reordered."""
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    if "\n" in line:
        raise FieldLogError("record serialises to more than one line: %r" % (rec,))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def read_log(path: str) -> List[Dict]:
    """Every record, in file order. A line that does not parse RAISES with its line number.

    It does not skip-and-continue: a field log is small, hand-made and irreplaceable, and the
    repo's recurring bug is a count that does not total its own losses. A dropped line here would
    be a receiver or an event that quietly stopped existing.
    """
    out: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise FieldLogError("%s line %d is not JSON: %s" % (path, i, e))
            if not isinstance(rec, dict) or "type" not in rec:
                raise FieldLogError("%s line %d is not a field-log record: %r" % (path, i, rec))
            out.append(rec)
    return out


# ---------------------------------------------------------------------------- records

def check_lat_lon(lat_deg, lon_deg, what: str) -> None:
    """Refuse a coordinate that cannot be a place: non-numeric, non-finite, out of range, or the
    0,0 a failed GPS read produces. Bad field input is refused when it is recorded, not later."""
    for label, v, lim in (("--lat", lat_deg, 90.0), ("--lon", lon_deg, 180.0)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise FieldLogError("%s %s %r is not a finite number" % (what, label, v))
        if abs(float(v)) > lim:
            raise FieldLogError("%s %s %r is outside +/-%g degrees" % (what, label, v, lim))
    if float(lat_deg) == 0.0 and float(lon_deg) == 0.0:
        raise FieldLogError("%s --lat/--lon 0,0 is what a failed GPS read gives, not a place"
                            % (what,))


def placement_record(name: str, node_id: int, height_m: Optional[float],
                     sigma_m: float,
                     ref: Optional[str] = None,
                     range_m: Optional[float] = None,
                     bearing_deg: Optional[float] = None,
                     bearing_ref: str = "true",
                     declination_deg: Optional[float] = None,
                     range_kind: str = "horizontal",
                     lat_deg: Optional[float] = None, lon_deg: Optional[float] = None,
                     h_ell_m: Optional[float] = None,
                     u_source: Optional[str] = None, sigma_u_m: Optional[float] = None,
                     note: str = "", t_utc_s: Optional[int] = None) -> Dict:
    """One receiver, where it was put and what that is worth. Geometry is NOT resolved here.

    Two ways to say where: a tape-and-compass `range`/`bearing` from a named reference node, or a
    `lat`/`lon`. Both leave the HEIGHT to `height_m`, metres above the reference node -- see the
    module docstring for why a GNSS height is not one.
    """
    if not name:
        raise FieldLogError("placement needs a --name")
    if not isinstance(node_id, int) or isinstance(node_id, bool):
        raise FieldLogError("placement needs an integer --node-id (the id the firmware sends); "
                            "guessing one attaches this position to the wrong receiver")
    has_polar = range_m is not None or bearing_deg is not None
    has_geod = lat_deg is not None or lon_deg is not None
    if has_polar and has_geod:
        raise FieldLogError("give --range/--bearing or --lat/--lon, not both")
    if has_polar:
        if range_m is None or bearing_deg is None:
            raise FieldLogError("--range needs --bearing and vice versa")
        if not ref:
            raise FieldLogError("--range/--bearing are measured FROM somewhere: give --ref")
        if not math.isfinite(range_m) or not (range_m > 0.0):
            raise FieldLogError("--range %r: need a finite positive distance" % (range_m,))
        if bearing_ref not in ("true", "magnetic"):
            raise FieldLogError("--bearing-ref must be 'true' or 'magnetic'")
        if bearing_ref == "magnetic" and declination_deg is None:
            # A compass reads magnetic. Southern Pennsylvania is about -11 deg, which at the
            # measured 16.6 m nyquist-mach baseline is 3.2 m of cross-range -- larger than every
            # other placement term. Assuming a declination would bury that; refusing surfaces it.
            raise FieldLogError(
                "--bearing-ref magnetic needs --declination: a compass bearing is not a true "
                "bearing, and the difference here is about 11 deg, which is 3.2 m across a 16.6 m "
                "baseline. There is no default, because a wrong default is invisible.")
        if range_kind not in ("horizontal", "slope"):
            raise FieldLogError("--range-kind must be 'horizontal' or 'slope'")
    elif has_geod:
        if lat_deg is None or lon_deg is None:
            raise FieldLogError("--lat needs --lon and vice versa")
        check_lat_lon(lat_deg, lon_deg, "placement")
    else:
        raise FieldLogError("placement needs --range/--bearing or --lat/--lon")
    if sigma_m is None or not math.isfinite(float(sigma_m)) or float(sigma_m) < 0.0:
        raise FieldLogError("placement needs a finite non-negative --sigma-m (horizontal). It is "
                            "not optional: an unstated horizontal sigma becomes 0.0 in the survey "
                            "and reads as a perfectly known position.")
    if height_m is not None and not math.isfinite(float(height_m)):
        raise FieldLogError("--height %r is not finite" % (height_m,))
    if sigma_u_m is not None and (not math.isfinite(float(sigma_u_m)) or float(sigma_u_m) < 0.0):
        raise FieldLogError("--sigma-u %r: need a finite non-negative number" % (sigma_u_m,))
    # Absent stays absent, all the way down: survey.to_dict() omits an absent u_source and
    # height_provenance() counts it as unmeasured. Writing 0.0 here would be the same lie one
    # layer earlier, where nothing downstream could ever catch it.
    return {
        "type": "placement", "v": RECORD_VERSION,
        "t_utc_s": int(t_utc_s if t_utc_s is not None else now_utc_second()),
        "name": str(name), "node_id": int(node_id),
        "ref": ref or None,
        "range_m": None if range_m is None else float(range_m),
        "bearing_deg": None if bearing_deg is None else float(bearing_deg),
        "bearing_ref": bearing_ref if has_polar else None,
        "declination_deg": None if declination_deg is None else float(declination_deg),
        "range_kind": range_kind if has_polar else None,
        "lat_deg": None if lat_deg is None else float(lat_deg),
        "lon_deg": None if lon_deg is None else float(lon_deg),
        "h_ell_m": None if h_ell_m is None else float(h_ell_m),
        "height_m": None if height_m is None else float(height_m),
        "u_source": None if u_source is None else str(u_source),
        "sigma_u_m": None if sigma_u_m is None else float(sigma_u_m),
        "sigma_m": float(sigma_m),
        "note": str(note or ""),
    }


def temp_record(temp_c: float, instrument: str, at_node: str,
                t_utc_s: Optional[int] = None, truncated_from: Optional[str] = None,
                note: str = "") -> Dict:
    """Air temperature, with the instrument named because that is what makes it a calibration.

    The nodes carry a BMP280 that has never been read against a thermometer, and c = 331.3 +
    0.606*T means 1 degC is 0.18% of every range. An unattributed number cannot calibrate anything.
    """
    if not math.isfinite(float(temp_c)):
        raise FieldLogError("--temp-c %r is not finite" % (temp_c,))
    if not (-90.0 <= float(temp_c) <= 60.0):
        raise FieldLogError("--temp-c %r is outside -90..60 degC: this is air temperature in "
                            "degrees Celsius, and a Fahrenheit reading here is silent" % (temp_c,))
    if not instrument:
        raise FieldLogError("--instrument is required: 'a thermometer' cannot calibrate a BMP280")
    if not at_node:
        raise FieldLogError("--at is required: temperature is a place, not a session")
    rec = {"type": "temp", "v": RECORD_VERSION,
           "t_utc_s": int(t_utc_s if t_utc_s is not None else now_utc_second()),
           "temp_c": float(temp_c), "instrument": str(instrument), "at_node": str(at_node),
           "c_mps": float(sound_speed(float(temp_c))), "note": str(note or "")}
    if truncated_from is not None:
        rec["t_truncated_from"] = str(truncated_from)
    return rec


def mark_record(t_utc_s: int, cls: str, lat_deg: Optional[float] = None,
                lon_deg: Optional[float] = None, note: str = "",
                truncated_from: Optional[str] = None) -> Dict:
    """A wall-clock second and what happened in it. Second resolution is deliberate."""
    if cls not in MARK_CLASSES:
        raise FieldLogError("class %r is not one of %s. 'other' plus --note carries anything "
                            "else; a new class invented in the field cannot be reconciled with "
                            "the ones already written." % (cls, ", ".join(MARK_CLASSES)))
    if not isinstance(t_utc_s, int) or isinstance(t_utc_s, bool):
        raise FieldLogError("mark time must be an integer UTC second, got %r" % (t_utc_s,))
    if (lat_deg is None) != (lon_deg is None):
        raise FieldLogError("--lat needs --lon and vice versa")
    if lat_deg is not None:
        check_lat_lon(lat_deg, lon_deg, "mark")
    rec = {"type": "mark", "v": RECORD_VERSION, "t_utc_s": int(t_utc_s), "class": cls,
           "lat_deg": None if lat_deg is None else float(lat_deg),
           "lon_deg": None if lon_deg is None else float(lon_deg),
           "note": str(note or "")}
    if truncated_from is not None:
        rec["t_truncated_from"] = str(truncated_from)
    return rec


# ---------------------------------------------------------------------------- survey emit

def origin_h_ell_is_placeholder(origin: Optional[Dict]) -> bool:
    """True when the frame origin's ellipsoid height is the 0.0 placeholder, or absent."""
    if not isinstance(origin, dict):
        return True
    h = origin.get("h_ell_m")
    if h is None or isinstance(h, bool) or not isinstance(h, (int, float)):
        return True
    return abs(float(h)) <= ORIGIN_H_ELL_PLACEHOLDER_TOL_M


def origin_is_fictional(origin: Optional[Dict]) -> bool:
    """hear.backend.survey's rule: a `fictional` key with any value but an explicit false counts."""
    return isinstance(origin, dict) and "fictional" in origin and origin["fictional"] is not False


def latest_placements(records: Sequence[Dict]) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Last placement per receiver name, and how many earlier ones it superseded.

    A correction in the field is a new record, not an edit. The count is returned rather than
    discarded so emit-survey can say what it did not use.
    """
    latest: Dict[str, Dict] = {}
    superseded: Dict[str, int] = {}
    for r in records:
        if r.get("type") != "placement":
            continue
        n = str(r.get("name", ""))
        if n in latest:
            superseded[n] = superseded.get(n, 0) + 1
        latest[n] = r
    return latest, superseded


def _enu_of_placement(p: Dict, base: Optional[SV.Survey], origin: Optional[Dict],
                      by_name: Dict[str, int]) -> Tuple[float, float, float]:
    """(e, n, u) in the survey frame for one placement record, or raise saying why not."""
    name = p.get("name")
    h = p.get("height_m")
    if h is None:
        # The same rule as hear/backend/survey.py:_read_node, one layer earlier and with a name
        # attached: the loader would refuse this node anyway, but only after the session is over.
        raise FieldLogError(
            "receiver %r has no recorded height: a missing coordinate is never 0.0. "
            "hear/backend/survey.py refuses a node with no u_m, so this receiver cannot enter a "
            "survey at all -- put a tape on it and log another placement." % (name,))
    h = float(h)

    if p.get("h_ell_m") is not None and origin_h_ell_is_placeholder(origin):
        oh = origin.get("h_ell_m") if isinstance(origin, dict) else None
        raise FieldLogError(
            "receiver %r gives h_ell_m %.3f m but the survey origin's h_ell_m is %r, a "
            "placeholder. Converting an ellipsoid height through that origin puts this receiver "
            "%.1f m in the air with a perfect-looking residual: Survey.enu_of() on a real fix "
            "returns the site's whole ellipsoid height as u against an h_ell_m of 0.0. Use "
            "--height (metres above the reference node, from a tape), or state the true origin "
            "height with --origin-h-ell."
            % (name, float(p["h_ell_m"]), oh,
               float(p["h_ell_m"]) - (float(oh) if isinstance(oh, (int, float)) else 0.0)))

    ref_name = p.get("ref")
    ref_e = ref_n = ref_u = 0.0
    if ref_name:
        if base is None or ref_name not in by_name:
            raise FieldLogError(
                "receiver %r is placed from reference %r, which is not in the base survey. A "
                "height above an unknown node is not a height." % (name, ref_name))
        rp = base.position(by_name[ref_name])
        ref_e, ref_n, ref_u = float(rp[0]), float(rp[1]), float(rp[2])
    elif p.get("lat_deg") is None:
        raise FieldLogError("receiver %r has neither a reference node nor a lat/lon" % (name,))

    if p.get("range_m") is not None:
        r = float(p["range_m"])
        if p.get("range_kind") == "slope":
            if abs(h) >= r:
                raise FieldLogError(
                    "receiver %r: slope range %.3f m is not longer than the %.3f m height "
                    "difference; one of the two is wrong." % (name, r, abs(h)))
            r = math.sqrt(r * r - h * h)
        b = float(p["bearing_deg"])
        if p.get("bearing_ref") == "magnetic":
            b += float(p["declination_deg"])
        rad = math.radians(b)
        e, n, u = ref_e + r * math.sin(rad), ref_n + r * math.cos(rad), ref_u + h
    else:
        if origin is None:
            raise FieldLogError(
                "receiver %r is given as lat/lon but the survey has no origin to convert it "
                "into. Give a --base survey with an origin, or place it with "
                "--ref/--range/--bearing." % (name,))
        if origin_is_fictional(origin):
            raise FieldLogError(
                "receiver %r is given as lat/lon but the survey origin is marked fictional (the "
                "public repo does not carry the site), so the conversion would misplace it with "
                "no error. Set %s='lat,lon,h_ell_m' to the real origin, or place it with "
                "--ref/--range/--bearing." % (name, SV.SITE_ORIGIN_ENV))
        o = (float(origin["lat_deg"]), float(origin["lon_deg"]),
             float(origin.get("h_ell_m", 0.0)))
        # The origin's own height is passed in deliberately, so the returned u is zero by
        # construction and the height comes from the tape. The frame-scale term this leaves in the
        # HORIZONTAL is h/R: a few millimetres at 140 m, against the whole origin height in the
        # vertical.
        en = GEO.geodetic_to_enu(float(p["lat_deg"]), float(p["lon_deg"]), o[2], *o)
        e, n, u = float(en[0]), float(en[1]), ref_u + h
    check_height_agreement(p, u, origin)
    return (e, n, u)


def check_height_agreement(p: Dict, u_m: float, origin: Optional[Dict]) -> Optional[float]:
    """Two stated heights for one receiver must agree, or neither is used.

    ⚠️A SECOND HEIGHT MUST NOT BE SILENTLY DISCARDED. A record can carry both a tape
    `height_m` and a GNSS `h_ell_m`; picking one and dropping the other is the repo's recurring
    bug -- the survey would look complete and the disagreement would exist only in the log. The
    two are compared against `survey.HEIGHT_SIGMA_TOL_M`, the vertical tolerance the array's own
    geometry sets (0.25 m keeps the height term near 15 mm on the measured 16.6 m pair), and a
    wider gap is refused with both numbers named. Returns the geodetic height difference, or None
    when there was only one height to begin with.
    """
    if p.get("h_ell_m") is None or origin_h_ell_is_placeholder(origin):
        return None
    u_geo = float(p["h_ell_m"]) - float(origin["h_ell_m"])
    if abs(u_geo - float(u_m)) > SV.HEIGHT_SIGMA_TOL_M:
        raise FieldLogError(
            "receiver %r gives two heights that disagree by %.3f m: the tape puts it at u = "
            "%.3f m, and h_ell_m %.3f m against origin h_ell_m %.3f m puts it at u = %.3f m. "
            "That is wider than the %.2f m the array's geometry tolerates, so neither is used -- "
            "log another placement with the one that is right."
            % (p.get("name"), abs(u_geo - float(u_m)), float(u_m), float(p["h_ell_m"]),
               float(origin["h_ell_m"]), u_geo, SV.HEIGHT_SIGMA_TOL_M))
    return u_geo


def build_survey(records: Sequence[Dict], base_path: Optional[str] = None,
                 origin_h_ell_m: Optional[float] = None,
                 min_nodes: int = 3) -> Tuple[SV.Survey, Dict]:
    """A validated Survey from the log's placements, plus a report of what it did.

    With a `--base`, the base survey's nodes and origin are carried through unchanged (including
    their u_source/sigma_u_m -- to_dict() now writes them, so a round trip no longer launders a
    nominal storey into a measurement) and the log's receivers are added to them.

    With no base and every placement geodetic, the whole survey is built through
    `survey.from_wgs84_nodes`, which needs a real origin ellipsoid height: `--origin-h-ell`.
    """
    latest, superseded = latest_placements(records)
    if not latest:
        raise FieldLogError("no placement records in the log: nothing to survey")

    base: Optional[SV.Survey] = None
    origin: Optional[Dict] = None
    if base_path:
        base = SV.load_survey(base_path, min_nodes=1)
        origin = dict(base.origin) if isinstance(base.origin, dict) else None
    if origin_h_ell_m is not None and origin is not None:
        origin["h_ell_m"] = float(origin_h_ell_m)
        origin["source"] = "%s; h_ell_m restated by tools/field_log.py --origin-h-ell" % (
            origin.get("source", "unstated"),)

    by_name: Dict[str, int] = {}
    if base is not None:
        for i in base.ids:
            if base.names[i]:
                by_name[base.names[i]] = i

    all_geodetic = all(p.get("lat_deg") is not None and p.get("h_ell_m") is not None
                       for p in latest.values())
    if base is None and all_geodetic:
        if origin_h_ell_m is None:
            raise FieldLogError(
                "a survey built from lat/lon/h_ell needs a real origin ellipsoid height. Give "
                "--origin-h-ell; there is no default, because 0.0 is the placeholder "
                "node_survey.py writes and it would put every node hundreds of metres out.")
        ents = []
        for nm in sorted(latest):
            p = latest[nm]
            # A tape height logged alongside the ellipsoid height is a cross-check on the number
            # this path is about to use, not a spare value to drop.
            if p.get("height_m") is not None:
                check_height_agreement(p, float(p["h_ell_m"]) - float(origin_h_ell_m),
                                       {"h_ell_m": float(origin_h_ell_m)})
            e = {"node_id": int(p["node_id"]), "name": nm,
                 "lat_deg": float(p["lat_deg"]), "lon_deg": float(p["lon_deg"]),
                 "h_ell_m": float(p["h_ell_m"]), "sigma_m": float(p["sigma_m"])}
            if p.get("u_source") is not None:
                e["u_source"] = p["u_source"]
            if p.get("sigma_u_m") is not None:
                e["sigma_u_m"] = float(p["sigma_u_m"])
            ents.append(e)
        org = {"lat_deg": ents[0]["lat_deg"], "lon_deg": ents[0]["lon_deg"],
               "h_ell_m": float(origin_h_ell_m),
               "source": "tools/field_log.py --origin-h-ell, anchored on %s" % ents[0]["name"]}
        sv = SV.from_wgs84_nodes(ents, origin=org, min_nodes=min_nodes)
        return sv, {"mode": "wgs84", "added": sorted(latest), "carried": [],
                    "superseded": superseded, "provenance": sv.height_provenance()}

    nodes: List[Dict] = []
    if base is not None:
        nodes.extend(base.to_dict()["nodes"])
    carried = [n.get("name", "") for n in nodes]
    taken_ids = {int(n["node_id"]): n.get("name", "") for n in nodes}
    taken_names = {n.get("name") for n in nodes if n.get("name")}
    for name in sorted(latest):
        p = latest[name]
        nid = int(p["node_id"])
        if nid in taken_ids:
            raise FieldLogError(
                "receiver %r uses node_id %d, which the base survey already gives to %r. Two "
                "receivers with one id are one receiver as far as every solver is concerned."
                % (name, nid, taken_ids[nid]))
        if name in taken_names:
            raise FieldLogError("receiver %r is already in the base survey; a re-survey of an "
                                "existing node belongs in tools/node_survey.py" % (name,))
        e, n, u = _enu_of_placement(p, base, origin, by_name)
        ent = {"node_id": nid, "name": name, "e_m": e, "n_m": n, "u_m": u,
               "sigma_m": float(p["sigma_m"])}
        if p.get("u_source") is not None:
            ent["u_source"] = p["u_source"]
        if p.get("sigma_u_m") is not None:
            ent["sigma_u_m"] = float(p["sigma_u_m"])
        nodes.append(ent)
        taken_ids[nid] = name
        taken_names.add(name)

    doc: Dict = {"frame": SV._FRAME, "units": SV._UNITS, "nodes": nodes}
    if origin is not None:
        doc["origin"] = origin
    sv = SV.from_dict(doc, min_nodes=min_nodes)
    return sv, {"mode": "enu", "added": sorted(latest), "carried": sorted(carried),
                "superseded": superseded, "provenance": sv.height_provenance()}


# ---------------------------------------------------------------------------- CLI

def _sub_place(a) -> int:
    rec = placement_record(
        name=a.name, node_id=a.node_id, height_m=a.height, sigma_m=a.sigma_m,
        ref=a.ref, range_m=a.range, bearing_deg=a.bearing, bearing_ref=a.bearing_ref,
        declination_deg=a.declination, range_kind=a.range_kind,
        lat_deg=a.lat, lon_deg=a.lon, h_ell_m=a.h_ell,
        u_source=a.u_source, sigma_u_m=a.sigma_u, note=a.note)
    append_record(a.log, rec)
    print("placement %s (node_id %d) appended to %s" % (rec["name"], rec["node_id"], a.log))
    if rec["height_m"] is None:
        print("  ^ NO HEIGHT. emit-survey will refuse this receiver rather than default 0.0, so "
              "it cannot enter a survey until a tape height is logged.")
    if rec["u_source"] is None or rec["sigma_u_m"] is None:
        print("  ^ no --u-source and/or no --sigma-u: height_provenance() will report this "
              "receiver as unmeasured, which is correct until someone puts a tape on it.")
    return 0


def _sub_temp(a) -> int:
    if a.at_time:
        sec, trunc = parse_utc_second(a.at_time, a.truncate_subsecond)
    else:
        sec, trunc = now_utc_second(), None
    rec = temp_record(a.temp_c, a.instrument, a.at, t_utc_s=sec, truncated_from=trunc,
                      note=a.note)
    append_record(a.log, rec)
    print("temp %.2f degC at %s -> c = %.2f m/s, appended to %s"
          % (rec["temp_c"], rec["at_node"], rec["c_mps"], a.log))
    return 0


def _sub_mark(a) -> int:
    sec, trunc = parse_utc_second(a.at_time, a.truncate_subsecond)
    rec = mark_record(sec, getattr(a, "class"), lat_deg=a.lat, lon_deg=a.lon, note=a.note,
                      truncated_from=trunc)
    append_record(a.log, rec)
    print("mark %s at %d (%sZ) appended to %s"
          % (rec["class"], rec["t_utc_s"],
             _dt.datetime.fromtimestamp(rec["t_utc_s"], _dt.timezone.utc).isoformat()[:19], a.log))
    if trunc is not None:
        print("  ^ sub-second input %r was TRUNCATED to the second and the record says so."
              % (trunc,))
    return 0


def _sub_show(a) -> int:
    recs = read_log(a.log)
    counts: Dict[str, int] = {}
    for r in recs:
        counts[r.get("type", "?")] = counts.get(r.get("type", "?"), 0) + 1
    print("%s: %d records -- %s"
          % (a.log, len(recs),
             ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "empty"))
    latest, superseded = latest_placements(recs)
    for n in sorted(latest):
        p = latest[n]
        print("  placement %-12s node_id %-4d height %s  u_source %r  sigma_u %s  sigma_m %.3f%s"
              % (n, p["node_id"],
                 "MISSING" if p.get("height_m") is None else "%.3f m" % p["height_m"],
                 p.get("u_source"),
                 "unstated" if p.get("sigma_u_m") is None else "%.3f m" % p["sigma_u_m"],
                 float(p.get("sigma_m", 0.0)),
                 "" if not superseded.get(n) else "  (%d superseded)" % superseded[n]))
    for r in recs:
        if r.get("type") == "mark":
            print("  mark %-9s %d  %s" % (r["class"], r["t_utc_s"], r.get("note", "")))
    return 0


def _sub_emit(a) -> int:
    recs = read_log(a.log)
    sv, rep = build_survey(recs, base_path=a.base, origin_h_ell_m=a.origin_h_ell,
                           min_nodes=a.min_nodes)
    if os.path.exists(a.out) and not a.force:
        print("refusing to overwrite %s without --force" % a.out, file=sys.stderr)
        return 3
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(sv.to_dict(), fh, indent=2)
        fh.write("\n")
    print("wrote %s -- %d nodes (%s mode); carried %s, added %s"
          % (a.out, len(sv), rep["mode"], ", ".join(rep["carried"]) or "nothing",
             ", ".join(rep["added"])))
    for n, k in sorted(rep["superseded"].items()):
        print("  %s: used the last of %d placements, %d superseded" % (n, k + 1, k))
    pr = rep["provenance"]
    print("  height provenance: %s"
          % (pr["note"] or "every height stated and within +/-%.2f m" % pr["tol_m"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Append-only field log for placements, temperatures and event marks.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("place", help="record where a receiver was put")
    p.add_argument("--log", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--node-id", type=int, required=True,
                   help="the id this receiver's firmware sends; never guessed")
    p.add_argument("--ref", help="reference node the range/bearing and height are measured from")
    p.add_argument("--range", type=float, help="metres from --ref")
    p.add_argument("--bearing", type=float, help="degrees clockwise from north")
    p.add_argument("--bearing-ref", choices=("true", "magnetic"), default="true")
    p.add_argument("--declination", type=float,
                   help="magnetic declination in degrees, east positive; required with "
                        "--bearing-ref magnetic")
    p.add_argument("--range-kind", choices=("horizontal", "slope"), default="horizontal")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--h-ell", type=float,
                   help="GNSS/RTK ellipsoid height. REFUSED at emit while the survey origin's "
                        "h_ell_m is the 0.0 placeholder")
    p.add_argument("--height", type=float,
                   help="metres above --ref, from a tape. Omitting it is recorded, and "
                        "emit-survey then refuses this receiver rather than defaulting 0.0")
    p.add_argument("--u-source", help="how the height was obtained, in words")
    p.add_argument("--sigma-u", type=float, help="vertical 1-sigma, metres")
    p.add_argument("--sigma-m", type=float, required=True, help="horizontal 1-sigma, metres")
    p.add_argument("--note", default="")
    p.set_defaults(fn=_sub_place)

    p = sub.add_parser("temp", help="record an air temperature with its instrument")
    p.add_argument("--log", required=True)
    p.add_argument("--temp-c", type=float, required=True)
    p.add_argument("--instrument", required=True)
    p.add_argument("--at", required=True, help="node the thermometer was held at")
    p.add_argument("--at-time", help="UTC second; defaults to now")
    p.add_argument("--truncate-subsecond", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(fn=_sub_temp)

    p = sub.add_parser("mark", help="record a wall-clock SECOND and what happened in it")
    p.add_argument("--log", required=True)
    p.add_argument("--at-time", required=True, help="UTC second, e.g. 2026-09-12T19:04:07Z")
    p.add_argument("--class", required=True, choices=MARK_CLASSES, dest="class")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--truncate-subsecond", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(fn=_sub_mark)

    p = sub.add_parser("show", help="what the log holds")
    p.add_argument("--log", required=True)
    p.set_defaults(fn=_sub_show)

    p = sub.add_parser("emit-survey", help="turn the placements into a validated survey.json")
    p.add_argument("--log", required=True)
    p.add_argument("--base", help="existing survey.json whose nodes and origin are carried")
    p.add_argument("--out", required=True)
    p.add_argument("--origin-h-ell", type=float,
                   help="the frame origin's TRUE ellipsoid height, metres")
    p.add_argument("--min-nodes", type=int, default=3)
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    p.set_defaults(fn=_sub_emit)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (FieldLogError, SV.SurveyError) as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
