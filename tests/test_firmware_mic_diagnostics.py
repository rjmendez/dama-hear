"""Mic self-test diagnostics: explicit states, backwards-compatible legacy field, and no quiet=false failure."""
import ctypes
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "mic_diagnostics.h"
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"


class MicDiag(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("state_name", ctypes.c_char_p),
        ("legacy", ctypes.c_char_p),
        ("reason", ctypes.c_char_p),
        ("samples", ctypes.c_uint32),
        ("lo", ctypes.c_int16),
        ("hi", ctypes.c_int16),
        ("mean", ctypes.c_int32),
        ("span", ctypes.c_uint32),
        ("mean_abs", ctypes.c_uint32),
        ("zero_cross_pct", ctypes.c_uint32),
        ("same_adj_pct", ctypes.c_uint32),
        ("unique", ctypes.c_uint32),
        ("sat_pct", ctypes.c_uint32),
        ("rail_hits", ctypes.c_uint32),
    ]


@pytest.fixture(scope="module")
def micdiag(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: mic_diagnostics.h could not be compiled")
    d = tmp_path_factory.mktemp("mic_diag")
    (d / "w.c").write_text(
        '#include "%s"\n' % HDR
        + "void w_diag(const int16_t *samples, size_t n, mic_diag_t *out){ *out = mic_diag_classify(samples, n); }\n"
          "void w_missing(mic_diag_t *out){ *out = mic_diag_missing(\"no_samples\"); }\n"
    )
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_diag.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.POINTER(MicDiag)]
    lib.w_diag.restype = None
    lib.w_missing.argtypes = [ctypes.POINTER(MicDiag)]
    lib.w_missing.restype = None
    return lib


def _classify(lib, values):
    arr = (ctypes.c_int16 * len(values))(*values) if values else None
    out = MicDiag()
    lib.w_diag(arr, len(values), ctypes.byref(out))
    return out


def _missing(lib):
    out = MicDiag()
    lib.w_missing(ctypes.byref(out))
    return out


def test_missing_samples_are_a_capture_failure(micdiag):
    got = _missing(micdiag)
    assert got.state_name == b"capture-failure"
    assert got.legacy == b"silent"
    assert got.reason == b"no_samples"


def test_all_zero_samples_are_not_misreported_as_quiet(micdiag):
    got = _classify(micdiag, [0] * 64)
    assert got.state_name == b"capture-failure"
    assert got.reason == b"all_zero_samples"


def test_a_constant_nonzero_probe_is_stuck(micdiag):
    got = _classify(micdiag, [7] * 64)
    assert got.state_name == b"stuck"
    assert got.reason == b"constant_sample"
    assert got.legacy == b"silent"


def test_a_two_level_toggle_is_floating(micdiag):
    got = _classify(micdiag, [-1, 1] * 64)
    assert got.state_name == b"floating"
    assert got.reason == b"two_level_toggle"
    assert got.legacy == b"silent"


def test_a_quiet_probe_stays_healthy_for_the_legacy_gate(micdiag):
    got = _classify(micdiag, [0, 1, 0, -1, 1, 0, -1, 0] * 16)
    assert got.state_name == b"quiet"
    assert got.reason == b"low_variation"
    assert got.legacy == b"ok"
    assert got.mean_abs <= 2


def test_a_narrow_noisy_probe_is_called_floating(micdiag):
    vals = [-12, 9, -11, 8, -10, 7, -9, 6, -8, 5, -7, 4, -6, 3, -5, 2] * 8
    got = _classify(micdiag, vals)
    assert got.state_name == b"floating"
    assert got.reason == b"narrow_noisy_span"
    assert got.zero_cross_pct >= 20
    assert got.unique >= 12


def test_rail_hits_are_saturated(micdiag):
    got = _classify(micdiag, [32767, -32768] * 64)
    assert got.state_name == b"saturated"
    assert got.reason == b"rail_hits"
    assert got.legacy == b"saturated"
    assert got.sat_pct >= 90


def test_real_variation_is_normal(micdiag):
    vals = [-180, -75, 10, 120, -60, 210, -95, 70] * 16
    got = _classify(micdiag, vals)
    assert got.state_name == b"normal"
    assert got.reason == b"signal_variation_present"
    assert got.legacy == b"ok"
    assert got.span > 64


def test_status_contract_carries_legacy_and_explicit_mic_fields():
    code = re.sub(r"/\*.*?\*/", "", INO.read_text(), flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    assert "selftest_mic_set_diag(mic_diag_classify" in code
    for token in (r'\"mic\":\"%s\"', r'\"mic_state\":\"%s\"', r'\"mic_reason\":\"%s\"',
                  r'\"mic_stats\":{\"samples\":%lu', r'\"zero_cross_pct\":%lu',
                  r'\"same_adj_pct\":%lu', r'\"unique\":%lu', r'\"sat_pct\":%lu',
                  r'\"rail_hits\":%lu'):
        assert token in code, token
