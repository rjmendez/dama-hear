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
        """⚠️A RENAME OF THE SOLVER'S PARAMETER MUST FAIL, NOT DEGRADE. While the carrying and
        consuming sides were separate branches this driver PROBED the signature and fell back to
        an unweighted fit -- right then, wrong now that both are composed, because an unweighted
        fallback is indistinguishable in the output from a weighted fit on equal sigmas."""
        import inspect
        from hear.solve import point as PT
        params = inspect.signature(PT.solve).parameters
        assert TD.SOLVER_SIGMA_KWARG == "sigmas"
        assert TD.SOLVER_TAKES_SIGMA is True, "a False one cannot import; see the raise below it"
        assert TD.SOLVER_SIGMA_KWARG in params
        assert not (set(params) & {"sigma_s", "arrival_sigma_s", "weights", "sigma"}), (
            "point.solve took a per-receiver uncertainty under a name this driver does not "
            "pass; the two branches agreed on `sigmas`, seconds")

    def test_the_driver_refuses_to_import_against_a_solver_with_no_sigma(self):
        """The import-time refusal that replaced the probe, exercised without re-importing: the
        source must RAISE on a missing parameter and must not fall back anywhere."""
        src = (ROOT / "tools" / "hear_tdoa.py").read_text()
        i = src.index("SOLVER_TAKES_SIGMA = ")
        block = src[i:i + 700]
        assert "raise ImportError" in block, (
            "the probe's silent fallback must be an import-time refusal now that both sides of "
            "the seam are composed")
        assert "not SOLVER_TAKES_SIGMA or sigmas is None" not in src, (
            "_sigma_kwargs must not keep a branch that silently returns no weighting when the "
            "solver lacks the parameter -- that branch is the silencer")

    def test_the_unit_is_seconds_end_to_end_from_the_nanoseconds_on_the_wire(self):
        """⚠️ns ON THE WIRE, s AT THE SOLVER, AND ONE PLACE CONVERTS. A driver that converted and
        a nodeclass that also converted would divide by 1e9 twice and weight every receiver at
        1e-9 of its real sigma, which no assertion downstream would catch."""
        ns = 106_038.0
        s = NC.stamp_t_sigma_s(ns, "gotchi-phone")
        assert s == pytest.approx(ns / 1e9, rel=1e-12), "gotchi-phone's capture term is 0"
        assert 1e-5 < s < 1e-3, "seconds, not nanoseconds and not milliseconds"
        assert TD._sigma_kwargs([s, s]) == {"sigmas": [s, s]}


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

    def _phone_pool(self, tmp_path):
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
        return root, sv, policy

    def test_the_outer_door_refuses_the_phone_before_the_bias_gate_is_reached(self, tmp_path):
        """⚠️COMPOSED ORDERING, PINNED. `heterogeneous_receiver_class` sits AHEAD of the clock
        and bias gates and catches `source == "phone"` unconditionally, so with the default
        policy this row never reaches `capture_path_bias` at all. Pinned because the two gates
        were written on separate branches that did not conflict textually: whichever runs first
        is the reason an operator reads in the ledger, and that ordering is a decision, not an
        accident of merge order."""
        root, sv, policy = self._phone_pool(tmp_path)
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["by_reason"].get(TD.D_HETEROGENEOUS_CLASS) == 1
        assert a["by_reason"].get(TD.D_PATH_BIAS) is None

    def test_the_bias_gate_refuses_a_phone_row_the_clock_gate_admitted(self, tmp_path):
        """End to end through `admit()` with the heterogeneous door OPEN: one phone row stating
        a clock sigma INSIDE the bound, surveyed and classed, lands in `capture_path_bias` and
        not in `stamp_sigma_...`.

        ⚠️THE OPT-IN IS SET HERE SO THE GATE UNDER TEST IS THE ONE REACHED. Opening the outer
        door does NOT open this one -- that is the property being pinned, and it is the whole
        reason the bias verdict is a separate terminal reason."""
        root, sv, policy = self._phone_pool(tmp_path)
        policy["heterogeneous_receivers"] = True
        # the ARRIVAL sub-survey is what admit() maps names through; pass the same survey so the
        # row gets past `unsurveyed_node` and the CLOCK/BIAS gates are the ones under test.
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["by_reason"].get(TD.D_HETEROGENEOUS_CLASS) is None
        assert a["by_reason"].get(TD.D_STAMP_SIGMA) is None, (
            "its stated 106.4 us is inside the 129.4 us bound; refusing it on the clock is the "
            "defect this change removed")
        assert a["by_reason"].get(TD.D_PATH_BIAS) == 1
        detail = [r["detail"] for r in a["ledger"] if r["drop_reason"] == TD.D_PATH_BIAS][0]
        assert "capture-path" in detail and "BIAS" in detail

    def test_an_unclassed_phone_is_charged_its_own_class_and_not_a_xiao(self, tmp_path):
        """⚠️THE HOLE BETWEEN THE TWO BRANCHES. The bias branch documented that survey.json MUST
        carry `"class": "gotchi-phone"` on a phone; nothing enforced it. Unclassed, nodeclass
        charges the strictest ARRIVAL class -- xiao-s3-pps, whose path_bias_s is the MEASURED
        62.5 us of ITS OWN capture path -- so with the heterogeneous door open an unclassed phone
        would clear the bias gate as a XIAO while carrying 13.122 ms = 4.50 m of its own.

        On the 2026-09-11 pool it was refused anyway, on the CLOCK, because the XIAO's 100 us
        capture term RSSes a stated 106 us over the 129.4 us bound. That holds only while a phone
        states more than xiao-s3-pps.max_stated_clock_sigma_s() = 82.1 us; the lowest any handset
        has stated is 100.0 us. This pins the refusal to the gate that is actually true of the
        receiver, at a sigma BELOW that margin where the old arithmetic admitted it."""
        root = tmp_path / "pool"
        (root / "records" / "2026-09-10").mkdir(parents=True)
        row = {"anchored": True, "ts_utc_s": 1788998406.636, "node": "handset",
               "source": "phone", "key": "k1", "clock_tier": "gnss",
               "sync_sigma_ns": 40_000.0, "onset_found": True, "utc_trusted": True}
        (root / "records" / "2026-09-10" / "phone.jsonl").write_text(json.dumps(row) + "\n")
        sv = SV.Survey(positions={1: (0.0, 0.0, 0.0)}, names={1: "handset"},
                       sigma_m={1: 0.5}, classes={},          # ⚠️NO class key: the whole point
                       origin={"lat_deg": 40.0, "lon_deg": -76.0, "h_ell_m": 0.0})
        policy = {"sources": ["phone"], "days": [], "since": None, "until": None,
                  "lookback_h": 10000.0, "settle_s": 0.0, "clock_unstated": "admit",
                  "onset_unstated": "admit", "max_sync_sigma_ns": None, "latency_cal": {},
                  "heterogeneous_receivers": True}
        # 40 us clears xiao-s3-pps too: sqrt(100^2 + 40^2) = 107.7 us, inside 129.4 us. So the
        # OLD arithmetic reached the bias gate and passed it on the XIAO's measured 62.5 us.
        assert NC.get("xiao-s3-pps").stamp_admissible(40_000.0) is True
        assert NC.get("xiao-s3-pps").capture_bias_bounded() is True
        a = TD.admit(str(root), sv, sv, policy, now=1788998606.0)
        assert a["n_admitted"] == 0, "an unclassed phone must never be admitted as a XIAO"
        assert a["by_reason"].get(TD.D_PATH_BIAS) == 1, (
            "and the reason must be its own capture path, not a clock it is inside")
        assert TD.gate_class("phone", None) == "gotchi-phone"
        assert TD.gate_class("node", None) is None
        assert TD.gate_class("phone", "xiao-s3-pps") == "xiao-s3-pps", "the survey's word wins"

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
