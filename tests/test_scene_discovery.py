"""The scene lane died silently once; these are the cases that would have caught it.

Firmware moved from scene.csv to scene-YYYYMMDD.csv (commit 4dbfe26) and the drain kept asking
for the old names, fetching a frozen file and ingesting nothing while reporting `ok`. A test that
only checks "the drain fetched something" cannot see that, so these check WHICH names it asks for.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                          # noqa: E402
from hear import scenefile as SF                                    # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402
from tools.hear_drain import scene_names, SCENE_FILES               # noqa: E402
from tests.test_hear_drain import _scene_row                        # noqa: E402


def test_the_dated_file_is_found_and_the_newest_one_is_the_live_one():
    ls = {"scene.csv": 20_751_993, "scene-00000000.csv": 4_096,
          "scene-20260908.csv": 9_000_000, "scene-20260909.csv": 2_003_040, "dets.csv": 10}
    names, live = scene_names(ls)
    assert live == "scene-20260909.csv", "the newest dated file is the one still growing"
    assert set(names) == {"scene.csv", "scene-00000000.csv",
                          "scene-20260909.csv", "scene-20260908.csv"}


def test_the_node_that_serves_no_legacy_file_is_still_drained():
    # rankine, measured 2026-09-09: no scene.csv at all. The old code asked for it, got a 404,
    # recorded "scene.csv absent" and drained no scene rows from that node ever.
    names, live = scene_names({"scene-00000000.csv": 4096, "scene-20260909.csv": 1_605_608})
    assert live == "scene-20260909.csv"
    assert "scene-20260909.csv" in names


def test_the_prelock_file_is_fetched_not_skipped():
    # scene-00000000.csv holds rows written before the node knew the date. They are real rows;
    # pool.py keeps them and marks them unanchored. Skipping them here would silently drop
    # every boot's first minutes.
    names, _ = scene_names({"scene-00000000.csv": 4096, "scene-20260909.csv": 10})
    assert "scene-00000000.csv" in names


def test_a_blind_run_still_fetches_something():
    # /ls failed. A drain that then asks for nothing turns a blind spot into a data outage.
    assert scene_names(None) == (SCENE_FILES, "scene.csv")
    assert scene_names({}) == (SCENE_FILES, "scene.csv")


def test_old_firmware_that_only_has_the_legacy_names_still_works():
    names, live = scene_names({"scene.csv": 100, "scene-prev.csv": 50})
    assert live == "scene.csv", "with no dated file the legacy file is still the live one"
    assert set(names) == {"scene.csv", "scene-prev.csv"}


def test_prev_is_never_mistaken_for_the_live_file():
    names, live = scene_names({"scene.csv": 100, "scene-prev.csv": 50, "scene-20260909.csv": 7})
    assert live == "scene-20260909.csv"
    assert "scene-prev.csv" in names


# ---------------------------------------------------------------- the refetch the discovery cost

class MultiFileNode:
    """A node serving several scene files at once, which is what discovery made possible.

    `FakeNode` in test_hear_drain.py serves exactly one file, so it cannot see what happens to the
    OTHER names -- and the other names are where the cost is. Every fetch is recorded so a test can
    assert on transfer, not just on rows.
    """

    def __init__(self, files, node="nyquist"):
        self.node = node
        self.files = dict(files)                 # name -> bytes
        self.sd_calls = []                       # (name, tail)
        self.bytes_served = 0

    def status(self, ip, timeout=None):
        return {"node": self.node, "uptime_s": 500,
                "scene": {"rows": 0, "written": 0, "short_blocks": 0, "write_fail": 0},
                "acq": {"fs_clean_hz": 16000.0, "win_s": 300, "drop_s": 0, "drop_samples": 0}}

    def sd(self, ip, name, timeout=None, tail=None):
        self.sd_calls.append((name, tail))
        body = self.files.get(name)
        if body is None:
            return None
        if tail and len(body) > tail:
            body = body[len(body) - tail:]
        self.bytes_served += len(body)
        if tail:
            return body if b"\n" in body else None
        return body

    def ls(self, ip, timeout=None):
        return {n: len(b) for n, b in self.files.items()}


def _scene_bytes(rows, node="nyquist", start_uptime=1):
    """A scene.csv body with `rows` rows, using the same generator the drain's tests use."""
    head = ",".join(SF.S2.declared) + "\n"
    out = []
    for i in range(rows):
        up = start_uptime + i
        out.append(_scene_row(SF.S2, 1788813341000000 + up * 1_000_000, up, up * 16384,
                              node=node, seed=i + 1))
    return (head + ("\n".join(out) + "\n" if out else "")).encode()


@pytest.fixture
def multi(monkeypatch):
    def build(files, node="nyquist"):
        n = MultiFileNode(files, node=node)
        monkeypatch.setattr(HD, "fetch_status", n.status)
        monkeypatch.setattr(HD, "fetch_sd", n.sd)
        monkeypatch.setattr(HD, "_ls_sizes", n.ls)
        return n
    return build


def _fetched_whole(n, name):
    return [c for c in n.sd_calls if c[0] == name and not c[1]]


def test_the_frozen_legacy_file_is_pulled_once_not_every_run(tmp_path, multi):
    """⚠️THE REGRESSION DISCOVERY WOULD HAVE SHIPPED.

    Before discovery, scene.csv was the LIVE name and was tailed. After it, a node with a dated
    file makes scene.csv a ROLLED name -- and rolled names are fetched WHOLE. nyquist's is
    20,751,993 B and the node serves 40-135 KB/s, so pulling it whole every 15 min would spend
    most of the CronJob's interval re-reading rows the pool already has, with the node deaf for
    2-7 minutes of it. The content-addressed ingest makes the repeat free in STORAGE only.
    """
    n = multi({"scene.csv": _scene_bytes(40), "scene-20260909.csv": _scene_bytes(5, start_uptime=900)})
    pl = P.Pool(str(tmp_path / "pool"))
    for _ in range(4):
        HD.drain_node(pl, n.node, "10.0.0.1")
    assert len(_fetched_whole(n, "scene.csv")) == 1, \
        "the frozen legacy file was re-downloaded whole on a later run"
    assert len([c for c in n.sd_calls if c[0] == "scene-20260909.csv"]) == 4, \
        "the live file must still be fetched every run"


def test_a_skip_is_reported_not_silent(tmp_path, multi):
    # A drain that quietly does nothing is the failure this module exists to catch, so the skip
    # appears in the run record with the size it was skipped at.
    n = multi({"scene.csv": _scene_bytes(10), "scene-20260909.csv": _scene_bytes(3, start_uptime=900)})
    pl = P.Pool(str(tmp_path / "pool"))
    HD.drain_node(pl, n.node, "10.0.0.1")
    out = HD.drain_node(pl, n.node, "10.0.0.1")
    skipped = [f for f in out["files"] if f.get("skipped_unchanged")]
    assert [f["name"] for f in skipped] == ["scene.csv"]
    assert skipped[0]["size"] == len(n.files["scene.csv"])


def test_a_rolled_file_that_grows_again_is_refetched(tmp_path, multi):
    # The skip is licensed by a SIZE, not by having seen the name. If the node appends to a file
    # the drain considers rolled, the new bytes must still land.
    n = multi({"scene.csv": _scene_bytes(10), "scene-20260909.csv": _scene_bytes(3, start_uptime=900)})
    pl = P.Pool(str(tmp_path / "pool"))
    HD.drain_node(pl, n.node, "10.0.0.1")
    n.files["scene.csv"] += _scene_bytes(4, start_uptime=500).split(b"\n", 1)[1]
    HD.drain_node(pl, n.node, "10.0.0.1")
    assert len(_fetched_whole(n, "scene.csv")) == 2, "a file that grew again was skipped"


def test_yesterdays_tailed_file_is_not_skipped_on_its_tail_mark(tmp_path, multi):
    """⚠️THE TRAP IN THE FIX ITSELF.

    Yesterday's dated file was the LIVE file, so its watermark is a size written by a TAIL -- a
    fetch that reached back SCENE_TAIL_BYTES, not to byte 0. When the date rolls it becomes a
    rolled name, and skipping it because "the size matches the mark" would strand every row
    before that tail window permanently. Only a mark written by a whole-file fetch may skip.
    """
    big = _scene_bytes(400)
    n = multi({"scene-20260909.csv": big})
    pl = P.Pool(str(tmp_path / "pool"))
    HD.SCENE_TAIL_BYTES, old = 2000, HD.SCENE_TAIL_BYTES
    try:
        HD.drain_node(pl, n.node, "10.0.0.1")           # tailed: reaches back 2000 B of a big file
        assert len(big) > 2000, "fixture must be bigger than the tail for this to mean anything"
        n.files["scene-20260910.csv"] = _scene_bytes(2, start_uptime=5000)
        HD.drain_node(pl, n.node, "10.0.0.1")           # now rolled
    finally:
        HD.SCENE_TAIL_BYTES = old
    assert len(_fetched_whole(n, "scene-20260909.csv")) == 1, \
        "the rolled file was skipped on a mark that a tail wrote, stranding its early rows"
