"""The whole backend surface the importer is allowed to depend on, plus a local-directory adapter.

⚠️NOT A PRODUCTION BACKEND. `LocalDirBackend` is a POSIX directory used by tests and by
`--dry-run`. No backend has been selected (import plan §20 G1); when one is, it implements this
same `Protocol` in exactly one module and no vendor type appears in any signature here -- the
repository already keeps `boto3`/AWS imports out of the standalone modules and this keeps that
true by construction.

THE TWO THINGS THE COMMIT NEEDS, AND WHY THE SECOND EXISTS. The publish step is a *conditional*
put: create-if-absent. On a POSIX directory that is `O_CREAT|O_EXCL`, which is atomic. A store
without conditional put needs the fallback primitive -- a lease with a fencing epoch recorded in
the pointer document -- and that is a design change, not a config change, so both are modelled
here and the pointer document carries `lease_epoch` either way.

⚠️THERE IS NO `delete_object`. Staging is the only deletable prefix in the importer's interface.
Deleting a published object belongs to the custodian role and the importer credential must not
have the capability, because the cheapest rung of the rollback ladder ("delete the shadow") must
be an action someone else takes deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import os
import shutil
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol


class BackendTransient(Exception):
    """A 5xx/timeout/throttle-shaped failure. Retryable; never a reason to skip an object."""


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
    #: `committed` | `replay` | `conflict` | `fenced`
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
    """Nine calls. Anything the importer cannot say with these, it does not get to do."""

    def put_staged(self, run_id: str, staging_id: str, body: bytes) -> StagedRef: ...

    def get_range(self, key: str, offset: int = 0, length: Optional[int] = None) -> bytes: ...

    def head(self, key: str) -> Optional[ObjectHead]: ...

    def put_immutable(self, key: str, body: bytes, *, if_absent: bool = True) -> PutResult: ...

    def commit_pointer(self, key: str, doc: Dict[str, Any], *,
                       lease: Optional[Lease] = None) -> CommitResult: ...

    def delete_staged(self, run_id: str, staging_id: str) -> None: ...

    def list_prefix(self, prefix: str) -> Iterator[ObjectHead]: ...

    def acquire_lease(self, name: str, ttl_s: float, holder: str) -> Optional[Lease]: ...

    def renew_lease(self, lease: Lease, ttl_s: float) -> Optional[Lease]: ...

    def release_lease(self, lease: Lease) -> None: ...


class LocalDirBackend:
    """A directory pretending to be an object store, with real conditional-put semantics.

    Durability follows the pattern the drain already uses: write a tmp file, fsync, `os.replace`
    for anything that may be overwritten; `O_CREAT|O_EXCL` for anything that may not. Nothing here
    is fast and nothing here needs to be -- its job is to make the *rules* executable.
    """

    def __init__(self, root: str, *, now=time.time) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._now = now
        #: Fault injection for the retry/quarantine tests. Each entry is consumed once.
        self.fail_next_put: List[Exception] = []
        self.reads = 0
        self.writes = 0
        self.bytes_written = 0

    # ------------------------------------------------------------------ paths

    def _path(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("bad key: %r" % (key,))
        return os.path.join(self.root, key)

    def _write_exclusive(self, path: str, body: bytes) -> bool:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError as exc:  # pragma: no cover - surfaced as transient, retried
            if exc.errno in (errno.ENOSPC, errno.EIO):
                raise BackendTransient(str(exc))
            raise
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.writes += 1
        self.bytes_written += len(body)
        return True

    def _maybe_fail(self) -> None:
        if self.fail_next_put:
            raise self.fail_next_put.pop(0)

    # --------------------------------------------------------------- staging

    def put_staged(self, run_id: str, staging_id: str, body: bytes) -> StagedRef:
        self._maybe_fail()
        key = "hear/v1/staging/%s/%s" % (run_id, staging_id)
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self.writes += 1
        self.bytes_written += len(body)
        return StagedRef(run_id=run_id, staging_id=staging_id, key=key, bytes=len(body))

    def delete_staged(self, run_id: str, staging_id: str) -> None:
        path = self._path("hear/v1/staging/%s/%s" % (run_id, staging_id))
        if os.path.exists(path):
            os.unlink(path)

    # ----------------------------------------------------------------- reads

    def get_range(self, key: str, offset: int = 0, length: Optional[int] = None) -> bytes:
        self.reads += 1
        with open(self._path(key), "rb") as fh:
            fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def head(self, key: str) -> Optional[ObjectHead]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        return ObjectHead(key=key, bytes=os.path.getsize(path))

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

    def put_immutable(self, key: str, body: bytes, *, if_absent: bool = True) -> PutResult:
        self._maybe_fail()
        path = self._path(key)
        if not if_absent:
            raise ValueError("published objects are immutable; if_absent=False is not offered")
        created = self._write_exclusive(path, body)
        return PutResult(key=key, created=created, bytes_written=len(body) if created else 0)

    def commit_pointer(self, key: str, doc: Dict[str, Any], *,
                       lease: Optional[Lease] = None) -> CommitResult:
        """The only observable step. Create-if-absent; an existing key is inspected, never clobbered."""
        self._maybe_fail()
        if lease is not None and not self._lease_is_live(lease):
            # A zombie writer that lost its lease and came back is refused here rather than
            # trusted because it still has bytes in memory.
            return CommitResult(key=key, outcome="fenced")
        body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path = self._path(key)
        if self._write_exclusive(path, body):
            return CommitResult(key=key, outcome="committed", generation=int(doc.get("generation", 1)))
        with open(path, "rb") as fh:
            existing = json.loads(fh.read().decode("utf-8"))
        if existing.get("blob_key") == doc.get("blob_key"):
            return CommitResult(key=key, outcome="replay",
                                generation=int(existing.get("generation", 1)),
                                existing_blob=existing.get("blob_key"))
        return CommitResult(key=key, outcome="conflict",
                            generation=int(existing.get("generation", 1)),
                            existing_blob=existing.get("blob_key"))

    # ---------------------------------------------------------------- leases

    def _lease_path(self, name: str) -> str:
        return self._path("hear/v1/lease/%s.json" % name.replace("/", "_"))

    def _read_lease(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._lease_path(name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _lease_is_live(self, lease: Lease) -> bool:
        cur = self._read_lease(lease.name)
        if cur is None:
            return False
        return int(cur["epoch"]) == lease.epoch and float(cur["expires_utc_s"]) > self._now()

    def acquire_lease(self, name: str, ttl_s: float, holder: str) -> Optional[Lease]:
        cur = self._read_lease(name)
        now = self._now()
        if cur is not None and float(cur["expires_utc_s"]) > now and cur["holder"] != holder:
            return None
        epoch = int(cur["epoch"]) + 1 if cur else 1
        return self._write_lease(name, holder, epoch, now + ttl_s)

    def renew_lease(self, lease: Lease, ttl_s: float) -> Optional[Lease]:
        if not self._lease_is_live(lease):
            return None
        return self._write_lease(lease.name, lease.holder, lease.epoch, self._now() + ttl_s)

    def release_lease(self, lease: Lease) -> None:
        if self._lease_is_live(lease):
            self._write_lease(lease.name, lease.holder, lease.epoch, 0.0)

    def _write_lease(self, name: str, holder: str, epoch: int, expires: float) -> Lease:
        path = self._lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"name": name, "holder": holder, "epoch": epoch, "expires_utc_s": expires}
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(json.dumps(doc, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
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
