-- Rollback of 0004_hear_durable_access_control.
--
-- Roles are NOT dropped: another database in the same cluster may have granted them, and
-- DROP ROLE fails while any grant remains. Revoking and dropping the objects is the reversal;
-- removing the roles is a separate, deliberate cluster-level act.
DROP VIEW IF EXISTS hear.durable_records_audit;
DROP VIEW IF EXISTS hear.durable_records_operator;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['durable_records', 'durable_record_ids', 'cache_attempts',
                                   'durable_counters', 'durable_events'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON hear.%I', v_table);
        EXECUTE format('ALTER TABLE hear.%I DISABLE ROW LEVEL SECURITY', v_table);
    END LOOP;
END;
$$;

ALTER DEFAULT PRIVILEGES IN SCHEMA hear
    REVOKE SELECT, INSERT, UPDATE ON TABLES FROM hear_durable_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA hear
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM hear_durable_admin;

REVOKE ALL ON ALL TABLES IN SCHEMA hear FROM hear_durable_writer, hear_durable_reader,
                                             hear_durable_auditor, hear_durable_admin;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA hear FROM hear_durable_writer, hear_durable_admin;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA hear FROM hear_durable_writer, hear_durable_reader,
                                                hear_durable_auditor, hear_durable_admin;
REVOKE USAGE ON SCHEMA hear FROM hear_durable_writer, hear_durable_reader,
                                 hear_durable_auditor, hear_durable_admin;
