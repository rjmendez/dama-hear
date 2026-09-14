"""health.csv is archived from the bytes it gained, not re-fetched whole every run (#99).

No HTTP: /status, /sd and /ls are served by a fake, as in tests/test_hear_drain.py.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                          # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402
from tests.test_hear_drain import FakeNode                          # noqa: E402

HEADER = "node,utc_us,uptime_s,fix,sats,pps,note"


class HealthNode(FakeNode):
    """A card node whose health.csv grows one row per 30 s, rows of uneven length."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.health = (HEADER + "\n").encode()
        self.health_rows = []
        self.fail_health = False

    def grow_health(self, k):
        for _ in range(k):
            i = len(self.health_rows)
            row = "%s,%d,%d,3,%d,%d,%s" % (self.node, self.boot_epoch_us + i * 30_000_000,
                                          i * 30, 5 + i % 17, i * 30, "x" * (40 + (i * 37) % 300))
            self.health_rows.append(row)
            self.health += (row + "\n").encode()
        return self

    def roll_health(self, header=HEADER):
        self.health = (header + "\n").encode()
        return self

    def sd(self, ip, name, timeout=None, tail=None):
        if name != "health.csv":
            return super().sd(ip, name, timeout=timeout, tail=tail)
        self.sd_calls.append((name, tail))
        if self.fail_health:
            raise OSError("connection reset")
        body = self.health
        if tail and len(body) > tail:
            body = body[len(body) - tail:]
        return body

    def ls(self, ip, timeout=None):
        out = super().ls(ip, timeout)
        out["health.csv"] = len(self.health)
        return out


@pytest.fixture
def node(monkeypatch):
    n = HealthNode()
    n.grow(20)
    monkeypatch.setattr(HD, "fetch_status", n.status)
    monkeypatch.setattr(HD, "fetch_sd", n.sd)
    monkeypatch.setattr(HD, "_ls_sizes", n.ls)
    return n


def _health_calls(n):
    return [tail for name, tail in n.sd_calls if name == "health.csv"]


def _entry(r):
    return [f for f in r["files"] if f["name"] == "health.csv"]


def _archived_rows(pl):
    rows = set()
    d = os.path.join(pl.root, "raw", "nyquist")
    for f in sorted(os.listdir(d)):
        if not f.endswith("-health.csv"):
            continue
        lines = open(os.path.join(d, f)).read().splitlines()
        assert lines[0] == HEADER, "%s is not a CSV on its own" % f
        for line in lines[1:]:
            assert line.count(",") == HEADER.count(","), "%s holds a partial row: %r" % (f, line)
            rows.add(line)
    return rows


def _drain(pl, n, stamp):
    return HD.drain_node(pl, n.node, "10.0.0.1", stamp=stamp)


def test_the_first_run_fetches_whole_and_keeps_the_header(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(50)
    r = _drain(pl, node, 1000)
    assert _health_calls(node) == [None]
    assert open(_entry(r)[0]["archived"], "rb").read() == node.health
    mark = HD.read_watermarks(pl.root)["nyquist"]["health.csv"]
    assert mark["size"] == len(node.health) and mark["header"] == HEADER


def test_a_later_run_asks_only_for_what_the_file_gained(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(3000)
    _drain(pl, node, 1000)
    before = len(node.health)
    node.grow_health(30)
    r = _drain(pl, node, 2000)
    tail = _health_calls(node)[-1]
    assert tail == len(node.health) - before + HD.CONTEXT_OVERLAP_BYTES
    assert _entry(r)[0]["bytes"] == tail < len(node.health) // 20


def test_every_row_on_the_card_reaches_an_archive(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    for stamp in range(1000, 13000, 1000):
        node.grow_health(1 + stamp % 7 * 5)
        _drain(pl, node, stamp)
    assert _archived_rows(pl) == set(node.health_rows)


def test_an_unchanged_file_is_not_fetched(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(10)
    _drain(pl, node, 1000)
    r = _drain(pl, node, 2000)
    assert _health_calls(node) == [None]
    assert _entry(r)[0]["skipped_unchanged"] is True


def test_a_rolled_file_is_fetched_whole(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(100)
    _drain(pl, node, 1000)
    node.roll_health().grow_health(3)
    r = _drain(pl, node, 2000)
    assert _health_calls(node)[-1] is None and _entry(r)[0]["rolled"] is True
    assert HD.read_watermarks(pl.root)["nyquist"]["health.csv"]["size"] == len(node.health)


def test_a_tail_answered_with_the_whole_file_keeps_that_files_own_header(tmp_path, node,
                                                                        monkeypatch):
    """/ls saw the old file grow, then it rolled with a new schema before /sd was served: the tail
    covers the whole new file, whose first line is the new header, not a fragment."""
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(100)
    _drain(pl, node, 1000)
    stale = len(node.health) + 500
    monkeypatch.setattr(HD, "_ls_sizes", lambda ip, timeout=None: {**node.ls(ip), "health.csv": stale})
    new_header = HEADER + ",rssi"
    node.roll_health(new_header)
    node.health += b"nyquist,1,2,3,4,5,x,-60\n"
    r = _drain(pl, node, 2000)
    assert _health_calls(node)[-1] == 500 + HD.CONTEXT_OVERLAP_BYTES
    e = _entry(r)[0]
    assert e["whole_response"] is True
    assert open(e["archived"], "rb").read() == node.health
    mark = HD.read_watermarks(pl.root)["nyquist"]["health.csv"]
    assert mark["header"] == new_header
    assert mark["size"] == len(node.health) == e["size"], "the mark must be the file as served"

    # The new file grows past the size /ls reported for the old one: no byte may be skipped.
    monkeypatch.setattr(HD, "_ls_sizes", node.ls)
    served = len(node.health)
    while len(node.health) <= stale:
        node.grow_health(10)
    r = _drain(pl, node, 3000)
    assert _health_calls(node)[-1] == len(node.health) - served + HD.CONTEXT_OVERLAP_BYTES
    assert node.health[served:] in open(_entry(r)[0]["archived"], "rb").read()


def test_a_blind_listing_takes_a_bounded_tail_and_keeps_the_mark(tmp_path, node, monkeypatch):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(10)
    _drain(pl, node, 1000)
    mark = HD.read_watermarks(pl.root)["nyquist"]["health.csv"]

    def no_ls(ip, timeout=None):
        raise ConnectionResetError(104, "Connection reset by peer")
    monkeypatch.setattr(HD, "_ls_sizes", no_ls)
    monkeypatch.setattr(HD.time, "sleep", lambda s: None)
    node.grow_health(400)
    _drain(pl, node, 2000)
    assert _health_calls(node)[-1] == HD.CONTEXT_BLIND_TAIL_BYTES
    assert HD.read_watermarks(pl.root)["nyquist"]["health.csv"] == mark

    monkeypatch.setattr(HD, "_ls_sizes", node.ls)
    _drain(pl, node, 3000)
    assert _archived_rows(pl) == set(node.health_rows)


def test_a_blind_tail_that_returns_the_whole_file_marks_the_served_length(tmp_path, node,
                                                                         monkeypatch):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(500)
    _drain(pl, node, 1000)

    def no_ls(ip, timeout=None):
        raise ConnectionResetError(104, "Connection reset by peer")
    monkeypatch.setattr(HD, "_ls_sizes", no_ls)
    monkeypatch.setattr(HD.time, "sleep", lambda s: None)
    node.roll_health().grow_health(3)
    assert len(node.health) < HD.CONTEXT_BLIND_TAIL_BYTES
    r = _drain(pl, node, 2000)
    e = _entry(r)[0]
    assert e["whole_response"] is True
    assert HD.read_watermarks(pl.root)["nyquist"]["health.csv"]["size"] == len(node.health)


def test_a_blind_first_run_archives_nothing_rather_than_a_headerless_fragment(tmp_path, node,
                                                                              monkeypatch):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(10)

    def no_ls(ip, timeout=None):
        raise ConnectionResetError(104, "Connection reset by peer")
    monkeypatch.setattr(HD, "_ls_sizes", no_ls)
    monkeypatch.setattr(HD.time, "sleep", lambda s: None)
    r = _drain(pl, node, 1000)
    assert _health_calls(node) == [] and _entry(r)[0]["skipped_unmeasured"] is True
    assert "health.csv" not in HD.read_watermarks(pl.root).get("nyquist", {})

    monkeypatch.setattr(HD, "_ls_sizes", node.ls)
    node.grow_health(5)
    _drain(pl, node, 2000)
    assert _health_calls(node) == [None]
    assert _archived_rows(pl) == set(node.health_rows)


def test_a_failed_fetch_keeps_the_mark(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(10)
    _drain(pl, node, 1000)
    mark = HD.read_watermarks(pl.root)["nyquist"]["health.csv"]
    node.grow_health(20)
    node.fail_health = True
    r = _drain(pl, node, 2000)
    assert any(e.startswith("health.csv:") for e in r["errors"])
    assert HD.read_watermarks(pl.root)["nyquist"]["health.csv"] == mark
    node.fail_health = False
    _drain(pl, node, 3000)
    assert _archived_rows(pl) == set(node.health_rows)


def test_a_mark_without_a_header_is_refetched_whole(tmp_path, node):
    pl = P.Pool(str(tmp_path / "pool"))
    node.grow_health(10)
    HD.write_watermarks(pl.root, {"nyquist": {"health.csv": {"size": 5, "at": 1}}})
    _drain(pl, node, 1000)
    assert _health_calls(node) == [None]
