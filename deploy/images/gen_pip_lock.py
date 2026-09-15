#!/usr/bin/env python3
"""Resolve a base-image variant's Python dependencies to a hash-pinned lock file.

    python3 deploy/images/gen_pip_lock.py numeric > requirements/lock/numeric.txt
    python3 deploy/images/gen_pip_lock.py ml-cpu  > requirements/lock/ml-cpu.txt
    python3 deploy/images/gen_pip_lock.py ml-gpu  > requirements/lock/ml-gpu.txt

    # a service image's own closure, resolved the same way
    python3 deploy/images/gen_pip_lock.py service:hear-heartbeat \\
        > requirements/lock/service-hear-heartbeat.txt

    python3 deploy/images/gen_pip_lock.py --check                  # no docker, no network
    python3 deploy/images/gen_pip_lock.py --print-digest python 3.13-slim

⚠️THE POINT IS THAT PyPI STOPS BEING A RUNTIME DEPENDENCY. Every workload in deploy/k8s runs
`pip install` at pod start, against the live index, into a PVC or an emptyDir. That makes a
sensor pipeline's startup depend on a network service nobody here operates, and it means no two
pods are provably running the same bytes. A lock file resolved once, at build time, with a
sha256 per artifact, is what replaces it.

⚠️RESOLUTION HAPPENS INSIDE THE PINNED BASE IMAGE, not on the workstation. The closure and the
selected wheels depend on the interpreter version, the libc and the platform; resolving on a
py3.12 laptop and installing on py3.13-slim is how a lock file comes to describe an image that
was never built. The base is read from deploy/images/base-images.txt BY DIGEST, and that digest
is stamped into the lock header, so bumping a base image invalidates every lock built on it.

⚠️THE LOCK IS PER PLATFORM. `--hash` names one artifact; a manylinux x86_64 wheel is not the
aarch64 one. These locks are linux/amd64, which is what the k3s nodes are. A second architecture
is a second lock file, not a re-resolve of this one.

`--check` re-reads each input requirements file and compares its sha256 against the one stamped
in the lock header. That is the half of the audit that needs neither docker nor the network, so
it can run in the ordinary pytest job (tests/test_image_pins.py); the images workflow runs the
full regeneration and fails on a diff.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_IMAGES = os.path.join("deploy", "images", "base-images.txt")
PLATFORM = "linux/amd64"

# variant -> (base image name, base image tag, input requirements file)
#
# ⚠️Adding a variant here is adding a Dockerfile AND a lock file; tests/test_image_pins.py fails
# on any of the three being missing. A worker that needs a dependency no variant provides gets
# that dependency in its own service image, layered on `runtime` -- it does not get a new shared
# base image per worker, which is the /pool/pylib mistake with a registry in front of it.
VARIANTS = {
    # numpy + scipy: the hear-drain, hear-score and hear-tdoa lanes, which today share
    # /pool/pylib and therefore let whichever pod reaches an empty PVC first choose the versions.
    "numeric": ("python", "3.13-slim", "requirements/image-numeric.txt"),
    # onnxruntime (hear-tag) + ai-edge-litert (hear-birdnet). Both are CPU inference runtimes and
    # neither carries model weights: see deploy/images/README.md, "the model boundary".
    "ml-cpu": ("python", "3.13-slim", "requirements/image-ml-cpu.txt"),
    # the hear-embed/perch lane. Its base is the vendored TensorFlow GPU image, whose CUDA and
    # cuDNN stack is the reason it is not built on python:3.13-slim.
    "ml-gpu": ("tensorflow/tensorflow", "2.20.0-gpu", "requirements/image-ml-gpu.txt"),
}

# service name -> (base image name, base image tag, input requirements file)
#
# ⚠️A SERVICE IMAGE IS ONE WORKLOAD'S CODE PLUS ITS OWN CLOSURE, and it gets its own lock rather
# than a shared one -- that is the whole difference from `/pool/pylib`, where three workloads
# "pin" numpy into one directory and the first pod to reach it decides. Services build FROM
# `hear-runtime`, which adds no Python package to its base, so a service's closure resolves
# inside the *same upstream base digest* the runtime image is built from. Resolving here rather
# than inside a locally built `hear-runtime` keeps the lock reproducible from the checkout alone,
# and tests/test_service_images.py fails if a service Dockerfile stops building on `hear-runtime`.
#
# ⚠️A SERVICE THAT NEEDS numpy/scipy/onnxruntime DOES NOT GET THEM HERE. It builds on the variant
# that already carries them (`hear-numeric`, `hear-ml-cpu`) and its own lock covers only what
# that variant lacks -- see deploy/images/service/README.md.
SERVICES = {
    # The Phase 1.5 pilot: tools/hear_heartbeat_receiver.py, whose only third-party import is
    # `redis`. deploy/k8s/hear-heartbeat.yaml pip-installs exactly this today, at every pod start.
    "hear-heartbeat": ("python", "3.13-slim", "requirements/image-service-hear-heartbeat.txt"),
}

#: The argument `gen_pip_lock.py <target>` accepts: base variants by bare name, services as
#: `service:<name>`, so the two namespaces cannot collide as more workloads migrate.
SERVICE_PREFIX = "service:"


def targets():
    """target name -> (image name, tag, input requirements file), bases and services together."""
    out = dict(VARIANTS)
    for name, spec in SERVICES.items():
        out[SERVICE_PREFIX + name] = spec
    return out


def lock_path(target):
    """The repo-relative lock file a target generates.

    Services are namespaced on disk (`service-<name>.txt`) for the same reason they are
    namespaced on the command line: `requirements/lock/hear-heartbeat.txt` sitting beside
    `requirements/lock/numeric.txt` would read as a fifth base image.
    """
    if target.startswith(SERVICE_PREFIX):
        return os.path.join("requirements", "lock",
                            "service-%s.txt" % target[len(SERVICE_PREFIX):])
    return os.path.join("requirements", "lock", "%s.txt" % target)


def base_images(root=ROOT):
    """(name, tag) -> digest, from deploy/images/base-images.txt."""
    out = {}
    with open(os.path.join(root, BASE_IMAGES)) as fh:
        for line in fh:
            line = line.split("#", 1)[0].split()
            if not line:
                continue
            name, tag, digest = line
            out[(name, tag)] = digest
    return out


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def registry_digest(name, tag):
    """HEAD the manifest on Docker Hub. No pull, no docker daemon."""
    repo = name if "/" in name else "library/" + name
    token = json.load(urllib.request.urlopen(
        "https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:%s:pull" % repo))["token"]
    req = urllib.request.Request(
        "https://registry-1.docker.io/v2/%s/manifests/%s" % (repo, tag), method="HEAD")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]))
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Docker-Content-Digest"]


def resolve(target, root=ROOT):
    name, tag, req = targets()[target]
    digest = base_images(root)[(name, tag)]
    ref = "%s@%s" % (name, digest)
    report = subprocess.run(
        ["docker", "run", "--rm", "--platform", PLATFORM,
         "-v", "%s:/src:ro" % root, "-w", "/src", ref,
         "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
         "--report", "/dev/stdout", "-r", req],
        check=True, capture_output=True, text=True).stdout
    report = json.loads(report)
    pins = []
    for item in report["install"]:
        meta = item["metadata"]
        hashes = item.get("download_info", {}).get("archive_info", {}).get("hashes", {})
        if "sha256" not in hashes:
            raise SystemExit(
                "%s %s resolved to an artifact with no sha256 -- a local path or a VCS URL "
                "cannot be locked" % (meta["name"], meta["version"]))
        pins.append((meta["name"].lower().replace("_", "-"), meta["version"], hashes["sha256"]))
    pins.sort()

    pip_version = subprocess.run(
        ["docker", "run", "--rm", "--platform", PLATFORM, ref, "pip", "--version"],
        check=True, capture_output=True, text=True).stdout.split()[1]

    out = [
        "# generated by deploy/images/gen_pip_lock.py -- do not edit by hand",
        "#",
        "# variant:  %s" % target,
        "# base:     %s:%s@%s" % (name, tag, digest),
        "# input:    %s sha256:%s" % (req, sha256_file(os.path.join(root, req))),
        "# platform: %s" % PLATFORM,
        "# pip:      %s" % pip_version,
        "#",
        "# Installed with --require-hashes --no-deps: the resolver ran once, here, and the build",
        "# only replays it. Regenerate with",
        "#",
        "#     python3 deploy/images/gen_pip_lock.py %s > %s" % (
            target, lock_path(target)),
    ]
    for pkg, version, sha in pins:
        out.append("%s==%s \\" % (pkg, version))
        out.append("    --hash=sha256:%s" % sha)
    return "\n".join(out) + "\n"


def lock_header(target, root=ROOT):
    """The `key: value` comments at the top of a generated lock file."""
    path = os.path.join(root, lock_path(target))
    head = {}
    with open(path) as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            body = line[1:].strip()
            if ":" in body:
                key, _, value = body.partition(":")
                key = key.strip()
                if key in ("variant", "base", "input", "platform", "pip"):
                    head[key] = value.strip()
    return head


def check(root=ROOT):
    """Every target has a lock whose stamped input digest still describes its input file."""
    problems = []
    pinned = base_images(root)
    for target, (name, tag, req) in sorted(targets().items()):
        rel = lock_path(target)
        lock = os.path.join(root, rel)
        if not os.path.exists(lock):
            problems.append("%s: no %s" % (target, rel))
            continue
        head = lock_header(target, root)
        want_input = "%s sha256:%s" % (req, sha256_file(os.path.join(root, req)))
        if head.get("input") != want_input:
            problems.append(
                "%s: lock was generated from `%s`, the checkout has `%s` -- rerun "
                "gen_pip_lock.py %s" % (target, head.get("input"), want_input, target))
        want_base = "%s:%s@%s" % (name, tag, pinned[(name, tag)])
        if head.get("base") != want_base:
            problems.append(
                "%s: lock was resolved on `%s`, base-images.txt now pins `%s` -- rerun "
                "gen_pip_lock.py %s" % (target, head.get("base"), want_base, target))
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?", choices=sorted(targets()),
                    metavar="TARGET",
                    help="a base variant (%s) or a service (%s)"
                         % (", ".join(sorted(VARIANTS)),
                            ", ".join(SERVICE_PREFIX + s for s in sorted(SERVICES))))
    ap.add_argument("--check", action="store_true",
                    help="verify every lock against its input, without docker")
    ap.add_argument("--print-digest", nargs=2, metavar=("NAME", "TAG"),
                    help="HEAD a tag's manifest and print the digest to pin it by")
    args = ap.parse_args(argv)

    if args.print_digest:
        print(registry_digest(*args.print_digest))
        return 0
    if args.check:
        problems = check()
        for p in problems:
            sys.stderr.write("%s\n" % p)
        return 1 if problems else 0
    if not args.target:
        ap.error("name a target, or --check, or --print-digest")
    sys.stdout.write(resolve(args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
