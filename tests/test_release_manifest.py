"""Immutable hear_node release manifests are deterministic and offline-verifiable."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))
import board_profiles  # noqa: E402
import release_ca_bundle  # noqa: E402
import release_manifest  # noqa: E402


TAG = "v9.9.9"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _build_info():
    return {
        "tag": TAG,
        "commit": COMMIT,
        "sketch": "firmware/hear_node",
        "fqbn": board_profiles.FQBN,
        "esp32_core": "3.3.11",
        "arduino_cli": "1.5.1",
        "variants": [
            {
                "board_class": "xiao-s3-pps",
                "release_stem": "hear_node-xiao-s3-pps",
                "fqbn": board_profiles.FQBN,
                "psram_mode": "octal",
                "build_flags": ["-DHEAR_ALLOW_NO_WIFI"],
            },
            {
                "board_class": "esp32s3-i2s-gps",
                "release_stem": "hear_node-esp32s3-i2s-gps",
                "fqbn": board_profiles.FQBN,
                "psram_mode": "octal",
                "build_flags": ["-DHEAR_ALLOW_NO_WIFI", "-DHEAR_BOARD_ESP32S3_I2S_GPS"],
            },
            {
                "board_class": "esp32s3-i2s-gps",
                "release_stem": "hear_node-esp32s3-i2s-gps-qspi",
                "fqbn": board_profiles.PSRAM_MODES["quad"]["fqbn"],
                "psram_mode": "quad",
                "build_flags": ["-DHEAR_ALLOW_NO_WIFI", "-DHEAR_BOARD_ESP32S3_I2S_GPS"],
            },
        ],
        "credentials": "none compiled in; read from the node's NVS (enroll.py)",
    }


def _source_state(dirty=False):
    return {
        "repository": "rjmendez/dama-hear",
        "commit": COMMIT,
        "commit_short": COMMIT[:7],
        "describe": TAG if not dirty else TAG + "-dirty",
        "dirty": dirty,
        "dirty_paths": ["firmware/hear_node/hear_node.ino"] if dirty else [],
    }


def _write_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    for board_class, psram_mode in (
        ("xiao-s3-pps", "octal"),
        ("esp32s3-i2s-gps", "octal"),
        ("esp32s3-i2s-gps", "quad"),
    ):
        for kind in release_manifest.UPLOAD_KINDS:
            name = board_profiles.release_asset_name(TAG, board_class, kind, psram_mode)
            (dist / name).write_bytes(("%s:%s:%s\n" % (board_class, psram_mode, kind)).encode("utf-8"))
    build_info = dist / "build-info.json"
    build_info.write_text(json.dumps(_build_info(), indent=2) + "\n", encoding="utf-8")
    return dist, build_info


def test_git_source_state_ignores_generated_release_outputs(monkeypatch):
    def fake_git(_root, *args):
        table = {
            ("rev-parse", "HEAD"): COMMIT,
            ("rev-parse", "--short", "HEAD"): COMMIT[:7],
            ("describe", "--always", "--tags"): TAG,
            ("status", "--porcelain", "--untracked-files=all"): "\n".join([
                "?? dist/build-info.json",
                "?? dist/release-manifest.json",
            ]),
        }
        return table[args]

    monkeypatch.setattr(release_manifest, "_git", fake_git)
    state = release_manifest.git_source_state(ROOT, ignore_paths=("dist",))
    assert state["dirty"] is False
    assert state["describe"] == TAG


def test_generation_is_deterministic(monkeypatch, tmp_path):
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state())
    release_manifest.write_manifest(ROOT, dist, build_info)
    first_manifest = (dist / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    first_schema = (dist / release_manifest.SCHEMA_NAME).read_text(encoding="utf-8")
    release_manifest.write_manifest(ROOT, dist, build_info)
    assert (dist / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8") == first_manifest
    assert (dist / release_manifest.SCHEMA_NAME).read_text(encoding="utf-8") == first_schema


def test_verify_fails_when_an_artifact_is_tampered(monkeypatch, tmp_path):
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state())
    release_manifest.write_manifest(ROOT, dist, build_info)
    victim = dist / board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
    victim.write_bytes(b"x" * victim.stat().st_size)
    with pytest.raises(ValueError, match="sha256"):
        release_manifest.verify_release_directory(dist / release_manifest.MANIFEST_NAME, dist, expected_tag=TAG)


def _generated_manifest(monkeypatch, tmp_path):
    """A real, schema-valid manifest plus the dist dir it describes."""
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state())
    manifest = release_manifest.write_manifest(ROOT, dist, build_info)
    return manifest, dist


def test_verify_fails_when_a_release_profile_does_not_match_the_asset(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    name = board_profiles.release_asset_name(TAG, "esp32s3-i2s-gps", "app")
    data = (dist / name).read_bytes()
    with pytest.raises(ValueError, match="does not declare"):
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "xiao-s3-pps", {name: data})


def test_downloaded_assets_verify_against_a_well_formed_manifest(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    assert not release_manifest.manifest_schema_problems(manifest)
    names = [board_profiles.release_asset_name(TAG, "xiao-s3-pps", kind)
             for kind in ("app", "bootloader")]
    assets = {name: (dist / name).read_bytes() for name in names}
    summary = release_manifest.verify_downloaded_release_assets(
        json.dumps(manifest), TAG, "xiao-s3-pps", assets)
    assert summary["tag"] == TAG
    assert summary["commit"] == COMMIT
    assert summary["verified_assets"] == sorted(names)


def test_qspi_assets_verify_only_against_the_quad_variant(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    name = board_profiles.release_asset_name(TAG, "esp32s3-i2s-gps", "app", "quad")
    data = (dist / name).read_bytes()
    summary = release_manifest.verify_downloaded_release_assets(
        json.dumps(manifest), TAG, "esp32s3-i2s-gps", {name: data}, psram_mode="quad")
    assert summary["psram_mode"] == "quad"
    with pytest.raises(ValueError, match="does not declare"):
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "esp32s3-i2s-gps", {name: data}, psram_mode="octal")


@pytest.mark.parametrize("section", ["build", "inputs", "variants", "source"])
def test_manifest_missing_a_required_section_is_refused(monkeypatch, tmp_path, section):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
    assets = {name: (dist / name).read_bytes()}
    manifest.pop(section)
    with pytest.raises(ValueError, match="does not match release-manifest.schema.json"):
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "xiao-s3-pps", assets)


def test_manifest_artifact_without_a_name_is_refused(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
    assets = {name: (dist / name).read_bytes()}
    variant = next(v for v in manifest["variants"] if v["board_class"] == "xiao-s3-pps")
    variant["artifacts"][0].pop("name")
    with pytest.raises(ValueError) as excinfo:
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "xiao-s3-pps", assets)
    assert "name" in str(excinfo.value)


def test_manifest_artifact_with_a_malformed_hash_is_refused(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
    assets = {name: (dist / name).read_bytes()}
    variant = next(v for v in manifest["variants"] if v["board_class"] == "xiao-s3-pps")
    variant["artifacts"][0]["sha256"] = "not-a-hash"
    with pytest.raises(ValueError, match="sha256|schema"):
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "xiao-s3-pps", assets)


def test_malformed_manifest_json_is_refused_as_valueerror():
    with pytest.raises(ValueError, match="not valid JSON"):
        release_manifest.verify_downloaded_release_assets(
            "{not json", TAG, "xiao-s3-pps", {})


def test_release_directory_verify_refuses_a_schema_incomplete_manifest(monkeypatch, tmp_path):
    manifest, dist = _generated_manifest(monkeypatch, tmp_path)
    manifest.pop("build")
    (dist / release_manifest.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match release-manifest.schema.json"):
        release_manifest.verify_release_directory(
            dist / release_manifest.MANIFEST_NAME, dist, expected_tag=TAG)


def test_verify_fails_when_an_expected_artifact_is_missing(monkeypatch, tmp_path):
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state())
    release_manifest.write_manifest(ROOT, dist, build_info)
    missing = dist / board_profiles.release_asset_name(TAG, "esp32s3-i2s-gps", "partitions")
    missing.unlink()
    with pytest.raises(ValueError, match="missing artifact"):
        release_manifest.verify_release_directory(dist / release_manifest.MANIFEST_NAME, dist, expected_tag=TAG)


def test_dirty_source_is_refused_by_default_and_marked_when_allowed(monkeypatch, tmp_path):
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state(dirty=True))
    with pytest.raises(ValueError, match="refusing unverifiable"):
        release_manifest.build_manifest(ROOT, dist, build_info)
    manifest, _ = release_manifest.build_manifest(ROOT, dist, build_info, allow_dirty=True)
    assert manifest["source"]["dirty"] is True
    assert manifest["source"]["verifiable"] is False
    assert manifest["source"]["refusals"]


def test_release_manifest_hashes_the_ca_bundle_artifacts_when_present(monkeypatch, tmp_path):
    dist, build_info = _write_dist(tmp_path)
    release_ca_bundle.write_release_artifacts(ROOT, dist)
    monkeypatch.setattr(release_manifest, "git_source_state", lambda *args, **kwargs: _source_state())
    manifest = release_manifest.write_manifest(ROOT, dist, build_info)
    artifacts = {item["name"]: item for item in manifest["release_artifacts"]}
    for name, kind in ((release_ca_bundle.BUNDLE_NAME, "ca-bundle"),
                       (release_ca_bundle.METADATA_NAME, "ca-bundle-metadata")):
        assert artifacts[name]["kind"] == kind
    summary = release_manifest.verify_release_directory(
        dist / release_manifest.MANIFEST_NAME, dist, expected_tag=TAG)
    assert summary["artifacts_checked"] >= len(manifest["release_artifacts"])
