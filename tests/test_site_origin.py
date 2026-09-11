"""HEAR_SITE_ORIGIN: the one door the real site origin comes through, and the refusal without it."""
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.backend import survey as SV                            # noqa: E402
from tools import hear_tdoa as HT                                # noqa: E402

NODES = [
    {"node_id": 1, "name": "nyquist", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "sigma_m": 0.717},
    {"node_id": 2, "name": "mach", "e_m": -16.602, "n_m": -0.272, "u_m": 3.0, "sigma_m": 0.521},
    {"node_id": 3, "name": "rankine", "e_m": -4.58, "n_m": 10.48, "u_m": 3.0, "sigma_m": 0.49},
]
SITE = "-33.8688,151.2093,58.0"


@pytest.fixture(autouse=True)
def _no_site(monkeypatch):
    monkeypatch.delenv(SV.SITE_ORIGIN_ENV, raising=False)


def write_survey(tmp_path, fictional=True):
    origin = {"lat_deg": 12.3456, "lon_deg": -45.6789, "h_ell_m": 0.0}
    if fictional:
        origin["fictional"] = True
    p = tmp_path / "survey.json"
    p.write_text(json.dumps({"frame": "enu_local", "units": "m", "origin": origin,
                             "nodes": NODES}))
    return str(p)


class TestTheOverride:

    def test_unset_keeps_the_files_origin(self, tmp_path):
        s = SV.load_survey(write_survey(tmp_path))
        assert s.origin_geodetic() == (12.3456, -45.6789, 0.0)
        assert s.origin_is_fictional()

    def test_set_replaces_the_origin_and_nothing_else(self, tmp_path, monkeypatch):
        p = write_survey(tmp_path)
        before = SV.load_survey(p)
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, SITE)
        s = SV.load_survey(p)
        assert s.origin_geodetic() == (-33.8688, 151.2093, 58.0)
        assert not s.origin_is_fictional()
        assert (s.positions(s.ids) == before.positions(before.ids)).all()
        assert s.names == before.names and s.sigma_m == before.sigma_m

    def test_whitespace_around_fields_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, " -33.8688 , 151.2093 , 58 ")
        assert SV.load_survey(write_survey(tmp_path)).origin_geodetic() == (-33.8688, 151.2093,
                                                                              58.0)

    @pytest.mark.parametrize("bad", [
        "", "-33.8688,151.2093", "-33.8688,151.2093,58.0,1", "south,east,0", "nan,0,0",
        "inf,0,0", "1e1,0,0", "+1,0,0", "1_0,0,0", "91.5,0,0", "0,181.5,0", "0,0,0",
        "-33.8688,151.2093,20000"])
    def test_a_malformed_value_is_refused_without_echoing_it(self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, bad)
        with pytest.raises(SV.SiteOriginError) as ei:
            SV.load_survey(write_survey(tmp_path, fictional=False))
        for field in bad.split(","):
            if len(field.strip()) >= 3:
                assert field.strip() not in str(ei.value)


class TestTheProductionRefusal:

    def test_tests_and_offline_tools_can_still_load_a_fictional_survey(self, tmp_path):
        assert SV.load_survey(write_survey(tmp_path)).origin_is_fictional()

    def test_requiring_a_real_origin_refuses_the_fictional_one(self, tmp_path):
        with pytest.raises(SV.SiteOriginError, match=SV.SITE_ORIGIN_ENV):
            SV.load_survey(write_survey(tmp_path), require_real_origin=True)

    def test_requiring_a_real_origin_passes_once_the_site_is_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, SITE)
        s = SV.load_survey(write_survey(tmp_path), require_real_origin=True)
        assert s.origin_geodetic()[0] == -33.8688

    def test_a_survey_not_marked_fictional_needs_no_site(self, tmp_path):
        SV.load_survey(write_survey(tmp_path, fictional=False), require_real_origin=True)

    def _plan(self, tmp_path):
        return HT.main(["--pool", str(tmp_path / "pool"), "--out", str(tmp_path / "out"),
                        "--survey", write_survey(tmp_path), "--plan-only"])

    def test_hear_tdoa_refuses_to_run_on_the_fictional_origin(self, tmp_path, capsys):
        assert self._plan(tmp_path) == 1
        assert SV.SITE_ORIGIN_ENV in capsys.readouterr().err

    def test_hear_tdoa_runs_once_the_site_is_set(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(SV.SITE_ORIGIN_ENV, SITE)
        assert self._plan(tmp_path) == 0
        assert SV.SITE_ORIGIN_ENV not in capsys.readouterr().err

    def test_the_check_refuses_too_rather_than_passing_on_stale_gates(self, tmp_path, capsys):
        rc = HT.main(["--pool", str(tmp_path / "pool"), "--out", str(tmp_path / "out"),
                      "--survey", write_survey(tmp_path), "--check"])
        assert rc == 1
        assert SV.SITE_ORIGIN_ENV in capsys.readouterr().err

    def test_the_shipped_survey_is_marked_fictional(self):
        assert SV.load_survey(str(ROOT / "survey.json")).origin_is_fictional()


_ENV = re.compile(r"- name: HEAR_SITE_ORIGIN\s+valueFrom:\s+"
                  r"secretKeyRef: \{ name: hear-site, key: origin \}")


def test_every_manifest_that_mounts_the_survey_sets_the_site_from_the_secret():
    seen = 0
    for f in sorted((ROOT / "deploy" / "k8s").glob("*.yaml")):
        if f.name.endswith("-code.yaml"):
            continue
        for doc in re.split(r"(?m)^---\s*$", f.read_text()):
            n_mounts = doc.count("subPath: survey.json")
            if n_mounts:
                seen += 1
                assert len(_ENV.findall(doc)) == n_mounts, f.name
    assert seen >= 2
