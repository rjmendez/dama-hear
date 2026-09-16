"""The Phase 2 cut-over rehearsal: backfill, reconciliation, rollback, and who is authoritative.

`tests/test_hear_durable_pg_schema.py` checks the migration files; `tests/test_hear_durable_pg_store_parity.py`
checks the schema's behaviour against the shipping SQLite store. Neither of them imports
anything: the migration plan's §6 backfill, §7 reconciliation and §9 rollback were, until this
file, described but not rehearsed -- and a rollback path first executed during an incident is not
a rollback path.

What is asserted here, and how it manages to run in CI with no server:

* the corpus is **built, never exported**. tools/hear_durable_ledger_fixture.py writes the legacy
  ledgers with the shipping ``SqliteDurableRecordStore``, so the fixture tracks the receiver's
  real shape and no live pool data is read, copied or needed;
* the import runs into ``ModelSink``, an in-memory model of ``hear.backfill_record()``. A model is
  only worth the evidence it agrees with the database, so ``TestModelAgreesWithTheServer`` runs
  the identical corpus through the real function on an ephemeral scratch database and requires
  the two snapshots to be equal, field for field. Without a server that class skips and
  everything else still runs;
* every destination write in this suite goes to a ``hear_test_<random>`` database created and
  dropped by the test, and ``PsqlSink`` refuses any other name before it connects.

The rehearsal proves nothing about a cut-over having happened, because none has: the last class
asserts the store seam still refuses ``postgres`` and that SQLite remains the authoritative
ledger.
"""
import copy
import hashlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import hear_durable_backfill as BF  # noqa: E402
import hear_durable_cutover_rehearsal as REH  # noqa: E402
import hear_durable_ledger_fixture as FIX  # noqa: E402
import hear_durable_pg as PG  # noqa: E402
import hear_durable_pg_testdb as TESTDB  # noqa: E402
import hear_durable_reconcile as RC  # noqa: E402
import hear_heartbeat_receiver as HR  # noqa: E402


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """The two-ledger legacy corpus, built once and never written to by a test."""
    return FIX.build_corpus(tmp_path_factory.mktemp("corpus"))


@pytest.fixture(scope="module")
def receipt(tmp_path_factory):
    """One offline rehearsal, read by several tests."""
    return REH.rehearse(tmp_path_factory.mktemp("receipt"))


@pytest.fixture
def imported(corpus):
    """The corpus, imported once into a fresh model destination."""
    sink = BF.ModelSink()
    runs = {}
    for name, ledger in corpus.items():
        with BF.LedgerReader(ledger.path) as reader:
            runs[name] = BF.run_backfill(reader, sink, name, dry_run=False, batch_size=2)
    return sink, runs


def sources(corpus):
    snapshots = []
    for name, ledger in corpus.items():
        with BF.LedgerReader(ledger.path) as reader:
            snapshots.append(RC.snapshot_sqlite(reader, origin=name))
    return RC.merge_sources(snapshots)


# ------------------------------------------------------------------------------------------------
# The corpus: the ledger shapes the import has to survive
# ------------------------------------------------------------------------------------------------
class TestTheCorpusIsBuiltNotExported:
    def test_the_ledgers_are_written_by_the_shipping_store(self, corpus):
        ledger = corpus[FIX.RECEIVER_LEDGER]
        con = sqlite3.connect(ledger.path)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        con.close()
        # The tables the receiver's own _init_db creates; a hand-written fixture schema would
        # drift from it silently and the import would be rehearsed against a shape that is not
        # in production.
        assert {"durable_records", "cache_attempts", "cache_claims", "refused_messages"} <= tables

    def test_one_sqlite_ledger_cannot_hold_the_collision_the_two_together_do(self, corpus):
        for name, ledger in corpus.items():
            con = sqlite3.connect(ledger.path)
            devices = [row[0] for row in con.execute(
                "SELECT device_id FROM durable_records WHERE record_uid = ?",
                (FIX.COLLIDING_UID,))]
            con.close()
            assert len(devices) <= 1, f"{name} held two devices under one uid; SQLite cannot"
        holders = set()
        for ledger in corpus.values():
            con = sqlite3.connect(ledger.path)
            holders.update(row[0] for row in con.execute(
                "SELECT device_id FROM durable_records WHERE record_uid = ?",
                (FIX.COLLIDING_UID,)))
            con.close()
        assert len(holders) == 2

    def test_a_repeated_refusal_is_one_row_with_two_occurrences(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            refusals = reader.refusals()
        repeated = [r for r in refusals if r["occurrences"] > 1]
        assert len(refusals) == 2 and len(repeated) == 1 and repeated[0]["occurrences"] == 2

    def test_the_fixture_rebuilds_to_the_same_ledger(self, tmp_path):
        first = FIX.build_corpus(tmp_path / "a")
        again = FIX.build_corpus(tmp_path / "a")
        assert FIX.summarize(first)["ledgers"].keys() == FIX.summarize(again)["ledgers"].keys()
        with BF.LedgerReader(again[FIX.RECEIVER_LEDGER].path) as reader:
            assert reader.count(reader.freeze_id()) == len(FIX.receiver_rows())


# ------------------------------------------------------------------------------------------------
# The source is read-only, and says so by trying
# ------------------------------------------------------------------------------------------------
class TestTheSourceIsUntouchable:
    def test_the_reader_cannot_write_and_proves_it(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            reader.assert_read_only()
            with pytest.raises(sqlite3.OperationalError):
                reader._con.execute("DELETE FROM durable_records")

    def test_a_writable_connection_is_refused_rather_than_used(self, corpus, tmp_path):
        copy_path = tmp_path / "writable.sqlite3"
        copy_path.write_bytes(corpus[FIX.RECEIVER_LEDGER].path.read_bytes())
        reader = BF.LedgerReader(copy_path)
        reader._con.close()
        reader._con = sqlite3.connect(copy_path)     # what a careless edit would leave behind
        with pytest.raises(BF.BackfillRefused):
            reader.assert_read_only()
        reader.close()

    def test_a_full_import_leaves_every_source_byte_where_it_was(self, corpus):
        before = {name: hashlib.sha256(ledger.path.read_bytes()).hexdigest()
                  for name, ledger in corpus.items()}
        sink = BF.ModelSink()
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                BF.run_backfill(reader, sink, name, dry_run=False)
        after = {name: hashlib.sha256(ledger.path.read_bytes()).hexdigest()
                 for name, ledger in corpus.items()}
        assert before == after

    def test_a_missing_ledger_is_refused_not_created(self, tmp_path):
        with pytest.raises(BF.BackfillRefused):
            BF.LedgerReader(tmp_path / "absent.sqlite3")
        assert not (tmp_path / "absent.sqlite3").exists()


# ------------------------------------------------------------------------------------------------
# Row selection, acknowledgement derivation and poison
# ------------------------------------------------------------------------------------------------
class TestWhatTheImportReads:
    def test_the_acknowledgement_time_is_the_ledger_s_own_and_a_failure_is_not_one(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            rows = {row.record_uid: row for row in reader.rows(reader.freeze_id())}
        cached = rows["a" * 64]
        assert cached.acknowledged_at == "2026-09-01T01:00:00Z"
        # Attempted, failed, never acknowledged: importing this as published would tell the drain
        # Redis already has a record it never received.
        assert rows["e" * 64].acknowledged_at is None
        assert rows["d" * 64].acknowledged_at is None

    def test_the_freeze_id_bounds_the_import_and_the_rest_is_dual_write_s(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            freeze = reader.freeze_id()
            assert reader.count(freeze) == len(FIX.receiver_rows())
            assert reader.count(2) == 2
            assert [row.sqlite_id for row in reader.rows(2)] == [1, 2]
            assert [row.sqlite_id for row in reader.rows(freeze, after_id=freeze)] == []

    @pytest.mark.parametrize("uid,fragment", [
        ("1" * 64, "not valid JSON"),
        ("2" * 64, "disagrees with the row's device_id"),
    ])
    def test_a_poison_row_is_named_not_guessed_at(self, corpus, uid, fragment):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            row = next(r for r in reader.rows(reader.freeze_id()) if r.record_uid == uid)
        reason = BF.classify(row)
        assert reason is not None and fragment in reason

    def test_a_clean_row_classifies_clean(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            rows = [r for r in reader.rows(reader.freeze_id()) if r.record_uid == "f" * 64]
        assert BF.classify(rows[0]) is None

    def test_the_mach_era_body_is_carried_byte_for_byte(self, corpus, imported):
        sink, _ = imported
        record = sink.records[("default", "node-mach", "f" * 64)]
        body = json.loads(record.body_json)
        assert body["site"] == "Ca\u00f1ada del Oro \u2014 north"
        assert body["note"] == 'tab\there "quoted" \\ backslash'
        assert record.payload_sha256 == hashlib.sha256(
            record.body_json.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------
# The import itself
# ------------------------------------------------------------------------------------------------
class TestTheImport:
    def test_a_dry_run_is_a_plan_and_nothing_else(self, corpus):
        sink = BF.ModelSink()
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            run = BF.run_backfill(reader, sink, FIX.RECEIVER_LEDGER, dry_run=True)
        assert run.rows_imported > 0
        assert sink.records == {} and sink.watermarks == {}

    def test_an_acknowledged_row_arrives_published_and_is_never_republished(self, imported):
        sink, _ = imported
        record = sink.records[("default", "node-alpha", "a" * 64)]
        assert record.state == "published"
        assert record.published_at == "2026-09-01T01:00:00Z"
        assert record.publish_attempts == 0
        assert record.ingest_source == BF.INGEST_SOURCE

    def test_an_unacknowledged_row_arrives_pending_so_the_drain_sends_it_once(self, imported):
        sink, _ = imported
        record = sink.records[("default", "node-beta", "d" * 64)]
        assert (record.state, record.published_at, record.cache_target) == ("pending", None, None)

    def test_the_legacy_uid_is_preserved_rather_than_rewritten(self, imported):
        sink, _ = imported
        assert all(r.legacy_record_uid == r.record_uid for r in sink.records.values())

    def test_a_poison_row_is_quarantined_counted_and_fails_the_run(self, corpus, tmp_path):
        sink = BF.ModelSink()
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            run = BF.run_backfill(reader, sink, FIX.RECEIVER_LEDGER, dry_run=False)
        assert len(run.rejects) == 2
        assert run.exit_code == 1
        assert run.rows_imported == len(corpus[FIX.RECEIVER_LEDGER].importable_rows)
        # The clean rows around it still landed: one bad row costs a quarantine line, not a batch.
        assert ("default", "node-alpha", "a" * 64) in sink.records

    def test_the_watermark_passes_the_poison_row_so_a_resume_does_not_stall(self, corpus):
        sink = BF.ModelSink()
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            run = BF.run_backfill(reader, sink, FIX.RECEIVER_LEDGER, dry_run=False, batch_size=2)
            assert run.last_id == reader.freeze_id()

    def test_two_devices_one_uid_stay_two_records(self, imported):
        sink, _ = imported
        alpha = sink.records[("default", "node-alpha", FIX.COLLIDING_UID)]
        delta = sink.records[("default", "node-delta", FIX.COLLIDING_UID)]
        assert alpha.body_json != delta.body_json
        assert sink.collisions()[FIX.COLLIDING_UID] == ["node-alpha", "node-delta"]

    def test_the_same_device_s_record_seen_twice_is_imported_once(self, imported):
        sink, runs = imported
        assert runs[FIX.BRIDGE_LEDGER].rows_skipped == 1
        assert sink.records[("default", "node-alpha", "a" * 64)].duplicate_arrivals == 1

    def test_a_second_full_run_imports_nothing_and_changes_nothing(self, corpus, imported):
        sink, _ = imported
        before = copy.deepcopy(sink.snapshot()["records"])
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                again = BF.run_backfill(reader, sink, name, dry_run=False, resume=False)
            assert again.rows_imported == 0
            assert again.rows_skipped == len(ledger.importable_rows)
        assert sink.snapshot()["records"] == before

    def test_an_interrupted_run_resumed_equals_an_uninterrupted_one(self, corpus, imported):
        uninterrupted, _ = imported
        interrupted = BF.ModelSink()
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                killed = BF.run_backfill(reader, interrupted, name, dry_run=False, batch_size=2,
                                         stop_after_rows=2)
            assert killed.completed is False
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                resumed = BF.run_backfill(reader, interrupted, name, dry_run=False, batch_size=2)
            assert resumed.resumed_from > 0, "the watermark was not used"
        assert interrupted.snapshot()["records"] == uninterrupted.snapshot()["records"]

    def test_losing_the_watermark_costs_time_not_truth(self, corpus, imported):
        """Correctness is idempotency; the watermark is only an optimisation."""
        sink, _ = imported
        before = copy.deepcopy(sink.snapshot()["records"])
        sink.watermarks.clear()
        for name, ledger in corpus.items():
            with BF.LedgerReader(ledger.path) as reader:
                BF.run_backfill(reader, sink, name, dry_run=False)
        assert sink.snapshot()["records"] == before


# ------------------------------------------------------------------------------------------------
# Reconciliation
# ------------------------------------------------------------------------------------------------
class TestReconciliation:
    def test_a_clean_import_reconciles_and_states_its_conservation(self, corpus, imported):
        sink, runs = imported
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(sink.snapshot()),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert result.ok and result.exit_code == 0
        assert result.conserved
        assert result.conservation_statement().startswith(
            f"sqlite_rows({result.sqlite_rows}) - skipped_duplicates(1) = ")
        assert result.recovered_collisions == {FIX.COLLIDING_UID: ["node-alpha", "node-delta"]}

    def test_a_missing_row_fails_the_run(self, corpus, imported):
        sink, runs = imported
        snapshot = sink.snapshot()
        snapshot["records"] = snapshot["records"][1:]
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert not result.ok and result.exit_code == 2 and len(result.missing) == 1
        assert not result.conserved

    def test_an_extra_row_fails_the_run(self, corpus, imported):
        sink, runs = imported
        snapshot = sink.snapshot()
        extra = dict(snapshot["records"][0], record_uid="c0ffee" + "0" * 58)
        snapshot["records"] = snapshot["records"] + [extra]
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert not result.ok and len(result.extra) == 1

    def test_one_changed_byte_is_caught_by_the_digest_and_not_by_the_count(self, corpus, imported):
        sink, runs = imported
        snapshot = sink.snapshot()
        snapshot["records"][0]["payload_sha256"] = hashlib.sha256(b"a different body").hexdigest()
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert result.count_mismatches == []
        assert result.conserved
        assert len(result.digest_mismatches) == 1 and not result.ok

    def test_a_state_that_disagrees_with_the_ledger_fails_the_run(self, corpus, imported):
        sink, runs = imported
        snapshot = sink.snapshot()
        pending = next(r for r in snapshot["records"] if r["state"] == "pending")
        pending["state"] = "published"
        pending["published_at"] = "2026-09-02T04:00:00Z"
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert len(result.state_mismatches) == 1 and not result.ok

    def test_a_publish_later_than_the_ledger_s_acknowledgement_fails_the_run(self, corpus, imported):
        """Plan §7.5: a backfilled row published after its SQLite ack is an import bug."""
        sink, runs = imported
        snapshot = sink.snapshot()
        published = next(r for r in snapshot["records"] if r["state"] == "published")
        published["published_at"] = "2099-01-01T00:00:00Z"
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert len(result.timestamp_regressions) == 1 and not result.ok

    def test_live_dual_write_rows_are_not_counted_as_a_surplus(self, corpus, imported):
        sink, runs = imported
        snapshot = sink.snapshot()
        live = dict(snapshot["records"][0], record_uid="1" * 63 + "a", ingest_source="lan_http")
        snapshot["records"] = snapshot["records"] + [live]
        result = RC.reconcile(sources(corpus), RC.snapshot_destination(snapshot),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert result.ok, "the backfill reconciliation must ignore rows the live path wrote"

    def test_refusal_parity_is_identity_and_occurrences_not_wall_clock(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            source = RC.snapshot_sqlite(reader, origin=FIX.RECEIVER_LEDGER)
        destination = RC.Snapshot(origin="postgres")
        # A server-stamped received_at differs from the receiver's by construction; the rest of
        # the row is the evidence and has to match exactly.
        destination.refusals = {uid: {"occurrences": row["occurrences"],
                                      "body_bytes": row["body_bytes"]}
                                for uid, row in source.refusals.items()}
        assert RC.reconcile(source, source, compare_refusals=False).ok
        merged = RC.reconcile(RC.Snapshot(origin="sqlite"), RC.Snapshot(origin="postgres"))
        assert merged.ok
        result = RC.reconcile(source, _with_refusals(source, destination.refusals))
        assert result.refusal_mismatches == []

    def test_a_lost_or_undercounted_refusal_is_a_mismatch(self, corpus):
        with BF.LedgerReader(corpus[FIX.RECEIVER_LEDGER].path) as reader:
            source = RC.snapshot_sqlite(reader, origin=FIX.RECEIVER_LEDGER)
        undercounted = {uid: dict(row, occurrences=1) for uid, row in source.refusals.items()}
        assert RC.reconcile(source, _with_refusals(source, undercounted)).refusal_mismatches
        dropped = dict(list(undercounted.items())[:1])
        assert RC.reconcile(source, _with_refusals(source, dropped)).refusal_mismatches

    def test_the_bucket_digest_depends_on_the_bodies_not_their_order(self):
        assert RC.bucket_digest(["a", "b"]) == RC.bucket_digest(["a", "b"])
        assert RC.bucket_digest(["a", "b"]) != RC.bucket_digest(["a", "c"])
        assert RC.bucket_digest([]) == hashlib.sha256(b"").hexdigest()


def _with_refusals(source: RC.Snapshot, refusals: dict) -> RC.Snapshot:
    """``source`` repeated as a destination carrying ``refusals``: only the quarantine differs."""
    destination = RC.Snapshot(origin="postgres")
    for (device_id, record_uid), record in source.records.items():
        destination.add(device_id, record_uid, record["telemetry_path"], record["day"],
                        record["payload_sha256"], record["state"], record["published_at"],
                        record["received_at"])
    destination.refusals = dict(refusals)
    return destination


# ------------------------------------------------------------------------------------------------
# Rollback: the source stays authoritative, and the reverse path works before it is needed
# ------------------------------------------------------------------------------------------------
class TestRollback:
    @pytest.fixture
    def rolled_back(self, corpus, imported, tmp_path):
        """A copy of the receiver ledger as an M6 rollback would find it: a record it never saw
        because Postgres was primary, and a record it has but does not know Redis accepted."""
        sink, _ = imported
        ledger = tmp_path / "rolled-back.sqlite3"
        ledger.write_bytes(corpus[FIX.RECEIVER_LEDGER].path.read_bytes())
        con = sqlite3.connect(ledger)
        with con:
            con.execute("DELETE FROM cache_attempts WHERE record_uid = ?", ("a" * 64,))
            con.execute("DELETE FROM cache_attempts WHERE record_uid = ?", ("c" * 64,))
            con.execute("DELETE FROM durable_records WHERE record_uid = ?", ("c" * 64,))
        con.close()
        records = [dict(r, body_json=sink.records[("default", r["device_id"],
                                                   r["record_uid"])].body_json)
                   for r in sink.snapshot()["records"]]
        devices = {row.device_id for row in corpus[FIX.RECEIVER_LEDGER].importable_rows}
        return ledger, records, devices

    def test_it_refuses_to_write_a_ledger_whose_writer_may_be_running(self, rolled_back):
        ledger, records, devices = rolled_back
        with pytest.raises(BF.BackfillRefused):
            BF.run_reverse_backfill(records, ledger, dry_run=False, device_ids=devices)

    def test_it_refuses_a_locked_ledger_even_when_told_the_writer_is_stopped(self, rolled_back):
        ledger, records, devices = rolled_back
        holder = sqlite3.connect(ledger, timeout=0.1)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(BF.BackfillRefused) as caught:
                BF.run_reverse_backfill(records, ledger, dry_run=False, writer_stopped=True,
                                        device_ids=devices)
            assert "locked" in str(caught.value)
        finally:
            holder.rollback()
            holder.close()

    def test_the_dry_run_writes_nothing(self, rolled_back):
        ledger, records, devices = rolled_back
        before = hashlib.sha256(ledger.read_bytes()).hexdigest()
        plan = BF.run_reverse_backfill(records, ledger, dry_run=True, device_ids=devices)
        assert plan.rows_restored == 1 and plan.acknowledgements_seeded == 2
        assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before

    def test_it_restores_the_record_and_the_acknowledgement_it_lost(self, rolled_back):
        ledger, records, devices = rolled_back
        run = BF.run_reverse_backfill(records, ledger, dry_run=False, writer_stopped=True,
                                      device_ids=devices)
        assert run.rows_restored == 1 and run.acknowledgements_seeded == 2
        con = sqlite3.connect(ledger)
        con.row_factory = sqlite3.Row
        restored = con.execute("SELECT * FROM durable_records WHERE record_uid = ?",
                               ("c" * 64,)).fetchone()
        acked = {uid: con.execute(
            "SELECT COUNT(*) FROM cache_attempts WHERE record_uid = ? AND outcome = 'succeeded'",
            (uid,)).fetchone()[0] for uid in ("a" * 64, "c" * 64)}
        con.close()
        assert restored is not None
        assert json.loads(restored["payload_json"])["device_id"] == "node-alpha"
        # Without the seeded acknowledgement a rolled-back SQLite replay would publish these to
        # Redis a second time -- the one rollback the plan says is not free (§9, M6).
        assert acked == {"a" * 64: 1, "c" * 64: 1}

    def test_running_it_twice_neither_duplicates_a_record_nor_an_acknowledgement(self, rolled_back):
        ledger, records, devices = rolled_back
        BF.run_reverse_backfill(records, ledger, dry_run=False, writer_stopped=True,
                                device_ids=devices)
        second = BF.run_reverse_backfill(records, ledger, dry_run=False, writer_stopped=True,
                                         device_ids=devices)
        assert second.rows_restored == 0 and second.acknowledgements_seeded == 0

    def test_it_never_restores_another_ledger_s_devices(self, rolled_back):
        ledger, records, devices = rolled_back
        BF.run_reverse_backfill(records, ledger, dry_run=False, writer_stopped=True,
                                device_ids=devices)
        con = sqlite3.connect(ledger)
        foreign = con.execute("SELECT COUNT(*) FROM durable_records WHERE device_id IN "
                              "('node-gamma', 'node-delta')").fetchone()[0]
        con.close()
        assert foreign == 0

    def test_the_rehearsal_leaves_the_corpus_it_was_given_byte_identical(self, corpus, tmp_path):
        before = {name: hashlib.sha256(ledger.path.read_bytes()).hexdigest()
                  for name, ledger in corpus.items()}
        receipt = REH.rehearse(tmp_path / "rehearsal")
        assert receipt.passed
        after = {name: hashlib.sha256(ledger.path.read_bytes()).hexdigest()
                 for name, ledger in corpus.items()}
        assert before == after


# ------------------------------------------------------------------------------------------------
# The rehearsal receipt
# ------------------------------------------------------------------------------------------------
class TestTheReceipt:
    def test_every_gate_is_present_and_passes_offline(self, receipt):
        assert {gate.id for gate in receipt.gates} == {
            "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R10", "R11"}
        failed = [gate.id for gate in receipt.gates if not gate.passed]
        assert failed == [], f"failed gates: {failed}"
        assert receipt.passed and receipt.mode == "offline-model"

    def test_the_receipt_carries_the_conservation_statement(self, receipt):
        gate = next(g for g in receipt.gates if g.id == "R07")
        assert "sqlite_rows(" in gate.detail["conservation"]
        assert gate.detail["conserved"] is True

    def test_a_failed_gate_fails_the_receipt(self, receipt):
        copied = REH.Receipt(mode=receipt.mode, started_at=receipt.started_at,
                             gates=list(receipt.gates))
        copied.add("R99", "an invented failure", False)
        assert not copied.passed

    def test_the_receipt_is_json_serialisable_evidence(self, receipt, tmp_path):
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(receipt.as_dict(), indent=2, sort_keys=True))
        loaded = json.loads(path.read_text())
        assert loaded["rehearsal"] == "phase2-postgres-cutover" and loaded["passed"] is True

    def test_the_cli_writes_the_receipt_and_reports_through_its_exit_code(self, tmp_path, capsys):
        out = tmp_path / "receipt.json"
        code = REH.main(["--workdir", str(tmp_path / "work"), "--out", str(out)])
        capsys.readouterr()
        assert code == 0 and json.loads(out.read_text())["passed"] is True


# ------------------------------------------------------------------------------------------------
# Nothing has been cut over
# ------------------------------------------------------------------------------------------------
class TestSqliteIsStillAuthoritative:
    def test_the_postgres_store_seam_still_refuses(self):
        with pytest.raises(ValueError, match="not implemented"):
            HR.make_durable_store("postgres")

    def test_the_shipping_store_is_still_the_sqlite_one(self, tmp_path):
        assert isinstance(HR.make_durable_store("sqlite", str(tmp_path / "ledger.sqlite3")),
                          HR.SqliteDurableRecordStore)
        assert HR.DURABLE_STORE != "postgres"

    @pytest.mark.parametrize("dsn", [
        "postgresql://postgres@127.0.0.1:5432/dama_hear",
        "postgresql://postgres@db.internal:5432/postgres",
        "host=db.internal dbname=hear user=hear",
        "postgresql://postgres@127.0.0.1:5432/hear_test_nothexadecimal",
        "postgresql://postgres@127.0.0.1:5432/",
    ])
    def test_the_import_refuses_every_destination_that_is_not_a_scratch_database(self, dsn):
        with pytest.raises(BF.BackfillRefused):
            BF.PsqlSink(dsn)

    def test_a_generated_scratch_name_is_the_only_thing_it_accepts(self):
        name = TESTDB.scratch_name()
        sink = BF.PsqlSink(f"postgresql://postgres@127.0.0.1:5432/{name}")
        assert sink.dsn.endswith(name)

    def test_no_tool_in_this_lane_imports_a_postgres_driver(self):
        for module in (BF, RC, REH, FIX):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            assert "psycopg" not in source and "import asyncpg" not in source

    def test_the_authority_gate_says_all_of_this_in_one_place(self):
        gate = REH._authority_gate()
        assert gate["passed"]
        assert gate["postgres_store_seam_refused"]
        assert gate["backfill_writes_scratch_databases_only"]


# ------------------------------------------------------------------------------------------------
# Against a real server: the model has to be the database, or it is decoration
# ------------------------------------------------------------------------------------------------
_ADMIN_DSN, _WHY = TESTDB.availability()


def test_the_live_half_of_the_rehearsal_is_not_quietly_skipping():
    """CI sets HEAR_PG_REQUIRE_LIVE: a skipped rehearsal must not pass for a missing server."""
    if os.environ.get("HEAR_PG_REQUIRE_LIVE", "").strip().lower() in ("", "0", "false", "no"):
        pytest.skip("live Postgres is optional outside the durable-postgres CI job")
    assert _ADMIN_DSN is not None, _WHY


@pytest.fixture(scope="module")
def db():
    with TESTDB.ephemeral_database(_ADMIN_DSN) as database:
        database.apply_migrations()
        yield database


@pytest.fixture(scope="module")
def both(db, tmp_path_factory):
    """The identical corpus, imported into the model and into the real function."""
    built = FIX.build_corpus(tmp_path_factory.mktemp("live-corpus"))
    model, server = BF.ModelSink(), BF.PsqlSink(db.dsn)
    runs = {}
    for name, ledger in built.items():
        for sink in (model, server):
            with BF.LedgerReader(ledger.path) as reader:
                run = BF.run_backfill(reader, sink, name, dry_run=False, batch_size=2)
            if sink is server:
                runs[name] = run
    return built, model, server, runs


@pytest.mark.skipif(_ADMIN_DSN is None, reason=_WHY)
class TestModelAgreesWithTheServer:
    def test_the_two_destinations_hold_exactly_the_same_records(self, both):
        _, model, server, _ = both
        assert server.snapshot()["records"] == model.snapshot()["records"]

    def test_the_watermarks_agree(self, both):
        _, model, server, _ = both
        for ledger, mark in model.snapshot()["watermarks"].items():
            live = server.snapshot()["watermarks"][ledger]
            assert (live["rows_imported"], live["rows_skipped"], live["last_sqlite_id"]) == \
                   (mark["rows_imported"], mark["rows_skipped"], mark["last_sqlite_id"])

    def test_the_server_watermark_does_not_count_skipped_duplicates(self, both):
        """A divergence from plan §6, found by rehearsing it rather than by reading it.

        The plan's partial-failure table says a duplicate increments ``rows_skipped``;
        ``hear.backfill_record()`` (migration 0005) only writes backfill_watermarks when it
        inserts, so that column stays 0. Nothing is lost -- the duplicate is counted on the
        identity row and by the run -- but a reconciliation that took its skip count from the
        database would compute a surplus and fail a correct import. It takes it from the run.
        """
        _, _, server, runs = both
        assert sum(run.rows_skipped for run in runs.values()) == 1
        assert all(mark["rows_skipped"] == 0
                   for mark in server.snapshot()["watermarks"].values())
        duplicated = int(TESTDB.psql_value(
            server.dsn, "SELECT duplicate_arrivals FROM hear.durable_record_ids "
                        "WHERE device_id = 'node-alpha' AND record_uid = "
                        f"'{"a" * 64}'"))
        assert duplicated == 1

    def test_the_server_recovers_the_collision_the_ledgers_could_not_hold(self, both):
        _, model, server, _ = both
        assert server.collisions() == {FIX.COLLIDING_UID: ["node-alpha", "node-delta"]}
        assert server.collisions() == model.collisions()

    def test_the_reconciliation_closes_against_the_server(self, both):
        built, _, server, runs = both
        result = RC.reconcile(sources(built), RC.snapshot_destination(server.snapshot()),
                              skipped_duplicates=sum(r.rows_skipped for r in runs.values()),
                              compare_refusals=False)
        assert result.ok, result.as_dict()

    def test_the_server_agrees_that_every_body_hashes_to_its_own_digest(self, db):
        assert db.value("SELECT count(*) FROM hear.verify_payload_integrity()") == "0"

    def test_the_counters_conserve_what_was_imported(self, db, both):
        _, _, _, runs = both
        health = json.loads(db.value("SELECT hear.health_snapshot()::text"))
        imported = sum(run.rows_imported for run in runs.values())
        assert health["records_backfilled"] == imported
        assert health["records_persisted"] == imported
        # The soak metric: backfill must never claim Redis accepted anything.
        assert health["cache_successes"] == 0
        assert health["pending_records"] + _published(db) == imported
        assert health["records_duplicate"] == 0, "a skipped duplicate is not a persist-duplicate"

    def test_a_backfilled_published_row_is_never_claimed_by_the_drain(self, db, both):
        claimed = json.loads(db.value(
            "SELECT COALESCE(json_agg(json_build_object('uid', record_uid, 'device', device_id)"
            ")::text, '[]') FROM hear.claim_pending('rehearsal-worker', 100, 60)"))
        states = json.loads(db.value(
            "SELECT COALESCE(json_object_agg(state, n)::text, '{}') FROM "
            "(SELECT state::text AS state, count(*) AS n FROM hear.durable_records "
            "GROUP BY state) s"))
        assert states.get("published", 0) > 0
        assert all(c["device"] in ("node-beta", "node-gamma") for c in claimed), claimed
        assert states.get("claimed", 0) == len(claimed)

    def test_an_expired_lease_returns_the_record_instead_of_stranding_it(self, db):
        released = db.value("SELECT hear.release_expired_claims()")
        assert released == "0", "nothing has expired yet; a lease that expires early is a bug"
        db.sql("UPDATE hear.durable_records SET claim_expires_at = now() - interval '1 minute' "
               "WHERE state = 'claimed'")
        again = int(db.value("SELECT hear.release_expired_claims()"))
        assert again > 0
        assert db.value("SELECT count(*) FROM hear.durable_records WHERE state = 'claimed'") == "0"

    def test_the_import_is_idempotent_on_the_server_too(self, db, both):
        built, _, server, _ = both
        before = server.snapshot()["records"]
        for name, ledger in built.items():
            with BF.LedgerReader(ledger.path) as reader:
                again = BF.run_backfill(reader, server, name, dry_run=False, resume=False)
            assert again.rows_imported == 0
        # Claims and releases moved state around; identity, bodies and arrival times did not.
        after = server.snapshot()["records"]
        assert [(r["device_id"], r["record_uid"], r["payload_sha256"], r["received_at"])
                for r in after] == \
               [(r["device_id"], r["record_uid"], r["payload_sha256"], r["received_at"])
                for r in before]

    def test_the_whole_rehearsal_passes_against_an_ephemeral_server(self, tmp_path):
        receipt = REH.rehearse(tmp_path / "live-rehearsal", dsn=_ADMIN_DSN)
        failed = [gate.id for gate in receipt.gates if not gate.passed]
        assert failed == [], json.dumps(receipt.as_dict(), indent=2)
        assert receipt.mode == "live-ephemeral"


def _published(db) -> int:
    return int(db.value("SELECT count(*) FROM hear.durable_records WHERE state = 'published'"))
