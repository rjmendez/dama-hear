"""ADR 0011's device trust bundle is bounded, parseable and release-verifiable."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))

import release_ca_bundle as rcb  # noqa: E402


def test_the_tracked_bundle_matches_adr_0011_scope_and_the_header_default():
    bundle_text, metadata = rcb.build_ca_bundle(ROOT)
    rcb.verify_header_matches_bundle(bundle_text)
    assert metadata["target_macro"] == "HEAR_PUSH_CA_CERT"
    assert metadata["state"] == "steady-state"
    assert metadata["certificate_count"] == 1
    assert metadata["steady_state_max_certificates"] == 1
    assert metadata["overlap_max_certificates"] == 2
    assert metadata["applies_to"] == [
        "current HTTPS push path through HEAR_PUSH_HOST",
        "future /v1/ingest/batches device batch client",
    ]
    assert [c["name"] for c in metadata["certificates"]] == ["amazon-root-ca-1"]


def test_generate_and_verify_are_deterministic_and_emit_valid_pem(tmp_path):
    first = rcb.write_release_artifacts(ROOT, tmp_path)
    second = rcb.write_release_artifacts(ROOT, tmp_path)
    assert first == second
    assert (tmp_path / rcb.BUNDLE_NAME).read_text(encoding="utf-8").startswith(
        "-----BEGIN CERTIFICATE-----\n")
    checked = rcb.verify_release_artifacts(ROOT, tmp_path)
    assert checked["sha256"] == first["sha256"]


def test_build_refuses_a_missing_cert_source(tmp_path):
    spec = json.loads(rcb.SPEC_PATH.read_text(encoding="utf-8"))
    spec["certificates"][0]["path"] = "nope.pem"
    bad = tmp_path / "bad-spec.json"
    bad.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        rcb.build_ca_bundle(tmp_path, bad)


def test_build_refuses_a_malformed_cert_source(tmp_path):
    malformed = tmp_path / "bad.pem"
    malformed.write_text("-----BEGIN CERTIFICATE-----\nnot-base64\n-----END CERTIFICATE-----\n",
                         encoding="utf-8")
    spec = json.loads(rcb.SPEC_PATH.read_text(encoding="utf-8"))
    spec["certificates"][0]["path"] = "bad.pem"
    spec["certificates"][0]["sha256"] = "0" * 64
    bad = tmp_path / "bad-spec.json"
    bad.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="valid PEM certificate"):
        rcb.build_ca_bundle(tmp_path, bad)


def test_verify_refuses_bundle_metadata_or_pem_drift(tmp_path):
    rcb.write_release_artifacts(ROOT, tmp_path)
    (tmp_path / rcb.BUNDLE_NAME).write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    with pytest.raises(ValueError, match="valid PEM CA bundle|tracked certificate bundle"):
        rcb.verify_release_artifacts(ROOT, tmp_path)
