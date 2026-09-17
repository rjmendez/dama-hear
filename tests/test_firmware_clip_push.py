"""Host-compiled proof for hear_node's no-SD push path: clip_push.h against a bare `cc`.

clip_push.h has no mbedtls dependency on purpose (see the header's own comment) so it builds
on any host with a C compiler, exactly like tests/test_firmware_spool_push.py does for
spool_push.h. Two things are checked that matter more than the rest: the firmware's SHA-256
against the FIPS 180-4 vectors, and the firmware's upload_id against the real Python
hear.ingest.clipupload module -- not a copy of it -- so the two sides of the wire cannot drift
without a failing test.
"""
from __future__ import annotations

import ctypes
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from hear.ingest import clipupload as CU

ROOT = Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "clip_push.h"
BUILD = ROOT / ".otabuild" / "host_tests" / "clip_push"


@pytest.fixture(scope="module")
def clip_push():
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: clip_push.h could not be compiled")
    BUILD.mkdir(parents=True, exist_ok=True)
    src = BUILD / "w.c"
    so = BUILD / "w.so"
    src.write_text(
        '#include "%s"\n' % HDR
        + "int w_sha256(const uint8_t *data, size_t len, char *hex_out){"
          " hear_sha256_hex_of(data, len, hex_out); return 64;}\n"
          "int w_upload_id(const char *node, const char *boot, uint32_t sample, char *out){"
          " hear_clip_upload_id(node, boot, sample, out); return (int)strlen(out);}\n"
          "int w_init_idem(const char *device_id, const char *upload_id, char *out, size_t cap){"
          " return hear_clip_init_idempotency_key(device_id, upload_id, out, cap);}\n"
          "int w_complete_idem(const char *device_id, const char *upload_id, const char *sha,"
          "                    char *out, size_t cap){"
          " return hear_clip_complete_idempotency_key(device_id, upload_id, sha, out, cap);}\n"
          "int w_init_body(const char *device_id, const char *node, const char *boot,"
          "               uint32_t sample, const char *basename, uint32_t clip_bytes,"
          "               uint32_t chunk_bytes, const char *source, const char *upload_id,"
          "               char *out, size_t cap){"
          " return hear_clip_init_body(device_id, node, boot, sample, basename, clip_bytes,"
          "                           chunk_bytes, source, upload_id, out, cap);}\n"
          "int w_complete_body(uint32_t clip_bytes, const char *sha, char *out, size_t cap){"
          " return hear_clip_complete_body(clip_bytes, sha, out, cap);}\n"
          "int w_chunk_path(const char *upload_id, uint32_t idx, char *out, size_t cap){"
          " return hear_clip_chunk_path(upload_id, idx, out, cap);}\n"
          "int w_complete_path(const char *upload_id, char *out, size_t cap){"
          " return hear_clip_complete_path(upload_id, out, cap);}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror", str(src), "-o", str(so)],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_sha256.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_char_p]
    lib.w_sha256.restype = ctypes.c_int
    lib.w_upload_id.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p]
    lib.w_upload_id.restype = ctypes.c_int
    lib.w_init_idem.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.w_init_idem.restype = ctypes.c_int
    lib.w_complete_idem.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                    ctypes.c_char_p, ctypes.c_size_t]
    lib.w_complete_idem.restype = ctypes.c_int
    lib.w_init_body.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32,
                                ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p,
                                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.w_init_body.restype = ctypes.c_int
    lib.w_complete_body.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.w_complete_body.restype = ctypes.c_int
    lib.w_chunk_path.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
    lib.w_chunk_path.restype = ctypes.c_int
    lib.w_complete_path.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.w_complete_path.restype = ctypes.c_int
    return lib


def _u8(buf: bytes):
    return (ctypes.c_uint8 * len(buf)).from_buffer_copy(buf) if buf else (ctypes.c_uint8 * 0)()


# --------------------------------------------------------------- SHA-256 against FIPS 180-4

@pytest.mark.parametrize("msg", [b"", b"abc",
                                 b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"])
def test_sha256_matches_hashlib(clip_push, msg):
    out = ctypes.create_string_buffer(65)
    clip_push.w_sha256(_u8(msg), len(msg), out)
    assert out.value.decode() == hashlib.sha256(msg).hexdigest()


def test_sha256_of_one_full_chunk(clip_push):
    """The chunk size the wire actually uses, not just short textbook vectors."""
    msg = bytes((i * 7 + 3) % 256 for i in range(32768))
    out = ctypes.create_string_buffer(65)
    clip_push.w_sha256(_u8(msg), len(msg), out)
    assert out.value.decode() == hashlib.sha256(msg).hexdigest()


# --------------------------------------------------------------- identity parity with Python

@pytest.mark.parametrize("node,boot,sample", [
    ("gold", "a1b2c3", 4242),
    ("ageev", "0f0f0f0f0f0f", 0),
    ("kasami", "deadbeefcafe", 999999999),
])
def test_upload_id_matches_the_python_contract(clip_push, node, boot, sample):
    out = ctypes.create_string_buffer(64)
    n = clip_push.w_upload_id(node.encode(), boot.encode(), sample, out)
    got = out.value[:n].decode()
    assert got == CU.upload_id(node, boot, sample)
    assert len(got) == 32


def test_idempotency_keys_match_the_python_contract(clip_push):
    upload = CU.upload_id("gold", "a1b2c3", 4242)
    out = ctypes.create_string_buffer(160)
    n = clip_push.w_init_idem(b"gold", upload.encode(), out, len(out))
    assert n > 0
    assert out.value[:n].decode() == CU.init_idempotency_key("gold", upload)

    sha = hashlib.sha256(b"whatever").hexdigest()
    out2 = ctypes.create_string_buffer(160)
    n2 = clip_push.w_complete_idem(b"gold", upload.encode(), sha.encode(), out2, len(out2))
    assert n2 > 0
    assert out2.value[:n2].decode() == CU.complete_idempotency_key("gold", upload, sha)


def test_idempotency_key_buffer_too_small_fails_closed(clip_push):
    out = ctypes.create_string_buffer(4)
    n = clip_push.w_init_idem(b"gold", b"0" * 32, out, len(out))
    assert n == -1


# --------------------------------------------------------------- frame builders round-trip

def test_init_body_is_admissible_to_the_python_validator(clip_push):
    node, boot, sample = "gold", "a1b2c3", 4242
    upload_id = CU.upload_id(node, boot, sample)
    basename = f"{node}-{boot}-{sample:010d}.wav"
    out = ctypes.create_string_buffer(512)
    n = clip_push.w_init_body(node.encode(), node.encode(), boot.encode(), sample,
                              basename.encode(), CU.NOMINAL_CLIP_BYTES, CU.CHUNK_BYTES,
                              b"psram_ring", upload_id.encode(), out, len(out))
    assert n > 0
    import json
    body = json.loads(out.value[:n].decode())
    assert CU.validate_init(body, credential_device_id=node) is None
    assert body["upload_source"] == "psram_ring"
    assert body["upload_id"] == upload_id


def test_complete_body_is_admissible_to_the_python_validator(clip_push):
    sha = "ab" * 32
    out = ctypes.create_string_buffer(256)
    n = clip_push.w_complete_body(CU.NOMINAL_CLIP_BYTES, sha.encode(), out, len(out))
    assert n > 0
    import json
    body = json.loads(out.value[:n].decode())
    assert CU.validate_complete(body, bytes_received=CU.NOMINAL_CLIP_BYTES,
                                chunks_received=CU.chunk_count(CU.NOMINAL_CLIP_BYTES),
                                expected_chunks=CU.chunk_count(CU.NOMINAL_CLIP_BYTES),
                                clip_bytes=CU.NOMINAL_CLIP_BYTES) is None


def test_chunk_and_complete_paths_match_the_route_templates(clip_push):
    upload_id = "0" * 32
    out = ctypes.create_string_buffer(128)
    n = clip_push.w_chunk_path(upload_id.encode(), 7, out, len(out))
    assert out.value[:n].decode() == CU.CHUNK_ROUTE_TEMPLATE.format(upload_id=upload_id,
                                                                     chunk_index=7)
    out2 = ctypes.create_string_buffer(128)
    n2 = clip_push.w_complete_path(upload_id.encode(), out2, len(out2))
    assert out2.value[:n2].decode() == CU.COMPLETE_ROUTE_TEMPLATE.format(upload_id=upload_id)
