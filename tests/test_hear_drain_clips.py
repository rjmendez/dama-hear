"""The clip lane: names out of dets.csv, WAVs off the card, and a census that totals itself.

⚠️THE PRE-CHANGE BEHAVIOUR EVERY TEST HERE FAILS AGAINST. Measured on the live fleet 2026-09-09:
625 clips written, ~478 destroyed, and NOT ONE CLIP HAD EVER LEFT A NODE. `tools/hear_drain.py`
contained zero mentions of clips, so there was no consumer for the `clip` column at all; and
`fetch_sd` requires a body to start with `b"node"` or `b"utc_us"`, which reports a present
128044 B WAV -- it starts with `b"RIFF"` -- as ABSENT. Each test below names its own case.

No HTTP is done. `ClipNode` replaces the four calls the drain makes and counts every one, which
is what makes both the sequencing and the request LIST exactly checkable: the ESP32 serves one
client at a time and refuses the rest, so "how many requests, in what order, never overlapping"
is a correctness property here and not a performance one.
"""
import binascii
import json
import os
import struct
import sys
import urllib.error

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import clips as CL                                        # noqa: E402
from hear import detsfile as DF                                     # noqa: E402
from hear import pool as P                                          # noqa: E402
from hear import sketch as SK                                       # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402


# ---------------------------------------------------------------- fixture bodies

def _frame_hex(seed=1, fs=16000.0):
    rng = np.random.default_rng(seed)
    q = rng.integers(-128, 127, size=(20, 8), dtype=np.int8)
    return binascii.hexlify(SK.pack(597174, -40.0, 1140, q, fs=fs)).decode()


def _dets_row(clip, node="nyquist", utc_us=1757459321000000, uptime_s=54912, sample=1016781646,
              seed=1, fs=16000.169, trigger="lf", clip_why="ok"):
    vals = {"node_id": node, "utc_us": str(utc_us), "uptime_s": str(uptime_s),
            "sample": str(sample), "pps_n": "12", "us_since_pps": "1234", "trigger": trigger,
            "flags": "0", "fs_hz": "%.3f" % fs, "sketch_back": "736",
            "frame_hex": _frame_hex(seed), "clip": clip, "clip_why": clip_why}
    return ",".join(vals[c] for c in DF.G5.written)


def _dets_csv(rows):
    return (",".join(DF.G5.declared) + "\n" + "\n".join(rows) + "\n").encode()


def _wav(fs=16000, samples=64000, channels=1, bits=16):
    """A canonical 44-byte-header WAV, byte-for-byte the shape night_node.ino writes."""
    data = b"\x11\x22" * samples
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, fs, fs * channels * bits // 8,
                          channels * bits // 8, bits)
            + b"data" + struct.pack("<I", len(data)) + data)


CLIP_A = "/clips/nyquist-db21acd5-1016781646.wav"
CLIP_B = "/clips/nyquist-db21acd5-1016797000.wav"
CLIP_C = "/clips/nyquist-db21acd5-1016812000.wav"


def _name(sample, boot="db21acd5", node="nyquist"):
    return "/clips/%s-%s-%d.wav" % (node, boot, sample)


# ---------------------------------------------------------------- the fake node

class ClipNode:
    """One node, one client at a time, over the four calls the drain makes.

    ⚠️`max_in_flight` IS AN ASSERTION TARGET, NOT INSTRUMENTATION. The real node refuses a second
    concurrent request rather than queueing it, so a drain that ever opens two is a drain that
    loses a clip and reports success.
    """

    def __init__(self, node="nyquist", dets_rows=(), clips=None, prev_rows=None):
        self.node = node
        self.dets_rows = list(dets_rows)
        self.prev_rows = prev_rows
        self.clips = dict(clips or {})           # "/clips/x.wav" -> body, or absent => 404
        self.http_error = {}                     # "/clips/x.wav" -> status code
        self.get_calls = []
        self.sd_calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.dets_raises = False
        self.watch = None                        # callable run at the first clip request

    def status(self, ip, timeout=None):
        return {"node": self.node, "uptime_s": 54912,
                "acq": {"fs_clean_hz": 16000.0, "win_s": 300, "drop_s": 0, "drop_samples": 0}}

    def ls(self, ip, timeout=None):
        return {"dets.csv": 4557}

    def sd(self, ip, name, timeout=None, tail=None):
        self.sd_calls.append((name, tail))
        if name == "dets.csv":
            if self.dets_raises:
                raise OSError("dets.csv refused")
            return _dets_csv(self.dets_rows) if self.dets_rows else None
        if name == "dets-prev.csv" and self.prev_rows:
            return _dets_csv(self.prev_rows)
        return None

    def get(self, url, timeout=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            path = url.split("file=", 1)[1]
            if not self.get_calls and self.watch:
                self.watch()
            self.get_calls.append(path)
            if path in self.http_error:
                raise urllib.error.HTTPError(url, self.http_error[path], "no", None, None)
            if path not in self.clips:
                raise urllib.error.HTTPError(url, 404, "not found", None, None)
            return self.clips[path]
        finally:
            self.in_flight -= 1


@pytest.fixture
def wired(monkeypatch):
    """Install a ClipNode over the four network calls and hand back the builder."""
    def build(**kw):
        n = ClipNode(**kw)
        monkeypatch.setattr(HD, "fetch_status", n.status)
        monkeypatch.setattr(HD, "fetch_sd", n.sd)
        monkeypatch.setattr(HD, "_ls_sizes", n.ls)
        monkeypatch.setattr(HD, "_get", n.get)
        return n
    return build


def _pool(tmp_path):
    return P.Pool(str(tmp_path / "pool"))


def _clip_paths(n):
    return [p for p in n.get_calls if p.startswith("/clips/")]


# ---------------------------------------------------------------- S3: what a clip IS

class TestAClipIsNotACsv:
    """⚠️`fetch_sd` REPORTS A PRESENT CLIP AS ABSENT. That is the whole reason `fetch_clip` exists."""

    def test_a_riff_body_is_a_clip_and_not_an_absent_file(self, wired):
        wired(clips={CLIP_A: _wav()})
        # The pre-change path, run against the same 128044 B body the node really serves.
        assert HD.fetch_sd("10.0.0.1", CLIP_A) is None, (
            "fetch_sd's header sniff must still read a WAV as absent -- if this ever passes, the "
            "sniff changed and the CSV lane's absent-file detection changed with it")
        body, reason = HD.fetch_clip("10.0.0.1", CLIP_A)
        assert reason is None and body is not None
        assert len(body) == CL.CLIP_BYTES == 128044
        assert body[:4] == b"RIFF"

    def test_a_zero_byte_200_is_a_refusal_not_a_clip(self, wired):
        # ⚠️MEASURED: /sd?file=/clips answers 200 with a 0-byte body. 200 is not proof of a file.
        wired(clips={CLIP_A: b""})
        assert HD.fetch_clip("10.0.0.1", CLIP_A) == (None, "empty")

    def test_a_short_body_is_short_and_a_non_wav_is_not_riff(self, wired):
        wired(clips={CLIP_A: _wav()[:5000], CLIP_B: b"utc_us,uptime_s\n1,2\n"})
        assert HD.fetch_clip("10.0.0.1", CLIP_A) == (None, "short")
        assert HD.fetch_clip("10.0.0.1", CLIP_B) == (None, "not_riff")

    def test_a_404_and_a_500_are_told_apart(self, wired):
        n = wired(clips={})
        n.http_error[CLIP_B] = 500
        assert HD.fetch_clip("10.0.0.1", CLIP_A) == (None, "http_404")
        assert HD.fetch_clip("10.0.0.1", CLIP_B) == (None, "http_500")

    def test_a_transport_failure_never_raises_at_the_caller(self, monkeypatch):
        def boom(url, timeout=None):
            raise OSError("connection refused")
        monkeypatch.setattr(HD, "_get", boom)
        assert HD.fetch_clip("10.0.0.1", CLIP_A) == (None, "transport")


# ---------------------------------------------------------------- S2: names out of dets.csv

class TestTheNamesComeFromTheDetsColumn:
    """Pre-change: nothing read the `clip` column at all; there was no consumer."""

    def test_the_names_are_used_verbatim_and_oldest_first(self, tmp_path, wired):
        rows = [_dets_row(_name(300), sample=300, seed=3),
                _dets_row(_name(100), sample=100, seed=1),
                _dets_row(_name(200), sample=200, seed=2)]
        n = wired(dets_rows=rows, clips={_name(s): _wav() for s in (100, 200, 300)})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert _clip_paths(n) == [_name(100), _name(200), _name(300)], (
            "⚠️ORDER IS BY EVICTION RISK. The flashed fleet evicts FIFO oldest-by-name, so "
            "oldest-first is most-at-risk-first; got %r" % _clip_paths(n))
        assert r["clips_fetched"] == 3

    def test_a_name_that_is_not_a_clip_path_is_refused_and_never_fetched(self, tmp_path, wired):
        rows = [_dets_row("/clips/../../etc/passwd", sample=1, seed=1),
                _dets_row("/night.csv", sample=2, seed=2),
                _dets_row("", sample=3, seed=3),
                _dets_row(_name(400), sample=400, seed=4)]
        n = wired(dets_rows=rows, clips={_name(400): _wav()})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert _clip_paths(n) == [_name(400)], (
            "a node-supplied string reached the network before it was parsed: %r" % _clip_paths(n))
        assert r["clips_refused"] == {"bad_name": 2}, r["clips_refused"]
        # An empty cell is not a refusal: it is a detection that produced no clip.
        assert r["clips_seen"] == 3

    def test_a_dotdot_name_is_refused_where_it_is_PARSED_not_where_it_is_written(self, tmp_path,
                                                                                 wired):
        """⚠️'no file called passwd appeared' passes on a drain that fetches nothing at all -- it
        passed against the pre-change tree, which is exactly the decorative test this repo has
        been bitten by. So the assertion is that the refusal was COUNTED and INDEXED: the name was
        rejected by the parser, which is the one place a node-supplied string becomes a path."""
        pl = _pool(tmp_path)
        n = wired(dets_rows=[_dets_row("/clips/../../etc/passwd"),
                             _dets_row(_name(1), sample=1, seed=2)],
                  clips={_name(1): _wav()})
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert r["clips_refused"] == {"bad_name": 1}
        bad = [row for row in CL.read_index(pl.root).values()
               if row["outcome"] == "refused_name"]
        assert len(bad) == 1 and bad[0]["clip"] == "/clips/../../etc/passwd"
        assert bad[0]["basename"] is None and "'..'" in bad[0]["reason"]
        assert _clip_paths(n) == [_name(1)], "the bad name still reached the network"
        for _dirpath, _dirs, files in os.walk(pl.root):
            for f in files:
                assert "passwd" not in f

    def test_dets_prev_is_a_name_source_too_and_the_two_are_deduplicated(self, tmp_path, wired):
        row = _dets_row(_name(500), sample=500, seed=5)
        n = wired(dets_rows=[row], prev_rows=[row], clips={_name(500): _wav()})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_seen"] == 1 and r["clips_fetched"] == 1
        assert _clip_paths(n) == [_name(500)], "the same clip was fetched twice from two files"


# ---------------------------------------------------------------- S4: the index is the memory

class TestTheIndexIsTheDedupKey:
    """Pre-change: nothing was fetched at all, so there was no index to skip against."""

    def test_a_fetched_name_is_skipped_on_the_second_run(self, tmp_path, wired):
        rows = [_dets_row(_name(600), sample=600)]
        pl = _pool(tmp_path)
        wired(dets_rows=rows, clips={_name(600): _wav()})
        first = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert first["clips_fetched"] == 1 and first["clips_already_held"] == 0

        n2 = wired(dets_rows=rows, clips={_name(600): _wav()})
        second = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert _clip_paths(n2) == [], "a clip the pool already holds was fetched again"
        assert second["clips_fetched"] == 0 and second["clips_already_held"] == 1

    def test_a_404_is_recorded_as_evicted_and_never_reprobed(self, tmp_path, wired):
        # ⚠️WITHOUT THE NEGATIVE CACHE the drain re-probes 321 dead nyquist names every 15 min
        # forever -- measured: 321 of 370 dets-named clips are already 404.
        rows = [_dets_row(_name(700), sample=700)]
        pl = _pool(tmp_path)
        n = wired(dets_rows=rows, clips={})
        first = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert first["clips_gone"] == 1 and _clip_paths(n) == [_name(700)]

        n2 = wired(dets_rows=rows, clips={})
        second = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert _clip_paths(n2) == [], "a name already known destroyed was probed again"
        assert second["clips_gone"] == 0 and second["clips_already_gone"] == 1

    def test_a_refusal_is_indexed_too_so_the_census_survives_the_run(self, tmp_path, wired):
        rows = [_dets_row(_name(800), sample=800), _dets_row("/night.csv", sample=801, seed=2)]
        pl = _pool(tmp_path)
        wired(dets_rows=rows, clips={_name(800): b""})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        idx = list(CL.read_index(pl.root).values())
        assert sorted(r["outcome"] for r in idx) == ["refused_bad_body", "refused_name"]
        assert [r["reason"] for r in idx if r["outcome"] == "refused_bad_body"] == ["empty"]

    def test_a_stored_row_carries_the_path_the_bytes_are_at(self, tmp_path, wired):
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(900), sample=900)], clips={_name(900): _wav()})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        row = list(CL.read_index(pl.root).values())[0]
        assert row["outcome"] == "stored"
        # The fixture's utc_us is 1757459321000000, which is 2025-09-09T20:28:41Z. ⚠️The spec's
        # worked example pairs that same utc_us with a 2026-09-09 path; the DAY IS DERIVED from
        # the stamp and nothing here re-states it, so the derivation is what is asserted.
        assert row["path"] == "clips/2025-09-09/nyquist/nyquist-db21acd5-900.wav", row["path"]
        assert not os.path.isabs(row["path"]), "the index must not pin an absolute pod path"
        on_disk = os.path.join(pl.root, row["path"])
        assert os.path.getsize(on_disk) == CL.CLIP_BYTES
        assert row["bytes"] == CL.CLIP_BYTES and len(row["sha256"]) == 64

    def test_both_rate_readings_travel_side_by_side(self, tmp_path, wired):
        # ⚠️A SHIPPED CONDITION ON MACH: a whole boot headed 22624 Hz while the CSV said 16000.
        # Keeping one of the two would have made the disagreement invisible.
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(910), sample=910, fs=16000.169)],
              clips={_name(910): _wav(fs=22624)})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        row = list(CL.read_index(pl.root).values())[0]
        assert row["outcome"] == "stored", "a 22624 Hz header must be KEPT, not refused at ingest"
        assert row["fs_hz"] == 16000.169 and row["wav_header_fs_hz"] == 22624

    def test_an_unanchored_clip_gets_no_invented_window(self, tmp_path, wired):
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(920), sample=920, utc_us=0)],
              clips={_name(920): _wav()})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        row = list(CL.read_index(pl.root).values())[0]
        assert row["anchored"] is False
        assert row["t_start_utc_s"] is None and row["t_end_utc_s"] is None
        assert row["path"].startswith("clips/unanchored/"), row["path"]

    def test_the_clip_joins_to_its_dets_row_without_a_second_index(self, tmp_path, wired):
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(930), sample=930)], clips={_name(930): _wav()})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        row = list(CL.read_index(pl.root).values())[0]
        assert row["record_key"] in pl.keys(), (
            "record_key must be the parent dets row's pool key or the clip joins to nothing")


# ---------------------------------------------------------------- the cap, and saying so

class TestTheCapIsReportedWhenItBites:
    """Pre-change: `drain_node`'s run record had none of these keys, so a cap could bind in
    silence while the run reported success -- the exact failure this codebase exists to prevent."""

    def test_the_run_record_carries_clips_cap_hit_and_clips_deferred_by_cap(self, tmp_path, wired):
        rows = [_dets_row(_name(s), sample=s, seed=i) for i, s in enumerate((1, 2, 3, 4, 5))]
        n = wired(dets_rows=rows, clips={_name(s): _wav() for s in (1, 2, 3, 4, 5)})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1", clip_max_per_node=2)
        assert r["clips_fetched"] == 2
        assert r["clips_cap_hit"] is True and r["clips_cap_reason"] == "count"
        assert r["clips_deferred_by_cap"] == 3
        assert len(_clip_paths(n)) == 2, "the cap must be tested BEFORE the request is spent"

    def test_a_deferred_clip_is_retried_next_run(self, tmp_path, wired):
        rows = [_dets_row(_name(s), sample=s, seed=i) for i, s in enumerate((1, 2, 3))]
        pl = _pool(tmp_path)
        wired(dets_rows=rows, clips={_name(s): _wav() for s in (1, 2, 3)})
        HD.drain_node(pl, "nyquist", "10.0.0.1", clip_max_per_node=1)
        n2 = wired(dets_rows=rows, clips={_name(s): _wav() for s in (1, 2, 3)})
        r2 = HD.drain_node(pl, "nyquist", "10.0.0.1", clip_max_per_node=9)
        assert r2["clips_already_held"] == 1 and r2["clips_fetched"] == 2, (
            "deferred_by_cap must NOT be terminal; got %r" % r2)
        assert _clip_paths(n2) == [_name(2), _name(3)]

    def test_the_deadline_stops_the_fetch_and_names_itself(self, tmp_path, wired):
        rows = [_dets_row(_name(s), sample=s, seed=i) for i, s in enumerate((1, 2, 3))]
        n = wired(dets_rows=rows, clips={_name(s): _wav() for s in (1, 2, 3)})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1", clip_deadline_s=0.0)
        assert r["clips_cap_reason"] == "deadline" and r["clips_deferred_by_cap"] == 3
        assert _clip_paths(n) == []

    def test_a_full_disk_stops_the_fetch_before_it_fills_the_pvc(self, tmp_path, wired,
                                                                monkeypatch):
        monkeypatch.setattr(CL, "free_bytes", lambda root: 1)
        n = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_cap_reason"] == "disk" and r["clips_deferred_by_cap"] == 1
        assert _clip_paths(n) == []


# ---------------------------------------------------------------- §14: the census totals itself

class TestTheCensusTotalsItself:
    def test_the_census_totals_itself(self, tmp_path, wired):
        rows = ([_dets_row(_name(s), sample=s, seed=s) for s in (1, 2, 3, 4, 5, 6)]
                + [_dets_row("/night.csv", sample=7, seed=7)])
        pl = _pool(tmp_path)
        # 1 stored beforehand, 2 stored now, 3 destroyed (404), 4 a 0-byte 200, 5+6 over the cap.
        wired(dets_rows=[rows[0]], clips={_name(1): _wav()})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        wired(dets_rows=rows, clips={_name(1): _wav(), _name(2): _wav(), _name(4): b"",
                                     _name(5): _wav(), _name(6): _wav()})
        r = HD.drain_node(pl, "nyquist", "10.0.0.1", clip_max_per_node=2)
        total = (r["clips_fetched"] + r["clips_already_held"] + r["clips_already_gone"]
                 + r["clips_gone"] + sum(r["clips_refused"].values())
                 + r["clips_deferred_by_cap"])
        assert total == r["clips_seen"] == 7, r
        assert r["clips_already_held"] == 1 and r["clips_gone"] == 1
        assert r["clips_refused"] == {"empty": 1, "bad_name": 1}, r["clips_refused"]

    def test_a_store_that_fails_is_counted_and_is_not_terminal(self, tmp_path, wired,
                                                              monkeypatch):
        # The bytes arrived and could not be written. A run that called that `stored` would make
        # the index say a clip is held that is not on disk -- and `stored` is terminal, so it
        # would never be asked for again.
        pl = _pool(tmp_path)
        n = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})

        def boom(*a, **kw):
            raise OSError("read-only file system")
        monkeypatch.setattr(HD, "_store_clip", boom)
        r = HD.drain_clips(pl, "nyquist", "10.0.0.1",
                           HD.clip_candidates([("dets.csv", _dets_csv(n.dets_rows))], "nyquist"))
        assert r["clips_fetched"] == 0 and r["clips_refused"] == {"store_OSError": 1}
        total = (r["clips_fetched"] + r["clips_already_held"] + r["clips_already_gone"]
                 + r["clips_gone"] + sum(r["clips_refused"].values())
                 + r["clips_deferred_by_cap"])
        assert total == r["clips_seen"] == 1
        assert CL.read_index(pl.root) == {}, "a failed store must leave no terminal index row"


# ---------------------------------------------------------------- one client at a time

class TestOneClientAtATime:
    """⚠️MEASURED: the ESP32 serves ONE client and REFUSES the rest. /ls answers in 35-118 ms idle,
    degrades to 7.3 s during a large transfer, and is refused outright mid-request."""

    def test_only_one_request_is_ever_in_flight(self, tmp_path, wired):
        rows = [_dets_row(_name(s), sample=s, seed=i) for i, s in enumerate(range(1, 9))]
        n = wired(dets_rows=rows, clips={_name(s): _wav() for s in range(1, 9)})
        HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert n.max_in_flight == 1, (
            "%d concurrent requests reached one node; it refuses the second rather than queueing "
            "it, so this loses clips and reports success" % n.max_in_flight)


# ---------------------------------------------------------------- unmeasured is not clean

class TestUnmeasuredIsNotClean:
    def test_an_unreachable_node_reports_clips_unknown_and_never_clips_gone_zero(self, tmp_path,
                                                                                monkeypatch):
        def boom(ip, timeout=None):
            raise OSError("no route to host")
        monkeypatch.setattr(HD, "fetch_status", boom)
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_unknown"] is True
        assert r["clips_gone"] is None and r["clips_seen"] is None, (
            "a run that never reached the node reported a count: %r" % r)
        assert "/status" in r["clips_reason"]

    def test_a_refused_identity_measures_no_clip(self, tmp_path, wired):
        wired(node="mach", dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_unknown"] is True and r["clips_fetched"] is None
        assert "mach" in r["clips_reason"]

    def test_a_dets_fetch_that_failed_is_unknown_not_a_clean_zero(self, tmp_path, wired):
        n = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        n.dets_raises = True
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_unknown"] is True and r["clips_seen"] is None, (
            "no dets.csv means no discoverable name -- that is unmeasured, not zero clips")
        assert "dets.csv" in r["clips_reason"]

    def test_a_disabled_lane_says_so_rather_than_reporting_zero(self, tmp_path, wired):
        n = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1", clip_max_per_node=0)
        assert r["clips_unknown"] is True and "disabled" in r["clips_reason"]
        assert _clip_paths(n) == []

    def test_a_node_with_no_clips_at_all_measures_zero_and_says_zero(self, tmp_path, wired):
        wired(dets_rows=[_dets_row("", sample=1)], clips={})
        r = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")
        assert r["clips_unknown"] is False and r["clips_seen"] == 0 and r["clips_gone"] == 0


# ---------------------------------------------------------------- S8: it reaches the gate

class TestItReachesTheGate:
    """⚠️`check` runs `17 * * * *` and the drain `*/15 * * * *`, so a per-run field is four runs
    stale before the gate reads it. Pre-change `write_heartbeat` had no clip ring and `check()`
    read no clip field at all."""

    def _four_runs(self, tmp_path, wired, t0, **kw):
        pl = _pool(tmp_path)
        rows = [_dets_row(_name(s), sample=s, seed=i) for i, s in enumerate((1, 2, 3))]
        for i in range(4):
            wired(dets_rows=rows, clips={_name(s): _wav() for s in (1, 2, 3)})
            r = HD.drain_node(pl, "nyquist", "10.0.0.1", **kw)
            HD.write_heartbeat(pl.root, [r], None, now=t0 + i * 900)
        return pl

    def test_a_cap_hit_reaches_the_gate_four_runs_later(self, tmp_path, wired):
        t0 = 1757459321.0
        pl = self._four_runs(tmp_path, wired, t0, clip_max_per_node=1)
        code, lines = HD.check(pl.root, now=t0 + 4 * 900, unfetched_window_s=7200.0)
        text = "\n".join(lines)
        assert code == 1, text
        assert "CAP BOUND" in text and "count" in text, text
        hb = json.load(open(HD.heartbeat_path(pl.root)))
        ring = hb["sensors"]["nyquist"]["clips_recent"]
        assert len(ring) == 4, "the ring must keep every run, not just the last"
        # Run 1 defers 2, run 2 defers 1, run 3 clears the backlog, run 4 has nothing to do. The
        # gate sees 3 deferrals across the window; reading only the LAST run would see zero.
        assert [e["deferred"] for e in ring] == [2, 1, 0, 0]
        assert [e["cap_reason"] for e in ring] == ["count", "count", None, None]
        assert ring[-1]["deferred"] == 0, (
            "the newest run is clean -- this is exactly the case a per-run field hides")

    def test_an_uncapped_run_is_reported_measured_and_passes(self, tmp_path, wired):
        t0 = 1757459321.0
        pl = self._four_runs(tmp_path, wired, t0)
        code, lines = HD.check(pl.root, now=t0 + 4 * 900, unfetched_window_s=7200.0)
        text = "\n".join(lines)
        assert code == 0, text
        assert "clips +3 fetched" in text, text

    def test_an_unknown_run_is_unknown_at_the_gate_and_never_a_clean_zero(self, tmp_path,
                                                                         monkeypatch):
        def boom(ip, timeout=None):
            raise OSError("no route to host")
        monkeypatch.setattr(HD, "fetch_status", boom)
        pl = _pool(tmp_path)
        t0 = 1757459321.0
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        HD.write_heartbeat(pl.root, [r], None, now=t0)
        code, lines = HD.check(pl.root, now=t0 + 60, unfetched_window_s=7200.0)
        text = "\n".join(lines)
        assert "clips UNKNOWN" in text, text
        assert "0 destroyed" not in text, text
        ring = json.load(open(HD.heartbeat_path(pl.root)))["sensors"]["nyquist"]["clips_recent"]
        assert ring[-1] == {"at": t0, "seen": None, "fetched": None, "gone": None,
                            "deferred": None, "cap_reason": None, "unknown": True,
                            "reason": r["clips_reason"]}

    def test_destroyed_clips_report_but_do_not_fail_by_default(self, tmp_path, wired):
        # ⚠️666 clips are ALREADY destroyed and still named in current dets.csv files. A gate
        # armed today fires on the backlog and gets muted on day one, which is worse than none.
        t0 = 1757459321.0
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(s), sample=s, seed=s) for s in (1, 2)], clips={})
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        HD.write_heartbeat(pl.root, [r], None, now=t0)
        code, lines = HD.check(pl.root, now=t0 + 60, unfetched_window_s=7200.0)
        assert code == 0 and "2 destroyed" in "\n".join(lines)
        code, lines = HD.check(pl.root, now=t0 + 60, unfetched_window_s=7200.0, max_clips_lost=1)
        assert code == 1 and "destroyed before this drain reached them" in "\n".join(lines)

    def test_the_gate_flags_reach_the_gate(self, tmp_path, monkeypatch):
        """A flag that parses and is then dropped on the floor is the same as no flag."""
        seen = {}

        def fake_check(root, *a, **kw):
            seen.update(kw)
            return 0, []
        monkeypatch.setattr(HD, "check", fake_check)
        assert HD.main(["--pool", str(tmp_path), "--check",
                        "--max-clips-deferred", "4", "--max-clips-lost", "9"]) == 0
        assert seen["max_clips_deferred"] == 4 and seen["max_clips_lost"] == 9

    def test_the_drain_flags_reach_drain_node(self, tmp_path, monkeypatch):
        seen = {}

        def fake_drain(pl, node, ip, timeout=None, **kw):
            seen.update(kw)
            return {"node": node, "ok": True, "errors": [], "added": 0, "files": [],
                    "clips_unknown": True, "clips_reason": "stub"}
        monkeypatch.setattr(HD, "drain_node", fake_drain)
        HD.main(["--pool", str(tmp_path), "--node", "nyquist=10.0.0.1",
                 "--clip-max-per-node", "7", "--clip-deadline-s", "5",
                 "--clip-store-max-b", "123"])
        assert seen == {"clip_max_per_node": 7, "clip_deadline_s": 5.0,
                        "clip_store_max_bytes": 123}

    def test_the_defaults_are_the_measured_card_ceiling(self):
        # 6291456 / 128044 = 49 exactly, and all three nodes report budget_left_clips 0.
        assert HD.CLIP_MAX_PER_NODE_DEFAULT == 6291456 // CL.CLIP_BYTES == 49
        assert HD.DEFAULT_MAX_CLIPS_DEFERRED == 0, "deferral is a design invariant, not a range"
        assert HD.DEFAULT_MAX_CLIPS_LOST == -1, (
            "666 clips are already destroyed and still named in current dets.csv files; a gate "
            "armed today fires on the backlog and gets muted on day one")


# ---------------------------------------------------------------- prune

class TestPruneRunsBeforeTheFetch:
    """The audio is a cache of something the node already destroyed; the index is the record."""

    def test_room_is_made_before_the_bytes_are_asked_for(self, tmp_path, wired):
        pl = _pool(tmp_path)
        old = CL.store_path(pl.root, "2026-09-01", "nyquist", "nyquist-aa-1.wav")
        os.makedirs(os.path.dirname(old), exist_ok=True)
        open(old, "wb").write(b"\0" * CL.CLIP_BYTES)
        state = {}
        n = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        n.watch = lambda: state.update(old_still_there=os.path.exists(old))
        r = HD.drain_node(pl, "nyquist", "10.0.0.1", clip_store_max_bytes=1000)
        assert r["prune"]["files_deleted"] == 1
        assert state == {"old_still_there": False}, (
            "the prune must run BEFORE the fetch, so the room the fetch needs already exists")
        assert r["clips_fetched"] == 1

    def test_the_index_line_survives_the_audio(self, tmp_path, wired):
        pl = _pool(tmp_path)
        wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        HD.drain_node(pl, "nyquist", "10.0.0.1")
        row = list(CL.read_index(pl.root).values())[0]
        CL.prune(pl.root, 0, 1757460000.0)
        after = list(CL.read_index(pl.root).values())[0]
        assert not os.path.exists(os.path.join(pl.root, row["path"]))
        assert after["outcome"] == "stored" and after["audio_pruned_at"] == 1757460000.0
        # ⚠️STILL TERMINAL. The node destroyed it long ago; re-probing costs a request to learn
        # nothing, and 321 such names are already on the card.
        n2 = wired(dets_rows=[_dets_row(_name(1), sample=1)], clips={_name(1): _wav()})
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert _clip_paths(n2) == [] and r["clips_already_held"] == 1
