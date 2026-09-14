import json
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "modules", "supersonic"))

import classify as CL  # noqa: E402
import train as TR  # noqa: E402
import train_sketch as TS  # noqa: E402


def test_hand_feature_loader_expands_user_paths_and_preserves_missing_gap(tmp_path):
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    (labels_dir / "a.json").write_text(json.dumps({"id": "a", "label": "crack"}))
    items = [{"id": "a", "utc": 6.1, "gap": None, "rise": 1, "decay": 2,
              "cent": 3, "crest": 4, "fhi": 5, "peak": 6}]
    items_path = tmp_path / "items.json"
    items_path.write_text(json.dumps(items))

    labels, loaded = TR.load(str(labels_dir / "*.json"), str(items_path))
    X, y, groups, ids = TR.build(labels, loaded, ["gap", "peak"])
    assert ids == ["a"]
    assert X.tolist() == [[-1.0, 6.0]]
    assert y.tolist() == [1]
    assert groups.tolist() == [2]


def test_training_step_requires_both_classes_and_enough_groups():
    X = np.array([[1.0], [2.0], [3.0], [4.0]])
    with pytest.raises(ValueError, match="both classes"):
        TR.fit_and_score(X, np.zeros(4), np.arange(4), ["peak"], splits=2)
    with pytest.raises(ValueError, match="distinct groups"):
        TR.fit_and_score(X, np.array([0, 1, 0, 1]), np.array([0, 0, 1, 1]), ["peak"], splits=3)


def test_training_step_returns_finite_grouped_predictions():
    X = np.array([[1.0], [1.2], [4.0], [4.2], [7.0], [7.2], [10.0], [10.2]])
    y = np.array([0, 0, 0, 1, 1, 1, 1, 0])
    groups = np.arange(8)
    auc, acc, predictions, model = TR.fit_and_score(X, y, groups, ["peak"], splits=4)
    assert 0.0 <= auc <= 1.0
    assert 0.0 <= acc <= 1.0
    assert np.isfinite(predictions).all()
    assert model.predict_proba(TR.prep(X, ["peak"])).shape == (8, 2)


def test_model_inference_rejects_bad_weights_and_nonfinite_features():
    model = {"features": ["peak"], "w": [1.0], "b": 0.0}
    assert CL.score({"peak": 2.0}, model) > 0.5
    assert CL.score({"peak": float("nan")}, model) is None
    with pytest.raises(ValueError, match="equal lengths"):
        CL.score({"peak": 2.0}, {"features": ["peak"], "w": [], "b": 0.0})


def test_sketch_training_rejects_empty_or_one_class_data():
    with pytest.raises(ValueError, match="non-empty"):
        TS.nested_auc(np.empty((0, 2)), np.array([]), np.array([]))
    with pytest.raises(ValueError, match="both classes"):
        TS.nested_auc(np.ones((8, 2)), np.zeros(8), np.arange(8))
