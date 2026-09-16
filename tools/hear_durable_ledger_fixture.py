#!/usr/bin/env python3
"""Builds synthetic legacy SQLite ledgers for the Phase 2 cut-over rehearsal.

The rehearsal (tools/hear_durable_cutover_rehearsal.py) has to be run against *a* ledger, and the
one thing it must never be run against is a real one: exporting live pool data is exactly the
operation the migration plan defers to M5, under an operator decision nobody has taken. So the
rehearsal builds its own source instead, here, from the shipping
``SqliteDurableRecordStore`` -- not from a hand-written CREATE TABLE. If the receiver's ledger
shape changes, this fixture changes with it, and a backfill written against a stale shape fails
in CI rather than in front of an operator.

What the corpus deliberately contains, because each one is a documented failure mode:

* **cached** records (a succeeded ``cache_attempts`` row) and **uncached** ones -- backfill must
  import the first as ``published`` with the ledger's own acknowledgement time and the second as
  ``pending`` (plan §6);
* a record whose only attempt **failed**, which is still pending truth, not published truth;
* a **cross-ledger uid collision**: the same ``record_uid`` held by two different devices in two
  different ledgers. One SQLite file cannot hold both (``UNIQUE(record_uid)``); Postgres keeps
  both because its identity is ``(tenant_id, device_id, record_uid)``, and that recovery is the
  first direct measurement of defect D1;
* a **mach-style old-firmware payload**: unknown fields, a non-ASCII site name, no clock state --
  the bodies a re-serialising importer would silently rewrite;
* **poison rows** written past the store's own API (malformed JSON body, and a body whose
  ``device_id`` disagrees with its column), because a ledger that has been running for months is
  not guaranteed to be clean and the importer's behaviour on those rows is a gate, not a guess;
* **refusals**, including a repeat that must bump ``occurrences`` rather than add a row.

Nothing here reads the environment, the network, or any path the caller did not pass in.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hear_heartbeat_receiver as HR  # noqa: E402

#: The two ledgers the migration plan names as import sources.
RECEIVER_LEDGER = "heartbeat-receiver"
BRIDGE_LEDGER = "mqtt-bridge"

#: A uid held by two devices, one per ledger. The legacy (v1) identity scheme made this possible;
#: the scoped (v2) scheme prevents new ones, and history keeps the old ones.
COLLIDING_UID = "0" * 64

DAY_ONE = "2026-09-01"
DAY_TWO = "2026-09-02"


@dataclass(frozen=True)
class FixtureRow:
    """One intended legacy row, before it is written."""
    device_id: str
    record_uid: str
    telemetry_path: str
    body: dict
    received_at: str
    cached_at: Optional[str] = None      # a succeeded cache_attempts row at this time
    failed_at: Optional[str] = None      # a failed cache_attempts row at this time
    poison: Optional[str] = None         # 'malformed_json' | 'device_mismatch'


@dataclass
class FixtureLedger:
    name: str
    path: pathlib.Path
    rows: list[FixtureRow] = field(default_factory=list)
    refusals: list[tuple] = field(default_factory=list)

    @property
    def importable_rows(self) -> list[FixtureRow]:
        return [row for row in self.rows if row.poison is None]


def body_for(device_id: str, uid: str, received_at: str, telemetry_path: str = "hear/heartbeat",
             **extra) -> dict:
    """A record body in exactly the shape the receiver persists."""
    record = {
        "device_id": device_id,
        "telemetry_path": telemetry_path,
        "idempotency_key": f"{device_id}-{uid[:8]}",
        "received_at": received_at,
        "receiver_schema_version": HR.RECEIVER_SCHEMA_VERSION,
    }
    record.update(extra)
    return record


def _at(day: str, hour: int, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00Z"


def receiver_rows() -> list[FixtureRow]:
    rows = [
        # Two published days for one device: the reconciliation buckets are (device, path, day),
        # so a ledger that only ever covered one day would not exercise the grouping at all.
        FixtureRow("node-alpha", "a" * 64, "hear/heartbeat",
                   body_for("node-alpha", "a" * 64, _at(DAY_ONE, 1), uptime_s=61),
                   _at(DAY_ONE, 1), cached_at=_at(DAY_ONE, 1)),
        FixtureRow("node-alpha", "b" * 64, "hear/heartbeat",
                   body_for("node-alpha", "b" * 64, _at(DAY_ONE, 2), uptime_s=122),
                   _at(DAY_ONE, 2), cached_at=_at(DAY_ONE, 2)),
        FixtureRow("node-alpha", "c" * 64, "hear/heartbeat",
                   body_for("node-alpha", "c" * 64, _at(DAY_TWO, 3), uptime_s=183),
                   _at(DAY_TWO, 3), cached_at=_at(DAY_TWO, 3)),
        # Never acknowledged by Redis: the outbox's whole reason to exist. Must import pending.
        FixtureRow("node-beta", "d" * 64, "hear/heartbeat",
                   body_for("node-beta", "d" * 64, _at(DAY_TWO, 4), uptime_s=7),
                   _at(DAY_TWO, 4)),
        # Attempted and failed. Still pending truth.
        FixtureRow("node-beta", "e" * 64, "hear/event",
                   body_for("node-beta", "e" * 64, _at(DAY_TWO, 5),
                            telemetry_path="hear/event", event="clip_written"),
                   _at(DAY_TWO, 5), failed_at=_at(DAY_TWO, 5)),
        # Old firmware, as it actually arrived: unknown keys, non-ASCII, no clock state.
        FixtureRow("node-mach", "f" * 64, "hear/heartbeat",
                   body_for("node-mach", "f" * 64, _at(DAY_ONE, 6),
                            site="Cañada del Oro \u2014 north", legacy_mach_frame=True,
                            mach_scene=[1, 2, 3], note="tab\there \"quoted\" \\ backslash"),
                   _at(DAY_ONE, 6), cached_at=_at(DAY_ONE, 6)),
        # The collision's first device. The bridge ledger holds the same uid for another device.
        FixtureRow("node-alpha", COLLIDING_UID, "hear/heartbeat",
                   body_for("node-alpha", COLLIDING_UID, _at(DAY_ONE, 7), uptime_s=900),
                   _at(DAY_ONE, 7), cached_at=_at(DAY_ONE, 7)),
        # Written past the store's API, as months of history can be.
        FixtureRow("node-beta", "1" * 64, "hear/heartbeat",
                   body_for("node-beta", "1" * 64, _at(DAY_TWO, 8)),
                   _at(DAY_TWO, 8), poison="malformed_json"),
        FixtureRow("node-beta", "2" * 64, "hear/heartbeat",
                   body_for("node-gamma", "2" * 64, _at(DAY_TWO, 9)),
                   _at(DAY_TWO, 9), poison="device_mismatch"),
    ]
    return rows


def bridge_rows() -> list[FixtureRow]:
    return [
        FixtureRow("node-gamma", "9" * 64, "hear/heartbeat",
                   body_for("node-gamma", "9" * 64, _at(DAY_ONE, 11), uptime_s=42),
                   _at(DAY_ONE, 11), cached_at=_at(DAY_ONE, 11)),
        FixtureRow("node-gamma", "8" * 64, "hear/heartbeat",
                   body_for("node-gamma", "8" * 64, _at(DAY_TWO, 12), uptime_s=84),
                   _at(DAY_TWO, 12)),
        # The same device's same record, seen by both ledgers: a genuine duplicate. The import
        # must skip exactly this one, and the row-conservation statement subtracts exactly it.
        FixtureRow("node-alpha", "a" * 64, "hear/heartbeat",
                   body_for("node-alpha", "a" * 64, _at(DAY_ONE, 1), uptime_s=61),
                   _at(DAY_ONE, 1), cached_at=_at(DAY_ONE, 1)),
        # The collision's second device: one uid, two devices, two ledgers.
        FixtureRow("node-delta", COLLIDING_UID, "hear/heartbeat",
                   body_for("node-delta", COLLIDING_UID, _at(DAY_ONE, 13), uptime_s=13),
                   _at(DAY_ONE, 13), cached_at=_at(DAY_ONE, 13)),
    ]


REFUSALS = [
    ("hear/heartbeat", "node-beta", "lan_http", "device id must be a non-empty string",
     '{"device_id": ""}'),
    # The same refusal twice: occurrences must be 2 and the row count must stay 1.
    ("hear/heartbeat", "node-beta", "lan_http", "device id must be a non-empty string",
     '{"device_id": ""}'),
    ("hear/event", "unknown", "mqtt_bridge", "body must be an object", "not json at all"),
]


def _write_row(store: HR.SqliteDurableRecordStore, row: FixtureRow) -> None:
    body_json = HR.encode_json(row.body)
    store.persist(row.record_uid, row.body, body_json)
    if row.cached_at is not None:
        _attempt(store.path, row.record_uid, "succeeded", row.cached_at)
    if row.failed_at is not None:
        _attempt(store.path, row.record_uid, "failed", row.failed_at)


def _attempt(path: str, record_uid: str, outcome: str, created_at: str) -> None:
    """Writes one cache_attempts row with a chosen timestamp.

    ``note_cache_success()`` stamps *now*, and a fixture whose acknowledgement times are all "now"
    cannot show that backfill preserves the ledger's own acknowledgement time -- which is the
    property plan §7.5 fails a run on.
    """
    con = sqlite3.connect(path)
    try:
        con.execute("INSERT INTO cache_attempts (record_uid, cache_target, outcome, created_at) "
                    "VALUES (?, 'redis', ?, ?)", (record_uid, outcome, created_at))
        con.commit()
    finally:
        con.close()


def _write_poison(path: str, row: FixtureRow) -> None:
    """Inserts a row the store's own API would never produce."""
    payload = HR.encode_json(row.body)
    if row.poison == "malformed_json":
        payload = payload[:-1] + ","      # truncated object: parses nowhere
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO durable_records (record_uid, telemetry_path, device_id, "
            "idempotency_key, payload_json, received_at, receiver_schema_version, created_at, "
            "durable_schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.record_uid, row.telemetry_path, row.device_id,
             row.body.get("idempotency_key"), payload, row.received_at,
             HR.RECEIVER_SCHEMA_VERSION, row.received_at, HR.DURABLE_SCHEMA_VERSION))
        con.commit()
    finally:
        con.close()


def build_ledger(path: pathlib.Path, name: str, rows: list[FixtureRow],
                 refusals: list[tuple] = ()) -> FixtureLedger:
    # Rebuilt from scratch every time: a fixture that accumulated rows across runs would make
    # the rehearsal's counts depend on how often it had been run before.
    for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        leftover.unlink(missing_ok=True)
    store = HR.SqliteDurableRecordStore(str(path))
    for row in rows:
        if row.poison is None:
            _write_row(store, row)
        else:
            _write_poison(str(path), row)
    for refusal in refusals:
        store.record_refusal(*refusal)
    return FixtureLedger(name=name, path=path, rows=list(rows), refusals=list(refusals))


def build_corpus(directory: pathlib.Path) -> dict[str, FixtureLedger]:
    """The two-ledger corpus the rehearsal imports. Returns ``{ledger_name: FixtureLedger}``."""
    directory.mkdir(parents=True, exist_ok=True)
    return {
        RECEIVER_LEDGER: build_ledger(directory / "heartbeat-receiver.sqlite3", RECEIVER_LEDGER,
                                      receiver_rows(), REFUSALS),
        BRIDGE_LEDGER: build_ledger(directory / "mqtt-bridge.sqlite3", BRIDGE_LEDGER,
                                    bridge_rows()),
    }


def summarize(corpus: dict[str, FixtureLedger]) -> dict:
    out: dict = {"ledgers": {}, "colliding_uid": COLLIDING_UID}
    for name, ledger in corpus.items():
        rows = ledger.rows
        out["ledgers"][name] = {
            "path": str(ledger.path),
            "rows": len(rows),
            "importable": len(ledger.importable_rows),
            "cached": sum(1 for r in rows if r.cached_at and r.poison is None),
            "pending": sum(1 for r in rows if not r.cached_at and r.poison is None),
            "poison": sum(1 for r in rows if r.poison is not None),
            "refusals": len(ledger.refusals),
            "devices": sorted({r.device_id for r in rows}),
        }
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=pathlib.Path,
                    help="directory to build the fixture ledgers in (created if absent)")
    args = ap.parse_args(argv)
    corpus = build_corpus(args.out)
    print(json.dumps(summarize(corpus), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
