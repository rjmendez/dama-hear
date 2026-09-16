"""Reading an append-only JSONL that is still being written, without tearing a row or a file.

The drain appends to these files every fifteen minutes. Everything here is about the boundary
between "what this run may claim" and "what belongs to the next run", which is the only place a
stream import can silently lose or duplicate rows.
"""
from __future__ import annotations

import gzip
import json
import os

import pytest

from hear.objectstore import keys as K
from hear.objectstore import streams as S
from tests import objectstore_mini_pool as MINI


@pytest.fixture()
def pool(tmp_path):
    return MINI.build(str(tmp_path / "pool"))


def test_a_segment_never_contains_a_partial_row(pool):
    frozen = S.freeze_range(pool["records_path"])
    seg = S.segment(frozen, "record-seg", ("2026-09-12", "node"), 1)
    assert seg.row_count == len(pool["records_rows"])
    assert frozen.body.endswith(b"\n")
    for row in S.rows_of(frozen):
        assert set(row) == set(pool["records_rows"][0])


def test_the_bytes_after_the_last_newline_are_counted_not_dropped(pool):
    frozen = S.freeze_range(pool["records_path"])
    assert frozen.deferred_partial_tail == pool["records_torn_tail"]
    assert frozen.end == os.path.getsize(pool["records_path"]) - frozen.deferred_partial_tail


def test_an_append_after_the_freeze_is_invisible_to_this_run(pool):
    frozen = S.freeze_range(pool["records_path"])
    with open(pool["records_path"], "ab") as fh:
        fh.write(json.dumps({"key": "late"}, sort_keys=True).encode("utf-8") + b"\n")
    assert "late" not in frozen.body.decode("utf-8")
    # ...and the next run picks it up from where this one stopped, with no overlap and no gap.
    nxt = S.freeze_range(pool["records_path"], start=frozen.end)
    assert b"late" in nxt.body


def test_the_deferred_tail_is_completed_by_the_next_run_and_not_duplicated(pool):
    frozen = S.freeze_range(pool["records_path"])
    with open(pool["records_path"], "ab") as fh:
        fh.write(pool["records_torn_remainder"])  # the rest of the torn row finally lands
    nxt = S.freeze_range(pool["records_path"], start=frozen.end)
    first = S.segment(frozen, "record-seg", ("2026-09-12", "node"), 1)
    second = S.segment(nxt, "record-seg", ("2026-09-12", "node"), 2)
    assert second.row_count == 1
    assert not set(first.record_digests) & set(second.record_digests)


def test_a_stream_digest_is_unchanged_by_reappending_the_same_rows(pool):
    frozen = S.freeze_range(pool["records_path"])
    seg = S.segment(frozen, "record-seg", ("2026-09-12", "node"), 1)
    shuffled = list(reversed(pool["records_rows"]))
    body = b"".join(json.dumps(r, sort_keys=True).encode("utf-8") + b"\n" for r in shuffled)
    other = S.FrozenRange(path="memory", start=0, end=len(body), dev=0, ino=0,
                          deferred_partial_tail=0, body=body)
    assert S.segment(other, "record-seg", ("2026-09-12", "node"), 1).stream_digest == seg.stream_digest


def test_a_multi_member_gzip_scene_hashes_to_its_record_set_not_its_bytes(pool):
    frozen = S.freeze_gzip_range(pool["scene_path"])
    seg = S.segment(frozen, "scene-seg", ("2026-09-12", "mach"), 1, gzipped=True)
    assert seg.row_count == len(pool["scene_rows"])
    assert seg.stream_digest != K.sha256_hex(frozen.body)

    # The same rows written as ONE member: different bytes, same identity.
    single = gzip.compress(b"".join(
        json.dumps(r, sort_keys=True).encode("utf-8") + b"\n" for r in pool["scene_rows"]))
    other = S.FrozenRange(path="memory", start=0, end=len(single), dev=0, ino=0,
                          deferred_partial_tail=0, body=single)
    assert single != frozen.body
    assert S.segment(other, "scene-seg", ("2026-09-12", "mach"), 1, gzipped=True).stream_digest \
        == seg.stream_digest


def test_a_rotated_source_file_is_deferred_not_torn(pool, monkeypatch):
    path = pool["records_path"]
    real_stat = os.stat

    def rotated(p, *a, **kw):
        st = real_stat(p, *a, **kw)
        if str(p) == path:
            class Fake:
                st_dev, st_ino, st_size = st.st_dev, st.st_ino + 1, st.st_size
            return Fake()
        return st

    monkeypatch.setattr(os, "stat", rotated)
    with pytest.raises(S.SourceRotated):
        S.freeze_range(path)


def test_a_truncated_source_file_is_deferred_not_torn(tmp_path):
    path = tmp_path / "live.jsonl"
    path.write_bytes(b'{"key":"a"}\n{"key":"b"}\n')
    real_stat = os.stat

    def shrunk(p, *a, **kw):
        st = real_stat(p, *a, **kw)
        if str(p) == str(path):
            class Fake:
                st_dev, st_ino, st_size = st.st_dev, st.st_ino, 1
            return Fake()
        return st

    import unittest.mock as mock
    with mock.patch.object(os, "stat", shrunk):
        with pytest.raises(S.SourceRotated):
            S.freeze_range(str(path))


def test_the_row_index_rebuilds_from_the_segment_alone(pool):
    frozen = S.freeze_range(pool["records_path"])
    seg = S.segment(frozen, "record-seg", ("2026-09-12", "node"), 1)
    index = S.row_index(seg)
    assert len(index) == seg.row_count
    rebuilt = S.row_index(S.segment(frozen, "record-seg", ("2026-09-12", "node"), 1))
    assert rebuilt == index
    for i, row in enumerate(S.rows_of(frozen)):
        assert index[K.record_digest(row)] == (seg.logical_id, i)


def test_reading_a_stream_does_not_modify_the_source(pool):
    from hear.objectstore.stage import census

    before = census(pool["root"])
    S.segment(S.freeze_range(pool["records_path"]), "record-seg", ("2026-09-12", "node"), 1)
    S.segment(S.freeze_gzip_range(pool["scene_path"]), "scene-seg", ("2026-09-12", "mach"), 1,
              gzipped=True)
    assert census(pool["root"]) == before
