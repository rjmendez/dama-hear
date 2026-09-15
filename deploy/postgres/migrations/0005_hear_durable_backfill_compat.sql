-- 0005_hear_durable_backfill_compat
--
-- Import surface for the two existing SQLite ledgers (/state/heartbeat-receiver.sqlite3 and
-- /state/mqtt-bridge.sqlite3), plus the views that make the collisions those ledgers hid
-- visible before anything is imported.
--
-- The one invariant of backfill: it never publishes. A record that SQLite already cached is
-- imported as 'published' with the ledger's own acknowledgement time, so the drain worker will
-- not re-send years-old heartbeats to Redis and overwrite live state. A record SQLite never
-- managed to cache is imported as 'pending', which is the truth about it, and the drain worker
-- will publish it exactly once.

CREATE TABLE IF NOT EXISTS hear.backfill_watermarks (
    source_ledger  text        NOT NULL PRIMARY KEY,
    last_sqlite_id bigint      NOT NULL DEFAULT 0 CHECK (last_sqlite_id >= 0),
    rows_imported  bigint      NOT NULL DEFAULT 0 CHECK (rows_imported >= 0),
    rows_skipped   bigint      NOT NULL DEFAULT 0 CHECK (rows_skipped >= 0),
    started_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz
);

COMMENT ON TABLE hear.backfill_watermarks IS
    'Resume point per source SQLite ledger: the highest durable_records.id already imported.';

-- Imports one legacy row. Idempotent on (tenant_id, device_id, record_uid), so an interrupted
-- backfill is resumed by simply running it again -- the watermark is an optimisation, not the
-- correctness argument.
CREATE OR REPLACE FUNCTION hear.backfill_record(
    p_source_ledger           text,
    p_device_id               text,
    p_record_uid              text,
    p_body_json               text,
    p_telemetry_path          text,
    p_received_at             timestamptz,
    p_receiver_schema_version smallint,
    p_acknowledged_at         timestamptz DEFAULT NULL,
    p_idempotency_key         text        DEFAULT NULL,
    p_tenant_id               text        DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    v_tenant text := coalesce(p_tenant_id,
                              nullif(current_setting('hear.tenant_id', true), ''),
                              'default');
    v_new_id bigint := nextval('hear.durable_records_record_id_seq');
    v_sha    bytea  := sha256(convert_to(p_body_json, 'UTF8'));
    v_owner  bigint;
BEGIN
    INSERT INTO hear.durable_record_ids AS ids
        (tenant_id, device_id, record_uid, record_id, received_at, payload_sha256)
    VALUES (v_tenant, p_device_id, p_record_uid, v_new_id, p_received_at, v_sha)
    ON CONFLICT (tenant_id, device_id, record_uid) DO UPDATE
        SET last_seen_at = now(),
            duplicate_arrivals = ids.duplicate_arrivals + 1
    RETURNING ids.record_id INTO v_owner;

    IF v_owner <> v_new_id THEN
        RETURN false;       -- already present, from the live path or an earlier backfill pass
    END IF;

    INSERT INTO hear.durable_records
        (record_id, tenant_id, device_id, record_uid, legacy_record_uid, ingest_source,
         telemetry_path, idempotency_key, body_json, payload_sha256, received_at,
         receiver_schema_version, state, published_at, cache_target, publish_attempts)
    VALUES (v_new_id, v_tenant, p_device_id, p_record_uid, p_record_uid, 'backfill_sqlite',
            p_telemetry_path, p_idempotency_key, p_body_json, v_sha, p_received_at,
            p_receiver_schema_version,
            CASE WHEN p_acknowledged_at IS NULL THEN 'pending' ELSE 'published' END,
            p_acknowledged_at,
            CASE WHEN p_acknowledged_at IS NULL THEN NULL ELSE p_source_ledger END,
            0);

    INSERT INTO hear.backfill_watermarks (source_ledger, rows_imported)
    VALUES (p_source_ledger, 1)
    ON CONFLICT (source_ledger) DO UPDATE
        SET rows_imported = hear.backfill_watermarks.rows_imported + 1,
            updated_at = now();

    RETURN true;
END;
$$;

COMMENT ON FUNCTION hear.backfill_record(text, text, text, text, text, timestamptz, smallint, timestamptz, text, text) IS
    'Imports one SQLite ledger row. Never publishes; an already-cached row arrives as published.';

CREATE OR REPLACE FUNCTION hear.backfill_advance(
    p_source_ledger  text,
    p_last_sqlite_id bigint,
    p_completed      boolean DEFAULT false
)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO hear.backfill_watermarks (source_ledger, last_sqlite_id, completed_at)
    VALUES (p_source_ledger, p_last_sqlite_id, CASE WHEN p_completed THEN now() END)
    ON CONFLICT (source_ledger) DO UPDATE
        SET last_sqlite_id = greatest(hear.backfill_watermarks.last_sqlite_id, p_last_sqlite_id),
            completed_at = CASE WHEN p_completed THEN now() ELSE hear.backfill_watermarks.completed_at END,
            updated_at = now();
$$;

-- The audited defect, made queryable: one record_uid claimed by more than one device. In the
-- SQLite ledger every one of these was a record that existed only for its first device and was
-- silently discarded for the others; here they coexist, and this view is how the operator sees
-- how much of that happened before the cut-over.
CREATE OR REPLACE VIEW hear.legacy_uid_collisions
WITH (security_invoker = true) AS
    SELECT ids.tenant_id,
           ids.record_uid,
           count(*)                       AS device_count,
           array_agg(ids.device_id ORDER BY ids.device_id) AS device_ids,
           min(ids.first_seen_at)         AS first_seen_at,
           max(ids.last_seen_at)          AS last_seen_at
      FROM hear.durable_record_ids ids
     GROUP BY ids.tenant_id, ids.record_uid
    HAVING count(*) > 1;

COMMENT ON VIEW hear.legacy_uid_collisions IS
    'record_uids shared by several devices: rows the unscoped SQLite ledger could not keep.';

-- Same identity, different body. SQLite keeps the first and discards the rest with no trace;
-- here the first still wins, but the disagreement is counted and shows up here.
CREATE OR REPLACE VIEW hear.identity_conflicts
WITH (security_invoker = true) AS
    SELECT ids.tenant_id,
           ids.device_id,
           ids.record_uid,
           ids.conflicting_arrivals,
           ids.duplicate_arrivals,
           ids.first_seen_at,
           ids.last_seen_at
      FROM hear.durable_record_ids ids
     WHERE ids.conflicting_arrivals > 0;

COMMENT ON VIEW hear.identity_conflicts IS
    'Identities that received a second, different body; the first body is the stored one.';

GRANT SELECT ON hear.legacy_uid_collisions, hear.identity_conflicts
    TO hear_durable_auditor, hear_durable_admin;
GRANT SELECT, INSERT, UPDATE ON hear.backfill_watermarks TO hear_durable_admin;
GRANT EXECUTE ON FUNCTION
    hear.backfill_record(text, text, text, text, text, timestamptz, smallint, timestamptz, text, text),
    hear.backfill_advance(text, bigint, boolean)
    TO hear_durable_admin;
