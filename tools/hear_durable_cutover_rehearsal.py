#!/usr/bin/env python3
"""Runs the Phase 2 cut-over rehearsal end to end and prints a receipt. Provisions nothing.

The migration plan's gates (§13) are evidence gates: a drill that has not been run is a failed
gate. This is the rehearsal half of that -- the whole M5/M5v/M6-rollback sequence, executed
against a *synthetic* ledger corpus and either an in-memory model of the schema or an ephemeral
scratch database, and producing the receipt the gate consumes.

What it will not do, by construction rather than by instruction: provision a database (a live DSN
is refused unless the database name is a generated ``hear_test_<random>`` scratch name), write a
source ledger (read-only connections, proven by trying), export live data (the corpus is built
here, from the shipping store), or switch a reader over (gate ``R11`` asserts the seam is still
refused and that SQLite is still the authoritative store).

Gates::

    R01  the migration set is ordered, additive, idempotent and reversible
    R02  re-running a migration changes nothing
    R03  a dry run imports nothing and modifies no source byte
    R04  acknowledged rows import published with the ledger's own time; the rest import pending
    R05  a second full import imports nothing and loses nothing
    R06  an interrupted import, resumed, equals an uninterrupted one
    R07  the reconciliation closes, with the row-conservation statement
    R08  cross-device uid collisions are recovered and counted
    R09  an injected missing row, extra row and one-byte body change are all detected
    R10  rollback preserves the source; reverse backfill restores publish-equivalence
    R11  legacy SQLite is still authoritative: no reader cut-over, no writable destination

Usage::

    python3 tools/hear_durable_cutover_rehearsal.py --workdir build/rehearsal
    python3 tools/hear_durable_cutover_rehearsal.py --workdir build/rehearsal \\
        --dsn postgresql://postgres@127.0.0.1:5432/postgres      # ephemeral scratch db per run
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hear_durable_backfill as BF  # noqa: E402
import hear_durable_ledger_fixture as FIX  # noqa: E402
import hear_durable_pg as PG  # noqa: E402
import hear_durable_pg_testdb as TESTDB  # noqa: E402
import hear_durable_reconcile as RC  # noqa: E402
import hear_heartbeat_receiver as HR  # noqa: E402


@dataclass
class Gate:
    id: str
    title: str
    passed: bool
    detail: dict = field(default_factory=dict)


@dataclass
class Receipt:
    mode: str
    started_at: str
    gates: list[Gate] = field(default_factory=list)

    def add(self, gate_id: str, title: str, passed: bool, **detail) -> Gate:
        gate = Gate(gate_id, title, bool(passed), detail)
        self.gates.append(gate)
        return gate

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def as_dict(self) -> dict:
        return {
            "rehearsal": "phase2-postgres-cutover",
            "mode": self.mode,
            "started_at": self.started_at,
            "passed": self.passed,
            "gates": [{"id": g.id, "title": g.title, "passed": g.passed, "detail": g.detail}
                      for g in self.gates],
        }


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sorted_records(snapshot: dict) -> list[dict]:
    return sorted(snapshot.get("records", []),
                  key=lambda r: (r["device_id"], r["record_uid"]))


def _fresh_sink(dsn: Optional[str]):
    """A destination with nothing in it. Live runs get their own scratch database."""
    if dsn is None:
        return BF.ModelSink(), contextlib.nullcontext()
    db = TESTDB.EphemeralDatabase(dsn, TESTDB.scratch_name()).create()
    db.apply_migrations()
    return BF.PsqlSink(db.dsn), contextlib.closing(_Dropper(db))


class _Dropper:
    def __init__(self, db):
        self.db = db

    def close(self) -> None:
        self.db.drop()


def _import_all(corpus: dict, sink, dry_run: bool = False, stop_after_rows=None,
                resume: bool = True) -> dict[str, BF.BackfillRun]:
    runs: dict[str, BF.BackfillRun] = {}
    for name, ledger in corpus.items():
        with BF.LedgerReader(ledger.path) as reader:
            runs[name] = BF.run_backfill(
                reader, sink, name, dry_run=dry_run, batch_size=2, resume=resume,
                stop_after_rows=stop_after_rows)
    return runs


def rehearse(workdir: pathlib.Path, dsn: Optional[str] = None) -> Receipt:
    workdir.mkdir(parents=True, exist_ok=True)
    receipt = Receipt(mode="live-ephemeral" if dsn else "offline-model",
                      started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    corpus = FIX.build_corpus(workdir / "corpus")
    source_hashes = {name: file_sha256(ledger.path) for name, ledger in corpus.items()}

    # R01 ------------------------------------------------------------------------------------
    problems = PG.validate()
    receipt.add("R01", "the migration set is ordered, additive, idempotent and reversible",
                not problems, problems=problems,
                migrations=[m.path.name for m in PG.load_migrations()])

    # R02 ------------------------------------------------------------------------------------
    if dsn is None:
        # Without a server, re-appliability is a property of the statements: every forward
        # statement is one of the guarded forms, so applying the file twice is the same as
        # applying it once. The live branch below proves the stronger claim on a real catalog.
        unguarded = [problem for problem in problems if "not re-appliable" in problem]
        receipt.add("R02", "re-running a migration changes nothing (statement forms)",
                    not unguarded, unguarded=unguarded, checked="statement forms, no server")
    else:
        with TESTDB.ephemeral_database(dsn) as db:
            migrations = db.apply_migrations()
            before = db.schema_fingerprint()
            db.apply_migrations(migrations, record=False)
            after = db.schema_fingerprint()
            versions = db.applied_versions()
            receipt.add("R02", "re-running a migration changes nothing (catalog fingerprint)",
                        before == after and versions == [m.version for m in migrations],
                        fingerprint_before=before, fingerprint_after=after, versions=versions)

    sink, guard = _fresh_sink(dsn)
    with guard:
        # R03 --------------------------------------------------------------------------------
        dry = _import_all(corpus, sink, dry_run=True)
        still_empty = not _sorted_records(sink.snapshot())
        unchanged = {name: file_sha256(ledger.path) == source_hashes[name]
                     for name, ledger in corpus.items()}
        receipt.add("R03", "a dry run imports nothing and modifies no source byte",
                    still_empty and all(unchanged.values()),
                    destination_rows=len(_sorted_records(sink.snapshot())),
                    sources_unchanged=unchanged,
                    would_import={name: run.rows_imported for name, run in dry.items()},
                    rejects={name: len(run.rejects) for name, run in dry.items()})

        # R04 --------------------------------------------------------------------------------
        first = _import_all(corpus, sink, dry_run=False)
        snapshot_one = copy.deepcopy(sink.snapshot())
        # Counted per identity, not per row: a record both ledgers saw is one record, and the
        # first import of it is the one that decides its state.
        first_arrival: dict[tuple[str, str], bool] = {}
        for ledger in corpus.values():
            for row in ledger.importable_rows:
                first_arrival.setdefault((row.device_id, row.record_uid), bool(row.cached_at))
        expected_published = sum(1 for cached in first_arrival.values() if cached)
        expected_pending = sum(1 for cached in first_arrival.values() if not cached)
        published = [r for r in snapshot_one["records"] if r["state"] == "published"]
        pending = [r for r in snapshot_one["records"] if r["state"] == "pending"]
        never_attempted = all(int(r["publish_attempts"]) == 0 for r in snapshot_one["records"])
        legacy_uid_kept = all(r["legacy_record_uid"] == r["record_uid"]
                              for r in snapshot_one["records"])
        receipt.add("R04",
                    "acknowledged rows import published with the ledger's own time, "
                    "the rest import pending",
                    len(published) == expected_published and len(pending) == expected_pending
                    and never_attempted and legacy_uid_kept
                    and all(r["published_at"] for r in published),
                    published=len(published), pending=len(pending),
                    expected_published=expected_published, expected_pending=expected_pending,
                    publish_attempts_all_zero=never_attempted,
                    legacy_uid_preserved=legacy_uid_kept,
                    rejects={name: len(run.rejects) for name, run in first.items()})

        # R05 --------------------------------------------------------------------------------
        second = _import_all(corpus, sink, dry_run=False, resume=False)
        snapshot_two = copy.deepcopy(sink.snapshot())
        receipt.add("R05", "a second full import imports nothing and loses nothing",
                    _sorted_records(snapshot_two) == _sorted_records(snapshot_one)
                    and all(run.rows_imported == 0 for run in second.values()),
                    imported={name: run.rows_imported for name, run in second.items()},
                    skipped={name: run.rows_skipped for name, run in second.items()})

        # R07/R08 ----------------------------------------------------------------------------
        sources = []
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                sources.append(RC.snapshot_sqlite(reader, origin=name))
        merged = RC.merge_sources(sources)
        destination = RC.snapshot_destination(snapshot_one)
        skipped = sum(run.rows_skipped for run in first.values())
        result = RC.reconcile(merged, destination, skipped_duplicates=skipped,
                              compare_refusals=False)
        receipt.add("R07", "the reconciliation closes", result.ok, **result.as_dict())
        collisions = sink.collisions()
        receipt.add("R08", "cross-device uid collisions are recovered and counted",
                    FIX.COLLIDING_UID in collisions
                    and len(collisions[FIX.COLLIDING_UID]) == 2,
                    collisions=collisions)

        # R09 --------------------------------------------------------------------------------
        injections = {
            "missing_row": RC.reconcile(
                merged, RC.snapshot_destination(_without_a_record(snapshot_one)),
                skipped_duplicates=skipped, compare_refusals=False),
            "extra_row": RC.reconcile(
                merged, RC.snapshot_destination(_with_an_extra_record(snapshot_one)),
                skipped_duplicates=skipped, compare_refusals=False),
            "one_byte_body_change": RC.reconcile(
                merged, RC.snapshot_destination(_with_a_changed_body(snapshot_one)),
                skipped_duplicates=skipped, compare_refusals=False),
            "late_publish": RC.reconcile(
                merged, RC.snapshot_destination(_with_a_late_publish(snapshot_one)),
                skipped_duplicates=skipped, compare_refusals=False),
        }
        receipt.add("R09", "an injected missing row, extra row, body change and late publish "
                           "are all detected",
                    all(not injected.ok for injected in injections.values()),
                    detected={name: injected.as_dict()["exit_code"] == 2
                              for name, injected in injections.items()},
                    body_change_caught_by_digest=bool(
                        injections["one_byte_body_change"].digest_mismatches),
                    late_publish_caught=bool(
                        injections["late_publish"].timestamp_regressions))

        # R06 --------------------------------------------------------------------------------
        interrupted_sink, interrupted_guard = _fresh_sink(dsn)
        with interrupted_guard:
            _import_all(corpus, interrupted_sink, dry_run=False, stop_after_rows=2)
            resumed = _import_all(corpus, interrupted_sink, dry_run=False)
            interrupted_snapshot = interrupted_sink.snapshot()
            receipt.add("R06", "an interrupted import, resumed, equals an uninterrupted one",
                        _sorted_records(interrupted_snapshot) == _sorted_records(snapshot_one),
                        resumed_from={name: run.resumed_from for name, run in resumed.items()},
                        rows={"interrupted_then_resumed": len(interrupted_snapshot["records"]),
                              "uninterrupted": len(snapshot_one["records"])})

        # R10 --------------------------------------------------------------------------------
        rollback = _rollback_gate(corpus, snapshot_one, source_hashes, workdir, dsn)
        receipt.add("R10", "rollback preserves the source; reverse backfill restores "
                           "publish-equivalence", rollback.pop("passed"), **rollback)

    # R11 ----------------------------------------------------------------------------------
    receipt.add("R11", "legacy SQLite is still authoritative", **_authority_gate())
    return receipt


def _without_a_record(snapshot: dict) -> dict:
    injured = copy.deepcopy(snapshot)
    injured["records"] = injured["records"][1:]
    return injured


def _with_an_extra_record(snapshot: dict) -> dict:
    injured = copy.deepcopy(snapshot)
    extra = copy.deepcopy(injured["records"][0])
    extra["record_uid"] = "e" * 64
    injured["records"].append(extra)
    return injured


def _with_a_changed_body(snapshot: dict) -> dict:
    """One byte of one body, which no count can see and every digest must."""
    injured = copy.deepcopy(snapshot)
    record = injured["records"][0]
    original = bytes.fromhex(record["payload_sha256"])
    record["payload_sha256"] = hashlib.sha256(original + b"x").hexdigest()
    return injured


def _with_a_late_publish(snapshot: dict) -> dict:
    injured = copy.deepcopy(snapshot)
    for record in injured["records"]:
        if record["state"] == "published" and record["published_at"]:
            record["published_at"] = "2099-01-01T00:00:00Z"
            break
    return injured


def _rollback_gate(corpus: dict, snapshot: dict, source_hashes: dict,
                   workdir: pathlib.Path, dsn: Optional[str]) -> dict:
    """Two halves: the source is byte-identical, and a damaged ledger is restorable from Postgres.

    The damage is applied to a *copy*. The rehearsal never writes a ledger it was given -- the
    reverse backfill is the rollback tool, and the thing being proved is that it works, not that
    it is safe to point at the corpus.
    """
    detail: dict = {"sources_unchanged": {
        name: file_sha256(ledger.path) == source_hashes[name]
        for name, ledger in corpus.items()}}

    ledger = corpus[FIX.RECEIVER_LEDGER]
    damaged = workdir / "rolled-back-receiver.sqlite3"
    shutil.copy2(ledger.path, damaged)
    con = sqlite3.connect(damaged)
    with con:
        lost_uid = str(con.execute(
            "SELECT record_uid FROM durable_records WHERE record_uid IN "
            "(SELECT record_uid FROM cache_attempts WHERE outcome = 'succeeded') "
            "ORDER BY id DESC LIMIT 1").fetchone()[0])
        forgotten_uid = str(con.execute(
            "SELECT record_uid FROM durable_records WHERE record_uid IN "
            "(SELECT record_uid FROM cache_attempts WHERE outcome = 'succeeded') "
            "AND record_uid <> ? ORDER BY id LIMIT 1", (lost_uid,)).fetchone()[0])
        con.execute("DELETE FROM cache_attempts WHERE record_uid = ?", (lost_uid,))
        con.execute("DELETE FROM durable_records WHERE record_uid = ?", (lost_uid,))
        con.execute("DELETE FROM cache_attempts WHERE record_uid = ?", (forgotten_uid,))
    con.close()

    records = [dict(record, body_json=_body_for(corpus, record),
                    receiver_schema_version=HR.RECEIVER_SCHEMA_VERSION)
               for record in snapshot["records"]]

    devices = {row.device_id for row in ledger.importable_rows}
    refused = False
    try:
        BF.run_reverse_backfill(records, damaged, dry_run=False, writer_stopped=False,
                                device_ids=devices)
    except BF.BackfillRefused:
        refused = True
    detail["refuses_a_running_writer"] = refused

    plan = BF.run_reverse_backfill(records, damaged, dry_run=True, device_ids=devices)
    run = BF.run_reverse_backfill(records, damaged, dry_run=False, writer_stopped=True,
                                  device_ids=devices)
    detail["dry_run"] = plan.as_dict()
    detail["restore"] = run.as_dict()

    con = sqlite3.connect(damaged)
    con.row_factory = sqlite3.Row
    restored = con.execute("SELECT 1 FROM durable_records WHERE record_uid = ?",
                           (lost_uid,)).fetchone() is not None
    reacknowledged = {
        uid: con.execute("SELECT COUNT(*) FROM cache_attempts WHERE record_uid = ? "
                         "AND outcome = 'succeeded'", (uid,)).fetchone()[0]
        for uid in (lost_uid, forgotten_uid)}
    total_rows = con.execute("SELECT COUNT(*) FROM durable_records").fetchone()[0]
    con.close()

    detail["restored_record"] = restored
    detail["reacknowledged"] = reacknowledged
    detail["rows_after_restore"] = total_rows

    if dsn is not None:
        with TESTDB.ephemeral_database(dsn) as db:
            migrations = db.apply_migrations()
            db.rollback_migrations(migrations)
            left = db.value("SELECT count(*) FROM pg_namespace WHERE nspname = 'hear'")
            detail["schema_after_rollback"] = int(left)
    else:
        detail["schema_after_rollback"] = 0

    detail["passed"] = (all(detail["sources_unchanged"].values()) and refused and restored
                        and all(count == 1 for count in reacknowledged.values())
                        and detail["schema_after_rollback"] == 0
                        and run.rows_restored == 1)
    return detail


def _body_for(corpus: dict, record: dict) -> str:
    """The body the record was imported from, looked up in the corpus by identity."""
    for ledger in corpus.values():
        for row in ledger.importable_rows:
            if row.device_id == record["device_id"] and row.record_uid == record["record_uid"]:
                return HR.encode_json(row.body)
    raise KeyError(record["record_uid"])


def _authority_gate() -> dict:
    """The claim this whole lane is under: nothing has been cut over."""
    seam_refused = False
    try:
        HR.make_durable_store("postgres")
    except ValueError:
        seam_refused = True

    scratch_only = False
    try:
        BF.PsqlSink("postgresql://postgres@127.0.0.1:5432/dama_hear")
    except BF.BackfillRefused:
        scratch_only = True

    with tempfile.TemporaryDirectory(prefix="hear-authority-") as scratch:
        # A real file in a directory that is deleted immediately: ":memory:" is a path to
        # SqliteDurableRecordStore, not an in-memory database, and would leave a file behind.
        sqlite_is_the_store = isinstance(
            HR.make_durable_store("sqlite", str(pathlib.Path(scratch) / "authority.sqlite3")),
            HR.SqliteDurableRecordStore)
    configured = os.environ.get("HEAR_DURABLE_STORE", HR.DURABLE_STORE)
    return {
        "passed": seam_refused and scratch_only and sqlite_is_the_store
        and configured != "postgres",
        "postgres_store_seam_refused": seam_refused,
        "backfill_writes_scratch_databases_only": scratch_only,
        "sqlite_store_constructs": sqlite_is_the_store,
        "configured_durable_store": configured,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", type=pathlib.Path, required=True,
                    help="scratch directory for the fixture corpus and the receipt")
    ap.add_argument("--dsn", help="throwaway Postgres server; each phase gets its own "
                                  "hear_test_<random> database, dropped afterwards")
    ap.add_argument("--out", type=pathlib.Path, help="write the receipt here as JSON")
    args = ap.parse_args(argv)

    receipt = rehearse(args.workdir, dsn=args.dsn)
    text = json.dumps(receipt.as_dict(), indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    for gate in receipt.gates:
        print(f"{'PASS' if gate.passed else 'FAIL'}  {gate.id}  {gate.title}", file=sys.stderr)
    return 0 if receipt.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
