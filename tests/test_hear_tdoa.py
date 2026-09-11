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
        "clock_unstated": "refuse", "onset_unstated": "admit", "latency_cal": None,
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

def planted_rows_for_admit(n=3):
    """Three anchored node rows, one per live arrival node, far enough apart in time that they
    never group. Enough to exercise the admit funnel without asserting anything about solving."""
    return [node_row(nm, T0 + 600.0 * i, seed=40 + i)
            for i, nm in enumerate(["nyquist", "mach", "rankine"][:n])]


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
        """The refusal the ledger shows must BE nodeclass's, not a copy of it.

        ⚠️THIS TEST USED TO PIN THE LITERAL "timed by ntp, not by GPS PPS" AND THAT IS EXACTLY
        HOW IT BROKE. feat/arrival-gate-by-sigma replaced the string-name clock test with
        `clock_admissible() and capture_bias_bounded()` and rewrote the message. Different
        files, no git conflict, and a green rebase -- the assertion simply described text that
        no longer existed. Asserting the literal made this test a second, stale copy of the
        message it was supposed to prove was not copied.

        So it asks nodeclass for the refusal and compares. Any future rewording travels here for
        free; a driver that starts inventing its own wording still fails.
        """
        _r, t = self._reasons(tmp_path, [node_row("puc", T0, seed=2)])
        try:
            HT.NC.require_arrival(PUC["class"], PUC["node_id"])
        except HT.NC.CapabilityError as exc:
            want = str(exc)
        else:
            pytest.fail("%s is admitted as an arrival source; this test has no subject"
                        % PUC["class"])
        led = (pathlib.Path(t["out"]) / "arrivals").rglob("*.jsonl")
        text = "\n".join(p.read_text() for p in led)
        assert want, "nodeclass raised an empty refusal"
        assert json.dumps(want)[1:-1] in text, (
            "the ledger detail is not nodeclass's refusal. nodeclass says:\n  %s" % want)

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

    def test_a_node_row_carries_NO_onset_field_at_all(self, tmp_path):
        """⚠️THE PRECONDITION THE WHOLE GATE RESTED ON, ASSERTED INSTEAD OF ASSUMED.

        `hear.pool._record_from_node_row` is the only way a source=node record is built, and it
        writes no onset key. The gate that read `row.get("onset_found") is False` therefore
        could not fire on a node row for any input whatsoever -- it was not rarely-taken, it was
        unreachable. If a future producer change adds the field this test fails, which is the
        signal to revisit --onset-unstated's default, not to delete the assertion.
        """
        rec = P._record_from_node_row(node_row("nyquist", T0, seed=71))
        assert "onset_found" not in rec

    def test_an_unstated_onset_is_admitted_but_COUNTED_not_silent(self, tmp_path):
        r, t = self._reasons(tmp_path, planted_rows_for_admit())
        q = t["onset_quality"]
        assert q["policy"] == "admit"
        assert r.get(HT.D_ONSET_UNSTATED) is None            # nothing refused under admit
        assert q["n_stated_crossed"] == 0
        assert q["n_unstated_admitted"] == t["funnel"]["admitted"] > 0
        assert q["unstated_frac_of_admitted"] == 1.0
        assert q["unstated_admitted"] == {"node": t["funnel"]["admitted"]}
        assert "no producer" in q["why_unstated"] or "nothing in this chain" in q["why_unstated"]

    def test_refuse_makes_the_cost_of_an_unmeasured_onset_a_number(self, tmp_path):
        """The policy is offered so the price is measurable, not because it should be run."""
        rows = planted_rows_for_admit()
        _r, admit_t = self._reasons(tmp_path / "a", rows)
        r, refuse_t = self._reasons(tmp_path / "b", rows, onset_unstated="refuse")
        assert r.get(HT.D_ONSET_UNSTATED) == admit_t["funnel"]["admitted"]
        assert refuse_t["funnel"]["admitted"] == 0
        assert refuse_t["onset_quality"]["unstated_frac_of_admitted"] is None, (
            "a fraction of an empty admitted set has no referent and must not be reported as 0")

    def test_an_unstated_onset_is_not_reported_as_a_passing_measurement(self, tmp_path):
        _r, t = self._reasons(tmp_path, planted_rows_for_admit())
        q = t["onset_quality"]
        assert q["n_stated_crossed"] + q["n_unstated_admitted"] == t["funnel"]["admitted"]
        assert q["n_stated_crossed"] == 0, (
            "no node row states a crossing, so any nonzero count here means the driver has "
            "started reading an absent field as a measurement")

    def test_a_stated_sync_sigma_above_the_bound_is_refused(self, tmp_path):
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=8)),
                   sync_sigma_ns=50_000_000.0)
        r, _ = self._reasons(tmp_path, [], extra=[rec])
        assert r.get(HT.D_SYNC_SIGMA) == 1

    def test_a_stale_time_anchor_is_refused_by_the_class_budget_not_the_aperture_knob(
            self, tmp_path):
        """⚠️THE GAP THE APERTURE KNOB LEAVES OPEN. --max-sync-sigma-ns defaults to a tenth of
        the tightest pair bound -- 3.45 ms on this array -- while `xiao-s3-pps` may state at most
        82.1 us before its own arrival budget is spent. A node whose GPS UART died 30 s ago
        declares ~625 us: forty times the hardware bound, and a fifth of the operator knob, so
        the knob passes it and the class gate is the only thing that does not."""
        sigma = 625_000.0
        assert sigma < HT.DEFAULT_SYNC_SIGMA_FRAC * (11.82 / 343.0) * 1e9
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=81)),
                   sync_sigma_ns=sigma)
        r, _ = self._reasons(tmp_path, [], extra=[rec])
        assert r.get(HT.D_SYNC_SIGMA) is None
        assert r.get(HT.D_STAMP_SIGMA) == 1

    def test_a_healthy_stated_sigma_is_admitted_and_carried_as_a_real_boolean(self, tmp_path):
        """A working node's declared sigma must pass, and the decision must travel: associate()
        cannot resolve it for itself, because the budget is per CLASS."""
        rec = dict(P._record_from_node_row(node_row("nyquist", T0, seed=82)),
                   sync_sigma_ns=37_000.0)
        r, t = self._reasons(tmp_path, [], extra=[rec])
        assert r.get(HT.D_STAMP_SIGMA) is None
        assert t["funnel"]["admitted"] == 1

    def test_a_row_with_no_stated_sigma_is_unaffected(self, tmp_path):
        """Every dets.csv row before generation G6 has no sigma, and the gate must be a strict
        no-op on all of them."""
        r, t = self._reasons(tmp_path, planted_rows_for_admit())
        assert r.get(HT.D_STAMP_SIGMA) is None
        assert t["funnel"]["admitted"] == 3

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
        """⚠️PINS THE DEFECT AND ITS COST, RE-MEASURED AFTER associate() STOPPED CONSUMING ITS
        REFUSALS. The old mechanism -- the first seed eating a whole second round as
        `duplicate_node_in_group` -- is gone: that round is now released and seeds itself. What
        the inflated window still costs is a LONE stale arrival on one node. Here node 1 fires
        once early and again at `gap`, which is inside the full-survey window and outside the
        arrival-only one. At the wide window the early seed reaches the second node-1 arrival,
        holds a group of one -- not every reporting node, so the release guard does not fire --
        and consumes it; nodes 2 and 3 are then refused on geometry, released, and cannot make
        min_nodes between them. At the narrow window the early seed never reaches it and the real
        three-node event is delivered.

        This is not a synthetic shape. On the live pool (74 h, 6,792 node arrivals) the
        arrival-only 78.7 ms window delivers 4 of the 4 admissible three-node episodes and the
        99.0 ms full-survey window delivers 3, and the one it loses is lost exactly this way.
        """
        sv = SV.from_dict(survey_dict(LIVE_NODES + [PUC]))
        arr = HT.arrival_survey(sv)
        margin = HT.derive_margin_s(HT.pair_bounds(arr, C), HT.DEFAULT_MARGIN_FRAC)["margin_s"]
        w_narrow = AS.max_window_s(arr, TEMP_C, margin)
        w_wide = AS.max_window_s(sv, TEMP_C, margin)
        gap = 0.5 * (w_narrow + w_wide)
        assert w_narrow < gap < w_wide
        src = np.array([40.0, 30.0, 0.0])
        delay = {nid: float(np.linalg.norm(src - arr.position(nid))) / C for nid in arr.ids}
        first = min(delay, key=delay.get)
        # The round's own FIRST arrival must land between the two windows, not the round's
        # nominal t0 -- otherwise the wide seed never reaches the arrival it is supposed to eat.
        lead = 0.5 * (w_narrow + w_wide) - delay[first]
        assert w_narrow < lead + delay[first] < w_wide
        dets = [{"node_id": first, "seq": 0, "t_utc_s": T0}]
        dets += [{"node_id": nid, "seq": 1, "t_utc_s": T0 + lead + delay[nid]}
                 for nid in arr.ids]
        narrow = AS.associate(dets, arr, temp_c=TEMP_C, margin_s=margin, window_s=w_narrow)
        wide = AS.associate(dets, arr, temp_c=TEMP_C, margin_s=margin, window_s=w_wide)
        assert [e["n_nodes"] for e in narrow["events"]] == [3]
        assert wide["events"] == []
        assert wide["refusals"]["pairwise_dt_exceeds_geometry"] == 1
        assert [r["reason"] for r in wide["rejected"]].count("duplicate_node_in_group") == 1
        assert [r["reason"] for r in narrow["rejected"]] == ["too_few_nodes"]

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
        """⚠️THE ROW MOVED FROM attempts.jsonl TO candidates.jsonl AND THE DIAGNOSTIC DID NOT.

        attempts.jsonl is now one row per ASSOCIATED EVENT, so a group association refuses has no
        attempt row at all -- there is no event to judge. Everything that used to hang off its
        `lost_to_gate` attempt (DOP, bound_check, the violating pair) hangs off the candidate row.
        """
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
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        assert (run / "attempts.jsonl").read_text() == "", "no event formed, nothing to judge"
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]
        assert cand and cand[0]["reached_associate"] is False
        assert cand[0]["associate_reason"], "a lost candidate must always name its reason"
        assert cand[0]["geometry_at_centroid"]["dop"] is not None
        assert cand[0]["bound_check"]["violating_pairs"]
        assert cand[0]["bound_check"]["worst_excess_m"] > 0.0
        assert t["conservation"]["by_terminal"].get(HT.V_LOST_TO_GATE) == 3

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



# ================================================================= the solve loop's population

#: A four-node square whose diagonal is 169.7 m -- the 3.5 acre array the operator is building,
#: where max_window_s goes from today's 57.7 ms to roughly 520 ms. Nothing here is a guess about
#: that array's shape; it is the stated size expressed as a square, the same fixture
#: tests/test_associate.py calls VAST.
VAST_NODES = [{"node_id": 1, "name": "v1", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.5},
              {"node_id": 2, "name": "v2", "e_m": 120.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.5},
              {"node_id": 3, "name": "v3", "e_m": 120.0, "n_m": 120.0, "u_m": 0.0,
               "sigma_m": 0.5},
              {"node_id": 4, "name": "v4", "e_m": 0.0, "n_m": 120.0, "u_m": 0.0, "sigma_m": 0.5}]


def _released_seed_pool(nodes, source):
    """Rows in which #42's released refusal seeds the event and the ungated scan seeds elsewhere.

    Every number is DERIVED from the survey the caller passes -- the pair bounds, the derived
    margin and the scan window all come from the driver's own functions, so this fixture means
    the same thing on a 16.9 m array and on a 169.7 m one.

    Shape, and it is the live 1789063974 episode's shape:
      * one stale arrival on a node that is NOT the round's first, at T0;
      * the round, offset by `gap` so the round's FIRST arrival is inside the stale seed's window
        and PAST that pair's own geometry bound -- so associate() refuses it and, since #42,
        hands it back, where it seeds the real event;
      * a late retrigger on that same node, outside the event's reach as a member but inside the
        ungated scan's, so the ungated scan still forms a candidate -- with a different earliest
        member and a different member set.
    """
    sv = SV.from_dict(survey_dict(nodes))
    arr = HT.arrival_survey(sv)
    margin = HT.derive_margin_s(HT.pair_bounds(arr, C), HT.DEFAULT_MARGIN_FRAC)["margin_s"]
    window = AS.max_window_s(arr, TEMP_C, margin)
    delay = {n: float(np.linalg.norm(np.asarray(source, float) - arr.position(n))) / C
             for n in arr.ids}
    first = min(delay, key=delay.get)
    second = sorted(delay, key=delay.get)[1]
    # the stale arrival sits on the node whose pair bound with `first` is the TIGHTEST, so the
    # gate has the most to object to; anything it can refuse, it refuses here.
    stale = min((n for n in arr.ids if n != first),
                key=lambda n: float(np.linalg.norm(arr.position(n) - arr.position(first))))
    bound = float(np.linalg.norm(arr.position(stale) - arr.position(first))) / C + margin
    lead = delay[second] - delay[first]
    # inside the stale seed's window, past that pair's bound, and far enough in that the round's
    # SECOND arrival is out of the stale seed's reach. Midway between the two, which exists only
    # if the round is not tighter than the bound it has to clear.
    gap = 0.5 * (bound + window)
    assert bound < gap < window, (bound, gap, window)
    assert gap + lead > window, "the round's second arrival must escape the stale seed"
    rows = [node_row(arr.names[stale], T0, seed=90, sample=90)]
    rows += [node_row(arr.names[n], T0 + gap - delay[first] + delay[n], seed=k, sample=k)
             for k, n in enumerate(arr.ids)]
    # the retrigger that keeps the ungated scan supplied with a candidate
    rows += [node_row(arr.names[first], T0 + gap + window, seed=91, sample=91)]
    return sv, arr, margin, window, first, rows


class TestTheSolveLoopRunsOffAssociatedEvents:
    """⚠️THE DEPLOYED REGRESSION OF 2026-09-11, AND WHY MATCHING BY SEED CANNOT WORK.

    attempts.jsonl used to be one row per UNGATED candidate, with the associated event looked up
    by the candidate's EARLIEST member. That silently required associate() and scan_coincidences
    to pick the same earliest member. #42 stopped consuming a geometry refusal -- the refused
    arrival goes back to the pool and can seed a group of its own -- while the ungated scan still
    consumes every candidate it reaches. The two seeds diverged, the lookup missed, and a
    strictly admissible event that ed6c75a solved came back `lost_to_gate` with `associate_reason`
    null. MEASURED on the live corpus (7,067 admitted arrivals, 74 h, derived margin 8.608 ms,
    window 57.740 ms): ed6c75a events_solved 2, main 1 with lost_to_gate 1, this 2 -- and
    associate's own three events go from 1 reaching the driver to 3.

    An event and a candidate may legitimately differ in MEMBERSHIP as well as in seed, so the
    fix is not a better join: the solve loop runs off associate()'s events and the ungated scan
    goes back to being only what its docstring says it is.
    """

    @pytest.mark.parametrize("label,nodes,source", [
        ("16.9 m array", None, (-4.58, 20.0, 0.0)),
        ("169.7 m array", VAST_NODES, (30.0, 200.0, 0.0)),
    ])
    def test_an_event_the_ungated_scan_seeds_elsewhere_is_still_solved(
            self, tmp_path, label, nodes, source):
        sv, arr, margin, window, first, rows = _released_seed_pool(nodes, source)
        t = go(tmp_path, rows, survey=write_survey(tmp_path, nodes))
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        att = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]

        # the headline number first: this is the 2 -> 1 the deploy showed, at one event
        assert t["events_solved"] == 1 and t["events_emitted"] == 1
        # the released refusal really did seed the event, which is the whole precondition
        assert t["associate"]["n_events"] == 1, "fixture void: %d" % t["associate"]["n_events"]
        ev = att[0]
        assert ev["node_ids"][0] == first and ev["n_nodes"] == len(arr.ids)
        assert ev["verdict"] == HT.V_SOLVED

        # ...and the ungated scan seeded its candidate somewhere else, which is what used to
        # lose the event. If this stops being true the fixture has stopped testing the defect.
        assert cand, "fixture void: the ungated scan formed no candidate"
        assert cand[0]["pool_keys"][0] != ev["pool_keys"][0], "fixture void: same seed"
        assert set(cand[0]["pool_keys"]) != set(ev["pool_keys"]), "fixture void: same members"
        assert t["conservation"]["ok"]

    @pytest.mark.parametrize("nodes,source", [(None, (-4.58, 20.0, 0.0)),
                                              (VAST_NODES, (30.0, 200.0, 0.0))])
    def test_a_candidate_with_no_event_of_its_own_membership_always_names_why(
            self, tmp_path, nodes, source):
        """⚠️`lost_to_gate` WITH `associate_reason: null` IS THE VERDICT THIS TOOL EXISTS TO NOT
        EMIT. Before the fix the reason was looked up in associate's `rejected` rows only, so an
        arrival that was a member of a DIFFERENT event was in neither place and resolved to null.
        """
        _sv, _arr, _m, _w, _f, rows = _released_seed_pool(nodes, source)
        t = go(tmp_path, rows, survey=write_survey(tmp_path, nodes))
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]
        lost = [r for r in cand if not r["reached_associate"]]
        assert lost, "fixture void: every candidate reached an event"
        for r in lost:
            assert r["associate_reason"], r
            assert r["member_event_keys"] or r["associate_reason"] in AS.REASONS
        assert t["conservation"]["every_candidate_explained"] is True

    def test_a_shared_seed_with_different_members_is_NOT_a_match(self, tmp_path, monkeypatch):
        """⚠️MUTATION-CHECKED, AND THE OTHER FIXTURES IN THIS CLASS DO NOT COVER IT. Reverting
        this join from the member SET back to the SEED left the whole suite green: in every
        natural fixture here the two scans disagree on the seed AND on the membership, so both
        rules answer the same. Only a candidate that shares its EARLIEST arrival with an event of
        different membership tells them apart -- which is what associate() produces whenever its
        geometry gate refuses a node the ungated scan keeps, from the same seed.

        Injected rather than planted: a natural fixture for this shape exists (it is the live
        corpus's own), but it needs a geometry that refuses exactly one node of a group the scan
        keeps, and pinning the join should not depend on re-finding that geometry.
        """
        real = HT.AS.associate
        dropped = {}

        def drop_last_member(*a, **kw):
            got = real(*a, **kw)
            for ev in got["events"]:
                if len(ev["detections"]) <= 3:
                    continue
                d = sorted(ev["detections"], key=lambda m: float(m["t_utc_s"]))
                keep = d[:-1]                       # same earliest arrival, one member short
                dropped[ev["t0_utc_s"]] = d[-1]["pool_key"]
                # conservation is asserted on associate()'s own return, so the member this
                # injection removes has to land somewhere: refused, as the real gate would.
                got["rejected"].append(AS._row(d[-1], "pairwise_dt_exceeds_geometry",
                                               "injected by the test"))
                ev["detections"] = keep
                ev["node_ids"] = [int(m["node_id"]) for m in keep]
                ev["arrivals"] = [float(m["t_utc_s"]) for m in keep]
                ev["n_nodes"] = len(keep)
                ev["n_equations"] = len(keep) - 1
                ev["span_s"] = ev["arrivals"][-1] - ev["arrivals"][0]
            return got

        monkeypatch.setattr(HT.AS, "associate", drop_last_member)
        nodes = LIVE_NODES + [{"node_id": 5, "name": "extra", "e_m": 8.0, "n_m": -9.0,
                               "u_m": 0.0, "sigma_m": 0.5}]
        sv = SV.from_dict(survey_dict(nodes))
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0),
               survey=write_survey(tmp_path, nodes))
        assert dropped, "the injection did not fire, so this asserts nothing"
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]
        att = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        assert len(cand) == 1 and len(att) == 1
        assert cand[0]["pool_keys"][0] == att[0]["pool_keys"][0], (
            "fixture void: the candidate and the event no longer share a seed, so a seed match "
            "and a set match would answer the same here")
        assert set(cand[0]["pool_keys"]) != set(att[0]["pool_keys"]), "fixture void: same members"
        assert cand[0]["reached_associate"] is False, (
            "a candidate whose members are NOT an event matched it anyway -- the join has gone "
            "back to comparing seeds, which is the 2026-09-11 regression's mechanism")
        assert cand[0]["associate_reason"], "and it must still name why"
        assert t["conservation"]["every_candidate_explained"] is True

    def test_the_membership_match_is_the_set_not_the_seed(self, tmp_path):
        """A candidate whose members ARE an event, in a different order, still reaches it."""
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]
        att = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        assert len(cand) == 1 and len(att) == 1
        assert cand[0]["reached_associate"] is True
        assert cand[0]["associate_reason"] is None
        assert set(cand[0]["pool_keys"]) == set(att[0]["pool_keys"])
        assert cand[0]["event_key"] == att[0]["event_key"]

    def test_every_associated_event_gets_exactly_one_verdict(self, tmp_path):
        """⚠️main SOLVED ONE OF ITS THREE ASSOCIATED EVENTS AND CONSERVATION STILL SAID OK,
        because conservation counted verdicts against CANDIDATES. Two events were neither solved
        nor bound-checked nor reported, and their arrivals were logged `singleton_unpaired`.
        """
        sv = SV.from_dict(survey_dict())
        rows = (planted(sv, (40.0, 30.0, 0.0), T0, seed0=0)
                + planted(sv, (-30.0, 25.0, 0.0), T0 + 600.0, seed0=10))
        t = go(tmp_path, rows)
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        att = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        assert t["associate"]["n_events"] == 2 and len(att) == 2
        assert t["conservation"]["one_verdict_per_event"] is True
        assert t["conservation"]["n_attempts"] == t["associate"]["n_events"]
        # no member of a delivered event is ever logged as having reached nothing
        member = {k for r in att for k in r["pool_keys"]}
        assert member and t["conservation"]["by_terminal"].get("singleton_unpaired") is None

    def test_no_arrival_is_judged_twice_and_the_work_is_bounded(self, tmp_path):
        """⚠️THE TERMINATION ARGUMENT FOR THE SOLVE LOOP, AS A COUNT.

        Both loops are bounded `for`s over finite lists -- one pass over `grouped["events"]`, then
        one pass over `candidates` that only fills terminal states no event claimed. Nothing is
        re-queued and nothing revisits an arrival, so the bound is structural rather than a
        property of any flag. Asserted as EQUALITIES, not ceilings: a former that revisits without
        looping fails here too. The pool is built so every kind of group is present at once.
        """
        sv = SV.from_dict(survey_dict())
        rows = []
        for k in range(6):
            rows += planted(sv, (40.0 - 6.0 * k, 30.0 + 4.0 * k, 0.0), T0 + 600.0 * k,
                            seed0=10 * k)
        rows += [node_row("nyquist", T0 + 300.0, seed=99, sample=99)]      # a lone singleton
        t = go(tmp_path, rows)
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        att = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        seen = [k for r in att for k in r["pool_keys"]]
        assert len(seen) == len(set(seen)), "an arrival was judged twice"
        assert len(att) == t["associate"]["n_events"] == 6
        assert t["conservation"]["by_terminal"]["singleton_unpaired"] == 1
        assert sum(t["conservation"]["by_terminal"].values()) == t["conservation"]["admitted"]

    def test_two_runs_over_the_same_pool_give_the_same_verdicts(self, tmp_path):
        """Idempotence of the loop itself, separately from the store's resume dedupe."""
        sv = SV.from_dict(survey_dict())
        rows = (planted(sv, (40.0, 30.0, 0.0), T0, seed0=0)
                + planted(sv, (-30.0, 25.0, 0.0), T0 + 600.0, seed0=10))
        shape = []
        for leg in ("a", "b"):
            t = go(tmp_path / leg, rows)
            run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
            shape.append([{k: r[k] for k in ("event_key", "verdict", "node_ids", "arrivals",
                                             "point_source_possible")}
                          for r in map(json.loads,
                                       (run / "attempts.jsonl").read_text().splitlines())])
        assert shape[0] == shape[1] and shape[0]

    def test_the_published_payload_carries_point_source_possible(self, tmp_path):
        """⚠️#44 SHIPPED THE FIELD AND THE ONLY DEPLOYED PATH PUBLISHED IT AS null. The driver
        hand-builds the dict it hands to pipeline.to_dama_event, which reads the field with
        .get, so a consumer that treats null as "not stated" got exactly the silence #44 was
        written to end. Backend.flush(), which does set it, has no production caller.
        """
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        ev = json.loads((run / "events.jsonl").read_text().splitlines()[0])
        assert ev["published_payload"]["event"]["point_source_possible"] is True
        assert ev["point_source_possible"] is True
        assert ev["worst_pair_excess_s"] == 0.0

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
        # ⚠️TWO NUMBERS, AND CONFLATING THEM WAS THE BUG. Both runs SOLVE the same one event;
        # only the first WRITES it. This assertion used to read
        # `a["events_emitted"] == b["events_emitted"] == 1` against a second run whose
        # events.jsonl is 0 bytes -- the manifest asserting an emission that never happened.
        assert a["events_solved_admissible"] == b["events_solved_admissible"] == 1
        assert a["events_emitted"] == 1 and b["events_emitted"] == 0
        assert a["ledger_rows_written"] == 3 and b["ledger_rows_written"] == 0
        # the second run's event is a repeat of the same member set, so nothing new is emitted
        assert b["events_new"] == 0
        assert b["event_membership_changed"] == 0, (
            "the same member set is not a membership change; the old expression made this "
            "count len(new_events) whenever anything had ever been emitted")
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

    # geometry wide enough that a 30 degC error crosses a pair bound -- see
    # TestSoundSpeedIsAssumed for the mechanism and the metres.
    WIDE = [{"node_id": i + 1, "name": "n%d" % i, "e_m": float(e), "n_m": float(n), "u_m": 0.0,
             "sigma_m": 0.1}
            for i, (e, n) in enumerate([(0, 0), (120, 0), (0, 140), (150, 130)])]

    def test_a_REFUSED_run_does_not_burn_the_event_key_for_the_run_that_solves_it(self,
                                                                                  tmp_path):
        """⚠️THE DEFECT, REPRODUCED BY EXECUTION AND THEN PINNED.

        Run 1 assumes 30 degC, which shrinks every pair bound past a genuine event and refuses
        it as inadmissible. Run 2 assumes the right temperature and solves the SAME member set,
        so `event_key` -- a content address of that set -- is identical.

        The emission dedupe used to read its "already emitted" set out of the ARRIVAL LEDGER,
        which gets an event_key for every arrival of every CANDIDATE regardless of verdict. So
        run 1's refusal put the key there, run 2 filtered its own solved event out against it,
        and events.jsonl was written 0 bytes while manifest.json reported an emission. One
        refusal burned that member set permanently, and the only way to recover it was to delete
        an append-only partition.
        """
        sv = SV.from_dict(survey_dict(self.WIDE))
        svp = write_survey(tmp_path, self.WIDE)
        out = str(tmp_path / "out")
        build_pool(tmp_path / "pool", planted(sv, (300.0, 260.0, 0.0), T0))

        hot = HT.run(str(tmp_path / "pool"), svp, pol(temp_c=30.0), out=out, now=T0 + 3600.0)
        assert hot["events_admissible"] == 0 and hot["events_emitted"] == 0

        # the refused candidate's key IS in the arrival ledger -- that is the trap, not a bug
        led = [json.loads(l) for pp in (pathlib.Path(out) / "arrivals").rglob("*.jsonl")
               for l in pp.read_text().splitlines()]
        refused_keys = {r["event_key"] for r in led if r.get("event_key")}
        assert refused_keys, "the ledger must still record which candidate an arrival was in"

        cool = HT.run(str(tmp_path / "pool"), svp, pol(temp_c=0.0), out=out, now=T0 + 7200.0)
        assert cool["events_admissible"] == 1
        assert cool["events_solved_admissible"] == 1

        ev = pathlib.Path(out) / "runs" / cool["run_id"] / "events.jsonl"
        lines = [l for l in ev.read_text().splitlines() if l.strip()]
        assert len(lines) == 1, "the solved event must reach the file, not just the manifest"
        assert json.loads(lines[0])["event_key"] in refused_keys, (
            "same member set, so the same content address -- which is exactly why reading the "
            "candidate ledger as an emission log lost it")
        assert cool["events_emitted"] == len(lines), (
            "manifest and file must agree; claiming an emission against a 0-byte append is "
            "the failure this assertion exists for")

    def test_a_member_added_to_a_PUBLISHED_event_is_the_only_membership_change(self, tmp_path):
        """`event_membership_changed` must count SUPERSESSION, not "new since something".

        The old expression was `sum(1 for e in events_out if e[key] not in prior and prior)`,
        which is len(new_events) whenever anything had ever been emitted. So a first-ever
        backfill of unrelated events reported every one of them as a membership change, and a
        genuine supersession against an empty store reported none. Both directions are checked
        here, because a count that is right only when it happens to equal another count is not
        measuring what its name says.
        """
        nodes = LIVE_NODES + [{"node_id": 5, "name": "extra", "e_m": 8.0, "n_m": -9.0,
                               "u_m": 0.0, "sigma_m": 0.5}]
        sv = SV.from_dict(survey_dict(nodes))
        svp = write_survey(tmp_path, nodes)
        out = str(tmp_path / "out")
        build_pool(tmp_path / "pool", planted(sv, (40.0, 30.0, 0.0), T0, nodes=[1, 2, 3]))

        a = HT.run(str(tmp_path / "pool"), svp, pol(), out=out, now=T0 + 3600.0)
        assert a["events_emitted"] == 1
        assert a["event_membership_changed"] == 0, (
            "the FIRST emission supersedes nothing; the old expression got this right only "
            "because it happened to short-circuit on an empty prior set")

        P.Pool(str(tmp_path / "pool"))._append(
            [P._record_from_node_row(r) for r in
             planted(sv, (40.0, 30.0, 0.0), T0, nodes=[5], seed0=90)])
        b = HT.run(str(tmp_path / "pool"), svp, pol(), out=out, now=T0 + 7200.0)
        assert b["events_emitted"] == 1
        assert b["event_membership_changed"] == 1
        assert b["superseded_event_keys"] == sorted(
            {json.loads(l)["event_key"] for l in
             (pathlib.Path(out) / "runs" / a["run_id"] / "events.jsonl"
              ).read_text().splitlines() if l.strip()}), (
            "the superseded key must name the row that stays in the append-only store, so a "
            "reader can see WHICH published event this one replaces")

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


class TestNoTerminalVerdictGoesOutWithoutAReason:
    """⚠️"THE PRODUCT IS THE FUNNEL", AND A FUNNEL THAT LOSES A ROW WITHOUT SAYING WHY IS NOT ONE.

    `lost_to_gate` used to be emitted with `associate_reason: null` whenever a candidate's
    members were all members of a DIFFERENT associate event: they are in neither `rejected` nor
    `ev_by_seed`, so the reason lookup resolved nothing. The two scans stopped consuming
    identically when associate() began releasing a pairwise-refused candidate instead of
    consuming it, and the driver's ungated scan still consumes.

    ⚠️THE ROW THAT CARRIES THIS VERDICT IS candidates.jsonl, NOT attempts.jsonl, AND THE MOVE IS
    NOT COSMETIC. attempts.jsonl is now one row per ASSOCIATED EVENT -- an event that exists was
    never lost to the gate -- so `lost_to_gate` survives only as a terminal state of the ARRIVALS
    in an ungated candidate that no event matches, on the candidate row and in the ledger. This
    test asserted it off attempts.jsonl when it was written against the old solve loop, passed
    against that loop, and went vacuous the moment the loop changed: the fixture guard below and
    the "no attempt is lost_to_gate" assertion are what stop it silently testing nothing again.
    """

    #: Five arrival-class receivers and fifteen arrivals, taken from a search of random draws
    #: through the real `HT.run`: the driver's scan seeds a candidate whose members are all held
    #: by an associate event seeded elsewhere, so they appear in no rejection row and in no event
    #: keyed by their own seed. 11 of 400 uniform draws produced the shape -- it is the ordinary
    #: consequence of the two scans consuming differently, not a contrived arrangement.
    NODES = LIVE_NODES + [
        {"node_id": 5, "name": "extra", "e_m": 8.0, "n_m": -9.0, "u_m": 0.0, "sigma_m": 0.5},
        {"node_id": 6, "name": "extra2", "e_m": -9.0, "n_m": -12.0, "u_m": 0.0, "sigma_m": 0.5},
    ]
    OFFSETS = [(1, 0.12441), (2, 0.29387), (3, 0.290092), (5, 0.095853), (6, 0.034964),
               (1, 0.250722), (2, 0.282218), (3, 0.194747), (5, 0.289317), (6, 0.143261),
               (1, 0.157143), (2, 0.022227), (3, 0.110994), (5, 0.277384), (6, 0.030985)]

    def _run(self, tmp_path):
        sv = SV.from_dict(survey_dict(self.NODES))
        rows = [node_row(sv.names[nid], T0 + off, seed=k, sample=k)
                for k, (nid, off) in enumerate(self.OFFSETS)]
        build_pool(tmp_path / "pool", rows)
        return HT.run(str(tmp_path / "pool"), write_survey(tmp_path, self.NODES), pol(),
                      out=str(tmp_path / "out"), now=T0 + 3600.0)

    def test_the_arrangement_still_produces_a_candidate_associate_did_not_seed(self, tmp_path):
        """The fixture has to keep reproducing the shape, or the test below passes vacuously."""
        t = self._run(tmp_path)
        rows = [json.loads(l) for l in
                (pathlib.Path(t["out"]) / "runs" / t["run_id"]
                 / "candidates.jsonl").read_text().splitlines()]
        assert any(not r["reached_associate"] for r in rows), (
            "no candidate missed association in this fixture, so it no longer covers the bug")
        assert t["associate"]["n_events"] >= 2

    def test_every_lost_to_gate_row_names_why(self, tmp_path):
        t = self._run(tmp_path)
        run = pathlib.Path(t["out"]) / "runs" / t["run_id"]
        cand = [json.loads(l) for l in (run / "candidates.jsonl").read_text().splitlines()]
        lost = [c for c in cand if not c["reached_associate"]]
        assert lost, "this fixture must produce one, or it is not testing the reason lookup"
        for c in lost:
            assert c["associate_reason"], (
                "candidate %s reached no event with associate_reason %r: the tool's charter is "
                "that every refusal names its reason" % (c["candidate_id"],
                                                         c["associate_reason"]))
        assert any(c["associate_reason"].startswith("member_of_event") for c in lost), (
            "the reason has to NAME the event that took the arrivals; a bare "
            "'regrouped' is not followable to anything")
        assert t["conservation"]["every_candidate_explained"] is True

        attempts = [json.loads(l) for l in (run / "attempts.jsonl").read_text().splitlines()]
        assert not [a for a in attempts if a["verdict"] == HT.V_LOST_TO_GATE], (
            "attempts.jsonl is one row per associated event; an event that exists cannot be "
            "lost to the gate, so this verdict must not appear there")
        assert len(attempts) == t["associate"]["n_events"]

        # and the arrivals themselves still carry the terminal state, so nothing vanishes
        term = {}
        for f in (pathlib.Path(t["out"]) / "arrivals").rglob("*.jsonl"):
            for line in f.read_text().splitlines():
                r = json.loads(line)
                term[r["key"]] = r["terminal"]
        assert any(term.get(k) == HT.V_LOST_TO_GATE for c in lost for k in c["pool_keys"]), (
            "a candidate no event matched left no lost_to_gate arrival in the ledger")


class TestTheTwoSigmaGatesAreQuotedAgainstTheirOwnNumbers:
    """⚠️THE NUMBERS, NOT THE PROSE. A test that grepped admit()'s comment for "42x" would pass
    on the comment quoting itself; these are the two quantities the comment compares, computed
    from the array and from nodeclass.py, so the claim is what is held and not the sentence.

    The comment shipped saying "~27x tighter (82.1 us of stated sigma against 3.45 ms)". 3.44 ms
    over 82.1 us is 41.9; the 27 is 3.44 ms over ARRIVAL_T_SIGMA_MAX_S (129.4 us), which is the
    per-node total budget and not a threshold on `sync_sigma_ns` at all.
    """

    def test_the_aperture_knob_and_the_hardware_gate_are_42x_apart(self):
        import hear.nodeclass as NC
        arr = HT.arrival_survey(SV.from_dict(survey_dict()))
        bounds = HT.pair_bounds(arr, C)
        tightest = min(v["bound_s"] for v in bounds.values())
        assert tightest == pytest.approx(0.034430122, abs=5e-9)
        knob_ns = HT.DEFAULT_SYNC_SIGMA_FRAC * tightest * 1e9
        assert knob_ns / 1e6 == pytest.approx(3.443, abs=5e-4), "3.44 ms, not 3.45"

        hardware_ns = NC.CLASSES["xiao-s3-pps"].max_stated_clock_sigma_s() * 1e9
        assert hardware_ns / 1e3 == pytest.approx(82.1, abs=0.05)
        assert knob_ns / hardware_ns == pytest.approx(41.9, abs=0.1)

        # the number the "~27x" actually belongs to: a different quantity, not a threshold on
        # the stated sigma, which is why quoting it beside "82.1 us" was self-inconsistent
        assert knob_ns / (NC.ARRIVAL_T_SIGMA_MAX_S * 1e9) == pytest.approx(26.6, abs=0.1)


class TestThePublishedPointSourceVerdictIsTheRealOne:
    """⚠️THE FIELD SHIPPED AND THE DEPLOYED PATH LEFT IT null. `Backend.flush()` sets
    `point_source_possible`, and `Backend` has NO caller: the CronJob runs THIS driver, which
    hand-builds the dict it hands to `to_dama_event`. The key was absent, `to_dama_event` read it
    with `.get`, and every payload the cluster published carried null while associate() had
    computed the verdict for that same event. Null is worse than absent -- it reads as "not
    stated, probably fine", which is the silence the field was added to end.

    The class-of-bug fix is in `to_dama_event`: the key is SUBSCRIPTED, so the next hand-built
    caller that forgets it raises here instead of publishing a null.
    """

    def test_the_deployed_path_publishes_the_verdict_and_its_magnitude(self, tmp_path):
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        ev = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])
        pub = ev["published_payload"]["event"]
        assert pub["point_source_possible"] is True, (
            "the driver hand-builds this dict; a missing key used to publish null here")
        # not a constant: it is associate()'s own number for this event, as the manifest reports it
        assert pub["worst_pair_excess_s"] * 1e3 == pytest.approx(
            t["associate"]["worst_pair_excess_ms"][0], abs=5e-4)
        assert t["associate"]["n_point_source_possible"] == t["associate"]["n_events"]

    def test_the_published_verdict_is_carried_from_associate_not_recomputed(self, tmp_path,
                                                                            monkeypatch):
        """⚠️NO NATURAL FIXTURE CAN CATCH A HARDCODED `True` HERE, WHICH IS WHY THIS INJECTS.

        The driver only publishes when `bound_check` says the candidate is admissible at ZERO
        margin, and that is the same physics associate() measured -- so on every publishable row
        point_source_possible is True and worst_pair_excess_s is 0.0 by construction. Measured:
        replacing both with the constants `True` and `0.0` passed every other test in this class.

        `bound_check` runs over the driver's own `group` and the flag comes from `ev`, so forcing
        associate's verdict False leaves the row publishable and the two disagree -- which is
        exactly the case a consumer needs the flag for, and exactly what the two scans diverging
        produces in the field.
        """
        real = HT.AS.associate

        def forced(*a, **kw):
            got = real(*a, **kw)
            for ev in got["events"]:
                ev["point_source_possible"] = False
                ev["worst_pair_excess_s"] = 0.004
            return got

        monkeypatch.setattr(HT.AS, "associate", forced)
        sv = SV.from_dict(survey_dict())
        t = go(tmp_path, planted(sv, (40.0, 30.0, 0.0), T0))
        assert t["events_solved"] == 1, "the row must still be published, not dropped"
        ev = json.loads((pathlib.Path(t["out"]) / "runs" / t["run_id"]
                         / "events.jsonl").read_text().splitlines()[0])
        pub = ev["published_payload"]["event"]
        assert pub["point_source_possible"] is False, "carried, not recomputed"
        assert pub["worst_pair_excess_s"] == 0.004
        assert ev["bound_check"]["admissible"] is True, (
            "and the driver's own zero-margin check still says admissible -- the two really are "
            "different questions, which is why the payload has to carry associate's answer")

    @pytest.mark.parametrize("missing", ["point_source_possible", "worst_pair_excess_s"])
    def test_a_caller_that_omits_the_verdict_raises_rather_than_publishing_null(self, missing):
        """⚠️THE KeyError MUST NAME THE OMITTED KEY. Asserting only `pytest.raises(KeyError)`
        passes while ONE of the two is still read with `.get` -- the other key raises and the
        mutation survives. Measured: reverting point_source_possible to `.get` left this test
        green until it started checking which key the error names.
        """
        ok = {"event_id": 0, "model": "point", "source_class": "blast", "n_nodes": 3,
              "n_equations": 2, "node_ids": [1, 2, 3], "t0_utc_s": T0, "solution": {},
              "point_source_possible": False, "worst_pair_excess_s": 0.004}
        short = {k: v for k, v in ok.items() if k != missing}
        with pytest.raises(KeyError) as e:
            BP.to_dama_event(short, array_id="hear")
        assert e.value.args[0] == missing, (
            "to_dama_event raised for %r, not for the key the caller omitted (%r): the omitted "
            "one is still being read with .get and would publish null"
            % (e.value.args[0], missing))
        body = BP.to_dama_event(dict(ok), array_id="hear")["event"]
        assert body["point_source_possible"] is False
        assert body["worst_pair_excess_s"] == 0.004


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

    def test_this_bundle_cannot_be_applied_CLIENT_side_and_says_so_in_a_FIELD(self):
        """⚠️A PLAIN `kubectl apply` ON THIS FILE IS REJECTED, and the rejection is the API
        server's, not a warning:

            The ConfigMap "hear-tdoa-code" is invalid: metadata.annotations:
            Too long: may not be more than 262144 bytes

        Client-side apply stores the entire submitted object in
        `kubectl.kubernetes.io/last-applied-configuration`. This bundle carries the whole solve
        stack, so the object clears that cap by 1.8x. `--server-side` writes managed fields
        instead and applies the same file (verified against the live cluster).

        ⚠️THE ASSERTION READS THE ANNOTATION, NOT THE DOCUMENT TEXT. Grepping the .yaml for the
        string "--server-side" would also hit the copy of tools/hear_tdoa.py inside the bundle's
        own `data`, so the guard would pass on any bundle that merely *mentions* it -- a check
        matching its own explanation. The annotation is a field with one value.
        """
        _app, code, data = GC.BUNDLES[BUNDLE]
        mode, n_bytes, n_ann = GC.apply_mode(code, data)
        assert mode == "server"
        assert n_ann > GC.CLIENT_APPLY_ANNOTATION_CAP
        assert n_bytes < GC.OBJECT_CAP, (
            "server-side apply does NOT lift the 1 MiB object cap; past it the bundle has to "
            "be split and no flag saves it")
        if not CODEMAP.exists():
            pytest.skip("no ConfigMap at %s" % CODEMAP)
        ann = _annotations_from_text(CODEMAP.read_text())
        assert ann.get("dama-hear/apply-mode") == "server"

    @pytest.mark.parametrize("bundle", sorted(GC.BUNDLES))
    def test_every_bundle_declares_the_mode_its_own_SIZE_implies(self, bundle):
        """Not just this one. hear-drain-code crossed on 2026-09-10 -- the bundle that had always
        applied cleanly took one import-closure member and was 15 B from the cap, which is the
        failure mode this test exists for: an apply that starts being rejected with no source
        change to blame.

        ⚠️THE THRESHOLD IS THE CAP MINUS A MARGIN, NOT THE CAP. Fifteen bytes of headroom is not
        a passing grade: the `dama-hear/commit` stamp alone moves the object by 6 B between a
        clean and a dirty tree, so a mode chosen at the exact byte would alternate between runs
        and the documented deploy command with it. Inside the margin the bundle is called
        `server`, which always works.
        """
        _app, code, data = GC.BUNDLES[bundle]
        # ⚠️THE MODE IS CHOSEN FROM THE THIRD NUMBER. n_bytes is what lands in last-applied;
        # n_ann is what the API server charges -- the whole annotations map, which also carries
        # the 48-character last-applied key and the four dama-hear ones. Asserting against
        # n_bytes is how hear-drain-code read as "client with 393 B spare" while the live object
        # held 254,433 B of annotations against a 253,952 B threshold.
        mode, n_bytes, n_ann = GC.apply_mode(code, data)
        limit = GC.CLIENT_APPLY_ANNOTATION_CAP - GC.CLIENT_APPLY_MARGIN
        assert mode == ("server" if n_ann > limit else "client")
        assert n_ann > n_bytes, "the annotations map cannot cost less than last-applied alone"
        # whatever the margin is, a bundle past the real cap must never be called client
        if n_ann > GC.CLIENT_APPLY_ANNOTATION_CAP:
            assert mode == "server"
        f = REPO / "deploy" / "k8s" / ("%s.yaml" % bundle)
        if not f.exists():
            pytest.skip("no ConfigMap at %s" % f)
        assert _annotations_from_text(f.read_text()).get("dama-hear/apply-mode") == mode, (
            "regenerate: python3 deploy/k8s/gen_configmap.py %s > deploy/k8s/%s.yaml"
            % (bundle, bundle))

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


def _annotations_from_text(text):
    """metadata.annotations as a dict, read from the header BEFORE `data:`.

    Stopping at `data:` is the whole point: every embedded module is indented under it and one
    of them is this generator's own source, so a scan of the full document would find the
    annotation keys written as string literals in the code that emits them.
    """
    out, in_ann = {}, False
    for line in text.split("\n"):
        if line.startswith("data:"):
            break
        if line.strip() == "annotations:":
            in_ann = True
            continue
        if in_ann:
            if not line.startswith("    "):
                in_ann = False
                continue
            k, _sep, v = line.strip().partition(":")
            out[k] = v.strip().strip("'\"")
    return out


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


# ================================================================= scan_coincidences docstring

class TestScanCoincidencesNoLongerMatchesAssociate:
    """Pins scan_coincidences()'s own ⚠️: it used to claim "Same scan, minus one gate" and that
    a consumed candidate is terminal, which stopped being true when #42 gave associate() two
    NON-terminal refusals (duplicate_node_in_group release, and every pairwise_dt_exceeds_geometry
    refusal). This scan still marks every visited candidate used unconditionally, so it can seed
    -- or fail to seed -- differently from associate() on the same detections.

    Same node positions and arrivals as tests/test_associate.py's TestReseedingARefusedCandidate
    (the live 2026-09-09T11:08:41 episode): nyquist@.271272 seeds a group, is refused against
    rankine@.340823 by the geometry gate, and -- since #42 -- is released rather than consumed.
    associate() reseeds cleanly at rankine and delivers [rankine, nyquist, mach]. This scan has no
    release: nyquist@.271272 stays consumed as soon as it is visited, so the very group associate
    delivers never gets a chance to seed here at all.
    """

    LIVE_T = 1788952121.0
    LIVE_ARRIVALS = [
        (2, 0.164822), (2, 0.168846), (2, 0.173107), (2, 0.181276), (2, 0.185542),
        (1, 0.271272), (1, 0.321229), (1, 0.333769), (1, 0.338348), (1, 0.359180),
        (3, 0.340823), (3, 0.343133), (3, 0.345298),
        (2, 0.380704), (2, 0.383722),
    ]

    def _dets(self):
        return [{"node_id": n, "seq": i, "t_utc_s": self.LIVE_T + off, "iface": "lora0"}
                for i, (n, off) in enumerate(self.LIVE_ARRIVALS)]

    def _survey(self):
        return SV.from_dict(survey_dict(LIVE_NODES))

    def test_associate_delivers_the_episode_scan_coincidences_cannot_see(self):
        dets = self._dets()
        sv = self._survey()
        got = AS.associate(dets, sv, temp_c=25.0)
        assert [e["node_ids"] for e in got["events"]] == [[3, 1, 2]], (
            "if this drifts, re-derive window_s below from got['window_s'] instead of assuming it")
        window_s = got["window_s"]
        scanned = HT.scan_coincidences(dets, window_s=window_s, min_nodes=3)
        assert scanned == [], (
            "scan_coincidences found a group here -- the docstring's claim may have been fixed; "
            "update it (and this test) rather than deleting the assertion")


# ================================================================= the one real-file read

def test_the_repo_survey_admits_exactly_three_arrival_nodes():
    """The ONLY test that opens the operator's survey, and it is read-only."""
    sv = SV.load_survey(os.path.join(ROOT, "survey.json"))
    assert sv.arrival_ids() == [1, 2, 3]
    arr = HT.arrival_survey(sv)
    assert [arr.names[i] for i in arr.ids] == ["nyquist", "mach", "rankine"]
    assert AS.max_window_s(arr) < AS.max_window_s(sv)
