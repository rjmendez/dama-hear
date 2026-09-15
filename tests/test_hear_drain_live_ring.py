"""A node with no card is drained from its live ring, and a detection that ring dropped is counted.

No HTTP: /status, /sd, /ls and /detections are served by a fake, as in tests/test_hear_drain.py.
"""
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                          # noqa: E402
from hear import sketch as SK                                       # noqa: E402
from hear.backend import survey as SV                               # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402
from tests.test_hear_drain import FakeNode                          # noqa: E402

T = 1_788_900_000
# a fictional position near the fictional test origin, never the site
POS = {"mean_lat": 40.2901234, "mean_lon": -79.1198765, "mean_hell_m": 312.5, "hacc_m": 1.4,
       "vacc_m": 2.2, "n": 4200}


def _frame_hex(seed):
    q, ref = SK.sketch(np.random.default_rng(seed).normal(0, 300.0, 4096), 16000.0,
                       layout=SK.LAYOUT_FIXED)
    return SK.pack(1000 + seed % 50000, ref, 500, q, fs=16000.0, layout=SK.LAYOUT_FIXED).hex()


class CardlessNode(FakeNode):
    """What gold, ageev and kasami serve: `sd: false` and a /detections ring, no card files."""

    def __init__(self, node="gold", **kw):
        super().__init__(node=node, **kw)
        self.ring = []
        self.det_n = 0
        self.detection_calls = 0

    def fire(self, k):
        for _ in range(k):
            h = _frame_hex(self.det_n + 1)
            self.ring.append({"i": self.det_n,
                              "utc_us": self.boot_epoch_us + self.uptime * 1_000_000
                              + self.det_n * 1000,
                              "uptime_s": self.uptime, "sample": self.det_n * 4096,
                              "pps_n": self.uptime, "us_since_pps": 1234, "trigger": 900,
                              "flags": 0, "fs_hz": 16000.0, "frame_len": len(h) // 2,
                              "frame": h, "clip": "", "clip_why": ""})
            self.det_n += 1
        return self

    def tick(self, seconds):
        self.uptime += int(seconds)
        return self

    def reboot(self):
        self.ring, self.det_n, self.uptime = [], 0, 5
        self.boot_epoch_us += 3_600_000_000
        return self

    def status(self, ip, timeout=None):
        st = super().status(ip, timeout)
        st.update({"class": "esp32s3-i2s-gps", "sd": False, "sd_free_mb": 0, "sd_total_mb": 0,
                   "pos": dict(POS), "gps": {"fix": 3}, "audio": {"detections": self.det_n}})
        return st

    def detections(self, ip, timeout=None, cursor=None, limit=None, until=None):
        self.detection_calls += 1
        return json.dumps(self.ring[-HD.LIVE_RING_HTTP_MAX:]).encode()


class TruncatingNode(CardlessNode):
    """gold on 2026-09-13: a 200 whose body stops mid-row once the node's heap runs out."""

    def __init__(self, cap=10 ** 9, **kw):
        super().__init__(**kw)
        self.cap = cap

    def detections(self, ip, timeout=None, cursor=None, limit=None, until=None):
        return super().detections(ip, timeout)[:self.cap]


class CursorNode(CardlessNode):
    """A firmware page that walks the whole ring by cursor while bare `/detections` stays legacy."""

    def __init__(self, ring_cap=1024, boot_id="00000000000000a1", grow_after=None, **kw):
        super().__init__(**kw)
        self.ring_cap = ring_cap
        self.boot_id = boot_id
        self.grow_after = dict(grow_after or {})

    def reboot(self):
        super().reboot()
        self.boot_id = "%016x" % ((int(self.boot_id, 16) + 1) & ((1 << 64) - 1) or 1)
        return self

    def _page(self, cursor, limit, until):
        total = self.det_n
        held = min(total, self.ring_cap)
        oldest = total - held
        start = oldest
        gap = None
        if limit is None:
            limit = HD.LIVE_RING_HTTP_MAX
        if cursor is not None:
            boot, start = HD.parse_live_cursor(cursor)
            if boot != self.boot_id:
                gap = {"kind": "reboot", "lost_rows": oldest, "requested_cursor": cursor,
                       "resume_cursor": HD.live_ring_cursor(self.boot_id, oldest),
                       "previous_boot_unmeasured": True}
                start = oldest
            elif start < oldest:
                gap = {"kind": "overrun", "lost_rows": oldest - start, "requested_cursor": cursor,
                       "resume_cursor": HD.live_ring_cursor(self.boot_id, oldest)}
                start = oldest
        snap = total
        if until is not None:
            boot, snap = HD.parse_live_cursor(until)
            assert boot == self.boot_id
            snap = min(snap, total)
        if start > snap:
            snap = start
        end = min(snap, start + int(limit))
        rows = [dict(r) for r in self.ring if start <= r["i"] < end]
        return {
            "contract": HD.LIVE_RING_CURSOR_CONTRACT,
            "node": self.node,
            "boot_id": self.boot_id,
            "boot_epoch_us": self.boot_epoch_us,
            "cursor": cursor,
            "oldest_cursor": None if not held else HD.live_ring_cursor(self.boot_id, oldest),
            "newest_cursor": None if total == 0 else HD.live_ring_cursor(self.boot_id, total - 1),
            "next_cursor": HD.live_ring_cursor(self.boot_id, end),
            "until_cursor": HD.live_ring_cursor(self.boot_id, snap),
            "limit": int(limit),
            "returned": len(rows),
            "has_more": end < snap,
            "gap": gap,
            "rows": rows,
        }

    def detections(self, ip, timeout=None, cursor=None, limit=None, until=None):
        self.detection_calls += 1
        if cursor is None and limit is None and until is None:
            return super().detections(ip, timeout)
        body = json.dumps(self._page(cursor, limit, until)).encode()
        if self.detection_calls in self.grow_after:
            self.tick(1).fire(self.grow_after[self.detection_calls])
        return body


class BrokenCursorNode(CardlessNode):
    def detections(self, ip, timeout=None, cursor=None, limit=None, until=None):
        self.detection_calls += 1
        return b'{"contract":"cursor-v1","rows":"not-a-list"}'


@pytest.fixture
def cardless(monkeypatch):
    def build(cls=CardlessNode, **kw):
        n = cls(**kw)
        monkeypatch.setattr(HD, "fetch_status", n.status)
        monkeypatch.setattr(HD, "fetch_sd", n.sd)
        monkeypatch.setattr(HD, "_ls_sizes", n.ls)
        monkeypatch.setattr(HD, "fetch_detections", n.detections)
        return n
    return build


def _run(pl, n, stamp):
    return HD.drain_node(pl, n.node, "10.0.0.9", stamp=stamp)


class TestCardlessDrain:
    def test_the_live_ring_is_ingested_and_no_card_file_is_ever_asked_for(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless().fire(5)
        r = _run(pl, n, T)
        assert r["ok"] and r["no_card"] and r["added"] == 5
        assert n.sd_calls == [] and n.ls_calls == 0 and n.detection_calls == 1
        assert r["live_ring_lost"] is None and "first sighting" in r["live_ring_reason"]
        assert r["unfetched_unknown"] is False and r["clips_unknown"] is False

    def test_rows_served_twice_are_stored_once_and_a_quiet_run_loses_nothing(self, tmp_path,
                                                                             cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless()
        r1 = _run(pl, n.fire(4), T)
        r2 = _run(pl, n.tick(900), T + 900)
        assert r1["added"] == 4 and r2["added"] == 0 and r2["live_ring_lost"] == 0

    def test_detections_the_ring_dropped_before_the_drain_came_are_counted(self, tmp_path,
                                                                           cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless()
        _run(pl, n.fire(5), T)
        r = _run(pl, n.tick(900).fire(200), T + 900)
        assert r["added"] == HD.LIVE_RING_HTTP_MAX
        assert r["live_ring_lost"] == 200 - HD.LIVE_RING_HTTP_MAX

    def test_a_reboot_that_outlives_the_old_uptime_is_still_a_reboot(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless()
        _run(pl, n.fire(3), T)
        n.reboot().tick(400).fire(130)            # new uptime 405 > the old boot's 100
        r = _run(pl, n, T + 900)
        assert r["live_ring_lost"] == 2 and "rebooted" in r["live_ring_reason"]

    def test_the_position_is_filed_under_state_and_never_in_the_run_report(self, tmp_path,
                                                                           cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        r = _run(pl, cardless().fire(1), T)
        doc = json.loads((tmp_path / "pool" / "state" / HD.NODE_POSITIONS_FILE).read_text())
        g = doc["nodes"]["gold"]
        assert (g["lat_deg"], g["lon_deg"], g["h_ell_m"]) == (POS["mean_lat"], POS["mean_lon"],
                                                              POS["mean_hell_m"])
        assert (g["fixes"], g["fix"], g["class"], g["at"]) == (POS["n"], 3, "esp32s3-i2s-gps", T)
        assert r["position"] == {"fixes": POS["n"], "hacc_m": POS["hacc_m"]}
        text = json.dumps(r)
        assert str(POS["mean_lat"]) not in text and str(POS["mean_lon"]) not in text

    def test_a_node_that_averaged_no_fix_files_no_position_and_drops_a_stale_one(self, tmp_path,
                                                                                 cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless().fire(1)
        _run(pl, n, T)
        path = tmp_path / "pool" / "state" / HD.NODE_POSITIONS_FILE
        assert "gold" in json.loads(path.read_text())["nodes"]
        zero = dict(POS, mean_lat=0.0, mean_lon=0.0, mean_hell_m=0.0, hacc_m=0.0, vacc_m=0.0, n=0)
        real = n.status
        n.status = lambda ip, timeout=None: dict(real(ip, timeout), pos=dict(zero))
        HD.fetch_status = n.status
        r = _run(pl, n.tick(900), T + 900)
        assert r["position"] is None and r["ok"]
        assert "gold" not in json.loads(path.read_text())["nodes"]

    def test_what_the_drain_files_is_what_the_survey_fallback_reads(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        _run(pl, cardless().fire(1), T)
        doc = json.loads((tmp_path / "pool" / "state" / HD.NODE_POSITIONS_FILE).read_text())
        base = SV.from_dict({"frame": "enu_local", "units": "m",
                             "origin": {"lat_deg": 40.29, "lon_deg": -79.12, "h_ell_m": 300.0},
                             "nodes": [{"node_id": 1, "name": "a", "e_m": 0, "n_m": 0, "u_m": 0},
                                       {"node_id": 2, "name": "b", "e_m": 30, "n_m": 0, "u_m": 0},
                                       {"node_id": 3, "name": "c", "e_m": 0, "n_m": 30, "u_m": 0}]})
        sv, rep = SV.augment_from_node_gps(base, doc["nodes"], T + 60)
        assert rep and rep[0]["used"], rep
        assert sv.position_sources[SV.gps_node_id("gold")] == SV.POSITION_SOURCE_GPS

    def test_a_body_the_node_cut_short_is_salvaged_and_its_newest_rows_are_late_not_lost(
            self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless(cls=TruncatingNode).fire(20)
        n.cap = len(json.dumps(n.ring).encode()) // 2 + 7          # cut mid-row
        r1 = _run(pl, n, T)
        assert r1["ok"] and 0 < r1["added"] < 20
        assert r1["live_ring_truncated_bytes"] == n.cap
        assert r1["live_ring_pending"] == 20 - r1["added"]
        n.cap = 10 ** 9
        r2 = _run(pl, n.tick(900), T + 900)
        assert r1["added"] + r2["added"] == 20
        assert r2["live_ring_lost"] == 0 and r2["live_ring_truncated_bytes"] is None

    def test_a_truncated_body_shows_on_the_check_and_does_not_fail_it(self, tmp_path, cardless):
        root = str(tmp_path / "pool")
        pl = P.Pool(root)
        n = cardless(cls=TruncatingNode).fire(20)
        n.cap = len(json.dumps(n.ring).encode()) // 2 + 7
        HD.write_heartbeat(root, [_run(pl, n, T)], None, now=T)
        code, lines = HD.check(root, now=T + 60)
        assert code == 0, lines
        assert "body TRUNCATED by the node in 1 of 1 run(s)" in "\n".join(lines)

    def test_check_reads_a_card_less_node_as_not_applicable_not_failed(self, tmp_path, cardless):
        root = str(tmp_path / "pool")
        pl = P.Pool(root)
        n = cardless()
        HD.write_heartbeat(root, [_run(pl, n.fire(3), T)], None, now=T)
        HD.write_heartbeat(root, [_run(pl, n.tick(900).fire(2), T + 900)], None, now=T + 900)
        code, lines = HD.check(root, now=T + 960)
        line = next(x for x in lines if x.startswith("gold"))
        assert code == 0, lines
        assert "scene n/a (no card)" in line and "clips n/a (no card)" in line
        assert "NEVER" not in line and "live ring 8 row(s) read, +5 new over 2 run(s)" in line
        assert "/ 2 page(s) (legacy)" in line

    def test_check_fails_on_a_live_ring_loss(self, tmp_path, cardless):
        root = str(tmp_path / "pool")
        pl = P.Pool(root)
        n = cardless()
        HD.write_heartbeat(root, [_run(pl, n.fire(5), T)], None, now=T)
        HD.write_heartbeat(root, [_run(pl, n.tick(900).fire(200), T + 900)], None, now=T + 900)
        code, lines = HD.check(root, now=T + 960)
        assert code == 1 and "LIVE RING LOST 72" in "\n".join(lines)


def test_a_node_with_a_card_takes_the_card_path_exactly_as_before(tmp_path, monkeypatch):
    n = FakeNode().grow(10)
    monkeypatch.setattr(HD, "fetch_status", n.status)
    monkeypatch.setattr(HD, "fetch_sd", n.sd)
    monkeypatch.setattr(HD, "_ls_sizes", n.ls)
    called = []
    monkeypatch.setattr(HD, "fetch_detections", lambda *a, **k: called.append(a) or b"[]")
    r = HD.drain_node(P.Pool(str(tmp_path / "pool")), n.node, "10.0.0.1", stamp=T)
    assert not r.get("no_card") and called == [] and n.sd_calls


def test_a_truncated_live_ring_frame_is_counted_not_stored(tmp_path):
    pl = P.Pool(str(tmp_path / "pool"))
    h = _frame_hex(1)
    body = [{"i": 0, "utc_us": 1788900000000000, "uptime_s": 10, "sample": 1, "fs_hz": 16000.0,
             "frame_len": len(h) // 2, "frame": h},
            {"i": 1, "utc_us": 1788900001000000, "uptime_s": 11, "sample": 2, "fs_hz": 16000.0,
             "frame_len": len(h) // 2, "frame": h[:-4]}]
    p = tmp_path / "d.json"
    p.write_text(json.dumps(body))
    e = pl.ingest_detections_json(str(p), default_node="gold")
    assert (e["rows"], e["added"], e["skip_reasons"]) == (2, 1, {"frame_len_mismatch": 1})
    assert pl.ingest_detections_json(str(p), default_node="gold")["added"] == 0


class TestCursorDrain:
    def test_cursor_paginates_the_whole_ring_in_one_run(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless(cls=CursorNode)
        _run(pl, n, T)  # seed cursor at 0 so the next run is measured, not a first sighting
        r = _run(pl, n.tick(900).fire(260), T + 900)
        assert r["ok"] and r["added"] == 260
        assert r["live_ring_rows"] == 260
        assert r["live_ring_lost"] == 0
        assert r["live_ring_mode"] == HD.LIVE_RING_CURSOR_CONTRACT
        assert r["live_ring_pages"] == math.ceil(260 / HD.LIVE_RING_HTTP_MAX)
        assert r["live_ring_pending"] == 0

    def test_cursor_snapshot_is_deterministic_and_leaves_later_rows_for_the_next_run(
            self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless(cls=CursorNode, grow_after={2: 50})  # 2nd call = 1st paged call after the seed
        _run(pl, n, T)
        r1 = _run(pl, n.tick(900).fire(200), T + 900)
        r2 = _run(pl, n.tick(900), T + 1800)
        assert r1["added"] == 200 and r1["live_ring_pending"] == 0
        assert r2["added"] == 50 and r2["live_ring_lost"] == 0

    def test_cursor_overrun_is_explicit_and_counted(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless(cls=CursorNode, ring_cap=16)
        _run(pl, n, T)
        r = _run(pl, n.tick(900).fire(40), T + 900)
        assert r["added"] == 16
        assert r["live_ring_lost"] == 24
        assert r["live_ring_gap"]["kind"] == "overrun"
        assert "aged behind" in r["live_ring_reason"]

    def test_cursor_reboot_is_explicit_and_counts_current_boot_rows_that_aged_out(
            self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        n = cardless(cls=CursorNode, ring_cap=16)
        _run(pl, n, T)
        _run(pl, n.tick(900).fire(3), T + 900)
        n.reboot().tick(400).fire(20)
        r = _run(pl, n, T + 1800)
        assert r["live_ring_lost"] == 4
        assert r["live_ring_gap"]["kind"] == "reboot"
        assert ("previous boot are unmeasured"
                in r["live_ring_reason"].replace("the previous boot", "previous boot"))

    def test_cursor_empty_ring_is_a_measured_zero(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        r = _run(pl, cardless(cls=CursorNode), T)
        assert r["ok"] and r["added"] == 0
        assert (r["live_ring_rows"], r["live_ring_lost"], r["live_ring_pages"]) == (0, 0, 1)

    def test_cursor_node_keeps_the_legacy_array_for_a_bare_request(self, cardless):
        n = cardless(cls=CursorNode).fire(3)
        assert isinstance(json.loads(n.detections("10.0.0.9").decode()), list)
        page = json.loads(n.detections("10.0.0.9", cursor=HD.live_ring_cursor(n.boot_id, 0),
                                       limit=2).decode())
        assert page["contract"] == HD.LIVE_RING_CURSOR_CONTRACT

    def test_a_malformed_cursor_page_fails_cleanly(self, tmp_path, cardless):
        pl = P.Pool(str(tmp_path / "pool"))
        r = _run(pl, cardless(cls=BrokenCursorNode).fire(1), T)
        assert not r["ok"]
        assert "detections:" in r["errors"][0]


class TestCursorParsing:
    def test_parse_live_ring_salvages_a_truncated_cursor_page(self):
        n = CardlessNode().fire(2)
        body = {
            "contract": HD.LIVE_RING_CURSOR_CONTRACT,
            "node": "gold",
            "boot_id": "00000000000000a1",
            "boot_epoch_us": 1788900000000000,
            "cursor": HD.live_ring_cursor("00000000000000a1", 0),
            "oldest_cursor": HD.live_ring_cursor("00000000000000a1", 0),
            "newest_cursor": HD.live_ring_cursor("00000000000000a1", 1),
            "next_cursor": HD.live_ring_cursor("00000000000000a1", 2),
            "until_cursor": HD.live_ring_cursor("00000000000000a1", 2),
            "limit": 128,
            "returned": 2,
            "has_more": False,
            "gap": None,
            "rows": [n.ring[0], n.ring[1]],
        }
        text = json.dumps(body)
        cut = text[:-2].rfind("}") + 1
        got = HD.parse_live_ring(text[:cut].encode())
        assert got["truncated"] == cut
        assert got["returned"] == 2

    def test_parse_live_ring_rejects_a_malformed_cursor_object(self):
        bad = b'{"contract":"cursor-v1","boot_id":"00000000000000a1","rows":"nope"}'
        with pytest.raises(ValueError):
            HD.parse_live_ring(bad)
