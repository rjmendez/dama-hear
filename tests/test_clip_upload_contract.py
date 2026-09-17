"""hear.ingest.clip.v1: the chunked WAV upload contract, held to its own declarations.

Push-only clip ingestion has to be buildable from two sides at once -- a Python receiver and an
ESP32 that cannot import it -- so the constants live twice and are asserted equal here. The rest
of the file pins the parts that are cheap to state and expensive to get wrong: identity is the
clip name and not the bytes, a chunk index is an offset, and no path exists from received audio
to the corpus that does not pass a speech verdict.
"""
import pathlib
import re

import pytest

from hear import clips as CL
from hear.ingest import clipupload as CU

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = (ROOT / "firmware" / "hear_node" / "clip_upload.h").read_text()
DOC = (ROOT / "docs" / "phase4-push-clip-upload.md").read_text()


def _define(name):
    m = re.search(r"^#define\s+%s\s+(\S+)\s*$" % re.escape(name), HEADER, re.M)
    assert m is not None, f"{name} missing from clip_upload.h"
    return m.group(1)


def _define_int(name):
    return int(_define(name).rstrip("uU"))


def _define_str(name):
    value = _define(name)
    assert value.startswith('"') and value.endswith('"'), name
    return value[1:-1]


# --------------------------------------------------------------- identity, not content

def test_upload_id_is_the_clip_key():
    """The upload joins the existing clip index by identity, so a retry is not a second clip."""
    assert CU.upload_id("nyquist", "a1b2c3", 12345) == CL.clip_key("nyquist", "a1b2c3", 12345)


def test_upload_id_ignores_the_bytes():
    """Identity is known before a byte is uploaded; that is what makes resume cheap."""
    assert CU.upload_id("gold", "0f0f", 1) != CU.upload_id("gold", "0f0f", 2)


def test_idempotency_keys_are_derived_and_stable():
    upload = CU.upload_id("ageev", "beef", 7)
    assert CU.init_idempotency_key("ageev", upload) == CU.init_idempotency_key("ageev", upload)
    sha = "ab" * 32
    key = CU.complete_idempotency_key("ageev", upload, sha)
    assert key != CU.complete_idempotency_key("ageev", upload, "cd" * 32)
    assert len(key) <= 128


# --------------------------------------------------------------- chunking arithmetic

def test_the_nominal_clip_is_fifteen_chunks():
    assert CU.chunk_count(CU.NOMINAL_CLIP_BYTES) == 15
    assert CU.expected_chunk_bytes(0, CU.NOMINAL_CLIP_BYTES) == CU.CHUNK_BYTES
    assert CU.expected_chunk_bytes(14, CU.NOMINAL_CLIP_BYTES) == 21_292


def test_chunk_spans_tile_the_clip_exactly():
    total = CU.chunk_count(CU.NOMINAL_CLIP_BYTES)
    spans = [CU.chunk_span(i, CU.NOMINAL_CLIP_BYTES) for i in range(total)]
    assert spans[0][0] == 0 and spans[-1][1] == CU.NOMINAL_CLIP_BYTES
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert sum(e - s for s, e in spans) == CU.NOMINAL_CLIP_BYTES


def test_a_clip_over_the_bound_is_refused_before_any_byte():
    with pytest.raises(ValueError, match="clip_bytes_invalid"):
        CU.chunk_count(CU.MAX_CLIP_BYTES + 1)
    with pytest.raises(ValueError, match="chunk_bytes_invalid"):
        CU.chunk_count(CU.NOMINAL_CLIP_BYTES, CU.MAX_CHUNK_BYTES + 1)
    with pytest.raises(ValueError, match="chunk_index_out_of_range"):
        CU.chunk_span(15, CU.NOMINAL_CLIP_BYTES)


def test_the_chunk_bound_covers_the_largest_clip():
    assert CU.MAX_CHUNKS >= CU.chunk_count(CU.MAX_CLIP_BYTES)
    assert CU.MAX_CLIP_BYTES > CU.NOMINAL_CLIP_BYTES


# --------------------------------------------------------------- the privacy seam

def test_only_a_verdict_leaves_scoring():
    assert CU.TRANSITIONS[CU.STATE_SCORING] == (CU.STATE_PROMOTED, CU.STATE_PURGED)


def test_no_path_reaches_promoted_without_scoring():
    """Every edge into `promoted` starts at `scoring`. Promotion cannot be reached from a
    freshly assembled upload, from a chunk write, or from a resumed session."""
    sources = [s for s, nxt in CU.TRANSITIONS.items() if CU.STATE_PROMOTED in nxt]
    assert sources == [CU.STATE_SCORING]


def test_speech_and_unscored_both_end_in_purge():
    assert CU.terminal_state_for_verdict("SPEECH_DETECTED") == CU.STATE_PURGED
    assert CU.terminal_state_for_verdict("NOT_SCORED") == CU.STATE_PURGED
    assert CU.terminal_state_for_verdict("NO_SPEECH") == CU.STATE_PROMOTED


def test_an_unknown_verdict_fails_closed():
    assert CU.terminal_state_for_verdict("probably_fine") == CU.STATE_PURGED


def test_terminal_states_have_no_outgoing_edges():
    for state in CU.TERMINAL_STATES:
        assert CU.TRANSITIONS[state] == (), state
        assert CU.is_terminal(state)
    assert not any(CU.is_terminal(s) for s in
                   (CU.STATE_OPEN, CU.STATE_RECEIVING, CU.STATE_ASSEMBLING, CU.STATE_SCORING))


def test_a_purged_clip_key_is_terminal_and_refusable():
    assert CU.STATE_PURGED in CU.CLIP_KEY_TERMINAL_STATES
    assert "clip_key_purged" in CU.REFUSAL_REASONS


def test_audio_upload_needs_its_own_scope():
    assert "clip:write" in CU.REQUIRED_SCOPES and "ingest:write" in CU.REQUIRED_SCOPES


def test_every_transition_target_is_a_declared_state():
    for state, targets in CU.TRANSITIONS.items():
        assert state in CU.STATES
        assert all(t in CU.STATES for t in targets), state
    assert set(CU.TRANSITIONS) == set(CU.STATES)


# --------------------------------------------------------------- frame validation

def _init_body(**over):
    body = {
        "upload_schema_version": 1,
        "device_id": "gold",
        "node": "gold",
        "boot": "a1b2c3",
        "sample": 4242,
        "clip_basename": "gold-a1b2c3-4242.wav",
        "clip_bytes": CU.NOMINAL_CLIP_BYTES,
        "chunk_bytes": CU.CHUNK_BYTES,
        "upload_source": "psram_ring",
        "upload_id": CU.upload_id("gold", "a1b2c3", 4242),
    }
    body.update(over)
    return body


def test_a_well_formed_init_is_admissible_from_a_no_sd_node():
    assert CU.validate_init(_init_body(), credential_device_id="gold") is None


def test_an_sd_node_declares_its_own_source():
    body = _init_body(upload_source="sd")
    assert CU.validate_init(body, credential_device_id="gold") is None


@pytest.mark.parametrize("over,reason", [
    ({"upload_schema_version": 2}, "upload_schema_version_unsupported"),
    ({"device_id": "nyquist"}, "device_identity_mismatch"),
    ({"upload_source": "carrier_pigeon"}, "upload_source_unknown"),
    ({"clip_bytes": CU.MAX_CLIP_BYTES + 1}, "clip_bytes_invalid"),
    ({"clip_bytes": 0}, "clip_bytes_invalid"),
    ({"chunk_bytes": CU.MAX_CHUNK_BYTES * 2}, "chunk_bytes_invalid"),
    ({"clip_basename": ""}, "clip_name_invalid"),
    ({"sample": -1}, "clip_name_invalid"),
    ({"upload_id": "0" * 32}, "clip_key_mismatch"),
    ({"upload_id": "not-a-key"}, "clip_key_mismatch"),
])
def test_init_refusals_are_named(over, reason):
    assert CU.validate_init(_init_body(**over), credential_device_id="gold") == reason
    assert reason in CU.REFUSAL_REASONS


def test_init_without_a_credential_is_never_a_free_pass():
    assert CU.validate_init(_init_body(), credential_device_id=None) == "credential_missing"


def test_complete_requires_every_byte_and_a_body_hash():
    ok = {"upload_schema_version": 1, "sha256": "ab" * 32,
          "clip_bytes": CU.NOMINAL_CLIP_BYTES}
    assert CU.validate_complete(ok, bytes_received=CU.NOMINAL_CLIP_BYTES, chunks_received=15,
                                expected_chunks=15, clip_bytes=CU.NOMINAL_CLIP_BYTES) is None
    assert CU.validate_complete(ok, bytes_received=CU.NOMINAL_CLIP_BYTES - 1, chunks_received=14,
                                expected_chunks=15,
                                clip_bytes=CU.NOMINAL_CLIP_BYTES) == "clip_bytes_incomplete"
    bad = dict(ok, sha256="nope")
    assert CU.validate_complete(bad, bytes_received=CU.NOMINAL_CLIP_BYTES, chunks_received=15,
                                expected_chunks=15,
                                clip_bytes=CU.NOMINAL_CLIP_BYTES) == "clip_sha256_mismatch"


# --------------------------------------------------------------- firmware/Python parity

def test_firmware_header_mirrors_the_python_limits():
    assert _define_int("HEAR_CLIP_CHUNK_BYTES") == CU.CHUNK_BYTES
    assert _define_int("HEAR_CLIP_MAX_CHUNK_BYTES") == CU.MAX_CHUNK_BYTES
    assert _define_int("HEAR_CLIP_MAX_CLIP_BYTES") == CU.MAX_CLIP_BYTES
    assert _define_int("HEAR_CLIP_MAX_CHUNKS") == CU.MAX_CHUNKS
    assert _define_int("HEAR_CLIP_NOMINAL_CLIP_BYTES") == CU.NOMINAL_CLIP_BYTES
    assert _define_int("HEAR_CLIP_UPLOAD_TTL_S") == CU.UPLOAD_TTL_S
    assert _define_int("HEAR_CLIP_MAX_OPEN_UPLOADS") == CU.MAX_OPEN_UPLOADS_PER_DEVICE
    assert _define_int("HEAR_CLIP_UPLOAD_SCHEMA_VERSION") == CU.CLIP_UPLOAD_SCHEMA_MAJOR


def test_firmware_header_mirrors_the_python_wire_strings():
    assert _define_str("HEAR_CLIP_INIT_PATH") == CU.INIT_ROUTE
    assert _define_str("HEAR_CLIP_INIT_MEDIA_TYPE") == CU.INIT_MEDIA_TYPE
    assert _define_str("HEAR_CLIP_COMPLETE_MEDIA_TYPE") == CU.COMPLETE_MEDIA_TYPE
    assert _define_str("HEAR_CLIP_STATUS_MEDIA_TYPE") == CU.STATUS_MEDIA_TYPE
    assert _define_str("HEAR_CLIP_CHUNK_MEDIA_TYPE") == CU.CHUNK_MEDIA_TYPE
    assert _define_str("HEAR_CLIP_CHUNK_DIGEST_HEADER") == CU.CHUNK_DIGEST_HEADER
    assert _define_str("HEAR_CLIP_UPLOAD_SOURCE_RING") == "psram_ring"
    assert _define_str("HEAR_CLIP_UPLOAD_SOURCE_SD") == "sd"
    assert set(CU.UPLOAD_SOURCES) == {"psram_ring", "sd"}


def test_firmware_chunk_path_formats_match_the_route_templates():
    assert _define_str("HEAR_CLIP_CHUNK_PATH_FMT") == \
        CU.CHUNK_ROUTE_TEMPLATE.replace("{upload_id}", "%s").replace("{chunk_index}", "%u")
    assert _define_str("HEAR_CLIP_COMPLETE_PATH_FMT") == \
        CU.COMPLETE_ROUTE_TEMPLATE.replace("{upload_id}", "%s")
    assert _define_str("HEAR_CLIP_STATUS_PATH_FMT") == \
        CU.UPLOAD_ROUTE_TEMPLATE.replace("{upload_id}", "%s")


def test_the_upload_id_buffer_holds_a_clip_key():
    assert _define_int("HEAR_CLIP_UPLOAD_ID_MAX") >= len(CU.upload_id("gold", "a", 1)) + 1


def test_the_firmware_never_needs_a_whole_clip_in_ram():
    """The point of chunking for a no-SD node: the largest buffer is one chunk, not 480 kB."""
    assert CU.MAX_CHUNK_BYTES * 16 <= CU.MAX_CLIP_BYTES


# --------------------------------------------------------------- the doc is part of the contract

def test_the_contract_doc_states_every_route_and_state():
    for method, route in CU.ROUTES:
        assert f"{method} {route}" in DOC, (method, route)
    for state in CU.STATES:
        assert f"`{state}`" in DOC, state


def test_the_contract_doc_names_the_quarantine_invariant():
    assert "/pool/corpus/clips" in DOC
    assert "silero-vad-privacy-contract.md" in DOC
