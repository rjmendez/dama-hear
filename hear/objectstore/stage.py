"""Stage -> verify -> publish metadata -> commit pointer. The publish contract, executable.

⚠️ONLY STEP 4 IS OBSERVABLE. An object is either pointed at or it does not exist. Staging is a
separate prefix with its own lifecycle, metadata is immutable and digest-addressed (so re-writing
it is a no-op), and the pointer commit is a single conditional put. There is no state in which a
reader can see half an object.

⚠️NO PAYLOAD IS EVER HELD WHOLE. The perch model is 392 MB and the importer is a guest on a pod
near its limit, so a task names a *source of chunks* and every stage of the pipeline -- hash,
encrypt, stage, read back, copy to the blob, re-verify a dedupe hit -- walks that source again
rather than keeping bytes around. The consequence worth stating: a source digest cannot be checked
before the bytes are staged, because checking it first would mean reading everything twice or
holding it. So staging happens first, and a mismatch deletes the staged object and quarantines.
Staging is not observable, which is precisely why it is safe to put it before the check.

⚠️VERIFY AGAINST THE DIGEST RECORDED AT INGEST, NOT ONE COMPUTED FROM THE SAME READ. For raw
archives and clip bodies the digest already exists -- `ledger.jsonl.sha256`, `index.jsonl.sha256`
-- and comparing against it proves a chain that starts at the fetch off the node. A digest
computed from the bytes just written proves only that the store echoed them back, and the metadata
says exactly which of the two happened (`digest_source`). Conflating them would mean the strongest
claim the import can make and the weakest look identical afterwards.

⚠️A RESTRICTED CLASS IS ENCRYPTED OR IT IS NOT IMPORTED. `keys.RESTRICTED_CLASSES` is derived from
the sensitivity labels, so it is `clip` and `raw` (ambient audio, 7-decimal coordinates) *and*
`tdoa-arrival-seg` and `tdoa-run`, whose arrival rows are precise locations in a short, guessable
document (key design §8). Without an injected `crypto.ObjectCrypto` -- and the default key provider
refuses everything -- such a task is quarantined `key_provider_unavailable` and the run continues.
Nothing in this package invents a key. When a provider *is* injected, the blob id is
`HMAC-SHA256(K_tenant_index, plaintext_digest)`, the plaintext digest goes only into the sealed
metadata sub-document, and every ledger row, counter and quarantine record for that object carries
the ciphertext digest instead -- a plaintext digest in a key or a log is a confirmation oracle.

⚠️A TRANSIENT STORE FAILURE IS CONTAINED AND RETRIED AT EVERY STEP THAT WRITES. Staging, the blob
put, the readback, the metadata put and the pointer commit are each idempotent by construction, so
each is retried `MAX_TRANSIENT_RETRIES` times and an exhausted one quarantines that object as
`backend_transient` with the run still closing, reporting and exiting non-zero. A backend I/O fault
(`ENOSPC`, `EIO`) is classified as that transient by the backend itself and is never re-read as
`source_unreadable`: a full volume is not corrupt evidence, and telling an operator it is sends
them to re-fetch bytes that were never wrong.

⚠️ONE OBJECT'S FAILURE ABORTS ONE OBJECT. A readback mismatch, a corrupt source, an unreadable
file: quarantine that object, count it, keep going -- an import that stops on the first bad byte
never finishes and teaches an operator to rerun blindly. Exactly three things halt more than one
object: a pointer conflict (halts that class), a model pin mismatch (halts that class), a failed
gate (aborts the run before any read). The run's exit is non-zero whenever anything was
quarantined, so "kept going" never means "nobody noticed".

⚠️EXACTLY ONE OBJECT IS ALLOWED TO BE WRITTEN TWICE: the open tail of a live stream, republished
as a new pointer generation under an if-generation-matches commit, at most once per
(class, partition) per run. Everything else is create-if-absent. A republish that loses the CAS
re-reads the pointer and retries once; if it loses again the object is quarantined rather than
forced, because two importers republishing the same tail is a concurrency bug, not a retry.

⚠️THE SOURCE IS OPENED READ-ONLY AND IS NEVER WRITTEN. No fsync on a source fd, no `utimes`, no
rename, no `.tmp` cleanup, no repair of a corrupt file. `/pool` has no backup; the importer must
never be the first mover on any byte it did not create.

NOT PRODUCTION: no backend is selected and no gate (G0-G6) is satisfied. `Importer` is exercised
by tests against `LocalDirBackend` and a synthetic pool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import backend as B
from . import crypto as C
from . import keys as K
from . import streaming as S
from .ledger import ResumeState, RunLedger, replay

#: Digest provenance, strongest first. Only the first two prove anything about the fetch itself.
STRONG_DIGEST_SOURCES = frozenset({"ledger.jsonl", "index.jsonl", "pinned-constant"})
WEAK_DIGEST_SOURCES = frozenset({"computed-at-import", "stream-digest", "sqlite-logical"})

MAX_TRANSIENT_RETRIES = 5
#: One re-read-and-retry of a lost generation CAS. A second loss is a concurrency bug.
MAX_GENERATION_RETRIES = 1
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
    #: Where the bytes come from: a path under the read-only source mount, an in-memory body (a
    #: small document), or a chunk-source factory (a segment, a re-encode, a synthetic fixture).
    source_path: Optional[str] = None
    body: Optional[bytes] = None
    chunks: Optional[S.ChunkSource] = None
    #: The digest recorded at ingest, if one exists, and where it came from.
    expected_digest: Optional[str] = None
    expected_bytes: Optional[int] = None
    digest_source: str = "computed-at-import"
    byte_range: Optional[Tuple[int, int]] = None
    generation: int = 1
    #: The open tail of a live stream: the one object allowed a second write, as a new generation.
    republish: bool = False
    #: What the caller last saw the pointer at. `None` means "read it at commit time".
    expect_generation: Optional[int] = None

    @property
    def source_ref(self) -> str:
        return self.source_path or ("%s/%s" % (self.object_class, self.logical_id))

    @property
    def task_id(self) -> str:
        return K.task_id(self.object_class, self.source_ref, self.byte_range)

    def source(self, *, chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> S.ChunkSource:
        """A fresh chunk iterator over this task's bytes, every time it is called."""
        if self.chunks is not None:
            return self.chunks
        if self.body is not None:
            return S.bytes_chunks(self.body, chunk_bytes=chunk_bytes)
        if self.source_path is None:
            raise ValueError("a task needs a source path, a body or a chunk source")
        return S.file_chunks(self.source_path, chunk_bytes=chunk_bytes)


@dataclass
class RunReport:
    run_id: str
    outcome: str = "complete"
    counters: Dict[str, int] = field(default_factory=lambda: {
        "published": 0, "replayed": 0, "deduped": 0, "quarantined": 0, "deferred": 0,
        "skipped_done": 0, "bytes_read": 0, "bytes_written": 0, "retries": 0,
        "deferred_partial_tail": 0, "republished": 0, "generation_retries": 0,
        "encrypted": 0,
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
                 git_commit: str = "unknown", crypto: Optional[C.ObjectCrypto] = None,
                 chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> None:
        self.store = store
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.holder = holder
        self.lease = lease
        self.git_commit = git_commit
        self.chunk_bytes = chunk_bytes
        #: ⚠️THE DEFAULT SEALS NOTHING AND NAMES NO CIPHER. `ObjectCrypto()` is a refusing key
        #: provider *and* a refusing cipher, so a restricted class is quarantined
        #: `key_provider_unavailable` rather than published. The previous default built
        #: `HmacCtrCipher` and passed `allow_test_cipher=True` itself, which made the guard against
        #: shipping a test cipher into a real run something the constructor routinely defeated.
        self.crypto = crypto or C.ObjectCrypto(tenant_id=tenant_id)
        self.ledger = RunLedger(ledger_path, run_id)
        self.resume: ResumeState = replay(ledger_path)
        #: (class, partition) already republished this run -- the §10.1 "never more than one" rule.
        self._republished: Set[Tuple[str, Tuple[str, ...]]] = set()

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
                           key_schema_version=K.KEY_SCHEMA_VERSION,
                           commit_primitive=self.store.capabilities().commit_primitive)
        for task in tasks:
            if task.object_class in report.halted_classes:
                report.counters["deferred"] += 1
                self.ledger.append("deferred", task_id=task.task_id, reason="class_halted")
                continue
            if self.resume.is_done(task.task_id):
                report.counters["skipped_done"] += 1
                continue
            outcome = self._one_guarded(task, report)
            if outcome == "fenced":
                report.outcome = "aborted"
                break
        self.ledger.checkpoint(report.counters, tasks[-1].task_id if tasks else None)
        if report.outcome != "aborted":
            report.outcome = "partial" if (report.counters["deferred"] or report.quarantined) else "complete"
        self.ledger.append("run_close", outcome=report.outcome, counters=dict(report.counters))
        return report

    # -------------------------------------------------------------- one task

    #: How a failure that belongs to one object is named when it escapes any step of the pipeline.
    #: ⚠️THE MAP IS THE HANDLER. The re-stage after a bad readback is a second full read of the
    #: source, so it can fail in every way the first read could -- an unreadable file, a key
    #: provider that went away, a source that moved. When only the first read was wrapped, those
    #: failures escaped `_one`, escaped `run`, and took the whole import down without a ledger
    #: close, a report, or a quarantine record for the object that caused it. One object's failure
    #: aborts one object, so every read goes through the same table.
    OBJECT_FAILURES = (
        (C.KeyUnavailable, "key_provider_unavailable", "message"),
        (C.SourceChanged, "source_changed_during_import", "none"),
        (C.TamperDetected, "stored_blob_corrupt", "none"),
        # ⚠️A STORE THAT KEEPS FAILING QUARANTINES ONE OBJECT, NOT THE RUN. Only the staging put
        # was ever retried, so a transient on the blob put, the metadata put or the pointer commit
        # escaped `_one`, escaped `run`, and took the import down with no ledger close, no report
        # and no quarantine record -- the exact failure the table below exists to prevent, on the
        # three steps most likely to meet a throttle. It is listed before `OSError` because a
        # backend fault that arrived as an `ENOSPC` has already been classified as this by
        # `backend.as_backend_fault`, and must never be re-read as unreadable source data.
        (B.BackendTransient, "backend_transient", "message"),
        (OSError, "source_unreadable", "type"),
    )

    def _failure_reason(self, exc: BaseException) -> Optional[Tuple[str, Optional[str]]]:
        for kind, reason, detail in self.OBJECT_FAILURES:
            if isinstance(exc, kind):
                if detail == "message":
                    return reason, str(exc)
                if detail == "type":
                    return reason, type(exc).__name__
                return reason, None
        return None

    def _one_guarded(self, task: ImportTask, report: RunReport) -> str:
        """Run one object and let no per-object failure out. The run still closes and reports."""
        try:
            return self._one(task, report)
        except BaseException as exc:  # noqa: BLE001 - narrowed immediately by the table
            named = self._failure_reason(exc)
            if named is None:
                raise
            reason, detail = named
            # Whatever this object staged is not going to be published, and a staged copy of a
            # restricted class is ciphertext nobody is coming back for. Removing it is the same
            # bounded-staging rule the publish path follows, and it is best-effort: a store that
            # is failing is not a reason to lose the quarantine record.
            try:
                self.store.delete_staged(self.run_id, task.task_id)
            except Exception:  # noqa: BLE001 - cleanup must never replace the real failure
                pass
            if detail is None:
                self._quarantine(task, report, reason)
            else:
                self._quarantine(task, report, reason, detail=detail)
            return "quarantined"

    def _with_transient_retry(self, task: ImportTask, report: RunReport, step: str, call):
        """Run one backend step, retrying a `BackendTransient` and containing an exhausted one.

        Every step this wraps is idempotent by construction -- an immutable create-if-absent, a
        digest-addressed metadata write, a pointer commit that replays or rolls forward -- which is
        what makes a retry safe rather than a second publish. When the budget is gone the exception
        is re-raised and `_one_guarded` turns it into one `backend_transient` quarantine record.
        """
        last: Optional[B.BackendTransient] = None
        for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
            try:
                return call()
            except B.BackendTransient as exc:
                last = exc
                report.counters["retries"] += 1
                self.ledger.append("failed", task_id=task.task_id, attempt=attempt, step=step,
                                   error_class="backend_transient", retryable=True)
        raise B.BackendTransient("%s failed %d times: %s" % (step, MAX_TRANSIENT_RETRIES, last))

    def _stage_guarded(self, task: ImportTask, report: RunReport,
                       restricted: bool) -> Tuple["StagedFacts", Optional[B.StagedRef]]:
        """`_stage`, with the source deleted from staging if it failed halfway through.

        Raises the same exceptions `_stage` does; `_one_guarded` is what turns them into a
        quarantine record, so the first read and the re-stage get identical treatment.
        """
        try:
            return self._stage(task, report, restricted)
        except Exception:
            self.store.delete_staged(self.run_id, task.task_id)
            raise

    def _one(self, task: ImportTask, report: RunReport) -> str:
        tid = task.task_id
        self.ledger.append("claim", task_id=tid, attempt=1)
        restricted = self.crypto.is_restricted(task.object_class)

        if task.republish:
            slot = (task.object_class, tuple(task.partition))
            if slot in self._republished:
                self._quarantine(task, report, "republish_repeated")
                return "quarantined"
            self._republished.add(slot)

        # ---------------------------------------------------- stage (streaming)
        sealed, staged = self._stage_guarded(task, report, restricted)
        if staged is None:
            self._quarantine(task, report, "backend_transient", detail="retries_exhausted")
            return "quarantined"

        plaintext_digest = sealed.plaintext_digest
        stored_digest = sealed.stored_digest
        report.counters["bytes_read"] += sealed.plaintext_bytes
        if restricted:
            report.counters["encrypted"] += 1

        expected = task.expected_digest or plaintext_digest
        strong = task.digest_source in STRONG_DIGEST_SOURCES

        # A source whose bytes disagree with the digest recorded when it was fetched is corrupt at
        # rest. It is quarantined and is NEVER repaired, re-hashed in place, or silently accepted
        # under a freshly computed digest -- that would launder a corruption into a fact.
        if strong and plaintext_digest != expected:
            self.store.delete_staged(self.run_id, staged.staging_id)
            self._quarantine(task, report, "source_digest_mismatch",
                             expected=expected, actual=plaintext_digest)
            return "quarantined"
        if task.expected_bytes is not None and sealed.plaintext_bytes != task.expected_bytes \
                and plaintext_digest == expected:
            self.store.delete_staged(self.run_id, staged.staging_id)
            self._quarantine(task, report, "length_mismatch_on_digest_match",
                             expected_bytes=task.expected_bytes,
                             actual_bytes=sealed.plaintext_bytes)
            return "quarantined"

        # ------------------------------------------- readback (streaming, no buffering)
        readback = S.digest_source(self.store.range_source(staged.key, chunk_bytes=self.chunk_bytes))
        if readback.digest != stored_digest or readback.bytes != sealed.stored_bytes:
            self.store.delete_staged(self.run_id, staged.staging_id)
            retry_sealed, staged = self._stage_guarded(task, report, restricted)  # one re-stage
            if staged is None:
                self._quarantine(task, report, "readback_mismatch",
                                 expected=self._public(task, expected, stored_digest))
                return "quarantined"
            report.counters["bytes_read"] += retry_sealed.plaintext_bytes
            # ⚠️THE RE-STAGE IS A NEW READ OF THE SOURCE, so it gets the same interrogation the
            # first one got. Carrying the first attempt's plaintext digest forward would let a
            # source that moved between the two reads be published under the digest of bytes the
            # store no longer holds -- a laundered change, which is the failure this whole file
            # exists to refuse.
            if retry_sealed.plaintext_digest != plaintext_digest \
                    or retry_sealed.plaintext_bytes != sealed.plaintext_bytes:
                self.store.delete_staged(self.run_id, staged.staging_id)
                self._quarantine(task, report, "source_changed_during_import")
                return "quarantined"
            sealed = retry_sealed
            stored_digest = retry_sealed.stored_digest
            again = S.digest_source(
                self.store.range_source(staged.key, chunk_bytes=self.chunk_bytes))
            if again.digest != stored_digest or again.bytes != sealed.stored_bytes:
                self._quarantine(task, report, "readback_mismatch",
                                 expected=self._public(task, expected, stored_digest))
                return "quarantined"
        self.ledger.append("verified", task_id=tid,
                           digest=self._public(task, expected, stored_digest),
                           digest_source=task.digest_source,
                           encrypted=restricted)

        # -------------------------------------------------------------- blob
        blob_key = K.blob_key(sealed.blob_id, algo=sealed.blob_algo,
                              tenant=self.tenant_id if restricted else None)
        put = self._with_transient_retry(
            task, report, "blob_put",
            lambda: self.store.put_immutable_stream(
                blob_key, self.store.range_source(staged.key, chunk_bytes=self.chunk_bytes)))
        deduped = not put.created
        if deduped:
            # Existing-blob trust is verified, not assumed: the only thing that catches silent
            # storage corruption is re-reading the blob before pointing a new object at it.
            stored = self._with_transient_retry(
                task, report, "blob_readback",
                lambda: S.digest_source(self.store.range_source(blob_key,
                                                                chunk_bytes=self.chunk_bytes)))
            if stored.digest != stored_digest or stored.bytes != sealed.stored_bytes:
                self._quarantine(task, report, "stored_blob_corrupt",
                                 expected=self._public(task, expected, stored_digest))
                return "quarantined"
            report.counters["deduped"] += 1
        report.counters["bytes_written"] += put.bytes_written

        # ---------------------------------------------------------- metadata
        meta = self._metadata(task, sealed, blob_key)
        meta_body = K.canonical_json(meta)
        mkey = K.meta_key(task.object_class, task.logical_id, K.sha256_hex(meta_body))
        mput = self._with_transient_retry(
            task, report, "metadata_put", lambda: self.store.put_immutable(mkey, meta_body))
        report.counters["bytes_written"] += mput.bytes_written

        # ------------------------------------------------------------ commit
        okey = K.object_key(task.object_class, task.partition, task.logical_id)
        return self._commit(task, report, okey, blob_key, mkey, staged, deduped, put)

    # ---------------------------------------------------------------- commit

    def _commit(self, task: ImportTask, report: RunReport, okey: str, blob_key: str, mkey: str,
                staged: B.StagedRef, deduped: bool, put: B.PutResult) -> str:
        tid = task.task_id
        attempts = MAX_GENERATION_RETRIES + 1 if task.republish else 1
        expect_generation = task.expect_generation
        generation = task.generation

        for attempt in range(1, attempts + 1):
            if task.republish:
                head = self._with_transient_retry(task, report, "pointer_head",
                                                  lambda: self.store.head(okey))
                current = head.generation if head else 0
                if expect_generation is None or attempt > 1:
                    expect_generation = current
                generation = current + 1
            doc = {"object_key": okey, "blob_key": blob_key, "meta_key": mkey,
                   "generation": generation, "state": "published",
                   "lease_epoch": self.lease.epoch if self.lease else None,
                   "lease_token": self.lease.token if self.lease else None}
            if task.republish:
                doc["supersedes"] = generation - 1 if generation > 1 else None
            commit = self._with_transient_retry(
                task, report, "pointer_commit",
                lambda: self.store.commit_pointer(
                    okey, doc, lease=self.lease,
                    expect_generation=expect_generation if task.republish else None))

            if commit.outcome == "generation_conflict":
                report.counters["generation_retries"] += 1
                self.ledger.append("failed", task_id=tid, attempt=attempt,
                                   error_class="generation_conflict", retryable=True)
                expect_generation = None  # re-read the head on the next attempt
                continue
            break

        if commit.outcome == "generation_conflict":
            # Two importers republishing one tail is a concurrency bug (import plan §10.1), not
            # something to win by trying harder.
            self._quarantine(task, report, "generation_conflict",
                             existing_blob=commit.existing_blob)
            return "quarantined"
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
            if task.republish and commit.generation > 1:
                report.counters["republished"] += 1
        self.ledger.append("published", task_id=tid, object_key=okey, blob_key=blob_key,
                           meta_key=mkey, generation=commit.generation, deduped=deduped,
                           bytes_written=put.bytes_written, idempotent_replay=replayed,
                           supersedes=doc.get("supersedes"))
        self.store.delete_staged(self.run_id, staged.staging_id)
        return "published"

    # ----------------------------------------------------------------- parts

    def _stage(self, task: ImportTask, report: RunReport,
               restricted: bool) -> Tuple["StagedFacts", Optional[B.StagedRef]]:
        """Stream the source into staging, hashing (and sealing) on the way past.

        Nothing is buffered: the plaintext digest, the stored digest and both byte counts are
        accumulated chunk by chunk and are the only things that survive.
        """
        source = task.source(chunk_bytes=self.chunk_bytes)
        if restricted:
            # Two streaming passes, never one buffer: the first measures the plaintext digest the
            # data key converges on, the second seals. `seal_stream` compares the two and refuses
            # a source that changed in between.
            measured = S.digest_source(source)
            sealed_chunks, finish = self.crypto.seal_stream(task.object_class, source,
                                                            measured.digest)
            ref = self._stage_with_retry(task, sealed_chunks, report)
            if ref is None:
                return StagedFacts.empty(), None
            obj = finish()
            facts = StagedFacts(
                plaintext_digest=obj.plaintext_digest, plaintext_bytes=obj.plaintext_bytes, stored_digest=obj.ciphertext_digest,
                stored_bytes=obj.ciphertext_bytes, blob_id=obj.blob_id, blob_algo=obj.blob_algo,
                sealed=obj)
        else:
            # A fresh digest per walk: a transient retry re-reads the source, and reusing a
            # half-walked accumulator would hand the next stage the digest of a truncated read.
            seen: Dict[str, S.StreamStat] = {}

            def _wrapped():
                d = S.DigestingChunks(source())
                yield from d
                seen["stat"] = d.stat()

            ref = self._stage_with_retry(task, _wrapped, report)
            if ref is None:
                return StagedFacts.empty(), None
            stat = seen["stat"]
            facts = StagedFacts(plaintext_digest=stat.digest, plaintext_bytes=stat.bytes,
                                stored_digest=stat.digest, stored_bytes=stat.bytes,
                                blob_id=stat.digest, blob_algo="sha256", sealed=None)
        self.ledger.append("staged", task_id=task.task_id, staging_id=ref.staging_id,
                           bytes=ref.bytes,
                           digest=self._public(task, facts.plaintext_digest, facts.stored_digest),
                           encrypted=restricted)
        return facts, ref

    def _stage_with_retry(self, task: ImportTask, source: S.ChunkSource,
                          report: RunReport) -> Optional[B.StagedRef]:
        staging_id = task.task_id
        for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
            try:
                return self.store.put_staged_stream(self.run_id, staging_id, source)
            except B.BackendTransient:
                report.counters["retries"] += 1
                self.ledger.append("failed", task_id=task.task_id, attempt=attempt,
                                   step="staging_put", error_class="backend_transient",
                                   retryable=True)
        return None

    def _public(self, task: ImportTask, plaintext_digest: Optional[str],
                stored_digest: Optional[str]) -> Optional[str]:
        """The digest this object is allowed to have written down outside the sealed metadata."""
        return self.crypto.public_digest(task.object_class, plaintext_digest, stored_digest)

    def _metadata(self, task: ImportTask, facts: "StagedFacts", blob_key: str) -> Dict[str, Any]:
        """The §5 envelope, trimmed to the fields this scaffold can honestly fill in.

        For a restricted class the clear part carries the HMAC blob id and the *ciphertext* digest;
        the plaintext digest is only in `sealed`, which is encrypted under the object's data key.
        """
        restricted = facts.sealed is not None
        blob: Dict[str, Any] = {
            "algo": facts.blob_algo, "blob_id": facts.blob_id, "blob_key": blob_key,
            "bytes": facts.stored_bytes, "digest_source": task.digest_source,
        }
        if restricted:
            blob["ciphertext_digest"] = facts.stored_digest
            blob["plaintext_digest_location"] = "sealed"
        else:
            blob["digest"] = facts.plaintext_digest
        doc: Dict[str, Any] = {
            "key_schema_version": K.KEY_SCHEMA_VERSION,
            "object_class": task.object_class,
            "object_key": K.object_key(task.object_class, task.partition, task.logical_id),
            "logical_id": task.logical_id,
            "blob": blob,
            "partition": list(task.partition),
            "governance": {"tenant_id": self.tenant_id, "state": "published",
                           "sensitivity": sorted(K.SENSITIVITY.get(task.object_class, ()))},
            "provenance": {"producer": "hear/objectstore/stage.py", "run_id": self.run_id,
                           "git_commit": self.git_commit, "status": "non-production-scaffold"},
        }
        if restricted:
            doc["sealed"] = facts.sealed.sealed_metadata
            doc["encryption"] = {"boundary": "client-side", "cipher": facts.sealed.cipher_name,
                                 "key_ref": facts.sealed.key_ref.as_metadata()}
        return doc

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


@dataclass(frozen=True)
class StagedFacts:
    """What is known after one streaming pass, and the only thing that outlives the bytes."""

    plaintext_digest: Optional[str] = None
    plaintext_bytes: int = 0
    stored_digest: Optional[str] = None
    stored_bytes: int = 0
    blob_id: Optional[str] = None
    blob_algo: str = "sha256"
    sealed: Optional[C.SealedObject] = None

    @classmethod
    def empty(cls) -> "StagedFacts":
        return cls()


def census(root: str) -> Dict[str, Tuple[int, int, int, str]]:
    """`path -> (size, mtime_ns, inode, sha256)` for every file under a tree.

    The non-mutation proof: take it before a run and after, and require equality. Size alone would
    miss an in-place byte flip; mtime alone would miss a write that preserved it. The digest is
    streamed, because this walks a tree that contains a 392 MB model file.
    """
    out: Dict[str, Tuple[int, int, int, str]] = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            digest = S.digest_source(S.file_chunks(full)).digest
            out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, st.st_ino, digest)
    return out
