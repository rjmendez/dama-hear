"""Synthetic contract examples only: these do not validate BMA bearings."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from hear.ingest.bma_fleet import convert_fleet


def test_canonical_firmware_synthetic_fleet():
    # rjmendez/bma360dx-firmware origin/master 1736f3e273f61c0cd836c7ff6da36e0d22610573,
    # bench-kit/bma-audiod/contracts/fixtures/fleet_feed.json, copied verbatim.
    # This fixture and origin are fictitious, not a validated field result.
    fixture = Path(__file__).resolve().parents[1] / "testdata" / "bma-peer" / "fleet_feed.json"
    fleet = json.loads(fixture.read_text(encoding="utf-8"))
    origin = {"origin_id": "example-origin", "lat": 0, "lon": 0, "alt_m": 0,
              "datum": "WGS84", "north": "true"}
    assert convert_fleet(fleet, origin, now_s=1794000000.5, max_age_s=1) == {
        "observations": [
            {"unit_id": "bma1", "det_id": "bma1:305419896:7",
             "source_time_wall_s": 1794000000.25, "origin_id": "example-origin",
             "pose": {"origin_id": "example-origin",
                      "position": {"e_m": 0.0, "n_m": 0.0, "u_m": 1.5, "sigma_m": 0.05},
                      "orientation": {"heading_deg": 12.5, "pitch_deg": 0.0, "roll_deg": 0.0,
                                      "port_normal": "+z", "sigma_heading_deg": 1.0},
                      "source": "FICTITIOUS EXAMPLE", "surveyed_utc": "2026-11-14T00:00:00Z"},
             "bearing_deg_true": 32.0054, "sigma_deg": 2.0,
             "clock_precise": False, "evidence": "unvalidated"},
            {"unit_id": "bma2", "det_id": "bma2:305419896:8",
             "source_time_wall_s": 1794000000.25, "origin_id": "example-origin",
             "pose": {"origin_id": "example-origin",
                      "position": {"e_m": 100.0, "n_m": 0.0, "u_m": 1.5, "sigma_m": 0.05},
                      "orientation": {"heading_deg": 270.0, "pitch_deg": 0.0, "roll_deg": 0.0,
                                      "port_normal": "+z", "sigma_heading_deg": 1.0},
                      "source": "FICTITIOUS EXAMPLE", "surveyed_utc": "2026-11-14T00:00:00Z"},
             "bearing_deg_true": 327.9946, "sigma_deg": 2.0,
             "clock_precise": False, "evidence": "unvalidated"},
        ],
        "refusals": [{"reason": "unsupported_ray", "ray_index": 2,
                      "unit_id": "bma3", "det_id": "bma3:77:3"}],
    }


@pytest.fixture
def inputs():
    origin = {"origin_id": "site", "lat": 38.0, "lon": -77.0, "alt_m": 42.0,
              "datum": "WGS84", "north": "true"}
    pose = {"origin_id": "site",
            "position": {"e_m": 1.0, "n_m": 2.0, "u_m": 3.0, "sigma_m": 0.1},
            "orientation": {"heading_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0,
                            "port_normal": "+z", "sigma_heading_deg": 2.0},
            "source": "synthetic", "surveyed_utc": "2026-01-01T00:00:00Z"}
    ray = {"unit_id": "bma1", "det_id": "bma1:12:34", "t_wall_s": 99.0, "kind": "azel",
           "sigma_deg": 3.0, "az_deg_local": 0.0, "el_deg_local": 0.0,
           "bearing_deg_world": 90.0, "mirror_bearings_deg_world": None}
    fleet = {"v": 1, "k": "fleet", "updated_s": 100.0, "origin_id": "site",
             "units": [{"unit_id": "bma1", "pose_state": "surveyed",
                        "pose": pose, "ambiguous": False}], "rays": [ray]}
    return fleet, origin


def test_complete_observation_and_wall_clock_labels(inputs):
    fleet, origin = inputs
    assert convert_fleet(fleet, origin, now_s=100, max_age_s=1) == {
        "observations": [{
            "unit_id": "bma1", "det_id": "bma1:12:34", "source_time_wall_s": 99.0,
            "origin_id": "site", "pose": fleet["units"][0]["pose"],
            "bearing_deg_true": 90.0, "sigma_deg": 3.0,
            "clock_precise": False, "evidence": "unvalidated",
        }], "refusals": []}


@pytest.mark.parametrize("field", ["source", "surveyed_utc"])
@pytest.mark.parametrize("invalid", ["before\ud800after", "before\udfffafter"])
def test_pose_text_requires_utf8_without_rewriting(inputs, field, invalid):
    fleet, origin = inputs
    pose = fleet["units"][0]["pose"]
    valid = "  Grüße 🌍  "
    pose[field] = valid
    result = convert_fleet(fleet, origin)
    assert result["refusals"] == []
    assert result["observations"][0]["pose"][field] == valid

    pose[field] = invalid
    result = convert_fleet(fleet, origin)
    assert result == {"observations": [], "refusals": [{
        "reason": "invalid_pose", "ray_index": 0,
        "unit_id": "bma1", "det_id": "bma1:12:34"}]}


@pytest.mark.parametrize("field", ["source", "surveyed_utc"])
@pytest.mark.parametrize("invalid", ["\ud800", "\udfff"])
def test_sender_never_sends_invalid_utf8_pose(inputs, monkeypatch, field, invalid):
    from hear.ingest.bma_peer import decode_message
    from tools import bma_aws_sender as sender

    fleet, origin = inputs
    reads = []
    calls = []

    def read_json(path):
        reads.append(path)
        obj = fleet if path == "fleet" else origin
        # Exercise the sender's JSON decoder, including escaped surrogate input.
        return sender._strict_json(json.dumps(obj).encode("utf-8"))

    def transport(*args):
        calls.append(args[4])
        return 202, b'{"accepted":1,"quarantined":0,"enqueued":1}'

    monkeypatch.setattr(sender, "read_local_json", read_json)
    pose = fleet["units"][0]["pose"]
    pose[field] = "  Grüße 🌍  "
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll("fleet", "origin", 100) == {
        "observed": 1, "refused": 0, "deduplicated": 0, "capacity_dropped": 0,
        "expired": 0, "attempted": 1, "accepted": 1, "quarantined": 0,
        "enqueued": 1, "unconfirmed": 0, "not_enqueued": 0, "input_error": 0,
        "pending": 0, "deferred": 0, "clock_rollback": 0}
    assert reads == ["fleet", "origin"]
    assert len(calls) == 1
    batch = json.loads(calls[0])
    assert batch["device_id"] == "bma-bma1"
    assert len(batch["messages"]) == 1
    message = batch["messages"][0]
    decoded = decode_message(json.dumps(message).encode(), "dama/bma-bma1/telemetry")
    assert decoded["observation"]["pose"][field] == "  Grüße 🌍  "

    calls.clear()
    reads.clear()
    pose[field] = invalid
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll("fleet", "origin", 100) == {
        "observed": 0, "refused": 1, "deduplicated": 0, "capacity_dropped": 0,
        "expired": 0, "attempted": 0, "accepted": 0, "quarantined": 0,
        "enqueued": 0, "unconfirmed": 0, "not_enqueued": 0, "input_error": 0,
        "pending": 0, "deferred": 0, "clock_rollback": 0}
    assert reads == ["fleet", "origin"]
    assert calls == []


@pytest.mark.parametrize(("field", "value"), [
    ("north", True), ("north", "magnetic"), ("datum", "NAD83"),
    ("lat", 89.01), ("lon", -181), ("alt_m", 10001),
    ("origin_id", "bad space"), ("lat", float("nan")), ("lat", True),
    ("unknown", 1),
])
def test_origin_rules(inputs, field, value):
    fleet, origin = inputs
    assert len(convert_fleet(fleet, origin)["observations"]) == 1
    origin[field] = value
    assert convert_fleet(fleet, origin) == {"observations": [], "refusals": [
        {"reason": "invalid_origin", "ray_index": None, "unit_id": None, "det_id": None}]}


@pytest.mark.parametrize(("target", "field", "value", "reason"), [
    ("fleet", "origin_id", None, "origin_mismatch"),
    ("fleet", "v", True, "invalid_fleet"),
    ("fleet", "updated_s", float("inf"), "invalid_fleet"),
    ("fleet", "rays", {}, "invalid_fleet"),
    ("unit", "pose_state", "orientation_only", "invalid_pose"),
    ("unit", "not_fused_reason", "no_pose", "invalid_pose"),
    ("unit", "ambiguous", True, "ambiguous_ray"),
    ("pose", "origin_id", "other", "invalid_pose"),
    ("pose", "valid", False, "invalid_pose"),
    ("position", "sigma_m", 0, "invalid_pose"),
    ("position", "u_m", None, "invalid_pose"),
    ("orientation", "sigma_heading_deg", None, "invalid_pose"),
    ("orientation", "port_normal", None, "invalid_pose"),
    ("ray", "kind", "cone", "unsupported_ray"),
    ("ray", "mirror_bearings_deg_world", [10, 170], "ambiguous_ray"),
    ("ray", "ambiguous", True, "ambiguous_ray"),
    ("ray", "sigma_deg", 0, "invalid_ray"),
    ("ray", "t_wall_s", None, "invalid_ray"),
    ("ray", "el_deg_local", 91, "invalid_ray"),
    ("ray", "bearing_deg_world", 91, "contradictory_bearing"),
    ("ray", "det_id", "other:12:34", "invalid_identity"),
])
def test_refusals_do_not_fabricate(inputs, target, field, value, reason):
    fleet, origin = inputs
    assert len(convert_fleet(fleet, origin)["observations"]) == 1
    unit, ray = fleet["units"][0], fleet["rays"][0]
    pose = unit["pose"]
    obj = {"fleet": fleet, "unit": unit, "ray": ray, "pose": pose,
           "position": pose["position"], "orientation": pose["orientation"]}[target]
    obj[field] = value
    result = convert_fleet(fleet, origin)
    assert result["observations"] == []
    assert [r["reason"] for r in result["refusals"]] == [reason]


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("updated_s", 89, "stale_feed"), ("updated_s", 101, "future_feed"),
    ("t_wall_s", 89, "stale_ray"), ("t_wall_s", 101, "future_ray"),
])
def test_explicit_freshness_includes_feed_and_ray(inputs, field, value, reason):
    fleet, origin = inputs
    assert len(convert_fleet(fleet, origin, now_s=100, max_age_s=10)["observations"]) == 1
    (fleet if field == "updated_s" else fleet["rays"][0])[field] = value
    result = convert_fleet(fleet, origin, now_s=100, max_age_s=10)
    assert result["observations"] == []
    assert [r["reason"] for r in result["refusals"]] == [reason]
    assert len(convert_fleet(fleet, origin)["observations"]) == 1


def test_freshness_arguments_are_paired_and_valid(inputs):
    fleet, origin = inputs
    assert len(convert_fleet(fleet, origin, now_s=100, max_age_s=1)["observations"]) == 1
    for opts in ({"now_s": 100}, {"max_age_s": 1}, {"now_s": True, "max_age_s": 1},
                 {"now_s": 100, "max_age_s": -1}):
        assert convert_fleet(fleet, origin, **opts)["refusals"][0]["reason"] == "invalid_freshness"


def test_all_duplicate_detections_and_units_are_refused(inputs):
    fleet, origin = inputs
    assert len(convert_fleet(fleet, origin)["observations"]) == 1
    fleet["rays"].append(deepcopy(fleet["rays"][0]))
    result = convert_fleet(fleet, origin)
    assert result["observations"] == []
    assert [r["reason"] for r in result["refusals"]] == ["duplicate_detection"] * 2
    fleet["rays"].pop()
    fleet["units"].append(deepcopy(fleet["units"][0]))
    result = convert_fleet(fleet, origin)
    assert result["observations"] == []
    assert [r["reason"] for r in result["refusals"]] == ["duplicate_unit"]


def test_mixed_malformed_rays_are_isolated_and_ordered(inputs):
    fleet, origin = inputs
    fleet["rays"] = [None, fleet["rays"][0], {"unit_id": "unknown", "det_id": "unknown:1:2"}]
    result = convert_fleet(fleet, origin)
    assert [r["det_id"] for r in result["observations"]] == ["bma1:12:34"]
    assert result["refusals"] == [
        {"reason": "invalid_ray", "ray_index": 0, "unit_id": None, "det_id": None},
        {"reason": "unknown_unit", "ray_index": 2, "unit_id": "unknown", "det_id": "unknown:1:2"},
    ]


@pytest.mark.parametrize(("heading", "pitch", "roll", "normal", "az", "el", "bearing"), [
    (0, 0, 0, "+z", 0, 0, 0), (0, 0, 0, "+z", 90, 0, 270),
    (0, 0, 0, "-z", 90, 0, 90), (90, 0, 0, "+z", 0, 0, 90),
])
def test_true_bearing_transforms(inputs, heading, pitch, roll, normal, az, el, bearing):
    fleet, origin = inputs
    orientation = fleet["units"][0]["pose"]["orientation"]
    orientation.update(heading_deg=heading, pitch_deg=pitch, roll_deg=roll, port_normal=normal)
    ray = fleet["rays"][0]
    ray.update(az_deg_local=az, el_deg_local=el, bearing_deg_world=bearing)
    assert convert_fleet(fleet, origin)["observations"][0]["bearing_deg_true"] == bearing
    ray["bearing_deg_world"] = (bearing + 10) % 360
    assert convert_fleet(fleet, origin)["refusals"][0]["reason"] == "contradictory_bearing"


def test_independently_rounded_near_vertical_bearing(inputs):
    fleet, origin = inputs
    orientation = fleet["units"][0]["pose"]["orientation"]
    orientation.update(heading_deg=0, pitch_deg=45, roll_deg=0, port_normal="+z")
    ray = fleet["rays"][0]
    # Canonical hub.frames.world_bearing(orientation, .0049, 44.9549)
    # gives 355.60340741253384: independently rounding each angle to .01
    # yields (0, 44.95, 355.60), not the central bearing 0.
    ray.update(az_deg_local=0, el_deg_local=44.95, bearing_deg_world=355.60)
    result = convert_fleet(fleet, origin)
    assert result["refusals"] == []
    assert result["observations"][0]["bearing_deg_true"] == 355.60
    ray["bearing_deg_world"] = 340
    assert convert_fleet(fleet, origin)["refusals"] == [{
        "reason": "contradictory_bearing", "ray_index": 0,
        "unit_id": "bma1", "det_id": "bma1:12:34"}]
    # Canonical world_bearing(.0049, 44.9949) is 325.80579193477325.
    # Its rounded local direction is too close to vertical to resolve bearing.
    ray.update(el_deg_local=44.99, bearing_deg_world=325.81)
    assert convert_fleet(fleet, origin)["refusals"] == [{
        "reason": "ambiguous_bearing", "ray_index": 0,
        "unit_id": "bma1", "det_id": "bma1:12:34"}]
