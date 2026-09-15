"""A public release asset must declare that it carries no fleet credentials, and prove it.

WHAT HAPPENED. release.yml builds `hear_node` from a checkout with no secrets.h -- correct, and
deliberate: a fleet token compiled into a public GitHub asset is a published fleet credential. But
HEAR_PUSH_TOKEN and HEAR_ADMIN_TOKEN were compile-time ONLY, and NVS carried the node's name and
its Wi-Fi and nothing else. So rankine took the published v0.1.5 image over the air and came back
answering /status, having every heartbeat rejected with 401, and refusing /update, /reboot,
/format and /gate from EVERYONE, including the installer that had just flashed it. USB was the
only way back.

The node-side half of that is fixed: credentials now live in the NVS provisioning record and
flash.py gates on `auth.*.src == "nvs"` before and after an OTA (PR #196, tests/test_prov_line.py,
tests/test_nvs_enrollment.py, tests/test_enrollment_gate.py).

This file pins the RELEASE-PACKAGING half, which is what makes that gate trustworthy:

  1. The workflows refuse to build or publish from a tree containing secrets.h, so "the published
     image has no credentials" is asserted at build time rather than assumed from CI's checkout.
  2. build-info.json and release-manifest.json say what the image IS -- `image_class:
     unprovisioned`, `compiled_in_credentials: false`, and the list of things a node must already
     hold -- so an installer reads the claim instead of inferring it.
  3. The manifest generator refuses to write any other claim, and the installer refuses to
     install an asset that makes one: a release advertising compiled-in credentials is a release
     that published a fleet token, and installing it would spread that token fleet-wide.
  4. Manifests that predate the field (v0.1.5 and earlier) stay installable, so the fleet is not
     stranded on firmware it cannot update.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))

import board_profiles  # noqa: E402
import release_manifest  # noqa: E402

RELEASE_YML = (ROOT / ".github" / "workflows" / "release.yml").read_text()
FIRMWARE_YML = (ROOT / ".github" / "workflows" / "firmware.yml").read_text()


class TestTheWorkflowsCannotPublishACredentialedImage:
    def test_the_release_workflow_refuses_a_checkout_that_has_secrets_h(self):
        assert "firmware/hear_node/secrets.h" in RELEASE_YML
        assert "firmware/puc_node/secrets.h" in RELEASE_YML
        assert "the images may carry fleet credentials" in RELEASE_YML

    def test_the_firmware_build_refuses_to_compile_credentials_into_an_artifact(self):
        assert "no compiled-in credentials in the build tree" in FIRMWARE_YML
        assert "find firmware -name secrets.h" in FIRMWARE_YML

    def test_the_release_workflow_stamps_the_image_class_into_build_info(self):
        assert '"image_class": "unprovisioned"' in RELEASE_YML
        assert '"compiled_in_credentials": False' in RELEASE_YML
        assert ('"provisioning_required": ["node_id", "wifi", "admin_token", "push_token"]'
                in RELEASE_YML)

    def test_the_release_notes_say_an_unprovisioned_image_is_not_node_ready(self):
        assert "**unprovisioned**" in RELEASE_YML
        assert "no admin token and no push token" in RELEASE_YML
        assert "auth.admin.src" in RELEASE_YML and "auth.push.src" in RELEASE_YML


class TestTheManifestDeclaresWhatTheImageIs:
    def test_a_generated_manifest_declares_the_contract(self, monkeypatch, tmp_path):
        from tests.test_release_manifest import _write_dist, _source_state, TAG  # noqa: E402

        dist, build_info = _write_dist(tmp_path)
        monkeypatch.setattr(release_manifest, "git_source_state", lambda *a, **k: _source_state())
        manifest = release_manifest.write_manifest(ROOT, dist, build_info)
        assert manifest["build"]["image_class"] == release_manifest.IMAGE_CLASS_UNPROVISIONED
        assert manifest["build"]["compiled_in_credentials"] is False
        assert "admin_token" in manifest["build"]["provisioning_required"]
        assert "push_token" in manifest["build"]["provisioning_required"]
        assert not release_manifest.manifest_schema_problems(manifest)
        assert manifest["tag"] == TAG

    def test_generation_refuses_a_tree_that_still_holds_secrets_h(self, monkeypatch, tmp_path):
        """secrets.h in the tree means the compile may have baked credentials into the binaries.
        Refusing to write a manifest is cheaper than rotating every token on the fleet after the
        assets are public. Pointed at a file that really does exist so the check, not the
        monkeypatch, is what fires."""
        from tests.test_release_manifest import _write_dist, _source_state  # noqa: E402

        dist, build_info = _write_dist(tmp_path)
        monkeypatch.setattr(release_manifest, "git_source_state", lambda *a, **k: _source_state())
        monkeypatch.setattr(release_manifest, "SECRETS_HEADER",
                            "firmware/hear_node/secrets.h.example")
        with pytest.raises(ValueError, match="secrets.h"):
            release_manifest.write_manifest(ROOT, dist, build_info)

    def test_build_info_declaring_a_credentialed_image_cannot_be_published(self, tmp_path):
        info = {"tag": "v9.9.9", "commit": "0" * 40, "sketch": "firmware/hear_node",
                "fqbn": "x", "esp32_core": "3.3.11", "arduino_cli": "1.5.1", "variants": [],
                "compiled_in_credentials": True}
        p = tmp_path / "build-info.json"
        p.write_text(json.dumps(info))
        with pytest.raises(ValueError, match="must never contain a fleet token"):
            release_manifest.load_build_info(p)
        info["compiled_in_credentials"] = False
        info["image_class"] = "node-ready"
        p.write_text(json.dumps(info))
        with pytest.raises(ValueError, match="image_class"):
            release_manifest.load_build_info(p)


class TestTheInstallerRefusesAnAssetThatClaimsCredentials:
    def test_an_asset_claiming_compiled_in_credentials_is_refused_outright(self):
        why = release_manifest.image_provisioning_refusal(
            {"build": {"compiled_in_credentials": True}})
        assert why and "never contain a fleet token" in why

    def test_an_unknown_image_class_is_refused_rather_than_guessed(self):
        why = release_manifest.image_provisioning_refusal({"build": {"image_class": "node-ready"}})
        assert why and "node-ready" in why

    def test_a_manifest_that_predates_the_field_is_still_installable(self):
        """v0.1.5 and earlier say nothing either way. Refusing them would strand the fleet on a
        firmware that cannot be updated at all; the node-side auth gate is what protects it."""
        assert release_manifest.image_provisioning_refusal({}) is None
        assert release_manifest.image_provisioning_refusal({"build": {}}) is None

    def test_a_declared_unprovisioned_image_passes(self):
        build = {"image_class": release_manifest.IMAGE_CLASS_UNPROVISIONED,
                 "compiled_in_credentials": False}
        assert release_manifest.image_provisioning_refusal({"build": build}) is None

    def test_downloaded_assets_are_refused_before_they_are_installed(self, monkeypatch, tmp_path):
        """The check the installer actually runs: flash.py verifies every downloaded asset through
        verify_downloaded_release_assets, so a credential-bearing release stops there."""
        from tests.test_release_manifest import _write_dist, _source_state, TAG  # noqa: E402

        dist, build_info = _write_dist(tmp_path)
        monkeypatch.setattr(release_manifest, "git_source_state", lambda *a, **k: _source_state())
        manifest = release_manifest.write_manifest(ROOT, dist, build_info)
        name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
        assets = {name: (dist / name).read_bytes()}
        release_manifest.verify_downloaded_release_assets(
            json.dumps(manifest), TAG, "xiao-s3-pps", assets)

        manifest["build"]["compiled_in_credentials"] = True
        with pytest.raises(ValueError, match="never contain a fleet token"):
            release_manifest.verify_downloaded_release_assets(
                json.dumps(manifest), TAG, "xiao-s3-pps", assets)

    def test_an_offline_release_directory_is_refused_too(self, monkeypatch, tmp_path):
        from tests.test_release_manifest import _write_dist, _source_state, TAG  # noqa: E402

        dist, build_info = _write_dist(tmp_path)
        monkeypatch.setattr(release_manifest, "git_source_state", lambda *a, **k: _source_state())
        manifest = release_manifest.write_manifest(ROOT, dist, build_info)
        path = dist / release_manifest.MANIFEST_NAME
        manifest["build"]["image_class"] = "node-ready"
        path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="node-ready"):
            release_manifest.verify_release_directory(path, dist, expected_tag=TAG)
