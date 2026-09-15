# Worker base images — the reproducible substrate

This directory builds the images dama-hear workers will run on. It does **not** build a worker:
that is the first pilot's job (`deploy/images/service/`, not added yet). What is here is the part
every worker migration would otherwise have to invent separately — the pinned base, the lock
mechanism, the naming and the provenance.

## The problem this replaces

Every workload in `deploy/k8s` starts the same way:

```sh
pip install --no-cache-dir --target /pool/pylib 'numpy==2.2.6'
```

⚠️**PyPI is currently a runtime dependency of a sensor pipeline.** A CronJob that fires at
`*/15` reaches the public index to build its own interpreter environment before it collects
anything. An index outage, a yanked file or a DNS failure is a data gap, not a build failure.

⚠️**`/pool/pylib` is shared by hear-drain, hear-score and hear-tdoa.** All three "pin" numpy;
whichever pod reaches an empty PVC first actually chooses it, and the other two use what they
find. `deploy/k8s/README.md` already records this. Three pins, one directory, no enforcement.

⚠️**`python:3.13-slim` is a moving tag.** Nothing in the repo records which image any pod ran,
so "reproduce the run" is not a question the current deployment can answer.

⚠️**A ConfigMap is not a package.** Code arrives as ~26 individual `subPath` file mounts, which
do not hot-update; live objects have twice been found carrying arguments the checked-in files
lacked. `docs/standalone-migration.md` already forbids all of this in the standalone release.

## What is here

| file | what it is |
|---|---|
| `base-images.txt` | every upstream image, pinned by digest, for `deploy/images` **and** `deploy/k8s` |
| `gen_pip_lock.py` | resolves a variant's dependencies **inside its pinned base** and emits hashes |
| `Dockerfile.runtime` | `hear-runtime`: Python 3.13, non-root uid 65532, no packages, no code |
| `Dockerfile.numeric` | `hear-numeric`: + numpy/scipy — the drain / score / tdoa lanes |
| `Dockerfile.ml-cpu` | `hear-ml-cpu`: + onnxruntime / ai-edge-litert — the tag / birdnet lanes |
| `Dockerfile.ml-gpu` | `hear-ml-gpu`: TensorFlow 2.20.0 GPU + the matched CUDA 12.9 wheels — the embed / perch lane |
| `../../requirements/image-*.txt` | what a variant asks for (versions, human-edited) |
| `../../requirements/lock/*.txt` | what it resolved to (full closure, one sha256 per artifact, generated) |
| `../../tests/test_image_pins.py` | the guard: pins, digests, locks and manifests may not drift |

Four variants, not one per worker. A worker that needs something no variant provides gets it in
its **own** service image, layered on `hear-runtime` from its own lock file — not a new shared
base, which would be `/pool/pylib` again with a registry in front of it.

## Reproducibility, and its limits

Claimed:

- **The base is a digest.** `FROM python@sha256:…`, restated in `base-images.txt`, checked by the
  guard test. Two builds a month apart start from identical bytes.
- **The closure is hash-locked.** `gen_pip_lock.py` runs the resolver **once**, inside the pinned
  base, and records every artifact's sha256 — transitive ones included. Builds install with
  `--require-hashes --no-deps`, so the build replays a resolution instead of performing one, and
  a re-uploaded or substituted artifact fails the build rather than reaching a node.
- **The lock knows what it was resolved against.** Its header carries the base digest, the input
  file's sha256 and the platform. Bump a base and the locks are stale, by construction.
- **`PIP_ONLY_BINARY=:all:`** — no sdist is compiled during a build, so no build-time toolchain
  or network fetch leaks into the result.

Not claimed:

- ⚠️**Not bit-identical images.** `apt` metadata, timestamps and layer ordering still vary. What
  is reproducible is the *contents contract*: same base digest, same artifacts, same versions.
  Bit-identical layers would need a snapshot Debian mirror and `SOURCE_DATE_EPOCH`; the images
  install no apt packages precisely so that this gap stays small.
- ⚠️**One platform.** The locks are `linux/amd64`, which is what the k3s nodes are. A second
  architecture is a second lock file, not a re-resolve of these.
- ⚠️**`ml-gpu` is not Python 3.13.** It inherits TensorFlow's interpreter, so its lock is not
  interchangeable with the others.

## Offline / air-gapped builds

`docs/deployment.md` requires that an install "must not contact public registries, ACME, package
indexes, or telemetry endpoints". The pieces that makes possible are here:

- The lock files name every artifact and its hash, so a wheel mirror can be populated ahead of
  time and `pip` pointed at it with `PIP_INDEX_URL` — the hashes make the mirror's contents
  verifiable rather than trusted.
- The base digests are exact, so `docker save` of those two images plus the built variants is a
  complete build input set.
- No `apt-get install` in any variant. The only network the build needs is the index and the
  registry.

What is still missing for a true air-gapped build is a mirrored Debian snapshot for the base
image itself — which is why the base is *vendored by digest* rather than rebuilt.

## The model boundary

⚠️**Weights are not code, and they do not go in these images.** `mn10_as.onnx`, `birdnet_v24`
and `perch_v2` (and the 16 MB YAMNet file that is already too large for a ConfigMap) stay on the
PVC at `/pool/models/<lane>`, staged out of band and verified against the sha256 pinned in
`tools/hear_tag.py` before anything is tagged. The image provides the *runtime*; the weights are
data with their own lifecycle, their own registry (`docs/ml-lifecycle.md`) and their own rollback.

Baking them in would couple a model promotion to an image rebuild, multiply image size by the
model zoo, and put an artifact with a separate approval trail inside one whose identity is
supposed to be the code. The small JSON models in `modules/supersonic/` are the deliberate
exception: they are already in git, already in the code bundle, kilobytes, and `__file__`-relative
— they ship with the code because they *are* the code's data files.

## Naming and versioning

```
<registry>/dama-hear/base-runtime:<short-sha>
<registry>/dama-hear/base-numeric:<short-sha>
<registry>/dama-hear/base-ml-cpu:<short-sha>
<registry>/dama-hear/base-ml-gpu:<short-sha>
<registry>/dama-hear/<worker>:<short-sha>          # service images, from the first pilot on
```

- `<registry>` is the in-cluster registry the site already runs (`dama-bridge`,
  `embedding-worker` and the runners are published there), with `ghcr.io/rjmendez` as the
  mirror for anything that has to be pulled from outside the site.
- ⚠️**A tag is an alias; a manifest references a digest.** Tags are `<short-sha>` of the commit
  that built the image — never `latest`, never a mutable channel name. The `image:` line in a
  workload manifest carries `name@sha256:…`, so what a pod ran is answerable from git alone.
- A base image is rebuilt when its base digest moves, its lock changes, or its Dockerfile
  changes — all three are commits, so the short-sha tag is a correct version for it.
- Every image carries `org.opencontainers.image.base.name` / `.base.digest` / `.source` labels,
  so the chain from a running container to a commit does not depend on the tag surviving.

## SBOM, provenance and scanning

The images workflow (`.github/workflows/images.yml`) attaches, for every variant:

- an **SBOM** (SPDX, via buildx's `--sbom=true`),
- a **provenance attestation** (SLSA, via `--provenance=mode=max`) naming the workflow, the
  commit and the build inputs,
- a **vulnerability scan** (Trivy, `--exit-code 1` on HIGH/CRITICAL with a dated, reviewed
  ignore file — not a permanent allowlist).

⚠️**On a pull request the workflow builds but does not push.** Nothing reaches a registry from a
branch, so a fork cannot publish an image. Publishing is a `main`-only job, which is where the
digest that manifests will reference is recorded.

⚠️**A pull request gets no SBOM, and that is a buildx constraint, not a choice.** An attestation
makes the build emit a manifest list, and the docker exporter that `load: true` uses cannot
export one — `docker exporter does not currently support exporting manifest lists`. So a PR
proves the image *builds, runs and scans clean*, and `main` proves its *provenance*.

## Rollout and rollback compatibility

The ConfigMap path and the image path are designed to coexist, one workload at a time:

1. A migrated workload stops mounting its `code` and `deps` volumes and names an image digest.
   **Its ConfigMap stays applied and unreferenced.**
2. Rollback is `kubectl apply -f <workload>-code.yaml -f <workload>.yaml` from the pre-migration
   commit, or `kubectl -n dama rollout undo`. One pod cycle, no data migration to undo.
3. `base-images.txt` covers the tags `deploy/k8s` still uses, so an un-migrated workload and a
   migrated one cannot silently end up on different Pythons.
4. `requirements/image-*.txt` must agree with `requirements/ci-pods.txt`, which is what the
   `tests (py3.13, pods)` CI job installs — so an image is numerically what CI tested, before and
   after a migration.

⚠️**A rollout of an image-based Deployment is not free where the current manifests are.**
`hear-heartbeat` and `hear-mqtt-bridge` are `hostNetwork` with a `hostPort`, one replica and no
`strategy` block: the default RollingUpdate cannot schedule the new pod beside the old one. Any
workload in that shape needs `strategy: { type: Recreate }` in the same change that gives it an
image, or its first image rollout deadlocks.

## Recipes

```sh
# refresh an upstream digest (registry HEAD; no pull, no daemon)
python3 deploy/images/gen_pip_lock.py --print-digest python 3.13-slim

# re-resolve a variant's closure after editing requirements/image-<variant>.txt
python3 deploy/images/gen_pip_lock.py numeric > requirements/lock/numeric.txt

# the offline half of the audit (what pytest runs)
python3 deploy/images/gen_pip_lock.py --check
python3 -m pytest -q tests/test_image_pins.py

# build the CPU chain locally, by digest, the way CI does
docker build -f deploy/images/Dockerfile.runtime -t hear-runtime:dev .
docker build -f deploy/images/Dockerfile.numeric --build-arg RUNTIME_IMAGE=hear-runtime:dev -t hear-numeric:dev .
docker build -f deploy/images/Dockerfile.ml-cpu  --build-arg NUMERIC_IMAGE=hear-numeric:dev -t hear-ml-cpu:dev .
```
