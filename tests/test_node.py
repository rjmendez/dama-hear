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
