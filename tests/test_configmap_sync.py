"""The code the cluster runs must be the code in the checkout.

⚠️THE CONFIGMAP IS A SECOND COPY OF FIVE MODULES. `deploy/k8s/hear-drain-code.yaml` embeds
hear/pool.py, hear/scenefile.py, hear/detsfile.py, hear/sketch.py, hear/corpus.py and
tools/hear_drain.py verbatim, and the k3s CronJob imports THOSE, not the ones in this tree. A fix
landed in `hear/` and not regenerated is a fix the nightly drain does not have -- and nothing said
so: the ConfigMap is written whole on every apply, so the drift is invisible from the cluster and
invisible from a diff of the source file that was changed.

This test is the guard. It fails whenever a shipped file differs from its copy in the YAML, and
the fix is always the same one line, never an edit to the YAML:

    python3 deploy/k8s/gen_configmap.py > deploy/k8s/hear-drain-code.yaml

It deliberately does NOT check the `dama-hear/commit` annotation, which is a stamp of when the
file was generated and legitimately says `-dirty` in a working tree (the committed copy did).
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = pathlib.Path(__file__).resolve().parents[1]
YAML = ROOT / "deploy" / "k8s" / "hear-drain-code.yaml"
GEN = ROOT / "deploy" / "k8s" / "gen_configmap.py"


def _embedded():
    """{key: text} out of the ConfigMap, undoing the four-space block indent."""
    if not YAML.exists():
        pytest.skip("no ConfigMap in this checkout")
    text = YAML.read_text()
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


def _file_list():
    ns = {"__file__": str(GEN), "__name__": "gen_configmap"}
    exec(compile(GEN.read_text(), str(GEN), "exec"), ns)   # noqa: S102 -- our own file
    return ns["FILES"]


@pytest.mark.parametrize("key,rel", _file_list())
def test_the_shipped_copy_matches_the_checkout(key, rel):
    embedded = _embedded()
    assert key in embedded, "%s is not in the ConfigMap at all" % key
    want = (ROOT / rel).read_text()
    assert embedded[key] == want, (
        "%s has drifted from %s -- the cluster is running the old one. Regenerate:\n"
        "    python3 deploy/k8s/gen_configmap.py > deploy/k8s/hear-drain-code.yaml" % (key, rel))


def test_the_import_closure_check_still_runs():
    """gen_configmap.check() is what stops a new `hear` import shipping a CronJob that crashes
    only in the cluster. If it ever stops raising, this test is the one that says so."""
    ns = {"__file__": str(GEN), "__name__": "gen_configmap"}
    exec(compile(GEN.read_text(), str(GEN), "exec"), ns)   # noqa: S102
    ns["check"]()      # raises SystemExit if the closure is incomplete
