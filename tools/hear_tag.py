#!/usr/bin/env python3
"""Tag the pooled clips with EfficientAT mn10_as, and say what it could NOT tag.

    python3 tools/hear_tag.py --pool ~/hear-pool --model-dir ~/hear-pool/models/mn10_as
    python3 tools/hear_tag.py --pool ~/hear-pool --census          # read-only, writes nothing
    python3 tools/hear_tag.py --model-dir ... --verify-weights     # exit 2 unless both shas match
    python3 tools/hear_tag.py --pool ~/hear-pool --check           # exit 1 if not flowing

WHY THIS EXISTS. hear-drain pulls clips off the cards into `clips/index.jsonl` and
`clips/<day>/<node>/*.wav`. Until this file nothing had ever opened one. 478 clips were destroyed
unheard before collection landed; collecting them and never listening is the same outcome with
more disk used.

⚠️THE MODEL IS EfficientAT mn10_as, NOT YAMNet, AND THE SWAP WAS MADE ON MEASUREMENTS.
AudioSet mAP 0.471 against YAMNet's 0.306, at 4.88M parameters and 0.54 GMACs, MIT-licensed --
which matters on a fleet that also ships an Android APK. YAMNet was refused as a BACKBONE on this
corpus specifically: 361 of its 1024 dimensions hold exactly zero variance and 43.2 % of
directions are pinned by the covariance floor, against the scene corpus's rank 80/80 and 0 %
floored. The hugbot detector built on those embeddings measured mean AUC 0.624 and caught 0 % of
gaussian noise matched to the corpus. Its validation machinery was worth porting; its weights
were not.

⚠️THE ALTERNATIVES WERE REFUSED WITH NUMBERS, NOT PREFERENCES.
  * AST / PaSST / BEATs: 87-90M parameters for ~1 mAP over mn10_as at 4.88M. Self-supervised
    AudioSet models now reach 0.502. The decisive measurement is elsewhere: AudioSet-trained
    embeddings LOSE to bird-trained embeddings on all six bioacoustic datasets tested, bats and
    marine mammals included. Three of this site's four named targets are bioacoustic.
  * BirdNET V2.4: the only thing that answers WHICH bird, 48 kHz native -- and CC BY-NC-SA, which
    plausibly follows every probe onto the APK. Take BirdNET's answers via puc's BirdWeather
    feed; do not take its weights.
  * PANNs/CNN14: wants 32 kHz, and it
    measured 14 % lower confidence than YAMNet on the same A/B for 311 ms and 1.5 GB RSS.
  * Perch 2.0 is NOT a competitor to this file. It is the bioacoustic embedding tier, 1536-d,
    Apache-2.0, and it is still gated (docs/acoustic-stack.md S5). This is the coarse tier.

⚠️IT IS EVENT TRIAGE, NOT SPECIES ID, AND THAT IS A PROPERTY OF THE ONTOLOGY. AudioSet has `Bird`,
`Owl`, `Hoot`, `Chirp` and stops -- 527 classes, structurally incapable of Barred Owl vs Great
Horned Owl no matter how good the backbone gets.

⚠️NORMALISATION IS STILL MANDATORY, AND THE REASON CHANGED SIZE. Under YAMNet it was the
difference between an answer and no answer: at -49 to -62 dBFS every un-normalised clip came back
`Silence`. Measured over the 29 scorable clips of the 2026-09-10 staging set, mn10_as answers
`Silence` on 1 raw and 0 normalised -- it is not defeated by the level. What normalisation buys
is confidence (Speech 0.15 -> 0.29, Insect 0.13 -> 0.27 on individual clips), because the mel is
log-power with a fixed (log + 4.5) / 5 offset so absolute level moves every band. Still
mandatory, no longer load-bearing on its own.

⚠️THE RATE IS RESAMPLED, AND ONLY FROM 48 kHz. mn10_as is 32 kHz native and the nodes acquire at
48 kHz: hear/resample.py decimates 48 -> 32 (L=2 M=3), so every band the model reads is
measurement. A header rate that does not snap to 48 kHz is refused into a counted bucket.

⚠️THE WHOLE SCORE PICTURE IS STORED, NEVER A HARD TOP-1. Every class above SCORE_FLOOR, plus
`max_unstored_score` so the discarded tail is a number rather than an absence. hear/pool.py:721
already states the rule -- the pool's job is to hold everything and say what it holds. This repo
has twice lost data to a filter chosen at ingest, and a threshold picked now, on a corpus that is
mostly quiet and that nobody has listened to, either manufactures positives or deletes the honest
negatives a trainer would need.

⚠️THE 960-d EMBEDDING IS STORED BECAUSE THE AUDIO ROLLS OFF. It is free (same forward pass), and
`clips.prune()` deletes WAVs at a 2 GiB cap; the embedding is what makes later clustering possible
after the clip itself is gone.

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
from hear import resample as RESAMPLE                                     # noqa: E402

TAG_SCHEMA = "hear.clip_tag.v1"

MODEL_NAME = "efficientat-mn10_as"
#: Bumping this is what makes a re-tag a NEW row beside the old one rather than an overwrite.
#: It names the artifact, not the code: change it when the .onnx or the class map changes.
MODEL_VERSION = "onnx-1"

MODEL_FILE = "mn10_as.onnx"
CLASSMAP_FILE = "audioset_class_labels_indices.csv"

#: ⚠️THE ONNX IS BUILT HERE, SO THE CHAIN HAS THREE LINKS AND ALL THREE ARE PINNED. Upstream
#: publishes PyTorch checkpoints, not ONNX; `tools/export_mn10_onnx.py` converts one. The digest
#: below is of THAT script's output, and it is reproducible -- two runs of the export are
#: byte-identical (checked 2026-09-10). Verify the chain, not one end of it:
#:
#:     mn10_as_mAP_471.pt  0bd7dc24...  19,708,753 B   upstream, MIT, GitHub release v0.0.1
#:       -> tools/export_mn10_onnx.py                  in this repo, under review like any code
#:         -> mn10_as.onnx  1b718a05...  24,016,402 B  what the pod loads
MODEL_SHA256 = "1b718a05a68ba8eecf73ce87b5ce74fe266f4228ecaf2c8bdf8b972347dd553d"
MODEL_BYTES = 24016402
UPSTREAM_SHA256 = "0bd7dc2443af498c289a2e739f02ebb515d6aa3fd3ab9db539c86123ae368a4e"
UPSTREAM_BYTES = 19708753
UPSTREAM_URL = ("https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/"
                "mn10_as_mAP_471.pt")
MODEL_URL = "built by tools/export_mn10_onnx.py from " + UPSTREAM_URL

#: AudioSet's 527-class ontology index, from the same repo and release as the weights. YAMNet's
#: 521-class map is NOT interchangeable with it: different length, different order.
CLASSMAP_SHA256 = "cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429"
CLASSMAP_BYTES = 14675
CLASSMAP_URL = ("https://github.com/fschmid56/EfficientAT/raw/v0.0.1/"
                "metadata/class_labels_indices.csv")

#: What the session must actually produce. Checked at load, so a substituted model that happens
#: to hash right for some other reason still cannot be scored as if it were this one.
MODEL_CLASSES = 527
MODEL_EMBED_DIM = 960
#: ⚠️960, AND IT IS NOT 1024. tools/hear_bridge.py has four consumers that hard-code
#: `len(e) == 1024`; three drop a non-matching record SILENTLY and audio_anomaly_score.py returns
#: 0.0 / "not anomalous", which its own docstring calls worse than dropping. YAMNet's 1024 and
#: BirdNET's 1024 collide there; 960 cannot be mistaken for either, and every embedding this file
#: writes carries `dim` so a consumer dispatches on width before anything else.

#: The rate the model's mel frontend was trained at. Clips are 48 kHz and are decimated to it --
#: to it -- see hear/resample.py for which direction manufactures band and how that is labelled.
MODEL_FS_HZ = 32000
#: The model is fully convolutional with global pooling, so a clip goes through in ONE pass and
#: the time average is the network's own. There is no hop count to average over as there was
#: under YAMNet, and inventing a windowing scheme here would be an unmeasured knob.
MODEL_PASSES = 1

#: The same weights under different input policies. Each lane has its own tag file, resume set
#: and heartbeat. `mn10` is the original whole-clip pass and keeps clips/tags.jsonl and
#: state/tag_heartbeat.json as they were. `mn10_pad10` zero-pads the normalised clip to the
#: model's 10 s training length (docs/clip-calibration-2026-09-10.md S4).
#: `perch_v2` runs Perch 2.0 on the GPU and stores embeddings only (see PerchEmbedder).
LANES: Dict[str, Dict[str, Any]] = {
    "mn10": {"model": "mn10", "store": None, "variant": None, "pad_to_s": None},
    "mn10_pad10": {"model": "mn10", "store": "mn10_pad10", "variant": "pad10", "pad_to_s": 10.0},
    "perch_v2": {"model": "perch_v2", "store": "perch_v2", "variant": None, "pad_to_s": None},
    "birdnet_v24": {"model": "birdnet_v24", "store": "birdnet_v24", "variant": None,
                    "pad_to_s": None},
}
DEFAULT_LANE = "mn10"

#: Perch 2.0, the bioacoustic embedding tier of docs/acoustic-stack.md S6.3. Apache-2.0. The
#: Kaggle SavedModel is staged by hand like mn10_as.onnx; every file is pinned, and the model's
#: identity on a row is the digest over all of them.
PERCH_NAME = "perch_v2"
PERCH_VERSION = "kaggle-tf2-v2"
PERCH_ARCHIVE_URL = ("https://www.kaggle.com/api/v1/models/google/bird-vocalization-classifier/"
                     "tensorFlow2/perch_v2/2/download")
PERCH_ARCHIVE_SHA256 = "c04211da33038176efd299519c398b9486d64a2c1e63fafa4df331600552e556"
PERCH_FILES: Tuple[Tuple[str, str, int], ...] = (
    ("saved_model.pb", "d28faa13aa61eb369b9d8d66d483186da65b15d9220bb051c0003553ecae2766", 2701811),
    ("fingerprint.pb", "20274176be0d4f7009c4f5e6b2103519f5b961906218c6bf0f29fd465cf3d88d", 96),
    ("variables/variables.data-00000-of-00001",
     "69571ece6a9229bd339af37f935821b9e3f53869298bd9ad97135a0c87efcea1", 407104092),
    ("variables/variables.index",
     "29c49db4f95727b8c4c22522cf8bb8c77e2eb930ce5b5b43e3a00317d479da1d", 9236),
    ("assets/labels.csv", "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057", 312716),
    ("assets/perch_v2_ebird_classes.csv",
     "861aef71b679d8dcf07c0c71375188c46b680bd808c61a539f254dc21319bcc9", 147890),
)
PERCH_EMBED_DIM = 1536
#: 5.0 s at 32 kHz, the signature's fixed input width; a 48 kHz clip decimates to exactly this.
PERCH_WINDOW = 160000
#: perch_hoplite.zoo.taxonomy_model_tf peak-normalises every window to this before inference.
PERCH_TARGET_PEAK = 0.25

#: BirdNET V2.4, the species tier. ⚠️ITS WEIGHTS ARE CC BY-NC-SA 4.0: staged on the PVC for this
#: site's own non-commercial use, never committed to this repo and never shipped in the APK.
BIRDNET_NAME = "birdnet_v24"
BIRDNET_VERSION = "zenodo-15050749-fp32"
BIRDNET_ARCHIVE_URL = "https://zenodo.org/records/15050749/files/BirdNET_v2.4_tflite.zip"
BIRDNET_ARCHIVE_SHA256 = "31377e128d86fe7b65fa91b206b8804bad1cd934e624bbe6e7c1788170c95e57"
BIRDNET_FILES: Tuple[Tuple[str, str, int], ...] = (
    ("audio-model.tflite",
     "55f3e4055b1a13bfa9a2452731d0d34f6a02d6b775a334362665892794165e4c", 51726412),
    ("meta-model.tflite",
     "33aea6d21cc887d2414e9596d2531a480a3e5f4770c22aa257f217fb757d4653", 29526096),
    ("labels/en_us.txt", "b50b77b7c3dfe40cd637e8cccdca0173a0a4ddee8867b830ff3c1a566f477f16", 259740),
)
BIRDNET_CLASSES = 6522
BIRDNET_FS_HZ = 48000
#: 3.0 s, the model's input. Windows at 0, 1 and 2 s cover the whole 5.0 s clip.
BIRDNET_WINDOW = 144000
BIRDNET_HOP = 48000
#: BirdNET-Analyzer's default cut on the range model's output.
BIRDNET_LOCATION_THRESHOLD = 0.03
#: A species enters the heard summary at this score. OBSERVATION only.
BIRDNET_REPORT = 0.5
#: "lat,lon". From a Secret, never from this public repo.
SITE_ENV = "HEAR_SITE_LATLON"

#: FS_ACQ / FS_NOMINAL. The clip-writer bug stamped the nominal rate over acquisition-rate audio.
HEADER_RATE_FACTOR = 3

#: Coarse groups for the per-run "heard" summary; a clip takes the first match in score order.
#: OBSERVATION only -- nothing gates on it.
COARSE: Tuple[Tuple[str, frozenset], ...] = (
    ("insects", frozenset({"Insect", "Cricket", "Mosquito", "Fly, housefly", "Bee, wasp, etc.",
                           "Buzz"})),
    ("dog", frozenset({"Dog", "Bark", "Howl", "Bow-wow", "Growling", "Whimper (dog)", "Yip",
                       "Canidae, dogs, wolves"})),
    ("bird", frozenset({"Bird", "Bird vocalization, bird call, bird song", "Chirp, tweet", "Owl",
                        "Hoot", "Crow", "Caw", "Squawk", "Pigeon, dove", "Coo", "Fowl"})),
    ("frog", frozenset({"Frog", "Croak"})),
    ("train", frozenset({"Train", "Rail transport", "Railroad car, train wagon",
                         "Clickety-clack", "Train horn", "Train whistle"})),
    ("aircraft", frozenset({"Aircraft", "Fixed-wing aircraft, airplane", "Aircraft engine",
                            "Helicopter", "Jet engine", "Propeller, airscrew"})),
    ("road", frozenset({"Vehicle", "Car", "Truck", "Motor vehicle (road)", "Motorcycle", "Bus",
                        "Accelerating, revving, vroom", "Air horn, truck horn",
                        "Vehicle horn, car horn, honking", "Car passing by", "Tire squeal"})),
    ("weather", frozenset({"Wind", "Wind noise (microphone)", "Rustling leaves", "Rain",
                           "Rain on surface", "Raindrop", "Thunder", "Thunderstorm"})),
    ("machine", frozenset({"Engine", "Hum", "Mains hum", "Mechanical fan", "Air conditioning",
                           "Power tool", "Lawn mower", "Chainsaw", "Hammer", "Drill", "Sawing",
                           "Idling"})),
    ("impulse", frozenset({"Gunshot, gunfire", "Bang", "Explosion", "Boom", "Knock",
                           "Slap, smack", "Thump, thud", "Crack"})),
    ("speech", frozenset({"Speech", "Male speech, man speaking", "Female speech, woman speaking",
                          "Conversation", "Child speech, kid speaking", "Shout", "Yell"})),
    ("music", frozenset({"Music"})),
    ("silence", frozenset({"Silence"})),
)
#: Parents that say nothing on their own; skipped when picking a clip's group.
COARSE_SKIP = frozenset({"Animal", "Domestic animals, pets", "Wild animals",
                         "Outside, urban or manmade", "Outside, rural or natural",
                         "Inside, small room", "Inside, large room or hall"})

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

#: ⚠️THE NORMALISATION CANARY, AND THE ONE IT REPLACED COULD NOT FIRE. `--max-silence-frac` gated
#: the fraction of clips whose top class is `Silence`, which worked under YAMNet because an
#: un-normalised clip came back Silence every time. Measured on the 69-clip human calibration set
#: (docs/clip-calibration-2026-09-10.md), mn10_as gives silence_top_frac 0.0000 normalised and
#: 0.0435 un-normalised -- a threshold resting on THREE clips, whose 95 % interval reaches from
#: 0.9 % to 12 %, so a 2 % gate could pass a completely broken run.
#:
#: The run's MEAN TOP SCORE uses all 69 instead of 3: 0.254 healthy against 0.162 un-normalised,
#: higher on 58 of 69 clips, and at the job's 400-clip --limit the two run-level means are 13.6
#: sigma apart. The floor sits between them, nearer the broken end because a false HIGH is a
#: woken operator and a false ok is a quiet lane.
MEAN_TOP_SCORE_FLOOR = 0.20
#: Negative disables the gate and prints the number instead, the same convention as above.
MEAN_TOP_SCORE_REPORT_ONLY = -1.0
#: ⚠️IT IS CALIBRATED AGAINST EXACTLY ONE FAILURE. The `broken` arm above is normalisation
#: disabled and nothing else. Wrong weights, a corrupt resample or silence on the wire may not
#: move this number at all, so a run that passes is not a run that is known good.
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
    """The clip's header rate is not the acquisition rate."""


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
            (mp, MODEL_SHA256, MODEL_BYTES, "model_sha256", "model_bytes", MODEL_URL),
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

    ⚠️STILL MANDATORY, BUT FOR A SMALLER REASON THAN UNDER YAMNET, AND THE DIFFERENCE IS
    MEASURED. Over the 29 scorable clips of the 2026-09-10 staging set, top-1 was `Silence` on
    1 raw and 0 normalised -- where YAMNet answered `Silence` for every un-normalised clip. What
    normalisation buys mn10_as is confidence, not an answer: Speech 0.15 -> 0.29 and Insect
    0.13 -> 0.27 on individual clips. It stays mandatory because the model's mel is log-power
    with a fixed (log + 4.5) / 5 offset, so absolute level moves every band; it is no longer the
    difference between a result and no result.

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


def normalise_peak(x: "Any", target: float = PERCH_TARGET_PEAK) -> "Any":
    """Scale so the largest |sample| is `target`. Raises on digital silence, like normalise()."""
    import numpy as np
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak <= 0.0:
        raise ValueError("digital silence: every sample is zero, so no gain reaches peak %g"
                         % target)
    return (np.asarray(x, dtype=np.float64) * (target / peak)).astype(np.float32)


def fit_length(x: "Any", n: int, trim: bool = True) -> "Any":
    """Zero-pad to n samples; cut to n as well when `trim`."""
    import numpy as np
    if len(x) < n:
        return np.concatenate([x, np.zeros(n - len(x), dtype=x.dtype)])
    return x[:n] if trim else x


def prepare_rms(pcm: "Any", fs_hz: float, pad_to_s: Optional[float] = None) -> "Any":
    y, _ = normalise(pcm)
    return fit_length(y, int(round(pad_to_s * fs_hz)), trim=False) if pad_to_s else y


def prepare_perch(pcm: "Any", fs_hz: float, pad_to_s: Optional[float] = None) -> "Any":
    return normalise_peak(fit_length(pcm, PERCH_WINDOW), PERCH_TARGET_PEAK)


def prepare_birdnet(pcm: "Any", fs_hz: float, pad_to_s: Optional[float] = None) -> "Any":
    """The recording as it is. BirdNET measured level-invariant on the 69 heard clips (raw and
    peak-normalised gave identical scores), so nothing is scaled; digital silence is still
    refused."""
    import numpy as np
    x = np.asarray(pcm, dtype=np.float32)
    if not len(x) or not np.any(x):
        raise ValueError("digital silence: every sample is zero")
    return x


def to_model_rate(pcm: "Any", header_fs: int, csv_fs: Optional[float] = None,
                  fs_out: float = MODEL_FS_HZ) -> Dict[str, Any]:
    """Resample a clip to the model's rate, or refuse it. -> hear.resample.resample()'s dict.

    The header is authoritative, not the CSV: the CSV value rides beside it on the row.
    """
    try:
        return RESAMPLE.resample(pcm, float(header_fs), float(fs_out))
    except RESAMPLE.RateRefused as e:
        raise RateRefused("%s The dets CSV said %s Hz." % (e, csv_fs))


def settle_rate(n_samples: int, header_fs: int) -> Tuple[float, Optional[Dict[str, Any]]]:
    """(rate to read the body at, recovery record or None). Raises RateRefused.

    A header that snaps to 48 kHz is used as it stands. The one recovery allowed is the known lie,
    the nominal rate stamped over 48 kHz audio: header x HEADER_RATE_FACTOR must snap to 48 kHz
    AND the body must be exactly one clip at that rate while it is not one at the stated rate.
    Everything else stays refused, mach's 22,624 Hz boots and the 4.0 s 16 kHz-era clips included.
    """
    try:
        return RESAMPLE.snap(header_fs), None
    except RESAMPLE.RateRefused as exc:
        stated = str(exc)

    def one_clip(fs: float) -> bool:
        return abs(n_samples / fs - CLIPS.CLIP_TOTAL_S) <= CLIPS.CLIP_TOTAL_S * CLIPS.CLIP_DUR_TOL

    if not header_fs or header_fs <= 0:
        raise RateRefused(stated)
    try:
        fs = RESAMPLE.snap(header_fs * HEADER_RATE_FACTOR)
    except RESAMPLE.RateRefused:
        raise RateRefused(stated)
    if one_clip(fs) and not one_clip(header_fs):
        return fs, {"header_fs_hz": int(header_fs), "factor": HEADER_RATE_FACTOR, "fs_hz": fs}
    raise RateRefused("%s x%d snaps to %g Hz, but %d samples at that rate is %.3f s, not one "
                      "%.1f s clip." % (stated, HEADER_RATE_FACTOR, fs, n_samples,
                                        n_samples / fs, CLIPS.CLIP_TOTAL_S))


def coarse(scores: Dict[str, float]) -> str:
    """The coarse group of the top class that is not a bare parent (COARSE_SKIP). That class
    decides: an unmapped one gives "other" rather than falling through to a weaker class."""
    for name, _s in sorted(scores.items(), key=lambda kv: -kv[1]):
        if name in COARSE_SKIP:
            continue
        for group, names in COARSE:
            if name in names:
                return group
        return "other"
    return "none"


def format_heard(heard: Dict[str, Dict[str, int]]) -> str:
    parts = []
    for node in sorted(heard):
        groups = sorted(heard[node].items(), key=lambda kv: (-kv[1], kv[0]))
        parts.append("%s %s" % (node, " ".join("%s %d" % g for g in groups)))
    return " | ".join(parts) if parts else "nothing tagged"


class Tagger:
    """EfficientAT mn10_as under onnxruntime. One session, reused across clips.

    ⚠️mn10_as REPLACED YAMNet ON THE MEASUREMENT, NOT ON NOVELTY. AudioSet mAP 0.471 against
    YAMNet's 0.306 at 4.88M parameters (MIT, so no licence question follows it onto a fleet that
    also ships an APK). YAMNet was additionally refused as a BACKBONE: 361 of its 1024 dimensions
    hold exactly zero variance on this corpus and 43.2 % of directions are pinned by the
    covariance floor, against the scene corpus's 80/80 and 0 % (hear/validate.py). The hugbot
    detector built on it measured mean AUC 0.624 and 0 % detection on matched gaussian noise.
    Its validation machinery was worth keeping; the weights were not.

    ⚠️THE MEL FRONTEND IS INSIDE THE GRAPH. tools/export_mn10_onnx.py bakes EfficientAT's mel
    filterbank in as a constant and replaces torch.stft with a DFT conv1d, so this pod needs
    onnxruntime and numpy -- no torch, no torchaudio, no librosa. The PVC is the binding
    constraint on this cluster, which is the whole reason. The baked frontend reproduces
    AugmentMelSTFT to 8.4e-05 max abs error, and the exported graph reproduces PyTorch to
    7.6e-06 on logits and 1.0e-06 on embeddings (both checked on real-shaped audio, 2026-09-10).

    ⚠️THE OUTPUTS ARE IDENTIFIED BY THEIR LAST DIMENSION, NOT BY NAME OR POSITION. 527 is the
    class count and 960 the embedding width; each is required to appear exactly once, so a
    graph that hashes right for some other reason still cannot be scored as if it were this one.

    ⚠️LOGITS, NOT PROBABILITIES. The graph ends at the classifier's linear layer. AudioSet is
    multi-label, so the score is a per-class SIGMOID and the 527 scores do not sum to 1 -- a
    softmax here would invent a competition between Bird and Wind that the model never ran.
    """

    def __init__(self, model_path: str, class_map_path: str):
        import numpy as np
        import onnxruntime as ort                                 # lazy: absent in the test env
        self.class_names = load_class_map(class_map_path)
        if len(self.class_names) != MODEL_CLASSES:
            raise WeightsRefused("class map holds %d names, expected %d. YAMNet's 521-class map "
                                 "is not interchangeable with AudioSet's 527-class index"
                                 % (len(self.class_names), MODEL_CLASSES))
        opts = ort.SessionOptions()
        # One clip at a time on a shared node; letting ORT fan out across every core would
        # contend with whatever else the cluster is running for no wall-clock that matters at
        # ~20 events/hour.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._s = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        ins = self._s.get_inputs()
        if len(ins) != 1:
            raise WeightsRefused("the graph takes %d inputs, expected 1 waveform" % len(ins))
        self._in = ins[0].name
        widths = [int(o.shape[-1]) if isinstance(o.shape[-1], int) else -1
                  for o in self._s.get_outputs()]
        for want in (MODEL_CLASSES, MODEL_EMBED_DIM):
            if widths.count(want) != 1:
                raise WeightsRefused(
                    "the graph exposes output widths %r; exactly one tensor of width %d is "
                    "required and this is not the mn10_as export" % (widths, want))
        names = [o.name for o in self._s.get_outputs()]
        self._logit_o = names[widths.index(MODEL_CLASSES)]
        self._embed_o = names[widths.index(MODEL_EMBED_DIM)]
        self._np = np

    def tag(self, pcm: "Any", floor: float = SCORE_FLOOR) -> Dict[str, Any]:
        """-> {"scores", "max_unstored_score", "n_classes_scored", "n_passes", "embedding", ...}.

        ⚠️THE CLIP GOES THROUGH WHOLE, ONCE. mn10_as is fully convolutional and ends in a global
        pool, so the time average is the network's own rather than something this file computes.
        YAMNet needed a mean over 8 fixed hops; imposing a window scheme on a model that does not
        need one would be an unmeasured knob, and this repo's standard is that a knob is derived
        from a measured distribution or it does not exist.

        ⚠️THE AUDIO MUST ALREADY BE AT MODEL_FS_HZ. `to_model_rate` is the only way there, and it
        is what refuses a rate nobody configured.
        """
        np = self._np
        x = np.asarray(pcm, dtype=np.float32).reshape(1, -1)
        logits, embed = None, None
        out = self._s.run([self._logit_o, self._embed_o], {self._in: x})
        logits = np.asarray(out[0], dtype=np.float64).reshape(-1)
        embed = np.asarray(out[1], dtype=np.float64).reshape(-1)
        s = 1.0 / (1.0 + np.exp(-logits))
        keep = {self.class_names[i]: float(s[i]) for i in range(len(s)) if s[i] >= floor}
        dropped = [float(v) for v in s if v < floor]
        return {"scores": keep,
                # ⚠️THE DISCARDED TAIL IS A NUMBER, NOT AN ABSENCE. Without it a floor that is too
                # high and a clip that genuinely scored nothing are the same empty dict.
                "max_unstored_score": max(dropped) if dropped else 0.0,
                "n_classes_scored": int(len(s)),
                "n_passes": MODEL_PASSES,
                "embedding": [float(v) for v in embed],
                # ⚠️THE WIDTH TRAVELS WITH THE VECTOR. hear_bridge's consumers hard-code 1024 and
                # drop anything else in silence; a consumer of these rows dispatches on `dim`.
                "embedding_dim": int(len(embed))}


class PerchEmbedder:
    """Perch 2.0 under TensorFlow on the GPU. Embeddings only; the label head is not stored.

    ⚠️THE HEAD IS DROPPED ON A MEASUREMENT. On the 69 human-heard clips its top-1 agreed 11 times
    (peak-normalised; 6 with the insect head excluded, as S6.3 requires), against 30 for the
    padded mn10 lane, and it put owls on top of clips the listener called empty. The 1536-d
    embedding is the payload S6.4 names; a classifier over it needs labels this site does not
    have yet.

    ⚠️THE KAGGLE EXPORT IS CUDA-ONLY. On a CPU it refuses at the first call ("platform CPU is not
    among the platforms required: [CUDA]"), so this refuses at load instead of after staging.
    """

    def __init__(self, model_dir: str):
        import numpy as np
        import tensorflow as tf                                   # lazy: absent in the test env
        gpus = tf.config.list_physical_devices("GPU")
        if not gpus:
            raise WeightsRefused("no GPU is visible, and the perch_v2 export runs only on CUDA. "
                                 "Pin the pod to the 2080 Ti (CUDA_VISIBLE_DEVICES=0)")
        for g in gpus:
            tf.config.experimental.set_memory_growth(g, True)
        sig = tf.saved_model.load(os.path.expanduser(model_dir)).signatures["serving_default"]
        emb = sig.structured_outputs.get("embedding")
        if emb is None or int(emb.shape[-1]) != PERCH_EMBED_DIM:
            raise WeightsRefused("the SavedModel has no %d-wide `embedding` output; outputs are %r"
                                 % (PERCH_EMBED_DIM, sorted(sig.structured_outputs)))
        inputs = sig.structured_input_signature[1]
        if len(inputs) != 1:
            raise WeightsRefused("the SavedModel signature takes %d inputs %r, expected one "
                                 "waveform" % (len(inputs), sorted(inputs)))
        (self._in, spec), = inputs.items()
        if spec.shape.rank != 2 or spec.shape[-1] not in (None, PERCH_WINDOW):
            raise WeightsRefused("input %r is %s, expected (N, %d)"
                                 % (self._in, spec.shape, PERCH_WINDOW))
        self._f, self._tf, self._np = sig, tf, np

    def tag(self, pcm: "Any", floor: float = SCORE_FLOOR) -> Dict[str, Any]:
        np, tf = self._np, self._tf
        x = np.asarray(pcm, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != PERCH_WINDOW:
            raise ValueError("perch_v2 takes exactly %d samples, got %d" % (PERCH_WINDOW, x.shape[1]))
        e = self._f(**{self._in: tf.constant(x)})["embedding"].numpy().reshape(-1)
        return {"scores": None, "max_unstored_score": None, "n_classes_scored": 0,
                "n_passes": 1, "embedding": [float(v) for v in e], "embedding_dim": int(len(e))}


def verify_perch(model_dir: str) -> Dict[str, Any]:
    return verify_pinned(model_dir, PERCH_FILES, PERCH_ARCHIVE_URL, PERCH_ARCHIVE_SHA256)


def verify_birdnet(model_dir: str) -> Dict[str, Any]:
    return verify_pinned(model_dir, BIRDNET_FILES, BIRDNET_ARCHIVE_URL, BIRDNET_ARCHIVE_SHA256)


def verify_pinned(model_dir: str, files: Tuple[Tuple[str, str, int], ...], archive_url: str,
                  archive_sha256: str) -> Dict[str, Any]:
    """Every pinned file present, right size, right sha256. Never raises. The model's identity
    (`model_sha256`) is the digest over the pinned list, set only when all of it verified."""
    d = os.path.expanduser(model_dir)
    out: Dict[str, Any] = {"ok": False, "problems": [], "files": {}, "model_sha256": None,
                           "model_bytes": 0}
    for rel, want_sha, want_n in files:
        p = os.path.join(d, rel)
        if not os.path.exists(p):
            out["problems"].append("%s is absent. Stage the archive from %s (sha256 %s)"
                                   % (p, archive_url, archive_sha256))
            continue
        try:
            n = os.path.getsize(p)
            got = sha256_file(p) if n == want_n else None
        except OSError as exc:
            out["problems"].append("%s could not be read: %s" % (p, exc))
            continue
        if n != want_n:
            out["problems"].append("%s is %d B, expected %d B" % (p, n, want_n))
            continue
        out["files"][rel] = got
        out["model_bytes"] += n
        if got != want_sha:
            out["problems"].append("%s sha256 is %s, expected %s" % (p, got, want_sha))
    out["ok"] = not out["problems"]
    if out["ok"]:
        h = hashlib.sha256()
        for rel, sha, _n in files:
            h.update(("%s  %s\n" % (sha, rel)).encode())
        out["model_sha256"] = h.hexdigest()
    return out


def perch_block(verified: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": PERCH_NAME, "version": PERCH_VERSION, "file": "saved_model",
            "sha256": verified.get("model_sha256"), "archive_sha256": PERCH_ARCHIVE_SHA256,
            "runtime": "tensorflow", "input_fs_hz": MODEL_FS_HZ, "input_samples": PERCH_WINDOW,
            "target_peak": PERCH_TARGET_PEAK, "embed_dim": PERCH_EMBED_DIM, "n_classes": 0,
            "head_stored": False, "card": "tag_model_card-perch_v2.json"}


def perch_card(verified: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model_name": PERCH_NAME,
        "model_version": PERCH_VERSION,
        "model_sha256": verified.get("model_sha256"),
        "model_bytes": verified.get("model_bytes"),
        "files": verified.get("files"),
        "archive_url": PERCH_ARCHIVE_URL,
        "archive_sha256": PERCH_ARCHIVE_SHA256,
        "licence": "Apache-2.0",
        "runtime": "tensorflow 2.20 on CUDA; the export refuses a CPU",
        "what_a_row_is": (
            "a %d-d embedding of one 5.0 s clip, decimated 48 -> 32 kHz and peak-normalised to "
            "%g as perch_hoplite does. There are no scores: the label head is not stored."
            % (PERCH_EMBED_DIM, PERCH_TARGET_PEAK)),
        "why_no_head": (
            "on the 69 human-heard clips of docs/clip-calibration-2026-09-10.md the head's top-1 "
            "agreed 11/69 (6/69 with the insect head excluded, which acoustic-stack S6.3 "
            "requires), against 30/69 for mn10 padded to 10 s, and it ranked owls first on clips "
            "the listener called empty."),
        "provenance": "model",
        "human_verified": False,
        "usable_as_training_label": False,
    }


def site_latlon() -> Tuple[float, float]:
    """(lat, lon) from SITE_ENV, rounded to 0.1 deg, which is all a range model needs."""
    raw = os.environ.get(SITE_ENV, "")
    try:
        lat, lon = (float(v) for v in raw.split(","))
    except ValueError:
        raise WeightsRefused("%s is %r. BirdNET needs 'lat,lon' for its range filter; without one "
                             "it names birds from other continents" % (SITE_ENV, raw))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise WeightsRefused("%s=%r is not a latitude,longitude" % (SITE_ENV, raw))
    return round(lat, 1), round(lon, 1)


def birdnet_week(ts_utc_s: Optional[float]) -> int:
    """BirdNET's 48-week year: four per month, days 1-7 -> 1 ... 22-31 -> 4. -1 is any week."""
    if ts_utc_s is None:
        return -1
    t = time.gmtime(float(ts_utc_s))
    return (t.tm_mon - 1) * 4 + min(4, (t.tm_mday - 1) // 7 + 1)


def birdnet_group(scores: Dict[str, float]) -> str:
    """The top label's common name when it reaches BIRDNET_REPORT, else "below 0.5"."""
    if not scores:
        return "none"
    top = max(scores, key=scores.get)
    return top.split("_")[-1] if scores[top] >= BIRDNET_REPORT else "below %g" % BIRDNET_REPORT


class BirdNETTagger:
    """BirdNET V2.4 under LiteRT on the CPU, with its range model applied per clip week.

    ⚠️THE RANGE FILTER IS NOT OPTIONAL. The first run without it put Eurasian Magpie, Indian
    Scops-Owl and Spotted Crake at the top of Pennsylvania clips. The site comes from SITE_ENV and
    the tagger refuses to load without it; a clip with no UTC anchor is filtered for any week.
    Non-species classes ("Dog_Dog", "Engine_Engine", ...) are not ranged and always kept.
    """

    wants_time = True

    def __init__(self, model_dir: str):
        import numpy as np
        from ai_edge_litert.interpreter import Interpreter           # lazy: absent in tests
        d = os.path.expanduser(model_dir)
        self.lat, self.lon = site_latlon()
        with open(os.path.join(d, "labels/en_us.txt"), encoding="utf-8") as fh:
            self.labels = [l.strip() for l in fh if l.strip()]
        if len(self.labels) != BIRDNET_CLASSES:
            raise WeightsRefused("labels/en_us.txt holds %d names, expected %d"
                                 % (len(self.labels), BIRDNET_CLASSES))
        self._a = Interpreter(model_path=os.path.join(d, "audio-model.tflite"), num_threads=1)
        self._a.allocate_tensors()
        self._ai, self._ao = self._a.get_input_details()[0], self._a.get_output_details()[0]
        self._m = Interpreter(model_path=os.path.join(d, "meta-model.tflite"), num_threads=1)
        self._m.allocate_tensors()
        self._mi, self._mo = self._m.get_input_details()[0], self._m.get_output_details()[0]
        for what, got, want in (("audio input", self._ai["shape"], [1, BIRDNET_WINDOW]),
                                ("audio output", self._ao["shape"], [1, BIRDNET_CLASSES]),
                                ("range input", self._mi["shape"], [1, 3]),
                                ("range output", self._mo["shape"], [1, BIRDNET_CLASSES])):
            if [int(v) for v in got] != want:
                raise WeightsRefused("BirdNET %s is %s, expected %s" % (what, list(got), want))
        self._always = np.array([l.split("_")[0] == l.split("_")[-1] for l in self.labels])
        self._np, self._keep = np, {}

    def _in_range(self, week: int) -> "Any":
        if week not in self._keep:
            np = self._np
            self._m.set_tensor(self._mi["index"],
                               np.array([[self.lat, self.lon, week]], dtype=np.float32))
            self._m.invoke()
            loc = self._m.get_tensor(self._mo["index"])[0]
            self._keep[week] = (loc >= BIRDNET_LOCATION_THRESHOLD) | self._always
        return self._keep[week]

    def tag(self, pcm: "Any", floor: float = SCORE_FLOOR, week: int = -1) -> Dict[str, Any]:
        np = self._np
        x = np.asarray(pcm, dtype=np.float32)
        starts = list(range(0, max(1, len(x) - BIRDNET_WINDOW + 1), BIRDNET_HOP)) or [0]
        probs = []
        for s in starts:
            w = fit_length(x[s:s + BIRDNET_WINDOW], BIRDNET_WINDOW)
            self._a.set_tensor(self._ai["index"], w[None, :])
            self._a.invoke()
            z = self._a.get_tensor(self._ao["index"])[0].astype(np.float64)
            probs.append(1.0 / (1.0 + np.exp(-np.clip(z, -15.0, 15.0))))
        p = np.max(probs, axis=0)
        keep = self._in_range(week)
        scores = {self.labels[i]: float(p[i]) for i in np.flatnonzero(keep) if p[i] >= floor}
        below = p[keep & (p < floor)]
        return {"scores": scores,
                "max_unstored_score": float(below.max()) if below.size else 0.0,
                "n_classes_scored": int(keep.sum()), "n_passes": len(starts),
                "embedding": None, "embedding_dim": None,
                "extra": {"location_filter": {"lat": self.lat, "lon": self.lon, "week": week,
                                              "threshold": BIRDNET_LOCATION_THRESHOLD,
                                              "species_kept": int(keep.sum())},
                          "max_out_of_range_score": float(p[~keep].max()) if (~keep).any()
                          else 0.0}}


def birdnet_block(verified: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": BIRDNET_NAME, "version": BIRDNET_VERSION, "file": "audio-model.tflite",
            "sha256": verified.get("model_sha256"), "archive_sha256": BIRDNET_ARCHIVE_SHA256,
            "runtime": "ai-edge-litert", "input_fs_hz": BIRDNET_FS_HZ,
            "window_samples": BIRDNET_WINDOW, "hop_samples": BIRDNET_HOP,
            "n_classes": BIRDNET_CLASSES, "location_threshold": BIRDNET_LOCATION_THRESHOLD,
            "score_floor": SCORE_FLOOR, "licence": "CC BY-NC-SA 4.0",
            "card": "tag_model_card-birdnet_v24.json"}


def birdnet_card(verified: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model_name": BIRDNET_NAME,
        "model_version": BIRDNET_VERSION,
        "model_sha256": verified.get("model_sha256"),
        "model_bytes": verified.get("model_bytes"),
        "files": verified.get("files"),
        "archive_url": BIRDNET_ARCHIVE_URL,
        "archive_sha256": BIRDNET_ARCHIVE_SHA256,
        "licence": ("CC BY-NC-SA 4.0 (the weights). Run server-side for this site's own "
                    "non-commercial use; never committed to the repo, never shipped in the APK."),
        "decision": ("docs/acoustic-stack.md S6.3 refused BirdNET's weights on licence and took "
                     "its answers through puc's BirdWeather feed instead. The operator asked for "
                     "it to run beside EfficientAT and Perch on 2026-09-11, with the weights kept "
                     "off every distributed artifact."),
        "what_a_score_is": (
            "the per-class sigmoid of BirdNET V2.4's logits (clipped to +-15), maximised over "
            "three 3.0 s windows at 0, 1 and 2 s of the 48 kHz clip. Only classes the range model "
            "keeps for the site and the clip's week (threshold %g) are stored; the largest score "
            "it removed is `max_out_of_range_score`. It is a species HYPOTHESIS, not confirmed by "
            "any human." % BIRDNET_LOCATION_THRESHOLD),
        "measured": (
            "on the 69 human-heard clips (evenings, insects and dogs): no bird at >= 0.25 with "
            "the range filter on; Dog >= 0.25 on 8 of 15 dog clips. Without the filter it ranked "
            "Eurasian Magpie, Indian Scops-Owl and Spotted Crake first. Raw and peak-normalised "
            "input gave identical scores."),
        "provenance": "model",
        "human_verified": False,
        "usable_as_training_label": False,
    }


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

    ⚠️THE sha256 IS THE IDENTITY. "mn10_as" names a checkpoint family -- eleven assets in one
    upstream release differ only in mel bins and hop and every one of them is called mn10_as --
    plus whatever an export script makes of them. The digest is what separates the artifact that
    produced these numbers from the next thing dropped in the same directory.
    """
    return {"name": MODEL_NAME, "version": MODEL_VERSION,
            "file": MODEL_FILE, "sha256": verified.get("model_sha256"),
            "class_map_file": CLASSMAP_FILE, "class_map_sha256": verified.get("class_map_sha256"),
            "runtime": "onnxruntime", "input_fs_hz": MODEL_FS_HZ,
            "n_classes": MODEL_CLASSES, "embed_dim": MODEL_EMBED_DIM,
            "target_dbfs": TARGET_DBFS, "score_floor": SCORE_FLOOR,
            "card": "tag_model_card.json"}


def claim_block(species: bool = False) -> Dict[str, bool]:
    """The five things a reader must not miss, as booleans, on every row.

    A field named `scores` next to a field named `node` will be read as "what was heard". Each of
    these says what it is not, in a form a query can filter on rather than prose in a card.
    `species` is True for a model whose classes are species (BirdNET): a hypothesis, still not
    human-verified.
    """
    return {"is_species_id": bool(species),
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
        "model_source": MODEL_URL,
        "upstream_sha256": UPSTREAM_SHA256,
        "upstream_bytes": UPSTREAM_BYTES,
        "upstream_url": UPSTREAM_URL,
        "upstream_licence": "MIT",
        "export_script": "tools/export_mn10_onnx.py",
        "class_map_sha256": verified.get("class_map_sha256"),
        "class_map_bytes": verified.get("class_map_bytes"),
        "class_map_source": CLASSMAP_URL,
        "runtime": "onnxruntime==1.27.0",
        "what_a_score_is": (
            "one forward pass of EfficientAT mn10_as over the whole clip, read as a per-class "
            "SIGMOID over AudioSet's %d classes -- multi-label, so they do not sum to 1. The "
            "time average is the network's own global pool, not a mean this pipeline computes. "
            "It is a general-purpose sound-event score from a model distilled from transformers "
            "trained on YouTube audio, applied to a 5.0 s clip from a fixed outdoor microphone. "
            "It is NOT a species identification, NOT calibrated to this site, and NOT "
            "anything a human has confirmed." % MODEL_CLASSES),
        "why_this_model": (
            "AudioSet mAP 0.471 against YAMNet's 0.306 at 4.88M parameters, MIT-licensed. "
            "AST/PaSST/BEATs were refused at 87-90M parameters for ~1 mAP, and self-supervised "
            "AudioSet models reach 0.502 -- but the decisive measurement is that AudioSet-trained "
            "embeddings LOSE to bird-trained embeddings on all six bioacoustic datasets tested, "
            "and three of the four named targets here are bioacoustic. This model is the coarse "
            "triage tier; the bioacoustic tier is Perch 2.0 and it is still gated (docs/"
            "acoustic-stack.md S5)."),
        "normalisation": (
            "every clip is RMS-normalised to %.1f dBFS before inference, and the pre-"
            "normalisation level is stored per row. Measured on two real nyquist clips: at their "
            "recorded -56.9 and -62.1 dBFS the top class is Silence (0.406, 0.723); normalised, "
            "the same audio returns Animal/Cricket/Speech and Animal/Wild animals/Bird. A "
            "pipeline without this step emits Silence for every clip and exits 0."
            % TARGET_DBFS),
        "rate_policy": (
            "the WAV header rate must snap to the 48 kHz acquisition rate (0.5 %%); the clip "
            "is decimated to %d Hz. Any other rate is refused, not stretched." % MODEL_FS_HZ),
        "score_floor": SCORE_FLOOR,
        "score_floor_note": (
            "a STORAGE bound, not a decision threshold. Classes below it are summarised by "
            "max_unstored_score so the discarded tail is quantified. Nothing downstream may read "
            "%s as an operating point -- none has been validated on this corpus."
            % SCORE_FLOOR),
        "embedding_note": (
            "%d floats from the same forward pass's global pool. NOT 1024: four consumers in "
            "tools/hear_bridge.py hard-code that width and three drop a mismatch silently, so "
            "YAMNet's 1024 and BirdNET's 1024 are mutually confusable and 960 is not. Every row "
            "carries embedding_dim so a consumer dispatches on width first. Stored because "
            "clips.prune() deletes WAVs at a byte cap and the embedding is then the only "
            "fixed-axis representation of audio the node already destroyed."
            % MODEL_EMBED_DIM),
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
            "panns_cnn14": ("wants 32 kHz; measured 14% lower confidence than YAMNet on the same A/B; "
                            "311 ms and 1.5 GB RSS against 12 ms and 82 MB"),
        },
        "species_id_is_out_of_scope": (
            "YAMNet has Bird, Owl, Hoot, Chirp and stops. The goal is event triage: something "
            "happened, roughly what kind."),
    }


#: model -> how it is verified, loaded, identified on a row, described, and fed.
MODELS: Dict[str, Dict[str, Any]] = {
    "mn10": {"verify": verify_weights, "load": lambda d: Tagger(*weights_paths(d)),
             "block": model_block, "card": model_card, "card_file": "tag_model_card.json",
             "prepare": prepare_rms, "embed_only": False, "fs_hz": MODEL_FS_HZ,
             "group": coarse, "species": False},
    "perch_v2": {"verify": verify_perch, "load": PerchEmbedder, "block": perch_block,
                 "card": perch_card, "card_file": "tag_model_card-perch_v2.json",
                 "prepare": prepare_perch, "embed_only": True, "fs_hz": MODEL_FS_HZ,
                 "group": None, "species": False},
    "birdnet_v24": {"verify": verify_birdnet, "load": BirdNETTagger, "block": birdnet_block,
                    "card": birdnet_card, "card_file": "tag_model_card-birdnet_v24.json",
                    "prepare": prepare_birdnet, "embed_only": False, "fs_hz": BIRDNET_FS_HZ,
                    "group": birdnet_group, "species": True},
}


# ----------------------------------------------------------------- I/O

def state_dir(root: str) -> str:
    return os.path.join(root, "state")


def heartbeat_path(root: str, lane: str = DEFAULT_LANE) -> str:
    name = "tag_heartbeat.json" if lane == DEFAULT_LANE else "tag_heartbeat-%s.json" % lane
    return os.path.join(state_dir(root), name)


def card_path(root: str, name: str = "tag_model_card.json") -> str:
    return os.path.join(root, "clips", name)


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
            "silence_top": 0, "scored_any": 0, "dbfs": [], "unstored": [], "top_scores": [],
            "heard": {}}


def tally(t: Dict[str, Any], node: str, day: str, outcome: str,
          reason: Optional[str] = None, row: Optional[Dict[str, Any]] = None,
          group: Any = coarse) -> None:
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
            if top is not None:
                t["top_scores"].append(float(scores[top]))
            if row.get("scores") is not None and group is not None:
                h = t["heard"].setdefault(node or "?", {})
                g = group(scores)
                h[g] = h.get(g, 0) + 1
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
            floor: float = SCORE_FLOOR, now: Optional[float] = None,
            lane: str = DEFAULT_LANE) -> Dict[str, Any]:
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
    # The rate is settled before the length, because the length is measured in it.
    try:
        fs_read, recovered = settle_rate(len(pcm), header_fs)
    except RateRefused as exc:
        return {"ok": False, "reason": R_RATE_REFUSED,
                "detail": "%s The dets CSV said %s Hz." % (exc, row.get("fs_hz"))}
    dur_s = len(pcm) / float(fs_read)
    if abs(dur_s - CLIPS.CLIP_TOTAL_S) > CLIPS.CLIP_TOTAL_S * CLIPS.CLIP_DUR_TOL:
        return {"ok": False, "reason": R_WAV_SAMPLES,
                "detail": "%d samples at %g Hz is %.3f s; a clip is %.1f s (%.1f s pre, %.1f s post)"
                          % (len(pcm), fs_read, dur_s, CLIPS.CLIP_TOTAL_S, CLIPS.CLIP_PRE_S,
                             CLIPS.CLIP_POST_S)}
    try:
        rs = to_model_rate(pcm, fs_read, row.get("fs_hz"),
                           MODELS[LANES[lane]["model"]]["fs_hz"])
    except RateRefused as exc:
        return {"ok": False, "reason": R_RATE_REFUSED, "detail": str(exc)}
    # The level recorded is the RECORDING's, taken before the 48 -> 32 kHz decimation removes the
    # band above 16 kHz (measured: -1.8 dB on wideband noise). Normalisation runs after it, so the
    # model sees the target level regardless.
    pre_db = dbfs(pcm)
    pad_to_s = LANES[lane]["pad_to_s"]
    try:
        pcm = MODELS[LANES[lane]["model"]]["prepare"](rs["pcm"], rs["fs_hz"], pad_to_s)
    except ValueError as exc:
        return {"ok": False, "reason": R_DIGITAL_SILENCE, "detail": str(exc)}
    try:
        when = ({"week": birdnet_week(row.get("ts_utc_s") if row.get("anchored") else None)}
                if getattr(tagger, "wants_time", False) else {})
        got = tagger.tag(pcm, floor=floor, **when)
    except Exception as exc:
        return {"ok": False, "reason": R_MODEL_ERROR,
                "detail": "%s: %s" % (type(exc).__name__, exc)}

    out = {
        "tag_schema_version": TAGS.TAG_SCHEMA_VERSION,
        "schema": TAG_SCHEMA,
        "tag_key": TAGS.tag_key(ck, mb.get("name"), mb.get("version"), mb.get("sha256"),
                                LANES[lane]["variant"]),
        "lane": lane,
        "clip_key": ck,
        "det_ref": row.get("record_key"),
        "node": row.get("node"),
        "day": _row_day(row),
        "model": mb,
        "claim": claim_block(MODELS[LANES[lane]["model"]]["species"]),
        "scores": got["scores"],
        "max_unstored_score": got["max_unstored_score"],
        "n_classes_scored": got["n_classes_scored"],
        "n_passes": got["n_passes"],
        "embedding_dim": got["embedding_dim"],
        "embedding": got["embedding"],
        "pre_norm_dbfs": pre_db,
        "wav_header_fs_hz": int(header_fs),
        "rate_recovered": recovered,
        "input_pad_to_s": pad_to_s,
        "fs_model_hz": rs["fs_hz"],
        "fs_source_hz": rs["fs_source_hz"],
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
    out.update(got.get("extra") or {})
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
        tagger: Optional[Any] = None, verified: Optional[Dict[str, Any]] = None,
        lane: str = DEFAULT_LANE) -> Dict[str, Any]:
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
    if lane not in LANES:
        raise ValueError("unknown lane %r; known: %s" % (lane, ", ".join(sorted(LANES))))
    spec = LANES[lane]
    model = MODELS[spec["model"]]
    if verified is None:
        verified = model["verify"](model_dir)
    if tagger is None:
        if not verified["ok"]:
            raise WeightsRefused("; ".join(verified["problems"]))
        tagger = model["load"](model_dir)
    mb = model["block"](verified)

    t = empty_tally()
    census = index_census(root)                         # independent walk, before any dispatch
    t["index_lines"] = census["lines"]
    t["unparseable"] = census["unparseable"]
    t["superseded"] = census["superseded"]
    t["census_keys"] = census["keys"]
    index = CLIPS.read_index(root)
    t["index_keys"] = len(index)
    held, t["versions_held"] = TAGS.read_resume(root, spec["store"])
    t["lane"] = lane

    out_rows: List[Dict[str, Any]] = []
    t0 = time.time()
    stop_reason = None
    for row in work_order(index):
        node, day = row.get("node") or "?", _row_day(row)
        ck = row.get("clip_key")
        if isinstance(ck, str) and TAGS.tag_key(ck, mb.get("name"), mb.get("version"),
                                                mb.get("sha256"), spec["variant"]) in held:
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
        got = tag_one(tagger, row, root, mb, floor=floor, now=now, lane=lane)
        if got["ok"]:
            out_rows.append(got["row"])
            tally(t, node, day, "tagged", row=got["row"], group=model["group"])
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
    _tops = t.pop("top_scores")
    t["mean_top_score"] = (sum(_tops) / len(_tops)) if _tops else None
    t["n_top_scores"] = len(_tops)
    if model["embed_only"]:
        # No scores exist to gate on. None, not 0: check_tags reads 0 scored clips as a model
        # that produced no output.
        t["scored_any"] = t["silence_frac"] = t["mean_top_score"] = None
    t["observation_not_health"] = {
        "level_dbfs": _distribution(t.pop("dbfs")),
        # ⚠️AGGREGATED, NOT ONLY PER ROW. `max_unstored_score` made the discarded tail a number on
        # each row; nothing summed it, so a floor set too high looked exactly like a quiet period.
        "max_unstored_score": _distribution(t.pop("unstored")),
        "silence_top_frac": t["silence_frac"],
        "mean_top_score": t["mean_top_score"],
        "note": ("the class distribution is NOT an input to any gate. A quiet period is the "
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
            TAGS.append_tags(root, out_rows, spec["store"])
        _write_json_atomic(card_path(root, model["card_file"]), model["card"](verified))
        write_heartbeat(root, t, now=now, lane=lane)
    return t


def _distribution(vals: List[float]) -> Dict[str, Any]:
    if not vals:
        return {"n": 0}
    s = sorted(vals)

    def pct(f):
        return s[min(len(s) - 1, int(f * len(s)))]

    return {"n": len(s), "min": s[0], "p10": pct(0.1), "median": pct(0.5), "p90": pct(0.9),
            "max": s[-1]}


def write_heartbeat(root: str, report: Dict[str, Any], now: Optional[float] = None,
                    lane: str = DEFAULT_LANE) -> Dict[str, Any]:
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
    p = heartbeat_path(root, lane)
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
        "mean_top_score": report.get("mean_top_score"),
        "n_top_scores": report.get("n_top_scores"),
        "silence_frac": report.get("silence_frac"),
        # ⚠️`scored_any` IS AN ABSOLUTE SIGNAL AND `silence_frac` IS NOT. A model that returns
        # nothing above the floor for every clip reports silence_frac 0.000 -- the best possible
        # value -- and passed the Phase-3 gate more easily than any real run can. This says
        # whether the model produced OUTPUT AT ALL, which is not a claim about the audio.
        "scored_any": report.get("scored_any"),
        "versions_held": report.get("versions_held") or {},
        "by_reason": report.get("by_reason") or {},
        "by_node": report.get("by_node") or {},
        "new_buckets": new_buckets,
        "first_run": first_run,
        "observation_not_health": report.get("observation_not_health"),
        "heard": report.get("heard") or {},
        "lane": lane,
    }
    hb["runs"] = (hb["runs"] + [entry])[-RUN_RING:]
    _write_json_atomic(p, hb)
    return hb


def check_tags(root: str, *, max_silence_frac: float = SILENCE_FRAC_REPORT_ONLY,
               min_mean_top_score: float = MEAN_TOP_SCORE_REPORT_ONLY,
               window_s: float = DEFAULT_RUN_WINDOW_S,
               max_stale_s: float = DEFAULT_MAX_STALE_S,
               now: Optional[float] = None,
               lane: str = DEFAULT_LANE) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero when tagging is not flowing, not when the period was quiet.

    ⚠️EVERY GATE HERE CAN ACTUALLY FAIL, AND THE ABSOLUTE ONES COME FIRST. A gate of the form "if
    clips arrived and none were tagged" passes vacuously on an empty read -- point --pool at
    /pool instead of /pool/corpus and index_keys, refusals and conservation are 0, 0 and trivially
    consistent while nothing is being tagged at all. So: no heartbeat fails, no runs fails, a run
    that saw zero index rows fails, unverified weights fail, and a stale heartbeat fails whatever
    the store looks like. hear_drain.check()'s `if not sensors: return 1` is the same shape.

    ⚠️THE SILENCE FRACTION IS REPORT-ONLY FOREVER NOW, AND CALIBRATION IS WHAT RETIRED IT. It was
    "report-only until a human calibrates it"; 69 clips were heard and the answer was that
    mn10_as returns Silence top-1 on 0 of them healthy and 3 un-normalised, so any gate rests on
    three clips. `min_mean_top_score` is the canary that replaced it. At max_silence_frac < 0
    it prints the measured value and says on its own line that it is not gating. Setting it from
    a guess rather than from the measured envelope is the failure this repo names as its own.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(root, lane)
    if not os.path.exists(p):
        return 1, ["no heartbeat at %s -- lane %s has never completed a run" % (p, lane)]
    try:
        hb = json.load(open(p))
    except Exception as exc:
        return 1, ["heartbeat at %s is unreadable (%s)" % (p, exc)]
    runs = hb.get("runs") or []
    if not runs:
        return 1, ["heartbeat records no runs"]

    lines, bad = ["lane     %s" % lane], 0
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
                     "is a failure, not a quiet period (wrong --pool root?)"
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
                     "period" % tagged)
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
    # ⚠️REPORTED, NEVER GATED, AND NOT BECAUSE IT IS UNCALIBRATED. It IS calibrated now, and the
    # calibration is what retired it: mn10_as returns Silence top-1 on 0 of 69 human-heard clips
    # normalised and 3 of 69 un-normalised. A gate between those rests on three clips. Kept as an
    # observation because a sudden non-zero IS informative; it is just not a threshold.
    lines.append("silence  REPORT    %s of tagged clips top-class Silence -- NOT GATED, and it "
                 "cannot be: 0/69 healthy vs 3/69 un-normalised on the human calibration set "
                 "(docs/clip-calibration-2026-09-10.md S1.2). The normalisation canary is the "
                 "score floor below."
                 % ("%.3f" % frac if frac is not None else "n/a"))

    tops = [(float(r["mean_top_score"]), int(r.get("n_top_scores") or 0)) for r in window
            if r.get("mean_top_score") is not None]
    n_scored = sum(n for _, n in tops)
    mean_top = (sum(m * n for m, n in tops) / n_scored) if n_scored else None
    if min_mean_top_score < 0:
        lines.append("score    REPORT    mean top score %s over %d clip(s) -- NOT GATED"
                     % ("%.3f" % mean_top if mean_top is not None else "n/a", n_scored))
    elif mean_top is None:
        lines.append("score    ok        no scored clips in the window (min %.3f)"
                     % min_mean_top_score)
    elif mean_top < min_mean_top_score:
        lines.append("score    LOW       mean top score %.3f over %d clip(s), under %.3f -- the "
                     "measured cause is normalisation not running (0.254 healthy against 0.162 "
                     "un-normalised, 13.6 sigma apart at a 400-clip run). ⚠️It is calibrated "
                     "against that ONE failure; passing is not proof of health."
                     % (mean_top, n_scored, min_mean_top_score))
        bad += 1
    else:
        lines.append("score    ok        mean top score %.3f over %d clip(s) (min %.3f)"
                     % (mean_top, n_scored, min_mean_top_score))

    reasons: Dict[str, int] = {}
    for r in window:
        for k, v in (r.get("by_reason") or {}).items():
            reasons[k] = reasons.get(k, 0) + int(v)
    lines.append("refusals %s" % (json.dumps(reasons, sort_keys=True) if reasons
                                  else "none in the window"))
    heard: Dict[str, Dict[str, int]] = {}
    for r in window:
        for node, groups in (r.get("heard") or {}).items():
            h = heard.setdefault(node, {})
            for g, n in groups.items():
                h[g] = h.get(g, 0) + int(n)
    lines.append("heard    %s  (OBSERVATION, not a gate; 'speech' is the model's null response, "
                 "docs/clip-calibration-2026-09-10.md S2)" % format_heard(heard))
    obs = (last.get("observation_not_health") or {}).get("level_dbfs") or {}
    lines.append("levels   %s  (OBSERVATION, not a gate)" % json.dumps(obs, sort_keys=True))
    return (1 if bad else 0), lines


def format_report(t: Dict[str, Any]) -> str:
    out = ["lane %s" % t.get("lane", DEFAULT_LANE),
           "index %d line(s) = %d clip(s) + %d superseded + %d unparseable"
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
    out.append("heard (NOT a health input): " + format_heard(t.get("heard") or {}))
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
    ap.add_argument("--min-mean-top-score", type=float, default=MEAN_TOP_SCORE_REPORT_ONLY,
                    help="fail --check when the window's mean top score falls below this. "
                         "%.2f is the calibrated floor; negative reports without gating"
                         % MEAN_TOP_SCORE_FLOOR)
    ap.add_argument("--max-silence-frac", type=float, default=SILENCE_FRAC_REPORT_ONLY,
                    help="--check fails above this fraction of tagged clips whose top class is "
                         "Silence. Negative (the default) means REPORT ONLY -- set it from a "
                         "measured calibration set, never from a guess")
    ap.add_argument("--window-s", type=float, default=DEFAULT_RUN_WINDOW_S)
    ap.add_argument("--max-stale-s", type=float, default=DEFAULT_MAX_STALE_S)
    ap.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    ap.add_argument("--lane", action="append", choices=sorted(LANES),
                    help="lane to run; repeat for several, which share one model load. "
                         "--check reads exactly one. Default %s" % DEFAULT_LANE)
    a = ap.parse_args(argv)
    lanes = a.lane or [DEFAULT_LANE]
    models = sorted({LANES[lane]["model"] for lane in lanes})
    if len(models) != 1 and not a.check:
        print("one invocation runs one model; lanes %s use %s" % (lanes, models), file=sys.stderr)
        return 2
    model = MODELS[models[0]]

    root = os.path.expanduser(a.pool)

    if a.verify_weights:
        v = model["verify"](a.model_dir)
        if v["ok"] and models[0] == "mn10":
            print("weights OK  %s %s  %s %s"
                  % (MODEL_FILE, v["model_sha256"][:16], CLASSMAP_FILE,
                     v["class_map_sha256"][:16]))
            return 0
        if v["ok"]:
            print("weights OK  %s  tree sha256 %s" % (models[0], v["model_sha256"][:16]))
            return 0
        for p in v["problems"]:
            print("weights REFUSED: %s" % p, file=sys.stderr)
        return 2

    if a.check:
        if len(lanes) != 1:
            print("--check reads one lane; run it once per lane", file=sys.stderr)
            return 2
        code, lines = check_tags(root, max_silence_frac=a.max_silence_frac,
                                 min_mean_top_score=a.min_mean_top_score,
                                 window_s=a.window_s, max_stale_s=a.max_stale_s, lane=lanes[0])
        print("\n".join(lines))
        return code

    try:
        verified = model["verify"](a.model_dir)
        if not verified["ok"]:
            raise WeightsRefused("; ".join(verified["problems"]))
        tagger = model["load"](a.model_dir)
        reports = [run(root, model_dir=a.model_dir, limit=a.limit, deadline_s=a.deadline_s,
                       floor=a.score_floor, write=not a.census, tagger=tagger,
                       verified=verified, lane=lane) for lane in lanes]
    except WeightsRefused as exc:
        # ⚠️LOUD, AND EXIT 2 RATHER THAN 1, so a monitor can tell "the model is not what it says"
        # apart from "tagging is behind". A tagger that cannot name its weights must not run.
        print("REFUSED: the model did not verify or would not load, so nothing was tagged.\n  %s"
              % exc, file=sys.stderr)
        return 2
    for t in reports:
        print(json.dumps(t, indent=2, sort_keys=True) if a.json else format_report(t))
    # ⚠️REFUSALS DO NOT FAIL THE RUN. A refused clip is data this tool read and correctly declined
    # to tag; failing on it would make a pruned backlog look like a broken tagger forever. What
    # fails is losing track of a row.
    return 0 if all(t["conservation_ok"] for t in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
