"""tools/coord_guard.py: a coordinate pair far from the fictional origin fails, and is never printed.

Every coordinate here is built at test time from survey.json's fictional origin, so this file
carries none and the guard passes over it.
"""
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

import pytest
import yaml

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

    @pytest.mark.parametrize("key,axis", [("lat_deg", "lat"), ("lon_deg", "lon")])
    def test_a_lone_keyed_value_is_enough(self, key, axis):
        far = LAT0 + FAR if axis == "lat" else LON0 - FAR
        near = LAT0 + NEAR if axis == "lat" else LON0 - NEAR
        assert lines('"%s": %s' % (key, fmt(far))) == [1]
        assert lines('"%s": %s' % (key, fmt(near))) == []

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

    @pytest.mark.parametrize("a,b", [(90.0, 0.0), (-90.0, 0.0), (0.0, 180.0), (0.0, -180.0)])
    def test_a_pole_or_antimeridian_boundary_pair_is_detected(self, a, b):
        # 90/-90/180/-180 exactly are valid lat/lon values in their own right, not just invalid
        # values that happen to be rejected -- confirm the boundary itself is still a candidate.
        assert lines("%s, %s" % (fmt(a, 4), fmt(b, 4))) == [1]

    def test_a_value_invalid_on_both_axes_at_once_is_rejected(self):
        # 190.0 is invalid as a latitude (>90) and as a longitude (>180), so no ordering of the
        # pair is ever valid -- unlike e.g. (90.0001, 0.0), which is invalid as a latitude but a
        # perfectly good longitude under the swapped order, and so *is* still detected.
        assert lines("%s, %s" % (fmt(190.0, 4), fmt(0.0, 4))) == []

    def test_a_near_pole_value_is_still_a_pair_via_the_swapped_order(self):
        # 90.0001 is invalid as a latitude but valid as a longitude, so _orders' swapped-order
        # ambiguity still finds a valid reading -- this is intended, not a rejection case.
        assert lines("%s, %s" % (fmt(90.0001, 4), fmt(0.0, 4))) == [1]


class TestSignParsing:
    """numbers(): a '-' directly against the digit run is always its sign, whatever precedes
    the '-' itself -- see tools/coord_guard.py's numbers() docstring for the reasoning."""

    @pytest.mark.parametrize("prefix", ["a", "id", "5", "x_1", "v9"])
    def test_a_minus_glued_to_a_letter_or_digit_is_the_sign_not_dropped(self, prefix):
        v = -(LAT0 + FAR)
        toks = list(CG.numbers("%s%s" % (prefix, fmt(v))))
        assert len(toks) == 1
        assert toks[0][2] == pytest.approx(v)

    @pytest.mark.parametrize("context", [" ", "\n", "=", ":", ",", "(", "[", '"'])
    def test_a_signed_value_after_a_separator_keeps_its_sign(self, context):
        v = -(LAT0 + FAR)
        toks = list(CG.numbers("head%s%s" % (context, fmt(v))))
        assert len(toks) == 1
        assert toks[0][2] == pytest.approx(v)

    def test_a_signed_value_at_the_start_of_text_keeps_its_sign(self):
        v = -(LAT0 + FAR)
        toks = list(CG.numbers(fmt(v)))
        assert len(toks) == 1
        assert toks[0][2] == pytest.approx(v)

    @pytest.mark.parametrize("prefix", ["a", "id", "5", "x_1", "v9"])
    def test_a_far_pair_glued_to_an_identifier_minus_is_still_flagged(self, prefix):
        la, lo = far_pair()  # lo is negative here (west of the fictional origin)
        assert lines("%s%s, %s" % (prefix, fmt(lo), fmt(la))) == [1]

    @pytest.mark.parametrize("prefix", ["a", "id", "5", "x_1", "v9"])
    def test_a_near_pair_glued_to_an_identifier_minus_is_not_flagged(self, prefix):
        la, lo = near_pair()
        assert lines("%s%s, %s" % (prefix, fmt(lo), fmt(la))) == []

    def test_the_glued_value_never_appears_in_output(self):
        la, lo = far_pair()
        out = repr(guard().scan_text("x.txt", "id%s, %s" % (fmt(lo), fmt(la))))
        assert_not_printed(out, la, lo)

    @staticmethod
    def _unicode_minus(v):
        # The same signed decimal string numbers() would otherwise see, with just its leading
        # ASCII "-" swapped for U+2212 MINUS SIGN -- everything else about the text is identical
        # to the ASCII-minus form already covered elsewhere in this file.
        return fmt(v).replace("-", "−", 1)

    def test_a_unicode_minus_sign_is_recognized_in_an_inline_pair(self):
        la, lo = far_pair()
        far = "%s, %s" % (self._unicode_minus(lo), fmt(la))
        la_n, lo_n = near_pair()
        near = "%s, %s" % (self._unicode_minus(lo_n), fmt(la_n))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_unicode_minus_sign_is_recognized_in_a_keyed_value(self):
        far = '"lon_deg": %s' % self._unicode_minus(LON0 - FAR)
        near = '"lon_deg": %s' % self._unicode_minus(LON0 - NEAR)
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_unicode_minus_sign_is_recognized_in_an_array_pair(self):
        la, lo = far_pair()
        far = "[%s, %s]" % (self._unicode_minus(lo), fmt(la))
        la_n, lo_n = near_pair()
        near = "[%s, %s]" % (self._unicode_minus(lo_n), fmt(la_n))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_percent_encoded_comma_separator_is_recognized(self):
        la, lo = far_pair()
        far = "q=%s%%2C%s" % (fmt(la), fmt(lo))
        near_la, near_lo = near_pair()
        near = "q=%s%%2C%s" % (fmt(near_la), fmt(near_lo))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_bare_hemisphere_letter_alone_separates_a_sign_glued_pair(self):
        # "<lat>N-<lon>W": no comma or space between the numbers at all, just the hemisphere
        # letter, with the second number's sign now glued directly onto that letter.
        la, lo = far_pair()
        far = "%sN%sW" % (fmt(la), fmt(lo))
        near_la, near_lo = near_pair()
        near = "%sN%sW" % (fmt(near_la), fmt(near_lo))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_small_magnitude_array_pair_is_still_detected(self):
        # Both axes under 1 degree in magnitude: still genuinely far via the fictional origin
        # (tens of thousands of km away at these magnitudes), and array syntax is unambiguous
        # enough not to need the >=1.0 floor the loose inline-pair form uses.
        a, b = 0.5, 0.6
        assert lines("[%s, %s]" % (fmt(a, 4), fmt(b, 4))) == [1]
        # the equivalent bare (non-bracketed) pair intentionally keeps the floor
        assert lines("%s, %s" % (fmt(a, 4), fmt(b, 4))) == []


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

    def test_the_allowlist_still_matches_a_value_written_with_a_glued_minus(self, tmp_path):
        # The digest is computed from the parsed float value, not from the source text, so an
        # entry keyed by a value that happens to have been written with a glued minus still
        # matches once numbers() parses that value correctly.
        la, lo = far_pair()
        digest = CG.pair_digest(lo, la)
        allow = tmp_path / "allow.txt"
        allow.write_text("docs/x.md %s  # synthetic, not a coordinate\n" % digest)
        g = CG.Guard((LAT0, LON0), allow=CG.load_allow(str(allow)))
        assert g.scan_text("docs/x.md", "id%s, %s" % (fmt(lo), fmt(la))) == []
        assert [f.line for f in g.scan_text("docs/y.md", "id%s, %s" % (fmt(lo), fmt(la)))] == [1]


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

    def test_near_origin_values_and_genuine_binaries_pass(self, repo, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        la, lo = near_pair()
        _commit(repo, "near.txt", "%s, %s\n" % (fmt(la), fmt(lo)), "near")
        # Genuinely binary: dense NUL/high-byte content with no ASCII digit run anywhere in it,
        # so this is a control for "binary content doesn't false-positive", not a test of the
        # (removed) NUL-presence skip -- decoded with errors="replace" it has nothing
        # coordinate-shaped for the regexes to find.
        random_bytes = bytes((n * 137 + 41) % 256 for n in range(4096))
        (repo / "blob.bin").write_bytes(random_bytes)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "binary")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 0, out

    def test_a_leading_nul_byte_no_longer_hides_a_far_pair(self, repo, capsys):
        # A blob used to be dropped from scanning entirely on the mere presence of one NUL byte
        # anywhere in its first 8KB, so a single stray/leading NUL in front of a real coordinate
        # was a total, silent bypass. It no longer is: the NUL-presence heuristic was removed, so
        # every blob under the size cap is scanned regardless of stray NUL bytes in it.
        base = _git(repo, "rev-parse", "HEAD")
        far = "%s, %s" % tuple(fmt(v) for v in far_pair())
        _commit(repo, "blob.bin", "\0" + far, "one leading NUL, then a far pair")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 1 and "blob.bin:1:" in out
        assert_not_printed(out, *far_pair())

    def test_an_oversized_blob_is_reported_as_skipped_not_silently_passed(self, repo, capsys):
        # A blob over MAX_BLOB_BYTES is still not scanned (the cap exists to bound memory/CPU on
        # an accidentally-huge blob), but that must never look identical to "nothing was there
        # to find" -- the summary line now says a blob was excluded so a human reviewing a
        # REQUIRED, admins-included check can see coverage was incomplete.
        base = _git(repo, "rev-parse", "HEAD")
        far = "%s, %s" % tuple(fmt(v) for v in far_pair())
        padding = "x" * (CG.MAX_BLOB_BYTES + 1 - len(far))
        _commit(repo, "huge.txt", padding + far, "oversized")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 0, out
        assert "1 blob(s) over %d bytes not scanned" % CG.MAX_BLOB_BYTES in out
        assert_not_printed(out, *far_pair())

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

    @pytest.mark.parametrize("key", ["lat_deg", "lon_deg"])
    def test_a_malformed_origin_value_is_refused_without_echoing_it(self, repo, capsys, key):
        survey = json.loads((repo / "survey.json").read_text())
        la, lo = far_pair()
        survey["origin"][key] = "%s, %s x" % (fmt(la), fmt(lo))
        _commit(repo, "survey.json", json.dumps(survey), "malformed origin")
        with pytest.raises(SystemExit) as ei:
            run(capsys, repo, "tree")
        assert_not_printed(str(ei.value) + capsys.readouterr().out, la, lo)

    @pytest.mark.parametrize("name", [
        "a\n::warning::injected.txt", "b\r\n::error::injected.txt", "c%0A::error::x.txt",
        "d,line=1::x.txt", "e::f,g:h.txt", "%25%3A.txt"])
    def test_a_hostile_filename_cannot_add_a_workflow_command(self, repo, capsys, name):
        base = _git(repo, "rev-parse", "HEAD")
        la, lo = far_pair()
        _commit(repo, name, "%s, %s\n" % (fmt(la), fmt(lo)), "hostile")
        for args in (["tree"], ["range", base, "HEAD"], ["range", "", "HEAD"]):
            rc, out = run(capsys, repo, *args)
            assert rc == 1
            rows = out.split("\n")
            commands = [r for r in rows if r.startswith("::") or "\r" in r]
            assert len(commands) == 1, rows
            prop, _, message = commands[0][len("::error "):].partition("::")
            assert commands[0].startswith("::error file=")
            assert re.fullmatch(r"file=[^:,\r\n]*,line=1", prop), prop
            assert CG.escape_property(name) in prop
            assert "\r" not in message and CG.escape_data(name) in message
            assert all(r.startswith("coord_guard: ") for r in rows[1:] if r)
            assert_not_printed(out, la, lo)

    def test_escaping_matches_the_workflow_command_rules(self):
        assert CG.escape_data("%\r\n:,") == "%25%0D%0A:,"
        assert CG.escape_property("%\r\n:,") == "%25%0D%0A%3A%2C"

    def test_an_empty_range_passes(self, repo, capsys):
        rc, out = run(capsys, repo, "range", "HEAD", "HEAD")
        assert rc == 0, out


class TestWorkflow:
    """Runs the coord-guard.yml step's actual shell script (bash -e, matching how GitHub Actions
    invokes a `run:` block), not a re-typed copy of its logic, so this fails if the workflow's
    behavior changes even without changing its wording."""

    @staticmethod
    def _step_script():
        wf = yaml.safe_load((ROOT / ".github/workflows/coord-guard.yml").read_text())
        for step in wf["jobs"]["coordinates"]["steps"]:
            if step.get("name") == "every commit this event brings in":
                return step["run"]
        raise AssertionError("step not found")

    def test_a_ref_deletion_push_exits_before_any_python_invocation(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "python-called"
        stub = bin_dir / "python"
        stub.write_text('#!/bin/sh\ntouch "%s"\nexit 9\n' % sentinel)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")),
                   EVENT="push", BEFORE="f" * 40, AFTER="0" * 40, PR_BASE="", PR_HEAD="")
        proc = subprocess.run(["bash", "-e", "-c", self._step_script()],
                              cwd=ROOT, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert not sentinel.exists(), "the guard's python entry point ran on a ref-deletion push"
        assert "nothing to scan" in proc.stdout

    def test_a_normal_push_still_reaches_python(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "python-called"
        stub = bin_dir / "python"
        stub.write_text('#!/bin/sh\ntouch "%s"\nexit 0\n' % sentinel)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")),
                   EVENT="push", BEFORE="", AFTER="f" * 40, PR_BASE="", PR_HEAD="")
        proc = subprocess.run(["bash", "-e", "-c", self._step_script()],
                              cwd=ROOT, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert sentinel.exists()


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_this_checkout_passes_the_tree_scan(capsys):
    rc = CG.main(["--repo", str(ROOT), "tree"])
    assert rc == 0, capsys.readouterr().out
