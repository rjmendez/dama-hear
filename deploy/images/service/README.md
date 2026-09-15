# Service images — one workload, one image

A **service image** is one workload's code plus the closure that workload needs, layered on a
base image from [`..`](../README.md). It is what replaces a `*-code` ConfigMap: the code arrives
as a layer instead of ~1–26 `subPath` file mounts, and the dependency arrives from a hash lock
instead of a `pip install` against the public index at every pod start.

The base-image directory above owns the *mechanism* (pinned bases, lock generation, provenance).
[`docs/worker-packaging.md`](../../../docs/worker-packaging.md) owns the *migration* (boundaries,
ordering, gates, rollback). This directory is where the two meet, one workload at a time.

| file | what it is |
|---|---|
| `Dockerfile.hear-heartbeat` | the Phase 1.5 pilot (step 1 of the sequence) |
| `digests.txt` | the published digest of every migrated workload — `pending` until `main` builds it |
| `../../../requirements/image-service-*.txt` | what a service asks for (human-edited) |
| `../../../requirements/lock/service-*.txt` | what it resolved to (generated, one `sha256` per artifact) |
| `../../../deploy/k8s/<workload>.proposed.yaml` | the proposed cutover manifest, not applied |
| `../../../tests/test_service_images.py` | the guard: parity, pins, digests, and the code the image carries |

## The pilot: `hear-heartbeat`

One source file (`tools/hear_heartbeat_receiver.py`), one third-party import (`redis==7.4.0`),
its own PVC, an HTTP health probe and a single consumer manifest. It is the pilot because it is
the smallest instrument that can prove the whole mechanism, and because it is *not* the Phase 2
soak artifact — `hear-mqtt-bridge` is, and a change of artifact resets that 14-day window.

```
python:3.13-slim@sha256:9d2e555…        upstream, pinned in ../base-images.txt
  └── hear-runtime                       interpreter, nonroot uid 65532, no packages, no code
        └── hear-heartbeat               redis==7.4.0 from the lock, the receiver at /app/tools,
                                         USER root, ENTRYPOINT the receiver on port 5051
```

### What the image deliberately does **not** change

* **State.** `/state/heartbeat-receiver.sqlite3` and its PVC are untouched. A packaging change
  may not create, move, reformat or re-own durable state, and there is therefore nothing to undo
  in either direction.
* **The uid.** The container stays **root**. `hear-runtime` ends as uid 65532 and the base-image
  guard enforces that for base images — but the existing `/state` database, WAL and shm files
  were created by a root pod and `local-path` volumes get no `fsGroup` ownership management, so
  adopting 65532 in the same change would produce a write failure at runtime, on a PVC, after
  cutover. Re-owning `/state` is a separate, per-volume change with its own proof of write.
* **Anything else in the manifest.** `hostNetwork`, `dnsPolicy`, `hostPort` 5051, the Service,
  all nine env vars including both `secretKeyRef`s, both probes, resources, replicas and
  `strategy: Recreate` are byte-identical between `hear-heartbeat.yaml` and
  `hear-heartbeat.proposed.yaml`. That is asserted by parsing both, not by reading the diff.
* **The ConfigMap.** `hear-heartbeat-code` stays applied, stays in `gen_configmap.BUNDLES`, and
  stays covered by `tests/test_configmap_sync.py`. It is the rollback artifact until gate 1
  closes, and it is deleted in the retirement step, never in a cutover.

## Naming, identity and provenance

```
ghcr.io/<owner>/dama-hear/<workload>:<commit-sha>      # tag: an alias, for humans
ghcr.io/<owner>/dama-hear/<workload>@sha256:<digest>   # what a manifest references
```

Every service image carries, and CI proves:

* `org.opencontainers.image.revision` — the commit its code was copied from, passed as
  `SOURCE_COMMIT` at build time, so a running container resolves to a source tree without
  trusting the tag.
* `dama-hear.lock` — the lock file its closure was replayed from.
* `dama-hear.entrypoint` — the argv, which the manifest no longer restates.
* An **SBOM** (SPDX) and a **SLSA provenance attestation** naming the workflow, the commit and
  the build inputs, and a **Trivy** scan that fails the build on a fixable HIGH/CRITICAL.
* A **content check** run against the built image rather than the Dockerfile: the `sha256` of
  `/app/tools/hear_heartbeat_receiver.py` inside the image must equal the checkout's, `redis`
  must import at the locked version, the entrypoint must be the documented argv, and the
  effective uid must be 0 — see the `hear-heartbeat contains what the repo says` step in
  `.github/workflows/images.yml`.

⚠️**A pull request publishes nothing.** The same image is built, attested and scanned into a
registry service that lives and dies with the job. `ghcr.io` is a `main`-only path.

## Cutover — the procedure, in order

Preconditions (`docs/worker-packaging.md`, gate 1 and the drift precondition):

0. **Reconcile live-vs-repo first, in a separate change.** `kubectl -n dama get deploy
   hear-heartbeat -o yaml` and `kubectl -n dama get cm hear-heartbeat-code -o yaml` must match
   the committed manifest and the regenerated bundle. Migrating a workload whose live spec is
   unknown converts an unknown into an image.
1. **Record the digest.** After this change merges, the `main` run of `.github/workflows/ci.yml`
   publishes `ghcr.io/<owner>/dama-hear/hear-heartbeat:<sha>` and prints its digest in the job
   summary. Put that digest, and the commit tag, into `digests.txt` and into the `image:` field
   of `deploy/k8s/hear-heartbeat.proposed.yaml`, in one commit. The guard test fails until the
   two agree, and fails while either still carries the `pending` / all-zero sentinel.
2. **Answer open question 1** of `docs/worker-packaging.md` before the *second* workload moves:
   whether the cluster pulls from GHCR or a local mirror, and what `imagePullPolicy` and
   warm-cache behaviour that implies. The pilot may proceed on a manual pre-pull.
3. **Pre-pull on the node that holds `hostPort` 5051**, so the `Recreate` gap is a container
   start and not a registry round-trip:
   `sudo k3s ctr images pull ghcr.io/<owner>/dama-hear/hear-heartbeat@sha256:<digest>`
4. **Apply.** `kubectl -n dama apply -f deploy/k8s/hear-heartbeat.proposed.yaml`
   (the PVC and Service documents in it are identical to the applied ones, so this is a
   Deployment change; `Recreate` takes the old pod down first, which is required — one pod holds
   `hostPort` 5051 and an RWO PVC).
5. **Watch one pod cycle.** `kubectl -n dama rollout status deploy/hear-heartbeat --timeout=180s`

### Acceptance evidence — what gate 1 requires before it closes

Collected against the **live** rollout; none of it can be produced by CI, and until it exists the
cutover is not complete.

| # | Evidence | How |
|---|---|---|
| 1 | The pod is `Ready` **on the digest**, not on a tag | `kubectl -n dama get pod -l app=hear-heartbeat -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'` |
| 2 | `/healthz` returns the same shape as before | `curl -s http://<node>:5051/healthz` — `status`, `service`, `redis_target`, `durable_store` keys, same values for `redis_target` and `durable_store.backend` |
| 3 | All six devices appear in the durable ledger **after** cutover | `SELECT device_id, count(*), max(received_at) FROM durable_records GROUP BY 1;` in `/state/heartbeat-receiver.sqlite3` |
| 4 | `received_at` is continuous across the restart | no gap longer than the fleet's heartbeat interval plus the `Recreate` gap, per device |
| 5 | Zero failed cache attempts after cutover | `SELECT outcome, count(*) FROM cache_attempts WHERE id > <pre-cutover max id> GROUP BY 1;` |
| 6 | The Redis compatibility keys are still armed | `redis-cli --scan --pattern 'dama:hear:*'` and a `TTL` on one of them |
| 7 | No `pip install` ran | `kubectl -n dama logs deploy/hear-heartbeat` — the preamble is gone, so the first log line is the receiver's |
| 8 | The ConfigMap is applied and unreferenced | `kubectl -n dama get cm hear-heartbeat-code` succeeds; the pod spec has no `code` volume |
| 9 | **Rollback performed, not assumed** | below — and the pod returned to `Ready`, with the ledger continuous across *both* transitions |
| 10 | The digest, source commit and lock hash are in git | `digests.txt`, the `image:` field, and `requirements/lock/service-hear-heartbeat.txt` |

## Rollback

One command, one pod cycle, no data migration to reverse:

```sh
kubectl -n dama apply -f deploy/k8s/hear-heartbeat.yaml     # the pre-cutover manifest, unchanged
# or, equivalently, for the Deployment alone:
kubectl -n dama rollout undo deployment/hear-heartbeat
kubectl -n dama rollout status deploy/hear-heartbeat --timeout=180s
```

The ConfigMap it re-references was never deleted, so there is nothing to restore first. If it
somehow was: `kubectl -n dama apply -f deploy/k8s/hear-heartbeat-code.yaml` regenerates nothing —
the file in git *is* the artifact (`python3 deploy/k8s/gen_configmap.py hear-heartbeat-code`
reproduces it byte for byte, which `tests/test_configmap_sync.py` proves on every run).

⚠️**Rollback is part of the gate, not a contingency.** Gate 1 does not close until a rollback has
been performed on the live workload and the pod has returned to `Ready` on the ConfigMap path,
with the durable ledger continuous across both transitions. A rollback that has only been written
down has not been tested.

## Adding the next service image

1. `requirements/image-service-<workload>.txt` — the workload's non-stdlib imports, at the
   versions `requirements/ci-pods.txt` already pins.
2. Add it to `SERVICES` in `../gen_pip_lock.py`, then
   `python3 deploy/images/gen_pip_lock.py service:<workload> > requirements/lock/service-<workload>.txt`.
3. `Dockerfile.<workload>`, building on the *smallest* base that already carries its heavy
   dependencies (`hear-runtime`, `hear-numeric`, `hear-ml-cpu`, `hear-ml-gpu`) — a service lock
   covers only what that base lacks.
4. `deploy/k8s/<workload>.proposed.yaml` — the cutover manifest, changing only the five things in
   `docs/worker-packaging.md`'s per-workload procedure.
5. A row in `digests.txt`, `pending` until `main` publishes it.
6. A build step and a content check in `.github/workflows/images.yml`.

`tests/test_service_images.py` is parameterised over `SERVICES`, so steps 1–6 are each enforced
by a failing test rather than by this list.
