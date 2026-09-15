"""PSRAM bus mode follows the BOARD, not its class name.

gold and ageev both report class `esp32s3-i2s-gps` and do not carry the same memory: ageev is
16MB/8MB octal, gold is 8MB/2MB quad. Bus mode is compiled in, so the class-wide FQBN built gold
an image whose PSRAM never appeared -- these tests pin the per-node resolution that replaced it,
and pin just as hard that every node NOT listed keeps exactly what it was building before.
"""

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))

import board_profiles as bp  # noqa: E402


class TestPerNodePsramMode:
    def test_gold_builds_quad(self):
        assert bp.psram_mode("esp32s3-i2s-gps", "gold") == "quad"
        assert "PSRAM=enabled" in bp.fqbn("esp32s3-i2s-gps", "gold")
        assert "PSRAM=opi" not in bp.fqbn("esp32s3-i2s-gps", "gold")

    def test_ageev_keeps_octal(self):
        assert bp.psram_mode("esp32s3-i2s-gps", "ageev") == "octal"
        assert bp.fqbn("esp32s3-i2s-gps", "ageev") == "esp32:esp32:XIAO_ESP32S3:PSRAM=opi"

    def test_an_unlisted_node_keeps_the_previous_class_default(self):
        """kasami has never been scanned. It must not be guessed into a new binary."""
        for node in ("kasami", "a-node-nobody-has-scanned", None):
            assert bp.psram_mode("esp32s3-i2s-gps", node) == "octal"
            assert bp.fqbn("esp32s3-i2s-gps", node) == bp.FQBN

    def test_the_xiao_class_is_untouched(self):
        for node in ("nyquist", "rankine", "mach", "gold"):
            if node == "gold":
                # gold is recorded as esp32s3-i2s-gps; asking for it under another class is a
                # mistake about which board is in front of you, not a build option.
                with pytest.raises(ValueError):
                    bp.fqbn("xiao-s3-pps", node)
                continue
            assert bp.fqbn("xiao-s3-pps", node) == "esp32:esp32:XIAO_ESP32S3:PSRAM=opi"

    def test_node_ids_resolve_case_insensitively(self):
        assert bp.psram_mode("esp32s3-i2s-gps", "GOLD") == "quad"

    def test_every_recorded_override_names_a_known_class_and_mode(self):
        for node, rec in bp.NODE_PSRAM_MODES.items():
            assert node == node.lower()
            bp.require_board_class(rec["board_class"])
            bp.require_psram_mode(rec["psram_mode"])
            assert rec["why"], "an override is a measurement; it must say where it came from"

    def test_quad_fqbn_keeps_the_flash_and_partition_layout_the_class_had(self):
        quad = bp.PSRAM_MODES["quad"]["fqbn"]
        # The generic esp32s3 definition defaults to 4MB flash and a 1.2MB app partition; the
        # XIAO definition this replaces defaults to 8MB and default_8MB (3MB app, dual OTA).
        # Losing that silently would halve the OTA slot the boot guard depends on.
        assert "FlashSize=8M" in quad
        assert "PartitionScheme=default_8MB" in quad
        # USB CDC on boot, as XIAO_ESP32S3 has by default: the serial log is the bring-up channel.
        assert "CDCOnBoot=cdc" in quad


class TestBuildVariants:
    def test_the_default_variant_name_is_still_the_bare_board_class(self):
        assert bp.build_variant("esp32s3-i2s-gps", "ageev") == "esp32s3-i2s-gps"
        assert bp.build_variant("xiao-s3-pps", "nyquist") == "xiao-s3-pps"

    def test_gold_gets_its_own_variant_name(self):
        assert bp.build_variant("esp32s3-i2s-gps", "gold") == "esp32s3-i2s-gps-qspi"

    def test_every_build_expects_the_psram_it_asks_for(self):
        assert bp.expects_psram("esp32s3-i2s-gps", "gold")
        assert bp.expects_psram("esp32s3-i2s-gps", "ageev")


class TestReleaseImagesAreRefusedWhenTheBusDiffers:
    def test_gold_is_refused_the_class_release(self):
        why = bp.release_variant_refusal("esp32s3-i2s-gps", "gold")
        assert why and "quad" in why

    def test_default_nodes_still_take_the_class_release(self):
        assert bp.release_variant_refusal("esp32s3-i2s-gps", "ageev") is None
        assert bp.release_variant_refusal("esp32s3-i2s-gps", "kasami") is None
        assert bp.release_variant_refusal("xiao-s3-pps", "nyquist") is None


class TestCiBuildsEveryModeItCanFlash:
    def _matrix(self):
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "firmware.yml").read_text())
        return wf["jobs"]["build"]["strategy"]["matrix"]["include"]

    def test_every_psram_mode_a_node_can_be_built_with_has_a_ci_leg(self):
        """A mode nobody compiles is a mode that breaks the first time it is flashed."""
        built = {e["fqbn"] for e in self._matrix() if e["sketch"] == "hear_node"}
        for node, rec in bp.NODE_PSRAM_MODES.items():
            assert bp.fqbn(rec["board_class"], node) in built, node

    def test_the_published_class_images_are_still_the_default_mode(self):
        for entry in self._matrix():
            if entry["sketch"] == "hear_node" and entry["release_stem"]:
                assert entry["fqbn"] == bp.FQBN


class TestFirmwareRefusesToDegradeSilently:
    def test_the_sketch_asserts_the_psram_the_build_expects(self):
        ino = (ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text(errors="replace")
        assert "psram_boot_check" in ino
        assert "BOARD_HAS_PSRAM" in ino
        assert "psramFound()" in ino
        assert '\\"psram_fault\\":%s' in ino
