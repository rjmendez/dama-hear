"""An image whose pins can drift from the checkout is the ConfigMap problem with a registry.

Four things have to keep agreeing, and none of them agree by themselves:

  requirements/ci-pods.txt   what CI installs and tests against
  deploy/k8s/hear-*.yaml     what the un-migrated pods still pip-install at start
  requirements/image-*.txt   what a base image asks for
  requirements/lock/*.txt    what that resolved to, artifact by artifact

⚠️These run WITHOUT docker and WITHOUT the network, so they belong in the ordinary pytest job.
The half that needs a daemon -- regenerating a lock and diffing it -- is the images workflow.
"""
import os
import pathlib
import re
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGES = ROOT / "deploy" / "images"
sys.path.insert(0, str(IMAGES))

import gen_pip_lock  # noqa: E402

PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", re.M)
FROM = re.compile(r"^FROM\s+(\S+)", re.M)
DOCKERFILES = sorted(IMAGES.glob("Dockerfile.*"))
MANIFESTS = [p for p in sorted((ROOT / "deploy" / "k8s").glob("hear-*.yaml"))
             if not p.name.endswith("-code.yaml")]


def pins(path):
    return dict(PIN.findall(path.read_text()))


def test_every_variant_has_a_dockerfile_and_a_lock():
    for variant, (_, _, req) in sorted(gen_pip_lock.VARIANTS.items()):
        assert (IMAGES / ("Dockerfile.%s" % variant)).exists(), (
            "variant %s has no deploy/images/Dockerfile.%s" % (variant, variant))
        assert (ROOT / req).exists(), "variant %s names a missing %s" % (variant, req)
        assert (ROOT / "requirements" / "lock" / ("%s.txt" % variant)).exists(), (
            "variant %s has no requirements/lock/%s.txt -- run "
            "`python3 deploy/images/gen_pip_lock.py %s`" % (variant, variant, variant))


def test_no_dockerfile_starts_from_a_tag():
    """⚠️A tag is republished. A digest is the only FROM that means the same thing twice."""
    pinned = gen_pip_lock.base_images(ROOT)
    by_digest = {digest: name for (name, _tag), digest in pinned.items()}
    for df in DOCKERFILES:
        for ref in FROM.findall(df.read_text()):
            if ref.startswith("${"):
                # an internal parent, passed in as a digest by the images workflow
                assert ref in ("${RUNTIME_IMAGE}", "${NUMERIC_IMAGE}"), (
                    "%s builds on an unknown internal image %s" % (df.name, ref))
                continue
            assert "@sha256:" in ref, (
                "%s starts FROM %s -- pin it by digest and list it in "
                "deploy/images/base-images.txt" % (df.name, ref))
            name, _, digest = ref.partition("@")
            assert by_digest.get(digest) == name, (
                "%s starts FROM %s, which deploy/images/base-images.txt does not pin"
                % (df.name, ref))


def test_base_images_covers_what_the_cluster_still_runs():
    """The un-migrated manifests and the images must not end up on different Pythons."""
    pinned = {name for name, _tag in gen_pip_lock.base_images(ROOT)}
    tags = {}
    for m in MANIFESTS:
        for ref in re.findall(r"^\s*image:\s*(\S+)\s*$", m.read_text(), re.M):
            name, _, tag = ref.partition(":")
            tags.setdefault(name, set()).add(tag)
    assert tags, "no image: line found in deploy/k8s/hear-*.yaml"
    missing = sorted(set(tags) - pinned)
    assert not missing, (
        "deploy/k8s runs %s, which deploy/images/base-images.txt does not pin by digest"
        % ", ".join(missing))


def test_base_images_pins_the_tag_the_manifests_name():
    listed = {}
    for (name, tag) in gen_pip_lock.base_images(ROOT):
        listed.setdefault(name, set()).add(tag)
    for m in MANIFESTS:
        for ref in re.findall(r"^\s*image:\s*(\S+)\s*$", m.read_text(), re.M):
            name, _, tag = ref.partition(":")
            if name not in listed:
                continue
            assert tag in listed[name], (
                "%s runs %s:%s but base-images.txt pins %s of that image"
                % (m.name, name, tag, sorted(listed[name])))


def test_image_requirements_agree_with_the_ci_pods_pins():
    """⚠️An image numerically unlike CI is an image CI did not test."""
    ci = pins(ROOT / "requirements" / "ci-pods.txt")
    for req in sorted((ROOT / "requirements").glob("image-*.txt")):
        for pkg, version in pins(req).items():
            if pkg in ci:
                assert ci[pkg] == version, (
                    "%s wants %s==%s, requirements/ci-pods.txt pins %s"
                    % (req.name, pkg, version, ci[pkg]))


def test_image_requirements_agree_with_what_the_pods_install():
    """While both paths exist, the image and the pod it replaces install the same versions."""
    installed = {}
    for m in MANIFESTS:
        for pkg, version in re.findall(r"'([A-Za-z0-9._-]+)==([0-9][^']*)'", m.read_text()):
            installed.setdefault(pkg.lower(), {}).setdefault(version, []).append(m.name)
    assert installed, "no quoted pip pin found in deploy/k8s/hear-*.yaml"
    for req in sorted((ROOT / "requirements").glob("image-*.txt")):
        for pkg, version in pins(req).items():
            by_ver = installed.get(pkg.lower())
            if not by_ver:
                continue
            assert version in by_ver, (
                "%s wants %s==%s; the manifests install %s (%s)"
                % (req.name, pkg, version, sorted(by_ver),
                   ", ".join(sorted({f for v in by_ver.values() for f in v}))))


@pytest.mark.parametrize("variant", sorted(gen_pip_lock.VARIANTS))
def test_every_locked_requirement_carries_a_hash(variant):
    lock = (ROOT / "requirements" / "lock" / ("%s.txt" % variant)).read_text()
    body = [ln for ln in lock.splitlines() if ln and not ln.startswith("#")]
    assert body, "requirements/lock/%s.txt locks nothing" % variant
    names = [ln for ln in body if "==" in ln]
    hashes = [ln for ln in body if "--hash=sha256:" in ln]
    assert len(names) == len(hashes) and len(names) * 2 == len(body), (
        "requirements/lock/%s.txt has %d pins and %d hashes in %d lines -- every requirement "
        "needs exactly one --hash, or --require-hashes will reject the file in the build"
        % (variant, len(names), len(hashes), len(body)))
    for ln in hashes:
        assert re.fullmatch(r"\s+--hash=sha256:[0-9a-f]{64}", ln), "malformed hash line: %r" % ln


def test_every_lock_still_matches_its_input_and_its_base():
    """The stamped header is what makes `edited the pins, forgot to regenerate` visible."""
    problems = gen_pip_lock.check(ROOT)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("variant", sorted(gen_pip_lock.VARIANTS))
def test_the_lock_contains_everything_its_input_asked_for(variant):
    _, _, req = gen_pip_lock.VARIANTS[variant]
    lock = pins(ROOT / "requirements" / "lock" / ("%s.txt" % variant))
    lock = {k.lower().replace("_", "-"): v for k, v in lock.items()}
    for pkg, version in pins(ROOT / req).items():
        got = lock.get(pkg.lower().replace("_", "-"))
        assert got == version, (
            "%s asks for %s==%s, requirements/lock/%s.txt has %s"
            % (req, pkg, version, variant, got))


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.name)
def test_a_build_replays_a_lock_and_never_resolves_one(df):
    """⚠️`pip install <name>` in a Dockerfile is the PVC install with extra steps."""
    text = df.read_text()
    for line in re.findall(r"^\s*pip install.*", text, re.M):
        assert "--require-hashes" in line and "-r " in line, (
            "%s: `%s` installs without a lock" % (df.name, line.strip()))
        assert "--no-deps" in line, (
            "%s: `%s` re-runs the resolver at build time" % (df.name, line.strip()))


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.name)
def test_an_image_does_not_end_as_root(df):
    """The PVC the workers share has files owned by a uid; a root worker is not that uid."""
    users = re.findall(r"^USER\s+(\S+)", df.read_text(), re.M)
    assert users, "%s never sets USER" % df.name
    assert users[-1] == "65532:65532", (
        "%s ends as USER %s; every variant ends as the fixed nonroot uid 65532"
        % (df.name, users[-1]))


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.name)
def test_a_dockerfile_copies_only_its_own_lock(df):
    """A stray COPY is how application code leaks into a base image."""
    for src, _dst in re.findall(r"^COPY\s+(\S+)\s+(\S+)", df.read_text(), re.M):
        assert src.startswith("requirements/lock/"), (
            "%s copies %s into a BASE image; application code and model weights belong to a "
            "service image (deploy/images/README.md)" % (df.name, src))


def test_ci_runs_the_images_workflow_and_publishes_only_from_main():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "uses: ./.github/workflows/images.yml" in ci, (
        "ci.yml does not run the images workflow, so nothing builds the base images on a PR")
    assert "github.ref == 'refs/heads/main'" in ci, (
        "ci.yml must gate `publish` on main -- a branch build may not push an image")


def test_the_images_workflow_builds_every_cpu_variant():
    """⚠️A variant nothing builds is a lock file nobody notices going stale."""
    wf = (ROOT / ".github" / "workflows" / "images.yml").read_text()
    for variant in ("runtime", "numeric", "ml-cpu"):
        assert "deploy/images/Dockerfile.%s" % variant in wf, (
            "the images workflow never builds %s" % variant)
        assert "gen_pip_lock.py --check" in wf
    # ml-gpu is the documented exception: not built on a hosted runner, lock still regenerated
    # nowhere -- so the offline header check is what covers it, and it must be present.
    assert "ml-gpu" in wf, (
        "the images workflow must say why it does not build ml-gpu, or build it")


def test_no_image_is_referenced_by_a_moving_tag_in_the_workflow():
    wf = (ROOT / ".github" / "workflows" / "images.yml").read_text()
    assert ":latest" not in wf, "a manifest references a digest; `latest` is not a version"
    assert "${{ github.sha }}" in wf, "images are tagged with the commit that built them"


def test_the_scan_gates_and_its_exceptions_expire():
    """⚠️An ignore with no expiry is a permanent allowlist, and a scanner with one is a badge."""
    wf = (ROOT / ".github" / "workflows" / "images.yml").read_text()
    assert 'exit-code: "1"' in wf, "the Trivy step must fail the build, not decorate it"
    assert "trivyignores: deploy/images/trivyignore.yaml" in wf

    ignore = yaml.safe_load((IMAGES / "trivyignore.yaml").read_text())
    locked = set()
    for lock in sorted((ROOT / "requirements" / "lock").glob("*.txt")):
        locked |= {p.lower().replace("_", "-") for p in pins(lock)}
    for entry in ignore.get("vulnerabilities", []):
        assert entry.get("expired_at"), (
            "%s has no expired_at -- an accepted finding is accepted until a date"
            % entry.get("id"))
        assert entry.get("statement"), (
            "%s has no statement -- why it is accepted is the whole record" % entry.get("id"))
        for pkg in locked:
            assert pkg not in entry["statement"].split(), (
                "%s accepts a finding in %s, which is in a lock file: move the pin instead"
                % (entry.get("id"), pkg))
