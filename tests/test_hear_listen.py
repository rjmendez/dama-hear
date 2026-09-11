"""The listening tool's two load-bearing properties: audible output, honest measurements."""

import json
import math
import os
import struct
import subprocess
import sys
import wave

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "hear_listen.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import hear_listen as HL  # noqa: E402


def _wav(path, samples, fs=48000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def _tone(n, amp, fs=48000, f=440.0):
    return [int(round(amp * math.sin(2 * math.pi * f * i / fs))) for i in range(n)]


def _pool(tmp_path, rows):
    d = tmp_path / "corpus" / "clips"
    idx = []
    for r in rows:
        p = d / os.path.dirname(r["path"].split("/", 1)[1])
        p.mkdir(parents=True, exist_ok=True)
        _wav(str(tmp_path / "corpus" / r["path"]), r.pop("_samples"), r.pop("_fs", 48000))
        idx.append(r)
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "index.jsonl"), "w") as f:
        for r in idx:
            f.write(json.dumps(r) + "\n")
    return str(tmp_path)


def _row(node, day, name, samples, **kw):
    r = {"outcome": "stored", "node": node, "clip_key": name,
         "path": "clips/%s/%s/%s.wav" % (day, node, name), "ts_utc_s": 1788987605.0,
         "anchored": True, "trigger": "175", "wav_header_fs_hz": 48000, "fs_hz": 48000.0,
         "boot": "aaaaaaaa", "_samples": samples}
    r.update(kw)
    return r


def test_staged_copy_is_actually_audible(tmp_path):
    """The whole point. A -57 dBFS clip must leave here near -20 dBFS RMS.

    ⚠️This is the property the gate needed and did not have. A tool that copied the files out
    unchanged would pass every other check here and still be useless: the operator would play
    silence and write "nothing" on 30 rows.
    """
    quiet = _tone(240000, 45)   # ~ -57 dBFS
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", quiet)])
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "1",
                    "--seed", "1"], check=True, capture_output=True)
    m = json.load(open(os.path.join(out, "manifest.json")))
    c = m["clips"][0]
    assert c["rms_dbfs_orig"] < -50.0
    a, fs, _ = HL.read_pcm(os.path.join(out, c["loud"]))
    rms = math.sqrt(sum(s * s for s in a) / len(a))
    assert HL.RMS_TARGET_DBFS - 1.0 <= HL.dbfs(rms) <= HL.RMS_TARGET_DBFS + 1.0


def test_original_is_copied_byte_identical(tmp_path):
    """The measurement substrate must survive. --max-silence-frac is derived from it."""
    quiet = _tone(240000, 45)
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", quiet)])
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "1",
                    "--seed", "1"], check=True, capture_output=True)
    src = os.path.join(pool, "corpus", "clips", "2026-09-10", "mach", "a.wav")
    assert open(src, "rb").read() == open(os.path.join(out, "a.wav"), "rb").read()


def test_manifest_levels_describe_the_original_not_the_loud_copy(tmp_path):
    """⚠️The circularity guard. Reporting the normalised level would make every clip look like
    a -20 dBFS recording and would set the silence threshold from the tool's own gain."""
    quiet = _tone(240000, 45)
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", quiet)])
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "1",
                    "--seed", "1"], check=True, capture_output=True)
    c = json.load(open(os.path.join(out, "manifest.json")))["clips"][0]
    assert c["rms_dbfs_orig"] < -50.0, "reported the loud copy's level"
    assert c["gain_db"] > 25.0


def test_impulse_is_not_clipped_by_rms_normalisation(tmp_path):
    """A crack against a quiet floor has a huge crest factor; RMS-to--20 would square it off.
    The transient is the part worth hearing, so peak headroom bounds the gain."""
    s = [0] * 240000
    for i in range(119900, 120100):
        s[i] = 30000 if i % 2 else -30000
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", s)])
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "1",
                    "--seed", "1"], check=True, capture_output=True)
    c = json.load(open(os.path.join(out, "manifest.json")))["clips"][0]
    assert c["gain_bound_by"] == "peak"
    assert c["clipped_samples"] == 0


def test_sample_is_balanced_by_node_not_by_day_count(tmp_path):
    """⚠️Measured regression: cell-balancing gave the longest-running node 16 of 30 because it
    had three day-directories to another node's one."""
    rows = []
    for d in ("2026-09-08", "2026-09-09", "2026-09-10"):
        for i in range(20):
            rows.append(_row("mach", d, "m-%s-%d" % (d, i), _tone(4800, 200)))
    for i in range(20):
        rows.append(_row("rankine", "2026-09-10", "r-%d" % i, _tone(4800, 200)))
    pool = _pool(tmp_path / "p", rows)
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "20",
                    "--seed", "7"], check=True, capture_output=True)
    m = json.load(open(os.path.join(out, "manifest.json")))
    per = {}
    for c in m["clips"]:
        per[c["node"]] = per.get(c["node"], 0) + 1
    assert abs(per["mach"] - per["rankine"]) <= 2, per


def test_required_node_absence_is_reported_not_silent(tmp_path):
    """The gate names mach by name. Staging 30 clips with no mach in them and saying nothing
    would let an operator complete the gate against a set that cannot satisfy it."""
    pool = _pool(tmp_path / "p", [_row("nyquist", "2026-09-10", "a", _tone(4800, 200))])
    out = str(tmp_path / "out")
    r = subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "1",
                        "--seed", "1"], check=True, capture_output=True, text=True)
    assert "mach" in r.stdout
    m = json.load(open(os.path.join(out, "manifest.json")))
    assert m["require_node_unavailable"] == ["mach"]


def test_missing_audio_is_skipped_and_counted(tmp_path):
    """Audio is the prunable thing; an indexed row whose WAV was evicted must not abort the run
    or silently shrink the sample without saying so."""
    rows = [_row("mach", "2026-09-10", "a", _tone(4800, 200)),
            _row("mach", "2026-09-10", "b", _tone(4800, 200))]
    pool = _pool(tmp_path / "p", rows)
    os.remove(os.path.join(pool, "corpus", "clips", "2026-09-10", "mach", "b.wav"))
    out = str(tmp_path / "out")
    r = subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "2",
                        "--seed", "1"], check=True, capture_output=True, text=True)
    m = json.load(open(os.path.join(out, "manifest.json")))
    assert m["staged"] == 1
    assert len(m["skipped"]) == 1
    assert "pruned" in m["skipped"][0]["why"]


def test_sheet_has_one_blank_row_per_staged_clip(tmp_path):
    rows = [_row("mach", "2026-09-10", "c%d" % i, _tone(4800, 200)) for i in range(5)]
    pool = _pool(tmp_path / "p", rows)
    out = str(tmp_path / "out")
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "5",
                    "--seed", "1"], check=True, capture_output=True)
    body = open(os.path.join(out, "sheet.md")).read()
    data = [l for l in body.splitlines() if l.startswith("| ") and not l.startswith("| # ")
            and "---" not in l]
    assert len(data) == 5
    assert all(l.rstrip().endswith("|  |  |  |") for l in data), "sheet arrived pre-filled"


def test_a_full_scale_negative_sample_does_not_read_above_zero_dbfs():
    """int16 full scale is 32768, as hear_tag uses. At 32767 a -32768 sample measured > 0 dBFS."""
    m = HL.measure([-32768] * 4800, 48000)
    assert m["peak_dbfs_orig"] <= 0.0


def test_a_malformed_index_row_is_skipped_not_fatal(tmp_path):
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", _tone(240000, 200))])
    idx = os.path.join(pool, "corpus", "clips", "index.jsonl")
    with open(idx, "a") as fh:
        fh.write('{"outcome": "stored", "node": "mach"}\n')          # no path, no clip_key
        fh.write('{"outcome": "stored", "path": 7, "node": "mach", "clip_key": "x"}\n')
        fh.write("{torn\n")
    out = str(tmp_path / "out")
    r = subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", out, "--n", "5",
                        "--seed", "1"], check=True, capture_output=True, text=True)
    assert "skipped 3 malformed" in r.stdout
    assert json.load(open(os.path.join(out, "manifest.json")))["staged"] == 1


def test_a_non_empty_out_dir_is_refused_unless_forced(tmp_path):
    """Staging into a used directory mixes two runs' clips under one sheet."""
    pool = _pool(tmp_path / "p", [_row("mach", "2026-09-10", "a", _tone(240000, 200))])
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.loud.wav").write_bytes(b"x")
    r = subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", str(out), "--n", "1",
                        "--seed", "1"], capture_output=True, text=True)
    assert r.returncode != 0 and "already holds files" in (r.stderr + r.stdout)
    subprocess.run([sys.executable, TOOL, "--pool", pool, "--out", str(out), "--n", "1",
                    "--seed", "1", "--force"], check=True, capture_output=True)
