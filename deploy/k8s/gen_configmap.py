#!/usr/bin/env python3
"""Regenerate a deploy/k8s code ConfigMap from the checkout.

    python3 deploy/k8s/gen_configmap.py                  > deploy/k8s/hear-drain-code.yaml
    python3 deploy/k8s/gen_configmap.py hear-score-code  > deploy/k8s/hear-score-code.yaml
    python3 deploy/k8s/gen_configmap.py hear-tag-code    > deploy/k8s/hear-tag-code.yaml

⚠️THE CONFIGMAP IS WRITTEN WHOLE, so it must be GENERATED whole. Hand-editing one key in the
cluster works right up until the next apply silently reverts it, and a code ConfigMap that has
drifted from the checkout is indistinguishable from one that has not. This script is the only
supported way to change what a workload runs; the commit it was generated from is stamped into
the object's annotations so a running pod can be traced back to a source tree.

⚠️APPLY ONE BUNDLE'S FILES, NEVER `-f deploy/k8s/`. Each bundle below is its own ConfigMap so the
two apply cycles cannot reach each other -- regenerating hear-score must not republish
hear-drain's keys or re-stamp its commit annotation. See deploy/k8s/README.md.

Each bundle's `code` list is the import closure of its entry point and nothing else, and `data`
names the non-code files that entry point OPENS at runtime. `check()` fails on either kind of
gap: a `hear.*` or `modules.*` import that is not shipped, or a `.json` a shipped module resolves
against its own `__file__`. Both would otherwise produce a workload that crashes only in the
cluster, which is the failure this script exists to make impossible -- and the data half was the
half it did not have. Adding an import or a model file without adding it here fails generation.
"""
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRAIN_CODE = [
    ("hear__init__.py", "hear/__init__.py"),
    ("hear_sketch.py", "hear/sketch.py"),
    ("hear_corpus.py", "hear/corpus.py"),
    ("hear_detsfile.py", "hear/detsfile.py"),
    ("hear_scenefile.py", "hear/scenefile.py"),
    # ⚠️pool.py imports it: the alias table that lets a node's own pre-provisioning rows in.
    # Shipping pool.py without it is a drain that crashes on import in the cluster only.
    ("hear_identity.py", "hear/identity.py"),
    ("hear_pool.py", "hear/pool.py"),
    # ⚠️ALSO IN TAG_CODE, the way hear_sketch.py is in two bundles: one edit, two ConfigMaps to
    # regenerate, and tests/test_configmap_sync.py is what stops the two copies drifting.
    ("hear_clips.py", "hear/clips.py"),
    ("tools_hear_drain.py", "tools/hear_drain.py"),
    # ⚠️SHIPPED BECAUSE THE CHECK JOB NOW RUNS IT. hear-drain.yaml's hourly check calls
    # `python /app/tools/fleet.py --require-one-build ...`, and /app is this ConfigMap: a file
    # that is not listed here does not exist in the cluster, and the job would have failed on a
    # missing path rather than on the drift it was added to find. fleet.py imports nothing from
    # this repo -- stdlib only -- so it costs one key and drags in no other file.
    ("tools_fleet.py", "tools/fleet.py"),
]

# ⚠️SMALL ON PURPOSE. hear_score imports `hear.sketch` and `modules.supersonic.classify` and
# nothing else -- it walks the pool's jsonl files itself rather than through `hear.pool.Pool`,
# which is what keeps corpus.py, detsfile.py, scenefile.py and pool.py out of this bundle. Two
# files (hear/__init__.py, hear/sketch.py) are shared with hear-drain's bundle; the regeneration
# test in tests/test_hear_score.py is what stops the two copies drifting.
SCORE_CODE = [
    ("hear__init__.py", "hear/__init__.py"),
    ("hear_sketch.py", "hear/sketch.py"),
    ("modules_supersonic_classify.py", "modules/supersonic/classify.py"),
    ("tools_hear_score.py", "tools/hear_score.py"),
]
# ⚠️ALL THREE, BECAUSE classify.py NAMES ALL THREE. DEFAULT_MODEL, DEFAULT_SKETCH_MODEL and
# FLEET_SKETCH_MODEL are `__file__`-relative path constants evaluated at import; shipping only
# the one this workload passes on the command line leaves the other two as paths to nothing, and
# any later caller taking a default gets FileNotFoundError in the cluster. Total 8,250 B against
# a 1 MiB ConfigMap limit, and it lets an operator run the 20-band model to see its refusal
# census (measured: it refuses every 16 kHz frame, which is the whole pool) without a redeploy.
SCORE_DATA = [
    ("modules_supersonic_model.json", "modules/supersonic/model.json"),
    ("modules_supersonic_model_sketch.json", "modules/supersonic/model_sketch.json"),
    ("modules_supersonic_model_sketch_15.json", "modules/supersonic/model_sketch_15.json"),
]

# ⚠️SMALLER STILL, AND DELIBERATELY WITHOUT pool.py. hear_tag walks clips/index.jsonl itself --
# the same discipline that keeps hear-score's bundle to four files. hear/tags.py's scene_overlap
# TAKES a Pool as an argument rather than importing one, which is what makes that honest: the
# audit below is an `ast.walk` and would find a function-local import exactly as well as a
# top-level one, so "import it inside the function" would not have worked. hear_clips.py is in
# BOTH this bundle and DRAIN_CODE, the way hear_sketch.py is already in two -- one edit, two
# ConfigMaps to regenerate, and tests/test_configmap_sync.py is what stops the copies drifting.
#
# ⚠️THE WEIGHTS ARE NOT HERE AND CANNOT BE. yamnet.tflite is 16,096,668 B against a 1 MiB
# ConfigMap limit. It lives on the PVC at /pool/models/yamnet, fetched once by the CronJob's
# preamble and verified against the sha256 pinned in tools/hear_tag.py -- which IS shipped, so the
# digest travels with the code that enforces it.
TAG_CODE = [
    ("hear__init__.py", "hear/__init__.py"),
    # ⚠️clips.py READS THE WAV HEADER'S RATE AND ASKS sketch.py WHETHER THE FORMAT CAN NAME IT, so
    # sketch.py is in this closure too. It is now in all three bundles: one edit, three ConfigMaps
    # to regenerate, which is what check() exists to keep saying out loud.
    ("hear_sketch.py", "hear/sketch.py"),
    ("hear_clips.py", "hear/clips.py"),
    ("hear_tags.py", "hear/tags.py"),
    # The tagger's model is 32 kHz and the fleet writes 16 and 48; resample.py is the only way
    # across, and it is also what refuses a rate nobody configured.
    ("hear_resample.py", "hear/resample.py"),
    ("tools_hear_tag.py", "tools/hear_tag.py"),
]

# ⚠️THE BIGGEST BUNDLE, AND EVERY FILE IN IT IS AN IMPORT CLOSURE MEMBER RATHER THAN A CHOICE.
# tools/hear_tdoa.py composes the whole solve stack, so it drags hear/backend/pipeline.py (for
# to_dama_event -- reimplementing that payload shape is how a consumer comes to read a MISSING
# model as a FAILED solve), which drags hear/wire.py and the hear/node package, whose __init__
# imports detect, telemetry and pipeline together.
#
# ⚠️check() BELOW CANNOT VERIFY MOST OF THIS LIST, so it is not evidence that the list is right.
# _imported_paths resolves `from hear import x` and `from hear.<mod> import y`; it does NOT
# resolve a RELATIVE import (`from ..solve import shockwave`), and `from hear.backend import
# associate` resolves to the non-existent `hear/backend.py` and is silently dropped. Almost every
# import inside the hear package is one of those two shapes. The list was therefore derived by
# importing the entry point and reading sys.modules, and tests/test_hear_tdoa.py's
# TestTheDeployBundle is what keeps it honest -- not this audit.
TDOA_CODE = [
    ("hear__init__.py", "hear/__init__.py"),
    ("hear_sketch.py", "hear/sketch.py"),
    ("hear_corpus.py", "hear/corpus.py"),
    ("hear_detsfile.py", "hear/detsfile.py"),
    ("hear_scenefile.py", "hear/scenefile.py"),
    ("hear_pool.py", "hear/pool.py"),
    ("hear_geodesy.py", "hear/geodesy.py"),
    ("hear_nodeclass.py", "hear/nodeclass.py"),
    ("hear_wire.py", "hear/wire.py"),
    ("hear_node__init__.py", "hear/node/__init__.py"),
    ("hear_node_detect.py", "hear/node/detect.py"),
    ("hear_node_pipeline.py", "hear/node/pipeline.py"),
    ("hear_node_telemetry.py", "hear/node/telemetry.py"),
    ("hear_backend__init__.py", "hear/backend/__init__.py"),
    ("hear_backend_survey.py", "hear/backend/survey.py"),
    ("hear_backend_associate.py", "hear/backend/associate.py"),
    ("hear_backend_pipeline.py", "hear/backend/pipeline.py"),
    ("hear_solve__init__.py", "hear/solve/__init__.py"),
    ("hear_solve_placement.py", "hear/solve/placement.py"),
    ("hear_solve_shockwave.py", "hear/solve/shockwave.py"),
    ("hear_solve_point.py", "hear/solve/point.py"),
    ("hear_solve_consistency.py", "hear/solve/consistency.py"),
    ("hear_solve_soundspeed.py", "hear/solve/soundspeed.py"),
    ("hear_solve_calibrate.py", "hear/solve/calibrate.py"),
    ("tools_hear_tdoa.py", "tools/hear_tdoa.py"),
]
# ⚠️THE SURVEY IS DATA THIS WORKLOAD OPENS AT RUNTIME, and it is the ONE input that decides where
# every answer lands. It is listed by hand because _data_paths resolves a .json against the
# MODULE's own directory -- for tools/hear_tdoa.py that is `tools/survey.json`, which does not
# exist, so the audit sees nothing to require. hear-tdoa.yaml passes --survey /app/survey.json
# explicitly so the dependency is visible in the args and not only in the mount layout.
TDOA_DATA = [
    ("survey.json", "survey.json"),
]

#: name -> (app label, code files, data files). The first entry is the default, so the command
#: documented in deploy/k8s/README.md keeps working with no argument.
BUNDLES = {
    "hear-drain-code": ("hear-drain", DRAIN_CODE, []),
    "hear-score-code": ("hear-score", SCORE_CODE, SCORE_DATA),
    "hear-tag-code": ("hear-tag", TAG_CODE, []),
    "hear-tdoa-code": ("hear-tdoa", TDOA_CODE, TDOA_DATA),
}
DEFAULT_BUNDLE = "hear-drain-code"


def _imported_paths(tree):
    """Every repo-local module path a parsed file imports, as repo-relative .py paths.

    ⚠️`import x.y` WAS NOT LOOKED AT AT ALL, and neither was anything outside the `hear` package.
    The audit resolved `hear/<name>.py` from three `ImportFrom` shapes and had no `ast.Import`
    branch, so `from modules.supersonic import classify` and `import modules.supersonic.classify`
    both passed it silently -- a workload importing the classifier would have generated cleanly
    and raised ModuleNotFoundError only in the cluster, which is precisely what the docstring
    above claims cannot happen.
    """
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module == "hear" or (n.level and not n.module):
                out.update("hear/%s.py" % a.name for a in n.names)
            elif n.module and n.module.startswith("hear."):
                out.add("hear/%s.py" % n.module.split(".", 1)[1])
            elif n.module and n.module.startswith("modules."):
                # `from modules.supersonic import classify` -- the name is the module.
                base = n.module.replace(".", "/")
                out.update("%s/%s.py" % (base, a.name) for a in n.names)
                out.add("%s.py" % base)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "hear" or a.name.startswith(("hear.", "modules.")):
                    out.add("%s.py" % a.name.replace(".", "/"))
    return out


def _data_paths(tree, rel):
    """Every `<file>.json` a module resolves against its own directory, as repo-relative paths.

    ⚠️AN AST WALK OVER IMPORTS CANNOT SEE A MODEL FILE. `classify.FLEET_SKETCH_MODEL` is
    `os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_sketch_15.json")` -- an
    `open()`, not an import -- so an import-only audit shipped a ConfigMap whose pod died on
    `FileNotFoundError`. Verified: with the model removed from the file list the old check()
    passed without complaint and the entry point then raised FileNotFoundError on the model path.

    ⚠️IT ALSO PINS THE MOUNT LAYOUT. Because that path is `__file__`-relative, classify.py at
    /app/modules/supersonic/classify.py requires the model as a SIBLING subPath in the same
    directory; mounting it anywhere else leaves the default dead.
    """
    d = os.path.dirname(rel)
    out = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "join"):
            continue
        for a in n.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and a.value.endswith(".json"):
                out.add(os.path.join(d, a.value) if d else a.value)
    return out


def check(code, data):
    shipped = {p for _, p in code} | {p for _, p in data}
    missing = []
    for _, rel in code + data:
        if not os.path.exists(os.path.join(ROOT, rel)):
            missing.append("%s is listed but does not exist in the checkout" % rel)
    for _, rel in code:
        # Only .py is parsed. A JSON model is shipped as a data key and is not Python, even
        # where it happens to parse as one -- true/false/null are bare names, so a model with a
        # boolean in it is valid Python by luck and that is not a property to depend on.
        if not rel.endswith(".py") or not os.path.exists(os.path.join(ROOT, rel)):
            continue
        tree = ast.parse(open(os.path.join(ROOT, rel)).read())
        for p in sorted(_imported_paths(tree)):
            if os.path.exists(os.path.join(ROOT, p)) and p not in shipped:
                missing.append("%s imports %s" % (rel, p))
        for p in sorted(_data_paths(tree, rel)):
            if os.path.exists(os.path.join(ROOT, p)) and p not in shipped:
                missing.append("%s opens %s at runtime" % (rel, p))
    if missing:
        sys.exit("configmap would ship an incomplete closure:\n  "
                 + "\n  ".join(sorted(set(missing))))


def render(name, app, code, data, sha):
    out = ["apiVersion: v1", "kind: ConfigMap", "metadata:",
           "  name: %s" % name, "  namespace: dama",
           "  labels:", "    app: %s" % app,
           "  annotations:",
           "    dama-hear/commit: %r" % sha,
           "    dama-hear/generated-by: deploy/k8s/gen_configmap.py",
           "data:"]
    for key, rel in code + data:
        out.append("  %s: |" % key)
        out += ["    " + l for l in open(os.path.join(ROOT, rel)).read().split("\n")]
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    name = argv[0] if argv else DEFAULT_BUNDLE
    if name not in BUNDLES:
        sys.exit("unknown bundle %r; known: %s" % (name, ", ".join(sorted(BUNDLES))))
    app, code, data = BUNDLES[name]
    check(code, data)
    sha = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"]).decode().strip()
    dirty = subprocess.check_output(["git", "-C", ROOT, "status", "--porcelain"]).decode().strip()
    print(render(name, app, code, data, sha + ("-dirty" if dirty else "")))


if __name__ == "__main__":
    main()
