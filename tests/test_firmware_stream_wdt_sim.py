"""stream_ready() is compiled and RUN here against a simulated 5 s task watchdog, because the
defect it fixes is a timing property that no source scan can see.

v0.1.5 armed the loop task with loop_wdt_arm(5000) (PR #175). http.handleClient() runs the HTTP
handlers from inside loop(), so every byte of a response goes out between two of loop()'s
boot_wdt_service() calls, and stream_ready() -- the function every long handler gates each chunk
on -- reset nothing. rankine's card held scene-20260915.csv at 11 620 613 B; hear-drain fetches
whole rolled files, that transfer takes tens of seconds over Wi-Fi, and the node panicked with
reset=task_wdt (panic_code 0x0003) a few minutes into every */15 drain window on 2026-09-15
(14:51:05, 15:04:45, 15:18:10), losing the drain each time.

The harness gives the extracted function a virtual millisecond clock, a watchdog that fires when
5 000 virtual ms pass without an esp_task_wdt_reset(), and a socket whose writability, stalls and
disconnects are scripted. Every scenario is also run against a MUTANT of the same function with
the boot_wdt_service() line deleted -- i.e. exactly the shipped v0.1.5 behaviour -- so a
regression that removes the service call is caught by a failing assertion and not by a green run.
"""
import ctypes
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"

SCENE_BYTES = 11620613  # rankine's scene-20260915.csv, raw/ls_rankine.txt


def _code():
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def _fn(src, sig):
    i = src.index(sig)
    i = src.index("{", i)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[src.index(sig):j + 1]
    raise AssertionError("unbalanced block")


def _define(src, name):
    m = re.search(r"#define\s+%s\s+(\S+)" % name, src)
    assert m, name
    return int(m.group(1).rstrip("uU"))


CODE = _code()
STREAM_READY = _fn(CODE, "static bool stream_ready(")
CHUNK_B = _define(CODE, "STREAM_CHUNK_B")
WDT_MS = int(re.search(r"loop_wdt_arm\((\d+)\);", CODE).group(1))
STALL_MS = _define(CODE, "STREAM_STALL_MS")
DEFINES = "#define STREAM_CHUNK_B %u\n#define STREAM_STALL_MS %uu\n" % (CHUNK_B, STALL_MS)

HARNESS = r"""
#include <stdint.h>
#include <stddef.h>

// ---- virtual MCU clock and task watchdog -----------------------------------
static uint32_t sim_now, sim_last_service, sim_max_gap, sim_services;
static int      sim_panic;
static uint32_t sim_wdt_ms = %(wdt)u;

static void sim_charge(uint32_t ms) {          // spend ms of wall time doing something
  for (uint32_t k = 0; k < ms; k++) {
    sim_now++;
    uint32_t gap = sim_now - sim_last_service;
    if (gap > sim_max_gap) sim_max_gap = gap;
    if (gap >= sim_wdt_ms) sim_panic = 1;      // esp_task_wdt would reset the node here
  }
}
static uint32_t millis(void) { return sim_now; }
static void delay(uint32_t ms) { sim_charge(ms); }
static void yield(void) {}
static void esp_task_wdt_reset(void) { sim_services++; sim_last_service = sim_now; }
static bool boot_wdt_live = true;
static void boot_wdt_service(void) { if (boot_wdt_live) esp_task_wdt_reset(); yield(); }

// ---- scripted socket -------------------------------------------------------
static int      sim_connected = 1;
static uint32_t sim_link_gap;        // ms the socket needs before it can take another chunk
static uint32_t sim_next_writable;
static int      sim_never_writable;
static uint32_t sim_poll_ms = 0;
static unsigned long stream_gone_n, stream_stall_n;

struct timeval { long tv_sec, tv_usec; };
typedef struct { int fd; } fd_set;
#define FD_ZERO(p) ((p)->fd = -1)
#define FD_SET(f, p) ((p)->fd = (f))
static int select(int nfds, void *r, fd_set *w, void *e, struct timeval *tv) {
  (void)nfds; (void)r; (void)w; (void)e; (void)tv;
  sim_charge(sim_poll_ms);
  if (sim_never_writable) return 0;
  if (sim_now < sim_next_writable) return 0;
  return 1;
}

class WiFiClient {
 public:
  int fd() { return sim_connected ? 7 : -1; }
  bool connected() { return sim_connected != 0; }
};

// ---- audio pump: real work, but it does not touch the watchdog -------------
static uint32_t sim_pump_ms = 0, sim_pumps;
%(defines)s
static void stream_pump(uint64_t *due) { (void)due; sim_pumps++; sim_charge(sim_pump_ms); }

%(stream_ready)s

// ---- the /sd transfer loop, byte for byte what the handler runs ------------
typedef struct {
  int      panicked;
  uint32_t services, max_gap, elapsed_ms, pumps;
  uint64_t remain;
  unsigned long gone_n, stall_n;
} sim_result;

typedef struct {
  uint64_t bytes;
  uint32_t link_gap_ms;     // socket writability spacing
  uint32_t card_ms;         // per-chunk card read + socket write
  uint64_t block_at;        // bytes remaining when one card read blocks
  uint32_t block_ms;        // how long that read blocks
  uint64_t drop_at;         // bytes remaining when the client disconnects
  int      never_writable;
} sim_job;

extern "C" void sim_reset(void) {
  sim_now = sim_last_service = sim_max_gap = sim_services = 0;
  sim_panic = 0; sim_connected = 1; sim_next_writable = 0; sim_never_writable = 0;
  sim_pumps = 0; stream_gone_n = stream_stall_n = 0;
}

extern "C" void sim_sd_transfer(const sim_job *j, sim_result *out) {
  sim_reset();
  sim_link_gap = j->link_gap_ms;
  sim_never_writable = j->never_writable;
  WiFiClient c;
  uint64_t due = 0, remain = j->bytes;
  while (remain && stream_ready(c, &due)) {
    sim_next_writable = sim_now + sim_link_gap;
    if (j->block_ms && remain == j->block_at) sim_charge(j->block_ms);   // a card read that hangs
    uint64_t n = remain > %(chunk)u ? %(chunk)u : remain;
    sim_charge(j->card_ms);
    remain -= n;
    if (j->drop_at && remain <= j->drop_at) sim_connected = 0;
  }
  out->panicked = sim_panic;
  out->services = sim_services;
  out->max_gap = sim_max_gap;
  out->elapsed_ms = sim_now;
  out->pumps = sim_pumps;
  out->remain = remain;
  out->gone_n = stream_gone_n;
  out->stall_n = stream_stall_n;
}
"""


class Result(ctypes.Structure):
    _fields_ = [("panicked", ctypes.c_int), ("services", ctypes.c_uint32),
                ("max_gap", ctypes.c_uint32), ("elapsed_ms", ctypes.c_uint32),
                ("pumps", ctypes.c_uint32), ("remain", ctypes.c_uint64),
                ("gone_n", ctypes.c_ulong), ("stall_n", ctypes.c_ulong)]


class Job(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_uint64), ("link_gap_ms", ctypes.c_uint32),
                ("card_ms", ctypes.c_uint32), ("block_at", ctypes.c_uint64),
                ("block_ms", ctypes.c_uint32), ("drop_at", ctypes.c_uint64),
                ("never_writable", ctypes.c_int)]


def _build(tmp, name, body):
    cxx = shutil.which("c++") or shutil.which("g++")
    if cxx is None:
        pytest.skip("no c++ on PATH: stream_ready() was NOT executed by this run")
    src = tmp / ("%s.cpp" % name)
    src.write_text(HARNESS % {"wdt": WDT_MS, "chunk": CHUNK_B, "defines": DEFINES,
                              "stream_ready": body})
    so = tmp / ("%s.so" % name)
    r = subprocess.run([cxx, "-O1", "-fPIC", "-shared", "-Wall", "-Wno-unused-function",
                        str(src), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.sim_sd_transfer.argtypes = [ctypes.POINTER(Job), ctypes.POINTER(Result)]
    lib.sim_sd_transfer.restype = None
    return lib


def _run(lib, **kw):
    out = Result()
    lib.sim_sd_transfer(ctypes.byref(Job(**kw)), ctypes.byref(out))
    return out


@pytest.fixture(scope="module")
def fixed(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("wdt_fixed"), "fixed", STREAM_READY)


@pytest.fixture(scope="module")
def shipped(tmp_path_factory):
    """v0.1.5: the same function with the watchdog service deleted."""
    body = re.sub(r"\n\s*boot_wdt_service\(\);", "", STREAM_READY)
    return _build(tmp_path_factory.mktemp("wdt_shipped"), "shipped", body)


def test_the_mutant_is_the_shipped_function_minus_the_service_call():
    assert "boot_wdt_service();" in STREAM_READY, "stream_ready() services no watchdog at all"
    body = re.sub(r"\n\s*boot_wdt_service\(\);", "", STREAM_READY)
    assert "boot_wdt_service" not in body
    assert body.count("stream_pump(due);") == STREAM_READY.count("stream_pump(due);")
    assert body.count("STREAM_STALL_MS") == STREAM_READY.count("STREAM_STALL_MS")


def test_an_11_6_mb_sd_fetch_completes_without_a_task_wdt_panic(fixed, shipped):
    """rankine, 2026-09-15: ~11.6 MB over Wi-Fi at roughly 2 ms per 2 kB chunk is ~12 s of
    transfer, well past the 5 s loop watchdog."""
    job = dict(bytes=SCENE_BYTES, link_gap_ms=1, card_ms=1, block_at=0, block_ms=0,
               drop_at=0, never_writable=0)
    ok = _run(fixed, **job)
    assert ok.remain == 0, "the whole file must still be sent"
    assert not ok.panicked, "task_wdt would have reset the node mid-transfer"
    assert ok.max_gap < WDT_MS
    assert ok.elapsed_ms > WDT_MS, "the scenario must actually outlast the watchdog window"
    assert ok.services >= SCENE_BYTES // CHUNK_B, "one service per chunk at least"

    bad = _run(shipped, **job)
    assert bad.panicked, "the pre-fix function must fail this test"
    assert bad.max_gap >= WDT_MS


def test_a_slow_link_between_chunks_is_still_covered(fixed, shipped):
    """A socket that only takes a chunk every 900 ms spends the whole transfer inside
    stream_ready()'s wait, not in the handler's write."""
    job = dict(bytes=CHUNK_B * 40, link_gap_ms=900, card_ms=0, block_at=0, block_ms=0,
               drop_at=0, never_writable=0)
    ok = _run(fixed, **job)
    assert ok.remain == 0 and not ok.panicked
    assert ok.elapsed_ms > 3 * WDT_MS
    assert _run(shipped, **job).panicked


def test_a_card_read_that_blocks_still_panics(fixed):
    """The service call is inside the wait and nowhere else, so blocked I/O in the handler is
    NOT masked: a card read that hangs for 30 s reaches the watchdog exactly as before."""
    out = _run(fixed, bytes=CHUNK_B * 8, link_gap_ms=0, card_ms=0,
               block_at=CHUNK_B * 5, block_ms=30000, drop_at=0, never_writable=0)
    assert out.panicked, "a hung read must still reset the node"
    assert out.max_gap >= WDT_MS


def test_a_socket_that_never_takes_a_chunk_ends_the_response_instead_of_resetting(fixed, shipped):
    """STREAM_STALL_MS bounds the no-progress case: the handler gives up and the node stays up,
    with the stall counted where /status can read it."""
    job = dict(bytes=SCENE_BYTES, link_gap_ms=0, card_ms=0, block_at=0, block_ms=0,
               drop_at=0, never_writable=1)
    ok = _run(fixed, **job)
    assert not ok.panicked
    assert ok.stall_n == 1 and ok.gone_n == 0
    assert STALL_MS < ok.elapsed_ms <= STALL_MS + 100, "the stall deadline is unchanged"
    assert ok.remain == SCENE_BYTES, "nothing was sent"
    assert _run(shipped, **job).panicked, "v0.1.5 reset the node before the stall deadline"


def test_a_client_that_disconnects_mid_transfer_ends_the_response_at_once(fixed):
    out = _run(fixed, bytes=SCENE_BYTES, link_gap_ms=1, card_ms=1, block_at=0, block_ms=0,
               drop_at=SCENE_BYTES - CHUNK_B * 4, never_writable=0)
    assert not out.panicked
    assert out.gone_n == 1 and out.stall_n == 0
    assert out.remain > 0, "the transfer stops where the client went away"
    assert out.elapsed_ms < STALL_MS, "a gone client must not wait out the stall deadline"


def test_the_service_does_not_replace_the_audio_pump(fixed):
    """The watchdog reset is added to the wait, not swapped for it: audio, sketches and the
    stall deadline all still run on the same iteration."""
    out = _run(fixed, bytes=CHUNK_B * 16, link_gap_ms=3, card_ms=0, block_at=0, block_ms=0,
               drop_at=0, never_writable=0)
    assert out.remain == 0 and out.pumps >= 16
