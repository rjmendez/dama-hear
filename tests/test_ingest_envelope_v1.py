"""Compatibility tests for the canonical `hear.ingest.v1` envelope.

The point of these is mixed-version behavior: a fleet is never uniformly upgraded, and a
rollback must not orphan or duplicate data that a newer writer already produced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hear.ingest import envelope as EV
from tools import gen_ingest_contracts as GEN

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "contracts" / "fixtures" / "hear.ingest.v1"
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
ENTRIES = MANIFEST["fixtures"]


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / ("%s.json" % name)).read_text(encoding="utf-8"))


# --- generated artifacts stay in step with the field table --------------------------------

def test_checked_in_contracts_match_generator():
    assert GEN.stale_artifacts() == []


def test_schema_document_declares_codec_and_identity():
    doc = EV.schema_document()
    assert doc["x-schema-major"] == EV.SCHEMA_MAJOR
    assert doc["x-codecs"] == {"json": "application/vnd.dama.hear.ingest.v1+json"}
    assert doc["additionalProperties"] is True, "unknown fields must survive a v1 reader"
    assert doc["properties"]["schema_version"]["const"] == 1
    # Enums are advertised, not constrained: constraining them would make a newer writer's
    # member a hard schema failure instead of a non-dispatchable record.
    assert "enum" not in doc["properties"]["kind"]
    assert doc["properties"]["kind"]["x-known-values"] == list(EV.KINDS)


# --- fixture conformance ------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_fixture_matches_declared_outcome(entry):
    env = _load(entry["name"])
    result = EV.validate(env)
    assert result.status == entry["expect_status"]
    assert result.dispatchable == entry["expect_dispatchable"]
    assert sorted(set(result.reasons)) == entry["expect_reasons"]
    assert sorted(result.unknown_fields) == entry["expect_unknown_fields"]


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_fixture_round_trips_through_the_codec(entry):
    env = _load(entry["name"])
    assert EV.decode(EV.encode(env)) == env


def test_accepted_fixtures_carry_derived_event_ids():
    for entry in ENTRIES:
        if entry["expect_status"] != "accepted":
            continue
        env = _load(entry["name"])
        assert EV.verify_event_id(env), entry["name"]


# --- identity is closed --------------------------------------------------------------------

def test_additive_fields_do_not_change_event_id():
    new_writer = _load("forward-additive-unknown-fields")
    old_writer = _load("forward-additive-stripped")
    assert new_writer != old_writer
    assert new_writer["event_id"] == old_writer["event_id"]
    assert EV.derive_event_id(new_writer) == EV.derive_event_id(old_writer)


def test_identity_inputs_change_event_id():
    base = _load("valid-node-detection")
    for path, value in (("device_id", "mach"), ("kind", "scene"),
                        ("observed_at", "2026-09-14T18:03:11.251000Z"),
                        ("source", "import")):
        mutated = dict(base, **{path: value})
        assert EV.derive_event_id(mutated) != base["event_id"], path
    seq = dict(base, producer=dict(base["producer"], sequence=4472))
    assert EV.derive_event_id(seq) != base["event_id"]
    payload = dict(base, payload=dict(base["payload"], peak_db=-12.4))
    assert EV.derive_event_id(payload) != base["event_id"]


def test_event_id_is_stable_across_key_order_and_reencoding():
    env = _load("valid-node-detection")
    shuffled = dict(reversed(list(env.items())))
    assert EV.derive_event_id(shuffled) == env["event_id"]
    assert EV.derive_event_id(EV.decode(EV.encode(shuffled))) == env["event_id"]


def test_identity_inputs_are_frozen():
    # A change here is a major version change, not a patch. Failing loudly is the point.
    assert EV.IDENTITY_INPUTS == (
        "source", "device_id", "kind", "observed_at", "producer.boot_id",
        "producer.sequence", "producer.cursor", "payload",
    )


# --- forward, backward and rollback behavior ----------------------------------------------

def test_old_reader_accepts_new_writer_additive_fields():
    result = EV.validate(_load("forward-additive-unknown-fields"))
    assert result.ok
    assert set(result.unknown_fields) == {"ingest_hints", "clock.holdover_s", "adapter.commit"}


def test_unknown_enum_member_is_stored_but_not_dispatched():
    result = EV.validate(_load("forward-additive-unknown-fields"))
    assert result.ok and not result.dispatchable
    assert any(w["reason"] == "unknown_enum_value" and w["value"] == "seismic"
               for w in result.warnings)


def test_new_reader_accepts_old_writer_without_producer_block():
    env = _load("minimal-legacy-producer")
    assert "producer" not in env
    result = EV.validate(env)
    assert result.ok and result.dispatchable
    assert EV.verify_event_id(env)


def test_unsupported_major_is_refused_not_guessed():
    result = EV.validate(_load("unsupported-future-major"))
    assert result.status == "refused"
    assert result.reasons == ["schema_version_unsupported"]
    # Refusal happens before field interpretation, so no v2 field is read as if it were v1.
    assert result.errors[0]["supported"] == EV.SCHEMA_MAJOR


def test_v2_body_reusing_a_v1_name_is_not_reinterpreted():
    hostile = dict(_load("valid-node-detection"), schema_version=2,
                   clock={"valid": True, "tier": "gps_pps", "sigma_ns": "unknown-unit"})
    result = EV.validate(hostile)
    assert result.reasons == ["schema_version_unsupported"]


def test_rollback_keeps_unknown_fields_available_for_replay():
    # Preservation, not tolerance: an adapter that dropped unknown fields would silently
    # destroy data written before a rollback.
    env = _load("forward-additive-unknown-fields")
    assert json.loads(EV.encode(env).decode("utf-8")) == env


def test_strict_unknown_is_producer_side_only():
    env = _load("forward-additive-unknown-fields")
    assert EV.validate(env).ok
    assert not EV.validate(env, strict_unknown=True).ok


# --- refusal semantics ---------------------------------------------------------------------

def test_missing_required_field_is_refused_with_a_path():
    result = EV.validate(_load("malformed-missing-clock"))
    assert result.status == "refused"
    assert {"reason": "field_missing", "path": "clock"} in result.errors


def test_wrong_types_are_refused_rather_than_coerced():
    result = EV.validate(_load("malformed-field-types"))
    reasons = {e["reason"] for e in result.errors}
    assert {"type_invalid", "timestamp_not_rfc3339_utc", "value_out_of_range"} <= reasons
    paths = {e["path"] for e in result.errors}
    assert {"payload", "clock.valid", "clock.sigma_ns", "received_at"} <= paths


def test_valid_clock_without_observed_at_is_refused():
    result = EV.validate(_load("malformed-clock-valid-without-time"))
    assert result.reasons == ["clock_valid_without_observed_at"]


def test_degraded_node_without_anchor_is_accepted():
    env = _load("degraded-no-clock-anchor")
    result = EV.validate(env)
    assert result.ok
    assert env["observed_at"] is None and env["clock"]["valid"] is False


def test_non_object_and_non_json_bodies_are_refused():
    assert EV.validate([1, 2, 3]).reasons == ["not_an_object"]
    with pytest.raises(EV.EnvelopeError):
        EV.decode(b"\x00\x01not json")
    with pytest.raises(EV.EnvelopeError):
        EV.decode(b"[1,2,3]")


def test_schema_version_must_be_an_integer():
    env = dict(_load("valid-node-detection"), schema_version="1")
    assert "schema_version_invalid" in EV.validate(env).reasons
    env = dict(_load("valid-node-detection"), schema_version=True)
    assert "schema_version_invalid" in EV.validate(env).reasons


def test_refusal_record_preserves_evidence():
    raw = b'{"broken":'
    record = EV.refusal_record(raw, ["undecodable_body"], source="node-http",
                               adapter="node-http/1.0.0",
                               received_at="2026-09-14T18:03:12Z",
                               raw_ref="raw/quarantine/018f2c1c.json")
    assert record["raw_bytes"] == len(raw)
    assert record["raw_sha256"] == __import__("hashlib").sha256(raw).hexdigest()
    assert record["reasons"] == ["undecodable_body"]
    assert record["raw_ref"].startswith("raw/quarantine/")


# --- codec seam ------------------------------------------------------------------------------

def test_only_json_is_registered_for_v1():
    assert list(EV.CODEC_MEDIA_TYPES) == ["json"]
    with pytest.raises(EV.EnvelopeError):
        EV.encode(_load("valid-node-detection"), codec="cbor")
    with pytest.raises(EV.EnvelopeError):
        EV.decode(b"{}", codec="protobuf")


def test_media_type_negotiation():
    assert EV.codec_for_media_type("application/vnd.dama.hear.ingest.v1+json") == "json"
    assert EV.codec_for_media_type("application/json; charset=utf-8") == "json"
    with pytest.raises(EV.EnvelopeError):
        EV.codec_for_media_type("application/cbor")


def test_canonical_bytes_are_codec_independent_and_finite():
    env = _load("valid-node-detection")
    assert EV.canonical_bytes(env) == EV.canonical_bytes(EV.decode(EV.encode(env)))
    with pytest.raises(EV.EnvelopeError):
        EV.canonical_bytes({"x": float("nan")})
    with pytest.raises(EV.EnvelopeError):
        EV.encode(dict(env, payload={"peak_db": float("inf")}))


def test_binary_detection_frames_travel_as_opaque_payload():
    # The versioned binary frame keeps its own version/profile; the envelope never guesses
    # at its geometry, which is what hear/wire.py refuses to do too.
    payload = _load("valid-node-detection")["payload"]
    assert payload["wire_version"] == 2 and "frame_b64" in payload
