# Runbook — the `hear-heartbeat` ConfigMap → image cutover (Phase 1.5, gate 1)

What to do, in order, on the day the pilot workload stops arriving as a `ConfigMap` `subPath`
mount plus a `pip install` and starts arriving as an image; what to capture while doing it; how
to decide it worked; and how to put it back.

* The **decision** is [worker-packaging.md](worker-packaging.md) (boundaries, ordering, gates,
  rollback).
* The **artifact and its procedure** are
  [`deploy/images/service/README.md`](../deploy/images/service/README.md) — what the image is,
  the cutover steps, the ten-row evidence table.
* This document is the **operational procedure**: preconditions with their commands, the
  measurements, the negative scenarios, and the exact rollback. It does not restate the design.
* The **instrument** is [`tools/verify_heartbeat_cutover.py`](../tools/verify_heartbeat_cutover.py),
  guarded by `tests/test_heartbeat_cutover_verifier.py`. It reads files and computes verdicts. It
  never applies, patches, restarts, pulls, publishes, edits a sentinel or opens a database for
  writing, and `plan` *prints* commands rather than running them.

> ⚠️**THE CUTOVER IS CURRENTLY BLOCKED, BY CONSTRUCTION AND ON PURPOSE.**
> `deploy/images/service/digests.txt` records `hear-heartbeat` as `pending` and
> `deploy/k8s/hear-heartbeat.proposed.yaml` carries the all-zero digest sentinel, because a pull
> request publishes nothing. Until a `main` build publishes the image and a commit records its
> digest in *both* files, step 5 below must not be performed: applying the proposed manifest
> would produce `ImagePullBackOff` on a digest that cannot exist. `verify_heartbeat_cutover.py
> plan` refuses to print the apply procedure while that is true, and says why.

```sh
python3 tools/verify_heartbeat_cutover.py preflight    # where are we
python3 tools/verify_heartbeat_cutover.py plan         # what happens next, printed, not run
```

---

## 0. Preconditions — all of them, before anything is applied

| # | Precondition | How it is checked | Blocks |
|---|---|---|---|
| P1 | The image is **published** and its digest **recorded** in `digests.txt` *and* the `image:` field, in one commit | `preflight` → "digest published by a `main` build and recorded" | everything |
| P2 | The published image's **provenance** names this repository, the images workflow and the recorded commit | step 1 below, by hand | everything |
| P3 | The **live** Deployment and ConfigMap match the committed manifest and the generated bundle (R8 drift) | `preflight --live-deployment --live-configmap` | everything |
| P4 | A **state backup** taken with the SQLite backup API exists and passes `quick_check` | `preflight --state-backup` | everything |
| P5 | The node that holds `hostPort` 5051 has the digest **pre-pulled** | `preflight --node-images` | apply |
| P6 | The cutover diff is inside the **allowlist**: image, entrypoint, `code`/`deps` volumes and the `checksum/hear-heartbeat-code` pod annotation that restarted for the dropped mount, nothing else | `preflight` → "changes only packaging" | everything |
| P7 | `strategy: Recreate`, one replica, `hostPort` 5051, nine env vars with both `secretKeyRef`s, both probes, the `/state` mount, the PVC claim and the **root uid** are unchanged | `preflight` → the `proposed:` checks | everything |

P2 is the one that cannot be mechanised here, because it needs the registry:

```sh
REF=ghcr.io/rjmendez/dama-hear/hear-heartbeat@sha256:<digest>
docker buildx imagetools inspect "$REF" --raw          # the manifest, without pulling layers
gh attestation verify "oci://$REF" --repo rjmendez/dama-hear
# the labels the CI content check stamped, read back out of the published image:
docker buildx imagetools inspect "$REF" --format '{{json .Image.Config.Labels}}'
```

Accept only if: `org.opencontainers.image.revision` equals the commit in the `digests.txt` tag
column, `dama-hear.lock` names `requirements/lock/service-hear-heartbeat.txt`,
`dama-hear.entrypoint` is `python /app/tools/hear_heartbeat_receiver.py --port 5051`, the SLSA
provenance names `.github/workflows/images.yml` on `main`, and an SBOM is attached. A digest with
no attestation is not the image this repository built, whatever the tag says.

**Offline compatibility, already proven, and worth knowing before you start:**
`tests/test_hear_heartbeat_image_smoke.py` runs the image's exact `COPY` layout and `ENTRYPOINT`
under the proposed manifest's env with **Redis unreachable**, and asserts the probe is green and
an accepted POST is durable *before* it is cached. That is the property this workload's PVC
exists for, so the live cutover starts from a known-good expectation: a Redis outage during the
window degrades the cache, not the ledger.

---

## 1–4. Capture, back up, pre-pull

```sh
NS=dama
kubectl -n $NS get deploy hear-heartbeat -o json > live-deploy.json
kubectl -n $NS get cm hear-heartbeat-code -o json > live-cm.json
kubectl -n $NS get pod -l app=hear-heartbeat -o json > pod-before.json
curl -s http://<node>:5051/healthz > healthz-before.json
redis-cli -h <redis> --scan --pattern 'dama:hear:*' > redis-before.txt
```

**Back the ledger up with the SQLite backup API — never a raw copy of a live WAL.** A `cp` of a
database that is being written is a torn read, and a torn "backup" is worse than none, because it
is believed.

```sh
POD=$(kubectl -n $NS get pod -l app=hear-heartbeat -o jsonpath='{.items[0].metadata.name}')
kubectl -n $NS exec "$POD" -- python3 -c \
  'import sqlite3; s=sqlite3.connect("file:/state/heartbeat-receiver.sqlite3?mode=ro",uri=True); \
   d=sqlite3.connect("/state/pre-cutover.sqlite3"); s.backup(d); d.close(); s.close()'
kubectl -n $NS cp "$NS/$POD:/state/pre-cutover.sqlite3" ./pre-cutover.sqlite3

python3 tools/verify_heartbeat_cutover.py snapshot \
    --sqlite ./pre-cutover.sqlite3 --label before --out snapshot-before.json
```

Write down `max_record_id` from `snapshot-before.json`. **It is the watermark**: every
conservation and continuity statement afterwards is measured against it, and a snapshot taken
without it can only say "the ledger got bigger", which is not the same as "nothing was lost".

Pre-pull, so the `Recreate` gap is a container start and not a registry round-trip:

```sh
sudo k3s ctr images pull "$REF"
sudo k3s ctr images ls | grep "${REF#*@}" > node-images.txt

python3 tools/verify_heartbeat_cutover.py preflight \
    --live-deployment live-deploy.json --live-configmap live-cm.json \
    --node-images node-images.txt --state-backup ./pre-cutover.sqlite3
```

Do not proceed while that exits non-zero.

**Registry and pull policy, decided for the pilot.** The manifest sets no `imagePullPolicy`, and
a digest reference defaults to `IfNotPresent`, so a pre-pulled node starts from its own content
store and the cutover survives a registry outage. `Always` would convert every pod start —
including an unplanned kubelet restart at 03:00 — into a GHCR round-trip, and is therefore a
blocking finding in `preflight`. Whether the cluster gets a local mirror is open question 1 in
[worker-packaging.md](worker-packaging.md) and must be answered **before workload 2**; the pilot
proceeds on the manual pre-pull above.

---

## 5. Apply — what `Recreate` means on one node

```sh
kubectl -n $NS apply -f deploy/k8s/hear-heartbeat.proposed.yaml
kubectl -n $NS rollout status deploy/hear-heartbeat --timeout=180s
```

* `Recreate` **takes the old pod down first, and that is required, not tolerated**: one pod binds
  `hostPort` 5051 and holds an RWO PVC. Under `RollingUpdate` the surge pod would stay `Pending`
  on the port and the volume — and if it ever did start, two processes would write one SQLite
  ledger.
* Therefore **there is a real outage window**, one pod cycle long. During it the node's port 5051
  refuses connections and the fleet's POSTs fail. That is expected; the nodes retry and the
  ledger is idempotent, which is why evidence row 4 asks for continuity within
  `heartbeat interval + Recreate gap`, not for zero gap.
* Single node means **no capacity anywhere else**: if the new pod does not start, the workload is
  down until it does, or until rollback. That is the entire reason the image is pre-pulled and
  the rollback artifact is left applied.
* The PVC is untouched. `Recreate` detaches and re-attaches the same `local-path` volume on the
  same node; nothing about `/state` is created, moved, reformatted or re-owned, and the container
  stays **root** precisely so that it can keep writing files a root pod created.

---

## 6. Post-cutover validation

Wait at least two heartbeat intervals, then:

```sh
kubectl -n $NS get pod -l app=hear-heartbeat -o json > pod-after.json
kubectl -n $NS logs deploy/hear-heartbeat > logs-after.txt
curl -s http://<node>:5051/healthz > healthz-after.json
redis-cli -h <redis> --scan --pattern 'dama:hear:*' > redis-after.txt
redis-cli -h <redis> TTL "$(head -1 redis-after.txt)" >> redis-after.txt

# backup + copy again, exactly as in step 2, into ./post-cutover.sqlite3
python3 tools/verify_heartbeat_cutover.py snapshot --sqlite ./post-cutover.sqlite3 \
    --label after --since-id <watermark> --out snapshot-after.json

python3 tools/verify_heartbeat_cutover.py compare \
    --before snapshot-before.json --after snapshot-after.json
python3 tools/verify_heartbeat_cutover.py health \
    --before healthz-before.json --after healthz-after.json
```

What `compare` asserts, and the question each one actually answers:

| Assertion | The failure it exists to catch |
|---|---|
| `quick_check` is `ok`, no table missing | a torn or truncated ledger |
| the schema fingerprint is identical | a packaging change that migrated state |
| record ids never went backwards | a *different* database (a fresh PVC, an `emptyDir`) |
| every pre-cutover row still present (`--allow-pruned` defaults to **0**) | silent record loss hidden by new traffic |
| every device still present, and none shrank | a device dropped from the ledger |
| every device wrote again *after the watermark* | a device that looks alive only because of old rows |
| per-device gap ≤ interval + Recreate gap | a swallowed heartbeat interval |
| no new `failed` cache attempts | Redis reachable before, unreachable after (env/DNS/`hostNetwork` regression) |
| `succeeded` advanced | a durable-only pod: green probe, dead compatibility keys |
| the ledger grew | a `Ready` pod receiving nothing — probe green, port not serving the fleet |

`health` additionally holds `/healthz` to the *same shape*: `status`, `service`, `redis_target`,
`durable_store{backend,enabled,path,pending_records,cache_successes,cache_failures,last_cache_failure_at}`
and `refusals`, with `redis_target`, `durable_store.backend` and `durable_store.path` **equal**
to the pre-cutover capture. A moved `redis_target` or `path` is a configuration change wearing a
packaging change's clothes.

If retention pruning genuinely ran inside the window (it fires hourly against a 30-day
retention), pass `--allow-pruned <n>` with the number of rows you can account for. Do not raise
it to make a red line green: the default of zero is the correct default for a change that is not
allowed to touch state.

---

## 7. Rollback — performed, not assumed

Gate 1 does **not** close until this has actually been run and the ledger is continuous across
*both* transitions.

```sh
kubectl -n $NS apply -f deploy/k8s/hear-heartbeat.yaml     # the pre-cutover manifest, unchanged
# or, for the Deployment alone:
kubectl -n $NS rollout undo deployment/hear-heartbeat
kubectl -n $NS rollout status deploy/hear-heartbeat --timeout=180s
```

That restores, in one pod cycle: `image: python:3.13-slim`, the `pip install`-then-`exec` shell
preamble, the `code` ConfigMap volume mounted at `/app/tools/hear_heartbeat_receiver.py`
(`subPath: tools_hear_heartbeat_receiver.py`), and the `deps` `emptyDir`. The ConfigMap
`hear-heartbeat-code` was never deleted — it stays applied and unreferenced for the whole lane,
and is removed only in the retirement step. If it somehow was deleted:

```sh
kubectl -n $NS apply -f deploy/k8s/hear-heartbeat-code.yaml
# the file in git *is* the artifact:
python3 deploy/k8s/gen_configmap.py hear-heartbeat-code   # reproduces it byte for byte
```

**There is no state to reverse, in either direction.** The cutover did not create, move,
reformat or re-own `/state`, the PVC, the sqlite path or the uid, so the rolled-back pod opens
exactly the database the image pod was writing — and an older receiver binary reading it is safe
by construction (`cache_claims`, `durable_meta` and `refused_messages` carry no foreign key into
`durable_records`, and the `/healthz` index is purely additive). The pre-cutover backup from step
2 is insurance against corruption, not part of the rollback path.

Evidence it the same way:

```sh
kubectl -n $NS get pod -l app=hear-heartbeat -o json > pod-rollback.json
# backup + snapshot into snapshot-rollback.json, with --since-id <after's max_record_id>
python3 tools/verify_heartbeat_cutover.py compare --transition rollback \
    --before snapshot-after.json --after snapshot-rollback.json
```

The rollback pod must be `Ready`, on `python:3.13-slim`, with the `code` and `deps` volumes back.

---

## 8. Negative scenarios — what each one looks like, and what to do

| Scenario | Symptom | Action |
|---|---|---|
| Manifest applied while the digest is `pending` | `ImagePullBackOff` on the all-zero digest; **no** pod on the new spec, old pod already gone | `kubectl rollout undo`; the sentinel did its job — record the digest first |
| Node not pre-pulled, registry unreachable | `ErrImagePull`/`ImagePullBackOff`; the outage lasts as long as the pull does | roll back, pre-pull, retry. This is the whole argument for P5 |
| Registry reachable but digest absent (tag republished, wrong owner) | the pull fails on the digest; it never "pulls something" | verify P2 with `imagetools inspect`; do not substitute a tag |
| `RollingUpdate` sneaks in | surge pod `Pending` on `hostPort`/PVC, rollout hangs, old pod still serving | re-apply with `Recreate`; never force-delete to "unstick" it — two writers on one ledger is the outcome that loses data |
| Pod `CrashLoopBackOff` on start | `/healthz` never green; check the `HEAR_HEARTBEAT_TOKEN` secret, `/state` writability, the entrypoint argv | roll back; the offline smoke test reproduces the same argv and env locally |
| Pod `Ready`, ledger not growing | `compare` fails "new traffic was accepted" | check the `hostNetwork`/`hostPort` binding and the fleet's target; roll back if unexplained |
| Pod `Ready`, `cache_attempts.failed` climbing | `health` shows rising `cache_failures`; keys not refreshed | Redis reachability from the new pod (env, DNS policy). Durability is unaffected — the outbox replays — but the gate does not close |
| A device silent after the restart | `compare` fails "every device reported again" | that node's retry/backoff, not the packaging — but do not close the gate on it |
| Records missing below the watermark | `compare` fails "every record written before the cutover is still in the ledger" | **stop.** Roll back, keep the backup, investigate before any further apply |
| `quick_check` not `ok` | ledger corruption | roll back, restore from the step-2 backup, treat as an incident |
| Rollback pod cannot mount the ConfigMap | the `code` volume is missing — the bundle was deleted | `kubectl apply -f deploy/k8s/hear-heartbeat-code.yaml`; the file in git is the artifact |

---

## 9. The receipt

```sh
python3 tools/verify_heartbeat_cutover.py receipt \
    --pod pod-after.json --logs logs-after.txt \
    --health-before healthz-before.json --health-after healthz-after.json \
    --snapshot-before snapshot-before.json --snapshot-after snapshot-after.json \
    --redis-keys redis-after.txt --live-configmap live-cm.json \
    --rollback-pod pod-rollback.json --snapshot-rollback snapshot-rollback.json
```

It prints the ten rows of `deploy/images/service/README.md`'s acceptance table with a state per
row, and exits non-zero unless every one is `PASS`:

| # | Evidence | Closed by |
|---|---|---|
| 1 | Pod `Ready` **on the digest**, not a tag; exactly one pod | `pod-after.json` + `digests.txt` |
| 2 | `/healthz` same shape, same `redis_target`, same `durable_store` backend and path | the two health captures |
| 3 | All six devices in the ledger after cutover, none silent | the two snapshots |
| 4 | Every pre-cutover row survived; `received_at` continuous per device | the two snapshots, via the watermark |
| 5 | Zero failed cache attempts after cutover; successes advanced | the two snapshots |
| 6 | Redis compatibility keys still armed, with a positive TTL | `redis-after.txt` |
| 7 | No `pip install` ran — the preamble is gone | `logs-after.txt` |
| 8 | ConfigMap applied and **unreferenced** | `live-cm.json` + the pod's volumes |
| 9 | **Rollback performed**, pod `Ready` on the ConfigMap path, ledger continuous across both transitions | `pod-rollback.json` + the rollback snapshot |
| 10 | Digest, source commit and lock hash in git | `digests.txt`, the `image:` field, the lock |

A `MISSING` row is not a pass. An evidence set that is nine-tenths collected leaves gate 1 open,
and the next workload in the sequence does not start.

## Remaining gates after this one

Gate 1 closing does not license workload 2. `hear-mqtt-bridge` is additionally gated on open
question 1 (registry/mirror and pull policy), on PR #189 giving it `strategy: Recreate`, and on
gate 2: its cutover happens in the same rollout as the R1–R3 correctness fixes, or not during the
counted 14-day soak window at all.
