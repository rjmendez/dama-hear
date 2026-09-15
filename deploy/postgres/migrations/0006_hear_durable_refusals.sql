-- 0006_hear_durable_refusals
--
-- The refusal quarantine (risk register R4), so the Postgres generation keeps the observability
-- the SQLite ledger gained instead of losing it at cut-over.
--
-- A message that fails validation used to leave nothing behind but a log line: a node whose
-- firmware sends a shape this build rejects simply disappeared. The receiver now records every
-- refusal it is willing to store (tools/hear_heartbeat_receiver.py, record_refusal); this is the
-- same ledger on the server side.
--
-- What this is NOT:
--   * not an outbox. A refusal has no record_uid in hear.durable_records' namespace, is never
--     claimed, never published to Redis and never replayed. Nothing here references, or is
--     referenced by, the outbox tables, so no statement in this file -- or in the retention
--     function it installs -- can reach an accepted record;
--   * not part of hear.health_snapshot(). That key set is the cross-store durable contract
--     (tests/test_hear_durable_pg_schema.py::test_health_returns_the_sqlite_key_set) and it stays
--     exactly as it is. Refusal observability has its own function below, mirroring the
--     DurableRecordStore.refusal_health() seam key for key.
--
-- Capacity. This table accepts input that by definition failed validation, so it cannot be
-- trusted to be small, unique or infrequent: any publisher on the shared topic can mint distinct
-- rejected bodies. It is therefore a *bounded ring*, not an append-only ledger -- the same three
-- bounds the SQLite store enforces (per-row body cap, hard row/byte caps, per-source arrival rate
-- limit), with enforce_refusal_bounds() below as the server-side backstop. Bounded size is also
-- why this table is not partitioned: at the shipped caps (5,000 rows / 16 MiB) a daily partition
-- set would cost more maintenance than it saves, and retention here is a DELETE of a capped
-- table, not the partition DROP that hear.enforce_retention() performs on the outbox.

CREATE TABLE IF NOT EXISTS hear.refused_messages (
    refusal_id      bigint      GENERATED ALWAYS AS IDENTITY,
    tenant_id       text        NOT NULL DEFAULT coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default'),
    -- sha256 over (telemetry_path, device_id, source, reason, body_text): a node stuck resending
    -- one rejected body bumps occurrences instead of adding a row.
    refusal_uid     text        NOT NULL,
    telemetry_path  text        NOT NULL CHECK (telemetry_path IN ('hear/heartbeat', 'hear/event')),
    -- Best effort only, and never trusted as identity: for an MQTT refusal this is the topic
    -- segment (the one identity the broker ties to the publishing connection), for an HTTP one
    -- the claimed device_id, and 'unknown' when neither could be read.
    device_id       text        NOT NULL,
    source          text        NOT NULL CHECK (source IN ('lan_http', 'mqtt_bridge')),
    reason          text        NOT NULL CHECK (char_length(reason) <= 512),
    -- The bytes as received, truncated to HEAR_REFUSAL_MAX_BODY_CHARS. Deliberately text and not
    -- jsonb: a refused body is frequently not valid JSON, and repairing it would destroy the only
    -- thing this row exists to preserve.
    body_text       text        NOT NULL,
    body_bytes      integer     NOT NULL CHECK (body_bytes >= 0),
    truncated       boolean     NOT NULL DEFAULT false,
    occurrences     bigint      NOT NULL DEFAULT 1 CHECK (occurrences > 0),
    received_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, refusal_uid)
);

COMMENT ON TABLE hear.refused_messages IS
    'Quarantined messages that failed validation. Evidence, never replay material; bounded ring.';

CREATE INDEX IF NOT EXISTS refused_messages_received
    ON hear.refused_messages (tenant_id, received_at DESC, refusal_id DESC);
CREATE INDEX IF NOT EXISTS refused_messages_source
    ON hear.refused_messages (tenant_id, source, device_id, refusal_id);

-- Counters and the last-refusal timestamp get their own tables rather than extending
-- hear.durable_counters / hear.durable_events. Those objects belong to the outbox: widening their
-- CHECK constraint or their column list would make this migration a rewrite of 0001's contract
-- and its rollback a partial restore of it. Additive tables keep 0006 reversible by dropping only
-- what it created, and keep the quarantine's blast radius inside the quarantine.
CREATE TABLE IF NOT EXISTS hear.refusal_counters (
    tenant_id  text        NOT NULL,
    counter    text        NOT NULL
        CHECK (counter IN ('messages_refused', 'messages_refused_repeat', 'refusals_evicted')),
    value      bigint      NOT NULL DEFAULT 0 CHECK (value >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, counter)
);

COMMENT ON TABLE hear.refusal_counters IS
    'Monotonic quarantine counters, read in O(1) by hear.refusal_health_snapshot().';

CREATE TABLE IF NOT EXISTS hear.refusal_events (
    tenant_id       text        NOT NULL,
    last_refusal_at timestamptz,
    PRIMARY KEY (tenant_id)
);

COMMENT ON TABLE hear.refusal_events IS
    'Last-occurrence timestamp behind refusal_health(); the outbox equivalent is hear.durable_events.';

CREATE OR REPLACE FUNCTION hear.bump_refusal_counter(p_tenant_id text, p_counter text,
                                                     p_delta bigint DEFAULT 1)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO hear.refusal_counters (tenant_id, counter, value, updated_at)
    VALUES (p_tenant_id, p_counter, greatest(p_delta, 0), now())
    ON CONFLICT (tenant_id, counter) DO UPDATE
        SET value = hear.refusal_counters.value + greatest(p_delta, 0),
            updated_at = now();
$$;

-- Counters are maintained by a trigger, not by record_refusal(), so a writer that inserts
-- directly still cannot make the refusal health surface lie.
CREATE OR REPLACE FUNCTION hear.tg_refused_messages_counters()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM hear.bump_refusal_counter(NEW.tenant_id, 'messages_refused', 1);
        INSERT INTO hear.refusal_events (tenant_id, last_refusal_at)
        VALUES (NEW.tenant_id, NEW.received_at)
        ON CONFLICT (tenant_id) DO UPDATE SET last_refusal_at = EXCLUDED.last_refusal_at;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.occurrences > OLD.occurrences THEN
            PERFORM hear.bump_refusal_counter(NEW.tenant_id, 'messages_refused_repeat',
                                              NEW.occurrences - OLD.occurrences);
            INSERT INTO hear.refusal_events (tenant_id, last_refusal_at)
            VALUES (NEW.tenant_id, NEW.received_at)
            ON CONFLICT (tenant_id) DO UPDATE SET last_refusal_at = EXCLUDED.last_refusal_at;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM hear.bump_refusal_counter(OLD.tenant_id, 'refusals_evicted', 1);
    END IF;
    RETURN NULL;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'hear.refused_messages'::regclass
                      AND tgname = 'refused_messages_counters') THEN
        CREATE TRIGGER refused_messages_counters
            AFTER INSERT OR UPDATE OR DELETE ON hear.refused_messages
            FOR EACH ROW EXECUTE FUNCTION hear.tg_refused_messages_counters();
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------------------------
-- record
-- ---------------------------------------------------------------------------------------------
--
-- Insert-or-bump plus bound enforcement in one statement-level call, so a caller is either told a
-- refusal is stored and it is, or nothing changed. Returns NULL when this very refusal was the
-- row the bounds evicted (a body larger than the byte cap, or a cap of zero): the message is
-- refused either way, and saying so beats implying evidence exists.
CREATE OR REPLACE FUNCTION hear.record_refusal(
    p_tenant_id      text,
    p_refusal_uid    text,
    p_telemetry_path text,
    p_device_id      text,
    p_source         text,
    p_reason         text,
    p_body_text      text,
    p_truncated      boolean DEFAULT false,
    p_max_rows       integer DEFAULT 5000,
    p_max_bytes      bigint  DEFAULT 16777216
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO hear.refused_messages
        (tenant_id, refusal_uid, telemetry_path, device_id, source, reason,
         body_text, body_bytes, truncated, received_at)
    VALUES (p_tenant_id, p_refusal_uid, p_telemetry_path, p_device_id, p_source,
            left(p_reason, 512), p_body_text, octet_length(p_body_text), p_truncated, now())
    ON CONFLICT (tenant_id, refusal_uid) DO UPDATE
        SET occurrences = hear.refused_messages.occurrences + 1,
            received_at = now()
    RETURNING refusal_id INTO v_id;

    PERFORM hear.enforce_refusal_bounds(p_tenant_id, p_max_rows, p_max_bytes);

    IF NOT EXISTS (SELECT 1 FROM hear.refused_messages
                    WHERE tenant_id = p_tenant_id AND refusal_id = v_id) THEN
        RETURN NULL;
    END IF;
    RETURN v_id;
END;
$$;

COMMENT ON FUNCTION hear.record_refusal(text, text, text, text, text, text, text, boolean, integer, bigint) IS
    'Quarantines one refused message and re-applies the ring bounds in the same transaction.';

-- ---------------------------------------------------------------------------------------------
-- bounds
-- ---------------------------------------------------------------------------------------------
--
-- Age-independent caps. Without these the only limit is retention, so any publisher able to reach
-- the topic can mint unbounded distinct rejected bodies and fill the volume the *accepted*
-- telemetry shares -- turning rejected input into an availability failure.
--
-- Eviction always takes the oldest row of whichever (source, device_id) holds the most rows, so a
-- flooding publisher evicts its own history before anybody else's, and one loud node cannot erase
-- the evidence of a quiet one. Only hear.refused_messages is touched; accepted records are not
-- reachable from here.
-- SECURITY DEFINER, deliberately: 0004's rule is that the writer role may never DELETE, because
-- retention must be the only sanctioned deletion path for *records*. The ring still has to evict,
-- so the eviction lives in one auditable function that can only ever delete from
-- hear.refused_messages and only within the tenant it is given -- rather than handing the writer
-- a DELETE grant it could point anywhere. search_path is pinned so the body cannot be resolved
-- against a caller-controlled schema.
CREATE OR REPLACE FUNCTION hear.enforce_refusal_bounds(
    p_tenant_id text,
    p_max_rows  integer DEFAULT 5000,
    p_max_bytes bigint  DEFAULT 16777216
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, hear
AS $$
DECLARE
    v_rows    bigint;
    v_bytes   bigint;
    v_victim  bigint;
    v_evicted bigint := 0;
BEGIN
    LOOP
        SELECT count(*), coalesce(sum(body_bytes), 0)
          INTO v_rows, v_bytes
          FROM hear.refused_messages
         WHERE tenant_id = p_tenant_id;
        EXIT WHEN v_rows = 0;
        EXIT WHEN v_rows <= greatest(p_max_rows, 0) AND v_bytes <= greatest(p_max_bytes, 0);

        SELECT r.refusal_id INTO v_victim
          FROM hear.refused_messages r
         WHERE r.tenant_id = p_tenant_id
           AND (r.source, r.device_id) = (
                SELECT g.source, g.device_id
                  FROM hear.refused_messages g
                 WHERE g.tenant_id = p_tenant_id
                 GROUP BY g.source, g.device_id
                 ORDER BY count(*) DESC, max(g.refusal_id) DESC
                 LIMIT 1)
         ORDER BY r.refusal_id
         LIMIT 1;
        EXIT WHEN v_victim IS NULL;

        DELETE FROM hear.refused_messages
         WHERE tenant_id = p_tenant_id AND refusal_id = v_victim;
        v_evicted := v_evicted + 1;
    END LOOP;
    RETURN v_evicted;
END;
$$;

COMMENT ON FUNCTION hear.enforce_refusal_bounds(text, integer, bigint) IS
    'Hard row/byte ring bounds on the quarantine, independent of age. Never touches the outbox.';

-- Age-based sweep, so quarantined evidence is not kept longer than the telemetry it came from.
-- Same 90-day R0 ceiling and same dry-run-by-default posture as hear.enforce_retention().
CREATE OR REPLACE FUNCTION hear.enforce_refusal_retention(
    p_days    integer DEFAULT 30,
    p_dry_run boolean DEFAULT true
)
RETURNS TABLE (action text, row_count bigint, reason text)
LANGUAGE plpgsql
AS $$
DECLARE
    v_tenant text := coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default');
    v_cutoff timestamptz;
    v_rows   bigint;
BEGIN
    IF p_days <= 0 THEN
        action := 'skipped'; row_count := 0;
        reason := 'refusal retention disabled (p_days <= 0); the ring bounds still apply';
        RETURN NEXT;
        RETURN;
    END IF;
    IF p_days > 90 THEN
        RAISE EXCEPTION
            'retention of % days exceeds the R0-derived ceiling of 90 days (docs/data-governance.md)',
            p_days;
    END IF;

    v_cutoff := now() - make_interval(days => p_days);
    SELECT count(*) INTO v_rows
      FROM hear.refused_messages
     WHERE tenant_id = v_tenant AND received_at < v_cutoff;

    IF p_dry_run THEN
        action := 'would_delete'; row_count := v_rows;
        reason := format('refusals older than %s days', p_days);
        RETURN NEXT;
        RETURN;
    END IF;

    DELETE FROM hear.refused_messages
     WHERE tenant_id = v_tenant AND received_at < v_cutoff;
    action := 'deleted'; row_count := v_rows;
    reason := format('refusals older than %s days', p_days);
    RETURN NEXT;
END;
$$;

-- ---------------------------------------------------------------------------------------------
-- health
-- ---------------------------------------------------------------------------------------------
--
-- Mirrors DurableRecordStore.refusal_health() key for key. Kept separate from
-- hear.health_snapshot() on purpose: the durable health contract is what every backend must
-- implement, and a backend that cannot store refusals must not be made non-compliant by this.
CREATE OR REPLACE FUNCTION hear.refusal_health_snapshot(p_tenant_id text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    WITH scope AS (
        SELECT coalesce(p_tenant_id, nullif(current_setting('hear.tenant_id', true), ''), 'default') AS tenant_id
    ),
    counters AS (
        SELECT c.counter, c.value FROM hear.refusal_counters c, scope s
         WHERE c.tenant_id = s.tenant_id
    ),
    stored AS (
        SELECT count(*) AS n, coalesce(sum(r.body_bytes), 0) AS bytes
          FROM hear.refused_messages r, scope s
         WHERE r.tenant_id = s.tenant_id
    )
    SELECT jsonb_build_object(
        'backend', 'postgres',
        'enabled', true,
        'tenant_id', (SELECT tenant_id FROM scope),
        'refused_messages', (SELECT n FROM stored),
        'refused_bytes', (SELECT bytes FROM stored),
        'last_refusal_at', (SELECT e.last_refusal_at FROM hear.refusal_events e, scope s
                             WHERE e.tenant_id = s.tenant_id),
        'evicted_refusals', coalesce((SELECT value FROM counters WHERE counter = 'refusals_evicted'), 0),
        'repeat_refusals', coalesce((SELECT value FROM counters WHERE counter = 'messages_refused_repeat'), 0)
    );
$$;

COMMENT ON FUNCTION hear.refusal_health_snapshot(text) IS
    'Refusal quarantine observability; the Postgres side of DurableRecordStore.refusal_health().';

-- ---------------------------------------------------------------------------------------------
-- access control
-- ---------------------------------------------------------------------------------------------
--
-- Same posture as 0004: tenant isolation server-side, and the auditor reads refusals *without*
-- the body. A refused body is unvalidated device input -- it may contain anything the publisher
-- put there -- so the default read surface excludes it and only the writer/admin can see it.
ALTER TABLE hear.refused_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.refusal_counters  ENABLE ROW LEVEL SECURITY;
ALTER TABLE hear.refusal_events    ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['refused_messages', 'refusal_counters', 'refusal_events'] LOOP
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

CREATE OR REPLACE VIEW hear.refused_messages_audit
WITH (security_invoker = true) AS
    SELECT r.refusal_id,
           r.tenant_id,
           r.device_id,
           r.source,
           r.telemetry_path,
           r.reason,
           r.body_bytes,
           r.truncated,
           r.occurrences,
           r.received_at
      FROM hear.refused_messages r;

COMMENT ON VIEW hear.refused_messages_audit IS
    'Refusal metadata without the quarantined body; the auditor/operator read surface.';

GRANT SELECT, INSERT, UPDATE ON hear.refused_messages TO hear_durable_writer;
GRANT SELECT, INSERT, UPDATE ON hear.refusal_counters, hear.refusal_events TO hear_durable_writer;
GRANT SELECT ON hear.refusal_counters, hear.refusal_events
    TO hear_durable_reader, hear_durable_auditor, hear_durable_admin;
GRANT EXECUTE ON FUNCTION hear.bump_refusal_counter(text, text, bigint) TO hear_durable_writer;
GRANT SELECT, DELETE ON hear.refused_messages TO hear_durable_admin;
GRANT SELECT ON hear.refused_messages_audit TO hear_durable_reader, hear_durable_auditor;
GRANT EXECUTE ON FUNCTION hear.record_refusal(text, text, text, text, text, text, text, boolean, integer, bigint)
    TO hear_durable_writer;
GRANT EXECUTE ON FUNCTION hear.enforce_refusal_bounds(text, integer, bigint)
    TO hear_durable_writer, hear_durable_admin;
GRANT EXECUTE ON FUNCTION hear.enforce_refusal_retention(integer, boolean) TO hear_durable_admin;
GRANT EXECUTE ON FUNCTION hear.refusal_health_snapshot(text)
    TO hear_durable_writer, hear_durable_reader, hear_durable_auditor, hear_durable_admin;
