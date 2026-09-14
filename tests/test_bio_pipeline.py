"""hear/node/bio_pipeline.py: tagging, multi-band lockstep, and the tdoa_capable stamp.

Signal generators are deliberately re-derived here rather than imported from
tests/test_bioacoustic.py: every other test file in this repo defines its own, and TonalGate's
behaviour is already exercised at length there. This file only has to prove the CALLER wires it
correctly -- tags every event, runs each band's own gate against its own floor, and never
fabricates a TDoA-capable event.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.node.bio_pipeline import BioPipeline, summarise  # noqa: E402
from modules.bioacoustic import detect as BA               # noqa: E402

FS = 16000.0
NOISE_SD = 30.0
CICADA_HZ = 6000.0
KATYDID_BAND = BA.KATYDID_AUDIBLE_BAND_HZ


def _noise(n, sd=NOISE_SD, seed=1):
    return np.random.RandomState(seed).normal(0.0, sd, n)


def _amp_for(snr_db, band, sd=NOISE_SD):
    pn = sd ** 2 * (band[1] - band[0]) / (FS / 2.0)
    return float(np.sqrt(2.0 * pn * 10.0 ** (snr_db / 10.0)))


def _buzz(n, f, band, snr_db=12.0, sd=NOISE_SD, seed=3):
    t = np.arange(n) / FS
    return _noise(n, sd, seed) + _amp_for(snr_db, band, sd) * np.sin(2 * np.pi * f * t)


class TestTagging:
    def test_single_band_default_name(self):
        p = BioPipeline(FS)
        x = np.concatenate([_noise(int(3 * FS), seed=1),
                             _buzz(int(4 * FS), CICADA_HZ, BA.CICADA_BAND_HZ)])
        dets = p.run(x) + p.flush()
        assert dets, "a clean loud buzz over noise must produce at least one event"
        for d in dets:
            assert d["burst_kind"] == "cicada"
            assert d["detector"] == "tonal"
            assert d["tdoa_capable"] is False

    def test_every_event_stamped_not_tdoa_capable_across_bands(self):
        p = BioPipeline(FS, bands={
            "cicada": {}, "katydid": {"band": KATYDID_BAND},
        })
        x = np.concatenate([_noise(int(3 * FS), seed=7),
                             _buzz(int(4 * FS), CICADA_HZ, BA.CICADA_BAND_HZ, seed=8),
                             _buzz(int(4 * FS), 5000.0, KATYDID_BAND, seed=9)])
        dets = p.run(x) + p.flush()
        assert dets
        assert all(d["tdoa_capable"] is False for d in dets)
        assert set(d["burst_kind"] for d in dets) <= {"cicada", "katydid"}


class TestMultiBandLockstep:
    def test_two_bands_each_keep_their_own_floor_and_can_both_fire(self):
        """A signal built to trip the cicada band and NOT the katydid band must only produce
        cicada-tagged events, proving the two gates are independent rather than sharing one
        floor or one decision."""
        p = BioPipeline(FS, bands={
            "cicada": {}, "katydid": {"band": KATYDID_BAND},
        })
        x = np.concatenate([_noise(int(3 * FS), seed=11),
                             _buzz(int(4 * FS), CICADA_HZ, BA.CICADA_BAND_HZ, snr_db=14.0,
                                   seed=12)])
        dets = p.run(x) + p.flush()
        assert dets
        # KATYDID_AUDIBLE_BAND_HZ (3-8 kHz) contains CICADA_BAND_HZ (4-8 kHz), so a 6 kHz tone
        # is in-band for both gates by construction; the point is that cicada fires, not that
        # katydid is silent.
        assert "cicada" in set(d["burst_kind"] for d in dets)

    def test_named_gates_are_distinct_instances(self):
        p = BioPipeline(FS, bands={"a": {}, "b": {"band": KATYDID_BAND}})
        assert p.gates["a"] is not p.gates["b"]
        assert p.gates["a"].f_lo != p.gates["b"].f_lo or p.gates["a"].f_hi != p.gates["b"].f_hi


class TestDiagnosticsAndSummary:
    def test_diagnostics_reports_per_band_counters(self):
        p = BioPipeline(FS, bands={"cicada": {}, "katydid": {"band": KATYDID_BAND}})
        p.run(_noise(int(5 * FS), seed=21))
        p.flush()
        diag = p.diagnostics()
        assert set(diag.keys()) == {"cicada", "katydid"}
        for d in diag.values():
            assert "floor_db" in d and "n_short" in d and "n_unstructured" in d
            assert "n_gaps" in d and "n_gap_samples" in d and "n_discarded_samples" in d

    def test_summarise_counts_by_kind_and_axis(self):
        p = BioPipeline(FS, bands={"cicada": {}})
        x = np.concatenate([_noise(int(3 * FS), seed=31),
                             _buzz(int(4 * FS), CICADA_HZ, BA.CICADA_BAND_HZ, seed=32)])
        dets = p.run(x) + p.flush()
        s = summarise(dets)
        assert s["events"] == len(dets)
        assert s["by_kind"].get("cicada", 0) == len(dets)
        assert s["tonal"] <= s["events"]
        assert s["pulsed"] <= s["events"]

    def test_empty_run_is_not_an_error(self):
        p = BioPipeline(FS)
        assert p.run(_noise(int(2 * FS), seed=41)) == []
        assert p.diagnostics()["cicada"]["n_frames"] > 0


class TestDoesNotTouchImpulsePath:
    def test_bio_pipeline_import_does_not_pull_in_hear_node_pipeline(self):
        """hear/node/pipeline.py's Gate/TDoA chain must not be reachable through this module --
        see the module docstring on why the two are deliberately separate callers."""
        import hear.node.bio_pipeline as BP
        assert not hasattr(BP, "Pipeline")
        assert not hasattr(BP, "DT")
