-- Rollback of 0001_hear_durable_baseline.
--
-- DESTRUCTIVE: drops the schema and every record in it. Only valid while Postgres has never
-- been the durable store for a workload, or after the records in it have been exported --
-- during the migration sequence that is step M2, before any writer is pointed at it. The
-- SQLite ledgers are untouched by this file and remain the source of truth throughout.
DROP SCHEMA IF EXISTS hear CASCADE;
