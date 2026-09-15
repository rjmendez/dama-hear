-- 0003_hear_durable_partitions_and_retention
--
-- Partition maintenance and retention. Both are plain functions: this migration schedules
-- nothing and drops nothing at apply time. The operator wires ensure_partitions() to a timer
-- (or the store's existing prune worker) and calls enforce_retention() with --dry-run first.
--
-- Partition granularity is daily, not monthly. The measured record rate is ~51,840 records/day
-- for six nodes at one heartbeat per 10s (~33 MB/day at the measured ~639 B/record), and the
-- default retention in tools/hear_heartbeat_receiver.py is HEAR_DURABLE_RETENTION_DAYS=30, so a
-- monthly partition is the entire retention window: retention could only ever be enforced by
-- deleting rows, which is exactly the vacuum-churn the partitioning is there to avoid. Daily
-- partitions make the steady state ~30 live partitions of ~33 MB and make retention a DROP.

CREATE OR REPLACE FUNCTION hear.partition_name(p_parent text, p_day date)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT format('%s_%s', p_parent, to_char(p_day, 'YYYYMMDD'));
$$;

-- Creates the daily partitions for [today - p_days_back, today + p_days_ahead]. Idempotent, so
-- it is safe on every scheduler tick; the lead time is what keeps a month boundary (or a failed
-- tick) from becoming an ingest failure.
CREATE OR REPLACE FUNCTION hear.ensure_partitions(
    p_days_ahead integer DEFAULT 3,
    p_days_back  integer DEFAULT 1
)
RETURNS TABLE (relation text, created boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    v_day    date;
    v_parent text;
    v_name   text;
BEGIN
    FOREACH v_parent IN ARRAY ARRAY['durable_records', 'cache_attempts'] LOOP
        v_day := (now() AT TIME ZONE 'UTC')::date - greatest(p_days_back, 0);
        WHILE v_day <= (now() AT TIME ZONE 'UTC')::date + greatest(p_days_ahead, 0) LOOP
            v_name := hear.partition_name(v_parent, v_day);
            IF to_regclass(format('hear.%I', v_name)) IS NULL THEN
                EXECUTE format(
                    'CREATE TABLE hear.%I PARTITION OF hear.%I FOR VALUES FROM (%L) TO (%L)',
                    v_name, v_parent,
                    v_day::timestamptz, (v_day + 1)::timestamptz);
                relation := format('hear.%s', v_name);
                created := true;
                RETURN NEXT;
            ELSE
                relation := format('hear.%s', v_name);
                created := false;
                RETURN NEXT;
            END IF;
            v_day := v_day + 1;
        END LOOP;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION hear.ensure_partitions(integer, integer) IS
    'Idempotently creates daily partitions ahead of time; run on a timer, never at apply time.';

-- Rows that landed in a DEFAULT partition because maintenance had stopped. PostgreSQL refuses
-- to create a partition whose range overlaps rows already sitting in the default, so this is
-- the first thing to look at when ensure_partitions() starts failing.
CREATE OR REPLACE FUNCTION hear.unrouted_records(p_limit integer DEFAULT 100)
RETURNS TABLE (record_id bigint, received_at timestamptz, device_id text, state text)
LANGUAGE sql
STABLE
AS $$
    SELECT r.record_id, r.received_at, r.device_id, r.state
      FROM hear.durable_records_unrouted r
     ORDER BY r.received_at
     LIMIT greatest(p_limit, 0);
$$;

-- Retention.
--
-- Two hard rules, both inherited from the SQLite prune this replaces:
--   * a partition holding any record that is not 'published' is never dropped, no matter how
--     old -- a stalled Redis outage cannot be resolved by deleting the evidence;
--   * p_dry_run defaults to true, so the destructive form is always something someone typed.
--
-- The ceiling is 90 days: telemetry is class R0-derived in docs/data-governance.md, whose
-- default maximum is 90 days. The default of 30 days is the value already shipping as
-- HEAR_DURABLE_RETENTION_DAYS. Anything longer needs the written purpose/approver that the
-- governance document requires, and therefore a deliberate change here, not a parameter.
CREATE OR REPLACE FUNCTION hear.enforce_retention(
    p_days    integer DEFAULT 30,
    p_dry_run boolean DEFAULT true
)
RETURNS TABLE (action text, relation text, row_count bigint, reason text)
LANGUAGE plpgsql
AS $$
DECLARE
    v_cutoff    timestamptz;
    v_part      record;
    v_upper     timestamptz;
    v_unpub     bigint;
    v_rows      bigint;
    v_tenant    text := coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default');
    v_pruned    bigint := 0;
BEGIN
    IF p_days <= 0 THEN
        action := 'skipped'; relation := NULL; row_count := 0;
        reason := 'retention disabled (p_days <= 0), ledger keeps every record';
        RETURN NEXT;
        RETURN;
    END IF;
    IF p_days > 90 THEN
        RAISE EXCEPTION
            'retention of % days exceeds the R0-derived ceiling of 90 days (docs/data-governance.md)',
            p_days;
    END IF;

    v_cutoff := now() - make_interval(days => p_days);

    FOR v_part IN
        SELECT c.oid,
               parent.relname AS parent_name,
               c.relname      AS part_name,
               pg_get_expr(c.relpartbound, c.oid) AS bound
          FROM pg_class c
          JOIN pg_inherits i ON i.inhrelid = c.oid
          JOIN pg_class parent ON parent.oid = i.inhparent
          JOIN pg_namespace n ON n.oid = parent.relnamespace
         WHERE n.nspname = 'hear'
           AND parent.relname IN ('durable_records', 'cache_attempts')
         ORDER BY parent.relname, c.relname
    LOOP
        IF v_part.bound = 'DEFAULT' THEN
            action := 'refused'; relation := format('hear.%s', v_part.part_name);
            row_count := 0;
            reason := 'default partition is never dropped; route its rows with ensure_partitions() first';
            RETURN NEXT;
            CONTINUE;
        END IF;

        v_upper := (substring(v_part.bound from $re$TO \('([^']+)'\)$re$))::timestamptz;
        IF v_upper IS NULL OR v_upper > v_cutoff THEN
            CONTINUE;
        END IF;

        IF v_part.parent_name = 'durable_records' THEN
            EXECUTE format('SELECT count(*) FROM hear.%I WHERE state <> %L',
                           v_part.part_name, 'published')
               INTO v_unpub;
            IF v_unpub > 0 THEN
                action := 'refused'; relation := format('hear.%s', v_part.part_name);
                row_count := v_unpub;
                reason := 'partition still holds unpublished records';
                RETURN NEXT;
                CONTINUE;
            END IF;
        END IF;

        EXECUTE format('SELECT count(*) FROM hear.%I', v_part.part_name) INTO v_rows;

        IF p_dry_run THEN
            action := 'would_drop';
        ELSE
            EXECUTE format('DROP TABLE hear.%I', v_part.part_name);
            action := 'dropped';
            IF v_part.parent_name = 'durable_records' THEN
                v_pruned := v_pruned + v_rows;
            END IF;
        END IF;
        relation := format('hear.%s', v_part.part_name);
        row_count := v_rows;
        reason := format('older than %s days', p_days);
        RETURN NEXT;
    END LOOP;

    -- Identity rows are the deduplication horizon: they must outlive nothing and must not
    -- outlive their record, or a replayed old duplicate would be accepted as new.
    IF p_dry_run THEN
        SELECT count(*) INTO v_rows
          FROM hear.durable_record_ids ids
         WHERE ids.received_at < v_cutoff
           AND NOT EXISTS (SELECT 1 FROM hear.durable_records r
                            WHERE r.record_id = ids.record_id AND r.received_at = ids.received_at);
        action := 'would_drop';
    ELSE
        WITH gone AS (
            DELETE FROM hear.durable_record_ids ids
             WHERE ids.received_at < v_cutoff
               AND NOT EXISTS (SELECT 1 FROM hear.durable_records r
                                WHERE r.record_id = ids.record_id AND r.received_at = ids.received_at)
            RETURNING 1
        )
        SELECT count(*) INTO v_rows FROM gone;
        action := 'deleted';
    END IF;
    relation := 'hear.durable_record_ids';
    row_count := v_rows;
    reason := 'identity rows whose record partition is gone';
    RETURN NEXT;

    IF NOT p_dry_run THEN
        PERFORM hear.bump_counter(v_tenant, 'records_pruned', v_pruned);
        INSERT INTO hear.durable_events (tenant_id, last_prune_run_at, last_retention_days)
        VALUES (v_tenant, now(), p_days)
        ON CONFLICT (tenant_id) DO UPDATE
            SET last_prune_run_at = now(), last_retention_days = p_days;
    END IF;
END;
$$;

COMMENT ON FUNCTION hear.enforce_retention(integer, boolean) IS
    'Drops aged, fully published partitions. Dry-run by default; refuses partitions with pending rows.';
