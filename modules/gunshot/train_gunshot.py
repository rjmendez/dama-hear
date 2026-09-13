"""Train and evaluate a small 48 kHz gunshot detector."""
from __future__ import annotations
import argparse, json, os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import numpy as np
import soundfile as sf

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(_REPO_ROOT))

SAMPLE_RATE = 48_000.0
DEFAULT_NFFT, DEFAULT_HOP, DEFAULT_MELS, DEFAULT_FRAMES = 512, 240, 40, 64

@dataclass(frozen=True)
class FeatureConfig:
    sample_rate: float = SAMPLE_RATE
    n_fft: int = DEFAULT_NFFT
    hop_length: int = DEFAULT_HOP
    n_mels: int = DEFAULT_MELS
    frames: int = DEFAULT_FRAMES
    f_min: float = 20.0
    f_max: float = 20_000.0
    @property
    def feature_count(self): return self.n_mels * self.frames

@dataclass(frozen=True)
class Dataset:
    features: np.ndarray
    labels: np.ndarray
    paths: tuple[str, ...]

def wav_files(root: str | os.PathLike[str]) -> list[Path]:
    path = Path(root).expanduser()
    if not path.is_dir(): raise FileNotFoundError(f"audio directory does not exist: {path}")
    return sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() == '.wav')

def load_audio(path: str | os.PathLike[str], sample_rate: float = SAMPLE_RATE) -> np.ndarray:
    audio, rate = sf.read(str(path), always_2d=False, dtype='float32')
    if float(rate) != float(sample_rate):
        raise ValueError(f"{path}: expected {sample_rate:g} Hz, got {rate:g} Hz")
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 2: audio = audio.mean(axis=1)
    if audio.ndim != 1 or audio.size == 0 or not np.isfinite(audio).all():
        raise ValueError(f"{path}: audio must be a non-empty finite mono/stereo clip")
    return audio

def _mel_bank(config: FeatureConfig) -> np.ndarray:
    from hear import sketch
    return sketch.mel_filterbank(config.sample_rate, nfft=config.n_fft, bands=config.n_mels,
                                 f_lo=config.f_min, f_hi=config.f_max, layout=sketch.LAYOUT_FIXED)

def mel_spectrogram(audio: np.ndarray, config: FeatureConfig = FeatureConfig()) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    bank, window = _mel_bank(config), np.hanning(config.n_fft)
    out = np.zeros((config.n_mels, config.frames), dtype=np.float32)
    for frame in range(config.frames):
        chunk = audio[frame * config.hop_length:frame * config.hop_length + config.n_fft]
        if chunk.size < config.n_fft: chunk = np.pad(chunk, (0, config.n_fft - chunk.size))
        out[:, frame] = bank @ (np.abs(np.fft.rfft(chunk * window, config.n_fft)) ** 2)
    return (10.0 * np.log10(out + 1e-12)).astype(np.float32)

def extract_features(audio: np.ndarray, config: FeatureConfig = FeatureConfig()) -> np.ndarray:
    return mel_spectrogram(audio, config).reshape(-1).astype(np.float32)

def build_dataset(shotdata, background, config=FeatureConfig(), max_background=None) -> Dataset:
    positives, negatives = wav_files(shotdata), wav_files(background)
    if max_background is not None: negatives = negatives[:max_background]
    paths = positives + negatives
    if not paths: raise ValueError('no WAV files found in the supplied audio directories')
    features = np.stack([extract_features(load_audio(p, config.sample_rate), config) for p in paths])
    labels = np.concatenate((np.ones(len(positives), dtype=np.int8), np.zeros(len(negatives), dtype=np.int8)))
    return Dataset(features, labels, tuple(str(p) for p in paths))

def fit_model(features, labels, config=FeatureConfig()) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    x, y = np.asarray(features, dtype=np.float32), np.asarray(labels, dtype=np.int8)
    if x.ndim != 2 or x.shape[1] != config.feature_count:
        raise ValueError(f'expected features with shape (n, {config.feature_count}), got {x.shape}')
    if set(np.unique(y)) != {0, 1}: raise ValueError('training requires both gunshot (1) and background (0) labels')
    scaler = StandardScaler().fit(x)
    classifier = LogisticRegression(max_iter=2000, class_weight='balanced').fit(scaler.transform(x), y)
    return {'format':'gunshot_logmel_logistic_v1', 'sample_rate_hz':config.sample_rate,
            'n_fft':config.n_fft, 'hop_length':config.hop_length, 'n_mels':config.n_mels,
            'frames':config.frames, 'f_min_hz':config.f_min, 'f_max_hz':config.f_max,
            'mean':scaler.mean_.tolist(), 'scale':np.maximum(scaler.scale_, 1e-12).tolist(),
            'weights':classifier.coef_[0].tolist(), 'bias':float(classifier.intercept_[0]), 'n_train':int(len(y))}

def predict_proba(features, model) -> np.ndarray:
    x = np.asarray(features, dtype=np.float32)
    if x.ndim == 1: x = x[None, :]
    mean, scale, weights = np.asarray(model['mean']), np.asarray(model['scale']), np.asarray(model['weights'])
    if x.ndim != 2 or x.shape[1] != len(weights): raise ValueError(f'expected feature width {len(weights)}, got {x.shape}')
    z = ((x - mean) / scale) @ weights + float(model['bias'])
    return (1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))).astype(np.float32)

def infer_clip(path, model) -> float:
    config = FeatureConfig(sample_rate=float(model['sample_rate_hz']), n_fft=int(model['n_fft']),
                           hop_length=int(model['hop_length']), n_mels=int(model['n_mels']),
                           frames=int(model['frames']), f_min=float(model['f_min_hz']),
                           f_max=float(model['f_max_hz']))
    return float(predict_proba(extract_features(load_audio(path, config.sample_rate), config), model)[0])


def evaluate(features, labels, model):
    probabilities = predict_proba(features, model)
    return {'accuracy':float(np.mean((probabilities >= .5) == np.asarray(labels, dtype=bool))),
            'positive_rate':float(np.mean(probabilities >= .5))}

def export_json(model, path): Path(path).expanduser().write_text(json.dumps(model, indent=2) + '\n')

def export_tflite(model, path):
    try: import tensorflow as tf
    except ImportError as exc: raise RuntimeError('TFLite export requires optional tensorflow; JSON export is dependency-free') from exc
    width = len(model['weights'])
    layer = tf.keras.Sequential([tf.keras.Input(shape=(width,)), tf.keras.layers.Dense(1, activation='sigmoid')])
    layer.layers[0].set_weights([np.asarray(model['weights'], dtype=np.float32).reshape(width, 1), np.asarray([model['bias']], dtype=np.float32)])
    Path(path).expanduser().write_bytes(tf.lite.TFLiteConverter.from_keras_model(layer).convert())

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shotdata', default='~/shotdata'); parser.add_argument('--background', default='~/shotpull2')
    parser.add_argument('--out', required=True); parser.add_argument('--tflite'); parser.add_argument('--max-background', type=int)
    args = parser.parse_args(argv)
    dataset = build_dataset(args.shotdata, args.background, max_background=args.max_background)
    model = fit_model(dataset.features, dataset.labels); model['training_metrics'] = evaluate(dataset.features, dataset.labels, model)
    export_json(model, args.out)
    if args.tflite: export_tflite(model, args.tflite)
    print(json.dumps({'n_samples':len(dataset.labels), **model['training_metrics']})); return 0

if __name__ == '__main__': raise SystemExit(main())
