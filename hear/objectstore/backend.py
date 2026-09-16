"""The whole backend surface the importer is allowed to depend on, plus a local-directory adapter.

⚠️NOT A PRODUCTION BACKEND. `LocalDirBackend` is a POSIX directory used by tests and by
`--dry-run`. No backend has been selected (import plan §20 G1); when one is, it implements this
same `Protocol` in exactly one module and no vendor type appears in any signature here -- the
repository already keeps `boto3`/AWS imports out of the standalone modules and this keeps that
true by construction. Nothing in this file imports a cloud SDK, and nothing in this file may.

⚠️A PAYLOAD IS A STREAM, NOT A `bytes`. The largest object in scope is the 392 MB perch model and
the importer is a guest on a pod that is already near its memory limit, so every payload-shaped
call takes a `streaming.ChunkSource` (a callable returning a fresh iterator) and writes chunk by
chunk. The `bytes` forms are kept for documents -- pointers, metadata, quarantine records -- which
are small by construction, and they are thin wrappers over the streaming forms so there is one
write path and one set of counters.

THE TWO COMMIT PRIMITIVES, AND WHY BOTH ARE HERE. The publish step is a *conditional* put:
create-if-absent, or create-if-generation-matches for the one object that is allowed to change
(the republished open tail). On a POSIX directory that is `O_CREAT|O_EXCL`, which is atomic. A
store that cannot do it needs the fallback -- a lease with a fencing epoch recorded in the pointer
document -- and that is a design change, not a config change, so both are modelled here
(`Capabilities.conditional_put`) and the pointer document carries `lease_epoch` either way.
`LocalDirBackend(conditional_put=False)` is the honest simulation of the weaker store: its writes
are check-then-write, which *does* lose an update under a race, and the only thing that makes it
safe is that a commit without a live fencing lease is refused.

⚠️A GENERATION CLAIM AND ITS POINTER WRITE ARE TWO OPERATIONS, so a writer can die between them.
The claim is the CAS and is made first; if a later attempt finds generation N already claimed by a
document naming the same object, blob and predecessor, it *finishes that commit* rather than
reporting a conflict. Treating the orphan claim as somebody else's would wedge the object forever:
generation N can never be re-claimed, and generation N+1 can never be reached because the pointer
never advanced. A claim that names a different blob is still a real conflict.

⚠️A LEASE IS WON ATOMICALLY OR IT IS NOT WON -- WHERE THE STORE CAN DO THAT AT ALL. Under a
conditional put, acquisition is `O_CREAT|O_EXCL` on a file named for the *epoch* being claimed, so
two acquirers racing for the same epoch have exactly one winner and the loser is told `None`.
Read-then-write acquisition -- read the doc, decide it is expired, write your own -- hands the same
epoch to both racers, and an epoch two writers share fences neither of them. Every acquisition,
including a same-holder reacquisition after a crash, takes a *new* epoch and a *new* per-acquisition
token; a renewal keeps both and only moves the expiry. Liveness is re-read and compared on the token,
never remembered, so the instant a new acquisition lands every earlier lease object is fenced.

⚠️THE WEAK STORE HAS NO CAS, AND THE SIMULATION SAYS SO. `LocalDirBackend(conditional_put=False)`
reports `Capabilities(conditional_put=False, atomic_cas=False)` and really has no atomic primitive:
its object writes, its generation claims and its lease acquisitions are all check-then-write with a
real gap (`race_hook` stands in it), and a write that lands inside the gap really does clobber. The
mitigation it does have is the one a real weak store has -- a read-after-write confirm, which
catches a racer that lands after you but not one that lands after your confirm -- plus the rule
that a commit on this store is refused outright unless a live fencing lease backs it.

⚠️A STORE FAULT IS NOT A SOURCE FAULT. `ENOSPC`, `EIO` and their neighbours raised by a *backend*
write are classified as `BackendTransient` (retried, then quarantined as `backend_transient`);
an `OSError` raised by the source iterator is left alone and becomes `source_unreadable`. The
classification lives in the write syscalls themselves (`as_backend_fault`), not around the loop
that pulls from the source, because a full volume quarantined as corrupt source data sends an
operator to re-fetch bytes that were never wrong.

⚠️THERE IS NO `delete_object`. Staging is the only deletable prefix in the importer's interface.
Deleting a published object belongs to the custodian role and the importer credential must not
have the capability, because the cheapest rung of the rollback ladder ("delete the shadow") must
be an action someone else takes deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
import shutil
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Tuple

from . import streaming as S


class BackendTransient(Exception):
    """A 5xx/timeout/throttle-shaped failure. Retryable; never a reason to skip an object."""


#: The errno values a *backend* write reports when the store is the thing that failed: the volume
#: is full, the device errored, the quota is gone. They are not source-data faults and must never
#: be classified as one -- a full disk that gets quarantined as `source_unreadable` sends an
#: operator to re-fetch bytes that were never wrong.
BACKEND_FAULT_ERRNOS = frozenset(
    e for e in (getattr(errno, n, None)
                for n in ("ENOSPC", "EIO", "EDQUOT", "EROFS", "ENOMEM", "EBUSY", "EAGAIN",
                          "ETIMEDOUT", "ECONNRESET", "EPIPE"))
    if e is not None)


def as_backend_fault(exc: OSError) -> BaseException:
    """Classify one OSError raised by a *store* operation. Anything else is returned untouched."""
    if exc.errno in BACKEND_FAULT_ERRNOS:
        return BackendTransient("%s: %s" % (errno.errorcode.get(exc.errno, exc.errno), exc))
    return exc


@dataclass(frozen=True)
class Capabilities:
    """What the store can actually do, probed once and recorded, never assumed.

    `tools/objectstore_probe.py` (import plan §19) fills this in against a scratch prefix when a
    backend is finally chosen. Until then the only instances are the two `LocalDirBackend` modes,
    which exist so the importer's behaviour under *both* primitives is tested before anyone has to
    live with whichever one the chosen store turns out to offer.

    ⚠️`atomic_cas` IS A SEPARATE ANSWER FROM `conditional_put`, and a store that cannot do the
    first cannot do the second either. A store with no conditional put has no atomic
    compare-and-swap to build a generation claim or a lease acquisition on: every such operation
    degrades to check-then-write, which loses updates. The simulation says so here rather than
    quietly using an exclusive create for the operations nobody was looking at.
    """

    conditional_put: bool = True
    read_after_write: bool = True
    multipart: bool = True
    atomic_cas: bool = True

    @property
    def commit_primitive(self) -> str:
        return "conditional-put" if self.conditional_put else "lease-fencing"


@dataclass(frozen=True)
class StagedRef:
    run_id: str
    staging_id: str
    key: str
    bytes: int


@dataclass(frozen=True)
class ObjectHead:
    key: str
    bytes: int
    digest: Optional[str] = None
    generation: int = 0


@dataclass(frozen=True)
class PutResult:
    key: str
    created: bool
    bytes_written: int


@dataclass(frozen=True)
class CommitResult:
    key: str
    #: `committed` | `replay` | `conflict` | `generation_conflict` | `fenced`
    outcome: str
    generation: int = 0
    existing_blob: Optional[str] = None


@dataclass(frozen=True)
class Lease:
    """One acquisition of a named lease. ⚠️THE TOKEN IS THE FENCE, THE EPOCH IS ITS ORDER.

    `epoch` is monotonic so a fenced writer can be ordered against the one that displaced it;
    `token` is unique per *acquisition*, so two writers can never be handed the same fence even if
    they use the same holder name, and a holder that reacquires after a crash is a different
    writer from the zombie copy of itself that may still be running.
    """

    name: str
    holder: str
    epoch: int
    expires_utc_s: float
    token: str = ""


class Backend(Protocol):
    """The whole surface. Anything the importer cannot say with these, it does not get to do."""

    def capabilities(self) -> Capabilities: ...

    def put_staged_stream(self, run_id: str, staging_id: str, source: S.ChunkSource) -> StagedRef: ...

    def iter_range(self, key: str, offset: int = 0, length: Optional[int] = None, *,
                   chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> Iterator[bytes]: ...

    def range_source(self, key: str, offset: int = 0, length: Optional[int] = None, *,
                     chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> S.ChunkSource:
        """A re-openable source over a stored object. The publish path re-reads, so it needs one."""

    def get_range(self, key: str, offset: int = 0, length: Optional[int] = None) -> bytes: ...

    def head(self, key: str) -> Optional[ObjectHead]: ...

    def put_immutable_stream(self, key: str, source: S.ChunkSource, *,
                             if_absent: bool = True) -> PutResult: ...

    def put_immutable(self, key: str, body: bytes, *, if_absent: bool = True) -> PutResult: ...

    def commit_pointer(self, key: str, doc: Dict[str, Any], *,
                       lease: Optional[Lease] = None,
                       expect_generation: Optional[int] = None) -> CommitResult: ...

    def delete_staged(self, run_id: str, staging_id: str) -> None: ...

    def list_prefix(self, prefix: str) -> Iterator[ObjectHead]: ...

    def acquire_lease(self, name: str, ttl_s: float, holder: str) -> Optional[Lease]: ...

    def renew_lease(self, lease: Lease, ttl_s: float) -> Optional[Lease]: ...

    def release_lease(self, lease: Lease) -> None: ...


def generation_key(object_key: str, generation: int) -> str:
    """The immutable record of one pointer generation, beside the pointer and never on top of it.

    `obj/<class>/<partition…>/<logical_id>` is a leaf, so the generations cannot hang underneath
    it; they live at `ptrgen/…/g000001.json` and each one is written create-if-absent, which is
    what makes "generation N is claimed" a race the loser is told about instead of an overwrite.
    """
    if generation < 1:
        raise ValueError("a pointer generation starts at 1")
    if "/obj/" not in object_key:
        raise ValueError("not an object pointer key: %r" % (object_key,))
    return "%s/g%06d.json" % (object_key.replace("/obj/", "/ptrgen/", 1), generation)


class LocalDirBackend:
    """A directory pretending to be an object store, with real conditional-put semantics.

    Durability follows the pattern the drain already uses: write a tmp file, fsync, `os.replace`
    for anything that may be overwritten; `O_CREAT|O_EXCL` for anything that may not. Nothing here
    is fast and nothing here needs to be -- its job is to make the *rules* executable.

    `conditional_put=False` turns the exclusive create into the check-then-write a weaker store
    would give you, including its lost update. That mode is not a degraded convenience: it is the
    thing the lease has to be strong enough to cover, and a test drives the race through it.
    """

    def __init__(self, root: str, *, now=time.time, conditional_put: bool = True,
                 race_hook: Optional[Callable[[str], None]] = None) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._now = now
        self._conditional = conditional_put
        #: Called between the existence check and the write of a non-conditional store, so a test
        #: can interleave a second writer and see the lost update for itself.
        self.race_hook = race_hook
        #: Fault injection for the retry/quarantine tests. Each entry is consumed once.
        self.fail_next_put: List[Exception] = []
        #: Fault injection between a generation claim and the pointer write it belongs to: the
        #: window whose recovery is the whole point of `_claim_generation`'s `identical`.
        self.fail_after_claim: List[Exception] = []
        #: Fault injection at the write syscall itself -- an `OSError(ENOSPC)`/`OSError(EIO)` is
        #: the store failing, and the classification that turns it into `BackendTransient` rather
        #: than a source-data fault is what these exercise. Each entry is consumed once.
        self.fail_next_write: List[OSError] = []
        #: The same seam on the read path: a device error reading a blob back is the store
        #: failing, and must not be read as "the source is corrupt". Each entry is consumed once.
        self.fail_next_read: List[OSError] = []
        self.reads = 0
        self.writes = 0
        self.bytes_written = 0
        #: The largest single buffer this backend was ever handed. The streaming proof reads it.
        self.max_chunk_bytes = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(conditional_put=self._conditional, read_after_write=True,
                            multipart=True, atomic_cas=self._conditional)

    # ------------------------------------------------------------------ paths

    def _path(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("bad key: %r" % (key,))
        return os.path.join(self.root, key)

    def _observe(self, chunk: bytes) -> None:
        if len(chunk) > self.max_chunk_bytes:
            self.max_chunk_bytes = len(chunk)

    def _write_stream(self, fd: int, source: S.ChunkSource) -> int:
        """Stream a source into an open fd. ⚠️THE TWO FAILURE KINDS ARE KEPT APART HERE.

        Only the write syscalls are wrapped: an `OSError` from `os.write`/`os.fsync` is the *store*
        failing (a full volume, a bad device) and becomes `BackendTransient`, which the importer
        retries and finally quarantines as `backend_transient`. An `OSError` raised by the source
        iterator -- an unreadable file, a vanished path -- is raised from outside the wrapper and
        stays an `OSError`, which the importer quarantines as `source_unreadable`. Wrapping the
        whole loop would classify a full disk as corrupt source data and send an operator to
        re-fetch bytes that were never wrong.
        """
        total = 0
        for chunk in S.guard(source()):
            self._observe(chunk)
            off = 0
            while off < len(chunk):
                try:
                    if self.fail_next_write:
                        raise self.fail_next_write.pop(0)
                    off += os.write(fd, chunk[off:])
                except OSError as exc:
                    raise as_backend_fault(exc) from exc
            total += len(chunk)
        try:
            os.fsync(fd)
        except OSError as exc:  # pragma: no cover - device-level failure at flush time
            raise as_backend_fault(exc) from exc
        return total

    def _open_write(self, path: str, flags: int) -> int:
        """Every open this backend performs for writing, with store faults classified as such."""
        try:
            return os.open(path, flags, 0o644)
        except FileExistsError:
            raise
        except OSError as exc:
            raise as_backend_fault(exc) from exc

    def _write_exclusive(self, path: str, source: S.ChunkSource) -> Optional[int]:
        """Create-if-absent, streaming. `None` means the key already existed."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not self._conditional:
            return self._write_checked(path, source)
        try:
            fd = self._open_write(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        try:
            written = self._write_stream(fd, source)
        except BaseException:
            # ⚠️AN INTERRUPTED WRITE IS REMOVED, and this is not the `delete_object` the importer
            # is denied: the key was created by this call, was never committed and was never
            # observable. Leaving it behind would publish a truncated blob that every later run
            # finds present, re-reads, and quarantines as corrupt forever.
            os.close(fd)
            _unlink_quietly(path)
            raise
        os.close(fd)
        self.writes += 1
        self.bytes_written += written
        return written

    def _write_checked(self, path: str, source: S.ChunkSource) -> Optional[int]:
        """What a store without conditional put gives you: a check, a gap, and a write.

        The gap is real and `race_hook` is where a test stands in it. Nothing in the importer is
        allowed to treat this as a commit unless it also holds a live fencing lease.
        """
        if os.path.exists(path):
            return None
        if self.race_hook is not None:
            self.race_hook(path)
        # No second check here on purpose: a second writer that arrived inside the gap is exactly
        # the lost update this mode has, and hiding it would make the weak store look like the
        # strong one in every test that matters.
        return self._replace_write(path, source)

    def _replace_write(self, path: str, source: S.ChunkSource) -> int:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        fd = self._open_write(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
        try:
            written = self._write_stream(fd, source)
        except BaseException:
            os.close(fd)
            _unlink_quietly(tmp)  # a source that died mid-write leaves no scratch file behind
            raise
        os.close(fd)
        try:
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - a store-level failure at publish time
            _unlink_quietly(tmp)
            raise as_backend_fault(exc) from exc
        self.writes += 1
        self.bytes_written += written
        return written

    def _maybe_fail(self) -> None:
        """Injected store faults, classified the way a real one would be before anyone sees them."""
        if self.fail_next_put:
            exc = self.fail_next_put.pop(0)
            raise as_backend_fault(exc) if isinstance(exc, OSError) else exc

    # --------------------------------------------------------------- staging

    def put_staged_stream(self, run_id: str, staging_id: str, source: S.ChunkSource) -> StagedRef:
        self._maybe_fail()
        key = "hear/v1/staging/%s/%s" % (run_id, staging_id)
        written = self._replace_write(self._path(key), source)
        return StagedRef(run_id=run_id, staging_id=staging_id, key=key, bytes=written)

    def put_staged(self, run_id: str, staging_id: str, body: bytes) -> StagedRef:
        return self.put_staged_stream(run_id, staging_id, S.bytes_chunks(body))

    def delete_staged(self, run_id: str, staging_id: str) -> None:
        path = self._path("hear/v1/staging/%s/%s" % (run_id, staging_id))
        if os.path.exists(path):
            os.unlink(path)

    # ----------------------------------------------------------------- reads

    def iter_range(self, key: str, offset: int = 0, length: Optional[int] = None, *,
                   chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> Iterator[bytes]:
        """A read of the *store*, so a device-level failure here is classified as a store fault.

        `FileNotFoundError` and friends keep their identity -- a missing key is not a transient --
        but an `EIO` on the way out of the blob is the backend failing, not the source, and the
        importer must be told which.
        """
        self.reads += 1
        path = self._path(key)
        end = None if length is None else offset + length
        try:
            if self.fail_next_read:
                raise self.fail_next_read.pop(0)
            for chunk in S.file_chunks(path, chunk_bytes=chunk_bytes, start=offset, end=end)():
                yield chunk
        except OSError as exc:
            raise as_backend_fault(exc) from exc

    def range_source(self, key: str, offset: int = 0, length: Optional[int] = None, *,
                     chunk_bytes: int = S.DEFAULT_CHUNK_BYTES) -> S.ChunkSource:
        """A re-openable chunk source over a stored object -- the shape the publish path needs."""

        def _open() -> Iterator[bytes]:
            yield from self.iter_range(key, offset, length, chunk_bytes=chunk_bytes)

        return _open

    def get_range(self, key: str, offset: int = 0, length: Optional[int] = None) -> bytes:
        """The whole-body read. Documents only -- a payload uses `iter_range`/`range_source`."""
        return b"".join(self.iter_range(key, offset, length))

    def head(self, key: str) -> Optional[ObjectHead]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        generation = 0
        if "/obj/" in key:
            try:
                generation = int(json.loads(self.get_range(key)).get("generation", 0))
            except (ValueError, UnicodeDecodeError):  # pragma: no cover - not a pointer document
                generation = 0
        return ObjectHead(key=key, bytes=os.path.getsize(path), generation=generation)

    def list_prefix(self, prefix: str) -> Iterator[ObjectHead]:
        base = self._path(prefix)
        walk_root = base if os.path.isdir(base) else os.path.dirname(base)
        for dirpath, _dirs, names in os.walk(walk_root):
            for name in sorted(names):
                full = os.path.join(dirpath, name)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                if key.startswith(prefix):
                    yield ObjectHead(key=key, bytes=os.path.getsize(full))

    # ---------------------------------------------------------------- writes

    def put_immutable_stream(self, key: str, source: S.ChunkSource, *,
                             if_absent: bool = True) -> PutResult:
        self._maybe_fail()
        if not if_absent:
            raise ValueError("published objects are immutable; if_absent=False is not offered")
        written = self._write_exclusive(self._path(key), source)
        return PutResult(key=key, created=written is not None, bytes_written=written or 0)

    def put_immutable(self, key: str, body: bytes, *, if_absent: bool = True) -> PutResult:
        return self.put_immutable_stream(key, S.bytes_chunks(body), if_absent=if_absent)

    def commit_pointer(self, key: str, doc: Dict[str, Any], *,
                       lease: Optional[Lease] = None,
                       expect_generation: Optional[int] = None) -> CommitResult:
        """The only observable step.

        `expect_generation is None` is the strict rule that covers every object except one: create
        if absent, replay if the same blob is already there, conflict otherwise. `expect_generation`
        is the republish of an open tail (key design §11 step 4, "if-generation-matches"): the
        commit succeeds only if the pointer is still at the generation the caller read, so two
        importers that both decided to republish generation 4 cannot both write it.
        """
        self._maybe_fail()
        generation = int(doc.get("generation", 1))
        if lease is not None and not self._lease_is_live(lease):
            # A zombie writer that lost its lease and came back is refused here rather than
            # trusted because it still has bytes in memory.
            return CommitResult(key=key, outcome="fenced")
        if not self._conditional and lease is None:
            # Without conditional put the write is check-then-write, so a commit that is not
            # fenced by a live lease is a lost update waiting to happen. Refuse it by construction.
            return CommitResult(key=key, outcome="fenced")

        path = self._path(key)
        existing = self._read_pointer(key)

        if expect_generation is None:
            if existing is None:
                claim, claimed = self._claim_generation(key, doc)
                if claim == "taken":
                    # Generation 1 is already owned by a different document. The pointer is missing,
                    # so this is not a replay -- it is two importers disagreeing about the object.
                    other = json.loads(claimed)
                    return CommitResult(key=key, outcome="conflict", generation=generation,
                                        existing_blob=other.get("blob_key"))
                self._after_claim_hook(key)
                # ⚠️THE CLAIM IS WHAT GETS PUBLISHED, not the caller's copy of it. The generation
                # record is immutable and was written first, so finishing an interrupted commit
                # means publishing exactly what was claimed; anything else leaves the pointer and
                # its own generation record disagreeing about the same generation.
                if self._write_exclusive(path, S.bytes_chunks(claimed)) is not None:
                    return CommitResult(key=key, outcome="committed", generation=generation)
                existing = self._read_pointer(key)
            assert existing is not None
            if existing.get("blob_key") == doc.get("blob_key"):
                return CommitResult(key=key, outcome="replay",
                                    generation=int(existing.get("generation", 1)),
                                    existing_blob=existing.get("blob_key"))
            return CommitResult(key=key, outcome="conflict",
                                generation=int(existing.get("generation", 1)),
                                existing_blob=existing.get("blob_key"))

        # ------------------------------------------------ republish (generation CAS)
        current = int(existing.get("generation", 0)) if existing else 0
        if existing is not None and existing.get("blob_key") == doc.get("blob_key"):
            # The tail did not change since the last run. A republish of identical content is a
            # no-op, not a new generation: generations that say nothing make a rollback ladder
            # longer without making it more useful.
            return CommitResult(key=key, outcome="replay", generation=current,
                                existing_blob=existing.get("blob_key"))
        if current != expect_generation or generation != current + 1:
            return CommitResult(key=key, outcome="generation_conflict", generation=current,
                                existing_blob=existing.get("blob_key") if existing else None)
        claim, claimed = self._claim_generation(key, doc)
        if claim == "taken":
            # Someone else already claimed this generation with a *different* document. The loser
            # re-reads and retries.
            return CommitResult(key=key, outcome="generation_conflict", generation=current,
                                existing_blob=existing.get("blob_key") if existing else None)
        # `claim == "identical"` is the crash-in-the-middle case: a previous attempt claimed this
        # generation and died before the pointer write. Rolling forward is the only outcome that
        # does not wedge the object forever, and it is safe precisely because the claim we found
        # points at the same blob, in the same generation, superseding the same one.
        self._after_claim_hook(key)
        self._replace_write(path, S.bytes_chunks(claimed))
        return CommitResult(key=key, outcome="committed", generation=generation)

    def _after_claim_hook(self, key: str) -> None:
        """Where a test stands between the generation claim and the pointer write."""
        if self.fail_after_claim:
            raise self.fail_after_claim.pop(0)

    def _read_pointer(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _read_generation(self, key: str, generation: int) -> Optional[Dict[str, Any]]:
        gpath = self._path(generation_key(key, generation))
        if not os.path.isfile(gpath):
            return None
        with open(gpath, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _claim_generation(self, key: str, doc: Dict[str, Any]) -> Tuple[str, bytes]:
        """Claim generation N exclusively. `(claimed|identical|taken, the bytes now on disk)`.

        ⚠️`identical` IS A ROLL-FORWARD, NOT A CONFLICT. The claim and the pointer write are two
        operations, and a process that dies between them leaves generation N claimed with no
        pointer to show for it. Reading that back as "someone else owns N" wedges the object for
        every future run: the retry can never claim N, and can never move to N+1 either, because
        the pointer never advanced. So the claim is compared against the document we were about to
        write, and a match means we are finishing somebody's interrupted work -- possibly our own.

        ⚠️THE COMPARISON IS ON IDENTITY, NOT ON EVERY BYTE. A generation record also carries
        provenance that legitimately differs between runs (the metadata object is digest-addressed
        and its document names the run that produced it), so a byte comparison would call the
        second run's honest retry a conflict and wedge exactly the case this exists for. What must
        match is what the pointer *means*: the object, the blob it points at, the generation and
        what it supersedes.
        """
        generation = int(doc.get("generation", 1))
        gpath = self._path(generation_key(key, generation))
        body = _doc_bytes(doc)
        os.makedirs(os.path.dirname(gpath), exist_ok=True)
        if not self._conditional:
            return self._claim_generation_without_cas(gpath, doc, body)
        try:
            fd = self._open_write(gpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with open(gpath, "rb") as fh:
                found = fh.read()
            same = _generation_identity(json.loads(found)) == _generation_identity(doc)
            return ("identical" if same else "taken"), found
        try:
            written = self._write_stream(fd, S.bytes_chunks(body))
        finally:
            os.close(fd)
        self.writes += 1
        self.bytes_written += written
        return "claimed", body

    def _claim_generation_without_cas(self, gpath: str, doc: Dict[str, Any],
                                      body: bytes) -> Tuple[str, bytes]:
        """The same claim on a store that has no compare-and-swap. ⚠️IT CAN LOSE ONE.

        The exclusive create is not available here, so the claim is read-then-write with a real gap
        (`race_hook` stands in it) and the claim record is *overwritten* by a writer that arrives
        inside it. Using `O_CREAT|O_EXCL` here anyway -- which is what this backend used to do in
        both modes -- made the weak store look like it had the one primitive it is defined by not
        having, so every generation and lease test passed for the wrong reason and the fallback
        primitive was never actually exercised.

        Read-after-write is a separately probed capability (`Capabilities.read_after_write`), so a
        confirm read is allowed and is done: it catches the interleaving where the other writer
        lands *after* us. It cannot catch the one where they land after our confirm, which is
        exactly why a commit on this store is refused unless a live fencing lease backs it.
        """
        existing = None
        if os.path.isfile(gpath):
            with open(gpath, "rb") as fh:
                existing = fh.read()
        if existing is None:
            if self.race_hook is not None:
                self.race_hook(gpath)
            if os.path.isfile(gpath):
                with open(gpath, "rb") as fh:
                    existing = fh.read()
        if existing is not None:
            same = _generation_identity(json.loads(existing)) == _generation_identity(doc)
            return ("identical" if same else "taken"), existing
        self._replace_write(gpath, S.bytes_chunks(body))
        with open(gpath, "rb") as fh:  # the confirm read: did somebody land on top of us?
            found = fh.read()
        if _generation_identity(json.loads(found)) != _generation_identity(doc):
            return "taken", found
        return "claimed", body

    def pointer_generations(self, key: str) -> List[Dict[str, Any]]:
        """Every generation of one pointer, oldest first, with `superseded_by` derived.

        ⚠️`superseded_by` IS DERIVED, NEVER WRITTEN BACK. A generation record is immutable, so the
        design's "`superseded_by` filled in on the previous generation" is read as "generation N+1
        exists", which is the same fact without a mutation of a published object.
        """
        prefix = generation_key(key, 1).rsplit("/", 1)[0] + "/"
        out: List[Dict[str, Any]] = []
        for head in self.list_prefix(prefix):
            out.append(json.loads(self.get_range(head.key)))
        out.sort(key=lambda d: int(d.get("generation", 0)))
        for i, doc in enumerate(out):
            doc["superseded_by"] = out[i + 1]["generation"] if i + 1 < len(out) else None
        return out

    # ---------------------------------------------------------------- leases

    def _lease_dir(self, name: str) -> str:
        return self._path("hear/v1/lease/%s" % name.replace("/", "_"))

    def _epoch_path(self, name: str, epoch: int) -> str:
        return os.path.join(self._lease_dir(name), "e%09d.json" % epoch)

    def _current_lease(self, name: str) -> Optional[Dict[str, Any]]:
        d = self._lease_dir(name)
        if not os.path.isdir(d):
            return None
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        if not names:
            return None
        with open(os.path.join(d, names[-1]), "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _lease_is_live(self, lease: Lease) -> bool:
        """⚠️LIVENESS IS RE-READ, NOT REMEMBERED, and the token is what is compared.

        An epoch alone cannot tell two acquisitions apart when the same holder name reacquires --
        which is exactly the crash-and-restart case -- so a zombie and its replacement would both
        pass an epoch check and each believe it was fencing the other. The token is unique per
        acquisition, so the moment a new acquisition lands, every earlier lease object stops being
        live, under either commit primitive.
        """
        cur = self._current_lease(lease.name)
        if cur is None:
            return False
        if int(cur["epoch"]) != lease.epoch:
            return False
        if str(cur.get("token", "")) != lease.token:
            return False
        return float(cur["expires_utc_s"]) > self._now()

    def acquire_lease(self, name: str, ttl_s: float, holder: str) -> Optional[Lease]:
        """Claim the next epoch. ⚠️EVERY ACQUISITION IS A NEW EPOCH AND A NEW TOKEN.

        A live lease held by somebody else is not handed out. A live lease held by *this* holder is
        a takeover of its own acquisition -- the crash-and-restart case, where the zombie may still
        be running with bytes in memory -- and it gets a fresh epoch and a fresh token rather than a
        copy of the old one, so the zombie is fenced at its next commit. Handing back the epoch that
        was already out there (what this used to do for a same-holder acquirer) gave two concurrent
        writers one fence, and a fence two writers share fences neither of them.
        """
        cur = self._current_lease(name)
        now = self._now()
        if cur is not None and float(cur["expires_utc_s"]) > now and cur["holder"] != holder:
            return None
        epoch = int(cur["epoch"]) + 1 if cur else 1
        return self._write_epoch(name, holder, epoch, now + ttl_s, exclusive=True,
                                 token=_new_lease_token())

    def renew_lease(self, lease: Lease, ttl_s: float) -> Optional[Lease]:
        """Extend the acquisition the caller already holds: same epoch, same token, later expiry.

        A renewal is not an acquisition and must not mint a new fence -- every heartbeat would
        otherwise invalidate the epoch already written into the pointers this writer committed. A
        lease that is no longer live is refused here and has to go back through `acquire_lease`,
        where it becomes a new fence that displaces whatever took it.
        """
        if not self._lease_is_live(lease):
            return None
        return self._write_epoch(lease.name, lease.holder, lease.epoch, self._now() + ttl_s,
                                 exclusive=False, token=lease.token)

    def release_lease(self, lease: Lease) -> None:
        if self._lease_is_live(lease):
            self._write_epoch(lease.name, lease.holder, lease.epoch, 0.0, exclusive=False,
                              token=lease.token)

    def _write_epoch(self, name: str, holder: str, epoch: int, expires: float, *,
                     exclusive: bool, token: str) -> Optional[Lease]:
        path = self._epoch_path(name, epoch)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"name": name, "holder": holder, "epoch": epoch, "expires_utc_s": expires,
               "token": token}
        source = S.bytes_chunks(json.dumps(doc, sort_keys=True).encode("utf-8"))
        if exclusive and self._conditional:
            try:
                fd = self._open_write(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return None  # another acquirer claimed this epoch first
            try:
                self._write_stream(fd, source)
            finally:
                os.close(fd)
        elif exclusive:
            # ⚠️THERE IS NO CAS ON THIS STORE, SO THERE IS NONE HERE EITHER. Acquisition used to
            # use an exclusive create in both modes, which made the lease-fencing primitive -- the
            # whole reason the weak mode exists -- test as though it had the conditional put it is
            # defined by not having. It is check, gap, write, confirm: the confirm read (a probed
            # capability, `Capabilities.read_after_write`) catches an acquirer that lands after us,
            # and the one that lands after the confirm is caught by `_lease_is_live`, which re-reads
            # the record and compares the token before any commit is let through.
            if os.path.isfile(path):
                return None
            if self.race_hook is not None:
                self.race_hook(path)
            if os.path.isfile(path):
                return None
            self._replace_write(path, source)
            with open(path, "rb") as fh:
                if json.loads(fh.read().decode("utf-8")).get("token") != token:
                    return None
        else:
            self._replace_write(path, source)
        return Lease(name=name, holder=holder, epoch=epoch, expires_utc_s=expires, token=token)

    # ------------------------------------------------------------- test aids

    def corrupt(self, key: str, body: bytes) -> None:
        """Simulate silent storage corruption. Tests only; no importer path calls this."""
        path = self._path(key)
        with open(path, "wb") as fh:
            fh.write(body)

    def wipe_staging(self, run_id: str) -> None:
        path = self._path("hear/v1/staging/%s" % run_id)
        shutil.rmtree(path, ignore_errors=True)

    def keys_under(self, prefix: str) -> List[str]:
        return sorted(h.key for h in self.list_prefix(prefix))


def _new_lease_token() -> str:
    """A fresh fence for one acquisition. ⚠️NEVER DERIVED FROM THE HOLDER, THE EPOCH OR THE CLOCK.

    Anything derived from those collides for exactly the pair that must not collide: the same
    holder reacquiring after a crash, on a store whose epoch counter the zombie also knows.
    """
    return os.urandom(16).hex()


def _unlink_quietly(path: str) -> None:
    """Remove a file this call created and never committed. Nothing else is ever unlinked here."""
    try:
        os.unlink(path)
    except FileNotFoundError:  # pragma: no cover - the write never got that far
        pass


def _doc_bytes(doc: Dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


#: What two writers must agree on before one may finish the other's interrupted commit.
GENERATION_IDENTITY_FIELDS = ("object_key", "blob_key", "generation", "supersedes", "state")


def _generation_identity(doc: Dict[str, Any]) -> tuple:
    return tuple(doc.get(f) for f in GENERATION_IDENTITY_FIELDS)
