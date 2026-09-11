"""A GPS bring-up must not stop the microphone, and must not run on a link that already works.

Every node, 2026-09-11: /status sys.loop_max_boot_ms ~24,700 ms right after boot, and the log
line "gps   nothing decoded in 33s -- re-running bring-up (attempt 1)" at the same moment.

  * The watchdog armed on uptime. The runtime NMEA/UBX parser is fed only from loop(), so on the
    first pass after a setup() that took more than 20 s -- the boot sweep alone is ~24.7 s --
    nmea_valid and ubx_pvt were both 0 and the retry fired on a link setup() had just confirmed.
  * The sweep's waits called nothing that drains the I2S DMA, which holds 6 x 240 frames = 30 ms:
    gps_listen's 60 ms settle and 1.2 s per candidate rate, the 2.5 s confirmation, and the 300 ms
    before CFG-VALSET.

Scanned with comments and string literals removed, so prose cannot satisfy a check.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
BOARD = ROOT / "firmware" / "boards" / "xiao_s3_sense.h"

FLEET_MAX_BAUD = 230400    # nyquist's link; mach runs 115200


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


def _fn(name, code=CODE):
    m = re.search(r"^(?:static\s+)?[\w:*&<> ]+?\b%s\s*\([^;{]*\)\s*\{" % re.escape(name), code, re.M)
    assert m, "%s() not found" % name
    return _block(code, m.end() - 1)


def _watchdog():
    i = CODE.index("static const uint32_t GPS_RETRY_MS[]")
    return CODE[i:CODE.index("gps_bringup();", i)]


def _val(name):
    defs = {}
    for src in (_strip(BOARD.read_text()), CODE):
        for m in re.finditer(r"^[ \t]*#define[ \t]+(\w+)[ \t]+([^\n]+)$", src, re.M):
            defs[m.group(1)] = m.group(2).strip()

    def ev(n):
        assert n in defs, "%s is not defined" % n
        e = re.sub(r"\((?:uint32_t|uint64_t|int|size_t)\)", "", defs[n])
        e = re.sub(r"\b(\d+)(?:ULL|UL|U|u)\b", r"\1", e).replace("/", "//")
        for ident in sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", e)), key=len, reverse=True):
            e = re.sub(r"\b%s\b" % ident, "(%d)" % ev(ident), e)
        return eval(e, {"__builtins__": {}})
    return ev(name)


def test_the_retry_clock_starts_when_a_bring_up_ends_not_at_boot():
    b = _fn("gps_bringup")
    stamp = re.search(r"(\w+)\s*=\s*millis\(\);\s*\}$", b)
    assert stamp, "gps_bringup() must end by stamping when it FINISHED"
    name = stamp.group(1)
    assert b.rindex("gps_configure();") < stamp.start()
    assert len(re.findall(r"(?<!uint32_t )\b%s\s*=(?!=)" % name, CODE)) == 1, (
        "only gps_bringup() may move the retry clock")
    w = _watchdog()
    assert re.search(r"millis\(\)\s*-\s*%s\s*>\s*wait" % name, w), (
        "the watchdog must wait from the end of the last bring-up, setup()'s included")
    assert not re.search(r"\bup_ms\s*>", w), "uptime still arms the retry"


def test_no_wait_on_the_sweep_path_starves_the_microphone():
    for name in ("gps_listen", "gps_autobaud", "gps_bringup"):
        assert "delay(" not in _fn(name), "%s() still waits in delay()" % name
    assert "gps_wait_ms(60);" in _fn("gps_listen")
    assert "gps_wait_ms(300);" in _fn("gps_bringup")


def test_every_listen_pass_pumps_and_the_window_is_unchanged():
    b = _fn("gps_listen")
    i = b.index("while (millis() - t0 < window_ms)")
    assert re.match(r"\{\s*gps_pump\(&due\);\s*while \(Serial1\.available\(\)\)", _block(b, i))


def test_the_pump_moves_audio_and_sketches_and_nothing_else():
    for name in ("gps_pump", "gps_wait_ms"):
        body = _fn(name)
        assert "stream_pump(" in body
        for banned in ("handleClient", "clip_pump", "det_flush", "SD.", "csv_open", "Serial1"):
            assert banned not in body, "%s() reaches %s" % (name, banned)


def test_http_is_not_served_because_a_handler_runs_the_sweep():
    code = _strip(INO.read_text(), strings=True)
    i = code.index('http.on("/gpspins"')
    assert "gps_bringup();" in _block(code, code.index("[]()", i))


def test_setup_bring_up_waits_as_before_because_audio_is_not_up():
    s = _fn("setup")
    assert s.index("gps_bringup();") < s.index("i2s.begin(") < s.index("i2s_up = true;")
    assert len(re.findall(r"\bi2s_up\s*=\s*true", CODE)) == 1
    assert re.search(r"\bif \(i2s_up\) stream_pump\(due\);", _fn("gps_pump"))
    assert re.search(r"\bif \(!i2s_up\) \{ delay\(ms\); return; \}", _fn("gps_wait_ms"))


def test_the_bit_time_probes_stay_contiguous():
    """A run-length histogram with holes in it is a different measurement: the pin probe is sized
    to catch a 1 Hz burst of a few ms, and a 6 ms pump can swallow one whole."""
    for name in ("gps_bit_time_us", "gps_pick_pins"):
        assert not re.search(r"\b(?:gps_pump|gps_wait_ms|stream_pump|audio_pump)\s*\(", _fn(name)), name


def test_the_uart_holds_what_arrives_during_one_pump():
    b = _fn("gps_bringup")
    assert (b.index("Serial1.end();") < b.index("Serial1.setRxBufferSize(GPS_RX_BUF);")
            < b.index("gps_autobaud()"))
    pump_s = _val("STREAM_PUMP_MAX") * _val("BLOCK_US") / 1e6
    need = FLEET_MAX_BAUD / 10 * pump_s
    assert _val("GPS_RX_BUF") >= need, "%d B < %.0f B at %d baud" % (_val("GPS_RX_BUF"), need, FLEET_MAX_BAUD)
