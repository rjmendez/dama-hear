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
# Diameter 205 m -> window 627 ms, so the seed alone admits a half-second-late node. Nodes 2 and 4
# are 5 m apart, which is the pair that must do the rejecting.
PAIRED = Stub({1: (0, 0, 0), 2: (200, 0, 0), 3: (100, 150, 0), 4: (205, 0, 0)})
# Diameter 128.1 m -> window 403 ms, wider than the 85 ms round cadence.
WIDE = Stub({1: (0, 0, 0), 2: (100, 0, 0), 3: (50, 80, 0), 4: (0, 80, 0)})


def _round(base_t, seq):
    return [_det(1, base_t, seq), _det(2, base_t + 0.005, seq), _det(3, base_t + 0.008, seq)]


class TestArrivalQuality:
    """A detection can be real and still not carry a usable arrival TIME.

    Before this gate existed there was no door to refuse one at: anything naming a known node was
    admitted and any defect in its timestamp went straight into the solve. The two producers that
    already know their own stamp is not a measurement -- the node gate's `onset_found` and the
    phone's `utc_trusted` -- had nowhere to say so.
    """

    def test_absent_flags_mean_usable_so_this_is_a_no_op_on_old_data(self):
        """Most recorded detections predate these fields. Treating absence as suspect would
        retroactively reject the entire existing corpus."""
        got = AS.associate(_round(100.0, 0), TIGHT)
        _conserved(got, 3)
        assert len(got["events"]) == 1
        assert got["events"][0]["n_nodes"] == 3

    def test_a_fabricated_onset_is_refused_not_solved(self):
        """The walk returns the clamp edge, and the arrival is 25 ms early -- 8.6 m.

        ⚠️This was 18.4% of the 228-event reference corpus when the gate was written. Referring
        the fraction to the local trough took that to ZERO, so this is now a BACKSTOP for the
        residual case the trough reference deliberately does not cover: a window whose minimum
        sits at its left edge, where the rise predates the window and referring to it would
        report a confidently late onset instead of an honest clamp."""
        dets = _round(100.0, 0)
        dets[1]["onset_found"] = False
        # min_nodes=2 so the survivors can still form an event. At the default of 3 they cannot,
        # and they are then rejected as too_few_nodes -- which is correct but tests a different
        # thing, and asserting on it here would hide whether the quality gate fired at all.
        got = AS.associate(dets, TIGHT, min_nodes=2)
        _conserved(got, 3)
        assert [r["reason"] for r in got["rejected"]] == ["unusable_arrival"]
        assert "clamp edge" in got["rejected"][0]["detail"]
        # The other two still form an event; one bad node does not cost the others theirs.
        assert got["events"][0]["n_nodes"] == 2

    def test_a_refused_arrival_can_starve_an_event_below_min_nodes(self):
        """The cost, stated rather than discovered later. Refusing one node of three leaves two,
        which the default min_nodes rejects -- so a single fabricated onset can remove an event
        entirely. That is the right trade against solving it 8.6 m wrong, but it is a trade, and
        conservation still names every detection."""
        dets = _round(100.0, 0)
        dets[1]["onset_found"] = False
        got = AS.associate(dets, TIGHT)
        _conserved(got, 3)
        assert got["events"] == []
        # One too_few_nodes row PER GROUP MEMBER (associate.py:237), so the two survivors give
        # two rows, plus the one refused arrival.
        assert sorted(r["reason"] for r in got["rejected"]) == [
            "too_few_nodes", "too_few_nodes", "unusable_arrival"]

    def test_an_untrusted_phone_stamp_is_refused(self):
        dets = _round(100.0, 0)
        dets[2]["utc_trusted"] = False
        got = AS.associate(dets, TIGHT)
        _conserved(got, 3)
        assert got["rejected"][0]["reason"] == "unusable_arrival"
        assert "HAL" in got["rejected"][0]["detail"]

    def test_both_flags_false_names_both(self):
        dets = _round(100.0, 0)
        dets[0]["onset_found"] = False
        dets[0]["utc_trusted"] = False
        got = AS.associate(dets, TIGHT)
        detail = got["rejected"][0]["detail"]
        assert "onset_found=false" in detail and "utc_trusted=false" in detail

    def test_true_flags_pass_through(self):
        dets = _round(100.0, 0)
        for d in dets:
            d["onset_found"] = True
            d["utc_trusted"] = True
        got = AS.associate(dets, TIGHT)
        _conserved(got, 3)
        assert got["events"][0]["n_nodes"] == 3

    def test_only_an_explicit_false_refuses(self):
        """None is not False. A producer that emits the key but could not evaluate it must not be
        read as having declared the arrival bad."""
        assert AS.arrival_is_usable({"onset_found": None}) is True
        assert AS.arrival_is_usable({"onset_found": True}) is True
        assert AS.arrival_is_usable({}) is True
        assert AS.arrival_is_usable({"onset_found": False}) is False

    def test_the_three_values_utc_trusted_can_actually_take(self):
        """⚠️PINS THE OTHER HALF OF THE CONTRACT. `hear.corpus.Record.utc_trusted` returns
        exactly True / False / None -- None for "the producer did not say", which includes every
        node record and every phone row with no clock_tier. Whoever writes the phone-Record ->
        det-dict adapter must not turn that None into a refusal: it would throw away every node
        arrival, which is all of them today.
        """
        assert AS.arrival_is_usable({"utc_trusted": True}) is True
        assert AS.arrival_is_usable({"utc_trusted": None}) is True
        assert AS.arrival_is_usable({"utc_trusted": False}) is False
        # ...and the derived property feeds it the same three values, straight through.
        from hear import corpus as C
        assert AS.arrival_is_usable(
            {"utc_trusted": C.utc_trusted_of({"source": "phone", "clock_tier": "wall"})}) is False
        assert AS.arrival_is_usable(
            {"utc_trusted": C.utc_trusted_of({"source": "node"})}) is True

    def test_losing_every_node_leaves_no_event_and_still_conserves(self):
        dets = _round(100.0, 0)
        for d in dets:
            d["onset_found"] = False
        got = AS.associate(dets, TIGHT)
        _conserved(got, 3)
        assert got["events"] == []
        assert len(got["rejected"]) == 3


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


class TestWideArray:
    """The documented loss: rejection is terminal, so a wide-enough window eats whole rounds."""

    def test_rounds_after_the_first_are_lost_on_an_array_wider_than_the_cadence(self):
        """128.1 m diameter -> 403 ms window against an 85 ms cadence. All four nodes heard all
        three rounds; rounds 2 and 3 come back as rejections, never as events. This is a pin on a
        known cost, not an endorsement -- if re-seeding lands, this test is what has to change."""
        dets = [_det(n, 100.0 + r * 0.085 + i * 0.005, r)
                for r in range(3) for i, n in enumerate((1, 2, 3, 4))]
        got = AS.associate(dets, WIDE)
        assert got["diameter_m"] == pytest.approx(128.06, abs=0.01)
        assert got["window_s"] == pytest.approx(0.4029, abs=1e-4)
        assert got["window_s"] > 0.085, "fixture is void unless the window swallows the cadence"
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3, 4]]
        assert [r["reason"] for r in got["rejected"]] == ["duplicate_node_in_group"] * 8
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

    def test_the_rejecting_member_need_not_be_the_seed(self):
        """The gate is 'for EVERY member', and the phantom fixture cannot show that -- there the
        rejector IS the seed. Here the seed is 205 m away and admits node 4 at 500 ms; only node 2,
        5 m away, refuses it. A seed-only gate solves the phantom."""
        dets = [_det(1, 100.000, 0), _det(2, 100.100, 0), _det(3, 100.200, 0),
                _det(4, 100.500, 1)]          # node 4 skipped a round; 5 m from node 2
        got = AS.associate(dets, PAIRED)
        assert got["window_s"] > 0.5, "fixture is void unless the seed's own window admits node 4"
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3]]
        assert [(r["node_id"], r["reason"]) for r in got["rejected"]] == [
            (4, "pairwise_dt_exceeds_geometry")]
        # The seed is node 1; the member that caught it is node 2.
        assert got["rejected"][0]["seed_node_id"] == 1
        assert "node 4 vs node 2: dt 400.0 ms > 44.6 ms" in got["rejected"][0]["detail"]
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

    @pytest.mark.parametrize("temp_c,margin_s", [(20.0, AS.MARGIN_S), (0.0, 0.010), (35.0, 0.100)])
    def test_associate_reports_exactly_what_max_window_s_says(self, temp_c, margin_s):
        """The documented policy must BE the algorithm's, not a second copy of it."""
        got = AS.associate(_round(100.0, 0), TIGHT, temp_c=temp_c, margin_s=margin_s)
        assert got["window_s"] == AS.max_window_s(TIGHT, temp_c, margin_s)

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

    def test_a_reused_seq_a_window_later_is_a_new_event_not_a_duplicate(self):
        """seq is 8-bit and wraps (hear/wire.py:113); a rebooted node restarts it without wrapping.
        An unbounded (node_id, seq) dedupe collapses two real events into one and calls the second
        a duplicate. Two disjoint 3-node events 200 s apart, every frame seq 7."""
        dets = [d for base in (100.0, 300.0) for d in _round(base, 7)]
        got = AS.associate(dets, TIGHT)
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3], [1, 2, 3]]
        assert [e["t0_utc_s"] for e in got["events"]] == pytest.approx([100.0, 300.0], abs=1e-9)
        assert got["duplicates"] == [] and got["rejected"] == []
        _conserved(got, len(dets))

    def test_a_second_copy_inside_the_window_is_still_a_duplicate(self):
        """The bound must be the window, not equality of timestamps: the two-interface copy can
        arrive with a different receive time and is still one frame."""
        dets = _round(100.0, 0) + [_det(1, 100.020, 0, iface="mqtt")]   # window is 59 ms
        got = AS.associate(dets, TIGHT)
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3]]
        assert [d["iface"] for d in got["events"][0]["detections"] if d["node_id"] == 1] == \
            ["lora0"]
        assert [d["reason"] for d in got["duplicates"]] == ["duplicate_seq"]
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
