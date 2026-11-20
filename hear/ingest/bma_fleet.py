"""Conservative, offline BMA fleet v1 ray conversion.

The site origin follows firmware geo-feed's strict WGS84/true-north contract.
Only complete registry-surveyed poses and unambiguous azel rays are accepted.
Pose source and survey text must be nonempty and UTF-8 encodable; accepted
text is preserved verbatim.
No relative pose, range, geographic coordinate, precise time or validation is
inferred. ``source_time_wall_s`` is the hub wall time, for display only.

Returns ``{"observations": [...], "refusals": [...]}``. Observations contain
``unit_id``, ``det_id``, ``source_time_wall_s``, ``origin_id``, ``pose``
(registry origin_id, position, orientation, source and surveyed_utc),
``bearing_deg_true``, ``sigma_deg``, ``clock_precise`` and ``evidence``.
The bearing sigma is the ray's azimuth uncertainty, not combined with the
separately preserved pose uncertainties. Refusals contain ``reason``,
``ray_index``, ``unit_id`` and ``det_id``; null identifies unavailable data
or a feed-level failure. Ordering follows input order. Repeated unit or det
identities invalidate *all* their occurrences, regardless of input order.

If freshness is requested both ``now_s`` and ``max_age_s`` are required;
both the feed update and ray wall times must be within [now-max_age, now].
Freshness is only a display-time filter, not evidence of synchronized clocks.

The hub rounds ray angles to four decimal places; for compatibility this
reader allows independent rounding to 0.01 degrees. Bearing consistency is
bounded in direction space, so its angular tolerance widens near a vertical
ray. A ray whose rounding interval can reach vertical is refused as
``ambiguous_bearing``, rather than treating an undefined bearing as certain.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_ORIGIN_KEYS = {"origin_id", "lat", "lon", "alt_m", "datum", "north"}
_UNIT = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z", re.ASCII)
_ORIGIN = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z", re.ASCII)
_DET = re.compile(r"([a-z][a-z0-9_-]{0,23}):[0-9]+:[0-9]+\Z", re.ASCII)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _in(value, low, high, *, high_open=False, low_open=False):
    return (_number(value) and (value > low if low_open else value >= low)
            and (value < high if high_open else value <= high))


def _text(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _origin_valid(origin):
    return (isinstance(origin, dict) and set(origin) == _ORIGIN_KEYS
            and isinstance(origin["origin_id"], str)
            and _ORIGIN.fullmatch(origin["origin_id"]) is not None
            and _in(origin["lat"], -89, 89)
            and _in(origin["lon"], -180, 180)
            and _in(origin["alt_m"], -500, 10000)
            and origin["datum"] == "WGS84" and origin["north"] == "true")


def _pose(unit, origin_id):
    pose = unit.get("pose")
    if (unit.get("pose_state") != "surveyed" or not isinstance(pose, dict)
            or unit.get("not_fused_reason") == "no_pose"
            or (unit.get("fused") is True and unit.get("not_fused_reason") is not None)):
        return None
    pos, orient = pose.get("position"), pose.get("orientation")
    if (pose.get("origin_id") != origin_id or not isinstance(pos, dict)
            or not isinstance(orient, dict) or pose.get("valid", True) is not True
            or not _text(pose.get("source")) or not _text(pose.get("surveyed_utc"))):
        return None
    if not all(_number(pos.get(k)) for k in ("e_m", "n_m", "u_m")):
        return None
    if not _in(pos.get("sigma_m"), 0, float("inf"), low_open=True):
        return None
    if not (_in(orient.get("heading_deg"), 0, 360, high_open=True)
            and _in(orient.get("pitch_deg"), -90, 90)
            and _in(orient.get("roll_deg"), -180, 180, low_open=True)
            and orient.get("port_normal") in ("+z", "-z")
            and _in(orient.get("sigma_heading_deg"), 0, float("inf"), low_open=True)):
        return None
    return {
        "origin_id": origin_id,
        "position": {k: pos[k] for k in ("e_m", "n_m", "u_m", "sigma_m")},
        "orientation": {k: orient[k] for k in (
            "heading_deg", "pitch_deg", "roll_deg", "port_normal", "sigma_heading_deg")},
        "source": pose["source"], "surveyed_utc": pose["surveyed_utc"],
    }


def _bearing_status(ray, orient):
    """Return a refusal or None, allowing independent 0.01-degree rounding."""
    az, el = math.radians(ray["az_deg_local"]), math.radians(ray["el_deg_local"])
    x, y, z = math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)
    if orient["port_normal"] == "-z":
        y, z = -y, -z
    roll = math.radians(orient["roll_deg"])
    y, z = math.cos(roll) * y - math.sin(roll) * z, math.sin(roll) * y + math.cos(roll) * z
    pitch = math.radians(-orient["pitch_deg"])
    x, z = math.cos(pitch) * x + math.sin(pitch) * z, -math.sin(pitch) * x + math.cos(pitch) * z
    yaw = math.radians(90 - orient["heading_deg"])
    east, north = math.cos(yaw) * x - math.sin(yaw) * y, math.sin(yaw) * x + math.cos(yaw) * y
    horizontal = math.hypot(east, north)
    # Two local components each vary by <= .005 degrees. Rotation preserves
    # angular distance; their summed error bounds the cap around the direction.
    direction_error = math.radians(0.01)
    if horizontal <= math.sin(direction_error):
        return "ambiguous_bearing"
    expected = math.degrees(math.atan2(east, north)) % 360
    bearing_error = math.degrees(math.asin(math.sin(direction_error) / horizontal)) + 0.005
    difference = abs((ray["bearing_deg_world"] - expected + 180) % 360 - 180)
    return "contradictory_bearing" if difference > bearing_error + 1e-9 else None


def convert_fleet(fleet, origin, *, now_s=None, max_age_s=None):
    """Convert untrusted decoded JSON objects, refusing unsafe records."""
    result = {"observations": [], "refusals": []}

    def refuse(reason, index=None, ray=None):
        ray = ray if isinstance(ray, dict) else {}
        uid, det = ray.get("unit_id"), ray.get("det_id")
        result["refusals"].append({
            "reason": reason, "ray_index": index,
            "unit_id": uid if isinstance(uid, str) else None,
            "det_id": det if isinstance(det, str) else None,
        })

    if (now_s is None) != (max_age_s is None) or (
            now_s is not None and (not _in(now_s, 0, float("inf"))
                                   or not _in(max_age_s, 0, float("inf")))):
        refuse("invalid_freshness")
        return result
    if not _origin_valid(origin):
        refuse("invalid_origin")
        return result
    if (not isinstance(fleet, dict) or type(fleet.get("v")) is not int
            or fleet.get("v") != 1 or fleet.get("k") != "fleet"
            or not _in(fleet.get("updated_s"), 0, float("inf"))
            or not isinstance(fleet.get("units"), list)
            or not isinstance(fleet.get("rays"), list)):
        refuse("invalid_fleet")
        return result
    if fleet.get("origin_id") != origin["origin_id"]:
        refuse("origin_mismatch")
        return result
    if now_s is not None:
        age = now_s - fleet["updated_s"]
        if age < 0 or age > max_age_s:
            refuse("future_feed" if age < 0 else "stale_feed")
            return result

    units = fleet["units"]
    unit_counts = Counter(u["unit_id"] for u in units if isinstance(u, dict)
                          and isinstance(u.get("unit_id"), str))
    by_id = {u["unit_id"]: u for u in units if isinstance(u, dict)
             and isinstance(u.get("unit_id"), str) and unit_counts[u["unit_id"]] == 1}
    rays = fleet["rays"]
    det_counts = Counter(r["det_id"] for r in rays if isinstance(r, dict)
                         and isinstance(r.get("det_id"), str))
    for index, ray in enumerate(rays):
        if not isinstance(ray, dict):
            refuse("invalid_ray", index, ray)
            continue
        uid, det = ray.get("unit_id"), ray.get("det_id")
        match = _DET.fullmatch(det) if isinstance(det, str) else None
        if not isinstance(uid, str) or not _UNIT.fullmatch(uid) or not match or match[1] != uid:
            refuse("invalid_identity", index, ray)
        elif det_counts[det] != 1:
            refuse("duplicate_detection", index, ray)
        elif unit_counts[uid] > 1:
            refuse("duplicate_unit", index, ray)
        elif uid not in by_id:
            refuse("unknown_unit", index, ray)
        elif ray.get("kind") != "azel":
            refuse("unsupported_ray", index, ray)
        elif (ray.get("mirror_bearings_deg_world") is not None
              or ray.get("ambiguous", False) is not False
              or by_id[uid].get("ambiguous") is not False):
            refuse("ambiguous_ray", index, ray)
        elif (not _in(ray.get("t_wall_s"), 0, float("inf"))
              or not _in(ray.get("sigma_deg"), 0, float("inf"), low_open=True)
              or not _in(ray.get("bearing_deg_world"), 0, 360, high_open=True)
              or not _in(ray.get("az_deg_local"), 0, 360, high_open=True)
              or not _in(ray.get("el_deg_local"), -90, 90)):
            refuse("invalid_ray", index, ray)
        elif now_s is not None and (now_s - ray["t_wall_s"] < 0 or now_s - ray["t_wall_s"] > max_age_s):
            refuse("future_ray" if ray["t_wall_s"] > now_s else "stale_ray", index, ray)
        else:
            pose = _pose(by_id[uid], origin["origin_id"])
            if pose is None:
                refuse("invalid_pose", index, ray)
            else:
                reason = _bearing_status(ray, pose["orientation"])
                if reason:
                    refuse(reason, index, ray)
                else:
                    result["observations"].append({
                        "unit_id": uid, "det_id": det, "source_time_wall_s": ray["t_wall_s"],
                        "origin_id": origin["origin_id"], "pose": pose,
                        "bearing_deg_true": ray["bearing_deg_world"], "sigma_deg": ray["sigma_deg"],
                        "clock_precise": False, "evidence": "unvalidated",
                    })
    return result
