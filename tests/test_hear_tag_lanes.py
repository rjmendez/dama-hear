"""hear-tag lanes, the one header lie that is recovered, and the per-run heard summary."""
import hashlib
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hear_tag import (HT, TAGS, VERIFIED, StubTagger, quiet_noise, run,  # noqa: E402
                           store_clip)


def _key_before_lanes(ck, name, ver, sha):
    h = hashlib.sha256()
    for part in ("tag", ck, name, ver, sha or ""):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


class TestLanesLeaveTheOriginalStoreAlone:

    def test_the_default_key_is_the_key_written_before_lanes(self):
        assert TAGS.tag_key("c", "m", "v", "s") == _key_before_lanes("c", "m", "v", "s")
        assert TAGS.tag_key("c", "m", "v", "s", "pad10") != _key_before_lanes("c", "m", "v", "s")

    def test_each_lane_has_its_own_file(self, tmp_path):
        assert TAGS.tags_path(str(tmp_path)).endswith(os.path.join("clips", "tags.jsonl"))
        assert TAGS.tags_path(str(tmp_path), "mn10_pad10").endswith(
            os.path.join("clips", "tags-mn10_pad10.jsonl"))
        with pytest.raises(ValueError):
            TAGS.tags_path(str(tmp_path), "../x")

    def test_the_padded_lane_writes_beside_the_original_not_into_it(self, tmp_path):
        store_clip(tmp_path)
        t0 = run(tmp_path, StubTagger(), lane="mn10")
        t1 = run(tmp_path, StubTagger(), lane="mn10_pad10")
        assert (t0["tagged"], t1["tagged"], t1["already_tagged"]) == (1, 1, 0)
        a = list(TAGS.read_tags(str(tmp_path)))
        b = list(TAGS.read_tags(str(tmp_path), "mn10_pad10"))
        assert len(a) == len(b) == 1 and a[0]["tag_key"] != b[0]["tag_key"]
        assert (a[0]["lane"], b[0]["lane"]) == ("mn10", "mn10_pad10")
        assert HT.heartbeat_path(str(tmp_path)).endswith(os.path.join("state", "tag_heartbeat.json"))
        assert os.path.exists(HT.heartbeat_path(str(tmp_path), "mn10_pad10"))
        again = run(tmp_path, StubTagger(), lane="mn10_pad10")
        assert (again["tagged"], again["already_tagged"]) == (0, 1)

    def test_an_unknown_lane_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            run(tmp_path, StubTagger(), lane="nope")

    def test_check_reads_exactly_one_lane(self, tmp_path):
        assert HT.main(["--pool", str(tmp_path), "--check",
                        "--lane", "mn10", "--lane", "mn10_pad10"]) == 2


class TestThePaddedLane:

    def test_the_clip_is_normalised_then_zero_padded_to_ten_seconds(self, tmp_path):
        store_clip(tmp_path)
        whole, padded = StubTagger(), StubTagger()
        run(tmp_path, whole, lane="mn10")
        run(tmp_path, padded, lane="mn10_pad10")
        n = len(whole.calls[0])
        x = padded.calls[0]
        assert len(x) == 10 * HT.MODEL_FS_HZ > n
        assert abs(HT.dbfs(x[:n]) - HT.TARGET_DBFS) < 0.1, "normalise before padding"
        assert not np.any(x[n:])
        assert np.array_equal(x[:n], whole.calls[0])

    def test_the_row_says_how_it_was_fed(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger(), lane="mn10_pad10")
        run(tmp_path, StubTagger(), lane="mn10")
        assert next(TAGS.read_tags(str(tmp_path), "mn10_pad10"))["input_pad_to_s"] == 10.0
        assert next(TAGS.read_tags(str(tmp_path)))["input_pad_to_s"] is None


class TestOnlyTheKnownHeaderLieIsRecovered:
    """The clip writer stamped the nominal rate over 48 kHz audio. That one lie closes to exactly
    one clip at x3; nothing else may be laundered through the same path."""

    def _tag(self, tmp_path, n, fs, fs_csv=16000.0):
        row = store_clip(tmp_path, pcm=quiet_noise(n=n), fs=fs, fs_csv=fs_csv)
        return HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))

    @pytest.mark.parametrize("fs", [16000, 15990, 16005])
    def test_48k_audio_under_a_nominal_header_is_tagged_with_the_lie_kept(self, tmp_path, fs):
        got = self._tag(tmp_path, 240000, fs)
        assert got["ok"], got
        r = got["row"]
        assert r["wav_header_fs_hz"] == fs and r["fs_source_hz"] == 48000.0
        assert r["rate_recovered"] == {"header_fs_hz": fs, "factor": 3, "fs_hz": 48000.0}

    def test_a_16k_era_clip_stays_refused_as_a_rate_problem(self, tmp_path):
        got = self._tag(tmp_path, 64000, 16000)
        assert not got["ok"] and got["reason"] == HT.R_RATE_REFUSED
        assert "1.333 s" in got["detail"]

    @pytest.mark.parametrize("fs", [22848, 22624])
    def test_machs_bad_boot_stays_refused(self, tmp_path, fs):
        got = self._tag(tmp_path, 64000, fs, fs_csv=float(fs))
        assert not got["ok"] and got["reason"] == HT.R_RATE_REFUSED
        assert str(fs) in got["detail"]

    def test_a_correct_header_is_not_recorded_as_recovered(self, tmp_path):
        row = store_clip(tmp_path)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"] and got["row"]["rate_recovered"] is None


class TestTheHeardSummary:

    def test_coarse_skips_parents_and_takes_the_first_named_group(self):
        assert HT.coarse({"Animal": 0.5, "Dog": 0.3, "Speech": 0.2}) == "dog"
        assert HT.coarse({"Speech": 0.4}) == "speech"
        assert HT.coarse({"Animal": 0.4}) == "none"
        assert HT.coarse({"Accordion": 0.4, "Dog": 0.1}) == "other"

    def test_the_groups_do_not_overlap(self):
        seen = {}
        for g, names in HT.COARSE:
            for n in names:
                assert n not in seen, "%r is in %s and %s" % (n, seen.get(n), g)
                seen[n] = g
        assert not HT.COARSE_SKIP & set(seen)

    def test_a_run_counts_what_it_heard_and_the_check_prints_it(self, tmp_path):
        store_clip(tmp_path, node="mach")
        t = run(tmp_path, StubTagger())
        assert t["heard"] == {"mach": {"insects": 1}}
        _code, lines = HT.check_tags(str(tmp_path), now=t["at"])
        assert lines[0].split() == ["lane", "mn10"]
        assert any(l.startswith("heard") and "mach insects 1" in l for l in lines), lines
