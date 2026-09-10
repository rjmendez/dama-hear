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
# Point this at your own single-channel WAV of known shots to run the recorded-audio test.
# Unset, that test skips -- the field captures are not redistributable.
REAL = os.environ.get("DAMA_HEAR_REAL_WAV", "")


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

    @pytest.mark.skipif(not REAL or not os.path.exists(REAL),
                        reason="set DAMA_HEAR_REAL_WAV to a recorded capture to run this")
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


def _burst(rise_s, amp, at=12000, n=48000, f=4000.0, plateau_s=0.005, decay_s=0.020):
    """A shot with a KNOWN onset. Linear rise over `rise_s` from `at`, 5 ms plateau, decay.

    The carrier is a 4 kHz tone, not noise: 48 kHz / 4 kHz is 12 samples, so |sin| has period 6
    and the gate's 48-sample envelope window spans exactly 8 of them. Ripple cancels, and the
    envelope of the plateau is flat to machine precision -- which is what lets these tests pin an
    onset to a fraction of a sample instead of arguing about a noise realisation.
    """
    R, P = rise_s * FS, plateau_s * FS
    t = np.arange(n) - at                       # `at` may be fractional: see the sub-sample test
    s = np.clip(t / R, 0.0, 1.0)
    s = np.where(t > R + P, np.exp(-(t - R - P) / (decay_s * FS)), s)
    s[t < 0] = 0.0
    return amp * s * np.sin(2 * np.pi * f * np.arange(n) / FS)


class TestAmbientTau:
    """The ambient time constant, measured rather than read off the parameter.

    The old alpha was derived per 1 ms hop and applied per SAMPLE: the parameter said 10 s and the
    gate ran at 0.208 s. Nothing caught it, because every other gate test runs on a signal loud
    enough that the ambient is frozen by the arm/threshold logic for the whole event.
    """

    @staticmethod
    def _realised_tau(tau, lo=20.0, hi=200.0):
        """Step response. Seed the floor at `lo`, step the input to `hi`, bisect for the length
        of step it takes the floor to cover 63.2% of it. Both levels sit under the 800 floor
        threshold, so the gate never fires and the ambient updates on every sample."""
        def ambient_after(n):
            g = DT.Gate(FS, ambient_tau_s=tau)
            g.ambient = lo                      # exact, and skips a settling run
            g.process(np.full(n, hi), 0)
            return g.ambient
        target = lo + 0.6321205588 * (hi - lo)
        a, b = 1, int(20 * tau * FS)
        assert ambient_after(b) > target, "%.3f s tau did not converge in 20 tau" % tau
        while b - a > 1:
            m = (a + b) // 2
            if ambient_after(m) < target:
                a = m
            else:
                b = m
        return b / FS

    @pytest.mark.parametrize("tau", [0.05, DT.AMBIENT_TAU_S, 0.4])
    def test_the_parameter_means_what_it_says(self, tau):
        got = self._realised_tau(tau)
        assert abs(got / tau - 1.0) < 0.05, \
            "asked for %.4f s, measured %.4f s (%.0fx)" % (tau, got, got / tau)

    def test_the_legacy_symmetric_constant_is_still_reachable(self):
        # ⚠️docs/validation-full-captures.md (338 raw -> 168, 32 impossible clusters -> 1, zero
        # false alarms in 68.5 min) was produced with alpha = 1e-4 per sample. That is no longer
        # the DEFAULT -- see TestBurstDoesNotDesensitise -- but it must stay reproducible, or the
        # published table becomes unrepeatable rather than merely superseded.
        g = DT.Gate(FS, ambient_tau_s=DT.AMBIENT_TAU_S)
        assert g.alpha_rise == pytest.approx(1.0 / 10000.0, rel=1e-12)
        assert g.alpha_fall == pytest.approx(1.0 / 10000.0, rel=1e-12)
        assert self._realised_tau(DT.AMBIENT_TAU_S) < 0.25, "the legacy floor is not sub-second"

    def test_the_default_is_slow_up_and_quick_down(self):
        g = DT.Gate(FS)
        rise = 1.0 / g.alpha_rise / FS
        fall = 1.0 / g.alpha_fall / FS
        assert rise == pytest.approx(DT.AMBIENT_TAU_RISE_S, rel=1e-9)
        assert fall == pytest.approx(DT.AMBIENT_TAU_FALL_S, rel=1e-9)
        # The ordering is the whole design, not the specific seconds: the floor must not be able
        # to climb inside an event string, and must still fall back when the site quietens.
        assert rise > 5.0 * fall, "rise %.1f s is not slow relative to fall %.1f s" % (rise, fall)
        assert rise >= 20.0, "a %.1f s rise is short enough for a clap burst to lift" % rise

    def test_the_rising_step_really_takes_the_slow_limb(self):
        # Measured, not read off the parameter -- the same discipline as _realised_tau, but the
        # default is asymmetric so the legacy helper cannot express it.
        g = DT.Gate(FS)
        g.ambient = 20.0
        g.process(np.full(int(2.0 * FS), 200.0), 0)      # 2 s of a louder, still sub-threshold room
        # 2 s against a 30 s rise is 6.4% of the way, NOT the 6x the old 0.21 s limb would give
        assert g.ambient < 40.0, "ambient reached %.1f in 2 s -- the fast limb is still on" % g.ambient
        assert g.ambient > 20.0, "ambient did not rise at all; the deadlock guard is gone"

    def test_a_fallen_room_is_recovered_quickly(self):
        g = DT.Gate(FS)
        g.ambient = 200.0
        g.process(np.full(int(15.0 * FS), 20.0), 0)      # 3 fall-taus of quiet
        assert g.ambient < 40.0, "ambient stuck at %.1f after 15 s of quiet" % g.ambient


class TestBurstDoesNotDesensitise:
    """⚠️THE REGRESSION THIS FILE EXISTS FOR, measured in the field before it was written.

    On the node `mach`, 2026-09-09, clapping in the same room: ambient 22 -> 143 in five seconds,
    threshold 200 -> 1146, and not one clap fired -- while the phone beside it recorded every one.
    Across the three nodes the one in the occupied room had the highest peak envelope (16938,
    2.7x its siblings) and the FEWEST detections (294, against 689 and 470).

    The gate turned itself down exactly where there was most to hear, because the ambient
    estimator took its fast limb on everything below threshold -- which includes a transient's
    own reverberant tail and the noise of whoever is producing the transients.
    """

    @staticmethod
    def _clapping_room(n=6, gap=1.5, quiet=22.0, occupied=143.0, clap=683.0, seed=3):
        """The measured levels as a signal: a quiet floor, then a burst whose SUB-THRESHOLD
        material sits where mach's ambient actually went, with claps on top of it."""
        rng = np.random.default_rng(seed)
        x = list(rng.normal(0.0, quiet, int(8.0 * FS)))
        for _ in range(n):
            seg = rng.normal(0.0, occupied, int(gap * FS))
            c = int(0.02 * FS)
            seg[:c] += np.linspace(clap * 2.2, 0.0, c)
            x += list(seg)
        return np.array(x, dtype=np.float32)

    @staticmethod
    def _run(g, x):
        out = []
        for s in range(0, len(x), 4096):
            out += g.process(x[s:s + 4096], s)
        return out

    def test_the_burst_cannot_lift_the_threshold_out_of_its_own_reach(self):
        x = self._clapping_room()
        g = DT.Gate(FS, floor=200.0)
        self._run(g, x)
        # mach reached 143; the fix must keep it far below that inside a ~9 s burst
        assert g.ambient < 60.0, "ambient reached %.1f -- the burst lifted its own floor" % g.ambient
        assert g.threshold() < 500.0, \
            "threshold reached %.1f; mach's claps peaked at 683" % g.threshold()

    def test_the_legacy_gate_is_the_one_that_went_deaf(self):
        # The counter-case, so this file records WHY the default moved rather than asserting it.
        x = self._clapping_room()
        old = DT.Gate(FS, floor=200.0, ambient_tau_s=DT.AMBIENT_TAU_S)
        new = DT.Gate(FS, floor=200.0)
        self._run(old, x)
        self._run(new, x)
        assert old.ambient > 3.0 * new.ambient, \
            "legacy ambient %.1f vs fixed %.1f -- the limbs are not behaving differently" % (
                old.ambient, new.ambient)
        assert old.threshold() > 1.8 * new.threshold()


class TestOnset:
    """The gate must timestamp the ONSET. It used to report the envelope peak, which arrives one
    rise time late -- and rise time grows with range, so the error is a per-node bias that does
    not cancel in TDoA. The budget this project argues about is 183 us (node-hardware.md)."""

    @pytest.mark.parametrize("rise", [0.002, 0.005, 0.020])
    @pytest.mark.parametrize("amp", [2000.0, 20000.0, 200000.0])
    def test_onset_lands_on_the_rise_not_the_peak(self, rise, amp):
        # true onset of a linear ramp at ONSET_FRAC of its own peak
        true = 12000 + DT.ONSET_FRAC * rise * FS
        d = DT.Gate(FS).process(_burst(rise, amp), 0)
        assert len(d) == 1
        err = d[0]["onset_index"] - true
        assert abs(err) <= 2.0, "onset off by %+.2f samples (%.0f us)" % (err, err / FS * 1e6)
        # and the back-walk really moved: the sibling project's moves 0, so its onset IS its peak
        moved = d[0]["peak_index"] - d[0]["onset_index"]
        assert moved > 0.7 * rise * FS, "back-walk moved only %.1f samples" % moved

    @pytest.mark.parametrize("rise", [0.0005, 0.001, 0.002, 0.005, 0.020])
    def test_onset_is_amplitude_invariant_over_100x(self, rise):
        # Constant fraction exists for this: a fixed threshold is crossed earlier in the rise by
        # a loud round than a quiet one, which is precisely a range-dependent timing bias.
        # Measured spread here is 0.01 samples over 100x. Rises the 1 ms envelope cannot resolve
        # read early against the ramp -- 7.1 samples at a 0.5 ms rise, 2.2 at 1 ms -- but that is
        # a fixed offset of envelope(), identical at every amplitude, not a range-dependent bias.
        got = [DT.Gate(FS).process(_burst(rise, a), 0)[0]["onset_index"]
               for a in (2000.0, 20000.0, 200000.0)]
        assert max(got) - min(got) <= 2.0, "100x amplitude moved the onset by %.2f samples" % \
            (max(got) - min(got))

    def test_the_back_walk_must_be_on_the_envelope(self):
        # ⚠️THE TRAP, made falsifiable. Walking raw |x| from the peak stops at the first zero
        # crossing of the carrier, so it reports the peak as the onset -- the sibling project
        # ships that and its back-walk moves 0 samples over any amplitude.
        x = _burst(0.020, 20000.0)
        e = DT.envelope(x, FS)
        k = int(np.argmax(e))
        back = int(DT.GUARD_S * FS)
        on_env = DT.onset_index(e, k, back=back)
        on_raw = DT.onset_index(np.abs(x), k, back=back)
        assert k - on_env > 0.7 * 0.020 * FS, \
            "envelope walk moved only %.1f samples" % (k - on_env)
        assert k - on_raw < 12, \
            "raw |x| walk should stall inside one 4 kHz half-cycle, moved %.1f" % (k - on_raw)

    def test_the_crossing_is_interpolated_not_truncated(self):
        # straight envelope, 3 per sample from index 10, flat 100 after: the 20% crossing is
        # analytic at 10 + 20/3. Returning the last-below index gives 16.0 and loses a third of
        # a sample -- and 183 us is only 8.8 samples at 48 kHz.
        e = np.concatenate([np.zeros(10), 3.0 * np.arange(35), np.full(20, 102.0)])
        e = np.minimum(e, 100.0)
        got = DT.onset_index(e, int(np.argmax(e)), frac=0.2)
        assert got == pytest.approx(10.0 + 20.0 / 3.0, abs=1e-9)

    @pytest.mark.parametrize("frac", [0.25, 0.5, 0.75])
    def test_a_sub_sample_shift_reads_as_a_sub_sample_shift(self, frac):
        # end to end, including the 1 ms envelope. Residual measured at 0.22 samples (4.5 us)
        # over a 0.1-sample sweep -- a fortieth of the 183 us the whole design argues about.
        def on(at):
            return DT.Gate(FS).process(_burst(0.005, 20000.0, at=at), 0)[0]["onset_index"]
        moved = on(12000.0 + frac) - on(12000.0)
        assert abs(moved - frac) < 0.3, "%.2f sample shift read as %.2f" % (frac, moved)

    def test_onset_never_reports_after_its_own_peak(self):
        for rise in (0.0005, 0.005, 0.020):
            d = DT.Gate(FS).process(_burst(rise, 20000.0), 0)[0]
            assert d["onset_index"] <= d["peak_index"]
            assert d["index"] == int(d["onset_index"])

    @pytest.mark.parametrize("rise", [0.002, 0.020])
    def test_onset_is_offset_independent(self, rise):
        # given the whole event in one block, where it sits must not move the answer
        errs = [DT.Gate(FS).process(_burst(rise, 20000.0, at=8192 + off, n=64000), 0)[0]
                ["onset_index"] - (8192 + off + DT.ONSET_FRAC * rise * FS)
                for off in range(0, 4096, 512)]
        assert max(abs(e) for e in errs) <= 2.0, "worst %+.2f samples" % \
            max(errs, key=abs)

    def test_a_round_inside_the_previous_one_s_tail_is_timed_not_clamped(self):
        """⚠️THE 18.4% CASE, WITH KNOWN TRUTH.

        A round landing inside the previous round's decay tail never sees its envelope fall to
        20% of the NEW peak, because the old one is still ringing. Referred to zero, the walk ran
        to the clamp edge and returned it -- 25 ms early, 8.6 m of range. Referred to the local
        trough it lands on the event.

        Measured on the real corpus this takes the never-timed count from 42 of 228 to 0.
        """
        rng = np.random.default_rng(7)
        n, truth = int(0.30 * FS), int(0.045 * FS)

        def blip(count, amp, rise_s, decay_s):
            t = np.arange(count) / FS
            env = np.where(t < rise_s, t / max(rise_s, 1e-9), np.exp(-(t - rise_s) / decay_s))
            return amp * env * rng.normal(0, 1, count)

        # previous round at -3 dB still ringing, so the floor sits at ~0.23 of the new peak
        x = blip(n, 20000.0 * 10 ** (-3 / 20.0), 0.001, 0.060)
        x[truth:] += blip(n - truth, 20000.0, 0.0016, 0.030)
        x += rng.normal(0, 20.0, n)

        e = DT.envelope(x, FS)
        peak = int(np.argmax(e))
        idx, found = DT.onset_index_checked(e, peak, DT.ONSET_FRAC, back=int(DT.GUARD_S * FS))
        assert found is True, "still cannot time a round inside a decay tail"
        err_ms = (idx - truth) / FS * 1e3
        assert abs(err_ms) < 3.0, "onset off by %.1f ms" % err_ms

    def test_referring_to_the_floor_cannot_regress_a_quiet_event(self):
        """Where the floor is zero the two references are identical -- a strict generalisation."""
        e = np.concatenate([np.zeros(2000), np.linspace(0.0, 1.0, 500)])
        peak = len(e) - 1
        idx, found = DT.onset_index_checked(e, peak, 0.20, back=2400)
        assert found is True
        # the 20% crossing of a clean rise off a zero floor
        assert abs(idx - (2000 + 0.20 * 500)) < 2.0

    def test_a_trough_at_the_window_edge_is_not_a_baseline(self):
        """⚠️A window that TRUNCATES a rise has its minimum at the left edge. Referring the
        fraction to that turns an honest 'clamped, cannot see further back' into a confidently
        LATE onset. Only an interior trough is a baseline. All 228 real events have one; a
        truncated block does not, by construction."""
        e = np.linspace(0.5, 1.0, 1200)          # descending out of the window: never a trough
        peak = len(e) - 1
        idx, found = DT.onset_index_checked(e, peak, 0.20, back=1000)
        assert found is False, "a truncated rise must not be referred to its own edge"
        assert idx == peak - 1000

    def test_a_rise_that_predates_the_block_is_clamped_to_the_edge(self):
        # The gate is block-local. Hand it a block that starts partway up the rise, so the 20%
        # crossing is already behind it: the answer must be the edge, not an extrapolation.
        start = 12500
        d = DT.Gate(FS).process(_burst(0.020, 20000.0, at=12000)[start:], start)
        assert d and d[0]["onset_index"] == float(start)


class TestOnsetInThePipeline:
    # ⚠️`at` IS CHOSEN SO THE WHOLE EVENT LANDS INSIDE ONE 4096-SAMPLE BLOCK. The gate's peak
    # search is block-local (detect.py: `j = min(len(e), i + self.guard)`), so an event that
    # straddles a block edge gets a truncated peak, a truncated 20% target and an onset up to
    # 170 samples (3.5 ms) early -- measured over a 128-sample offset sweep, 7 of 32 offsets at a
    # 20 ms rise. That is a defect in the streaming contract, not in the onset estimator, and the
    # estimator is pinned offset-independently by test_onset_is_offset_independent below.
    AT = 8400

    def test_timestamp_starts_at_the_onset(self):
        rise = 0.020
        p = Pipeline(FS, node_us_of=lambda idx: int(round(idx / FS * 1e6)))
        d = p.run(_burst(rise, 20000.0, at=self.AT))[0]
        got = SK.unpack(d["frame"])["node_us"]
        assert got == int(round(d["onset_index"] / FS * 1e6))
        # the peak is ~one rise time later; that is exactly the bias this removed
        peak_us = int(round(d["peak_index"] / FS * 1e6))
        assert peak_us - got > 0.7 * rise * 1e6

    def test_the_sketch_cannot_resolve_a_real_crack_s_rise_and_does_not_pretend_to(self):
        """⚠️THIS REPLACES `test_the_sketch_contains_the_rise`, WHICH ASSERTED SOMETHING THE
        FORMAT CANNOT DO FOR ITS OWN TARGET SIGNAL.

        One analysis frame is NFFT/fs = 256/48000 = **5.33 ms**. The measured peak-to-onset
        distance over the 228 hand-labelled 2026-09-05 events is a **median of 1.56 ms**. The
        rise of a rifle crack is therefore SHORTER THAN ONE FRAME and frame 0 contains the peak
        no matter where the window starts -- swept here at 1/2/4/5.33/8/12/20 ms rises, frame 0
        is within 1.5 dB of the peak at every one.

        The old test passed only because a 20 ms synthetic rise is not a crack. Chasing that
        property also costs accuracy: sketching from the 25 ms-clamped onset instead of one hop
        measures 0.9443 against 0.9732 nested AUC on the real events.

        If the rise must be resolved, the lever is NFFT/HOP_S -- not the onset clamp.
        """
        for rise_s in (0.001, 0.002, 0.004, 0.008, 0.020):
            d = Pipeline(FS).run(_burst(rise_s, 20000.0, at=self.AT))[0]
            db = SK.unpack(d["frame"])["db"]
            band = int(np.argmax(db.max(axis=1)))
            assert db[band].max() - db[band, 0] < 6.0, (
                "a %.0f ms rise resolved into frame 0 -- if NFFT or HOP_S changed, this "
                "test's premise changed with it" % (rise_s * 1e3))
        assert SK.NFFT / FS > 0.00156, "one frame is no longer longer than a crack's rise"

    def test_the_sketch_window_is_clamped_tighter_than_the_timestamp(self):
        """⚠️THE TWO WANT DIFFERENT CLAMPS AND USED TO SHARE ONE NUMBER.

        The timestamp walks back to the 25 ms re-trigger guard, which is right for it. Starting
        the 33 ms sketch there slides it off the event: nested grouped CV on the same 228 events,
        varying ONLY the sketch start, gives 0.9443 at the 25 ms guard against 0.9732 at one hop
        -- the guard is worse than not walking back at all (0.9634).
        """
        d = Pipeline(FS).run(_burst(0.020, 20000.0, at=self.AT))[0]
        back_s = (d["peak_index"] - d["sketch_index"]) / FS
        assert 0 <= back_s <= DT.SKETCH_BACK_S + 1.0 / FS, \
            "sketch started %.1f ms before the peak; the clamp is %.1f ms" % (
                back_s * 1e3, DT.SKETCH_BACK_S * 1e3)
        # the timestamp is NOT clamped that tightly -- it still walks the full rise
        assert (d["peak_index"] - d["onset_index"]) / FS > 0.7 * 0.020

    def test_an_onset_that_never_crossed_the_fraction_says_so(self):
        """⚠️18.4% of the 2026-09-05 events never reach 20% of their own peak inside the guard --
        retriggers sitting in the previous round's decay tail. onset_index returns the CLAMP EDGE
        for those, 25 ms early, indistinguishable from a real slow rise. At 345 m/s that is 8.6 m
        of range on a project whose output is localisation."""
        fs = 48000.0
        e = np.linspace(0.5, 1.0, int(0.05 * fs))          # never drops below 20% of its peak
        peak = len(e) - 1
        idx, found = DT.onset_index_checked(e, peak, 0.20, back=int(DT.GUARD_S * fs))
        assert found is False
        assert idx == peak - int(DT.GUARD_S * fs), "not an onset: it is the clamp edge"
        # and a real rise is still found and still flagged
        e2 = np.concatenate([np.zeros(1000), np.linspace(0.0, 1.0, 200)])
        idx2, found2 = DT.onset_index_checked(e2, len(e2) - 1, 0.20, back=int(DT.GUARD_S * fs))
        assert found2 is True and idx2 > 1000
