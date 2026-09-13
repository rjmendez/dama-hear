"""tools/check_fleet_health.py — OPNsense lease resolution, /status parsing, layered health.

Three independent things this tool has to get right, tested in that order:
  1. reading OPNsense's DHCPv4 leases API and matching a node's short name against it
  2. reading a node's own /status document into the fields health depends on
  3. turning those fields into ONLINE / DEGRADED / OFFLINE without conflating "no sky yet" with
     "unreachable" -- see the module docstring for why that distinction is the whole point.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools"))
import check_fleet_health as H  # noqa: E402


# ---------------------------------------------------------------- OPNsense leases

class TestOpnsenseLeaseParsing:
    def test_parses_rows_from_a_typical_leases_response(self):
        payload = {"rows": [
            {"address": "172.16.100.105", "hostname": "nyquist", "mac": "aa:bb:cc:00:00:01",
             "state": "active"},
            {"address": "172.16.100.116", "hostname": "mach", "mac": "aa:bb:cc:00:00:02",
             "state": "active"},
        ], "rowCount": 2, "total": 2}
        leases = H.parse_opnsense_leases(payload)
        assert leases == [
            {"hostname": "nyquist", "address": "172.16.100.105", "mac": "aa:bb:cc:00:00:01",
             "state": "active"},
            {"hostname": "mach", "address": "172.16.100.116", "mac": "aa:bb:cc:00:00:02",
             "state": "active"},
        ]

    def test_accepts_the_kea_backed_ip_key_too(self):
        # OPNsense's Kea-backed leases endpoint has used `ip` where the ISC one uses `address`;
        # both must resolve to the same normalized field.
        leases = H.parse_opnsense_leases({"rows": [{"ip": "172.16.100.50", "hostname": "rankine"}]})
        assert leases[0]["address"] == "172.16.100.50"

    def test_a_lease_with_no_hostname_yet_is_kept_but_unnamed(self):
        # a lease that has not sent its DHCP option-12 string is normal, not malformed
        leases = H.parse_opnsense_leases({"rows": [{"address": "172.16.100.9", "hostname": ""}]})
        assert leases == [{"hostname": "", "address": "172.16.100.9", "mac": "", "state": ""}]

    def test_a_row_with_no_address_at_all_is_dropped(self):
        leases = H.parse_opnsense_leases({"rows": [{"hostname": "ghost"}]})
        assert leases == []

    def test_a_non_dict_row_is_ignored_rather_than_raising(self):
        leases = H.parse_opnsense_leases({"rows": ["garbage", None,
                                                    {"address": "1.2.3.4", "hostname": "n"}]})
        assert leases == [{"hostname": "n", "address": "1.2.3.4", "mac": "", "state": ""}]

    def test_a_response_with_no_rows_key_is_rejected(self):
        with pytest.raises(ValueError):
            H.parse_opnsense_leases({"not": "a leases response"})


class TestHostnameMatching:
    LEASES = [
        {"hostname": "nyquist", "address": "172.16.100.105", "mac": "", "state": "active"},
        {"hostname": "Mach.lan", "address": "172.16.100.116", "mac": "", "state": "active"},
        {"hostname": "", "address": "172.16.100.200", "mac": "", "state": "active"},
    ]

    def test_an_exact_hostname_matches(self):
        assert H.find_lease_by_hostname(self.LEASES, "nyquist")["address"] == "172.16.100.105"

    def test_matching_is_case_insensitive(self):
        assert H.find_lease_by_hostname(self.LEASES, "NYQUIST")["address"] == "172.16.100.105"

    def test_a_trailing_domain_on_the_lease_is_ignored(self):
        # OPNsense can report `mach.lan`; the node itself is asked for as just `mach`
        assert H.find_lease_by_hostname(self.LEASES, "mach")["address"] == "172.16.100.116"

    def test_no_match_returns_none(self):
        assert H.find_lease_by_hostname(self.LEASES, "rankine") is None

    def test_a_blank_hostname_query_never_matches_the_unnamed_lease(self):
        # a lease with no hostname must not be matchable by an equally-empty query
        assert H.find_lease_by_hostname(self.LEASES, "") is None

    def test_matching_is_exact_not_substring(self):
        # ⚠️`esp32s3-5B4B40` (a chip hostname) and `nyquist` (a friendly name) share no substring
        # by construction; a fuzzy match here risks scoring the wrong node on a real fleet.
        assert H.find_lease_by_hostname(self.LEASES, "nyq") is None


class TestResolveHosts:
    LEASES = [{"hostname": "nyquist", "address": "172.16.100.105", "mac": "", "state": "active"}]

    def test_a_bare_name_resolves_from_the_leases(self):
        assert H.resolve_hosts(["nyquist"], self.LEASES) == {"nyquist": "172.16.100.105"}

    def test_an_unmatched_bare_name_is_simply_absent(self):
        assert H.resolve_hosts(["rankine"], self.LEASES) == {}

    def test_name_equals_host_is_never_looked_up(self):
        # an explicit address must not be second-guessed by a (possibly stale) lease
        assert H.resolve_hosts(["nyquist=10.0.0.9"], self.LEASES) == {}

    def test_a_full_url_is_never_looked_up_either(self):
        assert H.resolve_hosts(["http://10.0.0.9/status"], self.LEASES) == {}


class TestSplitTargetWithResolvedLeases:
    def test_a_bare_name_uses_the_resolved_address_when_available(self):
        assert H.split_target("nyquist", {"nyquist": "172.16.100.105"}) == \
            ("nyquist", "http://172.16.100.105/status")

    def test_a_bare_name_falls_back_to_mdns_when_unresolved(self):
        assert H.split_target("rankine", {"nyquist": "172.16.100.105"}) == \
            ("rankine", "http://rankine.local/status")

    def test_name_equals_host_ignores_any_resolved_map(self):
        assert H.split_target("mach=172.16.100.116", {"mach": "10.0.0.1"}) == \
            ("mach", "http://172.16.100.116/status")

    def test_a_url_is_taken_as_given_and_gains_status(self):
        assert H.split_target("http://10.0.0.1")[1] == "http://10.0.0.1/status"


# ---------------------------------------------------------------- /status parsing

class TestStatusParsing:
    @staticmethod
    def _status(**overrides):
        base = {"node": "n", "fw": "8b9d5b1", "uptime_s": 100, "sd": True, "sd_free_mb": 30000,
               "gps": {"fix": 3, "sats": 20, "tacc_ns": 26},
               "pps": {"edges": 99, "spread_us": 4, "glitches": 0},
               "time": {"valid": True, "label_rejects": 0},
               "audio": {"detections": 5, "ambient": 6.0},
               "net": {"rssi": -55, "disc": 0},
               "gate": {"floor": 200}}
        base.update(overrides)
        return base

    def test_gps_fix_and_satellites_are_read(self):
        s = H.parse_status(self._status(gps={"fix": 3, "sats": 14, "tacc_ns": 40}))
        assert s["fix"] == 3
        assert s["sats"] == 14
        assert s["tacc_ns"] == 40

    def test_pps_edges_spread_and_glitches_are_read(self):
        s = H.parse_status(self._status(pps={"edges": 500, "spread_us": 12, "glitches": 3}))
        assert s["pps_edges"] == 500
        assert s["pps_spread_us"] == 12
        assert s["pps_glitches"] == 3

    def test_time_validity_true_and_false(self):
        assert H.parse_status(self._status(time={"valid": True, "label_rejects": 0}))["time_valid"]
        assert not H.parse_status(
            self._status(time={"valid": False, "label_rejects": 9}))["time_valid"]

    def test_rssi_is_read_when_associated(self):
        assert H.parse_status(self._status(net={"rssi": -62, "disc": 1}))["rssi"] == -62

    def test_rssi_is_none_when_not_associated(self):
        # /status reports rssi: null when the node has not joined a network yet
        assert H.parse_status(self._status(net={"rssi": None, "disc": 0}))["rssi"] is None

    def test_rssi_is_none_when_net_section_is_entirely_absent(self):
        # firmware before v0.1.1 does not emit `net` at all
        d = self._status()
        del d["net"]
        assert H.parse_status(d)["rssi"] is None

    def test_missing_gps_pps_time_sections_do_not_raise(self):
        d = {"node": "bare", "sd": False}
        s = H.parse_status(d)
        assert s["fix"] is None
        assert s["pps_edges"] is None
        assert s["time_valid"] is None
        assert s["sd"] is False

    def test_sd_free_space_is_read(self):
        assert H.parse_status(self._status(sd_free_mb=42))["sd_free_mb"] == 42


# ---------------------------------------------------------------- layered health evaluation

class TestHealthEvaluation:
    @staticmethod
    def _status(**overrides):
        base = {"node": "n", "fw": "8b9d5b1", "uptime_s": 100, "sd": True, "sd_free_mb": 30000,
               "gps": {"fix": 3, "sats": 20, "tacc_ns": 26},
               "pps": {"edges": 99, "spread_us": 4, "glitches": 0},
               "time": {"valid": True, "label_rejects": 0},
               "audio": {"detections": 5, "ambient": 6.0},
               "net": {"rssi": -55, "disc": 0},
               "gate": {"floor": 200}}
        base.update(overrides)
        return base

    # -- offline: layer 1 alone, and nothing below it is graded

    def test_a_failed_fetch_is_offline(self):
        r = H.evaluate_health("nyquist", None, ConnectionRefusedError("refused"))
        assert r["state"] == "offline"
        assert "refused" in r["reasons"][0]

    def test_offline_with_no_error_object_still_reports_a_reason(self):
        r = H.evaluate_health("nyquist", None)
        assert r["state"] == "offline"
        assert r["reasons"] == ["unreachable"]

    # -- online: every layer clean

    def test_a_fully_healthy_node_is_online_with_no_reasons(self):
        r = H.evaluate_health("n", self._status())
        assert r["state"] == "online"
        assert r["reasons"] == []
        assert r["node"] == "n"

    # -- degraded: layer 2 (GPS/timebase), one condition at a time

    def test_no_3d_fix_is_degraded_not_offline(self):
        # ⚠️THE CASE THIS TOOL EXISTS TO GET RIGHT: reachable, but no sky yet.
        r = H.evaluate_health("n", self._status(gps={"fix": 0, "sats": 0, "tacc_ns": None}))
        assert r["state"] == "degraded"
        assert any("fix" in why for why in r["reasons"])

    def test_invalid_time_is_degraded(self):
        r = H.evaluate_health("n", self._status(time={"valid": False, "label_rejects": 4}))
        assert r["state"] == "degraded"
        assert "no UTC anchor" in r["reasons"]

    def test_zero_pps_edges_is_degraded(self):
        r = H.evaluate_health("n", self._status(pps={"edges": 0, "spread_us": 0, "glitches": 0}))
        assert r["state"] == "degraded"
        assert any("timebase never locked" in why for why in r["reasons"])

    def test_pps_glitches_are_degraded(self):
        r = H.evaluate_health("n", self._status(pps={"edges": 500, "spread_us": 4, "glitches": 2}))
        assert r["state"] == "degraded"
        assert "2 pps glitch(es)" in r["reasons"]

    # -- degraded: layer 3 (link / capacity)

    def test_weak_rssi_is_degraded(self):
        r = H.evaluate_health("n", self._status(net={"rssi": -90, "disc": 0}))
        assert r["state"] == "degraded"
        assert any("rssi" in why for why in r["reasons"])

    def test_strong_rssi_is_not_degraded(self):
        r = H.evaluate_health("n", self._status(net={"rssi": -40, "disc": 0}))
        assert r["state"] == "online"

    def test_missing_sd_card_is_degraded(self):
        r = H.evaluate_health("n", self._status(sd=False, sd_free_mb=None))
        assert r["state"] == "degraded"
        assert "no SD card" in r["reasons"]

    def test_low_sd_free_space_is_degraded(self):
        r = H.evaluate_health("n", self._status(sd=True, sd_free_mb=10))
        assert r["state"] == "degraded"
        assert any("MB free" in why for why in r["reasons"])

    def test_ample_sd_free_space_is_not_degraded(self):
        r = H.evaluate_health("n", self._status(sd=True, sd_free_mb=30000))
        assert r["state"] == "online"

    # -- multiple simultaneous failures are all reported, not just the first

    def test_several_failing_layers_are_all_named(self):
        r = H.evaluate_health("n", self._status(
            gps={"fix": 1, "sats": 3, "tacc_ns": 900},
            time={"valid": False, "label_rejects": 20},
            net={"rssi": -95, "disc": 3}))
        assert r["state"] == "degraded"
        assert len(r["reasons"]) >= 3

    def test_the_reported_node_name_is_the_nodes_own_when_it_answered(self):
        # a flash that landed the wrong identity is a fact worth keeping, same rule tools/fleet.py
        # already follows for its `fw` column
        r = H.evaluate_health("rankine", self._status(node="nyquist"))
        assert r["node"] == "nyquist"


# ---------------------------------------------------------------- end-to-end CLI report

class TestFleetReport:
    """Mocked fetch_status standing in for a mixed fleet: one of each state."""

    def _run(self, capsys, mapping, monkeypatch):
        monkeypatch.setattr(H, "fetch_status", lambda url: mapping[url])
        rc = H.main(list(mapping))
        return rc, capsys.readouterr().out

    @staticmethod
    def _status(**overrides):
        base = {"node": "n", "fw": "8b9d5b1", "uptime_s": 100, "sd": True, "sd_free_mb": 30000,
               "gps": {"fix": 3, "sats": 20, "tacc_ns": 26},
               "pps": {"edges": 99, "spread_us": 4, "glitches": 0},
               "time": {"valid": True, "label_rejects": 0},
               "audio": {"detections": 5, "ambient": 6.0},
               "net": {"rssi": -55, "disc": 0},
               "gate": {"floor": 200}}
        base.update(overrides)
        return base

    def test_an_online_node_is_reported_online(self, capsys, monkeypatch):
        url = "http://good/status"
        rc, out = self._run(capsys, {url: self._status(node="good")}, monkeypatch)
        assert "ONLINE" in out
        assert rc == 0

    def test_an_unreachable_node_is_reported_offline(self, capsys, monkeypatch):
        def fetch_status(url):
            raise ConnectionRefusedError("refused")
        monkeypatch.setattr(H, "fetch_status", fetch_status)
        rc = H.main(["dead=1.2.3.4"])
        out = capsys.readouterr().out
        assert "OFFLINE" in out
        assert rc == 0, "the default must only report, same rule tools/fleet.py follows"

    def test_a_no_fix_node_is_reported_degraded_not_offline(self, capsys, monkeypatch):
        url = "http://nofix/status"
        rc, out = self._run(capsys, {url: self._status(node="nofix",
                                                        gps={"fix": 0, "sats": 0,
                                                             "tacc_ns": None})}, monkeypatch)
        assert "DEGRADED" in out
        assert "OFFLINE" not in out

    def test_require_online_gates_on_anything_not_online(self, monkeypatch):
        monkeypatch.setattr(H, "fetch_status",
                            lambda url: self._status(gps={"fix": 0, "sats": 0, "tacc_ns": None}))
        rc = H.main(["--require-online", "degraded=1.2.3.4"])
        assert rc == 2

    def test_require_online_passes_a_fully_healthy_fleet(self, monkeypatch):
        monkeypatch.setattr(H, "fetch_status", lambda url: self._status())
        rc = H.main(["--require-online", "good=1.2.3.4"])
        assert rc == 0

    def test_leases_file_resolves_a_bare_name_before_falling_back_to_mdns(self, capsys,
                                                                          monkeypatch, tmp_path):
        leases_file = tmp_path / "leases.json"
        leases_file.write_text(
            '{"rows": [{"address": "172.16.100.105", "hostname": "nyquist"}]}')

        seen_urls = []

        def fetch_status(url):
            seen_urls.append(url)
            return self._status(node="nyquist")

        monkeypatch.setattr(H, "fetch_status", fetch_status)
        rc = H.main(["--leases", str(leases_file), "nyquist"])
        assert rc == 0
        assert seen_urls == ["http://172.16.100.105/status"]
