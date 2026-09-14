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


def fmtc(v, places=4):
    """European decimal-comma spelling of fmt()."""
    return fmt(v, places).replace(".", ",")


def far_pair():
    return LAT0 + FAR / 3, LON0 - FAR


def near_pair():
    return LAT0 + NEAR, LON0 - NEAR


def guard(**kw):
    return CG.Guard((LAT0, LON0), **kw)


def lines(text, **kw):
    return [f.line for f in guard(**kw).scan_text("x.txt", text)]


def kinds(text, **kw):
    return [f.kind for f in guard(**kw).scan_text("x.txt", text)]


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
    def _with_dash(v, dash):
        # The same signed decimal string numbers() would otherwise see, with just its leading
        # ASCII "-" swapped for one Unicode dash/minus look-alike -- everything else about the
        # text is identical to the ASCII-minus form already covered elsewhere in this file.
        return fmt(v).replace("-", dash, 1)

    @pytest.mark.parametrize("dash", list(CG._DASHES))
    def test_a_unicode_minus_sign_is_recognized_in_an_inline_pair(self, dash):
        la, lo = far_pair()
        far = "%s, %s" % (self._with_dash(lo, dash), fmt(la))
        la_n, lo_n = near_pair()
        near = "%s, %s" % (self._with_dash(lo_n, dash), fmt(la_n))
        assert lines(far) == [1]
        assert lines(near) == []

    @pytest.mark.parametrize("dash", list(CG._DASHES))
    def test_a_unicode_minus_sign_is_recognized_in_a_keyed_value(self, dash):
        far = '"lon_deg": %s' % self._with_dash(LON0 - FAR, dash)
        near = '"lon_deg": %s' % self._with_dash(LON0 - NEAR, dash)
        assert lines(far) == [1]
        assert lines(near) == []

    @pytest.mark.parametrize("dash", list(CG._DASHES))
    def test_a_unicode_minus_sign_is_recognized_in_an_array_pair(self, dash):
        la, lo = far_pair()
        far = "[%s, %s]" % (self._with_dash(lo, dash), fmt(la))
        la_n, lo_n = near_pair()
        near = "[%s, %s]" % (self._with_dash(lo_n, dash), fmt(la_n))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_a_percent_encoded_comma_separator_is_recognized(self):
        la, lo = far_pair()
        far = "q=%s%%2C%s" % (fmt(la), fmt(lo))
        near_la, near_lo = near_pair()
        near = "q=%s%%2C%s" % (fmt(near_la), fmt(near_lo))
        assert lines(far) == [1]
        assert lines(near) == []

    def test_percent_decoding_does_not_shift_a_later_line_number(self):
        # %2C decodes to one byte, "," -- shorter than the three bytes it replaces, so an
        # earlier decode on an earlier line must not throw off line counting for a pair further
        # down. line_of() and numbers()/candidates() both work off the one already-normalized
        # text, so this should already hold; this test pins that down explicitly.
        la, lo = far_pair()
        text = "q=1%%2C2\nkeep\n%s%%2C%s" % (fmt(la), fmt(lo))
        assert lines(text) == [3]

    @pytest.mark.parametrize("template", ["took %ss-%ss", "cap %sn-%sn"])
    def test_a_unit_suffixed_range_is_not_a_coordinate_pair(self, template):
        # A zero-width separator right after a bare N/S/n/s letter used to turn a plain
        # timing/measurement range into a pair, because "-" glued to the second value already
        # reads as its sign: "took 1.2345s-2.3456s" and "cap 12.3456n-14.5678n" both got flagged.
        # SEPARATOR is back to requiring an actual comma/semicolon/space between values, so a
        # bare unit letter no longer counts. Two separate assignments, not one literal tuple, so
        # this file's own source never carries the two numbers comma-adjacent.
        a = 1.2345
        b = 2.3456
        assert lines(template % (a, b)) == []

    def test_a_small_magnitude_array_pair_is_not_flagged(self):
        # Both axes under 1 degree: the array branch keeps the same >=1.0 magnitude floor as
        # the loose inline-pair form, because a bracketed 2-3 element float array this small is
        # a common DSP/ML config shape (a normalized bounding box, a clip range, ...), not a
        # coordinate -- flagging it blocks an unrelated PR on a required, admins-included check.
        assert lines("[%s, %s]" % (fmt(0.25, 4), fmt(0.75, 4))) == []
        assert lines("bounds = [%s, %s, 1]" % (fmt(0.1234), fmt(0.5678))) == []
        assert lines("clip: [%s, %s]" % (fmt(-0.0125), fmt(0.0125))) == []


def iso_num(v, int_width=None):
    """"+DD.DDDD" / "-DD.DDDD" -- an ISO 6709-style signed number, optionally zero-padded to
    `int_width` integer digits (for the zero-padded-longitude form)."""
    s = fmt(abs(v))
    if int_width:
        intpart, _, frac = s.partition(".")
        s = intpart.zfill(int_width) + "." + frac
    return ("+" if v >= 0 else "-") + s


class TestRemainingGaps:
    """Each of gaps 1-8 from the coord_guard follow-up: a detection test and, where the gap is
    about narrowing rather than widening detection, a false-positive control."""

    # -- gap 1: ISO 6709 strings ---------------------------------------------

    def test_an_iso6709_pair_is_detected(self):
        la, lo = far_pair()
        assert kinds("%s%s/" % (iso_num(la), iso_num(lo))) == ["iso 6709 pair"]

    def test_an_iso6709_pair_with_an_altitude_is_detected_once(self):
        la, lo = far_pair()
        text = "%s%s+123.456/" % (iso_num(la), iso_num(lo))
        assert kinds(text) == ["iso 6709 pair"]

    def test_an_iso6709_altitude_with_four_decimals_does_not_form_a_second_pair(self):
        # The altitude in the previous test has only 3 fractional digits, below this guard's own
        # 4-digit floor, so it is never even tokenized as a number -- this pins down that a 4+
        # decimal third component still doesn't pair a second time against the longitude.
        la, lo = far_pair()
        alt = LAT0 + 5.0
        text = "%s%s%s/" % (iso_num(la), iso_num(lo), iso_num(alt))
        assert kinds(text) == ["iso 6709 pair"]

    def test_an_iso6709_pair_with_a_zero_padded_longitude_is_detected(self):
        la, lo = far_pair()
        assert kinds("%s%s/" % (iso_num(la), iso_num(lo, int_width=3))) == ["iso 6709 pair"]

    def test_an_iso6709_near_pair_passes(self):
        la, lo = near_pair()
        assert lines("%s%s/" % (iso_num(la), iso_num(lo))) == []

    def test_an_unsigned_first_number_is_not_read_as_iso6709(self):
        # The glued "-" is still the second number's sign (numbers()'s rule), but with no
        # explicit sign on the FIRST number this is a hyphenated range, not ISO 6709 -- gap 8
        # covers what happens to it, not an "iso 6709 pair" finding here.
        la, lo = far_pair()
        text = "%s%s" % (fmt(abs(la)), iso_num(-abs(lo)))
        assert "iso 6709 pair" not in kinds(text)

    # -- gap 2: a leading '+' is a sign, unless glued to an identifier --------

    def test_a_leading_plus_is_a_sign_in_an_inline_pair(self):
        la, lo = far_pair()
        assert lines("+%s, %s" % (fmt(la), fmt(lo))) == [1]
        la, lo = near_pair()
        assert lines("+%s, %s" % (fmt(la), fmt(lo))) == []

    def test_a_leading_plus_is_a_sign_in_a_keyed_value(self):
        la, lo = far_pair()
        assert lines("lat: +%s\nlon: +%s" % (fmt(la), fmt(lo))) == [1]

    def test_a_leading_plus_is_a_sign_in_an_array_pair(self):
        la, lo = far_pair()
        assert lines("[+%s, %s]" % (fmt(la), fmt(lo))) == [1]

    def test_a_plus_glued_to_an_identifier_is_not_consumed_as_a_sign(self):
        # "+" only counts as a sign when it is not itself glued to a preceding
        # letter/digit/underscore/dot -- unlike "-", which always does (numbers()'s rule).
        la, _ = far_pair()
        v = abs(la)
        unglued = list(CG.numbers(" +%s" % fmt(v)))
        glued = list(CG.numbers("x+%s" % fmt(v)))
        assert unglued[0][0] == 1  # "+" consumed: the token starts at the sign
        assert glued[0][0] == 2  # "+" left alone: the token starts at the digit

    # -- gap 3: '|' and '_' separators; '/' stays excluded --------------------

    @pytest.mark.parametrize("sep", ["|", "_"])
    def test_a_pipe_or_underscore_separator_is_recognized(self, sep):
        la, lo = far_pair()
        assert lines("%s%s%s" % (fmt(la), sep, fmt(lo))) == [1]
        la, lo = near_pair()
        assert lines("%s%s%s" % (fmt(la), sep, fmt(lo))) == []

    def test_a_slash_separator_stays_excluded(self):
        # Measured and rejected: adding '/' introduced far-candidate-only findings scanning this
        # repo's own history (ratios/fractions in prose) as well as two external corpora -- see
        # the PR body for the counts. This pins the existing "{a} / {b}" exclusion to a gap-3
        # decision, not just an untouched default.
        la, lo = far_pair()
        assert lines("%s / %s" % (fmt(la), fmt(lo))) == []

    # -- gap 4: glued hemisphere form <lat>N<lon>E -----------------------------

    def test_a_glued_hemisphere_pair_is_detected(self):
        la, lo = far_pair()
        ns, ew = ("N" if la >= 0 else "S"), ("E" if lo >= 0 else "W")
        assert lines("%s%s%s%s" % (fmt(abs(la)), ns, fmt(abs(lo)), ew)) == [1]
        la, lo = near_pair()
        ns, ew = ("N" if la >= 0 else "S"), ("E" if lo >= 0 else "W")
        assert lines("%s%s%s%s" % (fmt(abs(la)), ns, fmt(abs(lo)), ew)) == []

    def test_the_reverted_lowercase_zero_width_hemisphere_form_stays_unflagged(self):
        # Gap 4 is uppercase-only and needs BOTH hemisphere letters -- this is the form that was
        # tried and reverted before: a lowercase unit-suffix letter must not turn a plain range
        # into a pair.
        assert lines("1.2345s-2.3456s") == []
        assert lines("12.3456n-14.5678n") == []

    def test_a_single_hemisphere_letter_is_not_the_glued_form(self):
        la, lo = far_pair()
        assert "glued hemisphere pair" not in kinds("%sN%s" % (fmt(abs(la)), fmt(abs(lo))))

    # -- gap 5: decimal-comma coordinates --------------------------------------

    def test_a_decimal_comma_keyed_pair_is_detected(self):
        la, lo = far_pair()
        assert lines("lat: %s\nlon: %s" % (fmtc(la), fmtc(lo))) == [1]
        la, lo = near_pair()
        assert lines("lat: %s\nlon: %s" % (fmtc(la), fmtc(lo))) == []

    @pytest.mark.parametrize("template", ["{a}N {b}E", "N{a} E{b}"])
    def test_a_decimal_comma_hemisphere_pair_is_detected(self, template):
        la, lo = far_pair()
        text = template.format(a=fmtc(abs(la)), b=fmtc(abs(lo)))
        assert lines(text) == [1]

    @pytest.mark.parametrize("sep", [";", "\t", " "])
    def test_a_decimal_comma_pair_joined_by_separator_is_detected(self, sep):
        la, lo = far_pair()
        assert lines("%s%s%s" % (fmtc(la), sep, fmtc(lo))) == [1]
        la, lo = near_pair()
        assert lines("%s%s%s" % (fmtc(la), sep, fmtc(lo))) == []

    def test_bare_decimal_comma_lists_stay_unflagged(self):
        # No coordinate context (no key, no hemisphere letter, no ;/tab/whitespace pairing) --
        # these must read as plain integer/CSV lists, not coordinates.
        assert lines("[12,3456]") == []
        assert lines("12,3456,7890") == []
        la, lo = far_pair()
        assert lines("%s,%s" % (fmtc(la), fmtc(lo))) == []

    def test_a_capitalized_word_after_a_decimal_comma_number_is_not_a_hemisphere_letter(self):
        # The hemisphere letter must be a bare letter, not the first letter of a longer word --
        # this false positive was found scanning an external corpus during development.
        la, _ = far_pair()
        assert lines("%s North" % fmtc(abs(la))) == []
        assert lines("West %s" % fmtc(abs(la))) == []

    # -- gap 6: double percent-encoding ----------------------------------------

    @pytest.mark.parametrize("double", ["%252C", "%253B"])
    def test_a_double_percent_encoded_separator_is_recognized(self, double):
        la, lo = far_pair()
        assert lines("q=%s%s%s" % (fmt(la), double, fmt(lo))) == [1]
        near_la, near_lo = near_pair()
        assert lines("q=%s%s%s" % (fmt(near_la), double, fmt(near_lo))) == []

    def test_double_percent_decoding_does_not_shift_a_later_line_number(self):
        la, lo = far_pair()
        text = "q=1%%252C2\nkeep\n%s%%252C%s" % (fmt(la), fmt(lo))
        assert lines(text) == [3]

    # -- gap 7: sub-degree pairs need a coordinate context ---------------------

    def test_a_sub_degree_keyed_pair_is_detected(self):
        # Confirms keyed values already skip the >=1.0 magnitude floor that array/inline pairs
        # keep (test_a_small_magnitude_array_pair_is_not_flagged) -- no code change needed here,
        # just this test.
        assert lines("lat: %s\nlon: %s" % (fmt(0.1234), fmt(0.5678))) == [1]

    def test_a_sub_degree_pair_with_a_hemisphere_letter_is_detected(self):
        assert lines("%sN %sW" % (fmt(0.1234), fmt(0.5678))) == [1]

    def test_a_sub_degree_iso6709_pair_is_detected(self):
        assert lines("+%s-%s/" % (fmt(0.1234), fmt(0.5678))) == [1]

    def test_a_bare_sub_degree_inline_pair_is_not_flagged(self):
        # No key, no hemisphere letter, no ISO sign -- same >=1.0 floor as the array form, just
        # without brackets.
        assert lines("%s, %s" % (fmt(0.1234), fmt(0.5678))) == []

    # -- gap 8: a hyphenated range must not also pair with what follows -------

    def test_a_hyphenated_range_does_not_pair_with_a_following_number(self):
        la, lo = far_pair()
        extra = LAT0 + 0.7
        text = "%s-%s, %s" % (fmt(abs(la)), fmt(abs(lo)), fmt(extra))
        assert lines(text) == []

    def test_a_three_number_hyphen_chain_does_not_over_pair(self):
        a, b, c = LAT0 + 10.0, LAT0 + 11.0, LAT0 + 12.0
        assert lines("%s-%s-%s" % (fmt(a), fmt(b), fmt(c))) == []


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
        # A control for "binary content doesn't false-positive": every byte value except ASCII
        # digits, '.', '-' and newlines, so nothing in it can be coordinate-shaped.
        excluded = set(b"0123456789.-\n\r")
        payload = bytes(b for b in ((n * 137 + 41) % 256 for n in range(4096)) if b not in excluded)
        assert b"\0" in payload and not excluded & set(payload)
        (repo / "blob.bin").write_bytes(payload)
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
        # to find" -- an unscanned blob can't be certified clean, so the run now fails closed
        # over it, naming the commit and path, instead of reporting a silent pass.
        base = _git(repo, "rev-parse", "HEAD")
        far = "%s, %s" % tuple(fmt(v) for v in far_pair())
        padding = "x" * (CG.MAX_BLOB_BYTES + 1 - len(far))
        _commit(repo, "huge.txt", padding + far, "oversized")
        rc, out = run(capsys, repo, "range", base, "HEAD")
        assert rc == 1, out
        assert "1 blob(s) over %d bytes not scanned" % CG.MAX_BLOB_BYTES in out
        assert "huge.txt" in out and "blob over" in out
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
