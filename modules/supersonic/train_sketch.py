#!/usr/bin/env python3
"""Fit a classifier that consumes the SKETCH -- the bytes the fleet actually transmits.

The shipped `model.json` reads six hand-engineered features. Nothing consumed a sketch, so the
fleet was transmitting a representation no model could score. This fits one that does.

⚠️THE SKETCH DOES NOT MEASURABLY BEAT THE HAND FEATURES, AND THE DOCS USED TO IMPLY IT DID.
Nested grouped CV on the same 228 events and the same folds (C chosen INSIDE each outer fold, so
the number is not selected on its own test statistic):

    sketch, absolute dB (160)      0.9634
    six hand features              0.9584
    sketch + the six together      0.9668
    sketch, shape only (level out) 0.9450

Paired bootstrap over the 69 groups: sketch - hand = **+0.0054, 95% CI [-0.0098, +0.0232]**,
P(sketch better) 0.75. That is a tie. An earlier note quoted 0.9631 against 0.9589 as though the
sketch won; on this corpus, at this n, nothing separates them.

The case for the sketch is not accuracy. It is that (a) it is what fits in a Meshtastic packet,
(b) it commits to no interpretation, so the same bytes can be retrained for cicadas next year,
and (c) a node cannot be asked to compute `rise`/`decay`/`crest` reliably -- the envelope work
that produced those is exactly what does not fit on the node.

⚠️THINGS THAT DO NOT HELP, MEASURED, SO NOBODY RETRIES THEM.
  * Round-to-round interval, capped at 250 ms so it cannot encode an operator's pause:
    0.9634 -> 0.9630. Alone it scores AUC 0.32 -- ANTI-predictive, because short intervals mark
    retriggers, which are labelled not-shot. Burst structure carries nothing here.
  * Band/frame summaries (spectrum + envelope + deltas, 36 dims): 0.9554. The full sketch is
    better than a hand-summarised one, which is the argument against summarising it at all.
  * Uncapped interval (the leaky `gap`): 0.9627, i.e. the leak was not even buying anything.

USAGE
    python3 modules/supersonic/train_sketch.py \
        --items ~/analysis_20260905/label_items.json \
        --labels '~/analysis_20260905/labels/labels/*.json' \
        --out modules/supersonic/model_sketch.json
"""
from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from hear import sketch as SK              # noqa: E402
from hear.node import detect as DT         # noqa: E402

#: The clips are 0.5 s with the event about 60 ms in. Searching the whole clip lets a later
#: reflection steal the onset on 85 of 228 events; this is the same "chunk plus one guard" the
#: node and the phone use, expressed in the clip's own frame.
SEARCH_S = (0.035, 0.085)
GRID = [0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0]


def _onset(xi, fs):
    e = DT.envelope(xi, fs)
    lo, hi = int(SEARCH_S[0] * fs), int(SEARCH_S[1] * fs)
    return lo + int(np.argmax(e[lo:hi]))


def sketch_of(xi, fs, layout=SK.LAYOUT_FIXED):
    """The sketch a node would have sent for this clip: same onset rule, same bank, same bytes."""
    i0 = _onset(xi, fs)
    span = (SK.FRAMES - 1) * max(1, int(SK.HOP_S * fs)) + SK.NFFT
    seg = xi[i0:i0 + span]
    if len(seg) < span:
        seg = np.pad(seg, (0, span - len(seg)))
    q, ref = SK.sketch(seg, fs, layout=layout)
    return q, float(ref)


def load(labels_glob, items_path, layout=SK.LAYOUT_FIXED):
    import soundfile as sf
    L = {}
    for p in glob.glob(os.path.expanduser(labels_glob)):
        d = json.load(open(p))
        L[d["id"]] = d["label"]
    items = {i["id"]: i for i in json.load(open(os.path.expanduser(items_path)))}
    X, y, g, ids = [], [], [], []
    for k, lab in sorted(L.items()):
        if lab == "unsure" or k not in items:
            continue
        it = items[k]
        x, fs = sf.read(io.BytesIO(base64.b64decode(it["uri"].split(",", 1)[1])))
        if x.ndim > 1:
            x = x[:, 0]
        q, ref = sketch_of(x * 32767.0, fs, layout)
        X.append(q.astype(float).reshape(-1) / 2.0 + ref)      # absolute dB
        y.append(1 if lab in ("crack", "both") else 0)
        g.append(int(it["utc"] // 3))                          # one string never spans folds
        ids.append(k)
    return np.array(X), np.array(y), np.array(g), ids


def nested_auc(X, y, g, outer=5, inner=4):
    """AUC with C chosen inside each outer fold. A C picked against the reported score is a
    hyperparameter fitted on the test set, and this project has shipped one of those before."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    p = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=outer).split(X, y, g):
        best, bestC = -1.0, GRID[0]
        for C in GRID:
            ip = np.zeros(len(tr))
            for itr, ite in GroupKFold(n_splits=inner).split(X[tr], y[tr], g[tr]):
                m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000))
                m.fit(X[tr][itr], y[tr][itr])
                ip[ite] = m.predict_proba(X[tr][ite])[:, 1]
            a = roc_auc_score(y[tr], ip)
            if a > best:
                best, bestC = a, C
        m = make_pipeline(StandardScaler(), LogisticRegression(C=bestC, max_iter=5000))
        m.fit(X[tr], y[tr])
        p[te] = m.predict_proba(X[te])[:, 1]
    return roc_auc_score(y, p), p


def export(X, y, g, auc, layout, bands, frames):
    """Flatten the scaler into the weights so the node needs no sklearn -- 160 multiply-adds."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    best, bestC = -1.0, GRID[0]
    for C in GRID:
        p = np.zeros(len(y))
        for tr, te in GroupKFold(n_splits=5).split(X, y, g):
            m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000))
            m.fit(X[tr], y[tr])
            p[te] = m.predict_proba(X[te])[:, 1]
        a = roc_auc_score(y, p)
        if a > best:
            best, bestC = a, C
    m = make_pipeline(StandardScaler(), LogisticRegression(C=bestC, max_iter=5000)).fit(X, y)
    sc, lg = m.named_steps["standardscaler"], m.named_steps["logisticregression"]
    w = lg.coef_[0] / sc.scale_
    b = float(lg.intercept_[0] - np.dot(lg.coef_[0], sc.mean_ / sc.scale_))
    return {
        "kind": "sketch_db",
        "layout": layout, "bands": int(bands), "frames": int(frames),
        "order": "band_major",          # x[b*frames + t], matching SK.sketch's own reshape
        "w": w.tolist(), "b": b,
        "auc_nested_grouped_cv": float(auc), "n_train": int(len(y)),
        "C": float(bestC),
        "note": ("absolute dB: q/2 + ref_db. ref_db MUST be added back -- a sketch scored without "
                 "its reference is missing the single strongest term this project has measured."),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--items", default="~/analysis_20260905/label_items.json")
    ap.add_argument("--labels", default="~/analysis_20260905/labels/labels/*.json")
    ap.add_argument("--layout", default=SK.LAYOUT_FIXED,
                    choices=[SK.LAYOUT_NYQUIST, SK.LAYOUT_FIXED])
    ap.add_argument("--bands", type=int, default=SK.MEL_BANDS,
                    help="keep only the lowest N bands. A model that must score a 16 kHz node "
                         "cannot use bands that node does not have: SK.valid_bands(16000)=15.")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    X, y, g, ids = load(a.labels, a.items, a.layout)
    if a.bands < SK.MEL_BANDS:
        keep = np.array([b * SK.FRAMES + t for b in range(a.bands) for t in range(SK.FRAMES)])
        X = X[:, keep]
    print("n=%d shot=%d not-shot=%d groups=%d layout=%s dims=%d"
          % (len(y), y.sum(), len(y) - y.sum(), len(set(g)), a.layout, X.shape[1]))
    auc, _ = nested_auc(X, y, g)
    print("nested grouped 5x4 CV: AUC %.4f" % auc)
    payload = export(X, y, g, auc, a.layout, a.bands, SK.FRAMES)
    payload["min_fs_hz"] = float(min(
        [fs for fs in sorted(SK.FS_CODES) if SK.valid_bands(fs, layout=a.layout) >= a.bands]
        or [0.0]))
    print("chosen C %.3g, %d weights" % (payload["C"], len(payload["w"])))
    if a.out:
        json.dump(payload, open(os.path.expanduser(a.out), "w"), indent=1)
        print("wrote %s (%d B)" % (a.out, os.path.getsize(os.path.expanduser(a.out))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
