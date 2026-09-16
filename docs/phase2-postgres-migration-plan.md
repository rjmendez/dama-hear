# Phase 2 — SQLite durable outbox → canonical Postgres migration plan

Status: **plan only.** Nothing here provisions a database, deploys a store, changes the cluster,
backfills live data or cuts anything over. It is the implementation-ready sequence that the work
executes *after* its entry gates pass.

Companions, in the order they were produced:

* [durable-postgres-schema.md](durable-postgres-schema.md) + `deploy/postgres/` (PR #198, merged) —
  the canonical schema. **That document and its DDL are the source of truth for schema semantics.**
  Where this plan and the schema appear to disagree, the schema wins and this plan is wrong.
  This plan expands §9 of that document into executable stages; it adds no DDL.
* [migration-risk-register.md](migration-risk-register.md) — R1–R3, R7, R8, R10, R16 are the
  entry gates below.
* [bridge-durable-soak.md](bridge-durable-soak.md) — the soak evidence tool and pass criteria.
* [data-governance.md](data-governance.md) — retention class `R0-derived`, 90-day ceiling.
* [phase0-freeze-contracts.v1.md](phase0-freeze-contracts.v1.md) — the Redis surface that must be
  byte-identical on both sides of every stage.

---

## 1. What is being migrated, measured

Two writers share one Python seam (`DurableRecordStore`) and write to **two disjoint SQLite
ledgers on two 5 Gi RWO `local-path` PVCs**. Nothing reads either ledger except its own process.

Live read-only sample, `2026-09-15T15:12:59Z` (`sqlite3` opened `mode=ro`, `PRAGMA query_only=ON`;
no write verb was issued against the cluster):

| | `hear-mqtt-bridge` | `hear-heartbeat` receiver |
|---|---|---|
| Ledger | `/state/mqtt-bridge.sqlite3` (PVC `hear-mqtt-bridge-state`) | `/state/heartbeat-receiver.sqlite3` (PVC `hear-heartbeat-state`) |
| Records | 3,942 | 2 |
| Span | `2026-09-15T13:12:59Z` → `15:12:58Z` | `2026-09-15T06:07:28Z` → `06:09:18Z` |
| Paths | `hear/heartbeat` 3,820 · `hear/event` 105 (at the earlier sample) | `hear/heartbeat` 2 |
| Devices | ageev 730, gold 729, kasami 725, nyquist 646, mach 637, rankine 475 | nyquist 1, ageev 1 |
| `cache_attempts` | 3,971 succeeded, 0 failed | 2 succeeded, 0 failed |
| Records with >1 succeeded attempt | **29 (~0.74 %)** — R2/D2, live | 0 |
| `idempotency_key IS NULL` | 0 | 0 |
| `record_uid` claimed by >1 device | 0 observed (the collision is silent by construction — see R1) | 0 |
| Mean `payload_json` | 793 B (6.0 MB file incl. WAL/indexes ⇒ ≈1.5 KB/record on disk) | 760 B |
| `receiver_schema_version` / `durable_schema_version` | 1 / 1 | 1 / 1 |
| Deployment | `Recreate`, `replicas: 1`, `hostNetwork`, `HEAR_DURABLE_STORE=sqlite`, `HEAR_DURABLE_REPLAY_LIMIT=256` | same, plus `hostPort: 5051` |

Two facts shape everything below:

1. **The bridge carries the fleet; the receiver carries almost nothing.** The receiver's LAN path
   has had 2 records all day. So the bridge is the risky writer and the receiver is the cheap
   rehearsal — which is the reverse of the order in schema §9 M3/M6 if you read "bridge first" as
   "easiest first". It is still the right order, for a different reason: the bridge is where the
   evidence is, and a store that cannot hold the bridge cannot hold anything.
2. **All six devices send an `idempotency_key`, and it is the payload hash** (`record_uid ==
   idempotency_key`, e.g. gold's `f0f231f9…`). So the *current* cross-device collision probability
   is a SHA-256 collision, and the 0 observed above is expected. R1 is still real: nothing in the
   contract requires devices to keep doing this, and a reflash or key-scheme change makes
   collisions likely, with silent loss and an HTTP 204.

### 1.1 Column mapping, SQLite → Postgres

`payload_json` in SQLite is the same string the seam passes as `body_json`: the record
(`payload` + `received_at` + `receiver_schema_version`), serialized as
`json.dumps(sort_keys=True, separators=(",",":"))`. It is therefore **byte-identical to
`hear.durable_records.body_json`**, which is what makes hash reconciliation (§7) possible at all.

| SQLite | Postgres (`hear.`) | Note |
|---|---|---|
| `durable_records.id` | — (watermark only) | `AUTOINCREMENT`, monotone; the backfill resume key. Never imported as an identity. |
| `record_uid` | `durable_record_ids.record_uid` + `durable_records.legacy_record_uid` | identity becomes the tuple `(tenant_id, device_id, record_uid)`; the legacy value is preserved verbatim, never rewritten |
| `device_id` | `durable_records.device_id` | also the identity component that closes D1 |
| `telemetry_path` | `telemetry_path` | `CHECK` + body cross-check in the DDL |
| `idempotency_key` | `idempotency_key` | nullable, informational |
| `payload_json` | `body_json` (+ generated `payload` jsonb, + `payload_sha256`) | byte-identical; jsonb is query-only |
| `received_at` (RFC3339 `Z` text) | `received_at timestamptz` | partition key |
| `created_at` | — | equals `received_at` in the SQLite writer; carries no extra information |
| `receiver_schema_version` | `receiver_schema_version smallint` | 1 today |
| `durable_schema_version` | — | a property of the SQLite backend, not of the record |
| `cache_attempts(outcome='succeeded')` exists | `state='published'`, `published_at` = that attempt's `created_at`, `cache_target` = source ledger | the backfill invariant: **already-cached ⇒ imported published, never republished** |
| no successful attempt | `state='pending'` | will be published exactly once by the drain |
| `cache_attempts(outcome='failed')` | `durable_cache_attempts` rows | append-only log, partitioned |
| derived "pending" anti-join | explicit `state` + partial indexes | O(1) health |
| `prune_acknowledged()` row delete | `enforce_retention()` partition `DROP` | same rule (never drop unpublished), cheaper mechanism |

---

## 2. Stage model

The spine is schema §9's M0–M8. This plan keeps those identifiers so the two documents cannot
drift, and groups them into expand / migrate / verify / contract.

| Phase | Stage | Name | Touches production writes |
|---|---|---|---|
| **Expand** | M0 | Entry gates (semantic fix deployed, 14-day soak, register rows) | no |
| | M1 | Durable semantic fix (D1/D2/D3 + replay loop + monotonic guard) on SQLite | yes — SQLite only |
| | M2 | Provision + apply `0001`–`0005` to an empty database; partition maintenance | no |
| | M3a | `PostgresDurableRecordStore` implemented, flag-gated, **off** | no |
| **Migrate** | M3b | Bridge dual-write, `sqlite` primary (`HEAR_DURABLE_STORE=dual`) | yes |
| | M4 | ≥72 h dual-write soak, shadow compare running | yes |
| | M5 | Backfill both ledgers below the freeze watermark | no (never publishes) |
| | M5v | Full reconciliation: counts, per-record hashes, coverage | no |
| **Verify** | M6a | Bridge primary flips to `postgres`, SQLite demoted to secondary | yes |
| | M6b | Receiver cut over the same way (first cross-writer dedupe) | yes |
| | M6v | Cross-writer dedupe verification: `xadd` volume drop is duplicates, not coverage | no |
| **Contract** | M7a | Secondary SQLite write disabled; PVCs read-only ≥30 days | yes |
| | M7b | PVC snapshot archived; PVCs released | no |
| | M8 | `replicas > 1` considered — **only after `hostPort`/`hostNetwork` are resolved** | separate lane |

Each stage has an entry gate, an exit gate, and exactly one rollback action (§9). No stage
deletes or rewrites anything in SQLite until M7, so rollback is always "flip a flag back".

---

## 3. Feature flags and configuration

All new configuration is additive and defaults to today's behaviour. A pod that receives none of
it behaves exactly as it does now.

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `HEAR_DURABLE_STORE` | `none` \| `sqlite` \| `postgres` \| `dual` | `none` | `dual` is new. `postgres` currently raises; M3a makes it construct. |
| `HEAR_DURABLE_DUAL_PRIMARY` | `sqlite` \| `postgres` | `sqlite` | Which backend's commit gates the publish and whose `DurableEntry` is returned to `_write()`. The whole cut-over is this one value. |
| `HEAR_DURABLE_DUAL_SECONDARY_ERRORS` | `ignore` \| `count` \| `fail` | `count` | Secondary-write failure policy (§5). `fail` exists only for drills. |
| `HEAR_DURABLE_PG_DSN` | DSN | unset | **`Secret` only, never a ConfigMap.** Never logged, never in `/healthz`; `store.path` returns the DSN with password and query string stripped. |
| `HEAR_DURABLE_PG_TENANT` | text | `default` | Sets `hear.tenant_id` per session; drives RLS. |
| `HEAR_DURABLE_PG_POOL_MIN` / `_MAX` | int | `1` / `8` | Per schema §4. One transaction per `persist`, `READ COMMITTED`, no connect-per-call. |
| `HEAR_DURABLE_PG_CONNECT_TIMEOUT_S` | float | `5` | Bounded so a database outage degrades ingest instead of hanging the paho loop. |
| `HEAR_DURABLE_PG_STATEMENT_TIMEOUT_MS` | int | `3000` | Server-side, per session. |
| `HEAR_DURABLE_PG_CLAIM_LEASE_S` | int | *unset → operator decision O4* | Must exceed worst-case Redis publish latency; measure in M4 before setting. |
| `HEAR_DURABLE_PG_ATTEMPT_CAP` | int | *unset → O3* | Dead-letter cap. |
| `HEAR_DURABLE_PG_BACKOFF_CAP_S` | int | *unset → O3* | Backoff ceiling. |
| `HEAR_DURABLE_REPLAY_INTERVAL_S` | float | `5.0` (exists) | M1 makes replay loop until drained or budget, not one 256-row shot. |
| `HEAR_DURABLE_RETENTION_DAYS` | int | `30` (exists) | Postgres refuses >90 (governance ceiling). |
| `HEAR_DURABLE_SHADOW_COMPARE_S` | float | `0` (off) | Interval of the in-process shadow sampler (§8). |
| `HEAR_DURABLE_BACKFILL_FREEZE_ID` | int | unset | The SQLite `id` ceiling for backfill (§6). |

Rules:

* **Per workload, never fleet-wide.** The bridge and the receiver are flagged independently; mixed
  SQLite/Postgres across the two writers is a supported state and is exactly M3b–M6a.
* Flags live in the Deployment env, not in code defaults, so a rollback is `kubectl set env` +
  `Recreate` and is auditable in the object's revision history.
* A flag change is a restart. Both Deployments are `Recreate`/`replicas: 1`, so every flag change
  costs one ingest gap (§11).

---

## 4. Write ordering

The durability contract does not change: **the record is committed to the primary durable store
before Redis is touched, and the publish happens at most once.**

Single-backend (today, and after M7):

```
validate → persist(primary) → claim → publish(Redis) → note_cache_success/failure(primary)
```

Dual-write (M3b–M6b), with `P` = primary, `S` = secondary:

```
1. validate                      reject → no durable row (R4 is a separate lane; unchanged here)
2. persist(P)                    durable commit; returns (entry, cached/state)
3. persist(S)                    best-effort, same record_uid/body_json/received_at
                                 failure handled per HEAR_DURABLE_DUAL_SECONDARY_ERRORS
4. if P says already published → refresh liveness state only (D3), no stream append, stop
5. claim(P)                      Postgres: claim lease; SQLite: conditional-insert claim (M1)
   if not claimed               → another worker owns the publish, stop
6. publish(Redis)                write_state() always; append_stream() once
7. note_cache_success(P)         then note_cache_success(S), best-effort
   on failure: note_cache_failure(P) [+ (S)], re-raise → HTTP 503 / MQTT retry
```

Invariants, in priority order:

1. **Redis is never written before the primary commit.** The outbox exists for exactly this.
2. **The secondary never gates ingest** (unless `…SECONDARY_ERRORS=fail`, used only in drills).
3. **The publish decision has exactly one owner: the primary.** The secondary's `cached`/`state`
   is *recorded and compared*, never acted on. This is what stops dual-write from producing
   double publishes during the window where the two backends have different identity semantics.
4. **Replay drains the primary only.** Draining both would re-publish a record the other backend
   had already published. The secondary's pending set is a *comparison signal* (§8), not work.
5. **Ordering within a stage never changes.** The cut-over is a primary swap, which swaps step 2
   with step 3 atomically at process start — not a code path that can interleave.
6. **The Redis dedupe marker is scoped exactly like the durable identity.** The event stream's
   dedupe ZSET member is a single token, and today it is the bare `record_uid`. The moment
   Postgres stops merging two devices that share a uid, the *records* are distinct but that
   marker is not: the first device's event would suppress the second device's `XADD` and D1
   would reappear one layer out, invisible to every database-level test. The token the store
   passes to `RedisHeartbeatCache.write()` is therefore
   `tools/hear_durable_pg.py:redis_dedupe_token()` — `tenant|device|uid` — and both halves of
   that (the collision with the bare uid, the separation with the scoped token) are asserted in
   `tests/test_hear_durable_pg_store_parity.py`. This is a store-side change, not a schema one:
   the SQLite path keeps its current token until it is retired.

---

## 5. Dual-write error semantics

| Case | Primary | Secondary | Behaviour |
|---|---|---|---|
| Normal | ok | ok | publish per §4 |
| Secondary down (DB outage, PVC full) | ok | error | `count`: increment `durable_secondary_write_failures_total{backend}`, log once per 60 s with the record uid, **continue and publish**. `ignore`: same without the counter. `fail`: raise → 503/redeliver (drill only) |
| Primary down | error | n/a | raise before any Redis write → HTTP 503 / MQTT message not acked → device or broker redelivers. **No publish, no partial state.** This is today's behaviour and is preserved. |
| Secondary says duplicate, primary says new | ok(new) | ok(dup) | publish (primary decides). Counted as `durable_dual_divergence_total{kind="secondary_dup"}` — expected during M3b for records the secondary already held |
| Primary says duplicate, secondary says new | ok(dup) | ok(new) | no stream append; liveness refreshed. Counted `kind="primary_dup"`. Expected immediately after a backfill |
| Publish fails | `mark_failed` / failed attempt | best-effort same | record stays pending in both; replay retries the primary only |
| Publish succeeds, `note_cache_success(P)` fails | inconsistent | — | record remains pending → replayed → **one duplicate `xadd` possible**. Bounded and counted; this is the single at-least-once window in the design and it exists today. It is not made worse by dual-write |
| Secondary write succeeds after primary rollback | — | orphan row | secondary gets a row the primary never had. Detected by reconciliation as a one-sided record; harmless because the secondary is never drained |

Backpressure: a slow secondary must never become a slow ingest path. The Postgres store carries a
`CONNECT`/`STATEMENT` timeout (§3) and the secondary write is issued with the same budget; on
timeout it is a secondary failure, not a stall. The bridge's paho loop is single-threaded, so a
secondary write that blocks for seconds *is* an ingest outage — this is the single most likely way
this migration breaks production, and it is why `…SECONDARY_ERRORS=fail` is never used outside a
drill and why D-TO-1 (§13) fixes the timeout before M3b.

---

## 6. Backfill: watermark, checkpoints, partial failure

Tool: `tools/hear_durable_backfill.py` (to be written in M5; `deploy/postgres/migrations/0005`
already provides `hear.backfill_record()`, `hear.backfill_advance()`,
`hear.backfill_watermarks` and `hear.legacy_uid_collisions` — the plan adds **no DDL**).

Source access is read-only and enforced in code, as in `tools/bridge_soak_evidence.py`:
`sqlite3.connect("file:…?mode=ro", uri=True)` + `PRAGMA query_only=ON`. The backfill **cannot**
write, prune or vacuum the live ledger.

**Freeze watermark.** At M3b (dual-write start) capture `max(durable_records.id)` per ledger and
record it as `HEAR_DURABLE_BACKFILL_FREEZE_ID`. Backfill imports `id <= freeze_id` only; everything
above it is arriving live through dual-write. The two sets are disjoint by construction, and the
overlap region (records written between the snapshot read and the flag taking effect) is covered
by `backfill_record()` being idempotent on `(tenant_id, device_id, record_uid)`.

**Batching and checkpointing.**

* Order by `id`; batch size 1,000 (≈800 KB of bodies); one Postgres transaction per batch.
* After each committed batch call `hear.backfill_advance(source_ledger, last_id)`.
* Resume = read `backfill_watermarks.last_sqlite_id` and continue. The watermark is an
  optimisation; correctness comes from idempotency, so a lost watermark costs time, not truth.
* `acknowledged_at` = `MIN(cache_attempts.created_at WHERE outcome='succeeded')` for the uid →
  imported `published`. No successful attempt → imported `pending`.
* `--dry-run` (default) prints the plan: row count, byte estimate, published/pending split,
  `legacy_uid_collisions` output. The destructive form is always something someone typed.
* Rate limit: `--rows-per-second` default 2,000, so a full 30-day bridge ledger (~1.5 M rows)
  takes ~13 min and never competes with live ingest for the same connection pool. Run it with a
  **separate DSN/role**, not the writer's pool.

**Partial failure and retries.**

| Failure | Response |
|---|---|
| Transient PG error (deadlock, connection reset) | retry the batch with exponential backoff (0.5 s → 30 s, 6 attempts), then abort the run. The watermark still points at the last committed batch |
| One poison row (malformed JSON, `payload_sha256` CHECK failure, `device_id` mismatch with body) | batch is retried once row-by-row; the offending row is written to `backfill-rejects-<ledger>.jsonl` next to the run log with its uid and reason, counted, and **skipped**. A run with any reject exits non-zero |
| Backfill interrupted mid-batch | the transaction rolls back; re-run resumes from the watermark |
| Ledger row already present from the live path | `backfill_record()` returns `false`, `duplicate_arrivals` increments, `rows_skipped` increments. Expected and not an error |
| Run started twice concurrently | advisory lock on `hashtext(source_ledger)`; the second run refuses |

**What backfill must never do:** publish, write to SQLite, delete anything, or increment
`cache_successes`. Backfilled rows are counted in `records_backfilled` so they cannot contaminate
the soak metric.

---

## 7. Integrity, counts and hash reconciliation

Tool: `tools/hear_durable_reconcile.py` (M5v), read-only on both sides, exit 2 on any mismatch.

1. **Server-side integrity first.** `SELECT * FROM hear.verify_payload_integrity()` must return
   zero rows: every stored body hashes to its own `payload_sha256`. Run before and after backfill.
2. **Count reconciliation**, grouped by `(device_id, telemetry_path, date_trunc('day',
   received_at))`:
   * SQLite: `SELECT device_id, telemetry_path, substr(received_at,1,10), count(*) … WHERE id <= freeze_id`
   * Postgres: same grouping over `hear.durable_records` restricted to the source ledger's
     `cache_target`/`ingest_source='backfill_sqlite'` plus live rows for the overlap days.
   * Expected difference is exactly the number of rows `backfill_record()` reported as skipped
     duplicates. Any other delta fails.
3. **Hash reconciliation.** Per `(device, path, day)` bucket compute
   `sha256(concat(sha256(body_json) ordered by record_uid))` on both sides —
   `hashlib` on the SQLite side, `sha256(convert_to(body_json,'UTF8'))` on the Postgres side (which
   is already the stored `payload_sha256`, so Postgres does no re-hashing). Equal bucket digests
   prove byte-identical bodies, not merely equal counts. This is the check that catches a
   re-serialization bug, which is the failure mode that would silently change the Redis contract.
4. **Per-record spot check**: 1,000 uniformly sampled uids per ledger compared body-for-body,
   plus the 10 oldest and 10 newest.
5. **State reconciliation**: every SQLite row with a succeeded attempt must be `published` in
   Postgres; every row without one must be `pending`, `claimed` or `dead_letter`. A backfilled row
   in `published` with `published_at` **later than** its SQLite acknowledgement is a bug in the
   import and fails the run.
6. **Collision accounting**: `hear.legacy_uid_collisions` is captured before and after. Rows that
   appear only after the import are records the SQLite ledger *silently discarded* (R1/D1). They
   are reported as recovered data, not as errors — and their count is the first direct measurement
   of D1's real-world impact.
7. **Row conservation statement**: `sqlite_rows(≤freeze_id) + live_dual_rows − skipped_duplicates
   = postgres_rows`, printed as a single line, signed by the run's snapshot hashes. This is the
   artifact the M5v gate consumes.

---

## 8. Shadow read and compare

Two independent mechanisms, because one of them runs inside the writer and therefore cannot be
trusted to notice that the writer is wrong.

**In-process sampler** (`HEAR_DURABLE_SHADOW_COMPARE_S`, default off; 60 s during M4–M6): every
interval, take the last N=50 primary records and assert against the secondary that the record
exists, `body_json` is byte-identical, and the published/pending state agrees. Divergences
increment `durable_dual_divergence_total{kind}` and are logged once per kind per interval, with
the uid only — never the body.

**Out-of-process comparator** (a CronJob, every 15 min): read-only against both ledgers and the
Redis surface. It compares, over the last 15 minutes:

| Signal | Compared | Divergence means |
|---|---|---|
| record count per device | SQLite vs Postgres | one backend is dropping or the other is duplicating |
| `xadd` rate (`XLEN` delta on `dama:hear:events`) | vs published-record rate | D2 regression, or the cross-writer dedupe of M6 (expected, must be explained) |
| `dama:hear:{node}` TTL freshness | vs heartbeat arrival rate | D3 regression |
| `pending_records`, `oldest_pending_age_s` | both backends | a stalled drain |
| `health()` key set | both backends | a store returning a different health surface than the seam promises |

Rates, never absolute totals: the Postgres counters start at zero and the dashboards must compare
rates across the cut-over (schema §9). The comparator is read-only by the same `assert_read_only`
rule the soak tool already enforces.

**Shadow reads of the Redis contract**: at M4 and M6, capture 1,000 consecutive
`dama:hear:events` entries and diff them field-for-field against the pre-cut-over capture for the
same devices. The frozen contract (`phase0-freeze-contracts.v1.md`) must be byte-identical.

---

## 9. Rollback, per stage, with no data loss

The rollback argument in one sentence: **nothing in this plan ever writes to, prunes or deletes a
SQLite ledger, so until M7 the SQLite side is a complete, independent, still-live copy of the
truth, and rollback is a flag.**

| Stage | Rollback action | Time | Data loss | Residue |
|---|---|---|---|---|
| M1 | `kubectl rollout undo` / revert the ConfigMap to the previous provenance sha | ~1 min | none | records written under the fixed uid scheme remain; they de-duplicate as before |
| M2 | `deploy/postgres/rollback/0005…0001_down.sql` in reverse | minutes | none (database is empty and unreferenced) | none; `0001_down` is destructive only while Postgres has never been authoritative |
| M3a | none needed (code present, flag off) | — | none | — |
| M3b | `HEAR_DURABLE_STORE=sqlite` | one restart | none — SQLite has every record | Postgres holds rows nobody drains; they age out by retention or are truncated |
| M4 | as M3b | one restart | none | as M3b |
| M5 | `DELETE FROM hear.durable_records WHERE ingest_source='backfill_sqlite'` + matching identity rows, or drop the database | minutes | none — the source is untouched and read-only | none |
| M6a/M6b | `HEAR_DURABLE_DUAL_PRIMARY=sqlite` (stay dual), or `HEAR_DURABLE_STORE=sqlite` | one restart | **records published while Postgres was primary are already in Redis**, and dual-write means SQLite holds them too — provided the secondary write did not fail. If `durable_secondary_write_failures_total > 0`, run the reverse backfill (§9.1) before declaring rollback complete | rows in Postgres marked published that SQLite thinks are pending → SQLite replay would re-publish them. **Mitigation: after a reverse cut-over, seed SQLite's `cache_attempts` from Postgres `published_at` via the reverse backfill.** This is the one rollback that is not free |
| M7a | remount the PVCs read-write, `HEAR_DURABLE_STORE=dual` | one restart | none within retention | — |
| M7b | restore the PVC snapshot | hours | anything past the snapshot | — |
| M8 | scale to 1 | seconds | none | — |

### 9.1 Reverse backfill (Postgres → SQLite)

Exists so M6 rollback has the same "no data loss" property as M3b. `tools/hear_durable_backfill.py
--reverse` reads `hear.durable_records` rows above the SQLite ledger's last uid and inserts them
with `INSERT OR IGNORE`, plus a `cache_attempts(succeeded, created_at=published_at)` row for every
`published` record. It is idempotent, dry-run by default, and it is the only tool in this plan that
writes to SQLite — so it is **only ever run against a stopped writer**, guarded by a check that the
Deployment is scaled to 0 or the flag is already `postgres`.

It must be written in M3a, not in M6, and drilled in D4 (§12). A rollback path that is first
executed during the incident is not a rollback path.

### 9.2 Rollback triggers (pre-committed)

Any one of these, at any stage, rolls back without further discussion:

* row conservation (§7.7) does not close;
* `durable_secondary_write_failures_total` rate > 0.1 % over 15 min;
* duplicate `xadd` rate rises above the pre-stage baseline;
* any device's `dama:hear:{node}` liveness gap exceeds 90 s while the device is heartbeating;
* ingest p99 latency regresses >2× the pre-stage baseline, or the bridge's MQTT receive loop
  stalls (no record for 60 s with the broker reachable);
* `pending_records` grows monotonically for 15 min on the primary;
* any `verify_payload_integrity()` row;
* readiness flapping attributable to the durable store.

---

## 10. Lifecycle and retention

* Postgres retention: `hear.enforce_retention(days => 30, dry_run => …)`, daily, via CronJob;
  refuses >90 days (governance `R0-derived` ceiling); never drops a partition holding an
  unpublished record; never drops the `DEFAULT` partition; identity rows die with their record, so
  the dedupe horizon equals the retention window.
* `hear.ensure_partitions()` runs daily and pre-creates ≥7 days ahead. An unattended failure lands
  rows in the `DEFAULT` partition and **must alert** (`hear_durable_default_partition_rows > 0`) —
  degraded, not an outage.
* SQLite retention continues untouched throughout (`HEAR_DURABLE_RETENTION_DAYS=30`,
  `prune_acknowledged`). It is not disabled: a frozen ledger that fills its PVC is a new outage.
* At M7a both PVCs are remounted read-only and kept ≥30 days — one full retention window in which
  every Postgres record can still be checked against its origin. At M7b they are snapshotted
  off-PVC (which also discharges R7) and released.
* Export-before-drop remains **unresolved** (operator decision O2, schema §10.2). Until it is
  decided, retention drops. This plan does not silently pick a side.
* Dead letters are retained, alerted and never auto-deleted or auto-retried; draining one is a
  deliberate operator action with a recorded reason.

---

## 11. Traffic and maintenance impact

Measured baseline: ~52 k records/day across six nodes (~0.6 records/s steady, bursts to ~26/s
measured as the SQLite write ceiling), 105 events/day, ~1.5 KB/record on disk.

| Event | Impact | Mitigation |
|---|---|---|
| Any flag change | `Recreate` + `replicas: 1` ⇒ a 10–40 s ingest gap per workload | MQTT QoS redelivery covers the bridge; the LAN receiver's devices retry. Schedule in the daily low-traffic window; never during a calibration or capture session |
| Dual-write steady state | one extra network round trip per record; +1–3 ms p50 expected, must be measured in M4 | statement/connect timeouts (§3); secondary failures never block |
| Backfill | ~1.5 M rows at 2 k rows/s on a separate role/pool | rate limit, off-peak, dry-run first |
| M6 cut-over | `xadd` volume **drops** as cross-writer dedupe engages for the first time | verify it is duplicates falling, not coverage (§8) |
| Database maintenance (restart, failover, `VACUUM`) | ingest degrades to 503/redelivery while primary=postgres | readiness may fail on database loss; **liveness must not** — a database outage must not restart-loop the pod |
| Partition maintenance | none (pre-created) | `DEFAULT` partition alert |
| Node/PVC loss | unchanged from today until M7 | R7 snapshots |

Maintenance windows: every write-path stage transition is announced, has a named operator, a
pre-captured baseline snapshot, and a rollback command typed out in advance in the change record.

---

## 12. Observability and alerts

Metrics the store implementation must export (Prometheus, `/metrics` on both workloads — the
bridge has **no HTTP surface at all** today, so M3a includes giving it one with `/healthz` and
`/metrics`):

| Metric | Type | Alert |
|---|---|---|
| `durable_records_persisted_total{backend,path,device}` | counter | rate drop >50 % vs 1 h baseline → page |
| `durable_pending_records{backend}` | gauge | >500 for 10 m → page |
| `durable_oldest_pending_age_seconds{backend}` | gauge | >300 s → page |
| `durable_dead_letter_records` | gauge | >0 → ticket; >10 → page |
| `durable_publish_duplicates_total` | counter | any increase after M1 → page (D2 must be zero) |
| `durable_secondary_write_failures_total{backend}` | counter | >0.1 % of writes over 15 m → rollback trigger |
| `durable_dual_divergence_total{kind}` | counter | non-expected kinds → page |
| `durable_persist_seconds{backend}` | histogram | p99 >2× baseline → rollback trigger |
| `durable_default_partition_rows` | gauge | >0 → page (partition maintenance failed) |
| `durable_backfill_rows_total{ledger,outcome}` | counter | rejects >0 → run fails |
| `hear_node_liveness_gap_seconds{device}` | gauge | >90 s while heartbeating → page (D3 regression) |
| `durable_store_up{backend}` | gauge | 0 for 2 m → page |

Health surface rules: `health()` keeps the SQLite key set (superset allowed); `path` is the
**redacted** DSN; `/healthz` must answer in O(1) (counters + partial indexes, never `COUNT(*)`),
because it is the readiness probe at `periodSeconds: 10`. Readiness may fail on database loss;
liveness must not.

Dashboards compare **rates**, not totals, across the cut-over, because Postgres counters start at
zero. One dashboard row per stage transition, annotated with the change record.

---

## 13. Entry and exit gates

### Global entry gates (all must hold before M2 provisioning is even requested)

| Gate | Source | Evidence required |
|---|---|---|
| G1 — **durable semantic fix deployed** | R1, R2, R3, R16 | D1/D2/D3 regressions pass in CI, the fixed build runs on the live bridge, and the D2 duplicate-publish rate is **0** (baseline: 29/3,942 ≈ 0.74 % on 2026-09-15) |
| G2 — **14-day soak on the fixed build** | risk register "Soak validity" | `bridge_soak_evidence.py --milestone T+14d --require-pass` exits 0 against a T0 taken from the **first record written by the fixed build**. The current pre-gate burn-in does not count |
| G3 — six-device coverage continuous | R10 | rankine publishing for the whole window, or a written coverage exception |
| G4 — soak evidence is not destructible | R7 | dated off-PVC snapshots exist and one has been test-restored |
| G5 — manifest/live drift closed | R8 | PR #189 merged; live generation reconciles with `main` |
| G6 — operator decisions taken | schema §10 | O1–O7 below answered in writing |
| G7 — the plan's own tests exist | §14 | ephemeral-Postgres suite green in CI |

`D-TO-1` (a named precondition of M3b, not a global gate): the bridge's publish path is given a
bounded timeout so a slow secondary cannot stall the single-threaded paho loop.

### Per-stage gates

| Stage | Entry | Exit |
|---|---|---|
| M1 | G1 regressions written | fixed build live; duplicate publishes 0; replay drains without restart; monotonic guard proven by replaying a stale record |
| M2 | G1–G7 | `0001`–`0005` applied, re-applied as no-ops, rollback rehearsed on a scratch database; `ensure_partitions()` scheduled; roles/RLS verified; backup + PITR configured and one restore test passed |
| M3a | M2 exit | store passes the full seam suite against ephemeral Postgres; reverse backfill tool exists and is drilled (D4); bridge exposes `/healthz` + `/metrics`; DSN in a `Secret`; `psycopg` pin resolved (O6) |
| M3b | M3a exit + D-TO-1 + baseline snapshot captured | dual-write live for 1 h with zero secondary failures and p99 within 2× baseline |
| M4 | M3b exit | **≥72 h** dual-write: zero unexplained divergence, `pending` drains on both, Redis capture byte-identical, claim-lease value measured and set (O4) |
| M5 | M4 exit + freeze watermark recorded | backfill complete, exit 0, zero rejects; `legacy_uid_collisions` captured |
| M5v | M5 exit | row conservation closes; all bucket hashes equal; state reconciliation clean; `verify_payload_integrity()` empty |
| M6a | M5v exit + change record + operator present | bridge on `postgres` primary for 24 h; no rollback trigger fired |
| M6b | M6a exit | receiver on `postgres` primary; cross-writer dedupe observed; `xadd` drop explained as duplicates, with per-device coverage unchanged |
| M6v | M6b exit | 7 days on Postgres primary, dual-write still on, zero divergence |
| M7a | M6v exit + ≥30 days of Postgres retention accumulated | SQLite secondary disabled; PVCs read-only; alerts quiet for 7 days |
| M7b | M7a exit | snapshots archived and test-restored; PVCs released |
| M8 | `hostPort`/`hostNetwork` resolved (separate lane) | out of scope here |

### Operator decisions this plan does not make (carried from schema §10)

O1 database ownership (dedicated telemetry instance vs the canonical evidence instance) ·
O2 export before partition drop · O3 dead-letter cap and backoff cap · O4 claim lease duration
(measure in M4) · O5 retention beyond 30 days and separate attempt retention · O6 `psycopg` pin
and its runtime `pip install --target /deps` egress (interacts with the Phase 1.5 OCI lane) ·
O7 `hostPort 5051`/`hostNetwork`.

O1 and O6 block M2/M3a respectively. The rest have working defaults and are gated at G6 only so
they are chosen rather than inherited.

---

## 14. Test plan

All of it runs in CI except where marked. No test touches the live cluster.

**What exists now (M2's half of this plan, shipped ahead of the store).** The ephemeral-Postgres
harness and the schema/store parity suite are in the tree and green in CI:

* `tools/hear_durable_pg_testdb.py` creates a `hear_test_<random>` database per test on the
  server named by `HEAR_PG_TEST_DSN`, applies `0001`–`0005`, and drops it afterwards. It refuses
  to manage a database it did not create, and needs no driver — everything goes through `psql`;
* `tests/fixtures/durable_pg_store_parity.sql` (`PAR01`–`PAR10`) asserts byte-identical payload
  preservation, device-scoped identity, claim ordering, atomic claim → publish, lease expiry,
  backoff/dead-letter shape, O(1) health, backfill-never-republishes, and the partition and
  retention safeguards;
* `tests/test_hear_durable_pg_store_parity.py` runs that fixture, asserts the same invariants
  against the shipping SQLite store wherever the two must agree, proves `SKIP LOCKED` from two
  simultaneous sessions, proves the migration set re-applies as a no-op and rolls back
  completely, and holds the Redis-boundary requirements (heartbeat TTL re-arming, a device-scoped
  event dedupe token, replay in claim order);
* CI job `durable outbox on an ephemeral postgres` runs it against a `postgres:16` service
  container with `HEAR_PG_REQUIRE_LIVE=1`, so the suite cannot pass by skipping — gate G7.

**What exists now (the cut-over rehearsal, ahead of the store).** §6, §7 and §9.1 are rehearsed,
not merely written down — against a synthetic corpus, never against a pool ledger:

* `tools/hear_durable_ledger_fixture.py` builds the legacy corpus with the shipping
  `SqliteDurableRecordStore` (cached and uncached rows, a failed-only attempt, a mach-era body, a
  cross-ledger uid collision, poison rows, repeated refusals) — so no live data is exported to
  rehearse an import;
* `tools/hear_durable_backfill.py` is the M5 tool in rehearsal form: read-only source proven by a
  write probe, freeze watermark, batching, checkpoint/resume, poison quarantine with a non-zero
  exit, and `--reverse` (§9.1) guarded on a stopped, unlocked writer. Its destination must be a
  `hear_test_<random>` scratch database;
* `tools/hear_durable_reconcile.py` is M5v: per-`(device, path, day)` counts, bucket hash
  digests, state and acknowledgement-time checks, the row-conservation statement, exit 2 on any
  mismatch;
* `tools/hear_durable_cutover_rehearsal.py` runs gates `R01`–`R11` and prints the receipt §15
  asks for; `tests/test_hear_durable_cutover_rehearsal.py` runs the whole thing in CI, offline
  against a model of `hear.backfill_record()` and — with a server — against the real function,
  requiring the two to agree field for field.

Correction to the §6 table above, from that rehearsal: a duplicate arrival does **not** increment
`backfill_watermarks.rows_skipped`. `hear.backfill_record()` only writes that table when it
inserts; the duplicate is counted as `durable_record_ids.duplicate_arrivals` and by the run
report, which is where the reconciliation takes its skip count from.

What it deliberately does not contain: anything that needs the Postgres `DurableRecordStore`
itself (seam conformance parametrised over three backends, dual-write, reconciliation, reverse
backfill, the performance smoke). Those land with the store, and the list below is their spec.

**Unit / file-level (no server)** — extends `tests/test_hear_durable_pg_schema.py`:

* the migration set is ordered, idempotent, and every `NNNN_*.sql` has a matching `rollback/`;
* env-flag parsing: every combination of `HEAR_DURABLE_STORE` × `…DUAL_PRIMARY` resolves to the
  documented pair, and unknown values fail loudly at start, not at first record;
* `store.path` never contains a password or query string, for every DSN shape.

**Seam conformance (parametrised over `sqlite`, `postgres`, `dual`)** — one suite, three backends,
so the backends cannot diverge silently:

* persist → duplicate → `cached` semantics; `pending_records` ordering; `health()` key set;
  `note_cache_success/failure`; replay drains; prune/retention honours "never unpublished".

**Defect regressions (the reason for the whole phase):**

* D1a two devices, one key ⇒ 2 records, 2 publishes, each caller gets **its own** payload;
* D1b legacy uid preserved in `legacy_record_uid`; no string rewriting;
* D2a 8 concurrent identical records ⇒ exactly 1 `xadd`; D2b a failed publish is never
  double-published; D2c two claim workers get disjoint sets (`SKIP LOCKED`);
* D3a a deduplicated heartbeat re-arms `setex` and appends no stream entry;
  D3b a reboot loop emitting byte-identical payloads never shows offline;
* D4a replay of a stale record does not overwrite a newer cached record (monotonic guard).

**Dual-write:**

* secondary failure with `count` ⇒ publish still happens, counter increments, ingest unaffected;
* secondary failure with `fail` ⇒ 503 and no publish;
* primary failure ⇒ no Redis write at all (asserted against a fake Redis that records every call);
* primary-dup/secondary-new and the reverse both resolve to exactly one publish;
* a primary swap mid-suite (simulating the M6 restart) produces no duplicate publish.

**Backfill and reconciliation** (ephemeral Postgres + a fixture SQLite ledger built from real
shapes, including a mach-style old-firmware payload):

* import is idempotent across two full runs; watermark resume equals a single run;
* an already-cached row imports `published` with the ledger's ack time and **is never published**;
* an uncached row imports `pending` and is published exactly once by the drain;
* a poison row is quarantined, counted, and fails the run's exit code;
* cross-device uid collision in the source ⇒ both rows survive in Postgres and appear in
  `legacy_uid_collisions`;
* reconciliation detects an injected missing row, an injected extra row, and a one-byte body
  change (the hash check must catch what the count check cannot);
* reverse backfill restores a stopped SQLite ledger to publish-equivalence and refuses to run
  against a live writer.

**Performance smoke (CI, ephemeral):** 10 k persists ⇒ p99 recorded and asserted under a generous
ceiling, purely to catch an accidental connect-per-call or a missing index.

---

## 15. Drill plan

Drills are rehearsed on a scratch database and a copy of a ledger — **never on the live cluster** —
and each produces a dated receipt stored with the soak evidence. A drill that has not been run is
treated as a failed gate.

| # | Drill | Rehearses | Pass |
|---|---|---|---|
| D1 | **Postgres down during dual-write** (secondary role): stop the database for 10 min under synthetic load | §5 secondary-failure semantics | ingest never stops, publishes continue, counter rises, recovery needs no restart |
| D2 | **Postgres down while primary**: same outage with `DUAL_PRIMARY=postgres` | the degraded mode operators will actually meet | 503/redelivery, no partial Redis state, readiness fails, **liveness holds**, backlog drains on recovery with no duplicates |
| D3 | **Redis down 30 min** | the outbox's original purpose | every record durable, `pending` rises, drain empties it after recovery, exactly one `xadd` per record, liveness recovers |
| D4 | **Rollback from `postgres` primary**, including reverse backfill into a stopped SQLite writer | §9.1 | row conservation closes in both directions; no record published twice; total time recorded |
| D5 | **Backfill interrupted** (kill mid-batch, twice) then resumed | §6 checkpointing | final state identical to an uninterrupted run, byte-for-byte |
| D6 | **Claim-lease expiry**: kill a worker mid-publish | schema §4 leases | `release_expired_claims()` returns the record; it is published exactly once |
| D7 | **Dead-letter path**: force a permanently failing publish | attempt cap | record reaches `dead_letter`, alert fires, nothing is deleted, no infinite retry |
| D8 | **Retention refusal**: age a partition holding an unpublished record | schema §6 | `enforce_retention()` refuses to drop it and says why |
| D9 | **Restore drill**: restore the database from backup to a point before the backfill and re-run | O1/backup policy | restored state reconciles; PITR target achievable within the stated RTO |
| D10 | **Partition maintenance failure**: skip `ensure_partitions()` for a day | degradation, not outage | rows land in `DEFAULT`, alert fires, ingest continues, recovery re-attaches |
| D11 | **Secret rotation**: rotate the DSN password | operational hygiene | rotation with one restart, no credential in any log or `/healthz` |
| D12 | **PVC-full on the secondary** during dual-write | R7/capacity | secondary failures counted, ingest continues, no data loss on the primary |

---

## 16. What this plan explicitly does not do

Provision or size a database · write `PostgresDurableRecordStore` · change any manifest · apply
any DDL to any live system · backfill live data · cut anything over · start or restart the soak
clock · decide O1–O7 · resolve R4 (pre-parse refusal rows — a Phase 1 envelope lane that will add
its own additive migration) · resolve R6 (the Phase 3 import source backup) · unlock `replicas > 1`.
