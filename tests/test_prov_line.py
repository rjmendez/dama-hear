"""The USB enrollment line: what enroll.py sends is what the firmware's parser accepts, and
nothing the parser refuses leaves a record behind. hear_prov_line.h is compiled with cc and driven
directly, so this is the firmware's own parser rather than a Python copy of it."""
import ctypes
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
          "int w_size(void){return (int)sizeof(hear_prov_t);}\n")
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_parse.argtypes = [ctypes.c_char_p, ctypes.POINTER(Prov)]
    lib.w_parse.restype = ctypes.c_char_p
    lib.w_same.argtypes = [ctypes.POINTER(Prov), ctypes.POINTER(Prov)]
    lib.w_size.restype = ctypes.c_int
    assert lib.w_size() == ctypes.sizeof(Prov), "the ctypes mirror no longer matches hear_prov_t"
    return lib


def parse(lib, line):
    p = Prov()
    err = lib.w_parse(line.encode(), ctypes.byref(p))
    return (err.decode() if err else None), p


PAIRS = [("home net", "correct horse"), ("a=b:c d", "x" * 63), ("été", "12345678")]


def test_what_enroll_sends_the_firmware_reads_back_exactly(lib):
    err, p = parse(lib, enroll.prov_line("rankine", "xiao-s3-pps", PAIRS))
    assert err is None
    assert (p.node, p.cls, p.n) == (b"rankine", b"xiao-s3-pps", 3)
    for k, (s, k2) in enumerate(PAIRS):
        assert p.ssid[k].value == s.encode() and p.psk[k].value == k2.encode()


def test_the_limits_agree_on_both_sides(lib):
    most = [("n%d" % i, "password%d" % i) for i in range(enroll.MAX_NETS)]
    assert parse(lib, enroll.prov_line("a", "", most))[0] is None
    with pytest.raises(ValueError):
        enroll.prov_line("a", "", most + [("x", "password")])
    line = enroll.prov_line("a", "", [("x", "password")]) + " net=%s:%s" % (b"y".hex(), b"password".hex())
    assert parse(lib, line)[0] is None
    too_many = enroll.prov_line("a", "", most) + " net=%s:%s" % (b"y".hex(), b"password".hex())
    assert parse(lib, too_many)[0] == "too many networks"


@pytest.mark.parametrize("line,why", [
    ("PROV node=a net=78:70617373776f7264", "missing v=1"),
    ("PROV v=2 node=a net=78:70617373776f7264", "unsupported version"),
    ("PROV v=1 net=78:70617373776f7264", "missing node"),
    ("PROV v=1 node=a", "no networks"),
    ("PROV v=1 node=A net=78:70617373776f7264", "bad node"),
    ("PROV v=1 node=-a net=78:70617373776f7264", "bad node"),
    ("PROV v=1 node=a class=X net=78:70617373776f7264", "bad class"),
    ("PROV v=1 node=a net=78:7061737377", "bad psk"),
    ("PROV v=1 node=a net=:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=0078:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=7:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=zz:70617373776f7264", "bad ssid"),
    ("PROV v=1 node=a net=78", "net without ':'"),
    ("PROV v=1 node=a bogus=1 net=78:70617373776f7264", "unknown key"),
    ("PROV v=1 node=a loose net=78:70617373776f7264", "token without '='"),
    ("PROVv=1", "not a PROV line"),
])
def test_a_refused_line_leaves_nothing_behind(lib, line, why):
    err, p = parse(lib, line)
    assert err == why
    assert (p.node, p.cls, p.n) == (b"", b"", 0)


def test_an_ssid_longer_than_32_bytes_is_refused(lib):
    line = "PROV v=1 node=a net=%s:%s" % ((b"s" * 33).hex(), b"password".hex())
    assert parse(lib, line)[0] == "bad ssid"


def test_an_overlong_line_is_refused_before_it_is_read(lib):
    line = "PROV v=1 node=a net=%s:%s" % (b"s".hex(), (b"p" * 8).hex())
    assert parse(lib, line + " " * 1100)[0] == "line too long"


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


def test_sha256sums_is_checked_and_a_mismatch_refused():
    data = b"image"
    import hashlib
    good = "%s  hear_node-v1.bin\n" % hashlib.sha256(data).hexdigest()
    enroll.check_sums(good, "hear_node-v1.bin", data)
    with pytest.raises(ValueError):
        enroll.check_sums(good, "hear_node-v1.bin", data + b"x")
    with pytest.raises(ValueError):
        enroll.check_sums(good, "hear_node-v2.bin", data)
