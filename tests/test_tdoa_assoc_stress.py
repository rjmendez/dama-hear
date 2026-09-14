"""Unit tests for the association stress-testing harness (hear.sim.association_stress)."""
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear.backend.survey import Survey, from_dict
from hear.sim.association_stress import (
    AssociationStressTest,
    format_benchmark_summary,
    simulate_multi_source_burst,
)
from hear.sim.forward_model import SimNode


def _make_survey():
    """Build a 4-node non-collinear test survey."""
    return from_dict({
        "frame": "enu_local",
        "units": "m",
        "nodes": [
            {"node_id": 1, "e_m": 0.0, "n_m": 0.0, "u_m": 0.0},
            {"node_id": 2, "e_m": 50.0, "n_m": 0.0, "u_m": 0.0},
            {"node_id": 3, "e_m": 25.0, "n_m": 40.0, "u_m": 0.0},
            {"node_id": 4, "e_m": 0.0, "n_m": 40.0, "u_m": 5.0},
        ],
    })


def test_simulate_multi_source_burst():
    sources = simulate_multi_source_burst(
        num_sources=3,
        dt_range=(0.010, 0.050),
        spatial_bounds=((-50.0, 50.0), (-50.0, 50.0), (0.0, 5.0)),
        seed=123,
    )
    assert len(sources) == 3
    for i, s in enumerate(sources):
        assert s["source_id"] == i
        assert s["pos"].shape == (3,)
        if i > 0:
            dt = s["t0_s"] - sources[i - 1]["t0_s"]
            assert 0.009 <= dt <= 0.051


def test_generate_scenario_clean():
    survey = _make_survey()
    harness = AssociationStressTest(survey)
    sources = [
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.0},
        {"pos": (-10.0, 20.0, 2.0), "t0_s": 105.0},
    ]
    scenario = harness.generate_scenario(sources, seed=42)
    dets = scenario["detections"]
    assert len(dets) == 8  # 2 sources * 4 nodes
    assert scenario["direct_arrival_count"] == 8
    for d in dets:
        assert d["onset_found"] is True
        assert d["utc_trusted"] is True


def test_generate_scenario_with_clutter_drop_and_multipath():
    survey = _make_survey()
    harness = AssociationStressTest(survey)
    sources = [{"pos": (15.0, 15.0, 1.0), "t0_s": 100.0}]

    scenario = harness.generate_scenario(
        sources,
        clutter_rate_hz=10.0,
        drop_rate=0.25,
        multipath_rate=0.5,
        seed=123,
    )
    dets = scenario["detections"]
    assert len(dets) > 0
    clutter_dets = [d for d in dets if d.get("_is_clutter")]
    multipath_dets = [d for d in dets if d.get("_is_multipath")]
    direct_dets = [d for d in dets if d.get("_is_direct")]

    assert len(clutter_dets) > 0
    assert len(direct_dets) < 4  # dropped arrivals
    assert len(multipath_dets) >= 0


def test_association_and_metrics_well_spaced_sources():
    survey = _make_survey()
    harness = AssociationStressTest(survey, fixed_up_m=0.0)
    # Sources well separated in time (> window_s) and inside array footprint
    sources = [
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.0},
        {"pos": (15.0, 20.0, 0.0), "t0_s": 102.0},
    ]

    res = harness.run_test(sources, clutter_rate_hz=0.0, timing_noise_s=0.0001, seed=42)
    metrics = res["metrics"]

    assert metrics["num_events"] == 2
    assert metrics["grouping_precision"] == pytest.approx(1.0)
    assert metrics["grouping_recall"] == pytest.approx(1.0)
    assert metrics["split_event_rate"] == pytest.approx(0.0)
    assert metrics["merged_event_rate"] == pytest.approx(0.0)
    assert metrics["solver_success_rate"] == pytest.approx(1.0)
    assert metrics["mean_position_error_m"] < 1.0


def test_association_rapid_burst_merged_event_behavior():
    survey = _make_survey()
    harness = AssociationStressTest(survey)
    # Rapid burst from different positions (dt = 15 ms < max_window_s ~200 ms)
    sources = [
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.000},
        {"pos": (20.0, 20.0, 1.0), "t0_s": 100.015},
    ]

    res = harness.run_test(sources, clutter_rate_hz=0.0, seed=42)
    metrics = res["metrics"]
    assoc = res["association"]

    # Interleaved arrivals from two rapid sources merge into 1 unsolvable event
    assert len(assoc["events"]) == 1
    assert len(assoc["rejected"]) > 0
    assert metrics["merged_event_rate"] == pytest.approx(1.0)
    assert metrics["grouping_precision"] == pytest.approx(0.5)


def test_association_rapid_burst_colocated_rejection_behavior():
    survey = _make_survey()
    harness = AssociationStressTest(survey, fixed_up_m=0.0)
    # Rapid co-located burst (dt = 15 ms < max_window_s ~200 ms)
    sources = [
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.000},
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.015},
    ]

    res = harness.run_test(sources, clutter_rate_hz=0.0, timing_noise_s=0.0001, seed=42)
    metrics = res["metrics"]
    assoc = res["association"]

    assert len(assoc["events"]) == 1
    assert any(r["reason"] == "duplicate_node_in_group" for r in assoc["rejected"])
    assert metrics["grouping_precision"] == pytest.approx(1.0)
    assert metrics["grouping_recall"] == pytest.approx(0.5)


def test_benchmark_clutter_density():
    survey = _make_survey()
    harness = AssociationStressTest(survey)
    sources = [
        {"pos": (10.0, 10.0, 0.0), "t0_s": 100.0},
        {"pos": (-10.0, 25.0, 1.0), "t0_s": 105.0},
    ]

    rates = [0.0, 5.0, 20.0]
    bench = harness.benchmark_clutter_density(
        sources,
        clutter_rates=rates,
        num_trials=2,
        seed=10,
    )

    assert bench["clutter_rates"] == rates
    for r in rates:
        assert r in bench["results_by_rate"]
        m = bench["results_by_rate"][r]
        assert "grouping_precision_mean" in m
        assert "solver_success_rate_mean" in m

    summary = format_benchmark_summary(bench)
    assert "Clutter (Hz)" in summary
    assert "Precision" in summary
    assert "Solver Success" in summary


def test_dict_and_simnode_survey_normalization():
    # Dict survey
    dict_survey = {
        1: (0.0, 0.0, 0.0),
        2: (50.0, 0.0, 0.0),
        3: (25.0, 40.0, 0.0),
        4: (0.0, 40.0, 5.0),
    }
    harness1 = AssociationStressTest(dict_survey)
    assert harness1.survey.diameter_m() > 40.0

    # SimNode list survey
    simnodes = [
        SimNode(1, (0.0, 0.0, 0.0)),
        SimNode(2, (50.0, 0.0, 0.0)),
        SimNode(3, (25.0, 40.0, 0.0)),
        SimNode(4, (0.0, 40.0, 5.0)),
    ]
    harness2 = AssociationStressTest(simnodes)
    assert harness2.survey.diameter_m() > 40.0
