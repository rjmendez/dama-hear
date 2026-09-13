"""What may convert a sample index into a UTC time, and what may not.

⚠️THIS TEST EXISTS BECAUSE fs_clean IS NOT A CLOCK MEASUREMENT. It is a throughput estimate whose
numerator advances only in whole 256-sample I2S blocks, so every value it can take is exactly
(k * BLOCK) / win_s -- 12 readings from three independent sources were checked and all 12 are
exact, among them 16006.0952 = 336128/21 and 16000.8223 = 29189*256/467. Its resolution is
BLOCK/win_s Hz = 16000/win_s ppm: 2000 ppm at the 8 s minimum window, 1143 ppm at 14 s.

Two nodes were once 1118 ppm apart in this field -- smaller than one quantisation step of the
coarser of the two readings -- and both moved about 1000 ppm in an hour at constant temperature.
That value was nonetheless the slope `sample_to_utc` interpolated on, the WAV header rate, and
X-Audio-Fs-Hz, so a full 30 s /audio window pulled from the two nodes differed between them by
33-41 ms = 11-14 m of sound. The firmware now refuses to use the estimate as a timebase until its
window supports the class's own timing budget, and the arithmetic of that refusal is what this
file pins -- against hear/nodeclass.py, so the constant cannot drift away from the budget it was
derived from.

The check is on the SOURCE because there is no ESP32 toolchain in CI (tests/test_firmware_csv_
schema.py says the same, for the same reason). Comments are stripped before matching: a guard
that can be satisfied by the prose describing it is not a guard.
"""
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import nodeclass as NC                                  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1] / "firmware"
INO = ROOT / "hear_node" / "hear_node.ino"
# FS_NOMINAL is the BOARD's, not the sketch's: the sample rate belongs to the hardware profile.
BOARD = ROOT / "boards" / "xiao_s3_sense.h"


def _source(strip=True):
    if not INO.exists():
        pytest.skip("hear_node.ino not in this checkout")
    src = INO.read_text()
    if not strip:
        return src
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _define(src, name):
    for text in (src, BOARD.read_text() if BOARD.exists() else ""):
        m = re.search(r"#define\s+%s\s+(\d+)" % re.escape(name), text)
        if m:
            return int(m.group(1))
    raise AssertionError("no #define %s in the sketch or its board header" % name)


class TestTheMinimumWindow:
    def test_the_constant_is_derived_from_the_class_timing_budget_not_chosen(self):
        """The mark spacing is one PPS second, so a slope wrong by X ppm is X microseconds of
        error by the far end of a mark gap. The budget is the node class's own t_sigma_s."""
        src = _source()
        block = _define(src, "BLOCK")
        fs = _define(src, "FS_NOMINAL")
        win = _define(src, "FS_TIMEBASE_MIN_WIN_S")
        step_ppm = block / win / fs * 1e6
        budget_ppm = NC.get("xiao-s3-pps").t_sigma_s * 1e6      # us of error per 1 s mark gap
        assert step_ppm <= budget_ppm, (
            "a %d s window quantises the rate to %.0f ppm, which is %.0f us over a one-second "
            "mark gap against a %.0f us budget" % (win, step_ppm, step_ppm, budget_ppm))
        # and it is not needlessly long: one step less would breach the budget
        assert block / (win - 1) / fs * 1e6 > budget_ppm * 0.9

    def test_the_eight_second_window_the_estimator_starts_with_would_not_pass(self):
        """⚠️THE NUMBER THIS GUARD EXISTS FOR. fs_clean is recomputed from 8 clean seconds, which
        is 2000 ppm -- 2 ms over a mark gap, 0.69 m of sound. It was being used as the timebase
        from that moment on."""
        src = _source()
        step_ppm = _define(src, "BLOCK") / 8 / _define(src, "FS_NOMINAL") * 1e6
        cls = NC.get("xiao-s3-pps")
        assert step_ppm == pytest.approx(2000.0, abs=1.0)
        assert step_ppm * 1e-6 * 343.0 > cls.range_sigma_m()

    def test_the_gate_is_on_the_window_of_the_accepted_value(self):
        """⚠️NOT on fs_clean_secs, which is the LIVE window and goes to zero the instant a second
        is refused, while the value it last produced stays in use. Gating on the live one would
        make the timebase flip between fs_clean and nominal at every dropped block."""
        src = _source()
        m = re.search(r"static double fs_timebase\(\)\s*\{(.*?)\n\}", src, re.S)
        assert m, "fs_timebase() is gone"
        body = m.group(1)
        assert "fs_clean_win_s >= FS_TIMEBASE_MIN_WIN_S" in body
        assert "fs_clean_secs" not in body


class TestNothingBypassesIt:
    def test_no_conversion_uses_the_raw_estimate(self):
        """Six sites used to carry `fs_clean > 1000.0 ? fs_clean : nominal` inline: the two ring
        interpolators, the WAV header, X-Audio-Fs-Hz's span, the detection back-date, and
        /status's raw span. One helper, so the guard cannot be added to five of six."""
        src = _source()
        assert "? fs_clean :" not in src, "an inline timebase ternary is back"
        # every use of the value as a divisor/slope goes through the helper
        assert src.count("fs_timebase()") >= 6

    def test_the_estimate_is_still_reported_with_its_own_resolution(self):
        """Reporting fs_clean is fine; reporting it as if 4 decimal places meant something is not.
        Its step has to travel with it, in /status and in health.csv."""
        src = _source()
        assert "fs_step_ppm" in src
        assert '\\"fs_step_ppm\\":' in src, "/status must carry the step"
        assert "fs_step_ppm," in src, "health.csv must carry the step"


class TestTheDropCounter:
    def test_a_multi_second_straddle_is_charged_for_every_second(self):
        """⚠️THE MEASURED FAILURE. The drain's own fetch stalled mach for 32 s; 78,336 samples
        arrived where 512,000 were due, and the old charge -- one second's worth, once per pass --
        booked 2 seconds of it. That interval was 433,664 of the run's 437,504 total deficit, so
        the node read as though it were clocking 683,451 samples slow for the whole run."""
        src = _source()
        assert "drop_seconds += span" in src
        assert "uint64_t expect = (uint64_t)FS_NOMINAL * span" in src
        assert "drop_samples += (uint32_t)(expect - (uint64_t)d)" in src
        assert "drop_samples += (uint32_t)((double)FS_NOMINAL - (double)d)" not in src

    def test_a_second_that_delivers_too_much_is_refused_too(self):
        """Only the low side was ever tested. One drop-free interval on mach delivered 36.5 blocks
        too many -- 16346 Hz, +21,630 ppm -- and the firmware certified it into the rate. It is
        also how a MISSED edge announces itself: one interval that is really two arrives with
        about twice the samples, so this branch is what keeps the window's edge count equal to its
        second count."""
        src = _source()
        assert "over_seconds += span" in src
        assert re.search(r"\(double\)d > 1\.0\d \* \(double\)expect", src)

    def test_the_blind_spot_is_written_down_rather_than_papered_over(self):
        """A single lost 256-sample block cannot be caught by any fixed per-second threshold: at
        16006.6 Hz a PPS second is 62.53 blocks, so a 63-block second that loses one delivers
        exactly 62*256 = 15,872 -- bit-identical to a legal 62-block second. The arithmetic has to
        stay in the file, because the obvious 'fix' (lower the fraction) looks right and cannot
        work."""
        src = _source(strip=False)
        assert "15,872" in src and "62.53 blocks" in src
        # and the arithmetic itself still holds
        assert 62 * 256 == 15872 and 0.985 * 16000 < 15872


class TestTheTwoPoisonedFigures:
    def test_the_crystal_figure_no_longer_divides_by_the_edge_count(self):
        """(pps_us_last - pps_us_first) / (pps_count - 1) counts EDGES SEEN, not seconds ELAPSED.
        A missed edge or a probe resync leaves the span intact and the divisor short: mach read a
        median 3206 ppm and a maximum 37,988 ppm that way, while nyquist -- which had never
        resynced -- read 9.5-12.2 ppm."""
        src = _source()
        m = re.search(r"static double esp_clock_ppm\(uint32_t \*secs_out\)\s*\{(.*?)\n\}", src, re.S)
        assert m
        body = m.group(1)
        assert "pps_count" not in body and "pps_us_first" not in body
        assert "esp_iv_sum_us" in body and "esp_iv_n" in body

    def test_the_reported_sample_rate_is_the_drop_free_one(self):
        """i2s.measured_hz is not an inert diagnostic: it is the headline of the node's own web UI
        and what firmware/hear_node/watch.py announces on every 0.02 Hz move. Cumulative-over-
        cumulative served 7984.6726 Hz (-500,958 ppm) and 15332.5601 Hz (-41,715 ppm) from two
        nodes that were acquiring about 16 kHz."""
        src = _source()
        m = re.search(r"static double measured_fs\(\)\s*\{(.*?)\n\}", src, re.S)
        assert m
        body = m.group(1)
        assert "clean_samples" in body and "clean_seconds" in body
        assert "pps_samp_first" not in body
        # the support travels with the rate, in /status and in the UI
        assert '\\"clean_s\\":' in src

    def test_a_missed_edge_is_counted_where_it_can_be_seen(self):
        src = _source()
        assert "pps_gaps++" in src
        assert '\\"gaps\\":' in src


class TestWhatIsNotClaimed:
    def test_the_node_still_cannot_measure_its_own_pdm_clock(self):
        """⚠️MEASURED NEGATIVE, kept in the source. Every rate here is derived from the same I2S
        read counter, so an undetected block loss and a slow clock are the same number to all of
        them. nyquist's drop-free rate is 15994.760 Hz and mach's 16006.589-16010.413 Hz (the
        range is one interval its own filter admits); on matched drop-free intervals nyquist runs
        about 0.49 blocks per 30 s low, which is 261 ppm and does not close a 470-980 ppm gap.
        Nothing in this firmware distinguishes the two, and no test should imply that it does."""
        src = _source(strip=False)
        assert "external frequency" in src or "frequency counter" in src
