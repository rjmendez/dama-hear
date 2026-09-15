# Self-hosted acoustic data governance

This policy applies to every self-hosted deployment that captures, derives, stores,
transmits, labels, exports, or uses acoustic data. It is jurisdiction-neutral: the
operator must map these controls to applicable law, contracts, site rules, and
community commitments before enabling recording.

The default is **data minimisation at the edge**. A deployment should transmit detections
or bounded feature sketches, not continuous audio. Raw audio is an exception requiring a
declared purpose, approved retention class, and an auditable access path.

## 1. Data classes and recording indicators

Every node and service uses these classes:

| Class | Contents | Default handling |
|---|---|---|
| `telemetry` | health, battery, firmware, clock, link and configuration state | retain for operations only |
| `detection` | timestamp, node identity, location reference, confidence, flags and derived features | preferred uplink payload |
| `clip` | bounded audio or spectrogram around a detection | create only for an approved purpose |
| `raw_audio` | continuous or long-window PCM/container audio | disabled by default; highest restriction |
| `labels` | human annotations, review decisions and model outputs | retain with provenance and reviewer identity |
| `evidence` | preserved source material, hashes, manifests and custody events | immutable or append-only storage |
| `identity/access` | operator accounts, roles, grants and audit records | separate from acoustic content where practical |

Each recording-capable node has a visible or otherwise discoverable indicator showing
`recording disabled`, `derived-only`, `clip buffer`, or `raw recording`. The indicator
must change before the state changes, survive loss of connectivity, and be documented
for people at the site. The operator records node ID, site, purpose, configuration
version, start/stop time, and responsible owner for each recording session.

Consent is not assumed from the indicator. Before recording in a place where people may
be heard, the operator documents the lawful or authorised basis for capture, the notice
method, any consent or opt-out mechanism, and the person accountable for the decision.
Where consent is required, recording remains disabled or redacted until the consent
state is known. The policy does not prescribe one jurisdiction's consent standard.

## 2. Privacy zones and edge redaction

The site registry defines zones before deployment:

- **Allowed zone:** the approved target soundscape.
- **Buffer zone:** uncertain boundary; capture is minimised and reviewed.
- **Excluded zone:** residences, private work areas, paths, gathering areas, or any
  location the operator has chosen not to monitor.

Zone definitions are versioned polygons or node-relative masks with an owner and
effective time. A node refuses a configuration with no zone assignment or with stale
zone data. The central service rejects data whose node, time, or zone version cannot be
resolved.

Redaction runs before uplink and before durable storage where technically possible.
Implementations may use directional placement, gain limits, band limits, voice/activity
classification, speech suppression, masking, deletion of excluded windows, or derived
features only. Redaction must emit a result record containing policy version, algorithm
version, input interval, output class, and reason; it must not retain a hidden copy.
Because edge classifiers can fail, operators must treat uncertain redaction as
unredacted data and apply the stricter retention and access rules.

## 3. Access control and tenant boundaries

Use a self-hosted identity provider or equivalent account directory with unique
identities, MFA for privileged roles, short-lived credentials, and no shared admin
accounts. Minimum roles are:

- **operator:** configure nodes and view operational telemetry;
- **reviewer:** inspect approved clips and create labels;
- **evidence custodian:** create or release evidence exports and legal holds;
- **model steward:** use approved training sets and publish model lineage;
- **administrator:** manage infrastructure, not content by default;
- **auditor:** read audit records without changing content.

Grant access per tenant, site, data class, and purpose. A tenant ID is mandatory on
every object, queue, index, export, and audit event; authorization checks enforce it
server-side rather than relying on UI filtering. Encryption keys, buckets, database
schemas, and backup namespaces must be tenant-separated where feasible. Cross-tenant
operations require an explicit, time-bounded grant naming both tenants, purpose, fields,
approver, and expiry. Test that a tenant cannot enumerate IDs, infer object existence,
read backups, or receive notifications belonging to another tenant.

## 4. Retention classes and minimisation

The deployment chooses a retention class per purpose before capture:

| Class | Typical contents | Default maximum |
|---|---|---:|
| `R0-derived` | detections and non-reversible features | 90 days |
| `R1-clip` | short redacted clips for review/calibration | 30 days |
| `R2-raw` | raw audio needed for a declared investigation | 7 days |
| `R3-evidence` | preserved source and derivatives under hold | until release |
| `R4-audit` | access, configuration, deletion and custody events | 1 year |

These are operational defaults, not legal advice. An owner may choose a shorter period;
longer periods require a written purpose, approver, review date, and documented basis.
Retention clocks use the event's capture time, not upload time. A failed deletion,
unknown timestamp, or unresolved tenant places the item in a quarantined exception queue,
not in indefinite silent retention.

The pipeline stores the smallest useful artifact: detection before clip, clip before
raw audio, and a short window before a long window. Raw audio is never copied into
logs, crash reports, notebooks, message payloads, or model artifacts. Temporary buffers
are encrypted where persistent and are securely cleared or overwritten when released.

## 5. Encryption and key management

Use authenticated encryption in transit and at rest. Node-to-service links use mutually
authenticated device credentials; administrative interfaces use TLS with certificate
validation. Storage, backups, removable media, and exported manifests are encrypted.
Keys are held outside application data, rotated on a documented schedule and on
personnel/device compromise, and revoked when a node or account is retired.

Access to plaintext is granted only for the operation that needs it. Key access is
audited separately from object access. Losing a key is treated as a deletion event only
after the operator verifies that no usable replica or backup remains.

## 6. Deletion, holds, and recovery

Deletion requests identify tenant, object or time range, reason, requester, approver,
and deadline. The service resolves all replicas: node buffers, primary storage,
indexes, caches, exports, backups, and derived copies. It marks the item pending,
revokes ordinary access, deletes each copy, records completion or failure per location,
and emits a signed deletion receipt. A periodic reconciler finds orphaned copies.

Legal, contractual, safety, or incident holds suspend deletion only for the named
objects and time range. A hold records issuer, scope, authority, start time, review
date, and release authority. New copies inherit the hold. Release is a separate,
audited action; retention resumes from the original clock unless the documented basis
requires otherwise. A hold must never become a general-purpose retention extension.

Restore procedures preserve original object IDs, hashes, tenant IDs, retention clocks,
and custody history. A restore is a new audited event, not an excuse to reset retention.

## 7. Evidence integrity and chain of custody

When data may support an investigation, the custodian freezes the source and records:
object ID, tenant, node, capture interval, configuration and zone versions, collector,
time source, original format, byte length, cryptographic hash, and first custody event.
Use content-addressed or write-once storage for the preserved source. Never edit an
evidence object in place; create a derivative with a new ID and parent reference.

Every custody event records who, when, what action, source and destination, reason,
tool/version, resulting hash, and success or failure. Clock uncertainty, gaps, dropped
samples, redaction, transcoding, and clock corrections are facts in the record, not
silently repaired. Verification recomputes hashes and checks the complete event chain
before release or testimony.

## 8. Export manifests

An export is a package plus a machine-readable manifest. The manifest includes schema
version, export ID, tenant, purpose, requester and approver, object IDs, capture
intervals, node/site identifiers at the permitted precision, data classes, redaction
and transformation versions, hashes, parent evidence IDs, time zone/time source,
known gaps, retention/hold state, encryption and key-reference details, and a list of
excluded objects with reasons. The package contains no undeclared files or credentials.

Exports are least-privilege and time-limited. The recipient, delivery channel,
encryption method, expiry, download/access events, and destruction confirmation are
recorded. Re-exporting a derivative requires a new manifest and provenance link.

## 9. Audit and operations

Audit events cover recording-state changes, consent/notice configuration, zone changes,
redaction decisions, reads, searches, downloads, label edits, model dataset changes,
key use, grants, exports, holds, deletion, restore, and administrative actions. Audit
records are append-only, time-synchronised as far as practical, access-controlled, and
protected from the same failure that affects the content. Alert on bulk reads, denied
cross-tenant access, disabled redaction, stale node policy, unusual exports, and
deletion failures. Review alerts and retention exceptions on a defined cadence.

## 10. Model-training provenance

Training data is a declared dataset, never an implicit query over production storage.
Each dataset release records its owner, tenant scope, purpose, inclusion/exclusion
rules, source object IDs, label versions, redaction status, consent/authority state,
retention and hold checks, hashes or immutable snapshot ID, and creation time. A model
release records dataset ID, code and dependency versions, configuration, evaluator,
metrics, known limitations, and the model artifact hash.

Training jobs receive read-only access to an approved snapshot. They cannot widen
retention, bypass tenant boundaries, or retrieve raw audio unless the dataset approval
explicitly permits it. Deleting or withdrawing a source marks affected datasets and
models for impact review; the operator records whether retraining, unlearning, or no
action is required and why. Synthetic or externally supplied audio carries its own
license, provenance, and tenant classification.

## 11. Deployment gate

Do not enable recording until the deployment has: an owner and purpose; node indicators;
zone and redaction policy; consent/notice decision; retention classes; tenant and role
configuration; encryption and key recovery; deletion and hold procedures; audit storage;
export manifest generation; evidence-custody procedure if applicable; and a model
provenance owner if training is planned. Re-review after a new site, sensor, firmware
capture mode, tenant, purpose, or applicable requirement.
