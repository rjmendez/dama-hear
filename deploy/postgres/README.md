# Postgres durable-outbox migrations

Nothing here is applied by the repository, by CI, or by any manifest.
`make_durable_store("postgres")` still raises: the schema exists so the cut-over is a decision
about evidence, not about writing DDL under time pressure. The design and the reasoning are in
[`docs/durable-postgres-schema.md`](../../docs/durable-postgres-schema.md).

## Layout

```
migrations/NNNN_name.sql        forward, additive, idempotent, applied in order
rollback/NNNN_name_down.sql     the reversal of exactly that file
```

Forward migrations never drop or rewrite an object at the top level, and every statement is safe
to run twice, so a partially applied file is resumed by re-running it. `tools/hear_durable_pg.py`
enforces both in CI, where no PostgreSQL server exists.

## Use

```bash
python3 tools/hear_durable_pg.py --list           # versions and checksums
python3 tools/hear_durable_pg.py --check          # ordering, additivity, rollback pairing
python3 tools/hear_durable_pg.py --plan           # the psql commands, printed, not run
python3 tools/hear_durable_pg.py --rollback-plan
```

Applying them, when that is authorized, is the printed plan and nothing more:

```bash
psql -v ON_ERROR_STOP=1 --single-transaction -f deploy/postgres/migrations/0001_hear_durable_baseline.sql
# ... then record the row that --plan prints for each file
```

After applying, schedule partition maintenance and run retention in dry-run first:

```sql
SELECT * FROM hear.ensure_partitions(3, 1);       -- idempotent; run on a timer
SELECT * FROM hear.enforce_retention(30, true);   -- dry run; false actually drops
```

## Against a throwaway database

`tests/test_hear_durable_pg_schema.py` applies every migration twice, runs
`tests/fixtures/durable_pg_semantics.sql`, and rolls the schema back, but only when pointed at a
scratch database:

```bash
HEAR_PG_TEST_DSN=postgresql://postgres@127.0.0.1:5432/hear_scratch python3 -m pytest tests/test_hear_durable_pg_schema.py
```

It drops the `hear` schema at the end. Do not point it at anything you care about.

## The ephemeral suite

`tests/test_hear_durable_pg_store_parity.py` is the behavioural half: it checks that the schema
behaves like the SQLite store the receiver ships today wherever the two must agree, and
deliberately differently only where the audit says it must. It does not need a scratch database
that someone prepared -- `tools/hear_durable_pg_testdb.py` creates one per test, applies the
migrations, and drops it afterwards. `HEAR_PG_TEST_DSN` therefore points at the *server*, and
only databases named `hear_test_<random>` are ever created, written to, or dropped.

```bash
# any throwaway server; a container is the usual one
docker run --rm -d --name pg -e POSTGRES_PASSWORD=scratch postgres:16
export HEAR_PG_TEST_DSN=postgresql://postgres:scratch@127.0.0.1:5432/postgres

python3 tools/hear_durable_pg_testdb.py --self-test    # apply, re-apply, exercise, roll back
python3 -m pytest tests/test_hear_durable_pg_store_parity.py -q
```

Without `HEAR_PG_TEST_DSN` the live tests skip and the file-level ones still run. CI runs the
whole thing against a `postgres:16` service container in the `durable outbox on an ephemeral
postgres` job, with `HEAR_PG_REQUIRE_LIVE=1` so a missing server fails the job instead of
skipping it green.

What it asserts, beyond the DDL-shape checks in `test_hear_durable_pg_schema.py`:

| | invariant |
| --- | --- |
| `PAR01` | the replayed bytes are the arrived bytes; `jsonb` is a projection, never the replay source |
| `PAR02` | identity is device-scoped: four devices sharing one uid keep four records and four bodies |
| `PAR03` | claim order is (due, arrival) order, which is what makes replay monotonic |
| `PAR04` | claim → publish is atomic, and a second publish is a reported no-op |
| `PAR05` | a lease expires on time and only then; an abandoned record is re-drainable, not retried-against |
| `PAR06` | attempts count up, backoff grows and is capped, the cap quarantines instead of dropping |
| `PAR07` | health is counter reads and partial-index reads: its cost tracks the backlog, not the ledger |
| `PAR08` | backfill imports already-cached history as published and never republishes it |
| `PAR09` | the default partition is never dropped, unpublished history is never dropped, dry run drops nothing |
| `PAR10` | the store seam is complete: every `DurableRecordStore` method has exactly one counterpart |

The concurrency rules (`SKIP LOCKED` disjoint claims, one publish under a race, a rolled-back
claim) are asserted from two simultaneous `psql` sessions, because a single connection cannot
demonstrate them.

Requires PostgreSQL 15 or newer; validated on 16.

## The cut-over rehearsal

The migrations are the destination; `tools/hear_durable_cutover_rehearsal.py` is the dress
rehearsal for getting there and back. It imports a *synthetic* legacy corpus, reconciles it,
injects the faults a reconciliation exists to catch, rolls back, and prints a receipt.

```bash
python3 tools/hear_durable_cutover_rehearsal.py --workdir build/rehearsal --out receipt.json
# with a throwaway server, the same gates run against the real functions:
python3 tools/hear_durable_cutover_rehearsal.py --workdir build/rehearsal \
    --dsn postgresql://postgres@127.0.0.1:5432/postgres
```

| gate | what it proves |
| --- | --- |
| `R01` | the migration set is ordered, additive, idempotent and reversible |
| `R02` | re-running a migration changes nothing (statement forms offline; catalog fingerprint live) |
| `R03` | a dry run imports nothing and modifies no source byte |
| `R04` | acknowledged rows import `published` with the ledger's own time, the rest import `pending` |
| `R05` | a second full import imports nothing and loses nothing |
| `R06` | an interrupted import, resumed, equals an uninterrupted one |
| `R07` | the reconciliation closes, with the row-conservation statement |
| `R08` | cross-device uid collisions are recovered and counted |
| `R09` | an injected missing row, extra row, one-byte body change and late publish are all detected |
| `R10` | rollback preserves the source; reverse backfill restores publish-equivalence |
| `R11` | legacy SQLite is still authoritative: no reader cut-over, no writable destination |

The pieces, usable on their own:

```bash
python3 tools/hear_durable_ledger_fixture.py --out build/corpus     # build, never export
python3 tools/hear_durable_backfill.py --source build/corpus/heartbeat-receiver.sqlite3
python3 tools/hear_durable_reconcile.py --source build/corpus/heartbeat-receiver.sqlite3 \
    --destination build/destination.json
```

Three refusals are wired into the tooling rather than documented as advice:

* the source is opened `mode=ro` with `PRAGMA query_only=ON`, and the reader *tries to write* and
  requires the failure before it reads anything;
* the destination must be a `hear_test_<random>` scratch database — the same pattern
  `tools/hear_durable_pg_testdb.py` creates and drops. Any other name is refused before a
  connection is opened, so this generation of the tool cannot become the live import by flag;
* the reverse backfill (plan §9.1) is the only path that writes SQLite: dry-run by default, and
  it refuses unless the caller states the writer is stopped *and* the ledger is actually
  unlocked.

`tests/test_hear_durable_cutover_rehearsal.py` runs all of it. Offline it uses `ModelSink`, an
in-memory model of `hear.backfill_record()`; with `HEAR_PG_TEST_DSN` set it runs the identical
corpus through the real function on an ephemeral database and requires the two snapshots to be
equal field for field, so the model cannot drift away from the schema it stands in for.

**One divergence found by rehearsing the plan:** §6 says a duplicate increments the watermark's
`rows_skipped`; `hear.backfill_record()` only writes `hear.backfill_watermarks` when it inserts,
so that column stays `0`. Nothing is lost — the duplicate is counted on the identity row as
`duplicate_arrivals` — but the reconciliation takes its skip count from the run report, not from
the database, and the M5 tool must do the same or it will compute a surplus against a correct
import.
