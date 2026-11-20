import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import bma_field_report as report
from hear.ingest.bma_fleet import convert_fleet


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs():
    pose = {
        "origin_id": "arp", "position": {"e_m": -0.75, "n_m": 0, "u_m": -3, "sigma_m": 0.5},
        "orientation": {"heading_deg": 90, "pitch_deg": 45, "roll_deg": -90,
                        "port_normal": "-z", "sigma_heading_deg": 10},
        "source": "operator estimate", "surveyed_utc": "2026-11-14T21:30:00Z",
    }
    bma = {"observations": [{
        "unit_id": "bma1", "det_id": "bma1:5:1", "source_time_wall_s": 990,
        "origin_id": "arp", "pose": pose, "bearing_deg_true": 18.97, "sigma_deg": 3,
        "clock_precise": False, "evidence": "unvalidated",
    }], "refusals": []}
    hb = {"last_run_s": 999, "sensors": {
        "nyquist": {"kind": "node", "last_success_s": 900, "last_attempt_s": 999,
                    "last_error": None, "last_added": 2, "uptime_s": 300,
                    "unfetched_recent": [{"at": 999, "bytes": 0, "missing": False}]}}}
    status = {"node": "nyquist", "time": {"valid": True, "label_rejects": 2},
              "gps": {"fix": 3}, "pps": {"spread_us": 13}}
    fleet = {"v": 1, "k": "fleet", "origin_id": "arp", "updated_s": 995,
             "units": [{"unit_id": "bma1", "clock": {"domain": "LOCAL"},
                        "fused": False, "not_fused_reason": "clock_local"}]}
    return bma, hb, status, fleet


def test_offline_cli_replays_exact_evidence(tmp_path, inputs):
    bma, hb, status, fleet = inputs
    files = []
    for name, value in (("bma", bma), ("heartbeat", hb), ("status", status), ("fleet", fleet)):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        files.append(path)
    command = [sys.executable, str(ROOT / "tools" / "bma_field_report.py"),
               "--bma", str(files[0]), "--heartbeat", str(files[1]), "--status", str(files[2]),
               "--bma-fleet", str(files[3]), "--bma-site", "site-a", "--dama-site", "site-a",
               "--as-of-s", "1000", "--max-stale-s", "100"]
    before = [p.read_bytes() for p in files]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert (run.returncode, run.stderr) == (0, "")
    out = json.loads(run.stdout)
    assert out["site"] == "site-a"
    assert out["bma"]["conversion"] == bma
    assert out["bma"]["fleet_snapshot"] == fleet
    assert out["bma"]["fleet_freshness"] == {"state": "fresh", "age_s": 5}
    assert out["dama"]["heartbeat"] == hb
    assert out["dama"]["heartbeat_state"] == "recorded"
    assert out["dama"]["nodes"] == {"nyquist": {
        "drain_success": {"state": "fresh", "age_s": 100},
        "status_clock": "valid", "status_capture_freshness": "unknown", "status": status}}
    assert [p.read_bytes() for p in files] == before


def test_degraded_and_unknown_evidence_is_preserved(inputs):
    bma, hb, status, fleet = inputs
    bma["refusals"] = [{"reason": "stale_ray", "ray_index": 1, "unit_id": "bma2", "det_id": "bma2:5:2"},
                       {"reason": "invalid_identity", "ray_index": 2, "unit_id": "", "det_id": None}]
    hb["sensors"]["nyquist"].update(last_error="connection reset", last_success_s=899,
                                    last_unfetched_bytes=None,
                                    clips_recent=[{"unknown": True, "reason": "timeout"}])
    hb["sensors"]["mach"] = {"kind": "node", "last_error": "timeout"}
    status["time"]["valid"] = False
    fleet["updated_s"] = 899
    out = report.report(bma, hb, [status], "s", "s", 1000, 100, fleet)
    assert out["bma"]["conversion"]["refusals"] == bma["refusals"]
    assert out["bma"]["fleet_freshness"] == {"state": "stale", "age_s": 101}
    assert out["bma"]["fleet_snapshot"]["units"][0]["not_fused_reason"] == "clock_local"
    assert out["dama"]["heartbeat"]["sensors"]["nyquist"]["clips_recent"] == [
        {"unknown": True, "reason": "timeout"}]
    assert out["dama"]["heartbeat"]["sensors"]["nyquist"]["last_error"] == "connection reset"
    assert out["dama"]["nodes"]["nyquist"]["drain_success"] == {"state": "stale", "age_s": 101}
    assert out["dama"]["nodes"]["nyquist"]["status_clock"] == "invalid"
    assert out["dama"]["nodes"]["mach"] == {
        "drain_success": {"state": "unknown", "age_s": None}, "status_clock": "unknown",
        "status_capture_freshness": "unknown", "status": None}


def test_future_times_and_absent_status_do_not_look_fresh(inputs):
    bma, hb, _, fleet = inputs
    hb["sensors"]["nyquist"]["last_success_s"] = 1001
    fleet["updated_s"] = 1002
    out = report.report(bma, hb, [], "s", "s", 1000, 100, fleet)
    assert out["dama"]["nodes"]["nyquist"]["drain_success"] == {"state": "future", "age_s": -1}
    assert out["dama"]["nodes"]["nyquist"]["status_clock"] == "unknown"
    assert out["bma"]["fleet_freshness"] == {"state": "future", "age_s": -2}
    assert out["bma"]["conversion"]["observations"][0]["clock_precise"] is False


def test_real_converter_output_can_be_replayed(inputs, tmp_path):
    _, hb, status, _ = inputs
    pose = {"origin_id": "arp", "position": {"e_m": 1, "n_m": 2, "u_m": 3, "sigma_m": 0.5},
            "orientation": {"heading_deg": 90, "pitch_deg": 0, "roll_deg": 0,
                            "port_normal": "+z", "sigma_heading_deg": 10},
            "source": "synthetic", "surveyed_utc": "2026-11-14T21:30:00Z"}
    fleet = {"v": 1, "k": "fleet", "origin_id": "arp", "updated_s": 990,
             "units": [{"unit_id": "bma1", "pose_state": "surveyed", "pose": pose,
                        "ambiguous": False, "fused": False, "not_fused_reason": "clock_local",
                        "clock": {"domain": "LOCAL"}}],
             "rays": [{"unit_id": "bma1", "det_id": "bma1:5:1", "t_wall_s": 989,
                       "kind": "azel", "az_deg_local": 0, "el_deg_local": 0, "sigma_deg": 3,
                       "bearing_deg_world": 90, "mirror_bearings_deg_world": None},
                      {"unit_id": "bma1", "det_id": "bma1:5:2", "kind": "cone"}]}
    origin = {"origin_id": "arp", "lat": 40, "lon": -79, "alt_m": 200,
              "datum": "WGS84", "north": "true"}
    converted = convert_fleet(fleet, origin)
    out = report.report(converted, hb, [status], "s", "s", 1000, 100, fleet)
    assert out["bma"]["conversion"] == {
        "observations": [{"unit_id": "bma1", "det_id": "bma1:5:1", "source_time_wall_s": 989,
                          "origin_id": "arp", "pose": pose, "bearing_deg_true": 90,
                          "sigma_deg": 3, "clock_precise": False, "evidence": "unvalidated"}],
        "refusals": [{"reason": "unsupported_ray", "ray_index": 1,
                      "unit_id": "bma1", "det_id": "bma1:5:2"}]}
    assert out["bma"]["fleet_snapshot"]["units"][0]["clock"] == {"domain": "LOCAL"}
    files = []
    for name, value in (("fleet", fleet), ("origin", origin), ("heartbeat", hb)):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        files.append(path)
    command = [sys.executable, str(ROOT / "tools" / "bma_field_report.py"),
               "--fleet", str(files[0]), "--origin", str(files[1]), "--heartbeat", str(files[2]),
               "--bma-site", "s", "--dama-site", "s", "--as-of-s", "1000",
               "--max-stale-s", "100"]
    before = [p.read_bytes() for p in files]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert (run.returncode, run.stderr) == (0, "")
    cli = json.loads(run.stdout)
    assert cli["bma"]["conversion"] == out["bma"]["conversion"]
    assert cli["bma"]["fleet_snapshot"] == fleet
    assert cli["bma"]["fleet_freshness"] == {"state": "fresh", "age_s": 10}
    assert [p.read_bytes() for p in files] == before

    stale_command = command.copy()
    stale_command[-1] = "5"
    stale = subprocess.run(stale_command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert stale.returncode == 0
    assert json.loads(stale.stdout)["bma"]["conversion"] == {
        "observations": [], "refusals": [{"reason": "stale_feed", "ray_index": None,
                                          "unit_id": None, "det_id": None}]}
    origin["datum"] = "invalid"
    files[1].write_text(json.dumps(origin), encoding="utf-8")
    invalid = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert (invalid.returncode, invalid.stdout, invalid.stderr.strip()) == (
        2, "", "error: BMA converter refused input: invalid_origin")


@pytest.mark.parametrize("change, message", [
    ("site", "site mismatch"),
    ("embedded_site", "heartbeat site mismatch"),
    ("origin", "pose origin mismatch"),
    ("fleet_origin", "BMA fleet origin mismatch"),
    ("clock", "must retain unvalidated and imprecise clock labels"),
    ("bad_sensor", "last_success_s must be a finite number"),
    ("status_node", "status node not present in heartbeat"),
    ("duplicate_status", "duplicate status for node"),
    ("bad_status_clock", "status.time.valid must be boolean or null"),
])
def test_rejects_invalid_identity_or_health_with_valid_twin(inputs, change, message):
    bma, hb, status, fleet = copy.deepcopy(inputs)
    assert report.report(bma, hb, [status], "s", "s", 1000, 100, fleet)["site"] == "s"
    bma_site, dama_site, statuses = "s", "s", [status]
    if change == "site":
        dama_site = "other"
    elif change == "embedded_site":
        hb["site_id"] = "other"
    elif change == "origin":
        bma["observations"][0]["pose"]["origin_id"] = "other"
    elif change == "fleet_origin":
        fleet["origin_id"] = "other"
    elif change == "clock":
        bma["observations"][0]["clock_precise"] = True
    elif change == "bad_sensor":
        hb["sensors"]["nyquist"]["last_success_s"] = True
    elif change == "status_node":
        status["node"] = "other"
    elif change == "duplicate_status":
        statuses = [status, status]
    else:
        status["time"]["valid"] = "false"
    with pytest.raises(report.InputError, match=message):
        report.report(bma, hb, statuses, bma_site, dama_site, 1000, 100, fleet)


@pytest.mark.parametrize("invalid", [
    '{"observations":[],"observations":[],"refusals":[]}',
    '{"observations":[],"refusals":[],"extra":NaN}',
    '{"observations":[],"refusals":[],"extra":1e999}',
    '{"observations":',
])
def test_invalid_json_rejected_without_stdout(tmp_path, inputs, invalid):
    bma, hb, _, _ = inputs
    path = tmp_path / "bma.json"
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps(hb), encoding="utf-8")
    command = [sys.executable, str(ROOT / "tools" / "bma_field_report.py"), "--bma", str(path),
               "--heartbeat", str(heartbeat), "--bma-site", "s", "--dama-site", "s",
               "--as-of-s", "1000"]
    path.write_text(json.dumps(bma), encoding="utf-8")
    good = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert (good.returncode, json.loads(good.stdout)["site"]) == (0, "s")
    path.write_text(invalid, encoding="utf-8")
    bad = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert (bad.returncode, bad.stdout) == (2, "")
    assert bad.stderr.startswith("error: ")
