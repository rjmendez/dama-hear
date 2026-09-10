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
