#!/usr/bin/env python3
"""Record the real Silero VAD v5 answers for the synthetic fixtures in tests/privacy_signals.py.

    HEAR_VAD_MODEL=/path/to/silero_vad.onnx python3 tools/gen_silero_vad_golden.py \
        > testdata/silero_vad_golden.json

⚠️NOT A CI GENERATOR, AND DELIBERATELY NOT WIRED INTO ONE. The ~2 MB `silero_vad.onnx` is not in
this checkout and `requirements/ci-*.txt` carry no onnxruntime (`docs/silero-vad-privacy-contract.md`
§10 gate 1 pins the artifact on the pool, not here), so CI cannot regenerate this file. What CI
does instead is re-assert the separation the recorded numbers show and re-hash every fixture
waveform against the digests recorded beside them, in
`tests/test_silero_vad.py::TestTheRecordedDiscrimination`; where the weights DO exist,
`TestAgainstTheRealModel` re-derives every probability and compares frame for frame.

⚠️THE 64-SAMPLE CONTEXT IS NOT OPTIONAL. The v5 export takes 576 samples at 16 kHz: the previous
frame's last 64 samples followed by this frame's 512. Feeding a bare 512-sample frame runs without
error and answers ~0.001 on speech, which is why the `speech_like_without_context` ablation is
recorded here rather than described.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import privacy_signals as SIG                        # noqa: E402

RATE = 16000
CHUNK = 512
CONTEXT = 64
STATE = (2, 1, 128)
FIXTURES = ("speech_like", "silence", "gaussian_noise", "gaussian_noise_loud", "pink_noise",
            "pink_noise_loud", "bird_chirps", "tone_1k", "formant_drone")


def probabilities(session, x, *, context=CONTEXT, reset_every_frame=False):
    state = np.zeros(STATE, dtype=np.float32)
    tail = np.zeros(context, dtype=np.float32)
    out = []
    for i in range(0, len(x) - CHUNK + 1, CHUNK):
        if reset_every_frame:
            state = np.zeros(STATE, dtype=np.float32)
            tail = np.zeros(context, dtype=np.float32)
        frame = np.asarray(x[i:i + CHUNK], dtype=np.float32)
        inp = np.concatenate([tail, frame])[None, :] if context else frame[None, :]
        p, state = session.run(None, {"input": inp, "state": state,
                                      "sr": np.array(RATE, dtype=np.int64)})
        tail = frame[-context:] if context else tail
        out.append(round(float(np.asarray(p).reshape(-1)[0]), 6))
    return out


def main() -> int:
    model = os.environ.get("HEAR_VAD_MODEL", "")
    if not model or not os.path.exists(model):
        sys.stderr.write("set HEAR_VAD_MODEL to a silero_vad.onnx\n")
        return 2
    import onnxruntime as ort

    session = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}
    if names != {"input", "state", "sr"}:
        sys.stderr.write("not a v5 export: inputs are %s\n" % sorted(names))
        return 2

    fixtures = {}
    for name in FIXTURES:
        x = SIG.by_name(name)
        fixtures[name] = {
            "sha256": hashlib.sha256(np.asarray(x, dtype=np.float32).tobytes()).hexdigest(),
            "samples": int(len(x)),
            "is_speech_fixture": name == "speech_like",
            "probs": probabilities(session, x),
        }

    speech = SIG.by_name("speech_like")
    snapshot = {
        "schema": "hear.vad.fixture_golden.v1",
        "model": {"name": "silero-vad", "version": "v5", "file": "silero_vad.onnx",
                  "sha256": hashlib.sha256(open(model, "rb").read()).hexdigest()},
        "runtime": {"name": "onnxruntime", "version": ort.__version__},
        "frame": {"rate_hz": RATE, "chunk_samples": CHUNK, "context_samples": CONTEXT,
                  "state_shape": list(STATE)},
        "fixtures": fixtures,
        "ablations": {
            "speech_like_state_reset_every_frame": probabilities(session, speech,
                                                                 reset_every_frame=True),
            "speech_like_without_context": probabilities(session, speech, context=0),
            "speech_like_48k_decimated_naively": probabilities(
                session, np.asarray(SIG.speech_like_48k()[::3], dtype=np.float32)),
        },
    }
    json.dump(snapshot, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
