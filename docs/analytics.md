# Higher-level analytics

This layer sits above `hear/` geometry and module detectors. It does not replace the solver or
invent confidence where the geometry has none; it correlates detections, solver cues, operator
labels and deployment context into products an operator can query, alert on and explain.

## Names that must stay distinct

| Object | Origin | Meaning | Must not be treated as |
|---|---|---|---|
| Phone step counter | IMU / OS pedometer on a handset | Human or device steps counted from acceleration, usually coarse in time and not acoustically tied to a source | Acoustic footsteps, source position, or proof that a person was heard |
| Acoustic footstep | Microphone detector firing on impact-like footfall audio | A sound event with node timestamp, spectral/temporal features and classifier confidence | Phone step count, seismic detection, or a persistent track |
| Seismic detection | Geophone / accelerometer / floor vibration detector firing | Ground-coupled vibration event with its own propagation model, attenuation and false-alarm modes | Acoustic footstep or phone step counter |
| Source track | Analytics object over time | A hypothesised real-world source assembled from associated events and filtered state | A raw detection, a solver fix, or a guaranteed identity |

Raw detections are immutable evidence. Correlations, localisations, geofence crossings, tracks and
alerts are derived products that carry provenance back to the raw events and the assumptions used.

## Event model

Every detector produces the same envelope:

- `event_id`, `node_id`, `modality` (`acoustic`, `seismic`, `phone_motion`, `chirp`, etc.).
- PPS-disciplined event time when available; otherwise device time plus an explicit clock model.
- Detector label, score, feature vector summary, duration and quality flags.
- Optional local evidence pointer: clip id, waveform hash, spectrogram crop, seismic trace window or
  handset OS sample window.
- Calibration context: node survey version, clock quality, sound-speed model, detector/model
  version and geofence/privacy masks active at capture time.

Analytics may downsample, cluster and index these envelopes, but the original event and its
calibration context remain the audit source.

## Correlation across nodes and modalities

Correlation builds candidate event groups before localisation or tracking. It uses gates, not a
single global threshold:

1. **Time gate:** modality-specific arrival windows, clock uncertainty and propagation speed. Audio
   and seismic use different velocities; phone step counters use handset sample time and only enter
   as context unless the handset is a known source.
2. **Geometry gate:** known impossible TDoA bounds, DOP or rank checks and line-of-sight/geofence
   exclusions. Two acoustic nodes can yield a range-difference locus, not a point.
3. **Feature gate:** classifier label, spectral shape, footstep cadence, seismic waveform family,
   chirp id or module-specific embeddings.
4. **Context gate:** active schedules, node health, recent calibration, privacy zones, rain/wind
   flags and operator labels.

The output is an `event_group` with membership probabilities, rejected-neighbour reasons and the
set of modalities represented. A group can be useful without a position: for example, repeated
acoustic footsteps plus seismic detections along a boundary may initiate a low-confidence track
even when geometry is underdetermined.

## Confidence-aware localisation aggregation

Localisation aggregation accepts heterogeneous solver outputs:

- point fixes from sufficiently many well-placed nodes;
- range-difference loci from two-node acoustic pairs;
- bearing or sector cues from directional sensors;
- seismic proximity/range cues;
- known-source chirps or handset positions;
- negative evidence such as nodes that should have heard the event but did not.

Each cue contributes a likelihood surface over space and time with explicit covariance or an
explicit reason covariance is undefined. A two-node TDoA locus contributes a sheet/band likelihood
and `observable_dof = 1`; it must not be collapsed into CEP, GDOP or "confidence" as if it were a
point fix.

Aggregation products:

- **MVP:** best available cue set, confidence class (`none`, `locus`, `weak_fix`, `fix`), dominant
  error terms and an operator-readable uncertainty geometry.
- **Advanced:** posterior grids, multi-hypothesis maps, negative-evidence weighting, terrain-aware
  propagation and calibration marginalisation.

## Tracks

A source track is a temporal hypothesis, not an identity claim. The tracker owns four lifecycle
decisions.

| Lifecycle step | MVP behaviour | Advanced behaviour |
|---|---|---|
| Initiation | Start tentative track from repeated compatible groups, a high-confidence fix, or a geofence-critical event. Require minimum evidence count and modality-specific false-alarm controls. | Multi-hypothesis birth model, learned false-alarm rates, source-class priors and operator-confirmed seeds. |
| Association | Associate new groups by time, spatial/locus overlap, feature similarity, cadence and source class. Preserve ambiguous alternatives instead of forcing one winner. | Joint probabilistic data association, multi-target ambiguity handling, re-identification embeddings and map constraints. |
| Filtering | Maintain state as position/velocity when observable; otherwise maintain corridor/locus occupancy, confidence and last-seen evidence. | Particle/IMM filters, class-specific dynamics, non-linear geofence/map constraints and calibration uncertainty in state. |
| Termination | End tracks after missed detections, leaving a terminal state and evidence summary. Do not delete raw events. | Occlusion-aware termination, merge/split handling, dormant track reactivation and operator lifecycle review. |

Track confidence is separate from detector confidence and localisation confidence. A track can be
strong because many footsteps form a consistent cadence while its absolute position remains weak.

## Sensor fusion

Fusion policy is evidence-first:

- Acoustic footsteps and seismic detections can reinforce a source hypothesis when their time,
  cadence and feasible propagation agree.
- Phone step counters are context for a consenting known handset or field operator; they are not
  treated as independent acoustic evidence and must not be fused into an unknown-source track
  without identity/consent policy.
- Chirps are calibration and known-source events. They may calibrate phone/audio offsets and sound
  speed, and can validate association, but they should not train the system to equate handset motion
  with unknown acoustic sources.
- Absence is evidence only after node health, radio delay irrelevance, detector duty cycle and
  geofence/privacy masks are accounted for.

The fused result always exposes which modalities were used and which were withheld by policy,
geometry or quality gates.

## Geofences

Geofences operate on uncertainty geometry, not just point-in-polygon checks.

- **Hard exclusion:** privacy or no-retention zones mask raw evidence and derived products according
  to data-governance policy.
- **Operational boundary:** alert when a fix, locus band or track posterior intersects the boundary
  above a configured probability.
- **Calibration zone:** known-source events, survey quality or sound-speed probes are expected here.
- **Suppression zone:** recurring benign sources such as machinery, operator paths or test rigs.

Every geofence decision records whether it was based on a point fix, locus intersection, track
posterior, phone context or manual label.

## Temporal patterns

Temporal analytics should stay explainable and reversible:

- burst/volley clustering for supersonic events;
- footstep cadence and path continuity for acoustic/seismic footsteps;
- daily/weekly recurrence, first-seen/last-seen and dwell-time summaries;
- seasonal or weather-correlated acoustic activity for bioacoustics;
- node-health-aware gaps so outages are not interpreted as quiet periods.

MVP temporal products are aggregate counts, recurrence windows and track summaries. Advanced
products add anomaly detection, learned baselines, periodicity search and cross-site comparison.

## Alerts

Alerts are derived from event groups, localisations, tracks and geofences. They should be
deduplicated and stateful:

- trigger: source class, confidence class, geofence relation, track lifecycle change, rate spike or
  calibration failure;
- severity: safety critical, operational, calibration, health or review-needed;
- suppression: known benign source, active test, repeated same-track alert or privacy mask;
- payload: concise evidence bundle, uncertainty geometry, reason codes and recommended operator
  action.

MVP alerting is rule-based with explicit thresholds. Advanced alerting can rank by learned risk, but
must still emit the rule/evidence explanation that made the alert actionable.

## Search and query products

Operators need search over raw and derived entities:

- "show acoustic footsteps near fence A between 22:00 and 02:00";
- "find seismic detections without matching acoustic evidence";
- "show tracks that crossed geofence B with `weak_fix` or better";
- "list all alerts supported by fewer than three nodes";
- "show chirps used in calibration version X";
- "explain why event group Y did not become a track".

Index raw events, event groups, solver cues, tracks, alerts, labels, geofences and calibration
versions separately. Queries should return both product rows and evidence links, not flattened
summaries only.

## Explainable evidence

Every derived object carries an explanation object:

- inputs included and excluded, with reason codes;
- detector scores and model versions;
- timing, geometry and feature gates applied;
- localisation confidence class and dominant error terms;
- modality distinction, especially phone step counter vs acoustic footstep vs seismic detection;
- geofence intersection method;
- track association/filtering decision and lifecycle state;
- operator labels or overrides.

Explanations are part of the product contract because this system will often have underdetermined
geometry. The honest answer may be "consistent source corridor with repeated footsteps", not "person
at coordinate X".

## MVP

The first independent-platform analytics release should deliver:

1. Canonical event envelopes for acoustic, seismic, phone-motion and chirp/known-source inputs.
2. Event grouping across nodes with time, geometry, feature and context gates.
3. Localisation aggregation that preserves point fixes, loci and unobservable states distinctly.
4. Tentative/confirmed/terminated source tracks with simple association and missed-detection
   termination.
5. Geofence intersection for points, loci and track uncertainty.
6. Rule-based alerts with deduplication and evidence bundles.
7. Search over events, groups, tracks, alerts, geofences, labels and calibration versions.
8. Explanation objects attached to every group, localisation, track and alert.

Explicit MVP non-goals: learned cross-site anomaly detection, automatic identity attribution,
black-box risk scores, full multi-target JPDA, terrain-aware propagation, and treating phone step
counters as acoustic or seismic detections.

## Advanced roadmap

Advanced features become eligible only after MVP gates pass:

- posterior-grid localisation and multi-hypothesis maps;
- particle or IMM track filters with split/merge/reactivation;
- learned association embeddings and class-specific dynamics;
- terrain/weather propagation models and negative-evidence calibration;
- anomaly detection over temporal patterns and site baselines;
- federated or cross-deployment search products;
- operator workflow analytics and active-learning queues;
- richer evidence visualisations: uncertainty bands, modality timelines, provenance graphs and
  counterfactual "why not associated" views.

## Validation gates

Analytics features are allowed to progress only when these gates are measured on fixtures, replay
data or field trials:

| Gate | MVP pass condition | Blocks |
|---|---|---|
| Modality semantics | Tests prove phone step counters, acoustic footsteps, seismic detections and source tracks remain separate entity types and cannot be silently coerced. | Any fusion, search or alert release. |
| Correlation quality | Replay fixtures show expected groups, rejected neighbours and reason codes across at least acoustic-only, seismic-only, acoustic+seismic and phone-context cases. | Track initiation and alerts. |
| Geometry honesty | Two-node acoustic cases emit `locus`/`observable_dof = 1` and never CEP/GDOP/point confidence; multi-node cases preserve solver uncertainty and calibration versions. | Localisation aggregation. |
| Track lifecycle | Deterministic fixtures cover initiation, ambiguous association, missed detections, termination and no raw-event deletion. | Track-backed geofences and alerts. |
| Geofence uncertainty | Fixtures cover point inside/outside, locus intersection, posterior overlap and privacy-mask suppression. | Operational geofence alerts. |
| Alert dedupe | Replayed repeated detections produce one stateful alert per configured policy and include evidence/explanation payloads. | Operator notifications. |
| Search provenance | Query results link back to raw events, calibration context and derived-object explanations. | Search UI/API. |
| Explainability | Golden outputs include included/excluded evidence, gates, confidence class, modality distinctions and dominant error terms. | Any operator-facing derived product. |
| Calibration sensitivity | Perturb survey, clock and sound-speed inputs and verify confidence classes/error terms change in the expected direction. | Confidence-aware localisation and fusion. |
