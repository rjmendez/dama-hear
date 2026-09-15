-- Store-parity fixture for the Postgres durable outbox.
--
-- tests/fixtures/durable_pg_semantics.sql proves the audited *defects* are fixed. This file
-- proves the part that has to hold before a Postgres DurableRecordStore may be written at all:
-- that the schema behaves like the SQLite store the receiver already ships, everywhere the two
-- are required to agree, and behaves deliberately differently only where the audit says it must.
--
-- Every assertion here is decidable by the database alone. The ones that need the Python seam
-- (Redis TTL re-arming, event dedupe tokens, monotonic replay at the cache boundary) live in
-- tests/test_hear_durable_pg_store_parity.py, which runs this file first.
--
-- Run:  psql -v ON_ERROR_STOP=1 -f tests/fixtures/durable_pg_store_parity.sql
-- Runner: tests/test_hear_durable_pg_store_parity.py, against a database this suite created and
-- will drop. It is never run against anything else.
--
-- Leaves the database dirty on purpose: after a failure the rows are the evidence.

\set ON_ERROR_STOP on

SELECT set_config('hear.tenant_id', 'default', false);
SELECT count(*) FROM hear.ensure_partitions(2, 2);

-- ------------------------------------------------------------------------------------------
-- PAR00: this file counts rows and health counters absolutely, so it requires a ledger that
-- nothing else has written to. Say so here rather than fail later with a confusing message.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_records  bigint := (SELECT count(*) FROM hear.durable_records);
    v_attempts bigint := (SELECT count(*) FROM hear.cache_attempts);
    v_health   jsonb  := hear.health_snapshot();
    v_counter  text;
BEGIN
    IF v_records <> 0 OR v_attempts <> 0 THEN
        RAISE EXCEPTION
            'PAR00: this fixture needs a freshly migrated database (found % record(s), % attempt(s))',
            v_records, v_attempts;
    END IF;
    FOREACH v_counter IN ARRAY ARRAY['cache_successes', 'cache_failures', 'claims_expired',
                                     'dead_letter_records', 'records_backfilled',
                                     'unrouted_records', 'records_pruned'] LOOP
        IF coalesce((v_health ->> v_counter)::bigint, 0) <> 0 THEN
            RAISE EXCEPTION
                'PAR00: this fixture needs zeroed health counters (% is %)',
                v_counter, v_health ->> v_counter;
        END IF;
    END LOOP;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR01: the replayed bytes are the arrived bytes
--
-- The SQLite store hands back payload_json verbatim, and the Phase 0 contract freeze declares
-- those bytes frozen. jsonb reorders keys, rewrites numbers and collapses escapes, so a store
-- that replayed the jsonb projection would publish a different body than the device sent.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    -- Deliberately hostile to jsonb normalisation: unsorted keys, a \u escape, a trailing-zero
    -- decimal, an integer wider than float64, and a duplicated-looking nested key order.
    v_body text := '{"device_id":"nyquist","telemetry_path":"hear/heartbeat",' ||
                   '"uptime_s":10,"note":"caf\u00e9","ratio":1.500,' ||
                   '"seq":9007199254740993,"gps":{"fix":3,"alt_m":0.0}}';
    v_stored   text;
    v_returned text;
    v_projected text;
BEGIN
    SELECT body_json INTO v_returned FROM hear.persist_record(
        'nyquist', 'bytes-1', v_body, 'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF v_returned <> v_body THEN
        RAISE EXCEPTION 'PAR01: persist must return the arrived bytes, got %', v_returned;
    END IF;

    SELECT r.body_json, r.payload::text INTO v_stored, v_projected
      FROM hear.durable_records r WHERE r.record_uid = 'bytes-1';
    IF v_stored <> v_body THEN
        RAISE EXCEPTION 'PAR01: the stored body must be byte-identical, got %', v_stored;
    END IF;
    -- If this ever stops being true, jsonb has become lossless and the column comment is stale;
    -- until then it is the reason body_json exists at all.
    IF v_projected = v_body THEN
        RAISE EXCEPTION 'PAR01: expected the jsonb projection to differ from the stored bytes';
    END IF;

    -- A duplicate arrival gets the *stored* bytes back, not its own: the caller must publish
    -- what the ledger holds, or two workers could publish two different bodies for one identity.
    SELECT body_json INTO v_returned FROM hear.persist_record(
        'nyquist', 'bytes-1',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":11}',
        'hear/heartbeat', now(), 1::smallint, 'lan_http');
    IF v_returned <> v_body THEN
        RAISE EXCEPTION 'PAR01: a duplicate must be handed the stored body, got %', v_returned;
    END IF;

    -- And the digest still matches those bytes, which is what makes a restore verifiable.
    IF (SELECT payload_sha256 FROM hear.durable_records WHERE record_uid = 'bytes-1')
       <> sha256(convert_to(v_body, 'UTF8')) THEN
        RAISE EXCEPTION 'PAR01: the digest must cover the stored bytes';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR02: device-scoped idempotency, at more than two devices
--
-- SQLite keys on the bare uid, so exactly one of these rows survives. Here every device keeps
-- its own record and its own body, and the claim surface still carries the device that sent it.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_device text;
    v_body   text;
    v_out    record;
BEGIN
    FOREACH v_device IN ARRAY ARRAY['nyquist', 'mach', 'ageev', 'kasami'] LOOP
        v_body := format('{"device_id":"%s","telemetry_path":"hear/event","kind":"shot"}', v_device);
        SELECT * INTO v_out FROM hear.persist_record(
            v_device, 'shared-uid', v_body, 'hear/event', now(), 1::smallint, 'lan_http');
        IF v_out.is_duplicate THEN
            RAISE EXCEPTION 'PAR02: % must not be deduplicated against another device', v_device;
        END IF;
        IF v_out.body_json <> v_body THEN
            RAISE EXCEPTION 'PAR02: % must get its own payload back, got %', v_device, v_out.body_json;
        END IF;
    END LOOP;

    IF (SELECT count(*) FROM hear.durable_records WHERE record_uid = 'shared-uid') <> 4 THEN
        RAISE EXCEPTION 'PAR02: four devices, four records';
    END IF;
    IF (SELECT count(DISTINCT device_id) FROM hear.durable_records WHERE record_uid = 'shared-uid') <> 4 THEN
        RAISE EXCEPTION 'PAR02: the four records must belong to four devices';
    END IF;
    -- The Redis boundary dedupes events by a single token. With four records sharing one uid,
    -- that token cannot be the uid alone -- see redis_dedupe_token() in tools/hear_durable_pg.py
    -- and the boundary tests. The claim surface exposes the device so the token can be built.
    IF (SELECT count(*) FROM information_schema.routines r
         WHERE r.routine_schema = 'hear' AND r.routine_name = 'claim_pending') <> 1 THEN
        RAISE EXCEPTION 'PAR02: claim_pending must exist';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR03: claim order is the replay order, and it is deterministic
--
-- The SQLite store drains `ORDER BY r.id`. The Postgres claim drains
-- (next_attempt_at, record_id): due work first, and within one due instant, arrival order.
-- A drain that reordered records would let a stale heartbeat land on dama:hear:latest after a
-- newer one (defect D4).
-- ------------------------------------------------------------------------------------------
CREATE TEMP TABLE par03_claimed (rn bigint, record_id bigint, record_uid text, device_id text);

DO $$
DECLARE
    v_uids text[];
    v_ids  bigint[];
BEGIN
    PERFORM hear.persist_record('ordering', format('ord-%s', i),
        format('{"device_id":"ordering","telemetry_path":"hear/heartbeat","uptime_s":%s}', i),
        'hear/heartbeat', now(), 1::smallint, 'lan_http')
      FROM generate_series(1, 5) AS i;

    -- ord-2 is pushed into the future; everything else stays due now.
    UPDATE hear.durable_records SET next_attempt_at = now() + interval '1 hour'
     WHERE device_id = 'ordering' AND record_uid = 'ord-2';

    INSERT INTO par03_claimed (rn, record_id, record_uid, device_id)
    SELECT row_number() OVER (), p.record_id, p.record_uid, p.device_id
      FROM hear.claim_pending('drain-order', 50, 60) p;

    -- The whole batch comes back in (next_attempt_at, record_id) order, which for records that
    -- became due together is arrival order.
    SELECT array_agg(record_id ORDER BY rn) INTO v_ids FROM par03_claimed;
    IF v_ids IS DISTINCT FROM (SELECT array_agg(record_id ORDER BY record_id) FROM par03_claimed) THEN
        RAISE EXCEPTION 'PAR03: the claimed batch must be ordered by arrival, got %', v_ids;
    END IF;

    SELECT array_agg(record_uid ORDER BY rn) INTO v_uids
      FROM par03_claimed WHERE device_id = 'ordering';
    IF v_uids <> ARRAY['ord-1', 'ord-3', 'ord-4', 'ord-5'] THEN
        RAISE EXCEPTION 'PAR03: claim must drain due records in arrival order, got %', v_uids;
    END IF;
    IF (SELECT state FROM hear.durable_records
         WHERE device_id = 'ordering' AND record_uid = 'ord-2') <> 'pending' THEN
        RAISE EXCEPTION 'PAR03: a record that is not due yet must stay pending';
    END IF;

    -- Claiming again returns nothing: the lease, not the read, is what makes the drain safe.
    IF (SELECT count(*) FROM hear.claim_pending('drain-order-2', 10, 60)) <> 0 THEN
        RAISE EXCEPTION 'PAR03: leased records must not be claimable by a second drain';
    END IF;

    -- p_limit is honoured, and a non-positive limit claims nothing (the SQLite store returns []).
    IF (SELECT count(*) FROM hear.claim_pending('drain-order-3', 0, 60)) <> 0 THEN
        RAISE EXCEPTION 'PAR03: a zero limit must claim nothing';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR04: claim -> publish is atomic, and publishing is once
--
-- The transition must be all-or-nothing: a worker killed between the state change and the
-- attempt log must leave a record that is still claimable, not one that is 'published' with no
-- evidence it ever reached Redis.
-- ------------------------------------------------------------------------------------------
BEGIN;
SELECT set_config('hear.tenant_id', 'default', false);
DO $$
DECLARE
    v_rec record;
BEGIN
    SELECT r.record_id, r.received_at INTO v_rec
      FROM hear.durable_records r WHERE r.device_id = 'ordering' AND r.record_uid = 'ord-1';
    IF NOT hear.mark_published(v_rec.record_id, v_rec.received_at, 'redis://scratch:6379') THEN
        RAISE EXCEPTION 'PAR04: publishing a claimed record must succeed';
    END IF;
    IF (SELECT state FROM hear.durable_records
         WHERE record_id = v_rec.record_id AND received_at = v_rec.received_at) <> 'published' THEN
        RAISE EXCEPTION 'PAR04: the publish must be visible inside its own transaction';
    END IF;
END;
$$;
ROLLBACK;

DO $$
DECLARE
    v_rec record;
    v_ok  boolean;
BEGIN
    -- The aborted transaction left nothing behind: state, attempt log and counters all agree.
    IF (SELECT state FROM hear.durable_records
         WHERE device_id = 'ordering' AND record_uid = 'ord-1') <> 'claimed' THEN
        RAISE EXCEPTION 'PAR04: a rolled-back publish must leave the record claimed';
    END IF;
    IF EXISTS (SELECT 1 FROM hear.cache_attempts WHERE record_uid = 'ord-1') THEN
        RAISE EXCEPTION 'PAR04: a rolled-back publish must leave no attempt record';
    END IF;
    IF (hear.health_snapshot() ->> 'cache_successes')::bigint <> 0 THEN
        RAISE EXCEPTION 'PAR04: a rolled-back publish must not count as a success';
    END IF;

    SELECT r.record_id, r.received_at INTO v_rec
      FROM hear.durable_records r WHERE r.device_id = 'ordering' AND r.record_uid = 'ord-1';
    IF NOT hear.mark_published(v_rec.record_id, v_rec.received_at, 'redis://scratch:6379') THEN
        RAISE EXCEPTION 'PAR04: the retried publish must succeed';
    END IF;

    -- Publishing twice is a reported no-op, not a second attempt row: this is what stops a
    -- duplicate XADD after an ambiguous acknowledgement.
    v_ok := hear.mark_published(v_rec.record_id, v_rec.received_at, 'redis://scratch:6379');
    IF v_ok THEN
        RAISE EXCEPTION 'PAR04: a second publish must be refused';
    END IF;
    IF (SELECT count(*) FROM hear.cache_attempts WHERE record_uid = 'ord-1') <> 1 THEN
        RAISE EXCEPTION 'PAR04: exactly one attempt row per publish';
    END IF;
    IF (SELECT claimed_by FROM hear.durable_records
         WHERE record_id = v_rec.record_id AND received_at = v_rec.received_at) IS NOT NULL THEN
        RAISE EXCEPTION 'PAR04: publishing must release the lease';
    END IF;
    IF (hear.health_snapshot() ->> 'cache_successes')::bigint <> 1 THEN
        RAISE EXCEPTION 'PAR04: the publish must be counted exactly once';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR05: a lease expires on time, and only then
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_released integer;
    v_due      timestamptz;
BEGIN
    -- ord-3, ord-4, ord-5 are still leased with a 60 s lease: nothing is expired yet.
    v_released := hear.release_expired_claims();
    IF v_released <> 0 THEN
        RAISE EXCEPTION 'PAR05: a live lease must not be reclaimed, released %', v_released;
    END IF;

    -- Kill one worker: its lease ages out, the record returns to the pending set, and it is
    -- immediately due -- an abandoned record must not also serve a backoff it never earned.
    UPDATE hear.durable_records
       SET claim_expires_at = now() - interval '1 second'
     WHERE device_id = 'ordering' AND record_uid = 'ord-3';

    IF hear.release_expired_claims() <> 1 THEN
        RAISE EXCEPTION 'PAR05: the expired lease must be released';
    END IF;

    SELECT next_attempt_at INTO v_due FROM hear.durable_records
     WHERE device_id = 'ordering' AND record_uid = 'ord-3';
    IF v_due > now() THEN
        RAISE EXCEPTION 'PAR05: a released record must be due immediately, due at %', v_due;
    END IF;
    IF (SELECT publish_attempts FROM hear.durable_records
         WHERE device_id = 'ordering' AND record_uid = 'ord-3') <> 0 THEN
        RAISE EXCEPTION 'PAR05: an expired lease is not a failed attempt';
    END IF;
    IF (hear.health_snapshot() ->> 'claims_expired')::bigint <> 1 THEN
        RAISE EXCEPTION 'PAR05: the expiry must be counted';
    END IF;
    IF (SELECT count(*) FROM hear.claim_pending('drain-after-expiry', 10, 60)) <> 1 THEN
        RAISE EXCEPTION 'PAR05: the released record must be claimable again';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR06: retry, backoff, dead letter
--
-- The shape, not the numbers: attempts count up, the delay grows and is capped, the record is
-- quarantined at the cap instead of being retried forever, and a quarantined record is out of
-- the drain's way but still in the ledger.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_rec      record;
    v_state    text;
    v_prev_gap double precision := -1;
    v_gap      double precision;
    v_i        integer;
BEGIN
    PERFORM hear.persist_record('poison', 'poison-1',
        '{"device_id":"poison","telemetry_path":"hear/event","kind":"shot"}',
        'hear/event', now(), 1::smallint, 'lan_http');
    SELECT r.record_id, r.received_at INTO v_rec
      FROM hear.durable_records r WHERE r.device_id = 'poison';

    FOR v_i IN 1..5 LOOP
        v_state := hear.mark_failed(v_rec.record_id, v_rec.received_at, 'redis://scratch:6379',
                                    repeat('ConnectionError: refused ', 40), 6, 16);
        IF v_state <> 'pending' THEN
            RAISE EXCEPTION 'PAR06: attempt % must not quarantine yet, got %', v_i, v_state;
        END IF;
        SELECT extract(epoch FROM (r.next_attempt_at - now())) INTO v_gap
          FROM hear.durable_records r
         WHERE r.record_id = v_rec.record_id AND r.received_at = v_rec.received_at;
        IF v_gap < v_prev_gap THEN
            RAISE EXCEPTION 'PAR06: backoff must not shrink: % then %', v_prev_gap, v_gap;
        END IF;
        IF v_gap > 16 + 1 THEN
            RAISE EXCEPTION 'PAR06: backoff must respect the cap, got % s', v_gap;
        END IF;
        v_prev_gap := v_gap;
        IF (SELECT publish_attempts FROM hear.durable_records
             WHERE record_id = v_rec.record_id AND received_at = v_rec.received_at) <> v_i THEN
            RAISE EXCEPTION 'PAR06: attempts must count up';
        END IF;
    END LOOP;

    -- The error text is capped at the same 512 characters _error_text() truncates to.
    IF (SELECT char_length(last_error) FROM hear.durable_records
         WHERE record_id = v_rec.record_id AND received_at = v_rec.received_at) <> 512 THEN
        RAISE EXCEPTION 'PAR06: last_error must be capped at 512 characters';
    END IF;

    v_state := hear.mark_failed(v_rec.record_id, v_rec.received_at, 'redis://scratch:6379',
                                'ConnectionError: refused', 6, 16);
    IF v_state <> 'dead_letter' THEN
        RAISE EXCEPTION 'PAR06: the attempt cap must quarantine, got %', v_state;
    END IF;
    IF EXISTS (SELECT 1 FROM hear.claim_pending('drain-dead', 10, 60) WHERE record_uid = 'poison-1') THEN
        RAISE EXCEPTION 'PAR06: a dead-lettered record must not be drained again';
    END IF;
    IF (SELECT count(*) FROM hear.durable_records WHERE record_uid = 'poison-1') <> 1 THEN
        RAISE EXCEPTION 'PAR06: quarantine keeps the record; it never deletes it';
    END IF;
    IF (SELECT count(*) FROM hear.cache_attempts WHERE record_uid = 'poison-1') <> 6 THEN
        RAISE EXCEPTION 'PAR06: every attempt must be logged';
    END IF;
    IF (hear.health_snapshot() ->> 'dead_letter_records')::bigint <> 1
       OR (hear.health_snapshot() ->> 'cache_failures')::bigint <> 6 THEN
        RAISE EXCEPTION 'PAR06: health must report the failures and the dead letter';
    END IF;
    -- mark_failed on a record that no longer exists is a reported miss, not an exception: the
    -- drain must survive a partition dropped underneath an in-flight publish.
    IF hear.mark_failed(v_rec.record_id, now() - interval '400 days', 'redis://scratch:6379',
                        'gone', 6, 16) IS NOT NULL THEN
        RAISE EXCEPTION 'PAR06: failing a vanished record must report NULL';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR07: health is O(1) counters plus partial-index reads
--
-- /healthz is the readiness probe at periodSeconds 10. The SQLite store answers it with three
-- COUNT(*) scans over the whole ledger; the replacement must answer the absolute counts from
-- the counter table and the backlog metrics through the partial indexes, so the cost tracks the
-- backlog and not the ledger.
-- ------------------------------------------------------------------------------------------
-- EXPLAIN returns one row per plan line; a plpgsql SELECT INTO would keep only the first,
-- which is never the line that names the index.
CREATE OR REPLACE FUNCTION pg_temp.plan_of(p_query text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_line text;
    v_plan text := '';
BEGIN
    FOR v_line IN EXECUTE 'EXPLAIN (COSTS OFF) ' || p_query LOOP
        v_plan := v_plan || v_line || E'\n';
    END LOOP;
    RETURN v_plan;
END;
$fn$;

-- A scan of a partitioned table names the *partition's* index, whose name PostgreSQL derives
-- from its columns ("durable_records_20260915_received_at_idx"). The index the schema declared
-- is that index's parent, so the plan is resolved back to the parent names before it is judged.
CREATE OR REPLACE FUNCTION pg_temp.plan_indexes(p_query text)
RETURNS text[]
LANGUAGE sql
AS $fn$
    SELECT coalesce(array_agg(DISTINCT coalesce(parent.relname, idx.relname)), ARRAY[]::text[])
      FROM regexp_matches(pg_temp.plan_of(p_query), 'Scan using ([A-Za-z0-9_]+)', 'g') AS m
      JOIN pg_class idx ON idx.relname = m[1]
      LEFT JOIN pg_inherits inh ON inh.inhrelid = idx.oid
      LEFT JOIN pg_class parent ON parent.oid = inh.inhparent;
$fn$;

DO $$
DECLARE
    v_plan  text;
    v_used  text[];
    v_health jsonb;
BEGIN
    -- The planner is free to seqscan a fixture-sized table; what has to be true is that the
    -- partial indexes *can* answer these queries, which is what stops the cost from growing
    -- with the ledger. enable_seqscan=off makes the planner say so out loud.
    SET LOCAL enable_seqscan = off;

    v_used := pg_temp.plan_indexes($q$SELECT count(*) FROM hear.durable_records WHERE state = 'pending'$q$);
    IF NOT (v_used && ARRAY['durable_records_pending_due', 'durable_records_pending_age']) THEN
        RAISE EXCEPTION 'PAR07: the pending count must come from a partial index, used %', v_used;
    END IF;

    v_used := pg_temp.plan_indexes(
        $q$SELECT min(received_at) FROM hear.durable_records WHERE state = 'pending'$q$);
    IF NOT ('durable_records_pending_age' = ANY (v_used)) THEN
        RAISE EXCEPTION 'PAR07: oldest-pending age must come from the pending-age index, used %', v_used;
    END IF;

    v_used := pg_temp.plan_indexes(
        $q$SELECT * FROM hear.durable_records WHERE state = 'pending' AND next_attempt_at <= now()
           ORDER BY next_attempt_at, record_id LIMIT 256$q$);
    IF NOT ('durable_records_pending_due' = ANY (v_used)) THEN
        RAISE EXCEPTION 'PAR07: the claim scan must use the pending-due index, used %', v_used;
    END IF;

    v_used := pg_temp.plan_indexes(
        $q$SELECT count(*) FROM hear.durable_records WHERE state = 'dead_letter'$q$);
    IF NOT ('durable_records_dead_letter' = ANY (v_used)) THEN
        RAISE EXCEPTION 'PAR07: the dead-letter count must come from its partial index, used %', v_used;
    END IF;

    v_used := pg_temp.plan_indexes(
        $q$SELECT record_id FROM hear.durable_records
            WHERE state = 'claimed' AND claim_expires_at < now()$q$);
    IF NOT ('durable_records_claim_expiry' = ANY (v_used)) THEN
        RAISE EXCEPTION 'PAR07: lease reclaim must come from the claim-expiry index, used %', v_used;
    END IF;

    -- The counters themselves are a primary-key read, never an aggregate over the records.
    v_plan := pg_temp.plan_of(
        $q$SELECT value FROM hear.durable_counters
            WHERE tenant_id = 'default' AND counter = 'cache_successes'$q$);
    IF v_plan NOT ILIKE '%Index%' THEN
        RAISE EXCEPTION 'PAR07: a counter read must be an index lookup, plan: %', v_plan;
    END IF;

    v_health := hear.health_snapshot();
    -- Counters are absolute and monotonic: they count what happened, not what is left.
    IF (v_health ->> 'records_persisted')::bigint
       < (v_health ->> 'pending_records')::bigint + (v_health ->> 'dead_letter_records')::bigint THEN
        RAISE EXCEPTION 'PAR07: persisted must cover everything still in the ledger: %', v_health;
    END IF;
    IF (v_health ->> 'backend') <> 'postgres' OR NOT (v_health ->> 'enabled')::boolean THEN
        RAISE EXCEPTION 'PAR07: health must identify the backend it answers for';
    END IF;
    IF (v_health ->> 'schema_version')::int <> (SELECT max(version) FROM hear.schema_migrations) THEN
        RAISE EXCEPTION 'PAR07: health must report the applied schema version';
    END IF;
    IF v_health ? 'path' THEN
        RAISE EXCEPTION 'PAR07: the DSN is the Python store''s to redact, not the database''s';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR08: backfilled history is imported, never republished
--
-- A record the SQLite ledger already cached must arrive 'published' with the ledger's own
-- acknowledgement time. If it arrived pending, the drain would re-send months of heartbeats to
-- Redis and overwrite live state with history.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_ledger    text := '/state/heartbeat-receiver.sqlite3';
    v_before    bigint := (hear.health_snapshot() ->> 'cache_successes')::bigint;
    v_claimable integer;
BEGIN
    PERFORM hear.backfill_record(v_ledger, 'nyquist', 'legacy-cached',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":1}', 'hear/heartbeat',
        now() - interval '2 hours', 1::smallint, now() - interval '119 minutes');
    PERFORM hear.backfill_record(v_ledger, 'nyquist', 'legacy-uncached',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":2}', 'hear/heartbeat',
        now() - interval '2 hours', 1::smallint, NULL);

    IF (SELECT state FROM hear.durable_records WHERE record_uid = 'legacy-cached') <> 'published' THEN
        RAISE EXCEPTION 'PAR08: an already-cached row must import published';
    END IF;
    IF (SELECT published_at FROM hear.durable_records WHERE record_uid = 'legacy-cached') > now() - interval '1 hour' THEN
        RAISE EXCEPTION 'PAR08: the import must keep the ledger''s acknowledgement time';
    END IF;
    IF (SELECT state FROM hear.durable_records WHERE record_uid = 'legacy-uncached') <> 'pending' THEN
        RAISE EXCEPTION 'PAR08: a row the ledger never cached must import pending';
    END IF;
    IF (hear.health_snapshot() ->> 'cache_successes')::bigint <> v_before THEN
        RAISE EXCEPTION 'PAR08: backfill must not inflate cache_successes';
    END IF;
    IF (SELECT ingest_source FROM hear.durable_records WHERE record_uid = 'legacy-cached')
       <> 'backfill_sqlite'
       OR (SELECT legacy_record_uid FROM hear.durable_records WHERE record_uid = 'legacy-cached')
          <> 'legacy-cached' THEN
        RAISE EXCEPTION 'PAR08: an imported row must be traceable to the ledger it came from';
    END IF;

    -- The drain sees the uncached row and only the uncached row.
    SELECT count(*)::int INTO v_claimable
      FROM hear.claim_pending('drain-backfill', 50, 60) c
     WHERE c.record_uid IN ('legacy-cached', 'legacy-uncached');
    IF v_claimable <> 1 THEN
        RAISE EXCEPTION 'PAR08: exactly the uncached row is drainable, got %', v_claimable;
    END IF;

    -- Re-running an interrupted import imports nothing twice, whatever the watermark says.
    IF hear.backfill_record(v_ledger, 'nyquist', 'legacy-cached',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":1}', 'hear/heartbeat',
        now() - interval '2 hours', 1::smallint, now() - interval '119 minutes') THEN
        RAISE EXCEPTION 'PAR08: a second import pass must not duplicate';
    END IF;
    PERFORM hear.backfill_advance(v_ledger, 41);
    PERFORM hear.backfill_advance(v_ledger, 12);
    IF (SELECT last_sqlite_id FROM hear.backfill_watermarks WHERE source_ledger = v_ledger) <> 41 THEN
        RAISE EXCEPTION 'PAR08: the watermark must never move backwards';
    END IF;
    IF (SELECT rows_imported FROM hear.backfill_watermarks WHERE source_ledger = v_ledger) <> 2 THEN
        RAISE EXCEPTION 'PAR08: only the rows actually imported are counted';
    END IF;
    IF (hear.health_snapshot() ->> 'records_backfilled')::bigint <> 2 THEN
        RAISE EXCEPTION 'PAR08: imports must be counted as backfill, not as ingest';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR09: partition and retention safeguards
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_day  date := (now() AT TIME ZONE 'UTC')::date + 30;
    v_name text;
    v_rows bigint;
BEGIN
    -- ensure_partitions() is idempotent: the second run creates nothing.
    IF EXISTS (SELECT 1 FROM hear.ensure_partitions(2, 2) WHERE created) THEN
        RAISE EXCEPTION 'PAR09: a second partition run must create nothing';
    END IF;

    -- A record arriving outside every partition lands in the default rather than being refused,
    -- and says so in health. Ingest survives a stopped maintenance timer; the alert is the count.
    PERFORM hear.persist_record('unrouted', 'unrouted-1',
        '{"device_id":"unrouted","telemetry_path":"hear/heartbeat","uptime_s":1}',
        'hear/heartbeat', v_day::timestamptz, 1::smallint, 'lan_http');
    IF (hear.health_snapshot() ->> 'unrouted_records')::bigint <> 1 THEN
        RAISE EXCEPTION 'PAR09: an unrouted record must be visible in health';
    END IF;
    IF (SELECT count(*) FROM hear.unrouted_records(10)) <> 1 THEN
        RAISE EXCEPTION 'PAR09: unrouted_records() must list it';
    END IF;

    -- Retention refuses the default partition outright, whatever its age.
    IF NOT EXISTS (SELECT 1 FROM hear.enforce_retention(30, true)
                    WHERE action = 'refused' AND relation = 'hear.durable_records_unrouted') THEN
        RAISE EXCEPTION 'PAR09: the default partition must never be dropped';
    END IF;

    -- Retention disabled is a no-op, not an unbounded drop.
    IF NOT EXISTS (SELECT 1 FROM hear.enforce_retention(0, false) WHERE action = 'skipped') THEN
        RAISE EXCEPTION 'PAR09: p_days <= 0 must disable retention';
    END IF;

    -- An aged, fully published partition is dropped; the dry run before it changes nothing.
    v_day := (now() AT TIME ZONE 'UTC')::date - 45;
    v_name := hear.partition_name('durable_records', v_day);
    EXECUTE format('CREATE TABLE hear.%I PARTITION OF hear.durable_records '
                   'FOR VALUES FROM (%L) TO (%L)', v_name, v_day::timestamptz, (v_day + 1)::timestamptz);
    PERFORM hear.backfill_record('/state/mqtt-bridge.sqlite3', 'nyquist', 'aged-1',
        '{"device_id":"nyquist","telemetry_path":"hear/heartbeat","uptime_s":3}', 'hear/heartbeat',
        v_day::timestamptz + interval '3 hours', 1::smallint, v_day::timestamptz + interval '3 hours');

    IF NOT EXISTS (SELECT 1 FROM hear.enforce_retention(30, true)
                    WHERE action = 'would_drop' AND relation = format('hear.%s', v_name)) THEN
        RAISE EXCEPTION 'PAR09: the aged published partition should be a dry-run candidate';
    END IF;
    IF to_regclass(format('hear.%I', v_name)) IS NULL THEN
        RAISE EXCEPTION 'PAR09: a dry run must not drop anything';
    END IF;

    SELECT row_count INTO v_rows FROM hear.enforce_retention(30, false)
     WHERE action = 'dropped' AND relation = format('hear.%s', v_name);
    IF v_rows IS NULL THEN
        RAISE EXCEPTION 'PAR09: the aged partition should have been dropped';
    END IF;
    IF (hear.health_snapshot() ->> 'records_pruned')::bigint <> v_rows THEN
        RAISE EXCEPTION 'PAR09: pruned rows must be counted';
    END IF;
    IF EXISTS (SELECT 1 FROM hear.durable_record_ids WHERE record_uid = 'aged-1') THEN
        RAISE EXCEPTION 'PAR09: the dedupe horizon must follow the retention window';
    END IF;

    -- Deleting the evidence of an outage is not retention: a partition holding anything that
    -- Redis never accepted is refused no matter how old it is.
    v_day := (now() AT TIME ZONE 'UTC')::date - 46;
    v_name := hear.partition_name('durable_records', v_day);
    EXECUTE format('CREATE TABLE hear.%I PARTITION OF hear.durable_records '
                   'FOR VALUES FROM (%L) TO (%L)', v_name, v_day::timestamptz, (v_day + 1)::timestamptz);
    PERFORM hear.persist_record('stalled', 'stalled-1',
        '{"device_id":"stalled","telemetry_path":"hear/heartbeat","uptime_s":4}',
        'hear/heartbeat', v_day::timestamptz + interval '4 hours', 1::smallint, 'lan_http');
    IF NOT EXISTS (SELECT 1 FROM hear.enforce_retention(30, false)
                    WHERE action = 'refused' AND relation = format('hear.%s', v_name)
                      AND reason = 'partition still holds unpublished records') THEN
        RAISE EXCEPTION 'PAR09: unpublished history must survive retention';
    END IF;
    IF to_regclass(format('hear.%I', v_name)) IS NULL THEN
        RAISE EXCEPTION 'PAR09: the refused partition must still be there';
    END IF;
END;
$$;

-- ------------------------------------------------------------------------------------------
-- PAR10: the interface the Python store has to implement is present and complete
--
-- The seam is DurableRecordStore: persist / note_cache_success / note_cache_failure /
-- pending_records / prune_acknowledged / health. Each has exactly one counterpart here, and a
-- store that needed a seventh entry point would be changing the contract, not implementing it.
-- ------------------------------------------------------------------------------------------
DO $$
DECLARE
    v_missing text;
BEGIN
    SELECT string_agg(needed, ', ') INTO v_missing
      FROM unnest(ARRAY['persist_record', 'claim_pending', 'mark_published', 'mark_failed',
                        'release_expired_claims', 'health_snapshot', 'enforce_retention',
                        'ensure_partitions', 'verify_payload_integrity', 'backfill_record']) AS needed
     WHERE NOT EXISTS (SELECT 1 FROM pg_proc p
                        WHERE p.pronamespace = 'hear'::regnamespace AND p.proname = needed);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'PAR10: the store seam is missing %', v_missing;
    END IF;

    -- claim_pending is the replay surface: it must hand the caller everything
    -- RedisHeartbeatCache.write() needs (device_id, telemetry_path, the verbatim body and the
    -- record_uid), plus received_at so the caller can apply the heartbeat TTL / monotonic guard.
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p
         WHERE p.pronamespace = 'hear'::regnamespace AND p.proname = 'claim_pending'
           AND ARRAY['record_id', 'received_at', 'tenant_id', 'device_id', 'record_uid',
                     'telemetry_path', 'body_json', 'publish_attempts', 'claim_expires_at']
               <@ p.proargnames) THEN
        RAISE EXCEPTION 'PAR10: claim_pending must return the full replay surface, got %',
            (SELECT proargnames FROM pg_proc
              WHERE pronamespace = 'hear'::regnamespace AND proname = 'claim_pending');
    END IF;
END;
$$;

\echo 'durable_pg_store_parity: all assertions passed'
