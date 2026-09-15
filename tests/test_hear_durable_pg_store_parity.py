"""Store parity for the Phase 2 Postgres outbox: what has to be true before the store is written.

`tests/test_hear_durable_pg_schema.py` checks the migration *files* and the shape of the DDL.
This file checks behaviour, against a real server where one is available:

* an **ephemeral database** is created, migrated, exercised and dropped per test
  (tools/hear_durable_pg_testdb.py). Nothing else in the cluster is touched, and no test ever
  runs against a database this suite did not create;
* the invariants that decide whether a Postgres `DurableRecordStore` may replace the SQLite one
  are asserted on **both** backends wherever they must agree -- the SQLite half runs everywhere,
  so a parity claim is never just a description of Postgres;
* the seam that Redis sits behind (heartbeat TTL re-arming, event dedupe, monotonic replay) is
  asserted against the shipping `RedisHeartbeatCache` with a fake Redis, because that is the
  boundary a backend swap is most likely to break silently.

Without a server (`HEAR_PG_TEST_DSN` unset, or psql missing) the live class skips and everything
else still runs. CI provides the server as a service container; see .github/workflows/ci.yml.

Nothing here implements or enables the Postgres store: `make_durable_store("postgres")` must
still refuse, and the last test in this file asserts exactly that.
"""
import hashlib
import json
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import hear_durable_pg as PG  # noqa: E402
import hear_durable_pg_testdb as TESTDB  # noqa: E402
import hear_heartbeat_receiver as HR  # noqa: E402
from test_hear_heartbeat_receiver import FakeRedis  # noqa: E402

PARITY_FIXTURE = ROOT / "tests" / "fixtures" / "durable_pg_store_parity.sql"
SEMANTICS_FIXTURE = ROOT / "tests" / "fixtures" / "durable_pg_semantics.sql"
REDIS_TARGET = "redis://scratch:6379"


def lit(value: str) -> str:
    """A SQL string literal. Everything here is fixture data, never operator input."""
    return "'" + value.replace("'", "''") + "'"


def record_body(device_id: str, uid: str, telemetry_path: str = "hear/heartbeat", **extra) -> str:
    """A record body in exactly the form the receiver produces (sorted keys, compact)."""
    record = {"device_id": device_id, "telemetry_path": telemetry_path,
              "idempotency_key": uid, "received_at": HR.utc_now(),
              "receiver_schema_version": HR.RECEIVER_SCHEMA_VERSION}
    record.update(extra)
    return HR.encode_json(record)


# ------------------------------------------------------------------------------------------------
# The harness itself. A test database that could point at production is worse than no test.
# ------------------------------------------------------------------------------------------------
class TestEphemeralHarness:
    def test_only_generated_scratch_names_are_managed(self):
        assert TESTDB.SCRATCH_NAME_RE.match(TESTDB.scratch_name())
        for hostile in ("dama_hear", "postgres", "hear_test_", "hear_test_zz", ""):
            with pytest.raises(ValueError):
                TESTDB.EphemeralDatabase("postgresql://localhost/postgres", hostile)

    def test_two_scratch_names_do_not_collide(self):
        assert len({TESTDB.scratch_name() for _ in range(256)}) == 256

    def test_the_dsn_is_repointed_not_rebuilt(self):
        url = "postgresql://user:pw@db.example:5433/dama_hear?sslmode=require"
        moved = TESTDB.dsn_for_database(url, "hear_test_0123456789abcdef")
        assert moved.startswith("postgresql://user:pw@db.example:5433/hear_test_0123456789abcdef")
        assert "sslmode=require" in moved
        kv = "host=db.example port=5433 dbname=dama_hear user=hear"
        moved_kv = TESTDB.dsn_for_database(kv, "hear_test_0123456789abcdef")
        assert "dbname=dama_hear" not in moved_kv
        assert "dbname=hear_test_0123456789abcdef" in moved_kv
        assert "host=db.example" in moved_kv

    def test_a_dsn_never_reaches_a_message_with_its_password(self):
        assert TESTDB.redact("postgresql://user:hunter2@db.example/x") == \
            "postgresql://user:***@db.example/x"
        assert "hunter2" not in TESTDB.redact("host=db.example password=hunter2 user=hear")

    def test_availability_explains_itself_instead_of_failing(self):
        dsn, why = TESTDB.availability({})
        assert dsn is None
        assert TESTDB.DSN_ENV in why


# ------------------------------------------------------------------------------------------------
# The fixture, read as a file. These run with no server at all.
# ------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sql():
    return PARITY_FIXTURE.read_text(encoding="utf-8")


class TestParityFixtureIsSelfContained:
    def test_every_required_invariant_has_assertions(self, sql):
        required = {
            "PAR01": "byte-identical payload preservation",
            "PAR02": "device-scoped idempotency",
            "PAR03": "claim ordering / monotonic replay order",
            "PAR04": "atomic claim -> publish, published once",
            "PAR05": "claim expiry",
            "PAR06": "retry, backoff, dead letter",
            "PAR07": "O(1) health counters and partial indexes",
            "PAR08": "backfill imports published, never republishes",
            "PAR09": "partition and retention safeguards",
            "PAR10": "the store seam is complete",
        }
        for marker, what in required.items():
            raises = sql.count(f"RAISE EXCEPTION '{marker}:")
            assert raises >= 2, f"{marker} ({what}) has {raises} assertion(s)"

    def test_the_fixture_stops_on_the_first_violated_invariant(self, sql):
        assert "\\set ON_ERROR_STOP on" in sql

    def test_the_fixture_only_uses_objects_the_migrations_create(self, sql):
        import re
        created = set()
        for migration in PG.load_migrations():
            created.update(re.findall(
                r"CREATE (?:OR REPLACE )?(?:UNIQUE )?(?:TABLE|VIEW|FUNCTION|INDEX|SEQUENCE)"
                r"(?: IF NOT EXISTS)? hear\.(\w+)", migration.sql))
            # Partitions are created by ensure_partitions() at run time, not by DDL in a file.
            created.update(re.findall(r"CREATE TABLE hear\.%I", migration.sql))
        created.update({"durable_records_unrouted", "cache_attempts_unrouted"})
        # hear.tenant_id is the session GUC the RLS policy reads, not a schema object.
        used = set(re.findall(r"\bhear\.(\w+)", sql.replace("current_setting('hear.tenant_id'", "")))
        used.discard("tenant_id")
        unknown = {name for name in used - created if not name.startswith("durable_records_2")}
        assert unknown == set(), f"the fixture references objects no migration creates: {unknown}"

    def test_the_fixture_never_touches_anything_outside_its_own_database(self, sql):
        assert "dblink" not in sql and "postgres_fdw" not in sql
        assert "COPY " not in sql.upper().replace("COPY (", "")
        # The only DROPs a fixture may do are the ones enforce_retention() does to its own
        # aged partitions; it must never drop a schema, a role or a migration's object.
        assert "DROP SCHEMA" not in sql.upper()
        assert "DROP ROLE" not in sql.upper()

    def test_the_two_fixtures_do_not_restate_each_other(self, sql):
        semantics = SEMANTICS_FIXTURE.read_text(encoding="utf-8")
        assert "PAR01" not in semantics
        assert "durable_pg_store_parity: all assertions passed" in sql
        assert "durable_pg_semantics: all assertions passed" in semantics


# ------------------------------------------------------------------------------------------------
# The SQLite half of every parity claim. Runs everywhere, server or no server.
# ------------------------------------------------------------------------------------------------
class TestSqliteSideOfTheParity:
    @pytest.fixture
    def store(self, tmp_path):
        return HR.SqliteDurableRecordStore(str(tmp_path / "state" / "ledger.sqlite3"))

    def _persist(self, store, device_id, uid, **extra):
        body = record_body(device_id, uid, **extra)
        record = json.loads(body)
        return store.persist(uid, record, body), body

    def test_the_stored_bytes_are_the_arrived_bytes(self, store):
        entry, body = self._persist(store, "nyquist", "bytes-1", uptime_s=10)
        assert entry.body_json == body
        assert hashlib.sha256(entry.body_json.encode()).hexdigest() == \
            hashlib.sha256(body.encode()).hexdigest()
        # The duplicate is handed the stored bytes, not its own: the same rule the Postgres
        # persist_record() implements with (tenant_id, device_id, record_uid).
        again, other_body = self._persist(store, "nyquist", "bytes-1", uptime_s=99)
        assert other_body != body
        assert again.body_json == body

    def test_pending_records_drain_in_arrival_order(self, store):
        for i in range(5):
            self._persist(store, "ordering", f"ord-{i}", uptime_s=i)
        store.note_cache_success("ord-0", REDIS_TARGET)
        pending = store.pending_records(10)
        assert [entry.record_uid for entry in pending] == ["ord-1", "ord-2", "ord-3", "ord-4"]

    def test_health_reports_counts_and_the_last_failure(self, store):
        self._persist(store, "nyquist", "h-1")
        self._persist(store, "nyquist", "h-2")
        store.note_cache_success("h-1", REDIS_TARGET)
        store.note_cache_failure("h-2", REDIS_TARGET, RuntimeError("boom"))
        health = store.health()
        assert health["pending_records"] == 1
        assert health["cache_successes"] == 1 and health["cache_failures"] == 1
        assert health["last_cache_failure_at"] is not None

    def test_retention_never_removes_a_record_redis_has_not_accepted(self, store):
        self._persist(store, "nyquist", "kept")
        assert store.prune_acknowledged(0) == 0
        assert store.prune_acknowledged(30) == 0
        assert store.health()["pending_records"] == 1

    def test_the_defect_the_postgres_identity_model_removes(self, store):
        first, first_body = self._persist(store, "nyquist", "evt-1")
        second, second_body = self._persist(store, "mach", "evt-1")
        assert first_body != second_body
        # mach's record never lands and mach's caller is handed nyquist's payload.
        assert second.body_json == first_body
        assert store.health()["pending_records"] == 1
        assert PG.scoped_identity("nyquist", "evt-1") != PG.scoped_identity("mach", "evt-1")


# ------------------------------------------------------------------------------------------------
# The Redis boundary. A durable backend swap must not change what reaches Redis.
# ------------------------------------------------------------------------------------------------
class TestCacheBoundaryRequirements:
    def _cache(self, ttl_s=HR.HEARTBEAT_TTL_S):
        client = FakeRedis()
        return client, HR.RedisHeartbeatCache(client, heartbeat_ttl_s=ttl_s)

    def test_a_deduplicated_heartbeat_still_rearms_the_ttl(self):
        """D3: a device whose heartbeat deduplicates must not be allowed to look offline.

        `dama:hear:{node}` is a 30 s TTL key. Whatever the durable store decides about a repeat
        arrival, the liveness key has to be re-armed, so the store may never answer "duplicate"
        by skipping the cache write; it answers `cached` only for a record already *published*.
        """
        client, cache = self._cache(ttl_s=30)
        body = record_body("nyquist", "hb-1", uptime_s=1)
        cache.write(json.loads(body), body, "hb-1")
        assert client.ttls["dama:hear:nyquist"] == 30
        client.ttls["dama:hear:nyquist"] = 3     # as if 27 s had passed
        cache.write(json.loads(body), body, "hb-1")
        assert client.ttls["dama:hear:nyquist"] == 30
        assert client.values["dama:hear:latest"] == body

    def test_the_event_dedupe_token_has_to_carry_the_device(self):
        """D1 at the Redis boundary: two devices, one uid, one ZSET member.

        Postgres stops merging these two records. If the dedupe marker stays the bare uid, the
        first device's event suppresses the second device's XADD and the defect simply moves one
        layer out. The scoped token is what keeps the fix from stopping at the database.
        """
        client, cache = self._cache()
        first = record_body("nyquist", "evt-1", telemetry_path="hear/event")
        second = record_body("mach", "evt-1", telemetry_path="hear/event")

        cache.write(json.loads(first), first, "evt-1")
        cache.write(json.loads(second), second, "evt-1")
        stream = client.streams[cache.event_stream_key]
        assert [entry["device_id"] for entry in stream] == ["nyquist"], (
            "the bare uid is expected to suppress the second device; that is the defect")

        client, cache = self._cache()
        cache.write(json.loads(first), first, PG.redis_dedupe_token("nyquist", "evt-1"))
        cache.write(json.loads(second), second, PG.redis_dedupe_token("mach", "evt-1"))
        stream = client.streams[cache.event_stream_key]
        assert [entry["device_id"] for entry in stream] == ["nyquist", "mach"]

    def test_a_replayed_event_is_still_appended_once(self):
        client, cache = self._cache()
        body = record_body("nyquist", "evt-2", telemetry_path="hear/event")
        token = PG.redis_dedupe_token("nyquist", "evt-2")
        for _ in range(4):
            cache.write(json.loads(body), body, token)
        assert len(client.streams[cache.event_stream_key]) == 1

    def test_replaying_in_claim_order_leaves_the_newest_heartbeat_cached(self):
        """D4: the monotonic guard is claim order.

        `dama:hear:latest` is last-write-wins. The drain therefore has to replay a batch in the
        order the claim returned it -- (next_attempt_at, record_id), i.e. arrival order -- or a
        backlog flush can leave a stale body in the live key.
        """
        client, cache = self._cache()
        bodies = [record_body("nyquist", f"hb-{i}", uptime_s=i) for i in range(4)]
        for body in bodies:                      # claim order: oldest first
            cache.write(json.loads(body), body, f"hb-{body}")
        assert client.values["dama:hear:latest"] == bodies[-1]
        assert json.loads(client.values["dama:hear:nyquist"])["uptime_s"] == 3

    def test_the_scoped_token_is_stable_and_refuses_ambiguity(self):
        assert PG.redis_dedupe_token("nyquist", "evt-1") == "default|nyquist|evt-1"
        assert PG.redis_dedupe_token("nyquist", "evt-1") != PG.redis_dedupe_token("mach", "evt-1")
        assert PG.redis_dedupe_token("nyquist", "evt-1", "tenant-b") != \
            PG.redis_dedupe_token("nyquist", "evt-1")
        with pytest.raises(ValueError):
            PG.redis_dedupe_token("", "evt-1")
        with pytest.raises(ValueError):
            PG.redis_dedupe_token("nyq|uist", "evt-1")


# ------------------------------------------------------------------------------------------------
# Live, against a throwaway database.
# ------------------------------------------------------------------------------------------------
_ADMIN_DSN, _WHY = TESTDB.availability()


def test_the_live_suite_is_not_quietly_skipping_where_it_is_required():
    """CI sets HEAR_PG_REQUIRE_LIVE: a skipped suite must not pass for a missing server."""
    if os.environ.get("HEAR_PG_REQUIRE_LIVE", "").strip().lower() in ("", "0", "false", "no"):
        pytest.skip("live Postgres is optional outside the durable-postgres CI job")
    assert _ADMIN_DSN is not None, _WHY


@pytest.fixture(scope="module")
def migrations():
    return PG.load_migrations()


@pytest.mark.skipif(_ADMIN_DSN is None, reason=_WHY)
class TestAgainstAnEphemeralDatabase:
    @pytest.fixture
    def db(self, migrations):
        with TESTDB.ephemeral_database(_ADMIN_DSN) as database:
            database.apply_migrations(migrations)
            database.sql("SELECT count(*) FROM hear.ensure_partitions(2, 2)")
            yield database

    # -- migrations -------------------------------------------------------------------------
    def test_migrations_apply_reapply_as_a_noop_and_roll_back_completely(self, migrations):
        with TESTDB.ephemeral_database(_ADMIN_DSN) as database:
            database.apply_migrations(migrations)
            assert database.applied_versions() == [m.version for m in migrations]
            first = database.schema_fingerprint()

            database.apply_migrations(migrations)      # the resume case: run the whole file again
            assert database.schema_fingerprint() == first, "re-applying changed the schema"
            assert database.applied_versions() == [m.version for m in migrations]

            database.rollback_migrations(migrations)
            assert database.value(
                "SELECT count(*) FROM pg_namespace WHERE nspname = 'hear'") == "0"

            database.apply_migrations(migrations)      # and the schema comes back identical
            assert database.schema_fingerprint() == first

    def test_the_recorded_checksum_is_the_file_that_was_applied(self, db, migrations):
        for migration in migrations:
            recorded = db.value("SELECT checksum FROM hear.schema_migrations "
                                f"WHERE version = {migration.version}")
            assert recorded == migration.checksum
            assert recorded == PG.sha256_text(migration.path.read_text(encoding="utf-8"))

    def test_rolling_back_one_migration_leaves_the_ones_below_it_working(self, db, migrations):
        last = migrations[-1]
        db.run_file(last.rollback_path, single_transaction=True)
        db.sql(f"DELETE FROM hear.schema_migrations WHERE version = {last.version}")
        # The outbox still works without the backfill surface on top of it.
        db.sql("SELECT * FROM hear.persist_record('nyquist', 'after-rollback', "
               + lit(record_body("nyquist", "after-rollback"))
               + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")
        assert db.value("SELECT count(*) FROM hear.durable_records") == "1"
        db.run_file(last.path, single_transaction=True)
        assert db.value("SELECT count(*) FROM hear.backfill_watermarks") == "0"

    # -- the fixtures -----------------------------------------------------------------------
    def test_the_parity_fixture_passes(self, db):
        out = db.run_file(PARITY_FIXTURE)
        assert "durable_pg_store_parity: all assertions passed" in out

    def test_the_semantics_fixture_still_passes_on_the_same_schema(self, db):
        out = db.run_file(SEMANTICS_FIXTURE)
        assert "durable_pg_semantics: all assertions passed" in out

    def test_the_fixture_fails_when_the_invariant_it_guards_is_removed(self, db):
        """A green check that cannot go red proves nothing: take the index away and watch."""
        db.sql("DROP INDEX hear.durable_records_pending_due")
        with pytest.raises(TESTDB.PsqlError) as excinfo:
            db.run_file(PARITY_FIXTURE)
        assert "PAR07" in str(excinfo.value)

    # -- concurrency ------------------------------------------------------------------------
    def test_two_workers_claim_disjoint_sets(self, db):
        """D2c: SKIP LOCKED, proven from two connections, which is the only place it exists."""
        for i in range(6):
            db.sql(f"SELECT * FROM hear.persist_record('nyquist', 'c-{i}', "
                   + lit(record_body("nyquist", f"c-{i}", uptime_s=i))
                   + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")

        with db.session("worker-a") as a, db.session("worker-b") as b:
            a.execute("BEGIN")
            first = a.value("SELECT string_agg(record_uid, ',' ORDER BY record_uid) "
                            "FROM hear.claim_pending('worker-a', 4, 60)")
            # b runs while a's transaction is still open: the locked rows are skipped, not waited
            # on, so a slow worker can never stall the drain.
            b.execute("BEGIN")
            second = b.value("SELECT string_agg(record_uid, ',' ORDER BY record_uid) "
                             "FROM hear.claim_pending('worker-b', 4, 60)")
            a.execute("COMMIT")
            b.execute("COMMIT")

        claimed_a = set(first.split(",")) if first else set()
        claimed_b = set(second.split(",")) if second else set()
        assert len(claimed_a) == 4
        assert claimed_a & claimed_b == set(), "two workers claimed the same record"
        assert claimed_a | claimed_b == {f"c-{i}" for i in range(6)}
        assert db.value("SELECT count(*) FROM hear.durable_records WHERE state = 'claimed'") == "6"

    def test_a_record_is_published_once_even_when_two_workers_race_for_it(self, db):
        db.sql("SELECT * FROM hear.persist_record('nyquist', 'race-1', "
               + lit(record_body("nyquist", "race-1"))
               + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")
        rid = db.value("SELECT record_id FROM hear.durable_records WHERE record_uid = 'race-1'")
        rat = db.value("SELECT received_at FROM hear.durable_records WHERE record_uid = 'race-1'")
        publish = (f"SELECT hear.mark_published({rid}, {lit(rat)}::timestamptz, {lit(REDIS_TARGET)})")

        with db.session("worker-a") as a, db.session("worker-b") as b:
            a.execute("BEGIN")
            assert a.value(publish) == "t"
            b.execute("BEGIN")
            b.execute("SET LOCAL lock_timeout = '5s'")
            a.execute("COMMIT")
            assert b.value(publish) == "f", "the second publish must be refused, not duplicated"
            b.execute("COMMIT")

        assert db.value("SELECT count(*) FROM hear.cache_attempts "
                        "WHERE record_uid = 'race-1' AND outcome = 'succeeded'") == "1"

    def test_an_abandoned_claim_is_neither_lost_nor_double_published(self, db):
        db.sql("SELECT * FROM hear.persist_record('nyquist', 'abandon-1', "
               + lit(record_body("nyquist", "abandon-1"))
               + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")

        with db.session("worker-a") as a:
            a.execute("BEGIN")
            assert a.value("SELECT count(*) FROM hear.claim_pending('worker-a', 10, 1)") == "1"
            a.execute("ROLLBACK")        # the worker died before it committed the claim

        # A rolled-back claim never happened: the record is pending and immediately drainable.
        assert db.value("SELECT state FROM hear.durable_records "
                        "WHERE record_uid = 'abandon-1'") == "pending"
        assert db.value("SELECT count(*) FROM hear.claim_pending('worker-b', 10, 1)") == "1"

        # This one committed its claim and then died: the lease, not the connection, releases it.
        time.sleep(1.5)
        assert db.value("SELECT hear.release_expired_claims()") == "1"
        assert db.value("SELECT state FROM hear.durable_records "
                        "WHERE record_uid = 'abandon-1'") == "pending"
        assert db.value("SELECT (hear.health_snapshot() ->> 'claims_expired')") == "1"

    # -- parity with the shipping SQLite store ----------------------------------------------
    def test_the_two_backends_store_the_same_bytes(self, db, tmp_path):
        """The one thing a cut-over may not change: the bytes that reach Redis."""
        sqlite_store = HR.SqliteDurableRecordStore(str(tmp_path / "state" / "parity.sqlite3"))
        body = HR.encode_json({
            "device_id": "nyquist", "telemetry_path": "hear/heartbeat",
            "idempotency_key": "parity-1", "received_at": HR.utc_now(),
            "receiver_schema_version": HR.RECEIVER_SCHEMA_VERSION,
            # The shapes json.dumps and jsonb disagree about.
            "note": "caf\u00e9 \u2014 \"quoted\"", "ratio": 1.5, "seq": 9007199254740993,
            "gps": {"fix": 3}, "flags": [True, False, None],
        })
        entry = sqlite_store.persist("parity-1", json.loads(body), body)
        assert entry.body_json == body

        returned = db.value(
            "SELECT body_json FROM hear.persist_record('nyquist', 'parity-1', "
            + lit(body) + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")
        assert returned == body
        assert db.value("SELECT body_json = " + lit(body)
                        + " FROM hear.durable_records WHERE record_uid = 'parity-1'") == "t"
        assert db.value("SELECT encode(payload_sha256, 'hex') FROM hear.durable_records "
                        "WHERE record_uid = 'parity-1'") == \
            hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_the_two_backends_report_the_same_health_for_the_same_traffic(self, db, tmp_path):
        sqlite_store = HR.SqliteDurableRecordStore(str(tmp_path / "state" / "health.sqlite3"))
        for uid in ("h-1", "h-2", "h-3"):
            body = record_body("nyquist", uid)
            sqlite_store.persist(uid, json.loads(body), body)
            db.sql(f"SELECT * FROM hear.persist_record('nyquist', '{uid}', " + lit(body)
                   + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")

        sqlite_store.note_cache_success("h-1", REDIS_TARGET)
        sqlite_store.note_cache_failure("h-2", REDIS_TARGET, RuntimeError("refused"))
        rid = db.value("SELECT record_id FROM hear.durable_records WHERE record_uid = 'h-1'")
        rat = db.value("SELECT received_at FROM hear.durable_records WHERE record_uid = 'h-1'")
        db.sql(f"SELECT hear.mark_published({rid}, {lit(rat)}::timestamptz, {lit(REDIS_TARGET)})")
        rid = db.value("SELECT record_id FROM hear.durable_records WHERE record_uid = 'h-2'")
        rat = db.value("SELECT received_at FROM hear.durable_records WHERE record_uid = 'h-2'")
        db.sql(f"SELECT hear.mark_failed({rid}, {lit(rat)}::timestamptz, {lit(REDIS_TARGET)}, "
               "'refused', 50, 300)")

        sqlite_health = sqlite_store.health()
        pg_health = json.loads(db.value("SELECT hear.health_snapshot()::text"))

        for key, value in sqlite_health.items():
            if key == "path":
                assert key not in pg_health      # only the Python store knows (and redacts) it
                continue
            if key == "backend":
                assert pg_health[key] == "postgres"
                continue
            if key == "last_cache_failure_at":
                assert (value is None) == (pg_health[key] is None)
                continue
            assert pg_health[key] == value, key
        assert pg_health["pending_records"] == 2
        assert pg_health["cache_successes"] == 1 and pg_health["cache_failures"] == 1

    @staticmethod
    def _bulk_published(db, count, offset=0):
        """`count` published records, inserted the way a month of traffic would leave them."""
        db.sql(f"""
            INSERT INTO hear.durable_records
                (tenant_id, device_id, record_uid, ingest_source, telemetry_path, body_json,
                 payload_sha256, received_at, receiver_schema_version, state, published_at)
            SELECT 'default', 'bulk', 'bulk-' || i, 'lan_http', 'hear/heartbeat',
                   b.body, sha256(convert_to(b.body, 'UTF8')), now(), 1, 'published', now()
              FROM generate_series({offset + 1}, {offset + count}) AS i,
                   LATERAL (SELECT '{{"device_id":"bulk","telemetry_path":"hear/heartbeat",'
                                   '"n":' || i || '}}' AS body) b
        """)
        db.sql("ANALYZE hear.durable_records")

    @staticmethod
    def _buffers(session, query):
        """Pages touched by `query`: the cost that either tracks the backlog or the ledger."""
        plan = session.execute(
            "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY OFF) " + query)
        total = 0
        for line in plan.splitlines():
            if "Buffers:" in line:
                for token in line.split("Buffers:")[1].split():
                    if "=" in token:
                        total += int(token.split("=")[1].rstrip(","))
        assert total > 0, plan
        return total

    def test_the_health_surface_costs_the_backlog_not_the_ledger(self, db):
        """G05: the readiness probe fires every 10 s; its cost must not follow the ledger size.

        The SQLite store answers /healthz with three COUNT(*) scans, so the probe gets slower
        every day the ledger survives. Here the same answers come from partial indexes over the
        unpublished set and from primary-key counter reads, so adding published history must not
        make the probe touch more pages.
        """
        pending_count = "SELECT count(*) FROM hear.durable_records WHERE state = 'pending'"
        oldest = "SELECT min(received_at) FROM hear.durable_records WHERE state = 'pending'"
        counter = ("SELECT value FROM hear.durable_counters "
                   "WHERE tenant_id = 'default' AND counter = 'cache_successes'")

        for i in range(10):
            db.sql(f"SELECT * FROM hear.persist_record('nyquist', 'bl-{i}', "
                   + lit(record_body("nyquist", f"bl-{i}", uptime_s=i))
                   + ", 'hear/heartbeat', now(), 1::smallint, 'lan_http')")
        self._bulk_published(db, 200)

        with db.session("planner") as session:
            session.execute("SET enable_seqscan = off")
            before = {q: self._buffers(session, q) for q in (pending_count, oldest, counter)}
            self._bulk_published(db, 5000, offset=200)
            after = {q: self._buffers(session, q) for q in (pending_count, oldest, counter)}

        for query, cost in before.items():
            # 25x the ledger, and the same handful of pages: that is the whole point of the
            # partial indexes. A generous ceiling, because a page split is not a regression.
            assert after[query] <= cost + 4, (query, cost, after[query])

        # And the composed snapshot stays fast enough to be a 10 s readiness probe.
        started = time.monotonic()
        for _ in range(20):
            db.value("SELECT hear.health_snapshot()")
        assert (time.monotonic() - started) / 20 < 1.0

    def test_the_ledger_survives_what_it_is_supposed_to_refuse(self, db):
        """Every write the schema rejects is a class of corruption that cannot reach Redis."""
        body = record_body("nyquist", "guard-1")
        refusals = [
            ("a body that disagrees with its digest",
             "INSERT INTO hear.durable_records (tenant_id, device_id, record_uid, ingest_source,"
             " telemetry_path, body_json, payload_sha256, received_at, receiver_schema_version)"
             " VALUES ('default', 'nyquist', 'guard-1', 'lan_http', 'hear/heartbeat', "
             + lit(body) + ", sha256(convert_to('other', 'UTF8')), now(), 1)"),
            ("an unknown telemetry path",
             "INSERT INTO hear.durable_records (tenant_id, device_id, record_uid, ingest_source,"
             " telemetry_path, body_json, payload_sha256, received_at, receiver_schema_version)"
             " VALUES ('default', 'nyquist', 'guard-2', 'lan_http', 'hear/other', "
             + lit(body) + ", sha256(convert_to(" + lit(body) + ", 'UTF8')), now(), 1)"),
            ("an unknown ingest source",
             "INSERT INTO hear.durable_records (tenant_id, device_id, record_uid, ingest_source,"
             " telemetry_path, body_json, payload_sha256, received_at, receiver_schema_version)"
             " VALUES ('default', 'nyquist', 'guard-3', 'smoke_signal', 'hear/heartbeat', "
             + lit(body) + ", sha256(convert_to(" + lit(body) + ", 'UTF8')), now(), 1)"),
            ("a published record with no publish time",
             "INSERT INTO hear.durable_records (tenant_id, device_id, record_uid, ingest_source,"
             " telemetry_path, body_json, payload_sha256, received_at, receiver_schema_version,"
             " state) VALUES ('default', 'nyquist', 'guard-4', 'lan_http', 'hear/heartbeat', "
             + lit(body) + ", sha256(convert_to(" + lit(body) + ", 'UTF8')), now(), 1, 'published')"),
            ("a legacy uid on a record the backfill did not import",
             "INSERT INTO hear.durable_records (tenant_id, device_id, record_uid, legacy_record_uid,"
             " ingest_source, telemetry_path, body_json, payload_sha256, received_at,"
             " receiver_schema_version) VALUES ('default', 'nyquist', 'guard-5', 'old', 'lan_http',"
             " 'hear/heartbeat', " + lit(body) + ", sha256(convert_to(" + lit(body)
             + ", 'UTF8')), now(), 1)"),
        ]
        for what, statement in refusals:
            with pytest.raises(TESTDB.PsqlError):
                db.sql(statement)
            assert db.value("SELECT count(*) FROM hear.durable_records") == "0", what

    def test_the_cut_over_is_still_switched_off(self):
        """The suite proves the schema is ready. Readiness is not permission."""
        with pytest.raises(ValueError, match="postgres"):
            HR.make_durable_store("postgres", "postgresql://db.example/dama_hear")
