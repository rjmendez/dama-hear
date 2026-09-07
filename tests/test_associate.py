"""Event association. The cadence and phantom tests are the point of this file."""
import itertools
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.backend import associate as AS  # noqa: E402
from hear.solve import shockwave as SW  # noqa: E402


class Stub:
    """The whole survey contract associate needs: membership, position, diameter."""

    def __init__(self, nodes):
        self.nodes = {int(k): np.asarray(v, float) for k, v in nodes.items()}

    def __contains__(self, node_id):
        return int(node_id) in self.nodes

    def position(self, node_id):
        return self.nodes[int(node_id)]

    def diameter_m(self):
        return max(float(np.linalg.norm(a - b))
                   for a, b in itertools.combinations(self.nodes.values(), 2))


def _det(node_id, t_utc_s, seq=0, retrigger=False, iface="lora0"):
    return {"node_id": node_id, "seq": seq, "t_utc_s": t_utc_s, "peak": 1000,
            "ref_db": -20.0, "retrigger": retrigger, "profile_id": 0, "q": None,
            "iface": iface}


def _conserved(got, n_input):
    assert got["n_input"] == n_input
    accounted = (sum(e["n_nodes"] for e in got["events"])
                 + len(got["rejected"]) + len(got["duplicates"]))
    assert accounted == n_input, "%d of %d detections accounted for" % (accounted, n_input)
    for r in got["rejected"] + got["duplicates"]:
        assert r["reason"] in AS.REASONS
        assert any(ch.isdigit() for ch in r["detail"]), "detail must carry the numbers"


TIGHT = Stub({1: (0, 0, 0), 2: (10, 0, 0), 3: (5, 8, 0)})          # diameter 10 m, window 59 ms
SPREAD = Stub({1: (0, 0, 0), 2: (0, 40, 0), 3: (40, 0, 0), 4: (5, 0, 0)})   # diameter 56.6 m


def _round(base_t, seq):
    return [_det(1, base_t, seq), _det(2, base_t + 0.005, seq), _det(3, base_t + 0.008, seq)]


class TestCadences:
    """Measured spacings: 85 ms at 700 rpm, ~328 ms burst, 522 ms across a 19-round string."""

    @pytest.mark.parametrize("spacing_s", [0.085, 0.328, 0.522])
    def test_measured_round_cadences_stay_separate_events(self, spacing_s):
        dets = [d for r in range(3) for d in _round(100.0 + r * spacing_s, r)]
        got = AS.associate(dets, TIGHT)
        assert len(got["events"]) == 3, "rounds merged at %.0f ms" % (spacing_s * 1e3)
        assert [e["n_nodes"] for e in got["events"]] == [3, 3, 3]
        assert got["rejected"] == []
        assert [e["t0_utc_s"] for e in got["events"]] == pytest.approx(
            [100.0 + r * spacing_s for r in range(3)], abs=1e-9)
        _conserved(got, len(dets))

    def test_a_fixed_600_ms_window_swallows_the_next_two_rounds(self):
        """What the sibling project's constant costs: three rounds in, one event out."""
        dets = [d for r in range(3) for d in _round(100.0 + r * 0.085, r)]
        got = AS.associate(dets, TIGHT, window_s=0.600)
        assert len(got["events"]) == 1
        assert {r["reason"] for r in got["rejected"]} == {"duplicate_node_in_group"}
        assert len(got["rejected"]) == 6
        _conserved(got, len(dets))


class TestPairwiseGate:
    """A node that missed round 1 and sent round 2 is the phantom, and geometry catches it."""

    def _phantom(self, **kw):
        dets = [_det(1, 100.000, 0), _det(2, 100.030, 0), _det(3, 100.050, 0),
                _det(4, 100.085, 1)]          # node 4 skipped round 1
        return dets, AS.associate(dets, SPREAD, **kw)

    def test_a_late_round_from_a_nearby_node_is_rejected_by_its_own_separation(self):
        dets, got = self._phantom()
        assert len(got["events"]) == 1 and got["events"][0]["n_nodes"] == 3
        assert 4 not in got["events"][0]["node_ids"]
        bad = [r for r in got["rejected"] if r["node_id"] == 4]
        assert len(bad) == 1
        assert bad[0]["reason"] == "pairwise_dt_exceeds_geometry"
        # node 4 is 5 m from node 1: 5/343.42 + 30 ms = 44.6 ms, and it arrived 85 ms late.
        assert "dt 85.0 ms" in bad[0]["detail"]
        assert "> 44.6 ms" in bad[0]["detail"]
        assert bad[0]["seed_node_id"] == 1
        _conserved(got, len(dets))

    def test_a_600_ms_margin_admits_the_phantom(self):
        """The other half of the guard: widen the margin and the bogus node joins the solve."""
        dets, got = self._phantom(margin_s=0.600)
        assert len(got["events"]) == 1
        assert got["events"][0]["node_ids"] == [1, 2, 3, 4]
        assert got["rejected"] == []
        _conserved(got, len(dets))


class TestWindow:
    def test_window_is_computed_from_the_diameter_not_typed_in(self):
        small = Stub({1: (0, 0, 0), 2: (100, 0, 0), 3: (50, 1, 0)})
        big = Stub({1: (0, 0, 0), 2: (300, 0, 0), 3: (150, 3, 0)})
        w1 = AS.max_window_s(small)
        w3 = AS.max_window_s(big)
        assert w1 == pytest.approx(100.0 / SW.sound_speed(20.0) + AS.MARGIN_S, abs=1e-12)
        assert (w3 - AS.MARGIN_S) == pytest.approx(3.0 * (w1 - AS.MARGIN_S), rel=1e-9)

    def test_window_tightens_as_the_air_warms(self):
        small = Stub({1: (0, 0, 0), 2: (100, 0, 0), 3: (50, 1, 0)})
        assert AS.max_window_s(small, temp_c=35.0) < AS.max_window_s(small, temp_c=0.0)

    def test_c_comes_from_shockwave(self):
        got = AS.associate(_round(100.0, 0), TIGHT, temp_c=23.0)
        assert got["sound_speed_mps"] == pytest.approx(345.238, abs=1e-3)


class TestEarliestWins:
    def test_a_retrigger_cannot_displace_the_arrival_already_in_the_group(self):
        """60% of raw detections fired within 60 ms of the previous one, inside one blast's decay
        (docs/findings-2026-09-05.md:11). Keeping the last hands the group a reflection."""
        dets = _round(100.0, 0) + [_det(1, 100.030, 1, retrigger=True)]
        got = AS.associate(dets, TIGHT)
        ev = got["events"][0]
        assert ev["n_nodes"] == 3
        held = [d for d in ev["detections"] if d["node_id"] == 1]
        assert len(held) == 1
        assert held[0]["seq"] == 0 and held[0]["retrigger"] is False
        assert ev["t0_utc_s"] == pytest.approx(100.0, abs=1e-9)
        assert [r["reason"] for r in got["rejected"]] == ["duplicate_node_in_group"]
        assert got["rejected"][0]["seq"] == 1
        _conserved(got, len(dets))


class TestBookkeeping:
    def test_the_same_frame_over_two_interfaces_appears_once(self):
        dets = _round(100.0, 0) + [_det(1, 100.0, 0, iface="mqtt")]
        got = AS.associate(dets, TIGHT)
        ev = got["events"][0]
        assert ev["n_nodes"] == 3
        assert [d["iface"] for d in ev["detections"] if d["node_id"] == 1] == ["lora0"]
        assert len(got["duplicates"]) == 1
        assert got["duplicates"][0]["reason"] == "duplicate_seq"
        assert got["rejected"] == []
        _conserved(got, len(dets))

    def test_a_node_outside_the_survey_is_rejected_not_dropped(self):
        dets = _round(100.0, 0) + [_det(9, 100.004, 0)]
        got = AS.associate(dets, TIGHT)
        assert got["events"][0]["node_ids"] == [1, 2, 3]
        assert [r["reason"] for r in got["rejected"]] == ["unknown_node"]
        assert "9" in got["rejected"][0]["detail"]
        _conserved(got, len(dets))

    def test_a_group_below_min_nodes_emits_nothing_and_rejects_every_member(self):
        dets = [_det(1, 100.0, 0), _det(2, 100.005, 0)]
        got = AS.associate(dets, TIGHT, min_nodes=3)
        assert got["events"] == []
        assert [r["reason"] for r in got["rejected"]] == ["too_few_nodes"] * 2
        _conserved(got, len(dets))

    def test_node_ids_and_arrivals_are_index_aligned_and_sorted_by_arrival(self):
        """A row/arrival swap mislabels every solve, and no residual would show it."""
        dets = [_det(3, 100.008, 0), _det(1, 100.000, 0), _det(2, 100.005, 0)]
        ev = AS.associate(dets, TIGHT)["events"][0]
        assert ev["node_ids"] == [1, 2, 3]
        assert ev["arrivals"] == pytest.approx([100.000, 100.005, 100.008], abs=1e-9)
        assert [d["node_id"] for d in ev["detections"]] == ev["node_ids"]
        assert ev["span_s"] == pytest.approx(0.008, abs=1e-9)
        assert ev["n_equations"] == 2
