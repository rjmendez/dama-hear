"""The import streams. A 392 MB object must cost megabytes of memory, not hundreds of them.

⚠️THE CLAIM UNDER TEST IS "NO PAYLOAD IS EVER HELD WHOLE", and the only honest way to test it is
to import an object far larger than the memory the test allows itself. `tracemalloc` measures the
peak Python allocation across the whole import, and the backend records the largest single buffer
it was ever handed; a `body = fh.read()` anywhere in the pipeline fails both at once.

The object here is synthetic and generated from a counter, so it is deterministic, it never
touches `/pool`, and it is the same on every machine. `HEAR_OBJECTSTORE_BIG_IMPORT=1` raises the
size to 256 MiB for a manual run closer to the real perch model; CI runs the 64 MiB default,
which already exceeds any plausible buffer by a factor of 64.
"""
from __future__ import annotations

import hashlib
import os
import tracemalloc

import pytest

from hear.objectstore import backend as B
from hear.objectstore import keys as K
from hear.objectstore import stage as ST
from hear.objectstore import streaming as S

RUN = "2026-09-16T0000Z-stream"
CHUNK = 64 * 1024
BIG_BYTES = (256 if os.environ.get("HEAR_OBJECTSTORE_BIG_IMPORT") == "1" else 64) * 1024 * 1024
#: Generously above the pipeline's real footprint and far below the object. A buffered body is a
#: factor of a thousand over this; a chunk-at-a-time pipeline is well under it.
PEAK_ALLOWANCE = 8 * 1024 * 1024


def synthetic_chunks(total: int, *, chunk: int = CHUNK, seed: int = 0) -> S.ChunkSource:
    """Deterministic bytes produced on demand -- never a `bytes` of length `total`."""

    def _open():
        made = 0
        counter = seed
        while made < total:
            n = min(chunk, total - made)
            block = hashlib.sha256(b"%d/%d" % (seed, counter)).digest() * ((n // 32) + 1)
            counter += 1
            made += n
            yield block[:n]

    return _open


def expected_digest(total: int, **kw) -> str:
    return S.digest_source(synthetic_chunks(total, **kw)).digest


@pytest.fixture()
def store(tmp_path):
    return B.LocalDirBackend(str(tmp_path / "store"))


def _importer(store, tmp_path, run=RUN):
    return ST.Importer(store, str(tmp_path / "work" / run / "ledger.jsonl"), run,
                       git_commit="0000000", chunk_bytes=CHUNK)


def test_an_object_far_larger_than_memory_is_imported_without_being_buffered(store, tmp_path):
    digest = expected_digest(BIG_BYTES)
    task = ST.ImportTask(object_class="model-file", logical_id="perch-like.tflite",
                         partition=("models",), chunks=synthetic_chunks(BIG_BYTES),
                         expected_digest=digest, expected_bytes=BIG_BYTES,
                         digest_source="pinned-constant")

    tracemalloc.start()
    try:
        report = _importer(store, tmp_path).run([task])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.counters["published"] == 1
    assert report.counters["bytes_read"] == BIG_BYTES
    assert peak < PEAK_ALLOWANCE, "peak %d bytes for a %d byte object" % (peak, BIG_BYTES)
    # The backend saw nothing bigger than one chunk, on any of stage, blob copy or metadata.
    assert store.max_chunk_bytes <= CHUNK
    published = store.keys_under("hear/v1/obj/")
    assert published == [K.object_key("model-file", ("models",), "perch-like.tflite")]
    assert store.get_range(published[0]).count(b"blob_key") == 1


def test_the_staged_copy_and_the_blob_hold_the_same_bytes_the_source_had(store, tmp_path):
    size = 3 * CHUNK + 17  # deliberately not a chunk multiple
    digest = expected_digest(size, seed=7)
    task = ST.ImportTask(object_class="model-file", logical_id="odd-length.bin",
                         partition=("models",), chunks=synthetic_chunks(size, seed=7),
                         expected_digest=digest, expected_bytes=size,
                         digest_source="pinned-constant")
    report = _importer(store, tmp_path).run([task])
    assert report.counters["published"] == 1
    blob = K.blob_key(digest)
    assert S.digest_source(store.range_source(blob, chunk_bytes=CHUNK)) == S.StreamStat(digest, size)
    # Staging is cleaned up after the commit; the blob is the only copy left.
    assert store.keys_under("hear/v1/staging/") == []


def test_a_truncated_stream_never_yields_a_digest(store):
    d = S.DigestingChunks(synthetic_chunks(4 * CHUNK)())
    it = iter(d)
    next(it)
    with pytest.raises(RuntimeError):
        d.stat()  # a digest of a half-read stream is a digest of nothing


def test_a_chunk_that_is_really_a_buffered_body_is_refused():
    oversized = lambda: iter([b"x" * (S.MAX_RESIDENT_CHUNK_BYTES + 1)])
    with pytest.raises(S.ChunkTooLarge):
        S.digest_source(oversized)


def test_a_file_chunk_source_is_reopenable_and_read_only(tmp_path):
    path = tmp_path / "source.bin"
    body = bytes(range(256)) * 40
    path.write_bytes(body)
    before = ST.census(str(tmp_path))

    source = S.file_chunks(str(path), chunk_bytes=97)
    first = S.digest_source(source)
    second = S.digest_source(source)  # a source is a factory: walking it twice is free and equal
    assert first == second == S.StreamStat(hashlib.sha256(body).hexdigest(), len(body))
    assert max(len(c) for c in source()) == 97
    assert ST.census(str(tmp_path)) == before


def test_a_byte_range_source_reads_exactly_the_range(tmp_path):
    path = tmp_path / "ranged.bin"
    body = bytes(range(256)) * 10
    path.write_bytes(body)
    stat = S.digest_source(S.file_chunks(str(path), chunk_bytes=13, start=100, end=900))
    assert stat.bytes == 800
    assert stat.digest == hashlib.sha256(body[100:900]).hexdigest()


def test_a_large_import_is_resumable_without_re_reading_the_object(store, tmp_path):
    digest = expected_digest(BIG_BYTES)
    task = ST.ImportTask(object_class="model-file", logical_id="perch-like.tflite",
                         partition=("models",), chunks=synthetic_chunks(BIG_BYTES),
                         expected_digest=digest, expected_bytes=BIG_BYTES,
                         digest_source="pinned-constant")
    _importer(store, tmp_path).run([task])
    wrote = store.bytes_written

    again = _importer(store, tmp_path).run([task])
    assert again.counters["skipped_done"] == 1
    assert again.counters["bytes_read"] == 0
    assert store.bytes_written == wrote
