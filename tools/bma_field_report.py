#!/usr/bin/env python3
"""Replay converted BMA observations beside a local hear-drain heartbeat."""

import argparse
import ctypes
import json
import math
import os
import re
import sys


class InputError(ValueError):
    pass


def _object(value, where):
    if not isinstance(value, dict):
        raise InputError("%s must be an object" % where)
    return value


def _text(value, where):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InputError("%s must be a nonempty, trimmed string" % where)
    return value


def _number(value, where, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError("%s must be a finite number" % where)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise InputError("%s must be a finite number >= %s" % (where, minimum))
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _reject_constant(value):
    raise InputError("nonfinite JSON value: %s" % value)


def read_json(path):
    if path.startswith(("\\\\", "//")):
        raise InputError("network paths are not supported")
    resolved = os.path.realpath(path)
    if resolved.startswith(("\\\\", "//")):
        raise InputError("network paths are not supported")
    if os.name == "nt":
        drive = os.path.splitdrive(resolved)[0]
        if not drive or ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:
            raise InputError("input must be on a local fixed drive")
    try:
        if not os.path.isfile(path):
            raise InputError("not a local regular file: %s" % path)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _finite_tree(data)
        return data
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise InputError("cannot read JSON %s: %s" % (path, exc)) from exc


def _finite_tree(value):
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise InputError("nonfinite JSON value")


def _nullable_number(obj, key, where, minimum=0):
    value = obj.get(key)
    if value is not None:
        _number(value, "%s.%s" % (where, key), minimum)


def _age_state(timestamp, now, max_age):
    if timestamp is None:
        return {"state": "unknown", "age_s": None}
    age = now - timestamp
    return {"state": "future" if age < 0 else "stale" if age > max_age else "fresh",
            "age_s": age}


def _validate_observations(data):
    _object(data, "BMA conversion")
    for key in ("observations", "refusals"):
        if not isinstance(data.get(key), list):
            raise InputError("BMA conversion.%s must be an array" % key)
    origins = set()
    for i, observation in enumerate(data["observations"]):
        where = "observations[%d]" % i
        _object(observation, where)
        for key in ("unit_id", "det_id", "origin_id"):
            _text(observation.get(key), where + "." + key)
        unit = observation["unit_id"]
        if (re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", unit, re.ASCII) is None
                or re.fullmatch(re.escape(unit) + r":[0-9]+:[0-9]+", observation["det_id"],
                                re.ASCII) is None
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", observation["origin_id"],
                                re.ASCII) is None):
            raise InputError(where + " invalid identity")
        origin = observation["origin_id"]
        origins.add(origin)
        pose = _object(observation.get("pose"), where + ".pose")
        if pose.get("origin_id") != origin:
            raise InputError("%s pose origin mismatch" % where)
        position = _object(pose.get("position"), where + ".pose.position")
        orientation = _object(pose.get("orientation"), where + ".pose.orientation")
        for key in ("e_m", "n_m", "u_m", "sigma_m"):
            _number(position.get(key), where + ".pose.position." + key,
                    0 if key == "sigma_m" else None)
        for key in ("heading_deg", "pitch_deg", "roll_deg", "sigma_heading_deg"):
            _number(orientation.get(key), where + ".pose.orientation." + key,
                    0 if key == "sigma_heading_deg" else None)
        if (position["sigma_m"] == 0 or orientation["sigma_heading_deg"] == 0
                or not 0 <= orientation["heading_deg"] < 360
                or not -90 <= orientation["pitch_deg"] <= 90
                or not -180 < orientation["roll_deg"] <= 180):
            raise InputError(where + " invalid pose uncertainties or angles")
        if orientation.get("port_normal") not in ("+z", "-z"):
            raise InputError("%s invalid port_normal" % where)
        for key in ("source", "surveyed_utc"):
            if not isinstance(pose.get(key), str) or not pose[key].strip():
                raise InputError(where + ".pose." + key + " must be a nonempty string")
        bearing = _number(observation.get("bearing_deg_true"), where + ".bearing_deg_true", 0)
        if bearing >= 360:
            raise InputError("%s bearing must be below 360" % where)
        if _number(observation.get("sigma_deg"), where + ".sigma_deg", 0) == 0:
            raise InputError(where + ".sigma_deg must be positive")
        _number(observation.get("source_time_wall_s"), where + ".source_time_wall_s", 0)
        if observation.get("clock_precise") is not False or observation.get("evidence") != "unvalidated":
            raise InputError("%s must retain unvalidated and imprecise clock labels" % where)
    if len(origins) > 1:
        raise InputError("BMA observations contain multiple origins")
    for i, refusal in enumerate(data["refusals"]):
        where = "refusals[%d]" % i
        _object(refusal, where)
        _text(refusal.get("reason"), where + ".reason")
        if set(("ray_index", "unit_id", "det_id")) - refusal.keys():
            raise InputError("%s missing refusal fields" % where)
        index = refusal["ray_index"]
        if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
            raise InputError("%s.ray_index must be a nonnegative integer or null" % where)
        for key in ("unit_id", "det_id"):
            if refusal[key] is not None and not isinstance(refusal[key], str):
                raise InputError(where + "." + key + " must be a string or null")
    return origins


def report(bma, heartbeat, statuses, bma_site, dama_site, now, max_stale, fleet=None):
    _text(bma_site, "BMA site")
    _text(dama_site, "DAMA site")
    if bma_site != dama_site:
        raise InputError("site mismatch between BMA and DAMA")
    _number(now, "as-of", 0)
    _number(max_stale, "max-stale-s", 0)
    origins = _validate_observations(bma)
    _object(heartbeat, "heartbeat")
    for label, document in (("BMA conversion", bma), ("heartbeat", heartbeat)):
        if "site_id" in document and document["site_id"] != bma_site:
            raise InputError(label + " site mismatch")
    sensors = _object(heartbeat.get("sensors"), "heartbeat.sensors")
    _nullable_number(heartbeat, "last_run_s", "heartbeat")
    nodes = {}
    for name, sensor in sorted(sensors.items()):
        _text(name, "sensor name")
        where = "heartbeat.sensors.%s" % name
        _object(sensor, where)
        for key in ("last_success_s", "last_attempt_s", "uptime_s", "last_added"):
            _nullable_number(sensor, key, where)
        if sensor.get("last_error") is not None and not isinstance(sensor["last_error"], str):
            raise InputError(where + ".last_error must be a string or null")
        for key in ("unfetched_recent", "clips_recent", "live_recent"):
            if key in sensor and (not isinstance(sensor[key], list)
                                  or any(not isinstance(item, dict) for item in sensor[key])):
                raise InputError(where + "." + key + " must be an array of objects")
        nodes[name] = {"drain_success": _age_state(sensor.get("last_success_s"), now, max_stale),
                       "status_clock": "unknown", "status_capture_freshness": "unknown", "status": None}
    for status in statuses:
        _object(status, "status")
        if "site_id" in status and status["site_id"] != dama_site:
            raise InputError("status site mismatch")
        name = _text(status.get("node"), "status.node")
        if name not in nodes:
            raise InputError("status node not present in heartbeat: %s" % name)
        if nodes[name]["status"] is not None:
            raise InputError("duplicate status for node: %s" % name)
        clock = status.get("time")
        if clock is not None:
            _object(clock, "status.time")
            valid = clock.get("valid")
            if valid is not None and not isinstance(valid, bool):
                raise InputError("status.time.valid must be boolean or null")
            nodes[name]["status_clock"] = "valid" if valid is True else "invalid" if valid is False else "unknown"
        nodes[name]["status"] = status
    fleet_age = None
    if fleet is not None:
        _object(fleet, "BMA fleet")
        if "site_id" in fleet and fleet["site_id"] != bma_site:
            raise InputError("BMA fleet site mismatch")
        if fleet.get("k") != "fleet" or type(fleet.get("v")) is not int or fleet["v"] != 1:
            raise InputError("BMA fleet must be a fleet v1 document")
        origin = _text(fleet.get("origin_id"), "BMA fleet.origin_id")
        if origins and origin not in origins:
            raise InputError("BMA fleet origin mismatch")
        _number(fleet.get("updated_s"), "BMA fleet.updated_s", 0)
        if not isinstance(fleet.get("units"), list):
            raise InputError("BMA fleet.units must be an array")
        for unit in fleet["units"]:
            _object(unit, "BMA fleet unit")
            _text(unit.get("unit_id"), "BMA fleet unit.unit_id")
        fleet_age = _age_state(fleet["updated_s"], now, max_stale)
    return {
        "report": "offline_bma_dama_field_evidence_v1",
        "site": bma_site,
        "as_of_s": now,
        "max_stale_s": max_stale,
        "limitations": [
            "BMA bearings are unvalidated; source wall time is display-only, not precise timing.",
            "No observation matching, TDoA, localization, or live ingest is performed.",
            "Site labels and snapshot association are operator assertions, not independently verified.",
            "Drain freshness and a status clock flag are not a complete health or offline determination.",
        ],
        "bma": {"conversion": bma, "fleet_snapshot": fleet, "fleet_freshness": fleet_age},
        "dama": {"heartbeat": heartbeat, "heartbeat_state": "recorded" if sensors else "no_sensors",
                 "nodes": nodes},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bma", help="converted observations JSON file")
    source.add_argument("--fleet", help="BMA fleet v1 JSON file to convert locally")
    parser.add_argument("--origin", help="WGS84/true-north origin JSON, required with --fleet")
    parser.add_argument("--heartbeat", required=True, help="hear-drain heartbeat.json file")
    parser.add_argument("--status", action="append", default=[], help="node /status JSON file; repeatable")
    parser.add_argument("--bma-fleet", help="optional original fleet v1 snapshot for recorded health")
    parser.add_argument("--bma-site", required=True, help="operator-attested BMA site")
    parser.add_argument("--dama-site", required=True, help="operator-attested heartbeat/status site")
    parser.add_argument("--as-of-s", required=True, type=float, help="replay time, Unix seconds")
    parser.add_argument("--max-stale-s", type=float, default=7200.0, help="freshness threshold (default 7200)")
    args = parser.parse_args(argv)
    if bool(args.fleet) != bool(args.origin):
        parser.error("--fleet and --origin must be supplied together")
    if args.fleet and args.bma_fleet:
        parser.error("--bma-fleet cannot be combined with --fleet")
    try:
        if args.fleet:
            _number(args.as_of_s, "as-of", 0)
            _number(args.max_stale_s, "max-stale-s", 0)
            fleet = read_json(args.fleet)
            origin = read_json(args.origin)
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from hear.ingest.bma_fleet import convert_fleet
            bma = convert_fleet(fleet, origin, now_s=args.as_of_s, max_age_s=args.max_stale_s)
            for refusal in bma["refusals"]:
                if refusal["reason"] in ("invalid_fleet", "invalid_origin", "origin_mismatch",
                                         "invalid_freshness"):
                    raise InputError("BMA converter refused input: " + refusal["reason"])
        else:
            bma = read_json(args.bma)
            fleet = read_json(args.bma_fleet) if args.bma_fleet else None
        result = report(bma, read_json(args.heartbeat),
                        [read_json(path) for path in args.status], args.bma_site, args.dama_site,
                        args.as_of_s, args.max_stale_s, fleet)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
