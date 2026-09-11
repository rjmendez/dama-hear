"""dets.csv must not lose rows to a burst the node is expected to see.

nyquist, 2026-09-11: /status audio.detections 257 and audio.lost 74 over 31 minutes, the
detections arriving in bursts at a median spacing of 33 ms (~30 Hz). dets[] was a 128-slot ring in
internal RAM. det_flush() writes in order and will not pass a row whose clip is still pending, and
the head of a burst waits for its 4 s post-roll and then for the one-at-a-time clip writer -- so a
30 Hz burst overran 128 slots within seconds and det_flush counted the overwritten rows as lost.

The model here is det_flush + clip_pump + the loop's flush cadence, driven by constants read from
the sketch, so a change to any of them is judged by the same arithmetic. The sketch is scanned with
comments and string literals removed, so prose cannot satisfy a check.
"""
import math
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
BOARD = ROOT / "firmware" / "boards" / "xiao_s3_sense.h"

BURST_HZ = 30          # nyquist's burst: median detection spacing 33 ms
OLD_RING = 128         # the ring nyquist lost 74 rows with


def _strip(src, strings=False):
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif src[i] in "\"'" and not strings:
            q, j = src[i], i + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            out.append(q + q)
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


CODE = _strip(INO.read_text())


def _block(code, i):
    i = code.index("{", i)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i:j + 1]
    raise AssertionError("unbalanced braces")


def _fn(name):
    m = re.search(r"^(?:static\s+)?[\w:*&<> ]+?\b%s\s*\([^;{]*\)\s*\{" % re.escape(name), CODE, re.M)
    assert m, "%s() not found" % name
    return _block(CODE, m.end() - 1)


def _defines():
    d = {}
    for src in (_strip(BOARD.read_text()), CODE):
        for m in re.finditer(r"^[ \t]*#define[ \t]+(\w+)[ \t]+([^\n]+)$", src, re.M):
            d[m.group(1)] = m.group(2).strip()
    return d


DEFS = _defines()


def _val(name):
    assert name in DEFS, "%s is not defined" % name
    e = re.sub(r"\((?:uint32_t|uint64_t|int|size_t)\)", "", DEFS[name])
    e = re.sub(r"\b(\d+)(?:ULL|UL|U|u)\b", r"\1", e).replace("/", "//")
    for ident in sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", e)), key=len, reverse=True):
        e = re.sub(r"\b%s\b" % ident, "(%d)" % _val(ident), e)
    return eval(e, {"__builtins__": {}})


def _constants():
    fs = _val("FS_NOMINAL")
    loop = _fn("loop")
    return {
        "pass_s": _val("BLOCK") / fs,                                  # one loop pass per I2S block
        "post_s": _val("CLIP_POST_SAMPLES") / (fs * _val("DECIM")),
        "dedupe_s": _val("CLIP_DEDUPE_SAMPLES") / fs,
        "chunks": math.ceil(_val("CLIP_SAMPLES") / (_val("CLIP_CHUNK_B") // 2)),
        "wait_s": _val("CLIP_WAIT_MAX_S"),
        "budget": int(re.search(r"wrote < (\d+)", _fn("det_flush")).group(1)),
        "flush_s": int(re.search(r"millis\(\) - last_fl > (\d+)", loop).group(1)) / 1000,
        "tick_s": int(re.search(r"if \(millis\(\) - last > (\d+)\)", loop).group(1)) / 1000,
    }


def _psram_steps():
    """The ring sizes setup() will take from PSRAM, or None for a sketch with no PSRAM ring."""
    setup = _fn("setup")
    m = re.search(r"heap_caps_calloc\((\w+)\[k\],\s*sizeof\(Det\),\s*MALLOC_CAP_SPIRAM\)", setup)
    if not m:
        return None
    arr = re.search(r"static const uint32_t %s\[\]\s*=\s*\{([^}]*)\}" % m.group(1), setup)
    assert arr, "the PSRAM step list is not a literal array"
    return [_val(x.strip()) for x in arr.group(1).split(",")]


def _ring_under_test():
    steps = _psram_steps()
    return min(steps) if steps else _val("MAXDET")


class _Node:
    """det_n / det_flushed / det_lost, clip_pump's one-clip queue and det_flush's in-order write."""

    def __init__(self, c, cap):
        self.c, self.cap = c, cap
        self.t, self.clip = [], []
        self.det_n = self.flushed = self.lost = self.peak = 0
        self.written = []
        self.busy, self.chunks, self.clip_k, self.last_t, self.scan = False, 0, 0, None, 0

    def detect(self, t):
        self.t.append(t)
        self.clip.append(None)
        self.det_n += 1
        self.peak = max(self.peak, self.det_n - self.flushed)

    def _first(self):
        f = self.flushed
        if self.det_n > self.cap and self.det_n - self.cap > f:
            f = self.det_n - self.cap
        return f

    def _ready(self, k, now):
        return self.clip[k] is not None and now >= self.t[k] + 2 * self.c["pass_s"]

    def clip_pump(self, now):
        c = self.c
        if self.busy:
            self.chunks -= 1
            if not self.chunks:
                self.busy = False
                if self.clip_k >= self.det_n - self.cap:
                    self.clip[self.clip_k] = "ok"
                self.last_t = self.t[self.clip_k]
            return
        k = max(self._first(), self.scan)
        while k < self.det_n and self.clip[k] is not None:
            k += 1
        self.scan = k
        if k >= self.det_n:
            return
        if self.last_t is not None and self.t[k] - self.last_t < c["dedupe_s"]:
            self.clip[k] = "dedupe"
            return
        if now - self.t[k] < c["post_s"]:
            if now - self.t[k] > c["wait_s"]:
                self.clip[k] = "stalled"
            return
        self.busy, self.chunks, self.clip_k = True, c["chunks"], k

    def det_flush(self, now):
        if self.flushed == self.det_n or not self._ready(self._first(), now):
            return
        first = self.flushed
        if self.det_n - self.flushed > self.cap:
            first = self.det_n - self.cap
            self.lost += first - self.flushed
        k, wrote = first, 0
        while k < self.det_n and wrote < self.c["budget"] and self._ready(k, now):
            self.written.append(k)
            self.flushed = k + 1
            k += 1
            wrote += 1


def _run(c, cap, arrivals, t_end, freeze=(0.0, 0.0)):
    """freeze: a GPS bring-up, which pumps audio and sketches but runs neither clip_pump nor
    det_flush until it returns."""
    node, ai, now, last_fl, last_tick = _Node(c, cap), 0, 0.0, -1e9, 0.0
    while now < t_end:
        now += c["pass_s"]
        while ai < len(arrivals) and arrivals[ai] <= now:
            node.detect(arrivals[ai])
            ai += 1
        if freeze[0] <= now < freeze[1]:
            continue
        node.clip_pump(now)
        if node.det_n != node.flushed and now - last_fl > c["flush_s"]:
            last_fl = now
            node.det_flush(now)
        if now - last_tick > c["tick_s"]:
            last_tick = now
            node.det_flush(now)
    return node


def _burst(t0, c):
    return [t0 + i / BURST_HZ for i in range(int(BURST_HZ * c["wait_s"]))]


def test_a_30_hz_burst_lasting_clip_wait_max_s_loses_no_row():
    c = _constants()
    cap = _ring_under_test()
    arr = _burst(5.0, c)
    node = _run(c, cap, arr, t_end=5.0 + c["wait_s"] + 240)
    assert node.lost == 0, "a %d-slot ring lost %d of %d rows (peak backlog %d)" % (
        cap, node.lost, len(arr), node.peak)
    assert node.written == list(range(len(arr))), "rows missing or out of order in dets.csv"


def test_the_same_burst_inside_a_gps_bring_up_still_fits():
    """24.7 s is the sweep every node measured as sys.loop_max_boot_ms."""
    c = _constants()
    cap = _ring_under_test()
    arr = _burst(10.0, c)
    node = _run(c, cap, arr, t_end=60.0 + c["wait_s"] + 240, freeze=(5.0, 5.0 + 24.7))
    assert node.lost == 0, "a %d-slot ring lost %d rows to a burst during a bring-up" % (cap, node.lost)
    assert node.written == list(range(len(arr)))


def test_the_model_reproduces_the_field_loss_on_the_old_ring():
    """Not vacuous: the 128-slot ring with the same flush and clip constants loses rows."""
    c = _constants()
    node = _run(c, OLD_RING, _burst(5.0, c), t_end=5.0 + c["wait_s"] + 240)
    assert node.lost > 0


def test_no_detection_ring_sits_in_internal_ram():
    arrays = re.findall(r"\bstatic\s+Det\s+(\w+)\s*\[(\w+)\]", CODE)
    assert arrays, "the ring needs a static placeholder until setup() allocates it"
    for name, size in arrays:
        assert _val(size) <= 16, "static Det %s[%s] is %d slots of internal RAM" % (name, size, _val(size))
    assert re.search(r"\bstatic\s+Det\s*\*\s*dets\s*=", CODE)


def test_the_ring_comes_from_psram_after_the_raw_ring_and_leaves_the_same_reserve():
    setup = _fn("setup")
    m = re.search(r"heap_caps_calloc\(\w+\[k\],\s*sizeof\(Det\),\s*MALLOC_CAP_SPIRAM\)", setup)
    assert m, "the detection ring is not allocated from PSRAM"
    assert setup.index("ps_malloc(") < m.start(), "the ring must not take PSRAM ahead of the raw ring"
    guards = re.findall(
        r"heap_caps_get_largest_free_block\(MALLOC_CAP_SPIRAM\)\s*<\s*want\s*\+\s*(\w+)", setup)
    assert len(guards) == 2 and len(set(guards)) == 1, guards
    assert not guards[0].isdigit(), "both allocations must keep ONE named reserve"


def test_every_psram_step_holds_the_burst_and_survives_the_counter_wrap():
    steps = _psram_steps()
    assert steps, "no PSRAM ring"
    wait_s = _val("CLIP_WAIT_MAX_S")
    for s in steps:
        assert s >= BURST_HZ * wait_s, "%d slots < %d Hz x %d s" % (s, BURST_HZ, wait_s)
        assert (1 << 32) % s == 0, "det_n %% %d jumps when det_n wraps" % s


def test_every_slot_is_addressed_through_the_runtime_capacity():
    mods = set(re.findall(r"\bdets\s*\[[^\]]*%\s*(\w+)\s*\]", CODE))
    mods |= set(re.findall(r"\(det_n\+\+\)\s*%\s*(\w+)", CODE))
    assert mods == {"det_cap"}, mods
    bounds = set(re.findall(r"\bdet_n\s*[->]\s*([A-Za-z_]\w*)", CODE)) - {"det_flushed"}
    assert bounds == {"det_cap"}, bounds


def test_the_detections_endpoint_stays_bounded():
    """/detections builds one String; a 1024-slot ring at ~620 B a row would not fit in it."""
    b = _fn("h_dets")
    m = re.search(r"if \(n > (\w+)\) n = \1;", b)
    assert m, "h_dets is not capped by a fixed count"
    assert _val(m.group(1)) <= OLD_RING
    assert b.index(m.group(0)) < b.index("o.reserve(")
