"""v2 frame: what it adds over v1, what it costs, and what it refuses to guess at."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as SK   # noqa: E402
from hear import wire as WR     # noqa: E402

DAY0 = 20000 * 86400.0          # an epoch-aligned midnight, so the rollover cases are exact


def _q(bands=20, frames=8):
    return (np.arange(bands * frames, dtype=int).reshape(bands, frames) % 200 - 100).astype(np.int8)


class TestRoundTrip:
    def test_every_header_field_survives_the_wire(self):
        f = WR.pack_v2(us_of_day=12_345_678_901, node_id=4242, seq=200,
                       ref_db=-13.75, peak=31000, q=_q(), retrigger=True)
        got = WR.unpack_v2(f)
        assert got["us_of_day"] == 12_345_678_901
        assert got["node_id"] == 4242
        assert got["seq"] == 200
        assert got["peak"] == 31000
        assert got["retrigger"] is True
        assert got["profile_id"] == 0
        assert (got["bands"], got["frames"]) == (20, 8)
        assert got["ref_db"] == pytest.approx(-13.75, abs=0.25)   # 0.25 dB header quantisation
        assert np.array_equal(got["q"], _q())

    def test_the_five_flag_subfields_do_not_bleed_into_each_other(self):
        """seq, version, profile and retrigger share one 16-bit word with zero spare bits; an
        off-by-one shift shows up nowhere else."""
        for seq in (0, 1, 127, 128, 255):
            for retrig in (False, True):
                got = WR.unpack_v2(WR.pack_v2(1, 1, seq, 0.0, 0, _q(), retrigger=retrig))
                assert (got["seq"], got["retrigger"], got["profile_id"], got["version"]) \
                    == (seq, retrig, 0, 2)

    def test_db_is_recoverable_the_same_way_v1_recovers_it(self):
        q, ref = SK.sketch(np.random.RandomState(0).normal(0, 300, 4096), 48000.0)
        got = WR.unpack_v2(WR.pack_v2(0, 1, 0, ref, 1, q))
        assert got["db"].max() == pytest.approx(ref, abs=0.5)


class TestSizeBudget:
    def test_v2_costs_one_byte_over_v1_and_still_fits(self):
        assert WR.wire_size_v2(0) == 173
        assert WR.wire_size_v2(0) == SK.wire_size() + 1
        assert WR.fits_meshtastic_v2()
        assert WR.wire_size_v2(0) <= WR.MESHTASTIC_USABLE

    def test_v1_is_untouched(self):
        """The whole point of a separate module. If this fails, sketch.py was edited."""
        assert SK.wire_size() == 172
        q, ref = SK.sketch(np.random.RandomState(1).normal(0, 300, 4096), 48000.0)
        got = SK.unpack(SK.pack(999_999, ref, 31000, q, flags=3))
        assert got["node_us"] == 999_999 and got["flags"] == 3
        assert np.array_equal(got["q"], q)

    def test_profile_zero_is_frozen_against_the_sketch_constants(self):
        """If this fails someone edited MEL_BANDS or FRAMES. Add profile 1; profile 0 is the
        wire meaning of every already-deployed frame and cannot be reinterpreted."""
        assert WR.PROFILES[0] == (SK.MEL_BANDS, SK.FRAMES)
        assert WR.PROFILES[0] == (20, 8)

    def test_an_unlisted_geometry_is_refused_not_invented(self):
        with pytest.raises(ValueError, match="no profile for 24x8"):
            WR.profile_for(24, 8)
        with pytest.raises(ValueError, match="unknown profile id 7"):
            WR.profile_shape(7)


class TestTimestampRefusals:
    @pytest.mark.parametrize("us", [WR.US_PER_DAY, WR.US_PER_DAY + 1, -1, 2 ** 40 - 1])
    def test_an_out_of_range_timestamp_raises_and_is_never_masked(self, us):
        """sketch.py:91 masks with & 0xFFFFFFFF. A silently wrapped timestamp is a 343 m error
        that nothing downstream can detect."""
        with pytest.raises(ValueError, match="us_of_day"):
            WR.pack_v2(us, 1, 0, 0.0, 0, _q())

    def test_the_top_three_ts_bits_are_a_hard_invariant(self):
        b = bytearray(WR.pack_v2(1_000_000, 1, 0, 0.0, 0, _q()))
        b[4] |= 0x20                                        # bit 37
        with pytest.raises(ValueError, match="bits 37-39"):
            WR.unpack_v2(bytes(b))

    @pytest.mark.parametrize("n", [172, 174])
    def test_a_truncated_or_padded_frame_raises_rather_than_reshaping_garbage(self, n):
        f = WR.pack_v2(1, 1, 0, 0.0, 0, _q())
        b = f[:n] if n < len(f) else f + b"\x00" * (n - len(f))
        with pytest.raises(ValueError):
            WR.unpack_v2(b)

    def test_out_of_range_node_id_and_seq_raise(self):
        with pytest.raises(ValueError, match="node_id"):
            WR.pack_v2(0, 0x10000, 0, 0.0, 0, _q())
        with pytest.raises(ValueError, match="seq"):
            WR.pack_v2(0, 0, 256, 0.0, 0, _q())


class TestTheSubSecondTrap:
    def test_a_v1_sub_second_value_is_a_legal_time_but_never_the_same_key(self):
        """999999 is 00:00:00.999999 as us-of-day and 0.999999 s past an unnamed PPS second as
        v1 node_us. The values collide, the meanings do not, so the names must not either."""
        v2 = WR.unpack_v2(WR.pack_v2(999_999, 3, 1, -6.0, 100, _q()))
        assert v2["us_of_day"] == 999_999
        assert "node_us" not in v2

        v1 = SK.unpack(SK.pack(999_999, -6.0, 100, _q()))
        assert v1["node_us"] == 999_999
        assert "us_of_day" not in v1


class TestVersionDiscrimination:
    def test_a_real_v1_frame_decodes_as_v1_and_keeps_node_us(self):
        f = SK.pack(999_999, -6.0, 100, _q())
        assert WR.version_of(f) == 1
        d = WR.decode(f)
        assert d["version"] == 1 and d["node_us"] == 999_999
        assert "us_of_day" not in d

    def test_a_real_v2_frame_decodes_as_v2_and_keeps_us_of_day(self):
        f = WR.pack_v2(999_999, 3, 1, -6.0, 100, _q())
        assert WR.version_of(f) == 2
        d = WR.decode(f)
        assert d["version"] == 2 and d["us_of_day"] == 999_999 and d["node_id"] == 3
        assert "node_us" not in d

    def test_nonsense_is_refused_not_assigned_a_version(self):
        for b in (b"", b"\x00" * 50, b"\xff" * 173):
            with pytest.raises(ValueError, match="unrecognised frame"):
                WR.version_of(b)


class TestMidnightRollover:
    def test_two_events_across_midnight_are_02_seconds_apart_not_86399(self):
        """Each frame unwraps against its OWN receive time. Pinning the day from a single shared
        reference turns a 0.2 s straddle into a whole-day error, which no residual would catch."""
        t_a = DAY0 + 86399.9
        t_b = DAY0 + 86400.1
        a = WR.unwrap_utc(WR.us_of_day_from_utc(t_a), t_a + 0.05)
        b = WR.unwrap_utc(WR.us_of_day_from_utc(t_b), t_b + 0.05)
        assert b - a == pytest.approx(0.2, abs=1e-5)

    def test_a_shared_reference_on_the_wrong_side_of_midnight_is_what_breaks(self):
        # the counter-case that gives the test above teeth: same us-of-day, references 0.2 s
        # apart across midnight, and the answers land on different days by construction
        t_a, t_b = DAY0 + 86399.9, DAY0 + 86400.1
        assert WR.unwrap_utc(WR.us_of_day_from_utc(t_a), t_b) == pytest.approx(t_a, abs=1e-5)
        assert WR.unwrap_utc(WR.us_of_day_from_utc(t_b), t_a) == pytest.approx(t_b, abs=1e-5)

    @pytest.mark.parametrize("frac", [0.0, 0.25, 0.5, 0.75, 0.999999,
                                      86399.999999 / 86400.0, 1e-6 / 86400.0])
    def test_round_trip_through_us_of_day_is_lossless_to_a_microsecond(self, frac):
        t = DAY0 + frac * 86400.0
        assert WR.unwrap_utc(WR.us_of_day_from_utc(t), t) == pytest.approx(t, abs=1e-6)

    def test_a_receive_time_a_whole_day_out_shifts_every_node_equally(self):
        """It cancels in TDoA. This is why the midnight rule is 12 h and not stricter."""
        t = DAY0 + 43200.0
        us = WR.us_of_day_from_utc(t)
        assert WR.unwrap_utc(us, t + 86400.0) - WR.unwrap_utc(us, t) == pytest.approx(86400.0)
