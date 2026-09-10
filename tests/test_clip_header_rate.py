"""A clip's WAV header must state the rate its body was written at.

⚠️THIS IS A HEADSTONE. The 48 kHz rollout moved the clip body to FS_ACQ and left the header on
`fs_timebase()`, which is the FS_NOMINAL-domain rate. Every clip written between the 48 kHz flash
and this fix says ~16000 Hz over 48 kHz audio: a 5.0 s clip reads back as 15.0 s and plays an
octave and a half low. Seven of them are on the PVC.

⚠️IT COULD NOT BE CAUGHT DOWNSTREAM AND THAT IS THE WORST PART. hear_tag.py's assert_rate refuses
a clip whose header is not 16 kHz -- so a header that lies by saying exactly 16000 is the one
wrong rate the guard is blind to, and the audio would have gone into YAMNet 3x too slow, degrading
silently towards Silence with every counter reading healthy. /praw got the `* DECIM` at the same
time and the clip writer did not; nobody owned the seam.

⚠️COMMENTS ARE STRIPPED BEFORE SCANNING. A guard that greps its own prose passes against broken
code; this repo has shipped that bug twice.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "night_node" / "night_node.ino"
sys.path.insert(0, str(ROOT))

import hear.clips as CLIPS  # noqa: E402


def _code():
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


class TestTheFirmwareStampsTheAcquisitionRate:

    def test_every_wav_header_call_scales_an_fs_nominal_rate_by_decim(self):
        """⚠️Both writers, not just the one that broke. The clip writer and /praw both serialise
        FS_ACQ audio; a guard aimed only at the clip writer goes blind the next time one is
        added."""
        calls = re.findall(r"wav_header\s*\(([^;]*?)\)\s*;", _code(), flags=re.S)
        assert len(calls) >= 2, calls
        for c in calls:
            if "fsu" not in c:
                continue
            assert "fsu * DECIM" in c or "fsu*DECIM" in c, (
                "wav_header(%s) stamps the FS_NOMINAL timebase over acquisition-rate audio" % c)

    def test_the_clip_writer_specifically(self):
        m = re.search(r"wav_header\s*\(\s*hdr\s*,\s*CLIP_SAMPLES\s*\*\s*2\s*,(.*?)\)\s*;",
                      _code(), flags=re.S)
        assert m, "clip writer no longer calls wav_header(hdr, CLIP_SAMPLES * 2, ...)"
        assert "DECIM" in m.group(1), m.group(1)

    def test_clip_samples_is_counted_at_the_acquisition_rate(self):
        """If CLIP_PRE/POST_SAMPLES ever move back to FS_NOMINAL the `* DECIM` above becomes the
        bug instead of the fix, so the two are pinned together."""
        code = _code()
        assert re.search(r"#define\s+CLIP_PRE_SAMPLES\s+\(\(uint32_t\)FS_ACQ\)", code)
        assert re.search(r"#define\s+CLIP_POST_SAMPLES\s+\(\(uint32_t\)\(4 \* FS_ACQ\)\)", code)

    def test_the_python_side_knows_the_same_post_roll(self):
        """The firmware writes 1.0 + 4.0 s. CLIP_POST_S is only a fallback now, but a fallback
        that disagrees with the shipping firmware is a wrong answer waiting for a row with no
        body."""
        assert CLIPS.CLIP_PRE_S == 1.0
        assert CLIPS.CLIP_POST_S == 4.0
        assert 5.0 in CLIPS.CLIP_GEOMETRIES_S and 4.0 in CLIPS.CLIP_GEOMETRIES_S


class TestTheAlreadyWrittenClipsAreRecoverable:
    """The seven mis-headed clips on the PVC cannot be rewritten, so they have to be readable."""

    def test_the_misheaded_48k_clip_is_detected_and_corrected(self):
        fix = CLIPS.header_rate_suspect({"fs_hz": 16000, "dur_s": 15.0})
        assert fix and fix["decim"] == 3
        assert fix["true_fs_hz"] == 48000.0
        assert fix["true_dur_s"] == pytest.approx(5.0)

    def test_it_survives_the_nodes_real_clock_drift(self):
        """Header rates on the pool span 15968-16050 Hz; the correction cannot need exactly
        16000."""
        for fs in (15968, 15991, 16005, 16050):
            fix = CLIPS.header_rate_suspect({"fs_hz": fs, "dur_s": 240000 / float(fs)})
            assert fix and fix["decim"] == 3, fs

    def test_an_honest_16k_era_clip_is_left_alone(self):
        assert CLIPS.header_rate_suspect({"fs_hz": 16000, "dur_s": 4.0}) is None
        assert CLIPS.clip_total_s({"bytes": 128044, "wav_header_fs_hz": 16000}) == 4.0

    def test_a_correctly_headed_48k_clip_is_left_alone(self):
        assert CLIPS.header_rate_suspect({"fs_hz": 48000, "dur_s": 5.0}) is None
        assert CLIPS.clip_total_s({"bytes": 480044, "wav_header_fs_hz": 48000}) == 5.0

    def test_machs_22624_hz_boot_is_not_silently_reinterpreted(self):
        """mach shipped a whole boot headed 22624 Hz over 16 kHz audio. That is a DIFFERENT bug
        and this correction must not launder it into a plausible rate."""
        assert CLIPS.header_rate_suspect({"fs_hz": 22624, "dur_s": 4.0}) is None
        assert CLIPS.header_rate_suspect({"fs_hz": 22848, "dur_s": 4.0}) is None

    def test_an_ambiguous_reading_is_refused_rather_than_guessed(self):
        """⚠️MEASURED REGRESSION. Written first against a plausible RANGE, a 15.0 s body matched
        /2 (7.5 s at 32 kHz) before it matched the correct /3, and the first hit was returned.
        Requiring a length this fleet actually writes is what disambiguates; a reading that two
        factors both satisfy must return None."""
        assert CLIPS.header_rate_suspect({"fs_hz": 16000, "dur_s": 15.0})["decim"] == 3
        # 16 s claims 8.0 s at /2 and 5.33 s at /3 -- neither is a geometry, so: nothing.
        assert CLIPS.header_rate_suspect({"fs_hz": 16000, "dur_s": 16.0}) is None

    def test_a_short_clip_is_never_corrected(self):
        """Correction only ever applies above CLIP_PLAUSIBLE_MAX_S. Anything a clip could really
        be keeps the rate it claims."""
        for d in (0.5, 4.0, 5.0, 8.0):
            assert CLIPS.header_rate_suspect({"fs_hz": 16000, "dur_s": d}) is None


class TestTheWindowComesFromTheClipNotAConstant:

    def test_both_geometries_yield_their_own_post_roll(self):
        import hear.tags as TAGS
        old = {"sample": 1000000, "bytes": 128044, "wav_header_fs_hz": 16000, "dur_s": 4.0}
        new = {"sample": 1000000, "bytes": 480044, "wav_header_fs_hz": 48000, "dur_s": 5.0}
        assert TAGS.sample_window(old)["post_s"] == pytest.approx(3.0)
        assert TAGS.sample_window(new)["post_s"] == pytest.approx(4.0)

    def test_a_row_with_no_body_falls_back_and_says_so(self):
        import hear.tags as TAGS
        w = TAGS.sample_window({"sample": 1000000})
        assert w["post_s"] == TAGS.CLIP_POST_S

    def test_the_index_row_end_time_follows_the_clip_length(self):
        import struct

        def wav(n, fs):
            d = b"\0\0" * n
            return (b"RIFF" + struct.pack("<I", 36 + len(d)) + b"WAVEfmt "
                    + struct.pack("<I", 16) + struct.pack("<HHIIHH", 1, 1, fs, fs * 2, 2, 16)
                    + b"data" + struct.pack("<I", len(d)) + d)

        for n, fs, post in ((64000, 16000, 3.0), (240000, 48000, 4.0)):
            body = wav(n, fs)
            row = CLIPS.index_row(
                clip="/clips/nyquist-db21acd5-0000000100.wav",
                parts=CLIPS.parse_clip_name("/clips/nyquist-db21acd5-0000000100.wav"),
                node="nyquist", body=body, probe=CLIPS.wav_probe(body),
                dets={"utc_us": 1788997850800000, "ts_utc_s": 1788997850.8, "anchored": True,
                      "uptime_s": 1, "fs_hz": 16000.0, "trigger": "lf", "clip_why": "ok",
                      "dets_origin": "nyquist:/dets.csv", "record_key": "aa" * 16},
                path="clips/2026-09-10/nyquist/x.wav", outcome="stored", fetched_at=1.0)
            assert row["t_end_utc_s"] - row["ts_utc_s"] == pytest.approx(post), (n, fs)
            assert row["ts_utc_s"] - row["t_start_utc_s"] == pytest.approx(1.0)

    def test_the_misheaded_clip_gets_the_right_window_not_a_15_second_one(self):
        import struct
        d = b"\0\0" * 240000
        body = (b"RIFF" + struct.pack("<I", 36 + len(d)) + b"WAVEfmt " + struct.pack("<I", 16)
                + struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", len(d)) + d)
        row = CLIPS.index_row(
            clip="/clips/nyquist-db21acd5-0000000100.wav",
            parts=CLIPS.parse_clip_name("/clips/nyquist-db21acd5-0000000100.wav"),
            node="nyquist", body=body, probe=CLIPS.wav_probe(body),
            dets={"utc_us": 1788997850800000, "ts_utc_s": 1788997850.8, "anchored": True,
                  "uptime_s": 1, "fs_hz": 16000.0, "trigger": "lf", "clip_why": "ok",
                  "dets_origin": "nyquist:/dets.csv", "record_key": "aa" * 16},
            path="clips/2026-09-10/nyquist/x.wav", outcome="stored", fetched_at=1.0)
        assert row["dur_s"] == pytest.approx(5.0)
        assert row["t_end_utc_s"] - row["ts_utc_s"] == pytest.approx(4.0)
        assert row["header_rate_suspect"]["true_fs_hz"] == 48000.0
        assert row["wav_header_fs_hz"] == 16000, "the lying header is KEPT, not overwritten"
