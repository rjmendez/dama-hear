#!/usr/bin/env python3
"""Tiny hear_node heartbeat/event receiver that writes Redis state.

With ``HEAR_DURABLE_STORE=sqlite`` every accepted heartbeat/event is first committed to a local
append-only SQLite ledger and only then reflected into Redis. Redis stays the mixed-version cache
surface, and pending cache publishes can be replayed after a crash or cache outage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

import redis

logger = logging.getLogger("hear-heartbeat")

REDIS_HOST = os.environ.get("REDIS_HOST", "audit-redis.infra.svc.cluster.local")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS = os.environ.get("REDIS_PASS")
AUTH_TOKEN = os.environ.get("HEAR_HEARTBEAT_TOKEN")


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


class DurableStoreError(RuntimeError):
    """The durable ledger could not record or replay a record."""


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
                 socket_timeout_s: float = SOCKET_TIMEOUT_S) -> type[BaseHTTPRequestHandler]:
    class ReceiverHandler(BaseHTTPRequestHandler):
        server_version = "hear-heartbeat-receiver/1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(socket_timeout_s)

        def do_GET(self) -> None:
            if urlparse(self.path).path != "/healthz":
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
            self.raw_body = None
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

        def require_auth(self, token: Optional[str]) -> None:
            if not token:
                return
            got = self.headers.get("X-Hear-Token")
            if got != token:
                raise RequestError("unauthorized", status=401)

        def read_json_body(self, max_body_bytes: int) -> Any:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                raise RequestError("missing Content-Length")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise RequestError("invalid Content-Length") from exc
            if length < 0:
                raise RequestError("invalid Content-Length")
            if length > max_body_bytes:
                raise RequestError(
                    f"payload too large ({length} > {max_body_bytes} bytes)", status=413)
            try:
                raw = self.rfile.read(length)
            except (TimeoutError, socket.timeout) as exc:
                raise RequestError("request body read timed out", status=408) from exc
            # Kept before parsing: a body that fails to decode or parse is exactly the one worth
            # quarantining, and what is quarantined is the received bytes, not a repaired copy.
            self.raw_body = raw
            if len(raw) != length:
                raise RequestError("truncated request body")
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise RequestError("request body must be UTF-8 JSON") from exc
            except json.JSONDecodeError as exc:
                raise RequestError("malformed JSON") from exc

        def send_json(self, data: Mapping[str, Any], code: int = 200) -> None:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
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
                  durable_replay_interval_s: float = 0.0,
                  durable_replay_limit: int = DURABLE_REPLAY_LIMIT,
                  durable_prune_interval_s: float = 0.0,
                  durable_retention_days: int = DURABLE_RETENTION_DAYS) -> ReceiverServer:
    return ReceiverServer(
        (bind, port),
        make_handler(store, max_body_bytes=max_body_bytes,
                     auth_token=auth_token, socket_timeout_s=socket_timeout_s),
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
    print(f"[hear-heartbeat] Redis target configured -> {target}")
    print(f"[hear-heartbeat] durable backend={store.durable_store.backend} path={store.durable_store.path}")
    if replay["attempted"] or replay["failed"]:
        print("[hear-heartbeat] replay pending attempted=%d synced=%d failed=%d remaining=%d"
              % (replay["attempted"], replay["synced"], replay["failed"],
                 replay["remaining_pending"]))
    print(f"[hear-heartbeat] Listening on http://{args.bind}:{args.port}")
    server = create_server(
        args.bind, args.port, store, max_body_bytes=args.max_body_bytes,
        auth_token=auth_token, socket_timeout_s=args.socket_timeout_s,
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
