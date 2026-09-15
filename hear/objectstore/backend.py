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

⚠️A LEASE IS WON ATOMICALLY OR IT IS NOT WON. Acquisition is `O_CREAT|O_EXCL` on a file named for
the *epoch* being claimed, so two acquirers racing for the same epoch have exactly one winner and
the loser is told `None` rather than handed a lease it does not own. Read-then-write acquisition
-- read the doc, decide it is expired, write your own -- hands the same epoch to both racers, and
an epoch two writers share fences neither of them.

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
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol

from . import streaming as S


class BackendTransient(Exception):
    """A 5xx/timeout/throttle-shaped failure. Retryable; never a reason to skip an object."""


@dataclass(frozen=True)
class Capabilities:
    """What the store can actually do, probed once and recorded, never assumed.

    `tools/objectstore_probe.py` (import plan §19) fills this in against a scratch prefix when a
    backend is finally chosen. Until then the only instances are the two `LocalDirBackend` modes,
    which exist so the importer's behaviour under *both* primitives is tested before anyone has to
    live with whichever one the chosen store turns out to offer.
    """

    conditional_put: bool = True
    read_after_write: bool = True
    multipart: bool = True

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
    name: str
    holder: str
    epoch: int
    expires_utc_s: float


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
        self.reads = 0
        self.writes = 0
        self.bytes_written = 0
        #: The largest single buffer this backend was ever handed. The streaming proof reads it.
        self.max_chunk_bytes = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(conditional_put=self._conditional, read_after_write=True, multipart=True)

    # ------------------------------------------------------------------ paths

    def _path(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("bad key: %r" % (key,))
        return os.path.join(self.root, key)

    def _observe(self, chunk: bytes) -> None:
        if len(chunk) > self.max_chunk_bytes:
            self.max_chunk_bytes = len(chunk)

    def _write_stream(self, fd: int, source: S.ChunkSource) -> int:
        total = 0
        for chunk in S.guard(source()):
            self._observe(chunk)
            off = 0
            while off < len(chunk):
                off += os.write(fd, chunk[off:])
            total += len(chunk)
        os.fsync(fd)
        return total

    def _write_exclusive(self, path: str, source: S.ChunkSource) -> Optional[int]:
        """Create-if-absent, streaming. `None` means the key already existed."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not self._conditional:
            return self._write_checked(path, source)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return None
        except OSError as exc:  # pragma: no cover - surfaced as transient, retried
            if exc.errno in (errno.ENOSPC, errno.EIO):
                raise BackendTransient(str(exc))
            raise
        try:
            written = self._write_stream(fd, source)
        finally:
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
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
        try:
            written = self._write_stream(fd, source)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        self.writes += 1
        self.bytes_written += written
        return written

    def _maybe_fail(self) -> None:
        if self.fail_next_put:
            raise self.fail_next_put.pop(0)

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
        self.reads += 1
        path = self._path(key)
        end = None if length is None else offset + length
        for chunk in S.file_chunks(path, chunk_bytes=chunk_bytes, start=offset, end=end)():
            yield chunk

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
                body = _doc_bytes(doc)
                if self._write_exclusive(path, S.bytes_chunks(body)) is not None:
                    self._record_generation(key, doc)
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
        if self._record_generation(key, doc) is None:
            # Someone else already claimed this generation. The loser re-reads and retries.
            return CommitResult(key=key, outcome="generation_conflict", generation=current,
                                existing_blob=existing.get("blob_key") if existing else None)
        self._replace_write(path, S.bytes_chunks(_doc_bytes(doc)))
        return CommitResult(key=key, outcome="committed", generation=generation)

    def _read_pointer(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _record_generation(self, key: str, doc: Dict[str, Any]) -> Optional[int]:
        """Claim generation N exclusively. `None` means another writer already holds it."""
        gkey = generation_key(key, int(doc.get("generation", 1)))
        gpath = self._path(gkey)
        os.makedirs(os.path.dirname(gpath), exist_ok=True)
        try:
            fd = os.open(gpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return None
        try:
            written = self._write_stream(fd, S.bytes_chunks(_doc_bytes(doc)))
        finally:
            os.close(fd)
        self.writes += 1
        self.bytes_written += written
        return written

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
        cur = self._current_lease(lease.name)
        if cur is None:
            return False
        return int(cur["epoch"]) == lease.epoch and float(cur["expires_utc_s"]) > self._now()

    def acquire_lease(self, name: str, ttl_s: float, holder: str) -> Optional[Lease]:
        """Claim the next epoch with an exclusive create. Two racers, one winner, no shared epoch."""
        cur = self._current_lease(name)
        now = self._now()
        if cur is not None and float(cur["expires_utc_s"]) > now:
            if cur["holder"] != holder:
                return None
            return self._write_epoch(name, holder, int(cur["epoch"]), now + ttl_s, exclusive=False)
        epoch = int(cur["epoch"]) + 1 if cur else 1
        return self._write_epoch(name, holder, epoch, now + ttl_s, exclusive=True)

    def renew_lease(self, lease: Lease, ttl_s: float) -> Optional[Lease]:
        if not self._lease_is_live(lease):
            return None
        return self._write_epoch(lease.name, lease.holder, lease.epoch, self._now() + ttl_s,
                                 exclusive=False)

    def release_lease(self, lease: Lease) -> None:
        if self._lease_is_live(lease):
            self._write_epoch(lease.name, lease.holder, lease.epoch, 0.0, exclusive=False)

    def _write_epoch(self, name: str, holder: str, epoch: int, expires: float, *,
                     exclusive: bool) -> Optional[Lease]:
        path = self._epoch_path(name, epoch)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"name": name, "holder": holder, "epoch": epoch, "expires_utc_s": expires}
        source = S.bytes_chunks(json.dumps(doc, sort_keys=True).encode("utf-8"))
        if exclusive:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return None  # another acquirer claimed this epoch first
            try:
                self._write_stream(fd, source)
            finally:
                os.close(fd)
        else:
            self._replace_write(path, source)
        return Lease(name=name, holder=holder, epoch=epoch, expires_utc_s=expires)

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


def _doc_bytes(doc: Dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
