# The canonical Postgres schema for the durable heartbeat/event outbox

Status: **design + DDL, not deployed and not switched on.** `make_durable_store("postgres")`
still raises, and no manifest references a database. This document and
`deploy/postgres/migrations/` define what Phase 2 will apply; the decision to apply it belongs
to the soak gate, not to this change.

Inputs: the interface audit of the shipping SQLite store
(`DurableRecordStore` / `SqliteDurableRecordStore` in `tools/hear_heartbeat_receiver.py`), the
two writers that share it (`hear_heartbeat_receiver.py`, `hear_mqtt_bridge.py`), the frozen
Redis contract in `docs/phase0-freeze-contracts.v1.md`, and the retention/access rules in
`docs/data-governance.md`.

---

## 1. What the SQLite store does today, and what carries over

| Property today | Carried over | Why |
|---|---|---|
| Durable commit **before** Redis | yes | It is the whole point of the outbox. `hear.persist_record()` returns only after the record is committed. |
| First body wins on a duplicate | yes | `INSERT OR IGNORE` semantics; callers already depend on getting the stored record back. |
| `cached` suppresses a second publish | yes | The event stream append is the one non-idempotent Redis operation. |
| Attempts are an append-only log | yes | `hear.cache_attempts`, partitioned. |
| Pending = "no successful attempt" | **no** | Replaced by an explicit `state` column. Deriving liveness from an anti-join is what makes `/healthz` a table scan. |
| Identity is the bare `record_uid` | **no** | See §2. |
| Retention deletes rows | **no** | Replaced by partition drops. Same rule, cheaper mechanism. |

Three defects were confirmed against the shipping code. The schema closes the first; the second
is already mitigated in Redis by the event dedupe script; the third is receiver-side and is
named here so it is not lost.

* **D1 — cross-device identity collision.** `_record_uid()` returns the device's own
  `idempotency_key` unchanged, and `durable_records.record_uid` is `UNIQUE` over the whole
  ledger. Two devices that emit the same key collide: the second device's record is discarded
  and the caller is handed the *first device's* payload. Closed by §2.
* **D2 — duplicate publish under concurrency.** Mitigated on the Redis side by
  `_EVENT_DEDUPE_SCRIPT`. The schema adds the second half: a publish is a lease (§4), so N
  workers and `replicas > 1` are safe without relying on the cache to deduplicate.
* **D3 — a deduplicated heartbeat does not re-arm the liveness TTL.** `_write()` returns early
  on `stored.cached` and never touches Redis, so a node emitting byte-identical heartbeats can
  go "offline" while heartbeating. This is a change to `_write()`/`RedisHeartbeatCache`, not to
  the schema, and belongs to the store-implementation task. The schema supports the fix by
  returning `state` and `cached` separately, so the caller can always refresh state and only
  suppress the stream append.

---

## 2. Identity: `(tenant_id, device_id, record_uid)`

The dedupe key is a tuple, not a rewritten string, and it lives in its own table.

**Why not a unique constraint on the records table.** `hear.durable_records` is RANGE
partitioned on `received_at` so retention is a partition `DROP`. PostgreSQL requires a unique
constraint on a partitioned table to contain the partition key, so `UNIQUE (device_id,
record_uid, received_at)` would deduplicate *within a partition only*: a duplicate arriving
after midnight would insert a second row. That is a regression against SQLite, which
deduplicates over the whole ledger. So:

* `hear.durable_record_ids` — unpartitioned, `PRIMARY KEY (tenant_id, device_id, record_uid)`,
  one narrow row per record, pointing at `(record_id, received_at)`;
* `hear.durable_records` — partitioned, holds the body.

Both are written in one transaction by `hear.persist_record()`. The cost is one extra small
table (~100 B/record against the measured ~639 B/record); the benefit is that idempotency means
the same thing on both backends.

**The Python seam does not change.** `persist(record_uid, record, body_json)` already receives
the whole record, and `record["device_id"]` is validated before it gets there. The Postgres
store scopes the identity itself. That is what allows the receiver and the bridge to run on
different backends during the migration without a shared code change.

**Deduplication horizon = retention window.** An identity row is deleted when the partition
holding its record is dropped (§6). A duplicate that arrives after its record has aged out is
accepted as a new record and published again. The alternative — keeping identities forever —
turns the dedupe table into the unbounded growth the retention work just removed. At the
default of 30 days this is not reachable by any live device.

**Tenant.** `docs/data-governance.md` requires a tenant ID on every object with server-side
enforcement. `tenant_id` defaults to the session setting `hear.tenant_id`, falling back to
`'default'`, which is what the current single-site deployment is. Nothing changes operationally
today and the boundary already exists when a second site appears.

---

## 3. Record state machine

```
      validate_*_payload() ── reject ──▶ (no row, HTTP 400)
                │
                ▼
      hear.persist_record()
                │
   new ─────────┴───────── duplicate ──▶ returns the stored body + state
    │                                    (cached = state is 'published')
    ▼
 ┌──────────┐  claim_pending()   ┌──────────┐  mark_published()   ┌───────────┐
 │ pending  │ ─────────────────▶ │ claimed  │ ──────────────────▶ │ published │
 └──────────┘                    └──────────┘                     └───────────┘
      ▲                            │      │
      │  release_expired_claims()  │      │ mark_failed() under the cap
      └────────────────────────────┘      ▼
                                   ┌─────────────┐
                                   │ dead_letter │  at the attempt cap; retained, alerted,
                                   └─────────────┘  never auto-deleted, never auto-retried
```

Every transition is a function, so "published exactly once" is defined once rather than
re-implemented per writer. `published_at`, `dead_lettered_at` and the claim fields are tied to
`state` by CHECK constraints, so a row cannot be half-way through a transition.

---

## 4. Concurrency: leases, not hope

`hear.claim_pending(worker, limit, lease_s)` selects due pending rows
`ORDER BY next_attempt_at, record_id ... FOR UPDATE SKIP LOCKED` and stamps them `claimed` with
an expiry. Verified against PostgreSQL 16: two concurrent workers claiming from four pending
records receive disjoint sets and neither blocks.

* A worker killed mid-publish leaves an expired lease, not a stranded record;
  `hear.release_expired_claims()` returns it to the pending set on the next replay tick.
* Retry is exponential (`2^attempts` seconds, capped) and then terminal: at the attempt cap the
  record becomes `dead_letter` — retained and alertable, never dropped, never retried forever.
  Today a permanently failing record is retried on every restart and inflates `pending_records`
  for the life of the PVC.
* This removes the *database's* objection to `replicas > 1`. It does not remove the others:
  `hostPort: 5051` and `hostNetwork` still pin the receiver to one pod per node, and the
  `Recreate` strategy still exists because of the RWO PVC. Postgres alone does not unlock HA.

Connection handling belongs to the store implementation, not here: a pool
(`min 1 / max 8`), one transaction per `persist`, `READ COMMITTED`, and no connect-per-call.

---

## 5. Payload integrity

* `body_json text NOT NULL` is authoritative and is what gets republished. `payload` is a
  `GENERATED ... STORED` jsonb projection for querying only. jsonb normalises number formatting
  and key order; the receiver's bytes are `json.dumps(sort_keys=True, separators=(",",":"))`,
  and `docs/phase0-freeze-contracts.v1.md` freezes the Redis surface. Replaying a re-serialised
  jsonb value would be a silent contract change.
* `payload_sha256` is `CHECK (payload_sha256 = sha256(convert_to(body_json,'UTF8')))`: a body
  that does not hash to its own digest is not a row. `hear.verify_payload_integrity()` re-checks
  stored rows after a restore or a manual repair.
* `CHECK ((body_json::jsonb ->> 'device_id') = device_id)` and the same for `telemetry_path`: a
  record cannot be indexed or routed as something other than what it says it is.

---

## 6. Partitions and retention

**Daily partitions**, on `received_at` for records and `created_at` for attempts, created ahead
of time by `hear.ensure_partitions()` and backed by a `DEFAULT` partition so an unattended
maintenance failure degrades to "rows land in the default and an alert fires" rather than to an
ingest outage.

Daily, not monthly, because the retention window is 30 days: the measured rate is ~51,840
records/day for six nodes (~33 MB/day at ~639 B/record), so a monthly partition *is* the whole
window and retention could only be enforced by deleting rows — the vacuum churn partitioning
exists to avoid. Daily gives ~30 live partitions of ~33 MB and makes retention a `DROP`.

`hear.enforce_retention(days, dry_run)`:

* **defaults to 30 days**, the value already shipping as `HEAR_DURABLE_RETENTION_DAYS`;
* **refuses more than 90 days**: telemetry is class `R0-derived` in `docs/data-governance.md`,
  whose default maximum is 90 days. A longer window needs the written purpose and approver that
  document requires, and therefore a deliberate change here, not a parameter;
* **never drops a partition holding a record that is not `published`**, no matter how old. This
  is the SQLite prune's rule (`prune_acknowledged` only deletes rows with a successful attempt)
  and it is what stops a stalled Redis outage from being resolved by deleting the evidence;
* **is dry-run by default**, so the destructive form is always something someone typed;
* never drops the `DEFAULT` partition;
* deletes identity rows only once their record is gone, keeping the dedupe horizon and the
  retention window equal.

Export-before-drop is **not** implemented and is an operator decision (§10).

---

## 7. Health and metrics in O(1)

`health()` today runs three `COUNT(*)` scans plus an anti-join on every call, and `/healthz` is
the readiness probe at `periodSeconds: 10`. In Postgres:

* absolute counters (`cache_successes`, `cache_failures`, `records_persisted`,
  `records_duplicate`, `records_conflicting`, `records_backfilled`, `records_pruned`,
  `claims_expired`, `dead_lettered`) live in `hear.durable_counters`, maintained by triggers so
  a writer that bypasses the functions still cannot make the health surface lie;
* `pending_records`, `claimed_records`, `dead_letter_records` and `oldest_pending_age_s` are
  answered from partial indexes over the working set, not the table;
* `last_cache_failure_at` / `last_publish_at` come from `hear.durable_events`.

`hear.health_snapshot()` returns every key the SQLite `health()` returns except `path`, which
only the Python store knows and must return redacted — the DSN carries a password and must come
from a `Secret`, never a ConfigMap, and must never appear in `/healthz` or a log line.

Still owed by the store/manifest work, not by the schema: Prometheus metrics, alerts on
`pending_records`, `oldest_pending_age_s`, `dead_letter_records` and unrouted rows, a `/healthz`
for the MQTT bridge (which currently exposes nothing), and the rule that readiness may fail on
database loss while liveness must not, so a database outage degrades ingest instead of
restart-looping the pod.

---

## 8. Privacy and access control

Roles, as group roles with no logins, mapped onto `docs/data-governance.md`:

| Role | Can | Cannot |
|---|---|---|
| `hear_durable_writer` | persist, claim, publish, fail, read counters | **delete anything** |
| `hear_durable_reader` (operator) | `hear.durable_records_operator`, health | read raw bodies or other tenants |
| `hear_durable_auditor` | `hear.durable_records_audit`, attempts, counters | read payload bodies |
| `hear_durable_admin` | schema, partitions, retention | — |

Row-level security is enabled on every table with a `tenant_isolation` policy driven by the
`hear.tenant_id` session setting, so a caller who forgets a `WHERE` clause still cannot read
another tenant's rows. Views are `security_invoker`, so RLS applies through them.

`hear.durable_records_operator` strips `gps.lat`, `gps.lon` and `gps.alt_m`. Today's firmware
sends `gps: {"fix": n}` and no coordinates, so nothing is stripped yet. The redaction is there
because the repository already treats coordinates as a controlled class
(`tools/coord_guard.py`), and a redacted default is the version of that which survives a
firmware change nobody remembered to re-review.

Deletion is confined to the administrator: retention is the only sanctioned deletion path, and
a compromised or buggy writer cannot erase the ledger it writes to.

---

## 9. Migration, backfill, mixed deployment, rollback

Additive at every step. The SQLite ledgers are never written to by any of this.

| Step | Action | Reversal |
|---|---|---|
| M0 | **Gate**: live bridge soak enabled and green. | — |
| M1 | Land D3 (always refresh the liveness key) and the monotonic replay guard on SQLite; soak. | revert |
| M2 | Apply `0001`–`0005` to an empty database; schedule `ensure_partitions()`. No app change. | `rollback/0001…_down.sql` drops the schema |
| M3 | Implement `PostgresDurableRecordStore`; enable it on the **MQTT bridge only**, SQLite PVC still mounted. | env back to `sqlite` |
| M4 | Soak ≥ 72 h: Redis surface, pending, duplicates, liveness. | as M3 |
| M5 | Backfill both ledgers with `hear.backfill_record()`. | `DELETE … WHERE ingest_source='backfill_sqlite'` |
| M6 | Cut the LAN receiver over. **First-ever cross-writer dedupe**: records arriving on both the LAN and MQTT paths collapse to one row and one stream entry, so `xadd` volume drops. Verify that is duplicates falling, not coverage. | env back to `sqlite` |
| M7 | Keep both PVCs read-only ≥ 30 days, then archive. | PVC snapshot |
| M8 | Only then consider `replicas > 1` — after `hostPort`/`hostNetwork` are resolved. | scale to 1 |

**Mixed SQLite/Postgres is a supported state**, and M3–M6 are exactly that state. While it
lasts:

* the two backends have **different identity semantics**. A uid collision that Postgres keeps is
  still dropped by whichever writer is still on SQLite. Cross-writer dedupe does not exist until
  both are on Postgres, so `xadd` volume drops only at M6;
* `health()` reports its own backend's counts. Absolute counters restart from zero on the
  Postgres side; the dashboards compare rates, not totals, across the cut-over;
* Redis remains the single mixed-version compatibility surface and is byte-identical throughout;
* rollback at any point is one environment variable, and the SQLite PVC still holds everything
  it held, because nothing in this design ever writes to or deletes from it.

**Backfill invariant: it never publishes.** A row the SQLite ledger had already cached is
imported as `published` with the ledger's own acknowledgement time, so the drain worker does not
re-send month-old heartbeats to Redis and overwrite live state. A row SQLite never cached is
imported as `pending` and will be published exactly once. Backfilled rows are counted separately
(`records_backfilled`) and never inflate `cache_successes` — the metric the soak is watching.
The legacy uid is preserved in `legacy_record_uid`; scoping comes from the `device_id` column,
so no string rewriting is needed. `hear.legacy_uid_collisions` shows, before anything is
imported, how many records the unscoped ledger could not keep.

Rollback is per migration, in reverse, in `deploy/postgres/rollback/`. Only `0001`'s rollback is
destructive, and only while Postgres has never been authoritative for a workload. `0002`'s
rollback leaves the tables as inert storage, which is the correct state to be in while
`HEAR_DURABLE_STORE` is flipped back to `sqlite`.

---

## 10. Decisions this design does not make

Named, not silently defaulted. Each is a parameter or a one-line change, not a redesign.

1. **Database ownership** — a telemetry-dedicated instance, or the same one intended for the
   canonical evidence store. Affects backup policy and blast radius, nothing in the DDL.
2. **Export before partition drop.** Retention currently drops. Whether aged records must first
   be exported to object storage is a governance decision with no precedent in the repository.
3. **Dead-letter cap and backoff cap** (`hear.mark_failed` parameters, provisionally 50 attempts
   and 300 s). No project evidence sets these; the store will pass configured values.
4. **Claim lease duration** (provisionally 60 s). Must exceed the worst-case Redis publish
   timeout; that number is not measured yet.
5. **Retention beyond 30 days**, up to the 90-day `R0-derived` ceiling, and whether attempts
   should age out faster than records. The audit proposed 180/14; the code ships 30, so 30 is
   what this implements.
6. **`psycopg` version pin and its runtime `pip install --target /deps` egress.** Both pods
   install dependencies at start; adding a driver adds a start-time dependency and a new
   assertion in the manifest tests.
7. **`hostPort 5051` / `hostNetwork`** — the actual precondition for `replicas > 1`.

---

## 11. Files

| Path | What |
|---|---|
| `deploy/postgres/migrations/0001_hear_durable_baseline.sql` | schema, tables, partitioning, indexes, integrity constraints |
| `deploy/postgres/migrations/0002_hear_durable_claims_and_counters.sql` | persist/claim/publish/fail/release, counters, health, integrity verification |
| `deploy/postgres/migrations/0003_hear_durable_partitions_and_retention.sql` | partition maintenance and retention |
| `deploy/postgres/migrations/0004_hear_durable_access_control.sql` | roles, RLS, redacted views, grants |
| `deploy/postgres/migrations/0005_hear_durable_backfill_compat.sql` | SQLite import surface, watermarks, collision views |
| `deploy/postgres/rollback/*_down.sql` | one reversal per migration |
| `tools/hear_durable_pg.py` | reads/checks/plans the migrations; never connects |
| `tests/fixtures/durable_pg_semantics.sql` | executable assertions for the audited defects |
| `tests/test_hear_durable_pg_schema.py` | file-level guards (no server) + an opt-in real-server run |

Validated against PostgreSQL 16: all five migrations apply to an empty database, apply a second
time as no-ops, pass every assertion in the semantics fixture, and roll back to nothing.
Minimum supported version is 15 (`security_invoker` views).
