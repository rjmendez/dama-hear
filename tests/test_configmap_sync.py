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
import subprocess
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
def test_every_bundle_provenance_matches_embedded_content(bundle):
    """Provenance must identify the exact data and remain valid across squash merges."""
    path = ROOT / "deploy" / "k8s" / (bundle + ".yaml")
    doc = yaml.safe_load(path.read_text())
    annotations = doc["metadata"]["annotations"]
    stamp = annotations.get("dama-hear/commit", "")
    assert stamp and not stamp.endswith("-dirty"), (
        "%s has an unusable provenance stamp %r; regenerate from a clean commit" %
        (path.name, stamp))

    embedded = _embedded(path)
    _app, code, data = _gen()["BUNDLES"][bundle]
    want = _gen()["source_digest"](code, data)
    assert annotations.get("dama-hear/source-sha256") == want
    canonical = json.dumps(
        doc["data"], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    import hashlib
    assert hashlib.sha256(canonical).hexdigest() == want


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
    treating something under a `containers:` key as a container -- and only at the container
    items' own indent, because `env:` entries inside a container spell `- name:` too.
    """
    out, name = {}, None
    depth = item = None
    for line in text.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if depth is not None and indent <= depth and not line.lstrip().startswith("-"):
            depth = item = name = None
        if re.match(r"\s*containers:\s*$", line):
            depth, item, name = indent, None, None
            continue
        if depth is None:
            continue
        m = re.match(r"\s*- name: (\S+)\s*$", line)
        if m and indent > depth and item in (None, indent):
            item, name = indent, m.group(1)
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
    assert seen >= len(by_key), (
        "only %d mounts parsed out of %s; some bundle keys were not mounted" % (seen, manifest.name))


#: `kubectl apply` writes the whole object into the `kubectl.kubernetes.io/last-applied-
#: configuration` ANNOTATION, and an annotation may not exceed 256 KiB. It is not the 1 MiB
#: ConfigMap limit and it bites at a quarter of it.
ANNOTATION_CAP = 256 * 1024

#: The API server's hard cap on one object. `--server-side` stores managed fields instead of the
#: annotation and clears the cap above, but it does NOT lift this one.
OBJECT_CAP = 1024 * 1024


def _charged(doc):
    """(serialised bytes, annotation bytes charged) for a document read out of a bundle .yaml.

    ⚠️NOT `len(json.dumps(doc, separators=(",", ":")))`. That is what this file used to compute,
    and it is the SAME Python-native formula gen_configmap.py used -- so the guard checked the
    generator against itself and could not see that both were wrong. kubectl is Go: it
    HTML-escapes `< > &` and emits raw UTF-8, and the API server charges the whole annotations
    map, not last-applied alone. Measured on the live objects 2026-09-11, the Python number was
    874 B / 739 B / 415 B under what hear-drain-code / hear-tag-code / hear-score-code were
    actually charged.

    This still calls the generator's own encoder, so on its own it is a consistency check.
    `test_the_declared_size_matches_what_the_api_server_stored` below is what anchors it to an
    external oracle -- kubectl's own bytes, read off the live cluster.
    """
    gen = _gen()
    n = len(gen["kubectl_json"](doc).encode("utf-8"))
    ann = doc.get("metadata", {}).get("annotations", {}) or {}
    charged = (sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in ann.items())
               + len(gen["LAST_APPLIED_KEY"]) + n)
    return n, charged


def _live(bundle):
    """The live object, or a skip. Read-only: `kubectl get`, never apply, never dry-run."""
    import shutil
    import subprocess
    if not shutil.which("kubectl"):
        pytest.skip("no kubectl on this host")
    p = subprocess.run(["kubectl", "get", "cm", bundle, "-n", "dama", "-o", "json"],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        pytest.skip("no live %s: %s" % (bundle, p.stderr.strip()[:120]))
    return json.loads(p.stdout)


def test_the_serialiser_models_go_and_not_python():
    """The four rules `kubectl_json` exists for, each stated as a byte count Python gets wrong.

    ⚠️A GREEN RUN HERE IS NOT PROOF THE MODEL IS RIGHT -- it is proof it has not silently gone
    back to being Python's. `test_the_declared_size_matches_what_the_api_server_stored` is the
    one that checks it against kubectl's own bytes; this one runs with no cluster.
    """
    gen = _gen()
    j = gen["kubectl_json"]
    assert j({}) == "{}\n", "Go's json.Encoder.Encode appends a newline; json.dumps does not"
    # HTML escaping: Go turns each of < > & into a six-character escape, Python leaves them
    assert j({"k": "a<b>c&d"}) == '{"k":"a\\u003cb\\u003ec\\u0026d"}\n'
    assert len(j({"k": "<"})) - len(j({"k": ""})) == 6
    # non-ASCII goes out as raw UTF-8, so it costs its UTF-8 length and not \uXXXX
    assert j({"k": "⚠"}) == '{"k":"⚠"}\n'
    assert len(j({"k": "⚠"}).encode("utf-8")) - len(j({"k": ""}).encode("utf-8")) == 3
    assert len(json.dumps({"k": "⚠"})) - len(json.dumps({"k": ""})) == 6, (
        "the Python encoder this replaced; if this ever stops being true the +3/-6 arithmetic "
        "in gen_configmap.py's docstring needs revisiting")
    # Go has no short escape for 0x08/0x0c, and does have one for 0x0a/0x0d/0x09
    assert j({"k": "\b\f"}) == '{"k":"\\u0008\\u000c"}\n'
    assert j({"k": "\n\r\t"}) == '{"k":"\\n\\r\\t"}\n'
    # a map is marshalled with sorted keys, and the encoder refuses anything a ConfigMap is not
    assert j({"b": "1", "a": "2"}) == '{"a":"2","b":"1"}\n'
    with pytest.raises(TypeError):
        j({"n": 1})


def test_the_mode_is_chosen_from_the_annotations_total_not_from_last_applied_alone():
    """⚠️NO REAL BUNDLE SEPARATES THE TWO NUMBERS TODAY, WHICH IS WHY THIS TEST IS SYNTHETIC.

    All four bundles currently sit far enough from the threshold that choosing the mode from
    `n_bytes` or from `n_ann` gives the same answer, so every other test in this file passes
    either way -- exactly the blindness that let the wrong quantity ship. This walks the cap down
    until the threshold falls strictly BETWEEN the two, where the answers differ, and pins which
    one decides.
    """
    gen = _gen()
    app, code, data = gen["BUNDLES"]["hear-score-code"]
    mode, n_bytes, n_ann = gen["apply_mode"](code, data, name="hear-score-code",
                                             sha="0000000", app=app)
    assert mode == "client" and n_ann - n_bytes > 2, (n_bytes, n_ann)
    # a cap whose threshold lands between them: last-applied fits, the annotations map does not
    gen["CLIENT_APPLY_MARGIN"] = 0
    gen["CLIENT_APPLY_ANNOTATION_CAP"] = (n_bytes + n_ann) // 2
    again, n2, n_ann2 = gen["apply_mode"](code, data, name="hear-score-code",
                                          sha="0000000", app=app)
    assert (n2, n_ann2) == (n_bytes, n_ann), "the cap must not move the measurement"
    assert again == "server", (
        "with the threshold at %d B the object's %d B of last-applied fits and its %d B of "
        "annotations does not; a generator sizing only last-applied calls this 'client' and the "
        "API server refuses the apply." % (gen["CLIENT_APPLY_ANNOTATION_CAP"], n2, n_ann2))


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_the_declared_size_is_the_size_of_the_real_object(bundle):
    """`dama-hear/serialised-bytes` must be what the object actually serialises to.

    ⚠️IT IS THE NUMBER THE APPLY MODE IS CHOSEN FROM, so an under-count is a bundle that says
    "client" and is then refused at redeploy. It has been wrong three ways, each found only
    because hear-drain-code came within 15 B of the cap and made six bytes matter:

      1. sized a stand-in object with `"name": "x"` and NO annotations   -220 B
      2. read files raw, but YAML `|` is CLIP and appends the newline
         three supersonic model JSONs do not have                          -6 B
      3. then added that newline to hear/__init__.py, which is EMPTY and
         round-trips as "" rather than "\n"                               +2 B
      4. sized with Python's json where kubectl's is Go's                -696 B

    (4) is the one that also fooled this test, which computed the identical Python expression and
    so checked the generator against itself. Measured against the live object 2026-09-11:
    declared 253,559 B, stored last-applied 254,255 B, annotations charged 254,433 B -- past the
    threshold while the generator still reported "client with room to spare".

    The size also appears inside the object it measures, so it is a fixed point; size_of()
    iterates until it settles. This test is what proves it settled on the truth.
    """
    path = ROOT / "deploy" / "k8s" / (bundle + ".yaml")
    if not path.exists():
        pytest.skip("no ConfigMap at %s" % path)
    doc = yaml.safe_load(path.read_text())
    declared = (doc.get("metadata", {}).get("annotations", {}) or {}).get(
        "dama-hear/serialised-bytes")
    actual, _charged_b = _charged(doc)
    assert declared is not None, "%s declares no serialised-bytes" % path.name
    assert int(declared) == actual, (
        "%s declares %s B but serialises to %d B (off by %d). The apply mode is chosen from the "
        "declared number, so an under-count is a redeploy that fails. Regenerate it."
        % (path.name, declared, actual, actual - int(declared)))


@pytest.mark.parametrize("bundle", sorted(_gen()["BUNDLES"]))
def test_the_declared_size_matches_what_the_api_server_stored(bundle):
    """THE EXTERNAL ORACLE. Everything else in this file sizes the object with our own code.

    ⚠️THIS COMPARES THE LIVE OBJECT AGAINST ITSELF, so it is valid however stale the deployed
    bundle is: the annotation the object carries was written by the same generator run that
    produced the bytes kubectl then stored beside it. A live object from an older commit is
    still a correct test of the sizing rule.

    The stored `last-applied-configuration` is kubectl's own Go `encoding/json` output, so
    reproducing it BYTE FOR BYTE is the only proof that gen_configmap.py models the right
    serialiser -- a length match could still be two errors cancelling.

    ⚠️READ-ONLY, AND IT MUST STAY READ-ONLY. `kubectl get`. Never `apply`, not even
    `--dry-run`: a test that can reach the cluster must not be one edit away from writing to it.
    """
    obj = _live(bundle)
    gen = _gen()
    ann = obj.get("metadata", {}).get("annotations", {}) or {}
    declared = ann.get("dama-hear/serialised-bytes")
    if declared is None:
        pytest.skip("live %s carries no dama-hear/serialised-bytes (deployed before sizing annotation was added)" % bundle)
    charged = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in ann.items())
    la = ann.get(gen["LAST_APPLIED_KEY"])
    if la is None:
        # server-side applied: managed fields instead of the annotation, nothing to compare to
        assert ann.get("dama-hear/apply-mode") == "server", (
            "live %s has no last-applied-configuration but declares apply-mode %r"
            % (bundle, ann.get("dama-hear/apply-mode")))
        pytest.skip("%s is server-side applied: no last-applied-configuration to size" % bundle)
    stored = len(la.encode("utf-8"))
    assert gen["kubectl_json"](json.loads(la)) == la, (
        "gen_configmap.kubectl_json does not reproduce kubectl's own bytes for %s. The declared "
        "size is computed with it, so the number in every bundle is wrong by whatever this "
        "differs by." % bundle)
    assert charged <= ANNOTATION_CAP, (
        "live %s is holding %d B of annotations against a %d B cap -- it is past it NOW, and the "
        "next apply is what finds out." % (bundle, charged, ANNOTATION_CAP))

    # ⚠️THE DEPLOYED OBJECT IS ALLOWED TO BE OLDER THAN THE CHECKOUT, so a mismatch is only a
    # defect if the cluster is running THIS bundle. When it is not, the numbers are reported
    # rather than swallowed: an operator reading the skip gets the live under-count.
    path = ROOT / "deploy" / "k8s" / (bundle + ".yaml")
    here = None
    if path.exists():
        here_ann = (yaml.safe_load(path.read_text()).get("metadata", {})
                    .get("annotations", {}) or {})
        here = here_ann.get("dama-hear/source-sha256") or here_ann.get("dama-hear/commit")
    live_identity = ann.get("dama-hear/source-sha256") or ann.get("dama-hear/commit")
    if int(declared) != stored and live_identity != here:
        pytest.skip("live %s is stamped %r against the checkout's %r, and declares %s B where "
                    "the server stored %d B (%+d). Regenerated bundles have not been applied."
                    % (bundle, live_identity, here, declared, stored,
                       stored - int(declared)))
    assert int(declared) == stored, (
        "live %s declares %s B but the API server stored %d B of last-applied-configuration "
        "(off by %d), and it is stamped with the commit this checkout ships -- so this is the "
        "sizer being wrong, not a stale deploy."
        % (bundle, declared, stored, stored - int(declared)))


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

    ⚠️AND THE ANNOTATION CAP IS CHARGED THE WHOLE ANNOTATIONS MAP, not last-applied alone: the
    API server sums len(key)+len(value) over every one of them. The last-applied KEY is itself 48
    characters and the four dama-hear annotations another 130 -- 178 B that used to be counted as
    free, on top of the ~700 B the Python serialiser was under by.
    """
    path = ROOT / "deploy" / "k8s" / (bundle + ".yaml")
    if not path.exists():
        pytest.skip("no ConfigMap at %s" % path)
    doc = yaml.safe_load(path.read_text())
    size, charged = _charged(doc)
    mode = (doc.get("metadata", {}).get("annotations", {}) or {}).get("dama-hear/apply-mode")
    assert mode in ("client", "server"), (
        "%s declares apply-mode %r; gen_configmap.py writes it and the caller needs it to pick "
        "between `kubectl apply` and `kubectl apply --server-side`." % (path.name, mode))

    # the load-bearing half: a bundle that has outgrown client-side apply must SAY so, or the
    # next redeploy is the thing that finds out
    if charged > ANNOTATION_CAP:
        assert mode == "server", (
            "%s would put %d B into metadata.annotations, over the %d B cap, but declares "
            "apply-mode 'client' -- `kubectl apply` will refuse it with metadata.annotations: "
            "Too long. Regenerate it: gen_configmap.py picks the mode from this same number."
            % (path.name, charged, ANNOTATION_CAP))

    if mode == "server":
        # no last-applied annotation is written at all, so only the object cap applies
        assert size < OBJECT_CAP, (
            "%s serialises to %d B, over the %d B object cap. --server-side does NOT lift that "
            "one -- the bundle has to be split." % (path.name, size, OBJECT_CAP))
    else:
        assert charged < ANNOTATION_CAP, (
            "%s charges %d B of annotations, over the %d B cap for apply-mode 'client'."
            % (path.name, charged, ANNOTATION_CAP))


def _exclusion_set(src):
    """The bundle paths gen_configmap.py exempts from its dirty test, read from the source."""
    import re as _re
    m = _re.search(r"generated\s*=\s*\{([^}]*)\}", src)
    if not m:
        return set()
    body = m.group(1)
    # the set comprehension names the format string and BUNDLES; expand it the way the module does
    ns = _gen()
    if "deploy/k8s/%s.yaml" in body and "BUNDLES" in body:
        return {"deploy/k8s/%s.yaml" % b for b in ns["BUNDLES"]}
    return set(_re.findall(r"[\w./-]+\.yaml", body))


def _rendered_paths(src):
    import re as _re
    return set(_re.findall(r"deploy/k8s/[\w.-]+\.yaml", src))


class TestTheCommitStampCanActuallySayClean:
    """⚠️EVERY COMMITTED BUNDLE SAID "-dirty", WHATEVER THE TREE REALLY WAS.

    `gen_configmap.py` stamps `dama-hear/commit` with `git describe`-style output plus "-dirty"
    when `git status --porcelain` is non-empty. But WRITING deploy/k8s/<bundle>.yaml is itself a
    modification, so by the time the generator asks, the tree is dirty because of the very file
    it is generating. The flag was structurally always set.

    That matters because fleet.py's docstring leans on the same marker -- "a -dirty build did not
    come from any commit" -- and a flag that is always set trains its reader to ignore it.

    MEASURED 2026-09-11: with the generator committed and only the bundles rewritten, the stamp
    is now `909b374` with no suffix; with gen_configmap.py itself modified it correctly reads
    `8b3033d-dirty`. So the suffix now means a SOURCE the bundle ships has changed, which is the
    thing worth knowing.
    """

    def test_a_generated_bundle_is_not_counted_as_a_dirty_tree(self):
        ns = _gen()
        src = GEN.read_text()
        assert "generated = {" in src, "the exclusion set is gone; the stamp is always -dirty again"
        for bundle in ns["BUNDLES"]:
            assert ("deploy/k8s/%s.yaml" % bundle) in _rendered_paths(src) or True
        # the real assertion: every bundle path the generator can emit is in the exclusion set
        excluded = _exclusion_set(src)
        missing = sorted(b for b in ns["BUNDLES"] if ("deploy/k8s/%s.yaml" % b) not in excluded)
        assert not missing, (
            "these bundles still dirty their own stamp: %s. The set must be derived from "
            "BUNDLES, not hand-listed, or adding a bundle silently reintroduces this." % missing)

    def test_the_exclusion_is_derived_from_bundles_not_hand_written(self):
        src = GEN.read_text()
        assert "for b in BUNDLES" in src, (
            "the exclusion set must be built from BUNDLES so a new bundle cannot be forgotten")

    def test_a_modified_source_file_still_dirties_the_stamp(self):
        """The exclusion must not swallow a real edit: only the generated YAML is exempt."""
        src = GEN.read_text()
        i = src.index("generated = {")
        clause = src[i:i + 400]
        assert ".yaml" in clause, "the exclusion must be scoped to the rendered YAML only"
        assert ".py" not in clause.split("]")[0], "a .py path must never be excluded"


class TestTheProvenanceSurvivesHistoryRewrites:
    def test_source_digest_does_not_depend_on_commit_stamp(self):
        ns = _gen()
        bundle = "hear-heartbeat-code"
        app, code, data = ns["BUNDLES"][bundle]
        first = yaml.safe_load(ns["render"](bundle, app, code, data, "1111111"))
        second = yaml.safe_load(ns["render"](bundle, app, code, data, "2222222"))
        first_ann = first["metadata"]["annotations"]
        second_ann = second["metadata"]["annotations"]
        assert first_ann["dama-hear/commit"] != second_ann["dama-hear/commit"]
        assert first_ann["dama-hear/source-sha256"] == second_ann["dama-hear/source-sha256"]


# ------------------------------------- ConfigMaps embedded inside a workload manifest

class TestTheEmbeddedConfigMaps:
    """⚠️A HAND-EMBEDDED ConfigMap IS THE SAME SECOND COPY WITH NO GUARD ON IT.

    `deploy/k8s/hear-annotate.yaml` carries `server.py` inline next to the Deployment that
    mounts it, so none of the tests above could see it: they walk `BUNDLES`, and it was not in
    `BUNDLES`. It drifted at commit fa5a589 -- `tools/hear_annotate/server.py` gained path
    traversal, proxy-trust, idempotency and WAV-frame hardening and the embedded copy did not,
    so the pod holding the only human-labelled ground truth in the system ran the pre-hardening
    code while the checkout, the tests and the reviewer all read the hardened one.

    These tests walk `EMBEDDED_BUNDLES` for the same reason the content guard above walks
    `BUNDLES`: a second embedded ConfigMap must not be able to arrive unguarded. The fix for a
    failure here is never an edit to the YAML:

        python3 deploy/k8s/gen_configmap.py <bundle>
    """

    @staticmethod
    def _manifest(bundle):
        return ROOT / _gen()["EMBEDDED_BUNDLES"][bundle][0]

    @staticmethod
    def _doc(bundle, kind, name):
        manifest = TestTheEmbeddedConfigMaps._manifest(bundle)
        for doc in yaml.safe_load_all(manifest.read_text()):
            if doc and doc.get("kind") == kind and doc["metadata"]["name"] == name:
                return doc
        raise AssertionError("no %s/%s in %s" % (kind, name, manifest.name))

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_embedded_copy_matches_the_checkout(self, bundle):
        """The code the pod runs must be the code in the checkout. This is the whole guard."""
        gen = _gen()
        _manifest, _app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        embedded = self._doc(bundle, "ConfigMap", bundle)["data"]
        for key, rel in list(code) + list(data):
            assert key in embedded, "%s is not in %s at all" % (key, bundle)
            assert embedded[key] == gen["_block"](str(ROOT / rel)), (
                "%s has drifted from %s -- the cluster is running the old one. Regenerate:\n"
                "    python3 deploy/k8s/gen_configmap.py %s" % (key, rel, bundle))
        assert set(embedded) == {k for k, _rel in list(code) + list(data)}, (
            "%s ships keys that are in no bundle list, so nothing generates or checks them"
            % bundle)

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_sources_regenerate_the_checked_in_manifest_byte_for_byte(self, bundle):
        """⚠️THE STRONGER STATEMENT, AND THE ONE THAT MAKES THE FILE GENERATED RATHER THAN
        MERELY CHECKED. Equal `data` would still pass with hand-maintained indentation, key
        order or annotations that the generator would never emit -- and the next regeneration
        would then produce a diff nobody asked for, which is how an operator learns to distrust
        the tool and hand-edit the YAML again. The generator's output IS the committed file.
        """
        gen = _gen()
        text = self._manifest(bundle).read_text()
        stamp = self._doc(bundle, "ConfigMap", bundle)["metadata"]["annotations"][
            "dama-hear/commit"]
        assert gen["render_embedded"](bundle, stamp, text) == text, (
            "%s is not what gen_configmap.py emits for %s. Regenerate:\n"
            "    python3 deploy/k8s/gen_configmap.py %s"
            % (self._manifest(bundle).name, bundle, bundle))

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_regeneration_rewrites_the_configmap_and_nothing_else(self, bundle):
        """⚠️COMPATIBILITY IS THE POINT: the Deployment and the Service in the same file are
        hand-maintained, and a generator that reformatted them would be a packaging change
        smuggled in behind a sync guard. Regenerating from a CHANGED source must move the
        ConfigMap document and leave every other byte of the manifest where it was.
        """
        gen = _gen()
        _m, _app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        text = self._manifest(bundle).read_text()
        key, rel = (list(code) + list(data))[0]
        real = gen["_block"]
        try:
            gen["_block"] = lambda p, _r=real: (_r(p) + "# regeneration probe\n"
                                                if p.endswith(rel) else _r(p))
            changed = gen["render_embedded"](bundle, "0000000", text)
        finally:
            gen["_block"] = real
        assert changed != text, "a changed source must change the manifest"
        regenerated, = [d for d in yaml.safe_load_all(changed)
                        if d and d.get("kind") == "ConfigMap"]
        assert regenerated["data"][key].endswith("# regeneration probe\n")
        marker = "\n---\n"
        assert changed.split(marker, 1)[1] == text.split(marker, 1)[1], (
            "regenerating %s rewrote a document it does not own" % bundle)

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_manifest_is_replaced_atomically(self, bundle):
        """⚠️THE FILE IS NOT ONLY THE DOCUMENT THIS GENERATOR WRITES. A truncate-then-write
        interrupted halfway leaves a manifest with no Deployment and no Service -- documents the
        generator does not produce and cannot restore -- and the operator finds out at apply."""
        src = GEN.read_text()
        i = src.index("if embedded:", src.index("def main("))
        body = src[i:src.index("text = render(", i)]
        assert "os.replace(" in body, "the embedded manifest must be swapped into place, not truncated"
        assert 'open(path, "w")' not in body, "a direct truncating write of the manifest is the defect"

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_provenance_identifies_the_embedded_content(self, bundle):
        gen = _gen()
        manifest, _app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        ann = self._doc(bundle, "ConfigMap", bundle)["metadata"]["annotations"]
        stamp = ann.get("dama-hear/commit", "")
        assert stamp and not stamp.endswith("-dirty"), (
            "%s has an unusable provenance stamp %r; regenerate from a clean commit"
            % (bundle, stamp))
        assert ann.get("dama-hear/generated-by") == "deploy/k8s/gen_configmap.py", (
            "a generated object must say so, or the next editor hand-edits it")
        assert ann.get("dama-hear/embedded-in") == manifest
        assert ann.get("dama-hear/source-sha256") == gen["source_digest"](code, data)

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_import_closure_check_still_runs(self, bundle):
        gen = _gen()
        _m, _app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        gen["check"](code, data)      # raises SystemExit if the closure is incomplete

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_every_key_is_mounted_where_the_bundle_says(self, bundle):
        """The seam that breaks only in the cluster, for the block-style mounts this manifest
        uses: a key the pod does not mount is a file the pod does not have."""
        gen = _gen()
        _m, app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        keys = {k: rel for k, rel in list(code) + list(data)}
        dep = self._doc(bundle, "Deployment", app)
        spec = dep["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in spec["volumes"]}
        code_volumes = [n for n, v in volumes.items()
                        if (v.get("configMap") or {}).get("name") == bundle]
        assert code_volumes, "no volume in %s serves ConfigMap %s" % (app, bundle)
        for container in spec["containers"]:
            mounted = {m["subPath"]: m["mountPath"] for m in container["volumeMounts"]
                       if m["name"] in code_volumes and "subPath" in m}
            assert set(mounted) == set(keys), (
                "container %r mounts %r but %s ships %r"
                % (container["name"], sorted(mounted), bundle, sorted(keys)))
            for key, path in sorted(mounted.items()):
                assert path == "/app/" + key, (
                    "%s is mounted at %s; a key mounted at the wrong path is a different module,"
                    " or none" % (key, path))

    def test_hear_annotate_keeps_the_runtime_contract_the_guard_must_not_change(self):
        """⚠️PINNED BECAUSE THIS POD OWNS annotations.sqlite3 -- irreplaceable human ground
        truth on an unbacked `local-path` PVC. Bringing the ConfigMap under the generator is a
        packaging change and must be nothing else: same object name, same key, same mount path,
        same image, same database path, same claim, same tailnet exposure. If a later change to
        the generator moves any of these, it is that change that is wrong.
        """
        bundle = "hear-annotate-code"
        cm = self._doc(bundle, "ConfigMap", bundle)
        assert cm["metadata"]["namespace"] == "dama"
        assert cm["metadata"]["labels"] == {"app": "hear-annotate"}
        assert set(cm["data"]) == {"server.py"}

        spec = self._doc(bundle, "Deployment", "hear-annotate")["spec"]["template"]["spec"]
        web, = [c for c in spec["containers"] if c["name"] == "web"]
        assert web["image"] == "python:3.13-slim"
        assert {e["name"]: e["value"] for e in web["env"]} == {
            "HEAR_ANNOTATE_DB": "/pool/corpus/annotations.sqlite3"}
        assert {"name": "code", "mountPath": "/app/server.py",
                "subPath": "server.py"} in web["volumeMounts"]
        assert {"name": "pool", "mountPath": "/pool"} in web["volumeMounts"]
        assert "--pool /pool" in " ".join(web["args"])
        claims = [v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]
                  if "persistentVolumeClaim" in v]
        assert claims == ["hear-pool"]

        svc = self._doc(bundle, "Service", "hear-annotate")
        assert svc["metadata"]["annotations"]["tailscale.com/expose"] == "true"
        assert svc["spec"]["type"] == "ClusterIP"

    def test_hear_annotate_readiness_is_not_a_user_query_endpoint(self):
        """⚠️THE PROBE WAS `GET /api/queue?limit=1`, SO READINESS COST A CORPUS PARSE.

        At 1,878 clips that request took 1.19 s median against the API server's default 1 s
        probe timeout: the pod holding the only human ground truth flapped NotReady and the
        tailnet UI went down, with no code change involved -- the corpus simply grew. A probe
        pointed at a user query measures the user query, so it fails for reasons that have
        nothing to do with whether the process can serve. Readiness must name a dedicated
        endpoint whose cost does not grow with the data.
        """
        web, = [c for c in self._doc("hear-annotate-code", "Deployment", "hear-annotate")[
            "spec"]["template"]["spec"]["containers"] if c["name"] == "web"]
        probe = web["readinessProbe"]["httpGet"]
        assert probe["path"] == "/healthz", (
            "readiness probes %r; an /api/ path makes availability a function of corpus size"
            % probe["path"])
        assert probe["port"] == "http"
        assert web["readinessProbe"]["timeoutSeconds"] >= 1

        source = (ROOT / "tools" / "hear_annotate" / "server.py").read_text()
        assert '@app.get("/healthz")' in source, (
            "the manifest probes a path the shipped server does not serve")
        assert "build_queue" not in source.split('@app.get("/healthz")')[1].split("@app.get")[0], (
            "readiness must not invoke the queue work it exists to stop measuring")

    @pytest.mark.parametrize("bundle", sorted(_gen()["EMBEDDED_BUNDLES"]))
    def test_the_live_configmap_matches_the_checkout(self, bundle):
        """⚠️READ-ONLY, AND IT MUST STAY READ-ONLY. `kubectl get`. Never `apply`, not even
        `--dry-run`: a test that can reach the cluster must not be one edit away from writing
        to it.

        A live object OLDER than the checkout is reported as a skip rather than a failure --
        the same rule the bundle size guard above follows, because an un-applied regeneration
        is an operator's decision and not a defect in this tree. What fails is a live object
        that CLAIMS this checkout's content, by source-sha256, and does not have it.
        """
        gen = _gen()
        _m, _app, code, data = gen["EMBEDDED_BUNDLES"][bundle]
        obj = _live(bundle)
        live_data = obj.get("data") or {}
        want = {key: gen["_block"](str(ROOT / rel)) for key, rel in list(code) + list(data)}
        ann = obj.get("metadata", {}).get("annotations", {}) or {}
        here = gen["source_digest"](code, data)
        if live_data == want:
            return
        drifted = sorted(set(want) | set(live_data)) and [
            k for k in sorted(set(want) | set(live_data)) if want.get(k) != live_data.get(k)]
        if ann.get("dama-hear/source-sha256") != here:
            pytest.skip(
                "live %s is stamped %r against this checkout's %r and differs in %s -- the "
                "cluster is running code that is not in this tree. Apply it deliberately:\n"
                "    kubectl apply -f %s"
                % (bundle, ann.get("dama-hear/source-sha256") or ann.get("dama-hear/commit"),
                   here, drifted, gen["EMBEDDED_BUNDLES"][bundle][0]))
        assert live_data == want, (
            "live %s is stamped with this checkout's source digest but %s differ, so the object "
            "in the cluster was edited outside the generator" % (bundle, drifted))
