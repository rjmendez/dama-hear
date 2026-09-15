-- Rollback of 0006_hear_durable_refusals.
--
-- Drops the quarantine and everything that maintains it. The refusal rows go with the table:
-- they are evidence about *rejected* input, never accepted telemetry, and nothing outside this
-- file references them -- so unlike the outbox there is nothing here whose loss could be
-- mistaken for losing data a device successfully delivered.
--
-- Nothing outside this file is modified by 0006, so nothing outside it has to be restored here:
-- the quarantine's counters and last-refusal timestamp live in their own tables rather than in
-- hear.durable_counters / hear.durable_events.
DROP VIEW IF EXISTS hear.refused_messages_audit;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['refused_messages', 'refusal_counters', 'refusal_events'] LOOP
        IF to_regclass(format('hear.%I', v_table)) IS NOT NULL THEN
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON hear.%I', v_table);
            EXECUTE format('ALTER TABLE hear.%I DISABLE ROW LEVEL SECURITY', v_table);
        END IF;
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS refused_messages_counters ON hear.refused_messages;

REVOKE ALL ON FUNCTION hear.refusal_health_snapshot(text)
    FROM hear_durable_writer, hear_durable_reader, hear_durable_auditor, hear_durable_admin;

DROP FUNCTION IF EXISTS hear.refusal_health_snapshot(text);
DROP FUNCTION IF EXISTS hear.enforce_refusal_retention(integer, boolean);
DROP FUNCTION IF EXISTS hear.record_refusal(text, text, text, text, text, text, text, boolean, integer, bigint);
DROP FUNCTION IF EXISTS hear.enforce_refusal_bounds(text, integer, bigint);
DROP FUNCTION IF EXISTS hear.tg_refused_messages_counters();
DROP FUNCTION IF EXISTS hear.bump_refusal_counter(text, text, bigint);
DROP TABLE IF EXISTS hear.refused_messages;
DROP TABLE IF EXISTS hear.refusal_counters;
DROP TABLE IF EXISTS hear.refusal_events;
