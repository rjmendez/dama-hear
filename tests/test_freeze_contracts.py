"""Phase 0 freeze tooling stays reproducible and metadata-only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "freeze_contracts.py"
JSON_OUT = ROOT / "docs" / "data" / "phase0-freeze-contracts.v1.json"
MD_OUT = ROOT / "docs" / "phase0-freeze-contracts.v1.md"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def test_json_output_matches_the_checked_in_baseline():
    got = _run("--format", "json")
    assert got.returncode == 0, got.stderr
    assert got.stdout == JSON_OUT.read_text()


def test_markdown_output_matches_the_checked_in_baseline():
    got = _run("--format", "markdown")
    assert got.returncode == 0, got.stderr
    assert got.stdout == MD_OUT.read_text()


def test_check_mode_passes_against_the_checked_in_files():
    got = _run("--check")
    assert got.returncode == 0, got.stderr
    assert got.stdout == ""


def test_generated_inventory_stays_metadata_only_for_sensitive_fixtures():
    got = _run("--format", "json")
    assert got.returncode == 0, got.stderr
    doc = json.loads(got.stdout)
    payload = json.dumps(doc, sort_keys=True)
    assert "40.2924768" not in payload
    assert "-79.1221561" not in payload
    assert "172.16.100.105" not in payload
    assert "api.botnet.floppydicks.net" not in payload
    files = {item["path"]: item for item in doc["corpus_fixture_metadata"]["files"]}
    assert files["tests/fixtures/status_nyquist.json"]["summary"]["type"] == "object"
    assert "top_level_keys" in files["tests/fixtures/status_nyquist.json"]["summary"]


def _load_tool():
    sys.path.insert(0, str(ROOT))
    from tools import freeze_contracts

    return freeze_contracts


def test_spatial_mqtt_default_is_read_from_source_not_a_sentinel():
    tool = _load_tool()
    got = tool.extract_default_string("hear/spatial.py", "MQTT_SPATIAL_TOPIC", "SENTINEL")
    assert got != "SENTINEL"
    assert got == "dama/hear/spatial_events"
    assert got in (ROOT / "hear" / "spatial.py").read_text()
    topics = tool.mqtt_topics()["topics"]
    assert any(t["topic_pattern"] == got for t in topics)


def test_missing_default_raises_instead_of_silently_falling_back():
    tool = _load_tool()
    try:
        tool.extract_default_string("hear/spatial.py", "NO_SUCH_ENV_KEY_FOR_TESTS")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an absent env default")


def test_changing_the_real_spatial_default_changes_the_frozen_baseline():
    source = ROOT / "hear" / "spatial.py"
    original = source.read_bytes()
    before = json.loads(_run("--format", "json").stdout)
    try:
        source.write_bytes(
            original.replace(
                b'os.environ.get("MQTT_SPATIAL_TOPIC", "dama/hear/spatial_events")',
                b'os.environ.get("MQTT_SPATIAL_TOPIC", "dama/hear/spatial_events_v2")',
            )
        )
        assert source.read_bytes() != original, "fixture edit did not apply"
        mutated = _run("--format", "json")
        assert mutated.returncode == 0, mutated.stderr
        after = json.loads(mutated.stdout)
    finally:
        source.write_bytes(original)

    patterns = {t["topic_pattern"] for t in after["mqtt_topics"]["topics"]}
    assert "dama/hear/spatial_events_v2" in patterns
    assert after["mqtt_topics"]["section_hash"] != before["mqtt_topics"]["section_hash"]
    assert after["baseline_hash"] != before["baseline_hash"]


CONTRACT_ID = "hear.ingest.v1"
SCHEMA_ARTIFACT = ROOT / "contracts" / "schemas" / ("%s.schema.json" % CONTRACT_ID)
FIXTURE_DIR = ROOT / "contracts" / "fixtures" / CONTRACT_ID
FIXTURE_ARTIFACT = FIXTURE_DIR / "valid-node-detection.json"
FIXTURE_MANIFEST = FIXTURE_DIR / "manifest.json"


def _published(doc):
    section = doc["published_contracts"]
    return {row["contract_id"]: row for row in section["contracts"]}


def _mutate_and_check(path: Path, mutate):
    """Apply a byte-level edit, return (freeze --check result, mutated snapshot), restore."""
    original = path.read_bytes()
    try:
        path.write_bytes(mutate(original))
        assert path.read_bytes() != original, "fixture edit did not apply"
        checked = _run("--check")
        regenerated = _run("--format", "json")
        assert regenerated.returncode == 0, regenerated.stderr
        return checked, json.loads(regenerated.stdout)
    finally:
        path.write_bytes(original)


def test_generated_contract_artifacts_are_inside_the_frozen_scope():
    doc = json.loads(_run("--format", "json").stdout)
    section = doc["published_contracts"]
    assert section["present"] is True
    row = _published(doc)[CONTRACT_ID]
    assert row["schema_file"]["path"] == "contracts/schemas/%s.schema.json" % CONTRACT_ID
    assert row["manifest_file"]["path"] == (
        "contracts/fixtures/%s/manifest.json" % CONTRACT_ID
    )
    frozen = {item["path"] for item in row["fixture_files"]}
    on_disk = {
        p.relative_to(ROOT).as_posix()
        for p in FIXTURE_DIR.glob("*.json")
        if p.name != "manifest.json"
    }
    assert frozen == on_disk and frozen
    assert row["manifest_declared"]["schema_id"] == CONTRACT_ID


def test_frozen_artifact_digests_match_the_bytes_on_disk():
    tool = _load_tool()
    row = _published(json.loads(_run("--format", "json").stdout))[CONTRACT_ID]
    for item in [row["schema_file"], row["manifest_file"], *row["fixture_files"]]:
        raw = (ROOT / item["path"]).read_bytes()
        assert item["sha256"] == tool.sha256_bytes(raw)
        assert item["size_bytes"] == len(raw)


def test_generated_schema_drift_fails_the_freeze_check():
    before = json.loads(_run("--format", "json").stdout)
    checked, after = _mutate_and_check(
        SCHEMA_ARTIFACT,
        lambda raw: raw.replace(b'"title": "hear.ingest.v1"', b'"title": "hear.ingest.v1 "'),
    )
    assert checked.returncode == 1, "freeze check passed despite a mutated schema artifact"
    assert "phase0-freeze-contracts.v1.json is stale" in checked.stderr
    assert (after["published_contracts"]["section_hash"]
            != before["published_contracts"]["section_hash"])
    assert after["baseline_hash"] != before["baseline_hash"]


def test_generated_fixture_drift_fails_the_freeze_check():
    before = json.loads(_run("--format", "json").stdout)
    checked, after = _mutate_and_check(
        FIXTURE_ARTIFACT,
        lambda raw: raw.replace(b'"device_id": "nyquist"', b'"device_id": "nyquist-2"'),
    )
    assert checked.returncode == 1, "freeze check passed despite a mutated fixture payload"
    assert (after["published_contracts"]["section_hash"]
            != before["published_contracts"]["section_hash"])
    assert after["baseline_hash"] != before["baseline_hash"]


def test_fixture_manifest_drift_fails_the_freeze_check():
    before = json.loads(_run("--format", "json").stdout)
    checked, after = _mutate_and_check(
        FIXTURE_MANIFEST,
        lambda raw: raw.replace(b'"expect_dispatchable": true', b'"expect_dispatchable": false'),
    )
    assert checked.returncode == 1, "freeze check passed despite a mutated fixture manifest"
    row = _published(after)[CONTRACT_ID]
    declared = {f["name"]: f["expect_dispatchable"] for f in row["manifest_declared"]["fixtures"]}
    assert declared["valid-node-detection"] is False
    assert (after["published_contracts"]["section_hash"]
            != before["published_contracts"]["section_hash"])
    assert after["baseline_hash"] != before["baseline_hash"]


def test_a_new_generated_fixture_file_fails_the_freeze_check():
    extra = FIXTURE_DIR / "zz-unfrozen-fixture.json"
    assert not extra.exists()
    try:
        extra.write_text('{"event_id": "test-only"}\n', encoding="utf-8")
        checked = _run("--check")
        after = json.loads(_run("--format", "json").stdout)
    finally:
        extra.unlink()
    assert checked.returncode == 1, "freeze check passed despite an unfrozen artifact"
    paths = {item["path"] for item in _published(after)[CONTRACT_ID]["fixture_files"]}
    assert "contracts/fixtures/%s/zz-unfrozen-fixture.json" % CONTRACT_ID in paths
    assert _run("--check").returncode == 0


def test_published_artifacts_are_not_claimed_as_contract_sources_of_truth():
    """The layout gate reads schema_identifiers as sources of truth; generated output is not one."""
    doc = json.loads(_run("--format", "json").stdout)
    identifiers = {i["schema"]: i["source_paths"] for i in doc["schemas"]["schema_identifiers"]}
    assert ".schema.json" not in identifiers
    assert identifiers[CONTRACT_ID] == ["hear/ingest/envelope.py"]
    assert not any(p.startswith("contracts/") for paths in identifiers.values() for p in paths)
