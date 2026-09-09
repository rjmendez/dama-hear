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
import os
import pathlib
import sys

import pytest

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
