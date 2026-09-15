from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
HEAR = (ROOT / "firmware/hear_node/hear_node.ino").read_text()
PUC = (ROOT / "firmware/puc_node/puc_node.ino").read_text()
PLATFORM_LOG = (ROOT / "firmware/lib/hear_platform/src/hear_log.cpp").read_text()


def _function_body(source, name):
    start = source.index(name)
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace:i + 1]
    raise AssertionError("unterminated function: %s" % name)


def test_heap_and_stack_watermarks_are_diagnostic_contracts():
    for source in (HEAR, PUC):
        assert "ESP.getMinFreeHeap()" in source
        assert "ESP.getMaxAllocHeap()" in source
        assert "uxTaskGetStackHighWaterMark(NULL)" in source
        assert 'heap_min' in source
        assert 'heap_max_alloc' in source
        assert 'stack_high_watermark_words' in source


def test_audio_pump_has_no_dynamic_allocation_or_string_reallocation():
    body = _function_body(HEAR, "static void audio_pump()")
    assert not re.search(r"\b(String|malloc|calloc|realloc)\b", body)


def test_audio_capture_buffers_are_static_and_bounded():
    for name in ("blk", "acblk", "dcblk", "dhist", "dscratch"):
        assert re.search(r"static\s+int16_t\s+%s\s*\[" % name, HEAR)
    assert "ps_malloc(want)" in HEAR
    assert "heap_caps_calloc(want_n[k], sizeof(Det)" in HEAR
    assert "PSRAM_KEEP_B" in HEAR


def test_dynamic_diagnostic_buffers_are_released():
    assert "free(work);" in HEAR
    assert "free(all);" in HEAR
    assert "free(b0); free(b1);" in PUC


def test_platform_log_is_a_fixed_ring_not_an_unbounded_accumulator():
    assert "static char s_buf[HEAR_LOG_CAP]" in PLATFORM_LOG
    assert "o.reserve(HEAR_LOG_CAP + 1)" in PLATFORM_LOG
    assert "s_w = n - right" in PLATFORM_LOG
