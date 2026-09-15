-- 0002_hear_durable_claims_and_counters
--
-- The behaviour of the outbox, expressed once, in the database: persist, claim, publish, fail,
-- expire, and the O(1) health snapshot. Every writer -- receiver, MQTT bridge, backfill, a
-- future replay tool -- goes through these functions, so "a record is published exactly once"
-- is not re-implemented per caller.
--
-- Additive: creates functions and triggers in the `hear` schema only. Nothing outside `hear` is
-- referenced, and no existing object is altered.

-- ---------------------------------------------------------------------------------------------
-- Counters
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION hear.bump_counter(p_tenant_id text, p_counter text, p_delta bigint DEFAULT 1)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO hear.durable_counters (tenant_id, counter, value, updated_at)
    VALUES (p_tenant_id, p_counter, greatest(p_delta, 0), now())
    ON CONFLICT (tenant_id, counter) DO UPDATE
        SET value = hear.durable_counters.value + greatest(p_delta, 0),
            updated_at = now();
$$;

COMMENT ON FUNCTION hear.bump_counter(text, text, bigint) IS
    'Monotonic counter increment; the health() counts are read from here instead of COUNT(*).';

-- Counters are maintained by triggers rather than by the persist/publish functions so that a
-- writer which bypasses them (the SQLite backfill, a manual repair) cannot leave the health
-- surface lying about what is in the table.
CREATE OR REPLACE FUNCTION hear.tg_durable_records_counters()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM hear.bump_counter(NEW.tenant_id, 'records_persisted', 1);
        IF NEW.ingest_source = 'backfill_sqlite' THEN
            PERFORM hear.bump_counter(NEW.tenant_id, 'records_backfilled', 1);
        END IF;
        IF NEW.state = 'dead_letter' THEN
            PERFORM hear.bump_counter(NEW.tenant_id, 'dead_lettered', 1);
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.state = 'dead_letter' AND OLD.state IS DISTINCT FROM 'dead_letter' THEN
            PERFORM hear.bump_counter(NEW.tenant_id, 'dead_lettered', 1);
        END IF;
        IF NEW.state = 'published' AND OLD.state IS DISTINCT FROM 'published' THEN
            INSERT INTO hear.durable_events (tenant_id, last_publish_at)
            VALUES (NEW.tenant_id, now())
            ON CONFLICT (tenant_id) DO UPDATE SET last_publish_at = now();
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION hear.tg_cache_attempts_counters()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.outcome = 'succeeded' THEN
        PERFORM hear.bump_counter(NEW.tenant_id, 'cache_successes', 1);
    ELSE
        PERFORM hear.bump_counter(NEW.tenant_id, 'cache_failures', 1);
        INSERT INTO hear.durable_events (tenant_id, last_cache_failure_at)
        VALUES (NEW.tenant_id, NEW.created_at)
        ON CONFLICT (tenant_id) DO UPDATE SET last_cache_failure_at = NEW.created_at;
    END IF;
    RETURN NULL;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgname = 'durable_records_counters'
                     AND tgrelid = 'hear.durable_records'::regclass) THEN
        CREATE TRIGGER durable_records_counters
            AFTER INSERT OR UPDATE OF state ON hear.durable_records
            FOR EACH ROW EXECUTE FUNCTION hear.tg_durable_records_counters();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgname = 'cache_attempts_counters'
                     AND tgrelid = 'hear.cache_attempts'::regclass) THEN
        CREATE TRIGGER cache_attempts_counters
            AFTER INSERT ON hear.cache_attempts
            FOR EACH ROW EXECUTE FUNCTION hear.tg_cache_attempts_counters();
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------------------------
-- persist
-- ---------------------------------------------------------------------------------------------
--
-- Equivalent of SqliteDurableRecordStore.persist(): commit the record before Redis is touched,
-- return the *stored* body (first writer wins, exactly as INSERT OR IGNORE behaves today) and
-- report whether this record has already been published so the caller can suppress a second
-- non-idempotent stream append.
--
-- Two differences from SQLite, both deliberate:
--   * identity is (tenant_id, device_id, record_uid), which closes the cross-device collision;
--   * a duplicate arrival is counted (duplicate_arrivals) and a duplicate arrival carrying a
--     *different* body is counted separately (conflicting_arrivals) instead of vanishing.
CREATE OR REPLACE FUNCTION hear.persist_record(
    p_device_id               text,
    p_record_uid              text,
    p_body_json               text,
    p_telemetry_path          text,
    p_received_at             timestamptz,
    p_receiver_schema_version smallint,
    p_ingest_source           text,
    p_idempotency_key         text DEFAULT NULL,
    p_tenant_id               text DEFAULT NULL
)
RETURNS TABLE (
    record_id     bigint,
    received_at   timestamptz,
    is_duplicate  boolean,
    is_conflict   boolean,
    state         text,
    cached        boolean,
    body_json     text
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_tenant    text := coalesce(p_tenant_id,
                                 nullif(current_setting('hear.tenant_id', true), ''),
                                 'default');
    v_new_id    bigint := nextval('hear.durable_records_record_id_seq');
    v_sha       bytea  := sha256(convert_to(p_body_json, 'UTF8'));
    v_id_row    record;
    v_rec       record;
BEGIN
    INSERT INTO hear.durable_record_ids AS ids
        (tenant_id, device_id, record_uid, record_id, received_at, payload_sha256)
    VALUES (v_tenant, p_device_id, p_record_uid, v_new_id, p_received_at, v_sha)
    ON CONFLICT (tenant_id, device_id, record_uid) DO UPDATE
        SET last_seen_at = now(),
            duplicate_arrivals = ids.duplicate_arrivals + 1,
            conflicting_arrivals = ids.conflicting_arrivals
                + CASE WHEN ids.payload_sha256 IS DISTINCT FROM excluded.payload_sha256 THEN 1 ELSE 0 END
    RETURNING ids.record_id, ids.received_at, ids.payload_sha256 INTO v_id_row;

    IF v_id_row.record_id = v_new_id THEN
        INSERT INTO hear.durable_records
            (record_id, tenant_id, device_id, record_uid, ingest_source, telemetry_path,
             idempotency_key, body_json, payload_sha256, received_at, receiver_schema_version)
        VALUES (v_new_id, v_tenant, p_device_id, p_record_uid, p_ingest_source, p_telemetry_path,
                p_idempotency_key, p_body_json, v_sha, p_received_at, p_receiver_schema_version);

        RETURN QUERY SELECT v_new_id, p_received_at, false, false, 'pending'::text, false, p_body_json;
        RETURN;
    END IF;

    SELECT r.record_id, r.received_at, r.state, r.body_json
      INTO v_rec
      FROM hear.durable_records r
     WHERE r.received_at = v_id_row.received_at
       AND r.record_id = v_id_row.record_id;

    IF NOT FOUND THEN
        -- The identity outlived its record: only reachable if retention dropped the partition
        -- while the identity row survived. Re-own the identity rather than refusing the write,
        -- so a retention race degrades to "published again", never to "silently dropped".
        INSERT INTO hear.durable_records
            (record_id, tenant_id, device_id, record_uid, ingest_source, telemetry_path,
             idempotency_key, body_json, payload_sha256, received_at, receiver_schema_version)
        VALUES (v_new_id, v_tenant, p_device_id, p_record_uid, p_ingest_source, p_telemetry_path,
                p_idempotency_key, p_body_json, v_sha, p_received_at, p_receiver_schema_version);

        UPDATE hear.durable_record_ids
           SET record_id = v_new_id, received_at = p_received_at, payload_sha256 = v_sha
         WHERE tenant_id = v_tenant AND device_id = p_device_id AND record_uid = p_record_uid;

        RETURN QUERY SELECT v_new_id, p_received_at, false, false, 'pending'::text, false, p_body_json;
        RETURN;
    END IF;

    PERFORM hear.bump_counter(v_tenant, 'records_duplicate', 1);
    IF v_id_row.payload_sha256 IS DISTINCT FROM v_sha THEN
        PERFORM hear.bump_counter(v_tenant, 'records_conflicting', 1);
    END IF;

    RETURN QUERY SELECT v_rec.record_id,
                        v_rec.received_at,
                        true,
                        (v_id_row.payload_sha256 IS DISTINCT FROM v_sha),
                        v_rec.state,
                        (v_rec.state = 'published'),
                        v_rec.body_json;
END;
$$;

COMMENT ON FUNCTION hear.persist_record(text, text, text, text, timestamptz, smallint, text, text, text) IS
    'Durable-before-Redis commit. Returns the stored body and whether it was already published.';

-- ---------------------------------------------------------------------------------------------
-- claim / publish / fail
-- ---------------------------------------------------------------------------------------------
--
-- SKIP LOCKED leases, not row-visibility guesses: a claim is a short lease on a pending row,
-- so N drain workers (and therefore replicas > 1) cannot publish the same record twice, and a
-- worker killed mid-publish does not strand the record -- its lease expires and
-- hear.release_expired_claims() returns the row to the pending set.
CREATE OR REPLACE FUNCTION hear.claim_pending(
    p_worker    text,
    p_limit     integer     DEFAULT 256,
    p_lease_s   integer     DEFAULT 60,
    p_tenant_id text        DEFAULT NULL,
    p_now       timestamptz DEFAULT NULL
)
RETURNS TABLE (
    record_id        bigint,
    received_at      timestamptz,
    tenant_id        text,
    device_id        text,
    record_uid       text,
    telemetry_path   text,
    body_json        text,
    publish_attempts integer,
    claim_expires_at timestamptz
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_now timestamptz := coalesce(p_now, now());
BEGIN
    IF p_limit <= 0 THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH due AS (
        SELECT r.record_id AS rid, r.received_at AS rat
          FROM hear.durable_records r
         WHERE r.state = 'pending'
           AND r.next_attempt_at <= v_now
           AND (p_tenant_id IS NULL OR r.tenant_id = p_tenant_id)
         ORDER BY r.next_attempt_at, r.record_id
         LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE hear.durable_records r
       SET state = 'claimed',
           claimed_by = p_worker,
           claimed_at = v_now,
           claim_expires_at = v_now + make_interval(secs => greatest(p_lease_s, 1))
      FROM due
     WHERE r.record_id = due.rid AND r.received_at = due.rat
    RETURNING r.record_id, r.received_at, r.tenant_id, r.device_id, r.record_uid,
              r.telemetry_path, r.body_json, r.publish_attempts, r.claim_expires_at;
END;
$$;

COMMENT ON FUNCTION hear.claim_pending(text, integer, integer, text, timestamptz) IS
    'Leases up to p_limit due pending records with FOR UPDATE SKIP LOCKED; safe for N workers.';

CREATE OR REPLACE FUNCTION hear.mark_published(
    p_record_id    bigint,
    p_received_at  timestamptz,
    p_cache_target text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    v_rec record;
BEGIN
    UPDATE hear.durable_records r
       SET state = 'published',
           published_at = now(),
           claimed_by = NULL,
           claimed_at = NULL,
           claim_expires_at = NULL,
           cache_target = p_cache_target,
           last_error = NULL
     WHERE r.record_id = p_record_id
       AND r.received_at = p_received_at
       AND r.state <> 'published'
    RETURNING r.tenant_id, r.device_id, r.record_uid INTO v_rec;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    INSERT INTO hear.cache_attempts
        (tenant_id, device_id, record_uid, record_id, received_at, cache_target, outcome)
    VALUES (v_rec.tenant_id, v_rec.device_id, v_rec.record_uid, p_record_id, p_received_at,
            p_cache_target, 'succeeded');
    RETURN true;
END;
$$;

-- Backoff and the dead-letter cap are parameters, not constants: the store passes whatever the
-- operator configured, and this function only guarantees the shape (exponential, capped, then
-- quarantined -- never dropped, never retried forever).
CREATE OR REPLACE FUNCTION hear.mark_failed(
    p_record_id      bigint,
    p_received_at    timestamptz,
    p_cache_target   text,
    p_error_text     text,
    p_max_attempts   integer DEFAULT 50,
    p_backoff_cap_s  integer DEFAULT 300
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_rec        record;
    v_attempts   integer;
    v_state      text;
    v_backoff_s  integer;
BEGIN
    SELECT r.tenant_id, r.device_id, r.record_uid, r.publish_attempts
      INTO v_rec
      FROM hear.durable_records r
     WHERE r.record_id = p_record_id AND r.received_at = p_received_at
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_attempts := v_rec.publish_attempts + 1;
    -- 2^n seconds, capped; the exponent is clamped before exponentiation so a long-lived
    -- poison record cannot overflow the interval arithmetic on its way to the cap.
    v_backoff_s := least(power(2, least(v_attempts, 20))::bigint, greatest(p_backoff_cap_s, 1))::integer;
    v_state := CASE WHEN p_max_attempts > 0 AND v_attempts >= p_max_attempts
                    THEN 'dead_letter' ELSE 'pending' END;

    UPDATE hear.durable_records r
       SET state = v_state,
           publish_attempts = v_attempts,
           next_attempt_at = now() + make_interval(secs => v_backoff_s),
           claimed_by = NULL,
           claimed_at = NULL,
           claim_expires_at = NULL,
           cache_target = p_cache_target,
           last_error = left(p_error_text, 512),
           dead_lettered_at = CASE WHEN v_state = 'dead_letter' THEN now() ELSE NULL END
     WHERE r.record_id = p_record_id AND r.received_at = p_received_at;

    INSERT INTO hear.cache_attempts
        (tenant_id, device_id, record_uid, record_id, received_at, cache_target, outcome, error_text)
    VALUES (v_rec.tenant_id, v_rec.device_id, v_rec.record_uid, p_record_id, p_received_at,
            p_cache_target, 'failed', left(p_error_text, 512));

    RETURN v_state;
END;
$$;

CREATE OR REPLACE FUNCTION hear.release_expired_claims(p_tenant_id text DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_released integer;
BEGIN
    WITH expired AS (
        UPDATE hear.durable_records r
           SET state = 'pending',
               claimed_by = NULL,
               claimed_at = NULL,
               claim_expires_at = NULL,
               next_attempt_at = least(r.next_attempt_at, now())
         WHERE r.state = 'claimed'
           AND r.claim_expires_at < now()
           AND (p_tenant_id IS NULL OR r.tenant_id = p_tenant_id)
        RETURNING r.tenant_id
    )
    SELECT count(*)::integer INTO v_released FROM expired;

    IF v_released > 0 THEN
        PERFORM hear.bump_counter(coalesce(p_tenant_id, 'default'), 'claims_expired', v_released);
    END IF;
    RETURN v_released;
END;
$$;

COMMENT ON FUNCTION hear.release_expired_claims(text) IS
    'Returns leases abandoned by a killed worker to the pending set; run on the replay interval.';

-- ---------------------------------------------------------------------------------------------
-- health
-- ---------------------------------------------------------------------------------------------
--
-- Same keys as DurableRecordStore.health(), plus the fields the SQLite backend cannot answer.
-- `path` is deliberately absent: only the Python store knows the DSN, and it must redact it.
CREATE OR REPLACE FUNCTION hear.health_snapshot(p_tenant_id text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    WITH scope AS (
        SELECT coalesce(p_tenant_id, nullif(current_setting('hear.tenant_id', true), ''), 'default') AS tenant_id
    ),
    counters AS (
        SELECT c.counter, c.value
          FROM hear.durable_counters c, scope s
         WHERE c.tenant_id = s.tenant_id
    ),
    pending AS (
        SELECT count(*) AS n, min(r.received_at) AS oldest
          FROM hear.durable_records r, scope s
         WHERE r.state = 'pending' AND r.tenant_id = s.tenant_id
    ),
    claimed AS (
        SELECT count(*) AS n
          FROM hear.durable_records r, scope s
         WHERE r.state = 'claimed' AND r.tenant_id = s.tenant_id
    ),
    dead AS (
        SELECT count(*) AS n
          FROM hear.durable_records r, scope s
         WHERE r.state = 'dead_letter' AND r.tenant_id = s.tenant_id
    ),
    unrouted AS (
        SELECT count(*) AS n FROM hear.durable_records_unrouted
    )
    SELECT jsonb_build_object(
        'backend', 'postgres',
        'enabled', true,
        'tenant_id', (SELECT tenant_id FROM scope),
        'pending_records', (SELECT n FROM pending),
        'claimed_records', (SELECT n FROM claimed),
        'dead_letter_records', (SELECT n FROM dead),
        'unrouted_records', (SELECT n FROM unrouted),
        'oldest_pending_age_s',
            CASE WHEN (SELECT oldest FROM pending) IS NULL THEN NULL
                 ELSE extract(epoch FROM (now() - (SELECT oldest FROM pending)))::bigint END,
        'cache_successes', coalesce((SELECT value FROM counters WHERE counter = 'cache_successes'), 0),
        'cache_failures', coalesce((SELECT value FROM counters WHERE counter = 'cache_failures'), 0),
        'records_persisted', coalesce((SELECT value FROM counters WHERE counter = 'records_persisted'), 0),
        'records_duplicate', coalesce((SELECT value FROM counters WHERE counter = 'records_duplicate'), 0),
        'records_conflicting', coalesce((SELECT value FROM counters WHERE counter = 'records_conflicting'), 0),
        'records_backfilled', coalesce((SELECT value FROM counters WHERE counter = 'records_backfilled'), 0),
        'records_pruned', coalesce((SELECT value FROM counters WHERE counter = 'records_pruned'), 0),
        'claims_expired', coalesce((SELECT value FROM counters WHERE counter = 'claims_expired'), 0),
        'last_cache_failure_at',
            (SELECT to_char(e.last_cache_failure_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
               FROM hear.durable_events e, scope s WHERE e.tenant_id = s.tenant_id),
        'last_publish_at',
            (SELECT to_char(e.last_publish_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
               FROM hear.durable_events e, scope s WHERE e.tenant_id = s.tenant_id),
        'schema_version', (SELECT max(version) FROM hear.schema_migrations)
    );
$$;

COMMENT ON FUNCTION hear.health_snapshot(text) IS
    'O(1) counters plus partial-index-backed pending metrics; safe to call from a 10s readiness probe.';

-- ---------------------------------------------------------------------------------------------
-- integrity
-- ---------------------------------------------------------------------------------------------
--
-- The CHECK constraint proves a body hashes to its digest at write time; this proves it still
-- does, which is the question after a restore, a page-level corruption or a manual repair.
CREATE OR REPLACE FUNCTION hear.verify_payload_integrity(
    p_since timestamptz DEFAULT NULL,
    p_limit integer     DEFAULT 1000
)
RETURNS TABLE (record_id bigint, received_at timestamptz, device_id text, record_uid text)
LANGUAGE sql
STABLE
AS $$
    SELECT r.record_id, r.received_at, r.device_id, r.record_uid
      FROM hear.durable_records r
     WHERE (p_since IS NULL OR r.received_at >= p_since)
       AND r.payload_sha256 IS DISTINCT FROM sha256(convert_to(r.body_json, 'UTF8'))
     ORDER BY r.received_at, r.record_id
     LIMIT greatest(p_limit, 0);
$$;
