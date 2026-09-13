"""A node with no survey entry is positioned from its own GPS mean, at a wider sigma, and says so.

Every coordinate here is built from the fictional test origin tests/test_hear_tdoa.py already uses.
"""
import json
import math
import os
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import geodesy as GEO                                  # noqa: E402
from hear import nodeclass as NC                                 # noqa: E402
from hear.backend import survey as SV                            # noqa: E402
from tools import hear_tdoa as HT                                # noqa: E402
from tests.test_hear_tdoa import (C, T0, build_pool, node_row,   # noqa: E402
                                  planted, pol, survey_dict, write_survey)

ORIGIN = survey_dict()["origin"]
NOW = T0 + 3600.0


def gps_entry(e, n, u=0.0, **over):
    lat, lon, h = GEO.enu_to_geodetic(e, n, u, ORIGIN["lat_deg"], ORIGIN["lon_deg"],
                                      ORIGIN["h_ell_m"])
    p = {"lat_deg": lat, "lon_deg": lon, "h_ell_m": h, "hacc_m": 1.2, "vacc_m": 2.0,
         "fixes": 5000, "fix": 3, "at": NOW - 60.0, "class": "esp32s3-i2s-gps"}
    p.update(over)
    return p


def base_survey(**origin_over):
    d = survey_dict()
    d["origin"] = dict(d["origin"], **origin_over)
    return SV.from_dict(d)


class TestSurveyFallback:
    def test_an_unsurveyed_node_is_placed_at_its_gps_mean(self):
        sv, rep = SV.augment_from_node_gps(base_survey(), {"gold": gps_entry(25.0, -20.0, 1.0)},
                                           NOW)
        gid = SV.gps_node_id("gold")
        assert gid in sv and sv.names[gid] == "gold"
        assert np.allclose(sv.position(gid), (25.0, -20.0, 1.0), atol=1e-6)
        assert sv.position_sources[gid] == SV.POSITION_SOURCE_GPS
        assert sv.classes[gid] == "esp32s3-i2s-gps"
        assert {sv.position_sources[i] for i in sv.ids if i != gid} == {"survey"}
        assert [r["name"] for r in rep if r["used"]] == ["gold"]

    def test_sigma_is_never_tighter_than_the_floor_and_hacc_only_widens_it(self):
        sv, _ = SV.augment_from_node_gps(
            base_survey(), {"a": gps_entry(25, -20, hacc_m=0.3), "b": gps_entry(-30, 25, hacc_m=6.0)},
            NOW)
        assert sv.sigma_m[SV.gps_node_id("a")] == SV.GPS_SIGMA_FLOOR_M
        assert sv.sigma_m[SV.gps_node_id("b")] == 6.0

    def test_a_surveyed_name_always_wins(self):
        base = base_survey()
        sv, rep = SV.augment_from_node_gps(base, {"mach": gps_entry(500.0, 500.0)}, NOW)
        assert sv is base and rep == []

    @pytest.mark.parametrize("over,word", [
        ({"fixes": 0}, "averaged no position fixes"),
        ({"fix": 1}, None),
        ({"fixes": 10}, "fix(es) averaged"),
        ({"hacc_m": 12.0}, "reject bound"),
        ({"at": NOW - 3 * 86400.0}, "old"),
        ({"at": None}, "timestamp"),
        ({"h_ell_m": None, "hmsl_m": 250.0}, "hmsl_m"),
        ({"lat_deg": None}, "lat_deg"),
    ])
    def test_an_unusable_gps_mean_is_reported_and_left_out(self, over, word):
        base = base_survey()
        sv, rep = SV.augment_from_node_gps(base, {"gold": gps_entry(25, -20, **over)}, NOW)
        if word is None:                     # a PMTK fix quality of 1 is a fix, not a refusal
            assert rep[0]["used"] is True, rep
            return
        assert sv is base
        assert rep[0]["used"] is False and word in rep[0]["why"], rep

    def test_a_fictional_origin_places_nothing(self):
        base = base_survey(fictional=True)
        sv, rep = SV.augment_from_node_gps(base, {"gold": gps_entry(25, -20)}, NOW)
        assert sv is base and "fictional" in rep[0]["why"]

    def test_an_addition_that_breaks_the_survey_returns_the_original(self):
        base = base_survey()
        sv, rep = SV.augment_from_node_gps(base, {"gold": gps_entry(0.01, 0.0, 0.0)}, NOW)
        assert sv is base and rep[0]["used"] is False and "invalid" in rep[0]["why"]

    def test_the_report_carries_no_coordinate(self):
        p = gps_entry(25, -20)
        _sv, rep = SV.augment_from_node_gps(base_survey(), {"gold": p}, NOW)
        text = json.dumps(rep)
        assert "lat" not in text and "lon" not in text
        assert ("%.5f" % p["lat_deg"]) not in text

    def test_ids_are_stable_per_name_and_above_the_surveyed_range(self):
        assert SV.gps_node_id("gold") == SV.gps_node_id("gold")
        assert SV.gps_node_id("gold") != SV.gps_node_id("ageev")
        for name in ("gold", "ageev", "kasami"):
            assert SV.GPS_NODE_ID_BASE <= SV.gps_node_id(name) <= 0xFFFF


def test_the_card_less_board_class_is_registered_and_refused_until_measured():
    c = NC.get("esp32s3-i2s-gps")
    assert c.path_bias_s is None and not c.contributes_arrival()
    with pytest.raises(NC.CapabilityError):
        NC.require_arrival("esp32s3-i2s-gps", node_id="gold")


def positions_file(tmp_path, entries):
    p = tmp_path / "node_positions.json"
    p.write_text(json.dumps({"schema": "hear.node_positions.v1", "nodes": entries}))
    return str(p)


class TestTdoaFallback:
    def _run(self, tmp_path, rows, entries=None, gps_path=None, **over):
        build_pool(tmp_path / "pool", rows)
        path = gps_path if gps_path is not None else positions_file(tmp_path, entries or {})
        return HT.run(str(tmp_path / "pool"), write_survey(tmp_path),
                      pol(gps_positions=path, **over), out=str(tmp_path / "out"), now=NOW)

    def test_a_card_less_board_is_positioned_then_refused_on_its_class(self, tmp_path):
        t = self._run(tmp_path, [node_row("gold", T0, seed=7)], {"gold": gps_entry(25, -20)})
        r = t["funnel"]["by_reason"]
        assert r.get(HT.D_NOT_ARRIVAL) == 1 and not r.get(HT.D_UNSURVEYED)
        sb = t["survey_block"]
        assert sb["position_sources"]["gold"] == SV.POSITION_SOURCE_GPS
        assert "gold" in [x["name"] for x in sb["refused_as_arrivals"]]
        assert "GPS-POSITIONED gold" in HT.format_report(t)
        assert "gps_refused" not in t["policy"]

    def test_with_no_positions_file_the_node_stays_unsurveyed(self, tmp_path):
        t = self._run(tmp_path, [node_row("gold", T0, seed=7)],
                      gps_path=str(tmp_path / "missing.json"))
        assert t["funnel"]["by_reason"].get(HT.D_UNSURVEYED) == 1
        assert "no positions file" in json.dumps(t["survey_block"]["gps_fallback"])

    def test_a_refused_gps_mean_is_named_in_the_unsurveyed_detail(self, tmp_path):
        t = self._run(tmp_path, [node_row("gold", T0, seed=7)],
                      {"gold": gps_entry(25, -20, fixes=0)})
        assert t["funnel"]["by_reason"].get(HT.D_UNSURVEYED) == 1
        led = "\n".join(p.read_text()
                        for p in (pathlib.Path(t["out"]) / "arrivals").rglob("*.jsonl"))
        assert "node GPS fallback not used" in led and "averaged no position fixes" in led

    def test_a_gps_positioned_pps_node_joins_the_solve_carrying_its_position_sigma(self, tmp_path):
        entries = {"newnode": gps_entry(25.0, -20.0, 0.0, hacc_m=3.0, **{"class": "xiao-s3-pps"})}
        sv, _ = SV.augment_from_node_gps(SV.from_dict(survey_dict()), entries, NOW)
        rows = planted(sv, (40.0, 30.0, 0.0), T0)
        for r in rows:
            r["sync_sigma_ns"] = "50"
        t = self._run(tmp_path, rows, entries)
        assert "newnode" in t["survey_block"]["arrival_names"]
        att = [json.loads(line) for line in
               (pathlib.Path(t["out"]) / "runs" / t["run_id"] / "attempts.jsonl")
               .read_text().splitlines()]
        gid = SV.gps_node_id("newnode")
        row = next(a for a in att if gid in a["node_ids"])
        k = row["node_ids"].index(gid)
        assert row["position_sources"][k] == SV.POSITION_SOURCE_GPS
        assert [s for j, s in enumerate(row["position_sources"]) if j != k] == ["survey"] * 3
        sig = row["arrival_sigma_s"]
        other = sig[(k + 1) % len(sig)]
        assert math.isclose(sig[k], math.hypot(other, 3.0 / C), rel_tol=1e-9)
        assert row["verdict"] == HT.V_SOLVED, row["verdict"]
        s = row["solution"]
        assert abs(s["east_m"] - 40.0) < 0.5 and abs(s["north_m"] - 30.0) < 0.5

    def test_nothing_the_run_reports_carries_a_node_coordinate(self, tmp_path):
        p = gps_entry(25, -20)
        t = self._run(tmp_path, [node_row("gold", T0, seed=7)], {"gold": p})
        text = HT.format_report(t) + json.dumps(HT._jsonable(t))
        assert ("%.6f" % p["lat_deg"]) not in text and ("%.6f" % p["lon_deg"]) not in text


def test_the_cli_defaults_gps_positions_under_the_pool_and_none_disables(tmp_path, monkeypatch):
    seen = {}

    def fake_run(root, survey, policy, **kw):
        seen["gps"] = policy["gps_positions"]
        raise HT.Refusal("stop")
    monkeypatch.setattr(HT, "run", fake_run)
    args = ["--pool", str(tmp_path / "pool"), "--survey", write_survey(tmp_path),
            "--source-class", "blast"]
    assert HT.main(args) == 1
    assert seen["gps"] == str(tmp_path / "pool" / "state" / "node_positions.json")
    assert HT.main(args + ["--gps-positions", "none"]) == 1
    assert seen["gps"] is None
