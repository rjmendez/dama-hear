"""Small, dependency-free audio preparation helpers."""
from __future__ import annotations

from typing import Any


def resample_linear(samples: Any, source_hz: float, target_hz: float) -> Any:
    """Return float32 samples at ``target_hz`` using linear interpolation."""
    import numpy as np
    x = np.asarray(samples, dtype=np.float32)
    if source_hz <= 0 or target_hz <= 0:
        raise ValueError("sample rates must be positive")
    if len(x) == 0 or source_hz == target_hz:
        return x.astype(np.float32, copy=True)
    n = max(1, int(round(len(x) * float(target_hz) / float(source_hz))))
    positions = np.arange(n, dtype=np.float64) * float(source_hz) / float(target_hz)
    return np.interp(positions, np.arange(len(x), dtype=np.float64), x).astype(np.float32)


def pad_or_trim(samples: Any, length: int) -> Any:
    """Return exactly ``length`` samples, zero-padding only at the end."""
    import numpy as np
    if length < 0:
        raise ValueError("length must be non-negative")
    x = np.asarray(samples, dtype=np.float32)
    if len(x) >= length:
        return x[:length].astype(np.float32, copy=True)
    return np.pad(x, (0, length - len(x))).astype(np.float32)


def prepare(samples: Any, source_hz: float, target_hz: float, length: int | None = None) -> Any:
    """Resample and optionally make a model input a fixed length."""
    out = resample_linear(samples, source_hz, target_hz)
    return pad_or_trim(out, length) if length is not None else out
