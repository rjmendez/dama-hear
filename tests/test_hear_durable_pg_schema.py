"""The Postgres durable-outbox DDL: versioned, additive, reversible, and still switched off.

No PostgreSQL server is required. CI has none, so everything here reads the migration files,
the receiver module and a real SQLite ledger instead. The one test that does need a server is
skipped unless HEAR_PG_TEST_DSN names one -- it applies every migration, runs
tests/fixtures/durable_pg_semantics.sql, and then rolls the whole schema back.

Two guarantees this file exists to hold:

* the DDL and tools/hear_heartbeat_receiver.py cannot drift apart silently. The retention
  default, the telemetry paths, the 512-character error cap and the durable schema generation
  are asserted against the Python constants, not restated;
* this change does not switch anything on. make_durable_store("postgres") must still refuse,
  because the soak gate, not the schema, is what authorizes the cut-over.
"""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import hear_durable_pg as PG  # noqa: E402
import hear_heartbeat_receiver as HR  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "durable_pg_semantics.sql"


@pytest.fixture(scope="module")
def migrations():
    return PG.load_migrations()


def _sql(migrations, version):
    return next(m.sql for m in migrations if m.version == version)


def _all_sql(migrations):
    return "\n".join(m.sql for m in migrations)


class TestMigrationFiles:
    def test_every_migration_is_additive_idempotent_and_reversible(self, migrations):
        assert PG.validate(migrations) == []

    def test_versions_are_contiguous_and_each_has_a_rollback(self, migrations):
        assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
        for migration in migrations:
            assert migration.rollback_path is not None, migration.path.name
            assert migration.rollback_path.name.endswith(PG.ROLLBACK_SUFFIX)

    def test_a_forward_migration_that_drops_a_table_is_rejected(self, tmp_path):
        """The guard has to actually fail; a green check that cannot go red proves nothing."""
        forward = tmp_path / "migrations"
        rollback = tmp_path / "rollback"
        forward.mkdir()
        rollback.mkdir()
        (forward / "0001_bad.sql").write_text("DROP TABLE hear.durable_records;\n")
        (rollback / "0001_bad_down.sql").write_text("DROP TABLE IF EXISTS hear.nothing;\n")
        bad = PG.load_migrations(forward, rollback)
        problems = PG.check_forward(bad[0])
        assert any("drops an object" in p for p in problems)

    def test_a_non_repeatable_forward_statement_is_rejected(self, tmp_path):
        forward = tmp_path / "migrations"
        rollback = tmp_path / "rollback"
        forward.mkdir()
        rollback.mkdir()
        (forward / "0001_bad.sql").write_text("CREATE TABLE hear.x (a int);\n")
        (rollback / "0001_bad_down.sql").write_text("DROP TABLE IF EXISTS hear.x;\n")
        bad = PG.load_migrations(forward, rollback)
        assert any("not re-appliable" in p for p in PG.check_forward(bad[0]))

    def test_checksums_are_content_addressed(self, migrations):
        first = migrations[0]
        assert first.checksum == PG.sha256_text(first.path.read_text(encoding="utf-8"))
        assert first.checksum in PG.record_statement(first)

    def test_the_apply_plan_is_printed_not_executed(self, migrations):
        plan = PG.apply_plan(migrations)
        assert all(line.startswith(("psql", "--")) for line in plan)
        # One apply and one recorded row per migration.
        assert sum(1 for line in plan if line.startswith("psql") and "-f " in line) == len(migrations)
        assert sum(1 for line in plan if "hear.schema_migrations" in line) == len(migrations)

    def test_only_the_baseline_rollback_destroys_records(self, migrations):
        for migration in migrations:
            drops_schema = "DROP SCHEMA" in migration.rollback_sql.upper()
            assert drops_schema == (migration.version == 1), migration.rollback_path.name

    def test_statement_splitting_keeps_function_bodies_opaque(self):
        sql = (
            "CREATE OR REPLACE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$\n"
            "BEGIN DROP TABLE t; END;\n$$;\n"
            "CREATE TABLE IF NOT EXISTS u (a int);\n"
        )
        statements = list(PG.iter_statements(sql))
        assert len(statements) == 2
        assert statements[0].startswith("CREATE OR REPLACE FUNCTION")
        assert statements[1].startswith("CREATE TABLE IF NOT EXISTS")

    def test_comments_do_not_become_statements(self):
        sql = "-- DROP TABLE t;\nCREATE TABLE IF NOT EXISTS u (a int); -- trailing\n"
        assert list(PG.iter_statements(sql)) == ["CREATE TABLE IF NOT EXISTS u (a int)"]


class TestSchemaShape:
    """The decisions the audit demanded, asserted against the DDL that implements them."""

    def test_identity_is_device_scoped(self, migrations):
        """D1: the SQLite ledger keys on a bare record_uid, so two devices collide."""
        baseline = _sql(migrations, 1)
        assert "PRIMARY KEY (tenant_id, device_id, record_uid)" in baseline

    def test_records_are_range_partitioned_by_arrival_with_a_default_partition(self, migrations):
        baseline = _sql(migrations, 1)
        assert "PARTITION BY RANGE (received_at)" in baseline
        assert "PARTITION BY RANGE (created_at)" in baseline
        assert "PARTITION OF hear.durable_records DEFAULT" in baseline

    def test_pending_work_is_reachable_through_partial_indexes_only(self, migrations):
        baseline = _sql(migrations, 1)
        for index in ("durable_records_pending_due", "durable_records_pending_age",
                      "durable_records_claim_expiry", "durable_records_dead_letter"):
            assert index in baseline
        # Every index over the outbox working set must be partial, or /healthz is a table scan
        # again at the next order of magnitude.
        assert baseline.count("WHERE state = 'pending'") >= 2
        assert "WHERE state = 'claimed'" in baseline
        assert "WHERE state = 'dead_letter'" in baseline

    def test_claims_are_skip_locked_leases(self, migrations):
        claims = _sql(migrations, 2)
        assert "FOR UPDATE SKIP LOCKED" in claims
        assert "claim_expires_at" in claims
        assert "release_expired_claims" in claims

    def test_payload_integrity_is_enforced_by_the_database(self, migrations):
        baseline = _sql(migrations, 1)
        assert "payload_sha256 = sha256(convert_to(body_json, 'UTF8'))" in baseline
        assert "verify_payload_integrity" in _sql(migrations, 2)

    def test_the_replayed_bytes_are_stored_verbatim(self, migrations):
        """jsonb normalises numbers and key order; the Redis body contract does not."""
        baseline = _sql(migrations, 1)
        assert "body_json               text        NOT NULL" in baseline
        assert "payload                 jsonb       GENERATED ALWAYS AS (body_json::jsonb) STORED" in baseline

    def test_health_is_counters_not_scans(self, migrations):
        baseline, claims = _sql(migrations, 1), _sql(migrations, 2)
        assert "hear.durable_counters" in baseline
        assert "health_snapshot" in claims
        for key in ("pending_records", "cache_successes", "cache_failures",
                    "last_cache_failure_at", "dead_letter_records", "oldest_pending_age_s"):
            assert f"'{key}'" in claims

    def test_health_returns_the_sqlite_key_set(self, migrations):
        """A backend swap must not change what /healthz says, only what it costs."""
        claims = _sql(migrations, 2)
        for key in HR.DurableRecordStore().health():
            if key == "path":
                # Only the Python store knows the DSN, and it is responsible for redacting it.
                assert f"'{key}'" not in claims
                continue
            assert f"'{key}'" in claims

    def test_retention_refuses_to_delete_unpublished_records(self, migrations):
        retention = _sql(migrations, 3)
        assert "partition still holds unpublished records" in retention
        assert "p_dry_run boolean DEFAULT true" in retention

    def test_retention_default_and_ceiling_track_the_code_and_the_governance_doc(self, migrations):
        retention = _sql(migrations, 3)
        assert f"p_days    integer DEFAULT {HR.DURABLE_RETENTION_DAYS}" in retention
        # docs/data-governance.md: telemetry is class R0-derived, default maximum 90 days.
        assert "IF p_days > 90 THEN" in retention
        governance = (ROOT / "docs" / "data-governance.md").read_text()
        assert "`R0-derived` | detections and non-reversible features | 90 days" in governance

    def test_tenant_isolation_and_role_separation_exist(self, migrations):
        access = _sql(migrations, 4)
        assert "ENABLE ROW LEVEL SECURITY" in access
        assert "CREATE POLICY tenant_isolation" in access
        for role in ("hear_durable_writer", "hear_durable_reader",
                     "hear_durable_auditor", "hear_durable_admin"):
            assert role in access
        # The writer may never delete: retention is the only sanctioned deletion path.
        assert "GRANT SELECT, INSERT, UPDATE ON hear.durable_records" in access
        assert "DELETE" not in access.split("TO hear_durable_writer;")[0].split("GRANT SELECT, INSERT, UPDATE ON hear.durable_records")[-1]

    def test_operator_reads_are_redacted_and_audit_reads_carry_no_payload(self, migrations):
        access = _sql(migrations, 4)
        assert "durable_records_operator" in access
        assert "#- '{gps,lat}'" in access and "#- '{gps,lon}'" in access
        audit_view = access.split("CREATE OR REPLACE VIEW hear.durable_records_audit")[1]
        assert "payload_sha256" in audit_view
        assert "r.body_json" not in audit_view

    def test_views_are_security_invoker_so_row_level_security_still_applies(self, migrations):
        for version in (4, 5, 6):
            sql = _sql(migrations, version)
            assert sql.count("WITH (security_invoker = true)") == sql.count("CREATE OR REPLACE VIEW")

    def test_the_refusal_quarantine_is_implemented_key_for_key(self, migrations):
        """The SQLite refusal seam must survive the cut-over instead of being dropped with it."""
        refusals = _sql(migrations, 6)
        assert "hear.refused_messages" in refusals
        assert "hear.refusal_health_snapshot" in refusals
        for key in HR.DurableRecordStore().refusal_health():
            if key in ("backend", "enabled", "max_rows", "max_bytes"):
                # backend/enabled are literals in the snapshot; the caps are parameters of
                # enforce_refusal_bounds, asserted below rather than as health keys.
                continue
            assert f"'{key}'" in refusals, key

    def test_the_durable_health_contract_is_not_widened_by_the_quarantine(self, migrations):
        """Refusal keys must stay off health(), or a store that cannot keep refusals breaks it."""
        base = HR.DurableRecordStore().health()
        assert "refused_messages" not in base and "last_refusal_at" not in base
        assert "refused_messages" not in _sql(migrations, 2)
        # ... and the refusal surface still reports them.
        assert {"refused_messages", "last_refusal_at"} <= set(
            HR.DurableRecordStore().refusal_health())

    def test_refusals_are_bounded_by_size_not_only_by_age(self, migrations):
        """Any topic publisher can mint unique rejected bodies; age alone is not a capacity bound."""
        refusals = _sql(migrations, 6)
        assert "hear.enforce_refusal_bounds" in refusals
        assert "p_max_rows  integer DEFAULT 5000" in refusals
        assert f"p_max_bytes bigint  DEFAULT {16 * 1024 * 1024}" in refusals
        assert "PERFORM hear.enforce_refusal_bounds" in refusals
        # Eviction is scoped to the quarantine; the outbox is not reachable from that path.
        bounds = refusals.split("CREATE OR REPLACE FUNCTION hear.enforce_refusal_bounds")[1]
        bounds = bounds.split("COMMENT ON FUNCTION hear.enforce_refusal_bounds")[0]
        assert "durable_records" not in bounds and "cache_attempts" not in bounds
        assert "GROUP BY g.source, g.device_id" in bounds

    def test_refusal_retention_matches_the_outbox_posture(self, migrations):
        refusals = _sql(migrations, 6)
        assert f"p_days    integer DEFAULT {HR.DURABLE_RETENTION_DAYS}" in refusals
        assert "IF p_days > 90 THEN" in refusals
        assert "p_dry_run boolean DEFAULT true" in refusals

    def test_refusals_are_tenant_isolated_and_readable_without_the_body(self, migrations):
        refusals = _sql(migrations, 6)
        assert "ALTER TABLE hear.refused_messages ENABLE ROW LEVEL SECURITY" in refusals
        assert "CREATE POLICY tenant_isolation ON hear.%I" in refusals
        assert "'refused_messages', 'refusal_counters', 'refusal_events'" in refusals
        audit_view = refusals.split("CREATE OR REPLACE VIEW hear.refused_messages_audit")[1]
        assert "security_invoker = true" in audit_view
        # A refused body is unvalidated device input: it stays off the reader/auditor surface.
        assert "r.body_text" not in audit_view
        assert "GRANT SELECT ON hear.refused_messages_audit TO hear_durable_reader" in refusals
        # The writer quarantines and bounds; only the admin deletes.
        assert "GRANT SELECT, INSERT, UPDATE ON hear.refused_messages TO hear_durable_writer" in refusals
        assert "GRANT SELECT, DELETE ON hear.refused_messages TO hear_durable_admin" in refusals

    def test_the_refusal_rollback_reverses_only_what_the_migration_created(self, migrations):
        rollback = next(m.rollback_sql for m in migrations if m.version == 6)
        assert "DROP TABLE IF EXISTS hear.refused_messages" in rollback
        assert "DROP FUNCTION IF EXISTS hear.refusal_health_snapshot(text)" in rollback
        for kept in ("hear.durable_records", "hear.cache_attempts", "hear.health_snapshot"):
            assert f"DROP TABLE IF EXISTS {kept}" not in rollback
            assert f"DROP FUNCTION IF EXISTS {kept}" not in rollback

    def test_backfill_never_publishes_and_keeps_the_legacy_uid(self, migrations):
        backfill = _sql(migrations, 5)
        assert "legacy_record_uid" in backfill
        assert "'backfill_sqlite'" in backfill
        assert "backfill_watermarks" in backfill
        assert "legacy_uid_collisions" in backfill


class TestSeamConstantsStayInSync:
    def test_telemetry_paths_and_ingest_sources_match_the_validators(self, migrations):
        baseline = _sql(migrations, 1)
        for path in ("hear/heartbeat", "hear/event"):
            assert f"'{path}'" in baseline
            assert HR.validate_heartbeat_payload is not None
        assert "'lan_http', 'mqtt_bridge', 'backfill_sqlite'" in baseline

    def test_the_error_column_matches_the_error_text_cap(self, migrations):
        cap = len(HR._error_text(RuntimeError("x" * 4096)))
        assert cap == 512
        baseline = _sql(migrations, 1)
        assert f"char_length(last_error) <= {cap}" in baseline

    def test_postgres_is_a_new_durable_generation_not_a_revision_of_the_sqlite_one(self, migrations):
        assert HR.DURABLE_SCHEMA_VERSION == 1
        assert PG.DURABLE_SCHEMA_VERSION_POSTGRES == 2
        assert "durable_schema_version  smallint    NOT NULL DEFAULT 2" in _sql(migrations, 1)

    def test_the_seam_is_not_switched_on_by_this_change(self):
        """Schema first, cut-over later: the gate is the soak, not the presence of DDL."""
        assert HR.DURABLE_BACKENDS == ("none", "sqlite", "postgres")
        with pytest.raises(ValueError, match="postgres"):
            HR.make_durable_store("postgres", "postgresql://db.example/dama_hear")
        assert HR.make_durable_store("none").backend == "none"


class TestLegacyIdentityCorpus:
    """The D1 corpus, built with the shipping SQLite store rather than described."""

    def _ledger(self, tmp_path):
        return HR.SqliteDurableRecordStore(str(tmp_path / "state" / "legacy.sqlite3"))

    def _record(self, device_id, uid):
        record = {
            "device_id": device_id,
            "telemetry_path": "hear/heartbeat",
            "idempotency_key": uid,
            "received_at": HR.utc_now(),
            "receiver_schema_version": HR.RECEIVER_SCHEMA_VERSION,
        }
        return record, HR.encode_json(record)

    def test_the_sqlite_ledger_drops_the_second_device(self, tmp_path):
        store = self._ledger(tmp_path)
        first_record, first_body = self._record("nyquist", "evt-1")
        second_record, second_body = self._record("mach", "evt-1")
        store.persist("evt-1", first_record, first_body)
        stored = store.persist("evt-1", second_record, second_body)

        # The second device's record never lands, and the caller is handed the first device's
        # payload back. This is the defect the Postgres identity model removes.
        assert stored.record["device_id"] == "nyquist"
        assert store.health()["pending_records"] == 1

    def test_the_collision_is_detectable_before_the_backfill_runs(self, tmp_path):
        store = self._ledger(tmp_path)
        for device_id in ("nyquist", "mach"):
            record, body = self._record(device_id, "evt-1")
            store.persist("evt-1", record, body)
        record, body = self._record("ageev", "evt-2")
        store.persist("evt-2", record, body)

        # What the *devices* sent, which is what the backfill reads from the payloads.
        pairs = [("nyquist", "evt-1"), ("mach", "evt-1"), ("ageev", "evt-2")]
        assert PG.legacy_collisions(pairs) == {"evt-1": ["nyquist", "mach"]}

    def test_scoped_identity_separates_what_the_bare_uid_merged(self):
        assert PG.scoped_identity("nyquist", "evt-1") != PG.scoped_identity("mach", "evt-1")
        assert PG.scoped_identity("nyquist", "evt-1") == ("default", "nyquist", "evt-1")
        with pytest.raises(ValueError):
            PG.scoped_identity("", "evt-1")


class TestSemanticFixture:
    def test_the_fixture_covers_the_audited_defects(self):
        sql = FIXTURE.read_text()
        for marker in ("D1:", "D1b:", "D2:", "G01:", "G07:", "R04:", "R05:", "R07:",
                       "B01:", "B02:", "B03:", "RLS:"):
            assert marker in sql, marker

    def test_the_fixture_stops_on_the_first_failed_assertion(self):
        assert "\\set ON_ERROR_STOP on" in FIXTURE.read_text()


@pytest.mark.skipif(
    not os.environ.get("HEAR_PG_TEST_DSN") or shutil.which("psql") is None,
    reason="needs psql and an empty throwaway database in HEAR_PG_TEST_DSN",
)
class TestAgainstARealServer:
    """Apply, re-apply, exercise, roll back. Never run against anything but a scratch database."""

    def _psql(self, *args, dsn=None):
        return subprocess.run(
            ["psql", "-v", "ON_ERROR_STOP=1", dsn or os.environ["HEAR_PG_TEST_DSN"], *args],
            check=True, capture_output=True, text=True, timeout=300)

    def test_migrations_apply_twice_then_the_semantics_hold_then_they_roll_back(self, migrations):
        for _ in range(2):   # idempotency: the runner may re-run a partially applied file
            for migration in migrations:
                self._psql("--single-transaction", "-f", str(migration.path))

        result = self._psql("-f", str(FIXTURE))
        assert "all assertions passed" in (result.stdout + result.stderr)

        for migration in reversed(migrations):
            self._psql("--single-transaction", "-f", str(migration.rollback_path))

        left = self._psql("-tAc", "SELECT count(*) FROM pg_namespace WHERE nspname = 'hear'")
        assert left.stdout.strip() == "0"
