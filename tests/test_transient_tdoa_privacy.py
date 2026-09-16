import json

import numpy as np
import pytest

from hear.privacy import transient_tdoa as TT


class Decision:
    def __init__(self, speech):
        self.speech = speech


class StubVad:
    def __init__(self, speech=False, fail=False):
        self.speech = speech
        self.fail = fail

    def detect(self, samples, rate):
        if self.fail:
            raise RuntimeError("model unavailable")
        assert samples.ndim == 1
        assert rate == 48000
        return Decision(self.speech)


def delayed_pair(delay_samples=13):
    rng = np.random.default_rng(7)
    source = rng.normal(0.0, 1.0, 4096).astype(np.float32)
    target = np.pad(source, (delay_samples, 0))[:len(source)]
    return source, target


def test_gcc_phat_recovers_fractional_record_delay():
    reference, target = delayed_pair()
    delay_s, peak, sigma_s = TT.gcc_phat(reference, target, 48000.0, 0.01)
    assert delay_s == pytest.approx(13.0 / 48000.0, abs=0.25 / 48000.0)
    assert peak > 0.9
    assert sigma_s >= 1.0 / (np.sqrt(12.0) * 48000.0)


def test_analysis_returns_only_derived_evidence_and_erases_owned_buffers():
    reference, target = delayed_pair()
    original_reference = reference.copy()
    window = TT.TransientTdoaWindow({"a": reference, "b": target}, 48000.0)

    record = window.analyze(
        StubVad(speech=True),
        reference_node_id="a",
        reference_arrival_utc_s=1_760_000_000.25,
        position_enu_m=(12.0, 8.0, 1.5),
    )

    assert window.erased
    assert np.array_equal(reference, original_reference), "caller memory is not silently modified"
    assert record["human_speech_detected"] is True
    assert record["zero_audio_retained"] is True
    assert record["arrivals"][1]["delta_s"] == pytest.approx(13.0 / 48000.0,
                                                             abs=0.25 / 48000.0)
    assert record["arrivals"][1]["arrival_utc_s"] == pytest.approx(
        1_760_000_000.25 + 13.0 / 48000.0
    )
    assert record["position_enu_m"] == {"east_m": 12.0, "north_m": 8.0, "up_m": 1.5}
    encoded = json.dumps(record)
    for forbidden in ("waveform", "samples", "embedding", "transcript", "pcm"):
        assert forbidden not in encoded.lower()


def test_vad_failure_is_fail_closed_after_tdoa_and_memory_is_erased():
    reference, target = delayed_pair()
    window = TT.TransientTdoaWindow({"a": reference, "b": target}, 48000.0)
    record = window.analyze(StubVad(fail=True))
    assert record["human_speech_detected"] is True
    assert record["vad_failed_closed"] is True
    assert window.erased


def test_vad_receives_a_copy_that_is_zeroed_after_the_decision():
    class RetainingVad:
        def detect(self, samples, rate):
            self.retained = samples
            samples[0] = 123.0
            return Decision(False)

    reference, target = delayed_pair()
    vad = RetainingVad()
    window = TT.TransientTdoaWindow({"a": reference, "b": target}, 48000.0)
    window.analyze(vad)
    assert np.count_nonzero(vad.retained) == 0
    assert window.erased


def test_processing_failure_still_erases_memory():
    reference, target = delayed_pair()
    target[:] = 0.0
    window = TT.TransientTdoaWindow({"a": reference, "b": target}, 48000.0)
    with pytest.raises(TT.TransientTdoaError, match="no measurable energy"):
        window.analyze(StubVad())
    assert window.erased


def test_window_is_bounded_and_single_use():
    too_long = np.zeros(int(10.0 * 48000) + 1, dtype=np.float32)
    with pytest.raises(TT.TransientTdoaError, match="exceeds"):
        TT.TransientTdoaWindow({"a": too_long, "b": too_long}, 48000.0)

    reference, target = delayed_pair()
    window = TT.TransientTdoaWindow({"a": reference, "b": target}, 48000.0)
    window.analyze(StubVad())
    with pytest.raises(TT.TransientTdoaError, match="already been consumed"):
        window.analyze(StubVad())


@pytest.mark.parametrize("record", [
    {"audio": b"secret"},
    {"arrivals": [{"waveform": [0.1, 0.2]}]},
    {"payload": [0.1, -0.2]},
    {"position_enu_m": {"east_m": float("nan")}},
])
def test_privacy_export_rejects_reconstructible_or_invalid_values(record):
    with pytest.raises(TT.TransientTdoaError):
        TT.assert_non_reconstructible(record)
