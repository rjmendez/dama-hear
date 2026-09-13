"""Event association. The cadence and phantom tests are the point of this file."""
import itertools
import os
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.backend import associate as AS  # noqa: E402
from hear.solve import shockwave as SW  # noqa: E402
from hear.solve import consistency as CONS  # noqa: E402


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
# Diameter 169.7 m -> window 524 ms. The operator is deploying over ~3.5 acres, roughly 170 m,
# where the window is 6.7x today's 78.7 ms and every chance coincidence scales with it. Nothing
# here is a guess about that array's shape; it is a square whose diagonal is the stated size.
VAST = Stub({1: (0, 0, 0), 2: (120, 0, 0), 3: (120, 120, 0), 4: (0, 120, 0)})


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

    def test_a_fixed_600_ms_window_costs_scan_work_now_instead_of_two_rounds(self):
        """What the sibling project's constant costs, re-measured after re-seeding.

        It used to cost two of three rounds. It no longer costs any of them -- the second and
        third rounds are released and seed their own groups -- so what is left to measure is the
        WORK: the same nine detections that the computed 59.1 ms window groups in 6 candidate
        visits take 15 at 600 ms, because every member of rounds 2 and 3 is scanned and refused
        by round 1 before it is reached as a seed. That ratio is the thing that grows: the window
        is the scan's only bound and a constant one is not bounded by the array at all.
        """
        dets = [d for r in range(3) for d in _round(100.0 + r * 0.085, r)]
        wide = AS.associate(dets, TIGHT, window_s=0.600)
        computed = AS.associate(dets, TIGHT)
        assert [e["node_ids"] for e in wide["events"]] == [[1, 2, 3]] * 3
        assert [e["node_ids"] for e in computed["events"]] == [[1, 2, 3]] * 3
        assert [e["t0_utc_s"] for e in wide["events"]] == \
            pytest.approx([e["t0_utc_s"] for e in computed["events"]], abs=1e-9)
        assert wide["rejected"] == [] and computed["rejected"] == []
        assert wide["refusals"]["duplicate_node_in_group"] == 9
        assert computed["refusals"]["duplicate_node_in_group"] == 0
        assert (wide["scan_candidate_visits"], computed["scan_candidate_visits"]) == (15, 6)
        _conserved(wide, len(dets))
        _conserved(computed, len(dets))


class TestWideArray:
    """The loss this module used to pin, and now pins the recovery of.

    A rejected candidate used to be terminal, so once the window (d/c + margin) exceeded the
    round spacing, round 2 landed inside round 1's scan, every node of it was
    `duplicate_node_in_group`, and the round was gone. On the 128.1 m fixture that was 1 event
    of 3 and 8 rejections. This is the scale the array is going to -- 16.9 m to roughly 170 m,
    window 78.7 ms to 520 ms -- so it is also the scale the release rule has to survive.
    """

    def test_every_round_is_recovered_on_an_array_wider_than_the_cadence(self):
        dets = [_det(n, 100.0 + r * 0.085 + i * 0.005, r)
                for r in range(3) for i, n in enumerate((1, 2, 3, 4))]
        got = AS.associate(dets, WIDE)
        assert got["diameter_m"] == pytest.approx(128.06, abs=0.01)
        assert got["window_s"] == pytest.approx(0.4029, abs=1e-4)
        assert got["window_s"] > 0.085, "fixture is void unless the window swallows the cadence"
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3, 4]] * 3
        assert [e["t0_utc_s"] for e in got["events"]] == pytest.approx(
            [100.0, 100.085, 100.170], abs=1e-9)
        assert got["rejected"] == []
        # 12 releases: rounds 2 and 3 are each scanned and refused by round 1, and round 3 again
        # by round 2. Not rows -- a count; see the module docstring for why.
        assert got["refusals"]["duplicate_node_in_group"] == 12
        _conserved(got, len(dets))

    @pytest.mark.parametrize("cadence_s", [0.085, 0.328, 0.522])
    def test_the_same_holds_at_the_scale_the_array_is_going_to(self, cadence_s):
        """169.7 m diameter -> 520 ms window, which swallows all three MEASURED cadences. Before
        the release rule this fixture returned 1 event at 85 ms and 2 at 328 ms."""
        dets = [_det(n, 100.0 + r * cadence_s + i * 0.005, r)
                for r in range(3) for i, n in enumerate((1, 2, 3, 4))]
        got = AS.associate(dets, VAST)
        assert got["window_s"] == pytest.approx(0.52416, abs=1e-4)
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3, 4]] * 3
        assert [e["t0_utc_s"] for e in got["events"]] == pytest.approx(
            [100.0 + r * cadence_s for r in range(3)], abs=1e-9)
        _conserved(got, len(dets))

    def test_a_node_that_misses_a_round_is_still_the_case_this_does_not_resolve(self):
        """⚠️RELEASING THE DUPLICATE DOES NOT MAKE THE AMBIGUOUS CASE SOLVABLE, AND IT CHANGES
        WHICH WAY IT FAILS. Node 3 is silent for round 2. On this 128.1 m array node 3's ROUND 3
        arrival is 95 ms after round 2's seed and the pair bound for nodes 1 and 3 is 94.3 m /
        343.4 + 30 ms = 305 ms, so the geometry gate has nothing to object to: round 2 absorbs it
        and round 3 collapses. Consuming the duplicate gave 1 event and seven rejections; this
        gives 2 events, of which the second is a MIS-ASSOCIATION of round 2 with one arrival from
        round 3. Both lose round 3. Neither is a solve, and the module's docstring already says
        this case is not resolved here -- what this test pins is that the new failure mode is
        known and named rather than discovered in the field.
        """
        dets = [_det(n, 100.0 + r * 0.085 + i * 0.005, r)
                for r in range(3) for i, n in enumerate((1, 2, 3, 4))
                if not (r == 1 and n == 3)]
        got = AS.associate(dets, WIDE)
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3, 4], [1, 2, 4, 3]]
        assert got["events"][1]["arrivals"] == pytest.approx(
            [100.085, 100.090, 100.100, 100.180], abs=1e-9)
        assert got["events"][1]["span_s"] == pytest.approx(0.095, abs=1e-9)
        # ...and the arrival it absorbed really is admissible against every member it joined.
        assert 0.095 < 94.34 / got["sound_speed_mps"] + got["margin_s"]
        _conserved(got, len(dets))


class TestPairwiseGate:
    """A node that missed round 1 and sent round 2 is the phantom, and geometry catches it."""

    def _phantom(self, **kw):
        dets = [_det(1, 100.000, 0), _det(2, 100.030, 0), _det(3, 100.050, 0),
                _det(4, 100.085, 1)]          # node 4 skipped round 1
        return dets, AS.associate(dets, SPREAD, **kw)

    def test_a_late_round_from_a_nearby_node_is_rejected_by_its_own_separation(self):
        """⚠️THE TERMINAL REASON IS NO LONGER THE REFUSAL, AND THE NUMBERS STILL HAVE TO SURVIVE.
        The gate still refuses node 4 -- that is what keeps it out of the event -- but the
        refusal no longer consumes it, so node 4 goes on to seed a group of one and ends as
        `too_few_nodes`. The numbers that refused it would then be printed nowhere, which is why
        they ride along in the detail."""
        dets, got = self._phantom()
        assert len(got["events"]) == 1 and got["events"][0]["n_nodes"] == 3
        assert 4 not in got["events"][0]["node_ids"]
        bad = [r for r in got["rejected"] if r["node_id"] == 4]
        assert len(bad) == 1
        assert bad[0]["reason"] == "too_few_nodes"
        assert got["refusals"]["pairwise_dt_exceeds_geometry"] == 1
        # node 4 is 5 m from node 1: 5/343.42 + 30 ms = 44.6 ms, and it arrived 85 ms late.
        assert "dt 85.0 ms" in bad[0]["detail"]
        assert "> 44.6 ms" in bad[0]["detail"]
        assert bad[0]["seed_node_id"] == 4, "it seeds its own group once it is not consumed"
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
        assert [(r["node_id"], r["reason"]) for r in got["rejected"]] == [(4, "too_few_nodes")]
        assert got["refusals"]["pairwise_dt_exceeds_geometry"] == 1
        # The group it was refused from was seeded by node 1; the member that caught it is node 2.
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
        # The retrigger is 30 ms out, which is the same-node bound exactly (d = 0, so the bound
        # IS the margin), so it is released and seeds a group of one. It displaced nothing, which
        # is what this test is about -- but the terminal reason is now `too_few_nodes` and the
        # release is counted, not rowed.
        assert [r["reason"] for r in got["rejected"]] == ["too_few_nodes"]
        assert got["rejected"][0]["seq"] == 1
        assert got["refusals"]["duplicate_node_in_group"] == 1
        assert "returned to the pool" in got["rejected"][0]["detail"]
        _conserved(got, len(dets))

    def test_inside_the_same_node_bound_the_retrigger_stays_terminal(self):
        """The other side of the boundary, and the case the 60%-within-60 ms measurement is
        actually about: a second arrival closer to the one already held than that node's own
        bound (d = 0 m / c + 30 ms margin) cannot be a different event, so it is consumed."""
        dets = _round(100.0, 0) + [_det(1, 100.020, 1, retrigger=True)]
        got = AS.associate(dets, TIGHT)
        assert [r["reason"] for r in got["rejected"]] == ["duplicate_node_in_group"]
        assert got["refusals"]["duplicate_node_in_group"] == 0
        assert "earliest wins, no replacement" in got["rejected"][0]["detail"]
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


# The 2026-09-09T11:08:41 episode, verbatim from the live pool (74 h, 6,792 anchored node
# arrivals, corpus pod dama-sketch-corpus /pool/corpus). Positions are survey.json's own, so the
# pair bounds here ARE the fleet's: 1-3 is 11.82 m -> 64.1 ms, 1-2 is 16.87 m -> 78.7 ms, 2-3 is
# 16.13 m -> 76.6 ms at 25 C. Offsets are seconds after 1788952121.0.
LIVE = Stub({1: (-0.0, 0.0, 0.0), 2: (-16.602, -0.272, 3.0), 3: (-4.58, 10.48, 3.0)})
LIVE_T = 1788952121.0
LIVE_ARRIVALS = [
    (2, 0.164822), (2, 0.168846), (2, 0.173107), (2, 0.181276), (2, 0.185542),
    (1, 0.271272), (1, 0.321229), (1, 0.333769), (1, 0.338348), (1, 0.359180),
    (3, 0.340823), (3, 0.343133), (3, 0.345298),
    (2, 0.380704), (2, 0.383722),
]


def _live_dets():
    return [{"node_id": n, "seq": i, "t_utc_s": LIVE_T + off, "iface": "lora0"}
            for i, (n, off) in enumerate(LIVE_ARRIVALS)]


def _shape(got):
    return ([(e["node_ids"], tuple(e["arrivals"])) for e in got["events"]],
            sorted((r["node_id"], r["seq"], r["reason"]) for r in got["rejected"]),
            sorted((r["node_id"], r["seq"], r["reason"]) for r in got["duplicates"]),
            dict(got["refusals"]), got["scan_seeds"], got["scan_candidate_visits"])


class TestReseedingARefusedCandidate:
    """The traced live instance. nyquist at ...121.271272 seeds; rankine at ...121.340823 is
    69.6 ms away and the 1-3 bound is 64.1 ms, so the gate refuses it -- correctly. Consuming it
    there ended the episode: 0 events out of an episode whose census holds 24 admissible triples.
    Returning it to the pool costs the gate nothing and delivers one of them.
    """

    def test_the_episode_the_shipped_scan_dropped_is_delivered(self):
        dets = _live_dets()
        got = AS.associate(dets, LIVE, temp_c=25.0)
        assert got["window_s"] == pytest.approx(0.078703, abs=1e-6)
        assert [e["node_ids"] for e in got["events"]] == [[3, 1, 2]]
        ev = got["events"][0]
        assert ev["arrivals"] == pytest.approx(
            [LIVE_T + 0.340823, LIVE_T + 0.359180, LIVE_T + 0.380704], abs=1e-9)
        assert ev["span_s"] == pytest.approx(0.039881, abs=1e-6)
        _conserved(got, len(dets))

    def test_the_delivered_group_is_admissible_at_ZERO_margin(self):
        """⚠️THE POINT IS NOT THAT AN EVENT APPEARED. A group that only fits because of the 30 ms
        margin is one the aperture cannot vouch for, and over the same 74 h the shipped scan's
        three deliveries included exactly one that did not need it. This one does not need it
        either: every pair is inside d/c with the margin removed."""
        got = AS.associate(_live_dets(), LIVE, temp_c=25.0)
        ev, c = got["events"][0], got["sound_speed_mps"]
        for a in range(3):
            for b in range(a + 1, 3):
                d_m = float(np.linalg.norm(LIVE.position(ev["node_ids"][a])
                                           - LIVE.position(ev["node_ids"][b])))
                assert abs(ev["arrivals"][b] - ev["arrivals"][a]) < d_m / c

    def test_the_gate_still_refuses_the_pair_that_made_it_refuse(self):
        """Nothing about the bound moved: the refusal still happens, with the same numbers. What
        changed is only that the refused arrival is still in the pool afterwards."""
        got = AS.associate(_live_dets(), LIVE, temp_c=25.0)
        assert got["refusals"]["pairwise_dt_exceeds_geometry"] == 3
        assert got["margin_s"] == AS.MARGIN_S
        assert abs(0.340823 - 0.271272) > 11.824 / got["sound_speed_mps"] + AS.MARGIN_S


class TestDuplicateReleaseGuard:
    """Both clauses are load-bearing and neither is tuned: the group must already hold every node
    this batch heard from, AND the arrival must be further from the one holding its slot than
    that node's own bound (one node, d = 0, so the bound is the margin).
    """

    def _three_and_a_late_first_node(self, gap_s):
        dets = [_det(n, 100.0 + i * 0.005, 0) for i, n in enumerate((1, 2, 3))]
        dets.append(_det(1, 100.0 + gap_s, 1))
        return dets

    def test_an_incomplete_group_consumes_the_duplicate(self):
        """Node 3 reports, but 400 ms later, so the 1-2 group is not complete when the second
        node-1 arrival reaches it. min_nodes=2 so the pair is an event at all."""
        dets = [_det(1, 100.0, 0), _det(2, 100.005, 0), _det(1, 100.050, 1), _det(3, 100.400, 0)]
        got = AS.associate(dets, TIGHT, min_nodes=2)
        assert got["reporting_nodes"] == 3
        assert [r["reason"] for r in got["rejected"]
                if r["node_id"] == 1 and r["seq"] == 1] == ["duplicate_node_in_group"]
        assert got["refusals"]["duplicate_node_in_group"] == 0
        _conserved(got, len(dets))

    def test_a_complete_group_releases_a_duplicate_beyond_the_same_node_bound(self):
        got = AS.associate(self._three_and_a_late_first_node(0.040), TIGHT, min_nodes=2)
        assert got["reporting_nodes"] == 3
        assert got["refusals"]["duplicate_node_in_group"] == 1
        assert [r["reason"] for r in got["rejected"]] == ["too_few_nodes"]

    def test_a_complete_group_still_consumes_one_inside_the_same_node_bound(self):
        got = AS.associate(self._three_and_a_late_first_node(0.020), TIGHT, min_nodes=2)
        assert got["refusals"]["duplicate_node_in_group"] == 0
        assert [r["reason"] for r in got["rejected"]] == ["duplicate_node_in_group"]

    def test_releasing_every_duplicate_is_what_the_second_clause_refuses(self):
        """⚠️MEASURED ON THE LIVE POOL, NOT PREFERRED. Releasing every duplicate delivered 7
        events over the 74 h instead of 5, NONE of them admissible at zero margin against 2 for
        this rule, the median spread back at 70.87 ms from 55.85 ms, and one episode delivered
        three times. This fixture is the shape of that: three coherent retriggers 20 ms behind
        the direct arrivals, inside one blast's decay, which an unguarded release turns into a
        second event at the same place."""
        dets = _round(100.0, 0) + [_det(n, 100.020 + i * 0.005, 1)
                                   for i, n in enumerate((1, 2, 3))]
        got = AS.associate(dets, TIGHT)
        assert [e["node_ids"] for e in got["events"]] == [[1, 2, 3]]
        assert got["refusals"]["duplicate_node_in_group"] == 0
        assert [r["reason"] for r in got["rejected"]] == ["duplicate_node_in_group"] * 3
        _conserved(got, len(dets))


class TestTerminationAndIdempotence:
    """⚠️A RE-SEEDING GROUPER CAN LOOP, AND THIS IS WHAT STOPS IT: the seed is committed before
    any candidate can be released, and `used` only ever goes False -> True.

    That is load-bearing and the loop shape is not. Mutation, measured: turning the pass into a
    worklist, scanning from index 0 instead of i + 1, and pushing a released index back on the
    queue are all EQUIVALENT -- every answer and every counter here is unchanged, because a
    released index is already `used` by the time anything reaches it again. Move the seed's own
    commit below the release and that same worklist re-queues a seed it never consumed: this
    file then does not terminate at all (measured: no result in 120 s where it passes in 0.12 s).
    The counters are asserted EXACTLY rather than as ceilings so that an implementation which
    revisits without looping is caught too.
    """

    def _one_node_burst(self, n, spacing_s):
        """Every arrival is from one node, so every group is complete at size 1 and every later
        arrival is a duplicate beyond the same-node bound -- released, by every seed whose window
        it falls in. This is the worst case for revisiting."""
        return [_det(1, 100.0 + i * spacing_s, i) for i in range(n)]

    def test_the_scan_is_one_forward_pass_and_the_work_is_exactly_countable(self):
        n, spacing = 400, 0.031
        got = AS.associate(self._one_node_burst(n, spacing), VAST, min_nodes=1)
        reach = int(got["window_s"] / spacing)
        expected = sum(min(reach, n - 1 - i) for i in range(n))
        assert got["scan_seeds"] == n, "every arrival seeds; none is consumed by another"
        assert got["scan_candidate_visits"] == expected
        assert got["refusals"]["duplicate_node_in_group"] == expected
        assert len(got["events"]) == n
        _conserved(got, n)

    def test_no_detection_is_committed_twice(self):
        """Conservation is the counted form of the same guarantee, on an input built so that
        every kind of refusal fires at once."""
        dets = (_round(100.0, 0) + _round(100.085, 1) + _round(100.170, 2)
                + [_det(1, 100.020, 9, retrigger=True), _det(9, 100.030, 0),
                   _det(2, 100.005, 0, iface="mqtt")])
        got = AS.associate(dets, TIGHT)
        _conserved(got, len(dets))
        seen = [(int(d["node_id"]), int(d["seq"]), float(d["t_utc_s"]))
                for e in got["events"] for d in e["detections"]]
        assert len(seen) == len(set(seen)), "a detection reached two events"

    def test_running_it_twice_gives_the_same_answer(self):
        dets = _live_dets()
        assert _shape(AS.associate(dets, LIVE, temp_c=25.0)) == \
            _shape(AS.associate(dets, LIVE, temp_c=25.0))

    def test_the_answer_does_not_depend_on_input_order(self):
        """The pool is sorted by (t, node_id, seq) before the scan, so the caller's order cannot
        reach the result."""
        dets = _live_dets()
        base = _shape(AS.associate(dets, LIVE, temp_c=25.0))
        rng = random.Random(20260910)
        for _ in range(20):
            shuffled = list(dets)
            rng.shuffle(shuffled)
            assert _shape(AS.associate(shuffled, LIVE, temp_c=25.0)) == base

    def test_an_event_re_associated_alone_reproduces_itself(self):
        """The fixed-point form of idempotence: an emitted event's own detections fed back must
        give that event and nothing else, or the grouping depends on what it discarded."""
        got = AS.associate(_live_dets(), LIVE, temp_c=25.0)
        for ev in got["events"]:
            again = AS.associate(list(ev["detections"]), LIVE, temp_c=25.0)
            assert [e["node_ids"] for e in again["events"]] == [ev["node_ids"]]
            assert again["events"][0]["arrivals"] == pytest.approx(ev["arrivals"], abs=1e-12)
            assert again["rejected"] == [] and again["duplicates"] == []


class TestAnEventSaysWhetherAPointSourceCouldHaveMadeIt:
    """⚠️THE GROUPER ADMITS ON d/c + MARGIN_S, AND THAT IS NOT THE PHYSICAL BOUND.

    MARGIN_S is 30 ms = 10.3 m of slop against an array 16.87 m across, so a group can clear
    admission and still describe arrivals no single point source anywhere could have produced.

    MEASURED on the live pool 2026-09-11 (7,073 anchored arrivals, 74 h): of four three-node
    events delivered, THREE are impossible -- spans 55.85, 70.37 and 74.70 ms against a largest
    pair bound of 48.7 ms, i.e. 2.81 m, 7.51 m and 9.01 m past what any source could produce.
    Before this field, nothing downstream could tell those three from the one real one.
    """

    def test_a_group_inside_every_pair_bound_is_possible(self):
        # TIGHT is 10 m across; 1 and 2 are 10 m apart = 29.1 ms at 20 C. 20 ms is inside it.
        got = AS.associate([_det(1, 100.0), _det(2, 100.020), _det(3, 100.010)], TIGHT)
        assert len(got["events"]) == 1
        e = got["events"][0]
        assert e["point_source_possible"] is True
        assert e["worst_pair_excess_s"] == 0.0
        assert got["events_point_source_possible"] == 1

    def test_a_group_the_margin_admits_but_geometry_forbids_is_flagged(self):
        # 1->2 is 10 m = 29.1 ms. 50 ms clears the 59.1 ms admission bound and is 20.9 ms past
        # the physical one, so the event is still DELIVERED and now says it cannot be real.
        got = AS.associate([_det(1, 100.0), _det(2, 100.050), _det(3, 100.025)], TIGHT)
        assert len(got["events"]) == 1, "it must still be delivered, not silently dropped"
        e = got["events"][0]
        assert e["point_source_possible"] is False
        assert e["worst_pair_excess_s"] > 0.015, e["worst_pair_excess_s"]
        assert got["events_point_source_possible"] == 0

    def test_the_excess_is_the_distance_past_the_bound_not_the_span(self):
        got = AS.associate([_det(1, 100.0), _det(2, 100.050), _det(3, 100.025)], TIGHT)
        e = got["events"][0]
        c = AS.SW.sound_speed(20.0)
        d12 = float(np.linalg.norm(TIGHT.position(1) - TIGHT.position(2)))
        assert e["worst_pair_excess_s"] == pytest.approx(0.050 - d12 / c, abs=1e-9)
        assert e["worst_pair_excess_s"] < e["span_s"], "excess is not the span"

    def test_the_count_matches_the_events(self):
        dets = ([_det(1, 100.0), _det(2, 100.020), _det(3, 100.010)]
                + [_det(1, 200.0), _det(2, 200.050), _det(3, 200.025)])
        got = AS.associate(dets, TIGHT)
        assert len(got["events"]) == 2
        assert got["events_point_source_possible"] == 1
        assert (got["events_point_source_possible"]
                == sum(1 for e in got["events"] if e["point_source_possible"]))

    def test_it_uses_the_repos_one_definition_of_the_bound(self):
        """⚠️A SECOND COPY OF |tau| <= d/c WOULD DRIFT FROM THE FIRST. consistency.py owns it."""
        import hear.backend.associate as mod
        assert mod.physically_possible is CONS.physically_possible
