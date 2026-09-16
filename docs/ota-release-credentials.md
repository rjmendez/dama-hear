# OTA release credential provisioning

## Incident

Release images are built by CI without `firmware/*/secrets.h`. In `hear_node`, Wi-Fi/name already
come from NVS, but `HEAR_PUSH_TOKEN` and `HEAR_ADMIN_TOKEN` still defaulted to empty compile-time
strings. A node flashed from v0.1.5 could pass `/status`, record to SD and look healthy while every
backend push was unauthenticated (`401`). If the admin token was also empty, `/update` and `/reboot`
failed closed, making remote recovery impossible.

Do not fix this by compiling real tokens into release assets: published binaries are downloadable
artifacts, so any baked-in backend/admin credential is disclosed to everyone who can fetch the
release.

## Secret audit

| Firmware | Name | Default/source | Needed at | Empty behavior |
| --- | --- | --- | --- | --- |
| `hear_node` | Wi-Fi SSIDs/PSKs (`WIFI_SSIDS`, `WIFI_PASSES`, `WIFI_N`) | `secrets.h`, or NVS `hear_prov` record from `enroll.py`; CI uses `-DHEAR_ALLOW_NO_WIFI` for generic release | Runtime | No STA network; node falls back to its AP and cannot be reached on the LAN |
| `hear_node` | Node identity/class (`NODE_ID`, `NODE_CLASS`) | `secrets.h`, or NVS; class has a safe board default | Runtime | MAC-derived node id; unusable for fleet identity/TDoA if not enrolled |
| `hear_node` | Fallback AP password (`AP_PASS_OVERRIDE` / `ap`) | NVS from `enroll.py`; otherwise MAC-derived | Runtime | Still unique per device, but not operator-chosen secret strength |
| `hear_node` | Backend push host (`HEAR_PUSH_HOST`) | compiled public default or NVS `phost` override | Runtime config, not secret | Pushes go to the default ingest host |
| `hear_node` | Backend push token (`HEAR_PUSH_TOKEN`) | `~/.hear_push` -> `secrets.h`, now copied/provisioned into NVS `ptoken` | Runtime secret | Pushes are sent without credentials and the backend returns `401` |
| `hear_node` | Admin token (`HEAR_ADMIN_TOKEN`) | `~/.hear_push` -> `secrets.h`, now copied/provisioned into NVS `atoken` | Runtime secret | Non-OTA privileged endpoints fail closed; `/update` stays open only when no admin token exists so a bad generic image is recoverable |
| `hear_node` | Push TLS CA / insecure override (`HEAR_PUSH_CA_CERT`, `HEAR_PUSH_TLS_INSECURE`) | tracked bounded PEM bundle (steady state: Amazon Root CA 1) plus optional `secrets.h` override | Build-time policy/config | Default remains verified TLS; insecure must be explicit |
| `puc_node` | Wi-Fi SSIDs/PSKs (`WIFI_*`) | `secrets.h`; CI can compile with `-DHEAR_ALLOW_NO_WIFI` | Runtime | AP-only/unreachable on LAN |
| `puc_node` | Node identity (`NODE_ID`) | `secrets.h`, else MAC-derived | Runtime | Unique but not enrolled fleet identity |
| `puc_node` | Admin token (`HEAR_ADMIN_TOKEN`) | `secrets.h`, default empty | Runtime secret | Privileged endpoints, including `/update` and `/reboot`, reject every request; a generic PUC artifact is not OTA-recoverable until PUC gets NVS credential provisioning |

## Mechanism

`hear_node` provisioning now stores runtime credentials in the existing `hear_prov` NVS record:

```text
PROV ... [phost=<hex>] [ptoken=<hex>] [atoken=<hex>] crc=<crc32>
```

`enroll.py` reads Wi-Fi from `~/.wifi` and push/admin settings from `~/.hear_push`, writes only a
masked summary, and never prints credential values. A build with `secrets.h` remains compatible:
on boot it copies compiled Wi-Fi/name/push/admin credentials into NVS, then later secret-free
release images use the NVS values.

Admin auth remains fail-closed for `/reboot`, `/format`, `/gate` writes and hardware probes. `/update`
is the only exception: if neither NVS nor the compiled image has an admin token, the firmware allows
an unauthenticated OTA as a recovery valve. That is intentionally less secure than a provisioned
node, but it avoids the unrecoverable state that stranded rankine. `flash.py --release` still refuses
to create that state on purpose; this valve exists for manual mistakes, old assets and lab recovery.

## Safe migration

1. Ensure local secret files exist on the operator machine (`~/.wifi` and `~/.hear_push`). Do not
   commit or paste their values.
2. For a node on USB, run:
   ```sh
   python3 firmware/hear_node/enroll.py <node> /dev/ttyACM0 --class <board-class> --no-flash
   ```
   Or first flash a non-release build from this tree with `flash.py <node> <ip>`; after reboot it
   copies compiled credentials into NVS.
3. Confirm `/status` has:
   - `prov.loaded: true`
   - `auth.push.configured: true`, `auth.push.src: "nvs"`
   - `auth.admin.configured: true`, `auth.admin.src: "nvs"`
   - after one heartbeat, `auth.push.last_code` is 2xx
4. Only then use:
   ```sh
   python3 firmware/hear_node/flash.py <node> <ip> --release v0.1.6
   ```

`flash.py --release` refuses nodes that cannot prove NVS Wi-Fi, NVS push/admin credentials, and
post-flash backend push success. `/status` health without the `auth` block is not sufficient.

## Release asset provenance

The node-side gate above is only trustworthy if a published asset really is credential-free, and
if an installer can tell. Both are now declared and enforced at build time:

* `release.yml` fails if `firmware/hear_node/secrets.h` or `firmware/puc_node/secrets.h` exists in
  the release checkout, and `firmware.yml` fails on any `secrets.h` anywhere in the tree it
  compiles. CI has never had one; the failure mode is a published fleet token and a fleet-wide
  rotation, so it is asserted rather than assumed.
* `release_ca_bundle.py` builds and verifies `hear-push-ca-bundle.pem` plus
  `hear-push-ca-bundle.json` from the tracked certificate files under
  `firmware/hear_node/trust_store/`, and refuses a release if the bundle drifts from the default
  `HEAR_PUSH_CA_CERT` macro or if any cert is missing/malformed.
* `dist/build-info.json` and `release-manifest.json` carry the claim as fields:

  ```json
  "image_class": "unprovisioned",
  "compiled_in_credentials": false,
  "provisioning_required": ["node_id", "wifi", "admin_token", "push_token"]
  ```

* `release_manifest.py` refuses to generate a manifest from build info declaring anything else, or
  from a tree that still holds `secrets.h`, and refuses to verify a downloaded release or an
  offline release directory whose manifest claims compiled-in credentials or a different image
  class. `flash.py` and `enroll.py` verify every downloaded asset through that function, so such a
  release stops before it is written to a node.
* Manifests published before these fields existed (`v0.1.5` and earlier) make no claim either way
  and stay installable. Refusing them would strand the fleet on firmware it cannot update; the
  node-side `auth` gate is what protects those installs.

## Token scope

This document describes the *mechanism*; whether `HEAR_ADMIN_TOKEN` is one fleet secret or one
secret per node — and its custody, rotation, revocation, lost-token recovery and redaction rules —
is decided in `docs/decisions/0006-admin-token-provisioning-policy.md`. The NVS record is per
device either way; today's tooling (`gen_secrets.read_push_config()`, `flash.py:admin_token()`)
reads a single bare key and so implements the fleet-wide shape only.

An unprovisioned image is not a node-ready image. Treat `image_class: unprovisioned` as "this
image supplies nothing; the node must already hold all four", and provision over USB with
`enroll.py` -- which is the only place a fleet token is ever written to a node.
