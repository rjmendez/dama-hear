import math

import numpy as np
import pytest

from hear.bio_tripwire import BioacousticPerimeterTripwire, SeismicImpact


def prime(tw):
    for t in (0.0, 1.0, 2.0):
        assert tw.process_frame(t, {"a": 40.0, "b": 40.0}) == []


def test_detects_spatial_silencing_and_seismic_confirmation():
    tw = BioacousticPerimeterTripwire(
        drop_threshold_db=9.0,
        positions_m={"a": (0.0, 0.0), "b": (10.0, 0.0)},
    )
    prime(tw)
    assert tw.process_frame(2.08, {"a": 30.0, "b": 40.0}, seismic_impacts=[SeismicImpact(2.12)]) == []
    assert tw.process_frame(2.16, {"a": 30.0, "b": 31.0}) == []
    events = tw.process_frame(3.16, {"a": 40.0, "b": 40.0})
    assert len(events) == 1
    event = events[0]
    assert event.onset_time_s == pytest.approx(2.08)
    assert event.drop_db == pytest.approx(10.0, abs=1.0)
    assert event.spatial_silencing_perimeter_m == pytest.approx(10.0)
    assert event.propagation_velocity_mps == pytest.approx(125.0)
    assert event.seismic_correlated
    assert event.tau_rec_s is not None


def test_onset_window_rejects_unrelated_late_microphone():
    tw = BioacousticPerimeterTripwire(drop_threshold_db=6.0, onset_window_s=0.1, min_silenced_mics=2)
    prime(tw)
    tw.process_frame(2.08, {"a": 30.0, "b": 40.0})
    # A second microphone arriving 200 ms later is not one perimeter event.
    tw.process_frame(2.28, {"a": 30.0, "b": 30.0})
    assert tw._active == {"a": 2.08}
    assert tw.process_frame(3.28, {"a": 40.0, "b": 40.0}) == []


def test_adaptive_tracker_does_not_follow_short_silence():
    tw = BioacousticPerimeterTripwire(drop_threshold_db=6.0, tracker_tau_s=100.0, min_silenced_mics=1)
    for i in range(6):
        tw.process_frame(float(i), {"a": 40.0})
    baseline = tw.baselines_db["a"]
    tw.process_frame(5.08, {"a": 30.0})
    assert tw.baselines_db["a"] == pytest.approx(baseline)


def test_psd_snapshots_are_reduced_only_inside_4_to_8_khz():
    tw = BioacousticPerimeterTripwire(min_silenced_mics=1, drop_threshold_db=6.0)
    freqs = np.array([1000.0, 5000.0, 7000.0, 12000.0])
    high = np.array([1.0, 10.0, 10.0, 1000.0])
    low = np.array([1.0, 1.0, 1.0, 1000.0])
    tw.process_frame(0.0, {"a": {"frequencies_hz": freqs, "power": high}})
    assert tw.process_frame(0.08, {"a": (freqs, low)}) == []
    assert tw._active == {"a": 0.08}


def test_invalid_band_and_time_order_are_refused():
    with pytest.raises(ValueError, match="unreachable"):
        BioacousticPerimeterTripwire(fs_hz=16000.0, band_hz=(15000.0, 20000.0))
    tw = BioacousticPerimeterTripwire()
    tw.process_frame(1.0, {"a": 40.0})
    with pytest.raises(ValueError, match="monotonic"):
        tw.process_frame(0.0, {"a": 40.0})


def test_unrecovered_silence_is_not_reported_on_flush():
    tw = BioacousticPerimeterTripwire(min_silenced_mics=2, drop_threshold_db=6.0)
    prime(tw)
    tw.process_frame(3.0, {"a": 20.0, "b": 20.0})
    assert tw.flush(5.0) == []
