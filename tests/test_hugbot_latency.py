"""tools/hugbot_latency.py -- the alignment, the arithmetic, and every refusal.

The measurement this covers rests on one claim: the ESP tap and the ESP ring carry the SAME
samples, so a true alignment is rho ~= 1.0 and anything less is a failure to align rather than a
noisy answer. Every test here plants a known offset and checks the tool recovers exactly that,
because a correlation tool that is merely "close" cannot support that claim.

No test opens a socket, reads /dev/shm, or touches a live robot; the pure core is exercised with
synthetic rings built in-process, and the fixture is a small recorded artefact under testdata/.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from tools import hugbot_latency as hl  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), os.pardir, "testdata",
                       "hugbot_latency_trials.json")


def _ring(n_frames, n_ch, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(0, 100, size=(n_frames, n_ch))


class TestTheAlignmentIsExactOrItIsNotAnAlignment:
    def test_a_planted_offset_is_recovered_to_the_sample(self):
        ring = _ring(20000, 1)
        off = 7321
        tap = ring[off:off + 1200, 0]
        rho, k = hl.align_in_channel(tap, ring[:, 0])
        assert k == off
        assert rho == pytest.approx(1.0, abs=1e-9)

    def test_a_segment_that_is_not_in_the_ring_does_not_reach_rho_one(self):
        ring = _ring(20000, 1, seed=1)
        stranger = _ring(1200, 1, seed=99)[:, 0]
        rho, _ = hl.align_in_channel(stranger, ring[:, 0])
        # The whole gate is that a non-match cannot masquerade as a match.
        assert rho < 0.5

    def test_the_column_is_derived_not_assumed(self):
        """The tool must FIND the column rather than trust slot i -> columns 2i.

        The live mapping does hold, but the same call is made for boards that are absent from the
        ring at that instant, where the argmax lands anywhere. Deriving the column is what lets
        rho -- and only rho -- decide whether the answer means anything.
        """
        ring = _ring(20000, 6)
        off, col = 4444, 4
        tap = ring[off:off + 1500, col]
        rho, found_col, k = hl.best_channel(tap, ring)
        assert (found_col, k) == (col, off)
        assert rho == pytest.approx(1.0, abs=1e-9)

    def test_a_silent_channel_cannot_win_by_being_flat(self):
        ring = _ring(20000, 3)
        ring[:, 1] = 0.0
        tap = ring[500:1700, 2]
        rho, col, _ = hl.best_channel(tap, ring)
        assert col == 2 and rho == pytest.approx(1.0, abs=1e-9)

    def test_a_ring_shorter_than_the_segment_is_refused_not_padded(self):
        with pytest.raises(hl.LatencyError, match="at least the tap segment"):
            hl.align_in_channel(np.zeros(500), np.zeros(100))

    def test_normalised_peak_refuses_mismatched_lengths(self):
        with pytest.raises(hl.LatencyError, match="equal lengths"):
            hl.normalised_peak(np.zeros(10), np.zeros(11))

    def test_a_scaled_copy_still_reads_as_the_same_samples(self):
        """Normalisation is load-bearing: an amplitude change must not look like a worse match."""
        ring = _ring(8000, 1)
        tap = ring[100:900, 0] * 17.0
        rho, k = hl.align_in_channel(tap, ring[:, 0])
        assert k == 100 and rho == pytest.approx(1.0, abs=1e-9)


class TestThePullAxisArithmetic:
    def test_the_sign_is_late_is_positive(self):
        """A frame the pull axis dates LATER than PortAudio must give a positive lag."""
        sr, wc = 48000, 1_000_000
        # the newest frame is 'now'; the frame one second back is dated t_snap - 1.0
        lag = hl.pull_axis_lag_s(t_snap=100.0, write_counter=wc, sample_rate=sr,
                                 frame_of_tap0=wc - sr, t_adc_tap0=98.9)
        assert lag == pytest.approx(0.1, abs=1e-9)

    def test_an_exactly_consistent_pair_gives_zero(self):
        sr, wc = 48000, 500_000
        lag = hl.pull_axis_lag_s(t_snap=10.0, write_counter=wc, sample_rate=sr,
                                 frame_of_tap0=wc - 2 * sr, t_adc_tap0=8.0)
        assert lag == pytest.approx(0.0, abs=1e-12)

    def test_a_nonsense_sample_rate_is_refused(self):
        with pytest.raises(hl.LatencyError, match="sample_rate must be positive"):
            hl.pull_axis_lag_s(1.0, 10, 0, 5, 1.0)


def _trials(n, serial="AAAA", rho=1.0, lag=50.0, col=4, jitter=0.0):
    return [{"trial": i, "slot": 0, "serial": serial, "ring_ch": col, "rho": rho,
             "lag_ms": lag + (jitter if i % 2 else -jitter), "host_backlog_ms": 320.4,
             "ring_anchored": False}
            for i in range(n)]


class TestSummariseRefusesRatherThanEmittingAMiddle:
    def test_too_few_aligned_trials_carries_no_lag_key_at_all(self):
        rows = _trials(4, rho=1.0) + _trials(20, rho=0.2)
        rec = hl.summarise(rows, rho_min=0.9, min_trials=8)["AAAA"]
        assert rec["status"] == "refused_unalignable"
        # ABSENT IS NOT ZERO: a consumer that skips `status` must get a KeyError, not a number.
        with pytest.raises(KeyError):
            rec["lag"]

    def test_the_refusal_explains_that_rho_measures_identity_not_noise(self):
        rec = hl.summarise(_trials(3), rho_min=0.9, min_trials=8)["AAAA"]
        assert "same" in rec["reason"] and "not that the lag is noisy" in rec["reason"]

    def test_a_board_whose_column_wanders_is_refused(self):
        rows = _trials(6, col=4) + _trials(6, col=0)
        rec = hl.summarise(rows, rho_min=0.9, min_trials=8)["AAAA"]
        assert rec["status"] == "refused_column_unstable"
        assert rec["ring_columns_seen"] == [0, 4]
        with pytest.raises(KeyError):
            rec["lag"]

    def test_a_spread_too_wide_to_have_a_referent_is_refused(self):
        rec = hl.summarise(_trials(20, jitter=100.0), min_trials=8,
                           max_spread_ms=50.0)["AAAA"]
        assert rec["status"] == "refused_spread"
        with pytest.raises(KeyError):
            rec["lag"]
        # the numbers survive as evidence for the refusal, under a name nothing will mistake
        assert rec["lag_rejected"]["spread_ms"] > 50.0

    def test_a_clean_board_reports_a_distribution_and_never_a_bare_point(self):
        rec = hl.summarise(_trials(20, jitter=2.0), min_trials=8)["AAAA"]
        assert rec["status"] == "ok"
        for key in ("p05_ms", "p50_ms", "p95_ms", "min_ms", "max_ms", "mad_ms", "spread_ms"):
            assert key in rec["lag"], key
        assert "mean_ms" not in rec["lag"]

    def test_boards_are_summarised_independently(self):
        rows = _trials(20, serial="AAAA") + _trials(2, serial="BBBB")
        out = hl.summarise(rows, min_trials=8)
        assert out["AAAA"]["status"] == "ok"
        assert out["BBBB"]["status"] == "refused_unalignable"


class TestTheSkewNeedsBothBoardsInOneRingRead:
    def _joint(self, n, lag_a, lag_b, rho=1.0):
        rows = []
        for i in range(n):
            rows.append({"trial": i, "slot": 0, "serial": "AAAA", "ring_ch": 0, "rho": rho,
                         "lag_ms": lag_a, "host_backlog_ms": 320.0, "ring_anchored": False})
            rows.append({"trial": i, "slot": 1, "serial": "BBBB", "ring_ch": 2, "rho": rho,
                         "lag_ms": lag_b, "host_backlog_ms": 320.0, "ring_anchored": False})
        return rows

    def test_the_skew_is_the_difference_of_the_two_lags(self):
        out = hl.channel_skew(self._joint(6, 100.0, 40.0))
        rec = out["AAAA/BBBB"]
        assert rec["status"] == "ok"
        assert rec["skew"]["p50_ms"] == pytest.approx(60.0)

    def test_snapshots_where_only_one_board_aligned_contribute_nothing(self):
        rows = self._joint(6, 100.0, 40.0)
        # demote every BBBB row: the pair should now have no joint alignment at all
        for r in rows:
            if r["serial"] == "BBBB":
                r["rho"] = 0.1
        assert hl.channel_skew(rows) == {}

    def test_too_few_joint_alignments_is_refused_and_carries_no_skew(self):
        rec = hl.channel_skew(self._joint(2, 100.0, 40.0))["AAAA/BBBB"]
        assert rec["status"] == "refused_too_few_joint_alignments"
        with pytest.raises(KeyError):
            rec["skew"]

    def test_boards_are_never_paired_across_different_snapshots(self):
        """t_snap and write_counter only cancel WITHIN one ring read; pairing across reads would
        silently fold the pull-axis drift into the skew."""
        rows = self._joint(4, 100.0, 40.0)
        for r in rows:                      # give every row its own trial id
            r["trial"] = id(r)
        assert hl.channel_skew(rows) == {}

    def test_a_board_is_never_skewed_against_itself(self):
        rows = self._joint(4, 100.0, 40.0)
        for r in rows:                      # every row now claims to be the same board
            r["serial"] = "AAAA"
        assert hl.channel_skew(rows) == {}

    def test_the_aperture_is_the_measured_one(self):
        assert hl.HUGBOT_ESP_APERTURE_M == pytest.approx(0.7388, abs=5e-4)


class TestTheVerdictTestsTheSpreadNotTheMedian:
    def test_a_large_but_constant_offset_is_admissible(self):
        """A constant is a bias you subtract. Half a second of it must not disqualify a receiver;
        only the part that MOVES between events can."""
        lag = hl._quantiles([500.0] * 20)
        v = hl.arrival_verdict(lag, capture_latency_s=0.178, capture_sigma_s=None)
        assert v["admissible"] is True

    def test_the_measured_hugbot_spread_is_refused(self):
        lag = hl._quantiles([34.1, 62.0, 246.0, 41.2, 248.3, 33.7])
        v = hl.arrival_verdict(lag, capture_latency_s=hl.HUGBOT_CAPTURE_LATENCY_S,
                               capture_sigma_s=hl.HUGBOT_CAPTURE_SIGMA_S)
        assert v["admissible"] is False
        assert v["over_budget_factor"] > 100

    def test_a_stated_capture_sigma_can_only_widen_the_answer(self):
        lag = hl._quantiles([10.0, 12.0, 14.0])
        narrow = hl.arrival_verdict(lag, capture_latency_s=0.178)
        wide = hl.arrival_verdict(lag, capture_latency_s=0.178, capture_sigma_s=0.0113)
        assert wide["sigma_s"] > narrow["sigma_s"]

    def test_the_capture_latency_has_no_default_because_zero_would_be_a_claim(self):
        with pytest.raises(hl.LatencyError, match="perfect transport"):
            hl.arrival_verdict(hl._quantiles([1.0, 2.0]))

    def test_the_verdict_always_says_it_is_only_a_floor(self):
        v = hl.arrival_verdict(hl._quantiles([1.0, 2.0]), capture_latency_s=0.178)
        assert v["floor_only"] is True
        assert "inside the ESP32" in v["floor_note"]


class TestTheBudgetIsTheDocumentedOneNotAConvenientOne:
    def test_it_matches_the_arithmetic_docs_node_hardware_states(self):
        """One degree of unmeasured air temperature over the 35 m long baseline.

        Recomputed here rather than copied, so a future edit to the constant has to argue with
        the physics instead of just changing a number.
        """
        recomputed = 35.0 * 0.606 / (343.0 ** 2)
        assert recomputed == pytest.approx(hl.ARRIVAL_ONE_WAY_BUDGET_S, rel=0.02)

    def test_the_budget_in_metres_is_a_few_centimetres(self):
        assert 0.05 < hl.ARRIVAL_ONE_WAY_BUDGET_S * hl.C_MPS < 0.08


class TestTheRecordedMeasurementStillReadsTheSameWay:
    """The fixture is the live 2026-09-10 run. It pins the CONCLUSION, not just the plumbing."""

    @pytest.fixture()
    def rows(self):
        with open(FIXTURE) as fh:
            return json.load(fh)

    def test_the_esp_ring_was_unanchored_for_every_trial(self, rows):
        assert {bool(r["ring_anchored"]) for r in rows} == {False}

    def test_exactly_one_board_could_be_aligned_at_all(self, rows):
        out = hl.summarise(rows, rho_min=0.9, min_trials=8)
        ok = [s for s, r in out.items() if r["status"] == "ok"]
        assert len(ok) == 1, out

    def test_that_board_is_refused_for_arrival_use_by_a_wide_margin(self, rows):
        out = hl.summarise(rows, rho_min=0.9, min_trials=8)
        rec = next(r for r in out.values() if r["status"] == "ok")
        v = hl.arrival_verdict(rec["lag"], hl.HUGBOT_CAPTURE_LATENCY_S, hl.HUGBOT_CAPTURE_SIGMA_S)
        assert v["admissible"] is False
        assert v["over_budget_factor"] > 50

    def test_the_host_backlog_is_not_a_constant(self, rows):
        b = hl._quantiles([r["host_backlog_ms"] for r in rows])
        assert b["spread_ms"] > 1.0, "a constant backlog would be subtractable; this one is not"

    def test_the_columns_disagree_by_more_than_the_array_could_explain(self, rows):
        """The aperture is the hard bound: two mics 0.739 m apart cannot differ by more than
        2.15 ms of flight, whatever the sound was."""
        bound_ms = 1e3 * hl.HUGBOT_ESP_APERTURE_M / hl.C_MPS
        skew = hl.channel_skew(rows, rho_min=0.9)
        measured = [r for r in skew.values() if r["status"] == "ok"]
        assert measured, "the fixture should contain at least one jointly-aligned pair"
        for rec in measured:
            assert rec["skew"]["spread_ms"] > bound_ms

    def test_the_report_renders_without_a_live_robot(self, rows):
        text = hl.render(rows, rho_min=0.9, min_trials=8, max_spread_ms=None)
        assert "REFUSED" in text
        assert "frame_time_ns() returns None" in text


class TestTheToolDoesNotReachOutsideItsOwnCheckout:
    def test_the_fixture_lives_in_this_repo(self):
        root = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))
        assert os.path.realpath(FIXTURE).startswith(root + os.sep)

    def test_no_default_path_points_at_another_clone(self):
        """The one absolute default is hugbot's own /dev/shm ring, which is a device-like path on
        the robot, not a checkout. Anything under a home directory would be reading someone
        else's tree."""
        assert not any(
            tok.startswith("/home/") or tok.startswith("~")
            for tok in (hl.read_ring.__defaults__ or ())
            if isinstance(tok, str))
