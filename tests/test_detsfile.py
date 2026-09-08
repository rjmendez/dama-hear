"""The five dets.csv generations, and the one whose header lies about its own rows."""
import binascii

import numpy as np
import pytest

from hear import detsfile as DF
from hear import sketch as SK


def _frame_hex(fs=16000.0, seed=1):
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), fs)
    return binascii.hexlify(SK.pack(597174, ref, 1140, q, fs=fs)).decode()


# The eight fields every generation shares, in order.
_BODY = "1788763952189911,1234,5000000,42,597174,1140,4608,16000.000"


def _csv(header, rows):
    return "\n".join([",".join(header)] + rows) + "\n"


class TestIdentify:
    @pytest.mark.parametrize("gen", DF.GENERATIONS)
    def test_every_declared_generation_is_recognised(self, gen):
        assert DF.identify(gen.declared) is gen

    def test_only_g3_declares_a_header_it_does_not_write(self):
        broken = [g for g in DF.GENERATIONS if g.broken_header]
        assert [g.name for g in broken] == ["G3"]
        assert DF.G3.declared[0] == "node" and "node" not in DF.G3.written

    def test_g3_and_g4_are_told_apart_by_the_first_column_alone(self):
        # The firmware renamed `node` -> `node_id` precisely to force the file to roll, so the
        # header change and the writer fix cannot be separated. If this ever stops holding, a G3
        # file becomes indistinguishable from a G4 one and every value shifts a column.
        assert DF.G3.declared[0] == "node"
        assert DF.G4.declared[0] == "node_id"
        assert len(DF.G3.declared) == len(DF.G4.declared)

    def test_an_unknown_header_is_refused_not_guessed(self):
        with pytest.raises(DF.UnknownSchema):
            DF.identify(["when", "what", "frame_hex"])

    def test_an_extended_known_header_is_refused_with_its_extra_columns_named(self):
        # G5 inserted a column BEFORE frame_hex. So "starts with a known generation" does not
        # imply the frame is where that generation puts it, and extension cannot be waved through.
        with pytest.raises(DF.UnknownSchema, match="temp_c"):
            DF.identify(list(DF.G5.declared) + ["temp_c"])


class TestG3:
    """The generation that cost 730 detections."""

    def test_a_header_named_read_would_shift_every_value_but_this_reader_does_not(self):
        fh = _frame_hex()
        text = _csv(DF.G3.declared, ["%s,%s,clip.wav,gate" % (_BODY, fh)])

        import csv as _csv_mod
        import io
        naive = next(_csv_mod.DictReader(io.StringIO(text)))
        # What DictReader does with it: the frame lands under `clip`, and `frame_hex` gets a path.
        assert naive["frame_hex"] == "clip.wav"
        # ⚠️And `utc_us` reads 1234 -- the UPTIME. Not a malformed value that would trip a
        # parser, a perfectly plausible small integer, which is why this went unnoticed.
        assert naive["utc_us"] == "1234"

        got = DF.read_text(text, default_node="nyquist")
        assert got.generation is DF.G3
        assert len(got.rows) == 1 and not got.skips
        assert got.rows[0]["frame_hex"] == fh
        assert got.rows[0]["clip"] == "clip.wav"

    def test_it_takes_its_node_from_the_argument_and_says_so(self):
        text = _csv(DF.G3.declared, ["%s,%s,," % (_BODY, _frame_hex())])
        r = DF.read_text(text, default_node="mach")
        assert r.rows[0]["node"] == "mach"
        assert r.rows[0]["node_from"] == "argument"

    def test_without_a_node_the_row_is_refused_and_counted(self):
        text = _csv(DF.G3.declared, ["%s,%s,," % (_BODY, _frame_hex())])
        r = DF.read_text(text)
        assert r.rows == []
        assert r.counts == {"no_node": 1}


class TestG5:
    def test_the_node_comes_from_the_file_and_sketch_back_survives(self):
        header = DF.G5.declared
        row = "mach,%s,64,%s,," % (_BODY, _frame_hex())
        r = DF.read_text(_csv(header, [row]))
        assert r.generation is DF.G5
        assert r.rows[0]["node"] == "mach" and r.rows[0]["node_from"] == "file"
        assert r.rows[0]["sketch_back"] == "64"

    def test_earlier_generations_state_no_window_rather_than_a_default(self):
        # ⚠️None means "this file cannot say". Filling in the pre-fix 736 would put a number the
        # firmware never wrote into rows that predate the column.
        r = DF.read_text(_csv(DF.G4.declared, ["nyquist,%s,%s,," % (_BODY, _frame_hex())]))
        assert r.rows[0]["sketch_back"] is None


class TestRefusals:
    def test_an_empty_file_is_an_error_not_a_quiet_night(self):
        with pytest.raises(ValueError, match="empty"):
            DF.read_text("")

    def test_a_short_row_is_counted_with_its_width_not_silently_dropped(self):
        text = _csv(DF.G5.declared, ["mach,%s,64,%s" % (_BODY, _frame_hex())])
        r = DF.read_text(text)
        assert r.rows == []
        assert list(r.counts) == ["row_width_11_expected_13"]

    def test_a_truncated_frame_is_counted_by_its_length(self):
        text = _csv(DF.G5.declared, ["mach,%s,64,dead,," % _BODY])
        r = DF.read_text(text)
        assert r.counts == {"frame_hex_len_4": 1}

    def test_a_blank_frame_column_is_its_own_reason(self):
        text = _csv(DF.G5.declared, ["mach,%s,64,,," % _BODY])
        assert DF.read_text(text).counts == {"no_frame": 1}

    def test_the_summary_reports_what_was_dropped(self):
        text = _csv(DF.G5.declared,
                    ["mach,%s,64,%s,," % (_BODY, _frame_hex()),
                     "mach,%s,64,dead,," % _BODY])
        s = DF.read_text(text).summary()
        assert s == {"generation": "G5", "rows": 1, "skipped": 1,
                     "reasons": {"frame_hex_len_4": 1}}
