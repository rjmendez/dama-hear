#!/usr/bin/env python3
"""Explicit, bounded, metadata-only BMA fleet sender."""

import argparse
import ctypes
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hear.ingest.bma_fleet import convert_fleet

MAX_FILE = 1024 * 1024
MAX_RESPONSE = 16384
MAX_MESSAGE = 65536
MAX_RAYS = 512
MAX_UNITS = 256
MAX_PENDING = 256
MAX_SEEN = 4096
MAX_ATTEMPTS = 16
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z", re.ASCII)


class InputError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise InputError("nonfinite JSON value")


def _strict_json(data):
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
        def finite(node):
            if isinstance(node, float) and not math.isfinite(node):
                raise InputError("nonfinite JSON value")
            if isinstance(node, dict):
                for item in node.values():
                    finite(item)
            elif isinstance(node, list):
                for item in node:
                    finite(item)
        finite(value)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise InputError("invalid JSON") from exc


def read_local_json(path):
    path = os.path.realpath(path)
    if path.startswith(("\\\\", "//")):
        raise InputError("network input path")
    if os.name == "nt":
        drive = os.path.splitdrive(path)[0]
        if not drive or ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:
            raise InputError("input must be on a local fixed drive")
    try:
        if not os.path.isfile(path):
            raise InputError("input is not a regular file")
        with open(path, "rb") as stream:
            data = stream.read(MAX_FILE + 1)
    except OSError as exc:
        raise InputError("input read failed") from exc
    if len(data) > MAX_FILE:
        raise InputError("input exceeds size limit")
    return _strict_json(data)


def endpoint(url, token):
    try:
        parts = urlsplit(url)
        port = parts.port or 443
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None
                or re.fullmatch(r"(?:/[A-Za-z0-9_-]{1,128})?/ingest/batch", parts.path) is None
                or parts.query or parts.fragment or not 1 <= port <= 65535
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            raise ValueError()
        if (not token or len(token) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError()
        return parts.hostname, port, parts.path
    except (ValueError, TypeError) as exc:
        raise InputError("invalid endpoint or token") from exc


def post(host, port, path, token, body, timeout):
    """Return bounded response, never following redirects or exposing errors."""
    conn = None
    try:
        conn = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context())
        conn.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            return None, None
        return response.status, raw
    except (OSError, http.client.HTTPException, ValueError):
        return None, None
    finally:
        if conn is not None:
            conn.close()


def _receipt(status, raw):
    if status != 202:
        return None
    try:
        obj = _strict_json(raw)
        values = [obj[k] for k in ("accepted", "quarantined", "enqueued")]
        if (any(type(v) is not int or v < 0 or v > 1 for v in values)
                or values[0] + values[1] != 1 or values[2] > values[0]):
            return None
        return values
    except (InputError, TypeError, KeyError):
        return None


class Sender:
    def __init__(self, host, port, token, timeout, max_age, transport=post,
                 *, path="/ingest/batch", max_attempts=4, site_id=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise InputError("invalid attempt limit")
        if site_id is not None and (not isinstance(site_id, str) or _SITE.fullmatch(site_id) is None):
            raise InputError("invalid site_id")
        self.host, self.port, self.path, self.token = host, port, path, token
        self.timeout, self.max_age, self.transport = timeout, max_age, transport
        self.site_id = site_id
        self.max_attempts = max_attempts
        self.seen = {}
        self.pending = {}
        self.high_water = None

    def poll(self, fleet_path, origin_path, now):
        report = {"observed": 0, "refused": 0, "deduplicated": 0,
                  "capacity_dropped": 0, "expired": 0, "attempted": 0,
                  "accepted": 0, "quarantined": 0, "enqueued": 0,
                  "unconfirmed": 0, "not_enqueued": 0, "input_error": 0,
                  "pending": 0, "deferred": 0, "clock_rollback": 0}
        if (isinstance(now, bool) or not isinstance(now, (float, int))
                or not math.isfinite(now) or now < 0):
            report["input_error"] = 1
            report["pending"] = len(self.pending)
            return report
        if self.high_water is not None and now < self.high_water:
            report["clock_rollback"] = 1
            report["pending"] = len(self.pending)
            return report
        self.high_water = now
        for key, (timestamp, _) in list(self.pending.items()):
            if now - timestamp > self.max_age:
                report["expired"] += 1
                del self.pending[key]
        for key, timestamp in list(self.seen.items()):
            if key not in self.pending and now - timestamp > self.max_age:
                del self.seen[key]
        try:
            fleet = read_local_json(fleet_path)
            origin = read_local_json(origin_path)
            if (not isinstance(fleet, dict) or not isinstance(fleet.get("rays"), list)
                    or not isinstance(fleet.get("units"), list)
                    or len(fleet["rays"]) > MAX_RAYS or len(fleet["units"]) > MAX_UNITS):
                raise InputError("fleet shape or capacity invalid")
            converted = convert_fleet(fleet, origin, now_s=now, max_age_s=self.max_age)
            report["observed"] = len(converted["observations"])
            report["refused"] = len(converted["refusals"])
            for obs in converted["observations"]:
                key = (obs["origin_id"], obs["det_id"])
                if key in self.seen:
                    report["deduplicated"] += 1
                    continue
                if len(self.pending) >= MAX_PENDING or len(self.seen) >= MAX_SEEN:
                    report["capacity_dropped"] += 1
                    continue
                timestamp = obs["source_time_wall_s"]
                if timestamp > 9223372036854775:
                    report["refused"] += 1
                    continue
                device = "bma-" + obs["unit_id"]
                message = {"device_id": device, "ts_ms": int(timestamp * 1000),
                           "telemetry_path": "bma/bearing", "schema": "bma.bearing.v1",
                           "observation": obs}
                if self.site_id is not None:
                    message["site_id"] = self.site_id
                body = json.dumps({"device_id": device, "messages": [message]},
                                  allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(body) > MAX_MESSAGE:
                    report["capacity_dropped"] += 1
                    continue
                self.seen[key] = timestamp
                self.pending[key] = (timestamp, body)
        except (InputError, OSError, ValueError, OverflowError, RecursionError):
            report["input_error"] = 1
        for key, (timestamp, body) in list(self.pending.items()):
            if report["attempted"] >= self.max_attempts:
                break
            report["attempted"] += 1
            status, raw = self.transport(self.host, self.port, self.path, self.token, body, self.timeout)
            receipt = _receipt(status, raw)
            if receipt is None:
                report["unconfirmed"] += 1
                self.pending[key] = self.pending.pop(key)
                continue
            accepted, quarantined, enqueued = receipt
            report["accepted"] += accepted
            report["quarantined"] += quarantined
            report["enqueued"] += enqueued
            report["not_enqueued"] += accepted - enqueued
            del self.pending[key]
        report["pending"] = len(self.pending)
        report["deferred"] = report["pending"] - report["unconfirmed"]
        return report


def bounded_float(low, high):
    def parse(value):
        try:
            number = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a number") from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise argparse.ArgumentTypeError("number outside allowed bounds")
        return number
    return parse


def site_token(value):
    if _SITE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("site ID must be 1-64 ASCII letters, digits, _, ., : or -, starting with a letter or digit")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet", required=True, help="local atomically replaced fleet JSON")
    parser.add_argument("--origin", required=True, help="local atomically replaced origin JSON")
    parser.add_argument("--polls", type=int, required=True, help="finite number of polls (1-1000)")
    parser.add_argument("--interval", type=bounded_float(0.1, 60), default=1.0)
    parser.add_argument("--timeout", type=bounded_float(0.1, 30), default=5.0)
    parser.add_argument("--max-age", type=bounded_float(0.1, 3600), default=5.0)
    parser.add_argument("--max-attempts", type=int, default=4,
                        help="maximum HTTP attempts per poll (1-16)")
    parser.add_argument("--site-id", type=site_token,
                        help="optional site identifier required by a site-filtering receiver")
    args = parser.parse_args(argv)
    if not 1 <= args.polls <= 1000:
        parser.error("--polls must be between 1 and 1000")
    if not 1 <= args.max_attempts <= MAX_ATTEMPTS:
        parser.error("--max-attempts must be between 1 and 16")
    try:
        token = os.environ.get("BMA_AWS_TOKEN", "")
        host, port, path = endpoint(os.environ.get("BMA_AWS_URL", ""), token)
    except InputError:
        print("invalid BMA_AWS_URL or BMA_AWS_TOKEN", file=sys.stderr)
        return 2
    sender = Sender(host, port, token, args.timeout, args.max_age,
                    path=path, max_attempts=args.max_attempts, site_id=args.site_id)
    failed = False
    for i in range(args.polls):
        report = sender.poll(args.fleet, args.origin, time.time())
        print(json.dumps(report, separators=(",", ":")), flush=True)
        failed |= any(report[k] for k in (
            "refused", "capacity_dropped", "expired", "unconfirmed",
            "not_enqueued", "quarantined", "input_error", "clock_rollback"))
        if i + 1 < args.polls:
            time.sleep(args.interval)
    return 1 if failed or sender.pending else 0


if __name__ == "__main__":
    sys.exit(main())
