"""A release image carries no credentials, so a node's name and Wi-Fi come from NVS.

The two ways in are enroll.py over USB and any build with compiled-in credentials, which copies
them into NVS at boot. The firmware is parsed with comments stripped, so this file's prose cannot
satisfy it.
"""
import hashlib
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))
import board_profiles  # noqa: E402
import flash  # noqa: E402
import enroll  # noqa: E402
import release_manifest  # noqa: E402

AUTH_STATUS = {"push": {"configured": True, "src": "nvs", "last_code": 204},
               "admin": {"configured": True, "src": "nvs"}}


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
    assert "prov.push_token" in compiled and "HEAR_PUSH_TOKEN" in compiled
    assert "prov.admin_token" in compiled and "HEAR_ADMIN_TOKEN" in compiled


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
    assert "hear_net_join(&prov," in setup
    assert "WIFI_SSIDS" not in setup and "WIFI_PASSES" not in setup


def test_status_says_where_the_credentials_came_from():
    assert re.search(r'\\"prov\\":\{\\"src\\":\\"%s\\",\\"nets\\":%d,\\"nvs\\":%s,'
                     r'\\"loaded\\":%s\}', _code())
    assert '\\"auth\\":{\\"push\\":{\\"configured\\":%s,\\"src\\":\\"%s\\",\\"last_code\\":%d,' in _code()
    assert '\\"admin\\":{\\"configured\\":%s,\\"src\\":\\"%s\\",\\"ota_recovery_open\\":%s}}' in _code()


def test_status_and_prov_report_the_boot_selftest():
    code = _code()
    assert '\\"selftest\\":{\\"mic\\":\\"%s\\",\\"mic_state\\":\\"%s\\",\\"mic_reason\\":\\"%s\\",' in code
    assert '\\"mic_stats\\":{\\"samples\\":%lu,\\"lo\\":%d,\\"hi\\":%d,\\"span\\":%lu,' in code
    assert '\\"gps\\":\\"%s\\",\\"pps\\":\\"%s\\",\\"wifi\\":\\"%s\\"}' in code
    assert "PROV STATE" in code and "push=%s admin=%s" in code
    assert "selftest=mic:%s,gps:%s,pps:%s,wifi:%s" in code


def test_setup_logs_one_concise_selftest_summary_and_keeps_the_watchdog_around_boot_probes():
    code = _code()
    setup = _body(code, "void setup(")
    assert 'logf("selftest mic=%s gps=%s pps=%s wifi=%s\\n"' in code
    arm = setup.index("boot_wdt_arm(15000);")
    for token in ("pinMode(PPS_PIN, INPUT_PULLDOWN);", "SD.begin(21, SPI, 20000000, \"/sd\", 8)",
                  "gps_bringup();", "Wire.begin(I2C_SDA, I2C_SCL, 100000);", "i2s.begin("):
        assert arm < setup.index(token), token
    assert setup.index("boot_wdt_disarm();") > setup.index("selftest_mic_probe();")


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


def _file_record(path, data):
    return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _release_manifest_text(tag, board_class, assets, dirty=False, psram_mode="octal"):
    """A schema-valid manifest; verification refuses anything less."""
    stub = b"stub\n"
    return json.dumps({
        "schema_version": release_manifest.SCHEMA_VERSION,
        "manifest_type": release_manifest.MANIFEST_TYPE,
        "tag": tag,
        "firmware_version": tag,
        "source": {
            "repository": "rjmendez/dama-hear",
            "commit": "abc123",
            "commit_short": "abc123",
            "describe": tag,
            "dirty": dirty,
            "dirty_paths": ["firmware/hear_node/hear_node.ino"] if dirty else [],
            "verifiable": not dirty,
            "refusals": ["dirty tree"] if dirty else [],
        },
        "build": {
            "sketch": "firmware/hear_node",
            "fqbn": board_profiles.FQBN,
            "libraries": ["firmware/lib"],
            "arduino_cli_version": "1.5.1",
            "esp32_core_version": "3.3.11",
            "credentials_policy": "none compiled in",
        },
        "inputs": [_file_record("firmware/hear_node/hear_node.ino", stub)],
        "generated_files": [_file_record("firmware/hear_node/decim.h", stub)],
        "schema_guards": [_file_record("tests/test_firmware_csv_schema.py", stub)],
        "variants": [{
            "board_class": board_class,
            "psram_mode": psram_mode,
            "release_stem": board_profiles.release_stem(board_class, psram_mode),
            "fqbn": board_profiles.PSRAM_MODES[psram_mode]["fqbn"],
            "build_flags": ["-DHEAR_ALLOW_NO_WIFI"],
            "board_header": _file_record(board_profiles.board_header(board_class), stub),
            "capture_profile": {
                "board_name": board_class,
                "board_header": board_profiles.board_header(board_class),
                "gps_protocol": "ubx",
                "mic_kind": "i2s",
                "mic_count": 1,
                "fs_nominal_hz": 16000,
                "decimation": 3,
                "fs_acquisition_hz": 48000,
                "mic_band_lo_hz": 50,
                "mic_band_hi_hz": 7000,
            },
            "partition_table": {
                "artifact": board_profiles.release_asset_name(tag, board_class, "partitions"),
                "sha256": hashlib.sha256(stub).hexdigest(),
                "bytes": len(stub),
                "source_path": None,
                "source_sha256": None,
                "source_status": "binary-only",
            },
            "artifacts": [{
                "name": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            } for name, data in assets.items()],
        }],
        "release_artifacts": [{
            "name": "build-info.json",
            "bytes": len(stub),
            "sha256": hashlib.sha256(stub).hexdigest(),
        }],
    })


class TestReleaseRefusal:
    GOOD = {"node": "nyquist", "class": "xiao-s3-pps",
            "prov": {"src": "compiled", "nets": 2, "nvs": True, "loaded": True},
            "auth": AUTH_STATUS}

    def test_an_enrolled_node_is_accepted(self):
        assert flash.release_refusal(self.GOOD, "nyquist") is None
        assert flash.release_refusal(
            {"node": "nyquist", "class": "xiao-s3-pps",
             "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
             "auth": AUTH_STATUS}, "nyquist") is None

    def test_a_release_requires_a_known_live_board_class(self):
        why = flash.release_refusal({"node": "nyquist", "class": "", "prov": self.GOOD["prov"]}, "nyquist")
        assert "no class" in why
        why = flash.release_refusal(
            {"node": "nyquist", "class": "mystery-board", "prov": self.GOOD["prov"]}, "nyquist")
        assert "unknown board class" in why

    def test_a_requested_board_class_must_match_live_status(self):
        why = flash.release_refusal(
            {"node": "gold", "class": "esp32s3-i2s-gps", "prov": self.GOOD["prov"]},
            "gold", "xiao-s3-pps")
        assert "esp32s3-i2s-gps" in why and "xiao-s3-pps" in why

    def test_firmware_that_predates_enrollment_is_refused_with_the_way_out(self):
        why = flash.release_refusal({"node": "nyquist", "class": "xiao-s3-pps", "fw": "87eb4d3"}, "nyquist")
        assert why and "predates" in why and "flash.py nyquist" in why

    @pytest.mark.parametrize("prov", [{"src": "compiled", "nets": 2, "nvs": False},
                                      {"src": "none", "nets": 0, "nvs": False},
                                      {"src": "nvs", "nets": 0, "nvs": True},
                                      {"src": "nvs", "nets": 1, "nvs": True, "loaded": False}])
    def test_a_node_without_an_nvs_record_is_refused(self, prov):
        assert flash.release_refusal({"node": "nyquist", "prov": prov, "auth": AUTH_STATUS}, "nyquist")

    @pytest.mark.parametrize("auth", [
        {"push": {"configured": False, "src": "missing"}, "admin": {"configured": True, "src": "nvs"}},
        {"push": {"configured": True, "src": "nvs"}, "admin": {"configured": False, "src": "missing"}},
        {"push": {"configured": True, "src": "compiled"}, "admin": {"configured": True, "src": "nvs"}},
    ])
    def test_a_release_requires_push_and_admin_credentials_in_nvs(self, auth):
        why = flash.release_refusal(dict(self.GOOD, auth=auth), "nyquist")
        assert why and ("auth." in why or "secret-free release" in why)

    def test_another_node_is_refused(self):
        assert "mach" in flash.release_refusal(dict(self.GOOD, node="mach"), "nyquist")

    def test_a_release_is_never_sent_over_usb(self):
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "nyquist", "/dev/ttyACM0", "--release", "v0.1.0"])


class TestPostFlashVersionCheck:
    """`flash: OK` used to mean only "a node with the right name answered".

    It does not follow that the image stayed up. A build that panics before
    HEAR_BOOT_HEALTHY_MS is reverted by hear_boot_guard() to the previous slot, which answers
    /status with the SAME node id and the OLD fw -- exactly what happened to nyquist twice on
    the v0.1.4-122-g794e3f5 heartbeat-push build. The version has to be part of the verdict.
    """

    def _flash(self, monkeypatch, states, built="v0.1.4-123-gabc1234"):
        monkeypatch.setattr(flash, "status", lambda host: next(states))
        monkeypatch.setattr(flash.os.path, "exists", lambda path: path.endswith("hear_node.ino.bin"))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash, "built_fw_version", lambda path=None: built)
        # An over-the-air build-mode flash refuses an image with no admin token, because that
        # image would refuse /update from everyone afterwards. This lane is about the version
        # check, so give it the token it needs to get that far.
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "s3cr3t")
        monkeypatch.setattr(flash.subprocess, "run", lambda cmd, **kw: type(
            "R", (), {"stdout": "OK", "stderr": "", "returncode": 0})())
        return flash.main(["flash.py", "gold", "172.16.100.50"])

    def _live(self, fw, uptime=4):
        # gold's class is esp32s3-i2s-gps: board_profiles records its silicon (2MB quad PSRAM)
        # against that class, and flash.py refuses a node/class pair that contradicts the record.
        return {"node": "gold", "class": "esp32s3-i2s-gps", "fw": fw, "uptime_s": uptime,
                "prov": {"src": "compiled", "nets": 1, "nvs": True}}

    def test_a_node_that_reverted_to_the_old_firmware_is_not_reported_as_ok(self, monkeypatch, capsys):
        # The boot guard put the previous slot back: right name, old version, forever.
        states = iter([self._live("v0.1.4-122-g794e3f5")] * 200)
        with pytest.raises(SystemExit) as e:
            self._flash(monkeypatch, states)
        assert e.value.code != 0
        out, err = capsys.readouterr()
        assert "v0.1.4-122-g794e3f5" in err and "v0.1.4-123-gabc1234" in err
        assert "boot guard" in err
        assert "flash: OK --" not in out

    def test_the_just_built_version_coming_back_is_ok(self, monkeypatch, capsys):
        states = iter([self._live("v0.1.4-123-gabc1234")] * 200)
        assert self._flash(monkeypatch, states) == 0
        assert "flash: OK --" in capsys.readouterr().out

    def test_the_old_version_during_the_reboot_window_is_waited_out_not_failed(self, monkeypatch):
        # First polls catch the pre-reboot image; the check must not fire on those.
        states = iter([self._live("v0.1.4-122-g794e3f5"),
                       self._live("v0.1.4-122-g794e3f5")]
                      + [self._live("v0.1.4-123-gabc1234", uptime=6)] * 40)
        assert self._flash(monkeypatch, states) == 0

    def test_no_assertable_build_version_skips_the_check_rather_than_failing(self, monkeypatch):
        # A tree with no git (FW_BUILD "unknown"/"unset") or no secrets.h has nothing to compare.
        states = iter([self._live("whatever-it-reports")] * 200)
        assert self._flash(monkeypatch, states, built=None) == 0

    def test_built_fw_version_reads_secrets_h_and_rejects_placeholders(self, tmp_path):
        p = tmp_path / "secrets.h"
        p.write_text('#define NODE_ID "gold"\n#define FW_BUILD "v0.1.4-123-gabc1234"\n')
        assert flash.built_fw_version(str(p)) == "v0.1.4-123-gabc1234"
        p.write_text('#define FW_BUILD "unknown"\n')
        assert flash.built_fw_version(str(p)) is None
        p.write_text('#define NODE_ID "gold"\n')
        assert flash.built_fw_version(str(p)) is None
        assert flash.built_fw_version(str(tmp_path / "nope.h")) is None

    def test_the_release_path_still_verifies_against_the_release_tag(self, monkeypatch):
        # Untouched: a release image's version is the tag, and its prov must come from NVS.
        good = {"node": "mach", "class": "xiao-s3-pps",
                "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                "auth": AUTH_STATUS}
        states = iter([good] + [dict(good, fw="v0.1.4-122-g794e3f5", uptime_s=90)] * 200)
        monkeypatch.setattr(flash, "status", lambda host: next(states))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash, "release_image", lambda tag, board_class, psram_mode=None, **kw: "/x/app.bin")
        monkeypatch.setattr(flash, "ota_post", lambda host, bin_path, token: ("OK", "200", ""))
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "admin-token")
        monkeypatch.setattr(flash.subprocess, "run", lambda cmd, **kw: type(
            "R", (), {"stdout": "OK", "stderr": "", "returncode": 0})())
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "mach", "172.16.100.50", "--release", "v0.1.5"])

    def test_release_post_flash_401_is_a_loud_failure(self, monkeypatch, capsys):
        before = {"node": "mach", "class": "xiao-s3-pps",
                  "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                  "auth": AUTH_STATUS}
        after = {"node": "mach", "class": "xiao-s3-pps", "fw": "v0.1.6", "uptime_s": 20,
                 "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                 "auth": {"push": {"configured": True, "src": "nvs", "last_code": 401},
                          "admin": {"configured": True, "src": "nvs"}}}
        states = iter([before, after])
        monkeypatch.setattr(flash, "status", lambda host: next(states))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash, "release_image", lambda tag, board_class, psram_mode=None, **kw: "/x/app.bin")
        monkeypatch.setattr(flash, "ota_post", lambda host, bin_path, token: ("OK", "200", ""))
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "admin-token")
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "mach", "172.16.100.50", "--release", "v0.1.6"])
        assert "401" in capsys.readouterr().err


class TestBoardClassSelection:
    def test_default_xiao_release_asset_name_is_unchanged_for_existing_nodes(self):
        assert board_profiles.release_asset_name("v0.1.3", "xiao-s3-pps", "app") \
            == "hear_node-xiao-s3-pps-v0.1.3.bin"
        assert board_profiles.build_extra_flags("xiao-s3-pps") == ""

    def test_gps_board_uses_the_compile_define_and_its_own_release_asset(self):
        assert board_profiles.build_extra_flags("esp32s3-i2s-gps") == "-DHEAR_BOARD_ESP32S3_I2S_GPS"
        assert board_profiles.release_asset_name("v0.1.3", "esp32s3-i2s-gps", "app") \
            == "hear_node-esp32s3-i2s-gps-v0.1.3.bin"
        assert board_profiles.release_asset_name("v0.1.3", "esp32s3-i2s-gps", "app", "quad") \
            == "hear_node-esp32s3-i2s-gps-qspi-v0.1.3.bin"

    def test_release_path_refuses_a_wrong_requested_board_class(self, monkeypatch):
        monkeypatch.setattr(flash, "status",
                            lambda host: {"node": "gold", "class": "esp32s3-i2s-gps",
                                          "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                                          "auth": AUTH_STATUS})
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "gold", "172.16.100.50", "--release", "v0.1.3", "--class", "xiao-s3-pps"])

    def test_release_path_uses_live_reported_qspi_variant_for_gold(self, monkeypatch):
        called = []
        good = {"node": "gold", "class": "esp32s3-i2s-gps",
                "sys": {"psram_bus": "quad"},
                "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                "auth": AUTH_STATUS}
        states = iter([good, dict(good, fw="v0.1.3", uptime_s=3)])
        monkeypatch.setattr(flash, "status", lambda host: next(states))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash.subprocess, "run", lambda cmd, **kw: type(
            "R", (), {"stdout": "OK", "stderr": "", "returncode": 0})())
        monkeypatch.setattr(flash, "ota_post", lambda host, bin_path, token: ("OK", "200", ""))
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "admin-token")

        def fake_release_image(tag, board_class, psram_mode=None, **kw):
            called.append((tag, board_class, psram_mode))
            return "/x/app.bin"

        monkeypatch.setattr(flash, "release_image", fake_release_image)
        assert flash.main(["flash.py", "gold", "172.16.100.50", "--release", "v0.1.3"]) == 0
        assert called == [("v0.1.3", "esp32s3-i2s-gps", "quad")]

    def test_release_path_refuses_live_psram_mode_that_contradicts_the_node_record(self, monkeypatch):
        monkeypatch.setattr(flash, "status",
                            lambda host: {"node": "gold", "class": "esp32s3-i2s-gps",
                                          "sys": {"psram_bus": "octal"},
                                          "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True},
                                          "auth": AUTH_STATUS})
        with pytest.raises(SystemExit):
            flash.main(["flash.py", "gold", "172.16.100.50", "--release", "v0.1.3"])

    def test_live_gps_node_build_adds_the_board_define(self, monkeypatch):
        cmds = []
        states = iter([
            {"node": "gold", "class": "esp32s3-i2s-gps",
             "prov": {"src": "nvs", "nets": 1, "nvs": True, "loaded": True}, "auth": AUTH_STATUS},
            {"node": "gold", "class": "esp32s3-i2s-gps", "fw": "dirty",
             "prov": {"src": "compiled", "nets": 1, "nvs": True}, "uptime_s": 3},
        ])

        monkeypatch.setattr(flash, "status", lambda host: next(states))
        monkeypatch.setattr(flash.os.path, "exists", lambda path: path.endswith("hear_node.ino.bin"))
        monkeypatch.setattr(flash.time, "sleep", lambda _: None)
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "s3cr3t")
        # gen_secrets.py is stubbed out below, so no secrets.h is written for this build and the
        # version assertion has nothing to assert against; that is the skip case, not a failure.
        monkeypatch.setattr(flash, "built_fw_version", lambda path=None: None)

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return type("R", (), {"stdout": "OK", "stderr": "", "returncode": 0})()

        monkeypatch.setattr(flash.subprocess, "run", fake_run)
        monkeypatch.setattr(flash, "ota_post", lambda host, bin_path, token: ("OK", "200", ""))
        monkeypatch.setattr(flash, "admin_token", lambda path=None: "admin-token")
        assert flash.main(["flash.py", "gold", "172.16.100.50"]) == 0
        compile_cmd = next(cmd for cmd in cmds if cmd[:3] == ["arduino-cli", "compile", "--fqbn"])
        assert "--build-property" in compile_cmd
        assert "compiler.cpp.extra_flags=-DHEAR_BOARD_ESP32S3_I2S_GPS" in compile_cmd

    def test_enroll_release_files_download_the_requested_board_class(self, monkeypatch, tmp_path):
        fetched = []
        payloads = {
            "hear_node-esp32s3-i2s-gps-v0.1.3.bin": b"app",
            "hear_node-esp32s3-i2s-gps-v0.1.3-bootloader.bin": b"boot",
            "hear_node-esp32s3-i2s-gps-v0.1.3-partitions.bin": b"parts",
        }
        manifest = _release_manifest_text("v0.1.3", "esp32s3-i2s-gps", payloads)

        def fake_fetch(url, timeout=60):
            name = url.rsplit("/", 1)[-1]
            fetched.append(name)
            if name == release_manifest.MANIFEST_NAME:
                return manifest.encode()
            return payloads[name]

        monkeypatch.setattr(enroll, "fetch", fake_fetch)
        enroll.release_files("v0.1.3", str(tmp_path), "esp32s3-i2s-gps")
        assert fetched == [
            release_manifest.MANIFEST_NAME,
            "hear_node-esp32s3-i2s-gps-v0.1.3.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-bootloader.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-partitions.bin",
        ]

    def test_enroll_release_files_download_the_qspi_variant(self, monkeypatch, tmp_path):
        fetched = []
        payloads = {
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3.bin": b"app",
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3-bootloader.bin": b"boot",
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3-partitions.bin": b"parts",
        }
        manifest = _release_manifest_text("v0.1.3", "esp32s3-i2s-gps", payloads,
                                          psram_mode="quad")

        def fake_fetch(url, timeout=60):
            name = url.rsplit("/", 1)[-1]
            fetched.append(name)
            if name == release_manifest.MANIFEST_NAME:
                return manifest.encode()
            return payloads[name]

        monkeypatch.setattr(enroll, "fetch", fake_fetch)
        enroll.release_files("v0.1.3", str(tmp_path), "esp32s3-i2s-gps", "quad")
        assert fetched == [
            release_manifest.MANIFEST_NAME,
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3.bin",
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3-bootloader.bin",
            "hear_node-esp32s3-i2s-gps-qspi-v0.1.3-partitions.bin",
        ]

    def test_enroll_release_files_falls_back_to_sha256sums_for_legacy_releases(self, monkeypatch, tmp_path):
        fetched = []

        def fake_fetch(url, timeout=60):
            name = url.rsplit("/", 1)[-1]
            fetched.append(name)
            if name == release_manifest.MANIFEST_NAME:
                raise OSError("404")
            return b"deadbeef  *file\n" if name == "SHA256SUMS" else name.encode()

        checked = []
        monkeypatch.setattr(enroll, "fetch", fake_fetch)
        monkeypatch.setattr(enroll, "check_sums", lambda sums, name, data: checked.append((name, data)))
        enroll.release_files("v0.1.3", str(tmp_path), "esp32s3-i2s-gps")
        assert fetched == [
            release_manifest.MANIFEST_NAME,
            "SHA256SUMS",
            "hear_node-esp32s3-i2s-gps-v0.1.3.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-bootloader.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-partitions.bin",
        ]
        assert [name for name, _ in checked] == [
            "hear_node-esp32s3-i2s-gps-v0.1.3.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-bootloader.bin",
            "hear_node-esp32s3-i2s-gps-v0.1.3-partitions.bin",
        ]

    def test_flash_release_image_uses_the_manifest_when_present(self, monkeypatch, tmp_path):
        name = "hear_node-esp32s3-i2s-gps-qspi-v0.1.3.bin"
        payload = b"app"
        manifest = _release_manifest_text("v0.1.3", "esp32s3-i2s-gps", {name: payload},
                                          psram_mode="quad")
        fetched = []

        def fake_fetch(url, timeout=60):
            tail = url.rsplit("/", 1)[-1]
            fetched.append(tail)
            if tail == release_manifest.MANIFEST_NAME:
                return manifest.encode()
            if tail == name:
                return payload
            raise AssertionError(tail)

        monkeypatch.setattr(flash, "REPO", str(tmp_path))
        monkeypatch.setattr(enroll, "fetch", fake_fetch)
        path = flash.release_image("v0.1.3", "esp32s3-i2s-gps", "quad")
        assert pathlib.Path(path).read_bytes() == payload
        assert fetched == [release_manifest.MANIFEST_NAME, name]
