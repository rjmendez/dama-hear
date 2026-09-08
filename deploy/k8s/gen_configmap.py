#!/usr/bin/env python3
"""Regenerate deploy/k8s/hear-drain-code.yaml from the checkout.

    python3 deploy/k8s/gen_configmap.py > deploy/k8s/hear-drain-code.yaml
    kubectl apply -f deploy/k8s/hear-drain-code.yaml -f deploy/k8s/hear-drain.yaml

⚠️THE CONFIGMAP IS WRITTEN WHOLE, so it must be GENERATED whole. Hand-editing one key in the
cluster works right up until the next apply silently reverts it, and a code ConfigMap that has
drifted from the checkout is indistinguishable from one that has not. This script is the only
supported way to change what the CronJob runs; the commit it was generated from is stamped into
the object's annotations so a running pod can be traced back to a source tree.

The file list is the import closure of tools/hear_drain.py and nothing else. `check()` below
fails if any of them imports a `hear` module that is not shipped -- adding an import without
adding it here would otherwise produce a CronJob that crashes only in the cluster.
"""
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES = [
    ("hear__init__.py", "hear/__init__.py"),
    ("hear_sketch.py", "hear/sketch.py"),
    ("hear_corpus.py", "hear/corpus.py"),
    ("hear_detsfile.py", "hear/detsfile.py"),
    ("hear_pool.py", "hear/pool.py"),
    ("tools_hear_drain.py", "tools/hear_drain.py"),
]


def check():
    shipped = {p for _, p in FILES}
    missing = []
    for _, rel in FILES:
        tree = ast.parse(open(os.path.join(ROOT, rel)).read())
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.ImportFrom) and n.module == "hear":
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level and not n.module:
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module \
                    and n.module.startswith("hear."):
                names = [n.module.split(".", 1)[1]]
            for nm in names:
                p = "hear/%s.py" % nm
                if os.path.exists(os.path.join(ROOT, p)) and p not in shipped:
                    missing.append("%s imports %s" % (rel, p))
    if missing:
        sys.exit("configmap would ship an incomplete import closure:\n  "
                 + "\n  ".join(sorted(set(missing))))


def main():
    check()
    sha = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"]).decode().strip()
    dirty = subprocess.check_output(["git", "-C", ROOT, "status", "--porcelain"]).decode().strip()
    out = ["apiVersion: v1", "kind: ConfigMap", "metadata:",
           "  name: hear-drain-code", "  namespace: dama",
           "  labels:", "    app: hear-drain",
           "  annotations:",
           "    dama-hear/commit: %r" % (sha + ("-dirty" if dirty else "")),
           "    dama-hear/generated-by: deploy/k8s/gen_configmap.py",
           "data:"]
    for key, rel in FILES:
        out.append("  %s: |" % key)
        out += ["    " + l for l in open(os.path.join(ROOT, rel)).read().split("\n")]
    print("\n".join(out))


if __name__ == "__main__":
    main()
