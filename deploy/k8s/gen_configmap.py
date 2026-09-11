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
import json
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
    # ⚠️pool.py imports it -- same reason it is in DRAIN_CODE. This is the third bundle to carry
    # hear/pool.py and the closure is per-bundle, so adding an import to pool.py is an edit in as
    # many places as there are bundles shipping it. check() is what says so out loud.
    ("hear_identity.py", "hear/identity.py"),
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


#: The API server's cap on `metadata.annotations` in bytes. A CLIENT-SIDE `kubectl apply` stores
#: the ENTIRE submitted object in the `kubectl.kubernetes.io/last-applied-configuration`
#: annotation, so a bundle whose serialised object clears this cap cannot be applied that way at
#: all -- the request is rejected outright:
#:
#:     $ kubectl apply --dry-run=server -f deploy/k8s/hear-tdoa-code.yaml
#:     The ConfigMap "hear-tdoa-code" is invalid: metadata.annotations:
#:     Too long: may not be more than 262144 bytes
#:
#: ⚠️IT IS NOT THE 1 MiB ConfigMap LIMIT AND IT BITES AT A QUARTER OF IT. `--server-side` writes
#: managed fields instead of that annotation and applies the same file cleanly (verified against
#: the live cluster the same day, same file: "configmap/hear-tdoa-code serverside-applied").
CLIENT_APPLY_ANNOTATION_CAP = 262144

#: The API server's hard cap on one object. Server-side apply does NOT lift this one, so it is
#: the real ceiling on how much source a bundle may carry however it is applied.
OBJECT_CAP = 1048576

#: How far under the annotation cap a bundle must sit before it is called client-appliable.
#:
#: ⚠️THIS EXISTS BECAUSE THE DECISION FLAPPED. On 2026-09-10 hear-drain-code serialised to
#: 262,129 B against the 262,144 B cap -- FIFTEEN bytes. The `dama-hear/commit` stamp alone moves
#: the object by 6 B between a clean and a dirty tree, so consecutive regenerations of the same
#: source would have alternated between "client" and "server" and the documented deploy command
#: would have changed with them. A mode that depends on whether the tree was dirty is not a mode.
#:
#: 8 KiB is about one more module, so the declaration survives an ordinary edit and only changes
#: when the bundle genuinely grows. Sitting inside the margin is not an error -- it means the
#: bundle is applied --server-side from now on, which always works.
CLIENT_APPLY_MARGIN = 8192


def _object(name, app, code, data, sha, mode, n_bytes):
    """The ConfigMap exactly as kubectl will serialise it -- the thing the cap applies to.

    `mode` and `n_bytes` land in annotations, so sizing is a fixed point: both candidate modes
    are 6 characters ("client"/"server") and n_bytes is written as a string, so the length is
    stable once the digit count is. size_of() below closes the loop.
    """
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "dama",
                         "labels": {"app": app},
                         "annotations": {"dama-hear/commit": sha,
                                         "dama-hear/generated-by": "deploy/k8s/gen_configmap.py",
                                         "dama-hear/apply-mode": mode,
                                         "dama-hear/serialised-bytes": str(n_bytes)}},
            "data": {key: _block(os.path.join(ROOT, rel)) for key, rel in code + data}}


def _block(path):
    """A file's bytes as the YAML `|` block scalar round-trips them.

    ⚠️`|` IS CLIP: it ends the value with exactly one newline whatever the file did. Three of
    the supersonic model JSONs have no trailing newline, so the shipped value is one byte longer
    than the file -- two, once JSON escapes it -- and sizing the raw read under-counted by 6 B
    on hear-score-code. Six bytes did not matter until hear-drain-code had fifteen.
    """
    text = open(path).read()
    if not text:
        # an empty file renders as `key: |` with no indented lines, which parses back as "",
        # not "\n" -- hear/__init__.py is empty and is in all four bundles
        return ""
    return text if text.endswith("\n") else text + "\n"


def size_of(name, app, code, data, sha):
    """Serialised bytes of the real object, resolved against its own self-reference.

    The size is written into the object, so it is a fixed point: iterate until it stops moving.
    Two passes settle it unless a digit is gained, three always.
    """
    n = 0
    for _ in range(4):
        got = len(json.dumps(_object(name, app, code, data, sha, "client", n),
                             separators=(",", ":")))
        if got == n:
            break
        n = got
    return n


def apply_mode(code, data, name="x", sha="0000000", app="x"):
    """"client" or "server": how this bundle has to be applied, and the two numbers behind it.

    ⚠️MEASURED ON THE SERIALISED OBJECT, NOT ON THE RENDERED YAML. What counts against the
    annotation cap is the JSON `kubectl` puts in last-applied-configuration, which is the object
    it is about to send -- so that is what is sized here. Sizing the YAML instead would be a
    proxy that is wrong in both directions: block-scalar indentation inflates it, and JSON's
    escaping of every newline inflates the other.

    ⚠️AND IT MUST BE *THIS* OBJECT, NOT A STAND-IN. This function used to size a payload with
    `"name": "x"` and no annotations at all, which under-measured the real file by ~220 B. With
    15 B of headroom that is the difference between "client" and a redeploy that fails, so the
    caller passes the real name and the byte cost of the real annotation block.
    """
    n = size_of(name, app, code, data, sha)
    return ("server" if n > CLIENT_APPLY_ANNOTATION_CAP - CLIENT_APPLY_MARGIN else "client"), n


def apply_command(name, mode):
    """The command that actually works for this bundle, so the file can carry its own.

    ⚠️`--force-conflicts` IS NOT OPTIONAL ON A BUNDLE THAT USED TO BE CLIENT-APPLIED. The first
    server-side apply of an object created by `kubectl apply` fails, because every field it
    touches is still owned by the "kubectl-client-side-apply" manager:

        error: Apply failed with 4 conflicts: conflicts with "kubectl-client-side-apply"
        - .data.hear_pool.py ...

    Measured on the live cluster 2026-09-10 the day hear-drain-code crossed the cap. Taking
    ownership is the correct resolution here and only here: a generated bundle has exactly one
    source of truth, this repo, so there is no other writer whose edit could be lost. Do not
    copy this flag onto a hand-maintained object.
    """
    if mode != "server":
        return "kubectl apply -f deploy/k8s/%s.yaml" % name
    return "kubectl apply --server-side --force-conflicts -f deploy/k8s/%s.yaml" % name


def render(name, app, code, data, sha):
    mode, n_bytes = apply_mode(code, data, name=name, sha=sha, app=app)
    if n_bytes > OBJECT_CAP:
        sys.exit("%s serialises to %d B, past the %d B object cap. Server-side apply does not "
                 "lift this one -- the bundle has to be split." % (name, n_bytes, OBJECT_CAP))
    out = ["# %s" % apply_command(name, mode),
           "# apply-mode %s: %d B serialised against a %d B last-applied-configuration cap."
           % (mode, n_bytes, CLIENT_APPLY_ANNOTATION_CAP),
           "apiVersion: v1", "kind: ConfigMap", "metadata:",
           "  name: %s" % name, "  namespace: dama",
           "  labels:", "    app: %s" % app,
           "  annotations:",
           "    dama-hear/commit: %r" % sha,
           "    dama-hear/generated-by: deploy/k8s/gen_configmap.py",
           # ⚠️STRUCTURED, NOT PROSE. The test that checks a bundle over the cap is marked
           # server-side reads THIS field. A test that grepped the document for the string
           # "--server-side" would also match the copy of this generator's own source, or of
           # tools/hear_tdoa.py, embedded in the bundle's `data` -- a guard matching its own
           # explanation and passing for the wrong reason.
           "    dama-hear/apply-mode: %s" % mode,
           "    dama-hear/serialised-bytes: %r" % str(n_bytes),
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
    # ⚠️THE GENERATED BUNDLES ARE EXCLUDED FROM THE DIRTY TEST, AND THEY HAVE TO BE.
    # Writing deploy/k8s/<bundle>.yaml dirties the tree, so a committed bundle could never carry
    # a clean stamp -- every one of them said "-dirty" whatever the tree really was. A flag that
    # is always set is not a flag; fleet.py's own warning that a -dirty build "did not come from
    # any commit" is exactly the signal this was drowning. What still dirties the stamp is a
    # change to any SOURCE the bundle ships, which is the thing worth knowing.
    porcelain = subprocess.check_output(
        ["git", "-C", ROOT, "status", "--porcelain"]).decode().splitlines()
    generated = {"deploy/k8s/%s.yaml" % b for b in BUNDLES}
    dirty = [ln for ln in porcelain if ln[3:].strip().strip('"') not in generated]
    stamp = sha + ("-dirty" if dirty else "")
    text = render(name, app, code, data, stamp)
    mode, n_bytes = apply_mode(code, data, name=name, sha=stamp, app=app)
    # stderr, because stdout is redirected into the .yaml by the documented command and an
    # operator who never opens the file would otherwise never see which apply works.
    sys.stderr.write("%s: %d B serialised, apply-mode %s\n  %s\n"
                     % (name, n_bytes, mode, apply_command(name, mode)))
    print(text)


if __name__ == "__main__":
    main()
