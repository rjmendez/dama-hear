"""tools/fleet.py — the build-drift reporter, and the addressing that stopped it working.

⚠️THIS TOOL EXISTS TO CATCH BUILD DRIFT AND COULD NOT BE RUN WHERE DRIFT HAPPENS. It built
`http://<name>.local/status`, and `.local` does not resolve from WSL or from inside k3s -- so on
the machines anyone actually uses it reported every node UNREACHABLE. The drift it was written for
then went unnoticed for two months: three nodes on three builds, one of them a hand-built sketch
reporting `fw: unknown`.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools"))
import fleet as F  # noqa: E402


class TestAddressing:
    def test_a_bare_name_still_uses_mdns(self):
        # unchanged, for whoever runs this from a host that has mDNS
        assert F.split_target("nyquist") == ("nyquist", "http://nyquist.local/status")

    def test_name_equals_host_works_without_mdns(self):
        # ⚠️THE REGRESSION. Same form tools/hear_drain.py takes, so one node map serves both.
        assert F.split_target("mach=172.16.100.116") == ("mach", "http://172.16.100.116/status")

    def test_a_url_is_taken_as_given_and_gains_status(self):
        assert F.split_target("http://10.0.0.1/status")[1] == "http://10.0.0.1/status"
        assert F.split_target("http://10.0.0.1")[1] == "http://10.0.0.1/status"
        assert F.split_target("http://10.0.0.1/")[1] == "http://10.0.0.1/status"

    def test_the_name_is_what_gets_displayed_not_the_address(self):
        # an operator reading a drift report needs the node, not an octet
        assert F.split_target("rankine=172.16.100.50")[0] == "rankine"

    @pytest.mark.parametrize("host", ["172.16.100.50", "nyquist.local", "esp32s3-5B4B40"])
    def test_any_host_form_is_accepted_after_the_equals(self, host):
        assert F.split_target("n=%s" % host)[1] == "http://%s/status" % host


class TestDriftReport:
    """The whole point: one build is quiet, more than one is loud."""

    @staticmethod
    def _status(fw, node="n", sats=20, fix=3):
        return {"node": node, "fw": fw, "uptime_s": 100, "sd": True, "sd_free_mb": 30000,
                "gps": {"fix": fix, "sats": sats, "tacc_ns": 26},
                "pps": {"edges": 99, "spread_us": 4, "glitches": 0},
                "time": {"valid": True, "label_rejects": 0},
                "audio": {"detections": 5, "ambient": 6.0},
                "gate": {"floor": 200}}

    def _run(self, capsys, mapping, monkeypatch):
        monkeypatch.setattr(F, "fetch", lambda n: mapping[n])
        F.main(list(mapping))
        return capsys.readouterr().out

    def test_one_build_reports_agreement(self, capsys, monkeypatch):
        out = self._run(capsys, {"a=1": self._status("8b9d5b1", "a"),
                                 "b=2": self._status("8b9d5b1", "b")}, monkeypatch)
        assert "all 2 nodes on 8b9d5b1" in out
        assert "SPLIT" not in out

    def test_a_split_fleet_is_called_out(self, capsys, monkeypatch):
        out = self._run(capsys, {"a=1": self._status("8b9d5b1", "a"),
                                 "b=2": self._status("unknown", "b")}, monkeypatch)
        assert "FLEET IS SPLIT" in out
        assert "8b9d5b1" in out and "unknown" in out

    def test_a_dirty_build_is_flagged_even_when_they_agree(self, capsys, monkeypatch):
        # ⚠️two nodes on the same -dirty string are NOT thereby on the same code
        out = self._run(capsys, {"a=1": self._status("8b9d5b1-dirty", "a"),
                                 "b=2": self._status("8b9d5b1-dirty", "b")}, monkeypatch)
        assert "did not come from a commit" in out

    def test_rows_are_labelled_by_the_node_not_the_argument(self, capsys, monkeypatch):
        # a flash that lands the wrong identity is the failure the fw column exists for, so the
        # report must show what ANSWERED, never what was asked for
        out = self._run(capsys, {"rankine=172.16.100.50": self._status("8b9d5b1", "nyquist")},
                        monkeypatch)
        assert "nyquist" in out


class TestTheSplitGate(TestDriftReport):
    """`--require-one-build` — opt-in, and ONLY for the split.

    The default stays report-only on purpose: a node with no sky yet is not a failure, and a tool
    that cries wolf about tonight teaches its operator to ignore it. A split fleet is different in
    kind -- never transient, never self-healing, and it silently invalidates the capture.
    """

    def _rc(self, mapping, monkeypatch, flag=True):
        monkeypatch.setattr(F, "fetch", lambda n: mapping[n])
        args = (["--require-one-build"] if flag else []) + list(mapping)
        return F.main(args)

    def test_a_split_is_an_error_when_asked_for(self, capsys, monkeypatch):
        rc = self._rc({"a=1": self._status("aaa", "a"), "b=2": self._status("bbb", "b")},
                      monkeypatch)
        assert rc != 0

    def test_a_split_is_still_only_a_report_by_default(self, capsys, monkeypatch):
        rc = self._rc({"a=1": self._status("aaa", "a"), "b=2": self._status("bbb", "b")},
                      monkeypatch, flag=False)
        assert rc == 0, "the default must not gate -- that is the whole design of this tool"

    def test_one_build_passes_the_gate(self, capsys, monkeypatch):
        assert self._rc({"a=1": self._status("x", "a"), "b=2": self._status("x", "b")},
                        monkeypatch) == 0

    def test_the_gate_says_nothing_about_a_node_with_no_fix(self, capsys, monkeypatch):
        # ⚠️the gate is for the split ALONE. A node still acquiring must not fail a scheduled run.
        assert self._rc({"a=1": self._status("x", "a"),
                         "b=2": self._status("x", "b", sats=0, fix=0)}, monkeypatch) == 0
