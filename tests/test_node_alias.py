"""An UNPROVISIONED id may be renamed to the board it is; a NAME may never be renamed at all.

⚠️THIS TEST EXISTS BECAUSE 233 ANCHORED RANKINE ARRIVALS WERE REFUSED 23 TIMES. rankine's card
carries 242 rows (233 with a PPS stamp, 2026-09-10 12:58:03Z-15:57:32Z) written by a firmware
build with no NODE_ID compiled in, so hear_node.ino:88's fallback named them `hear-5c4c94` --
the last three bytes of that board's own MAC. `pool._node_mismatch` compared that against the
fetch's `rankine` and refused every one of them, on every drain: /pool/corpus/ledger.jsonl held
5,566 dets and 41,352 scene `node_mismatch` skips for the id, all of them the same rows re-read.

⚠️THE FIX IS NOT A LOOSER GUARD, AND THAT IS WHAT MOST OF THIS FILE CHECKS. The rename is
confined to ids of the form `hear-<six hex>` that have a table entry stating their evidence;
`nyquist` can never become `mach` because the table cannot express it (`test_the_table_cannot
_express_a_rename_between_two_names`), and an unlisted `hear-...` id is still refused because a
MAC tail says which BOARD wrote a row and nothing about which position it stood at. The 2026-09-07
mis-flash case that motivated the guard is re-run here unchanged and must still be refused.
"""
import binascii
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import identity as ID                                   # noqa: E402
from hear import pool as P                                        # noqa: E402
from hear import scenefile as SF                                  # noqa: E402
from hear import sketch as SK                                     # noqa: E402

UTC0 = 1789045083090847                                           # 2026-09-10T12:58:03Z, the real
                                                                  # first refused rankine row
RAW = "hear-5c4c94"


def _dets_row(node, utc, seed):
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), 16000.0)
    frame = binascii.hexlify(SK.pack(597174, ref, 1140, q, fs=16000.0)).decode()
    return "%s,%d,14,%d,3,1200,1,2,16000.000,%d,%s,," % (node, utc, seed * 16384, 736, frame)


def _dets_csv(path, rows):
    hdr = ("node_id,utc_us,uptime_s,sample,pps_n,us_since_pps,trigger,flags,fs_hz,"
           "sketch_back,frame_hex,clip,clip_why")
    path.write_text("\n".join([hdr] + rows) + "\n")
    return str(path)


def _scene_row(node, utc, seed):
    q = np.random.default_rng(seed).integers(-128, 127, size=(20, 4), dtype=np.int8)
    vals = {"node": node, "utc_us": str(utc), "uptime_s": "14", "sample": str(seed * 16384),
            "bands": "20", "slices": "4", "span_ms": "1024", "ref_db4": "251", "frames": "64",
            "fft_us": "18987", "mel_hex": binascii.hexlify(q.tobytes()).decode(),
            "f_lo_hz": "62.5", "f_hi_hz": "7812.5"}
    return ",".join(vals[c] for c in SF.S2.written)


def _scene_csv(path, rows):
    path.write_text("\n".join([",".join(SF.S2.declared)] + rows) + "\n")
    return str(path)


def _rankine_card(tmp_path, name="dets.csv"):
    """The card's real shape: named rows, an unnamed-firmware boot, named rows again."""
    rows = ([_dets_row("rankine", UTC0 + i * 1_000_000, i) for i in range(4)]
            + [_dets_row(RAW, UTC0 + (10 + i) * 1_000_000, 50 + i) for i in range(6)]
            + [_dets_row("rankine", UTC0 + (30 + i) * 1_000_000, 90 + i) for i in range(4)])
    return _dets_csv(tmp_path / name, rows)


class TestTheTable:
    def test_the_declared_alias_is_the_one_the_pool_refused(self):
        assert ID.alias_of(RAW) == "rankine"
        assert ID.is_unprovisioned(RAW)
        assert "23 of 23" in ID.reason(RAW)               # the evidence, not just a mapping

    def test_a_name_a_human_chose_is_never_unprovisioned(self):
        for name in ("rankine", "nyquist", "mach", "puc", "hugbot", ""):
            assert not ID.is_unprovisioned(name)
            assert ID.alias_of(name) is None

    def test_the_shape_alone_renames_nothing(self):
        # ⚠️The load-bearing negative. `hear-aabbcc` is as unprovisioned as `hear-5c4c94`; what
        # separates them is a line of evidence, not a regex. Without this the guard would be
        # loosened for every unnamed board at once.
        assert ID.is_unprovisioned("hear-aabbcc")
        assert ID.alias_of("hear-aabbcc") is None
        assert ID.resolve("hear-aabbcc", "file") == ("hear-aabbcc", "file", None)

    def test_the_table_cannot_express_a_rename_between_two_names(self, monkeypatch):
        monkeypatch.setattr(ID, "ALIASES", {"nyquist": ("mach", "x" * 50)})
        with pytest.raises(AssertionError):
            ID._check_table()

    def test_an_alias_may_not_chain_or_target_an_unnamed_board(self, monkeypatch):
        monkeypatch.setattr(ID, "ALIASES", {"hear-aaaaaa": ("hear-bbbbbb", "x" * 50)})
        with pytest.raises(AssertionError):
            ID._check_table()

    def test_an_alias_without_evidence_is_refused(self, monkeypatch):
        monkeypatch.setattr(ID, "ALIASES", {"hear-aaaaaa": ("rankine", "because")})
        with pytest.raises(AssertionError):
            ID._check_table()

    def test_resolve_mutates_nothing_and_is_total(self):
        assert ID.resolve(None, None) == (None, None, None)
        assert ID.resolve(RAW, "file") == ("rankine", "alias", RAW)


class TestDetsIngest:
    def test_the_refused_rows_are_recovered_under_the_node_they_came_from(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_dets(_rankine_card(tmp_path), default_node="rankine",
                           origin="rankine:/dets.csv")
        assert e["added"] == 14                    # all 14, not 8
        assert e["skipped"] == 0
        assert e["skip_reasons"].get("node_mismatch", 0) == 0
        assert e["aliased"] == {RAW: 6}
        nodes = {r.node_id for r in pl.records()}
        assert nodes == {"rankine"}

    def test_the_raw_id_survives_on_the_row(self, tmp_path):
        # The objection `_node_mismatch` raises against relabelling -- that it destroys the only
        # trace of the flashing error -- is answered here or the rename is not allowed.
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_rankine_card(tmp_path), default_node="rankine")
        renamed = [r for r in pl.records() if r.extra.get("node_alias_of")]
        assert len(renamed) == 6
        assert {r.extra["node_alias_of"] for r in renamed} == {RAW}
        assert {r.extra["node_from"] for r in renamed} == {"alias"}
        # and a row that was never renamed does not carry the field at all: absent means
        # "never renamed", which False would not.
        untouched = [r for r in pl.records() if not r.extra.get("node_alias_of")]
        assert len(untouched) == 8
        assert all("node_alias_of" not in r.extra for r in untouched)

    def test_the_aliased_row_is_not_counted_as_a_skip(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_dets(_rankine_card(tmp_path), default_node="rankine")
        assert "aliased" not in e["skip_reasons"]
        assert sum(e["skip_reasons"].values()) == e["skipped"]
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]

    def test_re_ingesting_the_same_card_adds_nothing(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        path = _rankine_card(tmp_path)
        pl.ingest_dets(path, default_node="rankine")
        again = pl.ingest_dets(path, default_node="rankine")
        assert again["added"] == 0
        assert again["duplicate"] == 14
        assert again["aliased"] == {RAW: 6}        # still reported: it renamed, then deduped

    def test_one_detection_has_one_key_whichever_path_ingests_it(self, tmp_path):
        # ⚠️THIS IS WHY THE RENAME IS UNCONDITIONAL. `key()` hashes the node name, so a rename
        # that happened only when a `default_node` was passed would content-address the same
        # frame twice -- once as `rankine` from the drain, once as `hear-5c4c94` from a backfill
        # -- and the pool's own count would double for exactly the rows this fix recovers.
        pl = P.Pool(str(tmp_path / "pool"))
        path = _rankine_card(tmp_path)
        first = pl.ingest_dets(path, default_node=None)      # backfill, no node asserted
        assert first["added"] == 14
        second = pl.ingest_dets(path, default_node="rankine")
        assert second["added"] == 0
        assert second["duplicate"] == 14

    def test_a_second_pool_object_sees_the_recovered_rows_as_held(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_rankine_card(tmp_path), default_node="rankine")
        assert P.Pool(str(tmp_path / "pool")).ingest_dets(
            _rankine_card(tmp_path, "dets-prev.csv"), default_node="rankine")["added"] == 0


class TestTheGuardIsStillArmed:
    def test_an_unnamed_boot_on_the_WRONG_card_is_still_refused(self, tmp_path):
        # rankine's rows, fetched from mach. The rename resolves them to `rankine`, the guard
        # then disagrees with the fetch and refuses them -- under the RESOLVED name, because
        # that is the node the ledger has to name to be actionable.
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_dets(_rankine_card(tmp_path), default_node="mach")
        assert e["added"] == 0
        assert e["skipped"] == 14
        assert e["node_mismatch"] == {"rankine": 14}
        assert e["aliased"] == {RAW: 6}

    def test_the_2026_09_07_mis_flash_is_refused_exactly_as_before(self, tmp_path):
        # mach's card with one boot labelled `nyquist`: a NAME, not a MAC tail. Nothing about
        # this file may change.
        rows = ([_scene_row("mach", UTC0 + i * 1024000, i) for i in range(6)]
                + [_scene_row("nyquist", 0, 100 + i) for i in range(3)]
                + [_scene_row("mach", UTC0 + (10 + i) * 1024000, 200 + i) for i in range(6)])
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(_scene_csv(tmp_path / "scene.csv", rows), default_node="mach")
        assert e["added"] == 12
        assert e["skip_reasons"]["node_mismatch"] == 3
        assert e["node_mismatch"] == {"nyquist": 3}
        assert e["aliased"] == {}

    def test_an_unlisted_unnamed_board_is_refused_rather_than_guessed(self, tmp_path):
        rows = [_dets_row("hear-aabbcc", UTC0 + i * 1_000_000, i) for i in range(3)]
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_dets(_dets_csv(tmp_path / "dets.csv", rows), default_node="rankine")
        assert e["added"] == 0
        assert e["node_mismatch"] == {"hear-aabbcc": 3}


class TestSceneIngest:
    def test_recovered_scene_rows_land_in_the_nodes_own_partition(self, tmp_path):
        rows = ([_scene_row("rankine", UTC0 + i * 1024000, i) for i in range(4)]
                + [_scene_row(RAW, UTC0 + (10 + i) * 1024000, 50 + i) for i in range(5)])
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(_scene_csv(tmp_path / "scene.csv", rows), default_node="rankine")
        assert e["added"] == 9
        assert e["aliased"] == {RAW: 5}
        # ⚠️the failure this replaces: a partition named after a board that never had a position.
        day = os.listdir(pl.scene_dir)
        files = sorted(f for d in day for f in os.listdir(os.path.join(pl.scene_dir, d)))
        assert files == ["rankine.jsonl.gz"]
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]

    def test_re_ingesting_scene_adds_nothing(self, tmp_path):
        rows = [_scene_row(RAW, UTC0 + i * 1024000, i) for i in range(5)]
        path = _scene_csv(tmp_path / "scene.csv", rows)
        pl = P.Pool(str(tmp_path / "pool"))
        assert pl.ingest_scene(path, default_node="rankine")["added"] == 5
        assert pl.ingest_scene(path, default_node="rankine")["added"] == 0


class TestBackfillNaming:
    def test_the_archive_layout_names_its_own_node(self):
        from tools import hear_drain as D
        assert D.node_from_path("/pool/corpus/raw/rankine/1788899508-dets.csv") == "rankine"
        assert D.node_from_path("/d/20260907-2030/mach/scene.csv") == "mach"
        assert D.node_from_path("/d/nyquist_dets.csv") == "nyquist"

    def test_a_substring_is_not_a_node(self):
        # `known in path` filed everything under a `machine/` directory as mach, which would
        # have asserted the WRONG fetch identity -- the one failure worse than no identity.
        from tools import hear_drain as D
        assert D.node_from_path("/home/machine/drains/x_dets.csv") is None
        assert D.node_from_path("/home/machine/drains/x_dets.csv", "puc") == "puc"

    def test_every_node_the_drain_can_fetch_can_also_be_backfilled(self):
        # The roster that forgot `rankine` is the reason its archives could be re-ingested with
        # no identity to check against. Anything the pool has archived must be nameable.
        from tools import hear_drain as D
        assert "rankine" in D.KNOWN_NODES


class TestBackfillingAnArchivedTail:
    """⚠️MEASURED ON THE LIVE POOL, 2026-09-10. Ten of rankine's thirteen archived scene files --
    every one carrying the rows this fix recovers -- were refused ENTIRE by `backfill`, with the
    reader naming half a hex mel string as the file's header. `archive()` stores the body of a
    byte-range GET, so an archived scene.csv begins mid-row; `ingest_scene` refuses that by
    default and must keep refusing it, because a WHOLE file that starts mid-row is corruption.
    The caller says which it has.
    """

    def _tail(self, tmp_path, name="1789057032-scene-20260910.csv"):
        rows = ([_scene_row("rankine", UTC0 + i * 1024000, i) for i in range(3)]
                + [_scene_row(RAW, UTC0 + (10 + i) * 1024000, 50 + i) for i in range(4)])
        whole = "\n".join([",".join(SF.S2.declared)] + rows) + "\n"
        cut = whole.index("\n", whole.index("\n") + 1) - 12       # mid-row, as a tail arrives
        p = tmp_path / name
        p.write_text(whole[cut:])
        return str(p)

    def test_a_tail_is_refused_by_default(self, tmp_path):
        from tools import hear_drain as D
        pl = P.Pool(str(tmp_path / "pool"))
        out = D.backfill(pl, [self._tail(tmp_path)])
        assert "error" in out[0] and "UnknownSchema" in out[0]["error"]

    def test_the_caller_may_assert_it_is_a_tail_and_then_it_reads(self, tmp_path):
        from tools import hear_drain as D
        pl = P.Pool(str(tmp_path / "pool"))
        out = D.backfill(pl, [self._tail(tmp_path)], scene_is_tail=True)
        assert "error" not in out[0]
        assert out[0]["partial_first_line"]                       # the drop is REPORTED
        assert out[0]["aliased"] == {RAW: 4}
        assert out[0]["added"] >= 6                               # all but the dropped fragment
        assert {os.path.basename(f) for d in os.listdir(pl.scene_dir)
                for f in os.listdir(os.path.join(pl.scene_dir, d))} == {"rankine.jsonl.gz"}

    def test_re_ingesting_the_tail_adds_nothing(self, tmp_path):
        from tools import hear_drain as D
        pl = P.Pool(str(tmp_path / "pool"))
        path = self._tail(tmp_path)
        D.backfill(pl, [path], scene_is_tail=True)
        assert D.backfill(pl, [path], scene_is_tail=True)[0]["added"] == 0
