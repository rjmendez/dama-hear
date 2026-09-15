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
