# Self-hosted acoustic ML lifecycle

This is the smallest lifecycle that can improve DAMA Hear models without turning the current
pool, node, or annotation service into an ML platform. It applies independently to each acoustic
module; the initial target is the supersonic sketch classifier. A prediction is never a label, and
an event is never an alarm merely because one model scored it highly.

## MVP boundary

**Outcome.** A trusted operator can promote a reproducible, human-labelled candidate to a
versioned core model; then deploy its compatible edge model deliberately, observe it, and restore
the prior known-good model. The decision record names the data, labels, code, feature contract,
metrics, artifact digest, and deployment target.

**Not in the MVP.** No feature store, workflow orchestrator, online training, automatic promotion
or rollback, distributed training, generic model serving, or raw-audio upload from an edge node.
The existing JSONL/PVC corpus, SQLite annotation DB, Python trainers, release process, and k3s
jobs are the system of record. Add a registry service only when local JSON manifests cannot be
reviewed or protected adequately.

## 1. Corpus and retention

Keep three complementary evidence types, each with its honest scope.

| Evidence | Current source and purpose | Retention / identity |
|---|---|---|
| Impulse sketch | `dets.csv` / phone sketch ingestion, decoded by `hear.corpus` | Durable telemetry. Keep original frame bytes plus decoded fields, node/boot/sample or trusted UTC, capture rate, layout, valid bands, quantization reference, gate/retrigger/clipping state, and wire/profile version. |
| Scene descriptor | Ungated `scene.csv`, currently one 20-band by four-slice descriptor each 1.024 s | Durable telemetry for background and drift. Keep its bank/version, rate, node, boot-aware time anchor, and health counters. It is not interchangeable with the impulse sketch. |
| Raw clip | `hear-drain` copies 5 s, 48 kHz WAVs into `clips/`; `clips/index.jsonl` records arrival and checksum | Bounded private review cache, not the canonical corpus. Retain its immutable index row even when the WAV is pruned; its `clip_key` is name-derived and its body SHA-256 is integrity evidence. |

Continue to drain before changing node clip budgets. Collection remains strictly sequential and
the tagger/annotation workloads must never contact a node. Retain selected, human-labelled
clips longer than ordinary cache eviction only after a documented operator decision; otherwise
the durable manifest must say `audio_pruned_at` and a future training run must refuse to claim
that raw audio was available.

Build an export manifest, not a second database. One JSONL/JSON export per training run lists
stable `record_key`/`clip_key`, data path or body digest, node class, capture date range, rate,
feature contract, exclusion reason, and label revision. Never use arrival time as acoustic event
time. Preserve unanchored records for diagnostics, but exclude them from time-based splits and
cross-node localization claims.

## 2. Labels and weak supervision

`tools/hear_annotate` is the source of supervised labels: it already writes append-only,
user-attributed `provenance="human"` annotations with confidence and notes. Extend its operating
procedure before its schema: define a small module-specific taxonomy and label guide, for example
`crack`, `blast`, `both`, `non-target`, `ambiguous`, and `unusable` for supersonic clips. `ambiguous`
and `unusable` remain review outcomes, not negative examples. Store multiple annotators'
independent rows and make a reviewed adjudication/export revision instead of overwriting a
disagreement.

Model tags remain weak supervision only. `clips/tags.jsonl` and `hear_tagging` already pin model
name, version, and SHA-256 and explicitly set `usable_as_training_label: false`; keep that guard.
Use tags, gate type, trusted field logs, controlled playback, and known maintenance intervals to
*prioritize* collection and review. Each weak source must carry source, version/digest, target
classes, coverage, and a bounded-confidence rule. It may produce a candidate queue or a
"needs-review" stratum, never silently enter a gold training set.

For each approved label export, report counts by class, annotator/agreement state, node class,
site/session/day, rate/layout, and whether raw audio remains. Require a small double-labelled
audit sample for every new taxonomy or annotator cohort. Do not collapse `unknown` into
`non-target` merely to balance classes.

## 3. Feature and raw-audio policy

Train and deploy against the bytes a target can actually produce.

* **Supersonic edge model:** use the existing fixed-layout, absolute-dB sketch with `ref_db`
  restored, the exact onset/window rule, band count, frames, rate, filterbank, and valid-band
  mask recorded as part of the feature contract. For mixed 16/48 kHz data, use
  `hear.corpus.aligned_matrix()` only for fixed-layout records and only the common valid bands;
  otherwise train and evaluate separate rate-specific models. Never pad unmeasured high bands.
* **Supersonic core model:** may score the same sketch, add calibrated node-health and
  cross-node consistency only as separately versioned inputs, and retains the geometric solver
  as an independent safety check. It must not require a raw clip, because clips are cacheable and
  may be pruned.
* **Scene/bioacoustic work:** train from raw clips or the ungated scene descriptor according to
  the question. The 33 ms triggered impulse sketch cannot represent a chorus or absence. A
  raw-audio model must pin channel policy, DC/high-pass handling, sample rate, clip/window
  selection, normalization, augmentation, and model input rate. Playback/UI normalization is not
  automatically a training transform.

Absolute level was useful in the present sketch corpus, but is site and microphone sensitive.
Report an absolute-dB model and a shape-only baseline separately; do not quietly standardize away
the former's deployment assumption. Reject absent or incompatible rate/layout/feature metadata
at inference and count the refusal. A plausible score on a shifted frequency axis is worse than
a refusal.

## 4. Training, evaluation, and promotion

Training remains a deterministic Python command in the repository or a pinned local container,
using no network fetch during the run. The run manifest must pin:

* Git commit and dirty-state digest; Python/package lock or container digest; command, seed, and
  trainer configuration.
* Corpus export digest; every raw file digest used; label export/adjudication revision; feature
  extraction implementation and contract; model weights and output SHA-256.
* Fold assignment, threshold policy, calibration method, metrics, confusion matrices, refusal
  counts, and the resulting model card.

Split by the real independence unit, never random clips or adjacent firing events. The current
228-event supersonic corpus has a measured 22.4 s feature decorrelation lag whereas its original
3 s groups leak adjacent firing strings. Use session/range-day/device as the primary holdout when
available; within a session, merge groups until at least the measured correlation lag separates
folds. Keep a final untouched prospective holdout. A model trained at one range, rifle, and
afternoon is a site-specific prototype, not a general detector.

Promotion requires a recorded comparison against the currently deployed model and simple
predeclared gates: target recall/false-positive rate at the chosen operating threshold, per-class
precision/recall and calibration, compatible-input/refusal rate, core latency/resource use, and
no material degradation on node/rate/session strata. Report ROC/PR curves but do not promote on
AUC alone. For supersonic, also run the existing alarm-reproducibility and leakage reports; a
score from a split the report marks unseparated cannot support promotion.

## 5. Inference placement and uncertainty

| Tier | Responsibility | Output |
|---|---|---|
| Node/edge | Deterministic gate, onset, sketch/scene creation, feature-contract validation, and a tiny compatible scorer only where it reduces transmission or reaction latency | Score, model ID/digest, threshold decision, feature contract/version, and refusal/health counters; no model-derived label. |
| Core | Compatible scorer, calibration, cross-node association/geometry, model comparison, review queue, and durable audit records | Probability plus calibrated uncertainty, abstention reason, evidence/model identities, and a disposition that remains distinct from a human label. |

Keep the existing gate as a gate. A new edge classifier must be shadow-scored on stored traffic
before it replaces a threshold, and a core model cannot repair events that the edge never
captures. Initial deployment should therefore be **core-only** using existing sketches/clips;
add edge scoring only after the model exactly matches generated firmware features and its
benefit is measured.

Expose uncertainty as structured data: raw score, calibrated probability if calibration is
validated for that stratum, threshold, confidence/entropy or margin, compatible-input flag, and
`abstain_reason`. Abstain on incompatible feature contracts, missing calibration, out-of-domain
signals, or insufficient cross-node evidence; do not manufacture a low-confidence negative.
For localization, retain solver covariance/consistency separately from classifier uncertainty.

## 6. Registry, signed release, deployment, and rollback

Use a reviewable, append-only `models/registry.jsonl` plus immutable artifact directories as the
MVP registry. A registry entry names module, task, semantic version, artifact SHA-256, model-card
and run-manifest digests, input contract digest, runtime/board compatibility, thresholds and
calibration revision, parent/currently replaced model, approval identity/time, and status
(`candidate`, `shadow`, `approved`, `retired`, `revoked`). Never mutate a model artifact or reuse
a version for different bytes; revocation is a new registry record.

Core deployments pin artifact SHA-256 in a committed k8s ConfigMap/image reference and verify it
at process startup. The running service emits that digest on every prediction and health record.
This follows the present tagger practice of pinning and verifying local weights rather than
fetching them at run time.

For an edge model, bundle the generated constants/weights and its input-contract digest in the
firmware release. Sign the release manifest and firmware image with an offline-held project
signing key; nodes verify it with an embedded public key before applying it. SHA-256 manifests
already protect release asset integrity in `flash.py`, but a download checksum alone does not
authenticate a maliciously replaced release manifest, so it is not the long-term signing
boundary. Keep NVS node identity and Wi-Fi provisioning outside the model image, as the existing
release flow does.

Roll out one compatible canary node/class, then a small cohort, then the fleet only while
deployment health and model counters satisfy the predeclared gate. Preserve the prior approved
artifact locally and use the existing firmware's OTA boot-failback path for failed boots. A
model-quality rollback is explicit: re-pin the preceding approved core digest or flash the
previous signed release; record cause, affected cohort, and observed evidence. No automated
quality rollback is justified until drift thresholds have prospective baselines.

## 7. Drift, active learning, and operations

Monitor four separate conditions by model digest, node class, rate/layout, and time window:

1. **Input/data drift:** feature-band/reference-level summaries for sketches; clip RMS, duration,
   sample rate, and scene-band summaries; missing/invalid feature-contract rate; gate/retrigger,
   clipping, and node health counters.
2. **Prediction drift:** score, abstention, threshold-fire, class/tag, and cross-node agreement
   distributions. This is an alert for review, not evidence of accuracy drift.
3. **Performance drift:** only human-adjudicated delayed labels can measure precision, recall,
   calibration, or false alarms. Compare against the frozen promotion holdout and a rolling,
   stratified audit sample.
4. **System drift:** clip-fetch loss, audio-pruning state, clock/PPS trust, firmware/model
   version mix, and inference latency/errors. Do not interpret an empty corpus caused by failed
   collection as acoustic quiet.

Start with daily JSON summaries and fixed baselines, not an observability product. Page only for
collection/inference contract failures; create review work for distribution changes. Establish
baseline windows before setting quality alert thresholds.

The annotation queue already ranks uncertain and cross-model-disagreeing clips. Keep that as the
MVP active-learning policy, but stratify it: reserve random samples, high-confidence positives,
near-threshold cases, disagreements, novel embedding/feature regions, each node/rate, and new
weather/site/firmware periods. Random samples prevent an uncertainty-only queue from making the
next corpus unrepresentative. New labels enter only the next frozen export; they never
retroactively alter a reported evaluation set.

## 8. Privacy and access

Raw audio, site geometry, node identity mapping, and annotation notes are private operational
data. Keep the pool, annotation service, registry write path, and signing key off the public
repository and tailnet/role-restrict them. Encrypt storage/backups where available; log accesses
to raw clips and registry promotions; use pseudonymous node IDs in training exports; remove
unnecessary notes and speech-bearing clips before sharing. Enforce retention/deletion requests
against raw audio while preserving non-reversible aggregate metrics and an audit record that an
asset was deleted. Never add real coordinates to the repository; retain the existing coordinate
guard.

## First implementation slice

1. Write a module label guide and export a human-only, session-aware label manifest from the
   existing annotation store; capture disagreement rather than overwriting it.
2. Add a run-manifest/model-card writer around `modules/supersonic/train_sketch.py` and require
   its feature contract/digest at `score_sketch()` load time. Re-run with lag-respecting,
   session-aware splits and retain the prospective holdout.
3. Add the append-only registry entry plus core shadow scoring and daily model-digest/drift
   summary. Do not modify firmware yet.
4. Only after sufficient prospective labels demonstrate a core benefit, generate a compatible
   edge artifact, embed it in a signed firmware release, and canary it with explicit rollback.
