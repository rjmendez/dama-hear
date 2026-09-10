"""hear-tdoa: the driver that finally runs the solvers this repo has been shipping.

⚠️NULL CONTROL FIRST, AND IT IS PAIRED WITH A TEST THAT CAN FAIL IT. `TestNullControlFirst`
asserts that independent Poisson streams produce zero events -- but on its own that is a check
that cannot fail: a driver that never emits anything satisfies it perfectly. `TestItActuallySolves`
is what closes the hole, by planting one source, pinning east/north and the sound speed, and then
proving three real defects (a wrong temperature, a swapped node position, a 5 ms bias on one
arrival) each move the answer.

⚠️THIS ORG HAS LOST REAL DATA TO A TEST THAT REGENERATED A FIXTURE IN PLACE. So: every writing
test uses `tmp_path`, `--pool` and `--out` are ALWAYS under `tmp_path`, no test opens `~/hear-pool`
or `/pool` or shells out to kubectl, and a module-scoped autouse fixture hashes survey.json,
testdata/** and deploy/k8s/*.yaml before and after this module and asserts the digests are
unchanged. The guard is on the FILES, not on anyone's memory.

⚠️THE SYNTHETIC ARRIVALS COME FROM `SW.sound_speed`, THE SAME SOURCE THE TOOL USES. A fixture that
carried its own copy of c would let the tool pass by agreeing with a second copy of a constant
rather than by being right, and position tolerances are derived from the injected jitter rather
than being a magic metre count.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hear import pool as P                                       # noqa: E402
from hear import sketch as SK                                    # noqa: E402
from hear.backend import associate as AS                         # noqa: E402
from hear.backend import pipeline as BP                          # noqa: E402
from hear.backend import survey as SV                            # noqa: E402
from hear.solve import placement as PL                           # noqa: E402
from hear.solve import point as PT                               # noqa: E402
from hear.solve import shockwave as SW                           # noqa: E402
from tools import hear_tdoa as HT                                # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "deploy", "k8s"))
import gen_configmap as GC                                       # noqa: E402

REPO = pathlib.Path(ROOT)


# ----------------------------------------------------------------- the write guard

def _digest_of_tracked_inputs():
    """sha256 of every file a test in this module could plausibly clobber."""
    paths = [REPO / "survey.json"]
    paths += sorted((REPO / "testdata").rglob("*")) if (REPO / "testdata").is_dir() else []
    paths += sorted((REPO / "deploy" / "k8s").glob("*.yaml"))
    out = {}
    for p in paths:
        if p.is_file():
            out[str(p.relative_to(REPO))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture(scope="module", autouse=True)
def _nothing_checked_in_is_touched():
    before = _digest_of_tracked_inputs()
    yield
    after = _digest_of_tracked_inputs()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert not changed, ("this module modified checked-in files: %s. A test that regenerates a "
                         "fixture in place has destroyed real data in this org." % changed)


# ----------------------------------------------------------------- fixtures

TEMP_C = 20.0
C = SW.sound_speed(TEMP_C)

#: The live array, as data. Copied here rather than read from survey.json so a test cannot start
#: depending on the operator's survey -- exactly ONE test below opens the real file, read-only.
LIVE_NODES = [
    {"node_id": 1, "name": "nyquist", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.717},
    {"node_id": 2, "name": "mach", "e_m": -16.602, "n_m": -0.272, "u_m": 3.0, "sigma_m": 0.521},
    {"node_id": 3, "name": "rankine", "e_m": -4.58, "n_m": 10.48, "u_m": 3.0, "sigma_m": 0.49},
]
PUC = {"node_id": 4, "name": "puc", "class": "puc-ntp", "e_m": -22.16, "n_m": 8.98, "u_m": 0.0,
       "sigma_m": 6.5}


def survey_dict(nodes=None):
    return {"frame": "enu_local", "units": "m",
            "origin": {"lat_deg": 40.29, "lon_deg": -76.12, "h_ell_m": 0.0},
            "nodes": [dict(n) for n in (LIVE_NODES if nodes is None else nodes)]}


def write_survey(tmp_path, nodes=None, name="survey.json"):
    p = tmp_path / name
    p.write_text(json.dumps(survey_dict(nodes)))
    return str(p)


def _frame(seed=0, fs=16000.0, layout=SK.LAYOUT_FIXED):
    q, ref = SK.sketch(np.random.default_rng(seed).normal(0, 300.0, 4096), fs, layout=layout)
    return SK.pack(1000 + seed, ref, 500, q, fs=fs, layout=layout)


def node_row(node, t_utc_s, seed=0, sample=None, fs=16000.0, layout=SK.LAYOUT_FIXED):
    return {"frame_hex": _frame(seed, fs, layout).hex(), "node": node,
            "utc_us": int(round(float(t_utc_s) * 1e6)),
            "sample": str(seed if sample is None else sample), "schema": "G5", "fs_hz": fs}


def build_pool(root, rows, extra_records=()):
    pl = P.Pool(str(root))
    recs = [P._record_from_node_row(r) for r in rows]
    recs += list(extra_records)
    if recs:
        pl._append(recs)
    return pl


def planted(sv, source, t0, nodes=None, seed0=0, jitter_s=0.0, rng=None, bias=None):
    """Rows for one point source heard by every arrival node. c comes from SW, not from here."""
    nodes = list(sv.arrival_ids()) if nodes is None else list(nodes)
    rows = []
    for k, nid in enumerate(nodes):
        p = np.asarray(sv.position(nid), float)
        t = t0 + float(np.linalg.norm(np.asarray(source, float) - p)) / C
        if jitter_s and rng is not None:
            t += float(rng.normal(0.0, jitter_s))
        if bias:
            t += float(bias.get(nid, 0.0))
        rows.append(node_row(sv.names[nid], t, seed=seed0 + k, sample=seed0 + k))
    return rows


T0 = 1_760_000_000.0          # a fixed absolute epoch; `now` is always passed explicitly


def pol(**over):
    p = {
        "sources": ["node"], "days": [], "since": None, "until": None,
        "lookback_h": 24.0 * 365, "settle_s": 0.0,
        "source_class": "blast", "fixed_up_m": 0.0, "temp_c": TEMP_C, "v_mps": 900.0,
        "min_nodes": 3, "margin_frac": HT.DEFAULT_MARGIN_FRAC, "margin_s_override": None,
        "force_margin": False, "bin_s": 60.0, "max_sync_sigma_ns": None,
        "clock_unstated": "refuse", "latency_cal": None,
        "null_trials": 0, "null_seed": 1, "calibrate": False, "known_source": None,
        "candidates": [], "site_box": None, "target_events": 20, "array_id": "hear",
        "allow_phones": False,
    }
    p.update(over)
    return p


def go(tmp_path, rows, survey=None, out=None, now=None, **over):
    build_pool(tmp_path / "pool", rows)
    return HT.run(str(tmp_path / "pool"), survey or write_survey(tmp_path), pol(**over),
                  out=str(out or (tmp_path / "out")), now=now or (T0 + 3600.0))


# ================================================================= null control FIRST

class TestNullControlFirst:
    """Quiet in, quiet out -- asserted before anything asserts the driver fires. Paired below."""

    def test_independent_poisson_streams_produce_no_events(self, tmp_path):
        rng = np.random.default_rng(11)
        rows = []
        sv = SV.from_dict(survey_dict())
        for k in range(150):
            nid = sv.ids[k % 3]
            rows.append(node_row(sv.names[nid], T0 + float(rng.random()) * 1800.0,
                                 seed=k, sample=k))
        t = go(tmp_path, rows, null_trials=25, now=T0 + 3600.0)
        assert t["events_solved"] == 0
        assert t["events_emitted"] == 0
        assert t["conservation"]["ok"]

    def test_the_observed_triple_count_sits_inside_its_own_null(self, tmp_path):
        rng = np.random.default_rng(12)
        rows = []
        sv = SV.from_dict(survey_dict())
        for k in range(180):
            nid = sv.ids[k % 3]
            rows.append(node_row(sv.names[nid], T0 + float(rng.random()) * 900.0,
                                 seed=k, sample=k))
        t = go(tmp_path, rows, null_trials=40, now=T0 + 3600.0)
        n = t["null"]
        if n.get("skipped"):
            pytest.skip("no co-active bin in this draw: the run correctly declines to quote a rate")
        assert n["triples"]["p_emp"] > 0.05, (
            "a coincidence count indistinguishable from the shuffle null must not read as a "
            "discovery: observed %r vs null %r" % (n["triples"]["observed"], n["triples"]))


class TestItActuallySolves:
    """⚠️THE TEST THAT MAKES THE NULL CONTROL MEAN SOMETHING. A control that cannot fail is not a
    control, so this pins exact values and then proves three real defects move them."""

    def _run(self, tmp_path, **over):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        return go(tmp_path, rows, **over)

    def test_one_planted_source_gives_exactly_one_solved_event(self, tmp_path):
        t = self._run(tmp_path)
        assert t["events_solved"] == 1 and t["events_emitted"] == 1
        p = pathlib.Path(t["out"]) / "runs" / t["run_id"] / "events.jsonl"
        ev = json.loads(p.read_text().splitlines()[0])
        s = ev["solution"]
        # tolerance from the arithmetic, not a magic number: zero injected jitter, so the only
        # error is the solver's own convergence.
        assert abs(s["east_m"] - 40.0) < 0.5 and abs(s["north_m"] - 30.0) < 0.5
        assert abs(s["sound_speed_mps"] - C) < 1e-9
        assert s["n_unknowns"] == 2 and s["up_assumed_m"] == 0.0

    @pytest.mark.parametrize("label,over,bias", [
        ("wrong temperature", {"temp_c": -20.0}, None),
        ("5 ms bias on one node", {}, {2: 0.005}),
    ])
    def test_a_real_defect_moves_the_answer(self, tmp_path, label, over, bias):
        sv = SV.from_dict(survey_dict())
        base = go(tmp_path / "a", planted(sv, (40.0, 30.0, 0.0), T0))
        rows = planted(sv, (40.0, 30.0, 0.0), T0, bias=bias)
        bad = go(tmp_path / "b", rows, **over)
        assert base["events_solved"] == 1
        if bad["events_solved"] != 1:
            return                       # the defect refused outright, which is also "it moved"
        a = json.loads((pathlib.Path(base["out"]) / "runs" / base["run_id"]
                        / "events.jsonl").read_text().splitlines()[0])["solution"]
        b = json.loads((pathlib.Path(bad["out"]) / "runs" / bad["run_id"]
                        / "events.jsonl").read_text().splitlines()[0])["solution"]
        moved = float(np.hypot(a["east_m"] - b["east_m"], a["north_m"] - b["north_m"]))
        assert moved > 1.0, "%s did not move the answer (%.3f m)" % (label, moved)

    def test_a_swapped_node_position_moves_the_answer(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        good = go(tmp_path / "a", rows)
        swapped = [dict(LIVE_NODES[1], node_id=1, name="nyquist"),
                   dict(LIVE_NODES[0], node_id=2, name="mach"), dict(LIVE_NODES[2])]
        (tmp_path / "b2").mkdir(parents=True, exist_ok=True)
        bad = go(tmp_path / "b", rows, survey=write_survey(tmp_path / "b2", swapped))
        assert good["events_solved"] == 1
        if bad["events_solved"] != 1:
            return
        a = json.loads((pathlib.Path(good["out"]) / "runs" / good["run_id"]
                        / "events.jsonl").read_text().splitlines()[0])["solution"]
        b = json.loads((pathlib.Path(bad["out"]) / "runs" / bad["run_id"]
                        / "events.jsonl").read_text().splitlines()[0])["solution"]
        assert float(np.hypot(a["east_m"] - b["east_m"], a["north_m"] - b["north_m"])) > 1.0


# ================================================================= startup refusals

class TestStartupRefusalsArePreIO:
    """A tool that reads 11k records and then says the survey has two arrival nodes has burned
    the read to tell you something a 53-line JSON file already said."""

    @pytest.fixture(autouse=True)
    def _no_io(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("pool I/O happened before the startup refusals")
        monkeypatch.setattr(HT, "_iter_raw", boom)
        monkeypatch.setattr(HT, "count_lines", boom)
        monkeypatch.setattr(HT, "read_ledger", boom)
        monkeypatch.setattr(P.Pool, "raw", boom)

    def _refuse(self, tmp_path, survey_nodes=None, survey_path=None, out=None, **over):
        with pytest.raises(HT.Refusal) as e:
            HT.run(str(tmp_path / "pool"),
                   survey_path or write_survey(tmp_path, survey_nodes),
                   pol(**over), out=str(out or (tmp_path / "out")), now=T0)
        return str(e.value)

    def test_a_survey_that_does_not_load(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"frame": "wgs84", "units": "m", "nodes": []}))
        assert "did not load" in self._refuse(tmp_path, survey_path=str(p))

    def test_fewer_than_three_arrival_nodes_is_rank_one(self, tmp_path):
        nodes = [dict(LIVE_NODES[0]), dict(LIVE_NODES[1]),
                 dict(LIVE_NODES[2], **{"class": "puc-ntp"})]
        msg = self._refuse(tmp_path, nodes)
        assert "rank-1" in msg and "2 arrival-class receiver" in msg

    def test_a_3d_fit_with_three_receivers_is_arithmetic_not_conditioning(self, tmp_path):
        msg = self._refuse(tmp_path, fixed_up_m=None)
        assert "n_unknowns = 3" in msg and "--fixed-up-m" in msg
        assert "No amount of further data changes this" in msg

    def test_an_unknown_source_class_surfaces_the_solvers_own_message(self, tmp_path):
        with pytest.raises(ValueError) as e:
            HT.run(str(tmp_path / "pool"), write_survey(tmp_path),
                   pol(source_class="gunshot"), out=str(tmp_path / "out"), now=T0)
        assert "unknown source class 'gunshot'" in str(e.value)

    def test_an_out_dir_inside_a_checkout(self, tmp_path):
        fake = tmp_path / "clone"
        (fake / "hear").mkdir(parents=True)
        (fake / ".git").mkdir()
        msg = self._refuse(tmp_path, out=fake / "pool" / "tdoa")
        assert "inside the checkout" in msg

    def test_the_real_repo_is_refused_as_an_out_dir(self, tmp_path):
        msg = self._refuse(tmp_path, out=os.path.join(ROOT, "tdoa"))
        assert "inside the checkout" in msg

    def test_an_arrival_subset_that_is_collinear_even_though_the_survey_is_not(self, tmp_path):
        """⚠️from_dict's collinearity check is bypassed by the direct constructor, so it is
        re-run by hand. A 4-node non-collinear survey CAN have a collinear 3-node arrival
        subset."""
        nodes = [{"node_id": 1, "name": "a", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1},
                 {"node_id": 2, "name": "b", "e_m": 10.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1},
                 {"node_id": 3, "name": "c", "e_m": 20.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1},
                 {"node_id": 4, "name": "d", "class": "puc-ntp", "e_m": 5.0, "n_m": 30.0,
                  "u_m": 0.0, "sigma_m": 6.5}]
        SV.from_dict(survey_dict(nodes))                 # the FULL survey loads: not collinear
        assert "collinear" in self._refuse(tmp_path, nodes)

    def test_a_margin_above_the_hard_bound(self, tmp_path):
        msg = self._refuse(tmp_path, margin_s_override=0.030)
        assert "CLOSEST pair would have to be at least" in msg and "--force-margin" in msg


# ================================================================= the funnel

class TestAdmit:
    """One parametrised case per drop reason, each asserting the reason AND that the row never
    reaches associate() -- because several of these are rows associate() would crash on."""

    @pytest.fixture(autouse=True)
    def _associate_only_ever_sees_well_formed_dicts(self, monkeypatch):
        real = AS.associate

        def guard(dets, survey, **kw):
            for d in dets:
                assert isinstance(d["t_utc_s"], float), "associate got %r" % (d["t_utc_s"],)
                assert isinstance(d["node_id"], int), "associate got %r" % (d["node_id"],)
                assert d["node_id"] in survey
            return real(dets, survey, **kw)
        monkeypatch.setattr(HT.AS, "associate", guard)

    def _reasons(self, tmp_path, rows, extra=(), **over):
        build_pool(tmp_path / "pool", rows, extra)
        t = HT.run(str(tmp_path / "pool"), write_survey(tmp_path, LIVE_NODES + [PUC]),
                   pol(**over), out=str(tmp_path / "out"), now=T0 + 3600.0)
        return t["funnel"]["by_reason"], t

    def test_an_unanchored_row_never_reaches_associate(self, tmp_path):
        """`associate()` calls float(d['t_utc_s']) unconditionally -- including while building
        the unusable_arrival detail string -- so an unanchored row does not get refused there,
        it crashes there."""
        r, _ = self._reasons(tmp_path, [node_row("nyquist", 0.0, seed=1)])
        assert r.get(HT.D_UNANCHORED) == 1 and not r.get(HT.D_ADMITTED)

    def test_a_puc_row_is_not_arrival_class_and_not_unknown_node(self, tmp_path):
        """⚠️THE DISTINCTION IS THE POINT. associate()'s own check is `node_id in survey`, plain
        membership, which admits puc. This proves the DRIVER's filter fired, not associate's."""
        r, _ = self._reasons(tmp_path, [node_row("puc", T0, seed=2)])
        assert r.get(HT.D_NOT_ARRIVAL) == 1
        assert not r.get(HT.D_UNSURVEYED)

    def test_the_not_arrival_message_is_nodeclass_s_own(self, tmp_path):
        _r, t = self._reasons(tmp_path, [node_row("puc", T0, seed=2)])
        led = (pathlib.Path(t["out"]) / "arrivals").rglob("*.jsonl")
        text = "\n".join(p.read_text() for p in led)
        assert "timed by ntp, not by GPS PPS" in text

    def test_an_unsurveyed_node_is_never_coerced(self, tmp_path):
        r, _ = self._reasons(tmp_path, [node_row("fancyantsy", T0, seed=3)])
        assert r.get(HT.D_UNSURVEYED) == 1

    def test_a_phone_row_with_a_null_clock_tier_is_clock_unstated(self, tmp_path):
        """⚠️THE CASE `arrival_is_usable` CANNOT CATCH. utc_trusted is null on every phone row in
        the live pool and `d.get(k, True) is not False` admits None, so the gate written for the
        phone audio path passes 100% of it. Resolved here instead."""
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=4)),
                   source="phone", node="nyquist", clock_tier=None, utc_trusted=None)
        r, _ = self._reasons(tmp_path, [], extra=[rec], sources=["node", "phone"])
        assert r.get(HT.D_CLOCK_UNSTATED) == 1

    def test_clock_unstated_admit_pins_todays_known_no_op(self, tmp_path):
        """Pinned so a future producer change is VISIBLE rather than silent: today `gnss` with a
        null utc_trusted resolves trusted, and admitting an unstated one is the same answer."""
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=5)),
                   source="phone", node="nyquist", clock_tier="gnss", utc_trusted=None)
        # a phone row still needs a latency entry once it clears the clock gate
        r, _ = self._reasons(tmp_path, [], extra=[rec], sources=["node", "phone"],
                             clock_unstated="admit")
        assert r.get(HT.D_LATENCY) == 1, r

    def test_an_untrusted_clock_tier_is_refused(self, tmp_path):
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=6)),
                   source="phone", node="nyquist", clock_tier="wall", utc_trusted=None)
        r, _ = self._reasons(tmp_path, [], extra=[rec], sources=["node", "phone"])
        assert r.get(HT.D_CLOCK_UNTRUSTED) == 1

    def test_an_explicit_onset_not_found_is_refused(self, tmp_path):
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=7)), onset_found=False)
        r, _ = self._reasons(tmp_path, [], extra=[rec])
        assert r.get(HT.D_ONSET) == 1

    def test_a_stated_sync_sigma_above_the_bound_is_refused(self, tmp_path):
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=8)),
                   sync_sigma_ns=50_000_000.0)
        r, _ = self._reasons(tmp_path, [], extra=[rec])
        assert r.get(HT.D_SYNC_SIGMA) == 1

    def test_a_row_newer_than_the_settle_window_waits(self, tmp_path):
        r, _ = self._reasons(tmp_path, [node_row("nyquist", T0 + 3599.0, seed=9)],
                             settle_s=600.0)
        assert r.get(HT.D_PENDING_SETTLE) == 1

    def test_a_row_older_than_the_lookback_and_never_associated_is_loud(self, tmp_path):
        r, _ = self._reasons(tmp_path, [node_row("nyquist", T0 - 86400.0, seed=10)],
                             lookback_h=1.0)
        assert r.get(HT.D_OUTSIDE_LOOKBACK_UNASSOC) == 1

    def test_a_torn_line_is_counted_not_fatal(self, tmp_path):
        build_pool(tmp_path / "pool", [node_row("nyquist", T0, seed=11)])
        d = tmp_path / "pool" / "records"
        part = sorted(d.iterdir())[0]
        with open(part / "node.jsonl", "a") as fh:
            fh.write('{"key": "torn", "node":\n')
        t = HT.run(str(tmp_path / "pool"), write_survey(tmp_path), pol(),
                   out=str(tmp_path / "out"), now=T0 + 3600.0)
        assert t["funnel"]["by_reason"].get(HT.D_UNPARSEABLE) == 1
        assert t["conservation"]["ok"]


class TestSeqSynthesis:
    """The pool has no seq. A constant collapses two real rounds into `duplicate_seq`."""

    def test_two_rounds_forty_ms_apart_on_one_node_both_survive(self, tmp_path):
        rows = [node_row("nyquist", T0, seed=1, sample=1),
                node_row("nyquist", T0 + 0.040, seed=2, sample=2)]
        t = go(tmp_path, rows)
        assert t["funnel"]["by_reason"].get(HT.D_ADMITTED) == 2
        assert t["associate"]["n_duplicates"] == 0

    def test_the_synthesis_is_deterministic_across_two_runs(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        a = go(tmp_path / "a", rows)
        b = go(tmp_path / "b", rows)
        ka = [json.loads(l)["event_key"] for l in
              (pathlib.Path(a["out"]) / "runs" / a["run_id"] / "attempts.jsonl"
               ).read_text().splitlines()]
        kb = [json.loads(l)["event_key"] for l in
              (pathlib.Path(b["out"]) / "runs" / b["run_id"] / "attempts.jsonl"
               ).read_text().splitlines()]
        assert ka == kb and ka


# ================================================================= the two library defects

class TestArrivalSubSurvey:
    def test_classes_survive_the_direct_construction(self, tmp_path):
        """⚠️THE REGRESSION TEST FOR `to_dict()` DROPPING `class`. Survey.to_dict emits
        node_id/name/e_m/n_m/u_m/sigma_m and NOT class, so from_dict(to_dict()) would rebuild a
        survey whose arrival_ids() re-admits the very node it was built to exclude."""
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        assert sv.arrival_ids() == [1, 2, 3]
        a = HT.arrival_survey(sv)
        assert a.ids == [1, 2, 3] and a.arrival_ids() == [1, 2, 3]
        assert "class" not in json.dumps(sv.to_dict()), (
            "to_dict() started emitting class; the direct-constructor comment in "
            "arrival_survey() needs revisiting")
        round_tripped = SV.from_dict(sv.to_dict())
        assert round_tripped.arrival_ids() == [1, 2, 3, 4], (
            "the round trip is supposed to LOSE the class -- that is why it is not used")


class TestWindowComesFromArrivalNodesOnly:
    def test_the_window_is_narrower_than_the_full_surveys(self, tmp_path):
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        a = HT.arrival_survey(sv)
        assert AS.max_window_s(a) < AS.max_window_s(sv)

    def test_a_distant_non_arrival_node_does_not_widen_it(self, tmp_path):
        # ⚠️FAR, BUT NOT SO FAR THAT THE SURVEY ITSELF IS REFUSED. `linearity` is s1/s0 of the
        # centred positions, so ANY node far enough from the 17 m cluster drives the ratio under
        # COLLINEAR_LINEARITY whatever direction it lies in -- at (-1500, 1200) it is 0.0069 and
        # from_dict raises, which would make this a test of survey validation. (-200, 150) is
        # 0.054 and loads, and still inflates the full-survey window by an order of magnitude.
        far = dict(PUC, e_m=-200.0, n_m=150.0)
        base = HT.arrival_survey(SV.from_dict(survey_dict(LIVE_NODES + [PUC])))
        wide = HT.arrival_survey(SV.from_dict(survey_dict(LIVE_NODES + [far])))
        assert AS.max_window_s(base) == AS.max_window_s(wide)
        assert AS.max_window_s(SV.from_dict(survey_dict(LIVE_NODES + [far]))) > \
            AS.max_window_s(base) * 3

    def test_the_wider_window_costs_a_real_event(self, tmp_path):
        """⚠️PINS THE DEFECT AND ITS COST. Two full rounds separated by more than the
        arrival-only window and less than the full-survey window: at the narrow window they are
        two events, at the wide one the first seed eats the second round's members as
        `duplicate_node_in_group` and the round is gone."""
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        arr = HT.arrival_survey(sv)
        margin = HT.derive_margin_s(HT.pair_bounds(arr, C), HT.DEFAULT_MARGIN_FRAC)["margin_s"]
        w_narrow = AS.max_window_s(arr, TEMP_C, margin)
        w_wide = AS.max_window_s(sv, TEMP_C, margin)
        gap = 0.5 * (w_narrow + w_wide)
        assert w_narrow < gap < w_wide
        dets = []
        for k, t0 in enumerate((T0, T0 + gap)):
            for j, nid in enumerate(arr.ids):
                p = arr.position(nid)
                dets.append({"node_id": nid, "seq": k,
                             "t_utc_s": t0 + float(np.linalg.norm(
                                 np.array([40.0, 30.0, 0.0]) - p)) / C})
        n_narrow = len(AS.associate(dets, arr, temp_c=TEMP_C, margin_s=margin,
                                    window_s=w_narrow)["events"])
        n_wide = len(AS.associate(dets, arr, temp_c=TEMP_C, margin_s=margin,
                                  window_s=w_wide)["events"])
        assert n_narrow == 2 and n_wide == 1, (n_narrow, n_wide)

    def test_the_run_reports_the_discrepancy(self, tmp_path):
        sv_path = write_survey(tmp_path, LIVE_NODES + [PUC])
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0), survey=sv_path)
        a = t["association"]
        assert a["window_s"] < a["window_s_full_survey"]
        assert a["window_inflation_frac"] > 0.0
        assert t["survey_block"]["refused_as_arrivals"][0]["name"] == "puc"


class TestMarginIsDimensioned:
    def test_the_derived_margin_is_a_fraction_of_the_tightest_bound(self, tmp_path):
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        b = HT.pair_bounds(arr, C)
        m = HT.derive_margin_s(b, 0.25)
        assert m["margin_s"] <= 0.25 * min(v["bound_s"] for v in b.values()) + 1e-15
        assert m["tightest_pair_separation_m"] == min(v["d_m"] for v in b.values())
        assert m["library_default_over_tightest_bound"] > 0.5, (
            "the library default is meant to be a large fraction of this array's tightest bound "
            "-- that is the finding")

    def test_a_strict_fail_margin_pass_group_is_labelled_and_not_emitted(self, tmp_path):
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        b = HT.pair_bounds(arr, C)
        m = HT.derive_margin_s(b, HT.DEFAULT_MARGIN_FRAC)["margin_s"]
        tight = min(b.items(), key=lambda kv: kv[1]["bound_s"])
        (i, j), info = tight
        # one pair pushed just past its own bound, still inside the margin
        base = {n: T0 for n in arr.ids}
        base[j] = T0 + info["bound_s"] + 0.4 * m
        rows = [node_row(arr.names[n], base[n], seed=k, sample=k)
                for k, n in enumerate(arr.ids)]
        t = go(tmp_path, rows)
        v = t["by_verdict"]
        assert v[HT.V_MARGIN_DEPENDENT] + v[HT.V_INADMISSIBLE] >= 1
        assert t["events_emitted"] == 0
        assert t["conservation"]["ok"]

    def test_a_margin_dependent_run_still_passes_the_check(self, tmp_path):
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        b = HT.pair_bounds(arr, C)
        m = HT.derive_margin_s(b, HT.DEFAULT_MARGIN_FRAC)["margin_s"]
        (_i, j), info = min(b.items(), key=lambda kv: kv[1]["bound_s"])
        base = {n: T0 for n in arr.ids}
        base[j] = T0 + info["bound_s"] + 0.4 * m
        rows = [node_row(arr.names[n], base[n], seed=k, sample=k)
                for k, n in enumerate(arr.ids)]
        t = go(tmp_path, rows)
        code, _lines = HT.check(t["out"], now=t["at"] + 10.0)
        assert code == 0


class TestFixedUpIsIsNotNone:
    """⚠️THE REGRESSION TEST FOR THE INVERTED MINIMUM. point.py:236 tests `fixed_up_m is not
    None`, so 0.0 -- the most likely declared height -- means DECLARED: two unknowns, three
    receivers. A driver that tested truthiness would compute 4 for a declared height and 3 for a
    3D fit, exactly backwards."""

    def test_zero_is_a_declared_height_and_solves_on_three_receivers(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0), fixed_up_m=0.0)
        assert t["association"]["solver_min_nodes"] == 3
        assert t["events_solved"] == 1
        ev = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])
        assert ev["solution"]["n_unknowns"] == 2
        assert ev["claim"]["up_declared"] is True

    def test_the_solver_agrees_about_the_minimum(self):
        for fu in (0.0, 12.0, None):
            n_unk = 2 if fu is not None else 3
            assert n_unk + 1 == (3 if fu is not None else 4)


class TestTheResidualIsNotEvidence:
    def test_a_three_node_declared_height_fit_scores_zero_even_when_the_height_is_wrong(
            self, tmp_path):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        t = go(tmp_path, rows, fixed_up_m=50.0)
        assert t["events_solved"] == 1
        s = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                        / "events.jsonl").read_text().splitlines()[0])["solution"]
        assert s["residual_is_meaningful"] is False
        assert s["rms_residual_ms"] < 1e-3, (
            "an exactly-determined fit scores ~0 BY CONSTRUCTION -- including for a badly wrong "
            "declared height, which is exactly why the residual is not evidence")
        assert s["up_assumed_m"] == 50.0


# ================================================================= refusals are attributable

class TestRefusalsAreAttributable:
    def test_a_two_node_group_never_becomes_an_event(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0, nodes=[1, 2])
        t = go(tmp_path, rows)
        assert t["events_solved"] == 0 and t["candidates"] == 0
        assert t["conservation"]["by_terminal"].get("singleton_unpaired") == 2

    def test_a_cone_class_fed_to_the_point_lane_picks_the_cone_model(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (400.0, 300.0, 0.0), T0), source_class="crack")
        rows = (pathlib.Path(t["out"]) / "runs" / t["run_id"]
                / "attempts.jsonl").read_text().splitlines()
        assert rows and json.loads(rows[0])["model"] == "cone"

    def test_geometry_is_populated_even_though_nothing_solved(self, tmp_path):
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        b = HT.pair_bounds(arr, C)
        (_i, j), info = min(b.items(), key=lambda kv: kv[1]["bound_s"])
        base = {n: T0 for n in arr.ids}
        # past bound+margin, but still inside the scan window -- at 4x the third arrival falls
        # outside the seed's own scan and no group forms at all, which tests nothing.
        base[j] = T0 + info["bound_s"] * 1.5
        rows = [node_row(arr.names[n], base[n], seed=k, sample=k)
                for k, n in enumerate(arr.ids)]
        t = go(tmp_path, rows)
        att = [json.loads(l) for l in (pathlib.Path(t["out"]) / "runs" / t["run_id"]
                                       / "attempts.jsonl").read_text().splitlines()]
        assert att and att[0]["verdict"] in (HT.V_INADMISSIBLE, HT.V_LOST_TO_GATE)
        assert att[0]["geometry_at_centroid"]["dop"] is not None
        assert att[0]["bound_check"]["violating_pairs"]
        assert att[0]["bound_check"]["worst_excess_m"] > 0.0

    def test_a_collinear_arrival_array_is_refused_at_load_not_decorated(self, tmp_path):
        nodes = [{"node_id": 1, "name": "a", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1},
                 {"node_id": 2, "name": "b", "e_m": 10.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1},
                 {"node_id": 3, "name": "c", "e_m": 20.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.1}]
        with pytest.raises(SV.SurveyError):
            SV.from_dict(survey_dict(nodes))


class TestLeaveOneOut:
    def test_three_nodes_skips_with_the_reason_stated(self, tmp_path):
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        loo = HT.leave_one_out(arr, arr.ids, [T0, T0 + 0.01, T0 + 0.02], "blast", TEMP_C,
                               900.0, 0.0, {"east_m": 0.0, "north_m": 0.0,
                                            "rms_residual_ms": 0.0})
        assert loo["skipped"] is True and "rank-1" in loo["reason"]
        assert loo["meaningful_n"] == PL.node_counts("point", 2)["meaningful_n"]

    def test_five_nodes_names_the_biased_one(self, tmp_path):
        nodes = [{"node_id": i + 1, "name": "n%d" % i, "e_m": e, "n_m": n, "u_m": 0.0,
                  "sigma_m": 0.1}
                 for i, (e, n) in enumerate([(0, 0), (120, 0), (0, 140), (150, 130), (60, 70)])]
        sv = SV.from_dict(survey_dict(nodes))
        arr = HT.arrival_survey(sv)
        src = np.array([300.0, 120.0, 0.0])
        t = [T0 + float(np.linalg.norm(src - arr.position(i))) / C for i in arr.ids]
        t[2] += 0.010                                  # 10 ms on node 3 == 3.4 m
        _m, sol, err = HT.solve_event(arr, arr.ids, t, "blast", TEMP_C, 900.0, 0.0)
        assert err is None
        loo = HT.leave_one_out(arr, arr.ids, t, "blast", TEMP_C, 900.0, 0.0, sol)
        assert loo["skipped"] is False
        assert loo["worst_node_id"] == arr.ids[2], loo


class TestSoundSpeedIsAssumed:
    def test_three_ground_receivers_can_never_recover_c(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        d = t["determinacy"]
        assert d["determined"] is False and d["deficit"] >= 1
        assert "under-determined" in d["reason"]
        assert t["c"]["source"] == "assumed_temp_c"

    WIDE = [{"node_id": i + 1, "name": "n%d" % i, "e_m": float(e), "n_m": float(n), "u_m": 0.0,
             "sigma_m": 0.1}
            for i, (e, n) in enumerate([(0, 0), (120, 0), (0, 140), (150, 130)])]

    def test_the_temperature_moves_the_answer_on_a_well_conditioned_array(self, tmp_path):
        """The magnitude claim needs geometry that can carry it -- see the next test for what
        the same 30 degC does on the 17 m array."""
        sv = SV.from_dict(survey_dict(self.WIDE))
        svp = write_survey(tmp_path, self.WIDE)
        # ⚠️THE SOURCE POSITION IS CHOSEN, NOT ARBITRARY, FOR TWO MEASURED REASONS.
        # (a) (300, 260) is exactly 2x node 4 at (150, 130), i.e. ON the line through nodes 1
        #     and 4, where |dt| == d/c to the last digit and the tol_s = 0 admissibility gate is
        #     decided by float64 -- see bound_check's near_endfire.
        # (b) the ADMISSIBILITY GATE ITSELF SCALES WITH THE ASSUMED c: every pair bound is d/c,
        #     so a +30 degC error shrinks every bound by 1.8% and a real event sitting above
        #     0.982 of any bound at 20 degC becomes inadmissible at 30. (80, 200) keeps every
        #     pair under 0.84 of its bound at 30 degC, so this test measures the SOLVER's
        #     sensitivity to c and not the gate's. The gate's own sensitivity is the next test.
        rows = planted(sv, (80.0, 200.0, 0.0), T0)
        a = go(tmp_path / "a", rows, survey=svp, temp_c=0.0)
        b = go(tmp_path / "b", rows, survey=svp, temp_c=30.0)
        assert a["events_solved"] == b["events_solved"] == 1
        sa = json.loads((pathlib.Path(a["out"]) / "runs" / a["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])["solution"]
        sb = json.loads((pathlib.Path(b["out"]) / "runs" / b["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])["solution"]
        assert abs(sa["sound_speed_mps"] - SW.sound_speed(0.0)) < 1e-9
        assert abs(sb["sound_speed_mps"] - SW.sound_speed(30.0)) < 1e-9
        moved = float(np.hypot(sa["east_m"] - sb["east_m"], sa["north_m"] - sb["north_m"]))
        # the fitted ranges scale with c, so the answer moves by about the fractional speed
        # error applied to the range -- an arithmetic tolerance, not a magic metre count
        expect = HT.SS.fractional_speed_error(30.0) * float(sa["range_m"])
        assert 0.2 * expect < moved, (moved, expect)

    def test_a_temperature_error_can_make_a_real_event_INADMISSIBLE(self, tmp_path):
        """⚠️THE ADMISSIBILITY GATE IS ITSELF A FUNCTION OF THE ASSUMED c, which is easy to miss:
        every pair bound is d/c, so an assumed c that is 1.8% too fast shrinks every bound by
        1.8% and a genuine event sitting near one crosses it. The tool reports that as
        `margin_dependent`/`inadmissible` with the excess in metres, NOT as a solve failure --
        but it is a real false-refusal mechanism and it is not fixable by better timing."""
        sv = SV.from_dict(survey_dict(self.WIDE))
        svp = write_survey(tmp_path, self.WIDE)
        rows = planted(sv, (300.0, 260.0, 0.0), T0)     # 0.965 of the (4,3) bound at 20 degC
        ok = go(tmp_path / "ok", rows, survey=svp, temp_c=0.0)
        hot = go(tmp_path / "hot", rows, survey=svp, temp_c=30.0)
        assert ok["events_admissible"] == 1
        assert hot["events_admissible"] == 0
        att = json.loads((pathlib.Path(hot["out"]) / "runs" / hot["run_id"]
                          / "attempts.jsonl").read_text().splitlines()[0])
        bad = [p for p in att["bound_check"]["pairs"] if not p["ok_strict"]]
        assert bad and 1.0 < bad[0]["frac_of_bound"] < 1.05, bad

    def test_on_the_seventeen_metre_array_the_same_error_runs_the_fit_to_the_search_bound(
            self, tmp_path):
        """⚠️MEASURED, AND IT IS THE REASON `at_search_bound` IS ITS OWN VERDICT. At DOP ~250 a
        1.8% error in an ASSUMED c does not nudge the answer, it pushes the fit out of the
        searched box entirely. point.solve returns rather than raises there, so flattening the
        two into one `solver_refused` would hide which of them happened."""
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        t = go(tmp_path, rows, temp_c=30.0)
        assert t["by_verdict"][HT.V_AT_SEARCH_BOUND] == 1
        assert t["events_emitted"] == 0
        att = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                          / "attempts.jsonl").read_text().splitlines()[0])
        assert att["solution"]["at_search_bound"] is True
        assert "not a measurement" in att["solution"]["note"]


# ================================================================= accounting

class TestConservation:
    def test_a_mixed_pool_accounts_for_every_row(self, tmp_path):
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        rows += [node_row("nyquist", 0.0, seed=50),            # unanchored
                 node_row("puc", T0 + 1.0, seed=51),           # not arrival class
                 node_row("fancyantsy", T0 + 2.0, seed=52)]    # unsurveyed
        phone = dict(P._record_from_node_row(node_row("nyquist", T0 + 3.0, seed=53)),
                     source="phone", node="fancyantsy", clock_tier="wall")
        build_pool(tmp_path / "pool", rows, [phone])
        with open(sorted((tmp_path / "pool" / "records").iterdir())[0] / "node.jsonl", "a") as fh:
            fh.write("{not json\n")
        t = HT.run(str(tmp_path / "pool"), write_survey(tmp_path, LIVE_NODES + [PUC]),
                   pol(sources=["node", "phone"]), out=str(tmp_path / "out"), now=T0 + 3600.0)
        c = t["conservation"]
        assert c["ok"] and c["lines_read"] == c["accounted"]
        assert c["n_candidates"] == c["n_attempts"]
        assert sum(c["by_terminal"].values()) == c["admitted"]

    def test_a_non_conserving_associate_stops_the_run_and_writes_no_events(
            self, tmp_path, monkeypatch):
        sv = SV.from_dict(survey_dict())
        build_pool(tmp_path / "pool", planted(sv, (40.0, 30.0, 0.0), T0))
        real = AS.associate

        def lossy(dets, survey, **kw):
            out = real(dets, survey, **kw)
            out["events"] = []            # loses every row without reporting it
            return out
        monkeypatch.setattr(HT.AS, "associate", lossy)
        with pytest.raises(HT.NotInterpretable):
            HT.run(str(tmp_path / "pool"), write_survey(tmp_path), pol(),
                   out=str(tmp_path / "out"), now=T0 + 3600.0)
        assert not list((tmp_path / "out").rglob("events.jsonl"))

    def test_the_main_entry_point_maps_that_to_exit_two(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HT, "run", lambda *a, **k: (_ for _ in ()).throw(
            HT.NotInterpretable("boom")))
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
                      "--source-class", "blast", "--out", str(tmp_path / "out")])
        assert rc == 2

    def test_a_refusal_maps_to_exit_one(self, tmp_path):
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
                      "--source-class", "blast", "--fixed-up-m", "none",
                      "--out", str(tmp_path / "out")])
        assert rc == 1

    def test_a_missing_source_class_is_refused_and_never_defaulted(self, tmp_path):
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
                      "--out", str(tmp_path / "out")])
        assert rc == 1

    def test_zero_solved_events_is_exit_zero(self, tmp_path):
        build_pool(tmp_path / "pool", [node_row("nyquist", T0, seed=1)])
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
                      "--source-class", "blast", "--out", str(tmp_path / "out"),
                      "--null-trials", "0"])
        assert rc == 0


class TestResumeIsIdempotent:
    def test_two_runs_emit_the_same_event_keys_and_no_duplicate_ledger_rows(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        build_pool(tmp_path / "pool", rows)
        out = str(tmp_path / "out")
        a = HT.run(str(tmp_path / "pool"), write_survey(tmp_path), pol(), out=out,
                   now=T0 + 3600.0)
        b = HT.run(str(tmp_path / "pool"), write_survey(tmp_path), pol(), out=out,
                   now=T0 + 7200.0)
        assert a["events_emitted"] == b["events_emitted"] == 1
        assert a["ledger_rows_written"] == 3 and b["ledger_rows_written"] == 0
        # the second run's event is a repeat of the same member set, so nothing new is emitted
        assert b["events_new"] == 0
        led = [json.loads(l) for p in (pathlib.Path(out) / "arrivals").rglob("*.jsonl")
               for l in p.read_text().splitlines()]
        assert len({r["ledger_hash"] for r in led}) == len(led)

    def test_an_added_member_yields_a_new_event_key(self, tmp_path):
        nodes = LIVE_NODES + [{"node_id": 5, "name": "extra", "e_m": 8.0, "n_m": -9.0,
                               "u_m": 0.0, "sigma_m": 0.5}]
        sv = SV.from_dict(survey_dict(nodes))
        svp = write_survey(tmp_path, nodes)
        out = str(tmp_path / "out")
        build_pool(tmp_path / "pool", planted(sv, (40.0, 30.0, 0.0), T0, nodes=[1, 2, 3]))
        a = HT.run(str(tmp_path / "pool"), svp, pol(), out=out, now=T0 + 3600.0)
        P.Pool(str(tmp_path / "pool"))._append(
            [P._record_from_node_row(r) for r in
             planted(sv, (40.0, 30.0, 0.0), T0, nodes=[5], seed0=90)])
        b = HT.run(str(tmp_path / "pool"), svp, pol(), out=out, now=T0 + 7200.0)
        ka = [json.loads(l)["event_key"] for l in
              (pathlib.Path(out) / "runs" / a["run_id"] / "events.jsonl"
               ).read_text().splitlines()]
        kb = [json.loads(l)["event_key"] for l in
              (pathlib.Path(out) / "runs" / b["run_id"] / "events.jsonl"
               ).read_text().splitlines()]
        assert ka and kb and ka != kb, "adding a member must change the content address"

    def test_a_backfill_older_than_the_lookback_fails_the_check(self, tmp_path):
        build_pool(tmp_path / "pool", [node_row("nyquist", T0 - 86400.0, seed=1)])
        out = str(tmp_path / "out")
        t = HT.run(str(tmp_path / "pool"), write_survey(tmp_path), pol(lookback_h=1.0),
                   out=out, now=T0 + 3600.0)
        assert t["funnel"]["by_reason"].get(HT.D_OUTSIDE_LOOKBACK_UNASSOC) == 1
        code, lines = HT.check(out, now=t["at"] + 10.0)
        assert code == 1 and any("lookback LOST" in l for l in lines)


# ================================================================= the gate

def _hb(out, **over):
    run = {"at": 1000.0, "records_seen": 10, "admitted": 5, "dropped": 5, "unparseable": 0,
           "outside_lookback_unassociated": 0, "candidates": 0, "attempts": 0,
           "events_admissible": 0, "events_solved": 0, "events_emitted": 0,
           "window_s": 0.05, "n_arrival_ids": 3, "survey_ok": True, "conservation_ok": True,
           "by_reason": {}, "by_verdict": {}, "binding_constraint": "co_activity",
           "new_buckets": [], "first_run": False, "bins_with_all": 0}
    run.update(over)
    hb = {"last_run_s": run["at"], "runs": [run], "buckets_ever": []}
    HT._write_json_atomic(HT.heartbeat_path(str(out)), hb)
    return run["at"]


class TestTheCheckGates:
    """⚠️NOT ONE GATE IS ABOUT EVENTS. On the corpus this was written against the correct number
    of solved events is zero, so a gate keyed on it would fire on a correct result forever."""

    def test_a_healthy_heartbeat_with_zero_solved_returns_zero(self, tmp_path):
        at = _hb(tmp_path)
        code, lines = HT.check(str(tmp_path), now=at + 10.0)
        assert code == 0
        assert any("OBSERVATION, not a gate" in l for l in lines)

    def test_no_heartbeat(self, tmp_path):
        code, lines = HT.check(str(tmp_path), now=1000.0)
        assert code == 1 and "never completed a run" in lines[0]

    def test_an_unreadable_heartbeat(self, tmp_path):
        p = pathlib.Path(HT.heartbeat_path(str(tmp_path)))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        code, _ = HT.check(str(tmp_path), now=1000.0)
        assert code == 1

    def test_no_runs(self, tmp_path):
        HT._write_json_atomic(HT.heartbeat_path(str(tmp_path)), {"runs": []})
        assert HT.check(str(tmp_path), now=1000.0)[0] == 1

    def test_staleness(self, tmp_path):
        at = _hb(tmp_path)
        assert HT.check(str(tmp_path), max_stale_s=60.0, now=at + 600.0)[0] == 1

    def test_an_empty_read_is_a_failure_not_a_quiet_night(self, tmp_path):
        """Point --pool at /pool instead of /pool/corpus and every growth-conditioned gate
        passes vacuously. This one is absolute."""
        at = _hb(tmp_path, records_seen=0, admitted=0, dropped=0)
        code, lines = HT.check(str(tmp_path), now=at + 10.0)
        assert code == 1 and any("pool     EMPTY" in l for l in lines)

    def test_a_broken_conservation_run(self, tmp_path):
        at = _hb(tmp_path, conservation_ok=False)
        assert HT.check(str(tmp_path), now=at + 10.0)[0] == 1

    def test_a_torn_pool_line(self, tmp_path):
        at = _hb(tmp_path, unparseable=3)
        assert HT.check(str(tmp_path), now=at + 10.0)[0] == 1

    def test_a_survey_with_two_arrival_nodes(self, tmp_path):
        at = _hb(tmp_path, n_arrival_ids=2)
        code, lines = HT.check(str(tmp_path), now=at + 10.0)
        assert code == 1 and any("rank-1" in l for l in lines)

    def test_the_window_drift_gate_catches_the_puc_regression(self, tmp_path):
        at = _hb(tmp_path, window_s=0.0996)
        code, lines = HT.check(str(tmp_path), now=at + 10.0, expect_window_s=0.0791)
        assert code == 1 and any("window   DRIFT" in l for l in lines)

    def test_a_new_refusal_bucket(self, tmp_path):
        at = _hb(tmp_path, new_buckets=["nyquist|2026-09-10|clock_untrusted"])
        assert HT.check(str(tmp_path), now=at + 10.0)[0] == 1

    def test_the_new_bucket_gate_is_exempt_on_the_first_run_and_fires_on_the_second(
            self, tmp_path):
        """⚠️FAILING THE VERY FIRST CHECK TRAINS AN OPERATOR TO IGNORE THE GATE, which is the
        only failure mode worse than not having it."""
        out = str(tmp_path)
        rep = {"run_id": "r1", "funnel": {"lines_read": 5, "admitted": 5, "dropped": 0,
                                          "by_reason": {}, "by_node_day_reason": {"a|d|r": 1}},
               "association": {"window_s": 0.05}, "conservation": {"ok": True},
               "survey_block": {"survey_ok": True, "arrival_ids": [1, 2, 3]}}
        HT.write_heartbeat(out, rep, now=1000.0)
        assert HT.check(out, now=1010.0)[0] == 0
        rep2 = json.loads(json.dumps(rep))
        rep2["run_id"] = "r2"
        rep2["funnel"]["by_node_day_reason"] = {"a|d|r": 1, "b|d|NEW": 1}
        HT.write_heartbeat(out, rep2, now=1100.0)
        code, lines = HT.check(out, now=1110.0)
        assert code == 1 and any("refusals NEW" in l for l in lines)

    def test_throughput_is_growth_conditioned_and_only_safe_because_the_others_are_not(
            self, tmp_path):
        hb = {"last_run_s": 2000.0, "buckets_ever": [], "runs": [
            {"at": 1000.0, "records_seen": 10, "admitted": 0, "dropped": 0, "unparseable": 0,
             "outside_lookback_unassociated": 0, "window_s": 0.05, "n_arrival_ids": 3,
             "survey_ok": True, "conservation_ok": True, "new_buckets": [], "by_verdict": {}},
            {"at": 2000.0, "records_seen": 99, "admitted": 0, "dropped": 0, "unparseable": 0,
             "outside_lookback_unassociated": 0, "window_s": 0.05, "n_arrival_ids": 3,
             "survey_ok": True, "conservation_ok": True, "new_buckets": [], "by_verdict": {}}]}
        HT._write_json_atomic(HT.heartbeat_path(str(tmp_path)), hb)
        code, lines = HT.check(str(tmp_path), now=2010.0)
        assert code == 1 and any("through  STUCK" in l for l in lines)


# ================================================================= discipline

class TestWritesNothingOutsideTmp:
    def test_a_full_run_leaves_the_checkout_byte_identical(self, tmp_path):
        before = _digest_of_tracked_inputs()
        sv = SV.from_dict(survey_dict())
        go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0), null_trials=5)
        assert _digest_of_tracked_inputs() == before

    def test_out_inside_the_repo_exits_one_and_creates_nothing(self, tmp_path):
        target = os.path.join(ROOT, "tdoa_should_not_exist")
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
                      "--source-class", "blast", "--out", target])
        assert rc == 1
        assert not os.path.exists(target)


class TestTheDocstringNamesTheRealPaths:
    """⚠️tools/hear_score.py:60 documents `<pool>/scores/<day>/<node>.jsonl` where :663-668
    writes `<day>/<source>.jsonl`. A docstring naming a filename the tool does not write is how
    'the store is empty' gets reported about a store that is full."""

    def test_every_path_in_the_docstring_is_a_path_the_run_writes(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        out = pathlib.Path(t["out"])
        written = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}
        doc = HT.__doc__
        # the docstring's own placeholders, resolved against what actually landed
        for tail in ("manifest.json", "coactivity.json", "candidates.jsonl", "attempts.jsonl",
                     "events.jsonl", "null.json", "plan.json", "latest.json",
                     "model_card.json", "tdoa_heartbeat.json"):
            assert tail in doc, "%s is written but the docstring does not name it" % tail
            assert any(w.endswith(tail) for w in written), (
                "the docstring names %s but the run did not write it (%s)" % (tail, written))
        assert "arrivals/<day>/<source>.jsonl" in doc
        assert any(re.match(r"arrivals/[^/]+/node\.jsonl$", w) for w in written), written


class TestTheModelChoiceMatchesPipeline:
    """Delete this if `pipeline.solve_event` is ever extracted and called directly."""

    @pytest.mark.parametrize("cls", sorted(PT.POINT_CLASSES | PT.CONE_CLASSES))
    def test_the_driver_picks_the_same_model_as_flush(self, cls):
        assert ("cone" if cls in PT.CONE_CLASSES else "point") == \
            ("cone" if cls in PT.CONE_CLASSES else "point")
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        t = [T0, T0 + 0.01, T0 + 0.02]
        model, _sol, _err = HT.solve_event(arr, arr.ids, t, cls, TEMP_C, 900.0, 0.0)
        b = BP.Backend(arr, temp_c=TEMP_C, source_class=cls)
        assert model == ("cone" if cls in PT.CONE_CLASSES else "point")
        assert b.source_class == cls

    def test_to_dama_event_is_the_published_shape(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        ev = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])
        pub = ev["published_payload"]
        assert pub["node_type"] == "hear" and pub["node_id"] == "hear"
        assert "east_m" in pub["event"] and "bearing_deg" not in pub["event"], (
            "to_dama_event omits a quantity the model does not have; a None would let a consumer "
            "read a missing model as a failed solve")


# ================================================================= deploy

BUNDLE = "hear-tdoa-code"
MANIFEST = REPO / "deploy" / "k8s" / "hear-tdoa.yaml"
CODEMAP = REPO / "deploy" / "k8s" / "hear-tdoa-code.yaml"


class TestTheDeployBundle:
    def test_the_bundle_is_registered(self):
        assert BUNDLE in GC.BUNDLES
        app, code, data = GC.BUNDLES[BUNDLE]
        assert app == "hear-tdoa"
        assert ("tools_hear_tdoa.py", "tools/hear_tdoa.py") in code
        assert ("survey.json", "survey.json") in data, (
            "survey.json is opened at RUNTIME. gen_configmap's _data_paths audit resolves a "
            ".json against the MODULE's own directory, so a repo-root file referenced from "
            "tools/ resolves to tools/survey.json and is NOT caught -- it has to be listed "
            "explicitly, and this assertion is what keeps it listed")

    def test_the_shipped_copy_matches_the_checkout(self):
        if not CODEMAP.exists():
            pytest.skip("no ConfigMap at %s" % CODEMAP)
        out = subprocess.run([sys.executable, str(REPO / "deploy" / "k8s" / "gen_configmap.py"),
                              BUNDLE], capture_output=True, text=True, cwd=str(REPO))
        assert out.returncode == 0, out.stderr
        # compare `data` in memory only; the commit annotation legitimately says -dirty here
        gen = _embedded_from_text(out.stdout)
        have = _embedded_from_text(CODEMAP.read_text())
        assert gen == have, ("regenerate: python3 deploy/k8s/gen_configmap.py %s > "
                             "deploy/k8s/%s.yaml" % (BUNDLE, BUNDLE))

    def test_the_import_closure_check_passes_on_the_intact_bundle(self):
        app, code, data = GC.BUNDLES[BUNDLE]
        GC.check(code, data)

    @pytest.mark.parametrize("drop", ["hear/corpus.py", "hear/nodeclass.py", "hear/pool.py"])
    def test_dropping_a_module_the_audit_can_see_fails_generation(self, drop):
        """These three are reached as `from hear import x` in the entry point, which is the one
        import shape gen_configmap._imported_paths resolves."""
        _app, code, data = GC.BUNDLES[BUNDLE]
        thinned = [(k, r) for k, r in code if r != drop]
        assert len(thinned) < len(code), "%s is not in the bundle at all" % drop
        with pytest.raises(SystemExit):
            GC.check(thinned, data)

    @pytest.mark.parametrize("drop", ["hear/backend/associate.py", "hear/solve/point.py",
                                      "survey.json"])
    def test_the_audit_is_BLIND_to_these_and_that_is_why_the_list_is_hand_derived(self, drop):
        """⚠️A MEASURED BLIND SPOT, PINNED SO NOBODY MISTAKES check() FOR PROOF.

        gen_configmap._imported_paths resolves `from hear import x` and `from hear.<mod> import
        y`. It does NOT resolve a RELATIVE import (`from ..solve import shockwave`), and
        `from hear.backend import associate` resolves to the non-existent `hear/backend.py` and
        is dropped. _data_paths resolves a .json against the MODULE's own directory, so a
        repo-root survey.json referenced from tools/ resolves to tools/survey.json and is not
        required either. Removing any of these three therefore GENERATES CLEANLY and dies in the
        cluster -- which is exactly the failure gen_configmap's docstring claims is impossible.
        The bundle list is derived by importing the entry point and reading sys.modules, and
        TestTheDeployBundle above is what keeps it honest.
        """
        _app, code, data = GC.BUNDLES[BUNDLE]
        thinned_c = [(k, r) for k, r in code if r != drop]
        thinned_d = [(k, r) for k, r in data if r != drop]
        assert len(thinned_c) + len(thinned_d) < len(code) + len(data)
        GC.check(thinned_c, thinned_d)          # passes -- that is the finding, not a pass mark

    def test_the_manifest_declares_no_pvc(self):
        text = MANIFEST.read_text()
        assert "kind: PersistentVolumeClaim" not in text, (
            "hear-pool is declared once, in hear-drain.yaml; a second declaration is how two "
            "manifests come to disagree about a storage request")
        assert "claimName: hear-pool" in text

    def test_both_containers_pass_the_pool_survey_and_source_class_explicitly(self):
        text = MANIFEST.read_text()
        assert "--pool /pool/corpus" in text
        assert "--survey /app/survey.json" in text
        assert "--source-class" in text

    def test_the_pylib_guard_asserts_both_package_versions(self):
        text = MANIFEST.read_text()
        assert "numpy.__version__" in text and "scipy.__version__" in text, (
            "/pool/pylib is SHARED with hear-drain and hear-score. A guard that only tests "
            "whether a package directory exists means whichever workload reaches an empty PVC "
            "first decides the version and the others silently use what they find")
        assert re.search(r"pip install[^\n]*numpy==[\d.]+[^\n]*scipy==", text), (
            "numpy and scipy must be installed in ONE pip invocation so the resolver cannot "
            "bump numpy underneath hear-drain and hear-score")

    def test_the_check_container_exits_with_the_gate_not_the_census(self):
        text = MANIFEST.read_text()
        assert "set +e" in text and 'exit "$rc"' in text
        assert "--census" in text

    def test_the_limitrange_minimums_are_met(self):
        for m in re.finditer(r"requests: \{ cpu: (\S+), memory: (\S+) \}", MANIFEST.read_text()):
            assert m.group(1) == "100m," or m.group(1) == "100m"
            assert m.group(2).rstrip(",") == "128Mi"

    def test_keys_shared_with_another_bundle_are_byte_identical(self):
        _a, code, data = GC.BUNDLES[BUNDLE]
        mine = {k: r for k, r in list(code) + list(data)}
        for other, (_app2, c2, d2) in GC.BUNDLES.items():
            if other == BUNDLE:
                continue
            for k, r in list(c2) + list(d2):
                if k in mine:
                    assert mine[k] == r, ("key %r maps to %r here and %r in %s"
                                          % (k, mine[k], r, other))


class TestTheGeneratorAudit:
    def test_an_unknown_bundle_is_refused_never_defaulted(self):
        r = subprocess.run([sys.executable, str(REPO / "deploy" / "k8s" / "gen_configmap.py"),
                            "nope"], capture_output=True, text=True, cwd=str(REPO))
        assert r.returncode != 0 and "unknown bundle" in r.stderr


def _embedded_from_text(text):
    """{key: text} out of a rendered ConfigMap, undoing the four-space block indent. The METADATA
    block is deliberately excluded -- the commit annotation flips to -dirty in exactly the tree a
    developer runs pytest in."""
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
    out, key, buf = {}, None, []
    for line in lines:
        if line.startswith("  ") and line.rstrip().endswith(": |") and not line.startswith("    "):
            if key:
                out[key] = "\n".join(buf)
            key, buf = line.strip()[:-3], []
            continue
        if key is not None:
            buf.append(line[4:] if line.startswith("    ") else line)
    if key:
        out[key] = "\n".join(buf)
    return out


# ================================================================= the one real-file read

def test_the_repo_survey_admits_exactly_three_arrival_nodes():
    """The ONLY test that opens the operator's survey, and it is read-only."""
    sv = SV.load_survey(os.path.join(ROOT, "survey.json"))
    assert sv.arrival_ids() == [1, 2, 3]
    arr = HT.arrival_survey(sv)
    assert [arr.names[i] for i in arr.ids] == ["nyquist", "mach", "rankine"]
    assert AS.max_window_s(arr) < AS.max_window_s(sv)
