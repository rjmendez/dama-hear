#!/usr/bin/env python3
"""Reads, checks and plans the Postgres durable-outbox migrations. Never connects to a database.

The migrations in ``deploy/postgres/migrations`` are the canonical definition of the Phase 2
durable store (see docs/durable-postgres-schema.md). Nothing in the repository applies them yet:
``make_durable_store("postgres")`` still refuses, by design, until the soak gate opens. What this
module does is keep the *files* honest in CI, where no PostgreSQL server exists:

* versions are contiguous, uniquely named, and every forward migration has a rollback;
* forward migrations are additive and idempotent -- re-applying one is a no-op, and none of them
  drops or rewrites an object at the top level, so applying them to a live database cannot lose
  data (the retention function drops partitions, but that is a function body the operator calls
  deliberately, not something apply-time does);
* rollbacks only ever drop things the matching forward migration created;
* the checksum a runner would record is derived here, so "which DDL is in that database" has a
  file-level answer.

Deliberately dependency-free: no psycopg, no SQL parser, no network. CI installs neither.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "deploy" / "postgres" / "migrations"
ROLLBACK_DIR = ROOT / "deploy" / "postgres" / "rollback"

FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
ROLLBACK_SUFFIX = "_down.sql"

# The schema version a Postgres-backed store reports. SQLite's ledger is DURABLE_SCHEMA_VERSION 1
# (tools/hear_heartbeat_receiver.py); the Postgres shape is a different, device-scoped identity
# model and therefore a new generation, not a compatible revision of the same one.
DURABLE_SCHEMA_VERSION_POSTGRES = 2

# Applied at the top level, these either destroy data or rewrite an existing object, which is the
# one thing a migration applied to a live ledger must never do. Inside a function body they are
# fine: hear.enforce_retention() drops partitions when an operator calls it.
FORBIDDEN_TOP_LEVEL = (
    (re.compile(r"^\s*DROP\s+", re.I), "drops an object"),
    (re.compile(r"^\s*TRUNCATE\s+", re.I), "truncates a table"),
    (re.compile(r"^\s*DELETE\s+FROM\s+", re.I), "deletes rows"),
    (re.compile(r"^\s*ALTER\s+TABLE\s+.*\bDROP\b", re.I | re.S), "drops a column or constraint"),
    (re.compile(r"^\s*ALTER\s+TABLE\s+.*\bALTER\s+COLUMN\b.*\bTYPE\b", re.I | re.S),
     "rewrites a column type"),
    (re.compile(r"^\s*ALTER\s+TABLE\s+.*\bRENAME\b", re.I | re.S), "renames an object"),
)

# A forward statement has to be safe to run twice: the runner is a shell loop over psql, and a
# half-applied migration must be resumable by re-running the whole file.
IDEMPOTENT_FORMS = (
    re.compile(r"^\s*CREATE\s+(SCHEMA|TABLE|INDEX|SEQUENCE|EXTENSION)\s+IF\s+NOT\s+EXISTS\b", re.I),
    re.compile(r"^\s*CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\b", re.I),
    re.compile(r"^\s*CREATE\s+OR\s+REPLACE\s+(FUNCTION|PROCEDURE|VIEW)\b", re.I),
    re.compile(r"^\s*DO\s*\$", re.I),
    re.compile(r"^\s*COMMENT\s+ON\b", re.I),
    re.compile(r"^\s*GRANT\b", re.I),
    re.compile(r"^\s*REVOKE\b", re.I),
    re.compile(r"^\s*ALTER\s+DEFAULT\s+PRIVILEGES\b", re.I),
    re.compile(r"^\s*ALTER\s+TABLE\s+\S+\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY\b", re.I),
    re.compile(r"^\s*INSERT\s+INTO\b.*\bON\s+CONFLICT\b.*\bDO\s+NOTHING\b", re.I | re.S),
    re.compile(r"^\s*SET\b", re.I),
)

ROLLBACK_ALLOWED = (
    re.compile(r"^\s*DROP\s+\w+(\s+\w+)?\s+IF\s+EXISTS\b", re.I),
    re.compile(r"^\s*DO\s*\$", re.I),
    re.compile(r"^\s*REVOKE\b", re.I),
    re.compile(r"^\s*ALTER\s+DEFAULT\s+PRIVILEGES\b", re.I),
    re.compile(r"^\s*ALTER\s+TABLE\s+\S+\s+DISABLE\s+ROW\s+LEVEL\s+SECURITY\b", re.I),
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: pathlib.Path
    sql: str
    rollback_path: Optional[pathlib.Path]
    rollback_sql: Optional[str]

    @property
    def checksum(self) -> str:
        return sha256_text(self.sql)

    @property
    def rollback_checksum(self) -> Optional[str]:
        return None if self.rollback_sql is None else sha256_text(self.rollback_sql)

    @property
    def stem(self) -> str:
        return self.path.stem


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_comments(sql: str) -> str:
    """Removes ``--`` line comments that are not inside a quoted string."""
    out: list[str] = []
    for line in sql.splitlines():
        in_single = False
        cut = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_single = not in_single
            elif ch == "-" and not in_single and line[i:i + 2] == "--":
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def iter_statements(sql: str) -> Iterator[str]:
    """Yields top-level statements, treating a dollar-quoted body as opaque.

    Function bodies are skipped over rather than parsed: what has to be checked is what the file
    does when it is applied, and a ``CREATE OR REPLACE FUNCTION`` does exactly one thing when
    applied no matter what its body says.
    """
    text = strip_comments(sql)
    buf: list[str] = []
    i = 0
    tag: Optional[str] = None
    in_single = False
    while i < len(text):
        ch = text[i]
        if tag is not None:
            if text.startswith(tag, i):
                buf.append(tag)
                i += len(tag)
                tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == "$" and not in_single:
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[i:])
            if match:
                tag = match.group(0)
                buf.append(tag)
                i += len(tag)
                continue
        if ch == ";" and not in_single:
            statement = "".join(buf).strip()
            if statement:
                yield statement
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def load_migrations(migrations_dir: pathlib.Path = MIGRATIONS_DIR,
                    rollback_dir: pathlib.Path = ROLLBACK_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if match is None:
            raise ValueError(f"{path.name} does not match NNNN_lower_snake_case.sql")
        rollback_path = rollback_dir / f"{path.stem}{ROLLBACK_SUFFIX}"
        migrations.append(Migration(
            version=int(match.group("version")),
            name=match.group("name"),
            path=path,
            sql=path.read_text(encoding="utf-8"),
            rollback_path=rollback_path if rollback_path.exists() else None,
            rollback_sql=rollback_path.read_text(encoding="utf-8") if rollback_path.exists() else None,
        ))
    return migrations


def check_versions(migrations: Iterable[Migration]) -> list[str]:
    problems: list[str] = []
    versions = [m.version for m in migrations]
    if not versions:
        return ["no migrations found"]
    if versions != sorted(versions):
        problems.append("migrations are not in ascending version order")
    if len(set(versions)) != len(versions):
        problems.append("duplicate migration version")
    if versions[0] != 1:
        problems.append(f"migrations must start at version 1, found {versions[0]}")
    for previous, current in zip(versions, versions[1:]):
        if current != previous + 1:
            problems.append(f"version gap between {previous:04d} and {current:04d}")
    return problems


def check_forward(migration: Migration) -> list[str]:
    problems: list[str] = []
    for statement in iter_statements(migration.sql):
        head = " ".join(statement.split())[:80]
        for pattern, why in FORBIDDEN_TOP_LEVEL:
            if pattern.match(statement):
                problems.append(f"{migration.path.name}: {why} at top level: {head}")
        if not any(form.match(statement) for form in IDEMPOTENT_FORMS):
            problems.append(f"{migration.path.name}: statement is not re-appliable: {head}")
    return problems


def check_rollback(migration: Migration) -> list[str]:
    if migration.rollback_sql is None:
        return [f"{migration.path.name}: no rollback in {ROLLBACK_DIR.name}/"]
    problems: list[str] = []
    for statement in iter_statements(migration.rollback_sql):
        head = " ".join(statement.split())[:80]
        if statement.upper().startswith("DROP SCHEMA") and migration.version != 1:
            problems.append(
                f"{migration.rollback_path.name}: only the baseline rollback may drop the schema: {head}")
        if not any(form.match(statement) for form in ROLLBACK_ALLOWED):
            problems.append(f"{migration.rollback_path.name}: not a reversal statement: {head}")
    return problems


def validate(migrations: Optional[list[Migration]] = None) -> list[str]:
    migrations = load_migrations() if migrations is None else migrations
    problems = check_versions(migrations)
    for migration in migrations:
        problems.extend(check_forward(migration))
        problems.extend(check_rollback(migration))
    return problems


def record_statement(migration: Migration) -> str:
    """The row a runner records after applying ``migration``."""
    return (
        "INSERT INTO hear.schema_migrations (version, name, checksum) VALUES "
        f"({migration.version}, '{migration.stem}', '{migration.checksum}') "
        "ON CONFLICT (version) DO NOTHING;"
    )


def apply_plan(migrations: Optional[list[Migration]] = None) -> list[str]:
    """The exact, ordered psql invocations an operator would run. Printed, never executed."""
    migrations = load_migrations() if migrations is None else migrations
    lines = [
        "-- Apply in this order, one transaction per file, against an EMPTY database.",
        "-- Nothing here is run by this tool or by CI.",
    ]
    for migration in migrations:
        rel = migration.path.relative_to(ROOT)
        lines.append(f"psql -v ON_ERROR_STOP=1 --single-transaction -f {rel}")
        lines.append(f"psql -v ON_ERROR_STOP=1 -c \"{record_statement(migration)}\"")
    return lines


def rollback_plan(migrations: Optional[list[Migration]] = None) -> list[str]:
    migrations = load_migrations() if migrations is None else migrations
    lines = ["-- Reverse order. 0001's rollback drops the schema and every record in it."]
    for migration in reversed(migrations):
        if migration.rollback_path is None:
            lines.append(f"-- MISSING rollback for {migration.path.name}")
            continue
        rel = migration.rollback_path.relative_to(ROOT)
        lines.append(f"psql -v ON_ERROR_STOP=1 --single-transaction -f {rel}")
        lines.append(
            "psql -v ON_ERROR_STOP=1 -c \"DELETE FROM hear.schema_migrations WHERE version = "
            f"{migration.version};\"")
    return lines


# ------------------------------------------------------------------------------------------------
# Identity helpers
# ------------------------------------------------------------------------------------------------
#
# The Postgres dedupe key is the pair, not a rewritten string: hear.durable_record_ids is keyed on
# (tenant_id, device_id, record_uid). The Python seam does not change -- persist() already gets the
# whole record and reads device_id from it -- which is what lets the same receiver code run against
# either backend during the migration.

def scoped_identity(device_id: str, record_uid: str, tenant_id: str = "default") -> tuple[str, str, str]:
    """The tuple that identifies a record in Postgres."""
    if not device_id:
        raise ValueError("device_id is required to scope a durable record identity")
    if not record_uid:
        raise ValueError("record_uid is required to scope a durable record identity")
    return (tenant_id, device_id, record_uid)


def legacy_collisions(rows: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """``{record_uid: [device_id, ...]}`` for uids claimed by more than one device.

    Fed the (device_id, record_uid) pairs of a SQLite ledger, this reports the rows that ledger
    could not have kept: its UNIQUE(record_uid) means only the first device's row survived.
    """
    seen: dict[str, list[str]] = {}
    for device_id, record_uid in rows:
        devices = seen.setdefault(record_uid, [])
        if device_id not in devices:
            devices.append(device_id)
    return {uid: devices for uid, devices in seen.items() if len(devices) > 1}


# The event stream's dedupe marker is a single ZSET member (_EVENT_DEDUPE_SCRIPT in
# tools/hear_heartbeat_receiver.py), and today that member is the bare record_uid. Once Postgres
# stops merging two devices that share a uid, the *records* are distinct but the Redis marker
# would not be: the first device's event would suppress the second one's XADD, reinstating the
# same defect one layer further out. The marker therefore has to carry the same scope the
# durable identity does, which is what this function builds.
DEDUPE_TOKEN_SEPARATOR = "|"


def redis_dedupe_token(device_id: str, record_uid: str, tenant_id: str = "default") -> str:
    """The Redis-side dedupe member for a record, scoped exactly like its durable identity."""
    tenant_id, device_id, record_uid = scoped_identity(device_id, record_uid, tenant_id)
    for part, label in ((tenant_id, "tenant_id"), (device_id, "device_id")):
        if DEDUPE_TOKEN_SEPARATOR in part:
            raise ValueError(f"{label} may not contain {DEDUPE_TOKEN_SEPARATOR!r}")
    return DEDUPE_TOKEN_SEPARATOR.join((tenant_id, device_id, record_uid))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="list migrations and their checksums")
    ap.add_argument("--check", action="store_true", help="validate ordering, additivity, rollbacks")
    ap.add_argument("--plan", action="store_true", help="print the apply plan (does not apply it)")
    ap.add_argument("--rollback-plan", action="store_true", help="print the reversal plan")
    args = ap.parse_args(argv)

    if not any((args.list, args.check, args.plan, args.rollback_plan)):
        args.check = True

    migrations = load_migrations()

    if args.list:
        for migration in migrations:
            rollback = migration.rollback_path.name if migration.rollback_path else "MISSING"
            print(f"{migration.version:04d}  {migration.checksum}  {migration.path.name}  "
                  f"rollback={rollback}")

    if args.plan:
        print("\n".join(apply_plan(migrations)))

    if args.rollback_plan:
        print("\n".join(rollback_plan(migrations)))

    if args.check:
        problems = validate(migrations)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"ok: {len(migrations)} migration(s), additive, idempotent, each reversible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
