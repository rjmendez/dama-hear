# Phase 1.5 — immutable worker packaging

Decision record for the packaging lane named in
[migration-risk-register.md](migration-risk-register.md) ("Phase 1.5 — OCI images", risk **R9**).
[standalone-migration.md](standalone-migration.md) states the end state; this document states
*when* the workers get there, *what a worker image is allowed to contain*, and *what has to be
true before each one moves*.

**Status:** accepted, 2026-09-15. Supersedes the earlier scheduling assumption that packaging is
the last phase of the migration.

Companion document: `deploy/images/README.md`, delivered by the base-image lane, owns the base
images, the lock mechanism and the build provenance. This document owns the *migration*:
boundaries, ordering, gates and rollback. Neither repeats the other, and this one is deliberately
written so that it states the requirements it places on that lane rather than its design.

## Decision

**Immutable OCI worker packaging is promoted out of the retirement phase and runs as a side-lane
between Phase 1 (envelope and conformance) and Phase 2 (Postgres implementation). It must be
complete for every workload that participates in a dual-write or shadow-read comparison before
that comparison begins.**

Three consequences, stated as rules:

1. **No workload may participate in dual-write or shadow-read while its code arrives from a
   ConfigMap.** A comparison between a legacy result and a canonical result is only evidence if
   the legacy side is a known artifact. A ConfigMap-mounted worker is not.
2. **The lane is not gated on the Postgres or object-store lanes** and must not be scheduled
   behind them. It shares no artifact with them; its only cross-lane constraint is the soak
   window (see [Sequence](#sequence-and-gates)).
3. **`hear-heartbeat` is the pilot.** Scope, boundary and rollback are fixed below.

### Why not later

The original plan put packaging last, with the reasoning that it changes no contract. That is
true and it is not the point. The argument for promotion is that packaging is the *measurement
instrument* for every later phase:

* **A comparison needs a known baseline.** Phase 4 keeps legacy and canonical writers running
  together, and Phase 5 compares reads. Both treat the legacy result as the reference. Code that
  is edited into a live ConfigMap has twice been found diverged from the checked-in manifest —
  once on `hear-drain`, once on `hear-tag-code` together with its three dependent CronJob files.
  A mismatch found during dual-write would be unattributable: the canonical path, the legacy
  path, and "the legacy path is not the code we think it is" are indistinguishable.
* **The drift is silent by construction.** Code lands as individual `subPath` file mounts, and
  `subPath` mounts do not hot-update. A ConfigMap edit is invisible until the pod restarts, so
  the moment a change takes effect is a pod lifecycle event, not a deploy.
* **The blast radius is already concentrated.** `hear-tag-code` alone fans out to six CronJobs
  across three manifest files and two container images. One stale bundle is six stale workloads.
* **PyPI is a runtime dependency of a sensor pipeline.** Every workload resolves its interpreter
  environment at pod start against the public index. An index outage during a dual-write window
  is a comparison gap, and the durable writer is among the workloads that would be down.
* **The window is the cheapest it will ever be.** Phase 2 is soak-gated and Phase 3 is blocked on
  a `/pool` backup (R6). This lane is medium-sized, independent, and fits in that slack.

### What this lane is *not*

* Not a Compose/Helm release lane. `docs/deployment.md`'s release-manifest contract (one release
  metadata source generating both chart and Compose bundle) remains a later phase. Phase 1.5
  produces images and digest-pinned k8s manifests only.
* Not a model-weight lane. Weights keep their PVC staging and their `sha256` pins in
  `tools/hear_tag.py`. See [Boundaries](#boundaries).
* Not a security-hardening lane. mTLS, node authentication and broker ACLs (R15) are scheduled
  separately and must not be smuggled into a packaging change.

## Boundaries

Five boundaries, and the migration is largely the work of keeping them apart. The failure mode
being avoided is a "packaging" change that quietly moves configuration, state or data.

| Concern | Lives in | Rule |
|---|---|---|
| **Application code** | the image, at `/app` | Copied at build time from the source tree. A worker image contains the modules its entrypoint imports, and no ConfigMap mount supplies code. |
| **Third-party dependencies** | the image, from `requirements/lock/*.txt` | Installed at build time with `--require-hashes --no-deps`. No `pip install` in any `command`/`args`. No shared dependency directory. |
| **Model weights** | PVC `/pool/models/...`, staged out of band | Stay out of images. `yamnet.tflite` alone exceeds the ConfigMap limit and the Perch/BirdNET sets are larger; baking them in couples a model rollout to an image rebuild and vice versa. The `sha256` pins in `tools/hear_tag.py` remain the integrity check. |
| **Configuration** | env, Secrets, and site ConfigMaps | Node addresses, site origin, survey, Redis/broker endpoints, TTLs, retention, tokens. Never baked into a layer. `survey.json` and the model-selection JSON that are *bundle data today* are the ambiguous cases — see below. |
| **State** | PVCs and external services | `/pool` corpus, `/state/*.sqlite3` outboxes, Redis keys. An image migration must not create, move, reformat or re-own any of it. |

**The ambiguous cases, decided:**

* `modules/supersonic/*.json` (score models) and `survey.json` (TDoA geometry) are shipped as
  ConfigMap *data* keys today. `classify.py` opens its models `__file__`-relative, so they are
  part of the code artifact: **they go in the image**, and the image records its model-card
  identity. `survey.json` is site geometry: **it stays out**, mounted as site configuration, and
  the secret-sourced `origin` keeps its current handling. This split is the existing trust
  boundary, not a new one — the checked-in `survey.json` origin is deliberately fictional.
* `requirements/ci-pods.txt` remains the single declaration of pod-side pins and stays
  authoritative; the image locks are derived from it and the guard test already forbids the two
  from disagreeing.

**Identity boundary (this is the one that will bite).** The base image runs as uid `65532`.
Every current pod runs as root, and `/pool` is `drwxrwxrwx root:root` with `/pool/corpus`
`drwxr-xr-x root:root`; the two durable SQLite databases and their WAL files under `/state` were
created by root pods. `local-path` volumes do not get `fsGroup` ownership management, so a
worker that switches to uid `65532` in the same change that switches its image will fail to
write state that it previously owned — as a runtime error, on a PVC, after cutover.

Rule: **the uid change is a separate, later change from the image change, and it is per-volume.**
A worker that writes to no PVC (`hear-tdoa`'s read paths aside, this is only the pilot's
non-state surface) may adopt `65532` immediately. A worker that writes to `/pool` or `/state`
keeps running as root until an explicit ownership-reconciliation change re-owns that path and
proves the write. Do not infer from "the base image is nonroot" that a workload can be.

## Supply chain

The mechanism is built and owned by the base-image lane; this section states only what Phase 1.5
*requires of it*, so that a later worker migration cannot quietly lower the bar.

Required for every worker image, enforced in CI:

1. **Digest-pinned base.** No `FROM` resolves a tag. The upstream digest is recorded in
   `deploy/images/base-images.txt`, which also covers the tags `deploy/k8s` still runs so the two
   paths cannot drift while both exist.
2. **Hash-locked closure.** Dependencies install from a generated lock carrying one `sha256` per
   artifact, transitive ones included, resolved inside the pinned base. The lock header records
   the base digest and the input file's hash, so a base bump invalidates the lock by
   construction.
3. **SBOM and provenance per published image**, plus a vulnerability scan, produced by the image
   workflow. A pull request builds and discards; only `main` publishes.
4. **Deployment references a digest.** A manifest that names a tag is not migrated. The tag may
   appear as a comment or a label; the `image:` field carries `@sha256:`.
5. **The guard test is offline and mandatory.** Pins, locks, manifests and `ci-pods.txt` may not
   drift, no image ends as root by default, and no base image copies anything but its own lock.
6. **Release recording.** Each migrated workload's digest, source commit and lock hash are
   recorded in the change that migrates it, so "what ran on that day" is answerable from git
   alone.

Not claimed, and deliberately: images are not bit-reproducible (apt metadata, timestamps and
layer ordering vary), the locks are `linux/amd64` only, and the GPU variant inherits TensorFlow's
interpreter rather than Python 3.13. A true air-gapped build still needs a mirrored Debian
snapshot. These limits are recorded so a later reviewer does not assume a stronger guarantee than
exists.

## ConfigMap-to-image cutover

The two packaging paths coexist for the whole lane. The compatibility rule is that the ConfigMap
remains the **rollback artifact** until a workload's exit gate closes.

**Per-workload cutover procedure:**

1. Build and publish the service image; record its digest.
2. Change only these things in the workload manifest:
   * `image:` → the digest,
   * `command`/`args` → the direct entrypoint, deleting the `pip install`-then-`exec` shell,
   * remove the `code` and `deps` volumes and their mounts.
3. Change **nothing** else in the same commit. Specifically not: `hostNetwork`, `dnsPolicy`,
   ports/`hostPort`, Services, env vars or `secretKeyRef`s, probes, resources, replicas,
   schedules, `concurrencyPolicy`, PVC claims or mount paths, or the container's uid.
4. Leave the `*-code` ConfigMap applied and unreferenced. Do not delete it, and do not remove its
   `BUNDLES` entry or its sync test, until the workload's gate closes.
5. Keep the generator honest meanwhile: `gen_configmap.py` and `tests/test_configmap_sync.py`
   continue to run against every not-yet-migrated bundle. A bundle is removed from `BUNDLES` in
   the *retirement* change, not the cutover change.

**Rollout safety, for the two cases that exist:**

* **Deployments with a `hostPort` or an RWO PVC** must be `strategy: type: Recreate`. Under the
  default `RollingUpdate`, the surge pod cannot bind the port or attach the volume, so it stays
  `Pending` and the rollout hangs — and if it *did* start, two writers would share one SQLite
  ledger. `hear-heartbeat` already carries `Recreate`; `hear-mqtt-bridge` acquires it via PR
  #189, which must be merged before its cutover (R8).
* **CronJobs** cut over on their next scheduled fire. Migrate the worker and its `-check`
  companion in the same change — they are one unit of meaning — and, where one bundle feeds
  several CronJobs (`hear-tag-code` feeds six), either migrate all of them together or accept
  and document a window in which the same source runs from two different packagings.

**Rollback** is one `kubectl apply` of the pre-cutover manifest pair plus the ConfigMap, or
`kubectl rollout undo` for a Deployment. It restores in one pod cycle. There is no data migration
to reverse in either direction, because no cutover is permitted to touch state. Rollback must be
exercised, not assumed: it is part of the pilot gate.

**Live-vs-repo drift is a precondition, not a detail.** Live cluster state is currently ahead of
`main` in at least one workload (R8). Before any cutover, diff the live object against the
regenerated bundle and the committed manifest, and reconcile in a separate change. Migrating a
workload whose live spec is unknown converts an unknown into an image.

## Sequence and gates

Ordering is by blast radius and by what a failure costs, not by convenience.

| # | Workload(s) | Packaging | Why here |
|---|---|---|---|
| 0 | *base images* | `hear-runtime`, `hear-numeric`, `hear-ml-cpu`, `hear-ml-gpu` | Substrate. No workload changes. |
| 1 | **`hear-heartbeat`** (pilot) | service image on `hear-runtime` | One file, one dependency, its own PVC, an HTTP health probe, and a single consumer manifest. |
| 2 | `hear-mqtt-bridge` | same image family | Shares the receiver source with the pilot, so the pilot's build is most of it. Gated on the soak (below) and on PR #189. |
| 3 | `hear-annotate` | service image on `hear-runtime` | Single Deployment, but it owns `annotations.sqlite3` — irreplaceable human ground truth. Requires the hand-embedded ConfigMap to be brought under the generator *first*. |
| 4 | `hear-score` (+ `-check`) | `hear-numeric` | First CronJob and first shared-`/pool/pylib` consumer; models move into the image with it. |
| 5 | `hear-drain` (+ `-check`) | `hear-numeric` | Writes the corpus and talks to every node; the largest operational surface among the numeric lanes. |
| 6 | `hear-tdoa` (+ `-check`) | `hear-numeric` | Largest bundle, server-side apply only, import closure not verifiable by the current check. |
| 7 | `hear-tag`, `hear-birdnet` (+ `-check`s) | `hear-ml-cpu` | Six-way fan-out; weights stay on the PVC. |
| 8 | `hear-embed` (+ `-check`) | `hear-ml-gpu` | GPU, CUDA wheels, a non-3.13 interpreter. Last, because it is the only lane whose base is not shared. |
| 9 | *retirement* | — | Delete the `*-code` ConfigMaps, drop `BUNDLES`, retire `gen_configmap.py` and its sync test. |

**Gate 1 — pilot (`hear-heartbeat`).** Boundary: build on `hear-runtime`, install `redis==7.4.0`
from the lock, copy `tools/hear_heartbeat_receiver.py`, entrypoint
`python /app/tools/hear_heartbeat_receiver.py --port 5051`. Delete only the `code` and `deps`
volumes and mounts. Keep `hostNetwork`, `dnsPolicy`, `hostPort` 5051, the Service, all nine env
vars including both `secretKeyRef`s, both probes, resources, the `state` PVC mount, the sqlite
path, and root uid (the `/state` database is root-owned; see the identity boundary).

Exits when: the pod is `Ready` on the digest; `/healthz` returns the same shape as before; all six
devices appear in the durable ledger after cutover with continuous `received_at` across the
restart and zero failed cache attempts; the Redis compatibility keys are still armed; a rollback
to the ConfigMap path has been *performed* and the pod returned to `Ready`; and the image digest,
source commit and lock hash are recorded in the change.

**Gate 2 — soak interaction (blocking, and the only cross-lane constraint).** The Phase 2 durable
soak is a 14-day window that certifies the outbox's idempotency and liveness semantics. A restart
does not reset it — that was verified across the 14:38:49Z `Recreate` patch — but a *change of
artifact* does, because evidence gathered under one build cannot certify another.

Rule: **`hear-mqtt-bridge`'s packaging cutover happens in the same rollout as the R1–R3
correctness fixes, or not during the counted window at all.** The R1–R3 fixes already force a
reset; folding the packaging change into that same cutover costs one reset instead of two. The
pilot is unaffected: `hear-heartbeat` writes its own database and is not the soak artifact.

**Gate 3 — `hear-annotate` precondition.** Its `server.py` is hand-embedded inside
`deploy/k8s/hear-annotate.yaml` with no `BUNDLES` entry and no sync test. It is in sync today,
and nothing enforces that. Bring it under the generator and the guard test *before* migrating it,
so the rollback artifact is a generated bundle rather than a hand-maintained copy. Its
`annotations.sqlite3` is irreplaceable and unbacked (the same `local-path`/`Delete` exposure as
R6/R7): back it up with the SQLite backup API — never a raw copy of a live WAL — before cutover.

**Gate 4 — every CronJob lane.** One full schedule cycle on the image with output equivalent to
the pre-cutover run: same row/file counts for the same input window, same refusal accounting,
runtime within the observed envelope, and `/pool` writes landing with the same ownership and
paths. `hear-drain`'s existing overrun behaviour (runs observed from 218 s to 67 minutes against
a `Forbid` policy) is pre-existing and must not be *worsened*; it is not this lane's to fix.

**Gate 5 — lane exit, and the Phase 4 precondition.** Every workload that will participate in a
dual-write or shadow-read comparison runs from a digest; no `deploy/k8s` manifest contains a
`pip install`; no `*-code` ConfigMap is referenced by any running workload; `/pool/pylib`,
`/pool/pylib-tag`, `/pool/pylib-birdnet` and `/pool/pylib-perch` are unreferenced; and the
adapter-conformance item "deployment artifacts contain no runtime package install, shared
application PVC, unpinned image, undeclared external endpoint or required cloud credential" can
be asserted against `deploy/k8s` rather than deferred.

Retirement (step 9) happens *after* that, and never in the same release as a cutover.

## Relationship to the durable receiver and bridge

Both durable workloads are live, carrying fleet traffic, and are the subject of active
correctness work. That constrains this lane in three specific ways:

* **The bridge is soak evidence.** Its packaging cutover is scheduled onto the R1–R3 rollout
  (Gate 2). No exceptions for convenience: an image bump mid-window silently invalidates the
  gate the whole Phase 2 lane is waiting on.
* **The receiver is the pilot precisely because it is *not* the soak artifact.** It has the same
  dependency, the same source file and an HTTP health surface the bridge does not have, so it
  proves the build and the rollback with the smallest instrument. A successful pilot makes the
  bridge's cutover a manifest edit against an already-proven image.
* **Neither cutover may touch durability.** `HEAR_DURABLE_STORE`, `HEAR_DURABLE_DB`,
  `HEAR_DURABLE_REPLAY_LIMIT`, the `state` mount, the PVC claim and the replay/prune defaults are
  out of bounds for a packaging change, as are the uid and anything that would re-own `/state`.
  The correctness fixes change code inside the image; the packaging change changes how that code
  is delivered. Keeping them separable is what makes either one reviewable — and where Gate 2
  ships them together, they remain separate commits.

## Consequences

Accepted costs: two packaging paths coexist for the length of the lane, and `gen_configmap.py`,
its bundles and its sync test must be kept correct for workloads that have not moved. A worker
change during the lane may require both a bundle regeneration and an image build. Cutovers are
per-workload pod restarts (a CronJob lane absorbs this on its next fire; the two Deployments take
a `Recreate` gap of one pod cycle).

Rejected alternatives: *one image for all workers* — that is `/pool/pylib` with a registry in
front of it, and it couples a GPU dependency bump to the drain schedule. *Images plus ConfigMap
overrides* — retains the drift the lane exists to remove. *Wait for Compose/Helm* — that lane is
downstream of having images at all, and Phase 4 arrives first.

Reversibility: the whole lane is reversible per workload, by one `kubectl apply`, for as long as
the ConfigMaps exist. That is the reason retirement is a separate, last step.

## Open questions

These do not block the pilot; each must be answered before the step that depends on it.

1. **Registry availability.** Whether the cluster pulls from GHCR or a local mirror decides
   whether a node restart during a registry outage is survivable. `imagePullPolicy` and
   pre-pull/warm-cache behaviour are part of the answer, and this constraint applies to *every*
   step from 1 onward. Decide before step 2.
2. **`/pool` uid reconciliation.** Which workload re-owns which path, and in what order, so that
   the numeric and ML lanes can eventually stop running as root. Decide before step 4.
3. **`hear-tag` fan-out.** Whether steps 7's six CronJobs cut over atomically or in a documented
   split window. Decide before step 7.
4. **GPU base freshness.** `hear-ml-gpu` inherits its interpreter from upstream TensorFlow, so
   its refresh cadence is not the CPU chain's. Decide before step 8.
