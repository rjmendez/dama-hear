#!/usr/bin/env python3
"""Tiny hear_node heartbeat/event receiver that writes Redis state.

With ``HEAR_DURABLE_STORE=sqlite`` every accepted heartbeat/event is first committed to a local
append-only SQLite ledger and only then reflected into Redis. Redis stays the mixed-version cache
surface, and pending cache publishes can be replayed after a crash or cache outage.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse

import redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.ingest import observability as IO
from hear.ingest import batch as IB
from hear.ingest import clipupload as CU
from hear.ingest import envelope as EV

logger = logging.getLogger("hear-heartbeat")

REDIS_HOST = os.environ.get("REDIS_HOST", "audit-redis.infra.svc.cluster.local")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS = os.environ.get("REDIS_PASS")
AUTH_TOKEN = os.environ.get("HEAR_HEARTBEAT_TOKEN")
BATCH_CREDENTIALS_FILE = os.environ.get("HEAR_BATCH_CREDENTIALS_FILE")
BATCH_RAW_DIR = os.environ.get("HEAR_BATCH_RAW_DIR", "/state/ingest-batch/raw")
BATCH_POOL_ROOT = os.environ.get("HEAR_BATCH_POOL_ROOT", "/pool/corpus")
BATCH_ROUTE = "/v1/ingest/batches"
BATCH_ALIAS_ROUTE = "/ingest/batch"
BATCH_ADAPTER_NAME = "ingest-batch"
BATCH_ADAPTER_VERSION = os.environ.get("HEAR_BATCH_ADAPTER_VERSION", "0.1.0")
CLIP_UPLOAD_ADAPTER_NAME = "clip-upload"


DURABLE_BACKENDS = ("none", "sqlite", "postgres")
DURABLE_STORE = (os.environ.get("HEAR_DURABLE_STORE", "none") or "none").strip().lower()
DURABLE_DB = os.environ.get("HEAR_DURABLE_DB", "/state/heartbeats.sqlite3")
DURABLE_REPLAY_LIMIT = int(os.environ.get("HEAR_DURABLE_REPLAY_LIMIT", "256"))
DURABLE_REPLAY_INTERVAL_S = float(os.environ.get("HEAR_DURABLE_REPLAY_INTERVAL_S", "5.0"))
# Only ever prunes records that already have a *successful* cache_attempts row -- a record
# still pending replay is never eligible no matter how old, so a stalled Redis outage cannot
# silently lose data to the retention sweep. 0 (or below) disables pruning entirely, which
# keeps the phase-0 outbox's previous "grows forever" behavior for anyone not yet opted in.
DURABLE_RETENTION_DAYS = int(os.environ.get("HEAR_DURABLE_RETENTION_DAYS", "30"))
DURABLE_PRUNE_INTERVAL_S = float(os.environ.get("HEAR_DURABLE_PRUNE_INTERVAL_S", "3600"))
# Refusal quarantine (R4). A message that fails validation used to leave nothing behind but a log
# line, so a node whose firmware sends a shape this build rejects simply disappeared. Refusals are
# recorded in their *own* table -- never in durable_records, never replayed, never published.
#
# Three independent bounds, because the refusal ledger accepts input that by definition failed
# validation and therefore cannot be trusted to be small, unique or infrequent:
#   * per-row body cap: one refusal can never store more than this many characters;
#   * hard row/byte caps: the table is a ring, enforced inside the insert transaction, so a flood
#     of distinct bodies evicts older *refusals* and can never grow the shared PVC without bound
#     nor touch an accepted outbox row;
#   * a per-source arrival rate limit (see HeartbeatReceiverStore.record_refusal) so a flood is
#     dropped in memory instead of becoming one fsync per hostile message.
REFUSAL_MAX_BODY_CHARS = int(os.environ.get("HEAR_REFUSAL_MAX_BODY_CHARS", "8192"))
REFUSAL_MAX_ROWS = int(os.environ.get("HEAR_REFUSAL_MAX_ROWS", "5000"))
REFUSAL_MAX_BYTES = int(os.environ.get("HEAR_REFUSAL_MAX_BYTES", str(16 * 1024 * 1024)))
# Refusals accepted per source (device id, or the topic when no device could be identified) per
# window. Beyond it refusals are counted and dropped, never written. <= 0 disables the limiter;
# the hard caps above still apply.
REFUSAL_RATE_LIMIT = int(os.environ.get("HEAR_REFUSAL_RATE_LIMIT", "30"))
REFUSAL_RATE_WINDOW_S = float(os.environ.get("HEAR_REFUSAL_RATE_WINDOW_S", "60"))
# Seconds a writer may hold the exclusive right to publish one durable record to Redis before
# another writer (usually the background replay worker) may take it over. Bounded so a process
# that dies mid-publish cannot strand a record forever; long enough that a slow-but-alive Redis
# write is not raced by a second publisher.
DURABLE_CLAIM_LEASE_S = int(os.environ.get("HEAR_DURABLE_CLAIM_LEASE_S", "60"))
# Durable *generation*: 1 is this SQLite ledger, 2 is the Postgres schema in
# deploy/postgres/migrations (see tools/hear_durable_pg.py). Deliberately unchanged here -- the
# identity fix below is a revision of the SQLite generation, not a new generation, and a row
# stamped 2 must keep meaning "written by the Postgres store".
DURABLE_SCHEMA_VERSION = 1
# Revision of *how record_uid is computed* for this SQLite ledger, tracked per database file in
# durable_meta rather than per row. 1 = the bare idempotency_key (collided across devices),
# 2 = scoped by (telemetry_path, device_id, idempotency_key). See _record_uid.
DURABLE_IDENTITY_VERSION = 2
DURABLE_IDENTITY_META_KEY = "record_uid_identity_version"


def _configured_auth_token(token: Optional[str]) -> str:
    if token is None:
        raise ValueError("HEAR_HEARTBEAT_TOKEN / --auth-token must be set")
    token = token.strip()
    if not token:
        raise ValueError("HEAR_HEARTBEAT_TOKEN / --auth-token must be non-empty")
    return token


def _parse_port(env_val: Optional[str], default: int = 5051) -> int:
    if not env_val:
        return default
    if env_val.startswith("tcp://"):
        return int(env_val.rsplit(":", 1)[-1])
    try:
        return int(env_val)
    except ValueError:
        return default


API_PORT = _parse_port(os.environ.get("HEAR_HEARTBEAT_PORT", "5051"))
HEARTBEAT_TTL_S = int(os.environ.get("HEAR_HEARTBEAT_TTL_S", "30"))
MAX_BODY_BYTES = int(os.environ.get("HEAR_HEARTBEAT_MAX_BODY_BYTES", "8192"))
SOCKET_TIMEOUT_S = float(os.environ.get("HEAR_HEARTBEAT_SOCKET_TIMEOUT_S", "0.5"))
EVENT_STREAM_KEY = os.environ.get("HEAR_EVENT_STREAM_KEY", "dama:hear:events")
EVENT_STREAM_MAXLEN = int(os.environ.get("HEAR_EVENT_STREAM_MAXLEN", "1024"))
RECEIVER_SCHEMA_VERSION = 1
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_CLASS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_FW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,62}$")
_BOOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,31}$")
_EVENT_TYPES = {"clip_written", "detection_batch_ready"}
_CLOCK_STATES = frozenset({"LOCKED", "HOLDOVER", "DEGRADED", "FAULT"})
# Marks a time block this receiver reconstructed for a pre-clock-state firmware rather than one
# the device sent. Stored on the record, so a reader can tell an inferred clock state from a
# reported one.
LEGACY_TIME_SOURCE = "legacy_pre_clock_state_firmware"


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ProblemDetailError(RequestError):
    def __init__(self, *, status: int, code: str, detail: str,
                 retryable: bool = False,
                 field_errors: Optional[Sequence[Mapping[str, Any]]] = None,
                 headers: Optional[Mapping[str, str]] = None):
        super().__init__(detail, status=status)
        self.code = code
        self.detail = detail
        self.retryable = bool(retryable)
        self.field_errors = [dict(row) for row in (field_errors or ())]
        self.headers = dict(headers or {})


class DurableStoreError(RuntimeError):
    """The durable ledger could not record or replay a record."""


class BatchPromotionError(RuntimeError):
    """The shared corpus promotion step could not safely complete."""


@dataclass(frozen=True)
class BatchCredential:
    principal_id: str
    site_id: str
    scope: str
    permissions: tuple[str, ...]
    key_id: Optional[str]
    device_id: Optional[str] = None

    @property
    def site_scoped(self) -> bool:
        return self.scope == "site"

    @property
    def can_ingest(self) -> bool:
        return "ingest:write" in self.permissions

    @property
    def can_upload_clip(self) -> bool:
        return self.can_ingest and "clip:write" in self.permissions


@dataclass(frozen=True)
class StoredBatchReceipt:
    request_fingerprint: str
    response_status: int
    response_body: str
    request_id: str


@dataclass(frozen=True)
class StoredBatchPromotion:
    event_id: str
    state: str
    clip_key: Optional[str]
    pool_path: Optional[str]
    reason: Optional[str]


@dataclass(frozen=True)
class PendingBatchPromotion:
    event_id: str
    envelope_json: str
    raw_ref: Optional[str]
    batch_id: str
    site_id: str
    device_id: str


@dataclass(frozen=True)
class DurableEntry:
    record_uid: str
    telemetry_path: str
    body_json: str
    record: Dict[str, Any]
    cached: bool
    # True when *this* caller holds the exclusive right to publish the record to Redis. A second
    # concurrent caller carrying the identical record gets claimed=False and must not publish,
    # which is what keeps one durable row from producing several Redis writes.
    claimed: bool = True
    # durable_records.id of the stored row (0 when durability is disabled). Used to order replay
    # and to detect that a newer record for the same device already reached the cache.
    row_id: int = 0


@dataclass(frozen=True)
class RefusalEntry:
    """One message that failed validation, kept as evidence rather than replay material.

    Deliberately *not* a DurableEntry: a refusal has no record_uid in the outbox namespace, is
    never claimed, never published to Redis and never replayed. ``body_text`` is the received
    bytes as decoded, truncated to REFUSAL_MAX_BODY_CHARS, so what is quarantined is what the
    publisher actually sent -- not a normalised or repaired copy of it.
    """
    refusal_uid: str
    telemetry_path: str
    device_id: str
    source: str
    reason: str
    body_text: str
    truncated: bool
    received_at: str


def _refusal_body_text(raw: Any) -> tuple[str, bool]:
    """The received body as text, capped. Returns (text, truncated)."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        text = encode_json(raw)
    if len(text) > REFUSAL_MAX_BODY_CHARS:
        return text[:REFUSAL_MAX_BODY_CHARS], True
    return text, False


def _refusal_uid(telemetry_path: str, device_id: str, source: str,
                 reason: str, body_text: str) -> str:
    digest = hashlib.sha256()
    for part in (telemetry_path, device_id, source, reason, body_text):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


class DurableRecordStore:
    backend = "none"
    enabled = False
    path: Optional[str] = None

    def persist(self, record_uid: str, record: Dict[str, Any], body_json: str) -> DurableEntry:
        return DurableEntry(record_uid, str(record["telemetry_path"]), body_json, dict(record), False)

    def claim(self, record_uid: str) -> bool:
        return True

    def note_cache_success(self, record_uid: str, cache_target: str) -> None:
        return

    def note_cache_superseded(self, record_uid: str, cache_target: str) -> None:
        return

    def note_cache_failure(self, record_uid: str, cache_target: str, exc: BaseException) -> None:
        return

    def is_superseded(self, entry: DurableEntry) -> bool:
        return False

    def pending_records(self, limit: int) -> list[DurableEntry]:
        return []

    def prune_acknowledged(self, retention_days: int) -> int:
        return 0

    # -- refusal quarantine seam -------------------------------------------------------------
    #
    # Deliberately *not* part of health(): that key set is the cross-store durable contract the
    # Postgres generation must implement key for key (tests/test_hear_durable_pg_schema.py), and
    # widening it here would silently make every store that cannot store refusals non-compliant.
    # Refusal observability lives on its own surface instead, reported beside the durable store
    # in health_snapshot() and implemented per backend.

    def record_refusal(self, telemetry_path: str, device_id: str, source: str,
                       reason: str, raw_body: Any) -> Optional[RefusalEntry]:
        return None

    def refusals(self, limit: int = 50, telemetry_path: Optional[str] = None) -> list[RefusalEntry]:
        return []

    def prune_refusals(self, retention_days: int) -> int:
        return 0

    def refusal_health(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": False,
            "refused_messages": 0,
            "last_refusal_at": None,
            "max_rows": 0,
            "max_bytes": 0,
            "evicted_refusals": 0,
        }

    def health(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "path": self.path,
            "pending_records": 0,
            "cache_successes": 0,
            "cache_failures": 0,
            "last_cache_failure_at": None,
        }


class SqliteDurableRecordStore(DurableRecordStore):
    backend = "sqlite"
    enabled = True

    def __init__(self, path: str, claim_lease_s: int = DURABLE_CLAIM_LEASE_S,
                 refusal_max_rows: int = REFUSAL_MAX_ROWS,
                 refusal_max_bytes: int = REFUSAL_MAX_BYTES):
        if not path or not path.strip():
            raise ValueError("HEAR_DURABLE_DB / --durable-db must be set for sqlite durability")
        self.path = os.path.abspath(path)
        self.claim_lease_s = max(1, int(claim_lease_s))
        self.refusal_max_rows = max(0, int(refusal_max_rows))
        self.refusal_max_bytes = max(0, int(refusal_max_bytes))
        self._evicted_refusals = 0
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._init_db()

    def _connect(self, foreign_keys: bool = True) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False, timeout=30,
                              isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    @contextmanager
    def _transaction(self, foreign_keys: bool = True):
        """One explicit BEGIN IMMEDIATE transaction on its own (closed) connection.

        BEGIN IMMEDIATE takes SQLite's write lock up front, so a read-then-write sequence inside
        (``did anyone already publish / claim this record?`` then ``claim it``) is atomic against
        every other writer -- including a second process sharing the same PVC file, which an
        in-process ``threading.Lock`` alone cannot cover.
        """
        con = self._connect(foreign_keys=foreign_keys)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    # Never mask the real failure with a rollback-of-nothing error.
                    logger.debug("rollback after a failed durable transaction was a no-op")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    @contextmanager
    def _reading(self):
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    def _init_db(self) -> None:
        with self._transaction() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_uid TEXT NOT NULL UNIQUE,
                    telemetry_path TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    receiver_schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    durable_schema_version INTEGER NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS durable_records_path_device "
                "ON durable_records(telemetry_path, device_id, id)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_uid TEXT NOT NULL,
                    cache_target TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(record_uid) REFERENCES durable_records(record_uid)
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS cache_attempts_record_outcome "
                "ON cache_attempts(record_uid, outcome, id)"
            )
            # /healthz is probed every ~10s and counts succeeded/failed attempts plus the newest
            # failure. Without this index each probe is three full table scans of an append-only
            # ledger that only grows between prunes; with it they are bounded index scans. Purely
            # additive -- no column, constraint or query result changes, so an older receiver
            # binary reading or writing the same file is unaffected.
            con.execute(
                "CREATE INDEX IF NOT EXISTS cache_attempts_outcome_id "
                "ON cache_attempts(outcome, id)"
            )
            # Publish claims are deliberately a *separate* table with no foreign key: an older
            # receiver binary rolled back onto this same file neither reads nor writes it, and
            # its own FK-enforcing writes stay valid because nothing here references it.
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_claims (
                    record_uid TEXT PRIMARY KEY,
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            # Refused messages (R4). Separate table, no foreign key into durable_records and no
            # reference from it: an older receiver binary rolled back onto this same file neither
            # reads nor writes this table, and nothing it writes references it. body_bytes is
            # stored rather than computed so the byte cap is enforceable with an indexed SUM
            # instead of a scan over the bodies themselves.
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS refused_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    refusal_uid TEXT NOT NULL UNIQUE,
                    telemetry_path TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    body_bytes INTEGER NOT NULL,
                    truncated INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS refused_messages_received "
                "ON refused_messages(received_at, id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS refused_messages_source "
                "ON refused_messages(source, device_id, id)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_events (
                    event_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    observed_at TEXT,
                    received_at TEXT NOT NULL,
                    dispatchable INTEGER NOT NULL,
                    raw_ref TEXT,
                    envelope_json TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    principal_id TEXT NOT NULL,
                    credential_scope TEXT NOT NULL,
                    key_id TEXT
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS batch_events_site_device "
                "ON batch_events(site_id, device_id, received_at)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_outbox (
                    event_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES batch_events(event_id)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_receipts (
                    idempotency_scope TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    response_status INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_refusals (
                    refusal_uid TEXT PRIMARY KEY,
                    level TEXT NOT NULL CHECK (level IN ('frame', 'item')),
                    site_id TEXT,
                    device_id TEXT,
                    batch_id TEXT,
                    item_index INTEGER,
                    principal_id TEXT,
                    credential_scope TEXT,
                    key_id TEXT,
                    source TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    raw_ref TEXT,
                    raw_sha256 TEXT NOT NULL,
                    raw_bytes INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    classification TEXT,
                    event_id TEXT,
                    request_id TEXT NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS batch_refusals_lookup "
                "ON batch_refusals(site_id, device_id, received_at)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_promotions (
                    event_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('promoted', 'purged', 'skipped')),
                    clip_key TEXT,
                    pool_path TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES batch_events(event_id)
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS batch_promotions_state_updated "
                "ON batch_promotions(state, updated_at, event_id)"
            )
        self._migrate_record_uids()

    def _migrate_record_uids(self) -> None:
        """Rewrites identity-v1 rows whose record_uid was the bare (device-agnostic) idempotency key.

        v1 stored ``record_uid = idempotency_key``, so two devices reporting the same key collided:
        the second device's payload was silently dropped as a duplicate. v2 scopes the identity by
        telemetry_path + device_id (see _record_uid). Existing rows already carry both columns, so
        the correct new identity is computable in place and no record is dropped or rewritten in
        content. Gated on durable_meta so it runs once per database file, and the whole rewrite is
        one transaction, so an interrupted process leaves the file wholly un-migrated, never half.

        Foreign keys are disabled for this transaction only: cache_attempts references
        durable_records(record_uid) and both sides are renamed together here.
        """
        with self._transaction(foreign_keys=False) as con:
            row = con.execute("SELECT value FROM durable_meta WHERE key = ?",
                              (DURABLE_IDENTITY_META_KEY,)).fetchone()
            previous = int(row["value"]) if row is not None else 1
            # The scan runs on every open rather than being gated on ``previous``: if an older
            # binary is rolled back onto a migrated file and writes bare-key rows, the next
            # start-up still scopes them. The predicate is self-limiting -- a scoped uid is a
            # sha256 hex digest and can never equal the device's own key -- so a fully migrated
            # ledger matches no rows and the scan is a one-off cost at start-up.
            rows = con.execute(
                "SELECT record_uid, telemetry_path, device_id, idempotency_key "
                "FROM durable_records "
                "WHERE idempotency_key IS NOT NULL AND record_uid = idempotency_key"
            ).fetchall()
            for row in rows:
                old_uid = str(row["record_uid"])
                new_uid = _scoped_record_uid(
                    str(row["telemetry_path"]), str(row["device_id"]), str(row["idempotency_key"]))
                collision = con.execute(
                    "SELECT 1 FROM durable_records WHERE record_uid = ?", (new_uid,)
                ).fetchone()
                if collision is not None:
                    # Cannot happen from a v1 ledger (the colliding write was dropped), but a
                    # rolled-back-then-forward mixed-version file could produce it. Leave the
                    # legacy row exactly as it is rather than lose it to a UNIQUE violation.
                    logger.warning(
                        "durable record %s already migrated under %s; leaving legacy row",
                        old_uid, new_uid)
                    continue
                con.execute("UPDATE cache_attempts SET record_uid = ? WHERE record_uid = ?",
                            (new_uid, old_uid))
                con.execute("UPDATE cache_claims SET record_uid = ? WHERE record_uid = ?",
                            (new_uid, old_uid))
                con.execute("UPDATE durable_records SET record_uid = ? WHERE record_uid = ?",
                            (new_uid, old_uid))
            con.execute("INSERT OR REPLACE INTO durable_meta (key, value) VALUES (?, ?)",
                        (DURABLE_IDENTITY_META_KEY, str(DURABLE_IDENTITY_VERSION)))
            if rows:
                logger.info("scoped %d durable record identit(ies) from identity v%d to v%d",
                            len(rows), previous, DURABLE_IDENTITY_VERSION)

    def persist(self, record_uid: str, record: Dict[str, Any], body_json: str) -> DurableEntry:
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO durable_records
                (record_uid, telemetry_path, device_id, idempotency_key, payload_json,
                 received_at, receiver_schema_version, created_at, durable_schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_uid,
                    record["telemetry_path"],
                    record["device_id"],
                    _idempotency_key(record),
                    body_json,
                    record["received_at"],
                    int(record["receiver_schema_version"]),
                    record["received_at"],
                    DURABLE_SCHEMA_VERSION,
                ),
            )
            row = con.execute(
                "SELECT id, telemetry_path, payload_json FROM durable_records WHERE record_uid = ?",
                (record_uid,),
            ).fetchone()
            cached = con.execute(
                "SELECT 1 FROM cache_attempts WHERE record_uid = ? AND outcome = 'succeeded' LIMIT 1",
                (record_uid,),
            ).fetchone() is not None
            # Claiming inside the same transaction is what makes "one durable row -> one Redis
            # publish" true under concurrency: N simultaneous identical requests serialize here
            # and exactly one of them leaves holding the claim.
            claimed = False if cached else self._claim_locked(con, record_uid)
        if row is None:
            raise DurableStoreError(f"durable record {record_uid} disappeared after insert")
        try:
            stored_record = json.loads(row["payload_json"])
        except ValueError as exc:
            raise DurableStoreError(f"durable record {record_uid} stored malformed JSON") from exc
        if not isinstance(stored_record, dict):
            raise DurableStoreError(f"durable record {record_uid} stored a non-object payload")
        return DurableEntry(record_uid, str(row["telemetry_path"]), str(row["payload_json"]),
                            stored_record, cached, claimed, int(row["id"]))

    def _claim_locked(self, con: sqlite3.Connection, record_uid: str) -> bool:
        now = datetime.now(timezone.utc)
        con.execute("DELETE FROM cache_claims WHERE record_uid = ? AND expires_at <= ?",
                    (record_uid, _iso(now)))
        cur = con.execute(
            "INSERT OR IGNORE INTO cache_claims (record_uid, claimed_at, expires_at) "
            "VALUES (?, ?, ?)",
            (record_uid, _iso(now), _iso(now + timedelta(seconds=self.claim_lease_s))),
        )
        return cur.rowcount == 1

    def claim(self, record_uid: str) -> bool:
        with self._lock, self._transaction() as con:
            return self._claim_locked(con, record_uid)

    def note_cache_success(self, record_uid: str, cache_target: str) -> None:
        self._append_attempt(record_uid, cache_target, "succeeded", None)

    def note_cache_superseded(self, record_uid: str, cache_target: str) -> None:
        # Recorded as a normal success (the cache already holds strictly newer state for this
        # device) with a note, rather than as a new outcome value: the outcome CHECK constraint
        # and every existing pending/health query keep their exact v1 meaning.
        self._append_attempt(record_uid, cache_target, "succeeded",
                             "superseded by a newer cache-synced record for this device")

    def note_cache_failure(self, record_uid: str, cache_target: str, exc: BaseException) -> None:
        self._append_attempt(record_uid, cache_target, "failed", _error_text(exc))

    def is_superseded(self, entry: DurableEntry) -> bool:
        """True when a *newer* heartbeat for the same device already reached the cache.

        Replaying an old heartbeat body would SET/SETEX it back over that newer snapshot, so the
        fleet view would go backwards (stale uptime/counters) purely because of a retry. Only the
        last-writer-wins heartbeat keys can be stale-overwritten this way; the event path is an
        append-only stream with its own Redis-side dedupe, so it is never skipped here.
        """
        if entry.telemetry_path != "hear/heartbeat" or entry.row_id <= 0:
            return False
        device_id = entry.record.get("device_id")
        if not isinstance(device_id, str):
            return False
        with self._lock, self._reading() as con:
            row = con.execute(
                """
                SELECT 1
                FROM durable_records r
                WHERE r.telemetry_path = ? AND r.device_id = ? AND r.id > ?
                  AND EXISTS (
                      SELECT 1 FROM cache_attempts a
                      WHERE a.record_uid = r.record_uid AND a.outcome = 'succeeded'
                  )
                LIMIT 1
                """,
                (entry.telemetry_path, device_id, int(entry.row_id)),
            ).fetchone()
        return row is not None

    def prune_acknowledged(self, retention_days: int) -> int:
        """Deletes durable_records (and their cache_attempts) older than ``retention_days``.

        Only ever deletes a record that already has a successful cache_attempts row -- a record
        still pending replay is kept regardless of age, so this can never discard data Redis
        hasn't actually accepted yet. Without this the append-only ledger grows forever and
        eventually fills its PVC; with it, the ledger keeps at least ``retention_days`` of
        history for incident replay while old, already-synced rows are reclaimed.
        """
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with self._lock, self._transaction() as con:
            rows = con.execute(
                """
                SELECT r.record_uid
                FROM durable_records r
                WHERE r.created_at < ?
                  AND EXISTS (
                      SELECT 1 FROM cache_attempts a
                      WHERE a.record_uid = r.record_uid AND a.outcome = 'succeeded'
                  )
                """,
                (cutoff,),
            ).fetchall()
            uids = [row["record_uid"] for row in rows]
            if not uids:
                return 0
            placeholders = ",".join("?" for _ in uids)
            con.execute(
                f"DELETE FROM cache_attempts WHERE record_uid IN ({placeholders})", uids)
            con.execute(
                f"DELETE FROM cache_claims WHERE record_uid IN ({placeholders})", uids)
            con.execute(
                f"DELETE FROM durable_records WHERE record_uid IN ({placeholders})", uids)
        return len(uids)

    def record_refusal(self, telemetry_path: str, device_id: str, source: str,
                       reason: str, raw_body: Any) -> Optional[RefusalEntry]:
        """Durably records one refused message, then enforces the hard caps in the same write.

        Insert, read-back confirmation and cap enforcement share a single BEGIN IMMEDIATE
        transaction: either a caller is told a refusal is stored *and it is*, or nothing changed.
        Repeats of an identical refusal bump a counter instead of adding a row, so a stuck node
        resending the same rejected body cannot consume the ledger.

        The caps make this a ring over refusals only. Eviction always takes the oldest row of
        whichever (source, device) contributes the most rows, so one flooding publisher evicts
        its own history before anyone else's, and no statement here can touch durable_records,
        cache_attempts or cache_claims -- accepted outbox data is unreachable from this path.
        """
        body_text, truncated = _refusal_body_text(raw_body)
        refusal_uid = _refusal_uid(telemetry_path, device_id, source, reason, body_text)
        received_at = utc_now()
        body_bytes = len(body_text.encode("utf-8"))
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT INTO refused_messages
                    (refusal_uid, telemetry_path, device_id, source, reason, body_text,
                     body_bytes, truncated, received_at, occurrences)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(refusal_uid) DO UPDATE SET
                    occurrences = occurrences + 1,
                    received_at = excluded.received_at
                """,
                (refusal_uid, telemetry_path, device_id, source, reason, body_text,
                 body_bytes, 1 if truncated else 0, received_at),
            )
            self._enforce_refusal_caps(con)
            row = con.execute(
                "SELECT refusal_uid, telemetry_path, device_id, source, reason, body_text, "
                "truncated, received_at FROM refused_messages WHERE refusal_uid = ?",
                (refusal_uid,),
            ).fetchone()
        if row is None:
            # Only reachable when this very refusal was the one the caps evicted (a cap of zero,
            # or a body larger than the byte cap). The message is still refused; there is simply
            # no evidence row to hand back, and saying so is better than implying one exists.
            return None
        return RefusalEntry(str(row["refusal_uid"]), str(row["telemetry_path"]),
                            str(row["device_id"]), str(row["source"]), str(row["reason"]),
                            str(row["body_text"]), bool(row["truncated"]),
                            str(row["received_at"]))

    def _enforce_refusal_caps(self, con: sqlite3.Connection) -> int:
        """Evicts refusals until both the row and byte caps hold. Caller supplies the transaction."""
        evicted = 0
        # Bounded by construction: every pass deletes at least one row, and the loop stops as soon
        # as both caps hold, so a single insert can never do unbounded work.
        while True:
            rows, total_bytes = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(body_bytes), 0) FROM refused_messages"
            ).fetchone()
            if rows == 0:
                break
            if rows <= self.refusal_max_rows and total_bytes <= self.refusal_max_bytes:
                break
            victim = con.execute(
                """
                SELECT id FROM refused_messages
                WHERE source || char(31) || device_id = (
                    SELECT source || char(31) || device_id FROM refused_messages
                    GROUP BY source, device_id
                    ORDER BY COUNT(*) DESC, MAX(id) DESC LIMIT 1
                )
                ORDER BY id LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            con.execute("DELETE FROM refused_messages WHERE id = ?", (int(victim[0]),))
            evicted += 1
        if evicted:
            self._evicted_refusals += evicted
        return evicted

    def refusals(self, limit: int = 50, telemetry_path: Optional[str] = None) -> list[RefusalEntry]:
        if limit <= 0:
            return []
        query = ("SELECT refusal_uid, telemetry_path, device_id, source, reason, body_text, "
                 "truncated, received_at FROM refused_messages")
        params: list[Any] = []
        if telemetry_path is not None:
            query += " WHERE telemetry_path = ?"
            params.append(telemetry_path)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._reading() as con:
            rows = con.execute(query, params).fetchall()
        return [RefusalEntry(str(r["refusal_uid"]), str(r["telemetry_path"]), str(r["device_id"]),
                             str(r["source"]), str(r["reason"]), str(r["body_text"]),
                             bool(r["truncated"]), str(r["received_at"])) for r in rows]

    def prune_refusals(self, retention_days: int) -> int:
        """Age-based sweep. The hard caps above are what actually bounds the volume; this only
        keeps the quarantine from holding evidence longer than the telemetry it came from."""
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with self._lock, self._transaction() as con:
            cursor = con.execute("DELETE FROM refused_messages WHERE received_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def refusal_health(self) -> Dict[str, Any]:
        with self._lock, self._reading() as con:
            refused, body_bytes = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(body_bytes), 0) FROM refused_messages"
            ).fetchone()
            last = con.execute(
                "SELECT received_at FROM refused_messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "backend": self.backend,
            "enabled": True,
            "refused_messages": int(refused),
            "last_refusal_at": None if last is None else str(last[0]),
            "refused_bytes": int(body_bytes),
            "max_rows": self.refusal_max_rows,
            "max_bytes": self.refusal_max_bytes,
            "evicted_refusals": self._evicted_refusals,
        }

    def _append_attempt(self, record_uid: str, cache_target: str,
                        outcome: str, error_text: Optional[str]) -> None:
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT INTO cache_attempts (record_uid, cache_target, outcome, error_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_uid, cache_target, outcome, error_text, utc_now()),
            )
            # The publish attempt is over either way, so release the claim immediately instead of
            # waiting out the lease: a failed record becomes replayable on the very next sweep.
            con.execute("DELETE FROM cache_claims WHERE record_uid = ?", (record_uid,))

    def batch_lookup_receipt(self, idempotency_scope: str) -> Optional[StoredBatchReceipt]:
        with self._lock, self._reading() as con:
            row = con.execute(
                "SELECT request_fingerprint, response_status, response_body, request_id "
                "FROM batch_receipts WHERE idempotency_scope = ?",
                (idempotency_scope,),
            ).fetchone()
        if row is None:
            return None
        return StoredBatchReceipt(
            request_fingerprint=str(row["request_fingerprint"]),
            response_status=int(row["response_status"]),
            response_body=str(row["response_body"]),
            request_id=str(row["request_id"]),
        )

    def batch_store_receipt(self, *, idempotency_scope: str, request_fingerprint: str,
                            response_status: int, response_body: str, site_id: str,
                            principal_id: str, batch_id: str, request_id: str) -> None:
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO batch_receipts
                (idempotency_scope, request_fingerprint, response_status, response_body, site_id,
                 principal_id, batch_id, request_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (idempotency_scope, request_fingerprint, response_status, response_body,
                 site_id, principal_id, batch_id, request_id, utc_now()),
            )

    def batch_lookup_promotion(self, event_id: str) -> Optional[StoredBatchPromotion]:
        with self._lock, self._reading() as con:
            row = con.execute(
                "SELECT event_id, state, clip_key, pool_path, reason "
                "FROM batch_promotions WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredBatchPromotion(str(row["event_id"]), str(row["state"]),
                                    None if row["clip_key"] is None else str(row["clip_key"]),
                                    None if row["pool_path"] is None else str(row["pool_path"]),
                                    None if row["reason"] is None else str(row["reason"]))

    def batch_store_promotion(self, *, event_id: str, state: str,
                              clip_key: Optional[str] = None,
                              pool_path: Optional[str] = None,
                              reason: Optional[str] = None) -> StoredBatchPromotion:
        now = utc_now()
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT INTO batch_promotions
                (event_id, state, clip_key, pool_path, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    state = excluded.state,
                    clip_key = excluded.clip_key,
                    pool_path = excluded.pool_path,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (event_id, state, clip_key, pool_path, reason, now, now),
            )
        return StoredBatchPromotion(event_id, state, clip_key, pool_path, reason)

    def batch_unpromoted_events(self, limit: int = 64) -> list[PendingBatchPromotion]:
        if limit <= 0:
            return []
        with self._lock, self._reading() as con:
            rows = con.execute(
                """
                SELECT e.event_id, e.envelope_json, e.raw_ref, e.batch_id, e.site_id, e.device_id
                FROM batch_events e
                LEFT JOIN batch_promotions p ON p.event_id = e.event_id
                WHERE p.event_id IS NULL
                ORDER BY e.received_at, e.item_index, e.event_id
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [PendingBatchPromotion(str(row["event_id"]), str(row["envelope_json"]),
                                      None if row["raw_ref"] is None else str(row["raw_ref"]),
                                      str(row["batch_id"]), str(row["site_id"]),
                                      str(row["device_id"]))
                for row in rows]

    def batch_update_envelope(self, event_id: str, envelope_json: str) -> None:
        with self._lock, self._transaction() as con:
            con.execute("UPDATE batch_events SET envelope_json = ? WHERE event_id = ?",
                        (envelope_json, event_id))

    def batch_persist_event(self, event_id: str, site_id: str, device_id: str, source: str,
                            kind: str, observed_at: Optional[str], received_at: str,
                            dispatchable: bool, raw_ref: Optional[str], envelope_json: str,
                            batch_id: str, item_index: int, principal_id: str,
                            credential_scope: str, key_id: Optional[str]) -> bool:
        with self._lock, self._transaction() as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO batch_events
                (event_id, site_id, device_id, source, kind, observed_at, received_at,
                 dispatchable, raw_ref, envelope_json, batch_id, item_index, principal_id,
                 credential_scope, key_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, site_id, device_id, source, kind, observed_at, received_at,
                 1 if dispatchable else 0, raw_ref, envelope_json, batch_id, int(item_index),
                 principal_id, credential_scope, key_id),
            )
            duplicate = cur.rowcount == 0
            if not duplicate:
                con.execute(
                    "INSERT OR IGNORE INTO batch_outbox (event_id, state, created_at) "
                    "VALUES (?, ?, ?)",
                    (event_id, "pending", received_at),
                )
        return duplicate

    def batch_record_frame_refusal(self, *, raw: bytes, reasons: Sequence[str], source: str,
                                   adapter: str, received_at: str, batch_id: Optional[str],
                                   raw_ref: Optional[str], site_id: Optional[str],
                                   device_id: Optional[str], principal_id: Optional[str],
                                   credential_scope: Optional[str], key_id: Optional[str],
                                   request_id: str) -> None:
        self._batch_record_refusal(
            level="frame", raw=raw, reasons=reasons, source=source, adapter=adapter,
            received_at=received_at, batch_id=batch_id, item_index=None, raw_ref=raw_ref,
            site_id=site_id, device_id=device_id, principal_id=principal_id,
            credential_scope=credential_scope, key_id=key_id, classification=None,
            event_id=None, request_id=request_id,
        )

    def batch_record_item_refusal(self, *, raw: bytes, reasons: Sequence[str], source: str,
                                  adapter: str, received_at: str, batch_id: str,
                                  item_index: int, raw_ref: Optional[str],
                                  site_id: Optional[str], device_id: Optional[str],
                                  principal_id: Optional[str],
                                  credential_scope: Optional[str], key_id: Optional[str],
                                  classification: Optional[str], event_id: Optional[str],
                                  request_id: str) -> None:
        self._batch_record_refusal(
            level="item", raw=raw, reasons=reasons, source=source, adapter=adapter,
            received_at=received_at, batch_id=batch_id, item_index=item_index, raw_ref=raw_ref,
            site_id=site_id, device_id=device_id, principal_id=principal_id,
            credential_scope=credential_scope, key_id=key_id, classification=classification,
            event_id=event_id, request_id=request_id,
        )

    def _batch_record_refusal(self, *, level: str, raw: bytes, reasons: Sequence[str], source: str,
                              adapter: str, received_at: str, batch_id: Optional[str],
                              item_index: Optional[int], raw_ref: Optional[str],
                              site_id: Optional[str], device_id: Optional[str],
                              principal_id: Optional[str], credential_scope: Optional[str],
                              key_id: Optional[str], classification: Optional[str],
                              event_id: Optional[str], request_id: str) -> None:
        refusal_uid = hashlib.sha256(
            ("\x1f".join((
                level,
                site_id or "",
                device_id or "",
                batch_id or "",
                "" if item_index is None else str(item_index),
                ",".join(sorted(set(reasons))),
                hashlib.sha256(raw).hexdigest(),
            ))).encode("utf-8")
        ).hexdigest()
        with self._lock, self._transaction() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO batch_refusals
                (refusal_uid, level, site_id, device_id, batch_id, item_index, principal_id,
                 credential_scope, key_id, source, adapter, received_at, raw_ref, raw_sha256,
                 raw_bytes, reasons_json, classification, event_id, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (refusal_uid, level, site_id, device_id, batch_id, item_index, principal_id,
                 credential_scope, key_id, source, adapter, received_at, raw_ref,
                 hashlib.sha256(raw).hexdigest(), len(raw), encode_json({
                     "reasons": sorted(set(reasons)),
                 }), classification, event_id, request_id),
            )

    def pending_records(self, limit: int) -> list[DurableEntry]:
        if limit <= 0:
            return []
        with self._lock, self._reading() as con:
            rows = con.execute(
                """
                SELECT r.id, r.record_uid, r.telemetry_path, r.payload_json
                FROM durable_records r
                WHERE NOT EXISTS (
                    SELECT 1 FROM cache_attempts a
                    WHERE a.record_uid = r.record_uid AND a.outcome = 'succeeded'
                )
                ORDER BY r.id
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out: list[DurableEntry] = []
        for row in rows:
            try:
                record = json.loads(row["payload_json"])
            except ValueError as exc:
                raise DurableStoreError(
                    f"pending durable record {row['record_uid']} stored malformed JSON") from exc
            if not isinstance(record, dict):
                raise DurableStoreError(
                    f"pending durable record {row['record_uid']} stored a non-object payload")
            out.append(DurableEntry(str(row["record_uid"]), str(row["telemetry_path"]),
                                    str(row["payload_json"]), record, False, False,
                                    int(row["id"])))
        return out

    def health(self) -> Dict[str, Any]:
        with self._lock, self._reading() as con:
            pending = con.execute(
                """
                SELECT COUNT(*)
                FROM durable_records r
                WHERE NOT EXISTS (
                    SELECT 1 FROM cache_attempts a
                    WHERE a.record_uid = r.record_uid AND a.outcome = 'succeeded'
                )
                """
            ).fetchone()[0]
            successes = con.execute(
                "SELECT COUNT(*) FROM cache_attempts WHERE outcome = 'succeeded'"
            ).fetchone()[0]
            failures = con.execute(
                "SELECT COUNT(*) FROM cache_attempts WHERE outcome = 'failed'"
            ).fetchone()[0]
            last_failure = con.execute(
                "SELECT created_at FROM cache_attempts WHERE outcome = 'failed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "path": self.path,
            "pending_records": int(pending),
            "cache_successes": int(successes),
            "cache_failures": int(failures),
            "last_cache_failure_at": None if last_failure is None else last_failure[0],
        }


# Closes the crash/ambiguous-ACK window between "Redis accepted the write" and "SQLite recorded
# cache success" for hear/event telemetry specifically. Heartbeat writes (SETEX/SET) are plain
# overwrites -- replaying the same body is harmless -- so only the append-only event stream needs
# Redis-side atomicity to avoid a duplicate XADD entry; heartbeats stay on a regular pipeline
# below and never touch this script or its dedupe metadata.
#
# Cluster-safety: a Lua script may only touch keys that live on the same Redis Cluster hash slot,
# and every key it touches must be declared via KEYS (never built by string concatenation inside
# the script, which is an undeclared-key access that breaks cluster routing/redirection). All
# three keys here (the dedupe ZSET, its sequence counter, and the stream itself) are declared and
# hash-tagged to the *literal* event stream key: for an untagged key Redis hashes the whole key
# name, but for a "{tag}suffix" key it hashes only the tag. Using the stream key's own text as
# that tag (see _hash_tagged_to) makes CRC16(tag) identical to CRC16(stream_key), so the dedupe
# ZSET/seq counter always land on the same slot as the stream itself -- without changing the
# stream key's own literal name or behavior. The per-device "last event" cache key is
# deliberately kept *out* of this script: its natural hash tag would depend on device_id, which
# cannot share a fixed slot with the stream's tag, and it does not need atomicity anyway (see
# RedisHeartbeatCache.write).
#
# Retention alignment: the dedupe ZSET is trimmed to the *exact* same MAXLEN bound as the event
# stream (no APPROX/'~' trimming on either side), scored by a monotonic event-only sequence
# counter. Because this script is the sole writer of both, the dedupe ZSET always reflects
# exactly the set of record_uids physically retained in the stream -- unrelated heartbeat traffic
# never adds entries here and can never evict an event's dedupe marker.
_EVENT_DEDUPE_SCRIPT = """
local dedupe_key = KEYS[1]
local seq_key = KEYS[2]
local stream_key = KEYS[3]

local record_uid = ARGV[1]
local node_id = ARGV[2]
local body_json = ARGV[3]
local maxlen = tonumber(ARGV[4])

if redis.call('ZSCORE', dedupe_key, record_uid) then
    return 0
end

-- Streams written before the dedupe rollout have no marker. A pending SQLite record may refer
-- to one of them after an ambiguous ACK, so recognize the exact legacy payload before appending.
local existing = redis.call('XRANGE', stream_key, '-', '+')
for _, entry in ipairs(existing) do
    local fields = entry[2]
    local existing_node = nil
    local existing_payload = nil
    for i = 1, #fields, 2 do
        if fields[i] == 'device_id' then existing_node = fields[i + 1] end
        if fields[i] == 'payload' then existing_payload = fields[i + 1] end
    end
    if existing_node == node_id and existing_payload == body_json then
        local seq = redis.call('INCR', seq_key)
        redis.call('ZADD', dedupe_key, seq, record_uid)
        redis.call('ZREMRANGEBYRANK', dedupe_key, 0, -1 - maxlen)
        return 0
    end
end

redis.call('XADD', stream_key, 'MAXLEN', maxlen, '*', 'device_id', node_id, 'payload', body_json)

local seq = redis.call('INCR', seq_key)
redis.call('ZADD', dedupe_key, seq, record_uid)
redis.call('ZREMRANGEBYRANK', dedupe_key, 0, -1 - maxlen)
return 1
"""


def _hash_tagged_to(stream_key: str, suffix: str) -> str:
    """Builds ``stream_key``-derived key that Redis Cluster always routes to the same hash slot
    as ``stream_key`` itself, without altering ``stream_key``'s own literal name. See
    _EVENT_DEDUPE_SCRIPT's docstring for why this is required for cluster-safe multi-key scripts.
    """
    start = stream_key.find("{")
    if start >= 0:
        end = stream_key.find("}", start + 1)
        if end > start + 1:
            return stream_key[:start] + stream_key[start:end + 1] + suffix
    return "{" + stream_key + "}" + suffix


class RedisHeartbeatCache:
    def __init__(self, client: Any, heartbeat_ttl_s: int = HEARTBEAT_TTL_S,
                 event_stream_key: str = EVENT_STREAM_KEY,
                 event_stream_maxlen: int = EVENT_STREAM_MAXLEN):
        self.client = client
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.event_stream_key = event_stream_key
        self.event_stream_maxlen = event_stream_maxlen
        self.event_dedupe_key = _hash_tagged_to(event_stream_key, ":dedupe")
        self.event_dedupe_seq_key = _hash_tagged_to(event_stream_key, ":dedupe:seq")
        self._event_dedupe_script = client.register_script(_EVENT_DEDUPE_SCRIPT)

    def write(self, record: Dict[str, Any], body_json: str, record_uid: str) -> None:
        telemetry_path = record["telemetry_path"]
        node_id = record["device_id"]
        if telemetry_path == "hear/heartbeat":
            # A retry overwriting the same TTL key / latest snapshot with the same body is a
            # no-op in effect, so a plain (non-atomic, single-key-per-command) pipeline is
            # sufficient here and keeps this path entirely cluster-friendly.
            pipe = self.client.pipeline(transaction=False)
            pipe.setex(f"dama:hear:{node_id}", self.heartbeat_ttl_s, body_json)
            pipe.sadd("dama:hear:devices", node_id)
            pipe.set("dama:hear:latest", body_json)
            pipe.execute()
        elif telemetry_path == "hear/event":
            # Only the append-only stream write plus its dedupe marker need Redis-side atomicity
            # (a duplicate XADD is the one non-idempotent risk here). The per-device devices-set
            # membership and "last event" cache value are plain overwrites -- a retry rewriting
            # the same value is harmless -- so they stay outside the script as ordinary single-
            # key commands instead of adding cross-slot keys (this per-device key's hash tag
            # would depend on node_id, which cannot share a slot with the stream's fixed tag) to
            # a multi-key Lua invocation.
            self._event_dedupe_script(
                keys=[self.event_dedupe_key, self.event_dedupe_seq_key, self.event_stream_key],
                args=[record_uid, node_id, body_json, self.event_stream_maxlen],
            )
            self.client.sadd("dama:hear:devices", node_id)
            self.client.set(f"dama:hear:event:{node_id}", body_json)
        else:  # pragma: no cover - validators own this in practice.
            raise ValueError(f"unsupported telemetry_path {telemetry_path!r}")


class HeartbeatReceiverStore:
    def __init__(self, client: Any, heartbeat_ttl_s: int = HEARTBEAT_TTL_S,
                 event_stream_key: str = EVENT_STREAM_KEY,
                 event_stream_maxlen: int = EVENT_STREAM_MAXLEN,
                 redis_target: Optional[str] = None,
                 durable_store: Optional[DurableRecordStore] = None,
                 refusal_rate_limit: int = REFUSAL_RATE_LIMIT,
                 refusal_rate_window_s: float = REFUSAL_RATE_WINDOW_S):
        self.client = client
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.event_stream_key = event_stream_key
        self.event_stream_maxlen = event_stream_maxlen
        self.redis_target = redis_target or "unknown"
        self.durable_store = durable_store or DurableRecordStore()
        self.cache = RedisHeartbeatCache(
            client,
            heartbeat_ttl_s=heartbeat_ttl_s,
            event_stream_key=event_stream_key,
            event_stream_maxlen=event_stream_maxlen,
        )
        self.refusal_rate_limit = max(0, int(refusal_rate_limit))
        self.refusal_rate_window_s = float(refusal_rate_window_s)
        self._refusal_lock = threading.Lock()
        self._refusal_windows: Dict[str, tuple[float, int]] = {}
        self.suppressed_refusals = 0

    def record_refusal(self, telemetry_path: str, device_id: str, source: str,
                       reason: str, raw_body: Any) -> Optional[RefusalEntry]:
        """Quarantines a refused message, rate-limited per source.

        The limiter is in memory and costs no I/O, which is the point: without it every message a
        hostile or broken publisher sends becomes one synchronous fsync on the shared telemetry
        volume. Over the limit the refusal is counted (suppressed_refusals) and dropped; the
        message stays refused either way, so nothing here can turn bad input into an ingest
        failure. Storage failures are logged, never raised, for the same reason.
        """
        if not self._refusal_allowed(device_id or source):
            self.suppressed_refusals += 1
            return None
        try:
            return self.durable_store.record_refusal(
                telemetry_path, device_id, source, reason, raw_body)
        except Exception as exc:  # pragma: no cover - defensive; a refusal must never 500.
            logger.warning("could not quarantine refused %s message: %s", telemetry_path, exc)
            return None

    def _refusal_allowed(self, key: str) -> bool:
        if self.refusal_rate_limit <= 0 or self.refusal_rate_window_s <= 0:
            # <= 0 disables the limiter, matching the other "0 turns this off" knobs here. The
            # hard row/byte caps still bound what the flood can actually consume.
            return True
        now = time.monotonic()
        with self._refusal_lock:
            started, count = self._refusal_windows.get(key, (now, 0))
            if now - started >= self.refusal_rate_window_s:
                started, count = now, 0
            if count >= self.refusal_rate_limit:
                self._refusal_windows[key] = (started, count)
                return False
            self._refusal_windows[key] = (started, count + 1)
            return True

    def write_heartbeat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._write(payload)

    def write_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._write(payload)

    def _write(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(payload)
        record["received_at"] = utc_now()
        record["receiver_schema_version"] = RECEIVER_SCHEMA_VERSION
        body_json = encode_json(record)
        stored = self.durable_store.persist(_record_uid(payload), record, body_json)
        if stored.cached:
            self._refresh_cached(stored)
            return stored.record
        if not stored.claimed:
            # A concurrent caller carrying this exact record already holds the publish claim and
            # is writing the identical body to Redis right now. Publishing again here is what
            # turned one durable row into several Redis writes; the durable record is committed
            # either way, and the replay worker repairs it if that other caller fails.
            return stored.record
        try:
            self.cache.write(stored.record, stored.body_json, stored.record_uid)
        except Exception as exc:
            self.durable_store.note_cache_failure(stored.record_uid, self.redis_target, exc)
            raise
        self.durable_store.note_cache_success(stored.record_uid, self.redis_target)
        return stored.record

    def _refresh_cached(self, stored: DurableEntry) -> None:
        """Re-arms the TTL for a heartbeat whose durable record was already cached.

        A retried/duplicate heartbeat used to short-circuit before Redis, so during a reboot or
        retry loop the node's ``dama:hear:<id>`` key could expire and the node would read as
        offline even though it was still reporting. Re-writing the same stored body is a pure
        overwrite (identical bytes, fresh TTL), so it stays idempotent. Events are excluded: the
        stream is append-only and already deduped Redis-side, so a duplicate must stay a no-op.
        """
        if stored.telemetry_path != "hear/heartbeat":
            return
        if self.durable_store.is_superseded(stored):
            # An *older* heartbeat can be redelivered after a newer one was already cached (MQTT
            # QoS-1 redelivery, a node draining its local outbox, an out-of-order retry). Writing
            # its body back would roll dama:hear:<id> and dama:hear:latest backwards, which is the
            # same hazard replay_pending refuses. The newer record already armed this device's TTL
            # with current state, so the correct refresh here is none at all.
            return
        try:
            self.cache.write(stored.record, stored.body_json, stored.record_uid)
        except Exception as exc:
            # The record is already durable and already acknowledged by Redis once, so this is
            # not a pending-replay condition -- but the caller must still learn the TTL was not
            # re-armed instead of getting a false 204.
            self.durable_store.note_cache_failure(stored.record_uid, self.redis_target, exc)
            raise

    def replay_pending(self, limit: int = DURABLE_REPLAY_LIMIT) -> Dict[str, Any]:
        summary = {
            "backend": self.durable_store.backend,
            "attempted": 0,
            "synced": 0,
            "failed": 0,
            "remaining_pending": 0,
        }
        if not self.durable_store.enabled or limit <= 0:
            return summary
        # Oldest-first (pending_records orders by id) and claimed one at a time, so two replay
        # sweeps -- or a sweep racing a live request for the same record -- cannot both publish.
        pending = [entry for entry in self.durable_store.pending_records(limit)
                   if self.durable_store.claim(entry.record_uid)]
        summary["attempted"] = len(pending)
        for entry in pending:
            if self.durable_store.is_superseded(entry):
                # Counted as synced: the cache already holds strictly newer state for this
                # device, so there is nothing left to publish and nothing left pending.
                self.durable_store.note_cache_superseded(entry.record_uid, self.redis_target)
                summary["synced"] += 1
                continue
            try:
                self.cache.write(entry.record, entry.body_json, entry.record_uid)
            except Exception as exc:
                self.durable_store.note_cache_failure(entry.record_uid, self.redis_target, exc)
                summary["failed"] += 1
                continue
            self.durable_store.note_cache_success(entry.record_uid, self.redis_target)
            summary["synced"] += 1
        summary["remaining_pending"] = int(self.durable_store.health()["pending_records"])
        return summary

    def health_snapshot(self) -> Dict[str, Any]:
        refusals = dict(self.durable_store.refusal_health())
        refusals["suppressed_refusals"] = self.suppressed_refusals
        return {
            "redis_target": self.redis_target,
            "durable_store": self.durable_store.health(),
            # Separate surface, not extra durable_store keys: see the refusal seam on
            # DurableRecordStore for why the cross-store health contract stays as it is.
            "refusals": refusals,
        }


class BatchCredentialStore:
    """Bearer-token lookup by token hash. The file contains hashes, never cleartext tokens."""

    def __init__(self, credentials: Sequence[tuple[str, BatchCredential]]):
        self._by_hash = dict(credentials)

    @classmethod
    def from_file(cls, path: str) -> "BatchCredentialStore":
        if not path or not path.strip():
            raise ValueError("HEAR_BATCH_CREDENTIALS_FILE must be set for batch ingest")
        body = Path(path).read_text(encoding="utf-8")
        doc = json.loads(body)
        rows = doc.get("credentials") if isinstance(doc, dict) else doc
        if not isinstance(rows, list) or not rows:
            raise ValueError("batch credential store must be a non-empty credentials array")
        out: list[tuple[str, BatchCredential]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"credential {idx} must be an object")
            token_sha256 = _hex_field(row, "token_sha256", idx)
            scope = _choice_field(row, "scope", ("device", "site"), idx)
            site_id = _require_non_empty_string(row, "site_id", idx)
            principal_id = _require_non_empty_string(row, "principal_id", idx)
            perms = row.get("permissions")
            if not isinstance(perms, list) or not all(isinstance(v, str) and v for v in perms):
                raise ValueError(f"credential {idx} permissions must be a non-empty string array")
            device_id = row.get("device_id")
            if scope == "device":
                if not isinstance(device_id, str) or not device_id.strip():
                    raise ValueError(f"credential {idx} device scope requires device_id")
                device_id = device_id.strip()
            else:
                device_id = None
            cred = BatchCredential(
                principal_id=principal_id,
                site_id=site_id,
                scope=scope,
                permissions=tuple(perms),
                key_id=row.get("key_id") if isinstance(row.get("key_id"), str) else None,
                device_id=device_id,
            )
            out.append((token_sha256, cred))
        return cls(out)

    def authenticate(self, authorization: Optional[str]) -> BatchCredential:
        if not isinstance(authorization, str) or not authorization.strip():
            raise ProblemDetailError(status=401, code="credential_missing",
                                     detail="Authorization: Bearer is required")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ProblemDetailError(status=401, code="credential_missing",
                                     detail="Authorization must be Bearer <token>")
        token_sha256 = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        cred = self._by_hash.get(token_sha256)
        if cred is None:
            raise ProblemDetailError(status=401, code="credential_missing",
                                     detail="credential is missing, invalid or expired")
        if not cred.can_ingest:
            raise ProblemDetailError(status=403, code="forbidden",
                                     detail="credential lacks ingest:write")
        return cred


class BatchClipPromoter:
    _AUDIO_KEYS = ("wav_b64", "audio_wav_b64", "clip_wav_b64", "audio_b64")
    _CLIP_KEYS = ("clip", "clip_path", "clip_name")

    def __init__(self, *, pool_root: str = BATCH_POOL_ROOT,
                 audit_log: Optional[str] = None, vad: Any = None):
        self.pool_root = os.path.abspath(pool_root)
        self.audit_log = audit_log or os.path.join(self.pool_root, "clips", "vad_purge.jsonl")
        self._vad = vad

    def _modules(self):
        from hear import clips as clip_store
        from hear import identity as node_identity
        from hear.privacy import purge as privacy_purge
        return clip_store, node_identity, privacy_purge

    def _load_vad(self) -> Any:
        if self._vad is not None:
            return self._vad
        _clip_store, _node_identity, privacy_purge = self._modules()
        self._vad = privacy_purge.load_vad("auto")
        return self._vad

    @staticmethod
    def _mapping(value: Any) -> Optional[Mapping[str, Any]]:
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _string(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def _clip_name(self, payload: Mapping[str, Any]) -> Optional[str]:
        clip_store, _node_identity, _privacy_purge = self._modules()
        for key in self._CLIP_KEYS:
            got = self._string(payload.get(key))
            if got:
                return got
        got = self._string(payload.get("clip_basename"))
        if got:
            return clip_store.CLIP_DIR + "/" + got
        for nested_key in ("event", "legacy", "clip"):
            nested = self._mapping(payload.get(nested_key))
            if nested is None:
                continue
            got = self._clip_name(nested)
            if got:
                return got
        return None

    def _audio_b64(self, payload: Mapping[str, Any]) -> Optional[str]:
        for key in self._AUDIO_KEYS:
            got = self._string(payload.get(key))
            if got:
                return got
        for nested_key in ("event", "legacy", "clip"):
            nested = self._mapping(payload.get(nested_key))
            if nested is None:
                continue
            got = self._audio_b64(nested)
            if got:
                return got
        return None

    def carries_clip_audio(self, envelope: Mapping[str, Any]) -> bool:
        if envelope.get("kind") != "clip":
            return False
        payload = self._mapping(envelope.get("payload"))
        if payload is None:
            return False
        return self._clip_name(payload) is not None and self._audio_b64(payload) is not None

    def redact_audio_fields(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self.redact_audio_fields(child)
                    for key, child in value.items()
                    if str(key) not in self._AUDIO_KEYS}
        if isinstance(value, list):
            return [self.redact_audio_fields(child) for child in value]
        return value

    def _audio_bytes(self, payload: Mapping[str, Any], clip_name: str) -> bytes:
        raw = self._audio_b64(payload)
        if raw is None:
            raise BatchPromotionError(f"{clip_name}: clip payload carried no audio bytes")
        if raw.startswith("data:"):
            prefix = "base64,"
            if prefix not in raw:
                raise BatchPromotionError(f"{clip_name}: unsupported audio data URL")
            raw = raw.split(prefix, 1)[1]
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BatchPromotionError(f"{clip_name}: audio payload is not valid base64") from exc

    def _parse_observed_at(self, observed_at: Any) -> Optional[datetime]:
        value = self._string(observed_at)
        if value is None:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        except ValueError as exc:
            raise BatchPromotionError(f"observed_at is not RFC3339 UTC: {observed_at!r}") from exc

    def _clip_candidate(self, envelope: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        clip_store, node_identity, _privacy_purge = self._modules()
        if envelope.get("kind") != "clip":
            return None
        payload = self._mapping(envelope.get("payload"))
        if payload is None:
            return None
        clip_name = self._clip_name(payload)
        if clip_name is None or self._audio_b64(payload) is None:
            return None
        try:
            parts = clip_store.parse_clip_name(clip_name)
        except ValueError as exc:
            raise BatchPromotionError(str(exc)) from exc
        node = self._string(envelope.get("device_id")) or str(parts["node"])
        if parts["node"] != node and node_identity.alias_of(parts["node"]) != node:
            raise BatchPromotionError(
                "clip name says node %r, envelope says %r" % (parts["node"], node))
        observed = self._parse_observed_at(envelope.get("observed_at"))
        clock = self._mapping(envelope.get("clock")) or {}
        anchored = bool(clock.get("valid") is True and observed is not None)
        ts_utc_s = observed.timestamp() if anchored and observed is not None else None
        utc_us = int(round(ts_utc_s * 1_000_000.0)) if ts_utc_s is not None else 0
        return {
            "clip": clip_name,
            "parts": parts,
            "node": node,
            "body": self._audio_bytes(payload, clip_name),
            "clip_key": clip_store.clip_key(parts["node"], parts["boot"], parts["sample"]),
            "dets": {
                "utc_us": utc_us,
                "ts_utc_s": ts_utc_s,
                "anchored": anchored,
                "uptime_s": payload.get("uptime_s"),
                "fs_hz": payload.get("fs_hz"),
                "trigger": payload.get("trigger"),
                "clip_why": payload.get("clip_why"),
                "dets_origin": "batch-http",
                "record_key": payload.get("record_key"),
            },
        }

    def _receipt_exists(self, clip_key: str) -> bool:
        if not os.path.exists(self.audit_log):
            return False
        with open(self.audit_log, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("clip_key") == clip_key:
                    return True
        return False

    def _store_clean_clip(self, clip: Dict[str, Any]) -> str:
        clip_store, _node_identity, _privacy_purge = self._modules()
        clip_store.sweep_tmp(self.pool_root, time.time())
        day = clip_store._day(clip["dets"]["ts_utc_s"] if clip["dets"]["anchored"] else None)
        full = clip_store.store_path(self.pool_root, day, clip["node"], clip["parts"]["basename"])
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = "%s.%d.tmp" % (full, os.getpid())
        with open(tmp, "wb") as fh:
            fh.write(clip["body"])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, full)
        return os.path.relpath(full, self.pool_root)

    def ensure(self, store: "SqliteDurableRecordStore", event_id: str,
               envelope: Mapping[str, Any]) -> StoredBatchPromotion:
        prior = store.batch_lookup_promotion(event_id)
        if prior is not None:
            return prior
        clip_store, _node_identity, privacy_purge = self._modules()
        clip = self._clip_candidate(envelope)
        if clip is None:
            return store.batch_store_promotion(event_id=event_id, state="skipped",
                                               reason="non_clip_or_no_audio_payload")
        current = clip_store.read_index(self.pool_root).get(clip["clip_key"])
        if current is not None and current.get("outcome") == "stored":
            return store.batch_store_promotion(
                event_id=event_id,
                state="promoted",
                clip_key=clip["clip_key"],
                pool_path=None if current.get("path") is None else str(current.get("path")),
                reason="already_promoted",
            )
        if self._receipt_exists(clip["clip_key"]):
            return store.batch_store_promotion(
                event_id=event_id,
                state="purged",
                clip_key=clip["clip_key"],
                reason="already_purged",
            )
        receipt = privacy_purge.purge_wav_bytes(
            clip["body"],
            node=clip["node"],
            clip=clip["parts"]["basename"],
            vad=self._load_vad(),
        )
        if receipt is not None:
            privacy_purge.append_receipt(
                self.audit_log,
                replace(receipt, clip_key=clip["clip_key"], clip=clip["parts"]["basename"],
                        node=clip["node"]),
            )
            return store.batch_store_promotion(
                event_id=event_id,
                state="purged",
                clip_key=clip["clip_key"],
                reason=receipt.fail_closed_reason or receipt.verdict,
            )
        rel = self._store_clean_clip(clip)
        current = clip_store.read_index(self.pool_root).get(clip["clip_key"])
        if current is None or current.get("outcome") != "stored":
            clip_store.append_index(
                self.pool_root,
                (
                    clip_store.index_row(
                        clip=clip["clip"],
                        parts=clip["parts"],
                        node=clip["node"],
                        body=clip["body"],
                        probe=clip_store.wav_probe(clip["body"]),
                        dets=clip["dets"],
                        path=rel,
                        outcome="stored",
                        fetched_at=time.time(),
                    ),
                ),
            )
        return store.batch_store_promotion(event_id=event_id, state="promoted",
                                           clip_key=clip["clip_key"], pool_path=rel)


class BatchIngestAdapter:
    def __init__(self, durable_store: DurableRecordStore,
                 credential_store: Optional[BatchCredentialStore],
                 *, raw_root: str = BATCH_RAW_DIR,
                 pool_root: str = BATCH_POOL_ROOT,
                 adapter_name: str = BATCH_ADAPTER_NAME,
                 adapter_version: str = BATCH_ADAPTER_VERSION,
                 metrics: Optional[IO.IngestMetrics] = None,
                 load_error: Optional[str] = None,
                 before_receipt_store: Optional[Callable[[], None]] = None,
                 vad: Any = None):
        self.durable_store = durable_store
        self.credential_store = credential_store
        self.raw_root = os.path.abspath(raw_root)
        self.promoter = BatchClipPromoter(pool_root=pool_root, vad=vad)
        self.adapter_name = adapter_name
        self.adapter_version = adapter_version
        self.metrics = metrics or IO.IngestMetrics()
        self.load_error = load_error
        self.before_receipt_store = before_receipt_store

    @classmethod
    def from_config(cls, durable_store: DurableRecordStore,
                    credentials_file: Optional[str],
                    *, raw_root: str = BATCH_RAW_DIR,
                    pool_root: str = BATCH_POOL_ROOT,
                    adapter_name: str = BATCH_ADAPTER_NAME,
                    adapter_version: str = BATCH_ADAPTER_VERSION,
                    metrics: Optional[IO.IngestMetrics] = None) -> "BatchIngestAdapter":
        load_error = None
        creds = None
        try:
            if credentials_file:
                creds = BatchCredentialStore.from_file(credentials_file)
            else:
                load_error = "HEAR_BATCH_CREDENTIALS_FILE is not configured"
        except Exception as exc:
            load_error = str(exc)
        return cls(durable_store, creds, raw_root=raw_root, pool_root=pool_root,
                   adapter_name=adapter_name,
                   metrics=metrics,
                   adapter_version=adapter_version, load_error=load_error)

    def ingest(self, *, path: str, raw: bytes, content_type: str, idempotency_key: str,
               authorization: Optional[str], content_encoding: Optional[str],
               request_id: str) -> tuple[int, str, Dict[str, str]]:
        store = self._require_store()
        cred = self._authenticate(authorization)
        scope = IB.idempotency_scope(site_id=cred.site_id, principal=cred.principal_id,
                                     route="POST /v1/ingest/batches", key=idempotency_key)
        fingerprint = IB.request_fingerprint(raw)
        replay = store.batch_lookup_receipt(scope)
        if replay is not None:
            if replay.request_fingerprint != fingerprint:
                self.metrics.observe_idempotency_conflict(
                    site=cred.site_id,
                    device_id=cred.device_id or cred.principal_id,
                    source="batch-http",
                    adapter=self.adapter_name,
                )
                raise ProblemDetailError(
                    status=409,
                    code="idempotency_conflict",
                    detail="Idempotency-Key was reused with a different request body",
                )
            return replay.response_status, replay.response_body, {
                "Content-Type": IB.RECEIPT_MEDIA_TYPE,
                "X-Request-ID": request_id,
            }

        received_at = utc_now()
        frame_raw_ref = _retain_batch_bytes(
            self.raw_root, cred.site_id, cred.device_id or cred.principal_id,
            "frame-" + request_id, raw, suffix="json")
        if isinstance(content_encoding, str) and content_encoding.strip():
            store.batch_record_frame_refusal(
                raw=raw, reasons=["content_encoding_unsupported"], source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=None, raw_ref=frame_raw_ref, site_id=cred.site_id,
                device_id=cred.device_id, principal_id=cred.principal_id,
                credential_scope=cred.scope, key_id=cred.key_id, request_id=request_id)
            self.metrics.observe_frame_refusal(
                ["content_encoding_unsupported"],
                raw_body_bytes=len(raw),
                received_at=received_at,
                site=cred.site_id,
                source="batch-http",
                adapter=self.adapter_name,
            )
            raise ProblemDetailError(status=415, code="content_encoding_unsupported",
                                     detail="Content-Encoding is reserved and not enabled")

        try:
            codec = IB.codec_for_media_type(content_type)
        except IB.BatchError as exc:
            store.batch_record_frame_refusal(
                raw=raw, reasons=["unsupported_media_type"], source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=None, raw_ref=frame_raw_ref, site_id=cred.site_id,
                device_id=cred.device_id, principal_id=cred.principal_id,
                credential_scope=cred.scope, key_id=cred.key_id, request_id=request_id)
            self.metrics.observe_frame_refusal(
                ["unsupported_media_type"],
                raw_body_bytes=len(raw),
                received_at=received_at,
                site=cred.site_id,
                source="batch-http",
                adapter=self.adapter_name,
            )
            raise ProblemDetailError(status=415, code="unsupported_media_type", detail=str(exc))

        try:
            frame = IB.decode_batch(raw, codec)
        except IB.BatchError as exc:
            reason = "batch_too_large" if len(raw) > IB.MAX_BATCH_BYTES else "undecodable_body"
            store.batch_record_frame_refusal(
                raw=raw, reasons=[reason], source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=None, raw_ref=frame_raw_ref, site_id=cred.site_id,
                device_id=cred.device_id, principal_id=cred.principal_id,
                credential_scope=cred.scope, key_id=cred.key_id, request_id=request_id)
            self.metrics.observe_frame_refusal(
                [reason],
                raw_body_bytes=len(raw),
                received_at=received_at,
                site=cred.site_id,
                source="batch-http",
                adapter=self.adapter_name,
            )
            raise ProblemDetailError(
                status=413 if reason == "batch_too_large" else 400,
                code=reason,
                detail=str(exc),
            )

        validated = IB.validate_batch(
            frame,
            credential_device_id=cred.device_id,
            credential_site_id=cred.site_id,
            site_scoped=cred.site_scoped,
        )
        if not validated.ok:
            code = _frame_error_status(validated.reasons)
            store.batch_record_frame_refusal(
                raw=raw, reasons=validated.reasons, source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=frame.get("batch_id") if isinstance(frame, dict) else None,
                raw_ref=frame_raw_ref, site_id=cred.site_id, device_id=cred.device_id,
                principal_id=cred.principal_id, credential_scope=cred.scope,
                key_id=cred.key_id, request_id=request_id)
            self.metrics.observe_frame_refusal(
                validated.reasons,
                frame=frame,
                raw_body_bytes=len(raw),
                received_at=received_at,
                site=cred.site_id,
                source="batch-http",
                adapter=self.adapter_name,
            )
            raise ProblemDetailError(
                status=code,
                code=validated.reasons[0],
                detail="batch frame was refused",
                field_errors=[{"field": row.get("path"), "reason": row.get("reason")}
                              for row in validated.errors if row.get("path")],
            )

        results = self._persist_items(
            store=store,
            frame=frame,
            raw_messages=frame.get("messages") or [],
            provisional=validated.items,
            cred=cred,
            received_at=received_at,
            request_id=request_id,
        )
        try:
            self._promote_results(
                store=store,
                frame=frame,
                raw_messages=frame.get("messages") or [],
                results=results,
                cred=cred,
                received_at=received_at,
            )
        except Exception as exc:
            raise ProblemDetailError(status=503, code="batch_promotion_unavailable",
                                     detail=str(exc), retryable=True,
                                     headers={"Retry-After": "5"}) from exc
        frame_status = "refused" if all(result.status == "deferred" for result in results) else "accepted"
        self.metrics.observe_batch_results(
            frame,
            results,
            raw_body_bytes=len(raw),
            received_at=received_at,
            site=cred.site_id,
            source="batch-http",
            adapter=self.adapter_name,
            frame_status=frame_status,
        )
        if all(result.status == "deferred" for result in results):
            raise ProblemDetailError(status=503, code="durable_store_unavailable",
                                     detail="durable store unavailable",
                                     retryable=True, headers={"Retry-After": "5"})
        receipt = IB.build_receipt(frame, results, received_at=received_at,
                                   adapter=self.adapter_name,
                                   adapter_version=self.adapter_version)
        body = encode_json(receipt)
        if self.before_receipt_store is not None:
            self.before_receipt_store()
        store.batch_store_receipt(
            idempotency_scope=scope,
            request_fingerprint=fingerprint,
            response_status=200,
            response_body=body,
            site_id=cred.site_id,
            principal_id=cred.principal_id,
            batch_id=str(frame.get("batch_id")),
            request_id=request_id,
        )
        return 200, body, {"Content-Type": IB.RECEIPT_MEDIA_TYPE, "X-Request-ID": request_id}

    def _require_store(self) -> "SqliteDurableRecordStore":
        if not isinstance(self.durable_store, SqliteDurableRecordStore):
            raise ProblemDetailError(status=503, code="durable_store_unavailable",
                                     detail="batch ingest requires sqlite durable storage",
                                     retryable=True, headers={"Retry-After": "5"})
        if self.credential_store is None:
            raise ProblemDetailError(status=503, code="credential_store_unavailable",
                                     detail=self.load_error or "batch credential store unavailable",
                                     retryable=True, headers={"Retry-After": "5"})
        return self.durable_store

    def _authenticate(self, authorization: Optional[str]) -> BatchCredential:
        assert self.credential_store is not None
        return self.credential_store.authenticate(authorization)

    def _authenticate_clip(self, authorization: Optional[str]) -> BatchCredential:
        cred = self._authenticate(authorization)
        if not cred.can_upload_clip:
            raise ProblemDetailError(status=403, code="scope_missing",
                                     detail="credential lacks ingest:write and clip:write")
        return cred

    def _clip_root(self, cred: BatchCredential, upload_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ProblemDetailError(status=404, code="upload_not_found",
                                     detail="clip upload was not found")
        return os.path.join(self.raw_root, "clip_uploads", cred.site_id,
                            cred.device_id or cred.principal_id, upload_id)

    @staticmethod
    def _clip_meta_path(root: str) -> str:
        return os.path.join(root, "meta.json")

    def _load_clip_meta(self, root: str) -> Dict[str, Any]:
        try:
            with open(self._clip_meta_path(root), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except FileNotFoundError as exc:
            raise ProblemDetailError(status=404, code="upload_not_found",
                                     detail="clip upload was not found") from exc
        except ValueError as exc:
            raise ProblemDetailError(status=503, code="store_unavailable",
                                     detail="clip upload metadata is unreadable",
                                     retryable=True,
                                     headers={"Retry-After": "5"}) from exc
        if not isinstance(meta, dict):
            raise ProblemDetailError(status=503, code="store_unavailable",
                                     detail="clip upload metadata is invalid",
                                     retryable=True,
                                     headers={"Retry-After": "5"})
        return meta

    def _write_clip_meta(self, root: str, meta: Mapping[str, Any]) -> None:
        os.makedirs(root, exist_ok=True)
        self._write_json(self._clip_meta_path(root), meta)

    @staticmethod
    def _clip_response(meta: Mapping[str, Any], *, request_id: str,
                       status: int = 200) -> tuple[int, str, Dict[str, str]]:
        body = {
            "upload_schema_version": CU.CLIP_UPLOAD_SCHEMA_MAJOR,
            "upload_id": meta.get("upload_id"),
            "state": meta.get("state"),
            "chunk_bytes": meta.get("chunk_bytes"),
            "expected_chunks": meta.get("expected_chunks"),
            "received_chunks": len(meta.get("chunks") or {}),
            "received_bytes": meta.get("bytes_received", 0),
        }
        if meta.get("clip_key"):
            body["clip_key"] = meta.get("clip_key")
        if meta.get("pool_path"):
            body["pool_path"] = meta.get("pool_path")
        if meta.get("reason"):
            body["reason"] = meta.get("reason")
        return status, encode_json(body), {
            "Content-Type": CU.STATUS_MEDIA_TYPE,
            "X-Request-ID": request_id,
        }

    def clip_init(self, *, raw: bytes, content_type: str, authorization: Optional[str],
                  request_id: str) -> tuple[int, str, Dict[str, str]]:
        store = self._require_store()
        cred = self._authenticate_clip(authorization)
        if content_type.split(";", 1)[0].strip().lower() not in (
            CU.INIT_MEDIA_TYPE, "application/json",
        ):
            raise ProblemDetailError(status=415, code="unsupported_media_type",
                                     detail="unsupported clip init media type")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProblemDetailError(status=400, code="undecodable_body",
                                     detail="clip init body is not UTF-8 JSON") from exc
        if not isinstance(body, Mapping):
            raise ProblemDetailError(status=400, code="not_an_object",
                                     detail="clip init body must be a JSON object")
        credential_device_id = str(body.get("device_id")) if cred.site_scoped else cred.device_id
        reason = CU.validate_init(body, credential_device_id=credential_device_id)
        if reason is not None:
            raise ProblemDetailError(status=422, code=reason, detail="clip init was refused")
        upload_id = str(body["upload_id"])
        if self.promoter._receipt_exists(upload_id):
            raise ProblemDetailError(status=409, code="clip_key_purged",
                                     detail="clip key was already purged")
        from hear import clips as clip_store
        current = clip_store.read_index(self.promoter.pool_root).get(upload_id)
        if current is not None and current.get("outcome") == "stored":
            raise ProblemDetailError(status=409, code="clip_key_already_stored",
                                     detail="clip key was already promoted")
        root = self._clip_root(cred, upload_id)
        chunk_bytes = int(body.get("chunk_bytes", CU.CHUNK_BYTES))
        expected_chunks = CU.chunk_count(int(body["clip_bytes"]), chunk_bytes)
        existing = None
        try:
            existing = self._load_clip_meta(root)
        except ProblemDetailError as exc:
            if exc.status != 404:
                raise
        if existing is not None:
            same = all(existing.get(key) == body.get(key) for key in (
                "device_id", "node", "boot", "sample", "clip_basename",
                "clip_bytes", "chunk_bytes", "upload_source", "upload_id",
            ))
            if not same:
                raise ProblemDetailError(status=409, code="idempotency_conflict",
                                         detail="upload_id was reused with different metadata")
            return self._clip_response(existing, request_id=request_id)
        now = utc_now()
        meta: Dict[str, Any] = {
            "upload_schema_version": CU.CLIP_UPLOAD_SCHEMA_MAJOR,
            "site_id": cred.site_id,
            "principal_id": cred.principal_id,
            "credential_scope": cred.scope,
            "key_id": cred.key_id,
            "device_id": str(body["device_id"]),
            "node": str(body["node"]),
            "boot": str(body["boot"]),
            "sample": int(body["sample"]),
            "clip_basename": str(body["clip_basename"]),
            "clip_bytes": int(body["clip_bytes"]),
            "chunk_bytes": chunk_bytes,
            "expected_chunks": expected_chunks,
            "upload_source": str(body["upload_source"]),
            "upload_id": upload_id,
            "state": CU.STATE_OPEN,
            "chunks": {},
            "bytes_received": 0,
            "created_at": now,
            "updated_at": now,
        }
        self._write_clip_meta(root, meta)
        return self._clip_response(meta, request_id=request_id, status=201)

    def clip_status(self, *, upload_id: str, authorization: Optional[str],
                    request_id: str) -> tuple[int, str, Dict[str, str]]:
        cred = self._authenticate_clip(authorization)
        meta = self._load_clip_meta(self._clip_root(cred, upload_id))
        return self._clip_response(meta, request_id=request_id)

    def clip_chunk(self, *, upload_id: str, chunk_index: int, raw: bytes,
                   content_type: str, chunk_sha256: Optional[str],
                   authorization: Optional[str],
                   request_id: str) -> tuple[int, str, Dict[str, str]]:
        cred = self._authenticate_clip(authorization)
        if content_type.split(";", 1)[0].strip().lower() != CU.CHUNK_MEDIA_TYPE:
            raise ProblemDetailError(status=415, code="unsupported_media_type",
                                     detail="unsupported clip chunk media type")
        root = self._clip_root(cred, upload_id)
        meta = self._load_clip_meta(root)
        if CU.is_terminal(str(meta.get("state"))):
            return self._clip_response(meta, request_id=request_id)
        expected_len = CU.expected_chunk_bytes(
            chunk_index, int(meta["clip_bytes"]), int(meta["chunk_bytes"]))
        if len(raw) != expected_len:
            raise ProblemDetailError(status=422, code="chunk_length_mismatch",
                                     detail="clip chunk length did not match its byte range")
        digest = hashlib.sha256(raw).hexdigest()
        if chunk_sha256 != digest:
            raise ProblemDetailError(status=422, code="chunk_digest_mismatch",
                                     detail="clip chunk digest did not match")
        chunks = dict(meta.get("chunks") or {})
        existing = chunks.get(str(chunk_index))
        if isinstance(existing, Mapping):
            if existing.get("sha256") == digest and existing.get("bytes") == len(raw):
                return self._clip_response(meta, request_id=request_id)
            raise ProblemDetailError(status=409, code="chunk_conflict",
                                     detail="chunk index already has different bytes")
        chunk_path = os.path.join(root, f"{chunk_index:04d}.chunk")
        tmp = f"{chunk_path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, chunk_path)
        chunks[str(chunk_index)] = {"sha256": digest, "bytes": len(raw)}
        meta["chunks"] = chunks
        meta["bytes_received"] = int(meta.get("bytes_received") or 0) + len(raw)
        meta["state"] = CU.STATE_RECEIVING
        meta["updated_at"] = utc_now()
        self._write_clip_meta(root, meta)
        return self._clip_response(meta, request_id=request_id)

    def clip_complete(self, *, upload_id: str, raw: bytes, content_type: str,
                      authorization: Optional[str],
                      request_id: str) -> tuple[int, str, Dict[str, str]]:
        store = self._require_store()
        cred = self._authenticate_clip(authorization)
        if content_type.split(";", 1)[0].strip().lower() not in (
            CU.COMPLETE_MEDIA_TYPE, "application/json",
        ):
            raise ProblemDetailError(status=415, code="unsupported_media_type",
                                     detail="unsupported clip complete media type")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProblemDetailError(status=400, code="undecodable_body",
                                     detail="clip complete body is not UTF-8 JSON") from exc
        if not isinstance(body, Mapping):
            raise ProblemDetailError(status=400, code="not_an_object",
                                     detail="clip complete body must be a JSON object")
        root = self._clip_root(cred, upload_id)
        meta = self._load_clip_meta(root)
        reason = CU.validate_complete(
            body,
            bytes_received=int(meta.get("bytes_received") or 0),
            chunks_received=len(meta.get("chunks") or {}),
            expected_chunks=int(meta["expected_chunks"]),
            clip_bytes=int(meta["clip_bytes"]),
        )
        if reason is not None:
            raise ProblemDetailError(status=422, code=reason, detail="clip complete was refused")
        assembled = os.path.join(root, "assembled.wav")
        tmp = f"{assembled}.{os.getpid()}.tmp"
        h = hashlib.sha256()
        with open(tmp, "wb") as out:
            for idx in range(int(meta["expected_chunks"])):
                with open(os.path.join(root, f"{idx:04d}.chunk"), "rb") as inp:
                    while True:
                        chunk = inp.read(64 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                        out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if h.hexdigest() != body.get("sha256"):
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise ProblemDetailError(status=422, code="clip_sha256_mismatch",
                                     detail="assembled clip digest did not match")
        os.replace(tmp, assembled)
        wav_bytes = Path(assembled).read_bytes()
        envelope = self._clip_envelope(meta=meta, cred=cred, received_at=utc_now(),
                                       wav_bytes=wav_bytes)
        verdict = EV.validate(envelope, require_event_id_match=True)
        if not verdict.ok:
            raise ProblemDetailError(status=422, code=verdict.reasons[0],
                                     detail="assembled clip envelope was refused")
        redacted = self.promoter.redact_audio_fields(envelope)
        duplicate = store.batch_persist_event(
            str(envelope["event_id"]), cred.site_id, str(envelope["device_id"]),
            str(envelope["source"]), str(envelope["kind"]), envelope.get("observed_at"),
            str(envelope["received_at"]), verdict.dispatchable, None, encode_json(redacted),
            "clip-upload:" + upload_id, 0, cred.principal_id, cred.scope, cred.key_id)
        promotion = self.promoter.ensure(store, str(envelope["event_id"]), envelope)
        meta["state"] = promotion.state if promotion.state in (CU.STATE_PROMOTED, CU.STATE_PURGED) \
            else CU.STATE_REFUSED
        meta["clip_key"] = promotion.clip_key
        meta["pool_path"] = promotion.pool_path
        meta["reason"] = promotion.reason or ("duplicate" if duplicate else None)
        meta["updated_at"] = utc_now()
        if meta["state"] in (CU.STATE_PROMOTED, CU.STATE_PURGED):
            shutil.rmtree(root, ignore_errors=True)
        else:
            self._write_clip_meta(root, meta)
        return self._clip_response(meta, request_id=request_id)

    def clip_abort(self, *, upload_id: str, authorization: Optional[str],
                   request_id: str) -> tuple[int, str, Dict[str, str]]:
        cred = self._authenticate_clip(authorization)
        root = self._clip_root(cred, upload_id)
        meta = self._load_clip_meta(root)
        meta["state"] = CU.STATE_ABORTED
        meta["updated_at"] = utc_now()
        self._write_clip_meta(root, meta)
        shutil.rmtree(root, ignore_errors=True)
        return self._clip_response(meta, request_id=request_id)

    def _clip_envelope(self, *, meta: Mapping[str, Any], cred: BatchCredential,
                       received_at: str, wav_bytes: bytes) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "event_id": "",
            "source": "node-http",
            "site_id": cred.site_id,
            "device_id": str(meta["device_id"]),
            "device_class": "esp32s3-i2s-gps",
            "firmware_version": "unknown",
            "observed_at": None,
            "received_at": received_at,
            "clock": {"valid": False, "tier": "monotonic", "sigma_ns": 0},
            "kind": "clip",
            "schema_version": 1,
            "payload": {
                "clip": "/clips/%s" % str(meta["clip_basename"]),
                "wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
                "fs_hz": 48000.0,
                "upload_source": str(meta["upload_source"]),
                "clip_why": "push-upload",
                "record_key": "clip-upload:%s" % str(meta["upload_id"]),
            },
            "raw_ref": None,
            "adapter": {"name": CLIP_UPLOAD_ADAPTER_NAME, "version": self.adapter_version},
            "producer": {
                "boot_id": str(meta["boot"]),
                "sequence": int(meta["sample"]),
                "cursor": "clip-upload:%s" % str(meta["upload_id"]),
            },
        }
        envelope["event_id"] = EV.derive_event_id(envelope)
        return envelope

    def promote_pending(self, limit: int = 64) -> Dict[str, int]:
        out = {"attempted": 0, "promoted": 0, "failed": 0}
        store = self._require_store()
        for row in store.batch_unpromoted_events(limit):
            out["attempted"] += 1
            try:
                envelope = json.loads(row.envelope_json)
                if not isinstance(envelope, Mapping):
                    raise BatchPromotionError(
                        f"{row.event_id}: stored envelope is not a JSON object")
                self.promoter.ensure(store, row.event_id, envelope)
                if self.promoter.carries_clip_audio(envelope):
                    self._scrub_promoted_item(
                        store=store,
                        event_id=row.event_id,
                        envelope=envelope,
                        raw_ref=row.raw_ref,
                    )
                    self._delete_batch_frame_refs(
                        site_id=row.site_id,
                        device_id=row.device_id,
                        batch_id=row.batch_id,
                    )
            except Exception:
                out["failed"] += 1
                logger.exception("batch promotion replay failed for %s", row.event_id)
                continue
            out["promoted"] += 1
        return out

    def _persist_items(self, *, store: "SqliteDurableRecordStore", frame: Mapping[str, Any],
                       raw_messages: Sequence[Any], provisional: Sequence[IB.ItemResult],
                       cred: BatchCredential, received_at: str,
                       request_id: str) -> list[IB.ItemResult]:
        results: list[IB.ItemResult] = []
        batch_id = str(frame.get("batch_id"))
        for seed, item in zip(provisional, raw_messages):
            item_raw = _json_bytes(item)
            item_raw_ref = _retain_batch_bytes(
                self.raw_root, cred.site_id, str(frame.get("device_id")), batch_id, item_raw,
                suffix=f"item-{seed.index}.json")
            if seed.classification == IB.TRANSLATION_REQUIRED:
                results.append(self._persist_translated_item(
                    store=store, frame=frame, item=item, seed=seed, cred=cred,
                    received_at=received_at, raw_ref=item_raw_ref, request_id=request_id))
                continue
            if seed.status == "refused":
                refused = IB.ItemResult(seed.index, "refused", event_id=None,
                                        dispatchable=False, reasons=seed.reasons,
                                        raw_ref=item_raw_ref, classification=seed.classification)
                store.batch_record_item_refusal(
                    raw=item_raw, reasons=refused.reasons, source="batch-http",
                    adapter=self.adapter_name, received_at=received_at, batch_id=batch_id,
                    item_index=seed.index, raw_ref=item_raw_ref, site_id=cred.site_id,
                    device_id=str(frame.get("device_id")), principal_id=cred.principal_id,
                    credential_scope=cred.scope, key_id=cred.key_id,
                    classification=seed.classification, event_id=None, request_id=request_id)
                results.append(refused)
                continue
            envelope = dict(item)
            accepted = IB.ItemResult(seed.index, seed.status, event_id=seed.event_id,
                                     dispatchable=seed.dispatchable, reasons=seed.reasons,
                                     raw_ref=item_raw_ref, classification=seed.classification)
            try:
                duplicate = store.batch_persist_event(
                    accepted.event_id or "", cred.site_id, str(envelope.get("device_id")),
                    str(envelope.get("source")), str(envelope.get("kind")),
                    envelope.get("observed_at"), received_at, accepted.dispatchable,
                    item_raw_ref, encode_json(envelope), batch_id, seed.index,
                    cred.principal_id, cred.scope, cred.key_id)
            except sqlite3.Error:
                results.append(IB.ItemResult(seed.index, "deferred",
                                             reasons=["durable_store_unavailable"],
                                             raw_ref=item_raw_ref,
                                             classification=seed.classification))
                break
            if duplicate:
                results.append(IB.ItemResult(seed.index, "duplicate", event_id=accepted.event_id,
                                             dispatchable=accepted.dispatchable, reasons=[],
                                             raw_ref=item_raw_ref,
                                             classification=seed.classification))
            else:
                results.append(accepted)
        if len(results) < len(provisional):
            for seed, item in zip(provisional[len(results):], raw_messages[len(results):]):
                deferred = IB.ItemResult(seed.index, "deferred",
                                         reasons=["durable_store_unavailable"],
                                         raw_ref=None,
                                         classification=seed.classification)
                if seed.classification != IB.TRANSLATION_REQUIRED and seed.status == "refused":
                    deferred = IB.ItemResult(seed.index, "deferred",
                                             reasons=["durable_store_unavailable"],
                                             classification=seed.classification)
                results.append(deferred)
        return results

    def _promotion_envelope(self, *, item: Any, result: IB.ItemResult, frame: Mapping[str, Any],
                            cred: BatchCredential, received_at: str) -> Mapping[str, Any]:
        if result.classification == IB.TRANSLATED:
            return translate_legacy_batch_item(
                item, frame=frame, credential=cred, received_at=received_at,
                adapter_name=self.adapter_name, adapter_version=self.adapter_version,
                raw_ref=result.raw_ref or "",
            )
        if not isinstance(item, Mapping):
            raise BatchPromotionError(
                f"batch item {result.index} is not an object and cannot be promoted")
        return item

    def _raw_ref_path(self, raw_ref: Optional[str]) -> Optional[str]:
        if not raw_ref:
            return None
        return os.path.join(self.raw_root, raw_ref)

    def _batch_raw_dir(self, *, site_id: Optional[str], device_id: Optional[str],
                       batch_id: Optional[str]) -> Optional[str]:
        if not site_id or not device_id or not batch_id:
            return None
        return os.path.join(self.raw_root, "raw", site_id, device_id, batch_id)

    @staticmethod
    def _write_json(path: str, payload: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _scrub_promoted_item(self, *, store: "SqliteDurableRecordStore", event_id: str,
                             envelope: Mapping[str, Any], raw_ref: Optional[str]) -> None:
        redacted = self.promoter.redact_audio_fields(envelope)
        store.batch_update_envelope(event_id, encode_json(redacted))
        raw_path = self._raw_ref_path(raw_ref)
        if raw_path:
            self._write_json(raw_path, redacted)

    def _scrub_batch_frame(self, *, frame: Mapping[str, Any], site_id: str,
                           device_id: str) -> None:
        frame_paths = self._frame_raw_refs(
            site_id=site_id,
            device_id=device_id,
            batch_id=str(frame.get("batch_id") or ""),
        )
        redacted = self.promoter.redact_audio_fields(frame)
        for path in frame_paths:
            self._write_json(path, redacted)

    def _delete_batch_frame_refs(self, *, site_id: Optional[str], device_id: Optional[str],
                                 batch_id: Optional[str]) -> None:
        for path in self._frame_raw_refs(site_id=site_id, device_id=device_id, batch_id=batch_id):
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue

    def _frame_raw_refs(self, *, site_id: Optional[str], device_id: Optional[str],
                        batch_id: Optional[str]) -> list[str]:
        if not site_id or not device_id or not batch_id:
            return []
        root = os.path.join(self.raw_root, "raw", site_id, device_id)
        if not os.path.isdir(root):
            return []
        matches: list[str] = []
        for dirname in os.listdir(root):
            if not dirname.startswith("frame-"):
                continue
            candidate = os.path.join(root, dirname)
            if not os.path.isdir(candidate):
                continue
            for name in os.listdir(candidate):
                path = os.path.join(candidate, name)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        doc = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(doc, Mapping) and str(doc.get("batch_id") or "") == str(batch_id):
                    matches.append(path)
        return matches

    def _promote_results(self, *, store: "SqliteDurableRecordStore", frame: Mapping[str, Any],
                         raw_messages: Sequence[Any], results: Sequence[IB.ItemResult],
                         cred: BatchCredential, received_at: str) -> None:
        scrubbed_frame = False
        for result in results:
            if result.status not in ("accepted", "duplicate") or not result.event_id:
                continue
            envelope = self._promotion_envelope(
                item=raw_messages[result.index],
                result=result,
                frame=frame,
                cred=cred,
                received_at=received_at,
            )
            self.promoter.ensure(store, result.event_id, envelope)
            if self.promoter.carries_clip_audio(envelope):
                self._scrub_promoted_item(
                    store=store,
                    event_id=result.event_id,
                    envelope=envelope,
                    raw_ref=result.raw_ref,
                )
                scrubbed_frame = True
        if scrubbed_frame:
            self._scrub_batch_frame(
                frame=frame,
                site_id=cred.site_id,
                device_id=str(frame.get("device_id")),
            )

    def _persist_translated_item(self, *, store: "SqliteDurableRecordStore",
                                 frame: Mapping[str, Any], item: Any, seed: IB.ItemResult,
                                 cred: BatchCredential, received_at: str, raw_ref: str,
                                 request_id: str) -> IB.ItemResult:
        try:
            envelope = translate_legacy_batch_item(
                item, frame=frame, credential=cred, received_at=received_at,
                adapter_name=self.adapter_name, adapter_version=self.adapter_version,
                raw_ref=raw_ref)
        except RequestError as exc:
            refused = IB.resolve_translation(
                seed, status="refused", reasons=[_request_error_reason(exc)], raw_ref=raw_ref)
            store.batch_record_item_refusal(
                raw=_json_bytes(item), reasons=refused.reasons, source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=str(frame.get("batch_id")), item_index=seed.index, raw_ref=raw_ref,
                site_id=cred.site_id, device_id=str(frame.get("device_id")),
                principal_id=cred.principal_id, credential_scope=cred.scope,
                key_id=cred.key_id, classification=IB.TRANSLATED, event_id=None,
                request_id=request_id)
            return refused
        verdict = EV.validate(envelope, require_event_id_match=True)
        if not verdict.ok:
            refused = IB.resolve_translation(seed, status="refused", reasons=verdict.reasons,
                                             raw_ref=raw_ref)
            store.batch_record_item_refusal(
                raw=_json_bytes(item), reasons=refused.reasons, source="batch-http",
                adapter=self.adapter_name, received_at=received_at,
                batch_id=str(frame.get("batch_id")), item_index=seed.index, raw_ref=raw_ref,
                site_id=cred.site_id, device_id=str(frame.get("device_id")),
                principal_id=cred.principal_id, credential_scope=cred.scope,
                key_id=cred.key_id, classification=IB.TRANSLATED,
                event_id=envelope.get("event_id"), request_id=request_id)
            return refused
        result = IB.resolve_translation(seed, status="accepted", event_id=envelope["event_id"],
                                        dispatchable=verdict.dispatchable, raw_ref=raw_ref)
        try:
            duplicate = store.batch_persist_event(
                result.event_id or "", cred.site_id, str(envelope.get("device_id")),
                str(envelope.get("source")), str(envelope.get("kind")),
                envelope.get("observed_at"), received_at, result.dispatchable, raw_ref,
                encode_json(envelope), str(frame.get("batch_id")), seed.index,
                cred.principal_id, cred.scope, cred.key_id)
        except sqlite3.Error:
            return IB.resolve_translation(seed, status="deferred",
                                          reasons=["durable_store_unavailable"],
                                          raw_ref=raw_ref)
        if duplicate:
            return IB.resolve_translation(seed, status="duplicate", event_id=result.event_id,
                                          dispatchable=result.dispatchable, raw_ref=raw_ref)
        return result


class DurableReplayWorker:
    """Small stoppable background thread that periodically retries pending durable records.

    Shared by ReceiverServer and hear_mqtt_bridge so a durable record left behind by a Redis
    outage is replayed while the process stays up, not only at the next process startup. A no-op
    (no thread is started) when durability is disabled or ``interval_s`` is not positive.
    """

    def __init__(self, store: HeartbeatReceiverStore, interval_s: float,
                 limit: int = DURABLE_REPLAY_LIMIT, name: str = "durable-replay"):
        self.store = store
        self.interval_s = interval_s
        self.limit = limit
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        if interval_s > 0 and store.durable_store.enabled:
            self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        # Woken early (instead of a plain sleep) so stop() can return promptly.
        while not self._stop.wait(self.interval_s):
            try:
                self.store.replay_pending(limit=self.limit)
            except Exception:
                logger.exception("background durable replay attempt failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            if self.thread.is_alive():
                raise RuntimeError(
                    f"durable replay worker {self.thread.name!r} did not stop within {timeout}s")
            self.thread = None

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class DurablePruneWorker:
    """Small stoppable background thread that periodically reclaims acknowledged durable rows.

    Shared by ReceiverServer and hear_mqtt_bridge, mirroring DurableReplayWorker. A no-op (no
    thread is started) when durability is disabled, ``interval_s`` is not positive, or
    ``retention_days`` is not positive -- so the previous "keep everything forever" behavior is
    preserved for anyone who does not opt in.
    """

    def __init__(self, store: HeartbeatReceiverStore, interval_s: float,
                 retention_days: int = DURABLE_RETENTION_DAYS, name: str = "durable-prune"):
        self.store = store
        self.interval_s = interval_s
        self.retention_days = retention_days
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        if interval_s > 0 and retention_days > 0 and store.durable_store.enabled:
            self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        # Woken early (instead of a plain sleep) so stop() can return promptly.
        while not self._stop.wait(self.interval_s):
            try:
                pruned = self.store.durable_store.prune_acknowledged(self.retention_days)
                if pruned:
                    logger.info("pruned %d acknowledged durable record(s) older than %dd",
                                pruned, self.retention_days)
                refused = self.store.durable_store.prune_refusals(self.retention_days)
                if refused:
                    logger.info("pruned %d quarantined refusal(s) older than %dd",
                                refused, self.retention_days)
            except Exception:
                logger.exception("background durable prune attempt failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            if self.thread.is_alive():
                raise RuntimeError(
                    f"durable prune worker {self.thread.name!r} did not stop within {timeout}s")
            self.thread = None

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, server_address, handler_cls, store: HeartbeatReceiverStore,
                 durable_replay_interval_s: float = 0.0,
                 durable_replay_limit: int = DURABLE_REPLAY_LIMIT,
                 durable_prune_interval_s: float = 0.0,
                 durable_retention_days: int = DURABLE_RETENTION_DAYS):
        super().__init__(server_address, handler_cls)
        self.store = store
        self._replay_worker = DurableReplayWorker(
            store, durable_replay_interval_s, durable_replay_limit, name="hear-heartbeat-replay")
        self._prune_worker = DurablePruneWorker(
            store, durable_prune_interval_s, durable_retention_days, name="hear-heartbeat-prune")

    @property
    def _replay_thread(self) -> Optional[threading.Thread]:
        return self._replay_worker.thread

    def server_close(self) -> None:
        self._replay_worker.stop()
        self._prune_worker.stop()
        super().server_close()


def utc_now() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _scoped_record_uid(telemetry_path: str, device_id: str, idempotency_key: str) -> str:
    """Device-scoped durable identity for an idempotency-keyed payload.

    ``idempotency_key`` is only unique *within* a device (nodes mint it locally, e.g. from a boot
    id plus a counter), so using it as the whole identity made two devices that happened to pick
    the same key collide: the second device's payload was silently dropped as a duplicate and the
    caller got a 204 for a record that was never stored. Hashing the tuple keeps the identity a
    single opaque string -- no schema, transport or Redis contract changes -- while making it
    impossible for one device's key to shadow another's.
    """
    scope = "\x1f".join(("v2", telemetry_path, device_id, idempotency_key))
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def _record_uid(payload: Mapping[str, Any]) -> str:
    key = _idempotency_key(payload)
    if key is not None:
        return _scoped_record_uid(
            str(payload.get("telemetry_path") or ""),
            str(payload.get("device_id") or ""),
            key,
        )
    # Unkeyed payloads already hash the whole body, device_id included, so they were never
    # cross-device ambiguous and their identity is deliberately left byte-for-byte unchanged.
    return hashlib.sha256(encode_json(payload).encode("utf-8")).hexdigest()


def _idempotency_key(payload: Mapping[str, Any]) -> Optional[str]:
    raw = payload.get("idempotency_key")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw or None


def _error_text(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:512]


def add_durable_store_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--durable-store", default=DURABLE_STORE, choices=DURABLE_BACKENDS)
    ap.add_argument("--durable-db", default=DURABLE_DB)
    ap.add_argument("--durable-replay-limit", type=int, default=DURABLE_REPLAY_LIMIT)
    ap.add_argument("--durable-replay-interval-s", type=float, default=DURABLE_REPLAY_INTERVAL_S,
                    help="seconds between background replays of pending durable records while "
                         "the server is up; <= 0 disables the background worker")
    ap.add_argument("--durable-retention-days", type=int, default=DURABLE_RETENTION_DAYS,
                    help="days to keep an already cache-synced durable record before the "
                         "background prune worker reclaims it; <= 0 disables pruning and keeps "
                         "every record forever")
    ap.add_argument("--durable-prune-interval-s", type=float, default=DURABLE_PRUNE_INTERVAL_S,
                    help="seconds between background prune sweeps of acknowledged durable "
                         "records; <= 0 disables the background worker")


def make_durable_store(kind: str = DURABLE_STORE,
                       path: str = DURABLE_DB) -> DurableRecordStore:
    backend = (kind or "none").strip().lower()
    if backend == "none":
        return DurableRecordStore()
    if backend == "sqlite":
        return SqliteDurableRecordStore(path)
    if backend == "postgres":
        raise ValueError(
            "HEAR_DURABLE_STORE=postgres is not implemented in phase 0; keep the sqlite outbox "
            "or add psycopg and explicit DDL first"
        )
    raise ValueError(f"unknown durable store {kind!r}; expected one of {', '.join(DURABLE_BACKENDS)}")


def _require_non_empty_string(row: Mapping[str, Any], key: str, idx: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"credential {idx} {key} must be a non-empty string")
    return value.strip()


def _choice_field(row: Mapping[str, Any], key: str, choices: Sequence[str], idx: int) -> str:
    value = _require_non_empty_string(row, key, idx)
    if value not in choices:
        raise ValueError(f"credential {idx} {key} must be one of {', '.join(choices)}")
    return value


def _hex_field(row: Mapping[str, Any], key: str, idx: int) -> str:
    value = _require_non_empty_string(row, key, idx).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"credential {idx} {key} must be a 64-char sha256 hex digest")
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _retain_batch_bytes(root: str, site_id: str, device_id: str, batch_id: str,
                        raw: bytes, *, suffix: str) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    rel = os.path.join("raw", site_id, device_id, batch_id, f"{digest}-{suffix}")
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.exists(full):
        return rel.replace(os.sep, "/")
    tmp = full + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, full)
    return rel.replace(os.sep, "/")


def _frame_error_status(reasons: Sequence[str]) -> int:
    set_reasons = set(reasons)
    if "credential_missing" in set_reasons:
        return 401
    if {"batch_schema_version_unsupported", "batch_empty", "batch_too_many_items",
        "batch_id_invalid", "device_identity_mismatch"} & set_reasons:
        return 422
    return 400


def _problem_title(status: int) -> str:
    return {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        409: "Conflict",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        422: "Unprocessable Content",
        429: "Too Many Requests",
        503: "Service Unavailable",
    }.get(status, "Error")


def _request_error_reason(exc: RequestError) -> str:
    mapping = {
        "telemetry_schema_version must be 1": "schema_version_unsupported",
        "ts is required when time.valid is true": "field_missing",
        "ts must be null when time.valid is false": "clock_valid_without_observed_at",
    }
    return mapping.get(str(exc), "field_missing")


def _require_object(payload: Any, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError(f"{label} must be an object")
    return payload


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{key} must be a non-empty string")
    return value.strip()


def _require_ident(payload: Mapping[str, Any], key: str, regex: re.Pattern[str], label: str) -> str:
    value = _require_string(payload, key)
    if not regex.fullmatch(value):
        raise RequestError(f"{key} must be a short {label} token")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{key} must be an integer")
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be a boolean")
    return value


def _validate_time_block(body: Mapping[str, Any]) -> Dict[str, Any]:
    time_state = _require_object(body.get("time"), "time")
    time_valid = _require_bool(time_state, "valid")
    state = time_state.get("state")
    if state is not None:
        if not isinstance(state, str) or state not in _CLOCK_STATES:
            raise RequestError("time.state must be one of %s" % ", ".join(sorted(_CLOCK_STATES)))
        _require_ident(time_state, "boot_id", _BOOT_ID_RE, "boot id")
        for key in ("discontinuity_flags",):
            if time_state.get(key) is not None:
                _require_int(time_state, key)
        if time_valid:
            if state == "FAULT":
                raise RequestError("time.state FAULT requires time.valid false")
            for key in ("sync_sigma_ns", "anchor_age_us", "boot_epoch_us"):
                if time_state.get(key) is None:
                    raise RequestError(f"time.{key} is required when time.state is present")
                _require_int(time_state, key)
        else:
            if state != "FAULT":
                raise RequestError("time.valid false requires time.state FAULT when stated")
            for key in ("sync_sigma_ns", "anchor_age_us", "boot_epoch_us"):
                if time_state.get(key) is not None:
                    raise RequestError(f"time.{key} must be null when time.valid is false")
    return time_state


def _legacy_event_time_block(body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The time block a pre-#156 firmware would have sent on an event, inferred from ``ts``.

    That firmware emitted ``"time": {"valid": <bool>}`` on heartbeats but no time key at all on
    events (firmware/hear_node/hear_push_payload.h at f3b2ec9), so a node still running it has
    every heartbeat accepted and every event refused. The clock state it *did* express on an
    event is the ts field itself: hear_push_ts_field() writes the ISO timestamp when the clock is
    valid and a literal null when it is not. That is a total mapping, so the block can be
    reconstructed exactly -- no guessing, and nothing else about the body is treated as valid.

    Returns None for any other shape (a numeric ts, an empty string, a missing key), which keeps
    the refusal for genuinely malformed input.
    """
    if "time" in body:
        return None
    if "ts" not in body:
        return None
    ts = body["ts"]
    if ts is None:
        return {"valid": False, "source": LEGACY_TIME_SOURCE}
    if isinstance(ts, str) and ts.strip():
        return {"valid": True, "source": LEGACY_TIME_SOURCE}
    return None


def _validate_common(payload: Any, telemetry_path: str, *, require_gps: bool = True,
                     allow_legacy_time: bool = False) -> Dict[str, Any]:
    body = _require_object(payload, "payload")
    if allow_legacy_time:
        legacy_time = _legacy_event_time_block(body)
        if legacy_time is not None:
            # _require_object hands back the caller's own dict, so canonicalise onto a copy: the
            # quarantine and the bridge must still see the bytes the device actually sent.
            body = dict(body)
            body["time"] = legacy_time
    if body.get("telemetry_path") != telemetry_path:
        raise RequestError(f"telemetry_path must be {telemetry_path!r}")
    if body.get("telemetry_schema_version") != 1:
        raise RequestError("telemetry_schema_version must be 1")
    _require_ident(body, "device_id", _DEVICE_ID_RE, "device id")
    _require_ident(body, "class", _CLASS_RE, "class")
    _require_ident(body, "fw_version", _FW_RE, "firmware id")
    _require_int(body, "uptime_s")

    if require_gps:
        gps = _require_object(body.get("gps"), "gps")
        _require_int(gps, "fix")

    time_state = _validate_time_block(body)
    time_valid = time_state["valid"]
    ts = body.get("ts")
    if time_valid:
        if ts is None:
            raise RequestError("ts is required when time.valid is true")
        _require_string(body, "ts")
    elif ts is not None:
        raise RequestError("ts must be null when time.valid is false")
    return body


def validate_heartbeat_payload(payload: Any) -> Dict[str, Any]:
    body = _validate_common(payload, "hear/heartbeat", require_gps=True)
    counters = _require_object(body.get("counters"), "counters")
    for key in ("scene_rows_written", "dets_rows_written", "clips_written", "clips_evicted"):
        _require_int(counters, key)
    if body.get("wifi") is not None:
        wifi = _require_object(body.get("wifi"), "wifi")
        if wifi.get("rssi_dbm") is not None:
            _require_int(wifi, "rssi_dbm")
    return body


def validate_event_payload(payload: Any) -> Dict[str, Any]:
    body = _validate_common(payload, "hear/event", require_gps=False, allow_legacy_time=True)
    event_type = _require_string(body, "event_type")
    if event_type not in _EVENT_TYPES:
        raise RequestError("event_type must be one of %s" % ", ".join(sorted(_EVENT_TYPES)))
    _require_int(body, "event_seq")
    event = _require_object(body.get("event"), "event")
    if event_type == "clip_written":
        _require_string(event, "clip_basename")
        _require_int(event, "clips_written")
        _require_int(event, "clips_evicted")
    else:
        _require_int(event, "dets_rows_written")
        _require_int(event, "batch_rows")
    return body


def translate_legacy_batch_item(item: Any, *, frame: Mapping[str, Any], credential: BatchCredential,
                                received_at: str, adapter_name: str, adapter_version: str,
                                raw_ref: str) -> Dict[str, Any]:
    body = _require_object(item, "item")
    telemetry_path = body.get("telemetry_path")
    translated = dict(body)
    if telemetry_path == "hear/heartbeat":
        checked = validate_heartbeat_payload(translated)
        kind = "heartbeat"
        producer_sequence = None
        source = "import" if credential.site_scoped or frame.get("adapter") else "node-http"
        payload = {
            "telemetry_schema_version": 1,
            "uptime_s": checked["uptime_s"],
            "gps_fix": checked["gps"]["fix"],
            "wifi": checked.get("wifi"),
            "counters": checked["counters"],
            "legacy": checked,
        }
    elif telemetry_path == "hear/event":
        if translated.get("event_type") == "dets":
            translated["event_type"] = "detection_batch_ready"
        checked = validate_event_payload(translated)
        event_type = str(checked["event_type"])
        kind = "clip" if event_type == "clip_written" else "detection"
        producer_sequence = checked["event_seq"]
        source = "import" if credential.site_scoped or frame.get("adapter") else "node-http"
        payload = {
            "telemetry_schema_version": 1,
            "event_type": event_type,
            "event_seq": checked["event_seq"],
            "event": checked["event"],
            "legacy": checked,
        }
    else:
        raise RequestError("telemetry_path must be 'hear/heartbeat' or 'hear/event'")
    time_block = checked.get("time") or {}
    if time_block.get("valid") is True:
        tier = "gps_pps"
        sigma_ns = int(time_block.get("sync_sigma_ns") or 0)
    else:
        tier = "monotonic"
        sigma_ns = 0
    envelope = {
        "event_id": "",
        "source": source,
        "site_id": credential.site_id,
        "device_id": checked["device_id"],
        "device_class": checked["class"],
        "firmware_version": checked["fw_version"],
        "observed_at": checked.get("ts"),
        "received_at": received_at,
        "clock": {
            "valid": bool(time_block.get("valid")),
            "tier": tier,
            "sigma_ns": sigma_ns,
        },
        "kind": kind,
        "schema_version": EV.SCHEMA_MAJOR,
        "payload": payload,
        "raw_ref": raw_ref,
        "adapter": {"name": adapter_name, "version": adapter_version},
        "producer": {},
    }
    for key, value in (
        ("boot_id", time_block.get("boot_id")),
        ("boot_epoch_us", time_block.get("boot_epoch_us")),
        ("sequence", producer_sequence),
    ):
        if value is not None:
            envelope["producer"][key] = value
    if "boot_id" not in envelope["producer"] and frame.get("producer"):
        producer = frame.get("producer") or {}
        if isinstance(producer, Mapping):
            if producer.get("boot_id") is not None:
                envelope["producer"]["boot_id"] = producer.get("boot_id")
            if producer.get("boot_epoch_us") is not None:
                envelope["producer"]["boot_epoch_us"] = producer.get("boot_epoch_us")
    if not envelope["producer"]:
        envelope.pop("producer")
    envelope["event_id"] = EV.derive_event_id(envelope)
    return envelope


def _refused_device_id(raw_body: Any) -> str:
    """Best-effort device id for a refused body; "unknown" when it cannot be read.

    Never trusted as identity -- it only groups refusals and feeds the per-source rate limit.
    """
    try:
        if isinstance(raw_body, bytes):
            parsed = json.loads(raw_body.decode("utf-8"))
        elif isinstance(raw_body, str):
            parsed = json.loads(raw_body)
        else:
            parsed = raw_body
    except (ValueError, UnicodeDecodeError):
        return "unknown"
    if isinstance(parsed, dict):
        device_id = parsed.get("device_id")
        if isinstance(device_id, str) and _DEVICE_ID_RE.fullmatch(device_id.strip()):
            return device_id.strip()
    return "unknown"


def make_handler(store: HeartbeatReceiverStore,
                 max_body_bytes: int = MAX_BODY_BYTES,
                 auth_token: Optional[str] = AUTH_TOKEN,
                 socket_timeout_s: float = SOCKET_TIMEOUT_S,
                 ingest_metrics: Optional[IO.IngestMetrics] = None,
                 batch_adapter: Optional[BatchIngestAdapter] = None,
                 before_batch_response: Optional[
                     Callable[[int, str, Dict[str, str], str], None]
                 ] = None) -> type[BaseHTTPRequestHandler]:
    ingest_metrics = ingest_metrics or IO.IngestMetrics()

    class ReceiverHandler(BaseHTTPRequestHandler):
        server_version = "hear-heartbeat-receiver/1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(socket_timeout_s)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            request_id = self.request_id()
            clip_status = re.fullmatch(r"/v1/ingest/clips/([0-9a-f]{32})", path)
            if clip_status:
                try:
                    if batch_adapter is None:
                        raise ProblemDetailError(
                            status=503,
                            code="clip_upload_unavailable",
                            detail="clip upload route is not configured",
                            retryable=True,
                            headers={"Retry-After": "5"},
                        )
                    self.send_adapter_response(batch_adapter.clip_status(
                        upload_id=clip_status.group(1),
                        authorization=self.headers.get("Authorization"),
                        request_id=request_id,
                    ))
                except ProblemDetailError as exc:
                    self.send_problem(exc, request_id)
                return
            if path == "/metrics":
                self.send_prometheus(ingest_metrics.render_prometheus())
                return
            if path != "/healthz":
                self.send_error(404)
                return
            snapshot = store.health_snapshot()
            self.send_json({
                "status": "ok",
                "service": "hear-heartbeat-receiver",
                "redis_target": snapshot["redis_target"],
                "durable_store": snapshot["durable_store"],
                "refusals": snapshot["refusals"],
            })

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            self.raw_body = None
            request_id = self.request_id()
            clip_complete = re.fullmatch(r"/v1/ingest/clips/([0-9a-f]{32})/complete", path)
            if path == CU.INIT_ROUTE or clip_complete:
                try:
                    if batch_adapter is None:
                        raise ProblemDetailError(
                            status=503,
                            code="clip_upload_unavailable",
                            detail="clip upload route is not configured",
                            retryable=True,
                            headers={"Retry-After": "5"},
                        )
                    self.require_idempotency_key()
                    if clip_complete:
                        result = batch_adapter.clip_complete(
                            upload_id=clip_complete.group(1),
                            raw=self.read_raw_body(CU.MAX_CLIP_BYTES),
                            content_type=self.required_header("Content-Type"),
                            authorization=self.headers.get("Authorization"),
                            request_id=request_id,
                        )
                    else:
                        result = batch_adapter.clip_init(
                            raw=self.read_raw_body(8192),
                            content_type=self.required_header("Content-Type"),
                            authorization=self.headers.get("Authorization"),
                            request_id=request_id,
                        )
                    self.send_adapter_response(result)
                except ProblemDetailError as exc:
                    self.send_problem(exc, request_id)
                except RequestError as exc:
                    self.send_problem(
                        ProblemDetailError(status=exc.status,
                                           code="payload_too_large" if exc.status == 413 else
                                                "bad_request",
                                           detail=str(exc),
                                           retryable=exc.status in (408,)),
                        request_id)
                return
            if path in (BATCH_ROUTE, BATCH_ALIAS_ROUTE):
                try:
                    if batch_adapter is None:
                        raise ProblemDetailError(
                            status=503,
                            code="batch_ingest_unavailable",
                            detail="batch ingest route is not configured",
                            retryable=True,
                            headers={"Retry-After": "5"},
                        )
                    status, body, headers = batch_adapter.ingest(
                        path=path,
                        raw=self.read_raw_body(IB.MAX_BATCH_BYTES),
                        content_type=self.required_header("Content-Type"),
                        idempotency_key=self.require_idempotency_key(),
                        authorization=self.headers.get("Authorization"),
                        content_encoding=self.headers.get("Content-Encoding"),
                        request_id=request_id,
                    )
                except ProblemDetailError as exc:
                    self.send_problem(exc, request_id)
                    return
                except RequestError as exc:
                    self.send_problem(
                        ProblemDetailError(status=exc.status,
                                           code="payload_too_large" if exc.status == 413 else
                                                "bad_request",
                                           detail=str(exc),
                                           retryable=exc.status in (408,)),
                        request_id)
                    return
                except Exception as exc:  # pragma: no cover - crash window is tested below
                    self.close_connection = True
                    logger.exception("batch ingest request crashed: %s", exc)
                    return
                encoded = body.encode("utf-8")
                if before_batch_response is not None:
                    before_batch_response(status, body, headers, request_id)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            routes: Dict[str, tuple[Callable[[Any], Dict[str, Any]], Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
                "/api/hear/heartbeat": (validate_heartbeat_payload, store.write_heartbeat),
                "/api/hear/event": (validate_event_payload, store.write_event),
            }
            route = routes.get(path)
            if route is None:
                self.send_error(404)
                return
            validator, writer = route
            telemetry_path = path.replace("/api/", "", 1)
            try:
                self.require_auth(auth_token)
                writer(validator(self.read_json_body(max_body_bytes)))
            except RequestError as exc:
                # An authenticated caller whose body this build rejects gets a durable refusal
                # row (R4): the message is refused, but it stops disappearing. 401s are excluded
                # deliberately -- an unauthenticated stranger must not be able to write to the
                # telemetry volume at all.
                if exc.status != 401 and self.raw_body is not None:
                    store.record_refusal(telemetry_path, _refused_device_id(self.raw_body),
                                         "lan_http", str(exc), self.raw_body)
                self.send_json({"error": str(exc)}, exc.status)
                return
            except Exception as exc:  # pragma: no cover - exercised with a fake failing store.
                self.send_json({"error": "redis write failed", "detail": str(exc)}, 503)
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            self.raw_body = None
            request_id = self.request_id()
            chunk = re.fullmatch(r"/v1/ingest/clips/([0-9a-f]{32})/chunks/(\d+)", path)
            if not chunk:
                self.send_error(404)
                return
            try:
                if batch_adapter is None:
                    raise ProblemDetailError(
                        status=503,
                        code="clip_upload_unavailable",
                        detail="clip upload route is not configured",
                        retryable=True,
                        headers={"Retry-After": "5"},
                    )
                # No Idempotency-Key here on purpose: docs/phase4-push-clip-upload.md gives chunk
                # PUT its own dedup key -- X-Hear-Chunk-SHA256 plus the byte range implied by
                # chunk_index -- and clipupload.py has no chunk_idempotency_key to match, unlike
                # init/complete. Requiring one here rejected every spec-conformant firmware chunk
                # with 400 idempotency_key_missing (real hardware hit this; the test suite's
                # shared request helper always sent one, so it never caught it here).
                self.send_adapter_response(batch_adapter.clip_chunk(
                    upload_id=chunk.group(1),
                    chunk_index=int(chunk.group(2)),
                    raw=self.read_raw_body(CU.MAX_CHUNK_BYTES),
                    content_type=self.required_header("Content-Type"),
                    chunk_sha256=self.headers.get(CU.CHUNK_DIGEST_HEADER),
                    authorization=self.headers.get("Authorization"),
                    request_id=request_id,
                ))
            except ProblemDetailError as exc:
                self.send_problem(exc, request_id)
            except ValueError as exc:
                self.send_problem(ProblemDetailError(status=422, code=str(exc),
                                                     detail="clip chunk was refused"),
                                  request_id)
            except RequestError as exc:
                self.send_problem(
                    ProblemDetailError(status=exc.status,
                                       code="payload_too_large" if exc.status == 413 else
                                            "bad_request",
                                       detail=str(exc), retryable=exc.status in (408,)),
                    request_id)

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            request_id = self.request_id()
            clip = re.fullmatch(r"/v1/ingest/clips/([0-9a-f]{32})", path)
            if not clip:
                self.send_error(404)
                return
            try:
                if batch_adapter is None:
                    raise ProblemDetailError(
                        status=503,
                        code="clip_upload_unavailable",
                        detail="clip upload route is not configured",
                        retryable=True,
                        headers={"Retry-After": "5"},
                    )
                self.require_idempotency_key()
                self.send_adapter_response(batch_adapter.clip_abort(
                    upload_id=clip.group(1),
                    authorization=self.headers.get("Authorization"),
                    request_id=request_id,
                ))
            except ProblemDetailError as exc:
                self.send_problem(exc, request_id)

        def require_auth(self, token: Optional[str]) -> None:
            if not token:
                return
            got = self.headers.get("X-Hear-Token")
            if got != token:
                raise RequestError("unauthorized", status=401)

        def read_json_body(self, max_body_bytes: int) -> Any:
            raw = self.read_raw_body(max_body_bytes)
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise RequestError("request body must be UTF-8 JSON") from exc
            except json.JSONDecodeError as exc:
                raise RequestError("malformed JSON") from exc

        def read_raw_body(self, max_body_bytes: int) -> bytes:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or self.headers.get("Transfer-Encoding"):
                raise RequestError("missing Content-Length")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise RequestError("invalid Content-Length") from exc
            if length < 0:
                raise RequestError("invalid Content-Length")
            try:
                raw = self.rfile.read(length)
            except (TimeoutError, socket.timeout) as exc:
                raise RequestError("request body read timed out", status=408) from exc
            # Kept before parsing: a body that fails to decode or parse is exactly the one worth
            # quarantining, and what is quarantined is the received bytes, not a repaired copy.
            self.raw_body = raw
            if len(raw) != length:
                raise RequestError("truncated request body")
            if length > max_body_bytes:
                raise RequestError(
                    f"payload too large ({length} > {max_body_bytes} bytes)", status=413)
            return raw

        def require_idempotency_key(self) -> str:
            raw = self.headers.get("Idempotency-Key")
            if not isinstance(raw, str) or not raw.strip():
                raise ProblemDetailError(status=400, code="idempotency_key_missing",
                                         detail="Idempotency-Key is required")
            value = raw.strip()
            if len(value) > 128:
                raise ProblemDetailError(status=400, code="idempotency_key_invalid",
                                         detail="Idempotency-Key must be 128 characters or fewer")
            return value

        def required_header(self, name: str) -> str:
            value = self.headers.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ProblemDetailError(status=400, code=f"{name.lower()}_missing",
                                         detail=f"{name} is required")
            return value.strip()

        def request_id(self) -> str:
            got = self.headers.get("X-Request-ID")
            if isinstance(got, str) and got.strip():
                return got.strip()
            return "req_" + uuid.uuid4().hex

        def send_problem(self, exc: ProblemDetailError, request_id: str) -> None:
            detail = {
                "type": f"https://api.dama.example/problems/{exc.code}",
                "title": _problem_title(exc.status),
                "status": exc.status,
                "code": exc.code,
                "detail": exc.detail,
                "instance": request_id,
                "retryable": exc.retryable,
            }
            if exc.field_errors:
                detail["field_errors"] = exc.field_errors
            body = encode_json(detail).encode("utf-8")
            self.send_response(exc.status)
            self.send_header("Content-Type", "application/problem+json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", request_id)
            for key, value in exc.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, data: Mapping[str, Any], code: int = 200) -> None:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_adapter_response(self, result: tuple[int, str, Dict[str, str]]) -> None:
            status, body, headers = result
            encoded = body.encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_prometheus(self, body: bytes, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ReceiverHandler


def make_redis_client(redis_host: str = REDIS_HOST, redis_port: int = REDIS_PORT,
                      redis_pass: Optional[str] = REDIS_PASS) -> redis.Redis:
    return redis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_pass or None,
        decode_responses=True,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
        retry_on_timeout=False,
    )


def create_server(bind: str, port: int, store: HeartbeatReceiverStore,
                  max_body_bytes: int = MAX_BODY_BYTES,
                  auth_token: Optional[str] = AUTH_TOKEN,
                  socket_timeout_s: float = SOCKET_TIMEOUT_S,
                  ingest_metrics: Optional[IO.IngestMetrics] = None,
                  batch_adapter: Optional[BatchIngestAdapter] = None,
                  before_batch_response: Optional[
                      Callable[[int, str, Dict[str, str], str], None]
                  ] = None,
                  durable_replay_interval_s: float = 0.0,
                  durable_replay_limit: int = DURABLE_REPLAY_LIMIT,
                  durable_prune_interval_s: float = 0.0,
                  durable_retention_days: int = DURABLE_RETENTION_DAYS) -> ReceiverServer:
    if ingest_metrics is None and batch_adapter is not None:
        ingest_metrics = batch_adapter.metrics
    elif ingest_metrics is not None and batch_adapter is not None:
        batch_adapter.metrics = ingest_metrics
    return ReceiverServer(
        (bind, port),
        make_handler(store, max_body_bytes=max_body_bytes,
                     auth_token=auth_token, socket_timeout_s=socket_timeout_s,
                     ingest_metrics=ingest_metrics, batch_adapter=batch_adapter,
                     before_batch_response=before_batch_response),
        store,
        durable_replay_interval_s=durable_replay_interval_s,
        durable_replay_limit=durable_replay_limit,
        durable_prune_interval_s=durable_prune_interval_s,
        durable_retention_days=durable_retention_days,
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=API_PORT)
    ap.add_argument("--redis-host", default=REDIS_HOST)
    ap.add_argument("--redis-port", type=int, default=REDIS_PORT)
    ap.add_argument("--redis-pass", default=REDIS_PASS)
    ap.add_argument("--heartbeat-ttl", type=int, default=HEARTBEAT_TTL_S)
    ap.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    ap.add_argument("--event-stream-key", default=EVENT_STREAM_KEY)
    ap.add_argument("--event-stream-maxlen", type=int, default=EVENT_STREAM_MAXLEN)
    ap.add_argument("--auth-token", default=AUTH_TOKEN)
    ap.add_argument("--socket-timeout-s", type=float, default=SOCKET_TIMEOUT_S)
    ap.add_argument("--batch-credentials-file", default=BATCH_CREDENTIALS_FILE)
    ap.add_argument("--batch-raw-dir", default=BATCH_RAW_DIR)
    add_durable_store_args(ap)
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    auth_token = _configured_auth_token(args.auth_token)
    target = f"{args.redis_host}:{args.redis_port}"
    store = HeartbeatReceiverStore(
        make_redis_client(args.redis_host, args.redis_port, args.redis_pass),
        heartbeat_ttl_s=args.heartbeat_ttl,
        event_stream_key=args.event_stream_key,
        event_stream_maxlen=args.event_stream_maxlen,
        redis_target=target,
        durable_store=make_durable_store(args.durable_store, args.durable_db),
    )
    replay = store.replay_pending(limit=args.durable_replay_limit)
    batch_adapter = BatchIngestAdapter.from_config(
        store.durable_store, args.batch_credentials_file, raw_root=args.batch_raw_dir,
        adapter_version=BATCH_ADAPTER_VERSION)
    try:
        promoted = batch_adapter.promote_pending(limit=256)
        if promoted["attempted"]:
            print("[hear-heartbeat] batch promotion bootstrap attempted=%d promoted=%d failed=%d"
                  % (promoted["attempted"], promoted["promoted"], promoted["failed"]))
    except Exception:
        logger.exception("batch promotion bootstrap failed")
    print(f"[hear-heartbeat] Redis target configured -> {target}")
    print(f"[hear-heartbeat] durable backend={store.durable_store.backend} path={store.durable_store.path}")
    if batch_adapter.credential_store is None:
        print("[hear-heartbeat] batch ingest credentials unavailable; route will refuse: %s"
              % (batch_adapter.load_error or "unknown error"))
    if replay["attempted"] or replay["failed"]:
        print("[hear-heartbeat] replay pending attempted=%d synced=%d failed=%d remaining=%d"
              % (replay["attempted"], replay["synced"], replay["failed"],
                 replay["remaining_pending"]))
    print(f"[hear-heartbeat] Listening on http://{args.bind}:{args.port}")
    server = create_server(
        args.bind, args.port, store, max_body_bytes=args.max_body_bytes,
        auth_token=auth_token, socket_timeout_s=args.socket_timeout_s,
        batch_adapter=batch_adapter,
        durable_replay_interval_s=args.durable_replay_interval_s,
        durable_replay_limit=args.durable_replay_limit,
        durable_prune_interval_s=args.durable_prune_interval_s,
        durable_retention_days=args.durable_retention_days,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
