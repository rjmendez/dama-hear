"""The code the cluster runs must be the code in the checkout.

⚠️EACH CONFIGMAP IS A SECOND COPY OF ITS MODULES. `hear-drain-code.yaml` embeds hear/pool.py,
hear/scenefile.py, hear/detsfile.py, hear/sketch.py, hear/corpus.py and tools/hear_drain.py
verbatim; `hear-score-code.yaml` embeds hear/sketch.py, modules/supersonic/classify.py,
tools/hear_score.py and the three model files. The k3s workloads import THOSE, not the ones in
this tree. A fix landed in `hear/` and not regenerated is a fix the cluster does not have -- and
nothing said so: a ConfigMap is written whole on every apply, so the drift is invisible from the
cluster and invisible from a diff of the source file that was changed.

⚠️THE TEST WALKS `BUNDLES`, IT DOES NOT NAME THE FILES. A guard that hardcoded one bundle went
green while the other drifted, and hear/sketch.py is in BOTH -- one edit, two ConfigMaps to
regenerate. Every bundle gen_configmap.py can emit is checked, so adding a bundle cannot add an
unguarded copy.

This test is the guard. It fails whenever a shipped file differs from its copy in the YAML, and
the fix is always the same one line, never an edit to the YAML:

    python3 deploy/k8s/gen_configmap.py <bundle> > deploy/k8s/<bundle>.yaml

It deliberately does NOT check the `dama-hear/commit` annotation, which is a stamp of when the
file was generated and legitimately says `-dirty` in a working tree (the committed copy did).
"""
import json
import os
import pathlib
import re
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / "deploy" / "k8s" / "gen_configmap.py"


def _embedded(yaml_path):
    """{key: text} out of the ConfigMap, undoing the four-space block indent."""
    if not yaml_path.exists():
        pytest.skip("no ConfigMap at %s" % yaml_path)
    text = yaml_path.read_text()
    # the file's own final newline is not part of the last block
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
    out, key, buf = {}, None, []
    for line in lines:
        if line.startswith("  ") and line.rstrip().endswith(": |") and not line.startswith("    "):
            if key:
                out[key] = "\n".join(buf)
            key, buf = line.strip()[:-3], []
            continue
        if key is not None:
            buf.append(line[4:] if line.startswith("    ") else line)
    if key:
        out[key] = "\n".join(buf)
    return out


def _gen():
    ns = {"__file__": str(GEN), "__name__": "gen_configmap"}
    exec(compile(GEN.read_text(), str(GEN), "exec"), ns)   # noqa: S102 -- our own file
    return ns


def _every_shipped_file():
    """(bundle, key, repo-relative path) for every file in every bundle, code and data alike."""
    out = []
    for bundle, (_app, code, data) in sorted(_gen()["BUNDLES"].items()):
        for key, rel in list(code) + list(data):
            out.append((bundle, key, rel))
    return out


@pytest.mark.parametrize("bundle,key,rel", _every_shipped_file(),
                         ids=lambda v: v if isinstance(v, str) else str(v))
def test_the_shipped_copy_matches_the_checkout(bundle, key, rel):
    embedded = _embedded(ROOT / "deploy" / "k8s" / (bundle + ".yaml"))
    assert key in embedded, "%s is not in %s at all" % (key, bundle)
    want = (ROOT / rel).read_text()
    assert embedded[key] == want, (
        "%s has drifted from %s -- the cluster is running the old one. Regenerate:\n"
        "    python3 deploy/k8s/gen_configmap.py %s > deploy/k8s/%s.yaml"
        % (key, rel, bundle, bundle))


def test_every_bundle_has_a_checked_in_configmap():
    """A bundle gen_configmap.py can emit but nobody generated is a workload with no code."""
    missing = [b for b in _gen()["BUNDLES"]
               if not (ROOT / "deploy" / "k8s" / (b + ".yaml")).exists()]
    assert not missing, "no checked-in ConfigMap for: %s" % ", ".join(sorted(missing))


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_the_import_closure_check_still_runs(bundle):
    """gen_configmap.check() is what stops a new import shipping a workload that crashes only in
    the cluster. If it ever stops raising, this test is the one that says so."""
    ns = _gen()
    _app, code, data = ns["BUNDLES"][bundle]
    ns["check"](code, data)      # raises SystemExit if the closure is incomplete


# ---------------------------------------------------------------- the pod, not just the map

def _manifest_for(bundle):
    """`hear-drain-code` -> deploy/k8s/hear-drain.yaml. The workload is the bundle without the
    `-code` suffix, which is the naming every bundle here already follows."""
    return ROOT / "deploy" / "k8s" / (bundle[:-len("-code")] + ".yaml")


def _code_mounts(text):
    """{container name: set of subPaths mounted from the `code` volume}.

    ⚠️IT LOCATES `containers:` FIRST RATHER THAN MATCHING `- name:` ANYWHERE. The volume list
    also spells `- name: code` and `- name: pool`, so a bare `- name:` scan invents two extra
    "containers" that mount nothing -- and a guard that then required every container to mount
    every key would fail on them, while one that skipped empty ones would stop noticing a
    container whose mounts were dropped wholesale. Both failures are avoided by only ever
    treating something under a `containers:` key as a container.
    """
    out, name = {}, None
    depth = None
    for line in text.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if depth is not None and indent <= depth and not line.lstrip().startswith("-"):
            depth, name = None, None
        if re.match(r"\s*containers:\s*$", line):
            depth, name = indent, None
            continue
        if depth is None:
            continue
        m = re.match(r"\s*- name: (\S+)\s*$", line)
        if m and indent > depth:
            name = m.group(1)
            out.setdefault(name, set())
            continue
        m = re.search(r"name: code,.*subPath: (\S+?)\s*\}", line)
        if m and name:
            out[name].add(m.group(1))
    return out


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_every_bundle_key_is_mounted_by_every_container_that_uses_it(bundle):
    """⚠️THE SEAM THAT BREAKS ONLY IN THE CLUSTER, CHECKED FOR EVERY BUNDLE.

    The tests above prove the ConfigMap CONTENT matches the checkout and `gen_configmap.check()`
    proves the import closure is complete. Neither looks at the pod. The `code` volume mounts
    file-by-file by `subPath`, so a file added to a bundle without a matching `volumeMount` gives
    a ConfigMap that HAS the module and a container that raises ModuleNotFoundError on a timer --
    green generation, green sync test, dead workload.

    ⚠️THIS WALKS `BUNDLES` FOR THE SAME REASON THE CONTENT GUARD ABOVE DOES. The per-manifest
    copies of this check in tests/test_hear_drain_manifest.py and tests/test_hear_tag.py each
    name ONE bundle, so hear-score-code had no mount guard at all -- exactly the hardcoding this
    module's docstring warns about, reintroduced one manifest at a time.
    """
    _app, code, data = _gen()["BUNDLES"][bundle]
    keys = {k for k, _rel in list(code) + list(data)}
    manifest = _manifest_for(bundle)
    assert manifest.exists(), "bundle %r has no workload manifest at %s" % (bundle, manifest)
    mounts = _code_mounts(manifest.read_text())
    assert mounts, "no containers parsed out of %s" % manifest.name
    for container, got in sorted(mounts.items()):
        assert got == keys, (
            "%s container %r mounts %r but bundle %s ships %r -- the difference is %r, which is "
            "either a module the pod cannot import or a mount of a key that does not exist"
            % (manifest.name, container, sorted(got), bundle, sorted(keys),
               sorted(keys ^ got)))


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_every_mount_path_matches_the_bundle_path(bundle):
    """A key mounted at the wrong path imports as a different module, or as none."""
    by_key = {k: rel for k, rel in list(_gen()["BUNDLES"][bundle][1])
              + list(_gen()["BUNDLES"][bundle][2])}
    manifest = _manifest_for(bundle)
    seen = 0
    for line in manifest.read_text().splitlines():
        m = re.search(r"mountPath: (\S+?),\s*subPath: (\S+?)\s*\}", line)
        if not m:
            continue
        path, key = m.groups()
        assert key in by_key, "%s mounts %r, which is in no bundle" % (manifest.name, key)
        assert path == "/app/" + by_key[key], (
            "%s is mounted at %s but bundle %s says %s" % (key, path, bundle, by_key[key]))
        seen += 1
    assert seen >= 2 * len(by_key), (
        "only %d mounts parsed out of %s; the parser missed some" % (seen, manifest.name))


#: `kubectl apply` writes the whole object into the `kubectl.kubernetes.io/last-applied-
#: configuration` ANNOTATION, and an annotation may not exceed 256 KiB. It is not the 1 MiB
#: ConfigMap limit and it bites at a quarter of it.
ANNOTATION_CAP = 256 * 1024

#: The API server's hard cap on one object. `--server-side` stores managed fields instead of the
#: annotation and clears the cap above, but it does NOT lift this one.
OBJECT_CAP = 1024 * 1024


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_a_bundle_still_fits_the_way_it_declares_it_is_applied(bundle):
    """A bundle must fit the cap belonging to the apply mode it declares, and must declare the
    mode its own size demands.

    ⚠️SIZE THE SERIALISED OBJECT, NOT THE .yaml FILE. What lands in the annotation is the JSON
    kubectl is about to send. The two differ by thousands of bytes in BOTH directions: YAML
    block-scalar indentation adds two spaces per source line, while JSON escapes every newline
    into two characters and drops the indentation entirely. Measuring the file is a proxy wrong
    by more than the headroom it guards -- on 2026-09-10 the file metric failed hear-drain-code
    at 269,381 B while its serialised object was 260,833 B and the live API server answered
    `configmap/hear-drain-code configured (server dry run)`. A test that refuses what the
    cluster accepts sends the next reader to `--validate=false`, which does not help.

    All four states verified against the live cluster the same day:

        hear-drain-code  260,833 B  client  -> "configured"
        hear-tdoa-code   491,138 B  client  -> "metadata.annotations: Too long"
        hear-tdoa-code   491,138 B  server  -> "serverside-applied"
        hear-drain-code  300,849 B  client  -> "metadata.annotations: Too long"  (bloated on purpose)

    THE FIX FOR AN OVERSIZE BUNDLE IS NOT `--validate=false`: it is `--server-side`, which is
    what `dama-hear/apply-mode` records, or splitting the bundle. Past OBJECT_CAP neither helps.
    """
    path = ROOT / "deploy" / "k8s" / (bundle + ".yaml")
    if not path.exists():
        pytest.skip("no ConfigMap at %s" % path)
    doc = yaml.safe_load(path.read_text())
    size = len(json.dumps(doc, separators=(",", ":")))
    mode = (doc.get("metadata", {}).get("annotations", {}) or {}).get("dama-hear/apply-mode")
    assert mode in ("client", "server"), (
        "%s declares apply-mode %r; gen_configmap.py writes it and the caller needs it to pick "
        "between `kubectl apply` and `kubectl apply --server-side`." % (path.name, mode))

    # the load-bearing half: a bundle that has outgrown client-side apply must SAY so, or the
    # next redeploy is the thing that finds out
    if size > ANNOTATION_CAP:
        assert mode == "server", (
            "%s serialises to %d B, over the %d B annotation cap, but declares apply-mode "
            "'client' -- `kubectl apply` will refuse it with metadata.annotations: Too long. "
            "Regenerate it: gen_configmap.py picks the mode from this same number."
            % (path.name, size, ANNOTATION_CAP))

    cap = OBJECT_CAP if mode == "server" else ANNOTATION_CAP
    assert size < cap, (
        "%s serialises to %d B, over the %d B cap for apply-mode '%s'.%s"
        % (path.name, size, cap, mode,
           "" if mode == "client" else
           " --server-side does NOT lift the object cap -- the bundle has to be split."))
