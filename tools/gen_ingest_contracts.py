#!/usr/bin/env python3
"""Generate the checked-in `hear.ingest.v1` contract artifacts from `hear/ingest/envelope.py`.

    python3 tools/gen_ingest_contracts.py           # write artifacts
    python3 tools/gen_ingest_contracts.py --check   # fail if checked-in artifacts drift

Fixtures are generated, not hand-maintained, so a field-table edit cannot leave the golden
payloads describing an envelope that no longer exists. The fixture manifest carries each
fixture's expected validation outcome, which is what cross-language adapters assert against.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.ingest import envelope as EV  # noqa: E402

CONTRACTS = ROOT / "contracts"
SCHEMA_PATH = CONTRACTS / "schemas" / "hear.ingest.v1.schema.json"
FIXTURE_DIR = CONTRACTS / "fixtures" / "hear.ingest.v1"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

BASE: Dict[str, Any] = {
    "event_id": "",
    "source": "node-http",
    "site_id": "site-quarry-north",
    "device_id": "nyquist",
    "device_class": "esp32s3-i2s-gps",
    "firmware_version": "hear-node-2026.09.1",
    "observed_at": "2026-09-14T18:03:11.250000Z",
    "received_at": "2026-09-14T18:03:11.984000Z",
    "clock": {"valid": True, "tier": "gps_pps", "sigma_ns": 420000},
    "kind": "detection",
    "schema_version": 1,
    "payload": {
        "wire_version": 2,
        "profile_id": 0,
        "frame_b64": "AAECAwQFBgcICQoLDA0ODw==",
        "peak_db": -12.5,
    },
    "raw_ref": "raw/2026/09/14/nyquist/018f2c1a.bin",
    "adapter": {"name": "node-http", "version": "1.0.0"},
    "producer": {
        "boot_id": "018f2b90-5f3c-7c21-9a7e-1d2c3b4a5e6f",
        "boot_epoch_us": 1789495200000000,
        "sequence": 4471,
        "cursor": "dets:4471",
    },
}


def _copy(env: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(env))


def _signed(env: Dict[str, Any]) -> Dict[str, Any]:
    env = _copy(env)
    env["event_id"] = EV.derive_event_id(env)
    return env


def _valid() -> Dict[str, Any]:
    return _signed(BASE)


def _degraded_no_clock() -> Dict[str, Any]:
    """A node recording without a usable time anchor. Storable, not localizable."""
    env = _copy(BASE)
    env["observed_at"] = None
    env["clock"] = {"valid": False, "tier": "monotonic", "sigma_ns": 0}
    env["producer"] = {"boot_id": "018f2b90-5f3c-7c21-9a7e-1d2c3b4a5e6f", "sequence": 1,
                       "cursor": "dets:1"}
    return _signed(env)


def _minimal_legacy_producer() -> Dict[str, Any]:
    """An older writer with no producer block: every required field, nothing optional."""
    env = _copy(BASE)
    env.pop("producer")
    env["source"] = "node-mqtt"
    env["kind"] = "heartbeat"
    env["raw_ref"] = None
    env["payload"] = {"uptime_s": 86412, "gps_fix": 1, "clock_state": "LOCKED"}
    env["adapter"] = {"name": "node-mqtt", "version": "0.9.3"}
    return _signed(env)


def _gotchi_adapter() -> Dict[str, Any]:
    """Phone-sourced record: wall clock, unsurveyed, original payload preserved."""
    env = _copy(BASE)
    env["source"] = "gotchi"
    env["device_id"] = "gotchi-phone-7f31"
    env["device_class"] = "gotchi-phone"
    env["kind"] = "sketch"
    env["clock"] = {"valid": True, "tier": "wall", "sigma_ns": 250000000}
    env["raw_ref"] = "raw/2026/09/14/gotchi/018f2c1b.json"
    env["payload"] = {"producer_schema": "gotchi.sketch.v3", "surveyed": False,
                      "original": {"t": 1789495391.25, "rms": 0.031}}
    env["adapter"] = {"name": "gotchi-adapter", "version": "0.4.1"}
    env["producer"] = {"cursor": "sqs:018f2c1b"}
    return _signed(env)


def _forward_additive() -> Dict[str, Any]:
    """A newer v1 writer: additive fields plus an unknown enum member.

    A v1 reader must accept and preserve this, must derive the SAME event_id as the
    stripped fixture below, and must not dispatch it on `kind`.
    """
    env = _valid()
    env["kind"] = "seismic"
    env["clock"] = dict(env["clock"], holdover_s=12)
    env["ingest_hints"] = {"priority": "low"}
    env["adapter"] = dict(env["adapter"], commit="9f2c1ad")
    env["event_id"] = EV.derive_event_id(env)
    return env


def _forward_additive_stripped() -> Dict[str, Any]:
    """The same observation without the additive extras: identical event_id, other bytes."""
    env = _forward_additive()
    env.pop("ingest_hints")
    env["clock"] = {k: v for k, v in env["clock"].items() if k != "holdover_s"}
    env["adapter"] = {k: v for k, v in env["adapter"].items() if k != "commit"}
    return env


def _future_major() -> Dict[str, Any]:
    env = _valid()
    env["schema_version"] = 2
    return env


def _malformed_missing_clock() -> Dict[str, Any]:
    env = _valid()
    env.pop("clock")
    return env


def _malformed_types() -> Dict[str, Any]:
    env = _valid()
    env["received_at"] = "2026-09-14 18:03:11+00:00"
    env["clock"] = {"valid": "yes", "tier": "gps_pps", "sigma_ns": -5}
    env["payload"] = "not-an-object"
    return env


def _malformed_clock_claim() -> Dict[str, Any]:
    env = _valid()
    env["observed_at"] = None
    return env


FIXTURES = (
    ("valid-node-detection", _valid, "accepted", True, []),
    ("degraded-no-clock-anchor", _degraded_no_clock, "accepted", True, []),
    ("minimal-legacy-producer", _minimal_legacy_producer, "accepted", True, []),
    ("gotchi-adapter-sketch", _gotchi_adapter, "accepted", True, []),
    ("forward-additive-unknown-fields", _forward_additive, "accepted", False, []),
    ("forward-additive-stripped", _forward_additive_stripped, "accepted", False, []),
    ("unsupported-future-major", _future_major, "refused", False,
     ["schema_version_unsupported"]),
    ("malformed-missing-clock", _malformed_missing_clock, "refused", False,
     ["field_missing"]),
    ("malformed-field-types", _malformed_types, "refused", False,
     ["type_invalid", "value_out_of_range", "timestamp_not_rfc3339_utc"]),
    ("malformed-clock-valid-without-time", _malformed_clock_claim, "refused", False,
     ["clock_valid_without_observed_at"]),
)


def build() -> Dict[Path, str]:
    out: Dict[Path, str] = {}
    out[SCHEMA_PATH] = json.dumps(EV.schema_document(), indent=2) + "\n"

    entries: List[Dict[str, Any]] = []
    for name, factory, status, dispatchable, reasons in FIXTURES:
        env = factory()
        body = json.dumps(env, indent=2) + "\n"
        path = FIXTURE_DIR / ("%s.json" % name)
        out[path] = body
        result = EV.validate(env)
        if result.status != status:
            raise SystemExit("fixture %s expected %s, validator says %s (%s)"
                             % (name, status, result.status, result.reasons))
        if result.dispatchable != dispatchable:
            raise SystemExit("fixture %s expected dispatchable=%s" % (name, dispatchable))
        if sorted(set(result.reasons)) != sorted(set(reasons)):
            raise SystemExit("fixture %s expected reasons %s, got %s"
                             % (name, reasons, result.reasons))
        entries.append({
            "name": name,
            "file": path.name,
            "expect_status": status,
            "expect_dispatchable": dispatchable,
            "expect_reasons": sorted(set(reasons)),
            "expect_unknown_fields": sorted(result.unknown_fields),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })

    manifest = {
        "schema_id": EV.SCHEMA_ID,
        "schema_major": EV.SCHEMA_MAJOR,
        "schema_file": SCHEMA_PATH.relative_to(CONTRACTS).as_posix(),
        "codecs": dict(EV.CODEC_MEDIA_TYPES),
        "identity_inputs": list(EV.IDENTITY_INPUTS),
        "generator": "tools/gen_ingest_contracts.py",
        "fixtures": entries,
    }
    out[MANIFEST_PATH] = json.dumps(manifest, indent=2) + "\n"
    return out


def stale_artifacts() -> List[str]:
    artifacts = build()
    stale: List[str] = []
    for path, body in sorted(artifacts.items()):
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            stale.append(path.relative_to(ROOT).as_posix())
    known = {p.name for p in artifacts}
    if FIXTURE_DIR.exists():
        for existing in sorted(FIXTURE_DIR.glob("*.json")):
            if existing.name not in known:
                stale.append(existing.relative_to(ROOT).as_posix() + " (orphaned)")
    return stale


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="generate hear.ingest.v1 contract artifacts")
    ap.add_argument("--check", action="store_true",
                    help="verify checked-in artifacts match the generator")
    args = ap.parse_args(argv)

    if args.check:
        stale = stale_artifacts()
        if stale:
            print("stale contract artifacts:\n  " + "\n  ".join(stale), file=sys.stderr)
            print("rerun: python3 tools/gen_ingest_contracts.py", file=sys.stderr)
            return 1
        print("contract artifacts current")
        return 0

    for path, body in sorted(build().items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print("wrote %s" % path.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
