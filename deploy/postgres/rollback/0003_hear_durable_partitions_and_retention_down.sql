-- Rollback of 0003_hear_durable_partitions_and_retention.
--
-- Drops the maintenance functions. Partitions they created are data and stay; the parent
-- tables keep working, they simply stop gaining new daily partitions, and new rows land in the
-- DEFAULT partition instead of failing.
DROP FUNCTION IF EXISTS hear.enforce_retention(integer, boolean);
DROP FUNCTION IF EXISTS hear.unrouted_records(integer);
DROP FUNCTION IF EXISTS hear.ensure_partitions(integer, integer);
DROP FUNCTION IF EXISTS hear.partition_name(text, date);
