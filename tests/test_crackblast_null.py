"""The documented false-alarm curve must be the one the function produces.

The previous curve (p50~5 / p90~8 / p99~12 dB) was measured against the PRE-EVENT FLOOR while
`rise_db` is referred to the REVERBERATION TROUGH. Nothing checked it, so a 12 dB threshold
documented at ~1% false alarm actually ran at ~41%. These tests exist so that drift cannot
recur silently.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.solve import crackblast as CB  # noqa: E402

FS = 48000.0
NULL = "/tmp/rise_null_phone.npy"


class TestDocumentedCurve:
    def test_percentiles_are_monotonic_and_ordered(self):
        ks = sorted(CB.NULL_PERCENTILES)
        vs = [CB.NULL_PERCENTILES[k] for k in ks]
        assert vs == sorted(vs), "a higher percentile must not sit lower in dB"

    def test_false_alarm_falls_as_threshold_rises(self):
        ks = sorted(CB.NULL_FALSE_ALARM)
        vs = [CB.NULL_FALSE_ALARM[k] for k in ks]
        assert vs == sorted(vs, reverse=True)

    def test_the_old_wrong_threshold_is_recorded_as_bad(self):
        # 12 dB was documented at ~1%; it is ~41%. If someone "fixes" this back, fail loudly.
        assert CB.NULL_FALSE_ALARM[12] > 0.2, \
            "12 dB is a ~41% false-alarm operating point, not a 1% one"

    def test_suggested_threshold_meets_the_rate_it_promises(self):
        for target in (0.05, 0.02, 0.01):
            t = CB.suggested_threshold_db(target)
            assert CB.NULL_FALSE_ALARM[t] <= target

    def test_it_refuses_to_extrapolate_past_the_measured_curve(self):
        # a threshold nobody measured is exactly how the old figure got there
        with pytest.raises(ValueError, match="no measured threshold"):
            CB.suggested_threshold_db(0.0001)


class TestAgainstRealNull:
    """Checked against the measured null when it is present -- 1660 anchors over 140 shot-free
    phone clips, which agreed with the robot array to within 1.5 dB at every percentile."""

    @pytest.mark.skipif(not os.path.exists(NULL), reason="measured null not present")
    def test_documented_percentiles_match_the_measurement(self):
        V = np.load(NULL)
        for p, doc in CB.NULL_PERCENTILES.items():
            got = float(np.percentile(V, p))
            assert abs(got - doc) < 1.5, \
                "p%s documented %.1f dB, measured %.2f dB" % (p, doc, got)

    @pytest.mark.skipif(not os.path.exists(NULL), reason="measured null not present")
    def test_documented_false_alarm_rates_match_the_measurement(self):
        V = np.load(NULL)
        for t, doc in CB.NULL_FALSE_ALARM.items():
            got = float(np.mean(V >= t))
            assert abs(got - doc) < 0.05, \
                "%d dB documented FA %.3f, measured %.3f" % (t, doc, got)


class TestRiseReferenceItself:
    def test_noise_alone_produces_a_large_rise_db(self):
        # the whole point: second_arrival returns the max of its window, so rise_db is NOT ~0
        # on shot-free audio. A test asserting it is near zero would enshrine the old error.
        rng = np.random.RandomState(0)
        x = rng.normal(0, 200, int(3.0 * FS))
        v = CB.rise_reference(x, FS, [1.0, 1.4, 1.8, 2.2])
        assert v.size >= 1
        assert float(np.median(v)) > 4.0, "noise should still show several dB of rise"
