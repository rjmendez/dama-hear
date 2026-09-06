#!/usr/bin/env python3
"""Fit the shipped gunshot model. THIS FILE WAS MISSING.

shot_model.json was exported from a throwaway heredoc, so the weights could not be reproduced or
audited from anything on disk -- a shipped artifact nobody could refit. This is that script.

GROUPED CV is mandatory: one round appears on three boards inside one burst, so a random split
trains and tests on the same physical event and reports a number the field will not reproduce.

`gap` is dropped by default. Two reasons, both measured:
  LEAKAGE -- over the 1014 real board-events, 80.5% have gap under 1 s contributing <0.001 logits,
  while the 6.4% over 10 s reach +2.55 logits at p99 (813 s). That tail encodes when the operator
  paused between strings, not acoustics.
  UNSCOREABLE FIRST ROUND -- every capture's first event has no predecessor, so gap is None and
  score() refuses the event. 0 of 230 training rows had gap=None, so the case was never trained
  and is silently the most operationally important round in a string.
"""
import argparse, glob, json, os
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.metrics import roc_auc_score, classification_report

ALL_FEATURES = ["rise", "decay", "gap", "cent", "crest", "fhi", "peak"]
LOG10 = ["rise", "decay", "peak"]          # heavy-tailed; a linear model cannot see a decade raw


def load(labels_glob, items_path):
    L = {}
    for p in glob.glob(labels_glob):
        d = json.load(open(p))
        L[d["id"]] = d["label"]
    items = {i["id"]: i for i in json.load(open(items_path))}
    return L, items


def build(L, items, features):
    X, y, g, ids = [], [], [], []
    for k, lab in L.items():
        if lab == "unsure" or k not in items:
            continue
        it = items[k]
        row = []
        for f in features:
            v = it["gap"] if f == "gap" else it[f]
            row.append(-1.0 if v is None else float(v))
        X.append(row)
        y.append(1 if lab in ("crack", "both") else 0)
        g.append(int(it["utc"] // 3))       # one string never spans folds
        ids.append(k)
    return np.array(X, float), np.array(y), np.array(g), ids


def prep(X, features):
    Z = X.copy()
    for i, f in enumerate(features):
        if f in LOG10:
            Z[:, i] = np.log10(np.clip(Z[:, i], 1e-3, None))
    return Z


def fit_and_score(X, y, g, features, splits=5):
    Z = prep(X, features)
    m = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    p = cross_val_predict(m, Z, y, cv=GroupKFold(n_splits=splits), groups=g, method="predict_proba")[:, 1]
    return roc_auc_score(y, p), ((p >= 0.5) == y).mean(), p, m.fit(Z, y)


def export(model, features, auc, n):
    sc = model.named_steps["standardscaler"]
    lg = model.named_steps["logisticregression"]
    w = lg.coef_[0] / sc.scale_
    b = float(lg.intercept_[0] - np.dot(lg.coef_[0], sc.mean_ / sc.scale_))
    return {"features": features, "log10": [f for f in LOG10 if f in features],
            "w": w.tolist(), "b": b, "auc_grouped_cv": float(auc), "n_train": int(n)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels/labels/*.json")
    ap.add_argument("--items", default="label_items.json")
    ap.add_argument("--out", default=None, help="write shot_model.json here")
    ap.add_argument("--with-gap", action="store_true", help="keep the leaky feature (comparison only)")
    a = ap.parse_args()
    L, items = load(a.labels, a.items)
    feats = ALL_FEATURES if a.with_gap else [f for f in ALL_FEATURES if f != "gap"]
    X, y, g, _ = build(L, items, feats)
    print("n=%d shot=%d not-shot=%d groups=%d features=%s"
          % (len(y), y.sum(), len(y) - y.sum(), len(set(g)), ",".join(feats)))
    auc, acc, p, model = fit_and_score(X, y, g, feats)
    print("grouped 5-fold CV: AUC %.4f  acc %.4f" % (auc, acc))
    print(classification_report(y, (p >= 0.5).astype(int),
                                target_names=["not-shot", "shot"], digits=3))
    payload = export(model, feats, auc, len(y))
    for f, w in sorted(zip(feats, payload["w"]), key=lambda t: -abs(t[1])):
        print("   w[%-6s] %+.6g" % (f, w))
    if a.out:
        json.dump(payload, open(a.out, "w"), indent=1)
        print("wrote %s" % a.out)


if __name__ == "__main__":
    main()
