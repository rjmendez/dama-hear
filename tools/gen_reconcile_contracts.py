#!/usr/bin/env python3
"""Generate the draft `hear.reconcile.receipt.v1` artifacts from `hear/ingest/reconcile.py`.

    python3 tools/gen_reconcile_contracts.py            # write
    python3 tools/gen_reconcile_contracts.py --check    # fail if the tree has drifted

The output is a JSON Schema, a fixture set and a manifest of expected outcomes, in the same
shape `contracts/` uses -- but under `docs/phase4-dual-write-reconciliation/`, because the
receipt is **not published yet**. `docs/decisions/0005-phase4-dual-write-observability.md`
records why (publishing a contract id regenerates the Phase 0 freeze baseline, which the
in-flight ingest lane owns) and states the promotion procedure, which is a `git mv` plus a
baseline regeneration and no content change.

The drift gate is the point: a staged artifact with no gate stops describing its source
just as silently as a published one would.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.ingest import reconcile as RC  # noqa: E402

OUT_DIR = ROOT / "docs" / "phase4-dual-write-reconciliation"
SCHEMA_PATH = OUT_DIR / (RC.RECEIPT_CONTRACT_ID + ".schema.json")
FIXTURE_DIR = OUT_DIR / "fixtures"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

RUN_ID = "recon-2026-09-15T00"
WINDOW_START = "2026-09-15T00:00:00Z"
WINDOW_END = "2026-09-15T01:00:00Z"
EMITTED_AT = "2026-09-15T01:00:30Z"
COMPARATOR = {"name": "hear-reconcile", "version": "0.0.0-design",
              "receipt_major": RC.RECEIPT_CONTRACT_MAJOR}

SITE = "site-alpha"
LEGACY_WRITER = "tools/hear_heartbeat_receiver.py"
CANONICAL_WRITER = "hear.ingest.batch"


def _correlation(device: str, *, legacy_uid: str = None, event_id: str = None,
                 path: str = "hear/heartbeat", source: str = "node-http") -> Dict[str, Any]:
    cid = RC.correlation_id(legacy_uid=legacy_uid, event_id=event_id, site_id=SITE,
                            device_id=device, telemetry_path=path)
    return {
        "correlation_id": cid,
        "binding": RC.binding_of(legacy_uid=legacy_uid, event_id=event_id),
        "legacy_uid": legacy_uid,
        "event_id": event_id,
        "site_id": SITE,
        "device_id": device,
        "telemetry_path": path,
        "source": source,
    }


def _projection(device: str, *, rows: int = 4210, seq: int = 17) -> Dict[str, Any]:
    """The explicit compared projection. Field paths only, never a whole stored row."""
    return {
        "telemetry_path": "hear/heartbeat",
        "telemetry_schema_version": 1,
        "device_id": device,
        "class": "esp32s3-i2s-gps",
        "fw_version": "hear-node-0.1.6",
        "clock.tier": "gps_pps",
        "gps.fix": 3,
        "time.valid": True,
        "counters.dets_rows_written": rows,
        "counters.scene_rows_written": rows * 2,
        "counters.clips_written": 11,
        "counters.clips_evicted": 0,
        "producer.batch_sequence": seq,
    }


def _side(device: str, *, outcome: str = "written", writer: str = LEGACY_WRITER,
          version: str = "0.1.6", projection: Mapping[str, Any] = None,
          rows: int = 1, principal: str = None, scope: str = "device",
          reasons: Sequence[str] = (), tier: str = "gps_pps",
          written_at: str = "2026-09-15T00:30:00Z") -> Dict[str, Any]:
    proj = _projection(device) if projection is None else projection
    return {
        "outcome": outcome,
        "row_count": rows,
        "state_hash": RC.state_hash(proj) if outcome == "written" else None,
        "written_at": written_at if outcome != "absent" else None,
        "observed_at": "2026-09-15T00:29:58Z" if tier == "gps_pps" else None,
        "clock_tier": tier,
        "writer": writer,
        "writer_version": version,
        "reasons": list(reasons),
        "principal_id": principal if principal is not None else ("node:" + device),
        "credential_scope": scope,
        "key_id": "k-2026-09",
    }


def _case(name: str, description: str, pair: Mapping[str, Any],
          correlation: Mapping[str, Any], *, differences: Sequence[RC.Difference] = (),
          evidence: Mapping[str, Any] = None,
          forced_reason: str = None) -> Dict[str, Any]:
    pair = dict(pair)
    pair["differences"] = list(differences)
    comparison = RC.classify(pair)
    sampling = RC.sample_receipt(comparison.classification,
                                 correlation["correlation_id"],
                                 forced_reason=forced_reason)
    record: Dict[str, Any] = {
        "name": name,
        "description": description,
        "classification": comparison.classification,
        "severity": comparison.severity,
        "repair_action": comparison.repair_action,
    }
    if comparison.classification in RC.NON_TERMINAL:
        record["receipt"] = None
        return record
    record["receipt"] = RC.build_receipt(
        run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END,
        emitted_at=EMITTED_AT, correlation=correlation, pair=pair,
        comparison=comparison, differences=differences, sampling=sampling,
        evidence=evidence or {}, comparator_build=COMPARATOR,
    )
    return record


def cases() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    # 1. Both sides wrote the same projection. Audit evidence, forced into the sample so the
    #    fixture set always contains a positive control.
    device = "gold"
    corr = _correlation(device, legacy_uid="a" * 64, event_id="11111111-1111-5111-8111-111111111111")
    out.append(_case(
        "match-both-sides-agree",
        "Legacy and canonical wrote the same projection with the same GPS-anchored clock.",
        {"legacy": _side(device), "canonical": _side(device, writer=CANONICAL_WRITER,
                                                     version="0.0.0-design"),
         "age_s": 12.0, "grace_s": RC.LIVE_GRACE_S},
        corr, forced_reason="first hour after enabling this source",
        evidence={"legacy_raw_ref": "raw/legacy/2026/09/15/gold-0001",
                  "canonical_raw_ref": "raw/canonical/2026/09/15/gold-0001"},
    ))

    # 2. Canonical has not landed yet and the grace window is open: a wait, not a finding.
    device = "kasami"
    corr = _correlation(device, legacy_uid="b" * 64)
    out.append(_case(
        "pending-inside-grace-window",
        "Legacy wrote 30 s ago and canonical has not landed. Below the live grace window "
        "this is scheduling, not loss, and no receipt is written.",
        {"legacy": _side(device), "canonical": {"outcome": "absent"},
         "age_s": 30.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 3. The same absence after the grace window: the loss this whole exercise exists for.
    out.append(_case(
        "missing-canonical-after-grace",
        "The same absence once the grace window has closed. Critical, and repairable by "
        "replaying the inbox -- never by writing the row by hand.",
        {"legacy": _side(device), "canonical": {"outcome": "absent"},
         "age_s": 900.0, "grace_s": RC.LIVE_GRACE_S},
        corr, evidence={"legacy_raw_ref": "raw/legacy/2026/09/15/kasami-0007"},
    ))

    # 4. A drain backlog replayed hours later is normal on the backfill grace window.
    device = "ageev"
    corr = _correlation(device, legacy_uid="c" * 64, source="import", path="hear/event")
    out.append(_case(
        "backfill-lag-inside-backfill-grace",
        "An SD backlog replayed six hours after the legacy row. On the backfill grace "
        "window this is expected; using the live window here would page every drain run.",
        {"legacy": _side(device), "canonical": {"outcome": "absent"},
         "age_s": 21_600.0, "grace_s": RC.BACKFILL_GRACE_S},
        corr,
    ))

    # 5. Two canonical rows for one authoritative identity.
    device = "nyquist"
    corr = _correlation(device, legacy_uid="d" * 64,
                        event_id="22222222-2222-5222-8222-222222222222")
    out.append(_case(
        "duplicate-canonical-rows",
        "Identity-based dedup failed: two canonical rows mirror one legacy record. Judged "
        "before any value comparison, because 'which value differs' has no answer here.",
        {"legacy": _side(device), "canonical": _side(device, writer=CANONICAL_WRITER,
                                                     rows=2),
         "age_s": 60.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 6. A compared counter disagrees.
    device = "mach"
    corr = _correlation(device, legacy_uid="e" * 64,
                        event_id="33333333-3333-5333-8333-333333333333")
    drifted = _projection(device, rows=4209)
    out.append(_case(
        "value-divergence-on-a-counter",
        "A monotonic counter disagrees by one row. Counters are compared exactly, so this "
        "is critical: a counter that drifts is a write the canonical path did not make.",
        {"legacy": _side(device),
         "canonical": _side(device, writer=CANONICAL_WRITER, projection=drifted),
         "age_s": 45.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
        differences=[RC.Difference("counters.dets_rows_written", 4210, 4209,
                                   comparator="count")],
    ))

    # 7. The two sides disagree about who sent it.
    device = "rankine"
    corr = _correlation(device, legacy_uid="f" * 64,
                        event_id="44444444-4444-5444-8444-444444444444")
    out.append(_case(
        "identity-divergence-forged-device",
        "The canonical row is attributed to a different authenticated principal. Judged "
        "first and never sampled away: a pair that is not about the same producer is not a "
        "pair, and every later comparison would be meaningless.",
        {"legacy": _side(device),
         "canonical": _side(device, writer=CANONICAL_WRITER, principal="node:gold"),
         "age_s": 20.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 8. An unanchored clock on both sides is compared as a tier, never as a value.
    device = "gold"
    corr = _correlation(device, legacy_uid="0" * 64,
                        event_id="55555555-5555-5555-8555-555555555555")
    unanchored = dict(_projection(device))
    unanchored["clock.tier"] = "wall"
    unanchored["time.valid"] = False
    out.append(_case(
        "unanchored-clock-compared-as-tier",
        "Neither side had a GPS fix. Both wrote a null observation instant and the tier "
        "`wall`; comparing the numbers would manufacture a mismatch out of two honest "
        "writers, so only the tier is compared.",
        {"legacy": _side(device, projection=unanchored, tier="wall"),
         "canonical": _side(device, writer=CANONICAL_WRITER, projection=unanchored,
                            tier="wall"),
         "age_s": 15.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 9. Legacy accepted, canonical durably refused.
    device = "kasami"
    corr = _correlation(device, legacy_uid="1" * 64,
                        event_id="66666666-6666-5666-8666-666666666666")
    out.append(_case(
        "outcome-divergence-canonical-refused",
        "Legacy stored the body and canonical wrote a durable refusal. Both records exist, "
        "so nothing was dropped -- but one of the two validators is wrong and an operator "
        "has to decide which.",
        {"legacy": _side(device),
         "canonical": _side(device, writer=CANONICAL_WRITER, outcome="refused",
                            reasons=["item_unrecognized"]),
         "age_s": 30.0, "grace_s": RC.LIVE_GRACE_S},
        corr, evidence={"canonical_raw_ref": "quarantine/2026/09/15/kasami-0031"},
    ))

    # 10. A canonical row with no authoritative counterpart.
    device = "ageev"
    corr = _correlation(device, event_id="77777777-7777-5777-8777-777777777777")
    out.append(_case(
        "canonical-orphan-no-legacy-row",
        "A canonical row exists that the authoritative path never wrote. Not data loss -- "
        "legacy still has everything it ever had -- but something wrote canonically outside "
        "the dual-writer, so the binding is a `canonical_orphan`.",
        {"legacy": {"outcome": "absent"},
         "canonical": _side(device, writer=CANONICAL_WRITER),
         "age_s": 120.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 11. A producer sequence gap that outlived the order grace window.
    device = "mach"
    corr = _correlation(device, legacy_uid="2" * 64,
                        event_id="88888888-8888-5888-8888-888888888888")
    pair = {"legacy": _side(device),
            "canonical": _side(device, writer=CANONICAL_WRITER),
            "age_s": 600.0, "grace_s": RC.LIVE_GRACE_S,
            "sequence_gap": True, "order_grace_s": RC.ORDER_GRACE_S}
    out.append(_case(
        "order-divergence-persistent-sequence-gap",
        "A `batch_sequence` gap inside one `boot_id` that did not close within the order "
        "grace window. Warn, not critical: late arrival is normal and only a gap that "
        "persists is evidence.",
        pair, corr,
    ))

    # 12. The comparator could not decide. Louder than wrong.
    device = "rankine"
    corr = _correlation(device, legacy_uid="3" * 64,
                        event_id="99999999-9999-5999-8999-999999999999")
    out.append(_case(
        "unclassified-missing-state-hash",
        "One side published no projection digest, so no verdict is derivable. Classified "
        "`unclassified` at critical severity rather than quietly counted as a match.",
        {"legacy": _side(device),
         "canonical": dict(_side(device, writer=CANONICAL_WRITER), state_hash=None),
         "age_s": 60.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 13. A location-shaped field can never be quoted, whatever the allow list says.
    device = "gold"
    corr = _correlation(device, legacy_uid="4" * 64,
                        event_id="aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa")
    out.append(_case(
        "redaction-denies-location-and-credential-paths",
        "A survey position and a bearer credential differ between the sides. Both are "
        "recorded as digests: the deny list beats the allow list, so a receipt store can "
        "never become the place a real-world coordinate or a token was reintroduced.",
        {"legacy": _side(device),
         "canonical": _side(device, writer=CANONICAL_WRITER,
                            projection=_projection(device, rows=4211)),
         "age_s": 40.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
        differences=[
            RC.Difference("survey.node_latitude", "<legacy>", "<canonical>",
                          comparator="exact"),
            RC.Difference("auth.bearer_token", "<legacy>", "<canonical>",
                          comparator="exact"),
            RC.Difference("counters.dets_rows_written", 4210, 4211, comparator="count"),
        ],
    ))

    # 14. The canonical writer reported its own failure.
    device = "nyquist"
    corr = _correlation(device, legacy_uid="5" * 64)
    out.append(_case(
        "error-canonical-write-failed",
        "The canonical writer reported a failed write. Legacy stands and the site is "
        "unaffected, which is why this is warn rather than critical -- but it is repairable "
        "by replay and must not be left to the grace window to rediscover.",
        {"legacy": _side(device),
         "canonical": {"outcome": "errored", "writer": CANONICAL_WRITER,
                       "reasons": ["durable_write_failed"], "row_count": 0},
         "age_s": 5.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 15. The authoritative writer reported its own failure. Always critical: the site's
    #     source of truth failed, and that is true whether or not dual-write is enabled.
    device = "gold"
    corr = _correlation(device, legacy_uid="6" * 64)
    out.append(_case(
        "error-legacy-write-failed",
        "The authoritative writer reported a failed write. Dual-write did not cause it and "
        "cannot mask it; the comparator surfaces it because it is the one failure that is "
        "already data loss.",
        {"legacy": {"outcome": "errored", "writer": LEGACY_WRITER, "row_count": 0,
                    "reasons": ["durable_write_failed"]},
         "canonical": _side(device, writer=CANONICAL_WRITER),
         "age_s": 8.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 16. Two legacy rows for one canonical identity.
    device = "kasami"
    corr = _correlation(device, legacy_uid="7" * 64,
                        event_id="bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb")
    out.append(_case(
        "duplicate-legacy-rows",
        "Two authoritative rows map to one canonical identity -- the MQTT duplication the "
        "bridge already tolerates. Warn, not critical: legacy has always behaved this way "
        "and canonical collapsing them is the designed outcome, not a defect.",
        {"legacy": _side(device, rows=2),
         "canonical": _side(device, writer=CANONICAL_WRITER),
         "age_s": 50.0, "grace_s": RC.LIVE_GRACE_S},
        corr,
    ))

    # 17. Present on both sides, but the canonical row mirrors an older revision.
    device = "mach"
    corr = _correlation(device, legacy_uid="8" * 64,
                        event_id="cccccccc-cccc-5ccc-8ccc-cccccccccccc")
    out.append(_case(
        "stale-canonical-row",
        "Both rows exist and agree field by field, but the canonical row was written "
        "against an earlier revision of the legacy record. Replayable from the outbox; a "
        "stale row is not a lost row.",
        {"legacy": _side(device),
         "canonical": _side(device, writer=CANONICAL_WRITER,
                            written_at="2026-09-15T00:10:00Z"),
         "age_s": 1_800.0, "grace_s": RC.LIVE_GRACE_S, "stale_side": "canonical"},
        corr,
    ))

    # 18. The mirror image, during a backfill: the authoritative row is the older one.
    device = "nyquist"
    corr = _correlation(device, legacy_uid="9" * 64,
                        event_id="dddddddd-dddd-5ddd-8ddd-dddddddddddd", source="import")
    out.append(_case(
        "stale-legacy-row",
        "An import wrote the canonical row from a newer revision than the legacy row it "
        "mirrors. Warn and manual review only: the repair boundary is replay-only, and "
        "nothing in this design may rewrite an authoritative row to catch it up.",
        {"legacy": _side(device, written_at="2026-09-15T00:05:00Z"),
         "canonical": _side(device, writer=CANONICAL_WRITER),
         "age_s": 2_400.0, "grace_s": RC.BACKFILL_GRACE_S, "stale_side": "legacy"},
        corr,
    ))

    return out


def manifest(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "contract": RC.RECEIPT_CONTRACT_ID,
        "contract_major": RC.RECEIPT_CONTRACT_MAJOR,
        "status": "staged",
        "staged_reason": (
            "Publishing under contracts/ regenerates docs/data/phase0-freeze-contracts.v1.json, "
            "which the in-flight HTTPS batch ingest lane owns. Promotion is a move plus a "
            "baseline regeneration and no content change; see "
            "docs/decisions/0005-phase4-dual-write-observability.md."
        ),
        "generator": "tools/gen_reconcile_contracts.py",
        "source_of_truth": "hear/ingest/reconcile.py",
        "tolerances": {
            "live_grace_s": RC.LIVE_GRACE_S,
            "backfill_grace_s": RC.BACKFILL_GRACE_S,
            "order_grace_s": RC.ORDER_GRACE_S,
            "max_differences": RC.MAX_DIFFERENCES,
            "max_receipt_bytes": RC.MAX_RECEIPT_BYTES,
            "default_audit_denominator": RC.DEFAULT_AUDIT_DENOMINATOR,
        },
        "cases": [
            {
                "file": record["name"] + ".json",
                "description": record["description"],
                "expected_classification": record["classification"],
                "expected_severity": record["severity"],
                "expected_repair_action": record["repair_action"],
                "emits_receipt": record["receipt"] is not None,
            }
            for record in records
        ],
    }


def artifacts() -> Dict[str, str]:
    records = cases()
    out: Dict[str, str] = {
        _rel(SCHEMA_PATH): _dump(RC.receipt_schema_document()),
        _rel(MANIFEST_PATH): _dump(manifest(records)),
    }
    for record in records:
        body = {
            "case": record["name"],
            "description": record["description"],
            "expected_classification": record["classification"],
            "expected_severity": record["severity"],
            "expected_repair_action": record["repair_action"],
            "receipt": record["receipt"],
        }
        out[_rel(FIXTURE_DIR / (record["name"] + ".json"))] = _dump(body)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the tree differs from the generator")
    args = parser.parse_args(list(argv) if argv is not None else None)

    generated = artifacts()
    if args.check:
        problems: List[str] = []
        for rel, text in sorted(generated.items()):
            path = ROOT / rel
            if not path.is_file():
                problems.append("%s: missing" % rel)
            elif path.read_text() != text:
                problems.append("%s: differs from its generator" % rel)
        existing = {
            _rel(p) for p in sorted(OUT_DIR.rglob("*.json"))
        } if OUT_DIR.is_dir() else set()
        for rel in sorted(existing - set(generated)):
            problems.append("%s: not produced by the generator" % rel)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("rerun: python3 tools/gen_reconcile_contracts.py", file=sys.stderr)
            return 1
        print("reconcile artifacts current (%d files)" % len(generated))
        return 0

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for rel, text in sorted(generated.items()):
        (ROOT / rel).write_text(text)
    print("wrote %d files under %s" % (len(generated), _rel(OUT_DIR)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
