"""Seeded hostile-input tests for live detections, clocks, and TDoA arrivals."""
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P
from hear import sketch as SK
from hear import wire as WR
from hear.backend.survey import from_dict
from hear.sim.adversarial import mutate_arrivals
from hear.sim.association_stress import AssociationStressTest
from tools import hear_drain as HD


def _frame_hex(seed=1):
    q, ref = SK.sketch(np.random.default_rng(seed).normal(0, 300, 4096), 16000.0)
    return SK.pack(12_345, ref, 500, q, fs=16000.0).hex()


def _survey():
    return from_dict({"frame": "enu_local", "units": "m", "nodes": [
        {"node_id": 1, "e_m": 0, "n_m": 0, "u_m": 0},
        {"node_id": 2, "e_m": 50, "n_m": 0, "u_m": 0},
        {"node_id": 3, "e_m": 25, "n_m": 40, "u_m": 0},
        {"node_id": 4, "e_m": 0, "n_m": 40, "u_m": 0}]})


@pytest.mark.parametrize("utc_us", [-1, 2 ** 63, "1.5", "not-a-clock"])
def test_live_detection_rejects_hostile_clock_values(tmp_path, utc_us):
    frame = _frame_hex()
    path = tmp_path / "detections.json"
    path.write_text(json.dumps([{ "utc_us": utc_us, "sample": 1, "fs_hz": 16000,
                                  "frame_len": len(frame) // 2, "frame": frame }]))
    entry = P.Pool(str(tmp_path / "pool")).ingest_detections_json(str(path), "gold")
    assert entry["rows"] == entry["skipped"] == 1
    assert entry["skip_reasons"] == {"decode_ValueError": 1}


def test_live_detection_preserves_a_large_exact_clock(tmp_path):
    frame, value = _frame_hex(), 2 ** 53 - 1
    path = tmp_path / "detections.json"
    path.write_text(json.dumps([{ "utc_us": value, "sample": 1, "fs_hz": 16000,
                                  "frame_len": len(frame) // 2, "frame": frame }]))
    pool = P.Pool(str(tmp_path / "pool"))
    assert pool.ingest_detections_json(str(path), "gold")["added"] == 1
    assert next(pool.raw())["utc_us"] == value


def test_http_payload_fuzz_salvages_only_complete_rows_and_rejects_dirty_status(monkeypatch):
    valid = json.dumps([{ "frame": _frame_hex() }]).encode()
    body = valid + b"\xff\x00truncated"
    res = HD.parse_live_ring(body)
    assert res["rows"] and res["truncated"] == len(body)
    with pytest.raises(ValueError):
        HD.parse_live_ring(b"{\xffnot-a-list")
    monkeypatch.setattr(HD, "_get", lambda *_: b'{"node":"gold","bad":"\xff"}')
    with pytest.raises(ValueError):
        HD.fetch_status("gold")


def test_wire_fuzz_refuses_corruption_without_false_decode():
    frame = WR.pack_v2(1_000_000, 1, 1, -10.0, 100, np.zeros((20, 8), dtype=np.int8))
    rng = np.random.default_rng(7)
    for _ in range(500):
        bad = bytearray(frame[:int(rng.integers(0, len(frame) + 4))])
        if bad:
            bad[int(rng.integers(len(bad)))] ^= int(rng.integers(1, 256))
        try:
            decoded = WR.decode(bytes(bad))
        except ValueError:
            continue
        assert decoded["version"] in (1, 2)


def test_cursor_fuzz_never_turns_roll_or_cut_page_into_negative_loss():
    rng = np.random.default_rng(17)
    for _ in range(500):
        previous, current = int(rng.integers(0, 2 ** 32)), int(rng.integers(0, 2 ** 32))
        got = HD.scene_gap(current, int(rng.integers(0, current + 1)), previous,
                           float(rng.integers(1, 4096)))
        assert got["unfetched_bytes"] is None or got["unfetched_bytes"] >= 0
        if current < previous:
            assert got["rolled"] is True and got["unfetched_bytes"] == 0


def test_adversarial_arrivals_label_phase_slips_ghosts_and_simultaneous_sources():
    scenario = mutate_arrivals(AssociationStressTest(_survey(), fixed_up_m=0.0), [
        {"pos": (10, 10, 0), "t0_s": 100.0}, {"pos": (20, 20, 0), "t0_s": 100.0}],
        clock_jumps_s={1: 1.0, 2: -1.0}, bogus_hyperbola_rate=1.0,
        multipath_rate=1.0, snr_db_range=(3.0, 30.0), seed=99)
    dets = scenario["detections"]
    assert scenario["clock_jumps_s"] == {1: 1.0, 2: -1.0}
    assert any(d["_is_bogus_hyperbola"] for d in dets) and any(d["_is_multipath"] for d in dets)
    assert {d["clock_jump_s"] for d in dets if d["node_id"] in (1, 2)} >= {-1.0, 1.0}
    assert all(3.0 <= d["snr_db"] <= 30.0 and 0 <= d["seq"] <= 255 for d in dets)
