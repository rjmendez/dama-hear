#!/usr/bin/env python3
"""Tag the pooled clips with YAMNet, and say what it could NOT tag.

    python3 tools/hear_tag.py --pool ~/hear-pool --model-dir ~/hear-pool/models/yamnet
    python3 tools/hear_tag.py --pool ~/hear-pool --census          # read-only, writes nothing
    python3 tools/hear_tag.py --model-dir ... --verify-weights     # exit 2 unless both shas match
    python3 tools/hear_tag.py --pool ~/hear-pool --check           # exit 1 if not flowing

WHY THIS EXISTS. hear-drain now pulls clips off the cards into `clips/index.jsonl` and
`clips/<day>/<node>/*.wav`. Until this file nothing had ever opened one. 478 clips were destroyed
unheard before collection landed; collecting them and never listening is the same outcome with
more disk used.

⚠️IT IS EVENT TRIAGE, NOT SPECIES ID, AND THAT IS A PROPERTY OF THE MODEL. YAMNet has `Bird`,
`Owl`, `Hoot`, `Chirp` and stops -- it is structurally incapable of Barred Owl vs Great Horned
Owl. BirdNET was evaluated for the species question and refused: zero birds across nine real
clips, neotropical hypotheses at the confidence floor, 1 second of every 4 discarded (48 kHz x
3.0 s windows against a 4.0 s clip), and CC BY-NC-SA weights. PANNs/CNN14 was refused too: it
wants 32 kHz, 17% of its filterbank lands on guaranteed zeros at 16 kHz, and it measured 14%
lower confidence than YAMNet on the same A/B for 311 ms and 1.5 GB RSS against 12 ms and 82 MB.

⚠️NORMALISATION IS MANDATORY AND IT IS THE WHOLE FAILURE MODE. Measured on two real nyquist clips
pulled off the card 2026-09-09:

    nyquist-db21acd5-1082421378  raw -56.9 dBFS  ->  Silence 0.406 | Speech 0.183 | Animal 0.147
                                 RMS to -20 dBFS ->  Animal 0.307 | Cricket 0.204 | Speech 0.198
    nyquist-db21acd5-1082530195  raw -62.1 dBFS  ->  Silence 0.723 | Animal 0.055 | Fowl 0.042
                                 RMS to -20 dBFS ->  Animal 0.464 | Wild animals 0.331 | Bird 0.226

A pipeline that skips `normalise` exits 0 forever and emits Silence for every clip. It is not an
optimisation and it is not tuning; the un-normalised answer is wrong and looks healthy.

⚠️THE RATE IS ASSERTED, NEVER RESAMPLED. YAMNet does not validate its input rate and does not
resample -- feed it 22624 Hz audio and it degrades silently towards Silence rather than raising.
mach shipped a whole boot headed 22624 Hz, so this is a condition on the card today and not a
hypothetical. `assert_rate` REFUSES it, into a counted bucket. A resampler here would launder a
known firmware defect into plausible-looking tags.

⚠️THE WHOLE SCORE PICTURE IS STORED, NEVER A HARD TOP-1. Every class above SCORE_FLOOR, plus
`max_unstored_score` so the discarded tail is a number rather than an absence. hear/pool.py:721
already states the rule -- the pool's job is to hold everything and say what it holds. This repo
has twice lost data to a filter chosen at ingest, and a threshold picked now, on a corpus that is
mostly quiet and that nobody has listened to, either manufactures positives or deletes the honest
negatives a trainer would need.

⚠️THE 1024-d EMBEDDING IS STORED BECAUSE THE AUDIO IS A CACHE. It is free (same forward pass), it
is a fixed 16 kHz-native axis -- unlike the scene.csv (20x4, 62.5-7812.5 Hz) versus sketch (20x8,
300-20000 Hz) band-axis split that hear/pool.py refuses to pool across -- and `clips.prune()`
deletes WAVs at a 2 GiB cap. The embedding is what makes any later clustering possible without
re-fetching audio the node destroyed months ago.

⚠️A TAG IS NOT A LABEL AND MUST NOT BECOME A TRAINING TARGET. `provenance` is the literal string
"model" on every row and `claim.usable_as_training_label` is False. Fitting a scene- or sketch-
based model on these would measure whether a 20-band descriptor can reconstruct what a full-
fidelity model already decided, which is not correctness -- and this project has that exact
failure on file, all 35 dama ant models trained on circular self-labels.

WHAT IT WRITES. `<pool>/clips/tags.jsonl` (one row per clip per model version, append-only),
`<pool>/clips/tag_model_card.json` (the prose provenance, written once per model, referenced by
sha256 from every row) and `<pool>/state/tag_heartbeat.json` (instrumentation for --check). It
NEVER writes `clips/index.jsonl` -- that store is hear-drain's.

WEIGHTS ARE NEVER FETCHED BY THIS FILE. It verifies two pinned sha256 digests and refuses to run
otherwise, so a run is reproducible offline once the cache exists and a substituted model is a
loud failure rather than a different set of numbers. The CronJob's shell preamble does the one
fetch. See `verify_weights` for the two URLs and why each is pinned the way it is.

RESUME IS THE TAG STORE ITSELF -- the tag_key set, read back on every start, exactly as
hear_score.py rescans its score keys. No watermark, so there is nothing to tear.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import wave
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import clips as CLIPS                                            # noqa: E402
from hear import tags as TAGS                                             # noqa: E402

TAG_SCHEMA = "hear.clip_tag.v1"

MODEL_NAME = "yamnet"
#: Bumping this is what makes a re-tag a NEW row beside the old one rather than an overwrite.
#: It names the artifact, not the code: change it when the .tflite or the class map changes.
MODEL_VERSION = "tflite-1"

MODEL_FILE = "yamnet.tflite"
CLASSMAP_FILE = "yamnet_class_map.csv"

#: ⚠️VERIFIED BY DOWNLOADING AND HASHING THEM, 2026-09-09, not copied from a page. The Kaggle
#: endpoint serves a gzipped tar whose single member is `1.tflite`; the digest below is of that
#: MEMBER, because the tar wrapper carries an mtime and is not a stable identity.
YAMNET_SHA256 = "141fba1cdaae842c816f28edc4937e8b4f0af4c8df21862ccc6b52dc567993c3"
YAMNET_BYTES = 16096668
YAMNET_URL = ("https://www.kaggle.com/api/v1/models/google/yamnet/tfLite/tflite/1/download"
              "  (gzipped tar, member `1.tflite`)")

#: ⚠️PINNED TO A COMMIT, NOT TO `master`. The class map's own repo default branch is a moving ref;
#: this commit is the last one that touched the file (2019-11-21) and its bytes were checked to be
#: byte-identical to what `master` served on 2026-09-09.
CLASSMAP_SHA256 = "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"
CLASSMAP_BYTES = 14096
CLASSMAP_COMMIT = "dfffd623b6be8d1d9744b8e261fbac370d17c46d"
CLASSMAP_URL = ("https://raw.githubusercontent.com/tensorflow/models/" + CLASSMAP_COMMIT
                + "/research/audioset/yamnet/yamnet_class_map.csv")

#: What the interpreter must actually produce. Checked at load, so a substituted model that
#: happens to hash right for some other reason still cannot be scored as if it were this one.
YAMNET_CLASSES = 521
YAMNET_EMBED_DIM = 1024
#: 64000 samples in -> 8 hops of 0.48 s. Measured against the real interpreter, not assumed.
YAMNET_FRAMES = 8

#: RMS target. -20 dBFS is where the two real clips above stop reading as Silence; it is also far
#: enough below 0 that a 0.0134 peak clip does not clip after scaling.
TARGET_DBFS = -20.0

#: Classes below this are summarised by `max_unstored_score` instead of being stored. 0.01 keeps
#: roughly the top few dozen of 521 and is a STORAGE bound, not a decision threshold -- nothing
#: downstream may treat it as one.
SCORE_FLOOR = 0.01

#: `--check` sums the heartbeat ring over this window, and fails on a heartbeat older than this.
#: Coupled to the two crons in deploy/k8s/hear-tag.yaml: change either and move these.
RUN_RING = 64
DEFAULT_RUN_WINDOW_S = 7200.0
DEFAULT_MAX_STALE_S = 14400.0

DEFAULT_LIMIT = 400
DEFAULT_DEADLINE_S = 600.0

#: ⚠️REPORT-ONLY UNTIL THE PHASE-3 GATE IN docs/acoustic-stack.md IS MET. -1 means "print the
#: measured silence fraction and never fail on it". A threshold set before anyone has listened to
#: a single clip would be a number from a guess dressed as a measurement; this repo's own rule is
#: to derive a threshold from the measured envelope.
SILENCE_FRAC_REPORT_ONLY = -1.0
SILENCE_CLASS = "Silence"

# ----------------------------------------------------------------- refusal vocabulary
#
# Every way a stored index row can fail to become a tag. Each is a DIFFERENT operator action --
# a pruned WAV is the cap doing its job, a missing one is a bug, an out-of-tolerance rate is the
# fs_clean latch on mach -- so they are never lumped, and none of them may fall into a default.

R_NO_AUDIO = "no_audio_stored"
R_AUDIO_PRUNED = "audio_pruned"
R_AUDIO_MISSING = "audio_missing"
R_WAV_UNREADABLE = "wav_unreadable"
R_WAV_SHAPE = "wav_not_16bit_mono"
R_WAV_SAMPLES = "wav_sample_count"
R_RATE_REFUSED = "rate_out_of_tolerance"
R_DIGITAL_SILENCE = "digital_silence"
R_MODEL_ERROR = "model_error"
R_INDEX_ROW = "index_row_incomplete"
R_LINE_UNPARSEABLE = "index_line_unparseable"

REFUSAL_REASONS = (R_NO_AUDIO, R_AUDIO_PRUNED, R_AUDIO_MISSING, R_WAV_UNREADABLE, R_WAV_SHAPE,
                   R_WAV_SAMPLES, R_RATE_REFUSED, R_DIGITAL_SILENCE, R_MODEL_ERROR, R_INDEX_ROW,
                   R_LINE_UNPARSEABLE)


class WeightsRefused(RuntimeError):
    """The pinned model or class map is absent, the wrong size, or the wrong sha256.

    ⚠️IT IS RAISED, NOT LOGGED. A tagger that cannot prove which weights it is holding must stop:
    the alternative is a store full of rows attributing scores to a model that did not produce
    them, which no later reader can detect.
    """


class RateRefused(ValueError):
    """The WAV header states a rate YAMNet's fixed 16 kHz frontend cannot be fed."""


# ----------------------------------------------------------------- PURE

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def weights_paths(model_dir: str) -> Tuple[str, str]:
    d = os.path.expanduser(model_dir)
    return os.path.join(d, MODEL_FILE), os.path.join(d, CLASSMAP_FILE)


def verify_weights(model_dir: str) -> Dict[str, Any]:
    """Both artifacts present, right size, right sha256. Never raises; the caller decides.

    ⚠️IT HASHES THE FILE, IT DOES NOT TEST THAT A DIRECTORY EXISTS. The existing numpy guard in
    deploy/k8s/hear-drain.yaml tested only for a directory and therefore enforced nothing -- the
    first workload to reach an empty PVC decided the version and the other silently used whatever
    it found. Existence is the weaker question and it is not the one being asked here.
    """
    mp, cp = weights_paths(model_dir)
    out: Dict[str, Any] = {"ok": False, "problems": [], "model_path": mp, "class_map_path": cp,
                           "model_sha256": None, "class_map_sha256": None,
                           "model_bytes": None, "class_map_bytes": None}
    for path, want_sha, want_n, sha_field, n_field, url in (
            (mp, YAMNET_SHA256, YAMNET_BYTES, "model_sha256", "model_bytes", YAMNET_URL),
            (cp, CLASSMAP_SHA256, CLASSMAP_BYTES, "class_map_sha256", "class_map_bytes",
             CLASSMAP_URL)):
        if not os.path.exists(path):
            out["problems"].append("%s is absent. Fetch it from %s" % (path, url))
            continue
        n = os.path.getsize(path)
        out[n_field] = n
        if n != want_n:
            out["problems"].append("%s is %d B, expected %d B" % (path, n, want_n))
            continue
        got = sha256_file(path)
        out[sha_field] = got
        if got != want_sha:
            out["problems"].append("%s sha256 is %s, expected %s -- these are NOT the pinned "
                                   "weights and the run is refused" % (path, got, want_sha))
    out["ok"] = not out["problems"]
    return out


def read_wav(path: str) -> Tuple["Any", int]:
    """(float32 in [-1, 1], header rate). Raises on anything that is not 16-bit mono PCM.

    ⚠️NO RESAMPLING, HERE OR ANYWHERE. The rate is returned so `assert_rate` can refuse it. `wave`
    is stdlib and reads the canonical 44-byte header the firmware writes; hear.clips.wav_probe
    already validated the same header at drain time, and the two agreeing is checked by a test
    rather than assumed.
    """
    import numpy as np
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1:
            raise ValueError("channels=%d, not mono" % w.getnchannels())
        if w.getsampwidth() != 2:
            raise ValueError("sampwidth=%d bytes, not 16-bit" % w.getsampwidth())
        fs = int(w.getframerate())
        raw = w.readframes(w.getnframes())
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return pcm, fs


def dbfs(x: "Any") -> float:
    """20*log10(RMS). -inf for digital silence, which the caller must handle rather than scale."""
    import numpy as np
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0
    return float("-inf") if rms <= 0.0 else 20.0 * float(np.log10(rms))


def normalise(x: "Any", target_dbfs: float = TARGET_DBFS) -> Tuple["Any", float]:
    """(scaled audio, PRE-normalisation dBFS). Raises on digital silence.

    ⚠️MANDATORY. See the module docstring for the measured before/after on two real clips: at the
    -49 to -62 dBFS these are recorded at, YAMNet answers Silence and nothing else.

    ⚠️AN ALL-ZERO CLIP IS REFUSED, NOT SCALED. Its gain is undefined, and tagging it `Silence` --
    which is what any finite gain would produce -- makes a producer defect indistinguishable from
    the normalisation bug this function exists to prevent. It gets its own counted reason.
    """
    import numpy as np
    pre = dbfs(x)
    if pre == float("-inf"):
        raise ValueError("digital silence: every sample is zero, so no gain reaches %.1f dBFS"
                         % target_dbfs)
    y = np.clip(x * (10.0 ** ((target_dbfs - pre) / 20.0)), -1.0, 1.0)
    return y.astype(np.float32), pre


def assert_rate(header_fs: int, csv_fs: Optional[float] = None) -> None:
    """Raise unless the WAV header is within clips.FS_TOLERANCE_HZ of 16 kHz.

    ⚠️THE HEADER IS AUTHORITATIVE, NOT THE CSV. `fs_hz` in dets.csv is the node's own estimate and
    has disagreed with the file it describes by 6,624 Hz for a whole boot on mach. The header is
    what the samples were written at; the CSV value is carried into the tag row beside it so the
    disagreement stays visible, but it is never what is checked.
    """
    if abs(float(header_fs) - CLIPS.FS_NOMINAL_HZ) > CLIPS.FS_TOLERANCE_HZ:
        raise RateRefused(
            "WAV header states %d Hz; YAMNet's frontend is fixed at %d Hz and does not resample, "
            "so this would degrade silently towards Silence rather than fail. Tolerance is "
            "+/-%.0f Hz (measured header spread is 15986-16000; mach shipped a boot at 22624). "
            "The dets CSV said %s Hz."
            % (int(header_fs), int(CLIPS.FS_NOMINAL_HZ), CLIPS.FS_TOLERANCE_HZ, csv_fs))


class Tagger:
    """YAMNet under ai-edge-litert. One interpreter, reused across clips.

    ⚠️LiteRT, NOT TensorFlow. Bit-identical scores to the SavedModel at 12.1 ms/clip in 82 MB RSS
    from a 146 MB venv, against 14.3 ms in 903 MB from a 1.4 GB venv -- and the PVC is the binding
    constraint on this cluster, not CPU.

    ⚠️THE THREE OUTPUTS ARE IDENTIFIED BY THEIR LAST DIMENSION, NOT BY POSITION OR NAME. The
    exported graph names all three tensors `Identity`, `Identity_1`, `Identity_2`, which says
    nothing about which is which, and the order they are listed in is an implementation detail of
    the converter. 521 is the class count, 1024 the embedding width, 64 the mel bins; each is
    required to appear exactly once or the model is refused as not-this-model.
    """

    def __init__(self, model_path: str, class_map_path: str):
        from ai_edge_litert.interpreter import Interpreter        # lazy: absent in the test env
        self.class_names = load_class_map(class_map_path)
        if len(self.class_names) != YAMNET_CLASSES:
            raise WeightsRefused("class map holds %d names, expected %d"
                                 % (len(self.class_names), YAMNET_CLASSES))
        self._it = Interpreter(model_path=model_path)
        self._in = self._it.get_input_details()[0]
        widths = [int(d["shape"][-1]) for d in self._it.get_output_details()]
        for want in (YAMNET_CLASSES, YAMNET_EMBED_DIM):
            if widths.count(want) != 1:
                raise WeightsRefused(
                    "the model exposes output widths %r; exactly one tensor of width %d is "
                    "required and this is not YAMNet's tflite export" % (widths, want))
        self._score_i = [d["index"] for d in self._it.get_output_details()
                         if int(d["shape"][-1]) == YAMNET_CLASSES][0]
        self._embed_i = [d["index"] for d in self._it.get_output_details()
                         if int(d["shape"][-1]) == YAMNET_EMBED_DIM][0]

    def tag(self, pcm: "Any", floor: float = SCORE_FLOOR) -> Dict[str, Any]:
        """-> {"scores", "max_unstored_score", "n_classes_scored", "n_frames", "embedding"}.

        Scores and embedding are the MEAN over the model's 8 hops. A max would report the loudest
        0.48 s of a 4.0 s clip as the whole clip, which for a 1.0 s pre-roll plus a transient is
        exactly the wrong summary.
        """
        import numpy as np
        x = np.asarray(pcm, dtype=np.float32)
        self._it.resize_tensor_input(self._in["index"], [len(x)], strict=False)
        self._it.allocate_tensors()
        self._it.set_tensor(self._in["index"], x)
        self._it.invoke()
        s = np.asarray(self._it.get_tensor(self._score_i), dtype=np.float64)
        e = np.asarray(self._it.get_tensor(self._embed_i), dtype=np.float64)
        n_frames = int(s.shape[0])
        s, e = s.mean(axis=0), e.mean(axis=0)
        keep = {self.class_names[i]: float(s[i]) for i in range(len(s)) if s[i] >= floor}
        dropped = [float(v) for i, v in enumerate(s) if v < floor]
        return {"scores": keep,
                # ⚠️THE DISCARDED TAIL IS A NUMBER, NOT AN ABSENCE. Without it a floor that is too
                # high and a clip that genuinely scored nothing are the same empty dict.
                "max_unstored_score": max(dropped) if dropped else 0.0,
                "n_classes_scored": int(len(s)),
                "n_frames": n_frames,
                "embedding": [float(v) for v in e]}


def load_class_map(path: str) -> List[str]:
    """index,mid,display_name -> the display names, in index order."""
    names: List[str] = []
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if i == 0 and row and row[0] == "index":
                continue
            if len(row) < 3:
                raise WeightsRefused("class map row %d is %r, expected index,mid,display_name"
                                     % (i, row))
            names.append(row[2])
    return names


def model_block(verified: Dict[str, Any]) -> Dict[str, Any]:
    """Model identity carried on EVERY row, refused ones included.

    ⚠️THE sha256 IS THE IDENTITY. "yamnet" names a family with a SavedModel, a TFLite export, a
    quantised export and several forks; the digest is what separates the artifact that produced
    these numbers from the next thing somebody drops in the same directory.
    """
    return {"name": MODEL_NAME, "version": MODEL_VERSION,
            "file": MODEL_FILE, "sha256": verified.get("model_sha256"),
            "class_map_file": CLASSMAP_FILE, "class_map_sha256": verified.get("class_map_sha256"),
            "runtime": "ai-edge-litert", "input_fs_hz": int(CLIPS.FS_NOMINAL_HZ),
            "n_classes": YAMNET_CLASSES, "embed_dim": YAMNET_EMBED_DIM,
            "target_dbfs": TARGET_DBFS, "score_floor": SCORE_FLOOR,
            "card": "tag_model_card.json"}


def claim_block() -> Dict[str, bool]:
    """The five things a reader must not miss, as booleans, on every row.

    A field named `scores` next to a field named `node` will be read as "what was heard". Each of
    these says what it is not, in a form a query can filter on rather than prose in a card.
    """
    return {"is_species_id": False,
            "human_verified": False,
            "calibrated_to_this_site": False,
            "trained_on_this_corpus": False,
            # ⚠️§12 of the spec, as a field. All 35 dama ant models were trained on circular
            # self-labels; the refusal has to travel with the data, not only in a document.
            "usable_as_training_label": False}


def model_card(verified: Dict[str, Any]) -> Dict[str, Any]:
    """The prose provenance, written once per model rather than on every row."""
    return {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "model_sha256": verified.get("model_sha256"),
        "model_bytes": verified.get("model_bytes"),
        "model_source": YAMNET_URL,
        "class_map_sha256": verified.get("class_map_sha256"),
        "class_map_bytes": verified.get("class_map_bytes"),
        "class_map_source": CLASSMAP_URL,
        "class_map_pinned_to_commit": CLASSMAP_COMMIT,
        "runtime": "ai-edge-litert==2.2.0",
        "what_a_score_is": (
            "the mean over the clip's %d hops of YAMNet's per-class sigmoid on AudioSet's 521 "
            "classes. It is a general-purpose sound-event score from a model trained on YouTube "
            "audio, applied to a 4.0 s 16 kHz clip from a fixed outdoor microphone at night. It "
            "is NOT a species identification, NOT calibrated to this site, and NOT anything a "
            "human has confirmed." % YAMNET_FRAMES),
        "normalisation": (
            "every clip is RMS-normalised to %.1f dBFS before inference, and the pre-"
            "normalisation level is stored per row. Measured on two real nyquist clips: at their "
            "recorded -56.9 and -62.1 dBFS the top class is Silence (0.406, 0.723); normalised, "
            "the same audio returns Animal/Cricket/Speech and Animal/Wild animals/Bird. A "
            "pipeline without this step emits Silence for every clip and exits 0."
            % TARGET_DBFS),
        "rate_policy": (
            "the WAV header rate is asserted within +/-%.0f Hz of %d and REFUSED otherwise. No "
            "resampling anywhere: YAMNet neither validates nor resamples its input, so a wrong "
            "rate degrades silently. mach shipped a boot headed 22624 Hz."
            % (CLIPS.FS_TOLERANCE_HZ, int(CLIPS.FS_NOMINAL_HZ))),
        "score_floor": SCORE_FLOOR,
        "score_floor_note": (
            "a STORAGE bound, not a decision threshold. Classes below it are summarised by "
            "max_unstored_score so the discarded tail is quantified. Nothing downstream may read "
            "%s as an operating point -- none has been validated on this corpus."
            % SCORE_FLOOR),
        "embedding_note": (
            "%d floats, the mean of the same forward pass's per-hop embeddings. Stored because "
            "clips.prune() deletes WAVs at a byte cap and the embedding is then the only "
            "fixed-axis representation of audio the node already destroyed."
            % YAMNET_EMBED_DIM),
        "provenance": "model",
        "human_verified": False,
        "usable_as_training_label": False,
        "why_not_a_training_label": (
            "fitting a scene- or sketch-based model on these targets would measure whether a "
            "20-band descriptor can reconstruct what a full-fidelity model already decided, "
            "which is not correctness. All 35 dama ant models were trained on circular "
            "self-labels; this is the same shape. A trainer needs a human-verified held-out set, "
            "which does not exist yet."),
        "models_refused": {
            "birdnet": ("zero birds across nine real clips, neotropical hypotheses at the "
                        "confidence floor, 1 s of every 4 discarded (48 kHz x 3.0 s windows "
                        "against a 4.0 s clip), and CC BY-NC-SA weights"),
            "panns_cnn14": ("wants 32 kHz; 17% of its filterbank lands on guaranteed zeros at "
                            "16 kHz; measured 14% lower confidence than YAMNet on the same A/B; "
                            "311 ms and 1.5 GB RSS against 12 ms and 82 MB"),
        },
        "species_id_is_out_of_scope": (
            "YAMNet has Bird, Owl, Hoot, Chirp and stops. The goal is event triage: something "
            "happened, roughly what kind."),
    }


# ----------------------------------------------------------------- I/O

def state_dir(root: str) -> str:
    return os.path.join(root, "state")


def heartbeat_path(root: str) -> str:
    return os.path.join(state_dir(root), "tag_heartbeat.json")


def card_path(root: str) -> str:
    return os.path.join(root, "clips", "tag_model_card.json")


def index_census(root: str) -> Dict[str, int]:
    """A SECOND, INDEPENDENT WALK of clips/index.jsonl: lines, distinct keys, rewrites, torn.

    ⚠️THIS EXISTS TO GIVE THE CONSERVATION CHECK A DIFFERENT PROVENANCE FROM THE DISPATCH LOOP.
    `run()` counts what it did while walking `read_index`'s collapsed dict; this counts the file
    itself. `keys` from here and `len(read_index(root))` are two independent decodings of the same
    bytes, and it is their AGREEMENT that is asserted -- an arithmetic identity between numbers
    one loop produced would restate the loop rather than test it.

    `superseded` is COUNTED (a line whose clip_key was already seen), not derived by subtraction.
    prune() appends a second line per pruned clip, so this is the normal case and not an error.
    """
    p = CLIPS.index_path(root)
    out = {"lines": 0, "keys": 0, "superseded": 0, "unparseable": 0}
    if not os.path.exists(p):
        return out
    seen = set()
    with open(p, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out["lines"] += 1
            try:
                k = json.loads(line).get("clip_key")
            except Exception:
                out["unparseable"] += 1
                continue
            if not isinstance(k, str):
                out["unparseable"] += 1
                continue
            if k in seen:
                out["superseded"] += 1
            else:
                seen.add(k)
                out["keys"] += 1
    return out


def _row_day(row: Dict[str, Any]) -> str:
    """The clip's day partition. Taken from the stored path when there is one -- that is where the
    audio actually lives -- and otherwise derived the same way hear.clips does it."""
    p = row.get("path")
    if isinstance(p, str):
        parts = p.split("/")
        if len(parts) >= 3 and parts[0] == "clips":
            return parts[1]
    return CLIPS._day(row.get("ts_utc_s") if row.get("anchored") else None)


def work_order(index: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every indexed clip, OLDEST UTC DAY FIRST.

    ⚠️ORDER IS BY PRUNE RISK. `clips.prune()` deletes whole day directories oldest-first at a byte
    cap, so the oldest un-tagged clip is the one whose audio disappears next. Tagging newest-first
    would spend the run on the clips least likely to be gone by the next one.
    """
    return sorted(index.values(),
                  key=lambda r: (CLIPS._day_order(_row_day(r)),
                                 float(r.get("ts_utc_s") or 0.0), r.get("clip_key") or ""))


def empty_tally() -> Dict[str, Any]:
    return {"index_lines": 0, "index_keys": 0, "census_keys": 0, "unparseable": 0,
            "superseded": 0,
            "tagged": 0, "refused": 0, "already_tagged": 0, "deferred": 0,
            "by_reason": {}, "by_node": {}, "by_node_day_reason": {},
            "silence_top": 0, "scored_any": 0, "dbfs": [], "unstored": []}


def tally(t: Dict[str, Any], node: str, day: str, outcome: str,
          reason: Optional[str] = None, row: Optional[Dict[str, Any]] = None) -> None:
    n = t["by_node"].setdefault(node or "?", {"tagged": 0, "refused": 0, "already_tagged": 0,
                                              "deferred": 0, "silence_top": 0})
    if outcome == "tagged":
        t["tagged"] += 1
        n["tagged"] += 1
        if row is not None:
            scores = row.get("scores") or {}
            if scores:
                t["scored_any"] += 1
            top = max(scores, key=scores.get) if scores else None
            if top == SILENCE_CLASS:
                t["silence_top"] += 1
                n["silence_top"] += 1
            if row.get("pre_norm_dbfs") is not None:
                t["dbfs"].append(float(row["pre_norm_dbfs"]))
            if row.get("max_unstored_score") is not None:
                t["unstored"].append(float(row["max_unstored_score"]))
    elif outcome == "refused":
        t["refused"] += 1
        n["refused"] += 1
        t["by_reason"][reason] = t["by_reason"].get(reason, 0) + 1
        b = "%s|%s|%s" % (node or "?", day or "?", reason)
        t["by_node_day_reason"][b] = t["by_node_day_reason"].get(b, 0) + 1
    elif outcome == "already_tagged":
        t["already_tagged"] += 1
        n["already_tagged"] += 1
    elif outcome == "deferred":
        t["deferred"] += 1
        n["deferred"] += 1
    else:
        raise ValueError("unknown tally outcome %r" % outcome)


def tag_one(tagger: Any, row: Dict[str, Any], root: str, mb: Dict[str, Any],
            floor: float = SCORE_FLOOR, now: Optional[float] = None) -> Dict[str, Any]:
    """One index row -> one tag row, or a refusal. NEVER raises for a data problem.

    ⚠️IT READS ONLY THE FIELDS hear.clips DECLARES: clip_key, node, boot, sample, path, anchored,
    ts_utc_s, t_start/t_end_utc_s, fs_hz, wav_header_fs_hz, record_key, outcome, audio_pruned_at.
    Anything else in the index is hear-drain's business. The test that proves it strips every
    undeclared key and re-runs.
    """
    now = time.time() if now is None else now
    ck = row.get("clip_key")
    if not isinstance(ck, str) or not ck:
        return {"ok": False, "reason": R_INDEX_ROW,
                "detail": "index row carries no clip_key"}
    if row.get("outcome") != "stored" or not row.get("path"):
        return {"ok": False, "reason": R_NO_AUDIO,
                "detail": "index outcome is %r with path %r -- the node destroyed this clip "
                          "before it was fetched, and the row is all that is left of it"
                          % (row.get("outcome"), row.get("path"))}
    path = os.path.join(root, row["path"])
    if not os.path.exists(path):
        pruned = row.get("audio_pruned_at") is not None
        return {"ok": False,
                "reason": R_AUDIO_PRUNED if pruned else R_AUDIO_MISSING,
                "detail": ("clips.prune() deleted this WAV at the byte cap; the index row and any "
                           "tag row are what survive" if pruned else
                           "the index says stored at %r and no file is there, and no prune "
                           "recorded deleting it" % row["path"])}
    try:
        pcm, header_fs = read_wav(path)
    except Exception as exc:
        return {"ok": False, "reason": R_WAV_UNREADABLE,
                "detail": "%s: %s" % (type(exc).__name__, exc)}
    if len(pcm) * 2 + 44 != CLIPS.CLIP_BYTES:
        return {"ok": False, "reason": R_WAV_SAMPLES,
                "detail": "%d samples; a clip is %d (%.1f s pre + %.1f s post at %d Hz)"
                          % (len(pcm), (CLIPS.CLIP_BYTES - 44) // 2, CLIPS.CLIP_PRE_S,
                             CLIPS.CLIP_POST_S, int(CLIPS.FS_NOMINAL_HZ))}
    try:
        assert_rate(header_fs, row.get("fs_hz"))
    except RateRefused as exc:
        return {"ok": False, "reason": R_RATE_REFUSED, "detail": str(exc)}
    try:
        pcm, pre_db = normalise(pcm)
    except ValueError as exc:
        return {"ok": False, "reason": R_DIGITAL_SILENCE, "detail": str(exc)}
    try:
        got = tagger.tag(pcm, floor=floor)
    except Exception as exc:
        return {"ok": False, "reason": R_MODEL_ERROR,
                "detail": "%s: %s" % (type(exc).__name__, exc)}

    out = {
        "tag_schema_version": TAGS.TAG_SCHEMA_VERSION,
        "schema": TAG_SCHEMA,
        "tag_key": TAGS.tag_key(ck, MODEL_NAME, MODEL_VERSION, mb.get("sha256")),
        "clip_key": ck,
        "det_ref": row.get("record_key"),
        "node": row.get("node"),
        "day": _row_day(row),
        "model": mb,
        "claim": claim_block(),
        "scores": got["scores"],
        "max_unstored_score": got["max_unstored_score"],
        "n_classes_scored": got["n_classes_scored"],
        "n_frames": got["n_frames"],
        "embedding": got["embedding"],
        "pre_norm_dbfs": pre_db,
        "wav_header_fs_hz": int(header_fs),
        # ⚠️BOTH RATES, SIDE BY SIDE, ALWAYS. The CSV estimate and the file's own header disagreed
        # by 6,624 Hz for a whole boot; keeping one of them would have made that invisible.
        "csv_fs_hz": row.get("fs_hz"),
        "anchored": bool(row.get("anchored")),
        "window": ({"t_start_utc_s": row.get("t_start_utc_s"),
                    "t_end_utc_s": row.get("t_end_utc_s")}
                   if row.get("anchored") and row.get("t_start_utc_s") is not None else None),
        "sample_window": TAGS.sample_window(row),
        "provenance": "model",
        "created_utc_s": now,
    }
    return {"ok": True, "row": out}


def _write_json_atomic(path: str, obj: Any) -> None:
    """tmp + os.replace. A torn heartbeat is a check that reads nothing and passes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run(root: str, *, model_dir: str, limit: int = DEFAULT_LIMIT,
        deadline_s: float = DEFAULT_DEADLINE_S, floor: float = SCORE_FLOOR,
        write: bool = True, now: Optional[float] = None,
        tagger: Optional[Any] = None, verified: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
    """Tag every indexed clip not already tagged at this model version. Returns the run report.

    ⚠️IT REFUSES TO START WITHOUT VERIFIED WEIGHTS. `WeightsRefused` propagates out of here; there
    is no degraded mode in which the job runs and emits nothing, because that is indistinguishable
    from a corpus with nothing to tag.

    ⚠️NOTHING IS SWALLOWED. Every index KEY lands in exactly one of tagged / refused /
    already_tagged / deferred, every index LINE is either a first sighting, a superseded rewrite
    or unparseable, and both totals are compared against a separate pass over the same bytes.
    `conservation_ok: False` is a hard --check failure.
    """
    now = time.time() if now is None else now
    if verified is None:
        verified = verify_weights(model_dir)
    if tagger is None:
        if not verified["ok"]:
            raise WeightsRefused("; ".join(verified["problems"]))
        mp, cp = weights_paths(model_dir)
        tagger = Tagger(mp, cp)
    mb = model_block(verified)

    t = empty_tally()
    census = index_census(root)                         # independent walk, before any dispatch
    t["index_lines"] = census["lines"]
    t["unparseable"] = census["unparseable"]
    t["superseded"] = census["superseded"]
    t["census_keys"] = census["keys"]
    index = CLIPS.read_index(root)
    t["index_keys"] = len(index)
    held, t["versions_held"] = TAGS.read_resume(root)

    out_rows: List[Dict[str, Any]] = []
    t0 = time.time()
    stop_reason = None
    for row in work_order(index):
        node, day = row.get("node") or "?", _row_day(row)
        ck = row.get("clip_key")
        if isinstance(ck, str) and TAGS.tag_key(ck, MODEL_NAME, MODEL_VERSION,
                                                mb.get("sha256")) in held:
            tally(t, node, day, "already_tagged")
            continue
        if stop_reason is None:
            if len(out_rows) >= limit:
                stop_reason = "limit"
            elif time.time() - t0 >= deadline_s:
                stop_reason = "deadline"
        if stop_reason is not None:
            tally(t, node, day, "deferred")
            continue
        got = tag_one(tagger, row, root, mb, floor=floor, now=now)
        if got["ok"]:
            out_rows.append(got["row"])
            tally(t, node, day, "tagged", row=got["row"])
        else:
            tally(t, node, day, "refused", reason=got["reason"])

    accounted = t["tagged"] + t["refused"] + t["already_tagged"] + t["deferred"]
    t["accounted"] = accounted
    # ⚠️THREE COUNTS, TWO PROVENANCES. `accounted` comes from the dispatch loop, `index_keys` from
    # read_index's dict, `census_keys` from the independent file walk. The first equality tests the
    # loop against the store; the second tests the store against the bytes.
    t["conservation_ok"] = (accounted == t["index_keys"] == t["census_keys"]
                            and t["index_lines"] == (t["census_keys"] + t["superseded"]
                                                     + t["unparseable"]))
    t["stop_reason"] = stop_reason
    t["cap_hit"] = stop_reason is not None
    t["elapsed_s"] = time.time() - t0
    t["model"] = mb
    t["weights_ok"] = bool(verified["ok"])
    t["at"] = now
    t["silence_frac"] = (t["silence_top"] / float(t["tagged"])) if t["tagged"] else None
    t["observation_not_health"] = {
        "level_dbfs": _distribution(t.pop("dbfs")),
        # ⚠️AGGREGATED, NOT ONLY PER ROW. `max_unstored_score` made the discarded tail a number on
        # each row; nothing summed it, so a floor set too high looked exactly like a quiet night.
        "max_unstored_score": _distribution(t.pop("unstored")),
        "silence_top_frac": t["silence_frac"],
        "note": ("the class distribution is NOT an input to any gate. A quiet night is the "
                 "expected result; a gate keyed on 'did anything score high' fires on a correct "
                 "run. silence_top_frac is gated only once a human calibration set has been "
                 "measured -- see docs/acoustic-stack.md, the Phase-3 gate."),
    }

    # `versions_held` was read before this run wrote anything, so this run's own model is added
    # to it. Without that a new weights file is invisible to the gate until the NEXT run.
    if out_rows:
        vk = "%s/%s/%s" % (mb.get("name"), mb.get("version"), mb.get("sha256"))
        t["versions_held"][vk] = t["versions_held"].get(vk, 0) + len(out_rows)

    if write:
        if out_rows:
            TAGS.append_tags(root, out_rows)
        _write_json_atomic(card_path(root), model_card(verified))
        write_heartbeat(root, t, now=now)
    return t


def _distribution(vals: List[float]) -> Dict[str, Any]:
    if not vals:
        return {"n": 0}
    s = sorted(vals)

    def pct(f):
        return s[min(len(s) - 1, int(f * len(s)))]

    return {"n": len(s), "min": s[0], "p10": pct(0.1), "median": pct(0.5), "p90": pct(0.9),
            "max": s[-1]}


def write_heartbeat(root: str, report: Dict[str, Any], now: Optional[float] = None
                    ) -> Dict[str, Any]:
    """Merge this run into the heartbeat's ring, and record which refusal buckets are NEW.

    ⚠️A REASON APPEARING IN A NODE|DAY BUCKET THAT NEVER HAD IT IS AN EVENT, NOT A RATE. The rate
    refusal is a step function -- one boot's fs latch flips a whole node from 0% to 100% -- so a
    percentage threshold reads 0 right up until it reads 100. The first run is exempt, because it
    establishes the census and failing it would train an operator to ignore this gate.

    ⚠️THE RING EXISTS BECAUSE THE CHECK RUNS LESS OFTEN THAN THE TAGGER. A per-run field would be
    several runs stale before the gate read it -- the same trap hear_drain's `unfetched_recent`
    and hear_score's run ring both exist to close.
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
        "index_lines": report.get("index_lines"),
        "index_keys": report.get("index_keys"),
        "census_keys": report.get("census_keys"),
        "superseded": report.get("superseded"),
        "unparseable": report.get("unparseable"),
        "tagged": report.get("tagged"),
        "refused": report.get("refused"),
        "already_tagged": report.get("already_tagged"),
        "deferred": report.get("deferred"),
        "cap_hit": bool(report.get("cap_hit")),
        "stop_reason": report.get("stop_reason"),
        "conservation_ok": bool(report.get("conservation_ok")),
        "weights_ok": bool(report.get("weights_ok")),
        "silence_top": report.get("silence_top"),
        "silence_frac": report.get("silence_frac"),
        # ⚠️`scored_any` IS AN ABSOLUTE SIGNAL AND `silence_frac` IS NOT. A model that returns
        # nothing above the floor for every clip reports silence_frac 0.000 -- the best possible
        # value -- and passed the Phase-3 gate more easily than any real night can. This says
        # whether the model produced OUTPUT AT ALL, which is not a claim about the audio.
        "scored_any": report.get("scored_any"),
        "versions_held": report.get("versions_held") or {},
        "by_reason": report.get("by_reason") or {},
        "by_node": report.get("by_node") or {},
        "new_buckets": new_buckets,
        "first_run": first_run,
        "observation_not_health": report.get("observation_not_health"),
    }
    hb["runs"] = (hb["runs"] + [entry])[-RUN_RING:]
    _write_json_atomic(p, hb)
    return hb


def check_tags(root: str, *, max_silence_frac: float = SILENCE_FRAC_REPORT_ONLY,
               window_s: float = DEFAULT_RUN_WINDOW_S,
               max_stale_s: float = DEFAULT_MAX_STALE_S,
               now: Optional[float] = None) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero when tagging is not flowing, not when the night was quiet.

    ⚠️EVERY GATE HERE CAN ACTUALLY FAIL, AND THE ABSOLUTE ONES COME FIRST. A gate of the form "if
    clips arrived and none were tagged" passes vacuously on an empty read -- point --pool at
    /pool instead of /pool/corpus and index_keys, refusals and conservation are 0, 0 and trivially
    consistent while nothing is being tagged at all. So: no heartbeat fails, no runs fails, a run
    that saw zero index rows fails, unverified weights fail, and a stale heartbeat fails whatever
    the store looks like. hear_drain.check()'s `if not sensors: return 1` is the same shape.

    ⚠️THE SILENCE FRACTION IS REPORT-ONLY UNTIL A HUMAN HAS CALIBRATED IT. At max_silence_frac < 0
    it prints the measured value and says on its own line that it is not gating. Setting it from
    a guess rather than from the measured envelope is the failure this repo names as its own.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(root)
    if not os.path.exists(p):
        return 1, ["no heartbeat at %s -- hear-tag has never completed a run" % p]
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
        lines.append("tagger   STALE     last run %.0f s ago (max %.0f)" % (age, max_stale_s))
        bad += 1
    else:
        lines.append("tagger   ok        last run %.0f s ago" % age)

    if not last.get("weights_ok", True):
        lines.append("weights  REFUSED   the last run could not verify the pinned model sha256")
        bad += 1

    window = [r for r in runs if now - float(r.get("at") or 0.0) <= window_s] or [last]
    lines.append("window   %d run(s) in the last %.0f s" % (len(window), window_s))

    keys = last.get("index_keys") or 0
    if not keys:
        lines.append("index    EMPTY     the newest run read 0 clip rows from %s -- an empty read "
                     "is a failure, not a quiet night (wrong --pool root?)"
                     % CLIPS.index_path(root))
        bad += 1
    else:
        lines.append("index    ok        %d clip(s) in the index" % keys)

    torn = sum(int(r.get("unparseable") or 0) for r in window)
    if torn:
        lines.append("parse    TORN      %d unparseable index line(s) in the window" % torn)
        bad += 1

    if [r for r in window if not r.get("conservation_ok", True)]:
        lines.append("ledger   BROKEN    a run could not account for every index row")
        bad += 1

    newb = sorted({b for r in window for b in (r.get("new_buckets") or [])})
    if newb:
        lines.append("refusals NEW       %d node|day|reason bucket(s) never seen before: %s"
                     % (len(newb), ", ".join(newb[:6]) + (" ..." if len(newb) > 6 else "")))
        bad += 1

    # ⚠️CONDITIONED ON GROWTH, AND ONLY SAFE BECAUSE THE ABSOLUTE GATES ABOVE EXIST. In steady
    # state after catch-up `tagged` is legitimately 0 -- that is a tagger that is up to date.
    grew = (window[-1].get("index_keys") or 0) - (window[0].get("index_keys") or 0)
    fresh = sum(int(r.get("tagged") or 0) for r in window)
    if grew > 0 and fresh == 0:
        lines.append("through  STUCK     the index grew by %d clip(s) across the window and the "
                     "tagger emitted 0 rows" % grew)
        bad += 1
    else:
        lines.append("through  ok        index +%d, %d row(s) emitted across the window"
                     % (grew, fresh))

    deferred = sum(int(r.get("deferred") or 0) for r in window)
    if window[-1].get("cap_hit") and deferred:
        lines.append("cap      BINDING   %d clip(s) deferred in the window (%s) -- the tagger is "
                     "not keeping up with the drain" % (deferred, window[-1].get("stop_reason")))
        bad += 1
    else:
        lines.append("cap      ok        %d clip(s) deferred across the window" % deferred)

    # ⚠️ABSOLUTE, AND BEFORE THE SILENCE LINE. `tagged > 0 and scored_any == 0` says the model
    # emitted no class above the floor for a single clip -- an interpreter that loaded and is fed
    # or read wrong, or a --score-floor set past the top of the distribution. It is not a claim
    # about the class distribution and it can fire today.
    tagged = sum(int(r.get("tagged") or 0) for r in window)
    scored = sum(int(r.get("scored_any") or 0) for r in window)
    measured = any(r.get("scored_any") is not None for r in window)
    if measured and tagged > 0 and scored == 0:
        lines.append("scores   NONE      %d clip(s) tagged and not one scored a single class "
                     "above the floor -- the model produced no output, which is not a quiet "
                     "night" % tagged)
        bad += 1
    elif measured:
        lines.append("scores   ok        %d of %d tagged clip(s) scored at least one class"
                     % (scored, tagged))

    # ⚠️ONE VERSION STRING, ONE WEIGHTS DIGEST. `model_block` calls the sha256 the identity;
    # MODEL_VERSION is a promise a human keeps by hand. Two digests under one version string
    # means the store holds two models' scores that a consumer cannot tell apart.
    shas: Dict[str, set] = {}
    for r in window:
        for vk in (r.get("versions_held") or {}):
            name_ver, _, sha = vk.rpartition("/")
            shas.setdefault(name_ver, set()).add(sha)
    mixed = {k: sorted(v) for k, v in shas.items() if len(v) > 1}
    if mixed:
        lines.append("weights  MIXED     %s -- one version string, several weight files"
                     % json.dumps({k: [x[:12] for x in v] for k, v in mixed.items()},
                                  sort_keys=True))
        bad += 1

    sil = sum(int(r.get("silence_top") or 0) for r in window)
    frac = (sil / float(tagged)) if tagged else None
    if max_silence_frac < 0:
        lines.append("silence  REPORT    %s of tagged clips top-class Silence -- NOT GATED. The "
                     "threshold is set from a measured human calibration set, not from a guess; "
                     "see the Phase-3 gate in docs/acoustic-stack.md"
                     % ("%.3f" % frac if frac is not None else "n/a"))
    elif frac is not None and frac > max_silence_frac:
        lines.append("silence  HIGH      %.3f of tagged clips top-class Silence, over %.3f -- the "
                     "usual cause is normalisation not running" % (frac, max_silence_frac))
        bad += 1
    else:
        lines.append("silence  ok        %s (max %.3f)"
                     % ("%.3f" % frac if frac is not None else "n/a", max_silence_frac))

    reasons: Dict[str, int] = {}
    for r in window:
        for k, v in (r.get("by_reason") or {}).items():
            reasons[k] = reasons.get(k, 0) + int(v)
    lines.append("refusals %s" % (json.dumps(reasons, sort_keys=True) if reasons
                                  else "none in the window"))
    obs = (last.get("observation_not_health") or {}).get("level_dbfs") or {}
    lines.append("levels   %s  (OBSERVATION, not a gate)" % json.dumps(obs, sort_keys=True))
    return (1 if bad else 0), lines


def format_report(t: Dict[str, Any]) -> str:
    out = ["index %d line(s) = %d clip(s) + %d superseded + %d unparseable"
           % (t["index_lines"], t["index_keys"], t["superseded"], t["unparseable"]),
           "tagged %d  refused %d  already-tagged %d  deferred %d  (%s)"
           % (t["tagged"], t["refused"], t["already_tagged"], t["deferred"],
              "cap hit: %s" % t["stop_reason"] if t["cap_hit"] else "no cap hit"),
           "conservation %s (%d + %d + %d + %d == %d clip rows)"
           % ("OK" if t["conservation_ok"] else "BROKEN", t["tagged"], t["refused"],
              t["already_tagged"], t["deferred"], t["index_keys"])]
    for node in sorted(t["by_node"]):
        n = t["by_node"][node]
        out.append("  %-10s tagged %5d  refused %5d  already %5d  deferred %5d  silence-top %d"
                   % (node, n["tagged"], n["refused"], n["already_tagged"], n["deferred"],
                      n["silence_top"]))
    if t["by_reason"]:
        out.append("refusals by reason: " + json.dumps(t["by_reason"], sort_keys=True))
        for b in sorted(t["by_node_day_reason"]):
            out.append("  %-52s %d" % (b, t["by_node_day_reason"][b]))
    out.append("observation (NOT a health input): "
               + json.dumps(t["observation_not_health"]["level_dbfs"], sort_keys=True)
               + "  silence-top frac %s" % t["silence_frac"])
    out.append("⚠️a tag is a MODEL's opinion, not a label: no human has heard these clips, the "
               "scores are not species IDs, and clips/tag_model_card.json says why they must not "
               "be used as training targets.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", default="~/hear-pool",
                    help="pool root; clips/index.jsonl is read, clips/tags.jsonl is appended")
    ap.add_argument("--model-dir", default="~/hear-pool/models/yamnet",
                    help="directory holding %s and %s. ⚠️NOTHING HERE FETCHES THEM: both are "
                         "verified against a pinned sha256 and the run is refused otherwise"
                         % (MODEL_FILE, CLASSMAP_FILE))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="stop after this many clips; the rest are counted as deferred")
    ap.add_argument("--deadline-s", type=float, default=DEFAULT_DEADLINE_S)
    ap.add_argument("--score-floor", type=float, default=SCORE_FLOOR,
                    help="a STORAGE bound, not an operating point; the tail below it is still "
                         "summarised by max_unstored_score")
    ap.add_argument("--verify-weights", action="store_true",
                    help="hash the two artifacts against their pinned digests and exit; 0 when "
                         "both match, 2 otherwise. Tags nothing")
    ap.add_argument("--census", action="store_true",
                    help="tag everything and report, writing nothing")
    ap.add_argument("--check", action="store_true",
                    help="read the heartbeat and exit non-zero if tagging is not flowing")
    ap.add_argument("--max-silence-frac", type=float, default=SILENCE_FRAC_REPORT_ONLY,
                    help="--check fails above this fraction of tagged clips whose top class is "
                         "Silence. Negative (the default) means REPORT ONLY -- set it from a "
                         "measured calibration set, never from a guess")
    ap.add_argument("--window-s", type=float, default=DEFAULT_RUN_WINDOW_S)
    ap.add_argument("--max-stale-s", type=float, default=DEFAULT_MAX_STALE_S)
    ap.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    a = ap.parse_args(argv)

    root = os.path.expanduser(a.pool)

    if a.verify_weights:
        v = verify_weights(a.model_dir)
        if v["ok"]:
            print("weights OK  %s %s  %s %s"
                  % (MODEL_FILE, v["model_sha256"][:16], CLASSMAP_FILE,
                     v["class_map_sha256"][:16]))
            return 0
        for p in v["problems"]:
            print("weights REFUSED: %s" % p, file=sys.stderr)
        return 2

    if a.check:
        code, lines = check_tags(root, max_silence_frac=a.max_silence_frac,
                                 window_s=a.window_s, max_stale_s=a.max_stale_s)
        print("\n".join(lines))
        return code

    try:
        t = run(root, model_dir=a.model_dir, limit=a.limit, deadline_s=a.deadline_s,
                floor=a.score_floor, write=not a.census)
    except WeightsRefused as exc:
        # ⚠️LOUD, AND EXIT 2 RATHER THAN 1, so a monitor can tell "the model is not what it says"
        # apart from "tagging is behind". A tagger that cannot name its weights must not run.
        print("REFUSED: the pinned weights did not verify, so nothing was tagged.\n  %s" % exc,
              file=sys.stderr)
        return 2
    print(json.dumps(t, indent=2, sort_keys=True) if a.json else format_report(t))
    # ⚠️REFUSALS DO NOT FAIL THE RUN. A refused clip is data this tool read and correctly declined
    # to tag; failing on it would make a pruned backlog look like a broken tagger forever. What
    # fails is losing track of a row.
    return 0 if t["conservation_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
