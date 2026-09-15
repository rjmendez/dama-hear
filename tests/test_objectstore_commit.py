"""The commit: what makes it safe under a conditional put, and what has to make it safe without one.

⚠️THE COMMIT IS THE ONLY OBSERVABLE STEP, so every race in the import ends up here. This file
drives both primitives the design allows (import plan §3.3) through the same importer:

* **conditional put** (`LocalDirBackend(conditional_put=True)`) -- `O_CREAT|O_EXCL`, one request,
  naturally idempotent, and the loser of a race is *told* it lost;
* **lease + fencing** (`conditional_put=False`) -- check-then-write, which genuinely loses an
  update, so the commit is refused outright unless a live fencing lease is held and the epoch in
  the pointer document matches. The lost update is demonstrated here rather than asserted, because
  "the weak store needs a lease" is a claim that means nothing until you have watched it lose.

⚠️A LEASE IS WON ATOMICALLY OR NOT AT ALL. Read-then-write acquisition hands the same epoch to two
racers, and two writers holding one epoch fence neither of them. The race test forces both
acquirers to see the same stale lease and requires exactly one of them to come back with a lease.

⚠️EXACTLY ONE OBJECT IS WRITTEN TWICE: the republished open tail, as a new pointer generation
under an if-generation-matches commit, at most once per (class, partition) per run.
"""
from __future__ import annotations

import json

from hear.objectstore import backend as B
from hear.objectstore import keys as K
from hear.objectstore import stage as ST
from hear.objectstore import streaming as S

RUN = "2026-09-16T0000Z-commit"
SEG_CLASS = "record-seg"
PARTITION = ("2026-09-12", "mach")


def _importer(store, tmp_path, run=RUN, lease=None):
    return ST.Importer(store, str(tmp_path / "work" / run / "ledger.jsonl"), run,
                       lease=lease, git_commit="0000000")


def _tail_task(body: bytes, *, start: int = 0, republish: bool = False,
               expect_generation=None) -> ST.ImportTask:
    return ST.ImportTask(object_class=SEG_CLASS, logical_id="0", partition=PARTITION,
                         chunks=S.bytes_chunks(body), byte_range=(start, start + len(body)),
                         digest_source="stream-digest", republish=republish,
                         expect_generation=expect_generation)


def _pointer(store, okey):
    return json.loads(store.get_range(okey))


OKEY = K.object_key(SEG_CLASS, PARTITION, "0")


# ------------------------------------------------------------------ leases


def test_two_acquirers_racing_for_one_epoch_have_exactly_one_winner(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    first = store.acquire_lease("import/run", ttl_s=60, holder="a")
    clock["t"] += 61  # it expired; two importers now both believe they may take over

    stale = store._current_lease("import/run")
    monkeypatch.setattr(store, "_current_lease", lambda name: stale)
    b_lease = store.acquire_lease("import/run", ttl_s=60, holder="b")
    c_lease = store.acquire_lease("import/run", ttl_s=60, holder="c")

    assert [l is not None for l in (b_lease, c_lease)].count(True) == 1
    winner = b_lease or c_lease
    assert winner.epoch == first.epoch + 1


def test_a_live_lease_held_by_someone_else_is_not_handed_out(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    held = store.acquire_lease("import/run", ttl_s=60, holder="a")
    assert store.acquire_lease("import/run", ttl_s=60, holder="b") is None
    assert store.acquire_lease("import/run", ttl_s=60, holder="a").epoch == held.epoch
    store.release_lease(held)
    taken = store.acquire_lease("import/run", ttl_s=60, holder="b")
    assert taken is not None and taken.epoch == held.epoch + 1


def test_a_released_lease_cannot_be_used_to_commit(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    lease = store.acquire_lease("import/run", ttl_s=60, holder="a")
    store.release_lease(lease)
    report = _importer(store, tmp_path, lease=lease).run([_tail_task(b"{}\n")])
    assert report.outcome == "aborted"
    assert store.keys_under("hear/v1/obj/") == []


def test_the_epoch_in_the_pointer_document_is_the_one_that_committed_it(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), now=lambda: clock["t"])
    lease = store.acquire_lease("import/run", ttl_s=60, holder="a")
    _importer(store, tmp_path, lease=lease).run([_tail_task(b"{}\n")])
    assert _pointer(store, OKEY)["lease_epoch"] == lease.epoch


# ------------------------------------- the store without a conditional put


def test_a_store_without_a_conditional_put_really_does_lose_an_update(tmp_path):
    """The hazard, watched rather than asserted: check, gap, write, and the first write is gone."""
    store = B.LocalDirBackend(str(tmp_path / "store"), conditional_put=False)
    key = "hear/v1/obj/record-seg/x/0"

    def competitor(path):
        store.race_hook = None
        store.put_immutable(key, b"first writer")

    store.race_hook = competitor
    store.put_immutable(key, b"second writer")
    assert store.get_range(key) == b"second writer"  # the first writer's object is simply gone
    assert store.capabilities().commit_primitive == "lease-fencing"


def test_a_commit_without_a_lease_is_refused_when_the_store_cannot_do_a_conditional_put(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"), conditional_put=False)
    report = _importer(store, tmp_path).run([_tail_task(b"{}\n")])
    assert report.outcome == "aborted"
    assert store.keys_under("hear/v1/obj/") == []


def test_the_same_import_publishes_under_either_commit_primitive(tmp_path):
    """Both primitives must produce the same observable result, or the fallback is not a fallback."""
    body = b'{"a":1}\n'
    strong = B.LocalDirBackend(str(tmp_path / "strong"))
    _importer(strong, tmp_path, run=RUN + "-s").run([_tail_task(body)])

    weak = B.LocalDirBackend(str(tmp_path / "weak"), conditional_put=False)
    lease = weak.acquire_lease("import/run", ttl_s=600, holder="a")
    _importer(weak, tmp_path, run=RUN + "-w", lease=lease).run([_tail_task(body)])

    assert strong.keys_under("hear/v1/obj/") == weak.keys_under("hear/v1/obj/")
    assert strong.keys_under("hear/v1/blob/") == weak.keys_under("hear/v1/blob/")
    a, b = _pointer(strong, OKEY), _pointer(weak, OKEY)
    assert a["blob_key"] == b["blob_key"] and a["generation"] == b["generation"]


def test_a_zombie_with_a_stale_epoch_is_fenced_even_though_the_write_would_succeed(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), conditional_put=False,
                              now=lambda: clock["t"])
    zombie = store.acquire_lease("import/run", ttl_s=60, holder="zombie")
    clock["t"] += 61
    takeover = store.acquire_lease("import/run", ttl_s=60, holder="takeover")
    assert takeover.epoch == zombie.epoch + 1

    report = _importer(store, tmp_path, lease=zombie).run([_tail_task(b"{}\n")])
    assert report.outcome == "aborted"
    assert store.keys_under("hear/v1/obj/") == []

    ok = _importer(store, tmp_path, run=RUN + "-2", lease=takeover).run([_tail_task(b"{}\n")])
    assert ok.counters["published"] == 1


# ------------------------------------------------- conditional put conflicts


def test_a_racing_commit_of_the_same_blob_is_a_replay_for_the_loser(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    body = b'{"a":1}\n'
    _importer(store, tmp_path, run=RUN + "-a").run([_tail_task(body)])
    report = _importer(store, tmp_path, run=RUN + "-b").run([_tail_task(body)])
    assert report.counters["replayed"] == 1
    assert report.counters["published"] == 0
    assert report.exit_code == 0
    assert len(store.keys_under("hear/v1/obj/")) == 1


def test_a_racing_commit_of_a_different_blob_conflicts_and_does_not_overwrite(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    _importer(store, tmp_path, run=RUN + "-a").run([_tail_task(b'{"a":1}\n')])
    before = _pointer(store, OKEY)
    report = _importer(store, tmp_path, run=RUN + "-b").run([_tail_task(b'{"b":2}\n', start=8)])
    assert [q["error_class"] for q in report.quarantined] == ["pointer_conflict"]
    assert SEG_CLASS in report.halted_classes
    assert _pointer(store, OKEY) == before


# ------------------------------------------------------------- republishing


def _run_tail(store, tmp_path, rows, run, *, republish, expect_generation=None):
    body = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in rows)
    task = _tail_task(body, republish=republish, expect_generation=expect_generation)
    return _importer(store, tmp_path, run=run).run([task]), body


def test_an_open_tail_is_republished_as_a_new_generation_and_never_as_a_second_object(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    rows = [{"key": "%032x" % i} for i in range(6)]

    r1, _ = _run_tail(store, tmp_path, rows[:2], RUN + "-1", republish=True)
    r2, _ = _run_tail(store, tmp_path, rows[:4], RUN + "-2", republish=True)
    r3, body3 = _run_tail(store, tmp_path, rows, RUN + "-3", republish=True)

    assert [r.counters["published"] for r in (r1, r2, r3)] == [1, 1, 1]
    assert [r.counters["republished"] for r in (r1, r2, r3)] == [0, 1, 1]
    assert len(store.keys_under("hear/v1/obj/")) == 1  # one object, three generations

    head = _pointer(store, OKEY)
    assert head["generation"] == 3
    assert head["supersedes"] == 2
    assert head["blob_key"] == K.blob_key(S.digest_source(S.bytes_chunks(body3)).digest)

    gens = store.pointer_generations(OKEY)
    assert [g["generation"] for g in gens] == [1, 2, 3]
    assert [g["superseded_by"] for g in gens] == [2, 3, None]
    # Every generation's blob is still readable: rollback is republishing an older pointer.
    assert len(store.keys_under("hear/v1/blob/")) == 3


def test_a_republish_of_an_unchanged_tail_is_a_replay_and_adds_no_generation(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    rows = [{"key": "%032x" % i} for i in range(3)]
    _run_tail(store, tmp_path, rows, RUN + "-1", republish=True)
    report, _ = _run_tail(store, tmp_path, rows, RUN + "-2", republish=True)
    assert report.counters["replayed"] == 1
    assert report.counters["republished"] == 0
    assert _pointer(store, OKEY)["generation"] == 1
    assert [g["generation"] for g in store.pointer_generations(OKEY)] == [1]


def test_a_republish_that_loses_the_generation_race_re_reads_and_retries_once(tmp_path):
    """Another importer got generation 2 first; this one must not overwrite, it must re-read."""
    store = B.LocalDirBackend(str(tmp_path / "store"))
    rows = [{"key": "%032x" % i} for i in range(4)]
    _run_tail(store, tmp_path, rows[:2], RUN + "-1", republish=True)

    real_commit = store.commit_pointer
    fired = {"n": 0}

    def steal(key, doc, **kw):
        if fired["n"] == 0:
            fired["n"] = 1
            # A competing importer publishes generation 2 in the gap before our commit lands.
            other = dict(doc, generation=2, blob_key=doc["blob_key"] + "-other", supersedes=1)
            real_commit(key, other, expect_generation=1)
        return real_commit(key, doc, **kw)

    store.commit_pointer = steal
    report, body = _run_tail(store, tmp_path, rows, RUN + "-2", republish=True)
    store.commit_pointer = real_commit

    assert report.counters["generation_retries"] == 1
    assert report.counters["published"] == 1
    head = _pointer(store, OKEY)
    assert head["generation"] == 3 and head["supersedes"] == 2
    assert head["blob_key"] == K.blob_key(S.digest_source(S.bytes_chunks(body)).digest)
    assert [g["generation"] for g in store.pointer_generations(OKEY)] == [1, 2, 3]


def test_a_republish_that_keeps_losing_is_quarantined_rather_than_forced(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    rows = [{"key": "%032x" % i} for i in range(4)]
    _run_tail(store, tmp_path, rows[:2], RUN + "-1", republish=True)

    real_commit = store.commit_pointer
    state = {"gen": 1}

    def always_steal(key, doc, **kw):
        state["gen"] += 1
        other = dict(doc, generation=state["gen"], blob_key=doc["blob_key"] + "-other-%d" % state["gen"],
                     supersedes=state["gen"] - 1)
        real_commit(key, other, expect_generation=state["gen"] - 1)
        return real_commit(key, doc, **kw)

    store.commit_pointer = always_steal
    report, _ = _run_tail(store, tmp_path, rows, RUN + "-2", republish=True)
    store.commit_pointer = real_commit

    assert [q["error_class"] for q in report.quarantined] == ["generation_conflict"]
    assert report.counters["published"] == 0
    assert report.exit_code == 1


def test_only_one_open_tail_per_class_and_partition_is_republished_in_a_run(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    first = _tail_task(b'{"a":1}\n', republish=True)
    second = _tail_task(b'{"a":1}{"b":2}\n', start=8, republish=True)
    report = _importer(store, tmp_path).run([first, second])
    assert report.counters["published"] == 1
    assert [q["error_class"] for q in report.quarantined] == ["republish_repeated"]
    assert _pointer(store, OKEY)["generation"] == 1


def test_a_non_republish_task_can_never_add_a_generation(tmp_path):
    """The republish path is opt-in per task; everything else stays strict create-if-absent."""
    store = B.LocalDirBackend(str(tmp_path / "store"))
    _importer(store, tmp_path, run=RUN + "-1").run([_tail_task(b'{"a":1}\n')])
    report = _importer(store, tmp_path, run=RUN + "-2").run([_tail_task(b'{"b":2}\n', start=8)])
    assert [q["error_class"] for q in report.quarantined] == ["pointer_conflict"]
    assert _pointer(store, OKEY)["generation"] == 1


def test_a_republish_under_the_lease_primitive_is_fenced_without_a_live_lease(tmp_path):
    clock = {"t": 1000.0}
    store = B.LocalDirBackend(str(tmp_path / "store"), conditional_put=False,
                              now=lambda: clock["t"])
    lease = store.acquire_lease("import/run", ttl_s=60, holder="a")
    _importer(store, tmp_path, run=RUN + "-1", lease=lease).run(
        [_tail_task(b'{"a":1}\n', republish=True)])
    clock["t"] += 61  # the lease lapsed; the next republish must not land
    report = _importer(store, tmp_path, run=RUN + "-2", lease=lease).run(
        [_tail_task(b'{"a":1}{"b":2}\n', start=8, republish=True)])
    assert report.outcome == "aborted"
    assert _pointer(store, OKEY)["generation"] == 1


# ------------------------------------------- the claim/write window (roll-forward)


def _doc(gen: int, blob: str, *, supersedes=None):
    doc = {"object_key": OKEY, "blob_key": blob, "meta_key": "m", "generation": gen,
           "state": "published", "lease_epoch": None}
    if supersedes is not None:
        doc["supersedes"] = supersedes
    return doc


def test_a_first_publish_interrupted_between_its_claim_and_its_pointer_can_be_finished(tmp_path):
    """The claim is one write and the pointer is another; dying in between must not wedge the key.

    Before the fix the retry found generation 1 already claimed, read that as "someone else owns
    it", and returned a conflict -- forever, because the pointer it was conflicting with had never
    been written and so could never advance.
    """
    store = B.LocalDirBackend(str(tmp_path / "store"))
    store.fail_after_claim = [B.BackendTransient("evicted between the claim and the pointer")]

    try:
        store.commit_pointer(OKEY, _doc(1, "hear/v1/blob/sha256/aa"))
        raise AssertionError("the injected failure did not fire")
    except B.BackendTransient:
        pass
    assert store.head(OKEY) is None                       # nothing observable
    assert store._read_generation(OKEY, 1) is not None    # but the claim is on disk

    again = store.commit_pointer(OKEY, _doc(1, "hear/v1/blob/sha256/aa"))
    assert again.outcome == "committed" and again.generation == 1
    assert _pointer(store, OKEY)["blob_key"] == "hear/v1/blob/sha256/aa"
    assert [g["generation"] for g in store.pointer_generations(OKEY)] == [1]


def test_a_generation_claimed_by_a_different_document_is_still_a_real_conflict(tmp_path):
    """Roll-forward is only for *our own* interrupted work: byte-identical, or it is a conflict."""
    store = B.LocalDirBackend(str(tmp_path / "store"))
    store.fail_after_claim = [B.BackendTransient("evicted")]
    try:
        store.commit_pointer(OKEY, _doc(1, "hear/v1/blob/sha256/aa"))
    except B.BackendTransient:
        pass

    other = store.commit_pointer(OKEY, _doc(1, "hear/v1/blob/sha256/bb"))
    assert other.outcome == "conflict"
    assert other.existing_blob == "hear/v1/blob/sha256/aa"
    assert store.head(OKEY) is None  # and it did not overwrite the claim or publish a pointer


def test_a_republish_interrupted_between_its_claim_and_its_pointer_rolls_forward(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    assert store.commit_pointer(OKEY, _doc(1, "blob-1")).outcome == "committed"

    store.fail_after_claim = [B.BackendTransient("evicted")]
    doc2 = _doc(2, "blob-2", supersedes=1)
    try:
        store.commit_pointer(OKEY, doc2, expect_generation=1)
        raise AssertionError("the injected failure did not fire")
    except B.BackendTransient:
        pass
    assert _pointer(store, OKEY)["generation"] == 1

    finished = store.commit_pointer(OKEY, doc2, expect_generation=1)
    assert finished.outcome == "committed" and finished.generation == 2
    assert _pointer(store, OKEY)["blob_key"] == "blob-2"
    assert [g["generation"] for g in store.pointer_generations(OKEY)] == [1, 2]


def test_a_republished_generation_claimed_by_another_writer_still_loses_the_cas(tmp_path):
    store = B.LocalDirBackend(str(tmp_path / "store"))
    store.commit_pointer(OKEY, _doc(1, "blob-1"))
    store.fail_after_claim = [B.BackendTransient("evicted")]
    try:
        store.commit_pointer(OKEY, _doc(2, "blob-other", supersedes=1), expect_generation=1)
    except B.BackendTransient:
        pass

    lost = store.commit_pointer(OKEY, _doc(2, "blob-mine", supersedes=1), expect_generation=1)
    assert lost.outcome == "generation_conflict"
    assert _pointer(store, OKEY)["blob_key"] == "blob-1"


def test_an_importer_rerun_finishes_a_republish_whose_process_died_after_the_claim(tmp_path):
    """End to end: the wedge was an object no future run could ever publish again."""
    store = B.LocalDirBackend(str(tmp_path / "store"))
    rows = [{"key": "%032x" % i} for i in range(4)]
    _run_tail(store, tmp_path, rows[:2], RUN + "-w1", republish=True)

    store.fail_after_claim = [B.BackendTransient("the pod was evicted mid-commit")]
    try:
        _run_tail(store, tmp_path, rows, RUN + "-w2", republish=True)
        raise AssertionError("the injected failure did not fire")
    except B.BackendTransient:
        pass
    assert _pointer(store, OKEY)["generation"] == 1

    report, body = _run_tail(store, tmp_path, rows, RUN + "-w3", republish=True)
    assert report.counters["published"] == 1 and report.counters["quarantined"] == 0
    head = _pointer(store, OKEY)
    assert head["generation"] == 2 and head["supersedes"] == 1
    assert head["blob_key"] == K.blob_key(S.digest_source(S.bytes_chunks(body)).digest)
    assert [g["generation"] for g in store.pointer_generations(OKEY)] == [1, 2]


def test_a_write_that_dies_halfway_leaves_no_truncated_object_behind(tmp_path):
    """A partial immutable object is worse than none: every later run finds it and refuses.

    Create-if-absent means the truncated body would be treated as the real one forever -- read
    back, found to disagree with its own digest, and quarantined on every run. The interrupted
    write removes the key it created, which is not the `delete_object` the importer is denied
    because nothing ever pointed at it.
    """
    store = B.LocalDirBackend(str(tmp_path / "store"))

    def dying():
        yield b"a" * 1024
        raise OSError("the source went away mid-write")

    key = "hear/v1/blob/sha256/aa/bbbb/" + "c" * 64
    try:
        store.put_immutable_stream(key, dying)
        raise AssertionError("the injected failure did not fire")
    except OSError:
        pass
    assert store.head(key) is None
    assert store.keys_under("hear/v1/blob/") == []

    assert store.put_immutable_stream(key, S.bytes_chunks(b"the real body")).created is True
    assert store.get_range(key) == b"the real body"
