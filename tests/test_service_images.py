"""A service image is one workload's code and closure -- and it must contain exactly that.

`tests/test_image_pins.py` guards the *base* images. This module guards the images that carry
application code, and the manifests that would run them. The failure this exists to prevent is a
"packaging" change that quietly moves something else: a dependency that CI never tested, a
manifest that also lost a probe, a ConfigMap deleted in the same breath as the cutover that was
supposed to keep it as the rollback artifact, or an `image:` field pointing at a tag.

⚠️These run WITHOUT docker and WITHOUT the network. The half that needs a daemon -- does the
built image actually contain that file, at that hash, on that uid -- is the content check in
`.github/workflows/images.yml`, which reads the built image rather than the Dockerfile.
"""
import pathlib
import re
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGES = ROOT / "deploy" / "images"
SERVICE = IMAGES / "service"
K8S = ROOT / "deploy" / "k8s"
sys.path.insert(0, str(IMAGES))
sys.path.insert(0, str(K8S))

import gen_configmap as GC  # noqa: E402
import gen_pip_lock  # noqa: E402

SERVICES = sorted(gen_pip_lock.SERVICES)

#: An image reference that is syntactically a digest and can never resolve. It is what a proposed
#: manifest carries until its real digest is published and recorded, so an accidental apply fails
#: on a pull that cannot succeed instead of pulling whatever a tag points at today.
ZERO_DIGEST = "sha256:" + "0" * 64
PENDING = "pending"

#: The only parents a service image may have: the base variants, handed in by digest by the
#: images workflow. A service that starts FROM an upstream tag has reintroduced the problem.
PARENT_ARGS = ("${RUNTIME_IMAGE}", "${NUMERIC_IMAGE}", "${ML_CPU_IMAGE}", "${ML_GPU_IMAGE}")

#: workload -> the ConfigMap bundle it replaces. The bundle stays generated and guarded for the
#: whole lane; it is the rollback artifact and is retired in step 9, not in a cutover.
BUNDLE_FOR = {"hear-heartbeat": "hear-heartbeat-code"}

#: workload -> why it does not adopt the base image's nonroot uid, or None if it must.
#: docs/worker-packaging.md: the uid change is a separate, later, per-volume change, because
#: `local-path` volumes get no `fsGroup` management and the existing files are root-owned.
ROOT_BECAUSE_OF_STATE = {"hear-heartbeat": "/state"}


def dockerfile(workload):
    return SERVICE / ("Dockerfile.%s" % workload)


def manifest_path(workload):
    return K8S / ("%s.yaml" % workload)


def proposed_path(workload):
    return K8S / ("%s.proposed.yaml" % workload)


def digest_rows():
    """workload -> (repository, tag, digest), from deploy/images/service/digests.txt."""
    out = {}
    for line in (SERVICE / "digests.txt").read_text().splitlines():
        line = line.split("#", 1)[0].split()
        if not line:
            continue
        assert len(line) == 4, "malformed digests.txt row: %r" % (line,)
        out[line[0]] = tuple(line[1:])
    return out


def copies(workload):
    """[(src, dst)] for every COPY in a service Dockerfile."""
    return re.findall(r"^COPY\s+(\S+)\s+(\S+)", dockerfile(workload).read_text(), re.M)


def docs_of(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def one(path, kind):
    return next(d for d in docs_of(path) if d["kind"] == kind)


def container(doc):
    return doc["spec"]["template"]["spec"]["containers"][0]


# ------------------------------------------------------------------ the files exist at all

@pytest.mark.parametrize("workload", SERVICES)
def test_every_service_has_a_dockerfile_a_lock_and_a_proposed_manifest(workload):
    _, _, req = gen_pip_lock.SERVICES[workload]
    assert dockerfile(workload).exists(), "no deploy/images/service/Dockerfile.%s" % workload
    assert (ROOT / req).exists(), "%s names a missing %s" % (workload, req)
    lock = ROOT / gen_pip_lock.lock_path(gen_pip_lock.SERVICE_PREFIX + workload)
    assert lock.exists(), (
        "no %s -- run `python3 deploy/images/gen_pip_lock.py service:%s`"
        % (lock.relative_to(ROOT), workload))
    assert manifest_path(workload).exists(), (
        "%s names no workload manifest in deploy/k8s" % workload)
    assert proposed_path(workload).exists(), (
        "%s has no deploy/k8s/%s.proposed.yaml -- the cutover is not reviewable without the "
        "manifest it would apply" % (workload, workload))


@pytest.mark.parametrize("workload", SERVICES)
def test_every_locked_service_requirement_carries_a_hash(workload):
    lock = (ROOT / gen_pip_lock.lock_path(
        gen_pip_lock.SERVICE_PREFIX + workload)).read_text()
    body = [ln for ln in lock.splitlines() if ln and not ln.startswith("#")]
    assert body, "the %s lock locks nothing" % workload
    names = [ln for ln in body if "==" in ln]
    hashes = [ln for ln in body if "--hash=sha256:" in ln]
    assert len(names) == len(hashes) and len(names) * 2 == len(body), (
        "the %s lock has %d pins and %d hashes in %d lines -- every requirement needs exactly "
        "one --hash, or --require-hashes will reject the file in the build"
        % (workload, len(names), len(hashes), len(body)))
    for ln in hashes:
        assert re.fullmatch(r"\s+--hash=sha256:[0-9a-f]{64}", ln), "malformed hash line: %r" % ln


# ------------------------------------------------------------------ what the image is built from

@pytest.mark.parametrize("workload", SERVICES)
def test_a_service_image_is_built_on_a_base_variant_and_never_on_a_tag(workload):
    froms = re.findall(r"^FROM\s+(\S+)", dockerfile(workload).read_text(), re.M)
    assert froms, "Dockerfile.%s never says FROM" % workload
    for ref in froms:
        assert ref in PARENT_ARGS, (
            "Dockerfile.%s starts FROM %s; a service image builds on one of the base variants "
            "(%s), which the images workflow passes in by digest"
            % (workload, ref, ", ".join(PARENT_ARGS)))
    assert re.search(r"^ARG\s+SOURCE_COMMIT", dockerfile(workload).read_text(), re.M), (
        "Dockerfile.%s has no SOURCE_COMMIT ARG -- a running container must resolve to the "
        "commit its code came from without trusting the tag" % workload)


@pytest.mark.parametrize("workload", SERVICES)
def test_a_service_build_replays_its_own_lock_and_never_resolves_one(workload):
    text = dockerfile(workload).read_text()
    lock = gen_pip_lock.lock_path(gen_pip_lock.SERVICE_PREFIX + workload)
    installs = re.findall(r"^\s*RUN pip install.*", text, re.M)
    assert installs, "Dockerfile.%s installs nothing -- a service with no closure needs no lock"
    for line in installs:
        assert "--require-hashes" in line and "-r " in line, (
            "Dockerfile.%s: `%s` installs without a lock" % (workload, line.strip()))
        assert "--no-deps" in line, (
            "Dockerfile.%s: `%s` re-runs the resolver at build time" % (workload, line.strip()))
    srcs = [src for src, _ in copies(workload)]
    assert lock in srcs, (
        "Dockerfile.%s never copies %s -- it is installing from some other file" % (workload, lock)
    )
    strays = [s for s in srcs if s.startswith("requirements/lock/") and s != lock]
    assert not strays, (
        "Dockerfile.%s copies another workload's lock (%s); a service image carries its own "
        "closure, not a shared one" % (workload, ", ".join(strays)))


@pytest.mark.parametrize("workload", SERVICES)
def test_a_service_image_copies_only_files_that_exist_in_the_checkout(workload):
    for src, _dst in copies(workload):
        assert (ROOT / src).is_file(), (
            "Dockerfile.%s copies %s, which is not a file in this checkout" % (workload, src))


@pytest.mark.parametrize("workload", SERVICES)
def test_the_image_carries_exactly_the_code_its_rollback_artifact_carries(workload):
    """⚠️THE IMAGE AND ITS OWN ROLLBACK ARTIFACT MAY NOT CONTAIN DIFFERENT CODE.

    The ConfigMap stays applied as the rollback path for the whole lane. If the image were built
    from a different set of source files than the bundle ships, a rollback would not be a
    rollback -- it would be a second, unreviewed code change.
    """
    bundle = BUNDLE_FOR[workload]
    _app, code, _data = GC.BUNDLES[bundle]
    want = sorted(src for _key, src in code)
    got = sorted(src for src, _dst in copies(workload) if not src.startswith("requirements/"))
    assert got == want, (
        "the %s image copies %s; ConfigMap %s ships %s. The image and the rollback artifact must "
        "carry the same code." % (workload, got, bundle, want))


@pytest.mark.parametrize("workload", SERVICES)
def test_the_code_lands_where_the_configmap_mounted_it(workload):
    """Same path across the cutover: the entrypoint, the probes and the docs stay true."""
    bundle = BUNDLE_FOR[workload]
    mounts = {}
    for c in [container(one(manifest_path(workload), "Deployment"))]:
        for m in c.get("volumeMounts") or []:
            if m.get("subPath"):
                mounts[m["subPath"]] = m["mountPath"]
    _app, code, _data = GC.BUNDLES[bundle]
    for key, src in code:
        dst = dict(copies(workload))[src]
        assert dst == mounts[key], (
            "the image puts %s at %s; the ConfigMap mounts it at %s" % (src, dst, mounts[key]))


@pytest.mark.parametrize("workload", SERVICES)
def test_the_entrypoint_is_exec_form_and_is_what_the_shell_preamble_exec_d(workload):
    """⚠️The cutover deletes a `pip install`-then-`exec` shell. It may not change the argv."""
    text = dockerfile(workload).read_text()
    found = re.findall(r'^ENTRYPOINT\s+(\[.*\])\s*$', text, re.M)
    assert len(found) == 1, (
        "Dockerfile.%s must declare exactly one exec-form ENTRYPOINT (shell form would put a "
        "shell back in front of the process and swallow signals)" % workload)
    argv = yaml.safe_load(found[0])
    old = container(one(manifest_path(workload), "Deployment"))
    exec_line = re.search(r"^\s*exec (.+)$", "\n".join(old["args"]), re.M)
    assert exec_line, "the pre-cutover %s manifest has no `exec` line to compare against" % workload
    assert argv == exec_line.group(1).split(), (
        "the image runs %s; the manifest it replaces exec's `%s`"
        % (argv, exec_line.group(1)))


@pytest.mark.parametrize("workload", SERVICES)
def test_a_service_image_runs_as_root_only_where_state_ownership_requires_it(workload):
    """⚠️`USER root` is a decision with a reason, or it is an oversight.

    The base images end as uid 65532 and `tests/test_image_pins.py` enforces that. A service
    image may override it *only* for a workload that writes an existing root-owned PVC path,
    because `local-path` volumes get no `fsGroup` ownership management -- and then the reason has
    to be written down next to it, because the day that volume is re-owned this line changes.
    """
    text = dockerfile(workload).read_text()
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    why = ROOT_BECAUSE_OF_STATE.get(workload)
    if why is None:
        assert not users or users[-1] == "65532:65532", (
            "Dockerfile.%s ends as USER %s with no state-ownership reason recorded in "
            "ROOT_BECAUSE_OF_STATE" % (workload, users[-1]))
        return
    assert users and users[-1] in ("root", "root:root", "0", "0:0"), (
        "Dockerfile.%s is listed as needing root for %s but ends as USER %s"
        % (workload, why, users[-1] if users else "(inherited 65532)"))
    for token in (why, "fsGroup", "local-path", "docs/worker-packaging.md"):
        assert token in text, (
            "Dockerfile.%s runs as root without recording why: the justification must name %s"
            % (workload, token))


# ------------------------------------------------------------------ digests and identity

@pytest.mark.parametrize("workload", SERVICES)
def test_every_service_has_a_digest_record(workload):
    rows = digest_rows()
    assert workload in rows, (
        "deploy/images/service/digests.txt has no row for %s -- `what ran that day` must be "
        "answerable from git alone" % workload)
    repository, tag, digest = rows[workload]
    assert "/" in repository and ":" not in repository and "@" not in repository, (
        "%s: the repository column is a bare repository, without a tag or a digest" % workload)
    assert digest == PENDING or re.fullmatch(r"sha256:[0-9a-f]{64}", digest), (
        "%s: digest %r is neither `pending` nor a sha256" % (workload, digest))
    assert (tag == PENDING) == (digest == PENDING), (
        "%s: the tag and the digest must be recorded together" % workload)
    assert digest != ZERO_DIGEST, (
        "%s: the all-zero digest is the not-published-yet sentinel for a manifest; it is never a "
        "recorded digest" % workload)


@pytest.mark.parametrize("workload", SERVICES)
def test_a_proposed_manifest_references_the_recorded_digest_and_never_a_tag(workload):
    """⚠️Until the digest exists, the manifest must be unappliable -- not optimistic."""
    repository, _tag, digest = digest_rows()[workload]
    image = container(one(proposed_path(workload), "Deployment"))["image"]
    assert "@sha256:" in image, (
        "%s.proposed.yaml names %s; a migrated manifest carries a digest, never a tag"
        % (workload, image))
    got_repo, _, got_digest = image.partition("@")
    assert got_repo == repository, (
        "%s.proposed.yaml pulls from %s, digests.txt records %s"
        % (workload, got_repo, repository))
    if digest == PENDING:
        assert got_digest == ZERO_DIGEST, (
            "%s has no published digest yet, so %s.proposed.yaml must carry the all-zero "
            "sentinel and stay unappliable -- it carries %s"
            % (workload, workload, got_digest))
    else:
        assert got_digest == digest, (
            "%s.proposed.yaml runs %s; digests.txt records %s"
            % (workload, got_digest, digest))


@pytest.mark.parametrize("workload", SERVICES)
def test_the_applied_manifest_is_still_the_pre_cutover_one(workload):
    """The proposed manifest is a *proposal*: deploy/k8s/<workload>.yaml stays the rollback path.

    If somebody edits the applied manifest to the image as well, the repository no longer records
    what to roll back *to*, and the ConfigMap's only remaining reader is a test.
    """
    c = container(one(manifest_path(workload), "Deployment"))
    assert "@sha256:" not in c["image"], (
        "deploy/k8s/%s.yaml already names a digest -- the cutover and its rollback artifact have "
        "been collapsed into one file" % workload)
    volumes = {v["name"] for v in one(manifest_path(workload),
                                      "Deployment")["spec"]["template"]["spec"]["volumes"]}
    assert "code" in volumes, (
        "deploy/k8s/%s.yaml no longer mounts its ConfigMap; the rollback path is gone" % workload)


# ------------------------------------------------------------------ the cutover diff itself

@pytest.mark.parametrize("workload", SERVICES)
def test_the_proposed_manifest_changes_only_packaging(workload):
    """⚠️THE WHOLE REVIEW, MECHANISED. Parsed, not diffed: formatting must not decide it.

    docs/worker-packaging.md permits a cutover to change exactly the image, the entrypoint and
    the `code`/`deps` volumes. Everything else -- hostNetwork, dnsPolicy, ports, Services, env
    and secretKeyRefs, probes, resources, replicas, strategy, PVC claims and mount paths -- is
    out of bounds, and "I only meant to change the packaging" is exactly what a reviewer cannot
    verify by eye on a 100-line manifest.
    """
    old_docs, new_docs = docs_of(manifest_path(workload)), docs_of(proposed_path(workload))
    assert [d["kind"] for d in old_docs] == [d["kind"] for d in new_docs], (
        "the proposed manifest declares different objects than the one it replaces")

    for old, new in zip(old_docs, new_docs):
        if old["kind"] != "Deployment":
            assert old == new, (
                "the proposed manifest changes the %s; a packaging cutover touches the "
                "Deployment and nothing else" % old["kind"])
            continue

        old_c, new_c = container(old), container(new)
        assert "command" not in new_c and "args" not in new_c, (
            "the proposed %s container still carries command/args -- the image ENTRYPOINT is "
            "what runs, and a restated argv is a second place to drift" % workload)
        assert "pip install" not in yaml.safe_dump(new), (
            "the proposed %s manifest still installs a package at pod start" % workload)

        drop = {"code", "deps"}
        old_norm = yaml.safe_load(yaml.safe_dump(old))
        c = container(old_norm)
        for key in ("image", "command", "args"):
            c.pop(key, None)
        c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] not in drop]
        spec = old_norm["spec"]["template"]["spec"]
        spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in drop]

        new_norm = yaml.safe_load(yaml.safe_dump(new))
        container(new_norm).pop("image", None)

        assert old_norm == new_norm, (
            "the proposed %s Deployment differs from the applied one by more than the image, the "
            "entrypoint and the code/deps volumes" % workload)


@pytest.mark.parametrize("workload", SERVICES)
def test_the_cutover_keeps_the_state_mount_and_drops_only_code_and_deps(workload):
    old = one(manifest_path(workload), "Deployment")
    new = one(proposed_path(workload), "Deployment")
    old_vols = {v["name"] for v in old["spec"]["template"]["spec"]["volumes"]}
    new_vols = {v["name"] for v in new["spec"]["template"]["spec"]["volumes"]}
    assert old_vols - new_vols == {"code", "deps"}, (
        "the cutover removes %s; it may remove only the code and deps volumes"
        % sorted(old_vols - new_vols))
    assert not new_vols - old_vols, "the cutover adds volumes: %s" % sorted(new_vols - old_vols)
    for v in new["spec"]["template"]["spec"]["volumes"]:
        if v["name"] == "state":
            assert v == next(o for o in old["spec"]["template"]["spec"]["volumes"]
                             if o["name"] == "state"), (
                "the cutover changed the state volume; durable state is out of bounds")
    mounts = {m["mountPath"] for m in container(new).get("volumeMounts") or []}
    assert "/state" in mounts, "the cutover dropped the durable state mount"


@pytest.mark.parametrize("workload", SERVICES)
def test_the_configmap_stays_generated_guarded_and_applied(workload):
    """Retirement is step 9 of the sequence, and never part of a cutover."""
    bundle = BUNDLE_FOR[workload]
    assert bundle in GC.BUNDLES, (
        "%s was dropped from gen_configmap.BUNDLES in a cutover change; the rollback artifact "
        "stops being generated the moment it leaves that table" % bundle)
    assert (K8S / ("%s.yaml" % bundle)).exists(), (
        "deploy/k8s/%s.yaml is gone -- there is nothing left to roll back to" % bundle)
    assert "code" not in {v["name"] for v in one(proposed_path(workload),
                                                 "Deployment")["spec"]["template"]["spec"]["volumes"]}, (
        "the proposed manifest still mounts the ConfigMap; it must be applied and *unreferenced*")


# ------------------------------------------------------------------ CI covers it

@pytest.mark.parametrize("workload", SERVICES)
def test_the_images_workflow_builds_and_content_checks_every_service(workload):
    """⚠️A build nobody runs is a Dockerfile, not an image."""
    wf = (ROOT / ".github" / "workflows" / "images.yml").read_text()
    assert "deploy/images/service/Dockerfile.%s" % workload in wf, (
        "the images workflow never builds %s" % workload)
    assert "SOURCE_COMMIT=" in wf, (
        "the images workflow must stamp the source commit into every service image")
    assert "sha256sum" in wf, (
        "the images workflow must compare the code *inside* the built %s image against the "
        "checkout -- reading the Dockerfile only proves what was asked for" % workload)


def test_the_service_lane_documents_itself_and_is_wired_to_the_plan():
    readme = (SERVICE / "README.md").read_text()
    assert "docs/worker-packaging.md" in readme, (
        "deploy/images/service/README.md must name the migration plan it executes")
    assert "Rollback" in readme and "rollout undo" in readme, (
        "a cutover procedure without its rollback is half a procedure")
    for workload in SERVICES:
        assert workload in readme, "%s is undocumented in the service README" % workload
    parent = (IMAGES / "README.md").read_text()
    assert "service/" in parent, (
        "deploy/images/README.md still says the service directory does not exist")
