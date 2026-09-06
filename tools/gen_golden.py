#!/usr/bin/env python3
"""Regenerate the golden vectors the phone port is checked against.

    python3 tools/gen_golden.py            # rewrite testdata/*.json
    python3 tools/gen_golden.py --check    # fail if the committed files differ

WHY THESE EXIST. `AcousticSketch.kt` and `SketchWindow.kt` in dama-gotchi are hand ports of
`hear/sketch.py` and `hear/node/detect.py` -- there is no numpy on a phone and no numpy on an
ESP32. A classifier trained on a node's bytes is applied to a phone's, so the ports have to agree
on every one of the 160 quantised bytes, not approximately. These files are how that is checked
from the JVM side without this repo being on the build path.

⚠️THE VECTORS MUST BE SELF-CONSISTENT. `pcm_b64` is int16 and the expected sketch is computed
FROM it, so the JVM side can reproduce the vector from its own stored bytes. The first version of
this generator sketched the FLOAT signal and stored the int16-rounded PCM; the vectors were then
unreproducible by 24 of 160 bytes on the impulse case and by 0.0005 dB on a tone. Quantise first.
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hear import sketch as SK              # noqa: E402
from hear.node import detect as DT         # noqa: E402

TESTDATA = pathlib.Path(__file__).resolve().parents[1] / "testdata"
N = 4096


def _q16(x: np.ndarray) -> np.ndarray:
    """Round to int16 and widen back. Everything downstream sees only this."""
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _impulse(fs: float, amp: float, seed: int) -> np.ndarray:
    """Noise floor plus one decaying impulse -- the shape the whole project is about."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 20.0, N)
    i = N // 8
    t = np.arange(N - i) / fs
    x[i:] += amp * np.exp(-t * 400.0) * np.cos(2 * np.pi * 1800.0 * t)
    return x


def _tone(fs: float, f: float, amp: float) -> np.ndarray:
    return amp * np.sin(2 * np.pi * f * np.arange(N) / fs)


def sketch_cases():
    return [
        ("impulse_48k", 48000.0, _q16(_impulse(48000.0, 12000.0, 20260905)), SK.LAYOUT_NYQUIST),
        ("tone_2k_48k", 48000.0, _q16(_tone(48000.0, 2000.0, 8000.0)), SK.LAYOUT_NYQUIST),
        ("noise_48k", 48000.0, _q16(np.random.default_rng(7).normal(0.0, 300.0, N)),
         SK.LAYOUT_NYQUIST),
        ("silence_48k", 48000.0, np.zeros(N, dtype=np.int16), SK.LAYOUT_NYQUIST),
        # Deliberately over-driven: the sketch of a clipped report is a sketch of the clipping,
        # and both sides have to agree on that spectrum too.
        ("clipped_48k", 48000.0, _q16(_impulse(48000.0, 90000.0, 20260905)), SK.LAYOUT_NYQUIST),
        # 16 kHz exercises the mel bank's Nyquist clamp: F_HI is 20 kHz, above this Nyquist.
        ("tone_2k_16k", 16000.0, _q16(_tone(16000.0, 2000.0, 8000.0)), SK.LAYOUT_NYQUIST),
        # ⚠️THE FIXED LAYOUT. At 48 kHz it must be byte-identical to the rescaled one (the phone
        # sends the same frame either way); at 16 kHz it must NOT be, or the fix does nothing and
        # the top five bands would not be sitting at the floor where a masker expects them.
        ("impulse_48k_fixed", 48000.0, _q16(_impulse(48000.0, 12000.0, 20260905)),
         SK.LAYOUT_FIXED),
        ("noise_16k_fixed", 16000.0, _q16(np.random.default_rng(11).normal(0.0, 300.0, N)),
         SK.LAYOUT_FIXED),
        ("impulse_16k_fixed", 16000.0, _q16(_impulse(16000.0, 12000.0, 20260905)),
         SK.LAYOUT_FIXED),
    ]


def build_sketch_golden() -> dict:
    cases = []
    for name, fs, pcm, layout in sketch_cases():
        x = pcm.astype(float)                      # quantise FIRST, then sketch
        q, ref = SK.sketch(x, fs, layout=layout)
        peak = int(min(np.abs(x).max(), 65535))
        frame = SK.pack(123456, ref, peak, q, fs=fs, layout=layout)
        cases.append({
            "name": name, "fs": fs, "n": int(len(pcm)),
            "pcm_b64": _b64(pcm.tobytes()),
            "ref_db": float(ref),
            "q_b64": _b64(q.astype(np.int8).tobytes()),
            "frame_b64": _b64(frame),
            "frame_len": len(frame),
            "fs_code": SK.fs_code(fs),
            "layout": layout,
            "layout_bit": bool(layout == SK.LAYOUT_FIXED),
            "valid_bands": SK.valid_bands(fs, layout=layout),
            "band_edges_hz": [round(float(e), 4) for e in SK.band_edges_hz(fs, layout=layout)],
        })
    return {
        "schema": "hear.sketch.golden.v4",
        "mel_bands": SK.MEL_BANDS, "frames": SK.FRAMES,
        "f_lo": SK.F_LO, "f_hi": SK.F_HI, "hop_s": SK.HOP_S, "nfft": SK.NFFT,
        "wire_size": SK.wire_size(),
        "fs_shift": SK.FS_SHIFT, "fs_mask": SK.FS_MASK, "layout_bit": SK.LAYOUT_BIT,
        "layout_equivalent_above_hz": SK.LAYOUT_EQUIVALENT_ABOVE_HZ,
        "fs_codes": {str(int(k)): v for k, v in sorted(SK.FS_CODES.items())},
        "note": ("PCM is int16 and the vectors were computed FROM it -- reproduce by decoding "
                 "pcm_b64 to int16, widening to float, and sketching."),
        "cases": cases,
    }


def build_window_golden() -> dict:
    """Vectors for SketchWindow: the 1 ms envelope and the onset index taken from it.

    ⚠️THE OFFSET IS THE WHOLE POINT. `np.convolve(..., mode="same")` keeps the MIDDLE len(x) of
    the full convolution, which for an even kernel (48 taps at 48 kHz) is not centred: out[i]
    averages x[i-(n-1)+(n-1)//2 .. i+(n-1)//2]. A port that centres it instead moves the argmax by
    half a millisecond -- 24 samples, or 8 metres of apparent range.
    """
    cases = []
    for name, fs, pcm, search in [
        ("impulse_48k", 48000.0, _q16(_impulse(48000.0, 12000.0, 20260905)), (0, N)),
        # onset late in the search region: the phone's chunk-granular gate fires on the chunk, the
        # refined index has to land on the peak wherever in it that is
        ("late_onset_48k", 48000.0, _q16(_impulse(48000.0, 12000.0, 4242)[::-1].copy()), (0, N)),
        ("noise_48k", 48000.0, _q16(np.random.default_rng(7).normal(0.0, 300.0, N)), (100, 900)),
        ("tone_16k", 16000.0, _q16(_tone(16000.0, 2000.0, 8000.0)), (0, N)),
    ]:
        x = pcm.astype(float) / 32767.0            # the float scale the ring holds
        env = DT.envelope(x, fs)
        lo, hi = search
        idx = int(lo + np.argmax(env[lo:hi]))
        # a sparse sample of the envelope: enough to catch an offset error, small enough to read
        probes = sorted(set(int(v) for v in np.linspace(0, len(x) - 1, 41)))
        cases.append({
            "name": name, "fs": fs,
            "pcm_b64": _b64(pcm.tobytes()),
            "env_taps": int(max(1, int(1e-3 * fs))),
            "search_lo": lo, "search_hi": hi,
            "onset_index": idx,
            "probe_index": probes,
            "probe_env": [float(env[i]) for i in probes],
        })
    return {
        "schema": "hear.window.golden.v1",
        "env_ms": 1.0,
        "guard_s": DT.GUARD_S, "retrigger_s": DT.RETRIGGER_S,
        "note": ("x = int16 pcm / 32767.0, the scale AudioPullFormat and the capture ring use. "
                 "env is a 1 ms moving average of |x| with numpy 'same' alignment."),
        "cases": cases,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit non-zero if the committed files differ")
    a = ap.parse_args()
    rc = 0
    for name, doc in [("sketch_golden.json", build_sketch_golden()),
                      ("window_golden.json", build_window_golden())]:
        path = TESTDATA / name
        text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        if a.check:
            have = path.read_text() if path.exists() else ""
            if have != text:
                print(f"DIFFERS: {path}")
                rc = 1
            else:
                print(f"ok: {path}")
        else:
            path.write_text(text)
            print(f"wrote {path} ({len(text)} B)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
