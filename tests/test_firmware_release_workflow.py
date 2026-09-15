"""Firmware releases must publish one hear_node image per supported board/PSRAM variant."""

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_firmware_workflow_builds_hear_node_release_variants():
    yml = _text(".github/workflows/firmware.yml")
    assert 'board_class: "xiao-s3-pps"' in yml
    assert 'release_stem: "hear_node-xiao-s3-pps"' in yml
    assert 'artifact: "fw-hear_node-xiao-s3-pps"' in yml
    assert 'board_class: "esp32s3-i2s-gps"' in yml
    assert 'release_stem: "hear_node-esp32s3-i2s-gps"' in yml
    assert 'release_stem: "hear_node-esp32s3-i2s-gps-qspi"' in yml
    assert 'artifact: "fw-hear_node-esp32s3-i2s-gps-qspi"' in yml
    assert 'psram_mode: "quad"' in yml
    assert '-DHEAR_BOARD_ESP32S3_I2S_GPS' in yml
    assert 'artifact: "fw-hear_node-esp32s3-i2s-gps"' in yml


def test_release_workflow_publishes_per_board_psram_assets():
    yml = _text(".github/workflows/release.yml")
    for stem in ("hear_node-xiao-s3-pps", "hear_node-esp32s3-i2s-gps",
                 "hear_node-esp32s3-i2s-gps-qspi"):
        assert f'dist/{stem}-$TAG.bin' in yml
        assert f'dist/{stem}-$TAG-bootloader.bin' in yml
        assert f'dist/{stem}-$TAG-partitions.bin' in yml
        assert f'dist/{stem}-$TAG-merged.bin' in yml
        assert f'dist/{stem}-$TAG.elf' in yml
    assert "release-manifest.json" in yml
    assert "release-manifest.schema.json" in yml
    assert "release_manifest.py generate" in yml
    assert "release_manifest.py verify" in yml


def test_release_artifacts_do_not_dirty_the_source_checkout():
    yml = _text(".github/workflows/release.yml")
    assert "path: ${{ runner.temp }}/hear-node-release/" in yml
    assert "path: build/" not in yml
    assert "$RUNNER_TEMP/hear-node-release/" in yml
