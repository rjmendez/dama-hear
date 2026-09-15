"""A published firmware release must say what is in it, and be signed saying it.

WHAT WAS MISSING. v0.1.5 published binaries, a release manifest and SHA256SUMS. All three are
self-referential: a manifest and a checksum file are as republishable as the assets they describe,
so together they prove internal consistency and nothing about origin. docs/release-v0.1.6-
readiness.md §6 said so in as many words -- "no SPDX/CycloneDX SBOM and no Sigstore attestation
today ... do not claim SLSA provenance" -- which is the honest version of a gap, not a fix.

WHAT THIS PINS, and why each part is here rather than assumed:

  1. THE SBOM IS DERIVED, NOT WRITTEN. release_sbom.py computes it from the same tree and the same
     dist directory the manifest is computed from, so an SBOM that disagrees with the release is a
     failing check rather than a stale document nobody reads. It is deterministic -- no timestamp,
     no random serial -- so the manifest can hash it and two runs on one commit agree.

  2. THE SIGNATURE IS KEYLESS, WITH NO SECRET ANYWHERE. `actions/attest-build-provenance` signs
     through the job's GitHub OIDC identity: `id-token: write` plus `attestations: write`, no
     entry in `secrets`, nothing to rotate, nothing an operator can leak. This repository already
     refuses to compile a fleet token into a public asset; it must not acquire a signing key it
     would then have to keep out of one.

  3. THE ORDER IS PART OF THE PROOF. SBOM, then manifest (which hashes the SBOM), then the
     attestation over every file, then SHA256SUMS over all of it including the bundle. The
     manifest deliberately does NOT hash the bundle: the bundle's subjects include the manifest.

  4. THE INSTALLERS ENFORCE IT, AND DO NOT OVERSTATE IT. flash.py and enroll.py refuse a revoked
     tag before downloading, refuse a release whose declared SBOM or attestation is absent, and
     print "signature NOT checked" unless `gh attestation verify` actually ran.

  5. RELEASES THAT PREDATE ALL OF THIS STILL INSTALL. v0.1.5's manifest declares neither block,
     and the fleet has to be able to move off it.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))

import board_profiles  # noqa: E402
import enroll  # noqa: E402
import flash  # noqa: E402
import release_manifest as rm  # noqa: E402
import release_sbom as rs  # noqa: E402

from tests.test_release_manifest import _source_state, _write_dist, TAG  # noqa: E402

RELEASE_YML = (ROOT / ".github" / "workflows" / "release.yml").read_text()
REPO_SLUG = "rjmendez/dama-hear"


@pytest.fixture
def release(monkeypatch, tmp_path):
    """A complete release directory: binaries, SBOM, manifest -- built the way release.yml does."""
    dist, build_info = _write_dist(tmp_path)
    monkeypatch.setattr(rm, "git_source_state", lambda *a, **k: _source_state())
    sbom = rs.write_sbom(ROOT, dist, build_info)
    manifest = rm.write_manifest(ROOT, dist, build_info)
    return dist, manifest, sbom


def _dist_digests(dist: pathlib.Path):
    import hashlib
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(dist.iterdir()) if p.is_file()}


def _bundle(subjects, workflow=rm.ATTESTATION_WORKFLOW, repo=REPO_SLUG,
            predicate_type=rm.ATTESTATION_PREDICATE_TYPE):
    """A Sigstore-shaped bundle line. Signature material is irrelevant to the offline check --
    that check exists to answer "does this bundle even claim to cover these files", and says so."""
    statement = {
        "_type": rm.IN_TOTO_STATEMENT_TYPE,
        "predicateType": predicate_type,
        "subject": [{"name": name, "digest": {"sha256": digest}}
                    for name, digest in sorted(subjects.items())],
        "predicate": {
            "buildDefinition": {
                "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                "externalParameters": {
                    "workflow": {"ref": "refs/tags/" + TAG,
                                 "repository": "https://github.com/" + repo,
                                 "path": workflow},
                },
            },
            "runDetails": {"builder": {"id": "https://github.com/actions/runner"}},
        },
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                       "dsseEnvelope": {"payloadType": "application/vnd.in-toto+json",
                                        "payload": payload,
                                        "signatures": [{"sig": "aGk="}]}}).encode()


class TestTheWorkflowSignsWithoutHoldingASecret:
    def test_the_release_job_asks_for_oidc_and_attestation_permissions(self):
        assert "id-token: write" in RELEASE_YML
        assert "attestations: write" in RELEASE_YML

    def test_the_release_job_reads_no_repository_secret(self):
        """Keyless means keyless. A `secrets.` reference here would be a signing key to protect,
        and this workflow's whole premise is that a public asset carries no credential."""
        assert not re.search(r"\$\{\{\s*secrets\.", RELEASE_YML)

    def test_it_signs_every_published_file_with_attest_build_provenance(self):
        assert "actions/attest-build-provenance@v" in RELEASE_YML
        assert "subject-path: dist/*" in RELEASE_YML

    def test_the_sbom_is_generated_before_the_manifest_that_hashes_it(self):
        sbom_at = RELEASE_YML.index("release_sbom.py generate")
        manifest_at = RELEASE_YML.index("release_manifest.py generate")
        assert sbom_at < manifest_at

    def test_the_bundle_is_published_and_checksummed_last(self):
        """SHA256SUMS has to be written after the bundle lands in dist/, or the release publishes
        a file no checksum covers."""
        attest_at = RELEASE_YML.index("actions/attest-build-provenance@v")
        copy_at = RELEASE_YML.index(rm.ATTESTATION_BUNDLE_NAME + '"')
        sums_at = RELEASE_YML.index("sha256sum * > SHA256SUMS")
        publish_at = RELEASE_YML.rindex("gh release create")
        assert attest_at < copy_at < sums_at < publish_at

    def test_the_release_job_verifies_its_own_attestation_before_publishing(self):
        assert "release_manifest.py verify \\\n            --dist dist --tag \"$TAG\" --attestation" \
            in RELEASE_YML

    def test_the_notes_tell_an_operator_how_to_verify_and_what_is_not_covered(self):
        assert "gh attestation verify" in RELEASE_YML
        assert rm.SBOM_NAME in RELEASE_YML
        assert "--signer-workflow" in RELEASE_YML
        assert "does not check the signature" in RELEASE_YML


class TestTheSbomDescribesThisReleaseAndNoOther:
    def test_it_is_byte_identical_on_a_second_run(self, monkeypatch, tmp_path):
        """A manifest hashes it, so a timestamp or a random serial number would make every
        release unreproducible by construction."""
        dist, build_info = _write_dist(tmp_path)
        monkeypatch.setattr(rm, "git_source_state", lambda *a, **k: _source_state())
        first = rs.sbom_bytes(rs.build_sbom(ROOT, dist, build_info))
        second = rs.sbom_bytes(rs.build_sbom(ROOT, dist, build_info))
        assert first == second

    def test_it_is_cyclonedx_and_names_the_release(self, release):
        _, manifest, sbom = release
        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.6"
        assert sbom["serialNumber"].startswith("urn:uuid:")
        root = sbom["metadata"]["component"]
        assert root["name"] == "hear_node" and root["version"] == manifest["tag"]

    def test_every_published_binary_appears_with_its_sha256(self, release):
        dist, manifest, sbom = release
        index = rs.component_index(sbom)
        for variant in manifest["variants"]:
            for item in variant["artifacts"]:
                assert index[item["name"]] == item["sha256"]

    def test_the_source_closure_and_toolchain_pins_are_in_it(self, release):
        _, manifest, sbom = release
        index = rs.component_index(sbom)
        for item in manifest["inputs"] + manifest["generated_files"]:
            assert index[item["path"]] == item["sha256"]
        versions = {c["name"]: c.get("version") for c in sbom["components"]}
        assert versions["arduino-cli"] == manifest["build"]["arduino_cli_version"]
        assert versions["esp32:esp32"] == manifest["build"]["esp32_core_version"]

    def test_the_vendored_libraries_are_content_addressed(self, release):
        _, _, sbom = release
        libs = [c for c in sbom["components"] if c["bom-ref"].startswith("lib/")]
        assert libs, "firmware/lib has libraries; the SBOM must list them"
        for lib in libs:
            assert lib["hashes"][0]["alg"] == "SHA-256"
            assert len(lib["hashes"][0]["content"]) == 64

    def test_it_states_what_it_does_not_cover(self, release):
        """An SBOM that implies it enumerated the ESP-IDF fork inside the board package would be
        a more dangerous document than no SBOM at all."""
        _, _, sbom = release
        props = {p["name"]: p["value"] for p in sbom["metadata"]["properties"]}
        scope = json.loads(props["dama-hear:sbom-scope"])
        assert any("board package" in x for x in scope["excludes"])
        assert "published-firmware-binaries" in scope["covers"]

    def test_a_changed_binary_fails_the_sbom(self, release):
        dist, manifest, sbom = release
        name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
        (dist / name).write_bytes(b"tampered")
        with pytest.raises(ValueError, match=name):
            rs.verify_sbom(sbom, manifest=manifest, dist_dir=dist)

    def test_an_sbom_for_another_commit_is_refused(self, release):
        _, manifest, sbom = release
        for prop in sbom["metadata"]["component"]["properties"]:
            if prop["name"] == "dama-hear:source-commit":
                prop["value"] = "f" * 40
        with pytest.raises(ValueError, match="source commit"):
            rs.verify_sbom(sbom, manifest=manifest)


class TestTheManifestBindsTheSbomAndNamesItsSigner:
    def test_the_manifest_hashes_the_sbom(self, release):
        dist, manifest, _ = release
        block = manifest["sbom"]
        assert block["name"] == rm.SBOM_NAME
        assert block["sha256"] == _dist_digests(dist)[rm.SBOM_NAME]
        assert block["bytes"] == (dist / rm.SBOM_NAME).stat().st_size
        assert {"kind": rm.SBOM_KIND} .items() <= next(
            a for a in manifest["release_artifacts"] if a["name"] == rm.SBOM_NAME).items()

    def test_a_swapped_sbom_is_refused(self, release):
        dist, manifest, _ = release
        (dist / rm.SBOM_NAME).write_text('{"bomFormat": "CycloneDX"}')
        with pytest.raises(ValueError, match="sha256"):
            rm.verify_release_directory(dist / rm.MANIFEST_NAME, dist, expected_tag=TAG)

    def test_the_manifest_declares_a_keyless_signing_identity(self, release):
        _, manifest, _ = release
        att = manifest["attestation"]
        assert att["bundle"] == rm.ATTESTATION_BUNDLE_NAME
        assert att["predicate_type"] == rm.ATTESTATION_PREDICATE_TYPE
        assert att["signing"]["method"] == "sigstore-keyless-github-oidc"
        assert att["signing"]["issuer"] == rm.ATTESTATION_ISSUER
        assert att["signing"]["workflow"] == rm.ATTESTATION_WORKFLOW
        assert att["signing"]["secrets_required"] is False
        assert "gh attestation verify" in att["verify"]

    def test_the_manifest_does_not_hash_the_bundle_that_covers_it(self, release):
        _, manifest, _ = release
        names = [a["name"] for a in manifest["release_artifacts"]]
        assert rm.ATTESTATION_BUNDLE_NAME not in names
        assert "cannot attest to the bundle" in manifest["attestation"]["not_covered_by_manifest"]

    def test_the_schema_still_accepts_a_release_without_either(self, release):
        """v0.1.5's manifest has no sbom and no attestation. Refusing it would strand the fleet
        on a release it cannot update away from -- the rankine failure, one level up."""
        _, manifest, _ = release
        legacy = {k: v for k, v in manifest.items() if k not in ("sbom", "attestation")}
        assert not rm.manifest_schema_problems(legacy)
        assert not rm.manifest_schema_problems(manifest)

    def test_the_schema_version_did_not_move(self):
        assert rm.SCHEMA_VERSION == 1


class TestTheAttestationMustActuallyCoverTheRelease:
    def test_a_bundle_over_every_asset_passes_and_verifies_the_directory(self, release):
        dist, manifest, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        summary = rm.verify_release_directory(dist / rm.MANIFEST_NAME, dist, expected_tag=TAG,
                                              attestation=True)
        assert summary["sbom_checked"] is True
        assert rm.SBOM_NAME in summary["attestation_subjects"]
        assert summary["signature_verified"] is False

    def test_an_asset_the_bundle_does_not_mention_is_a_refusal(self, release):
        dist, manifest, _ = release
        digests = _dist_digests(dist)
        digests.pop("build-info.json")
        with pytest.raises(ValueError, match=re.escape("does not cover build-info.json")):
            rm.check_attestation_structure(_bundle(digests), manifest=manifest)

    def test_a_bundle_that_skips_the_manifest_itself_is_a_refusal(self, release):
        """The manifest is what every other hash is read out of, so an attestation that covers
        the binaries but not the manifest leaves the one swappable document unsigned."""
        dist, _, _ = release
        digests = _dist_digests(dist)
        digests.pop(rm.MANIFEST_NAME)
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(digests))
        with pytest.raises(ValueError, match=re.escape("does not cover " + rm.MANIFEST_NAME)):
            rm.verify_release_directory(dist / rm.MANIFEST_NAME, dist, expected_tag=TAG,
                                        attestation=True)

    def test_a_bundle_from_another_build_is_a_refusal(self, release):
        dist, manifest, _ = release
        digests = _dist_digests(dist)
        name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
        digests[name] = "a" * 64
        with pytest.raises(ValueError, match="covers a different " + re.escape(name)):
            rm.check_attestation_structure(_bundle(digests), manifest=manifest)

    def test_a_bundle_signed_by_another_workflow_is_a_refusal(self, release):
        dist, manifest, _ = release
        bad = _bundle(_dist_digests(dist), workflow=".github/workflows/somebody-elses.yml")
        with pytest.raises(ValueError, match="names workflow"):
            rm.check_attestation_structure(bad, manifest=manifest)

    def test_a_bundle_from_another_repository_is_a_refusal(self, release):
        dist, manifest, _ = release
        bad = _bundle(_dist_digests(dist), repo="attacker/dama-hear-mirror")
        with pytest.raises(ValueError, match="was built from"):
            rm.check_attestation_structure(bad, manifest=manifest)

    def test_a_non_provenance_predicate_is_a_refusal(self, release):
        dist, manifest, _ = release
        bad = _bundle(_dist_digests(dist), predicate_type="https://example.invalid/other/v1")
        with pytest.raises(ValueError, match="predicateType"):
            rm.check_attestation_structure(bad, manifest=manifest)

    def test_a_corrupt_bundle_says_so_instead_of_passing_empty(self, release):
        _, manifest, _ = release
        with pytest.raises(ValueError, match="not valid JSON"):
            rm.check_attestation_structure(b"not json\n", manifest=manifest)
        with pytest.raises(ValueError, match="no DSSE payload"):
            rm.check_attestation_structure(b'{"dsseEnvelope": {}}\n', manifest=manifest)
        with pytest.raises(ValueError, match="no statements"):
            rm.check_attestation_structure(b"\n\n", manifest=manifest)
        with pytest.raises(ValueError, match="base64"):
            rm.check_attestation_structure(
                b'{"dsseEnvelope": {"payload": "!!!!"}}\n', manifest=manifest)

    def test_asking_for_an_attestation_a_release_does_not_have_fails_loudly(self, release):
        """An operator who types --attestation and sees "verified" on a release that has none has
        been told the opposite of the truth."""
        dist, manifest, _ = release
        del manifest["attestation"]
        (dist / rm.MANIFEST_NAME).write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="predates signed attestations"):
            rm.verify_release_directory(dist / rm.MANIFEST_NAME, dist, expected_tag=TAG,
                                        attestation=True)

    def test_a_missing_signature_tool_is_an_error_not_a_pass(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rm.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="NOT verified"):
            rm.verify_attestation_signature(tmp_path / "b.jsonl", tmp_path / "a.bin")

    def test_the_signature_check_shells_out_with_no_credential(self, monkeypatch, tmp_path):
        seen = {}

        class R:
            returncode = 0
            stdout = "Verification succeeded!"
            stderr = ""

        monkeypatch.setattr(rm.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(rm.subprocess, "run", lambda cmd, **kw: (seen.update(cmd=cmd), R())[1])
        rm.verify_attestation_signature(tmp_path / "b.jsonl", tmp_path / "a.bin")
        cmd = seen["cmd"]
        assert cmd[1:4] == ["attestation", "verify", str(tmp_path / "a.bin")]
        assert "--bundle" in cmd and "--signer-workflow" in cmd
        assert not any("token" in part.lower() for part in cmd)

    def test_a_failed_signature_check_is_reported_with_the_tools_own_message(self, monkeypatch,
                                                                            tmp_path):
        class R:
            returncode = 1
            stdout = ""
            stderr = "the bundle's certificate identity does not match"

        monkeypatch.setattr(rm.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(rm.subprocess, "run", lambda cmd, **kw: R())
        with pytest.raises(ValueError, match="certificate identity"):
            rm.verify_attestation_signature(tmp_path / "b.jsonl", tmp_path / "a.bin")

    def test_verification_is_described_honestly(self, release):
        _, manifest, _ = release
        structural = rm.describe_verification(manifest, bundle_present=True,
                                              signature_verified=False)
        assert "signature NOT checked" in structural
        signed = rm.describe_verification(manifest, bundle_present=True, signature_verified=True)
        assert "signature VERIFIED" in signed
        assert rm.SBOM_NAME in signed


class TestTheInstallersEnforceIt:
    def _serve(self, dist, missing=()):
        files = {p.name: p.read_bytes() for p in dist.iterdir() if p.is_file()}
        for name in missing:
            files.pop(name, None)
        asked = []

        def fake_fetch(url, timeout=60):
            tail = url.rsplit("/", 1)[-1]
            asked.append(tail)
            if tail not in files:
                raise OSError("HTTP 404 for %s" % tail)
            return files[tail]

        return fake_fetch, asked

    def test_flash_downloads_and_checks_the_sbom_and_the_attestation(self, release, monkeypatch,
                                                                     tmp_path):
        dist, _, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        fetch, asked = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        path = flash.release_image(TAG, "xiao-s3-pps", "octal")
        name = board_profiles.release_asset_name(TAG, "xiao-s3-pps", "app")
        assert pathlib.Path(path).read_bytes() == (dist / name).read_bytes()
        assert rm.SBOM_NAME in asked and rm.ATTESTATION_BUNDLE_NAME in asked

    def test_flash_refuses_a_release_whose_declared_sbom_is_missing(self, release, monkeypatch,
                                                                    tmp_path, capsys):
        dist, _, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        fetch, _ = self._serve(dist, missing=[rm.SBOM_NAME])
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        with pytest.raises(SystemExit):
            flash.release_image(TAG, "xiao-s3-pps", "octal")
        assert "SBOM is missing" in capsys.readouterr().err

    def test_flash_refuses_a_release_whose_attestation_is_missing(self, release, monkeypatch,
                                                                  tmp_path, capsys):
        dist, _, _ = release
        fetch, _ = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        with pytest.raises(SystemExit):
            flash.release_image(TAG, "xiao-s3-pps", "octal")
        assert "--allow-unattested" in capsys.readouterr().err

    def test_allow_unattested_installs_and_says_the_attestation_was_not_checked(
            self, release, monkeypatch, tmp_path, capsys):
        dist, _, _ = release
        fetch, _ = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        flash.release_image(TAG, "xiao-s3-pps", "octal", allow_unattested=True)
        assert "attestation NOT checked" in capsys.readouterr().out

    def test_flash_does_not_claim_a_signature_it_did_not_check(self, release, monkeypatch,
                                                               tmp_path, capsys):
        dist, _, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        fetch, _ = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        flash.release_image(TAG, "xiao-s3-pps", "octal")
        out = capsys.readouterr().out
        assert "signature NOT checked" in out

    def test_flash_runs_gh_when_asked_to_verify_the_signature(self, release, monkeypatch,
                                                              tmp_path, capsys):
        dist, _, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        fetch, _ = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        monkeypatch.setattr(rm, "verify_attestation_signature",
                            lambda bundle, asset, repository=None: "Verification succeeded!")
        flash.release_image(TAG, "xiao-s3-pps", "octal", verify_signature=True)
        assert "signature VERIFIED" in capsys.readouterr().out

    def test_enroll_checks_the_same_things_over_usb(self, release, monkeypatch, tmp_path):
        dist, _, _ = release
        (dist / rm.ATTESTATION_BUNDLE_NAME).write_bytes(_bundle(_dist_digests(dist)))
        fetch, asked = self._serve(dist)
        monkeypatch.setattr(enroll, "fetch", fetch)
        dest = tmp_path / "usb"
        dest.mkdir()
        enroll.release_files(TAG, str(dest), "esp32s3-i2s-gps", "quad")
        assert (dest / board_profiles.upload_filename("app")).exists()
        assert rm.SBOM_NAME in asked and rm.ATTESTATION_BUNDLE_NAME in asked

    def test_a_legacy_release_still_installs_without_either(self, release, monkeypatch, tmp_path):
        """v0.1.5: manifest, no SBOM, no attestation. It must remain installable, and must not be
        described as more verified than it is."""
        dist, manifest, _ = release
        for key in ("sbom", "attestation"):
            manifest.pop(key, None)
        manifest["release_artifacts"] = [a for a in manifest["release_artifacts"]
                                         if a["kind"] != rm.SBOM_KIND]
        (dist / rm.MANIFEST_NAME).write_text(json.dumps(manifest))
        (dist / rm.SBOM_NAME).unlink()
        fetch, asked = self._serve(dist)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        monkeypatch.setattr(enroll, "fetch", fetch)
        flash.release_image(TAG, "xiao-s3-pps", "octal")
        assert rm.SBOM_NAME not in asked
        assert rm.describe_verification(manifest, False, False) == rm.MANIFEST_NAME


class TestRevocation:
    def test_the_revocation_list_is_a_valid_empty_document(self):
        doc = rm.load_revocations()
        assert doc["type"] == rm.REVOCATIONS_TYPE
        assert doc["revoked"] == [], "nothing is revoked today; this test is the shape check"
        assert rm.revocation_refusal("v0.1.6") is None

    def test_a_revoked_tag_is_refused_with_its_reason_and_successor(self, tmp_path):
        path = tmp_path / rm.REVOCATIONS_NAME
        path.write_text(json.dumps({
            "schema_version": 1, "type": rm.REVOCATIONS_TYPE,
            "revoked": [{"tag": "v0.1.6", "date": "2026-09-16",
                         "reason": "built from a dirty tree", "superseded_by": "v0.1.7"}]}))
        why = rm.revocation_refusal("v0.1.6", path)
        assert "dirty tree" in why and "v0.1.7" in why
        assert rm.revocation_refusal("v0.1.7", path) is None

    def test_a_revoked_release_is_refused_before_anything_is_downloaded(self, release,
                                                                        monkeypatch, tmp_path,
                                                                        capsys):
        dist, _, _ = release
        path = tmp_path / rm.REVOCATIONS_NAME
        path.write_text(json.dumps({
            "schema_version": 1, "type": rm.REVOCATIONS_TYPE,
            "revoked": [{"tag": TAG, "reason": "test", "superseded_by": None}]}))
        monkeypatch.setattr(rm, "load_revocations",
                            lambda p=None: json.loads(path.read_text()))
        fetch, asked = self._fetch_counter()
        monkeypatch.setattr(enroll, "fetch", fetch)
        monkeypatch.setattr(flash, "REPO", str(tmp_path / "clone"))
        with pytest.raises(SystemExit):
            flash.release_image(TAG, "xiao-s3-pps", "octal")
        assert "is revoked" in capsys.readouterr().err
        assert asked == [], "a revoked tag must be refused before the first request"

    def _fetch_counter(self):
        asked = []

        def fake_fetch(url, timeout=60):
            asked.append(url)
            raise AssertionError("nothing should have been downloaded")

        return fake_fetch, asked

    def test_a_malformed_revocation_list_fails_closed(self, tmp_path):
        path = tmp_path / rm.REVOCATIONS_NAME
        path.write_text("{]")
        with pytest.raises(ValueError, match="not valid JSON"):
            rm.revocation_refusal("v0.1.6", path)
        path.write_text('{"type": "something-else", "revoked": []}')
        with pytest.raises(ValueError, match="is not a"):
            rm.revocation_refusal("v0.1.6", path)


class TestTheDocsMatchTheCode:
    def test_the_provenance_plan_is_documented(self):
        doc = (ROOT / "docs" / "release-provenance.md").read_text()
        for needed in (rm.SBOM_NAME, rm.ATTESTATION_BUNDLE_NAME, "gh attestation verify",
                       "id-token", "revocation", "CycloneDX"):
            assert needed in doc, needed

    def test_the_readiness_runbook_no_longer_claims_the_gap_is_open(self):
        doc = (ROOT / "docs" / "release-v0.1.6-readiness.md").read_text()
        assert "no SPDX/CycloneDX SBOM and **no**" not in doc
        assert rm.SBOM_NAME in doc
        assert "release-provenance.md" in doc
