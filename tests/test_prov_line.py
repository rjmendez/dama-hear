"""The USB enrollment line: what enroll.py sends is what the firmware's parser accepts, a line
damaged in transit is refused, and nothing the parser refuses leaves a record behind.

hear_prov_line.h is compiled with cc and driven directly, so this is the firmware's own parser
rather than a Python copy of it.
"""
import ctypes
import hashlib
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "lib" / "hear_platform" / "src" / "hear_prov_line.h"
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))
import enroll  # noqa: E402


class Prov(ctypes.Structure):
    _fields_ = [("node", ctypes.c_char * 24), ("cls", ctypes.c_char * 24), ("n", ctypes.c_int),
                ("ssid", (ctypes.c_char * 33) * 8), ("psk", (ctypes.c_char * 65) * 8)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: the firmware's PROV parser was NOT executed by this run")
    d = tmp_path_factory.mktemp("prov")
    (d / "w.c").write_text(
        '#include "%s"\n' % HDR
        + "const char *w_parse(const char *l, hear_prov_t *p){return hear_prov_parse(l, p);}\n"
          "int w_same(const hear_prov_t *a, const hear_prov_t *b){return hear_prov_same(a, b);}\n"
          "unsigned w_crc(const char *s, unsigned long n){return hear_prov_crc32(s, n);}\n"
          "int w_size(void){return (int)sizeof(hear_prov_t);}\n")
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_parse.argtypes = [ctypes.c_char_p, ctypes.POINTER(Prov)]
    lib.w_parse.restype = ctypes.c_char_p
    lib.w_same.argtypes = [ctypes.POINTER(Prov), ctypes.POINTER(Prov)]
    lib.w_crc.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
    lib.w_crc.restype = ctypes.c_uint
    assert lib.w_size() == ctypes.sizeof(Prov), "the ctypes mirror no longer matches hear_prov_t"
    return lib


def parse(lib, line):
    p = Prov()
    err = lib.w_parse(line.encode(), ctypes.byref(p))
    return (err.decode() if err else None), p


def signed(body):
    return enroll.sign(body)


PAIRS = [("home net", "correct horse"), ("a=b:c d", "x" * 63), ("été", "12345678")]
NET = "net=%s:%s" % (b"x".hex(), b"password".hex())


def test_the_crc_is_zlibs(lib):
    import zlib
    for s in (b"", b"PROV v=1", bytes(range(256))):
        assert lib.w_crc(s, len(s)) == zlib.crc32(s) & 0xFFFFFFFF


def test_what_enroll_sends_the_firmware_reads_back_exactly(lib):
    err, p = parse(lib, enroll.prov_line("rankine", "xiao-s3-pps", PAIRS))
    assert err is None
    assert (p.node, p.cls, p.n) == (b"rankine", b"xiao-s3-pps", 3)
    for k, (s, k2) in enumerate(PAIRS):
        assert p.ssid[k].value == s.encode() and p.psk[k].value == k2.encode()


def test_a_line_damaged_in_transit_is_refused_not_saved(lib):
    """The USB CDC queue drops the rest of a packet when it is full. A hole an even number of hex
    digits long inside a PSK still parses as a shorter, wrong PSK -- only the crc catches it."""
    line = enroll.prov_line("rankine", "xiao-s3-pps", PAIRS)
    body = line[:line.rindex(" crc=")]
    i = body.index(b"x".hex() * 8)
    holed = body[:i] + body[i + 8:] + line[len(body):]
    err, p = parse(lib, holed)
    assert err == "bad crc" and p.n == 0
    for pos in range(5, len(line) - 1, 37):
        flipped = line[:pos] + ("0" if line[pos] != "0" else "1") + line[pos + 1:]
        assert parse(lib, flipped)[0] is not None, pos


def test_the_crc_must_be_last_and_present(lib):
    body = "PROV v=1 node=a " + NET
    assert parse(lib, body)[0] == "missing crc"
    good = signed(body)
    crc_tok = good[len(body):]
    assert parse(lib, "PROV v=1" + crc_tok + " node=a " + NET)[0] == "missing crc"
    assert parse(lib, good[:-1] + "g")[0] == "missing crc"


def test_the_limits_agree_on_both_sides(lib):
    most = [("n%d" % i, "password%d" % i) for i in range(enroll.MAX_NETS)]
    assert parse(lib, enroll.prov_line("a", "", most))[0] is None
    with pytest.raises(ValueError):
        enroll.prov_line("a", "", most + [("x", "password")])
    body = enroll.prov_line("a", "", most)
    body = body[:body.rindex(" crc=")] + " " + NET
    assert parse(lib, signed(body))[0] == "too many networks"


@pytest.mark.parametrize("body,why", [
    ("PROV node=a " + NET, "missing v=1"),
    ("PROV v=2 node=a " + NET, "unsupported version"),
    ("PROV v=1 " + NET, "missing node"),
    ("PROV v=1 node=a", "no networks"),
    ("PROV v=1 node=A " + NET, "bad node"),
    ("PROV v=1 node=-a " + NET, "bad node"),
    ("PROV v=1 node=a class=X " + NET, "bad class"),
    ("PROV v=1 node=a net=78:7061737377", "bad psk"),
    ("PROV v=1 node=a net=:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=0078:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=7:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=zz:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=%s:70617373776f7264" % (b"s" * 33).hex(), "bad ssid"),
    ("PROV v=1 node=a net=78", "net without ':'"),
    ("PROV v=1 node=a bogus=1 " + NET, "unknown key"),
    ("PROV v=1 node=a loose " + NET, "token without '='"),
])
def test_a_refused_line_leaves_nothing_behind(lib, body, why):
    err, p = parse(lib, signed(body))
    assert err == why
    assert (p.node, p.cls, p.n) == (b"", b"", 0)


def test_not_a_prov_line_and_overlong_are_refused_before_anything_else(lib):
    assert parse(lib, "PROVv=1")[0] == "not a PROV line"
    assert parse(lib, signed("PROV v=1 node=a " + NET) + " " * 1100)[0] == "line too long"


def test_same_compares_every_network(lib):
    _, a = parse(lib, enroll.prov_line("a", "c", PAIRS))
    _, b = parse(lib, enroll.prov_line("a", "c", PAIRS))
    assert lib.w_same(ctypes.byref(a), ctypes.byref(b)) == 1
    _, c = parse(lib, enroll.prov_line("a", "c", PAIRS[:2] + [(PAIRS[2][0], "different!")]))
    assert lib.w_same(ctypes.byref(a), ctypes.byref(c)) == 0


def test_enroll_refuses_what_the_node_would_refuse():
    for node, pairs in (("Bad", PAIRS), ("a", [("x", "short")]), ("a", [("", "password")]),
                        ("a", []), ("a", [("x" * 33, "password")])):
        with pytest.raises(ValueError):
            enroll.prov_line(node, "", pairs)


def test_enroll_sends_the_line_in_pieces_smaller_than_a_packet():
    sent = []

    class Port:
        def write(self, b):
            sent.append(bytes(b))

        def flush(self):
            pass

    line = enroll.prov_line("rankine", "xiao-s3-pps", PAIRS)
    enroll.time.sleep, real = (lambda s: None), enroll.time.sleep
    try:
        enroll.send_line(Port(), line)
    finally:
        enroll.time.sleep = real
    assert b"".join(sent) == line.encode() + b"\n"
    assert max(len(c) for c in sent) <= enroll.CHUNK < 256


def test_sha256sums_is_checked_and_a_mismatch_refused():
    data = b"image"
    good = "%s  hear_node-v1.bin\n" % hashlib.sha256(data).hexdigest()
    enroll.check_sums(good, "hear_node-v1.bin", data)
    with pytest.raises(ValueError):
        enroll.check_sums(good, "hear_node-v1.bin", data + b"x")
    with pytest.raises(ValueError):
        enroll.check_sums(good, "hear_node-v2.bin", data)
