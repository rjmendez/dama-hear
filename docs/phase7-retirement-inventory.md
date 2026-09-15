# Phase 7 retirement inventory (read-only, no authorization to retire)

Status: **inventory only**. Measured 2026-09-15 against `main` (`094810c`) and the live cluster
with read-only commands. This document authorizes **nothing**. It does not disable, delete, cut
over, scale, flash, expire, or migrate anything, and it is explicitly **not** a removal plan.

Phase 7 is strictly sequential behind Phase 2 (canonical store), Phase 3 (object import),
Phase 4 (dual write), Phase 5 (compare) and Phase 6 (read cutover), in that order, as fixed by
[standalone-migration.md](standalone-migration.md) §"Staged migration". None of those phases has
closed: there is **no hear-owned PostgreSQL or object store in the cluster** (only shared
`infra/audit-postgres`, `matrix/synapse-oxalis-postgres`, `infisical/postgres` exist; no MinIO),
and Phase 2's 14-day durable soak gate does not mature before **2026-09-29T13:12Z**
([migration-risk-register.md](migration-risk-register.md) "Soak validity"). Every row below is
therefore a *candidate*, dated, with the earliest phase at which it may even be **discussed**.

## How this was measured

Read-only only. Repository: fresh clone of `main`, `git`/`grep` reads. Cluster: `kubectl get`,
`kubectl get -o json`, and `kubectl exec` limited to read commands (`ls`, `env`, `grep`, Redis
`SCAN`/`TTL`/`TYPE`/`SMEMBERS`/`XLEN`/`INFO`/`CONFIG GET`). No object was created, patched,
deleted, scaled, restarted or applied; no key was written or expired; no retention, hold,
destruction or backup job was invoked; no firmware was touched.

---

## 1. Redis as source of truth

### 1.1 Measured state (live, 2026-09-15T18:2xZ)

| Fact | Measurement |
|---|---|
| Instance | `infra/StatefulSet/audit-redis`, `Service audit-redis` (`6379`, NodePort `30379`), image `redis:latest` (**unpinned tag**) |
| Tenancy | **Shared.** `db0` holds **43 260** keys; `dama:hear:*` is **10** of them. Dominant tenant `dama:consensus:*` (43 121). Others: `dama:colony`, `dama:wifi`, `dama:snapshot`, `dama:rl`, `dama:sim`, `dama:sensor`, `dama:bridge`, `dama:device_status`, `dama:device_snapshot`, `mesh:registry` |
| Eviction policy | `maxmemory-policy=allkeys-lru`, `maxmemory=6442450944`. **`dama:hear:*` is evictable irrespective of TTL.** `evicted_keys=0` so far; `expired_keys=2609` |
| Auth | Single `--requirepass` value passed as a **plaintext StatefulSet argument**; no ACL user separation between tenants (secret value deliberately not reproduced here) |

Live `dama:hear:*` keys, types and TTLs:

| Key | Type | TTL observed | Writer | Note |
|---|---|---|---|---|
| `dama:hear:{device_id}` | string | 21–28 s (TTL 30 s) | `hear_heartbeat_receiver.py:920`, bridge | present for `ageev`, `gold`, `kasami`, `mach` only |
| `dama:hear:devices` | set | **`-1` (no TTL)** | `:921`, `:936` | members `ageev, gold, kasami, mach, nyquist, rankine, test-verify` — contains a **test artifact** and two devices with no live key |
| `dama:hear:latest` | string | `-1` | `:922` | fleet-global "last heartbeat" |
| `dama:hear:event:{device_id}` | string | **`-1`** | `:937` | present for `mach`, `nyquist`, `rankine` — unbounded, no expiry |
| `dama:hear:events` | stream | `-1`, `XLEN=305` (maxlen 1024) | receiver/bridge | **lossy ring**, cannot be retirement evidence |

Readers/writers found: writers are `tools/hear_heartbeat_receiver.py` and `tools/hear_mqtt_bridge.py`
(and their ConfigMap copies `hear-heartbeat-code`, `hear-mqtt-bridge-code`). Read-only consumers in
tree: `tools/bridge_soak_evidence.py` (allow-listed read commands, `:105`),
`docs/durable-outbox-failure-drill-runbook.md`, `deploy/images/service/README.md` evidence step 6.
Contract baseline: `docs/data/phase0-freeze-contracts.v1.json` §Redis keys,
`tools/freeze_contracts.py:395-414`.

**Hidden-consumer sweep (cluster-wide, all namespaces):** only `dama/hear-heartbeat` and
`dama/hear-mqtt-bridge` reference `dama:hear` in any Deployment/CronJob/StatefulSet/DaemonSet spec.
Eleven other workloads use `audit-redis` (`agents/charlie`, `agents/oxalis`,
`agents/prometheus-oxalis-orchestrator`, `dama/ant-mirror-daemon`, `dama/dama-bridge-rust`,
`infra/consensus-engine`, `infra/dama-gpu-feedback`, `infra/fleet-api`) but on **other prefixes**;
`infra/fleet-api` reads `dama:ui:fleet_state`, `dama:world:state`, `dama:colony:state`,
`dama:sensor:*` — **not** `dama:hear:*`. No evidence of an out-of-cluster consumer was found, and
absence of a spec reference is **not** proof of absence for ad-hoc/operator clients (see §9).

### 1.2 Disposition

| Candidate | Category | Earliest phase/gate | Dependencies & owners | Destructive action required | Reversible shadow step | Proof needed before retirement | Residual risk |
|---|---|---|---|---|---|---|---|
| `dama:hear:*` **as authority** (liveness, latest, last event) | **Replace** (authority moves to canonical store; cache role kept) | Phase 7 step 1, gated on Phase 6 read cutover having served canonical reads for a full compatibility window | Owners: hear platform (receiver/bridge); consumer risk owner: operator tooling | **None.** Stopping *reliance* is a config/read-path change, not a delete | Keep writing the keys as a compatibility mirror while every operator surface reads canonical; flip back by pointing reads at Redis | Canonical health projection reproduces per-node liveness for ≥1 full window; drill proof that deleting/losing Redis changes no durable result (runbook L2/L6 already models it); R1–R3 fixed and re-soaked | `allkeys-lru` can evict hear keys during a memory spike in the *other* tenant (43k `dama:consensus` keys), so legacy liveness can go false-offline at any time before cutover |
| `dama:hear:events` stream (maxlen 1024) | **Remove** (after replacement) | Phase 7 step 1, after the above | Same | `XDEL`/key delete — **forbidden in the same release as any code cutover** | Leave the stream armed and unread; consumers ignore it | No consumer reads it for one window; canonical event log conserves rows (`input = accepted + duplicate + refused`) | It is already lossy (305/1024): it must never be used to prove conservation |
| `dama:hear:devices` set incl. `test-verify` | **Replace** (canonical device registry) | Phase 7 step 1 | Hear platform | `SREM`/delete | Mark stale members in the canonical registry first | Canonical registry lists exactly the enrolled fleet; `test-verify` traced to its creator | Removing a member is indistinguishable from a decommission unless the canonical registry records the reason |
| Shared `audit-redis` instance itself | **Keep** (not hear's to retire) | never, by hear | `infra` owns it; 8+ foreign consumers | **Never** `FLUSHDB`/`FLUSHALL`/rename — destroys other tenants (runbook §"Never") | n/a | n/a | Any hear-side "cleanup" script that scans without a `dama:hear:` prefix filter is a multi-tenant incident |
| Unpinned `redis:latest` + plaintext `--requirepass` arg + no per-tenant ACL | **Replace** (security lane, *not* a Phase 7 item) | independent of migration phases | `infra` | none | n/a | n/a | A tenant-scoped ACL is the prerequisite for hear ever deleting keys safely |

---

## 2. Shared PVC and mutable ConfigMap packaging

### 2.1 Measured state

`dama/hear-pool` (5 Gi requested, `local-path`, RWO, `reclaimPolicy: Delete`, node affinity
`desktop-bvrdk4j`, ~14.5 G in use per `storage-inventory`) is mounted by **14 live workloads**:
`hear-annotate`, CronJobs `hear-drain`, `hear-score`, `hear-tag`, `hear-tdoa`, `hear-birdnet`,
`hear-embed` and each `-check` twin — **plus `dama/dama-sketch-corpus`**, a dama-gotchi-owned
Deployment that mounts the same PVC read-write at `/pool` and shares `/pool/pylib`.

Two smaller state PVCs: `hear-heartbeat-state` (SQLite durable store, `HEAR_DURABLE_STORE=sqlite`,
`HEAR_DURABLE_DB=/state/heartbeat-receiver.sqlite3`) and `hear-mqtt-bridge-state` (Phase 2 soak
evidence). Both `local-path`, RWO, unbacked (R6, R7).

Mutable-packaging evidence: code is mounted from ConfigMaps (`hear-drain-code` 11 keys,
`hear-tdoa-code` 27, `hear-score-code` 7, `hear-tag-code` 7, `hear-annotate-code`,
`hear-heartbeat-code`, `hear-mqtt-bridge-code`) and dependencies are `pip install`ed at start into
the PVC or `emptyDir`: `numpy==2.2.6` (drain/score/tdoa → `/pool/pylib`), `onnxruntime==1.27.0`,
`ai-edge-litert`, `fastapi`/`uvicorn`, `redis==7.4.0`, `paho-mqtt` (bridge → `/deps`; sketch-corpus
→ `/pool/pylib`). **Live drift measured:** `hear-drain-code` carries
`dama-hear/commit=eccd7e9` while `hear-heartbeat-code`/`hear-mqtt-bridge-code` carry `8bebb50`,
and repository `main` is `094810c` — three generations coexist under one PVC.

### 2.2 Disposition

| Candidate | Category | Earliest phase/gate | Dependencies & owners | Destructive action required | Reversible shadow step | Proof needed | Residual risk |
|---|---|---|---|---|---|---|---|
| `hear-pool` as **shared cross-workload application directory** | **Replace** with per-service staging volumes + object store | Phase 7 step 2, strictly after Phase 3 import **and** a verified restore (R6 gate: backup exists and has been test-restored) | Owners: hear deployment; **cross-owner: dama-gotchi owns `dama-sketch-corpus`, which writes `/pool/sketch_corpus`** | Unmounting is non-destructive; **PVC delete is destructive and `reclaimPolicy: Delete` makes it irreversible** | Remount every consumer read-only against a frozen snapshot while canonical reads serve; revert by remounting RW | Object-store import reconciles per source/day/node with the `/pool` ledger; a clean-room restore reproduces a known event; gotchi's writer has an agreed alternative path | Single-node `local-path` with no replica: losing the node loses both the source and the rollback target before an off-node backup exists |
| `/pool/pylib` runtime pip target | **Remove** | Phase 1.5 (OCI images) — **earlier than Phase 7**, independently | deployment maintainers | delete a directory on the PVC | Ship the image, keep the ConfigMap+pip path applied but unused | Pilot workload runs from an immutable digest and a rollback has been *performed* (worker-packaging gate 1, evidence rows 1–10) | A registry outage during a restart currently takes the durable writer down (R9) |
| Code-bearing ConfigMaps (`*-code`) | **Keep as rollback artifact**, then remove | Phase 7 last, after every workload's image gate closes | deployment maintainers | `kubectl delete cm` | Leave the ConfigMap applied and unreferenced by the pod spec | Each workload has closed its image exit gate and its rollback manifest has been exercised | Deleting the ConfigMap deletes the documented one-command rollback (`worker-packaging.md` §ConfigMap-to-image cutover) |
| Live-vs-repo ConfigMap/manifest drift (`eccd7e9` vs `8bebb50` vs `094810c`; live `Recreate` strategy vs open PR #189) | **Replace** (reconcile) — a *precondition*, not a retirement | before any Phase 7 action | deployment maintainers | none | n/a | live generation reconciles against committed manifests | Applying from `main` today can revert `Recreate` → `RollingUpdate` and put two writers on one RWO SQLite ledger (R8) |

---

## 3. AWS ingest / forwarding / gotchi adapters

### 3.1 Measured state

Firmware pushes telemetry over TLS to `HEAR_PUSH_HOST` (default
`api.botnet.floppydicks.net`, port 443, `/ingest/batch`, bearer token,
`firmware/hear_node/hear_node.ino:100-2602`), whose ACM certificate chains to Amazon Root CA 1
(`hear_push_ca.h`). The documented chain is API Gateway → Lambda → SQS → Oxalis SQS →
`dama-sqs-consumer.py` → local MQTT (`deploy/k8s/hear-mqtt-bridge.yaml` header,
`standalone-migration.md`). In-cluster, `dama/mqtt-bridge-botnet` relays a **remote** broker
(`REMOTE_MQTT_HOST/USER/PASS` from secrets) into `127.0.0.1:31883`, and `dama/hear-mqtt-bridge`
(`hostNetwork`, plaintext MQTT, no mTLS) consumes `dama/+/telemetry` from that local broker.
`agents/oxalis` and `agents/prometheus-oxalis-orchestrator` are foreign-owned workloads on the
same Redis. gotchi coupling in tree: `hear-drain --phone-corpus /pool/sketch_corpus`;
`tools/gen_golden.py` / `tests/test_sketch_port.py` byte-agreement with `AcousticSketch.kt`;
`source="phone"` / `gotchi-phone` fallback class in `hear/solve/point.py`, `tools/hear_tdoa.py`.

### 3.2 Disposition

| Candidate | Category | Earliest phase/gate | Dependencies & owners | Destructive action | Reversible shadow step | Proof needed | Residual risk |
|---|---|---|---|---|---|---|---|
| AWS/Oxalis **forwarding path in use** | **Replace** (keep the adapter package and replay credentials) | Phase 7 step 3, per site, only where the local broker/API is proven reachable | Owners: dama-gotchi cloud + `agents/oxalis`; hear consumes | Deleting queues/routes/credentials — **out of scope, separate approval** | Point firmware `HEAR_PUSH_HOST` at the local ingest while leaving the cloud route armed; revert by reverting the provisioned host (no reflash: `prov.push_host` is provisioned, `hear_node.ino:241-278`) | Phase 4 HTTPS batch ingest accepts the same schema; a node's offline SD backlog replays without the cloud; zero canonical loss for a full window per board class | Firmware fleet is fragmented across three versions (R13) and mach's old build already produces rejected events; a host switch during fragmentation can strand one build |
| `dama/mqtt-bridge-botnet` remote relay | **Remove** (only with the AWS path, never before) | Phase 7 step 3 | Foreign remote broker owner | Deployment delete | Scale-to-zero is *not* proposed now; shadow = local ingest carries the same records first | Local path carries every device for a window with no gap in `durable_records.received_at` | It is the only path for a node that cannot reach the LAN broker; removing it converts a Wi-Fi fault into total telemetry loss |
| gotchi `dama-sketch-corpus` writer on `hear-pool` | **Replace** (optional adapter submitting canonical envelopes) | Phase 7 step 2 (PVC isolation), step 4 for the adapter itself — **per-site/operator choice, never a platform decision** | **Cross-owner: dama-gotchi maintainers**; requires their agreement and a migration window | none by hear | Dual-write phone sketches to the canonical path while the PVC write continues | Node-only capture/solve/restore is green with zero gotchi deployments (adapter-conformance case 4) | Silent breakage of a *foreign* workload if hear changes the PVC without the gotchi owner's sign-off |
| `AcousticSketch.kt` golden vectors, `source=phone`/`gotchi-phone` semantics | **Keep** (compatibility fixtures) | n/a | core maintainers | none | n/a | n/a | Retiring them would delete the only proof that historical phone rows decode identically |

---

## 4. Blind polling health assumptions

| Candidate | Evidence | Category | Earliest phase/gate | Destructive action | Reversible shadow step | Proof needed | Residual risk |
|---|---|---|---|---|---|---|---|
| Redis-TTL liveness (`dama:hear:{node}` 30 s ⇒ "offline") | `HEAR_HEARTBEAT_TTL_S=30`; R3: deduplicated heartbeats never re-arm the TTL, so a reboot-looping but healthy node reads offline; live `nyquist`/`rankine` have no key while remaining set members | **Replace** with a durable health projection carrying expiry semantics | Phase 7 step 1; blocked on R3 fix + re-soak | none | Serve legacy TTL verdicts while the canonical projection is computed and compared | Canonical projection and Redis agree for one full sample window, including a reboot loop and a Redis outage | This is the exact misleading-health-flag class the postmortem exists to eliminate; retiring it *before* the projection is trusted swaps one blind signal for another |
| `*-check` CronJob exit codes as fleet health (`hear-drain-check` `17 * * * *`, tag `:52`, score `:27`, birdnet `:58`, embed `:38`, tdoa `:47`) | Each reads `/pool` heartbeat/watermark files; thresholds (`--max-stale-s`, `--unfetched-window-s`, clip deferred/lost) are operator-tunable, so "healthy" is a flag set, not a fact | **Replace** (source-aware freshness from canonical records) | Phase 7 after Phase 6 read cutover | none | Keep the CronJobs running and compare their verdicts to canonical freshness | Verdict parity for a window, including a deliberately stale lane | Their history already includes a gate that could not fail (exit code taken from `--stats`); a replacement must keep failing **closed** |
| Node `/status` live polling (`check_fleet_health.py`, `fleet.py`, 15 s timeout, 2 attempts, unauthenticated) | `EXPECTED_NODES` and `DEPLOYED_DEFAULT_IPS` are **hard-coded** in the tool; drain/check CronJobs embed `172.16.100.x` | **Keep** as a recovery/diagnostic adapter; **Replace** the hard-coded fleet map with site config | Phase 7 (map), never for the probe itself | none | Read the map from canonical device registry while the literals remain as fallback | Registry-driven runs reproduce the literal-driven result | `mic_state` from `/status` is a latched boot probe (R11): any retirement gate keyed to it will refuse healthy nodes |
| `selftest.mic` / `mic_state` as hardware truth | R11 (gold/ageev false `capture-failure`) | **Keep** field for compatibility, **Replace** as a gate input | fleet lane, independent of Phase 7 | none | n/a | A re-probed firmware reports a state consistent with measured capture | Two incorrect hardware conclusions were already drawn from it |

---

## 5. Legacy storage and read paths

| Path | Evidence | Category | Earliest phase/gate | Reversible shadow step | Proof needed | Residual risk |
|---|---|---|---|---|---|---|
| `/pool/corpus` JSONL products (records, scores, tags, scene, tdoa, `ledger.jsonl`, `index.jsonl`) | `phase6-operator-read-audit.md` §"Authoritative. Unchanged." | **Keep** (read-only archive) until the retention clock expires; **Replace** as the authority | Phase 7 step 2, after Phase 3 import + restore proof | Freeze to read-only mounts while canonical serves | Row/byte conservation against the ledger; checksums exact | `ledger.jsonl` is the authoritative "how much data" answer; losing it removes the only conservation baseline |
| `annotations.sqlite3` (human ground truth, live WAL) | `hear-annotate` `/api/export` is an **unbounded, unauthenticated** full export | **Keep. Never a retirement candidate.** | n/a | n/a | n/a | Must be captured with the SQLite backup API, never a raw file copy |
| `heartbeat-receiver.sqlite3` / bridge outbox (Phase 2 durable seam) | Live, carrying the fleet; R1–R3 are now *production* defects | **Replace** by Postgres at Phase 2 — **not** a Phase 7 item | Phase 2 | Dual-write, keep SQLite | R1–R3 regressions pass and the 14-day soak re-runs | The soak evidence itself is on an unbacked `local-path` PVC (R7) |
| `hear_drain.py` fixed-tail acquisition | 2 MB scene tail loss after long outage (issue #107) | **Keep** as recovery/backfill adapter | not retired in Phase 7 | n/a | Either a bounded range catch-up or an explicitly accepted, documented RPO | Retiring drain before that decision converts a known gap into an unbounded one |
| `decode_ValueError` rows discarded before durable write (R4, issue #104) | Bridge rejects malformed messages with no refusal row | **Replace** (durable refusal rows) — Phase 1/2 blocker | before Phase 2 | n/a | A malformed message produces a durable, countable, replayable refusal | **Row conservation is unclosable until this lands, so no retirement can be proven at all** |

---

## 6. External owners and hidden consumers

| Party | Coupling | Consent needed for |
|---|---|---|
| `infra` (audit-redis, audit-postgres, fleet-api, consensus-engine, dama-gpu-feedback) | Shared Redis instance and credentials; 43 121 `dama:consensus:*` keys share the keyspace | Any key deletion, ACL, memory-policy or instance change |
| dama-gotchi (`dama/dama-sketch-corpus`, phone producers, Android sketch port) | RW mount of `hear-pool`; `/pool/sketch_corpus`; `/pool/pylib`; golden vectors | PVC isolation, `/pool` freeze, adapter retirement |
| `agents` (oxalis, prometheus-oxalis-orchestrator, charlie) | Cloud forwarding chain and shared Redis | Disabling AWS forwarding |
| Remote MQTT broker owner (`mqtt-bridge-botnet` secrets) | Credentialed relay into the local broker | Relay removal |
| Operators / ad-hoc clients | `redis-cli`, `kubectl exec`, runbooks that read `dama:hear:*` directly | Contract change announcements |
| `dama/ant-mirror-daemon`, `dama/dama-bridge-rust` | Same Redis, other prefixes | none for hear, but they share the eviction budget |

**Unknowns that must be closed before any removal:** no `CLIENT LIST`/keyspace-notification census
of who actually touches `dama:hear:*` was taken (it would need a longer read window than this
inventory); no audit of operator scripts outside the repository; no confirmation that the AWS-side
Lambda/SQS resources have no second subscriber.

---

## 7. Data retention, backup and legal constraints

- `docs/data-governance.md` requires a declared purpose, an approved retention class and an
  auditable access path per item; deletion must cascade to indexes, caches, exports, backups and
  derived copies, recording completion or failure **per location**; restores preserve original
  object IDs, hashes, retention clocks and custody history; legal holds suspend deletion and are a
  separately audited action. **A Phase 7 removal therefore requires a separately approved
  retention/destruction record — this document is not one and does not request one.**
- No hold state, retention class assignment or destruction request was read, created or invoked.
- `/pool` currently has **no backup, snapshot or replica** (R6); `local-path` `reclaimPolicy:
  Delete`; cluster backups cover only the k3s datastore. `pool-backup-restore.md` is the G0 gate:
  a restorable backup must exist and have been **test-restored** before the first import byte.
- Raw audio and clips are the most restricted class; any export path (`/api/export`,
  `/api/audio/{clip_key}`) is currently unauthenticated and must not be widened during migration.

---

## 8. Rollback artifacts and observable rollback conditions

Artifacts that must remain intact for the whole retirement sequence (retiring any of them is
itself a retirement candidate, and the **last** one):

| Artifact | Why it is the rollback |
|---|---|
| `deploy/k8s/hear-heartbeat.yaml`, `hear-mqtt-bridge.yaml`, `hear-drain.yaml`, … (pre-cutover manifests) | `kubectl apply` / `rollout undo` restores the ConfigMap+pip topology in one pod cycle |
| `*-code` ConfigMaps | The mounted source the rollback manifests reference |
| `docs/data/phase0-freeze-contracts.v1.json` + `tools/freeze_contracts.py` | The frozen legacy contract (Redis keys, wire, schemas) a rollback must still satisfy |
| `docs/durable-outbox-failure-drill-runbook.md` | The only rehearsed procedure for Redis-loss behaviour, with a `FLUSHDB`-never rule |
| `pool-backup-restore.md` archives/manifests/receipts (once produced) | The only path back from a bad import |
| AWS/Oxalis route + replay credentials | The only path back if the local ingest cannot reach a node |
| `requirements/lock/*`, `digests.txt` | Pin the image a cutover rolls forward to and the lock a rollback rolls back to |

Observable rollback conditions (from `standalone-migration.md` §Rollback, unchanged): canonical
loss/duplication outside the idempotency rule; raw checksum mismatch; row conservation not closing;
shadow-read disagreement beyond tolerance; p99 ingest lag breaching the SLO for 15 minutes; restore
failing to reproduce a known event; any change in node capture/SD behaviour; a board class needing
an unapproved wire interpretation. Rollback is configuration-first: switch reads back, keep both
writers, replay the inbox. Firmware rollback is a separate canary decision.

---

## 9. Summary: categories and earliest phase

| Keep | Replace | Remove (latest, separately approved) |
|---|---|---|
| `audit-redis` instance (foreign); node HTTP `/status` probe; `hear_drain.py` as backfill; golden sketch vectors; `annotations.sqlite3`; `/pool` read-only archive until retention expiry; rollback manifests and ConfigMaps *until* their gates close | Redis as liveness/event authority; TTL- and CronJob-exit-code health; `/pool` as shared application directory; ConfigMap+pip packaging; AWS/Oxalis forwarding *in use*; gotchi PVC writer; hard-coded fleet maps; SQLite durable seam (Phase 2) | `dama:hear:events` stream; `dama:hear:event:*` keys without TTL; `test-verify` registry member; `/pool/pylib`; `mqtt-bridge-botnet` relay; `*-code` ConfigMaps |

Earliest possible ordering, all still blocked today: **Phase 1.5** OCI images (independent of
retirement) → **Phase 2** Postgres after R1–R3 fixes and a re-soak → **Phase 3** import after the
R6 backup/restore gate → **Phase 4** dual write → **Phase 5** compare → **Phase 6** read cutover →
**Phase 7** step 1 Redis authority, step 2 PVC isolation, step 3 AWS forwarding, step 4 gotchi
adapter — one dependency at a time, never two in one release, and never a delete in the same
release as a code cutover.

### Residual risks that outlive every row above

1. **Row conservation is unclosable today** (R4): no retirement can be *proved*, only asserted.
2. **The rollback target is unbacked** (R6/R7): `/pool` and the soak evidence are single-node
   `local-path` with `reclaimPolicy: Delete`.
3. **Multi-tenant blast radius**: any unprefixed Redis operation, or any PVC action taken without
   the dama-gotchi owner, damages a workload hear does not own.
4. **`allkeys-lru`**: hear's "TTL means liveness" assumption can be falsified by another tenant's
   memory growth at any moment, before or after retirement.
5. **Absence of evidence**: the hidden-consumer sweep covers cluster workload specs only.
