"""Stage -> verify -> publish metadata -> commit pointer. The publish contract, executable.

⚠️ONLY STEP 4 IS OBSERVABLE. An object is either pointed at or it does not exist. Staging is a
separate prefix with its own lifecycle, metadata is immutable and digest-addressed (so re-writing
it is a no-op), and the pointer commit is a single conditional put. There is no state in which a
reader can see half an object.

⚠️VERIFY AGAINST THE DIGEST RECORDED AT INGEST, NOT ONE COMPUTED FROM THE SAME READ. For raw
archives and clip bodies the digest already exists -- `ledger.jsonl.sha256`, `index.jsonl.sha256`
-- and comparing against it proves a chain that starts at the fetch off the node. A digest
computed from the bytes just written proves only that the store echoed them back, and the metadata
says exactly which of the two happened (`digest_source`). Conflating them would mean the strongest
claim the import can make and the weakest look identical afterwards.

⚠️ONE OBJECT'S FAILURE ABORTS ONE OBJECT. A readback mismatch, a corrupt source, an unreadable
file: quarantine that object, count it, keep going -- an import that stops on the first bad byte
never finishes and teaches an operator to rerun blindly. Exactly three things halt more than one
object: a pointer conflict (halts that class), a model pin mismatch (halts that class), a failed
gate (aborts the run before any read). The run's exit is non-zero whenever anything was
quarantined, so "kept going" never means "nobody noticed".

⚠️THE SOURCE IS OPENED READ-ONLY AND IS NEVER WRITTEN. No fsync on a source fd, no `utimes`, no
rename, no `.tmp` cleanup, no repair of a corrupt file. `/pool` has no backup; the importer must
never be the first mover on any byte it did not create.

NOT PRODUCTION: no backend is selected and no gate (G0-G6) is satisfied. `Importer` is exercised
by tests against `LocalDirBackend` and a synthetic pool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import backend as B
from . import keys as K
from .ledger import ResumeState, RunLedger, replay

#: Digest provenance, strongest first. Only the first two prove anything about the fetch itself.
STRONG_DIGEST_SOURCES = frozenset({"ledger.jsonl", "index.jsonl", "pinned-constant"})
WEAK_DIGEST_SOURCES = frozenset({"computed-at-import", "stream-digest", "sqlite-logical"})

MAX_TRANSIENT_RETRIES = 5
#: The classes that halt when they conflict; everything else quarantines one object and continues.
HALTING_ERRORS = frozenset({"pointer_conflict", "model_pin_mismatch", "gate_failed"})


class GateFailed(Exception):
    """A preflight gate is unmet. Nothing is read, nothing is staged, nothing is published."""


@dataclass
class ImportTask:
    """One unit of work. Its id is derived from what it is, never from when it was scheduled."""

    object_class: str
    logical_id: str
    partition: Tuple[str, ...]
    #: Where the bytes come from: a path under the read-only source mount, or an in-memory body
    #: (a stream segment already frozen and trimmed by `streams.py`).
    source_path: Optional[str] = None
    body: Optional[bytes] = None
    #: The digest recorded at ingest, if one exists, and where it came from.
    expected_digest: Optional[str] = None
    expected_bytes: Optional[int] = None
    digest_source: str = "computed-at-import"
    byte_range: Optional[Tuple[int, int]] = None
    generation: int = 1

    @property
    def source_ref(self) -> str:
        return self.source_path or ("%s/%s" % (self.object_class, self.logical_id))

    @property
    def task_id(self) -> str:
        return K.task_id(self.object_class, self.source_ref, self.byte_range)


@dataclass
class RunReport:
    run_id: str
    outcome: str = "complete"
    counters: Dict[str, int] = field(default_factory=lambda: {
        "published": 0, "replayed": 0, "deduped": 0, "quarantined": 0, "deferred": 0,
        "skipped_done": 0, "bytes_read": 0, "bytes_written": 0, "retries": 0,
        "deferred_partial_tail": 0,
    })
    quarantined: List[Dict[str, Any]] = field(default_factory=list)
    halted_classes: Set[str] = field(default_factory=set)
    published_keys: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Non-zero whenever anything was quarantined or the run did not complete."""
        return 0 if (self.outcome == "complete" and not self.quarantined) else 1


class Importer:
    def __init__(self, store: B.Backend, ledger_path: str, run_id: str, *,
                 tenant_id: str = "dama", holder: str = "test", lease: Optional[B.Lease] = None,
                 git_commit: str = "unknown") -> None:
        self.store = store
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.holder = holder
        self.lease = lease
        self.git_commit = git_commit
        self.ledger = RunLedger(ledger_path, run_id)
        self.resume: ResumeState = replay(ledger_path)

    # ------------------------------------------------------------- preflight

    def preflight(self, gates: Dict[str, bool]) -> None:
        unmet = sorted(name for name, ok in gates.items() if not ok)
        if unmet:
            raise GateFailed("unmet gates: %s" % ", ".join(unmet))

    # ------------------------------------------------------------------- run

    def run(self, tasks: Sequence[ImportTask], *, gates: Optional[Dict[str, bool]] = None) -> RunReport:
        report = RunReport(run_id=self.run_id)
        if gates is not None:
            self.preflight(gates)
        self.ledger.append("run_open", gates=dict(gates or {}), git_commit=self.git_commit,
                           key_schema_version=K.KEY_SCHEMA_VERSION)
        for task in tasks:
            if task.object_class in report.halted_classes:
                report.counters["deferred"] += 1
                self.ledger.append("deferred", task_id=task.task_id, reason="class_halted")
                continue
            if self.resume.is_done(task.task_id):
                report.counters["skipped_done"] += 1
                continue
            outcome = self._one(task, report)
            if outcome == "fenced":
                report.outcome = "aborted"
                break
        self.ledger.checkpoint(report.counters, tasks[-1].task_id if tasks else None)
        if report.outcome != "aborted":
            report.outcome = "partial" if (report.counters["deferred"] or report.quarantined) else "complete"
        self.ledger.append("run_close", outcome=report.outcome, counters=dict(report.counters))
        return report

    # -------------------------------------------------------------- one task

    def _one(self, task: ImportTask, report: RunReport) -> str:
        tid = task.task_id
        self.ledger.append("claim", task_id=tid, attempt=1)

        try:
            body = self._read_source(task)
        except OSError as exc:
            self._quarantine(task, report, "source_unreadable", detail=type(exc).__name__)
            return "quarantined"
        report.counters["bytes_read"] += len(body)

        actual = K.sha256_hex(body)
        expected = task.expected_digest or actual
        strong = task.digest_source in STRONG_DIGEST_SOURCES

        # A source whose bytes disagree with the digest recorded when it was fetched is corrupt at
        # rest. It is quarantined and is NEVER repaired, re-hashed in place, or silently accepted
        # under a freshly computed digest -- that would launder a corruption into a fact.
        if strong and actual != expected:
            self._quarantine(task, report, "source_digest_mismatch",
                             expected=expected, actual=actual)
            return "quarantined"
        if task.expected_bytes is not None and len(body) != task.expected_bytes and actual == expected:
            self._quarantine(task, report, "length_mismatch_on_digest_match",
                             expected_bytes=task.expected_bytes, actual_bytes=len(body))
            return "quarantined"

        staged = self._stage_with_retry(task, body, report)
        if staged is None:
            self._quarantine(task, report, "backend_transient", detail="retries_exhausted")
            return "quarantined"
        self.ledger.append("staged", task_id=tid, staging_id=staged.staging_id,
                           bytes=staged.bytes, digest=actual)

        # Readback: fetch what the store now holds and compare it to the RECORDED digest.
        readback = self.store.get_range(staged.key)
        if K.sha256_hex(readback) != expected:
            self.store.delete_staged(self.run_id, staged.staging_id)
            staged = self._stage_with_retry(task, body, report)  # one re-stage, then give up
            readback = self.store.get_range(staged.key) if staged else b""
            if staged is None or K.sha256_hex(readback) != expected:
                self._quarantine(task, report, "readback_mismatch", expected=expected)
                return "quarantined"
        if len(readback) != len(body):
            self._quarantine(task, report, "length_mismatch_on_digest_match",
                             expected_bytes=len(body), actual_bytes=len(readback))
            return "quarantined"
        self.ledger.append("verified", task_id=tid, digest=expected,
                           digest_source=task.digest_source)

        blob_key = K.blob_key(expected)
        put = self.store.put_immutable(blob_key, body)
        deduped = not put.created
        if deduped:
            # Existing-blob trust is verified, not assumed: the only thing that catches silent
            # storage corruption is re-reading the blob before pointing a new object at it.
            stored = self.store.get_range(blob_key)
            if K.sha256_hex(stored) != expected or len(stored) != len(body):
                self._quarantine(task, report, "stored_blob_corrupt", expected=expected)
                return "quarantined"
            report.counters["deduped"] += 1
        report.counters["bytes_written"] += put.bytes_written

        meta = self._metadata(task, expected, len(body), blob_key)
        meta_body = K.canonical_json(meta)
        mkey = K.meta_key(task.object_class, task.logical_id, K.sha256_hex(meta_body))
        mput = self.store.put_immutable(mkey, meta_body)
        report.counters["bytes_written"] += mput.bytes_written

        okey = K.object_key(task.object_class, task.partition, task.logical_id)
        doc = {"object_key": okey, "blob_key": blob_key, "meta_key": mkey,
               "generation": task.generation, "state": "published",
               "lease_epoch": self.lease.epoch if self.lease else None}
        commit = self.store.commit_pointer(okey, doc, lease=self.lease)

        if commit.outcome == "fenced":
            self.ledger.append("failed", task_id=tid, attempt=1, error_class="lease_fenced",
                               retryable=True)
            return "fenced"
        if commit.outcome == "conflict":
            # One logical id may never point at two blobs within a generation. This is a human
            # decision, so the class halts rather than the importer guessing which blob is right.
            self._quarantine(task, report, "pointer_conflict", existing_blob=commit.existing_blob)
            report.halted_classes.add(task.object_class)
            return "halted"

        replayed = commit.outcome == "replay"
        if replayed:
            report.counters["replayed"] += 1
        else:
            report.counters["published"] += 1
            report.published_keys.append(okey)
        self.ledger.append("published", task_id=tid, object_key=okey, blob_key=blob_key,
                           meta_key=mkey, generation=task.generation, deduped=deduped,
                           bytes_written=put.bytes_written, idempotent_replay=replayed)
        self.store.delete_staged(self.run_id, staged.staging_id)
        return "published"

    # ----------------------------------------------------------------- parts

    def _read_source(self, task: ImportTask) -> bytes:
        if task.body is not None:
            return task.body
        # "rb" is the only mode this module ever opens a source with.
        with open(task.source_path, "rb") as fh:
            return fh.read()

    def _stage_with_retry(self, task: ImportTask, body: bytes,
                          report: RunReport) -> Optional[B.StagedRef]:
        staging_id = task.task_id
        for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
            try:
                return self.store.put_staged(self.run_id, staging_id, body)
            except B.BackendTransient:
                report.counters["retries"] += 1
                self.ledger.append("failed", task_id=task.task_id, attempt=attempt,
                                   error_class="backend_transient", retryable=True)
        return None

    def _metadata(self, task: ImportTask, digest: str, nbytes: int, blob_key: str) -> Dict[str, Any]:
        """The §5 envelope, trimmed to the fields this scaffold can honestly fill in."""
        return {
            "key_schema_version": K.KEY_SCHEMA_VERSION,
            "object_class": task.object_class,
            "object_key": K.object_key(task.object_class, task.partition, task.logical_id),
            "logical_id": task.logical_id,
            "blob": {"algo": "sha256", "digest": digest, "bytes": nbytes,
                     "digest_source": task.digest_source, "blob_key": blob_key},
            "partition": list(task.partition),
            "governance": {"tenant_id": self.tenant_id, "state": "published"},
            "provenance": {"producer": "hear/objectstore/stage.py", "run_id": self.run_id,
                           "git_commit": self.git_commit, "status": "non-production-scaffold"},
        }

    def _quarantine(self, task: ImportTask, report: RunReport, error_class: str, **detail: Any) -> None:
        """Record the refusal as an object. No source payload, no path for a restricted class."""
        doc: Dict[str, Any] = {
            "run_id": self.run_id, "task_id": task.task_id, "object_class": task.object_class,
            "error_class": error_class, "logical_id": task.logical_id,
        }
        doc.update({k: v for k, v in detail.items() if v is not None})
        if task.object_class in K.RESTRICTED_CLASSES:
            doc.pop("expected", None)
            doc.pop("actual", None)
            doc["digests_withheld"] = "restricted_class"
        qkey = K.quarantine_key(self.run_id, task.task_id)
        self.store.put_immutable(qkey, K.canonical_json(doc))
        report.counters["quarantined"] += 1
        report.quarantined.append(doc)
        self.ledger.append("failed", task_id=task.task_id, attempt=1, error_class=error_class,
                           retryable=False, quarantine_key=qkey)


def census(root: str) -> Dict[str, Tuple[int, int, int, str]]:
    """`path -> (size, mtime_ns, inode, sha256)` for every file under a tree.

    The non-mutation proof: take it before a run and after, and require equality. Size alone would
    miss an in-place byte flip; mtime alone would miss a write that preserved it.
    """
    out: Dict[str, Tuple[int, int, int, str]] = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            with open(full, "rb") as fh:
                digest = K.sha256_hex(fh.read())
            out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, st.st_ino, digest)
    return out
