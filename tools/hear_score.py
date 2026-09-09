#!/usr/bin/env python3
"""Score the pooled sketches with the shipped model, and say what it could NOT score.

    python3 tools/hear_score.py --pool ~/hear-pool                  # score everything new
    python3 tools/hear_score.py --pool ~/hear-pool --census         # read-only, writes nothing
    python3 tools/hear_score.py --pool ~/hear-pool --check          # exit 1 if nothing is flowing

WHY THIS EXISTS. `modules/supersonic/classify.score_sketch` has been trained, versioned and
shipped, and until this file it had ZERO callers outside its own tests. Every model in
`modules/supersonic/` was fitted, exported with its AUC, and never once applied to a byte the
fleet produced. The consequence is not that scores are wrong -- it is that nobody can say what
any detection WAS. The pool holds the frames; nothing read them.

⚠️THE POOL IS THE LANE THAT HAS INPUT. `dama-sketch-corpus` subscribes `dama/+/acoustic_sketch`
and has been logging "0 messages": the phone port is not in a release and the hugbot emitter is
an unmerged PR. A live-MQTT scorer would starve on day one. The phone/MQTT lane is nonetheless
already wired THROUGH this store -- the corpus worker lands JSONL on the PVC, `hear-drain
--phone-corpus` ingests it into the same pool, and this scorer reads every source the pool holds.
It degrades to zero input by construction, and `by_source` reports `phone: 0` as a NUMBER rather
than as an absence, so the day a publisher appears the count moves without a code change.

⚠️REFUSALS ARE THE PRODUCT, NOT AN ERROR PATH. `score_sketch` refuses rather than pads, and a
scorer that swallowed those refusals would report a quiet night from a corpus it could not read.
Measured on one real 1038-record pool: 955 (92.0%) refused, every one on the legacy `nyquist`
band layout, and the same corpus split by day is 0% scorable in the 2026-09-07 partition and
100% in the 2026-09-08 one. A single scalar refusal count reports neither of those honestly, so
every refusal is counted BY REASON, BY NODE and BY DAY PARTITION.

⚠️REFUSAL IS A STEP FUNCTION, NOT A RATE. `valid_bands` holds at 15 down to fs 13678 Hz and is 14
below it, so a rate drift crosses from 0% refused to 100% refused with nothing in between. There
is therefore no threshold to tune: a reason appearing in a bucket that never had it is an EVENT.
`--check` fails on a newly-seen reason, not on a percentage.

⚠️HEALTH MUST NOT KEY ON THE CLASS DISTRIBUTION. A normal night is P at the floor everywhere --
measured, 1 record of 1690 above 0.5 in one population and a maximum of 1.24e-5 across the 83
scorable rows of another. A check asking "did anything score high" reads a correct result as a
broken service. `--check` keys on THROUGHPUT, REFUSALS, PARSE FAILURES and STALENESS. The class
distribution is published under `observation_not_health` and is not an input to any gate.

⚠️P IS NOT "THE PROBABILITY A GUNSHOT HAPPENED". It is a frozen 228-event logistic separating
supersonic-crack-at-that-range from not-shot-at-that-range, on this frame's ABSOLUTE, UNCALIBRATED
level and 15-band shape. It is not calibrated to any field base rate (the corpus is 43.9% shots;
a 1e-3 field prior is -6.66 logits, which at this model's 0.5959 logits/dB is 11.2 dB against a
total p0.1-p0.9 range of 7.37 dB). It is not comparable between two nodes: median `ref_db` on one
real pool is 73.2 dB on mach and 44.9 dB on nyquist, a 28.4 dB gap = 16.9 logits = 3.9x that whole
range, on identical hardware. And a low P is not evidence of absence -- no true positive has ever
been scored on the night population, so no measured detection rate for it exists. Every row
carries those four facts as booleans so a reader cannot get the number without them.

⚠️THE SHIPPED AUC WAS NOT MEASURED AT THE RATE THE FLEET RUNS. `auc_nested_grouped_cv` is a
48 kHz train / 48 kHz test figure; 100% of the pooled node frames are 16 kHz. This repo has
already measured the number for exactly this operation -- a 48 kHz-fitted fixed-bank model applied
to 16 kHz audio over the common 15 bands -- and it is 0.9473 (hear/corpus.py:283-285,
hear/sketch.py:124-130). Publishing the bare 0.96648 against a 16 kHz frame would repeat, in a
machine-readable field that outlives this file, the wrong-artifact-AUC error this repo has already
made twice in its own docs. Both numbers ship on every row, and `auc_is_optimistic` ships beside
them because the grouping the CV used (`int(utc // 3)`) is 7.5x narrower than the corpus's
measured 22.4 s decorrelation lag (modules/supersonic/validate_sketch.py:19-26).

WHAT IT WRITES. `<pool>/scores/<day>/<node>.jsonl`, one row per pool record, plus
`<pool>/scores/model_card.json` (the prose provenance, written once per model, referenced by
sha256 from every row) and `<pool>/state/score_heartbeat.json` (instrumentation for `--check`).
It NEVER writes `<pool>/records` -- that store is hear-drain's, it has no locking, and a second
writer's key cache would silently miss the first writer's rows.

RESUME IS THE SCORE STORE ITSELF. On start it reads back every `key` it has already emitted and
scores the pool records whose key is absent. There is no watermark, so there is nothing to tear
and no second source of truth to disagree with the data.

⚠️A FILE, PARTITION, OFFSET OR TIMESTAMP WATERMARK WOULD BE WRONG, MEASURED. A later drain was
observed appending 513 rows INTO an already-scanned `2026-09-09/node.jsonl` and creating no new
partition, so every offset-based mark loses those rows permanently. Partition order is
`['2026-09-08', '2026-09-09', 'unanchored']` -- clockless rows sort last, after every dated one --
and rows are not time-ordered even within one file (2 and 5 timestamp inversions measured in two
real partition files). Cost of the full key rescan: 0.013 s over 1690 rows, 1.10 s over 550k.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️THE CLASSIFIER IS IMPORTED AS A PACKAGE PATH, NOT VIA A SECOND sys.path INSERT INTO
# modules/supersonic (which is how tests/test_classify_sketch.py reaches it). The difference is
# not style: deploy/k8s/gen_configmap.py's audit resolves `hear.*` and `modules.*` import names
# to files and refuses to ship a bundle missing one, and a bare `import classify` reaching the
# same module through a path insert is invisible to it -- the ConfigMap would generate cleanly
# without classify.py and the CronJob would die on ModuleNotFoundError in the cluster. `modules`
# and `modules/supersonic` carry no __init__.py, so this is a PEP 420 namespace import and needs
# nothing on disk beyond the file itself and PYTHONPATH=/app.
from hear import sketch as SK                                              # noqa: E402
from modules.supersonic import classify as CL                              # noqa: E402

SCORE_SCHEMA = "hear.sketch_score.v1"

#: The rate the shipped sketch models were FITTED at. Not inferred from the file: hear/sketch.py
#: :124 states it in as many words -- "a model trained on 48 kHz sketches and applied to the same
#: audio at 16 kHz drops from AUC 0.9634 to 0.9141". The pool is 16 kHz, so this constant is the
#: reason `auc_nested_grouped_cv` alone is not an honest field.
AUC_MEASURED_AT_FS_HZ = 48000.0

#: The same repo's measurement of what this model does at the rate the fleet actually runs:
#: fixed bank, common 15 bands, 48 kHz fit scored on 16 kHz audio. hear/corpus.py:285.
AUC_CROSS_RATE_48K_TO_16K = 0.9473

#: Corpus composition behind every shipped sketch model. modules/supersonic/validate_sketch.py:60,
#: "prevalence 43.9% over 100 shot / 128 not-shot". A logistic's intercept absorbs this, so it is
#: the one number a reader needs to shift z to a field prior; publishing `prior_applied: false`
#: without it is like publishing `level_calibrated: false` without ref_db.
TRAIN_POS, TRAIN_NEG = 100, 128

#: Grouping used by the shipped CV against the lag at which this corpus's features decorrelate.
#: modules/supersonic/validate_sketch.py:19-26. 3 s was sized to keep one firing string in one
#: fold; it was then reused as an independence guarantee it was never sized to provide.
CV_GROUP_WIDTH_S, DECORRELATION_LAG_S = 3.0, 22.4

#: 10*log10(0 + 1e-12) -- hear/sketch.py:174's floor, i.e. an ALL-ZERO sketch. Such a frame is
#: scorable and scores at the floor, so it is invisible to the refusal counter; it is a producer
#: defect rather than a quiet night and gets its own tag. Measured at 2.4% of one real pool.
SILENT_REF_DB = -119.9

#: How many runs the heartbeat keeps. The scorer runs 4x as often as the check (see
#: deploy/k8s/hear-score.yaml), so a per-run field would be three runs stale before the gate read
#: it -- the same trap tools/hear_drain.py's `unfetched_recent` ring exists to close.
RUN_RING = 64

#: `--check` sums the ring over this window. Must cover every scoring run since the previous check
#: or the measurements in between are never looked at. 7200 s is two check periods, so one missed
#: check still reports the loss. Coupled to the two schedules: change either cron and move this.
DEFAULT_RUN_WINDOW_S = 7200.0

#: `--check` fails when the last completed run is older than this. Two scoring periods plus slack.
DEFAULT_MAX_STALE_S = 7200.0

# ----------------------------------------------------------------- refusal vocabulary

#: Every way a record can fail to become a score. Each is a DIFFERENT operator action -- a layout
#: refusal is legacy firmware, a bands refusal is a rate mismatch, an unparseable line is a torn
#: write -- so they are never lumped. `UNCLASSIFIED` exists so that a refusal this file does not
#: recognise is still counted and still carries its message, rather than being dropped.
R_FRAMES = "frames_mismatch"
R_BANDS_SHORT = "bands_short"
R_LAYOUT_UNSTATED = "layout_unstated"
R_LAYOUT_MISMATCH = "layout_mismatch"
R_BANDS_FOR_RATE = "bands_insufficient_for_rate"
R_MODEL_WEIGHTS = "model_weights_mismatch"
R_FS_UNSTATED = "fs_unstated"
R_FRAME_UNDECODABLE = "frame_undecodable"
R_LINE_UNPARSEABLE = "line_unparseable"
R_UNCLASSIFIED = "mismatch_unclassified"

REFUSAL_REASONS = (R_FRAMES, R_BANDS_SHORT, R_LAYOUT_UNSTATED, R_LAYOUT_MISMATCH,
                   R_BANDS_FOR_RATE, R_MODEL_WEIGHTS, R_FS_UNSTATED, R_FRAME_UNDECODABLE,
                   R_LINE_UNPARSEABLE, R_UNCLASSIFIED)


# ----------------------------------------------------------------- PURE

def model_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def logits_per_db(model: Dict[str, Any]) -> float:
    """d(z)/d(ref_db). Every weight multiplies (q/2 + ref_db), so ref_db's coefficient is sum(w).

    This is the whole reason a bare P is not comparable between two nodes: it converts a level
    offset straight into logits. 0.595903 for the shipped 15-band model, so the p=0.1..0.9 range
    is 2*ln(9)/0.5959 = 7.37 dB of absolute, uncalibrated level.
    """
    return float(sum(float(w) for w in model["w"]))


def z_decompose(frame: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, Dict[str, float]]:
    """(z, {bias, level, shape}) for a frame the model accepts. sigmoid(clamp(z)) IS score_sketch.

    ⚠️PUBLISH z, NOT ONLY P. P saturates: every frame below z=-40 prints as 0.000000 and the
    28.4 dB inter-node level offset measured on real data is invisible in it. In z the offset is
    additive, so a reader can correct it after the fact; in P it cannot be recovered. The split
    into level and shape is what lets a reader see that a high P came from loudness rather than
    from spectrum -- measured, white noise at ref_db 50.8 scores 0.886 while a synthetic impulse
    at ref_db 108.0 scores 0.000000, so neither term dominates universally.
    """
    q, ref = frame["q"], float(frame["ref_db"])
    want_b, want_f = int(model["bands"]), int(model["frames"])
    w = model["w"]
    bias = float(model["b"])
    shape = 0.0
    i = 0
    for b in range(want_b):                       # band-major, matching model["order"]
        row = q[b]
        for t in range(want_f):
            shape += float(w[i]) * (row[t] / 2.0)
            i += 1
    level = logits_per_db(model) * ref
    return bias + level + shape, {"bias": bias, "level": level, "shape": shape}


def classify_refusal(frame: Dict[str, Any], model: Dict[str, Any]) -> str:
    """Which of `score_sketch`'s guards refused this frame.

    ⚠️THE ORDER IS score_sketch's OWN ORDER AND THAT IS THE POINT. The most specific cause must
    win. Measured on a real 1038-record pool: 955 rows are simultaneously legacy-`nyquist`-layout
    AND state no rate in the frame. Checking the rate first files all 955 under `fs_unstated`,
    which sends an operator to "a producer stopped stating its sample rate" when the actual fix is
    "reflash the nodes still emitting the pre-fixed-layout bank". Same 955 records, opposite
    conclusion, and the census is this tool's headline deliverable.

    Returns R_UNCLASSIFIED rather than guessing if no guard matches -- a refusal nobody recognises
    is still a counted refusal carrying its own message.
    """
    q = frame["q"]
    bands, frames = len(q), len(q[0])
    want_b, want_f = int(model["bands"]), int(model["frames"])
    if frames != want_f:
        return R_FRAMES
    if bands < want_b:
        return R_BANDS_SHORT
    if frame.get("layout") is None:
        return R_LAYOUT_UNSTATED
    if frame["layout"] != model.get("layout"):
        return R_LAYOUT_MISMATCH
    valid = frame.get("valid_bands")
    if valid is not None and valid < want_b:
        return R_BANDS_FOR_RATE
    if len(model["w"]) != want_b * want_f:
        return R_MODEL_WEIGHTS
    return R_UNCLASSIFIED


def model_block(model: Dict[str, Any], path: str, sha: str) -> Dict[str, Any]:
    """Model identity carried on EVERY row, refused ones included.

    ⚠️COPIED FROM THE LOADED FILE, NEVER RETYPED, so a row cannot claim an AUC its model does not
    state -- this repo has two live docstrings quoting AUCs that match no shipped artifact, which
    is exactly what retyping produces. The sha256 is what separates this 15-band model from the
    next 15-band model somebody trains; the band count alone would not.

    ⚠️THE AUC NEVER SHIPS BARE. `auc_nested_grouped_cv` was measured at 48 kHz, the pool is
    16 kHz, and the CV's grouping is narrower than the corpus's decorrelation lag. A reader who
    gets the number gets the two qualifications in the same object or the number is a lie of
    omission.
    """
    return {
        "file": os.path.basename(path),
        "sha256": sha,
        "kind": model.get("kind"),
        "bands": int(model["bands"]),
        "frames": int(model["frames"]),
        "order": model.get("order"),
        "layout": model.get("layout"),
        "min_fs_hz": model.get("min_fs_hz"),
        "n_train": model.get("n_train"),
        "auc_nested_grouped_cv": model.get("auc_nested_grouped_cv"),
        "auc_measured_at_fs_hz": AUC_MEASURED_AT_FS_HZ,
        "auc_cross_rate_48k_to_16k": AUC_CROSS_RATE_48K_TO_16K,
        "auc_is_optimistic": True,
        "logits_per_db": logits_per_db(model),
        "card": "model_card.json",
    }


def claim_block() -> Dict[str, bool]:
    """The five things a reader must not miss, as booleans, on every row.

    Prose belongs in the card; these are here because a field named `p` next to a field named
    `node` will be read as "the chance node X heard a gunshot", and each of these says it is not.
    """
    return {
        "is_general_classifier": False,
        "level_calibrated": False,
        "comparable_across_nodes": False,
        "prior_applied": False,
        "evidence_of_absence": False,
    }


def model_card(model: Dict[str, Any], path: str, sha: str) -> Dict[str, Any]:
    """The prose provenance, written once per model rather than on all ~550k rows a year.

    Measured reason for splitting it out: the full block repeated per row is 793 B of byte-
    identical text against a 781 B source record -- it would roughly double a 5 Gi PVC's score
    store to say the same sentence 550,000 times.
    """
    prior_logit = math.log(TRAIN_POS / float(TRAIN_NEG))
    return {
        "model_sha256": sha,
        "model_file": os.path.basename(path),
        "what_p_is": (
            "P is a frozen logistic fitted to 228 operator-labelled events separating "
            "supersonic-crack-at-that-range from not-shot-at-that-range, evaluated on this "
            "frame's absolute uncalibrated level and its %d-band shape. It is NOT a probability "
            "that a gunshot occurred, NOT calibrated to any field base rate, NOT comparable "
            "between two nodes, and NOT a general sound classifier."
            % int(model["bands"])),
        "trained_on": "228 operator-labelled events, one afternoon, one range, one rifle, one site",
        "labels_were": "y=1 for operator label 'crack' or 'both'; y=0 for everything else",
        "negatives_were": (
            "other amplitude-gate triggers at the same range on the same afternoon -- NOT "
            "insects, machinery, aircraft or any night-time sound. The model has never seen a "
            "single negative from the population it is being applied to here."),
        "upstream_gate": (
            "broadband amplitude over a 0.2083 s running ambient (hear/node/detect.py "
            "AMBIENT_TAU_S), so sustained sound largely never produces a sketch at all and P has "
            "no defined meaning for it -- not a low value, no value."),
        "train_pos": TRAIN_POS,
        "train_neg": TRAIN_NEG,
        "train_prevalence": TRAIN_POS / float(TRAIN_POS + TRAIN_NEG),
        "train_logit_offset": prior_logit,
        "how_to_apply_a_field_prior": (
            "shift z by logit(field_prior) - %.4f, then re-sigmoid. At a 1e-3 field prior that is "
            "%.2f logits = %.1f dB of ref_db, against a total p0.1-p0.9 range of %.2f dB."
            % (prior_logit,
               math.log(1e-3 / (1 - 1e-3)) - prior_logit,
               (math.log(1e-3 / (1 - 1e-3)) - prior_logit) / logits_per_db(model),
               2 * math.log(9.0) / logits_per_db(model))),
        "auc_nested_grouped_cv": model.get("auc_nested_grouped_cv"),
        "auc_measured_at_fs_hz": AUC_MEASURED_AT_FS_HZ,
        "auc_cross_rate_48k_to_16k": AUC_CROSS_RATE_48K_TO_16K,
        "auc_cross_rate_source": "hear/corpus.py:283-285, hear/sketch.py:124-130",
        "auc_is_optimistic": True,
        "auc_optimism_reason": (
            "nested grouped CV grouped events by int(utc // 3) = %.1f s; hear.validate."
            "decorrelation_lag_s measured these features not decorrelating until %.1f s apart, "
            "so 91.7%% of events have a different-group neighbour inside the lag. Under "
            "lag-respecting blocks both independent fits' alarm rates collapse from the 43.9%% "
            "design prevalence to 4.8%%/7.2%%. See modules/supersonic/validate_sketch.py."
            % (CV_GROUP_WIDTH_S, DECORRELATION_LAG_S)),
        "cv_group_width_s": CV_GROUP_WIDTH_S,
        "decorrelation_lag_s": DECORRELATION_LAG_S,
        "logits_per_db": logits_per_db(model),
        "p10_p90_span_db": 2 * math.log(9.0) / logits_per_db(model),
        "level_calibrated": False,
        "level_note": (
            "ref_db is 10*log10(max mel band power) over whatever numeric scale the producer fed "
            "in (hear/sketch.py:174). It is NOT SPL. A producer sketching float-normalised PCM "
            "instead of int16 counts is off by 10*log10(32768^2) = 90.31 dB = 53.8 logits, which "
            "pins the sigmoid at 0.0 or 1.0 silently. That is a units bug, not a gain trim."),
        "comparable_across_nodes": False,
        "comparability_note": (
            "median ref_db measured 73.2 dB on one node and 44.9 dB on another of identical "
            "hardware -- 28.4 dB = 16.9 logits = 3.9x the whole p0.1-p0.9 range. No shipped "
            "artifact corrects for it, so node_ref_db_offset_db is null with source 'none'."),
        "node_ref_db_offset_db": None,
        "node_ref_db_offset_source": "none",
        "evidence_of_absence": False,
        "operating_point_validated": False,
        "sensitivity_note": (
            "no true positive has ever been scored on the night population, so this tool has NO "
            "measured detection rate on it. The only tpr/fpr in the model file are in-sample and "
            "the training code itself calls the matching in-sample AUC meaningless. A run of "
            "near-zero scores is not 'no shots last night'."),
        "in_sample_only": {k: model.get(k) for k in
                           ("auc_in_sample_MEANINGLESS", "tpr_at_0.5_in_sample",
                            "fpr_at_0.5_in_sample") if k in model},
    }


def score_record(rec: Dict[str, Any], model: Dict[str, Any], mb: Dict[str, Any],
                 day: str, hydrate_fs: bool = False) -> Dict[str, Any]:
    """One stored pool record -> one score row. Never raises; every failure becomes a reason.

    ⚠️`sketch.unpack`'s DICT IS ALREADY score_sketch's INPUT CONTRACT -- q, ref_db, layout,
    valid_bands, fs_hz. No adapter is needed and none should be written; one would be a second
    place for the layout key to go missing, and a frame whose layout is absent must be REFUSED,
    not defaulted.

    ⚠️THE fs GUARD IS OURS BECAUSE score_sketch's IS SKIPPED WHEN fs IS UNSTATED. Its band check
    is `if valid is not None and valid < want_b`, and `unpack` sets valid_bands=None whenever the
    frame states no rate -- so an fs-less frame is scored with empty bands padded in as measured
    silence. Verified by stripping fs from a real 16 kHz frame: P=0.000000 instead of a refusal.
    It is checked AFTER score_sketch has had its say, so the layout and geometry causes -- which
    are more specific and imply different fixes -- always win the bucket.
    """
    row: Dict[str, Any] = {
        "schema": SCORE_SCHEMA,
        "key": rec.get("key"),
        "node": rec.get("node"),
        "source": rec.get("source"),
        "day": day,
        "ts_utc_s": rec.get("ts_utc_s"),
        "anchored": bool(rec.get("anchored")),
        "retrigger": bool(rec.get("retrigger")),
        "model": mb,
        "claim": claim_block(),
    }
    try:
        frame = SK.unpack(base64.b64decode(rec["frame_b64"]))
    except Exception as exc:                       # truncated frame, bad base64, absent field
        row.update(outcome="refused", refused_reason=R_FRAME_UNDECODABLE,
                   refused_detail="%s: %s" % (type(exc).__name__, exc),
                   p=None, z=None, z_terms=None, silent_frame=False, frame=None)
        return row

    frame_states_fs = frame.get("fs_hz") is not None
    pool_fs = rec.get("fs_hz")
    hydrated = False
    if hydrate_fs and not frame_states_fs and pool_fs is not None:
        # The CSV column the node wrote, which the frame's flag bits did not carry. Recorded as
        # hydrated so a row scored on a rate the FRAME never stated is never mistaken for one
        # that did. Measured: rescues 0 rows on the one real pool available, because those rows
        # are refused on layout first anyway -- it is here for a future fixed-layout producer
        # that states its rate only in the CSV.
        frame = dict(frame)
        frame["fs_hz"] = float(pool_fs)
        frame["valid_bands"] = SK.valid_bands(float(pool_fs), len(frame["q"]),
                                              layout=frame["layout"])
        hydrated = True

    ref_db = float(frame["ref_db"])
    row["frame"] = {
        "fs_hz": frame.get("fs_hz") if frame_states_fs or hydrated else pool_fs,
        "scored_at_fs_hz": frame.get("fs_hz"),
        "fs_stated_by": rec.get("fs_stated_by"),
        "frame_states_fs": frame_states_fs,
        "fs_hydrated_from_pool": hydrated,
        "bands": int(len(frame["q"])),
        "frames": int(len(frame["q"][0])),
        "valid_bands": frame.get("valid_bands"),
        "layout": frame.get("layout"),
        "ref_db": ref_db,
        "peak": frame.get("peak"),
        "no_context": bool(rec.get("no_context")),
        "clipped": rec.get("clipped"),
    }
    row["silent_frame"] = ref_db <= SILENT_REF_DB

    try:
        p = CL.score_sketch(frame, model)
    except CL.SketchMismatch as exc:
        row.update(outcome="refused", refused_reason=classify_refusal(frame, model),
                   refused_detail=str(exc), p=None, z=None, z_terms=None)
        return row

    if frame.get("valid_bands") is None:
        row.update(outcome="refused", refused_reason=R_FS_UNSTATED,
                   refused_detail=("this frame states no sample rate, so how many of its %d bands "
                                   "carry a measurement is unknown and score_sketch's band guard "
                                   "was skipped; the model wants %d"
                                   % (len(frame["q"]), int(model["bands"]))),
                   p=None, z=None, z_terms=None)
        return row

    z, terms = z_decompose(frame, model)
    row.update(outcome="scored", refused_reason=None, refused_detail=None,
               p=p, z=z, z_terms=terms)
    return row


def empty_tally() -> Dict[str, Any]:
    return {"lines_read": 0, "scored": 0, "refused": 0, "unparseable": 0, "silent": 0,
            "already_scored": 0, "by_reason": {}, "by_node": {}, "by_source": {},
            "by_node_day_reason": {}, "p": []}


def tally(t: Dict[str, Any], row: Dict[str, Any]) -> None:
    node = row.get("node") or "?"
    day = row.get("day") or "?"
    t["by_source"][row.get("source") or "?"] = t["by_source"].get(row.get("source") or "?", 0) + 1
    n = t["by_node"].setdefault(node, {"scored": 0, "refused": 0, "silent": 0})
    if row["outcome"] == "scored":
        t["scored"] += 1
        n["scored"] += 1
        if row.get("silent_frame"):
            t["silent"] += 1
            n["silent"] += 1
        if row.get("p") is not None:
            t["p"].append(float(row["p"]))
    else:
        t["refused"] += 1
        n["refused"] += 1
        r = row["refused_reason"]
        t["by_reason"][r] = t["by_reason"].get(r, 0) + 1
        t["by_node_day_reason"]["%s|%s|%s" % (node, day, r)] = \
            t["by_node_day_reason"].get("%s|%s|%s" % (node, day, r), 0) + 1


def distribution(ps: List[float]) -> Dict[str, Any]:
    """⚠️OBSERVATION, NOT A HEALTH INPUT, and the key it is published under says so.

    Measured basis: one population scored 437/437 non-zero with exactly 1 above 0.5; another had
    a maximum of 1.24e-5 over 83 rows. A normal night is P at the floor, so a gate keyed on "did
    anything score high" fires on a correct result and stays quiet on a broken one.
    """
    if not ps:
        return {"n": 0}
    s = sorted(ps)

    def pct(f):
        return s[min(len(s) - 1, int(f * len(s)))]

    return {"n": len(s), "min": s[0], "median": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
            "max": s[-1], "above_0_5": sum(1 for v in s if v > 0.5),
            "above_0_35": sum(1 for v in s if v > 0.35),
            "exactly_zero": sum(1 for v in s if v == 0.0)}


# ----------------------------------------------------------------- I/O

def records_dir(root: str) -> str:
    return os.path.join(root, "records")


def scores_dir(root: str) -> str:
    return os.path.join(root, "scores")


def state_dir(root: str) -> str:
    return os.path.join(root, "state")


def heartbeat_path(root: str) -> str:
    return os.path.join(state_dir(root), "score_heartbeat.json")


def _partitions(d: str) -> Iterator[Tuple[str, str]]:
    """(day-partition name, absolute jsonl path), oldest partition first.

    ⚠️WALKED HERE RATHER THAN THROUGH `Pool.raw()`, FOR TWO REASONS THAT ARE BOTH MEASURED.
    (1) `raw()` yields only the decoded dict, discarding WHICH day partition the line came from,
    and the day cannot be recovered from `ts_utc_s` for unanchored rows -- 421 of 1038 rows in one
    real pool have ts_utc_s=None and live in the `unanchored` partition, which is precisely where
    legacy pre-PPS frames collect and where the refusal breakdown matters most. (2) `raw()`'s
    `json.loads` carries no try/except where `Pool.keys()`'s identical parse does, so one torn
    line aborts a whole scan with a column offset instead of becoming a counted refusal.
    """
    if not os.path.isdir(d):
        return
    for day in sorted(os.listdir(d)):
        p = os.path.join(d, day)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if fn.endswith(".jsonl"):
                yield day, os.path.join(p, fn)


def count_lines(root: str) -> int:
    """How many stored records exist, counted WITHOUT decoding any of them.

    ⚠️THIS IS A SECOND, INDEPENDENT PASS AND THAT IS THE ENTIRE POINT. If `lines_read` were
    incremented by the same loop that files each record into a bucket, `lines_read == scored +
    refused + unparseable` would restate the loop rather than test it -- every path lands in a
    bucket by construction, so the assertion could not fail. Counting here, from the bytes, gives
    the two sides of the invariant different provenance, which is what makes it a check.
    """
    n = 0
    for _, path in _partitions(records_dir(root)):
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    n += 1
    return n


def already_scored(root: str) -> set:
    """Every pool key this store has already emitted a row for. THE resume token.

    Cost measured: 0.013 s over 1690 rows, 1.10 s over 550k -- cheaper than the watermark bugs it
    makes impossible. A line this scan cannot parse is skipped rather than fatal, matching
    `Pool.keys()`: a torn row in the SCORE store must not stop the scorer, it only means that key
    gets scored again, which is idempotent.
    """
    ks = set()
    for _, path in _partitions(scores_dir(root)):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    k = json.loads(line).get("key")
                except Exception:
                    continue
                if k:
                    ks.add(k)
    return ks


def _write_json_atomic(path: str, obj: Any) -> None:
    """tmp + os.replace. A torn heartbeat is a check that reads nothing and passes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run(root: str, model_path: str, hydrate_fs: bool = False, write: bool = True,
        now: Optional[float] = None) -> Dict[str, Any]:
    """Score every pool record not already scored. Returns the run report.

    ⚠️NOTHING IS EVER SWALLOWED. Each stored line lands in exactly one of scored / refused /
    unparseable, `lines_read` is counted by a separate pass over the same bytes, and the two are
    asserted to agree. `conservation_ok: False` is a hard `--check` failure, because the one
    outcome worse than a refused frame is a frame nobody can account for.
    """
    now = time.time() if now is None else now
    model = CL.load_model(model_path)
    sha = model_sha256(model_path)
    mb = model_block(model, model_path, sha)

    t = empty_tally()
    t["lines_read"] = count_lines(root)          # independent pass, before any dispatch
    seen = already_scored(root)
    out_lines: Dict[Tuple[str, str], List[str]] = {}
    newest_pool_ts: Optional[float] = None
    dispatched = 0

    for day, path in _partitions(records_dir(root)):
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                dispatched += 1
                try:
                    rec = json.loads(line)
                except Exception as exc:
                    t["unparseable"] += 1
                    t["by_reason"][R_LINE_UNPARSEABLE] = \
                        t["by_reason"].get(R_LINE_UNPARSEABLE, 0) + 1
                    t.setdefault("unparseable_where", []).append(
                        {"file": os.path.relpath(path, root), "line": lineno,
                         "error": "%s: %s" % (type(exc).__name__, exc)})
                    continue
                ts = rec.get("ts_utc_s")
                if ts and (newest_pool_ts is None or ts > newest_pool_ts):
                    newest_pool_ts = float(ts)
                if rec.get("key") in seen:
                    t["already_scored"] += 1
                    continue
                row = score_record(rec, model, mb, day, hydrate_fs=hydrate_fs)
                tally(t, row)
                out_lines.setdefault((day, rec.get("source") or "unknown"), []).append(
                    json.dumps(row, sort_keys=True))

    # ⚠️`already_scored` is a fourth outcome and must be in the invariant, otherwise a restart --
    # the normal case -- would look like mass loss. `dispatched` comes from the scoring walk;
    # `lines_read` came from the counting walk; they are compared, not shared.
    accounted = t["scored"] + t["refused"] + t["unparseable"] + t["already_scored"]
    t["dispatched"] = dispatched
    t["conservation_ok"] = (accounted == t["lines_read"] == dispatched)
    t["records_seen"] = t["lines_read"]
    t["newly_scored"] = t["scored"] + t["refused"]
    t["observation_not_health"] = {"p_distribution": distribution(t.pop("p")),
                                   "note": "not an input to any --check gate; see the module "
                                           "docstring for why a class-distribution gate fires on "
                                           "a correct result"}
    t["model"] = mb
    t["pool_newest_ts_utc_s"] = newest_pool_ts
    t["hydrate_fs"] = hydrate_fs
    t["at"] = now

    if write:
        for (day, source), lines in sorted(out_lines.items()):
            d = os.path.join(scores_dir(root), day)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s.jsonl" % source), "a") as fh:
                fh.write("\n".join(lines) + "\n")
        os.makedirs(scores_dir(root), exist_ok=True)
        _write_json_atomic(os.path.join(scores_dir(root), "model_card.json"),
                           model_card(model, model_path, sha))
        write_heartbeat(root, t, now=now)
    return t


def write_heartbeat(root: str, report: Dict[str, Any], now: Optional[float] = None
                    ) -> Dict[str, Any]:
    """Merge this run into the heartbeat's ring, and record which refusal reasons are NEW.

    ⚠️A REASON APPEARING IN A BUCKET THAT NEVER HAD IT IS AN EVENT, NOT A RATE. Refusal here is a
    step function -- `valid_bands` is 15 down to fs 13678 Hz and 14 below it -- so a threshold on
    a refusal PERCENTAGE reads 0% right up until it reads 100% and never warns in between. The
    heartbeat therefore keeps the cumulative set of node|day|reason buckets ever seen, and a run
    that introduces one is flagged for `--check` to fail on.

    ⚠️THE FIRST RUN IS EXEMPT, DELIBERATELY. It establishes the census -- on one real pool that
    census is 92% refused on legacy layout, which is CORRECT and not a regression. Failing the
    very first check would train an operator to ignore this gate, which is the only failure mode
    worse than not having it. `first_run: true` is recorded so the exemption is visible.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(root)
    hb: Dict[str, Any] = {"runs": [], "buckets_ever": []}
    if os.path.exists(p):
        try:
            hb = json.load(open(p))
            hb.setdefault("runs", [])
            hb.setdefault("buckets_ever", [])
        except Exception:
            hb = {"runs": [], "buckets_ever": []}

    ever = set(hb.get("buckets_ever") or [])
    first_run = not hb.get("runs")
    seen_now = set(report.get("by_node_day_reason") or {})
    new_buckets = [] if first_run else sorted(seen_now - ever)

    hb["last_run_s"] = now
    hb["model"] = report.get("model")
    hb["buckets_ever"] = sorted(ever | seen_now)
    entry = {
        "at": now,
        "records_seen": report.get("records_seen"),
        "lines_read": report.get("lines_read"),
        "dispatched": report.get("dispatched"),
        "already_scored": report.get("already_scored"),
        "newly_scored": report.get("newly_scored"),
        "scored": report.get("scored"),
        "refused": report.get("refused"),
        "unparseable": report.get("unparseable"),
        "silent": report.get("silent"),
        "conservation_ok": bool(report.get("conservation_ok")),
        "by_reason": report.get("by_reason") or {},
        "by_node": report.get("by_node") or {},
        "by_source": report.get("by_source") or {},
        "new_buckets": new_buckets,
        "first_run": first_run,
        "pool_newest_ts_utc_s": report.get("pool_newest_ts_utc_s"),
        "observation_not_health": report.get("observation_not_health"),
    }
    hb["runs"] = (hb["runs"] + [entry])[-RUN_RING:]
    _write_json_atomic(p, hb)
    return hb


def check(root: str, max_stale_s: float = DEFAULT_MAX_STALE_S,
          window_s: float = DEFAULT_RUN_WINDOW_S,
          now: Optional[float] = None) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero when scoring is not flowing, not when scores are low.

    ⚠️EVERY GATE HERE IS ONE THAT CAN ACTUALLY FAIL, AND SEVERAL ARE ABSOLUTE RATHER THAN
    CONDITIONED ON GROWTH. A gate of the form "if the pool grew and nothing was scored" passes
    vacuously on an empty read -- point the scorer at `/pool` instead of `/pool/corpus` and
    records_seen, refusals and conservation are all 0, 0 and trivially consistent, and the whole
    check goes green while nothing is being scored at all. `tools/hear_drain.py:825` already
    solved this shape (`if not sensors: return 1`) and the gates below copy it: no heartbeat
    fails, no runs fails, a run that saw zero records fails, and a heartbeat older than
    `max_stale_s` fails whatever the pool looks like.

    ⚠️IT READS A WINDOW OF THE RING, NOT THE LAST RUN. The scorer runs 4x as often as this check,
    so keying on the newest entry would discard three of every four measurements -- the same trap
    the drain's reach-back ledger hit.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(root)
    if not os.path.exists(p):
        return 1, ["no heartbeat at %s -- hear-score has never completed a run" % p]
    try:
        hb = json.load(open(p))
    except Exception as exc:
        return 1, ["heartbeat at %s is unreadable (%s)" % (p, exc)]
    runs = hb.get("runs") or []
    if not runs:
        return 1, ["heartbeat records no runs"]

    lines, bad = [], 0
    last = runs[-1]
    age = now - float(hb.get("last_run_s") or last.get("at") or 0.0)
    if age > max_stale_s:
        lines.append("scorer   STALE     last run %.0f s ago (max %.0f)" % (age, max_stale_s))
        bad += 1
    else:
        lines.append("scorer   ok        last run %.0f s ago" % age)

    window = [r for r in runs if now - float(r.get("at") or 0.0) <= window_s]
    if not window:
        window = [last]
    lines.append("window   %d run(s) in the last %.0f s" % (len(window), window_s))

    seen = last.get("records_seen") or 0
    if not seen:
        lines.append("pool     EMPTY     the newest run read 0 records from %s -- an empty read "
                     "is a failure, not a quiet night (wrong --pool root?)" % records_dir(root))
        bad += 1
    else:
        lines.append("pool     ok        %d record(s) in the store" % seen)

    torn = sum(int(r.get("unparseable") or 0) for r in window)
    if torn:
        lines.append("parse    TORN      %d unparseable pool line(s) in the window" % torn)
        bad += 1

    broken = [r for r in window if not r.get("conservation_ok", True)]
    if broken:
        lines.append("ledger   BROKEN    %d run(s) could not account for every stored line"
                     % len(broken))
        bad += 1

    newb = sorted({b for r in window for b in (r.get("new_buckets") or [])})
    if newb:
        lines.append("refusals NEW       %d node|day|reason bucket(s) never seen before: %s"
                     % (len(newb), ", ".join(newb[:6]) + (" ..." if len(newb) > 6 else "")))
        bad += 1

    # ⚠️CONDITIONED ON GROWTH, AND ONLY USEFUL BECAUSE THE ABSOLUTE GATES ABOVE EXIST. In steady
    # state after catch-up, `newly_scored` is legitimately 0 every run -- that is a scorer that
    # is up to date, not a broken one. Growth is measured across the window's own endpoints.
    grew = (window[-1].get("records_seen") or 0) - (window[0].get("records_seen") or 0)
    fresh = sum(int(r.get("newly_scored") or 0) for r in window)
    if grew > 0 and fresh == 0:
        lines.append("through  STUCK     the store grew by %d record(s) across the window and the "
                     "scorer emitted 0 rows" % grew)
        bad += 1
    else:
        lines.append("through  ok        store +%d, %d row(s) emitted across the window"
                     % (grew, fresh))

    reasons: Dict[str, int] = {}
    for r in window:
        for k, v in (r.get("by_reason") or {}).items():
            reasons[k] = reasons.get(k, 0) + int(v)
    lines.append("refusals %s" % (json.dumps(reasons, sort_keys=True) if reasons else "none in "
                                  "the window"))
    dist = (last.get("observation_not_health") or {}).get("p_distribution") or {}
    lines.append("scores   %s  (OBSERVATION, not a gate)" % json.dumps(dist, sort_keys=True))
    return (1 if bad else 0), lines


def format_report(t: Dict[str, Any]) -> str:
    out = ["read %d  scored %d  refused %d  unparseable %d  already-scored %d  silent %d"
           % (t["lines_read"], t["scored"], t["refused"], t["unparseable"],
              t["already_scored"], t["silent"]),
           "conservation %s (%d + %d + %d + %d == %d read == %d dispatched)"
           % ("OK" if t["conservation_ok"] else "BROKEN", t["scored"], t["refused"],
              t["unparseable"], t["already_scored"], t["lines_read"], t["dispatched"])]
    for node in sorted(t["by_node"]):
        n = t["by_node"][node]
        tot = n["scored"] + n["refused"]
        out.append("  %-10s scored %5d  refused %5d  (%5.1f%% scorable)  silent %d"
                   % (node, n["scored"], n["refused"],
                      100.0 * n["scored"] / tot if tot else 0.0, n["silent"]))
    if t["by_reason"]:
        out.append("refusals by reason: " + json.dumps(t["by_reason"], sort_keys=True))
        for b in sorted(t["by_node_day_reason"]):
            out.append("  %-52s %d" % (b, t["by_node_day_reason"][b]))
    out.append("by source: " + json.dumps(t["by_source"], sort_keys=True))
    out.append("P distribution (OBSERVATION, not a health input): "
               + json.dumps(t["observation_not_health"]["p_distribution"], sort_keys=True))
    out.append("⚠️P is not a probability that a gunshot occurred, is not comparable between "
               "nodes, and a low P is not evidence of absence. See scores/model_card.json.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", default="~/hear-pool", help="pool root; records/ is read, "
                                                          "scores/ and state/ are written")
    ap.add_argument("--model", default=CL.FLEET_SKETCH_MODEL,
                    help="⚠️the fleet model by default. The 20-band model refused 1690 of 1690 "
                         "pooled records for wanting 20 bands where a 16 kHz node carries 15")
    ap.add_argument("--hydrate-fs", action="store_true",
                    help="score a frame that states no rate using the rate the POOL recorded "
                         "from the CSV, instead of refusing it. Off by default: the frame is "
                         "authoritative about itself and a hydrated row is marked as such")
    ap.add_argument("--census", action="store_true",
                    help="score everything and report, writing nothing -- for the first look at "
                         "a pool nobody has scored")
    ap.add_argument("--check", action="store_true",
                    help="read the heartbeat and exit non-zero if scoring is not flowing; "
                         "scores nothing")
    ap.add_argument("--max-stale-s", type=float, default=DEFAULT_MAX_STALE_S)
    ap.add_argument("--window-s", type=float, default=DEFAULT_RUN_WINDOW_S,
                    help="--check sums the heartbeat ring over this many seconds. Must cover "
                         "every scoring run since the previous check ran")
    ap.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    a = ap.parse_args(argv)

    root = os.path.expanduser(a.pool)
    if a.check:
        code, lines = check(root, a.max_stale_s, window_s=a.window_s)
        print("\n".join(lines))
        return code

    t = run(root, os.path.expanduser(a.model), hydrate_fs=a.hydrate_fs, write=not a.census)
    print(json.dumps(t, indent=2, sort_keys=True) if a.json else format_report(t))
    # ⚠️REFUSALS DO NOT FAIL THE RUN. A refused frame is data this tool successfully read and
    # correctly declined to score; failing on it would make a legacy-firmware corpus look like a
    # broken scorer forever. What fails is losing track of a line.
    return 0 if t["conservation_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
