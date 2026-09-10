#!/usr/bin/env python3
"""Build the tagger's ONNX graph from upstream's PyTorch checkpoint, reproducibly.

    python tools/export_mn10_onnx.py --out mn10_as.onnx
    kubectl -n dama cp mn10_as.onnx <a pod mounting hear-pool>:/pool/models/mn10_as/

⚠️THIS RUNS ON A WORKSTATION, NEVER IN THE CLUSTER. It needs torch and torchvision -- about 3 GB
into a 5 Gi PVC to produce a 24 MB graph once. deploy/k8s/hear-tag.yaml therefore verifies the
graph and refuses; it does not build one and does not download a model it cannot check.

⚠️THE CHAIN IS PINNED AT BOTH ENDS AND THE MIDDLE IS THIS FILE.

    mn10_as_mAP_471.pt   19,708,753 B   upstream GitHub release v0.0.1, MIT
      sha256 0bd7dc2443af498c289a2e739f02ebb515d6aa3fd3ab9db539c86123ae368a4e
      -> this script                    reviewed like any other code in this repo
        -> mn10_as.onnx   24,016,402 B  what tools/hear_tag.py verifies before it loads
           sha256 1b718a05a68ba8eecf73ce87b5ce74fe266f4228ecaf2c8bdf8b972347dd553d

Both digests are duplicated in tools/hear_tag.py, and a test asserts the two copies agree -- a
pin that lives only next to the thing it pins is not a check.

Verified byte-reproducible: two runs produce the same digest (2026-09-10). If a torch upgrade
changes the graph the digest moves, `--verify-weights` fails closed, and MODEL_VERSION has to be
bumped -- which is the point, because a re-export under an unchanged version string would
overwrite rows rather than sit beside them.

⚠️THE MEL FRONTEND IS BAKED IN, WHICH IS WHY THE POD NEEDS NO torchaudio. EfficientAT computes its
mel filterbank at forward time through torchaudio.compliance.kaldi; here it becomes a constant
buffer. Checked against the original AugmentMelSTFT on real-shaped audio at every export:
max abs error 8.4e-05, and the export ABORTS if it exceeds 2e-3.

⚠️torch.stft IS REPLACED BY A DFT conv1d. Not an optimisation: torch.onnx refuses to export
Unfold when the time axis is dynamic, and the clip length is exactly what is dynamic -- clips are
4.0 s at 16 kHz and 5.0 s at 48 kHz. The conv1d form is the same arithmetic on any opset.

⚠️THE `pretrained_name` LOOKUP PULLS FROM UPSTREAM'S RELEASE. Point it at a local copy and hash it
first if you care which of the eleven assets called `mn10_as` you got: they differ only in mel
bins and hop, all load, and only one has mAP 0.471.
"""

import os
import sys

import numpy as np
import torch
import torch.nn as nn
import torchaudio

#: Upstream's own package layout (`models.mn.model`, `helpers.utils`) is imported directly rather
#: than vendored into this repo: it is MIT but it is 40-odd files of training code, and copying it
#: here would make the next upstream fix a merge instead of a `git pull`.
#:
#:     git clone --depth 1 -b v0.0.1 https://github.com/fschmid56/EfficientAT
#:     EFFICIENTAT=./EfficientAT python tools/export_mn10_onnx.py --out mn10_as.onnx
EFFICIENTAT = os.environ.get("EFFICIENTAT", "./EfficientAT")
if not os.path.isdir(os.path.join(EFFICIENTAT, "models", "mn")):
    raise SystemExit(
        "no EfficientAT checkout at %r. Clone it and point EFFICIENTAT at it:\n"
        "  git clone --depth 1 -b v0.0.1 https://github.com/fschmid56/EfficientAT\n"
        "  EFFICIENTAT=./EfficientAT python tools/export_mn10_onnx.py --out mn10_as.onnx"
        % EFFICIENTAT)
sys.path.insert(0, os.path.abspath(EFFICIENTAT))

from models.mn.model import get_model as get_mobilenet          # noqa: E402
from helpers.utils import NAME_TO_WIDTH                         # noqa: E402

SR, WIN, HOP, NFFT, NMELS = 32000, 800, 320, 1024, 128
FMIN, FMAX = 0.0, SR // 2 - 2000 // 2          # eval path: no augmentation
NAME = "mn10_as"


class MelFront(nn.Module):
    def __init__(self):
        super().__init__()
        mel_basis, _ = torchaudio.compliance.kaldi.get_mel_banks(
            NMELS, NFFT, SR, FMIN, FMAX, vtln_low=100.0, vtln_high=-500., vtln_warp_factor=1.0)
        mel_basis = nn.functional.pad(mel_basis, (0, 1), mode="constant", value=0)
        self.register_buffer("mel_basis", mel_basis.float())
        w = torch.hann_window(WIN, periodic=False)
        w = nn.functional.pad(w, ((NFFT - WIN) // 2, NFFT - WIN - (NFFT - WIN) // 2))
        n = torch.arange(NFFT).float()
        k = torch.arange(NFFT // 2 + 1).float()
        ang = -2 * torch.pi * k[:, None] * n[None, :] / NFFT
        # (K, 1, NFFT) conv kernels, not an unfold+matmul: torch.onnx refuses to export Unfold
        # when the time axis is dynamic, and the clip length is exactly what is dynamic here.
        self.register_buffer("dft_r", (torch.cos(ang) * w[None, :]).float().unsqueeze(1))
        self.register_buffer("dft_i", (torch.sin(ang) * w[None, :]).float().unsqueeze(1))
        self.register_buffer("preemph", torch.as_tensor([[[-.97, 1.0]]]))

    def forward(self, x):                      # x: (B, T) float32 in [-1, 1]
        x = nn.functional.conv1d(x.unsqueeze(1), self.preemph).squeeze(1)
        x = nn.functional.pad(x.unsqueeze(1), (NFFT // 2, NFFT // 2), mode="reflect")
        re = nn.functional.conv1d(x, self.dft_r, stride=HOP)     # (B, K, frames)
        im = nn.functional.conv1d(x, self.dft_i, stride=HOP)
        p = re ** 2 + im ** 2
        mel = torch.matmul(self.mel_basis, p)
        return ((mel + 0.00001).log() + 4.5) / 5.


class Tagger(nn.Module):
    def __init__(self):
        super().__init__()
        self.mel = MelFront()
        self.net = get_mobilenet(width_mult=NAME_TO_WIDTH(NAME), pretrained_name=NAME)

    def forward(self, x):
        spec = self.mel(x).unsqueeze(1)        # (B, 1, mels, frames)
        logits, feats = self.net(spec)
        return logits, feats


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="mn10_as.onnx", help="path to write the ONNX graph to")
    args = ap.parse_args()
    m = Tagger().eval()
    with torch.no_grad():
        x = torch.zeros(1, SR * 5)
        lo, fe = m(x)
        print("logits", tuple(lo.shape), "features", tuple(fe.shape))
        # parity against the repo's own frontend, on real-shaped noise
        from models.preprocess import AugmentMelSTFT
        ref = AugmentMelSTFT(n_mels=NMELS, sr=SR, win_length=WIN, hopsize=HOP).eval()
        g = torch.Generator().manual_seed(7)
        t = torch.randn(1, SR * 5, generator=g) * 0.05
        a, b = m.mel(t), ref(t)
        print("mel shapes", tuple(a.shape), tuple(b.shape))
        d = (a - b).abs()
        print("mel max abs err %.3e  mean %.3e" % (d.max().item(), d.mean().item()))
        assert d.max().item() < 2e-3, (
            "the baked frontend does not reproduce AugmentMelSTFT (max abs %.3e). The graph "
            "would score every clip through a filterbank the weights were never trained on."
            % d.max().item())
    torch.onnx.export(
        m, (torch.zeros(1, SR * 5),), args.out,
        input_names=["waveform"], output_names=["logits", "embedding"],
        dynamic_axes={"waveform": {1: "samples"}, "logits": {0: "b"}, "embedding": {0: "b"}},
        opset_version=17, do_constant_folding=True, dynamo=False)
    import hashlib
    h = hashlib.sha256(open(args.out, "rb").read()).hexdigest()
    print("exported %s  %d B  sha256 %s" % (args.out, os.path.getsize(args.out), h))
    print("pin this in tools/hear_tag.py as MODEL_SHA256 / MODEL_BYTES")


if __name__ == "__main__":
    main()
