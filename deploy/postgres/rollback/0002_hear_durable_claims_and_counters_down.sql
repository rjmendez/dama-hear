-- Rollback of 0002_hear_durable_claims_and_counters.
--
-- Drops the behaviour, keeps the records. After this the tables are inert storage again, which
-- is the correct state to roll back *to* while HEAR_DURABLE_STORE is flipped back to sqlite.
DROP TRIGGER IF EXISTS cache_attempts_counters ON hear.cache_attempts;
DROP TRIGGER IF EXISTS durable_records_counters ON hear.durable_records;
DROP FUNCTION IF EXISTS hear.tg_cache_attempts_counters();
DROP FUNCTION IF EXISTS hear.tg_durable_records_counters();
DROP FUNCTION IF EXISTS hear.verify_payload_integrity(timestamptz, integer);
DROP FUNCTION IF EXISTS hear.health_snapshot(text);
DROP FUNCTION IF EXISTS hear.release_expired_claims(text);
DROP FUNCTION IF EXISTS hear.mark_failed(bigint, timestamptz, text, text, integer, integer);
DROP FUNCTION IF EXISTS hear.mark_published(bigint, timestamptz, text);
DROP FUNCTION IF EXISTS hear.claim_pending(text, integer, integer, text, timestamptz);
DROP FUNCTION IF EXISTS hear.persist_record(text, text, text, text, timestamptz, smallint, text, text, text);
DROP FUNCTION IF EXISTS hear.bump_counter(text, text, bigint);
