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

Requires PostgreSQL 15 or newer; validated on 16.
