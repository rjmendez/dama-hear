# 0006 - `HEAR_ADMIN_TOKEN` scope, custody and lifecycle

- Status: proposed — **one operator decision is required before §Decision can be marked accepted**
  (see "The choice that is not mine to make")
- Date: 2026-09-15
- Scope: what an admin token *is* for this fleet — fleet-wide or per-node — where its authority of
  record lives, how it is injected, rotated, revoked and recovered, and what it may appear in.
- This record **creates, generates, solicits, prints, stores, transmits and modifies no token
  value.** It changes no firmware, no tool and no deployment. It names exactly which code changes
  become required *after* the operator chooses, and nothing is implemented here.
- Companion to: `docs/ota-release-credentials.md` (the mechanism), `docs/release-v0.1.6-readiness.md`
  §5 (which names this as the last decision gating the v0.1.6 rollout).
- Enforced today by: `tests/test_firmware_admin_auth.py`, `tests/test_flash_ota_auth.py`,
  `tests/test_nvs_enrollment.py`, `tests/test_enrollment_gate.py`, `tests/test_prov_line.py`.

## Problem

`HEAR_ADMIN_TOKEN` is the only credential standing in front of `/update`, `/reboot`, `/format`,
`/gate` writes and the hardware sweeps that repurpose live GPIOs
(`hear_node.ino:hear_auth_ok()`, `HEAR_REQUIRE_AUTH()`). v0.1.6 cannot roll out and a
gold/release recovery cannot be executed until the token exists, because
`flash.py --release` refuses any node whose live `/status` does not prove
`auth.admin.configured: true, src: "nvs"` (`deploy_gate.auth_reasons()`), and
`~/.hear_push` on the operator machine currently carries `HEAR_PUSH_TOKEN` only.

Nothing in the tree states whether one token covers the fleet or each node holds its own. The
mechanism is deliberately neutral: the NVS `hear_prov` record carries `atoken` **per device**
(`hear_prov_line.h`), so the *firmware* already supports either shape. The **tooling does not**:
`gen_secrets.read_push_config()` parses exactly three keys and `flash.py:admin_token()` returns the
single `HEAR_ADMIN_TOKEN` line regardless of which node it is talking to. Today, therefore, the
fleet is fleet-wide **by omission** rather than by decision — which is the failure mode this
repository keeps writing incidents about.

## Threat model

Assets: remote code execution on a node (`/update`), destructive control (`/reboot`, `/format`),
detection-gate tampering (`/gate` POST — it silently changes what the fleet considers an event), and
GPIO sweeps that can drive pins on live hardware.

Trust boundary: the node's HTTP port is **unauthenticated for reads** and sits on the same LAN the
sensor data rides. `/status` is deliberately open and deliberately valueless: it reports
`configured`, `src` and `ota_recovery_open`, never a token, length or hash
(`hear_node.ino` status builder). Anything that can resolve a node's IP can read that, so the token
is the entire control-plane boundary.

| # | Threat | Reachable today | Fleet-wide blast radius | Per-node blast radius |
|---|---|---|---|---|
| T1 | LAN attacker reflashes a node over `/update` | yes, if the token leaks | all 6 nodes | 1 node |
| T2 | Token disclosed in a log, PR, chat, agent transcript or shell history | the realistic leak path; token values are never on a command line today (`flash.py` uses the `X-Hear-Auth` header, `enroll.py` prints only a masked summary) | fleet rotation = 6 USB visits | 1 USB visit |
| T3 | Physical capture of one deployed node | flash is not encrypted; NVS is readable to anyone with the board | fleet-wide compromise from one stolen node | that node only |
| T4 | Published release asset carries a credential | blocked: `release.yml`/`firmware.yml` fail on any `secrets.h`, manifest asserts `compiled_in_credentials: false`, `release_manifest.py` refuses otherwise | fleet-wide disclosure | same protection |
| T5 | Token lost by the operator | node answers `/status` forever, refuses `/update` from everyone | fleet loses remote admin at once | one node |
| T6 | Tokenless image reaches a node | firmware opens an unauthenticated `/update` recovery valve (`hear_ota_recovery_open()`), `flash.py --release` refuses to create the state | anyone on the LAN can flash that node until it is enrolled | same |

T3 is decisive and is not hypothetical for this deployment: these are outdoor, physically reachable
field nodes, and a single fleet-wide secret makes "someone picked up one node" equal to "every node
must be re-enrolled over USB".

## What the cost of per-node actually is (measured, not assumed)

The usual argument for a fleet-wide secret is rotation cost. **Here it is zero difference**, and
that is a property of the tree, not an opinion:

* There is **no network path that writes NVS**. `PROV` lines are read only from the USB CDC port in
  `loop()` (`prov_serial_line()`), and a successful write replaces the whole record and reboots.
* Therefore rotating the admin token on a node running a release image requires **USB access to
  that node**, whether the new value is shared with five other nodes or not.
* Rotating "the fleet token" on a six-node fleet is six USB visits. Rotating six per-node tokens is
  the same six visits.

Per-node costs one thing only: a bounded change to three tools so that "the token for *this* node"
is resolvable. Fleet-wide costs a permanent, unbounded correlation between any one node's exposure
and every other node's control plane.

## Decision

1. **Admin tokens are per-node.** One distinct secret per `node_id`, provisioned into that node's
   own NVS `hear_prov.atoken`. No value is ever shared by two nodes, and no value is ever equal to
   `HEAR_PUSH_TOKEN` — the push token is fleet-wide by design (it authenticates to the backend and
   is matched against the `hear-heartbeat-token` k8s secret) and is a *different blast radius*.
   Reusing one as the other would silently merge the two.
2. **Authority of record is the operator's password manager, one entry per node**, named for the
   node id and the credential (`hear-admin/<node-id>`), recording: node id, provisioning date,
   firmware build at provisioning, and rotation reason for the previous value. `~/.hear_push` is a
   mode-`600` **working copy on the operator machine only**, never the authority, never backed up
   anywhere else, never synchronised.
3. **A node's token is generated on the operator machine at enrollment time**, by a password
   manager or `secrets.token_urlsafe`-class generator, ≥ 32 bytes of entropy, ASCII, ≤ 128 bytes
   (the `HEAR_PROV_TOKEN_MAX` limit the parser and firmware both enforce). It is typed/pasted into
   `~/.hear_push` and the password manager and nowhere else.
4. **Injection is USB enrollment only.** `enroll.py` over the wire in front of the operator. Never
   a command-line argument (shell history, `ps`), never a query string, never a compiled release
   asset.
5. **`/update` keeps its tokenless recovery valve**, unchanged. It is the reason a provisioning
   mistake is a walk to the node rather than a dead node, and `flash.py --release` continues to
   refuse to *create* that state without the spelled-out `--allow-admin-lockout`.
6. **`puc_node` is out of scope for per-node NVS provisioning** and stays compile-time
   (`puc_node.ino` has no `hear_prov` record). Its per-node separation is achieved by
   `gen_secrets.py --dir firmware/puc_node` per node, and a PUC flashed from a generic artifact is
   *not* OTA-recoverable. That gap is stated, not closed, here.

### Least privilege

* The admin token grants **node-local privileged control only**. It is not a backend credential, it
  is not accepted by any service, and no service validates it — `X-Hear-Auth` appears only in
  `hear_node.ino`, `puc_node.ino` and `flash.py` in this repository. Nothing in `hear/`,
  `tools/` or `deploy/` consumes it.
* No automation holds it. `tools/fleet.py`, `tools/check_fleet_health.py` and `tools/node_survey.py`
  read unauthenticated endpoints only, and must stay that way: giving a scheduled job an admin
  token converts a read-only monitor into fleet-wide RCE. **If a future automation needs privileged
  control, it gets its own ADR, not this token.**
* `/gate` GET stays unauthenticated and `/gate` POST stays guarded — the existing split is correct
  and is asserted by `tests/test_firmware_admin_auth.py`.

## The choice that is not mine to make

Everything above is a recommendation grounded in this tree. The operator decides **A or B**:

* **A (recommended): per-node.** Blast radius 1. Requires the tooling changes in the next section
  before the v0.1.6 rollout continues.
* **B: fleet-wide.** Works with today's tooling unchanged: one `HEAR_ADMIN_TOKEN` line in
  `~/.hear_push`, provisioned to all six nodes. Accepts T3 fleet-wide compromise from one captured
  node, and accepts that a single leak costs six USB visits *and* a re-provisioning of every node.
  If B is chosen, this ADR is accepted with §Decision item 1 replaced by "one fleet token, custody
  as in item 2 with a single entry `hear-admin/fleet`", and **no code changes are required at all**
  — only the readiness runbook's decision line is resolved.

No token exists yet, so neither option has a migration debt. This is the cheapest moment the
decision will ever have.

## Changes required after the decision (A only; none for B)

Each is small, testable offline, and must land as one PR before any node is enrolled.

1. `firmware/hear_node/gen_secrets.py:read_push_config()` — its key regex admits exactly
   `HEAR_PUSH_HOST|HEAR_PUSH_TOKEN|HEAR_ADMIN_TOKEN`. Extend it to also admit
   `HEAR_ADMIN_TOKEN_<node-id>` (node-id grammar `[a-z0-9][a-z0-9-]{0,22}`, matching
   `hear_prov_id_ok()`), and add:

   ```python
   def admin_token_for(node, cfg):   # per-node first, bare key ONLY as an explicit fleet mode
   ```

   The bare `HEAR_ADMIN_TOKEN` must **not** silently serve a node that has no per-node entry: that
   is how a per-node fleet decays back into a shared one. Resolution order is per-node key, else
   `HEAR_ADMIN_TOKEN` **only when** `HEAR_ADMIN_TOKEN_MODE=fleet` is present, else empty.
2. `firmware/hear_node/flash.py:admin_token()` — takes the node id and delegates to
   `admin_token_for()`. Both call sites (`admin_lockout_refusal(admin_token(), ...)` and
   `ota_post(target, bin_path, admin_token())`) pass the node being flashed. The 401 message
   already says the token must be the one the **running** image holds; it must additionally name
   the per-node key.
3. `firmware/hear_node/enroll.py:main()` — replaces `push_cfg.get("HEAR_ADMIN_TOKEN")` with
   `admin_token_for(a.node, push_cfg)`, and **refuses to enroll with no admin token unless a
   spelled-out opt-out is passed**, because an enrollment that silently omits it produces exactly
   the `ota_recovery_open: true` node `flash.py --release` will later refuse. The masked summary
   stays masked.
4. `firmware/hear_node/gen_secrets.py` `__main__` — writes the per-node value into `secrets.h`
   for the node it is generating, not the bare key.
5. Tests: extend `tests/test_flash_ota_auth.py` and add `tests/test_admin_token_scope.py` per the
   acceptance criteria below. No test may contain a realistic-looking token; use obvious fixtures.
6. Docs: `docs/ota-release-credentials.md` secret-audit row for `HEAR_ADMIN_TOKEN` gains "per-node,
   distinct per `node_id`", and `docs/release-v0.1.6-readiness.md` §5 records the resolved choice.

**Not changed, deliberately:** `hear_prov_line.h`, `hear_prov.cpp`, `hear_node.ino` and the NVS
record. They are already per-device; the compiled-vs-NVS precedence (`HEAR_ADMIN_TOKEN[0]` wins,
else NVS) is also unchanged — see the rotation trap below.

## Lifecycle

**Enrollment (injection).** Generate → password manager entry → `~/.hear_push` (mode 600) → node on
USB → `enroll.py <node> /dev/ttyACM0 --class <board-class> --no-flash` → verify on the live node:
`prov.loaded: true`, `auth.admin.configured: true`, `auth.admin.src: "nvs"`,
`auth.admin.ota_recovery_open: false`, `auth.push.configured: true`, `auth.push.src: "nvs"`, and
`auth.push.last_code` 2xx after one heartbeat. Only then is that node enrollable for
`flash.py --release`.

**Rotation.** Trigger on any of: suspected disclosure (T2), a node leaving physical custody or being
decommissioned (T3), operator-machine compromise or re-image, an operator handover, or 12 months.
Procedure per node: new value → password manager (keep the old entry marked superseded with the
reason until the node is verified) → `~/.hear_push` → USB `enroll.py --no-flash` → verify the
`auth` block → delete the superseded entry. Per-node scope means rotation is per-node; a fleet-wide
event rotates all six.

⚠️ **Rotation trap, and it is in the firmware today.** A compiled-in token wins over NVS at every
boot (`hear_node.ino`: `if (HEAR_ADMIN_TOKEN[0]) ... else if (have_nv) ...`). A node running a
locally-built image with a stale `secrets.h` will keep presenting the **old** token after a
successful NVS rotation, and `PROV` writes are refused outright by an image with compiled
credentials (`prov_serial_line()` answers `PROV ERR this image has compiled-in credentials`).
Rotation is therefore only meaningful on **secret-free release images**. A node still on a
compiled-credentials build must first be moved to a release image, or have `secrets.h` regenerated
and be reflashed, before its rotation counts.

**Revocation.** There is no revocation list and no server to hold one: the token is node-local, so
revocation *is* rotation on that node, and it is not complete until the node's `/status` has been
re-verified. Revoking a node that cannot be reached is not possible — the honest statement is
"pending physical access", and it must be tracked as such, not assumed.

**Decommissioning.** Before a node leaves custody, re-enroll it with a throwaway token or
`/format` + re-enroll, so the retired board carries no live fleet credential. Then delete the
password-manager entry.

## Behaviour when the token is lost

This is the case that stranded rankine, and the answer is now bounded — state it plainly in the
runbook:

| Node state | Lost-token consequence | Recovery |
|---|---|---|
| Release image (secret-free), token in NVS, value lost | `/update`, `/reboot`, `/format`, `/gate` POST refuse everyone, including `flash.py`. `/status` and data keep working. `auth.admin.configured: true`, `ota_recovery_open: false` | **USB `enroll.py --no-flash` writes a new record.** Not a brick, not a reflash: `PROV` is accepted because the image has no compiled credentials. Physical access is mandatory. |
| Compiled-credentials build, value lost | as above, **and** `PROV` is refused | USB reflash (`flash.py <node> /dev/ttyACM0`) with a regenerated `secrets.h`, or flash a release image and then enroll |
| No admin token at all | privileged routes refuse, but `/update` is **open unauthenticated** to the LAN | enroll immediately; treat the node as untrusted until `ota_recovery_open: false` |

The policy consequence: **lost token = one walk to one node** under per-node scope, and
**six walks** under fleet-wide.

## Logging and redaction

* Token values must never appear in: this repository, `secrets.h` (gitignored, never committed),
  release assets, CI logs, `/status`, the serial `PROV STATE` line, an issue, a PR, a chat message,
  an agent transcript, or shell history. Current behaviour already satisfies this and must be kept
  asserted: `enroll.py` prints `admin=yes|no`, `PROV STATE` prints `admin=yes|no`,
  `/status` prints `configured`/`src`/`ota_recovery_open`, and `flash.py` sends the value in a
  header.
* The `PROV` line on the USB wire carries the value hex-encoded — that is framing, **not
  protection**. Enroll only on a machine and cable the operator controls.
* `hear/ingest/reconcile.py`'s redaction pattern already blocks `token|secret|auth|credential`
  field names from mismatch receipts; no admin-token field may be added to any telemetry or receipt
  schema, redacted or not.
* An accidental disclosure is a T2 rotation trigger, and the rotation is not optional.

## Acceptance criteria (testable, offline, no node and no real secret)

1. `admin_token_for("gold", {"HEAR_ADMIN_TOKEN_gold": "fixture-a"}) == "fixture-a"`.
2. A node with no per-node entry and no `HEAR_ADMIN_TOKEN_MODE=fleet` resolves to `""` even when a
   bare `HEAR_ADMIN_TOKEN` is present — no silent fleet fallback.
3. With `HEAR_ADMIN_TOKEN_MODE=fleet`, the bare key resolves for every node (option B stays
   expressible and stays a deliberate, written-down state).
4. Two different node ids with per-node entries never resolve to the same value in the resolver's
   own fixtures, and a helper reports duplicate values across per-node keys as an error — a
   copy-paste that re-creates a fleet-wide secret must be caught at the tool, not in the field.
5. `flash.py` OTA to node `X` sends `X`'s token: the `X-Hear-Auth` header value equals
   `admin_token_for("X", cfg)`, and the token appears in **no** element of `ota_curl_cmd()` that is
   part of the URL (existing `test_the_token_never_goes_in_the_url` extended per node).
6. A tokenless OTA flash still stops **before compiling**, and its message still names both
   `--allow-admin-lockout` and the per-node key (existing
   `test_a_tokenless_ota_flash_stops_before_it_compiles_anything`).
7. `enroll.py` with no resolvable admin token refuses, and the refusal names the node and the key;
   with the spelled-out opt-out it proceeds and says so.
8. No credential value is an argument to any log, serial reply or exception message
   (`tests/test_nvs_enrollment.py::test_no_credential_is_an_argument_to_the_log_or_the_serial_reply`
   and `test_the_argument_check_would_catch_a_leak` still pass unchanged).
9. `/status` still exposes `configured`/`src`/`ota_recovery_open` and no value, length or hash
   (`tests/test_nvs_enrollment.py::test_status_says_where_the_credentials_came_from`).
10. `deploy_gate.auth_reasons(..., require_nvs_credentials=True)` still refuses
    `auth.admin.src != "nvs"` and `configured: false`.
11. Release provenance unchanged: `release_manifest.py` still refuses any build info not declaring
    `image_class: unprovisioned`, `compiled_in_credentials: false`,
    `provisioning_required: [..., "admin_token", ...]`.
12. Repository hygiene: no test, fixture, doc or manifest in the tree contains a value that could be
    a real token; `secrets.h` remains absent and CI-blocked.

Gate command (all offline):

```sh
python3 -m pytest -q tests/test_firmware_admin_auth.py tests/test_flash_ota_auth.py \
  tests/test_nvs_enrollment.py tests/test_enrollment_gate.py tests/test_prov_line.py \
  tests/test_release_manifest.py tests/test_admin_token_scope.py
```

## Rollout, mixed versions and recovery

1. Decide A or B. Nothing below starts first.
2. If A: land the tooling PR above, green on the gate command. No node is touched.
3. Enroll node-by-node over USB, verifying the `auth` block per node before moving on. A fleet is
   allowed to sit half-enrolled: an unenrolled node is backend-mute and `flash.py --release`
   refuses it, which is a loud, correct stop rather than a silent one.
4. Only then `flash.py <node> <ip> --release v0.1.6`, one node at a time, verifying `fw`,
   `auth.admin.src: "nvs"` and a 2xx `auth.push.last_code` after each.
5. **Mixed-version safety.** Nodes on compiled-credentials builds keep working: the boot path
   copies compiled credentials into NVS, so `src` becomes `nvs` without a USB visit. A node still
   on a pre-`auth`-block firmware reports no `auth` block at all and `deploy_gate` refuses it by
   design; that node is enrolled over USB, not forced through. Manifests published before the
   provenance fields (`v0.1.5` and earlier) stay installable and make no claim.
6. **Rollback.** A release OTA that misbehaves reverts through the existing boot-guard failback; the
   NVS record lives outside both OTA slots, so a revert does not lose the admin token. Rolling back
   to a *compiled-credentials* build re-introduces the rotation trap above and must be treated as a
   temporary state.
7. **gold/release recovery** proceeds under exactly this path — gold is enrolled over USB like any
   other node; nothing about the recovery justifies a shared token or an
   `--allow-admin-lockout` shortcut.

## Consequences

* Compromise of one node, one leaked value, or one stolen board no longer implies fleet-wide
  privileged access (A).
* One bounded tooling PR is required before enrollment (A); zero code changes (B).
* Rotation and revocation both require physical access, for every node, under either option. That
  is a property of the design (no network NVS write) and is accepted here, not worked around: an
  HTTP re-provisioning endpoint would be a new, permanently exposed privileged surface, and it
  would need its own ADR and its own threat model.
* The operator carries six secrets instead of one, in a password manager that already exists.
* `puc_node` remains without NVS credential provisioning and therefore without OTA recovery from a
  generic artifact. Named here so it is a known gap rather than a discovered one.
