-- Rollback of 0005_hear_durable_backfill_compat.
--
-- Drops the import surface only. Rows already imported are left in place: they are records, not
-- scaffolding, and deleting them is a retention decision, not a rollback. To undo an import
-- itself, delete WHERE ingest_source = 'backfill_sqlite' -- which is why that value exists.
DROP VIEW IF EXISTS hear.identity_conflicts;
DROP VIEW IF EXISTS hear.legacy_uid_collisions;
DROP FUNCTION IF EXISTS hear.backfill_advance(text, bigint, boolean);
DROP FUNCTION IF EXISTS hear.backfill_record(text, text, text, text, text, timestamptz, smallint, timestamptz, text, text);
DROP TABLE IF EXISTS hear.backfill_watermarks;
