"""Node chain: gate, telemetry, and the full pipeline against recorded audio."""
import os
import struct
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as SK           # noqa: E402
from hear.node import detect as DT      # noqa: E402
from hear.node import telemetry as TL   # noqa: E402
from hear.node.pipeline import Pipeline, summarise  # noqa: E402

FS = 48000.0
REAL = "/home/rjmendez/analysis_20260905/array/rear.wav"


def _shot(n=48000, at=8000, amp=25000.0):
    x = np.random.RandomState(1).normal(0, 40, n)
    x[at:at + 20] += amp * np.hanning(20)
    x[at + 20:at + 900] += amp * 0.25 * np.exp(-np.arange(880) / 200.0) * \
        np.random.RandomState(2).normal(0, 1, 880)
    return x


class TestEnvelope:
    def test_rise_uses_an_envelope_not_raw_samples(self):
        # THE REGRESSION. Walking raw |x| from the peak stops at the first zero crossing and
        # reports ~0.1 ms for everything; a whole feature set was silently wrong that way.
        x = _shot()
        e = DT.envelope(x, FS)
        i = int(np.argmax(e))
        assert e[i] > 0
        # the envelope must still be elevated well after the peak, where raw |x| has crossed zero
        assert e[i + int(0.005 * FS)] > 0.05 * e[i]


class TestGate:
    def test_finds_an_obvious_shot(self):
        g = DT.Gate(FS)
        d = g.process(_shot(), 0)
        assert len(d) >= 1
        assert abs(d[0]["t_s"] - 8000 / FS) < 0.01

    def test_flags_retriggers_rather_than_dropping_them(self):
        # a reflection is evidence about the site; suppressing it here destroys the echo analysis
        x = _shot()
        x[8000 + int(0.03 * FS):8000 + int(0.03 * FS) + 20] += 12000 * np.hanning(20)
        d = DT.Gate(FS).process(x, 0)
        assert len(d) >= 2, "the second arrival must not be silently dropped"
        assert d[1]["retrigger"], "arrival 30 ms later is inside one blast's decay"

    def test_ambient_floor_is_not_raised_by_the_event(self):
        g = DT.Gate(FS)
        g.process(_shot(), 0)
        assert g.ambient < 500, "a loud event must not drag the ambient floor up after it"

    def test_since_prev_is_none_for_the_first_detection(self):
        d = DT.Gate(FS).process(_shot(), 0)
        assert d[0]["since_prev_s"] is None


class TestTelemetry:
    def test_temperature_gives_sound_speed(self):
        d = TL.unpack(TL.pack(23.0, 1013.2, 55.0, -48.5, -12.0, 14, 3900, True))
        assert d["temp_c"] == pytest.approx(23.0, abs=0.01)
        assert d["sound_speed_mps"] == pytest.approx(345.24, abs=0.05)

    def test_missing_sensor_is_none_not_a_plausible_wrong_value(self):
        # a zero temperature would silently become c = 331.3 m/s and nobody would notice
        d = TL.unpack(TL.pack(None, None, None, -50.0, -10.0, 0, 3700, False))
        assert d["temp_c"] is None
        assert d["pressure_hpa"] is None
        assert d["sound_speed_mps"] is None

    def test_frame_is_small(self):
        assert TL.wire_size() <= 20

    def test_dama_shape_carries_the_acoustic_parameter(self):
        d = TL.unpack(TL.pack(18.0, 1000.0, 40.0, -55.0, -20.0, 9, 3800, True))
        p = TL.to_dama("hear-01", 1788642333.0, d)
        assert p["node_type"] == "hear"
        assert p["clock_tier"] == "pps"
        assert p["sound_speed_mps"] == pytest.approx(TL.sound_speed(18.0, 40.0), abs=1e-6)
        assert "leq_dbfs" in p["acoustic"]

    def test_free_running_clock_is_reported_as_such(self):
        d = TL.unpack(TL.pack(20.0, 1000.0, 50.0, -50.0, -10.0, 0, 3700, False))
        assert TL.to_dama("n", 0.0, d)["clock_tier"] == "free"

    def test_spl_stats_on_silence_do_not_blow_up(self):
        s = TL.spl_stats(np.zeros(1000), FS)
        assert np.isfinite(s["leq_dbfs"]) and s["clipped_frac"] == 0.0

    def test_clipping_is_reported(self):
        x = np.full(1000, 32767.0)
        assert TL.spl_stats(x, FS)["clipped_frac"] == 1.0


class TestPipeline:
    def test_every_frame_fits_one_meshtastic_packet(self):
        d = Pipeline(FS).run(_shot())
        assert d, "pipeline found nothing in a synthetic shot"
        for x in d:
            assert x["frame_len"] == SK.wire_size()
            assert SK.fits_meshtastic()

    def test_frame_unpacks_to_the_detection_it_came_from(self):
        d = Pipeline(FS).run(_shot())[0]
        got = SK.unpack(d["frame"])
        assert got["peak"] == int(min(d["peak"], 65535))
        assert got["ref_db"] == pytest.approx(d["ref_db"], abs=0.25)

    def test_retrigger_flag_survives_into_the_frame(self):
        x = _shot()
        x[8000 + int(0.03 * FS):8000 + int(0.03 * FS) + 20] += 12000 * np.hanning(20)
        d = Pipeline(FS).run(x)
        assert any(SK.unpack(x_["frame"])["flags"] & 1 for x_ in d)

    @pytest.mark.skipif(not os.path.exists(REAL), reason="recorded audio not present")
    def test_runs_on_real_recorded_audio(self):
        w = wave.open(REAL)
        a = np.frombuffer(w.readframes(min(w.getnframes(), int(FS * 20))), dtype="<i2").astype(float)
        fs = w.getframerate(); w.close()
        d = Pipeline(fs).run(a)
        s = summarise(d)
        assert s["detections"] > 10, "found nothing in 20 s of known-shot audio"
        # the field measured ~60% of raw detections as retriggers; this track is concatenated
        # events so it is denser, but the flag must be doing real work
        assert 0 < s["retriggers"] < s["detections"]
        assert s["bytes_if_all_sent"] == s["detections"] * SK.wire_size()


class TestRearm:
    """A fixed guard cannot tell a decay tail from a new round. Measured over 123.7 min of
    capture: one shot became 4-5 'rounds' spaced at exactly the 25 ms guard."""

    @staticmethod
    def _shot_with_tail(tail_s, n=96000, at=8000, amp=25000.0):
        x = np.random.RandomState(1).normal(0, 40, n)
        x[at:at + 20] += amp * np.hanning(20)
        L = int(tail_s * FS)
        x[at + 20:at + 20 + L] += (amp * 0.35 * np.exp(-np.arange(L) / (0.03 * FS)) *
                                   np.random.RandomState(2).normal(0, 1, L))
        return x

    @pytest.mark.parametrize("tail", [0.05, 0.15, 0.30])
    def test_one_shot_with_a_long_tail_is_one_detection(self, tail):
        d = DT.Gate(FS).process(self._shot_with_tail(tail), 0)
        assert len(d) == 1, "%.0f ms tail produced %d detections" % (tail * 1000, len(d))

    def test_two_separate_rounds_are_still_two(self):
        a = self._shot_with_tail(0.10)
        b = self._shot_with_tail(0.10, at=8000 + int(0.2 * FS))
        both = a + b - np.random.RandomState(1).normal(0, 40, len(a))
        d = DT.Gate(FS).process(both, 0)
        assert len(d) == 2
        assert abs((d[1]["t_s"] - d[0]["t_s"]) - 0.2) < 0.02

    def test_full_auto_at_85ms_is_not_collapsed(self):
        # ~700 rpm measured in the field. The re-arm must not swallow a real burst.
        x = np.random.RandomState(3).normal(0, 40, 96000)
        for k in range(5):
            at = 8000 + int(k * 0.085 * FS)
            x[at:at + 20] += 25000 * np.hanning(20)
            L = int(0.03 * FS)
            x[at + 20:at + 20 + L] += 8000 * np.exp(-np.arange(L) / (0.01 * FS))
        d = DT.Gate(FS).process(x, 0)
        assert len(d) == 5, "700 rpm burst gave %d of 5" % len(d)

    def test_rearm_state_survives_a_block_boundary(self):
        # the guard was enforced within a block but the decay crosses block edges
        x = self._shot_with_tail(0.30)
        g = DT.Gate(FS)
        out = []
        for s in range(0, len(x), 4096):
            out += g.process(x[s:s + 4096], s)
        assert len(out) == 1, "block-split decay produced %d detections" % len(out)
