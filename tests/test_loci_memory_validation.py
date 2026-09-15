"""Adversarial checks for Loci-backed acoustic localization safeguards."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear.backend import survey as SV
from hear.loci_validation import LociSpatialMemory
from hear.spatial import SpatialEventPipeline
from hear.solve import shockwave as SW


NODES = {1: (0.0, 0.0, 0.0), 2: (100.0, 0.0, 0.0), 3: (100.0, 100.0, 0.0),
         4: (0.0, 100.0, 0.0), 5: (50.0, 50.0, 0.0)}


def _survey(nodes=NODES):
    return SV.Survey(nodes, names={node_id: str(node_id) for node_id in nodes})


def _detections(source=(40.0, 60.0, 0.0), t0=1_757_000_000.0):
    c = SW.sound_speed(20.0)
    return [{"node_id": node_id, "seq": 1, "onset_found": True, "utc_trusted": True,
             "stamp_admissible": True, "t_utc_s": t0 + np.linalg.norm(np.asarray(source) - pos) / c}
            for node_id, pos in NODES.items()]


def test_one_second_nyquist_style_jump_is_isolated():
    memory = LociSpatialMemory(arrival_slack_s=0.002)
    rows = _detections(source=(0.0, 0.0, 0.0))
    rows[-1]["t_utc_s"] += 1.0
    result = memory.filter_arrivals([row["node_id"] for row in rows],
                                    [row["t_utc_s"] for row in rows], list(NODES.values()), 20.0)
    assert result["indices"] == [0, 1, 2, 3]
    assert result["rejected"] == [{"node_id": 5, "reason": "loci_speed_of_sound_incompatible",
                                    "violating_pairs": 4}]


def test_smaller_nlos_echo_is_filtered_before_solving():
    memory = LociSpatialMemory(arrival_slack_s=0.002)
    rows = _detections(source=(-200.0, -200.0, 0.0))
    rows[2]["t_utc_s"] += 0.010
    pipeline = SpatialEventPipeline(_survey(), fixed_up_m=0.0, geojsonl_path=None, loci_memory=memory)
    features = pipeline.process_coincidences(rows)
    assert len(features) == 1
    props = features[0]["properties"]
    assert props["east_m"] == pytest.approx(-200.0, abs=0.1)
    assert {row["node_id"] for row in props["loci_memory_rejections"]} == {3}


def test_dead_reckoning_anchor_drift_is_excluded_like_kasami():
    shifted = {**NODES, 5: (55.0, 55.0, 0.0)}
    memory = LociSpatialMemory(anchors={5: NODES[5]}, anchor_tolerance_m=5.0)
    pipeline = SpatialEventPipeline(_survey(shifted), fixed_up_m=0.0, geojsonl_path=None, loci_memory=memory)
    assert pipeline.loci_rogue_nodes[5]["drift_m"] > 5.0
    features = pipeline.process_coincidences(_detections())
    assert len(features) == 1
    assert features[0]["properties"]["east_m"] == pytest.approx(40.0, abs=0.1)
    assert {row["reason"] for row in features[0]["properties"]["loci_memory_rejections"]} == {"loci_anchor_drift"}


def test_nlos_solution_outside_recalled_map_is_refused():
    memory = LociSpatialMemory.from_dict({"bounds": {"east_m": [0, 100], "north_m": [0, 100]}})
    pipeline = SpatialEventPipeline(_survey(), fixed_up_m=0.0, geojsonl_path=None, loci_memory=memory)
    assert pipeline.loci_memory.accepts_position((50.0, 50.0, 0.0))
    assert not pipeline.loci_memory.accepts_position((150.0, 50.0, 0.0))
    assert pipeline.process_coincidences(_detections(source=(150.0, 50.0, 0.0))) == []


def test_ambiguous_pairwise_incompatibility_is_not_arbitrarily_blinded():
    memory = LociSpatialMemory(arrival_slack_s=0.0)
    result = memory.filter_arrivals([1, 2, 3], [0.0, 1.0, 2.0], list(NODES.values())[:3], 20.0)
    assert result["indices"] == [0, 1, 2]
    assert result["rejected"] == []
