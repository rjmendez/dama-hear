"""The import publishes atomically, resumes for free, and never touches the source.

Read these as the claims a Phase 3 shadow import would have to make before anyone let it near
`/pool`, each one written as the failure it prevents. NOTHING HERE TALKS TO A REAL BACKEND: the
store is a throwaway directory, the pool is synthetic, and no gate (G0 backup, G1 backend choice)
is satisfied, so this is a proof about the rules and not a licence to run them on evidence.
"""
from __future__ import annotations

import errno
import json
import os

import pytest

from hear.objectstore import backend as B
from hear.objectstore import crypto as C
from hear.objectstore import keys as K
from hear.objectstore import ledger as L
from hear.objectstore import stage as ST
from tests import objectstore_mini_pool as MINI

RUN = "2026-09-16T0000Z-a1b2c3"


class CountingBackend(B.LocalDirBackend):
    """Counts the listings, because "resume without listing the bucket" is a testable claim."""

    def __init__(self, root, **kw):
        super().__init__(root, **kw)
        self.list_calls = 0

    def list_prefix(self, prefix):
        self.list_calls += 1
        return super().list_prefix(prefix)


@pytest.fixture()
def pool(tmp_path):
    return MINI.build(str(tmp_path / "pool"))


@pytest.fixture()
def store(tmp_path):
    return CountingBackend(str(tmp_path / "store"))


def _ledger_path(tmp_path, run=RUN):
    return str(tmp_path / "work" / "runs" / run / "ledger.jsonl")


def _raw_task(path, digest, nbytes, logical=None):
    return ST.ImportTask(
        object_class="raw", logical_id=logical or os.path.basename(path),
        partition=("mach",), source_path=path,
        expected_digest=digest, expected_bytes=nbytes, digest_source="ledger.jsonl")


def _clip_task(path, digest, nbytes, clip_key, day):
    return ST.ImportTask(
        object_class="clip", logical_id=clip_key, partition=(day, "mach"), source_path=path,
        expected_digest=digest, expected_bytes=nbytes, digest_source="index.jsonl")


def _tasks(pool):
    a, b = pool["raw_duplicate_paths"]
    ca, cb = pool["clip_paths"]
    return [
        _raw_task(a, pool["raw_digest"], pool["raw_bytes"]),
        _raw_task(b, pool["raw_digest"], pool["raw_bytes"]),
        _clip_task(ca, pool["clip_digest"], pool["clip_bytes"], "9f2c" + "0" * 28, "2026-09-12"),
        _clip_task(cb, pool["clip_digest"], pool["clip_bytes"], "7e1d" + "0" * 28, "unanchored"),
    ]


#: Synthetic key material, deterministic across importer instances so a re-run converges on the
#: same ciphertext and dedupe still means something. Nothing here is a real key; see
#: `hear/objectstore/crypto.py`, and `test_the_default_importer_cannot_publish_a_restricted_class`
#: for what an importer without this does.
#: Pinned so two importer instances in one test converge on the same ciphertext; `crypto.py`
#: refuses to default a seed, because a default seed is a shipped key.
SEED = b"phase3-objectstore-import-test-seed"


def _crypto(tenant_id="dama"):
    return C.synthetic_crypto(tenant_id, SEED)


_UNSET = object()


def _importer(store, tmp_path, run=RUN, lease=None, crypto=_UNSET):
    return ST.Importer(store, _ledger_path(tmp_path, run), run, lease=lease,
                       git_commit="0000000",
                       crypto=_crypto() if crypto is _UNSET else crypto)


def _restricted_blob_key(plaintext_digest, object_class="raw", tenant_id="dama"):
    """What a restricted class is addressed by: the HMAC id, never the plaintext digest."""
    bid, algo = _crypto(tenant_id).blob_id(object_class, plaintext_digest)
    return K.blob_key(bid, algo=algo, tenant=tenant_id)


# --------------------------------------------------------------------- gates


def test_an_unmet_gate_reads_nothing_and_publishes_nothing(pool, store, tmp_path):
    imp = _importer(store, tmp_path)
    with pytest.raises(ST.GateFailed):
        imp.run(_tasks(pool), gates={"G0_pool_backup": False, "G1_backend_selected": True})
    assert store.keys_under("hear/v1/obj/") == []
    assert store.bytes_written == 0


# ------------------------------------------------------- identity and resume


def test_a_rerun_publishes_no_second_copy(pool, store, tmp_path):
    first = _importer(store, tmp_path).run(_tasks(pool))
    assert first.counters["published"] == 4
    wrote = store.bytes_written
    assert wrote > 0

    second = _importer(store, tmp_path).run(_tasks(pool))
    assert second.counters["published"] == 0
    assert second.counters["skipped_done"] == 4
    assert second.exit_code == 0
    assert store.bytes_written == wrote  # zero bytes, not "few bytes"
    assert len(store.keys_under("hear/v1/obj/")) == 4


def test_a_resume_reads_the_ledger_and_not_the_bucket(pool, store, tmp_path):
    _importer(store, tmp_path).run(_tasks(pool))
    before = store.list_calls
    resumed = _importer(store, tmp_path)
    assert resumed.resume.published
    resumed.run(_tasks(pool))
    assert store.list_calls == before


def test_a_resume_after_a_crash_publishes_exactly_what_is_missing(pool, store, tmp_path, monkeypatch):
    tasks = _tasks(pool)
    imp = _importer(store, tmp_path)
    real_commit = store.commit_pointer
    seen = {"n": 0}

    def crash(key, doc, **kw):
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt("power cut mid-run")
        return real_commit(key, doc, **kw)

    monkeypatch.setattr(store, "commit_pointer", crash)
    with pytest.raises(KeyboardInterrupt):
        imp.run(tasks)
    published_before = set(store.keys_under("hear/v1/obj/"))
    assert len(published_before) == 2

    monkeypatch.setattr(store, "commit_pointer", real_commit)
    report = _importer(store, tmp_path).run(tasks)
    assert report.counters["published"] == 2
    assert report.counters["skipped_done"] == 2
    assert len(store.keys_under("hear/v1/obj/")) == 4


def test_a_crashed_import_publishes_nothing_it_did_not_finish(pool, store, tmp_path, monkeypatch):
    """A crash between stage and commit leaves staging garbage and no visible object."""
    task = _tasks(pool)[0]

    def boom(key, doc, **kw):
        raise KeyboardInterrupt("crash before the only observable step")

    monkeypatch.setattr(store, "commit_pointer", boom)
    with pytest.raises(KeyboardInterrupt):
        _importer(store, tmp_path).run([task])
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/staging/") != []

    state = L.replay(_ledger_path(tmp_path))
    assert state.published == set()
    assert task.task_id in state.claimed


def test_the_ledger_never_leads_the_store(pool, store, tmp_path):
    """Every `published` row in the ledger has a pointer behind it -- set equality, both ways."""
    _importer(store, tmp_path).run(_tasks(pool))
    rows = [r for r in L.read_rows(_ledger_path(tmp_path)) if r["type"] == "published"]
    from_ledger = {r["object_key"] for r in rows}
    from_store = set(store.keys_under("hear/v1/obj/"))
    assert from_ledger == from_store


# ------------------------------------------------------------ verification


def test_a_readback_is_compared_to_the_recorded_digest_not_to_the_bytes_just_written(
        pool, store, tmp_path, monkeypatch):
    """The store echoing back whatever it was handed must not count as verification."""
    task = _tasks(pool)[0]
    # The readback is a stream now, so the lie has to be told at the streaming read.
    monkeypatch.setattr(store, "iter_range",
                        lambda key, offset=0, length=None, **kw: iter([b"different bytes"]))
    report = _importer(store, tmp_path).run([task])
    assert report.counters["published"] == 0
    assert [q["error_class"] for q in report.quarantined] == ["readback_mismatch"]
    assert report.exit_code == 1


def test_a_corrupted_source_is_quarantined_and_the_run_continues(pool, store, tmp_path):
    rotten = ST.ImportTask(
        object_class="raw", logical_id="nyquist-rotten", partition=("nyquist",),
        source_path=pool["rotten_path"], expected_digest=pool["rotten_recorded_digest"],
        expected_bytes=pool["raw_bytes"], digest_source="ledger.jsonl")
    tasks = [rotten] + _tasks(pool)
    report = _importer(store, tmp_path).run(tasks)
    assert report.counters["quarantined"] == 1
    assert report.quarantined[0]["error_class"] == "source_digest_mismatch"
    assert report.counters["published"] == 4  # the run kept going
    assert report.outcome == "partial" and report.exit_code == 1
    # The corrupt source was not repaired, re-hashed in place, or published under a new digest.
    assert not any("nyquist-rotten" in k for k in store.keys_under("hear/v1/obj/"))


def test_a_corrupted_object_in_the_store_is_caught_before_a_pointer_is_added_to_it(
        pool, store, tmp_path):
    """Dedupe must re-verify the blob it is about to point at; silent rot is caught nowhere else."""
    tasks = _tasks(pool)
    _importer(store, tmp_path).run([tasks[0]])
    blob = _restricted_blob_key(pool["raw_digest"])
    store.corrupt(blob, b"rotted in place")

    report = _importer(store, tmp_path, run="second-run").run([tasks[1]])
    assert report.counters["published"] == 0
    assert [q["error_class"] for q in report.quarantined] == ["stored_blob_corrupt"]


def test_a_digest_match_with_a_length_mismatch_is_refused(pool, store, tmp_path):
    task = _raw_task(pool["raw_duplicate_paths"][0], pool["raw_digest"], pool["raw_bytes"] + 1)
    report = _importer(store, tmp_path).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["length_mismatch_on_digest_match"]
    assert store.keys_under("hear/v1/obj/") == []


def test_a_quarantine_record_for_a_restricted_class_withholds_the_plaintext_digest(
        pool, store, tmp_path, monkeypatch):
    clip = _clip_task(pool["clip_paths"][0], pool["clip_digest"], pool["clip_bytes"],
                      "9f2c" + "0" * 28, "2026-09-12")
    monkeypatch.setattr(store, "iter_range",
                        lambda key, offset=0, length=None, **kw: iter([b"nope"]))
    report = _importer(store, tmp_path).run([clip])
    doc = report.quarantined[0]
    assert doc["digests_withheld"] == "restricted_class"
    assert pool["clip_digest"] not in json.dumps(doc)


# --------------------------------------------------------- the pointer commit


def test_a_commit_that_already_exists_with_the_same_blob_is_a_replay_not_an_error(
        pool, store, tmp_path):
    task = _tasks(pool)[0]
    _importer(store, tmp_path).run([task])
    # A different run id, so nothing is skipped by the ledger: the commit itself must be idempotent.
    report = _importer(store, tmp_path, run="second-run").run([task])
    assert report.counters["replayed"] == 1
    assert report.counters["published"] == 0
    assert report.exit_code == 0
    assert len(store.keys_under("hear/v1/obj/")) == 1


def test_a_commit_that_exists_with_a_different_blob_halts_the_class(pool, store, tmp_path):
    a, b = pool["raw_duplicate_paths"]
    first = _raw_task(a, pool["raw_digest"], pool["raw_bytes"], logical="collide")
    _importer(store, tmp_path).run([first])

    other_body = b"a different archive under the same logical id\n"
    other = tmp_path / "other-dets.csv"
    other.write_bytes(other_body)
    conflicting = ST.ImportTask(
        object_class="raw", logical_id="collide", partition=("mach",), source_path=str(other),
        expected_digest=K.sha256_hex(other_body), expected_bytes=len(other_body),
        digest_source="ledger.jsonl")
    follower = _raw_task(b, pool["raw_digest"], pool["raw_bytes"], logical="follower")

    report = _importer(store, tmp_path, run="second-run").run([conflicting, follower])
    assert [q["error_class"] for q in report.quarantined] == ["pointer_conflict"]
    assert "raw" in report.halted_classes
    assert report.counters["deferred"] == 1  # the follower was not attempted after the halt
    doc = json.loads(store.get_range(K.object_key("raw", ("mach",), "collide")))
    assert doc["blob_key"] == _restricted_blob_key(pool["raw_digest"])  # pointer untouched


# ------------------------------------------------------------ retry, fencing


def test_a_transient_backend_error_is_retried_and_publishes_once(pool, store, tmp_path):
    task = _tasks(pool)[0]
    store.fail_next_put = [B.BackendTransient("503"), B.BackendTransient("timeout")]
    report = _importer(store, tmp_path).run([task])
    assert report.counters["retries"] == 2
    assert report.counters["published"] == 1
    assert len(store.keys_under("hear/v1/obj/")) == 1


def test_a_backend_that_never_recovers_quarantines_the_object_and_not_the_run(pool, store, tmp_path):
    tasks = _tasks(pool)
    store.fail_next_put = [B.BackendTransient("503")] * ST.MAX_TRANSIENT_RETRIES
    report = _importer(store, tmp_path).run(tasks)
    assert [q["error_class"] for q in report.quarantined] == ["backend_transient"]
    assert report.counters["published"] == 3


def _flaky_step(store, monkeypatch, attr, match, failures):
    """Fail the first `failures` calls of one backend step with a transient, then behave."""
    real = getattr(store, attr)
    seen = {"n": 0}

    def flaky(key, *a, **kw):
        if match(key):
            seen["n"] += 1
            if seen["n"] <= failures:
                raise B.BackendTransient("503 slow down")
        return real(key, *a, **kw)

    monkeypatch.setattr(store, attr, flaky)
    return seen


def test_a_transient_on_the_blob_put_is_retried_rather_than_ending_the_run(pool, store, tmp_path,
                                                                          monkeypatch):
    """⚠️THE UNCONTAINED-TRANSIENT REGRESSION: only the staging put was ever retried.

    A throttle on the blob put, the metadata put or the pointer commit escaped the per-object
    handler entirely -- no retry, no quarantine record, no ledger close, no report: the whole
    import died on one 503 in the step most likely to meet one. Every step that writes is
    idempotent by construction, so every step that writes is now retried.
    """
    task = _tasks(pool)[0]
    _flaky_step(store, monkeypatch, "put_immutable_stream", lambda k: "/blob/" in k, 2)
    report = _importer(store, tmp_path).run([task])
    assert report.counters["published"] == 1
    assert report.counters["retries"] == 2
    assert report.counters["quarantined"] == 0
    assert len(store.keys_under("hear/v1/obj/")) == 1


def test_a_transient_on_the_metadata_put_and_on_the_commit_is_retried_too(pool, store, tmp_path,
                                                                         monkeypatch):
    tasks = _tasks(pool)[:1]
    _flaky_step(store, monkeypatch, "put_immutable", lambda k: "/meta/" in k, 2)
    _flaky_step(store, monkeypatch, "commit_pointer", lambda k: "/obj/" in k, 1)
    report = _importer(store, tmp_path).run(tasks)
    assert report.counters["published"] == 1
    assert report.counters["retries"] == 3
    assert report.exit_code == 0


def test_a_commit_that_never_stops_throttling_quarantines_one_object_and_closes_the_run(
        pool, store, tmp_path, monkeypatch):
    """Containment, not heroics: the object is refused, the run still reports, the exit is 1."""
    tasks = _tasks(pool)
    _flaky_step(store, monkeypatch, "commit_pointer",
                lambda k: k.endswith(tasks[0].logical_id), 10 ** 6)
    report = _importer(store, tmp_path).run(tasks)

    assert [q["error_class"] for q in report.quarantined] == ["backend_transient"]
    assert report.counters["published"] == 3
    assert report.counters["retries"] == ST.MAX_TRANSIENT_RETRIES
    assert report.outcome == "partial" and report.exit_code == 1
    rows = [json.loads(l) for l in open(_ledger_path(tmp_path))]
    assert rows[-1]["type"] == "run_close"              # the run closed rather than dying
    assert {r["step"] for r in rows if r.get("step")} == {"pointer_commit"}
    assert store.keys_under("hear/v1/staging/") == []   # and left no staged copy behind


def test_a_full_volume_is_a_backend_fault_and_not_corrupt_source_data(pool, store, tmp_path):
    """⚠️THE MISCLASSIFIED-ENOSPC REGRESSION: a full disk was quarantined as unreadable source.

    `ENOSPC`/`EIO` raised by a *store* write used to reach the importer as a bare `OSError`, which
    the per-object table reads as `source_unreadable` -- so an operator whose volume filled up was
    told their evidence was corrupt and sent to re-fetch bytes that were never wrong. Store faults
    are classified where they are raised; a source that really cannot be read still says so.
    """
    task = _tasks(pool)[0]
    store.fail_next_write = [OSError(errno.ENOSPC, "No space left on device")
                             for _ in range(ST.MAX_TRANSIENT_RETRIES)]
    report = _importer(store, tmp_path).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["backend_transient"]
    assert report.counters["retries"] == ST.MAX_TRANSIENT_RETRIES
    assert store.keys_under("hear/v1/obj/") == []

    # The other side of the classification, on the same importer: a source that is really gone.
    missing = _raw_task(str(tmp_path / "not-a-file"), pool["raw_digest"], pool["raw_bytes"],
                        logical="missing")
    report2 = _importer(store, tmp_path, run=RUN + "-src").run([missing])
    assert [q["error_class"] for q in report2.quarantined] == ["source_unreadable"]


def test_an_unreadable_backend_read_is_a_backend_fault(pool, store, tmp_path):
    """A device error reading the staged copy back is the store failing, not the source."""
    task = _tasks(pool)[0]
    store.fail_next_read = [OSError(errno.EIO, "input/output error")]
    report = _importer(store, tmp_path).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["backend_transient"]
    assert store.keys_under("hear/v1/obj/") == []


def test_a_zombie_importer_with_a_stale_lease_is_fenced_at_the_commit(pool, tmp_path):
    """The zombie is still alive, still holds bytes, and still believes it owns the run."""
    clock = {"t": 1000.0}
    store = CountingBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    stale = store.acquire_lease("import/run", ttl_s=60, holder="zombie")
    clock["t"] += 61  # the zombie stalled past its TTL; a takeover happened without it noticing
    fresh = store.acquire_lease("import/run", ttl_s=60, holder="takeover")
    assert fresh.epoch == stale.epoch + 1

    report = _importer(store, tmp_path, lease=stale).run(_tasks(pool))
    assert report.outcome == "aborted"
    assert store.keys_under("hear/v1/obj/") == []

    ok = _importer(store, tmp_path, run="second-run", lease=fresh).run(_tasks(pool))
    assert ok.counters["published"] == 4


def test_an_expired_lease_cannot_be_renewed_into_a_fresh_one(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    lease = store.acquire_lease("import/run", ttl_s=60, holder="a")
    clock["t"] += 61
    assert store.renew_lease(lease, ttl_s=60) is None
    taken = store.acquire_lease("import/run", ttl_s=60, holder="b")
    assert taken is not None and taken.epoch == lease.epoch + 1
    assert store.renew_lease(lease, ttl_s=60) is None


# ------------------------------------------------------------------- dedupe


def test_a_duplicate_archive_writes_a_pointer_and_no_bytes(pool, store, tmp_path):
    a, b = pool["raw_duplicate_paths"]
    report = _importer(store, tmp_path).run([
        _raw_task(a, pool["raw_digest"], pool["raw_bytes"]),
        _raw_task(b, pool["raw_digest"], pool["raw_bytes"]),
    ])
    assert report.counters["published"] == 2
    assert report.counters["deduped"] == 1
    blobs = store.keys_under("hear/v1/blob/")
    assert len(blobs) == 1
    published = [json.loads(store.get_range(k)) for k in store.keys_under("hear/v1/obj/")]
    assert {p["blob_key"] for p in published} == {_restricted_blob_key(pool["raw_digest"])}


def test_two_identical_clips_get_two_objects_and_one_blob(pool, store, tmp_path):
    ca, cb = pool["clip_paths"]
    report = _importer(store, tmp_path).run([
        _clip_task(ca, pool["clip_digest"], pool["clip_bytes"], "9f2c" + "0" * 28, "2026-09-12"),
        _clip_task(cb, pool["clip_digest"], pool["clip_bytes"], "7e1d" + "0" * 28, "unanchored"),
    ])
    assert report.counters["published"] == 2
    assert len([k for k in store.keys_under("hear/v1/blob/")]) == 1
    keys = store.keys_under("hear/v1/obj/")
    assert any("/unanchored/" in k for k in keys)


# ------------------------------------------------------------- non-mutation


def test_the_importer_never_writes_to_the_pool(pool, store, tmp_path):
    before = ST.census(pool["root"])
    report = _importer(store, tmp_path).run(_tasks(pool))
    assert report.counters["published"] == 4
    assert ST.census(pool["root"]) == before


def test_no_source_file_is_modified_by_a_dedupe_or_by_a_quarantine(pool, store, tmp_path):
    rotten = ST.ImportTask(
        object_class="raw", logical_id="nyquist-rotten", partition=("nyquist",),
        source_path=pool["rotten_path"], expected_digest=pool["rotten_recorded_digest"],
        expected_bytes=pool["raw_bytes"], digest_source="ledger.jsonl")
    before = ST.census(pool["root"])
    _importer(store, tmp_path).run(_tasks(pool) + [rotten])
    assert ST.census(pool["root"]) == before


def test_the_importer_has_no_way_to_delete_a_published_object(store):
    assert not hasattr(store, "delete_object")
    assert [m for m in dir(store) if m.startswith("delete")] == ["delete_staged"]


def test_a_published_object_is_never_overwritten_in_place(pool, store, tmp_path):
    task = _tasks(pool)[0]
    _importer(store, tmp_path).run([task])
    blob = _restricted_blob_key(pool["raw_digest"])
    result = store.put_immutable(blob, b"replacement bytes")
    assert result.created is False
    assert store.get_range(blob) != b"replacement bytes"
    with pytest.raises(ValueError):
        store.put_immutable(blob, b"replacement bytes", if_absent=False)


def test_a_restage_that_reads_different_bytes_is_refused_rather_than_published(store, tmp_path):
    """The re-stage after a bad readback is a NEW read, and it gets the same interrogation.

    Carrying the first attempt's digest forward would publish the second attempt's bytes under the
    first attempt's name -- a source that moved, laundered into a fact.
    """
    reads = {"n": 0}

    def shifting():
        reads["n"] += 1
        return iter([b"first read\n" if reads["n"] == 1 else b"second read\n"])

    real_iter = store.iter_range
    lied = {"n": 0}

    def liar(key, offset=0, length=None, **kw):
        if lied["n"] == 0 and "/staging/" in key:
            lied["n"] = 1
            return iter([b"not what was written"])
        return real_iter(key, offset, length, **kw)

    store.iter_range = liar
    task = ST.ImportTask(object_class="record-seg", logical_id="0",
                         partition=("2026-09-12", "mach"), chunks=shifting,
                         digest_source="stream-digest")
    report = _importer(store, tmp_path).run([task])
    store.iter_range = real_iter

    assert [q["error_class"] for q in report.quarantined] == ["source_changed_during_import"]
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/blob/") == []
    assert store.keys_under("hear/v1/staging/") == []


# ------------------------------------- a failure inside the re-stage is still one object's


def _flaky_readback(store, monkeypatch):
    """Make the *first* staging readback lie, so the object takes the re-stage path exactly once."""
    real = store.iter_range
    state = {"lied": False}

    def lying(key, offset=0, length=None, **kw):
        if "/staging/" in key and not state["lied"]:
            state["lied"] = True
            return iter([b"not what you wrote"])
        return real(key, offset, length, **kw)

    monkeypatch.setattr(store, "iter_range", lying)
    return state


def test_a_source_that_dies_during_the_re_stage_quarantines_and_the_run_still_closes(
        pool, store, tmp_path, monkeypatch):
    """⚠️THE RE-STAGE WAS OUTSIDE THE HANDLER, so a second-read failure aborted the whole import.

    The re-stage after a bad readback is a fresh read of the source and can fail in every way the
    first read could. When only the first read was wrapped, an `OSError` there escaped the object,
    escaped the run, and left no report, no `run_close` and no quarantine record -- one bad file
    taking down an import of thousands. One object's failure aborts one object.
    """
    reads = {"n": 0}

    def dying():
        reads["n"] += 1
        if reads["n"] >= 2:
            raise OSError("the source went away between the two reads")
        return iter([b"first read bytes\n"])

    bad = ST.ImportTask(object_class="record-seg", logical_id="0", partition=("2026-09-12", "mach"),
                        chunks=dying, digest_source="stream-digest")
    good = ST.ImportTask(object_class="record-seg", logical_id="1", partition=("2026-09-12", "mach"),
                         source_path=pool["records_path"], digest_source="computed-at-import")
    _flaky_readback(store, monkeypatch)

    report = _importer(store, tmp_path).run([bad, good])

    assert [q["error_class"] for q in report.quarantined] == ["source_unreadable"]
    assert report.quarantined[0]["logical_id"] == "0"
    assert report.counters["published"] == 1          # the next object was still imported
    assert report.outcome == "partial" and report.exit_code == 1
    rows = [json.loads(l) for l in open(_ledger_path(tmp_path))]
    assert rows[-1]["type"] == "run_close"           # the run closed instead of vanishing
    assert store.keys_under("hear/v1/staging/") == []  # and the half-written stage was cleaned up


def test_a_key_provider_that_fails_during_the_re_stage_quarantines_that_object_only(
        pool, store, tmp_path, monkeypatch):
    """The same window, reached through the crypto boundary instead of the filesystem."""
    crypto = _crypto()
    real_data_key = crypto.provider.data_key
    calls = {"n": 0}

    def flaky(tenant_id, data_class, context):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise C.KeyUnavailable("the KMS went away mid-import")
        return real_data_key(tenant_id, data_class, context)

    monkeypatch.setattr(crypto.provider, "data_key", flaky)
    clip = _clip_task(pool["clip_paths"][0], pool["clip_digest"], pool["clip_bytes"],
                      "9f2c" + "0" * 28, "2026-09-12")
    _flaky_readback(store, monkeypatch)

    report = _importer(store, tmp_path, crypto=crypto).run([clip])

    assert [q["error_class"] for q in report.quarantined] == ["key_provider_unavailable"]
    assert report.counters["published"] == 0
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/staging/") == []
    rows = [json.loads(l) for l in open(_ledger_path(tmp_path))]
    assert rows[-1]["type"] == "run_close"
