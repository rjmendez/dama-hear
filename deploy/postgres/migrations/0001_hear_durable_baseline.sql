-- 0001_hear_durable_baseline
--
-- The canonical Postgres shape of the durable heartbeat/event outbox that
-- tools/hear_heartbeat_receiver.py currently keeps in SQLite. Additive only: it creates a new
-- `hear` schema and nothing else in any database it is applied to. Applying it changes no
-- running workload -- HEAR_DURABLE_STORE=postgres is still refused by make_durable_store(),
-- so a cluster can carry this schema for as long as the soak gate needs before any writer
-- points at it.
--
-- Why the identity table exists (hear.durable_record_ids)
-- -------------------------------------------------------
-- SQLite enforces idempotency with `record_uid TEXT NOT NULL UNIQUE` over the whole ledger.
-- Postgres cannot reproduce that on a RANGE-partitioned table: a unique constraint on a
-- partitioned table must contain the partition key, so `UNIQUE (device_id, record_uid,
-- received_at)` would only deduplicate *within one partition* and a duplicate arriving after a
-- partition boundary would insert a second row -- a silent regression against SQLite. The
-- dedupe key therefore lives in its own small, unpartitioned table whose primary key is
-- (tenant_id, device_id, record_uid), while the bulky record body stays partitioned by arrival
-- time so retention remains a partition DROP. Both are written in one transaction by
-- hear.persist_record() (migration 0002).
--
-- Device-scoped identity (audit defect D1)
-- ----------------------------------------
-- The SQLite ledger keys on the bare record_uid, which for a payload carrying
-- `idempotency_key` is the device's own opaque string. Two devices that emit the same key
-- collide: the second device's record is discarded by INSERT OR IGNORE and never reaches
-- Redis. Here the dedupe key is (tenant_id, device_id, record_uid), so the collision cannot
-- happen, and the Python seam does not change: persist() already receives the full record and
-- can read record["device_id"] itself.
--
-- Requires PostgreSQL 15 or newer (validated on 16).

CREATE SCHEMA IF NOT EXISTS hear;

COMMENT ON SCHEMA hear IS
    'DAMA Hear telemetry durability: heartbeat/event outbox records, publish attempts and counters.';

-- Applied-migration ledger. Every file in deploy/postgres/migrations records exactly one row
-- here, keyed by its numeric version, with the sha256 of the file as applied.
CREATE TABLE IF NOT EXISTS hear.schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text        NOT NULL DEFAULT current_user
);

COMMENT ON TABLE hear.schema_migrations IS
    'One row per applied migration file; checksum is sha256 of the file contents as applied.';

-- ---------------------------------------------------------------------------------------------
-- Records
-- ---------------------------------------------------------------------------------------------
--
CREATE SEQUENCE IF NOT EXISTS hear.durable_records_record_id_seq AS bigint;

-- body_json is the authoritative column, not payload. The bytes published to
-- dama:hear:{node} / dama:hear:latest / the dama:hear:events stream are exactly the bytes the
-- receiver produced with json.dumps(sort_keys=True, separators=(",",":")); jsonb normalises
-- numbers and key order, so replaying a re-serialised jsonb value could change a cached body
-- that the Phase 0 contract freeze declares frozen. body_json is stored verbatim and replayed
-- verbatim; payload is a derived, query-only projection.
CREATE TABLE IF NOT EXISTS hear.durable_records (
    record_id               bigint      NOT NULL
        DEFAULT nextval('hear.durable_records_record_id_seq'),
    tenant_id               text        NOT NULL
        DEFAULT coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default')
        CHECK (tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'),
    device_id               text        NOT NULL
        CHECK (device_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'),
    record_uid              text        NOT NULL CHECK (record_uid <> ''),
    -- Set only by the SQLite backfill, which rewrites an unscoped uid to the scoped form and
    -- keeps the original here so historical rows stay traceable to the ledger they came from.
    legacy_record_uid       text,
    ingest_source           text        NOT NULL
        CHECK (ingest_source IN ('lan_http', 'mqtt_bridge', 'backfill_sqlite')),
    telemetry_path          text        NOT NULL
        CHECK (telemetry_path IN ('hear/heartbeat', 'hear/event')),
    idempotency_key         text,
    body_json               text        NOT NULL,
    payload                 jsonb       GENERATED ALWAYS AS (body_json::jsonb) STORED,
    payload_sha256          bytea       NOT NULL CHECK (octet_length(payload_sha256) = 32),
    received_at             timestamptz NOT NULL,
    receiver_schema_version smallint    NOT NULL CHECK (receiver_schema_version > 0),
    durable_schema_version  smallint    NOT NULL DEFAULT 2 CHECK (durable_schema_version > 0),
    state                   text        NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'claimed', 'published', 'dead_letter')),
    publish_attempts        integer     NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    next_attempt_at         timestamptz NOT NULL DEFAULT now(),
    claimed_by              text,
    claimed_at              timestamptz,
    claim_expires_at        timestamptz,
    cache_target            text,
    published_at            timestamptz,
    dead_lettered_at        timestamptz,
    -- _error_text() truncates to 512 characters; the column refuses anything longer so a
    -- backend swap cannot quietly start storing unbounded driver tracebacks.
    last_error              text        CHECK (last_error IS NULL OR char_length(last_error) <= 512),
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (received_at, record_id),
    -- Integrity is enforced, not asserted: a body that does not hash to its own digest is
    -- rejected at INSERT, so neither a corrupted transport nor a buggy writer can commit a
    -- record whose replayed bytes differ from the bytes it claimed to store.
    CONSTRAINT durable_records_payload_sha256_matches_body
        CHECK (payload_sha256 = sha256(convert_to(body_json, 'UTF8'))),
    -- The stored body must agree with the columns that index and route it; a writer that
    -- disagrees with its own payload is a bug, not a row.
    CONSTRAINT durable_records_body_matches_device
        CHECK ((body_json::jsonb ->> 'device_id') = device_id),
    CONSTRAINT durable_records_body_matches_path
        CHECK ((body_json::jsonb ->> 'telemetry_path') = telemetry_path),
    CONSTRAINT durable_records_published_at_matches_state
        CHECK ((state = 'published') = (published_at IS NOT NULL)),
    CONSTRAINT durable_records_dead_letter_at_matches_state
        CHECK ((state = 'dead_letter') = (dead_lettered_at IS NOT NULL)),
    CONSTRAINT durable_records_claim_is_complete
        CHECK (state <> 'claimed'
               OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)),
    CONSTRAINT durable_records_legacy_uid_only_from_backfill
        CHECK (legacy_record_uid IS NULL OR ingest_source = 'backfill_sqlite')
) PARTITION BY RANGE (received_at);

COMMENT ON TABLE hear.durable_records IS
    'Durable heartbeat/event outbox. One row per accepted record; daily RANGE partitions on received_at.';
COMMENT ON COLUMN hear.durable_records.body_json IS
    'Verbatim bytes published to Redis. Authoritative; never re-serialise from payload.';
COMMENT ON COLUMN hear.durable_records.payload IS
    'Query-only jsonb projection of body_json. Not the replay source.';
COMMENT ON COLUMN hear.durable_records.payload_sha256 IS
    'sha256 of body_json as UTF-8 bytes, computed by the writer and re-verifiable by hear.verify_payload_integrity().';

-- Rows that were accepted with an arrival time no partition covers. Its presence is what keeps
-- an unattended partition-maintenance failure from turning into an ingest outage; a non-zero
-- count is an alert, and hear.ensure_partitions() is what clears it.
CREATE TABLE IF NOT EXISTS hear.durable_records_unrouted
    PARTITION OF hear.durable_records DEFAULT;

-- ---------------------------------------------------------------------------------------------
-- Identity / dedupe
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hear.durable_record_ids (
    tenant_id            text        NOT NULL,
    device_id            text        NOT NULL,
    record_uid           text        NOT NULL,
    record_id            bigint      NOT NULL,
    received_at          timestamptz NOT NULL,
    payload_sha256       bytea       NOT NULL CHECK (octet_length(payload_sha256) = 32),
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    -- Duplicate arrivals of the same identity: the ordinary retry/at-least-once case.
    duplicate_arrivals   bigint      NOT NULL DEFAULT 0 CHECK (duplicate_arrivals >= 0),
    -- Same identity, *different* body (audit case D1b). First body wins, exactly as SQLite's
    -- INSERT OR IGNORE does today, but the disagreement is counted instead of being invisible.
    conflicting_arrivals bigint      NOT NULL DEFAULT 0 CHECK (conflicting_arrivals >= 0),
    PRIMARY KEY (tenant_id, device_id, record_uid)
);

COMMENT ON TABLE hear.durable_record_ids IS
    'Global idempotency key for the partitioned record table: (tenant_id, device_id, record_uid). '
    'Deduplication horizon equals the retention window; rows are removed with their partition.';

-- ---------------------------------------------------------------------------------------------
-- Cache attempts
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hear.cache_attempts (
    attempt_id   bigint      GENERATED ALWAYS AS IDENTITY,
    tenant_id    text        NOT NULL
        DEFAULT coalesce(nullif(current_setting('hear.tenant_id', true), ''), 'default'),
    device_id    text        NOT NULL,
    record_uid   text        NOT NULL,
    record_id    bigint,
    received_at  timestamptz,
    cache_target text        NOT NULL,
    outcome      text        NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    error_text   text        CHECK (error_text IS NULL OR char_length(error_text) <= 512),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (created_at, attempt_id),
    CONSTRAINT cache_attempts_error_only_on_failure
        CHECK (outcome = 'failed' OR error_text IS NULL)
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE hear.cache_attempts IS
    'Append-only publish attempt log, daily RANGE partitions on created_at. Diagnostic only: '
    'record state lives on hear.durable_records, so attempt history can be dropped independently.';

CREATE TABLE IF NOT EXISTS hear.cache_attempts_unrouted
    PARTITION OF hear.cache_attempts DEFAULT;

-- ---------------------------------------------------------------------------------------------
-- O(1) health counters
-- ---------------------------------------------------------------------------------------------
--
-- SQLite's health() runs three COUNT(*) scans plus an anti-join on every /healthz call, and
-- /healthz is the readiness probe at periodSeconds 10. That cost grows with the ledger. Here
-- the absolute counters are maintained by triggers (migration 0002) and read by primary key;
-- the only live aggregates left are pending/dead-letter counts and oldest-pending age, which
-- are answered from partial indexes over the pending set, not the whole table.
CREATE TABLE IF NOT EXISTS hear.durable_counters (
    tenant_id  text        NOT NULL,
    counter    text        NOT NULL
        CHECK (counter IN ('records_persisted', 'records_duplicate', 'records_conflicting',
                           'cache_successes', 'cache_failures', 'dead_lettered',
                           'claims_expired', 'records_pruned', 'records_backfilled')),
    value      bigint      NOT NULL DEFAULT 0 CHECK (value >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, counter)
);

COMMENT ON TABLE hear.durable_counters IS
    'Monotonic absolute counters, same semantics as the SQLite health() counts, read in O(1).';

CREATE TABLE IF NOT EXISTS hear.durable_events (
    tenant_id             text        NOT NULL,
    last_cache_failure_at timestamptz,
    last_publish_at       timestamptz,
    last_replay_run_at    timestamptz,
    last_prune_run_at     timestamptz,
    last_retention_days   integer,
    PRIMARY KEY (tenant_id)
);

COMMENT ON TABLE hear.durable_events IS
    'Last-occurrence timestamps behind health(): last_cache_failure_at and worker liveness.';

-- ---------------------------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------------------------
--
-- The outbox drain, the pending gauge and the oldest-pending age all read only unpublished
-- rows, which in steady state is a handful out of millions. Every one of them is a partial
-- index so their cost tracks the size of the backlog, never the size of the ledger.
CREATE INDEX IF NOT EXISTS durable_records_pending_due
    ON hear.durable_records (next_attempt_at, record_id)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS durable_records_pending_age
    ON hear.durable_records (received_at)
    WHERE state = 'pending';

-- Reclaiming a lease abandoned by a killed worker: also strictly over the in-flight set.
CREATE INDEX IF NOT EXISTS durable_records_claim_expiry
    ON hear.durable_records (claim_expires_at)
    WHERE state = 'claimed';

CREATE INDEX IF NOT EXISTS durable_records_dead_letter
    ON hear.durable_records (dead_lettered_at, record_id)
    WHERE state = 'dead_letter';

CREATE INDEX IF NOT EXISTS durable_records_device_recent
    ON hear.durable_records (tenant_id, device_id, received_at DESC);

CREATE INDEX IF NOT EXISTS durable_records_path_recent
    ON hear.durable_records (telemetry_path, received_at DESC);

CREATE INDEX IF NOT EXISTS durable_record_ids_record
    ON hear.durable_record_ids (record_id);

CREATE INDEX IF NOT EXISTS durable_record_ids_received
    ON hear.durable_record_ids (received_at);

CREATE INDEX IF NOT EXISTS cache_attempts_record_recent
    ON hear.cache_attempts (tenant_id, device_id, record_uid, created_at DESC);

CREATE INDEX IF NOT EXISTS cache_attempts_failures
    ON hear.cache_attempts (created_at DESC)
    WHERE outcome = 'failed';
