#!/usr/bin/env python3
"""Periodic node telemetry: environment and soundscape, packed for one small Meshtastic frame.

TEMPERATURE IS NOT AMBIENT DATA HERE, IT IS AN ACOUSTIC PARAMETER. c = 331.3 + 0.606*T, so a
degree is 0.606 m/s, which is 183 us over 35 m -- larger than the clock sync this whole design
works to protect. A BME280 per node gives c AT EACH SENSOR, which beats one global figure taken
from someone's memory of the afternoon. The 2026-09-05 session recorded no temperature at all
and the speed of sound had to be reconstructed from an operator's recollection.

Pressure and humidity are second-order for c but pressure doubles as an altitude check, which
the 3D geometry wants and the phone fleet's EKF has been wrong about for weeks.

Soundscape level is cheap and useful on its own: it sets the detector's expectations, and for
the bioacoustic module it IS the measurement rather than a side channel.
"""
from __future__ import annotations

import struct
from typing import Dict, Optional

import numpy as np


def sound_speed(temp_c: float, humidity_pct: float = 50.0) -> float:
    """Metres per second. Humidity is a ~0.1% effect at these temperatures; included so the
    caller can see it was considered rather than forgotten."""
    c = 331.3 + 0.606 * float(temp_c)
    return c * (1.0 + 0.0001 * (float(humidity_pct) - 50.0) / 50.0)


def spl_stats(x: np.ndarray, fs: float, full_scale_db: float = 120.0) -> Dict[str, float]:
    """Equivalent level and peak over a window, in dB relative to the mic's full scale.

    NOT calibrated to absolute SPL: that needs a reference the field does not have. Reported as
    dBFS with the assumed full-scale so a caller can convert if it ever gets a calibrator, and
    so nobody mistakes it for a measured sound-pressure level.
    """
    x = np.asarray(x, float)
    if x.size == 0:
        return {"leq_dbfs": -120.0, "peak_dbfs": -120.0, "clipped_frac": 0.0}
    rms = float(np.sqrt(np.mean(x * x)))
    pk = float(np.abs(x).max())
    fs_ref = 32768.0
    return {
        "leq_dbfs": 20.0 * np.log10(max(rms, 1e-6) / fs_ref),
        "peak_dbfs": 20.0 * np.log10(max(pk, 1e-6) / fs_ref),
        "clipped_frac": float(np.mean(np.abs(x) >= 32700)),
        "assumed_full_scale_db": full_scale_db,
    }


def pack(temp_c: Optional[float], pressure_hpa: Optional[float], humidity_pct: Optional[float],
         leq_dbfs: float, peak_dbfs: float, sats: int, batt_mv: int,
         pps_locked: bool, clipped_frac: float = 0.0) -> bytes:
    """20 B. Missing sensor values are sent as a sentinel, never as zero.

    A zero temperature is -273 m/s of nothing and a plausible-looking 331.3 m/s of everything;
    a sentinel makes the central side say "no temperature" instead of quietly using the wrong c.
    """
    def q(v, scale, lo, hi, sentinel=-32768):
        if v is None:
            return sentinel
        return int(np.clip(round(float(v) * scale), lo, hi))
    return struct.pack(
        "<hHhhhBHB",
        q(temp_c, 100, -32767, 32767),          # 0.01 degC
        0 if pressure_hpa is None else int(np.clip(round(pressure_hpa * 10), 0, 65535)),
        q(humidity_pct, 100, -32767, 32767),
        int(np.clip(round(leq_dbfs * 100), -32768, 32767)),
        int(np.clip(round(peak_dbfs * 100), -32768, 32767)),
        int(np.clip(sats, 0, 255)),
        int(np.clip(batt_mv, 0, 65535)),
        (1 if pps_locked else 0) | (int(np.clip(clipped_frac * 100, 0, 100)) << 1),
    )


def unpack(b: bytes) -> Dict:
    t, p, h, leq, pk, sats, mv, flags = struct.unpack("<hHhhhBHB", b[:14])
    out = {
        "temp_c": None if t == -32768 else t / 100.0,
        "pressure_hpa": None if p == 0 else p / 10.0,
        "humidity_pct": None if h == -32768 else h / 100.0,
        "leq_dbfs": leq / 100.0, "peak_dbfs": pk / 100.0,
        "sats": sats, "batt_mv": mv,
        "pps_locked": bool(flags & 1), "clipped_pct": (flags >> 1),
    }
    out["sound_speed_mps"] = (None if out["temp_c"] is None
                              else sound_speed(out["temp_c"], out["humidity_pct"] or 50.0))
    return out


def wire_size() -> int:
    return struct.calcsize("<hHhhhBHB")


def to_dama(node_id: str, t_utc: float, tel: Dict) -> Dict:
    """Shape a decoded telemetry frame as a dama fleet node payload.

    Field names follow what the fleet already publishes so a hear node is not a special case
    downstream -- it is another node with a thinner sensor set.
    """
    return {
        "node_id": node_id, "ts_utc_ms": int(t_utc * 1000), "node_type": "hear",
        "temp_c": tel["temp_c"], "pressure_hpa": tel["pressure_hpa"],
        "humidity_pct": tel["humidity_pct"],
        "sound_speed_mps": tel["sound_speed_mps"],
        "acoustic": {"leq_dbfs": tel["leq_dbfs"], "peak_dbfs": tel["peak_dbfs"],
                     "clipped_pct": tel["clipped_pct"]},
        "sat_used": tel["sats"], "batt_mv": tel["batt_mv"],
        "clock_tier": "pps" if tel["pps_locked"] else "free",
    }
