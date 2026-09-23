"""Chunked reads, digests computed in flight, and the rule that no body is ever held whole.

⚠️THE LARGEST OBJECT IN SCOPE IS 392 MB (the perch model file, import plan §10.2), and the pods
that would run an import are already near their memory limit. `bytes` is therefore not an allowed
shape for a payload anywhere in this package: a source is a *factory of chunk iterators*, a digest
is accumulated while the chunks go past, and the only thing that survives a stream is a
`StreamStat` -- a digest and a byte count, 96 bytes regardless of the object.

⚠️A SOURCE IS A FACTORY, NOT AN ITERATOR. The publish path reads the same bytes more than once
(stage, read back, copy to the blob) and an iterator can only be walked once. Handing the pipeline
an iterator would force it to buffer to replay, which is exactly the thing this module exists to
prevent, so it is handed a callable that opens a fresh read instead.

⚠️THE ONLY MODE STRING HERE IS `"rb"`. This module reads sources and never repairs, truncates,
renames or touches one; `stage.census()` proves it over a whole tree.

NOT PRODUCTION: no backend is selected and no gate (G0-G6) is satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from typing import Callable, Iterable, Iterator, Optional

#: One MiB. Small enough that a 392 MB object costs ~1 MiB of resident payload, large enough that
#: the per-chunk overhead is irrelevant. Tests override it downwards to cross boundaries cheaply.
DEFAULT_CHUNK_BYTES = 1 << 20

#: Nothing in this package may materialise a payload larger than this in one object. It is a
#: tripwire, not a tuning knob: if a code path grows a `body` again, a test sees it here.
MAX_RESIDENT_CHUNK_BYTES = 8 << 20

#: A chunk source: call it, get a fresh iterator over the same bytes.
ChunkSource = Callable[[], Iterator[bytes]]


class ChunkTooLarge(Exception):
    """A producer handed out a chunk bigger than the resident cap. That is the buffering bug."""


@dataclass(frozen=True)
class StreamStat:
    """Everything that is allowed to outlive a stream."""

    digest: str
    bytes: int


def file_chunks(path: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                start: int = 0, end: Optional[int] = None) -> ChunkSource:
    """A read-only chunk source over `[start, end)` of a file. Never opened for writing."""

    def _open() -> Iterator[bytes]:
        with open(path, "rb") as fh:  # "rb" is the only mode a source is ever opened with
            fh.seek(start)
            remaining = None if end is None else max(0, end - start)
            while True:
                want = chunk_bytes if remaining is None else min(chunk_bytes, remaining)
                if want <= 0:
                    return
                buf = fh.read(want)
                if not buf:
                    return
                if remaining is not None:
                    remaining -= len(buf)
                yield buf

    return _open


def bytes_chunks(body: bytes, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ChunkSource:
    """A chunk source over an in-memory body -- only for things that are *already* small.

    Metadata envelopes, quarantine records and pointer documents are the intended callers. A
    payload does not come through here; `file_chunks` or a generator does.
    """

    def _open() -> Iterator[bytes]:
        for off in range(0, len(body), chunk_bytes) or [0]:
            yield body[off:off + chunk_bytes]

    return _open


def generated_chunks(make: Callable[[], Iterator[bytes]]) -> ChunkSource:
    """A source whose bytes are produced on demand (a synthetic fixture, a re-encode, a segment)."""
    return make


def guard(chunks: Iterable[bytes], *, cap: int = MAX_RESIDENT_CHUNK_BYTES) -> Iterator[bytes]:
    """Pass chunks through, refusing any single chunk that is a buffered body in disguise."""
    for chunk in chunks:
        if len(chunk) > cap:
            raise ChunkTooLarge("a %d-byte chunk exceeds the %d-byte resident cap" % (len(chunk), cap))
        yield chunk


class DigestingChunks:
    """Wrap a chunk iterator; accumulate sha256 and a byte count as the chunks go past.

    The digest is only readable after the stream is exhausted, because a half-read stream has no
    digest and returning one anyway is how a truncated object gets published under a name that
    says it is complete.
    """

    def __init__(self, chunks: Iterable[bytes], *, cap: int = MAX_RESIDENT_CHUNK_BYTES) -> None:
        self._chunks = guard(chunks, cap=cap)
        self._hash = hashlib.sha256()
        self._bytes = 0
        self._done = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._hash.update(chunk)
            self._bytes += len(chunk)
            yield chunk
        self._done = True

    @property
    def complete(self) -> bool:
        return self._done

    def stat(self) -> StreamStat:
        if not self._done:
            raise RuntimeError("a digest of a half-read stream is not a digest of anything")
        return StreamStat(digest=self._hash.hexdigest(), bytes=self._bytes)


def digest_source(source: ChunkSource, *, cap: int = MAX_RESIDENT_CHUNK_BYTES) -> StreamStat:
    """Walk a source once, keep nothing but the digest and the length."""
    d = DigestingChunks(source(), cap=cap)
    for _ in d:
        pass
    return d.stat()


def drain(chunks: Iterable[bytes]) -> int:
    """Consume a stream for its side effects, returning the byte count and keeping no bytes."""
    total = 0
    for chunk in chunks:
        total += len(chunk)
    return total


def sink_to_path(chunks: Iterable[bytes], fd: int) -> int:
    """Write a stream to an already-open descriptor, one chunk at a time."""
    total = 0
    for chunk in chunks:
        off = 0
        while off < len(chunk):
            off += os.write(fd, chunk[off:])
        total += len(chunk)
    return total
