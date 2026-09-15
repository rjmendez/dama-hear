# Phase 6: operator read surface audit and shadow-read plan

What an operator can read today, who owns each answer, and how a canonical reader could be
compared against it **without** anyone switching a reader over.

> **Status.** Audit and design only. This document changes no reader, enables no shadow
> read, creates no service, CronJob, dashboard, alert rule, credential or cluster object,
> and does not move a frozen contract hash. The surfaces named in §2 remain authoritative
> for every operator answer until the Phase 5 comparator has run and its gate has been
> evaluated. Phase 6 is **gated**; this is preparatory mapping done early on purpose, so the
> cutover sequence is not designed in the same week it is executed.

Companions: [standalone-migration.md](standalone-migration.md) §"4. Cut over reads, retain
writes" (the staged step this phase implements), [migration-risk-register.md](migration-risk-register.md)
(what is currently true), [phase4-dual-write-observability.md](phase4-dual-write-observability.md)
(the write-side comparator and the receipt this plan reuses),
[api-boundaries.md](api-boundaries.md) (the target northbound contract these surfaces must
eventually become), [data-governance.md](data-governance.md) (the access/redaction rules a
read cutover must not weaken).

## 1. Scope

**In scope:** the inventory of every in-repository read surface an operator or an automated
consumer uses for health, detections, scenes, clips, annotations, TDoA, scores and durable
records; the contract, authentication, pagination, freshness semantics, ownership and
privacy boundary of each; the reader-specific migration risks; and a no-cutover shadow-read
integration plan compatible with the Phase 4 receipt and the Phase 5 comparator.

**Explicitly not in scope:** changing any reader; enabling a canonical read path; selecting
a feature-flag mechanism to be *used*; provisioning Postgres, an object store or a
comparator runtime; retiring Redis, the shared PVC or the AWS/Oxalis chain; approving
receipt retention; deciding the p99 ingest-lag SLO (still owed by the operator, ingest spec
§16.1); and any firmware or fleet action.

## 2. Position

| Party | Role in Phase 6 |
|---|---|
| `tools/hear_drain.py --check`, `tools/check_fleet_health.py`, `tools/fleet.py`, `tools/hear_heartbeat_receiver.py` `/healthz`, `tools/hear_annotate/server.py`, the `/pool` JSONL products | **Authoritative.** Unchanged. Every operator default still reads these. |
| Canonical reads (Postgres views, object manifests) | **Claimant.** Not wired to any operator surface, not enabled by this document. |
| The read comparator (§8) | **Auditor.** Reads both, writes receipts, serves nothing and changes neither. |

The auditor has no path into an operator answer. Everything in §8 follows from that: a
shadow read that can change what an operator sees is not a shadow read, it is an unreviewed
cutover.

## 3. Read surface inventory

Domains are abbreviated: **H** health, **D** detections, **Sc** scenes, **C** clips,
**A** annotations, **T** TDoA, **S** scores, **Dur** durable records.

### 3.1 HTTP APIs

| Surface | Domains | Backing store | Auth | Pagination | Freshness semantics |
|---|---|---|---|---|---|
| `GET /healthz` — annotate (`tools/hear_annotate/server.py:613`) | A | SQLite `annotations.sqlite3`; `stat` of `clips/index.jsonl` | none | n/a | O(1) probe by construction; a missing corpus index is **reported, not failed** (`server.py:613-637`) |
| `GET /api/queue?limit=` (`server.py:639`) | C, A | `/pool/corpus/clips/index.jsonl` + SQLite | none | `limit` only, clamped `1..200`, default 25; **no cursor** (`server.py:640-641`) | whatever the cached corpus parse last saw (`CorpusCache`) |
| `GET /api/audio/{clip_key}` (`server.py:645`) | C | clip WAV bytes under the corpus root, `safe_join`-confined | none | n/a | `Cache-Control: no-store`; `410` when the clip was pruned |
| `POST /api/annotations` (`server.py:667`) | A | SQLite; identity from proxy headers (`HEAR_ANNOTATE_TRUSTED_PROXIES`) | none (attribution only) | n/a | `409` on conflicting append |
| `GET /api/export` (`server.py:677`) | A | SQLite, **all** human annotation rows | none | **none — unbounded full export** | n/a |
| `GET /healthz` — heartbeat receiver (`tools/hear_heartbeat_receiver.py:1485`) | H | Redis target + durable SQLite status (`:1093-1098`) | none | n/a | reports cache/durable state, not per-node liveness |
| `POST /api/hear/heartbeat` (`:1498`) | H | writes `dama:hear:{device_id}`, streams, durable SQLite | **required** token, `HEAR_HEARTBEAT_TOKEN` (`:85-90`, `:1512`) | n/a | Redis TTL 30 s (`docs/phase0-freeze-contracts.v1.md` Redis keys) |
| Node `GET /status`, `/ls`, `/sd`, `/audio` (firmware; consumed by `tools/fleet.py:104`, `tools/hear_drain.py:2576`) | H, D, Sc, C | node SD/RAM | **none** (R15) | `/ls` directory listing, bounded tails | live poll, 15 s timeout, two attempts (`fleet.py:38-48`) |

The annotate Service and the heartbeat Deployment are the only in-repo HTTP read surfaces
exposed as cluster objects (`deploy/k8s/hear-annotate.yaml:797`, `deploy/k8s/hear-heartbeat.yaml:103`).
Both Kubernetes probes read `/healthz` (`hear-annotate.yaml:759-767`, `hear-heartbeat.yaml:83-87`),
which makes the readiness contract an operator-visible read surface, not an implementation
detail — the annotate probe comment records what happened the last time that was forgotten.

### 3.2 Scheduled readers (CronJob `-check` lanes)

These are the closest thing the deployment has to a dashboard: an hourly job whose exit code
is the signal.

| CronJob | Schedule | Reads | Failure meaning |
|---|---|---|---|
| `hear-drain-check` | `17 * * * *` | `/pool/heartbeat.json` (`hear_drain.py:2268`), watermarks (`:714`), optional node `/status` | stale sensor, unfetched scene bytes, deferred/lost clips, live-ring loss (`:2530-2568`) |
| `hear-tag-check` | `52 * * * *` | `/pool/corpus/clips/tags.jsonl` and inputs | tag lane stalled |
| `hear-birdnet-check` | `58 * * * *` | pool records/audio | bioacoustic lane stalled |
| `hear-embed-check` | `38 * * * *` | pool records/clips | embed lane stalled |
| `hear-score-check` | `27 * * * *` | `/pool/corpus/scores` | score lane stalled |
| `hear-tdoa-check` | `47 * * * *` | `/pool/corpus/tdoa/{arrivals,runs,model_card.json}` | TDoA lane stalled |

Schedules and paths: `docs/phase0-freeze-contracts.v1.md` §"Kubernetes and PVC layout".
`standalone-migration.md` already states the rule this creates a risk against: the core
"must not infer health from a CronJob exit code or schedule". Today, an operator does
exactly that, because it is the only aggregate signal that exists.

### 3.3 CLI and runbook readers

| Tool | Domains | Contract | Notes |
|---|---|---|---|
| `tools/hear_drain.py --check/--stats/--json` | H, D, Sc, C | human lines or a JSON run report (`:2657-2658`) | exit 1 on stale/missing heartbeat (`:2530-2536`); `--max-stale-s`, `--unfetched-window-s`, clip deferred/lost thresholds are all operator-tunable, so "healthy" is a flag set, not a fact |
| `tools/check_fleet_health.py` (`--data-report`, `--json`) | H | human table (`ONLINE`/`DEGRADED`/`OFFLINE`, `:1540-1542`) or JSON | reads DHCP/ARP leases, node `/status`, drain/score/tag heartbeats, durable health, and `kubectl` logs (`:883`, `:1418-1464`); OPNsense credentials come from a Kubernetes Secret (`:1460`) |
| `tools/fleet.py` | H | per-node `/status` summary | `--require-one-build` exit codes `0/1/2` (`:184-199`); without it, exit status means *answerability*, not health |
| `tools/bridge_soak_evidence.py` | H, Dur | Markdown or JSON snapshot (`:1098-1116`) | allow-listed read-only Redis commands (`:105`); the Phase 2 gate evidence reader ([bridge-durable-soak.md](bridge-durable-soak.md)) |
| `tools/hear_tdoa.py` | T | `read_ledger()`, `read_emitted_events()` (`:1813-1835`), `hear.tdoa_arrival.v1`, `hear.tdoa_model_card.v1` | filesystem records under `/pool/corpus/tdoa` |
| `tools/hear_score.py` | S | `hear.sketch_score.v1` JSONL | reads pool sketches + model files |
| `tools/hear_tag.py` | C, D | `hear.clip_tag.v2` | reads WAV/audio inputs (`:400`) |
| `tools/hear_listen.py`, `tools/hear_pair_census.py`, `tools/field_log.py` | C, D | sampled corpus/report output | operator inspection lanes |
| `hear/objectstore/ledger.py` | Dur | JSONL replay (`:88-127`) | resume reads the **local ledger**, not a bucket listing (`:1-18`) |

Runbooks that codify these reads: [durable-outbox-failure-drill-runbook.md](durable-outbox-failure-drill-runbook.md),
[bridge-durable-soak.md](bridge-durable-soak.md), [pool-backup-restore.md](pool-backup-restore.md),
[hear-latency-calibration-runbook.md](hear-latency-calibration-runbook.md). Each one is a
read consumer with an exit criterion, and each one has to keep working across a read cutover
or the gate evidence for the *other* phases becomes unreadable mid-migration.

### 3.4 Implicit consumers (the couplings a cutover actually breaks)

| Coupling | Evidence | Why it matters to a read cutover |
|---|---|---|
| Redis `dama:hear:{device_id}` (TTL 30 s), `dama:hear:devices`, `dama:hear:latest`, `dama:hear:event:{device_id}`, `dama:hear:events` (maxlen 1024) | freeze §"Redis keys"; writer `HeartbeatReceiverStore` | key **expiry is the liveness answer**. A canonical health projection must express "expired" explicitly; absence is not a value a comparator can compare |
| Shared PVC `hear-pool` (RWO, 5 Gi, `local-path`) read by 10 CronJob containers | freeze §"PVCs" | every lane reads another lane's application directory. A read cutover per lane is only possible because the *files* are the interface; that is also why it is fragile (R6, R7) |
| `hear-heartbeat-state`, `hear-mqtt-bridge-state` PVCs | freeze §"PVCs" | the durable SQLite outbox and the Phase 2 soak evidence live here; readers of the gate evidence are readers too |
| AWS/Oxalis chain | `tools/hear_mqtt_bridge.py:7` (comment), `deploy/k8s/hear-mqtt-bridge-code.yaml:1693` | **no in-repo consumer imports an AWS SDK**; `tools/recoupling_guard.py:73-75` makes that merge-blocking. The AWS dependency is an out-of-repo producer chain terminating in MQTT, so it is a *write*-path dependency, not a read surface. A read cutover neither fixes nor needs it |
| Kubernetes probes and `kubectl logs` | `hear-annotate.yaml:759`, `hear-heartbeat.yaml:83`, `check_fleet_health.py:883` | pod status is consumed as health by humans and by `check_fleet_health.py`; changing a readiness definition changes an operator answer |

### 3.5 Durable-record read contracts (designed, not deployed)

`deploy/postgres/migrations/0004_hear_durable_access_control.sql` already defines the read
boundary the canonical side will present:

* `hear.durable_records_operator` — state plus payload **with `gps.lat`, `gps.lon`,
  `gps.alt_m` removed** (`:95-102`); `SELECT` granted to `hear_durable_reader` only (`:152`).
* `hear.durable_records_audit` — custody metadata plus `payload_sha256_hex`, **never the
  payload body** (`:104-126`); granted to `hear_durable_auditor` (`:155`).
* `hear.health_snapshot(...)` — the O(1) aggregate health read, granted as a **function**,
  not as table access (`:153-156`).
* `hear.refused_messages_audit` and `hear.refusal_health_snapshot(...)` (`0006:384-401`) —
  refusals are readable as redacted audit rows, raw bodies are admin-only.
* `legacy_uid_collisions`, `identity_conflicts` (`0005:13-143`) — compatibility diagnostics,
  **not** a canonical record surface.

No current operator tool reads Postgres. That is a fact worth stating plainly: the canonical
read contract exists in DDL and the legacy read contract exists in Python, and nothing yet
connects them. Phase 6 is where that gap is either closed under a comparator or deferred.

## 4. Freshness and health semantics today

An operator answer about "is the fleet healthy" is currently assembled from five different
time models:

| Signal | Time model | Failure mode it hides |
|---|---|---|
| Redis key TTL (30 s) | expiry of a cache key | R3: a deduplicated heartbeat does not re-arm the TTL, so a healthy rebooting node reads as offline |
| `heartbeat.json` `last_success_s` vs `--max-stale-s` | wall clock, `time.time()` (`hear_drain.py:2530`) | a clock-untrusted node's own timestamps are not usable; drain compares its own wall clock instead |
| Node `/status` GPS fix class | per-GNSS-family thresholds (`fleet.py:66-77`, `:167-182`) | R14: `fix: 6` dead-reckoning reads as a fix unless the class rule is applied |
| `mic_state` | latched boot probe | R11: false `capture-failure` on healthy hardware; any gate trusting it refuses healthy nodes |
| CronJob exit code / pod readiness | scheduler state | a suspended or skipped tick looks like silence, not like a failure |

Two consequences for Phase 6. First, **the canonical side must not be compared against these
as if they were one clock**: a read comparison has to name the time model per field, exactly
as the write comparator does with `tier` for unanchored instants. Second, a read cutover that
preserves these semantics faithfully also preserves R3 and R11; the cutover is not the place
to fix them, and the comparator must classify a *known* legacy defect as an expected
difference with a recorded reason rather than as a canonical regression.

## 5. Privacy and redaction boundaries

| Boundary | State | Governance reference |
|---|---|---|
| `GET /api/audio/{clip_key}` returns raw clip WAV bytes with **no route-level authentication** | live; path-confined by `safe_join` but not authorized | `data-governance.md` §3 requires per-tenant, per-class, server-side authorization for `clip`/`raw_audio` |
| `GET /api/export` returns every human annotation, unbounded and unauthenticated | live | §3 (`labels` retained with reviewer identity), §9 (bulk-read alerting) |
| Node `/status` is unauthenticated and includes RSSI, identity, firmware, position state | live | R15; `SECURITY-REVIEW.md` findings 1–7 |
| `hear.durable_records_operator` strips GPS from the payload | designed | §2 edge/storage redaction, §3 least privilege |
| Reconcile receipts: deny-list-first redaction, `lat`/`lon`/`coord*`/`token`/`payload`/`audio`/… unquotable; raw bytes never enter a receipt | implemented (`hear/ingest/reconcile.py:193`, `:224`, `:253-275`) | §7 evidence integrity, §8 export manifests |
| `tools/coord_guard.py` makes a real-world coordinate merge-blocking in the tree | implemented, CI-gated (`.github/workflows/coord-guard.yml:17-62`) | §2 |

The asymmetry is the finding: the **canonical** side already has a redaction contract in
DDL and a receipt redaction profile in code, while the **legacy** side's most sensitive read
surfaces (clip audio, annotation export) have none. A read cutover that simply mirrors legacy
behaviour onto canonical reads would carry that gap forward under a "migrated" label. Phase 6
must therefore treat authorization on the annotate surfaces as a **prerequisite to widening
exposure**, not as a Phase 6 deliverable (it is a security-lane item; recording it here so the
cutover cannot silently inherit it).

## 6. Ownership

| Surface | Owning lane | Who must approve a change |
|---|---|---|
| Node `/status`, `/ls`, `/audio` | node/firmware | firmware maintainers; a mixed-version fleet (R13) means two shapes at once |
| Heartbeat receiver, Redis keys/TTLs, durable SQLite | ingest/durable lane | contract freeze §Redis keys; TTL is a frozen value |
| `/pool` products (`clips/index.jsonl`, `scores`, `tags.jsonl`, `tdoa/*`) | worker lanes (drain, score, tag, tdoa) | schema ids are frozen (`hear.sketch_score.v1`, `hear.clip_tag.v2`, `hear.tdoa_arrival.v1`, `hear.node_positions.v1`) |
| Annotate API + SQLite store | annotation/labelling lane | data-governance owner for any exposure change |
| Postgres views/roles | durable schema lane | migration + rollback pair required |
| CronJob schedules, probes, ConfigMap bundles | deployment lane | `deploy/k8s/gen_configmap.py` regeneration; ConfigMaps are written whole |
| Receipts and comparator contracts | contract lane | ADR + frozen bytes (`tools/freeze_contracts.py --check`) |

## 7. Reader-side migration risks

New rows, scoped to reads. Numbered `P6-R*` so they do not collide with the register's `R*`;
promote any of these into [migration-risk-register.md](migration-risk-register.md) when it
acquires live evidence.

| ID | Risk | Impact | Mitigation | Exit gate |
|---|---|---|---|---|
| P6-R1 | **No cursor anywhere.** `/api/queue` has `limit` only; `/api/export` is unbounded. A canonical reader with cursor pagination (`api-boundaries.md`) returns a *different shape*, so "same answer" is undefined for list reads | list comparisons are unclassifiable | define the compared projection as a **bounded, deterministically ordered page** with a declared sort key before any comparison runs | a read projection spec exists per list surface |
| P6-R2 | **Absence-as-value.** Redis TTL expiry and a missing `heartbeat.json` both mean "unhealthy" by absence. A canonical projection that returns an explicit `expired` row will mismatch structurally on every quiet node | permanent false mismatch, comparator gets muted | compare a *derived liveness verdict*, not the presence of a key; record the derivation on both sides | liveness projection defined and fixture-covered |
| P6-R3 | **Operator thresholds are inputs, not contracts.** `--max-stale-s`, clip deferred/lost thresholds and `--unfetched-window-s` change the verdict without changing the data | two sides can disagree because they were invoked differently | pin the threshold set into the run manifest and refuse a comparison whose two sides used different thresholds | run manifest carries the threshold set |
| P6-R4 | **Known legacy defects (R3, R11, R14) are baked into legacy answers** | canonical correctness reads as a mismatch | classify against a declared expected-difference list with a reason code per defect; never silently tolerate | expected-difference list reviewed and dated |
| P6-R5 | **Read amplification on the node HTTP client.** `hear_drain.py` and `fleet.py` already share one single-threaded node client; a shadow reader that polls `/status` adds a competing fetcher — the exact failure `standalone-migration.md` §2 forbids on the write side | node-side capture disruption | the read comparator **never** polls a node; it compares only already-landed records | comparator has no node-HTTP code path |
| P6-R6 | **Evidence-store growth.** Read comparisons produce far more pairs than write comparisons (every list row, every run) | receipt store growth and cost | sampling with a recorded denominator (receipt `sampling` block already carries it) and a per-run cap | sampling policy recorded; retention approved by data governance |
| P6-R7 | **Canonical side does not exist yet for most domains.** Postgres covers durable records only; scenes, clips, scores, TDoA and annotations have no canonical reader | a "shadow read" over a store nobody populates measures nothing | stage per domain, and treat an empty canonical side as `binding: legacy_orphan`, not as agreement | per-domain staging order agreed (§8.3) |
| P6-R8 | **PVC read-only/loss during a comparison** (R6/R7 are unmitigated) | comparison run aborts, or worse, reads a partially restored pool | comparator run manifest records source PVC generation and refuses to report a run whose source changed mid-window | run manifest source fields defined |

## 8. Shadow-read integration plan (no cutover)

### 8.1 Shape

```
legacy reader  ──serves operator──►  operator            (unchanged)
      │
      └──emits: projection + threshold set + observed_at ──┐
                                                           ├─► read comparator ─► receipts
canonical reader (off by default) ──emits: projection ─────┘        (serves nothing)
```

Three rules, each a direct consequence of the Phase 4 position:

1. **The comparator serves nothing.** No operator surface reads its output. If an operator
   answer can change because the comparator ran, it is a cutover.
2. **The comparator converts nothing.** It joins on identities recorded by the producing
   side, exactly as `reconcile.correlation_id()` does. It does not re-derive `event_id` from
   a legacy row, and it does not recompute a legacy verdict from canonical data — a
   comparator that reimplements the code it audits can agree with itself about the wrong
   answer.
3. **The comparator polls nothing live** (P6-R5). It reads landed records only.

### 8.2 Receipt compatibility

Record-scoped read comparisons reuse `hear.reconcile.receipt.v1` **unmodified**
(`hear/ingest/reconcile.py:44-51`). The receipt already carries everything a read comparison
needs: `correlation` (identities and binding), `sides.*.state_hash` over an explicit
projection (`:270`), `verdict` with a closed classification set, redacted `differences`
(`:193`, `:253-275`), `sampling`, `evidence` object keys, and a `comparator` block naming
the comparator build. A read comparator sets its own `comparator.name`/`build`, and
`disposition.queued` stays `false` — Phase 6 has no repair automation either.

Reused as-is, with the read meanings stated:

| Receipt element | Read meaning |
|---|---|
| `sides.<side>.state_hash` | hash of the **read projection** the surface returned for that record |
| `sides.<side>.observed_at` | when that side's reader observed it (not when the record was written) |
| `verdict.classification` | the same 15 members; `value_divergence` covers a differing field, `missing_canonical` a record the canonical reader cannot return, `pending` a canonical row inside `LIVE_GRACE_S`/`BACKFILL_GRACE_S` (`:153-161`) |
| `differences[].comparator` | `hash`/`exact`/`tier`/`count`/`numeric`, unchanged — unanchored instants stay **tier**-compared |
| `conservation()` (`:574`) | `compared == pending + match + Σ mismatches`, per run, unchanged |

**What does not fit, and must not be forced in.** Aggregate and list reads (`/api/queue`,
`check_fleet_health` tables, `health_snapshot()`) have no per-record identity pair, and
`reconcile.correlation_id()` raises rather than inventing one for a pair with neither
identity (`:287-313`). Synthesising a correlation key for an aggregate would defeat exactly
the guard that makes the write receipt trustworthy. Aggregate comparison therefore needs a
**separately allocated contract** (working name `hear.readcompare.receipt.v1`) with its own
ADR, generator and frozen fixtures under `contracts/` per ADR 0002 — not an edit to v1,
whose bytes are frozen (`tools/freeze_contracts.py --check`, `.github/workflows/ci.yml:96-115`).
Allocating it is Phase 6 work that begins only after the Phase 5 gate; naming it here is
scope control, not a decision.

### 8.3 Per-domain staging order

Ordered by whether a canonical side exists at all (P6-R7) and by blast radius:

| Stage | Domain | Canonical side | Compared projection | Blocked by |
|---|---|---|---|---|
| 1 | **Dur** durable records | `durable_records_operator` / `_audit`, `health_snapshot()` | record state, `payload_sha256_hex`, counters | Phase 2 gate (R1–R3 fixed and re-soaked), R16 |
| 2 | **H** health/liveness | canonical health projection with explicit expiry | derived liveness verdict per node + window counts (P6-R2) | stage 1; R3 recorded as expected difference |
| 3 | **D/Sc** detections and scene rows | canonical observations from the import | row counts per source/day/node, raw object hashes | Phase 3 import (R6: backed-up source) |
| 4 | **C** clips | object manifests | manifest identity + byte hash; **never** clip bytes in a receipt | Phase 3; clip access authorization |
| 5 | **S/T** scores and TDoA | replayed derived products | `numeric` tolerance **plus** model-card identity equality | stage 3; `hear.tdoa_model_card.v1` recorded per run |
| 6 | **A** annotations | canonical label store | label rows with reviewer identity handling per governance §3 | stage 4; annotate authorization |

Derived products (stages 5–6) are replayable and may be **regenerated rather than compared**
where the model card and inputs are identical; `standalone-migration.md` §"Historical data
conversion" already permits that, and a regenerated artifact with a recorded provenance is
cheaper evidence than a stored diff.

### 8.4 Run manifest

Every read-comparison run records, and refuses to be reported as evidence without:
window start/end; the **threshold set** used by both sides (P6-R3); the source PVC/database
generation (P6-R8); the sampling denominator; the comparator build; the expected-difference
list version (P6-R4); and the conservation result. A run whose two sides used different
thresholds, or whose source changed mid-window, is void — not a mismatch.

### 8.5 Rollback

There is nothing to roll back, and that is the design: the comparator writes receipts to its
own store and no operator surface reads it. Disabling it is deleting a scheduled job. This
property is what makes it safe to build the comparator before the gate that authorises the
cutover — and it is lost the moment a canonical read is wired into a real answer, which is
why that wiring is explicitly not in this document.

## 9. Gates

**Entry (all required before any Phase 6 implementation begins):**

1. Phase 2 — durable outbox on Postgres, R1/R2/R3 fixed and the 14-day window re-run from
   the fixed build (register §"Soak validity"); R16 discharged.
2. Phase 3 — object-store import with a test-restored `/pool` backup (R6).
3. Phase 4 — dual-write enabled and its exit gate met: zero unexplained mismatch over a
   rolling 7 days, conservation closing, coverage ≥ 99.9 %.
4. Phase 5 — the shadow-read comparator design accepted and its comparison run producing
   classified, conserved results.

**Phase 6 exit (from `standalone-migration.md` §4, restated with read-specific evidence):**
two clean backup/restore drills; one planned canonical outage with legacy fallback exercised;
no increase in node-side data loss; and for every result an operator can read, the active
release, source of truth and adapter are identifiable. Additionally, per this audit: a read
projection spec per surface (P6-R1), a dated expected-difference list (P6-R4), and an
approved receipt retention decision (P6-R6).

**Rollback trigger for a future read cutover** (configuration-first, unchanged from the
migration doc): shadow reads disagree beyond tolerance, or an operator answer changes
without a recorded release. Switch reads back to the legacy views; keep both writers running.

## 10. Open questions owed to an operator

Recorded, not decided:

1. Receipt retention for read comparisons (governance approval; §8.4 makes the volume
   materially larger than the write side).
2. Whether annotate `/api/audio` and `/api/export` are authorized **before** or **as part of**
   any read-surface work (§5). This audit's position: before, and independently.
3. Whether stages 5–6 compare or regenerate derived products (§8.3).
4. The p99 ingest-lag SLO still open from the ingest spec §16.1 — a read freshness SLO cannot
   be stated independently of it for health surfaces.
