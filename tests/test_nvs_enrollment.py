"""A release image carries no credentials, so a node's name and Wi-Fi come from NVS.

The two ways in are enroll.py over USB and any build with compiled-in credentials, which copies
them into NVS at boot. The firmware is parsed with comments stripped, so this file's prose cannot
satisfy it.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))
import flash  # noqa: E402


def _code(p=INO):
    s = re.sub(r"/\*.*?\*/", "", p.read_text(), flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def _body(code, sig):
    i = code.index(sig)
    j = code.index("{", i)
    depth = 0
    for k in range(j, len(code)):
        depth += {"{": 1, "}": -1}.get(code[k], 0)
        if depth == 0:
            return code[j:k + 1]
    raise AssertionError("unbalanced braces after %s" % sig)


def _branches(body):
    i = body.index("#if HEAR_WIFI_CONFIGURED")
    j = body.index("#else", i)
    k = body.index("#endif", j)
    return body[i:j], body[j:k]


def test_compiled_credentials_are_copied_into_nvs_and_nvs_is_used_without_them():
    b = _body(_code(), "static void node_identity(")
    assert "hear_prov_load(" in b
    compiled, nvs_only = _branches(b)
    assert "hear_prov_save(" in compiled and "hear_prov_same(" in compiled
    assert "prov = nv" in nvs_only and "hear_prov_save" not in nvs_only


def test_loaded_is_only_a_record_read_back_and_never_one_just_written():
    """nvs:true alone cannot tell a record the node read from one it wrote a moment ago. loaded is
    the proof that the read path works, which is what a release image depends on."""
    compiled, nvs_only = _branches(_body(_code(), "static void node_identity("))
    for branch in (compiled, nvs_only):
        m = re.search(r"prov_loaded\s*=\s*([^;]*);", branch)
        assert m, "prov_loaded is not set in this branch"
        assert "have_nv" in m.group(1) and "hear_prov_save" not in m.group(1)


def test_wifi_joins_from_the_loaded_record_not_the_compiled_arrays():
    setup = _body(_code(), "void setup(")
    assert "WiFi.begin(prov.ssid[k], prov.psk[k])" in setup
    assert "WIFI_SSIDS" not in setup and "WIFI_PASSES" not in setup


def test_status_says_where_the_credentials_came_from():
    assert re.search(r'\\"prov\\":\{\\"src\\":\\"%s\\",\\"nets\\":%d,\\"nvs\\":%s,'
                     r'\\"loaded\\":%s\}', _code())


def test_an_image_with_compiled_credentials_refuses_serial_provisioning():
    """They would win at the next boot and overwrite whatever was sent, so accepting it would be a
    lie that lasts until the reboot."""
    compiled, nvs_only = _branches(_body(_code(), "static void prov_serial_line("))
    assert "PROV ERR" in compiled and "hear_prov_save" not in compiled
    assert "hear_prov_parse(" in nvs_only and "hear_prov_save(" in nvs_only


def test_the_loop_reads_the_serial_line():
    assert "prov_serial_poll();" in _body(_code(), "void loop(")


LITERAL = re.compile(r'"(?:\\.|[^"\\\n])*"')


def _calls_without_literals(src):
    """Strings blanked FIRST, then comments: `http://` inside a format string would otherwise be
    read as the start of a comment and cut the literal in half."""
    s = LITERAL.sub('""', src)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    for fn in (r"logf", r"logln", r"Serial\.printf", r"Serial\.println", r"Serial\.print"):
        for m in re.finditer(r"\b%s\s*\(([^;]*)\);" % fn, s):
            yield m.group(0), m.group(1)


def test_no_credential_is_an_argument_to_the_log_or_the_serial_reply():
    """The AP line's format string says 'ssid' about the node's own AP, which is not a stored
    credential; the arguments are what can leak one."""
    calls = list(_calls_without_literals(INO.read_text()))
    assert len(calls) > 50, "the call scan found almost nothing, so it is not scanning"
    for call, args in calls:
        assert not re.search(r"\b(?:psk|ssid)\b", args), call


def test_the_argument_check_would_catch_a_leak():
    leak = 'logf("wifi  http://%s/ %s\\n", ip, prov.psk[k]);  // a comment'
    (_, args), = _calls_without_literals(leak)
    assert re.search(r"\b(?:psk|ssid)\b", args)


class TestReleaseRefusal:
    GOOD = {"node": "nyquist", "prov": {"src": "compiled", "nets": 2, "nvs": True, "loaded": True}}

    def test_an_enrolled_node_is_accepted(self):
        assert flash.release_refusal(self.GOOD, "nyquist") is None
        assert flash.release_refusal(
            {"node": "nyquist", "prov": {"src": "nvs", "nets": 1, "nvs": True}}, "nyquist") is None

    def test_firmware_that_predates_enrollment_is_refused_with_the_way_out(self):
        why = flash.release_refusal({"node": "nyquist", "fw": "87eb4d3"}, "nyquist")
        assert why and "predates" in why and "flash.py nyquist" in why

    @pytest.mark.parametrize("prov", [{"src": "compiled", "nets": 2, "nvs": False},
                                      {"src": "none", "nets": 0, "nvs": False},
                                      {"src": "nvs", "nets": 0, "nvs": True}])
    def test_a_node_without_an_nvs_record_is_refused(self, prov):
        assert flash.release_refusal({"node": "nyquist", "prov": prov}, "nyquist")

    def test_another_node_is_refused(self):
        assert "mach" in flash.release_refusal(dict(self.GOOD, node="mach"), "nyquist")

    def test_a_release_is_never_sent_over_usb(self):
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "nyquist", "/dev/ttyACM0", "--release", "v0.1.0"])
