"""tools/coord_guard.py: a coordinate pair far from the fictional origin fails, and is never printed.

Every coordinate here is built at test time from survey.json's fictional origin, so this file
carries none and the guard passes over it.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import coord_guard as CG                              # noqa: E402

_ORIGIN = json.loads((ROOT / "survey.json").read_text())["origin"]
LAT0, LON0 = _ORIGIN["lat_deg"], _ORIGIN["lon_deg"]
NEAR = 0.001
FAR = 1.5


def fmt(v, places=6):
    return "%.*f" % (places, v)


def far_pair():
    return LAT0 + FAR / 3, LON0 - FAR


def near_pair():
    return LAT0 + NEAR, LON0 - NEAR


def guard(**kw):
    return CG.Guard((LAT0, LON0), **kw)


def lines(text, **kw):
    return [f.line for f in guard(**kw).scan_text("x.txt", text)]


def assert_not_printed(out, *values):
    for v in values:
        for spelled in (fmt(v, 6), fmt(v, 4), fmt(abs(v), 4), fmt(abs(v), 2)):
            assert spelled not in out


class TestDetection:

    @pytest.mark.parametrize("template", [
        "{a}, {b}", "{a},{b}", "{a} {b}", "({a}, {b}, 120.0)", "{a};{b}", "{a}N {b}W",
        "x = [{a}, {b}]", "origin {a}, {b} here"])
    def test_a_far_inline_pair_is_found_and_a_near_one_passes(self, template):
        la, lo = far_pair()
        assert lines("head\n" + template.format(a=fmt(la), b=fmt(lo))) == [2]
        la, lo = near_pair()
        assert lines("head\n" + template.format(a=fmt(la), b=fmt(lo))) == []

    def test_either_order_is_a_pair(self):
        la, lo = far_pair()
        assert lines("[%s, %s]" % (fmt(lo), fmt(la))) == [1]
        la, lo = near_pair()
        assert lines("[%s, %s]" % (fmt(lo), fmt(la))) == []

    @pytest.mark.parametrize("keys", [("lat", "lon"), ("latitude", "longitude"),
                                      ("lat_deg", "lon_deg"), ("Lat", "Lng")])
    def test_keyed_values_split_across_json_lines(self, keys):
        la, lo = far_pair()
        text = json.dumps({keys[0]: la, "h_ell_m": 1.0, keys[1]: lo}, indent=2)
        assert lines(text) == [2]
        la, lo = near_pair()
        assert lines(json.dumps({keys[0]: la, keys[1]: lo}, indent=2)) == []

    def test_keyed_values_in_yaml_and_kwargs(self):
        la, lo = far_pair()
        assert lines("site:\n  lat: %s\n  lon: %s\n" % (fmt(la), fmt(lo))) == [2]
        assert lines("f(lat_deg=%s, lon_deg=%s)" % (fmt(la), fmt(lo))) == [1]

    def test_an_array_split_across_lines(self):
        la, lo = far_pair()
        assert lines("[\n  %s,\n  %s\n]" % (fmt(la), fmt(lo))) == [2]

    def test_a_lone_keyed_longitude_is_enough(self):
        assert lines('"lon_deg": %s' % fmt(LON0 - FAR)) == [1]
        assert lines('"lon_deg": %s' % fmt(LON0 - NEAR)) == []

    @pytest.mark.parametrize("template", [
        "{a3}, {b3}", "{a} / {b}", "v1.{a}", "{a}e-3, {b}", "latency: {a}", "{a}\n{b}",
        "0.{f}, 0.{f}"])
    def test_what_is_not_a_coordinate_pair_is_not_found(self, template):
        la, lo = far_pair()
        text = template.format(a=fmt(la), b=fmt(lo), a3=fmt(la, 3), b3=fmt(lo, 3),
                               f="%06d" % int(FAR * 123457))
        assert lines(text) == []

    def test_the_radius_is_the_boundary(self):
        text = "%s, %s" % (fmt(LAT0), fmt(LON0 - 0.2))
        assert lines(text, radius_km=CG.haversine_km(LAT0, LON0 - 0.2, LAT0, LON0) + 1) == []
        assert lines(text, radius_km=CG.haversine_km(LAT0, LON0 - 0.2, LAT0, LON0) - 1) == [1]

    def test_the_guard_passes_over_its_own_source_tests_and_docs(self):
        g = guard()
        for rel in ("tools/coord_guard.py", "tests/test_coord_guard.py", "README.md"):
            assert g.scan_text(rel, (ROOT / rel).read_text()) == [], rel


class TestAllowlist:

    @pytest.fixture
    def allowed(self, tmp_path):
        la, lo = far_pair()
        allow = tmp_path / "allow.txt"
        allow.write_text("docs/x.md %s  # not a coordinate\n" % CG.pair_digest(la, lo))
        return CG.Guard((LAT0, LON0), allow=CG.load_allow(str(allow))), la, lo

    def test_the_entry_allows_its_value_in_its_path(self, allowed):
        g, la, lo = allowed
        assert g.scan_text("docs/x.md", "%s, %s" % (fmt(la), fmt(lo))) == []

    def test_a_different_value_in_the_allowlisted_path_is_still_found(self, allowed):
        g, la, lo = allowed
        text = "%s, %s\n%s, %s" % (fmt(la), fmt(lo), fmt(la + NEAR), fmt(lo))
        out = g.scan_text("docs/x.md", text)
        assert [f.line for f in out] == [2]
        assert_not_printed(repr(out), la, lo)

    def test_the_same_value_in_a_different_path_is_still_found(self, allowed):
        g, la, lo = allowed
        assert [f.line for f in g.scan_text("docs/y.md", "%s, %s" % (fmt(la), fmt(lo)))] == [1]

    @pytest.mark.parametrize("entry", ["docs/x.md 0123abcd0123abcd\n", "docs/x.md  # a whole path\n",
                                       "docs/x.md 0123abcd  # short digest\n"])
    def test_an_entry_that_is_not_path_digest_reason_is_refused(self, tmp_path, entry):
        allow = tmp_path / "allow.txt"
        allow.write_text(entry)
        with pytest.raises(SystemExit):
            CG.load_allow(str(allow))

    def test_the_committed_allowlist_holds_digests_only(self):
        text = CG.ALLOW_FILE and pathlib.Path(CG.ALLOW_FILE).read_text()
        assert list(CG.numbers(text)) == []
        allow = CG.load_allow(CG.ALLOW_FILE)
        assert allow and all(digests for digests in allow.values())


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, check=True, text=True).stdout.strip()


def _commit(repo, rel, text, msg):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        p.unlink()
    else:
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, "survey.json", (ROOT / "survey.json").read_text(), "survey")
    return r


def run(capsys, repo, *args):
    rc = CG.main(["--repo", str(repo), "--allow", str(repo / "no-allowlist"), *args])
    return rc, capsys.readouterr().out


class TestScanModes:

    def test_a_pair_added_then_removed_fails_the_range_but_not_the_tree(self, repo, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        la, lo = far_pair()
        leak = _commit(repo, "tests/fixture.json",
                       json.dumps({"lat_deg": la, "lon_deg": lo}, indent=2), "leak")
        _commit(repo, "tests/fixture.json", None, "clean up")

        rc, out = run(capsys, repo, "tree")
        assert rc == 0, out
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 1
        assert "commit %s tests/fixture.json:2:" % leak[:12] in out
        assert_not_printed(out, la, lo)
        rc, out = run(capsys, repo, "range", "", "HEAD")
        assert rc == 1 and leak[:12] in out
        assert_not_printed(out, la, lo)

    def test_a_tree_finding_prints_path_and_line_only(self, repo, capsys):
        la, lo = far_pair()
        _commit(repo, "docs/site.md", "intro\n\nat %s, %s\n" % (fmt(la), fmt(lo)), "doc")
        rc, out = run(capsys, repo, "tree")
        assert rc == 1
        assert "docs/site.md:3:" in out
        assert_not_printed(out, la, lo)

    def test_a_leak_brought_in_by_a_merge_is_found(self, repo, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "-b", "side")
        la, lo = far_pair()
        _commit(repo, "a.txt", "%s %s\n" % (fmt(la), fmt(lo)), "side leak")
        _commit(repo, "a.txt", "gone\n", "side clean")
        _git(repo, "checkout", "-q", "-")
        _commit(repo, "b.txt", "main\n", "main work")
        _git(repo, "merge", "-q", "--no-edit", "side")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 1 and "a.txt:1:" in out
        assert_not_printed(out, la, lo)

    def test_near_origin_values_and_binaries_pass(self, repo, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        la, lo = near_pair()
        _commit(repo, "near.txt", "%s, %s\n" % (fmt(la), fmt(lo)), "near")
        far = "%s, %s" % tuple(fmt(v) for v in far_pair())
        _commit(repo, "blob.bin", "\0" + far, "binary")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 0, out

    def test_moving_the_origin_is_judged_against_the_base(self, repo, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        survey = json.loads((repo / "survey.json").read_text())
        la, lo = far_pair()
        survey["origin"].update(lat_deg=la, lon_deg=lo)
        _commit(repo, "survey.json", json.dumps(survey, indent=2), "move origin")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 1 and "survey.json:" in out
        assert_not_printed(out, la, lo)

    def test_an_origin_not_marked_fictional_is_refused_without_echoing_it(self, repo, capsys):
        survey = json.loads((repo / "survey.json").read_text())
        survey["origin"]["fictional"] = False
        _commit(repo, "survey.json", json.dumps(survey), "real origin")
        with pytest.raises(SystemExit) as ei:
            run(capsys, repo, "tree")
        assert_not_printed(str(ei.value) + capsys.readouterr().out, LAT0, LON0)

    def test_an_empty_range_passes(self, repo, capsys):
        rc, out = run(capsys, repo, "range", "HEAD", "HEAD")
        assert rc == 0, out


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_this_checkout_passes_the_tree_scan(capsys):
    rc = CG.main(["--repo", str(ROOT), "tree"])
    assert rc == 0, capsys.readouterr().out
