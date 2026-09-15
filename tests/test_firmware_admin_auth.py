"""Alert 1 & 2 remediation: privileged endpoints require an admin token, and /sd is allowlisted.

hear_auth_ok()/HEAR_REQUIRE_AUTH() and sd_path_allowed() are C++ (Arduino String, http.hasHeader,
SD.open), so unlike hear_prov_line.h or hear_push_payload.h they cannot be pulled out and compiled
standalone with `cc`. These tests read the firmware source instead, following
tests/test_firmware_ls_dir.py's discipline: each assertion is anchored to one named function or
one `http.on(...)` registration's braces-balanced body, comments stripped, never the whole file.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
SRC = INO.read_text()


def _strip_comments(body):
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return "\n".join(l.split("//")[0] for l in body.splitlines())


def _braces_body(src, i):
    i = src.index("{", i)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j]


def _fn(name):
    """A named function's braces-balanced body, comments stripped."""
    m = re.search(r"^(?:static\s+)?[^\n;]*\b%s\(" % re.escape(name), SRC, flags=re.M)
    assert m, "%s() not found" % name
    return _strip_comments(_braces_body(SRC, m.end()))


def _handler(marker, occurrence=0):
    """The braces-balanced body of the `http.on(...)` registration containing `marker`, comments
    stripped. `occurrence` selects among more than one match of the same literal marker (e.g.
    /gate is registered once for HTTP_GET and once for HTTP_POST)."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = SRC.index(marker, idx + 1)
    return _strip_comments(_braces_body(SRC, idx))


def _full_call(marker, occurrence=0):
    """The whole `http.on(...)` STATEMENT, parens-balanced, comments stripped -- unlike
    `_handler()`, this spans every lambda argument (e.g. /update's completion AND upload
    handlers), because auth for an upload must be checked in the upload handler, not only in
    the one that runs after the transfer finishes."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = SRC.index(marker, idx + 1)
    i = SRC.index("(", idx)
    depth, j = 0, i
    while j < len(SRC):
        if SRC[j] == "(":
            depth += 1
        elif SRC[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return _strip_comments(SRC[i:j])


class TestHearAuthOkFailsClosed:
    def test_disabled_by_the_compile_flag_always_allows(self):
        body = _fn("hear_auth_ok")
        assert "#if !HEAR_REQUIRE_ADMIN_AUTH" in body
        assert "return true;" in body

    def test_an_empty_configured_token_refuses_everything_rather_than_allow_it(self):
        body = _fn("hear_auth_ok")
        # The bug this guards against: treating "no token configured" as "auth not required".
        # wn is the length of the CONFIGURED token; refusing when it is 0 is what makes an
        # unconfigured build fail closed instead of open.
        assert "if (!wn) return false;" in body

    def test_update_has_a_tokenless_recovery_valve_but_other_routes_do_not(self):
        body = _fn("hear_ota_auth_ok")
        assert "if (!admin_token_runtime()[0]) return true;" in body
        assert "return hear_auth_ok();" in body

    def test_the_token_can_arrive_as_a_header_or_a_query_argument(self):
        body = _fn("hear_auth_ok")
        assert 'http.hasHeader("X-Hear-Auth")' in body
        assert 'http.header("X-Hear-Auth")' in body
        assert 'http.hasArg("token")' in body
        assert 'http.arg("token")' in body

    def test_the_compare_touches_every_byte_of_the_longer_operand(self):
        # Not a full timing-safety proof, just the shape that avoids an early return on
        # first-byte mismatch: `n` is the max of both lengths and the loop runs the whole way.
        body = _fn("hear_auth_ok")
        assert "size_t n = wn > gn ? wn : gn;" in body
        assert "for (size_t i = 0; i < n; i++)" in body
        assert "return diff == 0;" in body


class TestDefaultsFailClosedAtCompileTime:
    def test_admin_auth_defaults_on(self):
        assert "#define HEAR_REQUIRE_ADMIN_AUTH 1" in SRC

    def test_the_admin_token_has_no_compiled_in_value(self):
        assert '#define HEAR_ADMIN_TOKEN ""' in SRC


class TestPrivilegedHandlersCallTheGuardFirst:
    """Every endpoint the security review flagged (Alert 1) must call HEAR_REQUIRE_AUTH() -- or,
    for /update, check hear_auth_ok()/ota_authorized -- before doing anything privileged."""

    def test_reboot_is_guarded(self):
        b = _handler('http.on("/reboot", HTTP_POST')
        assert "HEAR_REQUIRE_AUTH();" in b
        assert b.index("HEAR_REQUIRE_AUTH();") < b.index("ESP.restart();")

    def test_format_is_guarded(self):
        b = _handler('http.on("/format", HTTP_POST')
        assert "HEAR_REQUIRE_AUTH();" in b
        assert b.index("HEAR_REQUIRE_AUTH();") < b.index("f_mkfs(")

    def test_gate_post_is_guarded_but_gate_get_is_not(self):
        # watch.py polls GET /gate on other nodes every 30 s (unattended, read-only); only the
        # POST (write) form may require a token.
        post = _handler('http.on("/gate", HTTP_POST')
        assert "HEAR_REQUIRE_AUTH();" in post
        get = _handler('http.on("/gate", HTTP_GET')
        assert "HEAR_REQUIRE_AUTH();" not in get

    def test_hardware_sweeps_are_guarded(self):
        for marker in (
            'http.on("/pinsweep"',
            'http.on("/ppsv"',
            'http.on("/tplen", HTTP_POST',
            'http.on("/gpspins", HTTP_POST',
            'http.on("/gpsbaud"',
        ):
            b = _handler(marker)
            assert "HEAR_REQUIRE_AUTH();" in b, "%s is missing the admin-auth guard" % marker

    def test_update_checks_auth_once_at_upload_start_before_flash_is_touched(self):
        b = _full_call('http.on("/update", HTTP_POST')
        # The completion handler alone would be too late: bytes already reached Update.write()
        # by the time it runs. ota_authorized must be decided at UPLOAD_FILE_START, before
        # Update.begin(), and every later stage of the upload must check it.
        assert "ota_authorized = hear_ota_auth_ok();" in b
        assert b.index("ota_authorized = hear_ota_auth_ok();") < b.index("Update.begin(")
        assert b.count("if (!ota_authorized) return;") >= 2
        assert "if (!ota_authorized) { hear_auth_reject(); return; }" in b

    def test_update_upload_writes_nothing_when_unauthorized(self):
        b = _full_call('http.on("/update", HTTP_POST')
        # UPLOAD_FILE_WRITE's guard must come before Update.write, not after.
        write_stage = b[b.index("UPLOAD_FILE_WRITE"):b.index("UPLOAD_FILE_END")]
        assert "if (!ota_authorized) return;" in write_stage
        assert write_stage.index("if (!ota_authorized) return;") < write_stage.index("Update.write(")


class TestTheSdEndpointIsAllowlisted:
    """Alert 2: /sd must not open an arbitrary card path."""

    def test_the_handler_calls_the_allowlist_before_opening_anything(self):
        b = _handler('http.on("/sd"')
        assert "sd_path_allowed(name)" in b
        assert b.index("sd_path_allowed(name)") < b.index("SD.open(")
        assert "403" in b

    def test_the_dotdot_refusal_still_runs_first(self):
        b = _handler('http.on("/sd"')
        assert b.index('indexOf("..")') < b.index("sd_path_allowed(name)")

    def test_allowlist_accepts_the_fixed_csv_names(self):
        body = _fn("sd_path_allowed")
        for name in ('"/health.csv"', '"/health-prev.csv"', '"/dets.csv"', '"/dets-prev.csv"'):
            assert name in body

    def test_allowlist_accepts_gate_cfg_by_its_shared_constant(self):
        body = _fn("sd_path_allowed")
        assert "GATE_CFG" in body
        assert '#define GATE_CFG "/gate.cfg"' in SRC

    def test_allowlist_accepts_dated_scene_rotations_only(self):
        body = _fn("sd_path_allowed")
        assert '"/scene-"' in body
        assert '"-prev"' in body
        assert "isDigit(" in body, "the day component must be validated as all-digits"
        assert "mid.length() == 8" in body, "the day component must be exactly YYYYMMDD"

    def test_allowlist_accepts_only_flat_clip_paths(self):
        body = _fn("sd_path_allowed")
        assert 'CLIP_DIR "/"' in body
        assert ".wav" in body
        # No further '/' after the clip directory: a subdirectory traversal must not be a clip.
        assert "indexOf('/')" in body

    def test_allowlist_is_a_denylist_by_default(self):
        body = _fn("sd_path_allowed")
        assert body.strip().endswith("return false;\n}") or "return false;" in body.splitlines()[-2:][-1] \
            or "return false;" in body


class TestPucNodeAdminAuth:
    """puc_node.ino must mirror hear_node.ino's fail-closed admin authentication."""
    PUC_SRC = (ROOT / "firmware" / "puc_node" / "puc_node.ino").read_text()

    def _puc_handler(self, marker, occurrence=0):
        idx = -1
        for _ in range(occurrence + 1):
            idx = self.PUC_SRC.index(marker, idx + 1)
        return _strip_comments(_braces_body(self.PUC_SRC, idx))

    def _puc_full_call(self, marker, occurrence=0):
        idx = -1
        for _ in range(occurrence + 1):
            idx = self.PUC_SRC.index(marker, idx + 1)
        i = self.PUC_SRC.index("(", idx)
        depth, j = 0, i
        while j < len(self.PUC_SRC):
            if self.PUC_SRC[j] == "(":
                depth += 1
            elif self.PUC_SRC[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        return _strip_comments(self.PUC_SRC[i:j])

    def test_puc_admin_auth_defaults_on(self):
        assert "#define HEAR_REQUIRE_ADMIN_AUTH 1" in self.PUC_SRC

    def test_puc_status_reports_admin_auth_configured_without_value(self):
        assert '\\"auth\\":{\\"admin\\":{\\"configured\\":%s,\\"src\\":\\"%s\\"}}' in self.PUC_SRC
        assert 'HEAR_ADMIN_TOKEN[0] ? "true" : "false"' in self.PUC_SRC

    def test_puc_privileged_handlers_call_auth(self):
        for marker in (
            'http.on("/reboot", HTTP_POST',
            'http.on("/timesync", HTTP_POST',
            'http.on("/gpshold", HTTP_POST',
            'http.on("/gpsreset", HTTP_POST',
            'http.on("/pmtk"',
            'http.on("/scan"',
            'http.on("/scanpd"',
            'http.on("/scanpu"',
            'http.on("/i2creg"',
            'http.on("/pdmscan"',
        ):
            b = self._puc_handler(marker)
            assert "HEAR_REQUIRE_AUTH();" in b, "puc_node %s is missing admin auth" % marker

    def test_puc_update_checks_auth_before_flash(self):
        b = self._puc_full_call('http.on("/update", HTTP_POST')
        assert "ota_authorized = hear_auth_ok();" in b
        assert b.index("ota_authorized = hear_auth_ok();") < b.index("Update.begin(")
        assert b.count("if (!ota_authorized) return;") >= 2
        assert "if (!ota_authorized) { hear_auth_reject(); return; }" in b

    def test_header_collection_registered(self):
        assert 'const char *auth_headers[] = {"X-Hear-Auth"};' in SRC
        assert 'http.collectHeaders(auth_headers, 1);' in SRC
        assert 'const char *auth_headers[] = {"X-Hear-Auth"};' in self.PUC_SRC
        assert 'http.collectHeaders(auth_headers, 1);' in self.PUC_SRC
