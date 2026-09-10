"""What may be reconstructed from an unanchored detection, and what must be refused.

The numbers in `hear/unanchored.py`'s docstring were measured on 2026-09-10 against the three
live cards and /pool/corpus. These tests pin the ARITHMETIC and the REFUSALS, because both are
what stop 517 rows being "recovered" with a back-projection that is wrong by a millisecond or,
worse, by a whole second.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import unanchored as U                                    # noqa: E402

#: An arbitrary but real-shaped epoch. Nothing depends on its value.
T0 = 1_789_000_000_000_000


def _row(uptime_s, pps_n, us_since_pps, utc_us=0, **kw):
    """One dets.csv row as `hear.detsfile` yields it: every field a string."""
    r = {"node": "mach", "uptime_s": str(uptime_s), "pps_n": str(pps_n),
         "us_since_pps": str(us_since_pps), "utc_us": str(utc_us), "sample": "0"}
    r.update({k: str(v) for k, v in kw.items()})
    return r


def _anchored(uptime_s, pps_n, us_since_pps, edge0_pps=0, edge0_utc=T0):
    """An anchored row on a perfect edge counter: edge k is at edge0_utc + (k - edge0_pps) s."""
    edge = edge0_utc + (pps_n - edge0_pps) * 1_000_000
    return _row(uptime_s, pps_n, us_since_pps, utc_us=edge + us_since_pps)


# ---------------------------------------------------------------- the arithmetic


class TestBracketedRecovery:
    def test_a_row_between_two_agreeing_anchors_gets_its_edge_back(self):
        """This is the whole claim: `utc_us == 0` says the node could not NAME the edge, not that
        it did not know WHICH edge. pps_n and us_since_pps place the row exactly."""
        rows = [_anchored(100, 100, 12_345),
                _row(140, 140, 500_000),
                _anchored(180, 180, 7_000)]
        got = U.recover_boot(rows)
        assert [r.index for r in got.recovered] == [1]
        assert got.recovered[0].utc_us == T0 + 140 * 1_000_000 + 500_000
        assert got.recovered[0].residual_us == 0
        assert got.recovered[0].span_s == 80
        assert got.refused == []

    def test_a_negative_offset_is_a_subtraction_not_a_magnitude(self):
        """⚠️us_since_pps IS SIGNED. A sample back-dated across an edge belongs to the previous
        second and the firmware decrements pps_n for it. Treating the field as unsigned would put
        the event 999 ms -- 343 m -- from where it happened."""
        rows = [_anchored(100, 100, 1_000),
                _row(140, 140, -400),
                _anchored(180, 180, 1_000)]
        got = U.recover_boot(rows)
        assert got.recovered[0].utc_us == T0 + 140 * 1_000_000 - 400

    def test_the_residual_is_reported_so_a_caller_can_be_stricter_than_the_gate(self):
        """EDGE_TOL_US is a refusal threshold, not an accuracy claim. The measured p99 of the
        real residuals is 91 us; a consumer whose budget is tighter than the gate filters on the
        field rather than lowering the constant for everyone."""
        rows = [_anchored(100, 100, 0),
                _row(140, 140, 0),
                _row(180, 180, 0, utc_us=T0 + 180 * 1_000_000 + 900)]   # 900 us late
        got = U.recover_boot(rows)
        assert got.recovered[0].residual_us == 900


class TestRefusals:
    def test_a_pre_lock_row_is_refused_even_though_anchors_follow_it(self):
        """⚠️THE ONE THAT MATTERS: 167 of the 169 unanchored rows on the live cards are here.
        Before the first label the timepulse free-runs on the module's own oscillator. In mach's
        archived health rows the ESP-vs-PPS figure reads 10.34-10.63 ppm through the unlocked
        window and 9.66 ppm once locked -- ~1 ppm, itself drifting -- so extrapolating back across
        1016 unlocked seconds accumulates about a millisecond, ~35 cm of sound, BEFORE the
        unbounded phase step the module takes when it switches to the locked timebase. Nothing in
        the record bounds that step, so the row is refused rather than given a plausible number."""
        rows = [_row(34, 31, 600_000),
                _row(84, 81, 500_000),
                _anchored(1088, 1086, 5_455),
                _anchored(1165, 1162, 540_593)]
        got = U.recover_boot(rows)
        assert got.recovered == []
        assert [r.reason for r in got.refused] == ["pre_lock", "pre_lock"]

    def test_a_lost_edge_refuses_the_row_instead_of_moving_it_343_metres(self):
        """⚠️pps_n IS NOT A GLOBALLY RELIABLE EDGE INDEX. 34 of 5175 adjacent anchored pairs on
        the live cards disagree by a near-whole number of seconds -- an edge the ISR never
        counted leaves pps_n short while UTC advanced anyway. A boot-wide fit would spread that
        second silently over every row in between; bracketing on the NEAREST anchors turns it
        into a refusal, because the two ends disagreeing IS the detection of the lost edge."""
        rows = [_anchored(100, 100, 0),
                _row(140, 140, 0),
                _row(180, 180, 0, utc_us=T0 + 181 * 1_000_000)]   # one edge never counted
        got = U.recover_boot(rows)
        assert got.recovered == []
        assert [r.reason for r in got.refused] == ["bracket_disagrees"]

    def test_a_row_after_the_last_anchor_has_no_bracket(self):
        rows = [_anchored(100, 100, 0), _row(140, 140, 0)]
        got = U.recover_boot(rows)
        assert [r.reason for r in got.refused] == ["after_last_anchor"]

    def test_pps_n_zero_is_the_firmware_saying_there_was_no_edge_at_all(self):
        """`if (!pn) off = 0;  // pre-lock: there is no edge to be offset FROM.` There is nothing
        to place such a row against, and inventing one is the failure mode this module exists to
        prevent."""
        rows = [_row(12, 0, 0), _anchored(100, 100, 0), _anchored(180, 180, 0)]
        got = U.recover_boot(rows)
        assert [r.reason for r in got.refused] == ["no_pps_n"]

    def test_the_gate_sits_between_the_measured_noise_and_the_smallest_real_slip(self):
        """⚠️FROM THE ENVELOPE, NOT A ROUND NUMBER. Over 5141 consistent adjacent pairs the worst
        residual measured 2331 us; the smallest genuine slip measured 999946 us. The gate has to
        separate those two populations and it must not be tightened to the middle of the first
        one, which would start refusing sound rows."""
        assert 2331 < U.EDGE_TOL_US < 999_946


# ---------------------------------------------------------------- cutting the file into boots


class TestSplitBoots:
    def test_it_cuts_where_uptime_restarts(self):
        rows = [_row(10, 8, 0), _row(50, 48, 0), _row(11, 9, 0)]
        assert [len(b) for b in U.split_boots(rows)] == [2, 1]

    def test_it_also_cuts_where_pps_n_restarts(self):
        """uptime_s has one-second resolution, so two boots can share a value; pps_n restarting
        is the second, independent signal."""
        rows = [_row(13, 500, 0), _row(13, 11, 0)]
        assert [len(b) for b in U.split_boots(rows)] == [1, 1]

    def test_two_boots_it_cannot_tell_apart_produce_no_recovery(self):
        """⚠️MEASURED ON THE LIVE nyquist CARD: two runs each hold rows at uptime 13 with pps_n 11
        whose stamps are 342 s apart, which is impossible inside one boot. dets.csv carries no
        boot id, so the cut cannot see it -- and it does not have to. The bracket's agreement
        check refuses the merged pair exactly as it refuses a lost edge, so an under-cut costs
        recoveries and never costs correctness."""
        merged = [_anchored(13, 11, 100),
                  _row(13, 11, 200),
                  _row(13, 11, 300, utc_us=T0 + 342_000_000)]      # a different boot entirely
        got = U.recover_boot(merged)
        assert got.recovered == []
        assert [r.reason for r in got.refused] == ["bracket_disagrees"]


# ---------------------------------------------------------------- the diagnosis


class TestDiagnose:
    def test_it_separates_the_boot_settle_from_a_node_running_without_a_clock(self):
        """The split that separates the fleet: subtract the rows every node writes in its first
        15 s and nyquist's 92 unanchored records become 5 while mach's 517 stay 475."""
        rows = ([_row(13, 11, 0), _row(14, 12, 0)]                 # settle, every boot has these
                + [_row(u, u - 3, 0) for u in (200, 400, 900)]     # a node with no clock
                + [_anchored(1000, 997, 0)])
        d = U.diagnose(rows, node="mach")
        assert (d.n_rows, d.n_unanchored, d.n_settle, d.n_late) == (6, 5, 2, 3)
        assert d.longest_late_s == 900
        # p90 of three samples is the second of them: the quantile is a nearest-rank index into
        # the sorted list, not an interpolation. `max` is reported separately for that reason.
        assert d.late_quantiles() == {"min": 200, "p50": 400, "p90": 400, "max": 900}

    def test_a_node_with_no_late_rows_has_no_distribution_rather_than_a_zero(self):
        """⚠️Reporting `longest_late_s = 0` and `p50 = 0` for a healthy node makes 'never
        happened' and 'happened, instantly' the same reading. An empty distribution is empty."""
        d = U.diagnose([_row(13, 11, 0), _anchored(20, 18, 0)], node="rankine")
        assert d.n_late == 0
        assert d.late_quantiles() == {}

    def test_the_settle_split_is_a_parameter_not_a_law(self):
        rows = [_row(20, 18, 0), _anchored(100, 98, 0)]
        assert U.diagnose(rows).n_late == 1
        assert U.diagnose(rows, settle_s=30).n_late == 0

    def test_an_empty_file_is_zero_and_not_a_crash(self):
        d = U.diagnose([], node="mach")
        assert (d.n_rows, d.n_unanchored, d.unanchored_frac, d.late_frac) == (0, 0, 0.0, 0.0)
