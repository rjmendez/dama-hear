"""v2 frame: what it adds over v1, what it costs, and what it refuses to guess at."""
import os
import struct
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
        # ⚠️the DEFAULT changed from 0 to DEFAULT_PROFILE: pack_v2 no longer derives the id from
        # q.shape, because a shape cannot say what rate or band layout produced it.
        assert got["profile_id"] == WR.DEFAULT_PROFILE
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
                    == (seq, retrig, WR.DEFAULT_PROFILE, 2)

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


class TestTheHeaderIsPinnedToBytesNotToItself:
    """A round trip through this module's own pack/unpack passes for ANY self-consistent layout.
    firmware/hear_poc/hear_core.h is an independent C implementation of the v1 header and
    hear_poc.ino compares 12 header bytes byte-exact against a golden frame; v2 gets the same
    treatment here, or a v2 firmware author reading the docstring emits frames Python misreads."""

    # 13 B for us_of_day=12345678901, ref_db=-13.75, peak=0x1234, node_id=0xBEEF, seq=200,
    # profile 0, retrigger. ts LE uint40 | ref4 <h> | peak <H> | node_id <H> | flags <H>.
    GOLDEN = bytes.fromhex("351cdcdf02" "c9ff" "3412" "efbe" "41c8")

    def _frame(self, profile_id=0):
        # ⚠️profile 0 EXPLICITLY. It used to be what pack_v2 derived from the shape; the default
        # is now DEFAULT_PROFILE (1, 48 kHz, fixed layout) because a frame must state its rate.
        # These tests pin the header BYTES, so they name the profile they pin rather than
        # inheriting whichever one is currently default.
        return WR.pack_v2(us_of_day=12_345_678_901, node_id=0xBEEF, seq=200, ref_db=-13.75,
                          peak=0x1234, q=_q(), retrigger=True, profile_id=profile_id)

    def test_the_header_bytes_are_exactly_these(self):
        h = self._frame()[:WR.HDR_V2]
        assert h == self.GOLDEN, "v2 header moved: %s" % h.hex(" ")
        # field by field, so a failure says WHICH field moved
        assert h[0:5] == (12_345_678_901).to_bytes(5, "little")
        assert h[5:7] == (-55).to_bytes(2, "little", signed=True)   # -13.75 dB at 0.25 dB
        assert h[7:9] == b"\x34\x12"                                # peak, not node_id
        assert h[9:11] == b"\xef\xbe"                               # node_id, not peak
        assert h[11:13] == (0xC841).to_bytes(2, "little")           # seq 200|ver 2<<5|retrig

    def test_peak_and_node_id_are_not_interchangeable(self):
        """Both are <H> and adjacent. Swapping them in pack AND unpack round-trips perfectly."""
        got = WR.unpack_v2(self._frame())
        assert (got["peak"], got["node_id"]) == (0x1234, 0xBEEF)
        assert struct.unpack_from("<H", self._frame(), 7)[0] == 0x1234

    def test_the_profile_field_starts_at_bit_1(self):
        """Force bit 1 on a profile-0 frame: if the field starts there, the id reads as 1.

        ⚠️This used to assert the frame was REFUSED, because profile 1 did not exist. It does now
        (20x8 at 48 kHz, fixed layout), so the same bit flip is observed by what it decodes to
        instead -- a stronger check, since it reads the field rather than only its absence."""
        b = bytearray(self._frame(profile_id=0))
        b[11] |= 0x02                                   # bit 1 -> profile 1 if the field is there
        got = WR.unpack_v2(bytes(b))
        assert got["profile_id"] == 1
        assert WR.profile_geometry(got["profile_id"]).fs_hz == 48000.0
        # and an id nothing defines is still refused, from the same base
        b2 = bytearray(self._frame(profile_id=0))
        b2[11] |= 0x08                                  # bit 3 -> profile 4
        with pytest.raises(ValueError, match="unknown profile id 4"):
            WR.unpack_v2(bytes(b2))

    def test_the_profile_field_is_4_bits_and_stops_below_the_version_field(self):
        """Profile ids 8-15 use bit 4. If profile and version overlap, an id in that half is read
        as a version bump and the frame is rejected for the wrong reason -- or accepted."""
        b = bytearray(self._frame(profile_id=0))        # from a ZERO base, so bit 4 alone is id 8
        b[11] |= 0x10                                   # bit 4 -> profile 8, version still 2
        with pytest.raises(ValueError, match="unknown profile id 8"):
            WR.unpack_v2(bytes(b))


class TestTheReceiveSideChecksTheTimestampToo:
    def test_a_forged_us_of_day_past_midnight_is_refused_on_unpack(self):
        """37 bits hold 137438953471 us; a day is 86400000000. Everything between is a legal
        uint37 and an illegal time, so the bits-37-39 check cannot see it. Without the range
        check this decodes to a wrong absolute second and nothing downstream can tell."""
        forged = 100_000_000_000
        assert forged < 2 ** 37 and forged > WR.US_PER_DAY
        b = bytearray(WR.pack_v2(1_000_000, 1, 0, 0.0, 0, _q()))
        b[0:5] = forged.to_bytes(5, "little")
        b = bytes(b)

        assert WR.version_of(b) == 2                    # the bad frame reaches unpack_v2
        with pytest.raises(ValueError, match="us_of_day 100000000000"):
            WR.unpack_v2(b)
        with pytest.raises(ValueError, match="us_of_day"):
            WR.decode(b)

    def test_the_wrong_time_it_would_otherwise_produce(self):
        """The cost of not checking, in seconds: 100000000000 us unwrapped against DAY0 is
        13600 s past the reference. That is the mislabel the module docstring refuses."""
        assert WR.unwrap_utc(100_000_000_000, DAY0) - DAY0 == pytest.approx(13600.0, abs=1e-6)

    @pytest.mark.parametrize("us", [WR.US_PER_DAY, WR.US_PER_DAY + 1, 2 ** 37 - 1])
    def test_every_value_in_the_illegal_band_is_refused(self, us):
        b = bytearray(WR.pack_v2(1_000_000, 1, 0, 0.0, 0, _q()))
        b[0:5] = int(us).to_bytes(5, "little")
        with pytest.raises(ValueError, match="us_of_day"):
            WR.unpack_v2(bytes(b))

    def test_the_last_legal_microsecond_of_the_day_still_decodes(self):
        """The counter-case: an off-by-one in the guard would eat 23:59:59.999999."""
        last = WR.US_PER_DAY - 1
        assert WR.unpack_v2(WR.pack_v2(last, 1, 0, 0.0, 0, _q()))["us_of_day"] == last


class TestPeakIsRefusedNotClamped:
    """v1 clamps with min(int(peak), 0xFFFF) (sketch.py:92). A clamped peak is a silent lie about
    the one field a clipping check reads, and it is the same failure class as the masked
    timestamp -- so v2 refuses it, and every refusal is a ValueError."""

    def test_a_peak_over_the_field_raises_instead_of_clamping(self):
        with pytest.raises(ValueError, match="peak 70000"):
            WR.pack_v2(0, 1, 0, 0.0, 70_000, _q())

    def test_a_negative_peak_raises_a_valueerror_not_a_struct_error(self):
        with pytest.raises(ValueError, match="peak -1"):
            WR.pack_v2(0, 1, 0, 0.0, -1, _q())

    def test_the_boundary_value_is_carried_exactly(self):
        assert WR.unpack_v2(WR.pack_v2(0, 1, 0, 0.0, 0xFFFF, _q()))["peak"] == 0xFFFF

    def test_an_absurd_ref_db_raises_a_valueerror_not_a_struct_error(self):
        with pytest.raises(ValueError, match="ref_db"):
            WR.pack_v2(0, 1, 0, 1e6, 0, _q())

    def test_a_one_dimensional_sketch_raises_a_valueerror_not_an_indexerror(self):
        with pytest.raises(ValueError, match="1-D"):
            WR.pack_v2(0, 1, 0, 0.0, 0, np.zeros(160, dtype=np.int8))

    def test_every_refusal_this_module_makes_is_catchable_as_one_type(self):
        """The contract the module docstring claims: one except clause covers pack_v2."""
        bad = [dict(us_of_day=WR.US_PER_DAY), dict(node_id=0x10000), dict(seq=256),
               dict(peak=70_000), dict(peak=-1), dict(ref_db=1e6),
               dict(q=np.zeros(160, dtype=np.int8)), dict(q=_q(24, 8)), dict(profile_id=7)]
        base = dict(us_of_day=0, node_id=1, seq=0, ref_db=0.0, peak=0, q=_q())
        for override in bad:
            with pytest.raises(ValueError):
                WR.pack_v2(**{**base, **override})
