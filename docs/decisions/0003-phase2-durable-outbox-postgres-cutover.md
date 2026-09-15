# 0003 - Phase 2 durable-outbox cut-over: dual-write, backfill and reversal

- Status: accepted
- Date: 2026-09-15
- Scope: how the durable heartbeat/event outbox moves from the two SQLite ledgers to the
  canonical Postgres schema. This record **does not** provision a database, deploy a store,
  change a manifest, apply DDL to any live system, backfill live data, or cut anything over.
- Builds on: [durable-postgres-schema.md](../durable-postgres-schema.md) and
  `deploy/postgres/` (PR #198, merged), which remain the source of truth for schema semantics.
- Companion runbook: [phase2-postgres-migration-plan.md](../phase2-postgres-migration-plan.md).

## Decision

1. **The cut-over is a dual-write migration, not a copy-and-switch.** A new
   `HEAR_DURABLE_STORE=dual` mode writes every record to both backends;
   `HEAR_DURABLE_DUAL_PRIMARY` selects which backend's commit gates the Redis publish. The entire
   cut-over, in both directions, is that one value.
2. **The primary is the sole owner of the publish decision.** The secondary's `cached`/`state` is
   recorded and compared, never acted on, and replay drains the primary only. During the window
   the two backends have different identity semantics, so acting on both would double-publish.
3. **The secondary never gates ingest.** A secondary write failure is counted and logged and the
   record is still published (`HEAR_DURABLE_DUAL_SECONDARY_ERRORS=count`). `fail` exists only for
   drills. The bridge's MQTT loop is single-threaded, so a blocking secondary is an ingest outage;
   the secondary write is issued under an explicit connect/statement timeout.
4. **Device scoping happens at the storage layer, not in the uid string.** `_record_uid()` keeps
   returning the wire key (or the payload hash); Postgres scopes identity as
   `(tenant_id, device_id, record_uid)` and SQLite gains a `(device_id, record_uid)` unique index.
   Rewriting the uid to `sha256(device_id‖key)` would change every in-flight record's identity at
   deploy time and produce a duplicate-publish burst for no additional correctness. The legacy
   value is preserved verbatim in `legacy_record_uid`; nothing is rewritten.
5. **Backfill never publishes and never writes to SQLite.** Rows the SQLite ledger had already
   cached import as `published` with the ledger's own acknowledgement time; rows it never cached
   import as `pending`. The source is opened read-only (`mode=ro`, `PRAGMA query_only=ON`), and
   that boundary is enforced in code, as in `tools/bridge_soak_evidence.py`.
6. **Backfill is bounded by a freeze watermark** captured at dual-write start
   (`max(durable_records.id)` per ledger) and checkpointed per 1,000-row batch through
   `hear.backfill_advance()`. The watermark is an optimisation; correctness comes from
   `hear.backfill_record()` being idempotent on the identity tuple.
7. **Correctness is proven by hashes, not by counts.** Reconciliation compares per
   `(device, path, day)` bucket digests over `sha256(body_json)` on both sides, plus a row
   conservation statement, plus server-side `verify_payload_integrity()`. SQLite `payload_json`
   and Postgres `body_json` are the same bytes, so a re-serialization bug — the failure mode that
   would silently change the frozen Redis contract — is detectable. A count check alone is not.
8. **Rollback is a flag until M7, because nothing writes to or deletes from SQLite before then.**
   The one exception is rollback *from* Postgres-primary, which needs a reverse backfill to seed
   SQLite's `cache_attempts` from `published_at` so replay does not re-publish. That tool is
   written at M3a and drilled at D4, not improvised during an incident.
9. **Rollback triggers are pre-committed**, not judged live: unclosed row conservation, secondary
   failure rate >0.1 %, any rise in duplicate `xadd`, a liveness gap >90 s on a heartbeating
   device, ingest p99 >2× baseline, monotonic `pending` growth, any integrity failure, or
   store-attributable readiness flapping.
10. **The gate for starting any of this is the fixed build, not the current soak.** The 14-day
    window must be measured from the first record written by the build that fixes D1/D2/D3, with
    six-device coverage, off-PVC evidence snapshots, and the live/manifest drift closed.

## Why

**Why dual-write rather than a cut-over with a backfill.** The two ledgers are disjoint and
nothing reads them, so a clean switch looks tempting. It fails on the reversal: once Postgres has
been authoritative for an hour, the SQLite ledger is missing an hour of records and rollback is
data loss. Dual-write keeps a complete, independent, still-live second copy for the whole window,
which is what turns "rollback" from a claim into a flag.

**Why the primary decides alone.** Cross-writer dedupe does not exist until both writers are on
Postgres. Until then a record can legitimately be new in one backend and a duplicate in the other,
and any rule that consults both produces either double publishes or dropped ones. One owner is the
only rule that holds in every intermediate state.

**Why storage-layer scoping (4) is not a compromise.** The audit proposed rewriting `_record_uid()`
to include the device. That does close D1 — and it also invalidates the identity of every record
currently in flight, so the first minutes after deploy would re-publish records the ledger already
holds under the old uid. The schema already scopes identity itself, precisely so the Python seam
does not change and the two writers can run different backends during the migration. Taking the
scoping at the storage layer gets the same correctness with no burst and no cross-backend seam
skew.

**Why hash reconciliation.** Both stores hold the same bytes today only because
`json.dumps(sort_keys=True, separators=(",",":"))` is used on both paths. Storing the jsonb
projection instead would normalise number formatting and key order, and the difference would be
invisible to a count check and fatal to a frozen wire contract. The bucket digest is the cheapest
assertion that catches it.

**Why the current soak cannot be the gate.** It is a real and useful burn-in, but the semantics it
certifies are the defective ones: the duplicate-publish rate is ~0.74 % (29 of 3,942 records at
2026-09-15T15:12Z) and structural, which already fails the declared soak criterion, and the uid
derivation is about to change. Evidence gathered under the old semantics cannot certify the new
ones.

## Consequences

* A new store backend, a new flag mode, two new tools (backfill/reverse-backfill, reconcile), one
  new comparator CronJob, and an HTTP surface for the bridge, which today exposes nothing.
* Every flag change is a restart, and both workloads are `Recreate`/`replicas: 1`, so each stage
  transition costs one short ingest gap covered by MQTT redelivery and device retry.
* Steady-state dual-write adds one round trip per record; the budget is p99 within 2× baseline and
  it is measured at M4, not assumed.
* `replicas > 1` remains blocked by `hostPort 5051`/`hostNetwork`, not by the database.
* Absolute counters restart at zero on the Postgres side; dashboards must compare rates across the
  cut-over.
* Open operator decisions O1–O7 (schema §10) stay open. O1 (database ownership) blocks provisioning
  and O6 (`psycopg` pin and its runtime install egress) blocks the store implementation; both are
  choices for an owner, not defaults this record is entitled to take.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Copy-and-switch with a maintenance window | Rollback after the switch is data loss; the window would have to span the backfill of a 30-day ledger |
| Drain both backends during dual-write | Guarantees double publishes; `xadd` is not idempotent |
| Rewrite `_record_uid()` to embed the device | Invalidates in-flight identities and re-publishes at deploy; the schema already scopes identity without a seam change |
| Keep dedupe identities forever | Reintroduces the unbounded growth retention exists to remove; the horizon is deliberately the retention window |
| Cut the receiver over first because it is nearly idle | The evidence is on the bridge. A store proven only against 2 records/day proves nothing |
| Treat the running soak as the gate | Certifies semantics that are being replaced, and already fails criterion S03 |
