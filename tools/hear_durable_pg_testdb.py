#!/usr/bin/env python3
"""Ephemeral PostgreSQL test databases for the Phase 2 durable-outbox suite.

The migrations in ``deploy/postgres/migrations`` are checked as *files* by
tools/hear_durable_pg.py, which never connects to anything. That catches an unorderable or
irreversible migration set; it cannot catch a schema whose semantics are wrong, because a CHECK
constraint, a SKIP LOCKED claim and a partition drop only exist once a server has parsed them.
This module is the other half: it creates a throwaway database, applies the migration set to it,
hands the tests a connection, and destroys the database afterwards.

Three rules it exists to enforce:

* **Nothing but a scratch database is ever touched.** Every database it creates is named
  ``hear_test_<random>``; it refuses to run against a database whose name it did not generate,
  and it drops only databases it created in this process.
* **No driver dependency.** CI installs ``requirements/ci-dev.txt``; adding psycopg to it for a
  suite that is skipped whenever no server is present would be a dependency for nothing. Every
  statement goes through ``psql``, which the CI job installs as a client package and which the
  operator runbook already uses (deploy/postgres/README.md).
* **The suite degrades to skips, never to failures.** Without ``HEAR_PG_TEST_DSN`` (or a
  reachable local server) the live tests skip and the file-level tests still run.

Usage in a test::

    dsn, why = testdb.availability()
    if dsn is None:
        pytest.skip(why)
    with testdb.ephemeral_database(dsn) as db:
        db.apply_migrations()
        db.run_file(FIXTURE)

CLI::

    python tools/hear_durable_pg_testdb.py --self-test    # apply, exercise, roll back, drop
"""
from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from typing import Iterator, Optional, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hear_durable_pg as PG  # noqa: E402

DSN_ENV = "HEAR_PG_TEST_DSN"
#: Databases this module is allowed to create and drop. Anything else is someone's data.
SCRATCH_NAME_RE = re.compile(r"^hear_test_[0-9a-f]{16}$")
DEFAULT_TIMEOUT_S = 300


class PsqlError(RuntimeError):
    """A psql invocation that exited non-zero, with its output attached."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message if not output else f"{message}\n{output.strip()}")
        self.output = output


# ------------------------------------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------------------------------------
def availability(env: Optional[dict] = None) -> tuple[Optional[str], str]:
    """``(admin_dsn, reason)``. ``admin_dsn`` is None when the live suite cannot run."""
    env = os.environ if env is None else env
    if shutil.which("psql") is None:
        return None, "psql is not installed; the live Postgres suite needs the client package"
    dsn = (env.get(DSN_ENV) or "").strip()
    if not dsn:
        return None, (
            f"{DSN_ENV} is unset; point it at a throwaway server "
            "(CI does this with a postgres service container)")
    ok, why = _ping(dsn)
    if not ok:
        return None, f"{DSN_ENV} is set but unreachable: {why}"
    return dsn, "available"


def _ping(dsn: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["psql", "-X", "-q", "-tAc", "SELECT 1", dsn],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:   # pragma: no cover - environment
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip().splitlines()[-1:][0] if (
            proc.stderr or proc.stdout).strip() else f"psql exited {proc.returncode}"
    return True, "ok"


# ------------------------------------------------------------------------------------------------
# DSN handling
# ------------------------------------------------------------------------------------------------
def dsn_for_database(dsn: str, dbname: str) -> str:
    """Returns ``dsn`` repointed at ``dbname``, for URL and key/value connection strings."""
    if "://" in dsn:
        parts = urllib.parse.urlsplit(dsn)
        return urllib.parse.urlunsplit(parts._replace(path="/" + dbname))
    keep = [token for token in dsn.split()
            if token and not token.lower().startswith(("dbname=", "database="))]
    keep.append(f"dbname={dbname}")
    return " ".join(keep)


def redact(dsn: str) -> str:
    """The DSN with any password removed; safe to put in an assertion message or a log."""
    if "://" in dsn:
        parts = urllib.parse.urlsplit(dsn)
        if parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"{parts.username}:***@{host}" if parts.username else host
            parts = parts._replace(netloc=netloc)
        return urllib.parse.urlunsplit(parts)
    return " ".join("password=***" if token.lower().startswith("password=") else token
                    for token in dsn.split())


def scratch_name() -> str:
    return f"hear_test_{secrets.token_hex(8)}"


# ------------------------------------------------------------------------------------------------
# psql plumbing
# ------------------------------------------------------------------------------------------------
def psql(dsn: str, *args: str, timeout: int = DEFAULT_TIMEOUT_S, check: bool = True) -> str:
    """Runs one psql invocation and returns stdout. Raises PsqlError on a non-zero exit."""
    proc = subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", dsn, *args],
        capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise PsqlError(f"psql exited {proc.returncode} against {redact(dsn)}",
                        proc.stdout + proc.stderr)
    return proc.stdout


def psql_value(dsn: str, sql: str, timeout: int = DEFAULT_TIMEOUT_S) -> str:
    return psql(dsn, "-tAc", sql, timeout=timeout).strip()


class Session:
    """A psql process kept open, so two of them can hold overlapping transactions.

    SKIP LOCKED cannot be demonstrated from one connection: the point of the claim is what a
    *second* worker sees while the first one's transaction is still open. One psql process per
    simulated worker is the smallest way to get that without adding a driver.
    """

    def __init__(self, dsn: str, name: str = "session"):
        self.name = name
        self._dsn = dsn
        self._token = f"--sync-{secrets.token_hex(6)}--"
        self._proc = subprocess.Popen(
            ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", dsn],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)

    def execute(self, sql: str, timeout: int = DEFAULT_TIMEOUT_S) -> str:
        """Runs ``sql`` and returns its output. Raises PsqlError if the session died on it."""
        assert self._proc.stdin is not None and self._proc.stdout is not None
        if not sql.rstrip().endswith((";", "$$", "\\gexec")):
            sql = sql.rstrip() + ";"
        self._proc.stdin.write(f"{sql}\n\\echo {self._token}\n")
        self._proc.stdin.flush()
        lines: list[str] = []
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                self._proc.wait(timeout=timeout)
                raise PsqlError(f"{self.name}: psql session ended while running: {sql.strip()}",
                                "".join(lines))
            if line.strip() == self._token:
                return "".join(lines)
            lines.append(line)

    def value(self, sql: str) -> str:
        return self.execute(sql).strip()

    def close(self) -> None:
        if self._proc.poll() is None:
            with contextlib.suppress(Exception):
                assert self._proc.stdin is not None
                self._proc.stdin.write("\\q\n")
                self._proc.stdin.flush()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=30)
        if self._proc.poll() is None:            # pragma: no cover - only if psql wedges
            self._proc.kill()
            self._proc.wait(timeout=30)


class EphemeralDatabase:
    """A disposable database. Created empty, dropped unconditionally on exit."""

    def __init__(self, admin_dsn: str, name: str):
        if not SCRATCH_NAME_RE.match(name):
            raise ValueError(f"refusing to manage {name!r}: not a generated scratch name")
        self.admin_dsn = admin_dsn
        self.name = name
        self.dsn = dsn_for_database(admin_dsn, name)
        self._sessions: list[Session] = []

    # -- lifecycle ------------------------------------------------------------------------
    def create(self) -> "EphemeralDatabase":
        psql(self.admin_dsn, "-c", f'CREATE DATABASE "{self.name}"')
        return self

    def drop(self) -> None:
        for session in list(self._sessions):
            with contextlib.suppress(Exception):
                session.close()
        self._sessions.clear()
        if not SCRATCH_NAME_RE.match(self.name):   # pragma: no cover - constructor guards it
            raise ValueError(f"refusing to drop {self.name!r}")
        psql(self.admin_dsn, "-c",
             "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             f"WHERE datname = '{self.name}' AND pid <> pg_backend_pid()", check=False)
        psql(self.admin_dsn, "-c", f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')

    # -- running SQL ----------------------------------------------------------------------
    def sql(self, statement: str) -> str:
        return psql(self.dsn, "-c", statement)

    def value(self, query: str) -> str:
        return psql_value(self.dsn, query)

    def run_file(self, path: pathlib.Path, single_transaction: bool = False) -> str:
        args = ["--single-transaction"] if single_transaction else []
        return psql(self.dsn, *args, "-f", str(path))

    @contextlib.contextmanager
    def session(self, name: str = "session") -> Iterator[Session]:
        session = Session(self.dsn, name=name)
        self._sessions.append(session)
        try:
            yield session
        finally:
            with contextlib.suppress(Exception):
                session.close()
            if session in self._sessions:
                self._sessions.remove(session)

    # -- migrations -----------------------------------------------------------------------
    def apply_migrations(self, migrations: Optional[Sequence[PG.Migration]] = None,
                         record: bool = True) -> list[PG.Migration]:
        """Applies the real migration set, exactly as the operator runbook does."""
        migrations = PG.load_migrations() if migrations is None else migrations
        for migration in migrations:
            self.run_file(migration.path, single_transaction=True)
            if record:
                self.sql(PG.record_statement(migration))
        return list(migrations)

    def rollback_migrations(self, migrations: Optional[Sequence[PG.Migration]] = None) -> None:
        migrations = PG.load_migrations() if migrations is None else migrations
        for migration in reversed(list(migrations)):
            assert migration.rollback_path is not None, migration.path.name
            self.run_file(migration.rollback_path, single_transaction=True)

    def applied_versions(self) -> list[int]:
        out = self.value("SELECT coalesce(string_agg(version::text, ',' ORDER BY version), '') "
                         "FROM hear.schema_migrations")
        return [int(v) for v in out.split(",") if v]

    def schema_fingerprint(self) -> str:
        """A stable digest of the schema's shape: what a re-apply must not change.

        Columns, constraints, indexes, policies and function signatures -- everything a second
        application of an "idempotent" migration could quietly alter.
        """
        return self.value(_FINGERPRINT_SQL)


_FINGERPRINT_SQL = """
SELECT md5(string_agg(line, E'\n' ORDER BY line)) FROM (
    SELECT format('col %s.%s %s %s %s', c.relname, a.attname,
                  format_type(a.atttypid, a.atttypmod), a.attnotnull,
                  coalesce(pg_get_expr(d.adbin, d.adrelid), '')) AS line
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE n.nspname = 'hear' AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT format('con %s %s', conname, pg_get_constraintdef(oid))
      FROM pg_constraint WHERE connamespace = 'hear'::regnamespace
    UNION ALL
    SELECT format('idx %s', indexdef) FROM pg_indexes WHERE schemaname = 'hear'
    UNION ALL
    SELECT format('pol %s.%s %s', tablename, policyname, coalesce(qual, ''))
      FROM pg_policies WHERE schemaname = 'hear'
    UNION ALL
    SELECT format('fun %s %s', p.proname, pg_get_function_identity_arguments(p.oid))
      FROM pg_proc p WHERE p.pronamespace = 'hear'::regnamespace
    UNION ALL
    SELECT format('trg %s %s', tgname, pg_get_triggerdef(oid))
      FROM pg_trigger WHERE NOT tgisinternal
       AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'hear'::regnamespace)
) shape
"""


@contextlib.contextmanager
def ephemeral_database(admin_dsn: str, migrations: Optional[Sequence[PG.Migration]] = None,
                       apply: bool = False) -> Iterator[EphemeralDatabase]:
    """Creates a scratch database, yields it, and drops it however the block exits."""
    db = EphemeralDatabase(admin_dsn, scratch_name()).create()
    try:
        if apply:
            db.apply_migrations(migrations)
        yield db
    finally:
        db.drop()


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def self_test(admin_dsn: str) -> int:
    fixtures = [ROOT / "tests" / "fixtures" / "durable_pg_semantics.sql",
                ROOT / "tests" / "fixtures" / "durable_pg_store_parity.sql"]
    with ephemeral_database(admin_dsn) as db:
        print(f"scratch database {db.name} on {redact(admin_dsn)}")
        migrations = db.apply_migrations()
        print(f"applied {len(migrations)} migration(s); versions {db.applied_versions()}")
        before = db.schema_fingerprint()
        db.apply_migrations(migrations, record=False)
        after = db.schema_fingerprint()
        print(f"re-apply is a no-op: {before == after}")
        if before != after:
            return 1
        db.rollback_migrations(migrations)
        left = db.value("SELECT count(*) FROM pg_namespace WHERE nspname = 'hear'")
        print(f"rolled back; hear schemas left: {left}")
        if left != "0":
            return 1

    # Both fixtures count rows and health counters absolutely, so each one gets a database
    # nothing else has written to - the same isolation the pytest suite gives them.
    for fixture in fixtures:
        if not fixture.exists():
            continue
        with ephemeral_database(admin_dsn) as db:
            db.apply_migrations()
            out = db.run_file(fixture)
            print(f"{fixture.name}: {out.strip().splitlines()[-1] if out.strip() else 'ran'}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", help=f"admin DSN of a throwaway server (default: ${DSN_ENV})")
    ap.add_argument("--self-test", action="store_true",
                    help="create a scratch database, apply, re-apply, exercise, roll back, drop")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.dsn:
        env[DSN_ENV] = args.dsn
    dsn, why = availability(env)
    if dsn is None:
        print(f"unavailable: {why}", file=sys.stderr)
        return 2
    print(f"ok: {redact(dsn)} is reachable")
    if args.self_test:
        return self_test(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
