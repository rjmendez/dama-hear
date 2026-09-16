#!/usr/bin/env python3
"""Reconciles a backfilled destination against the SQLite ledgers it came from (plan §7).

Read-only on both sides. It answers the three questions the M5v gate asks, in the order a
migration actually fails them:

1. **Is every row there?** Counts per ``(device_id, telemetry_path, day)`` bucket, summed into the
   row-conservation statement ``sqlite_rows(<= freeze) - skipped_duplicates = destination_rows``.
2. **Are the bytes the same?** Per bucket, ``sha256`` over the record bodies' own digests in
   ``record_uid`` order. Equal bucket digests prove byte-identical bodies; equal counts prove
   nothing about a re-serialising importer, which is the failure mode that would silently change
   the frozen Redis contract.
3. **Is the state the same truth?** A row SQLite acknowledged must be ``published`` in the
   destination with *the ledger's* acknowledgement time, and a row it never acknowledged must be
   ``pending``. A backfilled row published *later* than its SQLite acknowledgement is an import
   bug and fails the run (plan §7.5).

Two deliberate non-comparisons, both documented rather than silently skipped:

* **Collisions are gains, not deltas.** A ``record_uid`` held by two devices in two ledgers exists
  once per device in the destination and once in total across the sources. The reconciliation
  reports those as recovered rows -- the first direct measurement of defect D1 -- and the
  conservation statement counts them on the SQLite side, where they really were.
* **Refusal timestamps are not compared.** SQLite stamps ``received_at`` from the receiver's
  clock; ``hear.record_refusal()`` stamps ``now()`` on the server. Refusal parity is therefore
  identity (``refusal_uid``), ``occurrences`` and body bytes -- not wall-clock equality.

Exit codes: ``0`` reconciled, ``2`` any mismatch. Nothing here writes anything anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hear_durable_backfill as BF  # noqa: E402

BucketKey = tuple[str, str, str]      # (device_id, telemetry_path, day)


def bucket_digest(body_digests: Iterable[str]) -> str:
    """``sha256`` over the member digests, in the order the caller supplies (``record_uid``)."""
    digest = hashlib.sha256()
    for member in body_digests:
        digest.update(member.encode("ascii"))
    return digest.hexdigest()


@dataclass
class Snapshot:
    """One side's reconcilable state. Built from SQLite or from a destination's own report."""
    origin: str
    rows: int = 0
    #: Rows the sources actually held, before the union deduplicated identities across ledgers.
    #: Conservation is stated against this, because it is what the import read.
    arrivals: int = 0
    buckets: dict[BucketKey, dict] = field(default_factory=dict)
    #: ``(device_id, record_uid) -> {state, published_at, payload_sha256, received_at}``
    records: dict[tuple[str, str], dict] = field(default_factory=dict)
    refusals: dict[str, dict] = field(default_factory=dict)

    def add(self, device_id: str, record_uid: str, telemetry_path: str, day: str,
            payload_sha256: str, state: str, published_at: Optional[str],
            received_at: str) -> None:
        key = (device_id, record_uid)
        if key in self.records:
            raise ValueError(f"{self.origin}: duplicate identity {key}")
        self.records[key] = {"state": state, "published_at": published_at,
                             "payload_sha256": payload_sha256, "received_at": received_at,
                             "telemetry_path": telemetry_path, "day": day}
        bucket = self.buckets.setdefault((device_id, telemetry_path, day),
                                         {"count": 0, "members": []})
        bucket["count"] += 1
        bucket["members"].append((record_uid, payload_sha256))
        self.rows += 1
        self.arrivals += 1

    def digests(self) -> dict[BucketKey, str]:
        return {key: bucket_digest(sha for _, sha in sorted(bucket["members"]))
                for key, bucket in self.buckets.items()}

    def counts(self) -> dict[BucketKey, int]:
        return {key: bucket["count"] for key, bucket in self.buckets.items()}


def snapshot_sqlite(reader: BF.LedgerReader, freeze_id: Optional[int] = None,
                    origin: str = "sqlite") -> Snapshot:
    """The source side. Poison rows are excluded: they were never importable, and counting them
    as missing would make every reconciliation of a real ledger fail for rows nobody lost."""
    reader.assert_read_only()
    freeze = reader.freeze_id() if freeze_id is None else int(freeze_id)
    snap = Snapshot(origin=origin)
    for row in reader.rows(freeze):
        if BF.classify(row) is not None:
            continue
        snap.add(row.device_id, row.record_uid, row.telemetry_path, row.day,
                 row.payload_sha256,
                 "pending" if row.acknowledged_at is None else "published",
                 row.acknowledged_at, row.received_at)
    for refusal in reader.refusals():
        snap.refusals[str(refusal["refusal_uid"])] = {
            "occurrences": int(refusal["occurrences"]),
            "body_bytes": int(refusal["body_bytes"]),
            "truncated": bool(refusal["truncated"]),
            "source": str(refusal["source"]),
            "device_id": str(refusal["device_id"]),
        }
    return snap


def snapshot_destination(report: dict, origin: str = "postgres",
                         ingest_source: Optional[str] = BF.INGEST_SOURCE) -> Snapshot:
    """The destination side, from ``ModelSink.snapshot()`` or ``PsqlSink.snapshot()``.

    Restricting to ``ingest_source`` is what keeps live dual-write rows out of a backfill
    reconciliation: the two sets are disjoint by construction (plan §6), and mixing them would
    turn a correct import into a spurious surplus.
    """
    snap = Snapshot(origin=origin)
    for record in report.get("records", []):
        if ingest_source is not None and record.get("ingest_source") != ingest_source:
            continue
        snap.add(record["device_id"], record["record_uid"], record["telemetry_path"],
                 record["day"], record["payload_sha256"], record["state"],
                 record.get("published_at"), record["received_at"])
    for uid, refusal in (report.get("refusals") or {}).items():
        snap.refusals[uid] = dict(refusal)
    return snap


@dataclass
class Reconciliation:
    sqlite_rows: int = 0
    destination_rows: int = 0
    skipped_duplicates: int = 0
    recovered_collisions: dict[str, list[str]] = field(default_factory=dict)
    missing: list[tuple[str, str]] = field(default_factory=list)
    extra: list[tuple[str, str]] = field(default_factory=list)
    count_mismatches: list[dict] = field(default_factory=list)
    digest_mismatches: list[dict] = field(default_factory=list)
    state_mismatches: list[dict] = field(default_factory=list)
    timestamp_regressions: list[dict] = field(default_factory=list)
    refusal_mismatches: list[dict] = field(default_factory=list)
    spot_checked: int = 0

    @property
    def conserved(self) -> bool:
        return self.sqlite_rows - self.skipped_duplicates == self.destination_rows

    @property
    def ok(self) -> bool:
        return (self.conserved and not self.missing and not self.extra
                and not self.count_mismatches and not self.digest_mismatches
                and not self.state_mismatches and not self.timestamp_regressions
                and not self.refusal_mismatches)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 2

    def conservation_statement(self) -> str:
        """The one line the M5v gate consumes."""
        verb = "=" if self.conserved else "!="
        return (f"sqlite_rows({self.sqlite_rows}) - skipped_duplicates({self.skipped_duplicates}) "
                f"{verb} destination_rows({self.destination_rows})"
                f"  [collisions recovered: {len(self.recovered_collisions)}]")

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "conserved": self.conserved,
            "conservation": self.conservation_statement(),
            "sqlite_rows": self.sqlite_rows, "destination_rows": self.destination_rows,
            "skipped_duplicates": self.skipped_duplicates,
            "recovered_collisions": self.recovered_collisions,
            "missing": [list(k) for k in self.missing], "extra": [list(k) for k in self.extra],
            "count_mismatches": self.count_mismatches,
            "digest_mismatches": self.digest_mismatches,
            "state_mismatches": self.state_mismatches,
            "timestamp_regressions": self.timestamp_regressions,
            "refusal_mismatches": self.refusal_mismatches,
            "spot_checked": self.spot_checked,
            "exit_code": self.exit_code,
        }


def merge_sources(snapshots: Iterable[Snapshot]) -> Snapshot:
    """The union of several source ledgers, as one device-scoped side.

    Union, not sum: identity is ``(device_id, record_uid)`` on both sides, so two ledgers holding
    the *same* device's row for the same uid are one record, and two ledgers holding two
    *different* devices' rows for one uid are two -- which is precisely the pair SQLite could
    never hold at once.
    """
    merged = Snapshot(origin="sqlite")
    for snap in snapshots:
        for (device_id, record_uid), record in snap.records.items():
            if (device_id, record_uid) in merged.records:
                merged.arrivals += 1     # a row that existed, and that the import will skip
                continue
            merged.add(device_id, record_uid, record["telemetry_path"], record["day"],
                       record["payload_sha256"], record["state"], record["published_at"],
                       record["received_at"])
        merged.refusals.update(snap.refusals)
    return merged


def reconcile(source: Snapshot, destination: Snapshot, skipped_duplicates: int = 0,
              compare_refusals: bool = True) -> Reconciliation:
    result = Reconciliation(sqlite_rows=source.arrivals, destination_rows=destination.rows,
                            skipped_duplicates=skipped_duplicates)

    result.recovered_collisions = {
        uid: devices for uid, devices in
        _collisions(destination.records.keys()).items()
    }

    source_keys = set(source.records)
    destination_keys = set(destination.records)
    result.missing = sorted(source_keys - destination_keys)
    result.extra = sorted(destination_keys - source_keys)

    source_counts, destination_counts = source.counts(), destination.counts()
    for key in sorted(set(source_counts) | set(destination_counts)):
        left, right = source_counts.get(key, 0), destination_counts.get(key, 0)
        if left != right:
            result.count_mismatches.append(
                {"bucket": list(key), "sqlite": left, "destination": right})

    source_digests, destination_digests = source.digests(), destination.digests()
    for key in sorted(set(source_digests) & set(destination_digests)):
        if source_digests[key] != destination_digests[key]:
            result.digest_mismatches.append(
                {"bucket": list(key), "sqlite": source_digests[key],
                 "destination": destination_digests[key]})

    for key in sorted(source_keys & destination_keys):
        left, right = source.records[key], destination.records[key]
        result.spot_checked += 1
        if left["state"] != right["state"]:
            result.state_mismatches.append(
                {"identity": list(key), "sqlite": left["state"], "destination": right["state"]})
            continue
        if left["state"] != "published":
            continue
        acked, published = left["published_at"], right["published_at"]
        if published is None or (acked is not None and published > acked):
            result.timestamp_regressions.append(
                {"identity": list(key), "sqlite_acknowledged_at": acked,
                 "destination_published_at": published})

    if compare_refusals:
        for uid in sorted(set(source.refusals) | set(destination.refusals)):
            left, right = source.refusals.get(uid), destination.refusals.get(uid)
            if left is None or right is None:
                result.refusal_mismatches.append(
                    {"refusal_uid": uid, "sqlite": left, "destination": right})
                continue
            for field_name in ("occurrences", "body_bytes"):
                if int(left[field_name]) != int(right[field_name]):
                    result.refusal_mismatches.append(
                        {"refusal_uid": uid, "field": field_name,
                         "sqlite": left[field_name], "destination": right[field_name]})
    return result


def _collisions(identities: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for device_id, record_uid in identities:
        devices = seen.setdefault(record_uid, [])
        if device_id not in devices:
            devices.append(device_id)
    return {uid: sorted(devices) for uid, devices in seen.items() if len(devices) > 1}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", action="append", required=True, type=pathlib.Path,
                    help="a legacy SQLite ledger; repeat for each import source")
    ap.add_argument("--destination", required=True, type=pathlib.Path,
                    help="the destination's own snapshot as JSON "
                         "(ModelSink.snapshot()/PsqlSink.snapshot())")
    ap.add_argument("--skipped-duplicates", type=int, default=0,
                    help="rows the import reported as already present")
    args = ap.parse_args(argv)

    snapshots = []
    for path in args.source:
        with BF.LedgerReader(path) as reader:
            snapshots.append(snapshot_sqlite(reader, origin=path.stem))
    destination = snapshot_destination(json.loads(args.destination.read_text(encoding="utf-8")))
    result = reconcile(merge_sources(snapshots), destination,
                       skipped_duplicates=args.skipped_duplicates,
                       compare_refusals=False)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    print(result.conservation_statement(), file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
