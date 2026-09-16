#!/usr/bin/env python3
"""Dry-run, read-only census tooling for `dama:hear:*` on the shared `infra/audit-redis`.

    python3 tools/hear_redis_census.py --plan                  # default: prints, connects to nothing
    python3 tools/hear_redis_census.py --classify              # offline key classification (§4)
    python3 tools/hear_redis_census.py --gate manifest.json    # offline gate evaluation (§7, §8)

This implements the tooling half of [docs/phase7-redis-lifecycle-evidence.md]: it enumerates the
`dama:hear:*` keyspace, grades TTL/eviction policy compliance under `allkeys-lru`, collects the
consumer/subscriber evidence the design calls Methods A-E, and emits a consent-gated receipt.

**It is dry-run by default and it authorizes nothing.** `--plan` prints the exact command list a
live run would issue and exits; no client is constructed, no socket is opened. A live census runs
only with `--execute`, and `--execute` is refused unless every consent field of §3.2 is supplied
(approver, ticket, window, redaction profile, allow-list version) *and* the operator passes
`--i-understand-shared-instance`. Live execution against `infra/audit-redis` is a separate,
explicitly authorized operator task; nothing in this repository grants that authorization.

Hard rules, enforced in code rather than by convention:

1. **Allow-list, not deny-reasoning.** Every Redis command goes through `assert_redis_command`.
   Only §2.1's read set plus prefix-scoped `SCAN` and `OBJECT IDLETIME/FREQ` is permitted.
   `MONITOR`, `CONFIG`, `KEYS`, `SUBSCRIBE`, `CLIENT KILL/PAUSE/NO-EVICT`, `DEL`, `EXPIRE`,
   `PERSIST`, `FLUSHDB`, `XDEL`, `SREM`, `RENAME`, `MIGRATE`, `DEBUG`, `ACL` are refused by name.
2. **Prefix confinement.** Any scan pattern or key that is not under `dama:hear:` raises, so a
   foreign tenant's key name can never reach a receipt. hear is 10 keys of 43 260 here.
3. **kubectl read verbs only**, reusing `bridge_soak_evidence.assert_read_only`, so this tool can
   never apply, patch, delete, scale, roll out or copy.
4. **No key values, ever.** The census reads shapes -- `TYPE`, `TTL`, `STRLEN`, `SCARD`, `XLEN`,
   `IDLETIME` -- never bodies. Heartbeat bodies are position-bearing.
5. **Redaction at capture.** A `CLIENT LIST` row becomes a network class plus a salted HMAC
   pseudonym; addresses, command arguments and secret-named fields never reach the receipt, and
   the salt never does either.
6. **`dama:hear:events` is lossy by construction** (`maxlen 1024`). Any attempt to derive a count
   or a conservation term from it raises `LossyEvidence`.
7. **The receipt is staged, never frozen.** It is written to an operator evidence directory and
   is void -- not "clean" -- when a required field is missing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge_soak_evidence import (  # noqa: E402
    PRECISION,
    PRECISION_MASK,
    REDACTED,
    SECRET_NAME,
    UnsafeCommand,
    assert_read_only,
    redact_text,
    redact_tree,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FREEZE_BASELINE = os.path.join("docs", "data", "phase0-freeze-contracts.v1.json")

#: Versioned so a receipt can name the rules it ran under; a run whose versions differ from the
#: recorded ones is rejected as evidence (§5).
ALLOW_LIST_VERSION = "hear-redis-census-allowlist.v1"
REDACTION_PROFILE_VERSION = "hear-redis-census-redaction.v1"
TOOL_BUILD = "hear_redis_census.v1"
RECEIPT_KIND = "hear.rediscensus.receipt.v1"
MANIFEST_KIND = "hear.redisconsumer.manifest.v1"

PREFIX = "dama:hear:"
SCAN_PATTERN = PREFIX + "*"
DEFAULT_OUT_DIR = "~/hear-redis-census"
DEFAULT_SCAN_COUNT = 100
#: §3.2 design defaults: a 14-day window sampled once a minute.
DEFAULT_WINDOW_DAYS = 14
DEFAULT_SAMPLE_INTERVAL_S = 60

#: §2.1. `KEYS` is absent on purpose (O(N) on a live multi-tenant cache), and so is every
#: command that can affect another tenant's client.
REDIS_READ_COMMANDS: frozenset[str] = frozenset({
    "DBSIZE", "INFO", "EXISTS", "XLEN", "TTL", "TYPE", "SCARD", "STRLEN", "MEMORY", "PING",
    "XINFO", "SCAN", "OBJECT",
})
#: `OBJECT` is only a read in these two forms; `OBJECT HELP` is noise and anything else is refused.
OBJECT_SUBCOMMANDS: frozenset[str] = frozenset({"IDLETIME", "FREQ", "ENCODING", "REFCOUNT"})
#: Admin-scoped and privacy-bearing: available only under §3.2's consent protocol (never unattended).
CONSENT_ONLY_COMMANDS: frozenset[str] = frozenset({"CLIENT"})
CLIENT_SUBCOMMANDS: frozenset[str] = frozenset({"LIST"})
#: Named so the refusal is a test, not a review comment (§10 test 1).
FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
    "MONITOR", "CONFIG", "KEYS", "SUBSCRIBE", "PSUBSCRIBE", "PUBLISH", "DEBUG", "ACL", "FLUSHDB",
    "FLUSHALL", "SWAPDB", "RENAME", "MIGRATE", "XDEL", "XTRIM", "SREM", "SADD", "DEL", "UNLINK",
    "EXPIRE", "PEXPIRE", "PERSIST", "SET", "GETSET", "SETEX", "XADD", "LPUSH", "RPUSH", "SHUTDOWN",
    "REPLICAOF", "SLAVEOF", "SAVE", "BGSAVE", "BGREWRITEAOF", "SCRIPT", "EVAL", "EVALSHA",
    "FUNCTION", "RESET", "FAILOVER", "LATENCY", "SLOWLOG", "GET", "MGET", "SMEMBERS", "XRANGE",
    "XREAD", "HGETALL", "LRANGE", "DUMP", "RESTORE", "COPY", "MOVE", "SELECT",
})
#: Value-reading commands are refused separately so the message says *why* (§9: no key bodies).
VALUE_READING_COMMANDS: frozenset[str] = frozenset({
    "GET", "MGET", "SMEMBERS", "XRANGE", "XREAD", "HGETALL", "LRANGE", "DUMP", "GETRANGE",
    "SRANDMEMBER", "SPOP", "XREVRANGE",
})

#: The stream is `maxlen 1024` and already lossy; it can never be a count or a conservation term.
LOSSY_KEYS: frozenset[str] = frozenset({PREFIX + "events"})

#: `OBJECT FREQ` needs an LFU policy. The measured instance is `allkeys-lru`, and switching it is
#: an instance-wide change affecting eleven foreign workloads -- refused, recorded, not negotiated.
LFU_POLICIES: frozenset[str] = frozenset({"allkeys-lfu", "volatile-lfu"})

CLASS_AUTHORITATIVE = "A"
CLASS_CACHE = "C"
CLASS_SIGNAL = "S"
CLASS_UNCLASSIFIED = "U"

#: §3.2 rule 3: a deny-list-first redaction profile for a `CLIENT LIST` row.
CLIENT_FIELDS_KEPT: Tuple[str, ...] = ("name", "lib-name", "lib-ver", "user")
CLIENT_COUNTER_FIELDS: Tuple[str, ...] = ("cmd", "argv-mem", "age", "idle", "db")
#: A one-character salt is not a salt; it also matches every receipt as a substring.
MIN_SALT_LEN = 8
#: Everything else -- including every command argument -- is dropped rather than sanitized.

_PRIVATE_V4 = (
    (re.compile(r"^10\."), "rfc1918-10"),
    (re.compile(r"^192\.168\."), "rfc1918-192.168"),
    (re.compile(r"^172\.(1[6-9]|2\d|3[01])\."), "rfc1918-172.16/12"),
    (re.compile(r"^127\."), "loopback"),
    (re.compile(r"^169\.254\."), "link-local"),
    (re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\."), "cgnat-100.64/10"),
)


class CensusRefusal(RuntimeError):
    """A census action was refused by the safety envelope of §2."""


class LossyEvidence(RuntimeError):
    """Something tried to derive a count from `dama:hear:events`, which is lossy by construction."""


class ConsentMissing(CensusRefusal):
    """A live run was requested without the recorded consent §3.2 requires."""


# ---------------------------------------------------------------- key patterns and classes

@dataclass(frozen=True)
class KeyRule:
    """One frozen key pattern, its class today and its target lifecycle rule (§4, §6.1)."""

    pattern: str
    today_class: str
    target_class: str
    ttl_today: Optional[int]          # None == no TTL (observed `-1`)
    ttl_required: bool                # must the target rule carry a TTL?
    derivable_from: str
    notes: str = ""

    def matches(self, key: str) -> bool:
        return _pattern_regex(self.pattern).match(key) is not None


KEY_RULES: Tuple[KeyRule, ...] = (
    KeyRule(PREFIX + "event:{device_id}", CLASS_AUTHORITATIVE, CLASS_CACHE, None, True,
            "last durable event row per device",
            "TTL -1 today: one key per device forever is unbounded in device count"),
    KeyRule(PREFIX + "devices", CLASS_AUTHORITATIVE, CLASS_CACHE, None, True,
            "canonical device registry",
            "TTL -1 today: a set that only ever grows is a registry pretending to be a cache"),
    KeyRule(PREFIX + "latest", CLASS_AUTHORITATIVE, CLASS_CACHE, None, True,
            "MAX(received_at) over durable rows",
            "no TTL today; a stale fleet-global latest is worse than no value"),
    KeyRule(PREFIX + "events", CLASS_AUTHORITATIVE, CLASS_CACHE, None, False,
            "durable event log",
            "stream maxlen 1024: recent tail, lossy by construction, never a source of counts"),
    KeyRule(PREFIX + "{device_id}", CLASS_AUTHORITATIVE, CLASS_SIGNAL, 30, True,
            "canonical health projection over durable heartbeat rows",
            "TTL 30 s is a frozen contract value; changing it is an ADR, not a tuning knob"),
)


def _pattern_regex(pattern: str) -> "re.Pattern[str]":
    parts = re.split(r"(\{[a-z_]+\})", pattern)
    out = "".join(r"[^:]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + out + "$")


def rule_for_key(key: str) -> Optional[KeyRule]:
    """The most specific rule matching `key`; `dama:hear:{device_id}` is the fallback."""
    assert_prefix(key)
    literal = [r for r in KEY_RULES if "{" not in r.pattern and r.pattern == key]
    if literal:
        return literal[0]
    templated = [r for r in KEY_RULES if "{" in r.pattern and r.matches(key)]
    if not templated:
        return None
    return sorted(templated, key=lambda r: -len(r.pattern))[0]


def classify_key(key: str) -> str:
    rule = rule_for_key(key)
    return rule.today_class if rule else CLASS_UNCLASSIFIED


def frozen_key_patterns(baseline_path: Optional[str] = None) -> List[str]:
    """The `dama:hear:*` patterns in the frozen contract baseline (§10 test 8's input)."""
    path = baseline_path or os.path.join(ROOT, FREEZE_BASELINE)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [k["pattern"] for k in data.get("redis_keys", {}).get("keys", [])]


def classification_gaps(baseline_path: Optional[str] = None) -> List[str]:
    """Frozen patterns with no class in §4. Any result here is a blocking `U` (§4)."""
    known = {r.pattern for r in KEY_RULES}
    return [p for p in frozen_key_patterns(baseline_path) if p not in known]


# ---------------------------------------------------------------- safety envelope

def assert_prefix(value: str) -> None:
    """Rule §2.5: every scan, count and report is `dama:hear:`-scoped."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise CensusRefusal(
            "key or pattern %r is outside the %s prefix; hear is a minority tenant on a shared "
            "instance and may never name a foreign key" % (value, PREFIX))
    if "*" in value[:-1] or "?" in value or "[" in value:
        raise CensusRefusal("pattern %r may only use a trailing wildcard" % value)


def assert_not_lossy(key: str, what: str) -> None:
    """Rule §5/§10 test 10: nothing is ever counted out of the capped stream."""
    if key in LOSSY_KEYS:
        raise LossyEvidence(
            "%s may not be derived from %s: the stream is maxlen-capped and already discards "
            "silently, so it can never be conservation evidence" % (what, key))


def assert_redis_command(argv: Sequence[str], *, consent: bool = False) -> None:
    """The single choke point every Redis call goes through (§2.1, §2.2)."""
    argv = [str(a) for a in argv]
    if not argv:
        raise CensusRefusal("empty Redis command refused")
    name = argv[0].strip().upper()
    if name in VALUE_READING_COMMANDS:
        raise CensusRefusal(
            "%s reads a key value; the census records shapes (TYPE/TTL/STRLEN/SCARD/XLEN/"
            "IDLETIME), never bodies" % name)
    if name in FORBIDDEN_COMMANDS:
        raise CensusRefusal(
            "%s is refused: it is client-affecting, configuration-changing or writing on an "
            "instance shared with eleven foreign workloads" % name)
    if name in CONSENT_ONLY_COMMANDS:
        sub = argv[1].strip().upper() if len(argv) > 1 else ""
        if sub not in CLIENT_SUBCOMMANDS:
            raise CensusRefusal("CLIENT %s is refused; only CLIENT LIST is a census command" % sub)
        if not consent:
            raise ConsentMissing(
                "CLIENT LIST is admin-scoped and returns every tenant's client state; it runs "
                "only under the recorded consent protocol, never from an unattended job")
        return
    if name not in REDIS_READ_COMMANDS:
        raise CensusRefusal(
            "%s is not in the census allow-list (%s)" % (name, ", ".join(sorted(REDIS_READ_COMMANDS))))
    if name == "OBJECT":
        sub = argv[1].strip().upper() if len(argv) > 1 else ""
        if sub not in OBJECT_SUBCOMMANDS:
            raise CensusRefusal("OBJECT %s is not a read subcommand" % sub)
        if len(argv) > 2:
            assert_prefix(argv[2])
        return
    if name == "SCAN":
        upper = [a.upper() for a in argv]
        if "MATCH" not in upper:
            raise CensusRefusal("SCAN without MATCH would walk 43 000 foreign keys; refused")
        assert_prefix(argv[upper.index("MATCH") + 1])
        return
    if name in ("TTL", "TYPE", "STRLEN", "SCARD", "XLEN", "EXISTS", "XINFO"):
        for arg in argv[1:]:
            if arg.upper() in ("STREAM", "GROUPS", "FULL"):
                continue
            assert_prefix(arg)


def assert_kubectl_read_only(args: Sequence[str]) -> None:
    """§10 test 3: mirrors the soak tool's verb confinement rather than re-deriving it."""
    assert_read_only(args)


# ---------------------------------------------------------------- redaction (§3.2, §9)

def network_class(address: str) -> str:
    """The network class of a client address -- the only part of it that survives capture."""
    host = (address or "").rsplit(":", 1)[0].strip("[]")
    if not host:
        return "unknown"
    if ":" in host:
        return "ipv6-loopback" if host in ("::1",) else "ipv6"
    for pattern, label in _PRIVATE_V4:
        if pattern.match(host):
            return label
    return "public-or-unclassified"


def pseudonym(salt: str, address: str) -> str:
    """A stable salted pseudonym. The salt is kept out of the receipt (§9)."""
    if not salt or len(salt) < MIN_SALT_LEN:
        raise CensusRefusal(
            "a client pseudonym needs a campaign salt of at least %d characters; refusing to emit "
            "a raw address or a guessable pseudonym" % MIN_SALT_LEN)
    digest = hmac.new(salt.encode("utf-8"), (address or "").encode("utf-8"), hashlib.sha256)
    return "client-" + digest.hexdigest()[:16]


def parse_client_row(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for token in (line or "").split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    return out


def redact_client_row(line: str, salt: str) -> Dict[str, Any]:
    """Deny-list-first: a row becomes a pseudonym, a network class and counters. Nothing else."""
    raw = parse_client_row(line)
    address = raw.get("addr", "")
    row: Dict[str, Any] = {
        "client_pseudonym": pseudonym(salt, address),
        "network_class": network_class(address),
    }
    for field_name in CLIENT_FIELDS_KEPT:
        value = raw.get(field_name, "")
        if SECRET_NAME.search(field_name) or SECRET_NAME.search(value):
            value = REDACTED
        row[field_name.replace("-", "_")] = redact_text(value)
    for field_name in CLIENT_COUNTER_FIELDS:
        row[field_name.replace("-", "_")] = redact_text(raw.get(field_name, ""))
    assert_no_leak(row, salt=salt, raw_line=line)
    return row


_ADDR_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\[?[0-9a-fA-F]*:[0-9a-fA-F:]+\]?:\d+")


def assert_no_leak(row: Mapping[str, Any], *, salt: str, raw_line: str) -> None:
    """A receipt that would carry an address, the salt or a precise decimal is refused."""
    blob = json.dumps(row, sort_keys=True)
    if salt and salt in blob:
        raise CensusRefusal("the campaign salt must never appear in a receipt")
    if _ADDR_RE.search(blob):
        raise CensusRefusal("a client address reached a receipt row; refused rather than sanitized")
    if PRECISION.search(blob):
        raise CensusRefusal("a high-precision decimal reached a receipt row")
    kept = {str(v) for v in row.values()}
    for token in (raw_line or "").split():
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name in CLIENT_FIELDS_KEPT + CLIENT_COUNTER_FIELDS + ("addr",):
            continue
        # Short values (`db=0`, `sub=0`) are indistinguishable from a kept counter's digits, so
        # only a distinctive dropped value is evidence that a dropped field survived.
        if len(value) >= 4 and value not in kept and value in blob:
            raise CensusRefusal("dropped CLIENT LIST field %r survived into the row" % name)


# ---------------------------------------------------------------- planning (dry run)

@dataclass(frozen=True)
class PlannedCommand:
    kind: str                 # "redis" | "kubectl"
    argv: Tuple[str, ...]
    why: str
    consent_required: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "command": list(self.argv), "why": self.why,
                "consent_required": self.consent_required}


def census_plan(*, device_ids: Sequence[str] = (), include_client_census: bool = False,
                ) -> List[PlannedCommand]:
    """Exactly what a live run would issue, validated against the allow-list before it is printed."""
    plan: List[PlannedCommand] = [
        PlannedCommand("redis", ("PING",), "liveness of the connection itself"),
        PlannedCommand("redis", ("DBSIZE",), "instance-wide denominator: hear is a minority tenant"),
        PlannedCommand("redis", ("INFO", "memory"),
                       "maxmemory-policy and evicted_keys: the eviction exposure of §6.2"),
        PlannedCommand("redis", ("INFO", "stats"), "keyspace hits/misses as an instance denominator"),
        PlannedCommand("redis", ("SCAN", "0", "MATCH", SCAN_PATTERN, "COUNT", str(DEFAULT_SCAN_COUNT)),
                       "prefix-scoped cursor walk; KEYS stays excluded (O(N) on a live cache)"),
    ]
    for key in [PREFIX + "devices", PREFIX + "latest", PREFIX + "events",
                *[PREFIX + "event:" + d for d in device_ids],
                *[PREFIX + d for d in device_ids]]:
        plan.append(PlannedCommand("redis", ("TYPE", key), "key shape"))
        plan.append(PlannedCommand("redis", ("TTL", key), "TTL/expiry policy compliance (§6.1)"))
        plan.append(PlannedCommand("redis", ("OBJECT", "IDLETIME", key),
                                   "upper bound on time since last access (§3.3)"))
    plan.append(PlannedCommand("redis", ("SCARD", PREFIX + "devices"), "unbounded-set cardinality"))
    plan.append(PlannedCommand("redis", ("XLEN", PREFIX + "events"),
                               "stream length, recorded as a lossy tail and never as a count"))
    if include_client_census:
        plan.append(PlannedCommand("redis", ("CLIENT", "LIST"),
                                   "sampled client census, redacted at capture (§3.2)",
                                   consent_required=True))
    for cmd in plan:
        if cmd.kind == "redis":
            assert_redis_command(cmd.argv, consent=cmd.consent_required)
    return plan


# ---------------------------------------------------------------- live reader (consent-gated)

@dataclass
class Consent:
    """§3.2 step 1: no run without a recorded approver. Every field is receipt-bearing."""

    approver: str = ""
    ticket: str = ""
    window_days: float = float(DEFAULT_WINDOW_DAYS)
    sample_interval_s: float = float(DEFAULT_SAMPLE_INTERVAL_S)
    client_census: bool = False
    salt: str = ""
    acknowledged_shared_instance: bool = False

    def assert_sufficient(self) -> None:
        missing = [name for name, ok in (
            ("approver", bool(self.approver)),
            ("ticket", bool(self.ticket)),
            ("window_days", self.window_days > 0),
            ("acknowledged_shared_instance", self.acknowledged_shared_instance),
        ) if not ok]
        if self.client_census and not self.salt:
            missing.append("salt")
        if missing:
            raise ConsentMissing(
                "live census refused; missing recorded consent: %s. Live execution against "
                "infra/audit-redis is a separately authorized operator task" % ", ".join(missing))

    def as_dict(self) -> Dict[str, Any]:
        return {"approver": self.approver, "ticket": self.ticket,
                "window_days": self.window_days, "sample_interval_s": self.sample_interval_s,
                "client_census": self.client_census}


RedisExecutor = Callable[..., Any]


class CensusReader:
    """A Redis reader that physically cannot issue a refused command.

    The client is injected -- in tests it is an in-memory stub, in a live run it would be a
    `redis.Redis`. The reader never constructs one itself, which is why importing this module
    cannot open a socket.
    """

    def __init__(self, client: Any, *, consent: Optional[Consent] = None) -> None:
        self._client = client
        self.consent = consent or Consent()
        self.issued: List[Tuple[str, ...]] = []

    def execute(self, *argv: Any) -> Any:
        args = tuple(str(a) for a in argv)
        assert_redis_command(args, consent=bool(self.consent.client_census))
        self.issued.append(args)
        return self._client.execute_command(*args)

    # -- keyspace -------------------------------------------------------

    def scan_keys(self, pattern: str = SCAN_PATTERN, count: int = DEFAULT_SCAN_COUNT) -> List[str]:
        assert_prefix(pattern)
        keys: List[str] = []
        cursor = "0"
        seen_cursors = 0
        while True:
            cursor, batch = self.execute("SCAN", cursor, "MATCH", pattern, "COUNT", count)
            cursor = str(_text(cursor))
            for raw in batch or []:
                key = _text(raw)
                assert_prefix(key)  # a foreign key name may never reach a receipt
                if key not in keys:
                    keys.append(key)
            seen_cursors += 1
            if cursor == "0" or seen_cursors > 10000:
                break
        return sorted(keys)

    def key_shape(self, key: str) -> Dict[str, Any]:
        assert_prefix(key)
        shape: Dict[str, Any] = {"key": key, "type": _text(self.execute("TYPE", key)),
                                 "ttl_s": _int(self.execute("TTL", key))}
        shape["idle_time_s"] = _int(self.execute("OBJECT", "IDLETIME", key))
        kind = shape["type"]
        if kind == "set":
            shape["cardinality"] = _int(self.execute("SCARD", key))
        elif kind == "string":
            shape["size_bytes"] = _int(self.execute("STRLEN", key))
        elif kind == "stream":
            shape["stream_len"] = _int(self.execute("XLEN", key))
            shape["lossy"] = True
        rule = rule_for_key(key)
        shape["pattern"] = rule.pattern if rule else None
        shape["class_today"] = rule.today_class if rule else CLASS_UNCLASSIFIED
        shape["class_target"] = rule.target_class if rule else CLASS_UNCLASSIFIED
        return shape

    def server_facts(self) -> Dict[str, Any]:
        info = _info_dict(self.execute("INFO", "memory"))
        stats = _info_dict(self.execute("INFO", "stats"))
        policy = info.get("maxmemory_policy", "unknown")
        return {
            "dbsize": _int(self.execute("DBSIZE")),
            "maxmemory_policy": policy,
            "evicted_keys": _int(stats.get("evicted_keys", info.get("evicted_keys"))),
            "keyspace_hits": _int(stats.get("keyspace_hits")),
            "keyspace_misses": _int(stats.get("keyspace_misses")),
            "object_freq_collectable": policy in LFU_POLICIES,
        }

    # -- clients (consent-gated, redacted at capture) --------------------

    def client_sample(self) -> List[Dict[str, Any]]:
        self.consent.assert_sufficient()
        if not self.consent.client_census:
            raise ConsentMissing("CLIENT LIST was not consented for this campaign")
        raw = _text(self.execute("CLIENT", "LIST"))
        return [redact_client_row(line, self.consent.salt)
                for line in raw.splitlines() if line.strip()]


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _int(value: Any) -> Optional[int]:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


def _info_dict(blob: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(blob, Mapping):
        return {str(k): _text(v) for k, v in blob.items()}
    for line in _text(blob).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k] = v
    return out


# ---------------------------------------------------------------- lifecycle grading (§6)

@dataclass
class Finding:
    key_or_pattern: str
    verdict: str          # "pass" | "fail" | "unknown"
    rule: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"subject": self.key_or_pattern, "verdict": self.verdict, "rule": self.rule,
                "detail": redact_text(self.detail)}


def grade_ttl_policy(shapes: Sequence[Mapping[str, Any]]) -> List[Finding]:
    """§6.1: every TTL is a maximum staleness, and `-1` on a cache key is an unbounded surface."""
    findings: List[Finding] = []
    for shape in shapes:
        key = shape.get("key", "")
        rule = rule_for_key(key) if key else None
        ttl = shape.get("ttl_s")
        if rule is None:
            findings.append(Finding(key, "fail", "classification",
                                    "no class for this key; class U blocks every gate"))
            continue
        if not rule.ttl_required:
            findings.append(Finding(key, "pass", "ttl-policy",
                                    "%s is bounded by maxlen, not by TTL" % rule.pattern))
            continue
        if ttl is None:
            findings.append(Finding(key, "unknown", "ttl-policy", "TTL not collected"))
        elif ttl == -2:
            findings.append(Finding(key, "unknown", "ttl-policy",
                                    "key absent at sample time; absence is a value here, not a failure"))
        elif ttl == -1:
            findings.append(Finding(key, "fail", "ttl-policy",
                                    "TTL -1: unbounded growth surface whose only bound today is "
                                    "another tenant's LRU pressure (%s)" % rule.notes))
        elif rule.ttl_today is not None and ttl > rule.ttl_today:
            findings.append(Finding(key, "fail", "ttl-policy",
                                    "TTL %ss exceeds the frozen contract value %ss"
                                    % (ttl, rule.ttl_today)))
        else:
            findings.append(Finding(key, "pass", "ttl-policy", "TTL %ss is bounded" % ttl))
    return findings


def grade_eviction_exposure(server: Mapping[str, Any]) -> List[Finding]:
    """§6.2: `allkeys-lru` means every hear key is already treated as class C by the server."""
    policy = str(server.get("maxmemory_policy", "unknown"))
    findings = [Finding("instance", "fail" if policy.startswith("allkeys") else "unknown",
                        "eviction-exposure",
                        "maxmemory-policy=%s: dama:hear:* is evictable regardless of TTL, so key "
                        "presence is not a durability statement" % policy)]
    evicted = server.get("evicted_keys")
    findings.append(Finding("instance", "pass" if evicted == 0 else "fail" if evicted else "unknown",
                            "eviction-history",
                            "evicted_keys=%s" % ("unknown" if evicted is None else evicted)))
    if not server.get("object_freq_collectable", False):
        findings.append(Finding("instance", "unknown", "access-frequency",
                                "OBJECT FREQ needs an LFU policy; under %s it is not collectable "
                                "and changing the policy instance-wide is refused" % policy))
    return findings


def grade_boundedness(shapes: Sequence[Mapping[str, Any]], *, enrolled_devices: int,
                      max_envelope_bytes: int) -> List[Finding]:
    """§6.3: the bounded replacements must assert cardinality and byte size, not hope for them."""
    findings: List[Finding] = []
    for shape in shapes:
        key = shape.get("key", "")
        if shape.get("cardinality") is not None:
            size = int(shape["cardinality"])
            findings.append(Finding(
                key, "pass" if size <= enrolled_devices else "fail", "boundedness",
                "cardinality %d against an enrolled fleet of %d" % (size, enrolled_devices)))
        if shape.get("size_bytes") is not None:
            size = int(shape["size_bytes"])
            findings.append(Finding(
                key, "pass" if size <= max_envelope_bytes else "fail", "boundedness",
                "%d bytes against a max envelope of %d" % (size, max_envelope_bytes)))
    return findings


def count_from(shapes: Sequence[Mapping[str, Any]], key: str) -> int:
    """Any count taken from the capped stream raises rather than returning a number (§10 test 10)."""
    assert_not_lossy(key, "a count or conservation term")
    for shape in shapes:
        if shape.get("key") == key:
            for candidate in ("cardinality", "size_bytes"):
                if shape.get(candidate) is not None:
                    return int(shape[candidate])
    raise CensusRefusal("no counted shape for %s" % key)


# ---------------------------------------------------------------- consumer manifest (§5)

MANIFEST_REQUIRED: Tuple[str, ...] = (
    "consumer_id", "owner", "owner_contact_role", "discovery_method", "source_ref",
    "keys_touched", "access_kind", "status",
)
DISCOVERY_METHODS = frozenset({"A", "B", "C", "D", "E"})
MANIFEST_STATUSES = frozenset({"attributed", "unattributed", "retired", "disputed"})


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_manifest(manifest: Mapping[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    rows = manifest.get("consumers", [])
    if not rows:
        findings.append(Finding("manifest", "fail", "manifest-empty",
                                "an empty manifest is absence of evidence, not evidence of absence"))
    for row in rows:
        cid = str(row.get("consumer_id", "<unnamed>"))
        missing = [f for f in MANIFEST_REQUIRED if not row.get(f)]
        if missing:
            findings.append(Finding(cid, "fail", "manifest-row",
                                    "missing required field(s): %s" % ", ".join(missing)))
        if row.get("discovery_method") and row["discovery_method"] not in DISCOVERY_METHODS:
            findings.append(Finding(cid, "fail", "manifest-row",
                                    "discovery_method %r is not one of A-E" % row["discovery_method"]))
        if row.get("status") and row["status"] not in MANIFEST_STATUSES:
            findings.append(Finding(cid, "fail", "manifest-row",
                                    "status %r is not a manifest status" % row["status"]))
        for key in row.get("keys_touched", []) or []:
            assert_prefix(str(key))
        if row.get("status") == "attributed" and not row.get("replacement_answer"):
            findings.append(Finding(cid, "fail", "manifest-row",
                                    "an attributed reader needs a replacement_answer before any "
                                    "lifecycle change"))
    return findings


def unattributed_rows(manifest: Mapping[str, Any]) -> List[str]:
    return [str(r.get("consumer_id", "<unnamed>")) for r in manifest.get("consumers", [])
            if r.get("status") == "unattributed"]


# ---------------------------------------------------------------- coverage and receipts (§5)

RECEIPT_REQUIRED: Tuple[str, ...] = (
    "run_id", "window_start", "window_end", "method", "sample_count", "samples_lost",
    "longest_gap_s", "allow_list_version", "redaction_profile_version", "approver", "tool_build",
)


def coverage_verdict(*, longest_gap_s: float, claimed_cadence_s: float) -> Dict[str, Any]:
    """§3.2: a census with a four-hour hole cannot prove anything about an hourly job."""
    covered = longest_gap_s <= claimed_cadence_s
    return {
        "claimed_cadence_s": claimed_cadence_s,
        "longest_gap_s": longest_gap_s,
        "covered": covered,
        "detail": ("gap %gs is within the claimed cadence %gs" if covered else
                   "gap %gs exceeds the claimed cadence %gs; that cadence is NOT covered")
                  % (longest_gap_s, claimed_cadence_s),
    }


def receipt_status(receipt: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """A receipt missing any required field is **void**, not a negative result (§5)."""
    missing = [f for f in RECEIPT_REQUIRED if receipt.get(f) in (None, "", [])]
    if missing:
        return "void", missing
    if receipt.get("allow_list_version") != ALLOW_LIST_VERSION:
        return "void", ["allow_list_version"]
    if receipt.get("redaction_profile_version") != REDACTION_PROFILE_VERSION:
        return "void", ["redaction_profile_version"]
    return "valid", []


def build_receipt(*, run_id: str, window_start: str, window_end: str, method: str,
                  consent: Consent, sample_count: int, samples_lost: int, longest_gap_s: float,
                  server: Mapping[str, Any], shapes: Sequence[Mapping[str, Any]],
                  findings: Sequence[Finding], clients: Sequence[Mapping[str, Any]] = (),
                  unattributed: Sequence[str] = ()) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {
        "receipt_kind": RECEIPT_KIND,
        "staged": True,          # never promoted into the frozen baseline by this tool (§10 test 11)
        "frozen": False,
        "run_id": run_id,
        "window_start": window_start,
        "window_end": window_end,
        "method": method,
        "sample_count": sample_count,
        "samples_lost": samples_lost,
        "longest_gap_s": longest_gap_s,
        "allow_list_version": ALLOW_LIST_VERSION,
        "redaction_profile_version": REDACTION_PROFILE_VERSION,
        "approver": consent.approver,
        "consent_ticket": consent.ticket,
        "tool_build": TOOL_BUILD,
        # `server` is instance free text and goes through the tree redactor; the key shapes are
        # prefix-asserted names and integers, and the name-based redactor would blank a field
        # literally called "key", so they are carried as measured. Client rows and finding text
        # were already redacted at capture.
        "server": redact_tree(dict(server)),
        "keys": [dict(s) for s in shapes],
        "findings": [f.as_dict() for f in findings],
        "clients": [dict(c) for c in clients],
        "unattributed_count": len(unattributed),
        "unattributed": list(unattributed),
    }
    receipt["status"], receipt["missing_fields"] = receipt_status(receipt)
    for key in receipt["keys"]:
        assert_prefix(str(key.get("key", PREFIX)))
    return receipt


def write_receipt(receipt: Mapping[str, Any], out_dir: str) -> str:
    """Receipts land in an operator evidence directory; never in the frozen baseline."""
    target = os.path.abspath(os.path.expanduser(out_dir))
    if os.path.normpath(FREEZE_BASELINE) in os.path.normpath(target):
        raise CensusRefusal("a census receipt is staged evidence and may not be written into the "
                            "frozen contract baseline")
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "census-%s.json" % receipt.get("run_id", "unknown"))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


# ---------------------------------------------------------------- gates (§8)

#: Stage N may not be evaluated before stage N-1's evidence exists (§10 test 12).
STAGE_ORDER: Tuple[str, ...] = ("census", "compare", "dual_read", "quiet", "write_stop", "removal")
STAGE_EVIDENCE: Dict[str, str] = {
    "census": "a dated census receipt with a covered window",
    "compare": "a comparison receipt over the declared window",
    "dual_read": "a dual-read receipt with an expected-difference list",
    "quiet": "a quiet-period receipt",
    "write_stop": "a write-stop receipt",
    "removal": "a tenant-scoped ACL plus an approved removal record",
}


@dataclass
class GateResult:
    stage: str
    passed: bool
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage, "passed": self.passed, "reasons": list(self.reasons)}


def evaluate_gate(stage: str, *, manifest: Mapping[str, Any],
                  evidence: Mapping[str, Mapping[str, Any]],
                  declared_window_days: float = float(DEFAULT_WINDOW_DAYS)) -> GateResult:
    if stage not in STAGE_ORDER:
        raise CensusRefusal("unknown stage %r" % stage)
    reasons: List[str] = []
    index = STAGE_ORDER.index(stage)
    for earlier in STAGE_ORDER[:index]:
        row = evidence.get(earlier)
        if not row:
            reasons.append("stage %r has no evidence (%s)" % (earlier, STAGE_EVIDENCE[earlier]))
            continue
        if row.get("status") == "void":
            reasons.append("stage %r evidence is void, not a negative result" % earlier)
        window = row.get("window_days")
        if window is not None and float(window) < declared_window_days:
            reasons.append("stage %r window %s d is shorter than the declared %s d"
                           % (earlier, window, declared_window_days))
    for finding in validate_manifest(manifest):
        if finding.verdict == "fail":
            reasons.append("manifest: %s: %s" % (finding.key_or_pattern, finding.detail))
    blocked = unattributed_rows(manifest)
    if blocked:
        reasons.append("unattributed consumer(s) block every gate: %s" % ", ".join(sorted(blocked)))
    gaps = []
    try:
        gaps = classification_gaps()
    except OSError:
        reasons.append("frozen baseline unreadable; classification completeness unknown")
    if gaps:
        reasons.append("unclassified (class U) key pattern(s): %s" % ", ".join(sorted(gaps)))
    own = evidence.get(stage)
    if own and own.get("status") == "void":
        reasons.append("stage %r own evidence is void" % stage)
    return GateResult(stage, not reasons, reasons)


# ---------------------------------------------------------------- live run (injected client)

def run_census(client: Any, *, consent: Consent, run_id: Optional[str] = None,
               enrolled_devices: int = 6, max_envelope_bytes: int = 4096,
               now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """One census sample against an injected client. Never constructs a connection itself."""
    consent.assert_sufficient()
    reader = CensusReader(client, consent=consent)
    started = now or dt.datetime.now(dt.timezone.utc)
    reader.execute("PING")
    server = reader.server_facts()
    shapes = [reader.key_shape(k) for k in reader.scan_keys()]
    findings = list(grade_ttl_policy(shapes))
    findings += grade_eviction_exposure(server)
    findings += grade_boundedness(shapes, enrolled_devices=enrolled_devices,
                                  max_envelope_bytes=max_envelope_bytes)
    clients = reader.client_sample() if consent.client_census else []
    ended = started + dt.timedelta(days=consent.window_days)
    return build_receipt(
        run_id=run_id or started.strftime("%Y%m%dT%H%M%SZ"),
        window_start=started.isoformat(), window_end=ended.isoformat(),
        method="A+C" + ("+B" if consent.client_census else ""),
        consent=consent, sample_count=1, samples_lost=0,
        longest_gap_s=consent.sample_interval_s, server=server, shapes=shapes,
        findings=findings, clients=clients,
        unattributed=[c["client_pseudonym"] for c in clients if not c.get("name")],
    )


# ---------------------------------------------------------------- CLI

def render_plan(plan: Sequence[PlannedCommand]) -> str:
    lines = ["# dry run: this prints the plan and connects to nothing.",
             "# allow-list: %s   redaction profile: %s   build: %s"
             % (ALLOW_LIST_VERSION, REDACTION_PROFILE_VERSION, TOOL_BUILD),
             "# live execution against infra/audit-redis requires a separate, explicit",
             "# authorization; this tool refuses --execute without recorded consent.", ""]
    for cmd in plan:
        mark = "  [consent-gated]" if cmd.consent_required else ""
        lines.append("%-8s %-58s # %s%s" % (cmd.kind, " ".join(cmd.argv), cmd.why, mark))
    return "\n".join(lines)


def render_classification() -> str:
    lines = ["pattern | class today | target class | derivable from",
             "------- | ----------- | ------------ | --------------"]
    for rule in KEY_RULES:
        lines.append("%s | %s | %s | %s"
                     % (rule.pattern, rule.today_class, rule.target_class, rule.derivable_from))
    gaps = classification_gaps()
    lines.append("")
    lines.append("unclassified frozen patterns (class U, blocking): %s"
                 % (", ".join(gaps) if gaps else "none"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--plan", action="store_true",
                   help="print the command plan and exit (default; connects to nothing)")
    p.add_argument("--classify", action="store_true", help="print the §4 classification table")
    p.add_argument("--gate", metavar="MANIFEST.json", help="evaluate a stage gate offline")
    p.add_argument("--stage", default="census", choices=list(STAGE_ORDER))
    p.add_argument("--evidence", metavar="EVIDENCE.json", help="stage evidence index for --gate")
    p.add_argument("--device", action="append", default=[], dest="devices",
                   help="device id to include in the planned per-device reads")
    p.add_argument("--client-census", action="store_true",
                   help="include the consent-gated CLIENT LIST sample in the plan")
    p.add_argument("--execute", action="store_true",
                   help="run live (refused without full recorded consent and an injected client)")
    p.add_argument("--approver", default="", help="the recorded approver for a live run")
    p.add_argument("--ticket", default="", help="the consent ticket for a live run")
    p.add_argument("--i-understand-shared-instance", action="store_true", dest="ack",
                   help="acknowledge that audit-redis is shared with foreign tenants")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.classify:
        print(render_classification())
        return 0
    if args.gate:
        manifest = load_manifest(args.gate)
        evidence = load_manifest(args.evidence) if args.evidence else {}
        result = evaluate_gate(args.stage, manifest=manifest, evidence=evidence)
        print(json.dumps(result.as_dict(), indent=2) if args.json else
              ("gate %s: %s" % (result.stage, "PASS" if result.passed else "BLOCKED")
               + "".join("\n  - " + r for r in result.reasons)))
        return 0 if result.passed else 1
    if args.execute:
        consent = Consent(approver=args.approver, ticket=args.ticket,
                          client_census=args.client_census,
                          acknowledged_shared_instance=args.ack)
        try:
            consent.assert_sufficient()
        except ConsentMissing as exc:
            print("refused: %s" % exc, file=sys.stderr)
            return 2
        print("refused: this build has no live client factory. A live census against "
              "infra/audit-redis is a separately authorized operator task; wire an audited, "
              "read-only client and re-run under that authorization.", file=sys.stderr)
        return 2
    plan = census_plan(device_ids=args.devices, include_client_census=args.client_census)
    if args.json:
        print(json.dumps({"plan": [c.as_dict() for c in plan],
                          "allow_list_version": ALLOW_LIST_VERSION,
                          "redaction_profile_version": REDACTION_PROFILE_VERSION,
                          "tool_build": TOOL_BUILD, "executed": False}, indent=2))
    else:
        print(render_plan(plan))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
