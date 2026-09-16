#!/usr/bin/env python3
"""Rehearses the Phase 2 SQLite -> Postgres backfill (plan §6). Never writes the source ledger.

This is the M5 tool in its rehearsal generation: everything the real import does -- read-only
source access, a freeze watermark, batching, checkpoint/resume, poison quarantine, duplicate
accounting -- with two hard refusals wired into it so it cannot become the live import by
accident:

* the source is opened ``file:...?mode=ro`` with ``PRAGMA query_only=ON``, and
  :func:`assert_read_only` proves it by trying to write and requiring the failure;
* the destination, when it is a real server, must be a database named ``hear_test_<random>`` --
  the same scratch pattern tools/hear_durable_pg_testdb.py creates and drops. Pointed at any
  other database this tool refuses before it connects. Turning it into the live import is
  therefore a reviewed change to :data:`SCRATCH_ONLY`, not a flag someone can mistype.

Two destinations exist, deliberately:

``ModelSink``
    An in-memory model of ``hear.backfill_record()``'s contract (migration 0005). It lets the
    whole import, its idempotency and its reconciliation run in CI with no server at all.
    A model of a database is worth exactly as much as the evidence that it agrees with the
    database, so ``tests/test_hear_durable_cutover_rehearsal.py`` runs the same corpus through
    both sinks whenever a server is available and requires identical results.

``PsqlSink``
    The real function, on an ephemeral scratch database, through ``psql``. No driver dependency,
    same as the rest of the Phase 2 suite.

Reverse backfill (plan §9.1) is the rollback direction, Postgres -> SQLite. It is the only path
here that writes SQLite, it is dry-run by default, and it refuses to run unless the caller states
that the writer is stopped *and* the ledger is actually unlocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hear_durable_pg as PG  # noqa: E402
import hear_durable_pg_testdb as TESTDB  # noqa: E402

#: Destination databases this tool is allowed to write. Anything else is somebody's data.
SCRATCH_ONLY = TESTDB.SCRATCH_NAME_RE

INGEST_SOURCE = "backfill_sqlite"
TELEMETRY_PATHS = ("hear/heartbeat", "hear/event")
DEFAULT_BATCH = 1000


class BackfillRefused(RuntimeError):
    """The run was refused before it could touch anything."""


# ------------------------------------------------------------------------------------------------
# Source: read-only SQLite
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LegacyRow:
    sqlite_id: int
    device_id: str
    record_uid: str
    telemetry_path: str
    idempotency_key: Optional[str]
    body_json: str
    received_at: str
    receiver_schema_version: int
    acknowledged_at: Optional[str]

    @property
    def day(self) -> str:
        return self.received_at[:10]

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.body_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reject:
    sqlite_id: int
    record_uid: str
    reason: str


class LedgerReader:
    """Read-only access to a legacy ledger. Cannot write, prune or vacuum it."""

    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        if not self.path.exists():
            raise BackfillRefused(f"no such ledger: {self.path}")
        self._con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "LedgerReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def assert_read_only(self) -> None:
        """Proves the connection cannot write. A claim of read-only access is not one."""
        try:
            self._con.execute("CREATE TABLE _backfill_write_probe (x INTEGER)")
        except sqlite3.OperationalError:
            return
        raise BackfillRefused(f"{self.path} opened writable; refusing to read a live ledger "
                              "through a connection that could modify it")

    def freeze_id(self) -> int:
        """``max(durable_records.id)``: the boundary between backfill and live dual-write."""
        row = self._con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM durable_records").fetchone()
        return int(row["m"])

    def count(self, freeze_id: int) -> int:
        row = self._con.execute("SELECT COUNT(*) AS c FROM durable_records WHERE id <= ?",
                                (freeze_id,)).fetchone()
        return int(row["c"])

    def rows(self, freeze_id: int, after_id: int = 0) -> Iterator[LegacyRow]:
        """Rows ``after_id < id <= freeze_id`` in id order, each with its acknowledgement time.

        ``acknowledged_at`` is ``MIN(created_at)`` over the succeeded attempts, which is the
        moment Redis accepted the record. A failed attempt is not an acknowledgement.
        """
        cursor = self._con.execute(
            """
            SELECT r.id, r.device_id, r.record_uid, r.telemetry_path, r.idempotency_key,
                   r.payload_json, r.received_at, r.receiver_schema_version,
                   (SELECT MIN(a.created_at) FROM cache_attempts a
                     WHERE a.record_uid = r.record_uid AND a.outcome = 'succeeded') AS acked_at
              FROM durable_records r
             WHERE r.id > ? AND r.id <= ?
             ORDER BY r.id
            """,
            (after_id, freeze_id))
        for row in cursor:
            yield LegacyRow(
                sqlite_id=int(row["id"]),
                device_id=str(row["device_id"]),
                record_uid=str(row["record_uid"]),
                telemetry_path=str(row["telemetry_path"]),
                idempotency_key=None if row["idempotency_key"] is None else str(row["idempotency_key"]),
                body_json=str(row["payload_json"]),
                received_at=str(row["received_at"]),
                receiver_schema_version=int(row["receiver_schema_version"]),
                acknowledged_at=None if row["acked_at"] is None else str(row["acked_at"]),
            )

    def refusals(self) -> list[dict]:
        cursor = self._con.execute(
            "SELECT refusal_uid, telemetry_path, device_id, source, reason, body_text, "
            "body_bytes, truncated, occurrences, received_at FROM refused_messages "
            "ORDER BY refusal_uid")
        return [dict(row) for row in cursor]


def classify(row: LegacyRow) -> Optional[str]:
    """``None`` if the row is importable, otherwise why it is poison.

    The rules are the destination's own constraints, checked here so one bad row costs a
    quarantine line instead of a rolled-back batch: ``body_json`` must be a JSON object, its
    ``device_id`` must agree with the column the identity is built from, and the telemetry path
    must be one the schema's CHECK accepts.
    """
    try:
        body = json.loads(row.body_json)
    except ValueError:
        return "body_json is not valid JSON"
    if not isinstance(body, dict):
        return "body_json is not an object"
    claimed = body.get("device_id")
    if claimed is not None and str(claimed) != row.device_id:
        return f"body device_id {claimed!r} disagrees with the row's device_id {row.device_id!r}"
    if row.telemetry_path not in TELEMETRY_PATHS:
        return f"telemetry_path {row.telemetry_path!r} is not one the schema accepts"
    return None


# ------------------------------------------------------------------------------------------------
# Destinations
# ------------------------------------------------------------------------------------------------
class Sink(Protocol):
    def backfill_record(self, source_ledger: str, row: LegacyRow, tenant_id: str = "default") -> bool: ...
    def advance(self, source_ledger: str, last_sqlite_id: int, completed: bool = False) -> None: ...
    def watermark(self, source_ledger: str) -> int: ...


@dataclass
class ModelRecord:
    tenant_id: str
    device_id: str
    record_uid: str
    legacy_record_uid: str
    telemetry_path: str
    body_json: str
    payload_sha256: str
    received_at: str
    receiver_schema_version: int
    state: str
    published_at: Optional[str]
    cache_target: Optional[str]
    ingest_source: str = INGEST_SOURCE
    publish_attempts: int = 0
    duplicate_arrivals: int = 0


@dataclass
class ModelSink:
    """An in-memory model of ``hear.backfill_record()`` (migration 0005).

    Identity is ``(tenant_id, device_id, record_uid)``; a second arrival of the same identity
    counts a duplicate and imports nothing; an acknowledged row lands ``published`` with the
    ledger's acknowledgement time and zero publish attempts, so the drain never re-sends it.
    """
    records: dict[tuple[str, str, str], ModelRecord] = field(default_factory=dict)
    watermarks: dict[str, dict] = field(default_factory=dict)

    def backfill_record(self, source_ledger: str, row: LegacyRow, tenant_id: str = "default") -> bool:
        key = PG.scoped_identity(row.device_id, row.record_uid, tenant_id)
        existing = self.records.get(key)
        if existing is not None:
            # The identity counts the duplicate arrival; the watermark does not. Migration 0005's
            # hear.backfill_record() only touches backfill_watermarks on an insert, so its
            # rows_skipped column stays 0 no matter how many duplicates a run meets -- the plan's
            # §6 table says otherwise, and the run report below is therefore the authoritative
            # skip count for reconciliation. Modelling the plan instead of the function here
            # would make this model disagree with the database it stands in for.
            existing.duplicate_arrivals += 1
            return False
        self.records[key] = ModelRecord(
            tenant_id=tenant_id, device_id=row.device_id, record_uid=row.record_uid,
            legacy_record_uid=row.record_uid, telemetry_path=row.telemetry_path,
            body_json=row.body_json, payload_sha256=row.payload_sha256,
            received_at=row.received_at, receiver_schema_version=row.receiver_schema_version,
            state="pending" if row.acknowledged_at is None else "published",
            published_at=row.acknowledged_at,
            cache_target=None if row.acknowledged_at is None else source_ledger)
        self._mark(source_ledger, imported=1)
        return True

    def _mark(self, source_ledger: str, imported: int = 0) -> None:
        mark = self.watermarks.setdefault(
            source_ledger, {"last_sqlite_id": 0, "rows_imported": 0, "rows_skipped": 0,
                            "completed": False})
        mark["rows_imported"] += imported

    def advance(self, source_ledger: str, last_sqlite_id: int, completed: bool = False) -> None:
        mark = self.watermarks.setdefault(
            source_ledger, {"last_sqlite_id": 0, "rows_imported": 0, "rows_skipped": 0,
                            "completed": False})
        mark["last_sqlite_id"] = max(mark["last_sqlite_id"], int(last_sqlite_id))
        mark["completed"] = mark["completed"] or bool(completed)

    def watermark(self, source_ledger: str) -> int:
        return int(self.watermarks.get(source_ledger, {}).get("last_sqlite_id", 0))

    # -- the reconciliation surface, in the shape the Postgres side reports it ---------------
    def snapshot(self) -> dict:
        return {
            "records": [
                {"device_id": r.device_id, "record_uid": r.record_uid,
                 "telemetry_path": r.telemetry_path, "day": r.received_at[:10],
                 "received_at": r.received_at, "state": r.state,
                 "published_at": r.published_at, "payload_sha256": r.payload_sha256,
                 "ingest_source": r.ingest_source, "cache_target": r.cache_target,
                 "legacy_record_uid": r.legacy_record_uid,
                 "publish_attempts": r.publish_attempts}
                for r in sorted(self.records.values(),
                                key=lambda r: (r.device_id, r.record_uid))
            ],
            "watermarks": self.watermarks,
        }

    def collisions(self) -> dict[str, list[str]]:
        return PG.legacy_collisions([(r.device_id, r.record_uid) for r in self.records.values()])


class PsqlSink:
    """The real ``hear.backfill_record()``, on a scratch database, through ``psql``."""

    def __init__(self, dsn: str):
        name = _database_name(dsn)
        if not SCRATCH_ONLY.match(name or ""):
            raise BackfillRefused(
                f"refusing to import into {name!r}: this rehearsal only writes scratch databases "
                f"matching {SCRATCH_ONLY.pattern}")
        self.dsn = dsn

    def backfill_record(self, source_ledger: str, row: LegacyRow, tenant_id: str = "default") -> bool:
        sql = (
            "SELECT hear.backfill_record("
            f"{_lit(source_ledger)}, {_lit(row.device_id)}, {_lit(row.record_uid)}, "
            f"{_lit(row.body_json)}, {_lit(row.telemetry_path)}, {_lit(row.received_at)}::timestamptz, "
            f"{int(row.receiver_schema_version)}::smallint, "
            f"{'NULL' if row.acknowledged_at is None else _lit(row.acknowledged_at) + '::timestamptz'}, "
            f"{'NULL' if row.idempotency_key is None else _lit(row.idempotency_key)}, "
            f"{_lit(tenant_id)})")
        return TESTDB.psql_value(self.dsn, sql) == "t"

    def advance(self, source_ledger: str, last_sqlite_id: int, completed: bool = False) -> None:
        TESTDB.psql_value(
            self.dsn,
            f"SELECT hear.backfill_advance({_lit(source_ledger)}, {int(last_sqlite_id)}, "
            f"{'true' if completed else 'false'}) IS NULL")

    def watermark(self, source_ledger: str) -> int:
        out = TESTDB.psql_value(
            self.dsn, "SELECT COALESCE((SELECT last_sqlite_id FROM hear.backfill_watermarks "
                      f"WHERE source_ledger = {_lit(source_ledger)}), 0)")
        return int(out or 0)

    def snapshot(self) -> dict:
        rows = TESTDB.psql_value(self.dsn, """
            SELECT COALESCE(json_agg(x ORDER BY x.device_id, x.record_uid)::text, '[]') FROM (
              SELECT r.device_id, r.record_uid, r.telemetry_path,
                     to_char(r.received_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                     to_char(r.received_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS received_at,
                     r.state::text AS state,
                     to_char(r.published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS published_at,
                     encode(r.payload_sha256, 'hex') AS payload_sha256,
                     r.ingest_source::text AS ingest_source, r.cache_target,
                     r.legacy_record_uid, r.publish_attempts
                FROM hear.durable_records r) x""")
        marks = TESTDB.psql_value(self.dsn, """
            SELECT COALESCE(json_object_agg(source_ledger, json_build_object(
                'last_sqlite_id', last_sqlite_id, 'rows_imported', rows_imported,
                'rows_skipped', rows_skipped, 'completed', completed_at IS NOT NULL))::text, '{}')
              FROM hear.backfill_watermarks""")
        return {"records": json.loads(rows), "watermarks": json.loads(marks)}

    def collisions(self) -> dict[str, list[str]]:
        out = TESTDB.psql_value(self.dsn, """
            SELECT COALESCE(json_object_agg(record_uid, device_ids)::text, '{}')
              FROM hear.legacy_uid_collisions""")
        return json.loads(out)


def _database_name(dsn: str) -> Optional[str]:
    if "://" in dsn:
        import urllib.parse
        return urllib.parse.urlsplit(dsn).path.lstrip("/") or None
    for token in dsn.split():
        if token.lower().startswith(("dbname=", "database=")):
            return token.split("=", 1)[1]
    return None


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ------------------------------------------------------------------------------------------------
# The run
# ------------------------------------------------------------------------------------------------
@dataclass
class BackfillRun:
    source_ledger: str
    freeze_id: int
    resumed_from: int
    rows_read: int = 0
    rows_imported: int = 0
    rows_skipped: int = 0
    published: int = 0
    pending: int = 0
    last_id: int = 0
    completed: bool = False
    dry_run: bool = True
    rejects: list[Reject] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """A run with any reject exits non-zero, even though it imported everything else."""
        return 1 if self.rejects else 0

    def as_dict(self) -> dict:
        return {
            "source_ledger": self.source_ledger, "freeze_id": self.freeze_id,
            "resumed_from": self.resumed_from, "rows_read": self.rows_read,
            "rows_imported": self.rows_imported, "rows_skipped": self.rows_skipped,
            "published": self.published, "pending": self.pending, "last_id": self.last_id,
            "completed": self.completed, "dry_run": self.dry_run,
            "rejects": [{"sqlite_id": r.sqlite_id, "record_uid": r.record_uid,
                         "reason": r.reason} for r in self.rejects],
            "exit_code": self.exit_code,
        }


def run_backfill(reader: LedgerReader, sink: Optional[Sink], source_ledger: str, *,
                 freeze_id: Optional[int] = None, dry_run: bool = True,
                 batch_size: int = DEFAULT_BATCH, resume: bool = True,
                 stop_after_rows: Optional[int] = None,
                 tenant_id: str = "default") -> BackfillRun:
    """Imports ``id <= freeze_id`` from ``reader`` into ``sink``.

    ``stop_after_rows`` exists for the interrupted-run drill (D5): it abandons the run *between*
    batches, exactly where a killed process would leave it, so the resumed run has to produce the
    same final state as an uninterrupted one.
    """
    reader.assert_read_only()
    freeze = reader.freeze_id() if freeze_id is None else int(freeze_id)
    start = sink.watermark(source_ledger) if (sink is not None and resume) else 0
    run = BackfillRun(source_ledger=source_ledger, freeze_id=freeze, resumed_from=start,
                      last_id=start, dry_run=dry_run)

    batch: list[LegacyRow] = []
    for row in reader.rows(freeze, after_id=start):
        run.rows_read += 1
        reason = classify(row)
        if reason is not None:
            run.rejects.append(Reject(row.sqlite_id, row.record_uid, reason))
            run.last_id = max(run.last_id, row.sqlite_id)
            continue
        batch.append(row)
        if len(batch) >= batch_size:
            _commit(batch, sink, source_ledger, run, dry_run, tenant_id)
            batch = []
            if stop_after_rows is not None and run.rows_read >= stop_after_rows:
                return run
    if batch:
        _commit(batch, sink, source_ledger, run, dry_run, tenant_id)
    if not dry_run and sink is not None:
        sink.advance(source_ledger, run.last_id, completed=True)
    run.completed = True
    return run


def _commit(batch: list[LegacyRow], sink: Optional[Sink], source_ledger: str, run: BackfillRun,
            dry_run: bool, tenant_id: str) -> None:
    for row in batch:
        if dry_run or sink is None:
            imported = True
        else:
            imported = sink.backfill_record(source_ledger, row, tenant_id)
        if imported:
            run.rows_imported += 1
            if row.acknowledged_at is None:
                run.pending += 1
            else:
                run.published += 1
        else:
            run.rows_skipped += 1
        run.last_id = max(run.last_id, row.sqlite_id)
    if not dry_run and sink is not None:
        sink.advance(source_ledger, run.last_id)


# ------------------------------------------------------------------------------------------------
# Reverse backfill (plan §9.1): Postgres -> a stopped SQLite ledger
# ------------------------------------------------------------------------------------------------
@dataclass
class ReverseRun:
    rows_restored: int = 0
    rows_present: int = 0
    acknowledgements_seeded: int = 0
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {"rows_restored": self.rows_restored, "rows_present": self.rows_present,
                "acknowledgements_seeded": self.acknowledgements_seeded, "dry_run": self.dry_run}


def ledger_is_locked(path: pathlib.Path | str) -> bool:
    """True when another process holds a write lock on the ledger -- i.e. the writer is running."""
    con = sqlite3.connect(str(path), timeout=0.1)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        con.close()


def run_reverse_backfill(records: list[dict], ledger_path: pathlib.Path | str, *,
                         dry_run: bool = True, writer_stopped: bool = False,
                         device_ids: Optional[set[str]] = None) -> ReverseRun:
    """Restores records the SQLite ledger does not have, plus the acknowledgements it lost.

    ``device_ids`` selects the records this ledger is responsible for. A deployment's ledger
    holds its own devices, and restoring another ledger's rows into it would invent history the
    writer never had.

    The only path in this tool that writes SQLite, and the reason M6 rollback has the same
    "no data loss" property as M3b: a record published while Postgres was primary exists in
    Postgres as ``published``, and SQLite must learn both the record and the fact that Redis
    already accepted it -- otherwise a rolled-back replay publishes it a second time.
    """
    if not dry_run and not writer_stopped:
        raise BackfillRefused(
            "reverse backfill writes the SQLite ledger and is only safe against a stopped writer; "
            "pass writer_stopped=True (--acknowledge-writer-stopped) after scaling the "
            "deployment to 0")
    if not dry_run and ledger_is_locked(ledger_path):
        raise BackfillRefused(
            f"{ledger_path} is locked by another process: the writer is still running")

    run = ReverseRun(dry_run=dry_run)
    con = sqlite3.connect(str(ledger_path))
    con.row_factory = sqlite3.Row
    try:
        known = {str(r["record_uid"]) for r in
                 con.execute("SELECT record_uid FROM durable_records")}
        acked = {str(r["record_uid"]) for r in
                 con.execute("SELECT DISTINCT record_uid FROM cache_attempts "
                             "WHERE outcome = 'succeeded'")}
        for record in sorted(records, key=lambda r: (r["received_at"], r["record_uid"])):
            if device_ids is not None and record["device_id"] not in device_ids:
                continue
            uid = str(record["record_uid"])
            present = uid in known
            if present:
                run.rows_present += 1
            else:
                run.rows_restored += 1
            needs_ack = record.get("state") == "published" and uid not in acked
            if dry_run:
                if needs_ack:
                    run.acknowledgements_seeded += 1
                continue
            if not present:
                body = json.loads(record["body_json"])
                con.execute(
                    "INSERT OR IGNORE INTO durable_records (record_uid, telemetry_path, "
                    "device_id, idempotency_key, payload_json, received_at, "
                    "receiver_schema_version, created_at, durable_schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, record["telemetry_path"], record["device_id"],
                     body.get("idempotency_key"), record["body_json"], record["received_at"],
                     int(record.get("receiver_schema_version", 1)), record["received_at"], 1))
                known.add(uid)
            if needs_ack:
                con.execute(
                    "INSERT INTO cache_attempts (record_uid, cache_target, outcome, created_at) "
                    "VALUES (?, ?, 'succeeded', ?)",
                    (uid, record.get("cache_target") or "redis", record["published_at"]))
                acked.add(uid)
                run.acknowledgements_seeded += 1
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return run


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, type=pathlib.Path, help="legacy SQLite ledger")
    ap.add_argument("--ledger-name", help="source_ledger key (default: the file's stem)")
    ap.add_argument("--dsn", help=f"scratch destination DSN; the database must match "
                                  f"{SCRATCH_ONLY.pattern}. Omitted: the in-memory model")
    ap.add_argument("--execute", action="store_true", help="import (default: dry run)")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--freeze-id", type=int, help="import id <= this (default: max(id) now)")
    ap.add_argument("--rejects", type=pathlib.Path, help="write quarantined rows here as JSONL")
    args = ap.parse_args(argv)

    ledger_name = args.ledger_name or args.source.stem
    sink: Optional[Sink] = PsqlSink(args.dsn) if args.dsn else ModelSink()
    with LedgerReader(args.source) as reader:
        run = run_backfill(reader, sink, ledger_name, freeze_id=args.freeze_id,
                           dry_run=not args.execute, batch_size=args.batch_size)
    if args.rejects and run.rejects:
        args.rejects.write_text(
            "\n".join(json.dumps({"sqlite_id": r.sqlite_id, "record_uid": r.record_uid,
                                  "reason": r.reason}, sort_keys=True) for r in run.rejects) + "\n",
            encoding="utf-8")
    print(json.dumps(run.as_dict(), indent=2, sort_keys=True))
    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
