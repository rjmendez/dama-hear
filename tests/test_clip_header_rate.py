"""A clip's WAV header must state the rate its body was written at: FS_ACQ, not FS_NOMINAL.

Comments are stripped before scanning, so a guard cannot pass by matching its own prose.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
sys.path.insert(0, str(ROOT))

import hear.clips as CLIPS  # noqa: E402


def _code():
    src = re.sub(r"/\*.*?\*/", "", INO.read_text(), flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def test_every_wav_header_call_scales_the_decimated_rate_by_decim():
    calls = re.findall(r"wav_header\s*\(([^;]*?)\)\s*;", _code(), flags=re.S)
    assert len(calls) >= 2, calls
    for c in calls:
        if "fsu" in c:
            assert "fsu * DECIM" in c or "fsu*DECIM" in c, c


def test_the_clip_writer_stamps_the_acquisition_rate():
    m = re.search(r"wav_header\s*\(\s*hdr\s*,\s*CLIP_SAMPLES\s*\*\s*2\s*,(.*?)\)\s*;",
                  _code(), flags=re.S)
    assert m and "DECIM" in m.group(1)


def test_clip_samples_are_counted_at_the_acquisition_rate():
    code = _code()
    assert re.search(r"#define\s+CLIP_PRE_SAMPLES\s+\(\(uint32_t\)FS_ACQ\)", code)
    assert re.search(r"#define\s+CLIP_POST_SAMPLES\s+\(\(uint32_t\)\(4 \* FS_ACQ\)\)", code)


def test_the_python_side_agrees_on_the_clip_length():
    assert (CLIPS.CLIP_PRE_S, CLIPS.CLIP_POST_S, CLIPS.CLIP_TOTAL_S) == (1.0, 4.0, 5.0)
