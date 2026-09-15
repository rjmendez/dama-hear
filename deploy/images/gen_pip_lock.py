#!/usr/bin/env python3
"""Resolve a base-image variant's Python dependencies to a hash-pinned lock file.

    python3 deploy/images/gen_pip_lock.py numeric > requirements/lock/numeric.txt
    python3 deploy/images/gen_pip_lock.py ml-cpu  > requirements/lock/ml-cpu.txt
    python3 deploy/images/gen_pip_lock.py ml-gpu  > requirements/lock/ml-gpu.txt

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


def resolve(variant, root=ROOT):
    name, tag, req = VARIANTS[variant]
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
        "# variant:  %s" % variant,
        "# base:     %s:%s@%s" % (name, tag, digest),
        "# input:    %s sha256:%s" % (req, sha256_file(os.path.join(root, req))),
        "# platform: %s" % PLATFORM,
        "# pip:      %s" % pip_version,
        "#",
        "# Installed with --require-hashes --no-deps: the resolver ran once, here, and the build",
        "# only replays it. Regenerate with",
        "#",
        "#     python3 deploy/images/gen_pip_lock.py %s > requirements/lock/%s.txt" % (
            variant, variant),
    ]
    for pkg, version, sha in pins:
        out.append("%s==%s \\" % (pkg, version))
        out.append("    --hash=sha256:%s" % sha)
    return "\n".join(out) + "\n"


def lock_header(variant, root=ROOT):
    """The `key: value` comments at the top of a generated lock file."""
    path = os.path.join(root, "requirements", "lock", "%s.txt" % variant)
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
    """Every variant has a lock whose stamped input digest still describes its input file."""
    problems = []
    pinned = base_images(root)
    for variant, (name, tag, req) in sorted(VARIANTS.items()):
        lock = os.path.join(root, "requirements", "lock", "%s.txt" % variant)
        if not os.path.exists(lock):
            problems.append("%s: no requirements/lock/%s.txt" % (variant, variant))
            continue
        head = lock_header(variant, root)
        want_input = "%s sha256:%s" % (req, sha256_file(os.path.join(root, req)))
        if head.get("input") != want_input:
            problems.append(
                "%s: lock was generated from `%s`, the checkout has `%s` -- rerun "
                "gen_pip_lock.py %s" % (variant, head.get("input"), want_input, variant))
        want_base = "%s:%s@%s" % (name, tag, pinned[(name, tag)])
        if head.get("base") != want_base:
            problems.append(
                "%s: lock was resolved on `%s`, base-images.txt now pins `%s` -- rerun "
                "gen_pip_lock.py %s" % (variant, head.get("base"), want_base, variant))
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("variant", nargs="?", choices=sorted(VARIANTS))
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
    if not args.variant:
        ap.error("name a variant, or --check, or --print-digest")
    sys.stdout.write(resolve(args.variant))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
