"""Seam S1: the `/ls` handler and the drain's parser have to agree about one line format.

The handler is the ONLY way to see a clip whose dets row has already rolled off the card, and it
is the half of this seam that cannot be run -- the fleet is not flashed and must not be. So the
producer is checked by reading its source and the consumer by feeding it a golden body, and the
two meet at the line format `- <name>  <N> B`.

⚠️THESE READ ONE HANDLER BODY, NEVER THE WHOLE FILE, AND THAT SCOPING IS LOAD-BEARING. Measured:
`SD.open("/")` appears once MORE in this file, at hear_node.ino:1379, so
`test_ls_takes_a_directory_argument`'s negative assertion would fail against a correct handler if
it scanned the file. The same is true in reverse for the positive ones -- the prose above the
handler discusses `dir`, `..` and the entry cap.

Comments are stripped as well, following tests/test_clip_priority.py. That part is currently
defence-in-depth and not load-bearing: no assertion below changes truth value with comments left
in, checked. It stays because the handler's own comment block names every construct these tests
look for, so one reworded line is all it would take -- and this repo has shipped that bug twice.
"""
import re
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import hear_drain as D  # noqa: E402

INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "ls-clips-nyquist.txt"
ROOT_CAPTURE = Path(__file__).resolve().parent / "fixtures" / "ls_nyquist.txt"


def _handler(path):
    """The braces-balanced body of the `http.on("<path>", ...)` registration, comments removed.

    `_body()` in test_clip_priority.py finds a named C function; these handlers are lambdas, so
    the anchor is the registration string instead. Everything else is the same discipline.
    """
    src = INO.read_text()
    i = src.index('http.on("%s"' % path)
    i = src.index("{", i)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = src[i:j]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return "\n".join(l.split("//")[0] for l in body.splitlines())


class TestTheHandlerTakesADirectory:
    def test_ls_takes_a_directory_argument(self):
        # Pre-change this handler was `String o; File d = SD.open("/");` and called hasArg()
        # NOWHERE, so /clips could not be listed and clip names could not be discovered at all.
        b = _handler("/ls")
        assert 'hasArg("dir")' in b, "/ls must read an optional dir argument"
        assert 'arg("dir")' in b, "/ls must use the dir argument it checked for"
        assert 'SD.open("/")' not in b, (
            "/ls still hardcodes the root directory; the dir argument would be accepted and "
            "then ignored, which is worse than not accepting it")

    def test_an_absent_argument_still_lists_the_root(self):
        b = _handler("/ls")
        assert 'String("/")' in b, (
            "the dir default must be the root, so a caller that passes nothing -- every deployed "
            "caller today -- gets exactly the listing it got before")

    def test_a_dotdot_is_refused_by_the_node_and_not_only_by_the_client(self):
        b = _handler("/ls")
        assert '.."' in b and "400" in b, (
            "/ls must refuse '..' itself; hear.clips.parse_clip_name refusing it on the client "
            "protects the PVC, not the card")

    def test_a_path_that_is_not_a_directory_is_a_404_not_an_empty_listing(self):
        b = _handler("/ls")
        assert "isDirectory()" in b and "404" in b, (
            "a file, or a path that does not exist, must answer 404 -- an empty 200 would read "
            "as 'the directory is empty', which is the difference between measured-zero and "
            "unmeasured")

    def test_the_listing_is_capped_and_says_when_it_capped(self):
        b = _handler("/ls")
        assert "LS_MAX_ENTRIES" in b, "the listing must be entry-capped"
        assert "truncated" in b, (
            "hitting the cap must be announced in the body; a silently short listing is a "
            "partial census that reads as a complete one")

    def test_the_cap_is_not_sized_to_the_49_clip_budget(self):
        # clip_budget_left is a RAM counter reset full every boot with no startup rescan of
        # CLIP_DIR, so eviction only binds once THIS boot's counter is spent. The real ceiling is
        # free SD space (~155 files on a ~19 MiB-free card), not 49.
        m = re.search(r"#define\s+LS_MAX_ENTRIES\s+(\d+)", INO.read_text())
        assert m, "LS_MAX_ENTRIES must be a defined constant"
        assert int(m.group(1)) > 49, (
            "LS_MAX_ENTRIES is %s, at or under the 49-clip budget figure. That budget is not "
            "enforced across reboots, so sizing the cap to it caps the census below what the "
            "card actually holds" % m.group(1))

    def test_the_listing_is_streamed_rather_than_accumulated(self):
        b = _handler("/ls")
        assert "sendContent" in b, (
            "the listing must stream like /sd does; one growing String over a capped 256 entries "
            "is heap the node does not have to spare")


class TestTheNameIsDirectoryQualified:
    def test_a_subdirectory_listing_qualifies_its_names(self):
        b = _handler("/ls")
        assert "pre" in b and "substring(1)" in b, (
            "a name from a subdirectory must carry its directory, so it is usable straight into "
            "/sd?file=/<name> with no client-side reconstruction")

    def test_the_root_listing_format_is_unchanged(self):
        # The regression that would break every deployed caller: prefixing root names too.
        b = _handler("/ls")
        assert 'dir == "/"' in b, (
            "root must resolve to an EMPTY prefix explicitly; without that special case every "
            "root name gains a prefix and scene_names() stops recognising scene.csv")

    def test_the_root_capture_still_parses_exactly_as_before(self):
        # The real, live, unedited root listing off nyquist. This is the one half of the seam
        # that IS a capture, and it must survive the handler change untouched.
        got = D._ls_parse(ROOT_CAPTURE.read_text())
        assert got["scene.csv"] == 16771742
        assert got["dets.csv"] == 4557
        assert "clips" not in got, "`d clips  0 B` is a directory line and must not be a file"
        assert got.truncated_at is None


class TestTheListingParsesThroughTheDrainsOwnParser:
    def test_the_listing_parses_through_the_drains_own_parser(self):
        got = D._ls_parse(GOLDEN.read_text())
        assert got == {
            "clips/nyquist-db21acd5-1016781646.wav": 128044,
            "clips/nyquist-db21acd5-1016849331.wav": 128044,
            "clips/nyquist-db21acd5-1082421378.wav": 128044,
            "clips/nyquist-db21acd5-1082530195.wav": 128044,
            "clips/07-nyquist-4f0c9b12-0000149504.wav": 128044,
            "clips/23-nyquist-4f0c9b12-0000216064.wav": 128044,
        }

    def test_every_golden_name_is_the_measured_clip_size(self):
        got = D._ls_parse(GOLDEN.read_text())
        assert set(got.values()) == {D.CL.CLIP_BYTES_16K_4S}

    def test_a_truncation_marker_is_carried_not_dropped(self):
        # ⚠️THE REGRESSION THIS EXISTS FOR. `! truncated ...` starts with `!`, so the `- ` parse
        # drops it like any other unparseable line -- and a PARTIAL listing then reads as a
        # complete one. Before LsListing there was nowhere for this fact to live.
        body = GOLDEN.read_text() + "! truncated at 256 entries\n"
        got = D._ls_parse(body)
        assert got.truncated_at == 256
        assert len(got) == 6, "the truncation marker must not become a file entry"

    def test_a_complete_listing_says_so_rather_than_saying_nothing(self):
        assert D._ls_parse(GOLDEN.read_text()).truncated_at is None


class TestTheDrainCanActuallyAskForADirectory:
    def test_the_fetcher_takes_a_dir_and_puts_it_on_the_wire(self, monkeypatch):
        # Pre-change _ls_sizes had no dir parameter at all, so the handler's new argument was
        # unreachable from the drain -- a producer with no consumer.
        seen = []

        def fake(url, timeout=None):
            seen.append(url)
            return GOLDEN.read_bytes()

        monkeypatch.setattr(D, "_get", fake)
        D._ls_sizes("10.0.0.1", 5.0, dir="/clips")
        assert seen == ["http://10.0.0.1/ls?dir=/clips"]

    def test_no_dir_is_the_bare_url_every_deployed_caller_uses(self, monkeypatch):
        seen = []
        monkeypatch.setattr(D, "_get", lambda url, timeout=None: (seen.append(url) or b""))
        D._ls_sizes("10.0.0.1", 5.0)
        assert seen == ["http://10.0.0.1/ls"]


class TestTheListingBecomesAWorkList:
    NODE = "nyquist"

    def test_the_golden_listing_yields_every_clip_it_names(self):
        got = D.ls_candidates(D._ls_parse(GOLDEN.read_text()), self.NODE)
        assert [c["parts"]["basename"] for c in got] == [
            # oldest-first WITHIN a boot, and boots sort before samples -- eviction order, which
            # is fetch order, and never priority order.
            "07-nyquist-4f0c9b12-0000149504.wav",
            "23-nyquist-4f0c9b12-0000216064.wav",
            "nyquist-db21acd5-1016781646.wav",
            "nyquist-db21acd5-1016849331.wav",
            "nyquist-db21acd5-1082421378.wav",
            "nyquist-db21acd5-1082530195.wav",
        ]

    def test_both_shipped_name_shapes_survive_the_same_listing(self):
        got = {c["parts"]["basename"]: c["parts"]["prio"]
               for c in D.ls_candidates(D._ls_parse(GOLDEN.read_text()), self.NODE)}
        assert got["nyquist-db21acd5-1016781646.wav"] is None   # the flashed fleet writes no prefix
        assert got["07-nyquist-4f0c9b12-0000149504.wav"] == 7   # this checkout does
        assert got["23-nyquist-4f0c9b12-0000216064.wav"] == 23

    def test_a_listing_carries_no_time_and_invents_none(self):
        for c in D.ls_candidates(D._ls_parse(GOLDEN.read_text()), self.NODE):
            assert c["anchored"] is False
            assert c["ts_utc_s"] is None and c["fs_hz"] is None and c["record_key"] is None, (
                "a directory listing has no timestamp; a zero here would put the clip under a "
                "real UTC day it was never measured in")

    def test_the_real_root_capture_yields_no_clips_which_is_the_flashed_fleets_answer(self):
        # ⚠️NOT A STUB. This is the running firmware's actual /ls output: `clips` appears as a
        # DIRECTORY, so there is no clip name in it to find. ls_candidates is therefore live on
        # every drain today at zero extra requests, and returns the right answer.
        assert D.ls_candidates(D._ls_parse(ROOT_CAPTURE.read_text()), self.NODE) == []

    @pytest.mark.parametrize("name", [
        "clips/../../etc/passwd", "clips/nested/deep.wav", "clips/notes.txt",
        "clips/.wav", "dets.csv", "scene.csv",
    ])
    def test_a_name_that_is_not_a_clip_never_becomes_a_candidate(self, name):
        assert D.ls_candidates({name: 128044}, self.NODE) == []

    def test_a_listing_records_the_size_the_node_reported(self):
        got = D.ls_candidates({"clips/nyquist-db21acd5-1016781646.wav": 40960}, self.NODE)
        assert got[0]["ls_bytes"] == 40960, (
            "the listed size is the one number a listing has that dets does not; a short one is "
            "how a partial clip is spotted before 128 kB is spent asking for it")


class TestTheTwoDiscoverySourcesUnion:
    def test_dets_wins_because_it_is_the_richer_row(self):
        k = D.CL.clip_key("nyquist", "db21acd5", 1016781646)
        dets = [{"clip_key": k, "clip": "/clips/nyquist-db21acd5-1016781646.wav",
                 "anchored": True, "utc_us": 1757459321000000}]
        ls = D.ls_candidates({"clips/nyquist-db21acd5-1016781646.wav": 128044}, "nyquist")
        got = D.merge_candidates(dets, ls)
        assert len(got) == 1, "the same clip from both sources is one fetch, not two"
        assert got[0]["anchored"] is True, (
            "the dets row carries utc_us, fs_hz and the parent record key; the listing carries "
            "none of them, so dets must win")

    def test_a_clip_dets_no_longer_names_still_gets_fetched(self):
        # The entire point of the endpoint: 478+ clips were destroyed, and a clip outlives the
        # dets row that named it once dets.csv rolls.
        dets = []
        ls = D.ls_candidates(D._ls_parse(GOLDEN.read_text()), "nyquist")
        assert len(D.merge_candidates(dets, ls)) == 6

    def test_dets_order_is_preserved_because_it_is_the_eviction_order(self):
        a = D.CL.clip_key("nyquist", "aaaaaaaa", 1)
        b = D.CL.clip_key("nyquist", "aaaaaaaa", 2)
        dets = [{"clip_key": a}, {"clip_key": b}]
        ls = D.ls_candidates({"clips/nyquist-bbbbbbbb-0000000003.wav": 128044}, "nyquist")
        got = D.merge_candidates(dets, ls)
        assert [c["clip_key"] for c in got[:2]] == [a, b]
        assert len(got) == 3

    def test_an_empty_or_failed_listing_is_not_an_error(self):
        # A failed /ls is recorded as its own blind spot and must not take the clip lane with it.
        assert D.ls_candidates(None, "nyquist") == []
        assert D.merge_candidates([], D.ls_candidates(None, "nyquist")) == []
