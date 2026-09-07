"""The backend chain end to end: real frames in, a recovered trajectory out.

No hardware and no files. Arrivals are synthesised from shockwave.shock_time for a KNOWN track
across a KNOWN survey, packed into real v2 frames, and pushed through the chain. If any link --
frame layout, day unwrap, attribution, row order, units -- is wrong, the recovered bearing moves.
"""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as SK             # noqa: E402
from hear import wire as WR               # noqa: E402
from hear.backend import pipeline as BP   # noqa: E402
from hear.backend import survey as SV     # noqa: E402
from hear.node import telemetry as TL     # noqa: E402
from hear.solve import shockwave as SW    # noqa: E402

V = 900.0                       # M855, the round the 2026-09-05 session actually fired
T = 23.0                        # docs/findings-2026-09-05.md:42 -- c = 345.24 m/s
T0 = 1_757_000_000.0            # 15:33 UTC, nowhere near a day boundary
Q = np.zeros((20, 8), np.int8)  # the sketch body is irrelevant here; only its geometry is

# ⚠️COMPACT ON PURPOSE. hear/solve/shockwave.py:61 gives the miss term a coefficient of
# 4.876 ms/m at these V and T, so its cone sweeps sideways at 205 m/s -- slower than sound. Two
# nodes then disagree by more than their own separation allows, which is exactly what
# associate.py's pairwise gate refuses. Measured: at 45 m apart the model says 161.8 ms against a
# 159.8 ms bound. Under ~15 m of miss spread the two modules agree, so the fixtures stay there.
RING = [(-18.0, 0.0), (15.0, 7.2), (3.0, -16.8), (-4.8, 18.0), (10.8, 20.4)]
BEARING, OFFSET = 20.0, 5.0     # RING straddles this track, so both parameters are observable

# A 10 m detecting cluster plus one node 60 m out that does not hear this round. The far node is
# not decoration: diameter_m() spans the whole survey, so it is what widens the scan window enough
# for a late round-2 detection to reach the pairwise gate at all.
TIGHT = [(0.0, -5.0), (0.0, 5.0), (1.5, -1.5), (-1.5, 1.5), (60.0, 0.0)]
BURST_S = 0.085                 # 700 rpm, the tightest measured round spacing


def _survey(nodes):
    return SV.from_dict({
        "frame": "enu_local", "units": "m",
        "nodes": [{"node_id": i + 1, "e_m": e, "n_m": n, "u_m": 0.0}
                  for i, (e, n) in enumerate(nodes)],
    })


def _shock_arrivals(nodes, bearing_deg, offset_m, t0=T0):
    c = SW.sound_speed(T)
    br = math.radians(bearing_deg)
    return [t0 + SW.shock_time(p, br, offset_m, V, c) for p in nodes]


def _point_arrivals(nodes, source, t0=T0):
    c = SW.sound_speed(T)
    return [t0 + math.hypot(source[0] - p[0], source[1] - p[1]) / c for p in nodes]


def _frame(node_id, seq, t_utc_s, retrigger=False):
    return WR.pack_v2(WR.us_of_day_from_utc(t_utc_s), node_id, seq, -12.5, 4321, Q,
                      retrigger=retrigger)


def _frames(node_ids, arrivals, seq=0, iface="lora0", rx_lag_s=0.4):
    """Every frame is delivered late and by a DIFFERENT amount -- mesh hops and retries, hundreds
    of ms apart. If receive time ever leaked into the geometry, these lags would wreck it."""
    return [(_frame(nid, seq, t), iface, t + rx_lag_s + 0.31 * i)
            for i, (nid, t) in enumerate(zip(node_ids, arrivals))]


def _bearing_error(got, truth):
    return abs(((got - truth + 180.0) % 360.0) - 180.0)


class TestEndToEnd:
    def test_five_nodes_of_frames_recover_the_track_they_were_built_from(self):
        s = _survey(RING)
        got = BP.Backend(s, temp_c=T, v_mps=V, source_class="crack").run(
            _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET)))
        assert len(got["events"]) == 1
        ev = got["events"][0]
        assert ev["model"] == "cone" and ev["n_nodes"] == 5
        sol = ev["solution"]
        # same tolerances tests/test_shockwave.py:52-53 accepts for the solver on its own
        assert _bearing_error(sol["bearing_deg"], BEARING) < 1.0
        assert sol["offset_m"] == pytest.approx(OFFSET, abs=1.0)

    def test_frame_order_cannot_move_the_answer(self):
        """Row alignment. positions_2d rows are paired with arrivals by index, so a re-sort
        anywhere in the chain mislabels every node -- and no residual would catch it."""
        s = _survey(RING)
        fr = _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET))
        base = BP.Backend(s, temp_c=T, v_mps=V).run(fr)["events"][0]["solution"]["bearing_deg"]
        for order in ([2, 0, 4, 1, 3], [4, 3, 2, 1, 0]):
            got = BP.Backend(s, temp_c=T, v_mps=V).run([fr[i] for i in order])
            assert got["events"][0]["solution"]["bearing_deg"] == pytest.approx(base, rel=1e-6)

    def test_a_blast_routes_to_the_point_solver_and_lands_on_the_source(self):
        """⚠️3.5 m, where the cone path gets 1.0 m. point.solve's finite-difference refine
        (hear/solve/point.py:112) is blind at absolute UTC: a 6e-7 m probe step moves the residual
        by 2e-9 s against 2.4e-7 s of float64 rounding at t = 1.76e9, so it returns its 10 m grid
        seed. Measured on this fixture: exact at t0 = 1e3, 2.69 m off at t0 = 1.757e9."""
        s = _survey(RING)
        src = (40.0, -25.0)
        got = BP.Backend(s, temp_c=T, source_class="blast").run(
            _frames(range(1, 6), _point_arrivals(RING, src)))
        ev = got["events"][0]
        assert ev["model"] == "point"
        assert ev["solution"]["east_m"] == pytest.approx(src[0], abs=3.5)
        assert ev["solution"]["north_m"] == pytest.approx(src[1], abs=3.5)


class TestTheWindowGuard:
    """One node misses round 1 and sends only its round 2, 85 ms later. What separates the rounds
    is the pairwise geometric bound, not the window -- so widening the margin destroys the answer."""

    def _burst_frames(self):
        a = _shock_arrivals(TIGHT[:4], 0.0, 0.0)
        fr = _frames([1, 2, 3], a[:3])                       # round 1, nodes 1-3
        fr += _frames([4], [a[3] + BURST_S], seq=1)          # round 2, node 4 only
        return fr

    def test_the_late_node_is_rejected_by_geometry_and_the_rest_solve(self):
        # measured for this layout: 88-100 ms of disagreement against bounds of 41-49 ms
        got = BP.Backend(_survey(TIGHT), temp_c=T, v_mps=V).run(self._burst_frames())
        assert [r["reason"] for r in got["rejected"]] == ["pairwise_dt_exceeds_geometry"]
        ev = got["events"][0]
        assert ev["n_nodes"] == 3
        assert _bearing_error(ev["solution"]["bearing_deg"], 0.0) < 1.0

    def test_a_six_hundred_millisecond_margin_admits_it_and_wrecks_the_bearing(self):
        got = BP.Backend(_survey(TIGHT), temp_c=T, v_mps=V, margin_s=0.600).run(self._burst_frames())
        ev = got["events"][0]
        assert ev["n_nodes"] == 4, "the wide margin must actually admit the late node"
        assert _bearing_error(ev["solution"]["bearing_deg"], 0.0) > 5.0

    def test_the_window_comes_from_the_survey_not_from_a_constant(self):
        narrow = BP.Backend(_survey(TIGHT), temp_c=T, v_mps=V).run(self._burst_frames())
        wide = BP.Backend(_survey(RING), temp_c=T, v_mps=V).run(
            _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET)))
        assert wide["diameter_m"] != pytest.approx(narrow["diameter_m"], rel=1e-3)
        assert wide["window_s"] != pytest.approx(narrow["window_s"], rel=1e-3)


class TestResidualHonesty:
    def test_three_nodes_never_quote_their_residual_as_evidence(self):
        """2 equations, 2 unknowns: the residual is ~0 by construction and carries nothing.
        Measured on this fixture, the fit that produced it came back on a bearing 115 deg from
        truth -- at 0.30 ms."""
        tri = RING[:3]
        s = _survey(tri)
        got = BP.Backend(s, temp_c=T, v_mps=V).run(
            _frames([1, 2, 3], _shock_arrivals(tri, 40.1, 3.1)))
        sol = got["events"][0]["solution"]
        assert sol["n_equations"] == 2
        assert sol["residual_is_meaningful"] is False
        # 0.5 ms is the solver's own 0.25 deg / 0.25 m grid quantisation, not information
        assert sol["rms_residual_ms"] == pytest.approx(0.0, abs=0.5)

    def test_five_nodes_do_get_a_meaningful_residual(self):
        got = BP.Backend(_survey(RING), temp_c=T, v_mps=V).run(
            _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET)))
        sol = got["events"][0]["solution"]
        assert sol["n_equations"] == 4
        assert sol["residual_is_meaningful"] is True


class TestBadFrames:
    def test_a_v1_sketch_is_counted_not_guessed_at(self):
        """v1 carries no node id and no absolute second (hear/sketch.py:84-93). Inventing one from
        the interface would be a one-second, 343 m error nothing downstream could detect."""
        b = BP.Backend(_survey(RING), temp_c=T, v_mps=V)
        r = b.ingest(SK.pack(123_456, -12.5, 4321, Q), "lora0", T0)
        assert r["ok"] is False and r["detection"] is None
        assert r["reason"] == "v1_frame_has_no_node_id"
        assert b.flush()["decode_errors"] == [
            {"iface": "lora0", "n_bytes": SK.wire_size(), "reason": "v1_frame_has_no_node_id"}]

    def test_corruption_does_not_take_the_array_down(self):
        good = _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET))
        bent = bytearray(good[0][0])
        bent[11] ^= 0x02                       # profile bits: the frame's own length now lies
        b = BP.Backend(_survey(RING), temp_c=T, v_mps=V)
        for junk in (b"", b"\x00" * 50, bytes(bent)):
            r = b.ingest(junk, "lora0", T0)
            assert r["ok"] is False and r["reason"].startswith("decode_error")
        for f, iface, rx in good:
            b.ingest(f, iface, rx)
        got = b.flush()
        assert len(got["decode_errors"]) == 3
        assert len(got["events"]) == 1
        assert _bearing_error(got["events"][0]["solution"]["bearing_deg"], BEARING) < 1.0

    def test_a_node_outside_the_survey_never_reaches_an_event(self):
        fr = _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET))
        fr.append((_frame(99, 0, T0), "lora0", T0 + 0.4))
        got = BP.Backend(_survey(RING), temp_c=T, v_mps=V).run(fr)
        assert got["unknown_nodes"] == [{"iface": "lora0", "node_id": 99}]
        assert 99 not in got["events"][0]["node_ids"]


class TestTransportAgnostic:
    def test_the_same_bytes_differ_only_by_the_interface_they_arrived_on(self):
        s = _survey(RING)
        f = _frame(1, 0, T0)
        a = BP.Backend(s).ingest(f, "lora0", T0 + 0.4)["detection"]
        b = BP.Backend(s).ingest(f, "mqtt", T0 + 0.4)["detection"]
        assert a.pop("iface") == "lora0"
        assert b.pop("iface") == "mqtt"
        assert np.array_equal(a.pop("q"), b.pop("q"))
        assert a == b


class TestPublishing:
    def test_a_telemetry_frame_is_published_through_telemetry_to_dama_verbatim(self):
        """The 14 B frame carries no node id, so the interface has to supply one. Pinned so that
        hole stays visible rather than becoming a habit."""
        seen = []
        b = BP.Backend(_survey(RING), publish=seen.append)
        f = TL.pack(23.0, 1013.2, 41.0, -58.0, -20.0, 9, 4100, True)
        r = b.ingest(f, "lora0", T0)
        assert r["kind"] == "telemetry" and r["reason"] == "node_id_taken_from_iface"
        assert seen == [TL.to_dama("lora0", T0, TL.unpack(f))]
        assert seen[0]["node_type"] == "hear" and seen[0]["sound_speed_mps"] is not None

    def test_publish_is_called_once_per_event_and_once_per_telemetry_frame(self):
        seen = []
        b = BP.Backend(_survey(RING), temp_c=T, v_mps=V, publish=seen.append)
        b.ingest(TL.pack(23.0, 1013.2, 41.0, -58.0, -20.0, 9, 4100, True), "lora0", T0)
        got = b.run(_frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET)))
        assert len(seen) == 2
        assert got["n_published"] == 2
        assert seen[1] == got["events"][0]["published"]

    def test_the_event_payload_omits_the_quantities_its_model_does_not_have(self):
        got = BP.Backend(_survey(RING), temp_c=T, v_mps=V).run(
            _frames(range(1, 6), _shock_arrivals(RING, BEARING, OFFSET)))
        body = got["events"][0]["published"]["event"]
        assert "bearing_deg" in body and "offset_m" in body
        assert "east_m" not in body and "north_m" not in body
        assert got["events"][0]["published"]["node_type"] == "hear"
