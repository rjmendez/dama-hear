"""The continuous descriptor: its schema generations, and the tail fetch that has no header."""
import binascii
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                        # noqa: E402
from hear import scenefile as SF                                  # noqa: E402


def _mel(bands=20, slices=4, seed=1):
    rng = np.random.default_rng(seed)
    q = rng.integers(-128, 127, size=(bands, slices), dtype=np.int8)
    return q, binascii.hexlify(q.tobytes()).decode()


def _row(gen, utc, node="nyquist", bands=20, slices=4, seed=1, mel=None):
    _q, hexs = _mel(bands, slices, seed)
    if mel is not None:
        hexs = mel
    vals = {
        "node": node, "utc_us": str(utc), "uptime_s": "5158", "sample": "81854464",
        "bands": str(bands), "slices": str(slices), "span_ms": "1024", "ref_db4": "251",
        "frames": "64", "fft_us": "18987", "mel_hex": hexs,
        "f_lo_hz": "300", "f_hi_hz": "7840",
    }
    return ",".join(vals[c] for c in gen.written)


def _csv(gen, rows):
    return "\n".join([",".join(gen.declared)] + rows) + "\n"


class TestGenerations:
    @pytest.mark.parametrize("gen", SF.GENERATIONS)
    def test_each_is_recognised(self, gen):
        assert SF.identify(gen.declared) is gen

    def test_none_of_them_lies_about_its_own_rows(self):
        # ⚠️Unlike dets.csv G3. If a scene generation ever gains this property, the reader must
        # gain the same width dispatch detsfile has -- this test is what would notice.
        assert [g.name for g in SF.GENERATIONS if g.broken_header] == []

    def test_s2_prepends_and_s1_appends_so_neither_end_is_safe_to_assume(self):
        assert SF.S2.declared[0] == "node"
        assert SF.S1.declared[-2:] == ("f_lo_hz", "f_hi_hz")
        assert SF.S0.declared[-1] == "mel_hex"

    def test_an_unknown_header_is_refused(self):
        with pytest.raises(SF.UnknownSchema):
            SF.identify(["when", "what"])

    def test_an_extended_header_is_refused_with_its_extra_columns_named(self):
        with pytest.raises(SF.UnknownSchema, match="rh_pct"):
            SF.identify(list(SF.S2.declared) + ["rh_pct"])

    def test_an_empty_file_is_an_error_not_a_quiet_interval(self):
        with pytest.raises(ValueError, match="empty"):
            SF.read_text("")


class TestDecode:
    def test_the_cells_round_trip_with_their_reference(self):
        q, hexs = _mel()
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1788813341984000, mel=hexs)]))
        d = SF.decode_row(r.rows[0])
        assert np.array_equal(d["q"], q)
        assert d["ref_db"] == 251 / 4.0
        assert d["bands"] == 20 and d["slices"] == 4

    def test_the_second_axis_is_slices_and_frames_is_the_averaging(self):
        # ⚠️`frames` is the ROW's total FFT frame count (firmware SCENE_FRAMES = 4 slices x 16
        # frames = 64); per-slice averaging is frames/slices. Reading it as the shape silently
        # changes what every feature vector means.
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1788813341984000)]))
        d = SF.decode_row(r.rows[0])
        assert d["q"].shape == (d["bands"], d["slices"])
        assert d["frames_summed"] == 64 and d["frames_summed"] != d["slices"]

    def test_frames_is_the_row_total_and_frames_per_slice_derives_the_other(self):
        # ⚠️Firmware writes SCENE_FRAMES = SCENE_SLICES * SCENE_FRAMES_PER_SLICE = 4 * 16 = 64.
        # Reading that column as the per-slice count is wrong by a factor of `slices`, and the
        # wrong number looks entirely plausible.
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1788813341984000)]))
        d = SF.decode_row(r.rows[0])
        assert d["frames_summed"] == 64
        assert SF.frames_per_slice(d) == 16
        assert SF.frames_per_slice(d) * d["slices"] == d["frames_summed"]

    def test_frames_per_slice_reads_a_raw_row_too(self):
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1)]))
        assert SF.frames_per_slice(r.rows[0]) == 16

    def test_frames_per_slice_is_none_when_the_row_cannot_say(self):
        assert SF.frames_per_slice({"slices": 4}) is None
        assert SF.frames_per_slice({"frames_summed": 64}) is None

    def test_a_truncated_mel_is_refused_against_the_rows_own_geometry(self):
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1, mel="dead")]))
        with pytest.raises(ValueError, match="needs 160"):
            SF.decode_row(r.rows[0])

    def test_a_row_with_no_span_is_refused(self):
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1)]))
        r.rows[0]["span_ms"] = ""
        with pytest.raises(ValueError, match="span_ms"):
            SF.decode_row(r.rows[0])

    def test_a_row_with_no_reference_is_refused(self):
        r = SF.read_text(_csv(SF.S2, [_row(SF.S2, 1)]))
        r.rows[0]["ref_db4"] = ""
        with pytest.raises(ValueError, match="ref_db4"):
            SF.decode_row(r.rows[0])


class TestTailFetch:
    """`GET /sd?file=scene.csv&tail=N` starts mid-line and carries no header."""

    def _tail(self, gen, n=5, into=30):
        """A real tail: no header at all, and the cut lands INSIDE the first data row."""
        rows = [_row(gen, 1788813341984000 + i * 1024000, seed=i + 1) for i in range(n)]
        body = _csv(gen, rows)
        cut = len(",".join(gen.declared)) + 1 + into      # past the header, into row 1
        return body[cut:]

    def test_it_infers_the_generation_from_the_row_width(self):
        r = SF.read_text(self._tail(SF.S2), allow_partial_first_line=True)
        assert r.generation is SF.S2
        assert r.partial_first_line is True

    def test_the_leading_fragment_is_dropped_and_the_drop_is_reported(self):
        r = SF.read_text(self._tail(SF.S2, n=5), allow_partial_first_line=True)
        # 5 rows written, the first one truncated: 4 survive, and the discarded fragment is
        # COUNTED rather than merely flagged.
        assert len(r.rows) == 4
        assert r.summary()["partial_first_line"] is True
        assert r.counts == {"partial_first_line": 1}

    def test_a_headerless_body_is_refused_when_partial_is_not_declared(self):
        # ⚠️A whole file that begins mid-row is corruption, not a partial fetch. The two must not
        # read the same, which is why allow_partial_first_line is off by default.
        with pytest.raises(SF.UnknownSchema):
            SF.read_text(self._tail(SF.S2), allow_partial_first_line=False)

    def test_a_tail_large_enough_to_include_the_header_still_reads(self):
        body = _csv(SF.S2, [_row(SF.S2, 1788813341984000 + i * 1024000) for i in range(3)])
        r = SF.read_text(body, allow_partial_first_line=True)
        assert r.partial_first_line is False and len(r.rows) == 3

    def test_a_width_that_matches_no_generation_is_refused(self):
        with pytest.raises(SF.UnknownSchema, match="matches no generation"):
            SF.read_text("x,y\n1,2\n3,4\n", allow_partial_first_line=True)


class TestScenePool:
    def _write(self, tmp_path, name, gen, n, node="nyquist", utc0=1788813341984000, seed0=1):
        p = tmp_path / name
        p.write_text(_csv(gen, [_row(gen, utc0 + i * 1024000, node=node, seed=seed0 + i)
                                for i in range(n)]))
        return str(p)

    def test_scene_lands_in_its_own_store_not_among_the_sketches(self, tmp_path):
        # ⚠️The whole point of a separate store: `records` must keep meaning "gated events".
        pl = P.Pool(str(tmp_path / "pool"))
        e = pl.ingest_scene(self._write(tmp_path, "scene.csv", SF.S2, 10))
        assert e["added"] == 10
        assert pl.stats()["records"] == 0
        assert pl.scene_stats()["rows"] == 10

    def test_the_sketch_summary_does_not_claim_scenes_generations(self, tmp_path):
        # ⚠️One ledger, two stores. stats() must report only what the sketch store ingested.
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(self._write(tmp_path, "scene.csv", SF.S2, 5))
        s = pl.stats()
        assert s["generations"] == []
        assert s["ingests"] == 0
        assert s["skipped_at_ingest"] == {}

    def test_ingest_is_idempotent(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = self._write(tmp_path, "scene.csv", SF.S2, 20)
        assert pl.ingest_scene(f)["added"] == 20
        second = pl.ingest_scene(f)
        assert second["added"] == 0 and second["duplicate"] == 20

    def test_an_overlapping_tail_adds_only_the_new_rows(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(self._write(tmp_path, "a.csv", SF.S2, 20))
        # same 20 rows plus 5 more, which is what the next tail fetch looks like
        assert pl.ingest_scene(self._write(tmp_path, "b.csv", SF.S2, 25))["added"] == 5
        assert pl.scene_stats()["rows"] == 25

    def test_a_fresh_pool_object_still_deduplicates(self, tmp_path):
        root = str(tmp_path / "pool")
        f = self._write(tmp_path, "scene.csv", SF.S2, 12)
        P.Pool(root).ingest_scene(f)
        assert P.Pool(root).ingest_scene(f)["added"] == 0

    def test_the_arithmetic_closes(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        good = _row(SF.S2, 1788813341984000)
        bad = _row(SF.S2, 1788813342984000, mel="dead")
        p = tmp_path / "mixed.csv"
        p.write_text(_csv(SF.S2, [good, bad]))
        e = pl.ingest_scene(str(p))
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]
        assert e["added"] == 1 and e["skipped"] == 1

    def test_two_nodes_stay_separate(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_scene(self._write(tmp_path, "n.csv", SF.S2, 5, node="nyquist", seed0=1))
        pl.ingest_scene(self._write(tmp_path, "m.csv", SF.S2, 7, node="mach", seed0=50))
        assert pl.scene_stats()["by_node"] == {"nyquist": 5, "mach": 7}

    def test_an_unanchored_row_is_kept_and_marked(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        p = tmp_path / "u.csv"
        p.write_text(_csv(SF.S2, [_row(SF.S2, 0)]))
        assert pl.ingest_scene(str(p))["added"] == 1
        assert pl.scene_stats()["unanchored"] == 1

    def test_the_matrix_refuses_mixed_geometry(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        a = tmp_path / "a.csv"
        a.write_text(_csv(SF.S2, [_row(SF.S2, 1788813341984000, bands=20, slices=4)]))
        b = tmp_path / "b.csv"
        b.write_text(_csv(SF.S2, [_row(SF.S2, 1788813343984000, bands=16, slices=4, seed=9)]))
        pl.ingest_scene(str(a))
        pl.ingest_scene(str(b))
        with pytest.raises(ValueError, match="mixed scene geometry"):
            pl.scene_matrix()

    def test_the_matrix_restores_the_reference_level(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        q, hexs = _mel()
        p = tmp_path / "s.csv"
        p.write_text(_csv(SF.S2, [_row(SF.S2, 1788813341984000, mel=hexs)]))
        pl.ingest_scene(str(p))
        X, rows = pl.scene_matrix(mode="db")
        assert X.shape == (1, 20 * 4)
        assert np.allclose(X[0], (q.astype(float) / 2.0 + 251 / 4.0).reshape(-1))
