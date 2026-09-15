-- Rollback of 0006_hear_durable_refusals.
--
-- Drops the quarantine and everything that maintains it. The refusal rows go with the table:
-- they are evidence about *rejected* input, never accepted telemetry, and nothing outside this
-- file references them -- so unlike the outbox there is nothing here whose loss could be
-- mistaken for losing data a device successfully delivered.
--
-- durable_events.last_refusal_at is deliberately left in place: a column added with
-- ADD COLUMN IF NOT EXISTS is inert once nothing writes it, and dropping a column from a live
-- table shared with the outbox is a far bigger act than reversing this migration.
DROP VIEW IF EXISTS hear.refused_messages_audit;

DO $$
BEGIN
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON hear.refused_messages';
EXCEPTION
    WHEN undefined_table THEN NULL;
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
DROP TABLE IF EXISTS hear.refused_messages;
