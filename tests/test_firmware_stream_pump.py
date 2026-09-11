"""A handler that streams must keep the microphone running, and the pump must not touch a card file
the handler is reading.

/sd used to loop f.read -> client.write with no audio_pump(), so every drain fetch stopped audio,
scene, health and detections for the whole transfer: scene gaps over 20 s began within ~1 s of a
drain fetch of that node on nyquist 78/81, mach 98/108, rankine 33/44 times.

These scan the firmware SOURCE with comments stripped, so a comment naming a construct can neither
satisfy nor trip them.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
BOARD = ROOT / "firmware" / "boards" / "xiao_s3_sense.h"


def _strip(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l.split("//")[0] for l in src.splitlines())


CODE = _strip(INO.read_text())


def _block(src, i):
    """The braces-balanced block starting at the first '{' at or after i."""
    i = src.index("{", i)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced block")


def _fn(name):
    m = re.search(r"^static[^\n;]*\b%s\(" % re.escape(name), CODE, flags=re.M)
    assert m, "%s() not found" % name
    return _block(CODE, m.end())


def _handler(path):
    i = CODE.index('http.on("%s"' % path)
    return _block(CODE, CODE.index("[]()", i))


def _functions():
    out = {}
    for m in re.finditer(r"^(?:static\s+)?(?:[\w:*&<>]+\s+)+\**(\w+)\s*\([^;{]*\)\s*\{", CODE, flags=re.M):
        out.setdefault(m.group(1), _block(CODE, m.end() - 1))
    return out


def _define(name, src=CODE):
    m = re.search(r"#define\s+%s\s+(.+)" % name, src)
    assert m, name
    return m.group(1).strip()


LONG_HANDLERS = ("/sd", "/ls", "/audio", "/perf")


def test_every_long_handler_pumps_until_the_socket_can_take_a_chunk():
    for path in LONG_HANDLERS:
        h = _handler(path)
        assert "due = stream_start()" in h, path
        assert "stream_ready(c, &due)" in h, "%s sends without pumping audio" % path
    assert "http.client().write" not in _handler("/sd")


def test_stream_ready_never_blocks_on_the_socket():
    b = _fn("stream_ready")
    assert "stream_pump(due)" in b
    assert re.search(r"select\(fd \+ 1, NULL, &w, NULL, &tv\)", b)
    assert "struct timeval tv = {0, 0};" in b, "the writability probe must not wait"
    assert "STREAM_STALL_MS" in b


def test_stream_pump_takes_blocks_and_sketches():
    b = _fn("stream_pump")
    assert "audio_pump();" in b and "sketch_pump();" in b, (
        "aring holds 341 ms; a detection during a stream needs its sketch taken while it is there")
    assert "STREAM_PUMP_MAX" in b and "*due += BLOCK_US;" in b


def test_card_handlers_mark_the_card_held_while_the_file_is_open():
    sd = _handler("/sd")
    assert (sd.index("sd_streaming++") < sd.index("while (remain && stream_ready")
            < sd.index("f.close()") < sd.index("sd_streaming--"))
    ls = _handler("/ls")
    assert (ls.index("sd_streaming++") < ls.index("d.openNextFile()")
            < ls.index("d.close()") < ls.index("sd_streaming--"))


def test_the_pump_can_only_append_while_a_handler_holds_the_card():
    fns = _functions()
    seen, todo = set(), ["audio_pump", "sketch_pump"]
    while todo:
        f = todo.pop()
        if f in seen or f not in fns:
            continue
        seen.add(f)
        todo += [c for c in re.findall(r"\b(\w+)\s*\(", fns[f]) if c in fns and c != f]
    touching = {f for f in seen if re.search(r"\bSD\.|\bcsv_open\(|\bprune_oldest\(", fns[f])}
    assert touching == {"scene_emit", "csv_open", "prune_oldest"}, (
        "card access reachable from the pump changed: %s" % sorted(touching))
    b = fns["scene_emit"]
    g = b.index("if ((strcmp(day, cur_day) != 0 || !scenef) && !sd_streaming)")
    guarded = _block(b, g)
    for call in ("prune_oldest(", "csv_open(", "scenef.close()"):
        assert b.count(call) == guarded.count(call) + (1 if call == "scenef.close()" else 0), call
    assert "if (sd_streaming) scene_stream_skip++; else scene_write_fail++;" in b


def test_a_dropped_scene_row_is_counted_where_it_can_be_read():
    assert '\\"stream_skip\\":%lu' in _fn("status_json")
    assert "scene_stream_skip" in _fn("status_json")


def test_audio_stops_rather_than_serve_a_lapped_ring():
    assert "if (s < praw_floor()) break;" in _handler("/audio")


def test_a_clip_the_ring_laps_mid_write_is_dropped_not_labelled_ok():
    """The ring advances inside every long handler and clip_pump runs only from loop(), so a clip
    in flight can be overtaken; its remaining chunks would be later audio under clip_why=ok."""
    b = _fn("clip_pump")
    busy = _block(b, b.index("if (clip_busy)"))
    assert busy.index("bool lapped = clip_s < praw_floor();") < busy.index("memcpy(cbuf, praw + idx")
    assert "bool ok = false;" in busy and "if (!lapped) {" in busy
    assert "if (lapped) clip_skip_ring++; else clip_fail++;" in busy
    assert "d.clip_st = lapped ? CLIP_RING : CLIP_FAIL;" in busy


def test_praw_is_written_and_read_in_one_64_bit_domain():
    """praw_cap does not divide 2^32, so a uint32 acquisition cursor stops matching the write
    position once g_samples * DECIM passes 2^32: 24.86 h at 16 kHz."""
    assert "uint32_t w = (uint32_t)((g_samples64 * DECIM) % praw_cap);" in _fn("audio_pump")
    assert "g_samples += nd; g_samples64 += nd;" in _fn("audio_pump")
    assert re.search(r"^static uint64_t acq_of\(uint32_t d_samp\)", CODE, flags=re.M)
    assert re.search(r"^static uint64_t clip_s = 0;", CODE, flags=re.M)
    assert "uint64_t s = acq_of((uint32_t)got0);" in _handler("/audio")
    assert sorted(re.findall(r"\(([^()]*(?:\([^()]*\))?[^()]*) % praw_cap\)", CODE)) == \
        ["(g_samples64 * DECIM)", "clip_s", "s"]


def test_the_uint32_cursor_was_off_by_2_32_mod_the_ring_and_the_64_bit_one_is_not():
    wrap_d = -(-2 ** 32 // 3)
    for span_s, off in ((60, 887296), (80, 1847296)):
        cap = span_s * 48000
        g32 = g64 = wrap_d + 10 * 16000
        write = (g64 * 3) % cap
        assert ((g32 * 3) & 0xFFFFFFFF) % cap == (write - off) % cap
        widened = g64 - ((g32 - (g32 - 5)) & 0xFFFFFFFF)
        assert (widened * 3) % cap == ((g64 - 5) * 3) % cap
    g32, g64 = 3, 2 ** 32 + 3
    assert g64 - ((g32 - (2 ** 32 - 2)) & 0xFFFFFFFF) == 2 ** 32 - 2


def test_the_headroom_is_the_configured_dma_at_the_acquisition_rate():
    fs_acq = int(_define("FS_NOMINAL", _strip(BOARD.read_text())).split()[0]) * int(_define("DECIM"))
    assert fs_acq == 48000
    dma = eval(_define("I2S_DMA_SAMPLES"))
    assert dma == 6 * 240
    block = int(_define("BLOCK")) * int(_define("DECIM"))
    dma_ms, block_ms = dma * 1000 / fs_acq, block * 1000 / fs_acq
    assert (dma_ms, block_ms) == (30.0, 16.0)
    card_ms = int(_define("STREAM_CARD_US").rstrip("u")) / 1000
    assert block_ms + card_ms <= dma_ms
    assert int(_define("STREAM_CHUNK_B")) <= 5744 // 2, "CONFIG_LWIP_TCP_SND_BUF_DEFAULT / 2"
    for a in ("I2S_DMA_SAMPLES >= ABLOCK", "STREAM_CHUNK_B <= CONFIG_LWIP_TCP_SND_BUF_DEFAULT / 2",
              "BLOCK_US + STREAM_CARD_US <= I2S_DMA_US"):
        assert re.search(r"static_assert\(%s," % re.escape(a), CODE), a


def test_the_read_instant_anchors_the_schedule():
    b = _fn("audio_pump")
    r = b.index("i2s.readBytes(")
    assert "blk_read_us = (uint64_t)esp_timer_get_time();" in b[r:r + 200]
    assert "return blk_read_us + BLOCK_US;" in _fn("stream_start")
