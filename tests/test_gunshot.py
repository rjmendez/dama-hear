import sys
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules' / 'gunshot'))
import train_gunshot as TG  # noqa: E402

def _write(path, data, rate=48_000): sf.write(path, np.asarray(data, dtype=np.float32), rate)

def test_loader_reads_native_48k_and_rejects_other_rates(tmp_path):
    good, bad = tmp_path / 'good.wav', tmp_path / 'bad.wav'
    _write(good, np.zeros(800)); _write(bad, np.zeros(800), 16_000)
    assert TG.load_audio(good).shape == (800,)
    with pytest.raises(ValueError, match='expected 48000 Hz'): TG.load_audio(bad)

def test_dataset_loading_and_mel_features(tmp_path):
    shots, noise = tmp_path / 'shotdata' / 'Glock 17', tmp_path / 'shotpull2'
    shots.mkdir(parents=True); noise.mkdir()
    impulse = np.zeros(3_000, dtype=np.float32); impulse[1_000:1_010] = .8
    _write(shots / 'shot.wav', impulse); _write(noise / 'background.wav', np.random.default_rng(2).normal(0, .01, 3_000))
    cfg = TG.FeatureConfig(frames=8, n_mels=12)
    dataset = TG.build_dataset(shots.parent, noise, cfg)
    assert dataset.features.shape == (2, 96); assert dataset.labels.tolist() == [1, 0]
    assert TG.extract_features(impulse, cfg).shape == (96,)

def test_json_model_inference_matches_training_shape(tmp_path):
    cfg = TG.FeatureConfig(frames=4, n_mels=8); rng = np.random.default_rng(3)
    features = rng.normal(size=(12, cfg.feature_count)).astype(np.float32); labels = np.array([0, 1] * 6, dtype=np.int8)
    model = TG.fit_model(features, labels, cfg); probabilities = TG.predict_proba(features, model)
    assert probabilities.shape == (12,); assert np.all((probabilities >= 0) & (probabilities <= 1))
    path = tmp_path / 'model.json'; TG.export_json(model, path)
    assert path.exists() and TG.evaluate(features, labels, model)['accuracy'] >= .5

