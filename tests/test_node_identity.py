"""A row may not enter the pool under a node name the fetch contradicts.

⚠️THIS TEST EXISTS BECAUSE THE 2026-09-07 DRAIN MIS-FILED 276 ROWS. `mach/scene.csv` is one file
off one card, written across 15 boot sessions; rows 2610-2885 of it -- one complete boot,
uptime 14->298 s, sample 0 to 275*16384, every row `utc_us == 0` -- carry `node` = "nyquist".
They were recorded by whatever hardware stood at mach's position: the block's floor is 38.50 dB
and its quiet-time band spread 0.75 dB, inside mach's other fourteen sessions (36.75-39.00 dB,
0.50-1.00 dB) and nowhere near nyquist's four (23.75-26.25 dB, 1.50-16.50 dB). `node` is NODE_ID,
a compile-time #define, so the identity changed because the BINARY did.

`ingest_scene` bucketed on the row's own label, so those 276 rows landed in
`scene/unanchored/nyquist.jsonl.gz`. The whole drain holds exactly ONE legitimately unanchored
nyquist row, so that partition was 99.6% another node's microphone -- and `scene()` walks
`unanchored` by default while `scene_matrix()` has no switch for it.

Four things looked like this guard and were not, and each has a test below: `origin` reached only
the ledger, `default_node` is consulted by the READER only when the node cell is empty (so it is
structurally unreachable for a row that carries one), `node_from` is read by nothing, and the
ledger's `rows == added + duplicate + skipped` still closed because the rows were MIS-ROUTED
rather than dropped -- the loss a conservation check cannot see.
"""
import binascii
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import detsfile as DF                                   # noqa: E402
from hear import pool as P                                        # noqa: E402
from hear import scenefile as SF                                  # noqa: E402
from hear import sketch as SK                                     # noqa: E402

UTC0 = 1788813341984000


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


def _mach_card(tmp_path, name="scene.csv"):
    """The drained file's shape: mach rows, one unanchored boot labelled `nyquist`, mach rows."""
    rows = ([_scene_row("mach", UTC0 + i * 1024000, i) for i in range(6)]
            + [_scene_row("nyquist", 0, 100 + i) for i in range(3)]
            + [_scene_row("mach", UTC0 + (10 + i) * 1024000, 200 + i) for i in range(6)])
    return _scene_csv(tmp_path / name, rows)


def _dets_row(node, utc, seed):
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), 16000.0)
    frame = binascii.hexlify(SK.pack(597174, ref, 1140, q, fs=16000.0)).decode()
    return "%s,%d,14,%d,3,1200,1,2,16000.000,%d,%s,," % (node, utc, seed * 16384, 736, frame)


class TestSceneIngest:
    def test_a_row_the_fetch_contradicts_is_refused_and_counted(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(_mach_card(tmp_path), default_node="mach",
                            origin="mach:/scene.csv")
        assert e["added"] == 12
        assert e["skipped"] == 3
        assert e["skip_reasons"]["node_mismatch"] == 3
        # The names, so the ledger says a NODE IS FLASHED WRONG and not merely that three rows
        # were dropped. One reason key, one name field: a garbage file cannot grow the breakdown.
        assert e["node_mismatch"] == {"nyquist": 3}

    def test_the_conservation_assertion_still_closes(self, tmp_path):
        # ⚠️It closed before this guard too -- mis-routed rows are counted rows. It has to keep
        # closing for a reason now, which is that the refusal lands in `skipped`.
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(_mach_card(tmp_path), default_node="mach")
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]
        assert sum(e["skip_reasons"].values()) == e["skipped"]

    def test_nothing_lands_in_the_other_nodes_partition(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_mach_card(tmp_path), default_node="mach")
        assert list(pl.scene(node="nyquist")) == []
        # and specifically not in `unanchored`, which is where all 276 went and which `scene()`
        # walks by default.
        for day in os.listdir(pl.scene_dir):
            assert not os.path.exists(os.path.join(pl.scene_dir, day, "nyquist.jsonl.gz"))

    def test_the_rows_that_agree_still_land(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_mach_card(tmp_path), default_node="mach")
        got = list(pl.scene(node="mach"))
        assert len(got) == 12
        assert {r["node"] for r in got} == {"mach"}

    def test_a_file_the_fetch_cannot_place_is_left_alone(self, tmp_path):
        """⚠️THE GUARD IS NOT FREE. With no `default_node` there is nothing to contradict, so the
        rows file under their own labels exactly as before -- which is what happened to the
        drain's 276. `tools/hear_drain.py` knows the node it fetched from and must keep passing
        it; an ingest that omits it is unguarded by construction."""
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(_mach_card(tmp_path))
        assert e["added"] == 15 and e["skipped"] == 0
        assert len(list(pl.scene(node="nyquist"))) == 3

    def test_default_node_in_the_reader_cannot_do_this_job(self, tmp_path):
        """Trap (b), pinned. `scenefile.read_text` consults `default_node` only when the node cell
        is EMPTY, so for an S2 row -- which always carries one -- the argument is unreachable. A
        fix applied there would have looked right and changed nothing."""
        text = open(_mach_card(tmp_path)).read()
        read = SF.read_text(text, default_node="mach")
        assert [r["node"] for r in read.rows].count("nyquist") == 3
        assert {r["node_from"] for r in read.rows} == {"file"}

    def test_the_refusal_does_not_relabel(self, tmp_path):
        """Rewriting the label to the fetch's name would file the rows correctly and destroy the
        only evidence that a node was flashed with another node's secrets.h. Nothing carries the
        contradicted name into the store."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(_mach_card(tmp_path), default_node="mach")
        assert len(list(pl.scene())) == 12


class TestDetsIngest:
    def test_the_same_guard_covers_dets(self, tmp_path):
        """dets.csv escaped this only because the G3 generation never wrote its `node` column.
        G4/G5 do, so the file that mis-filed scene rows would mis-file detections next."""
        p = tmp_path / "dets.csv"
        rows = [_dets_row("mach", UTC0 + i * 1000000, i) for i in range(4)]
        rows.insert(2, _dets_row("nyquist", 0, 77))
        p.write_text("\n".join([",".join(DF.G5.declared)] + rows) + "\n")
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_dets(str(p), default_node="mach", origin="mach:/dets.csv")
        assert e["added"] == 4
        assert e["node_mismatch"] == {"nyquist": 1}
        assert e["skip_reasons"]["node_mismatch"] == 1
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]
        assert {r.node_id for r in pl.records()} == {"mach"}


class TestWhatItDoesNotClaim:
    def test_it_does_not_say_which_BOARD_recorded_the_rows(self):
        """⚠️MEASURED vs INFERRED, kept apart on purpose. What the drain's floor statistics show
        is that the 276 rows carry MACH'S ACOUSTIC SIGNATURE -- 38.50 dB floor, 0.75 dB spread.
        Whether that signature follows the BOARD or the SITE is not established (it needs the
        board swap in docs/, not a statistic), so "the mach device recorded them" is an inference.
        The guard does not depend on it either way: under both readings the rows are not
        nyquist's microphone, and that is the whole of what it acts on.
        """
        assert P._node_mismatch({"node": "nyquist", "node_from": "file"}, "mach") == "nyquist"
        assert P._node_mismatch({"node": "mach", "node_from": "file"}, "mach") is None
        # the name came from the argument itself: there are not two claims to compare
        assert P._node_mismatch({"node": "mach", "node_from": "argument"}, "mach") is None
        assert P._node_mismatch({"node": "nyquist", "node_from": "file"}, None) is None
