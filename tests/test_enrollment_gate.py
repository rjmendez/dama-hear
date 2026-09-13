"""Deployment readiness must come from fresh, healthy live evidence."""
from datetime import datetime, timezone
import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "hear_node"))
import enroll  # noqa: E402


GATE_CHECK = (pathlib.Path.home() / ".copilot" / "session-state"
              / "0c8a4016-1534-4cf6-ba62-c47c4e09d282" / "files"
              / "fleet-enrollment-rollout" / "scripts" / "gate_check.py")


@pytest.fixture(scope="module")
def gate_mod():
    if not GATE_CHECK.exists():
        pytest.skip("session rollout gate script is not present in this environment")
    import importlib.util
    spec = importlib.util.spec_from_file_location("fleet_gate_check", GATE_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


READY_TODOS = {
    "gold-production-firmware-build": "done",
    "multi-board-ota-safety": "done",
    "gold-three-node-replication-gate": "done",
}


def _evidence(selftest, captured_at="2026-09-12T20:52:00+00:00"):
    return {
        "captured_at": captured_at,
        "hardware": {"flash_bytes": 8 * 1024 * 1024, "psram_bytes": 2 * 1024 * 1024},
        "selftest": selftest,
    }


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_a_stale_evidence_file_is_rejected(tmp_path, gate_mod):
    p = _write_json(tmp_path / "gold.json", _evidence(
        {"mic": "ok", "gps": "ok", "pps": "ok", "wifi": "ok"},
        captured_at="2026-09-12T20:00:00+00:00",
    ))
    now = datetime(2026, 9, 12, 21, 0, tzinfo=timezone.utc).timestamp()
    reasons = gate_mod.gate("gold", p, True, READY_TODOS, release_rate_hz=48000,
                            stale_max_age_s=300, now=now)
    assert any("stale" in r for r in reasons), reasons


def test_an_unhealthy_live_selftest_blocks_enrollment_ready(monkeypatch):
    monkeypatch.setattr(enroll.wifi_store, "read_pairs", lambda _: [("field-net", "correcthorse")])
    monkeypatch.setattr(enroll, "exchange", lambda *args, **kwargs: "172.16.100.50")
    bad = {"node": "gold", "fw": "v0.1.0", "prov": {"src": "nvs", "nets": 1, "nvs": True},
           "selftest": {"mic": "silent", "gps": "ok", "pps": "ok", "wifi": "ok"}}
    monkeypatch.setattr(enroll.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Resp(json.dumps(bad).encode()))
    with pytest.raises(SystemExit):
        enroll.main(["gold", "/dev/ttyACM0", "--no-flash"])


def test_a_fully_healthy_fresh_live_report_passes(monkeypatch):
    monkeypatch.setattr(enroll.wifi_store, "read_pairs", lambda _: [("field-net", "correcthorse")])
    monkeypatch.setattr(enroll, "exchange", lambda *args, **kwargs: "172.16.100.50")
    good = {"node": "gold", "fw": "v0.1.0", "prov": {"src": "nvs", "nets": 1, "nvs": True},
            "selftest": {"mic": "ok", "gps": "ok", "pps": "ok", "wifi": "ok"}}
    monkeypatch.setattr(enroll.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Resp(json.dumps(good).encode()))
    assert enroll.main(["gold", "/dev/ttyACM0", "--no-flash"]) == 0
