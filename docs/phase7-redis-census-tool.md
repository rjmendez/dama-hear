# Redis census tooling (`tools/hear_redis_census.py`)

This is the tooling half of [phase7-redis-lifecycle-evidence.md](phase7-redis-lifecycle-evidence.md).
It is **dry-run by default and it authorizes nothing.** The design doc's §10 lists twelve tests a
future implementation PR must bring; `tests/test_hear_redis_census.py` brings them, and every one
runs against an in-memory fake instance — no Redis, no cluster, no network.

**Live execution against `infra/audit-redis` has not happened and is not authorized by this tool.**
A census run against the real shared instance is a separate operator task with its own recorded
approver, ticket, window and redaction profile. This build deliberately ships **no live client
factory**: `--execute` with full consent still exits non-zero and says so.

## What it does

| Command | Effect |
|---|---|
| `python3 tools/hear_redis_census.py` | prints the exact command plan a live run would issue, and exits. Connects to nothing |
| `--json` | the same plan, machine-readable, with `"executed": false` |
| `--device gold --client-census` | adds the per-device reads and the consent-gated `CLIENT LIST` sample to the plan |
| `--classify` | the §4 class table, plus any frozen key pattern still unclassified (class `U`, blocking) |
| `--gate manifest.json [--stage dual_read] [--evidence evidence.json]` | offline gate evaluation; exits 1 when blocked |
| `--execute` | refused without a recorded approver, ticket and `--i-understand-shared-instance`; refused again after that, because no live client is wired |

## The envelope, enforced in code

- **Allow-list, not deny-reasoning.** `assert_redis_command` permits §2.1's read set plus
  prefix-scoped `SCAN` and `OBJECT IDLETIME/FREQ`. `MONITOR`, `CONFIG`, `KEYS`, `SUBSCRIBE`,
  `CLIENT KILL`, `DEL`, `EXPIRE`, `PERSIST`, `FLUSHDB`, `XDEL`, `SREM`, `RENAME`, `MIGRATE`,
  `DEBUG` and `ACL` are refused by name, and so is every value-reading command: the census records
  shapes (`TYPE`/`TTL`/`STRLEN`/`SCARD`/`XLEN`/`IDLETIME`), never key bodies.
- **Prefix confinement.** Every pattern, key and manifest row is `dama:hear:`-scoped. A foreign key
  name returned by `SCAN` raises rather than reaching a receipt — hear owns 10 keys of 43 260 here.
- **kubectl read verbs only**, reusing `bridge_soak_evidence.assert_read_only`.
- **`CLIENT LIST` is consent-only**, never unattended, and is redacted at capture: a salted HMAC
  pseudonym plus a network class, `name`/`lib-name`/`lib-ver`/`user` and counters. Addresses,
  command arguments, secret-named values and high-precision decimals never reach a row; the
  campaign salt never reaches a receipt.
- **`allkeys-lru` awareness.** `OBJECT FREQ` needs an LFU policy, so on the measured instance the
  receipt records the signal as *not collectable* rather than silently missing; switching the
  instance-wide policy for hear's benefit is refused, not negotiated.
- **`dama:hear:events` is lossy by construction** (`maxlen 1024`). Deriving a count or a
  conservation term from it raises `LossyEvidence`.
- **Receipts are staged, never frozen.** They are written to an operator evidence directory, and a
  receipt missing any required field reports `void`, not "clean". `tools/freeze_contracts.py
  --check` stays green; promotion would be a separate, reviewed change.

## What it grades

TTL/expiry compliance per §6.1 (a `-1` on `dama:hear:devices`, `dama:hear:latest` and
`dama:hear:event:{device_id}` is a failing unbounded surface; a TTL above the frozen 30 s on
`dama:hear:{device_id}` is a contract violation; an absent key is `unknown`, because absence is a
value there), eviction exposure and `evicted_keys` history per §6.2, boundedness per §6.3, manifest
completeness and `unattributed` rows per §5, coverage arithmetic per §3.2, and stage ordering per
§8 — stage N is blocked while stage N−1's evidence is missing, void or shorter than declared.
