"""SD retention is a rolling cache policy, not an archive policy."""
import ctypes
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
POLICY = ROOT / "firmware" / "hear_node" / "sd_retention_policy.h"


def _code() -> str:
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//")[0] for line in src.splitlines())


def _body(name: str) -> str:
    src = _code()
    i = src.index(name + "(")
    i = src.index("{", i)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j]
    raise AssertionError("unclosed body for %s" % name)


@pytest.fixture(scope="module")
def policy(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH")
    d = tmp_path_factory.mktemp("sd_retention")
    (d / "w.c").write_text(
        '#include "%s"\n' % POLICY
        + "unsigned long long w_target(unsigned long long total){return sd_cache_target_free_bytes(total);}\n"
          "int w_classify(const char *path, int is_dir){return sd_cache_classify_path(path, is_dir);}\n"
    )
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_target.argtypes = [ctypes.c_ulonglong]
    lib.w_target.restype = ctypes.c_ulonglong
    lib.w_classify.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.w_classify.restype = ctypes.c_int
    return lib


def test_target_free_space_scales_with_card_size(policy):
    mib = 1024 * 1024
    assert policy.w_target(239 * mib) // mib == 23
    assert policy.w_target(30 * 1024 * mib) // mib == 3072
    assert policy.w_target(24 * mib) // mib == 6, "tiny cards cap the floor at 25% of capacity"


def test_protected_files_are_not_cache_candidates(policy):
    protected = 0
    rolling = 1
    unknown_rolling = 2
    for path in (b"/gate.cfg", b"/gps.cfg", b"/provisioning.json", b"/wifi.state",
                 b"/secrets.h", b"/device.key"):
        assert policy.w_classify(path, 0) == protected
    for path in (b"/scene-20260915.csv", b"/scene.csv", b"/dets.csv",
                 b"/health-prev.csv", b"/clips/rankine-00002aabc123-0000000100.wav",
                 b"/node.log"):
        assert policy.w_classify(path, 0) == rolling
    assert policy.w_classify(b"/kernel.img", 0) == unknown_rolling
    assert policy.w_classify(b"/System Volume Information", 1) == protected


def test_oldest_first_ordering_is_encoded_across_cache_categories():
    k = _body("static void sd_cache_key")
    keys = ["00-%s", "10-scene-legacy-%s", "20-%.*s-0-%s", "20-%.*s-1-%s",
            "30-%s", "40-clip-%u-%08lx-%010lu-%s", "90-%s"]
    assert all('"%s"' % x in k for x in keys)
    assert keys == sorted(keys), "lexicographic key prefixes are the oldest-first order"
    p = _body("static int sd_cache_prune")
    assert "qsort(" in p and "sd_cache_ent_cmp" in p
    assert "SD.remove(ents[i].path)" in p


def test_writers_prune_before_opening_large_or_continuous_files():
    clip = _body("static void clip_pump")
    assert clip.index("sd_cache_prune(CLIP_BYTES") < clip.index("SD.open(path, FILE_WRITE)")
    scene = _body("static void scene_emit")
    assert scene.index("sd_cache_prune(") < scene.index("csv_open(path, prev, SCENE_HDR)")
    det = _body("static void det_flush")
    assert det.index("sd_cache_prune(") < det.index('csv_open("/dets.csv"')
    loop = _body("void loop")
    assert loop.index("sd_cache_prune(4096u") < loop.index('csv_open("/health.csv"')


def test_the_hardcoded_64_mib_scene_only_pruner_is_gone():
    src = _code()
    assert "KEEP_FREE_MB" not in src
    assert "prune_oldest(" not in src
    assert "sd_cache_prune(" in src
    assert "sd_cache_classify_path" in src
