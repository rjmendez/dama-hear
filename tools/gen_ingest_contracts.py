#!/usr/bin/env python3
"""Generate the checked-in ingest contract artifacts from the field tables in `hear/ingest/`.

    python3 tools/gen_ingest_contracts.py           # write artifacts
    python3 tools/gen_ingest_contracts.py --check   # fail if checked-in artifacts drift

Two contracts are generated here:

* `hear.ingest.v1` from `hear/ingest/envelope.py` -- the canonical item envelope.
* `hear.ingest.batch.v1` from `hear/ingest/batch.py` -- the HTTPS batch request frame and
  its receipt (`docs/decisions/0002-phase4-https-batch-ingest-adapter.md`).

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
from hear.ingest import batch as BA  # noqa: E402

CONTRACTS = ROOT / "contracts"
SCHEMA_PATH = CONTRACTS / "schemas" / "hear.ingest.v1.schema.json"
FIXTURE_DIR = CONTRACTS / "fixtures" / "hear.ingest.v1"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

BATCH_SCHEMA_PATH = CONTRACTS / "schemas" / "hear.ingest.batch.v1.schema.json"
RECEIPT_SCHEMA_PATH = CONTRACTS / "schemas" / "hear.ingest.batch.receipt.v1.schema.json"
BATCH_FIXTURE_DIR = CONTRACTS / "fixtures" / "hear.ingest.batch.v1"
BATCH_MANIFEST_PATH = BATCH_FIXTURE_DIR / "manifest.json"

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


DEVICE_ID = BASE["device_id"]
RECEIPT_RECEIVED_AT = "2026-09-14T18:03:12.100000Z"
RECEIPT_ADAPTER = "ingest-batch"
RECEIPT_ADAPTER_VERSION = "0.1.0-design"

# The legacy shapes the fleet emits today, reproduced from
# `firmware/hear_node/hear_push_payload.h`. They exist as fixtures so the mixed-version
# window is tested rather than assumed: a node that has not been reflashed must be able to
# fill a batch, and its items must be routed to a translator instead of refused as junk.
LEGACY_HEARTBEAT: Dict[str, Any] = {
    "telemetry_path": "hear/heartbeat",
    "telemetry_schema_version": 1,
    "device_id": DEVICE_ID,
    "ts": "2026-09-14T18:03:10Z",
    "ts_ms": 1789495390000,
    "class": "esp32s3-i2s-gps",
    "fw_version": "hear-node-2026.09.1",
    "uptime_s": 86412,
    "gps": {"fix": 1},
    "time": {"valid": True, "state": "LOCKED", "sync_sigma_ns": 420000,
             "anchor_age_us": 812000, "boot_epoch_us": 1789495200000000,
             "boot_id": "018f2b905f3c7c21", "discontinuity_flags": 0},
    "wifi": {"rssi_dbm": -61},
    "counters": {"scene_rows_written": 91204, "dets_rows_written": 4471,
                 "clips_written": 38, "clips_evicted": 4},
}

LEGACY_EVENT: Dict[str, Any] = {
    "telemetry_path": "hear/event",
    "telemetry_schema_version": 1,
    "device_id": DEVICE_ID,
    "ts": "2026-09-14T18:03:11Z",
    "ts_ms": 1789495391000,
    "class": "esp32s3-i2s-gps",
    "fw_version": "hear-node-2026.09.1",
    "uptime_s": 86413,
    "time": {"valid": True, "state": "LOCKED", "sync_sigma_ns": 420000,
             "anchor_age_us": 813000, "boot_epoch_us": 1789495200000000,
             "boot_id": "018f2b905f3c7c21", "discontinuity_flags": 0},
    "event_type": "dets",
    "event_seq": 512,
    "event": {"dets_rows_written": 4471, "batch_rows": 12},
}


def _frame(messages: List[Any], **over: Any) -> Dict[str, Any]:
    frame: Dict[str, Any] = {
        "batch_schema_version": BA.BATCH_SCHEMA_MAJOR,
        "batch_id": "018f2c1a-batch-0007",
        "device_id": DEVICE_ID,
        "sent_at": "2026-09-14T18:03:11.990000Z",
        "messages": messages,
        "producer": {
            "boot_id": "018f2b90-5f3c-7c21-9a7e-1d2c3b4a5e6f",
            "boot_epoch_us": 1789495200000000,
            "batch_sequence": 7,
            "spool_backlog": 118,
        },
    }
    frame.update(over)
    return frame


def _item(sequence: int, **over: Any) -> Dict[str, Any]:
    env = _copy(BASE)
    env["producer"] = dict(env["producer"], sequence=sequence, cursor="dets:%d" % sequence)
    env.update(over)
    return _signed(env)


def _batch_valid() -> Dict[str, Any]:
    """Three canonical items from one node, in producer order."""
    return _frame([_item(4471), _item(4472), _item(4473)])


def _batch_legacy_messages() -> Dict[str, Any]:
    """A node that has not been reflashed, filling a batch with today's telemetry shape."""
    return _frame([_copy(LEGACY_HEARTBEAT), _copy(LEGACY_EVENT)],
                  batch_id="018f2c1a-batch-legacy-1")


def _batch_poison_item() -> Dict[str, Any]:
    """One unreadable item between two good ones.

    The batch is still admitted and the good items are still durable. This is the fixture
    that proves a single bad row cannot wedge a node's backlog behind it forever.
    """
    poison = _item(4472)
    poison.pop("clock")
    return _frame([_item(4471), poison, _item(4473)],
                  batch_id="018f2c1a-batch-poison-1")


def _batch_item_future_major() -> Dict[str, Any]:
    """A newer writer's item inside a frame this server understands.

    The item is refused (durably, with a reason) while the frame and its siblings proceed:
    an item major is not a frame major.
    """
    future = _item(4472)
    future["schema_version"] = 2
    return _frame([_item(4471), future], batch_id="018f2c1a-batch-itemv2-1")


def _batch_forward_additive() -> Dict[str, Any]:
    """A newer producer adding frame fields. Accepted, preserved, not reinterpreted."""
    frame = _batch_valid()
    frame["batch_id"] = "018f2c1a-batch-additive-1"
    frame["compression"] = "identity"
    frame["producer"] = dict(frame["producer"], link="wifi")
    return frame


def _batch_future_major() -> Dict[str, Any]:
    frame = _batch_valid()
    frame["batch_schema_version"] = 2
    frame["batch_id"] = "018f2c1a-batch-v2-1"
    return frame


def _batch_empty() -> Dict[str, Any]:
    return _frame([], batch_id="018f2c1a-batch-empty-1")


def _batch_too_many_items() -> Dict[str, Any]:
    return _frame([{} for _ in range(BA.MAX_ITEMS_PER_BATCH + 1)],
                  batch_id="018f2c1a-batch-oversize-1")


def _batch_identity_mismatch() -> Dict[str, Any]:
    """A body claiming a device the credential does not authorize."""
    return _frame([_item(4471)], device_id="mach",
                  batch_id="018f2c1a-batch-mismatch-1")


def _batch_item_identity_mismatch() -> Dict[str, Any]:
    """A correctly-addressed frame smuggling an item attributed to another node.

    `device_id` is an identity input, so accepting this would mint a durable, dispatchable
    event for a node that never sent it. The frame is fine; the item is refused.
    """
    forged = _copy(BASE)
    forged["device_id"] = "gold"
    forged["site_id"] = "site-somewhere-else"
    forged["producer"] = dict(forged["producer"], sequence=4472, cursor="dets:4472")
    forged = _signed(forged)
    return _frame([_item(4471), forged], batch_id="018f2c1a-batch-forged-1")


def _batch_gateway_site_scoped() -> Dict[str, Any]:
    """A drain/import adapter submitting for several nodes under a site credential."""
    mach = _copy(BASE)
    mach["device_id"] = "mach"
    mach["source"] = "import"
    mach["adapter"] = {"name": "hear-drain-shadow", "version": "0.1.0"}
    mach["producer"] = dict(mach["producer"], sequence=881, cursor="dets:881")
    return _frame([_item(4471), _signed(mach)], device_id="hear-drain-shadow",
                  batch_id="018f2c1a-batch-gateway-1",
                  adapter={"name": "hear-drain-shadow", "version": "0.1.0"})


def _batch_malformed_frame() -> Dict[str, Any]:
    frame = _batch_valid()
    frame["batch_id"] = "018f2c1a-batch-malformed-1"
    frame["sent_at"] = "2026-09-14 18:03:11+00:00"
    frame["messages"] = {"0": _item(4471)}
    return frame


def _batch_unrecognized_item() -> Dict[str, Any]:
    return _frame([_item(4471), {"hello": "world"}, "not-an-object"],
                  batch_id="018f2c1a-batch-junk-1")


# A credential is mandatory on this route, so every fixture declares the credential it is
# asserted against. `site` fixtures use a site-scoped gateway credential instead.
DEVICE_CRED = {"credential_device_id": DEVICE_ID}
SITE_CRED = {"credential_device_id": None, "credential_site_id": BASE["site_id"],
             "site_scoped": True}

# name, factory, credential, frame status, frame reasons, emit receipt
BATCH_FIXTURES = (
    ("valid-node-batch", _batch_valid, DEVICE_CRED, "accepted", [], True),
    # No receipt: every item is still awaiting translation, and a receipt may not be issued
    # in that state. The post-translation receipt is the adapter's, not the frame reader's.
    ("mixed-version-legacy-messages", _batch_legacy_messages, DEVICE_CRED, "accepted", [],
     False),
    ("poison-item-keeps-batch", _batch_poison_item, DEVICE_CRED, "accepted", [], True),
    ("item-future-major-refused-alone", _batch_item_future_major, DEVICE_CRED, "accepted", [],
     True),
    ("item-identity-mismatch", _batch_item_identity_mismatch, DEVICE_CRED, "accepted", [],
     True),
    ("gateway-site-scoped-batch", _batch_gateway_site_scoped, SITE_CRED, "accepted", [], True),
    ("forward-additive-frame-fields", _batch_forward_additive, DEVICE_CRED, "accepted", [],
     False),
    ("unrecognized-items-refused", _batch_unrecognized_item, DEVICE_CRED, "accepted", [], True),
    ("unsupported-future-batch-major", _batch_future_major, DEVICE_CRED, "refused",
     ["batch_schema_version_unsupported"], False),
    ("empty-batch", _batch_empty, DEVICE_CRED, "refused", ["batch_empty"], False),
    ("batch-too-many-items", _batch_too_many_items, DEVICE_CRED, "refused",
     ["batch_too_many_items"], False),
    ("device-identity-mismatch", _batch_identity_mismatch, DEVICE_CRED, "refused",
     ["device_identity_mismatch"], False),
    ("missing-credential", _batch_valid, {"credential_device_id": None}, "refused",
     ["credential_missing"], False),
    ("malformed-frame-field-types", _batch_malformed_frame, DEVICE_CRED, "refused",
     ["type_invalid", "timestamp_not_rfc3339_utc"], False),
)


def build_batch(out: Dict[Path, str]) -> None:
    out[BATCH_SCHEMA_PATH] = json.dumps(BA.batch_schema_document(), indent=2) + "\n"
    out[RECEIPT_SCHEMA_PATH] = json.dumps(BA.receipt_schema_document(), indent=2) + "\n"

    entries: List[Dict[str, Any]] = []
    for name, factory, credential, status, reasons, emit_receipt in BATCH_FIXTURES:
        frame = factory()
        body = json.dumps(frame, indent=2) + "\n"
        path = BATCH_FIXTURE_DIR / ("%s.json" % name)
        out[path] = body

        result = BA.validate_batch(frame, **credential)
        if result.status != status:
            raise SystemExit("batch fixture %s expected %s, validator says %s (%s)"
                             % (name, status, result.status, result.reasons))
        if sorted(set(result.reasons)) != sorted(set(reasons)):
            raise SystemExit("batch fixture %s expected reasons %s, got %s"
                             % (name, reasons, result.reasons))

        entry: Dict[str, Any] = {
            "name": name,
            "file": path.name,
            "credential": dict(credential),
            "expect_status": status,
            "expect_reasons": sorted(set(reasons)),
            "expect_unknown_fields": sorted(result.unknown_fields),
            "expect_items": [r.to_dict() for r in result.items],
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
        if result.ok:
            entry["expect_counts"] = BA.counts(result.items)
            entry["expect_ack_through_index"] = BA.ack_through_index(result.items)
            entry["expect_retry_indices"] = [r.index for r in result.items
                                             if r.status == "deferred"]
        if emit_receipt:
            receipt = BA.build_receipt(frame, result.items,
                                       received_at=RECEIPT_RECEIVED_AT,
                                       adapter=RECEIPT_ADAPTER,
                                       adapter_version=RECEIPT_ADAPTER_VERSION)
            receipt_body = json.dumps(receipt, indent=2) + "\n"
            receipt_path = BATCH_FIXTURE_DIR / ("%s.receipt.json" % name)
            out[receipt_path] = receipt_body
            entry["receipt_file"] = receipt_path.name
            entry["receipt_sha256"] = hashlib.sha256(
                receipt_body.encode("utf-8")).hexdigest()
        entries.append(entry)

    manifest = {
        "schema_id": BA.BATCH_SCHEMA_ID,
        "schema_major": BA.BATCH_SCHEMA_MAJOR,
        "schema_file": BATCH_SCHEMA_PATH.relative_to(CONTRACTS).as_posix(),
        "receipt_schema_id": BA.RECEIPT_SCHEMA_ID,
        "receipt_schema_file": RECEIPT_SCHEMA_PATH.relative_to(CONTRACTS).as_posix(),
        "item_schema_id": EV.SCHEMA_ID,
        "item_schema_file": SCHEMA_PATH.relative_to(CONTRACTS).as_posix(),
        "codecs": dict(BA.BATCH_CODEC_MEDIA_TYPES),
        "receipt_media_type": BA.RECEIPT_MEDIA_TYPE,
        "item_statuses": list(BA.ITEM_STATUSES),
        "item_results_stage": "frame-reader, pre-translation and pre-dedup",
        "item_results_note": (
            "expect_items is what the frame reader alone can conclude. It does not consult "
            "the durable store, so an item it calls `accepted` becomes `duplicate` when its "
            "event_id is already durable. An item classified `translation_required` carries "
            "no receipt-ready outcome at all: the adapter must translate it and call "
            "resolve_translation() to give it a real status, and build_receipt() refuses to "
            "issue a receipt while any item is still untranslated -- reporting one as "
            "`deferred` would tell the producer to resend it forever. A cross-language "
            "adapter asserts against these values for frame admission and item "
            "classification, not for the final persisted outcome."),
        "limits": {
            "max_items_per_batch": BA.MAX_ITEMS_PER_BATCH,
            "max_batch_bytes": BA.MAX_BATCH_BYTES,
            "max_item_bytes": BA.MAX_ITEM_BYTES,
            "max_clock_skew_s": BA.MAX_CLOCK_SKEW_S,
        },
        "generator": "tools/gen_ingest_contracts.py",
        "decision": "docs/decisions/0002-phase4-https-batch-ingest-adapter.md",
        "fixtures": entries,
    }
    out[BATCH_MANIFEST_PATH] = json.dumps(manifest, indent=2) + "\n"


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
    build_batch(out)
    return out


def stale_artifacts() -> List[str]:
    artifacts = build()
    stale: List[str] = []
    for path, body in sorted(artifacts.items()):
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            stale.append(path.relative_to(ROOT).as_posix())
    # Compare by full path, not by file name: two fixture directories both contain a
    # `manifest.json`, and name-only matching would hide an orphan in either of them.
    known = set(artifacts)
    for directory in (FIXTURE_DIR, BATCH_FIXTURE_DIR):
        if not directory.exists():
            continue
        for existing in sorted(directory.glob("*.json")):
            if existing not in known:
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
