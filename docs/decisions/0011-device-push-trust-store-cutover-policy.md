# 0011 - Device push trust-store policy for batch cutover

- Status: proposed
- Date: 2026-09-16
- Scope: how device firmware should trust the HTTPS ingest endpoint for the future batch client and the already-live single-record push path, whether that trust anchor is a single root or a bounded bundle, how trust rotation happens without a reflash at cutover, and which decisions remain operator-owned. This record modifies **no firmware file**, generates/prints/stores no certificate material, touches no device, changes no endpoint, and cuts over no transport.
- Companion to: `docs/phase4-https-batch-ingest.md` §11.2 (the cutover gate), `docs/phase4-device-batch-spool.md` §9/§13 (the device-side prerequisite and gate), `docs/decisions/0004-phase4-https-batch-ingest-adapter.md` (the additive HTTPS route), and `docs/decisions/0008-device-side-batch-client-spool.md` (the future device batch client that must not discover trust migration on a live node).
- Enforced by: nothing yet, deliberately. This is the decision gate that must be accepted before the CA-bundle build in `docs/phase4-device-batch-spool.md` §13 gate 3 can exist.

## Problem

The tree already has one live TLS trust mechanism for device push, and it is narrower than the
future cutover needs.

What exists today (verified in-tree, not assumed):

| Location | What it proves |
|---|---|
| `firmware/hear_node/hear_node.ino:2795-2828` | `push_post_json()` creates one `WiFiClientSecure` and configures trust with `client.setCACert(HEAR_PUSH_CA_CERT)` unless the explicit bench-only `HEAR_PUSH_TLS_INSECURE` escape hatch is enabled. |
| `firmware/hear_node/hear_push_ca.h` | `HEAR_PUSH_CA_CERT` defaults to the PEM for Amazon Root CA 1 and is overridable in `secrets.h` for a different `HEAR_PUSH_HOST`. |
| `docs/phase4-https-batch-ingest.md` §11.2 | Moving from the current AWS-shaped endpoint to a self-hosted `/v1/ingest/batches` endpoint must not discover the device trust-store problem during cutover. |
| `docs/phase4-device-batch-spool.md` §9/§13 | A spool pointed at an untrusted endpoint fills and evicts quietly, so a trust-store decision and a deployed CA-bundle build are **blocking** prerequisites before any node-side spool enablement. |

The failure mode to avoid is not only "TLS connect fails". On a future spooling node, an endpoint the
firmware does not trust becomes a silent operational failure: records append locally, retries continue,
and bounded eviction eventually hides a transport mistake as apparent fleet quiet.

A second failure mode is architectural drift. If the existing single-record push path and the future
batch client use different trust mechanisms, every credential rotation, endpoint migration and incident
response has to reason about two device TLS systems instead of one. Nothing in the current tree needs
that complexity.

## Decision

1. **Use one device TLS trust mechanism for both push paths: `setCACert(HEAR_PUSH_CA_CERT)`.**
   The existing single-record HTTPS push and the future `/v1/ingest/batches` client both use the
   same firmware trust input and the same `WiFiClientSecure` verification path. This record does
   **not** authorize a second trust mechanism unless a future ADR demonstrates a concrete need the
   shared mechanism cannot satisfy.

2. **The trust store is a small, bounded PEM bundle carried by `HEAR_PUSH_CA_CERT`, not a second
   runtime store and not a separate per-route knob.**
   The bundle may contain one root in steady state and two roots during a migration overlap. Its
   purpose is narrowly bounded: trust the current endpoint and, when needed, the next endpoint's
   root long enough to separate trust migration from endpoint migration.

3. **The preferred steady-state shape is still "as small as possible".**
   If one root is sufficient, the steady-state bundle is one root. If an overlap is active, the
   bundle contains only the current root plus the next root required for the planned cutover.
   This is not a general CA store and not an invitation to ship a broad public bundle.

4. **Rotation happens in three ordered stages, never as a same-moment cutover surprise:**
   1. **Trust expansion release.** While the old endpoint still works, ship a firmware build whose
      `HEAR_PUSH_CA_CERT` bundle trusts both the current root and the next root.
   2. **Endpoint cutover.** Only after the overlap build is deployed sufficiently for the intended
      cutover population may DNS/configuration move nodes to the new ingest endpoint.
   3. **Trust contraction release.** After the new endpoint is proven and rollback no longer needs
      the old root, ship a later ordinary firmware release that removes the retired root and
      returns the bundle to the smallest set that still matches reality.

5. **A cutover that requires a reflash at the moment of migration is rejected.**
   That is the explicitly rejected option from `docs/phase4-https-batch-ingest.md` §11.2, and it
   contradicts the repository rule that credential/trust rotation for this path must not depend on
   a synchronized emergency reflash window.

6. **`HEAR_PUSH_TLS_INSECURE` stays bench-only and release-forbidden.**
   It is an escape hatch for deliberate lab work, not a migration tool and not an operational
   shortcut. No production or release cutover plan may depend on it.

7. **This decision covers server authentication only.**
   It does not replace `docs/api-boundaries.md`'s target of mTLS for device identity. Until a later
   ADR lands a device key store, PKI and rotation story, the batch client and the legacy push path
   continue to authenticate the server with the shared CA bundle and authenticate the device with
   the existing push credential.

## Why the small-bundle approach is preferred over a permanently single pinned root

A permanently single-root policy is attractive only while the endpoint's issuing root never changes.
That is true today and unproven for the lifetime of the self-hosted ingest path.

The bounded-bundle approach wins for this tree because it preserves the live mechanism while adding
exactly one property the cutover requires: **overlap**.

- It keeps the same firmware API call (`setCACert`) and the same configuration seam
  (`HEAR_PUSH_CA_CERT`).
- It lets trust migration happen **before** endpoint migration, which is the operational ordering
  both Phase 4 documents already require.
- It keeps the trust set auditable and small instead of replacing one pin with a broad ambient CA
  store the firmware does not otherwise need.
- It avoids designing one trust story for `/api/hear/*` and another for `/v1/ingest/batches`.

## What this record does not decide

These remain explicitly out of scope for this design document and stay operator- or later-ADR-owned:

1. **Which concrete next root or issuing CA is chosen for the future ingest endpoint.**
   This record chooses the device-side trust-store shape, not the operator's PKI vendor or issuer.
2. **When the trust expansion release, endpoint cutover and trust contraction release happen.**
   The ordering is decided here; the schedule is an operational rollout choice.
3. **Whether device credential custody is per-node or fleet-wide.**
   `docs/decisions/0006-admin-token-provisioning-policy.md` owns that decision. The batch client
   requires only that its device credential remain rotatable without a reflash.
4. **When mTLS replaces bearer-style device credentials.**
   That requires a future firmware key-storage and PKI decision and is not silently decided here.
5. **How wide the deployment threshold must be before endpoint cutover is allowed.**
   "Sufficiently deployed" is a rollout gate the operator sets for the affected fleet slice.

## Consequences

- The existing single-record push path remains the compatibility anchor: any future batch client
  must reuse its trust mechanism rather than invent a second one.
- The implementation work implied by `docs/phase4-device-batch-spool.md` §13 gate 3 is now
  specific: produce an overlap-capable CA-bundle build before any node-side spool enablement.
- A later firmware change may keep `HEAR_PUSH_CA_CERT` as a single PEM in steady state and a
  concatenated PEM bundle during migrations, which is compatible with this decision and with the
  current code comments in `hear_push_ca.h`.
- The operator still owes the concrete CA choice and rollout timing before cutover can happen; this
  record deliberately names those as open instead of deciding them by documentation accident.
