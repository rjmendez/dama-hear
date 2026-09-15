"""Contract tests for the `hear.ingest.batch.v1` HTTPS batch frame and its receipt.

The frame exists to move `hear.ingest.v1` items over one request from a six-node fleet with
a lossy link. So the tests that matter are the ones about partial failure: a bad row must
not refuse a good batch, an acknowledgement must never outrun durability, and a batch resent
after a reboot must converge on the same durable events rather than double them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hear.ingest import batch as BA
from hear.ingest import envelope as EV
from tools import gen_ingest_contracts as GEN

FIXTURE_DIR = (Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
               / "hear.ingest.batch.v1")
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
ENTRIES = MANIFEST["fixtures"]


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _frame(name: str) -> dict:
    return _load("%s.json" % name)


def _entry(name: str) -> dict:
    return next(e for e in ENTRIES if e["name"] == name)


# --- generated artifacts stay in step with the field tables --------------------------------

def test_checked_in_batch_contracts_match_generator():
    assert GEN.stale_artifacts() == []


def test_batch_schema_declares_framing_limits_and_item_seam():
    doc = BA.batch_schema_document()
    assert doc["x-schema-major"] == BA.BATCH_SCHEMA_MAJOR
    assert doc["x-item-schema"] == EV.SCHEMA_URI, "the frame points at the item contract"
    assert doc["additionalProperties"] is True, "unknown frame fields must survive"
    assert doc["properties"]["batch_schema_version"]["const"] == 1
    assert doc["x-limits"]["max_items_per_batch"] == BA.MAX_ITEMS_PER_BATCH
    # The frame must not constrain item shape: items are versioned independently, and a
    # frame schema that validated them would refuse a newer item major at the wrong layer.
    assert "required" not in doc["properties"]["messages"]["items"]


def test_receipt_schema_declares_the_spool_release_rule():
    doc = BA.receipt_schema_document()
    assert doc["x-item-statuses"] == list(BA.ITEM_STATUSES)
    assert doc["properties"]["ack_through_index"]["minimum"] == -1
    assert "ack_through_index" in doc["description"]


def test_fixture_digests_pin_contents():
    import hashlib
    for entry in ENTRIES:
        body = (FIXTURE_DIR / entry["file"]).read_text(encoding="utf-8")
        assert hashlib.sha256(body.encode("utf-8")).hexdigest() == entry["sha256"], entry["name"]
        if "receipt_file" in entry:
            body = (FIXTURE_DIR / entry["receipt_file"]).read_text(encoding="utf-8")
            assert hashlib.sha256(body.encode("utf-8")).hexdigest() == entry["receipt_sha256"]


# --- fixture conformance --------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_fixture_matches_declared_outcome(entry):
    frame = _load(entry["file"])
    result = BA.validate_batch(frame, credential_device_id=entry["credential_device_id"])
    assert result.status == entry["expect_status"]
    assert sorted(set(result.reasons)) == entry["expect_reasons"]
    assert sorted(result.unknown_fields) == entry["expect_unknown_fields"]
    assert [r.to_dict() for r in result.items] == entry["expect_items"]
    if result.ok:
        assert BA.counts(result.items) == entry["expect_counts"]
        assert BA.ack_through_index(result.items) == entry["expect_ack_through_index"]


@pytest.mark.parametrize("entry", [e for e in ENTRIES if "receipt_file" in e],
                         ids=[e["name"] for e in ENTRIES if "receipt_file" in e])
def test_receipt_fixture_conserves_and_matches_results(entry):
    receipt = _load(entry["receipt_file"])
    counts = receipt["counts"]
    assert counts["submitted"] == (counts["accepted"] + counts["duplicate"]
                                   + counts["refused"] + counts["deferred"])
    assert len(receipt["results"]) == counts["submitted"]
    assert receipt["batch_id"] == _load(entry["file"])["batch_id"]
    assert receipt["server"]["envelope_major"] == EV.SCHEMA_MAJOR


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_fixture_round_trips_through_the_codec(entry):
    frame = _load(entry["file"])
    assert BA.decode_batch(EV.encode(frame)) == frame


# --- frame refusal versus item refusal -------------------------------------------------------

def test_one_bad_item_does_not_refuse_the_batch():
    # The whole reason a batch is not a transaction: a node with one unreadable row would
    # otherwise retry the same batch forever and never drain anything behind it.
    result = BA.validate_batch(_frame("poison-item-keeps-batch"),
                               credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert [r.status for r in result.items] == ["accepted", "refused", "accepted"]
    assert result.items[1].reasons == ["field_missing"]
    assert BA.ack_through_index(result.items) == 2


def test_item_major_is_not_frame_major():
    result = BA.validate_batch(_frame("item-future-major-refused-alone"),
                               credential_device_id=GEN.DEVICE_ID)
    assert result.ok, "a newer item does not make the frame unreadable"
    assert result.items[0].status == "accepted"
    assert result.items[1].status == "refused"
    assert result.items[1].reasons == ["schema_version_unsupported"]


def test_unsupported_frame_major_is_refused_before_any_item_is_read():
    result = BA.validate_batch(_frame("unsupported-future-batch-major"),
                               credential_device_id=GEN.DEVICE_ID)
    assert result.reasons == ["batch_schema_version_unsupported"]
    assert result.items == [], "no item may be interpreted under an unknown framing"
    assert result.errors[0]["supported"] == BA.BATCH_SCHEMA_MAJOR


def test_forward_additive_frame_fields_are_preserved_not_refused():
    frame = _frame("forward-additive-frame-fields")
    result = BA.validate_batch(frame, credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert set(result.unknown_fields) == {"compression", "producer.link"}
    assert json.loads(EV.encode(frame).decode("utf-8")) == frame


def test_unrecognized_items_are_refused_individually_with_a_reason():
    result = BA.validate_batch(_frame("unrecognized-items-refused"),
                               credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert [r.status for r in result.items] == ["accepted", "refused", "refused"]
    assert result.items[1].reasons == ["item_unrecognized"]
    assert result.items[2].classification is None


# --- identity binding --------------------------------------------------------------------------

def test_body_device_id_may_not_override_the_credential():
    frame = _frame("device-identity-mismatch")
    assert BA.validate_batch(frame, credential_device_id="mach").ok
    result = BA.validate_batch(frame, credential_device_id=GEN.DEVICE_ID)
    assert "device_identity_mismatch" in result.reasons


def test_limits_are_server_enforced():
    assert not BA.validate_batch(_frame("empty-batch"),
                                 credential_device_id=GEN.DEVICE_ID).ok
    over = BA.validate_batch(_frame("batch-too-many-items"),
                             credential_device_id=GEN.DEVICE_ID)
    assert over.reasons == ["batch_too_many_items"]
    assert over.errors[0]["limit"] == BA.MAX_ITEMS_PER_BATCH
    assert BA.body_too_large(b"x" * (BA.MAX_BATCH_BYTES + 1))
    with pytest.raises(BA.BatchError):
        BA.decode_batch(b"{" + b" " * BA.MAX_BATCH_BYTES + b"}")


def test_oversized_item_is_refused_without_refusing_the_batch():
    frame = _frame("valid-node-batch")
    fat = json.loads(json.dumps(frame["messages"][1]))
    fat["payload"] = dict(fat["payload"], blob="A" * (BA.MAX_ITEM_BYTES + 1))
    frame = dict(frame, messages=[frame["messages"][0], fat])
    result = BA.validate_batch(frame, credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert result.items[1].reasons == ["item_too_large"]


# --- acknowledgement, replay and ordering -------------------------------------------------------

def test_ack_is_a_contiguous_durable_prefix():
    items = [BA.ItemResult(0, "accepted"), BA.ItemResult(1, "refused", reasons=["x"]),
             BA.ItemResult(2, "deferred"), BA.ItemResult(3, "accepted")]
    # Index 3 is durable but index 2 is not. Acknowledging 3 would let the producer free a
    # record the server never stored, so the ack stops at the gap.
    assert BA.ack_through_index(items) == 1


def test_nothing_is_acknowledged_when_the_first_item_is_deferred():
    assert BA.ack_through_index([BA.ItemResult(0, "deferred"),
                                 BA.ItemResult(1, "accepted")]) == -1
    assert BA.ack_through_index([]) == -1


def test_producer_retries_only_what_was_not_acknowledged():
    receipt = _load(_entry("poison-item-keeps-batch")["receipt_file"])
    # The refused item is inside the acknowledged prefix and is durable as a refusal, so it
    # is never resent. Resending it would be an infinite loop on a body the server refuses.
    assert BA.unacknowledged_indices(receipt) == []
    receipt = _load(_entry("mixed-version-legacy-messages")["receipt_file"])
    assert BA.unacknowledged_indices(receipt) == [0, 1]


def test_counts_must_close():
    with pytest.raises(BA.BatchError):
        BA.counts([BA.ItemResult(0, "accepted")], submitted=2)
    assert BA.counts([BA.ItemResult(0, "accepted")], submitted=1)["submitted"] == 1


def test_unknown_item_status_is_refused_at_construction():
    with pytest.raises(BA.BatchError):
        BA.ItemResult(0, "probably-fine")


def test_reordering_and_rebatching_do_not_change_identity():
    # Delivery order is transport detail; identity is content. A node that reboots mid-spool
    # and re-splits its backlog differently must not create a second durable event.
    frame = _frame("valid-node-batch")
    ids = [BA.validate_batch(frame, credential_device_id=GEN.DEVICE_ID).items[i].event_id
           for i in range(3)]
    reversed_frame = dict(frame, messages=list(reversed(frame["messages"])))
    rev = BA.validate_batch(reversed_frame, credential_device_id=GEN.DEVICE_ID)
    assert [r.event_id for r in rev.items] == list(reversed(ids))
    split = dict(frame, messages=frame["messages"][:1], batch_id="018f2c1a-batch-0008")
    again = BA.validate_batch(split, credential_device_id=GEN.DEVICE_ID)
    assert again.items[0].event_id == ids[0]
    assert len(set(ids)) == 3, "distinct observations stay distinct"


def test_batch_id_is_not_a_deduplication_key():
    frame = _frame("valid-node-batch")
    renamed = dict(frame, batch_id="018f2c1a-batch-9999")
    a = BA.validate_batch(frame, credential_device_id=GEN.DEVICE_ID)
    b = BA.validate_batch(renamed, credential_device_id=GEN.DEVICE_ID)
    assert [r.event_id for r in a.items] == [r.event_id for r in b.items]


def test_malformed_batch_id_is_refused():
    for bad in ("", "short", "has space", "x" * 200):
        result = BA.validate_batch(dict(_frame("valid-node-batch"), batch_id=bad),
                                   credential_device_id=GEN.DEVICE_ID)
        assert "batch_id_invalid" in result.reasons or "type_invalid" in result.reasons


# --- mixed-version window -----------------------------------------------------------------------

def test_legacy_telemetry_bodies_are_recognized_not_refused():
    # This is the compatibility hinge. Today every node emits `telemetry_path` bodies; if the
    # frame reader refused them, enabling batch ingest would delete a whole fleet's uplink.
    result = BA.validate_batch(_frame("mixed-version-legacy-messages"),
                               credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert all(r.classification == BA.TRANSLATION_REQUIRED for r in result.items)
    assert all(r.status == "deferred" for r in result.items)
    assert all(r.reasons == [] for r in result.items), "legacy is not an error"


def test_classification_is_shallow_and_total():
    assert BA.classify_item({"schema_version": 1}) == "canonical"
    assert BA.classify_item({"telemetry_path": "hear/heartbeat"}) == BA.TRANSLATION_REQUIRED
    assert BA.classify_item({"telemetry_schema_version": 1}) == BA.TRANSLATION_REQUIRED
    assert BA.classify_item({"anything": 1}) == "unrecognized"
    assert BA.classify_item(None) == "unrecognized"
    assert BA.classify_item([1]) == "unrecognized"


def test_a_batch_may_mix_canonical_and_legacy_items():
    frame = _frame("valid-node-batch")
    mixed = dict(frame, messages=[frame["messages"][0], GEN.LEGACY_HEARTBEAT])
    result = BA.validate_batch(mixed, credential_device_id=GEN.DEVICE_ID)
    assert result.ok
    assert result.items[0].status == "accepted"
    assert result.items[1].classification == BA.TRANSLATION_REQUIRED
    assert BA.ack_through_index(result.items) == 0


# --- idempotency ------------------------------------------------------------------------------

def test_same_key_same_body_is_a_retry_and_a_different_body_is_not():
    raw = EV.encode(_frame("valid-node-batch"))
    assert BA.request_fingerprint(raw) == BA.request_fingerprint(bytes(raw))
    other = EV.encode(_frame("poison-item-keeps-batch"))
    assert BA.request_fingerprint(raw) != BA.request_fingerprint(other)


def test_idempotency_scope_separates_principals_routes_and_sites():
    base = dict(site_id="site-quarry-north", principal="node:nyquist",
                route="POST /v1/ingest/batches", key="k1")
    scope = BA.idempotency_scope(**base)
    assert scope != BA.idempotency_scope(**dict(base, principal="node:mach"))
    assert scope != BA.idempotency_scope(**dict(base, route="POST /v1/clips:request"))
    assert scope != BA.idempotency_scope(**dict(base, site_id="site-south"))
    assert scope == BA.idempotency_scope(**base)


# --- refusal visibility --------------------------------------------------------------------------

def test_whole_frame_refusal_still_leaves_durable_evidence():
    raw = b'{"batch_schema_version": 1, "messages": ['
    record = BA.batch_refusal_record(raw, ["undecodable_body"], source="node-http",
                                     adapter="ingest-batch/0.1.0",
                                     received_at="2026-09-14T18:03:12Z",
                                     batch_id="018f2c1a-batch-0007",
                                     raw_ref="raw/quarantine/018f2c1c.json")
    assert record["schema_id"] == BA.BATCH_SCHEMA_ID
    assert record["batch_id"] == "018f2c1a-batch-0007"
    assert record["raw_bytes"] == len(raw)
    assert record["reasons"] == ["undecodable_body"]


def test_undecodable_and_non_object_bodies_raise_before_interpretation():
    with pytest.raises(BA.BatchError):
        BA.decode_batch(b"\x00\x01not json")
    with pytest.raises(BA.BatchError):
        BA.decode_batch(b"[1,2,3]")
    assert BA.validate_batch([1, 2, 3]).reasons == ["not_an_object"]


def test_batch_media_type_negotiation():
    assert BA.codec_for_media_type(
        "application/vnd.dama.hear.ingest.batch.v1+json") == "json"
    assert BA.codec_for_media_type("application/json; charset=utf-8") == "json"
    with pytest.raises(BA.BatchError):
        BA.codec_for_media_type("application/cbor")
    with pytest.raises(BA.BatchError):
        BA.decode_batch(b"{}", codec="protobuf")


def test_frame_timestamps_are_rfc3339_utc_or_absent():
    frame = _frame("valid-node-batch")
    assert BA.validate_batch(dict(frame, sent_at=None),
                             credential_device_id=GEN.DEVICE_ID).ok
    bad = BA.validate_batch(dict(frame, sent_at="2026-09-14T18:03:11+00:00"),
                            credential_device_id=GEN.DEVICE_ID)
    assert "timestamp_not_rfc3339_utc" in bad.reasons
