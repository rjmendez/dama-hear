"""What a node reports about its own link and its own health, so a node that goes quiet can be
told apart from a node on a bad link, a node that reset, and a node whose loop stalled.

Written after mach's link fell from ~120 kB/s to ~10 kB/s with 5 % packet loss and every drain
came back truncated, while nothing the node exported could say why. Sources are parsed with string
literals and comments handled, so this file's prose cannot satisfy it.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
NET = ROOT / "firmware" / "lib" / "hear_platform" / "src" / "hear_net.cpp"
sys.path.insert(0, str(ROOT / "tools"))
import fleet  # noqa: E402

LITERAL = re.compile(r'"(?:\\.|[^"\\\n])*"')


def _code(p):
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


def test_the_join_ranks_configured_networks_by_what_the_scan_heard():
    b = _body(_code(NET), "int hear_net_join(")
    assert "WiFi.scanNetworks(" in b and "WiFi.scanDelete()" in b
    assert re.search(r"best\[order\[b\]\]\s*>\s*best\[order\[b\s*-\s*1\]\]", b), "not sorted strongest first"


def test_within_a_network_the_driver_takes_the_strongest_access_point_and_nothing_is_pinned():
    """The default WIFI_FAST_SCAN joins the first matching access point heard, and sort-by-signal
    only applies to an all-channel scan. A BSSID pin would do the join's job once and then strand
    the node on that one access point for every automatic reconnect."""
    b = _body(_code(NET), "int hear_net_join(")
    assert "WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN)" in b
    assert "WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL)" in b
    assert b.index("setScanMethod") < b.index("WiFi.begin(")
    for m in re.finditer(r"WiFi\.begin\(([^;]*)\);", b):
        assert m.group(1).replace(" ", "") == "p->ssid[k],p->psk[k]", "WiFi.begin is pinned: %s" % m.group(0)


def test_failed_attempts_during_the_join_are_not_counted_as_drops():
    b = _body(_code(NET), "int hear_net_join(")
    ok = b[b.index("WL_CONNECTED) {"):]
    assert re.search(r"disc_n\s*=\s*0", ok) and re.search(r"reconn_n\s*=\s*0", ok)


def test_a_disconnect_is_counted_with_its_reason():
    b = _body(_code(NET), "void hear_net_watch(")
    assert "ARDUINO_EVENT_WIFI_STA_DISCONNECTED" in b
    assert re.search(r"disc_n\s*=\s*disc_n\s*\+\s*1|disc_n\+\+", b)
    assert "info.wifi_sta_disconnected.reason" in b


def test_setup_joins_through_the_ranked_join_after_watching():
    setup = _body(_code(INO), "void setup(")
    assert setup.index("hear_net_watch()") < setup.index("hear_net_join(&prov")
    assert "WiFi.begin(" not in setup


def test_a_slow_link_is_given_time_and_every_give_up_is_counted():
    code = _code(INO)
    m = re.search(r"#define\s+STREAM_STALL_MS\s+(\d+)u?", code)
    assert m and int(m.group(1)) >= 15000, "a 5 s stall limit truncates every download on a weak link"
    b = _body(code, "static bool stream_ready(")
    assert "stream_stall_n++" in b and "stream_gone_n++" in b


def test_health_rows_carry_the_link_and_the_loop_appended_at_the_end():
    code = _code(INO)
    i = code.index("HEALTH_HDR[]")
    hdr = "".join(re.findall(r'"([^"]*)"', code[i:code.index(";", i)]))
    cols = hdr.split(",")
    assert cols[-6:] == ["rssi", "wifi_disc", "wifi_reason", "loop_max_ms", "heap_min", "stream_stalls"]
    assert cols.index("ubx_silent_max") == len(cols) - 7, "new columns must be APPENDED"


def test_the_per_row_loop_maximum_restarts_with_the_row():
    code = _code(INO)
    assert re.search(r"env_e_max_win = 0\.0f;\s*loop_max_us = 0;", code)


def test_status_reports_the_link_and_the_system():
    code = _code(INO)
    for field in ("net", "rssi", "rssi_join", "bssid", "disc", "reason", "sys", "reset",
                  "heap_min", "loop_max_ms", "stream_stalls"):
        assert re.search(r'\\"%s\\":' % field, code), field


def test_the_loop_measures_its_own_period():
    b = _body(_code(INO), "void loop(")
    assert b.index("micros()") < b.index("http.handleClient()")
    assert "loop_max_boot_us" in b


def test_no_credential_reaches_a_log_line_in_the_join():
    s = LITERAL.sub('""', NET.read_text())
    s = re.sub(r"//[^\n]*", "", s)
    calls = re.findall(r"\bhear_logf\s*\(([^;]*)\);", s)
    assert len(calls) >= 2
    for args in calls:
        assert not re.search(r"\b(?:psk|ssid)\b", args), args


class TestFleetRow:
    BASE = {"fw": "v0.1.1", "uptime_s": 60,
            "gps": {"fix": 3, "sats": 12, "tacc_ns": 25},
            "pps": {"edges": 60, "spread_us": 9, "glitches": 0},
            "time": {"valid": True, "label_rejects": 0},
            "audio": {"detections": 1, "ambient": 12.0},
            "gate": {"floor": 200}, "sd": True, "sd_free_mb": 29000}

    def test_it_shows_the_link(self):
        d = dict(self.BASE, net={"rssi": -71, "disc": 2})
        line = fleet.row("mach", d)
        assert "-71" in line and "disc 2" in line

    def test_firmware_without_the_fields_still_prints(self):
        line = fleet.row("mach", dict(self.BASE))
        assert line.startswith("mach") and "rssi" in line
