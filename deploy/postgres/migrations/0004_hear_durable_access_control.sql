-- 0004_hear_durable_access_control
--
-- Roles, tenant isolation and the redacted read surfaces.
--
-- docs/data-governance.md requires a tenant ID on every object with server-side enforcement,
-- and names the roles this maps onto: operator (view operational telemetry), auditor (read
-- audit records without changing content), administrator (manage infrastructure, not content).
-- This migration creates group roles only -- NOLOGIN, no passwords, nothing environment
-- specific. The deployment creates its own login roles and grants them membership.
--
-- Nobody except the administrator can DELETE. Retention is the only sanctioned deletion path
-- and it runs as the administrator; a compromised or buggy writer cannot erase the ledger it
-- writes to.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hear_durable_writer') THEN
        CREATE ROLE hear_durable_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hear_durable_reader') THEN
        CREATE ROLE hear_durable_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hear_durable_auditor') THEN
        CREATE ROLE hear_durable_auditor NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hear_durable_admin') THEN
        CREATE ROLE hear_durable_admin NOLOGIN;
    END IF;
END;
$$;

COMMENT ON ROLE hear_durable_writer IS 'Receiver/bridge outbox writer: persist, claim, publish, fail. No DELETE.';
COMMENT ON ROLE hear_durable_reader IS 'Operator telemetry read: redacted views and health only.';
COMMENT ON ROLE hear_durable_auditor IS 'Audit read: attempt history and record metadata, never payload bodies.';
COMMENT ON ROLE hear_durable_admin  IS 'Schema, partition maintenance and retention.';

-- ---------------------------------------------------------------------------------------------
-- Tenant isolation
-- ---------------------------------------------------------------------------------------------
--
-- The tenant is a session setting (hear.tenant_id), not an application filter, so a caller that
-- forgets a WHERE clause still cannot read another tenant's rows. The current single-site
-- deployment runs entirely as tenant 'default', which the policy treats as the fallback, so
-- enabling this changes nothing operationally today and is already in place when a second
-- tenant appears.
ALTER TABLE hear.durable_records    ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.durable_record_ids ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.cache_attempts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.durable_counters   ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.durable_events     ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['durable_records', 'durable_record_ids', 'cache_attempts',
                                   'durable_counters', 'durable_events'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_policies
                        WHERE schemaname = 'hear' AND tablename = v_table
                          AND policyname = 'tenant_isolation') THEN
            EXECUTE format($ddl$
                CREATE POLICY tenant_isolation ON hear.%I
                    USING (tenant_id = coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default'))
                    WITH CHECK (tenant_id = coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default'))
            $ddl$, v_table);
        END IF;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------------------------
-- Redacted read surfaces
-- ---------------------------------------------------------------------------------------------
--
-- Today's firmware sends `gps: {"fix": n}` and no coordinates (see validate_heartbeat_payload
-- and tests/test_firmware_heartbeat_push.py), so nothing is being stripped yet. The view exists
-- so that the day a node starts reporting a position, the operator read path does not silently
-- become a coordinate feed: the repository already treats coordinates as a controlled class
-- (tools/coord_guard.py), and a redacted default is the only version of that which survives a
-- firmware change nobody remembered to re-review.
--
-- security_invoker: the view is evaluated with the *caller's* rights, so row-level tenant
-- isolation still applies through it (PostgreSQL 15+).
CREATE OR REPLACE VIEW hear.durable_records_operator
WITH (security_invoker = true) AS
    SELECT r.record_id,
           r.tenant_id,
           r.device_id,
           r.record_uid,
           r.ingest_source,
           r.telemetry_path,
           r.received_at,
           r.state,
           r.publish_attempts,
           r.published_at,
           r.dead_lettered_at,
           r.last_error,
           ((r.payload #- '{gps,lat}') #- '{gps,lon}') #- '{gps,alt_m}' AS payload
      FROM hear.durable_records r;

COMMENT ON VIEW hear.durable_records_operator IS
    'Operator read surface: record state plus payload with any GPS coordinates removed.';

CREATE OR REPLACE VIEW hear.durable_records_audit
WITH (security_invoker = true) AS
    SELECT r.record_id,
           r.tenant_id,
           r.device_id,
           r.record_uid,
           r.legacy_record_uid,
           r.ingest_source,
           r.telemetry_path,
           r.received_at,
           r.created_at,
           r.state,
           r.publish_attempts,
           r.next_attempt_at,
           r.claimed_by,
           r.claim_expires_at,
           r.published_at,
           r.dead_lettered_at,
           r.last_error,
           encode(r.payload_sha256, 'hex') AS payload_sha256_hex
      FROM hear.durable_records r;

COMMENT ON VIEW hear.durable_records_audit IS
    'Audit read surface: custody metadata and payload digest, never the payload body.';

-- ---------------------------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON SCHEMA hear TO hear_durable_writer, hear_durable_reader,
                              hear_durable_auditor, hear_durable_admin;

GRANT SELECT, INSERT, UPDATE ON hear.durable_records, hear.durable_record_ids,
                                hear.durable_counters, hear.durable_events
    TO hear_durable_writer;
GRANT SELECT, INSERT ON hear.cache_attempts TO hear_durable_writer;
GRANT USAGE ON SEQUENCE hear.durable_records_record_id_seq TO hear_durable_writer;
GRANT SELECT ON hear.schema_migrations TO hear_durable_writer, hear_durable_reader,
                                          hear_durable_auditor, hear_durable_admin;
GRANT EXECUTE ON FUNCTION
    hear.persist_record(text, text, text, text, timestamptz, smallint, text, text, text),
    hear.claim_pending(text, integer, integer, text, timestamptz),
    hear.mark_published(bigint, timestamptz, text),
    hear.mark_failed(bigint, timestamptz, text, text, integer, integer),
    hear.release_expired_claims(text),
    hear.bump_counter(text, text, bigint),
    hear.health_snapshot(text)
    TO hear_durable_writer;

GRANT SELECT ON hear.durable_records_operator TO hear_durable_reader;
GRANT EXECUTE ON FUNCTION hear.health_snapshot(text) TO hear_durable_reader, hear_durable_auditor;

GRANT SELECT ON hear.durable_records_audit TO hear_durable_auditor;
GRANT SELECT ON hear.cache_attempts, hear.durable_counters, hear.durable_events
    TO hear_durable_auditor;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hear TO hear_durable_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA hear TO hear_durable_admin;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA hear TO hear_durable_admin;

-- New daily partitions must inherit the same grants, or the writer starts failing at the first
-- partition it did not exist for.
ALTER DEFAULT PRIVILEGES IN SCHEMA hear
    GRANT SELECT, INSERT, UPDATE ON TABLES TO hear_durable_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA hear
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hear_durable_admin;
