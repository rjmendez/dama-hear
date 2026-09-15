"""Boot mic probe settle window.

The self-test used to take ONE 768-sample read the instant i2s.begin() returned and latch its
verdict for the whole boot. An ICS-43434-class part is not driving data that early, so a healthy
node booted cold reported `capture-failure` (ageev: all-zero probe) or `stuck` (kasami: 98%
repeated samples) while its live 48 kHz audio, I2S counters and detections stayed healthy.

These tests drive mic_probe_settle() with a scripted fake microphone: no hardware, no Arduino.
They cover the three behaviours the fix has to have at once -- cold zeros then valid capture must
settle healthy, a microphone that never wakes must still fail, and the existing single-read
classification of normal/marginal input must not move.
"""
import ctypes
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "mic_diagnostics.h"
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"

BLOCK = 768  # ABLOCK on the shipping boards: BLOCK 256 * DECIM 3, i.e. 16 ms at 48 kHz


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
        ("attempts", ctypes.c_uint32),
        ("settle_ms", ctypes.c_uint32),
    ]


READ_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16),
                           ctypes.c_size_t)
NOW_FN = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
WAIT_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)


class ProbeCfg(ctypes.Structure):
    _fields_ = [
        ("read", READ_FN),
        ("now_ms", NOW_FN),
        ("wait_ms", WAIT_FN),
        ("ctx", ctypes.c_void_p),
        ("buf", ctypes.POINTER(ctypes.c_int16)),
        ("buf_samples", ctypes.c_size_t),
        ("settle_budget_ms", ctypes.c_uint32),
        ("retry_gap_ms", ctypes.c_uint32),
        ("max_attempts", ctypes.c_uint32),
    ]


@pytest.fixture(scope="module")
def micprobe(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: mic_diagnostics.h could not be compiled")
    d = tmp_path_factory.mktemp("mic_probe")
    (d / "w.c").write_text(
        '#include "%s"\n' % HDR
        + "void w_settle(const mic_probe_cfg_t *cfg, mic_diag_t *out){ *out = mic_probe_settle(cfg); }\n"
          "uint32_t w_budget(int cold){ return mic_probe_budget_ms(cold); }\n"
          "int w_healthy(int state){ return mic_diag_state_is_healthy((mic_diag_state_t)state); }\n"
    )
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_settle.argtypes = [ctypes.POINTER(ProbeCfg), ctypes.POINTER(MicDiag)]
    lib.w_settle.restype = None
    lib.w_budget.argtypes = [ctypes.c_int]
    lib.w_budget.restype = ctypes.c_uint32
    lib.w_healthy.argtypes = [ctypes.c_int]
    lib.w_healthy.restype = ctypes.c_int
    return lib


class FakeMic:
    """A microphone that hands out scripted blocks, one per read, holding the last one forever.

    `elapsed_ms` advances only when the probe waits or reads, so the test clock is exactly the
    boot time the firmware would spend -- no wall-clock flakiness.
    """

    def __init__(self, blocks, read_cost_ms=16, short_reads=()):
        self.blocks = [list(b) for b in blocks]
        self.read_cost_ms = read_cost_ms
        self.short_reads = set(short_reads)
        self.reads = 0
        self.waits = []
        self.now = 0

    def read(self, _ctx, dst, cap):
        idx = min(self.reads, len(self.blocks) - 1)
        self.reads += 1
        self.now += self.read_cost_ms
        if (self.reads - 1) in self.short_reads:
            return 0
        block = self.blocks[idx]
        n = min(len(block), cap)
        for i in range(n):
            dst[i] = ctypes.c_int16(block[i]).value
        return n

    def now_ms(self, _ctx):
        return self.now

    def wait(self, _ctx, ms):
        self.waits.append(ms)
        self.now += ms

    @property
    def elapsed_ms(self):
        return self.now


def _settle(lib, mic, budget_ms=750, gap_ms=20, max_attempts=16, buf_samples=BLOCK):
    buf = (ctypes.c_int16 * buf_samples)()
    cfg = ProbeCfg()
    cfg.read = READ_FN(mic.read)
    cfg.now_ms = NOW_FN(mic.now_ms)
    cfg.wait_ms = WAIT_FN(mic.wait)
    cfg.ctx = None
    cfg.buf = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int16))
    cfg.buf_samples = buf_samples
    cfg.settle_budget_ms = budget_ms
    cfg.retry_gap_ms = gap_ms
    cfg.max_attempts = max_attempts
    out = MicDiag()
    lib.w_settle(ctypes.byref(cfg), ctypes.byref(out))
    return out


def _zeros():
    return [0] * BLOCK


def _repeating(value=-3):
    return [value] * BLOCK


def _audio():
    vals = [-180, -75, 10, 120, -60, 210, -95, 70]
    return (vals * (BLOCK // len(vals) + 1))[:BLOCK]


def _quiet():
    vals = [0, 1, 0, -1, 1, 0, -1, 0]
    return (vals * (BLOCK // len(vals) + 1))[:BLOCK]


# ---------------------------------------------------------------- the false positive itself

def test_cold_all_zero_block_then_real_audio_settles_normal(micprobe):
    """ageev's boot: the first DMA block is zeros, the mic wakes, audio follows."""
    mic = FakeMic([_zeros(), _audio()])
    got = _settle(micprobe, mic)
    assert got.state_name == b"normal"
    assert got.legacy == b"ok"
    assert got.attempts == 2
    assert mic.reads == 2


def test_cold_repeating_block_then_real_audio_settles_normal(micprobe):
    """kasami's boot: the first block is 98%+ repeated samples, not a dead microphone."""
    mic = FakeMic([_repeating(), _repeating(), _audio()])
    got = _settle(micprobe, mic)
    assert got.state_name == b"normal"
    assert got.attempts == 3


def test_a_slow_mic_that_wakes_late_still_settles(micprobe):
    mic = FakeMic([_zeros()] * 8 + [_audio()])
    got = _settle(micprobe, mic)
    assert got.state_name == b"normal"
    assert got.attempts == 9
    assert got.settle_ms <= 750


def test_a_short_first_read_is_retried_not_latched(micprobe):
    """readBytes() returning nothing at all is the other shape of "too early"."""
    mic = FakeMic([_audio()], short_reads=(0,))
    got = _settle(micprobe, mic)
    assert got.state_name == b"normal"
    assert got.attempts == 2


def test_a_quiet_room_is_accepted_immediately(micprobe):
    mic = FakeMic([_quiet()])
    got = _settle(micprobe, mic)
    assert got.state_name == b"quiet"
    assert got.legacy == b"ok"
    assert got.attempts == 1
    assert got.settle_ms == 0 or got.settle_ms <= 16


# ---------------------------------------------------------------- real faults still fail

def test_a_microphone_that_never_wakes_is_still_a_capture_failure(micprobe):
    mic = FakeMic([_zeros()])
    got = _settle(micprobe, mic)
    assert got.state_name == b"capture-failure"
    assert got.reason == b"all_zero_samples"
    assert got.legacy == b"silent"
    assert got.attempts > 1, "a dead mic must be retried, not latched on the first read"


def test_a_stuck_microphone_is_still_stuck(micprobe):
    mic = FakeMic([_repeating(7)])
    got = _settle(micprobe, mic)
    assert got.state_name == b"stuck"
    assert got.legacy == b"silent"


def test_an_absent_microphone_that_never_returns_samples_is_a_capture_failure(micprobe):
    mic = FakeMic([_zeros()], short_reads=range(64))
    got = _settle(micprobe, mic)
    assert got.state_name == b"capture-failure"
    assert got.reason == b"no_samples"


def test_a_floating_pin_is_still_floating(micprobe):
    mic = FakeMic([([-1, 1] * (BLOCK // 2))])
    got = _settle(micprobe, mic)
    assert got.state_name == b"floating"
    assert got.legacy == b"silent"


# ---------------------------------------------------------------- the window is bounded

def test_the_settle_window_is_bounded_in_time_and_attempts(micprobe):
    mic = FakeMic([_zeros()])
    got = _settle(micprobe, mic, budget_ms=750, gap_ms=20)
    assert mic.elapsed_ms <= 750 + 16, "boot spent longer than the declared settle budget"
    assert got.attempts <= 16
    assert got.settle_ms <= 750


def test_a_zero_budget_is_the_old_single_read_behaviour(micprobe):
    mic = FakeMic([_zeros(), _audio()])
    got = _settle(micprobe, mic, budget_ms=0)
    assert got.attempts == 1
    assert got.state_name == b"capture-failure"


def test_a_warm_restart_budget_is_shorter_than_a_cold_one(micprobe):
    warm = micprobe.w_budget(0)
    cold = micprobe.w_budget(1)
    assert 0 < warm < cold
    assert cold <= 1000, "a cold boot must not spend a second of boot time on the mic probe"


def test_a_healthy_mic_costs_a_warm_restart_nothing(micprobe):
    """OTA restart: the part kept its supply and answers the first read."""
    mic = FakeMic([_audio()])
    got = _settle(micprobe, mic, budget_ms=micprobe.w_budget(0))
    assert got.attempts == 1
    assert mic.waits == []


def test_only_quiet_and_normal_end_the_window(micprobe):
    # capture-failure, stuck, floating, saturated are all shapes a not-yet-awake part produces.
    assert [micprobe.w_healthy(s) for s in range(6)] == [0, 0, 0, 1, 0, 1]


# ---------------------------------------------------------------- the firmware wires it up

def test_the_boot_probe_uses_the_settle_window():
    code = re.sub(r"/\*.*?\*/", "", INO.read_text(), flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    probe = code[code.index("static void selftest_mic_probe()"):]
    probe = probe[:probe.index("\n}\n") + 3]
    assert "mic_probe_settle(&cfg)" in probe, "the boot probe no longer goes through the settle window"
    assert "i2s.readBytes" not in probe, "the boot probe still latches one raw read"
    assert "mic_probe_budget_ms(mic_probe_cold_boot())" in probe, (
        "the settle budget must follow cold boot vs OTA restart")
    assert "esp_reset_reason()" in code


def test_the_status_contract_reports_how_the_verdict_was_reached():
    code = INO.read_text()
    for token in (r'\"attempts\":%lu', r'\"settle_ms\":%lu',
                  "selftest_mic_diag.attempts", "selftest_mic_diag.settle_ms"):
        assert token in code, token
