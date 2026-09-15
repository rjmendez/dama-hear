-- Semantic fixture for the Postgres durable outbox.
--
-- Runs the audited defect cases and the retention/claim rules against a real, empty database
-- that has migrations 0001-0006 applied, and raises on the first violated invariant. It is the
-- executable half of docs/durable-postgres-schema.md: every assertion here corresponds to a row
-- of the audit's test matrix that can be decided by the schema alone (the rest need the Python
-- store, which does not exist yet).
--
-- Run:  psql -v ON_ERROR_STOP=1 -f tests/fixtures/durable_pg_semantics.sql
-- Runner: tests/test_hear_durable_pg_schema.py, skipped unless HEAR_PG_TEST_DSN is set.
--
-- Leaves the database dirty on purpose: after a failure the rows are the evidence.

\set ON_ERROR_STOP on

SELECT set_config('hear.tenant_id', 'default', false);
SELECT count(*) FROM hear.ensure_partitions(2, 2);

-- ------------------------------------------------------------------------------------------
-- P01/P05: first write is pending, a repeat of it is a duplicate that returns the stored body
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_body   text := '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":10}';
    v_body2  text := '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":99}';
    v_first  record;
    v_again  record;
    v_other  record;
BEGIN
    SELECT * INTO v_first FROM hear.persist_record(
        'nyquist', 'evt-1', v_body, 'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF v_first.is_duplicate OR v_first.cached OR v_first.state <> 'pending' THEN
        RAISE EXCEPTION 'P01: first persist must be a new pending record, got %', v_first;
    END IF;

    SELECT * INTO v_again FROM hear.persist_record(
        'nyquist', 'evt-1', v_body2, 'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF NOT v_again.is_duplicate THEN
        RAISE EXCEPTION 'P05: repeat of the same identity must deduplicate';
    END IF;
    IF v_again.record_id <> v_first.record_id THEN
        RAISE EXCEPTION 'P05: duplicate must resolve to the stored record';
    END IF;
    -- D1b: first body wins, exactly as SQLite's INSERT OR IGNORE, but the disagreement is counted.
    IF v_again.body_json <> v_body THEN
        RAISE EXCEPTION 'D1b: the stored body must win, got %', v_again.body_json;
    END IF;
    IF NOT v_again.is_conflict THEN
        RAISE EXCEPTION 'D1b: a differing body for the same identity must be reported';
    END IF;

    -- D1: a second device using the same idempotency key is a different record. In SQLite this
    -- row was silently discarded and the first device's payload was returned to the caller.
    SELECT * INTO v_other FROM hear.persist_record(
        'mach', 'evt-1', '{"device_id":"mach","telemetry_path":"hear/heartbeat","uptime_s":3}',
        'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF v_other.is_duplicate OR v_other.record_id = v_first.record_id THEN
        RAISE EXCEPTION 'D1: two devices sharing a record_uid must keep two records';
    END IF;

    IF (SELECT count(*) FROM hear.durable_records WHERE record_uid = 'evt-1') <> 2 THEN
        RAISE EXCEPTION 'D1: expected exactly two rows for the colliding uid';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM hear.legacy_uid_collisions WHERE record_uid = 'evt-1') THEN
        RAISE EXCEPTION 'D1: the collision must be visible in hear.legacy_uid_collisions';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM hear.identity_conflicts WHERE record_uid = 'evt-1'
                                                           AND device_id = 'nyquist') THEN
        RAISE EXCEPTION 'D1b: the body conflict must be visible in hear.identity_conflicts';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- Integrity: a body that does not match its own digest, or its own columns, is not a row
-- ------------------------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO hear.durable_records
            (tenant_id, device_id, record_uid, ingest_source, telemetry_path, body_json,
             payload_sha256, received_at, receiver_schema_version)
        VALUES ('default', 'nyquist', 'corrupt-1', 'lan_http', 'hear/heartbeat',
                '{"device_id":"nyquist","telemetry_path":"hear/heartbeat"}',
                sha256(convert_to('something else', 'UTF8')), now(), 1);
        RAISE EXCEPTION 'integrity: a mismatched payload_sha256 must be rejected';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    BEGIN
        INSERT INTO hear.durable_records
            (tenant_id, device_id, record_uid, ingest_source, telemetry_path, body_json,
             payload_sha256, received_at, receiver_schema_version)
        VALUES ('default', 'nyquist', 'mismatch-1', 'lan_http', 'hear/heartbeat',
                '{"device_id":"mach","telemetry_path":"hear/heartbeat"}',
                sha256(convert_to('{"device_id":"mach","telemetry_path":"hear/heartbeat"}', 'UTF8')),
                now(), 1);
        RAISE EXCEPTION 'integrity: device_id must agree with the stored body';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    IF EXISTS (SELECT 1 FROM hear.verify_payload_integrity()) THEN
        RAISE EXCEPTION 'integrity: verify_payload_integrity() found a corrupt row';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- G01: leases. One claim per record, the second worker gets nothing, an expired lease returns.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_claim   record;
    v_second  integer;
    v_state   text;
BEGIN
    IF (SELECT count(*) FROM hear.claim_pending('worker-a', 10, 60)) <> 2 THEN
        RAISE EXCEPTION 'claim: both pending records should be claimable';
    END IF;

    SELECT count(*) INTO v_second FROM hear.claim_pending('worker-b', 10, 60);
    IF v_second <> 0 THEN
        RAISE EXCEPTION 'G01: a claimed record must not be claimable again, got %', v_second;
    END IF;

    SELECT r.record_id, r.received_at INTO v_claim
      FROM hear.durable_records r WHERE r.device_id = 'mach' LIMIT 1;

    IF NOT hear.mark_published(v_claim.record_id, v_claim.received_at, 'redis://audit-redis:6379') THEN
        RAISE EXCEPTION 'publish: marking a claimed record published must succeed';
    END IF;
    IF hear.mark_published(v_claim.record_id, v_claim.received_at, 'redis://audit-redis:6379') THEN
        RAISE EXCEPTION 'publish: publishing twice must be reported as a no-op';
    END IF;

    -- A published record is what makes persist() return cached=true, which is what suppresses a
    -- second, non-idempotent stream append.
    IF NOT (SELECT cached FROM hear.persist_record(
                'mach', 'evt-1', '{"device_id":"mach","telemetry_path":"hear/heartbeat","uptime_s":3}',
                'hear/heartbeat', now(), 1::smallint, 'lan_http')) THEN
        RAISE EXCEPTION 'D2: a published record must come back cached';
    END IF;

    -- Expire the remaining leases the way a killed worker would.
    UPDATE hear.durable_records SET claim_expires_at = now() - interval '1 minute'
     WHERE state = 'claimed';
    IF hear.release_expired_claims() <> 1 THEN
        RAISE EXCEPTION 'R07: expired leases must return to the pending set';
    END IF;

    SELECT state INTO v_state FROM hear.durable_records
     WHERE device_id = 'nyquist' AND record_uid = 'evt-1';
    IF v_state <> 'pending' THEN
        RAISE EXCEPTION 'R07: released record should be pending, got %', v_state;
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- R04/R05: backoff grows, then the record is quarantined instead of retried forever
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_rec    record;
    v_state  text;
    v_due    timestamptz;
BEGIN
    SELECT r.record_id, r.received_at INTO v_rec
      FROM hear.durable_records r WHERE r.device_id = 'nyquist' AND r.record_uid = 'evt-1';

    v_state := hear.mark_failed(v_rec.record_id, v_rec.received_at, 'redis://audit-redis:6379',
                                'ConnectionError: refused', 3, 300);
    IF v_state <> 'pending' THEN
        RAISE EXCEPTION 'R04: one failure must not quarantine the record, got %', v_state;
    END IF;
    SELECT next_attempt_at INTO v_due FROM hear.durable_records
     WHERE record_id = v_rec.record_id AND received_at = v_rec.received_at;
    IF v_due <= now() THEN
        RAISE EXCEPTION 'R04: a failed record must be due in the future';
    END IF;

    PERFORM hear.mark_failed(v_rec.record_id, v_rec.received_at, 'redis://audit-redis:6379',
                             'ConnectionError: refused', 3, 300);
    v_state := hear.mark_failed(v_rec.record_id, v_rec.received_at, 'redis://audit-redis:6379',
                                'ConnectionError: refused', 3, 300);
    IF v_state <> 'dead_letter' THEN
        RAISE EXCEPTION 'R05: the attempt cap must quarantine, got %', v_state;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM hear.durable_records
                    WHERE record_id = v_rec.record_id AND dead_lettered_at IS NOT NULL) THEN
        RAISE EXCEPTION 'R05: a dead-lettered record must keep its row and its timestamp';
    END IF;
    IF (SELECT (hear.health_snapshot() ->> 'dead_letter_records')::int) <> 1 THEN
        RAISE EXCEPTION 'R05: health must report the dead letter';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- G05: health is counters, not scans, and carries the SQLite key set
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_health jsonb := hear.health_snapshot();
    v_key    text;
BEGIN
    FOREACH v_key IN ARRAY ARRAY['backend', 'enabled', 'pending_records', 'cache_successes',
                                 'cache_failures', 'last_cache_failure_at'] LOOP
        IF NOT v_health ? v_key THEN
            RAISE EXCEPTION 'health: the SQLite key % is missing from the Postgres snapshot', v_key;
        END IF;
    END LOOP;
    IF (v_health ->> 'cache_successes')::int <> 1 THEN
        RAISE EXCEPTION 'health: expected one cache success, got %', v_health ->> 'cache_successes';
    END IF;
    IF (v_health ->> 'cache_failures')::int <> 3 THEN
        RAISE EXCEPTION 'health: expected three cache failures, got %', v_health ->> 'cache_failures';
    END IF;
    IF v_health ->> 'last_cache_failure_at' IS NULL THEN
        RAISE EXCEPTION 'health: a failure must set last_cache_failure_at';
    END IF;
    IF (v_health ->> 'records_conflicting')::int <> 1 THEN
        RAISE EXCEPTION 'health: the conflicting duplicate must be counted';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- B01/B03: backfill imports without publishing, and keeps the ledger's own uid
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_successes_before bigint := (hear.health_snapshot() ->> 'cache_successes')::bigint;
    v_imported boolean;
BEGIN
    v_imported := hear.backfill_record(
        '/state/mqtt-bridge.sqlite3', 'ageev', 'legacy-7',
        '{"device_id":"ageev","telemetry_path":"hear/event"}', 'hear/event',
        now() - interval '1 hour', 1::smallint, now() - interval '59 minutes');
    IF NOT v_imported THEN
        RAISE EXCEPTION 'B01: a new legacy row must import';
    END IF;
    IF hear.backfill_record(
        '/state/mqtt-bridge.sqlite3', 'ageev', 'legacy-7',
        '{"device_id":"ageev","telemetry_path":"hear/event"}', 'hear/event',
        now() - interval '1 hour', 1::smallint, now() - interval '59 minutes') THEN
        RAISE EXCEPTION 'B02: re-running an interrupted backfill must not duplicate';
    END IF;

    IF (SELECT state FROM hear.durable_records WHERE record_uid = 'legacy-7') <> 'published' THEN
        RAISE EXCEPTION 'B01: an already-cached legacy row must import as published, not pending';
    END IF;
    IF (SELECT legacy_record_uid FROM hear.durable_records WHERE record_uid = 'legacy-7') IS NULL THEN
        RAISE EXCEPTION 'B03: the ledger''s own uid must be preserved';
    END IF;
    -- A backfilled row was published by the ledger it came from, not by us: counting it as one
    -- of our cache successes would fake the very metric the soak is watching.
    IF (hear.health_snapshot() ->> 'cache_successes')::bigint <> v_successes_before THEN
        RAISE EXCEPTION 'B01: backfill must not publish or inflate cache_successes';
    END IF;
    IF (hear.health_snapshot() ->> 'records_backfilled')::bigint <> 1 THEN
        RAISE EXCEPTION 'B01: the import must be counted as a backfill';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- G06/G07: partitions and retention
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_old_day date := (now() AT TIME ZONE 'UTC')::date - 40;
    v_rows    bigint;
BEGIN
    EXECUTE format(
        'CREATE TABLE hear.durable_records_%s PARTITION OF hear.durable_records '
        'FOR VALUES FROM (%L) TO (%L)',
        to_char(v_old_day, 'YYYYMMDD'), v_old_day::timestamptz, (v_old_day + 1)::timestamptz);

    -- One aged record that was never published, and one that was.
    PERFORM hear.persist_record('nyquist', 'aged-pending',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":1}',
        'hear/heartbeat', v_old_day::timestamptz + interval '1 hour', 1::smallint, 'lan_http');

    PERFORM hear.backfill_record('/state/heartbeat-receiver.sqlite3', 'nyquist', 'aged-published',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":2}', 'hear/heartbeat',
        v_old_day::timestamptz + interval '2 hours', 1::smallint,
        v_old_day::timestamptz + interval '2 hours');

    -- Refused: the partition still holds a record Redis never accepted.
    IF NOT EXISTS (SELECT 1 FROM hear.enforce_retention(30, true)
                    WHERE action = 'refused' AND reason = 'partition still holds unpublished records') THEN
        RAISE EXCEPTION 'G07: retention must refuse a partition holding unpublished records';
    END IF;

    -- Dry run never drops.
    IF to_regclass(format('hear.durable_records_%s', to_char(v_old_day, 'YYYYMMDD'))) IS NULL THEN
        RAISE EXCEPTION 'G07: a dry run must not drop anything';
    END IF;

    -- Publish it, and the same partition becomes eligible.
    UPDATE hear.durable_records SET state = 'published', published_at = now()
     WHERE record_uid = 'aged-pending';

    SELECT row_count INTO v_rows FROM hear.enforce_retention(30, false)
     WHERE action = 'dropped'
       AND relation = format('hear.durable_records_%s', to_char(v_old_day, 'YYYYMMDD'));
    IF v_rows IS NULL OR v_rows <> 2 THEN
        RAISE EXCEPTION 'G07: the aged published partition should have been dropped with 2 rows, got %', v_rows;
    END IF;
    IF to_regclass(format('hear.durable_records_%s', to_char(v_old_day, 'YYYYMMDD'))) IS NOT NULL THEN
        RAISE EXCEPTION 'G07: the partition should be gone';
    END IF;
    -- Identity rows must not outlive their records, or a replayed duplicate would look new.
    IF EXISTS (SELECT 1 FROM hear.durable_record_ids WHERE record_uid = 'aged-published') THEN
        RAISE EXCEPTION 'G07: the dedupe horizon must follow the retention window';
    END IF;

    BEGIN
        PERFORM hear.enforce_retention(120, true);
        RAISE EXCEPTION 'G07: retention beyond the governance ceiling must be refused';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE '%ceiling%' THEN RAISE; END IF;
    END;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- Tenant isolation is enforced by the database, not by a WHERE clause the caller may forget
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_visible integer;
BEGIN
    PERFORM set_config('hear.tenant_id', 'tenant-b', false);
    PERFORM hear.persist_record('kasami', 'tenant-b-1',
        '{"device_id":"kasami","telemetry_path":"hear/heartbeat","uptime_s":5}',
        'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF (hear.health_snapshot() ->> 'records_persisted')::int <> 1 THEN
        RAISE EXCEPTION 'tenant: counters must be per tenant';
    END IF;
    PERFORM set_config('hear.tenant_id', 'default', false);
    SELECT count(*)::int INTO v_visible FROM hear.durable_records WHERE device_id = 'kasami';
    IF v_visible <> 1 THEN
        RAISE EXCEPTION 'tenant: the table owner is expected to bypass RLS during maintenance';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- Access control: the writer role is confined to its tenant and cannot delete history
-- ------------------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hear_test_writer') THEN
        CREATE ROLE hear_test_writer NOLOGIN IN ROLE hear_durable_writer;
    END IF;
END;
$$;

SET ROLE hear_test_writer;

DO $$
DECLARE
    v_seen integer;
BEGIN
    PERFORM set_config('hear.tenant_id', 'default', false);
    SELECT count(*)::int INTO v_seen FROM hear.durable_records WHERE device_id = 'kasami';
    IF v_seen <> 0 THEN
        RAISE EXCEPTION 'RLS: tenant-b rows must not be visible to a default-tenant writer';
    END IF;

    PERFORM set_config('hear.tenant_id', 'tenant-b', false);
    SELECT count(*)::int INTO v_seen FROM hear.durable_records WHERE device_id = 'kasami';
    IF v_seen <> 1 THEN
        RAISE EXCEPTION 'RLS: a writer must see its own tenant, got % rows', v_seen;
    END IF;

    -- Retention is the only sanctioned deletion path, and it does not run as the writer.
    BEGIN
        DELETE FROM hear.durable_records WHERE device_id = 'kasami';
        RAISE EXCEPTION 'grants: the writer role must not be able to delete records';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- R4Q: the refusal quarantine stores rejected input, bounds it, and cannot reach the outbox
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_id       bigint;
    v_rows     bigint;
    v_outbox   bigint;
    v_outbox2  bigint;
    v_evicted  bigint;
    v_health   jsonb;
BEGIN
    PERFORM set_config('hear.tenant_id', 'default', false);
    SELECT count(*) INTO v_outbox FROM hear.durable_records;

    -- A refused message is recorded, and a repeat of the identical refusal bumps the counter
    -- instead of adding a row.
    v_id := hear.record_refusal('default', 'uid-refusal-1', 'hear/event', 'mach', 'mqtt_bridge',
                                'time must be an object', '{"telemetry_path":"hear/event"}');
    IF v_id IS NULL THEN
        RAISE EXCEPTION 'R4Q: a refusal within the bounds must be stored';
    END IF;
    PERFORM hear.record_refusal('default', 'uid-refusal-1', 'hear/event', 'mach', 'mqtt_bridge',
                                'time must be an object', '{"telemetry_path":"hear/event"}');
    SELECT count(*) INTO v_rows FROM hear.refused_messages;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'R4Q: a repeated refusal must bump occurrences, got % rows', v_rows;
    END IF;
    IF (SELECT occurrences FROM hear.refused_messages WHERE refusal_uid = 'uid-refusal-1') <> 2 THEN
        RAISE EXCEPTION 'R4Q: the repeat was not counted';
    END IF;

    -- A flood of distinct bodies is bounded by row count, not by age, and the eviction takes
    -- from the loudest publisher.
    FOR i IN 1..40 LOOP
        PERFORM hear.record_refusal('default', 'flood-' || i, 'hear/event', 'flooder',
                                    'mqtt_bridge', 'malformed JSON', '{"n":' || i || '}',
                                    false, 10, 16777216);
    END LOOP;
    SELECT count(*) INTO v_rows FROM hear.refused_messages;
    IF v_rows > 10 THEN
        RAISE EXCEPTION 'R4Q: the row cap must bound the quarantine, got % rows', v_rows;
    END IF;

    -- The outbox is untouched by any of it.
    SELECT count(*) INTO v_outbox2 FROM hear.durable_records;
    IF v_outbox2 <> v_outbox THEN
        RAISE EXCEPTION 'R4Q: refusal handling must never touch accepted records (% -> %)',
            v_outbox, v_outbox2;
    END IF;

    -- Health reports the quarantine without widening the durable health contract.
    v_health := hear.refusal_health_snapshot();
    IF (v_health->>'refused_messages')::bigint <> v_rows THEN
        RAISE EXCEPTION 'R4Q: refusal health must report the stored rows';
    END IF;
    IF hear.health_snapshot() ? 'refused_messages' THEN
        RAISE EXCEPTION 'R4Q: the durable health contract must not gain refusal keys';
    END IF;

    -- Retention is dry-run by default and refuses to exceed the governance ceiling.
    PERFORM hear.enforce_refusal_retention(30);
    IF (SELECT count(*) FROM hear.refused_messages) <> v_rows THEN
        RAISE EXCEPTION 'R4Q: a dry-run retention must not delete anything';
    END IF;
    BEGIN
        PERFORM hear.enforce_refusal_retention(365, false);
        RAISE EXCEPTION 'R4Q: retention beyond 90 days must be refused';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE 'R4Q:%' THEN
            RAISE;
        END IF;
    END;
END;
$$;

RESET ROLE;
SELECT set_config('hear.tenant_id', 'default', false);

\echo 'durable_pg_semantics: all assertions passed'
