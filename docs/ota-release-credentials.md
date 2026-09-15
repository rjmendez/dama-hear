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
| `hear_node` | Admin token (`HEAR_ADMIN_TOKEN`) | `~/.hear_push` -> `secrets.h`, now copied/provisioned into NVS `atoken` | Runtime secret | Privileged endpoints fail closed: `/update`, `/reboot`, `/format`, `/gate` writes and hardware probes reject every request |
| `hear_node` | Push TLS CA / insecure override (`HEAR_PUSH_CA_CERT`, `HEAR_PUSH_TLS_INSECURE`) | tracked Amazon Root CA default, optional `secrets.h` override | Build-time policy/config | Default remains verified TLS; insecure must be explicit |
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
