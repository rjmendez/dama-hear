"""The stated sigma has to REACH the solver, and the bias verdict has to stay a separate word.

⚠️WHAT THIS EXISTS TO CATCH. `sync_sigma_ns` reached `hear/pool.py` and `corpus.Record` and then
stopped: the driver spent it on one boolean and threw the number away, and `point.solve` had
nowhere to put it, so every receiver voted at equal weight whatever it said about itself. A test
that only checked the boolean would have passed the whole way through that.

⚠️AND THE SECOND HALF, WHICH IS THE EASIER ONE TO LOSE. Weighting handles variance. It does not
handle bias. A receiver may clear the clock gate on its own stated sigma and must STILL be refused
while its capture-path delay is unmeasured -- an uncorrected offset moves the fit rather than
widening it, and is invisible in the exactly-determined 3-node residual this array produces. The
two refusals must stay two, with two messages, or an operator cannot tell "run the calibration"
from "fix a clock".
"""
import importlib
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear import nodeclass as NC                                          # noqa: E402
from hear.backend import associate as AS                                  # noqa: E402
from hear.backend import survey as SV                                     # noqa: E402

TD = importlib.import_module("tools.hear_tdoa") if "tools.hear_tdoa" in sys.modules else None
if TD is None:                                    # the driver is a script, not a package member
    sys.path.insert(0, str(ROOT / "tools"))
    TD = importlib.import_module("hear_tdoa")


def _survey():
    return SV.Survey(
        positions={1: (0.0, 0.0, 0.0), 2: (-16.6, -0.3, 3.0),
                   3: (-4.6, 10.5, 3.0), 4: (40.0, 25.0, 0.0)},
        names={1: "nyquist", 2: "mach", 3: "rankine", 4: "far"},
        sigma_m={1: 0.7, 2: 0.5, 3: 0.5, 4: 0.5},
        classes={}, origin={"lat_deg": 40.0, "lon_deg": -76.0, "h_ell_m": 0.0})


class TestTheSigmaIsCarriedThroughAssociate:
    def test_the_event_carries_one_sigma_per_arrival_in_order(self):
        """⚠️INDEX-ALIGNED OR IT IS WORSE THAN ABSENT. A misaligned list weights each receiver by
        its neighbour's sigma, which is a wrong answer that still looks like a weighted fit."""
        sv = _survey()
        dets = [{"node_id": 1, "seq": 0, "t_utc_s": 100.0000, "t_sigma_s": 100e-6},
                {"node_id": 2, "seq": 0, "t_utc_s": 100.0100, "t_sigma_s": 106.4e-6},
                {"node_id": 3, "seq": 0, "t_utc_s": 100.0050, "t_sigma_s": None}]
        out = AS.associate(dets, sv, margin_s=0.030, min_nodes=3)
        assert len(out["events"]) == 1
        ev = out["events"][0]
        assert len(ev["arrival_sigma_s"]) == len(ev["arrivals"])
        by_node = dict(zip(ev["node_ids"], ev["arrival_sigma_s"]))
        assert by_node[1] == pytest.approx(100e-6)
        assert by_node[2] == pytest.approx(106.4e-6)
        assert by_node[3] is None, "not stated must stay None, never 0.0 and never a class figure"

    def test_a_detection_that_states_nothing_gives_None_not_zero(self):
        """0.0 is a claim of a perfect clock. `nodeclass` refuses that at construction and this
        must not reintroduce it one layer up."""
        sv = _survey()
        dets = [{"node_id": i, "seq": 0, "t_utc_s": 100.0 + 0.001 * i} for i in (1, 2, 3)]
        ev = AS.associate(dets, sv, margin_s=0.030, min_nodes=3)["events"][0]
        assert ev["arrival_sigma_s"] == [None, None, None]


class TestTheDriverResolvesTheStatementIntoSeconds:
    def test_a_stated_sigma_becomes_a_number_on_the_detection(self, tmp_path):
        """⚠️THE LINE THE WHOLE CHAIN HANGS OFF, AND IT HAD NO GUARD. `admit()` spent
        `sync_sigma_ns` on one boolean and put nothing numeric on the detection, so associate()
        had nothing to carry and the solver nothing to weight. Replacing the resolved value with
        a bare `None` passed every other test in this file: found by mutation, 2026-09-11.

        The number is SECONDS and it is the class-resolved total, not the raw statement: the
        capture terms the statement does not measure are RSS'd in by nodeclass, which is the only
        place that knows the split.
        """
        root = tmp_path / "pool"
        (root / "records" / "2026-09-10").mkdir(parents=True)
        ssig = 25000.0                      # a healthy XIAO anchor, one second old
        row = {"anchored": True, "ts_utc_s": 1788998406.636, "node": "nyquist",
               "source": "node", "key": "n1", "sync_sigma_ns": ssig}
        (root / "records" / "2026-09-10" / "node.jsonl").write_text(json.dumps(row) + "\n")
        sv = SV.Survey(positions={1: (0.0, 0.0, 0.0)}, names={1: "nyquist"},
                       sigma_m={1: 0.5}, classes={},
                       origin={"lat_deg": 40.0, "lon_deg": -76.0, "h_ell_m": 0.0})
        policy = {"sources": ["node"], "days": [], "since": None, "until": None,
                  "lookback_h": 10000.0, "settle_s": 0.0, "clock_unstated": "admit",
                  "onset_unstated": "admit", "max_sync_sigma_ns": None, "latency_cal": {}}
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["n_admitted"] == 1
        det = a["dets"][0]
        assert det["t_sigma_s"] is not None, (
            "the stated sigma must reach the detection as a NUMBER; spending it only on "
            "stamp_admissible is the defect this change removes")
        assert det["t_sigma_s"] == pytest.approx(NC.stamp_t_sigma_s(ssig, None))
        assert det["t_sigma_s"] == pytest.approx(103.0776e-6, rel=1e-4), (
            "seconds, and the class's capture terms RSS'd in -- not the raw 25 us statement")
        assert det["sync_sigma_ns"] == ssig, "the nanosecond statement stays on the row too"


class TestTheSigmaReachesTheSolver:
    def test_the_driver_hands_the_solver_the_seconds_it_resolved(self, monkeypatch):
        """⚠️THE SEAM. The carrying side and the consuming side landed on separate branches, so
        this drives `solve_event` against a stand-in solver that RECORDS what it was given."""
        seen = {}

        def fake_solve(P, arrivals, source_class, temp_c=20.0, search_margin_m=500.0,
                       grid_step_m=10.0, fixed_up_m=None, sigmas=None):
            seen["sigmas"] = sigmas
            return {"east_m": 1.0, "north_m": 2.0, "up_m": 0.0, "position_observable": True,
                    "rms_residual_ms": 0.0, "at_search_bound": False}

        monkeypatch.setattr(TD.PT, "solve", fake_solve)
        monkeypatch.setattr(TD, "SOLVER_TAKES_SIGMA", True)
        sv = _survey()
        TD.solve_event(sv, [1, 2, 3], [100.0, 100.01, 100.005], "blast", 20.0, 340.0, 0.0,
                       arrival_sigma_s=[100e-6, 106.4e-6, 120e-6])
        assert seen["sigmas"] == [100e-6, 106.4e-6, 120e-6], (
            "seconds, in receiver order; the wire and the pool carry nanoseconds and the "
            "conversion belongs in nodeclass, where the class terms are RSS'd in")

    def test_a_mixed_group_is_not_weighted_and_the_cost_is_counted(self, monkeypatch):
        """⚠️THE SOLVER REFUSES A MIXTURE AND THIS REFUSES TO INVENT ONE. Filling a quiet
        receiver's slot with its CLASS figure would satisfy the signature with a number nobody
        measured for that detection. It falls back to equal weighting instead -- and that is not
        free, because a node array that has not taken the G6 flash standing beside a phone that
        states 106 us per row IS a mixed group."""
        seen = {}

        def fake_solve(P, arrivals, source_class, temp_c=20.0, search_margin_m=500.0,
                       grid_step_m=10.0, fixed_up_m=None, sigmas=None):
            seen["sigmas"] = sigmas
            return {"east_m": 1.0, "north_m": 2.0, "up_m": 0.0, "position_observable": True,
                    "rms_residual_ms": 0.0, "at_search_bound": False}

        monkeypatch.setattr(TD.PT, "solve", fake_solve)
        monkeypatch.setattr(TD, "SOLVER_TAKES_SIGMA", True)
        TD.solve_event(_survey(), [1, 2, 3], [100.0, 100.01, 100.005], "blast", 20.0, 340.0, 0.0,
                       arrival_sigma_s=[100e-6, None, 120e-6])
        assert seen["sigmas"] is None
        assert TD._sigma_kwargs([100e-6, None]) == {}
        assert TD._sigma_kwargs([None, None]) == {}
        assert TD._sigma_kwargs([1e-4, 2e-4]) == {"sigmas": [1e-4, 2e-4]}

    def test_leave_one_out_drops_the_sigma_at_the_same_index(self, monkeypatch):
        """A full-length sigma list against an n-1 arrival list weights every remaining receiver
        by its neighbour's number, and the lengths differ by one so nothing complains."""
        calls = []

        def fake_solve(P, arrivals, source_class, temp_c=20.0, search_margin_m=500.0,
                       grid_step_m=10.0, fixed_up_m=None, sigmas=None):
            calls.append((list(arrivals), None if sigmas is None else list(sigmas)))
            return {"east_m": 1.0, "north_m": 2.0, "up_m": 0.0, "position_observable": True,
                    "rms_residual_ms": 1.0, "at_search_bound": False,
                    "residual_is_meaningful": True}

        monkeypatch.setattr(TD.PT, "solve", fake_solve)
        monkeypatch.setattr(TD, "SOLVER_TAKES_SIGMA", True)
        sv = _survey()
        ids = [1, 2, 3, 4]
        arr = [100.0, 100.01, 100.005, 100.02]
        sig = [1e-4, 2e-4, 3e-4, 4e-4]
        TD.leave_one_out(sv, ids, arr, "blast", 20.0, 340.0, 0.0,
                         {"rms_residual_ms": 1.0, "east_m": 1.0, "north_m": 2.0},
                         arrival_sigma_s=sig)
        assert calls, "leave-one-out must actually run at 4 receivers with a declared height"
        for got_arr, got_sig in calls:
            assert len(got_sig) == len(got_arr)
            dropped = [s for s in sig if s not in got_sig]
            kept = [(a, s) for a, s in zip(arr, sig) if s in got_sig]
            assert len(dropped) == 1
            assert [s for _a, s in kept] == got_sig

    def test_the_solver_kwarg_name_and_unit_are_pinned(self):
        """⚠️THE PROBE DEGRADES SILENTLY IF THE SOLVER RENAMES ITS PARAMETER. It falls back to an
        unweighted fit rather than raising, which is the right behaviour while the two branches
        are separate and the wrong one afterwards. This is the alarm: if `point.solve` grows the
        parameter under any other name, this fails and the probe gets deleted with it."""
        import inspect
        from hear.solve import point as PT
        params = inspect.signature(PT.solve).parameters
        assert TD.SOLVER_SIGMA_KWARG == "sigmas"
        assert TD.SOLVER_TAKES_SIGMA == (TD.SOLVER_SIGMA_KWARG in params)
        assert not (set(params) & {"sigma_s", "arrival_sigma_s", "weights", "sigma"}), (
            "point.solve took a per-receiver uncertainty under a name this driver does not "
            "pass; the two branches agreed on `sigmas`, seconds")


class TestTheBiasVerdictStaysSeparate:
    def test_the_two_refusals_are_two_different_sentences(self):
        cls = NC.get("gotchi-phone")
        clock = cls.stamp_refusal(NC.ARRIVAL_T_SIGMA_MAX_S * 1e9 * 2)
        bias = cls.bias_refusal()
        assert clock and bias and clock != bias
        assert "clock sigma" in clock and "capture-path" in bias
        assert "BIAS" in bias, "the bias message must say what a weight cannot do about it"

    def test_a_phone_inside_its_clock_budget_is_still_refused_on_its_path(self):
        """⚠️THE WHOLE POINT OF PART C. 13.122 ms is 4.50 m and is the BEST of three handsets."""
        cls = NC.get("gotchi-phone")
        assert cls.stamp_admissible(106434.0) is True          # clock: admitted on its statement
        assert cls.stamp_refusal(106434.0) is None
        assert cls.capture_bias_bounded() is False             # bias: refused, separately
        assert cls.bias_refusal() is not None
        assert cls.path_bias_m() == pytest.approx(13.122e-3 * 343.0, rel=1e-9)

    def test_the_driver_gives_the_bias_its_own_terminal_reason(self):
        assert TD.D_PATH_BIAS in TD.DROP_REASONS
        assert TD.D_PATH_BIAS != TD.D_STAMP_SIGMA

    def test_the_bias_gate_refuses_a_phone_row_the_clock_gate_admitted(self, tmp_path):
        """End to end through `admit()`: one phone row stating a clock sigma INSIDE the bound,
        surveyed and classed, lands in `capture_path_bias` and not in `stamp_sigma_...`."""
        root = tmp_path / "pool"
        (root / "records" / "2026-09-10").mkdir(parents=True)
        row = {"anchored": True, "ts_utc_s": 1788998406.636, "node": "handset",
               "source": "phone", "key": "k1", "clock_tier": "gnss",
               "sync_sigma_ns": 106434.0, "onset_found": True, "utc_trusted": True}
        (root / "records" / "2026-09-10" / "phone.jsonl").write_text(json.dumps(row) + "\n")
        sv = SV.Survey(positions={1: (0.0, 0.0, 0.0)}, names={1: "handset"},
                       sigma_m={1: 0.5}, classes={1: "gotchi-phone"},
                       origin={"lat_deg": 40.0, "lon_deg": -76.0, "h_ell_m": 0.0})
        policy = {"sources": ["phone"], "days": [], "since": None, "until": None,
                  "lookback_h": 10000.0, "settle_s": 0.0, "clock_unstated": "admit",
                  "onset_unstated": "admit", "max_sync_sigma_ns": None, "latency_cal": {}}
        # the ARRIVAL sub-survey is what admit() maps names through; pass the same survey so the
        # row gets past `unsurveyed_node` and the CLOCK/BIAS gates are the ones under test.
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["by_reason"].get(TD.D_STAMP_SIGMA) is None, (
            "its stated 106.4 us is inside the 129.4 us bound; refusing it on the clock is the "
            "defect this change removed")
        assert a["by_reason"].get(TD.D_PATH_BIAS) == 1
        detail = [r["detail"] for r in a["ledger"] if r["drop_reason"] == TD.D_PATH_BIAS][0]
        assert "capture-path" in detail and "BIAS" in detail

    def test_a_node_row_is_untouched_by_the_new_gate(self, tmp_path):
        """⚠️THE REGRESSION GUARD. The bias gate charges an unclassed receiver the strictest
        arrival class, whose path_bias_s is MEASURED and bounded, so every node row passes it --
        which is the only reason adding this gate did not empty the corpus."""
        root = tmp_path / "pool"
        (root / "records" / "2026-09-10").mkdir(parents=True)
        row = {"anchored": True, "ts_utc_s": 1788998406.636, "node": "nyquist",
               "source": "node", "key": "n1"}
        (root / "records" / "2026-09-10" / "node.jsonl").write_text(json.dumps(row) + "\n")
        sv = SV.Survey(positions={1: (0.0, 0.0, 0.0)}, names={1: "nyquist"},
                       sigma_m={1: 0.5}, classes={},
                       origin={"lat_deg": 40.0, "lon_deg": -76.0, "h_ell_m": 0.0})
        policy = {"sources": ["node"], "days": [], "since": None, "until": None,
                  "lookback_h": 10000.0, "settle_s": 0.0, "clock_unstated": "admit",
                  "onset_unstated": "admit", "max_sync_sigma_ns": None, "latency_cal": {}}
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["n_admitted"] == 1
        assert a["by_reason"].get(TD.D_PATH_BIAS) is None
        assert a["dets"][0]["t_sigma_s"] is None, "stated nothing -> None, never the class figure"
