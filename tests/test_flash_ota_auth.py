"""flash.py must survive the endpoint-auth change it never knew about.

The firmware endpoint-auth work made protected /update uploads require
`X-Hear-Auth: <HEAR_ADMIN_TOKEN>`. Nothing on the client side moved: flash.py is the only OTA
client in the tree and it posted the image with no header at all, so the first node to run an
auth-aware image would have answered every future `flash.py <node> <ip>` with 401 and
"unauthorized". Runtime credentials now live in NVS, and release flashes are refused until /status
proves NVS has both push and admin auth before any bytes are written.

These tests pin both halves: the token is sent, and a tokenless image is refused before it can be
put somewhere only a USB cable can reach.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))

import flash  # noqa: E402


def _push_file(tmp_path, text):
    p = tmp_path / "hear_push"
    p.write_text(text)
    return str(p)


class TestAdminTokenSource:
    def test_token_comes_from_the_same_file_gen_secrets_compiles_from(self, tmp_path):
        p = _push_file(tmp_path, "HEAR_PUSH_TOKEN=pushpush\nHEAR_ADMIN_TOKEN=s3cr3t\n")
        assert flash.admin_token(p) == "s3cr3t"

    def test_a_missing_file_is_no_token_rather_than_an_error(self, tmp_path):
        assert flash.admin_token(str(tmp_path / "nope")) == ""

    def test_a_file_without_the_admin_line_is_no_token(self, tmp_path):
        assert flash.admin_token(_push_file(tmp_path, "HEAR_PUSH_TOKEN=pushpush\n")) == ""

    def test_surrounding_whitespace_and_quotes_do_not_become_part_of_the_token(self, tmp_path):
        p = _push_file(tmp_path, '  HEAR_ADMIN_TOKEN = "s3cr3t"  \n')
        assert flash.admin_token(p) == "s3cr3t"


class TestTheOtaUploadCarriesTheToken:
    def test_the_token_is_sent_as_a_header(self):
        cmd = flash.ota_curl_cmd("172.16.100.82", "/tmp/x.bin", "s3cr3t")
        assert "-H" in cmd
        assert "X-Hear-Auth: s3cr3t" in cmd
        assert cmd[cmd.index("-H") + 1] == "X-Hear-Auth: s3cr3t"

    def test_the_token_never_goes_in_the_url(self):
        """?token= works on the node, but a URL is what gets pasted into a log or a bug report."""
        cmd = flash.ota_curl_cmd("172.16.100.82", "/tmp/x.bin", "s3cr3t")
        url = [a for a in cmd if a.startswith("http://")]
        assert url == ["http://172.16.100.82/update"]
        assert "s3cr3t" not in url[0]

    def test_no_token_means_no_empty_header(self):
        cmd = flash.ota_curl_cmd("172.16.100.82", "/tmp/x.bin", "")
        assert "-H" not in cmd
        assert not any("X-Hear-Auth" in a for a in cmd)

    def test_the_image_and_the_target_are_still_what_they_were(self):
        for token in ("s3cr3t", ""):
            cmd = flash.ota_curl_cmd("172.16.100.82", "/some/hear_node.ino.bin", token)
            assert "firmware=@/some/hear_node.ino.bin" in cmd
            assert cmd[-1] == "http://172.16.100.82/update"

    def test_the_status_code_is_requested_so_a_401_is_distinguishable(self):
        cmd = flash.ota_curl_cmd("172.16.100.82", "/tmp/x.bin", "s3cr3t")
        assert "-w" in cmd
        assert "%{http_code}" in cmd[cmd.index("-w") + 1]


class TestTokenlessImagesAreNotPutOutOfReach:
    TARGET = "172.16.100.82"

    def test_no_token_is_refused_and_the_message_names_the_fix(self):
        why = flash.admin_lockout_refusal("", [], "gold", self.TARGET)
        assert why
        assert "HEAR_ADMIN_TOKEN" in why and "~/.hear_push" in why
        assert flash.LOCKOUT_OPT_OUT in why

    def test_a_token_is_all_it_takes(self):
        assert flash.admin_lockout_refusal("s3cr3t", [], "gold", self.TARGET) is None

    def test_the_opt_out_is_honoured_for_a_board_someone_can_reach(self):
        assert flash.admin_lockout_refusal(
            "", ["flash.py", flash.LOCKOUT_OPT_OUT], "gold", self.TARGET) is None

    def test_the_opt_out_is_not_spelled_force(self):
        """--force already means 're-identify this board'. Two unrelated risks, two flags."""
        assert flash.admin_lockout_refusal("", ["flash.py", "--force"], "gold", self.TARGET)


class TestReidentifyRefusalIsActionable:
    def test_a_different_node_is_refused_with_a_message(self):
        why = flash.reidentify_refusal({"node": "mach"}, "nyquist", "1.2.3.4", ["flash.py"])
        assert why and "mach" in why and "nyquist" in why

    def test_force_lets_a_deliberate_reidentification_through(self):
        """It used to evaluate to die("") -- a refusal with no reason, which --force could not
        actually override."""
        assert flash.reidentify_refusal(
            {"node": "mach"}, "nyquist", "1.2.3.4", ["flash.py", "--force"]) is None

    def test_the_right_node_and_an_unreachable_one_are_both_fine(self):
        assert flash.reidentify_refusal({"node": "gold"}, "gold", "1.2.3.4", []) is None
        assert flash.reidentify_refusal(None, "gold", "1.2.3.4", []) is None
        assert flash.reidentify_refusal({}, "gold", "1.2.3.4", []) is None


class TestTheRefusalIsWiredIntoTheFlashItself:
    LIVE = {"node": "gold", "class": "esp32s3-i2s-gps",
            "prov": {"src": "compiled", "nets": 1, "nvs": True}}

    def _mock(self, monkeypatch, token, cmds):
        monkeypatch.setattr(flash, "status", lambda host: dict(self.LIVE))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash, "admin_token", lambda path=None: token)
        monkeypatch.setattr(flash, "built_fw_version", lambda path=None: None)
        monkeypatch.setattr(flash.os.path, "exists", lambda p: p.endswith("hear_node.ino.bin"))
        monkeypatch.setattr(flash.subprocess, "run", lambda cmd, **kw: (
            cmds.append(cmd),
            type("R", (), {"stdout": "OK\n200", "stderr": "", "returncode": 0})())[1])

    def test_a_tokenless_ota_flash_stops_before_it_compiles_anything(self, monkeypatch, capsys):
        cmds = []
        self._mock(monkeypatch, "", cmds)
        with pytest.raises(SystemExit) as e:
            flash.main(["flash.py", "gold", "172.16.100.50"])
        assert e.value.code != 0
        assert "HEAR_ADMIN_TOKEN" in capsys.readouterr().err
        assert not any(c[:2] == ["arduino-cli", "compile"] for c in cmds), \
            "the refusal must land before a two-minute compile, not after it"

    def test_the_upload_that_does_run_carries_the_token(self, monkeypatch):
        cmds = []
        self._mock(monkeypatch, "s3cr3t", cmds)
        assert flash.main(["flash.py", "gold", "172.16.100.50"]) == 0
        upload = next(c for c in cmds if c[0] == "curl")
        assert "X-Hear-Auth: s3cr3t" in upload

    def test_a_usb_flash_is_exempt_because_the_board_is_already_in_reach(self, monkeypatch):
        cmds = []
        self._mock(monkeypatch, "", cmds)
        assert flash.main(["flash.py", "gold", "/dev/ttyACM0", "--class", "esp32s3-i2s-gps"]) == 0
        assert any(c[:2] == ["arduino-cli", "compile"] for c in cmds)


class TestTheSketchStillWantsTheHeaderThisSends:
    def test_the_firmware_reads_the_header_flash_py_writes(self):
        ino = (ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text(errors="replace")
        assert 'http.hasHeader("X-Hear-Auth")' in ino
        assert "ota_authorized = hear_ota_auth_ok();" in ino

    def test_an_empty_compiled_in_token_refuses_non_ota_admin_routes(self):
        """The premise of the lockout refusal, asserted against the source it is about."""
        ino = (ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text(errors="replace")
        assert "#define HEAR_ADMIN_TOKEN \"\"" in ino
        assert "if (!wn) return false;" in ino

    def test_update_has_the_last_resort_recovery_valve(self):
        ino = (ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text(errors="replace")
        assert "static bool hear_ota_auth_ok()" in ino
        assert "if (!admin_token_runtime()[0]) return true;" in ino
