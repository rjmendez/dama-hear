"""The pair census must count what `associate` admits, and its null must be a real null.

⚠️IT IS A BEFORE/AFTER INSTRUMENT, so the failure that matters is not a wrong absolute count but
a count that moves for a reason other than the pool changing. Both halves are pinned here: an
arrival that only one node heard contributes to no pair, a recovered arrival that lands inside
another node's admissible window contributes to exactly one, and the circular-shift null leaves
the pairs it does not touch alone -- the control that says the shift shifts what it claims to.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.backend import survey as SV                             # noqa: E402
from tools import hear_pair_census as PC                          # noqa: E402

# nyquist at the origin, mach 16.6 m west, rankine 11.5 m away: the real survey's geometry,
# rounded. window = diameter/c + 30 ms.
SURVEY = SV.from_dict({
    "frame": "enu_local", "units": "m",
    "nodes": [{"node_id": 1, "name": "nyquist", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0},
              {"node_id": 2, "name": "mach", "e_m": -16.6, "n_m": -0.3, "u_m": 3.0},
              {"node_id": 3, "name": "rankine", "e_m": -4.6, "n_m": 10.5, "u_m": 3.0}]})

T0 = 1789045083.0


def _det(nid, t, seq=0):
    return {"node_id": nid, "seq": seq, "t_utc_s": t, "node": {1: "nyquist", 2: "mach",
                                                               3: "rankine"}[nid]}


class TestCensus:
    def test_an_arrival_only_one_node_heard_pairs_with_nobody(self):
        dets = [_det(1, T0 + i * 60.0, i) for i in range(5)]
        c = PC.census(dets, SURVEY)
        assert c["pairs"] == {}
        assert c["n_input"] == 5

    def test_two_arrivals_inside_the_geometry_are_one_pair_episode(self):
        # 20 ms apart: well inside mach-rankine's own separation over c.
        c = PC.census([_det(2, T0), _det(3, T0 + 0.020)], SURVEY)
        assert c["pairs"] == {"2-3": 1}
        assert c["events_by_n_nodes"] == {2: 1}

    def test_an_arrival_the_geometry_forbids_is_not_an_episode(self):
        # 5 s apart is 1715 m of propagation across a 20 m array.
        c = PC.census([_det(2, T0), _det(3, T0 + 5.0)], SURVEY)
        assert c["pairs"] == {}

    def test_a_three_node_event_counts_for_all_three_pairs(self):
        c = PC.census([_det(1, T0), _det(2, T0 + 0.02), _det(3, T0 + 0.03)], SURVEY)
        assert c["pairs"] == {"1-2": 1, "1-3": 1, "2-3": 1}
        assert c["events_by_n_nodes"] == {3: 1}

    def test_a_recovered_arrival_shows_up_as_a_new_episode(self):
        # exactly the before/after this instrument exists for: one row added to rankine turns a
        # solo mach detection into a mach x rankine episode and nothing else moves.
        base = [_det(2, T0 + i * 300.0, i) for i in range(4)]
        assert PC.census(base, SURVEY)["pairs"] == {}
        after = PC.census(base + [_det(3, T0 + 300.0 + 0.015)], SURVEY)
        assert after["pairs"] == {"2-3": 1}


class TestTheNull:
    def test_the_shift_leaves_untouched_pairs_alone(self):
        # nyquist x mach must not move when RANKINE's clock is the one being shifted.
        dets = ([_det(1, T0 + i * 120.0, i) for i in range(6)]
                + [_det(2, T0 + i * 120.0 + 0.02, i) for i in range(6)]
                + [_det(3, T0 + 37.0 + i * 120.0, i) for i in range(6)])
        truth = PC.census(dets, SURVEY)["pairs"]
        null = PC.null_distribution(dets, SURVEY, node_id=3, draws=25)
        assert null["1-2"]["median"] == truth["1-2"]
        assert null["1-2"]["max"] == truth["1-2"]

    def test_the_null_reports_a_distribution_not_a_point(self):
        dets = ([_det(1, T0 + i * 90.0, i) for i in range(8)]
                + [_det(3, T0 + i * 90.0 + 0.01, i) for i in range(8)])
        null = PC.null_distribution(dets, SURVEY, node_id=3, draws=30)
        assert set(null["1-3"]) >= {"median", "p95", "max", "mean", "draws"}
        assert null["1-3"]["draws"] == 30
        # A perfectly coincident pair must beat its own shifted null; if it did not, the census
        # would be measuring the window and not the coincidence.
        assert PC.census(dets, SURVEY)["pairs"]["1-3"] > null["1-3"]["p95"]

    def test_a_node_with_one_arrival_has_no_null(self):
        assert PC.null_distribution([_det(1, T0), _det(3, T0 + 0.01)], SURVEY, 3, draws=5) == {}


# ================================================================= PART B: the receiver census
def _row(node, source="node", anchored=True, sync_sigma_ns=None):
    return {"node": node, "source": source, "anchored": anchored, "sync_sigma_ns": sync_sigma_ns}


class TestReceiverCensus:
    """PART B. hear/nodeclass.py's own two predicates, applied per receiver rather than at the
    door of one solve -- plus the geometry question nodeclass never asks: does this receiver
    even HAVE a position to contribute an arrival at."""

    def test_a_surveyed_node_with_unstated_class_is_assumed_xiao_s3_pps(self):
        rep = PC.receiver_census([_row("nyquist")], SURVEY)
        r = rep["nyquist"]
        assert r["class"] == "xiao-s3-pps" and r["class_assumed"]
        assert r["class_clock_admissible"] is True
        assert r["class_bias_bounded"] is True
        assert r["has_survey_position"] is True
        assert r["geometry_if_admitted"]["n_existing_arrival_receivers"] == 2

    def test_a_phone_with_no_survey_entry_cannot_contribute_an_arrival_at_all(self):
        rep = PC.receiver_census([_row("fancyantsy", source="phone")], SURVEY)
        r = rep["fancyantsy"]
        assert r["has_survey_position"] is False
        assert r["geometry_if_admitted"] is None
        assert "no survey entry" in r["position_note"]
        assert r["class"] == "gotchi-phone" and r["class_assumed"]
        # the class-wide bias bound: gotchi-phone's 13.122 ms is 143x the 91.5 us bound
        assert r["class_bias_bounded"] is False

    def test_position_accuracy_reports_the_budget_multiple_it_implies(self):
        rep = PC.receiver_census([_row("fancyantsy", source="phone")], SURVEY,
                                 phone_position_accuracy_m={"fancyantsy": 5.4})
        r = rep["fancyantsy"]
        assert r["position_accuracy_m"] == 5.4
        # 183 us one-way budget * 343 m/s = 0.0628 m; 5.4 m is ~86x that
        assert 80.0 < r["implied_baseline_error_budget_multiple"] < 90.0

    def test_the_deployed_gate_and_the_stated_sigma_gate_can_disagree(self):
        """THE FINDING THIS TABLE EXISTS TO SHOW. A phone stating a sigma comfortably inside the
        129.4 us per-node budget is STILL refused by the deployed gate, because
        nodeclass.stamp_admissible short-circuits on the phone CLASS's clock_admissible() before
        it ever reads the row's own number."""
        rows = [_row("myasshurts", source="phone", sync_sigma_ns=106_038.0) for _ in range(10)]
        rep = PC.receiver_census(rows, SURVEY)
        r = rep["myasshurts"]
        assert r["clock_pass_stated_sigma_only"] == 10   # 106 us < 129.4 us: passes on its own
        assert r["clock_pass_deployed_gate"] == 0        # class short-circuit refuses all 10
        assert r["clock_fail_deployed_gate"] == 10

    def test_a_latency_cal_entry_overrides_the_blended_class_wide_bias_number(self):
        """gotchi-phone's path_bias_s is ONE number standing in for three different handsets. A
        device with its OWN measured entry must be judged on that, not on the blend."""
        cal = {"by_node_id": {"myasshurts": 13_122_000}}       # ns; 13.122 ms, 4.5 m
        rep = PC.receiver_census([_row("myasshurts", source="phone")], SURVEY, latency_cal=cal)
        r = rep["myasshurts"]
        assert r["device_latency_cal_ns"] == 13_122_000
        assert r["device_bias_bounded"] is False            # 4.5 m still exceeds 91.5 us = 31 mm
        assert abs(r["device_bias_m"] - 13.122e-3 * 343.0) < 1e-9

    def test_geometry_if_admitted_reports_the_real_baselines(self):
        # a synthetic 4th node exactly 10 m east of nyquist
        sv4 = SV.from_dict({
            "frame": "enu_local", "units": "m",
            "nodes": [{"node_id": 1, "name": "nyquist", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0},
                     {"node_id": 2, "name": "mach", "e_m": -16.6, "n_m": -0.3, "u_m": 3.0},
                     {"node_id": 3, "name": "rankine", "e_m": -4.6, "n_m": 10.5, "u_m": 3.0},
                     {"node_id": 4, "name": "fourth", "e_m": 10.0, "n_m": 0.0, "u_m": 0.0}]})
        rep = PC.receiver_census([_row("fourth")], sv4, c_mps=343.0)
        g = rep["fourth"]["geometry_if_admitted"]
        assert g["n_existing_arrival_receivers"] == 3
        assert abs(g["separation_m"]["nyquist"] - 10.0) < 1e-9
        assert abs(g["min_separation_m"] - 10.0) < 1e-9
        assert abs(g["pair_bound_ms"]["nyquist"] - 10.0 / 343.0 * 1e3) < 1e-9
        # the pre-existing 3-node array's own diameter is unaffected by adding a 4th
        assert g["old_diameter_m"] > 0.0
        assert g["new_diameter_m"] >= g["old_diameter_m"]

    def test_kv_floats_parses_and_refuses_malformed_input(self):
        assert PC._parse_kv_floats(["a=1.5", "b=2"]) == {"a": 1.5, "b": 2.0}
        with pytest.raises(SystemExit):
            PC._parse_kv_floats(["nope"])
