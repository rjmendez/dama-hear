"""Reading a JSONL that is being appended to, without ever tearing a row or writing to the source.

⚠️NEVER READ TO EOF. The drain appends to `ledger.jsonl`, `clips/index.jsonl`, `records/*.jsonl`
and `scene/*.jsonl.gz` every 15 minutes while the importer runs. A read that chases EOF has no
defined end, and a POSIX append is atomic only up to `PIPE_BUF`, so the last line of a live file
is expected to be torn. The rule here: capture `end = fstat(fd).st_size` on an already-open
descriptor, read `[0, end)` from *that* descriptor, then move `end` back to the last newline
inside the range. Bytes after the last newline are the next run's job and are COUNTED
(`deferred_partial_tail`), never dropped -- `hear/pool.py` already names this case
(`partial_first_line`) as one that has silently lost rows before.

⚠️A ROTATED OR TRUNCATED SOURCE IS DEFERRED, NOT STITCHED. `(st_dev, st_ino)` recorded at open is
compared against the path at the end of the read. If the path is now a different file, the segment
is discarded and retried next run; half of an old file plus half of a new one is not a segment.

⚠️A SEGMENT'S IDENTITY IS ITS RECORD SET. `stream_digest` sorts record digests, so re-ingesting an
overlapping tail -- which the drain does constantly -- does not flip a segment's identity, and a
multi-member gzip (`gzip.open(path, "at")`, as `hear/pool.py` writes scene files) is addressed by
what it says rather than by the append history of its bytes.

Nothing in this module opens a source file for writing. The only mode string here is `"rb"`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import keys as K

#: Segment seal caps from the import plan. Small enough here that a test can cross them.
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_SEGMENT_ROWS = 200_000


class SourceRotated(Exception):
    """The path stopped being the file we opened. Defer; never stitch two files into one segment."""


@dataclass(frozen=True)
class FrozenRange:
    path: str
    start: int
    end: int
    dev: int
    ino: int
    #: bytes after the last newline inside the frozen window -- a torn tail, deferred not dropped
    deferred_partial_tail: int
    body: bytes = field(repr=False, default=b"")


@dataclass(frozen=True)
class Segment:
    object_class: str
    partition: Tuple[str, ...]
    seq: int
    byte_range: Tuple[int, int]
    row_count: int
    record_digests: Tuple[str, ...]
    stream_digest: str
    body: bytes = field(repr=False, default=b"")
    sealed: bool = True

    @property
    def logical_id(self) -> str:
        return str(self.seq)


def freeze_range(path: str, start: int = 0) -> FrozenRange:
    """Open read-only, freeze the end, trim to the last newline, prove the file did not rotate."""
    fd = os.open(path, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        end = st.st_size
        os.lseek(fd, start, os.SEEK_SET)
        want = max(0, end - start)
        chunks: List[bytes] = []
        got = 0
        while got < want:
            buf = os.read(fd, min(1 << 20, want - got))
            if not buf:
                break
            chunks.append(buf)
            got += len(buf)
        body = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        now = os.stat(path)
    except FileNotFoundError:
        raise SourceRotated(path)
    if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino) or now.st_size < end:
        raise SourceRotated(path)
    cut = body.rfind(b"\n")
    tail = len(body) - (cut + 1) if cut >= 0 else len(body)
    body = body[: cut + 1] if cut >= 0 else b""
    return FrozenRange(path=path, start=start, end=start + len(body), dev=st.st_dev, ino=st.st_ino,
                       deferred_partial_tail=tail, body=body)


def freeze_gzip_range(path: str, start: int = 0) -> FrozenRange:
    """A multi-member `.jsonl.gz`: freeze the compressed bytes, decode members the way the pool does.

    `hear/pool.py` reads scene files with `gzip.open(..., "rt")`, which walks every member, so that
    is what happens here. The frozen compressed bytes are kept as the blob body so the original
    file remains reproducible; identity is still the record set.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        end = st.st_size
        os.lseek(fd, start, os.SEEK_SET)
        body = os.read(fd, max(0, end - start))
    finally:
        os.close(fd)
    now = os.stat(path)
    if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino) or now.st_size < end:
        raise SourceRotated(path)
    return FrozenRange(path=path, start=start, end=start + len(body), dev=st.st_dev, ino=st.st_ino,
                       deferred_partial_tail=0, body=body)


def rows_of(frozen: FrozenRange, *, gzipped: bool = False) -> List[Dict[str, Any]]:
    raw = gzip.decompress(frozen.body) if gzipped else frozen.body
    out: List[Dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        out.append(json.loads(line.decode("utf-8")))
    return out


def segment(frozen: FrozenRange, object_class: str, partition: Iterable[str], seq: int, *,
            gzipped: bool = False, sealed: bool = True) -> Segment:
    """One sealed segment over a frozen range. Never contains a partial row, by construction."""
    rows = rows_of(frozen, gzipped=gzipped)
    digests = tuple(K.record_digest(r) for r in rows)
    return Segment(object_class=object_class, partition=tuple(partition), seq=seq,
                   byte_range=(frozen.start, frozen.end), row_count=len(rows),
                   record_digests=digests, stream_digest=K.stream_digest(digests),
                   body=frozen.body, sealed=sealed)


def row_index(seg: Segment) -> Dict[str, Tuple[str, int]]:
    """`row digest -> (segment logical id, ordinal)`; derived, rebuildable, never authority."""
    return {d: (seg.logical_id, i) for i, d in enumerate(seg.record_digests)}
