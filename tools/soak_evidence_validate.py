#!/usr/bin/env python3
"""Offline validation of a Phase 2 soak evidence *series*, from collected snapshot files only.

    python3 tools/soak_evidence_validate.py ~/bridge-soak/T0-* ~/bridge-soak/T24h-*
    python3 tools/soak_evidence_validate.py --series-dir ~/bridge-soak --require-pass

`tools/bridge_soak_evidence.py` grades one sample against the cluster it just read. That is what a
collection tool can do; it is not what a day-1 / day-7 / day-14 review needs. A review has to be
reproducible months later, by someone who cannot read the cluster, from the snapshot files alone,
and it has to answer the questions that only a *series* can answer:

* were accepted records conserved from one snapshot to the next, or did some of them disappear?
* did pending ever fail to drain, and did cache failures grow after the baseline?
* did every one of the five in-soak nodes keep publishing, with Rankine excluded *and documented*
  rather than silently missing?
* did Redis heartbeat keys stay volatile and re-armed, instead of becoming persistent or vanishing?
* did the refusal quarantine stay inside its caps, on its own surface, without being mixed into the
  primary durable health verdict?
* is this one continuous SQLite ledger, or was it reset and relabelled?
* did the deployment, code ConfigMap digest and durable semantics stay fixed for the whole soak?

The last one is the one that quietly ruins soaks: a re-rollout during the window silently restarts
the semantics under an unchanged milestone label. The authoritative baseline is therefore pinned
here (`bridge_soak_evidence.SOAK_BASELINE_T0`, 2026-09-15T18:26:22Z, the corrected durable-outbox
semantics rollout) and every snapshot must declare it.

This module reads files. It never reads a cluster, a database, Redis or the network, and it never
writes anything except its own report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob as globlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import bridge_soak_evidence as B  # noqa: E402

SERIES_SCHEMA = "dama-hear/soak-evidence-series/v1"
SNAPSHOT_SCHEMA = "dama-hear/bridge-soak-evidence/v1"

# The reviews this module exists to make reproducible.
REVIEW_MILESTONES: Dict[str, str] = {"T0": "baseline", "T+24h": "day-1", "T+7d": "day-7",
                                     "T+14d": "day-14"}
MILESTONE_ORDER: Tuple[str, ...] = B.MILESTONES
# Hours after the authoritative baseline each milestone is nominally taken at.
MILESTONE_HOURS: Dict[str, float] = {"T0": 0.0, "T+24h": 24.0, "T+7d": 168.0, "T+14d": 336.0}
# A sample may sit this far from its nominal offset and still be that milestone.
TIMING_TOLERANCE_H: Dict[str, float] = {"T0": 1.0, "T+24h": 6.0, "T+7d": 24.0, "T+14d": 48.0}
# A snapshot is a point sample of a live writer: `pending` is telemetry Redis has not acknowledged
# *yet*. Zero is the expectation; this is the ceiling before it is called a backlog.
DEFAULT_MAX_PENDING = 0
# Fields whose change means the soak's semantics were replaced and the baseline is no longer valid.
DEPLOYMENT_IDENTITY: Tuple[str, ...] = ("image", "container", "strategy", "host_network",
                                        "generation", "observed_generation")
DURABLE_IDENTITY: Tuple[str, ...] = B.DURABLE_ENV_KEYS


class EvidenceError(RuntimeError):
    """A snapshot file could not be read, or is not a soak snapshot."""


@dataclass
class Check:
    name: str
    verdict: str  # "pass" | "fail" | "unknown"
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail}


@dataclass
class Sample:
    """One collected snapshot file, with the fields the series checks read."""

    path: str
    snapshot: Dict[str, Any]

    @property
    def milestone(self) -> str:
        return str(self.snapshot.get("milestone") or "?")

    @property
    def label(self) -> str:
        return "%s (%s)" % (self.milestone, REVIEW_MILESTONES.get(self.milestone, "unscheduled"))

    @property
    def captured_at(self) -> Optional[dt.datetime]:
        return B._parse_ts(self.snapshot.get("captured_at"))

    def block(self, name: str) -> Dict[str, Any]:
        value = self.snapshot.get(name)
        return dict(value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------- loading

def resolve_snapshot_path(path: str) -> str:
    """Accepts a snapshot.json, or the dated directory `bridge_soak_evidence` wrote it into."""
    resolved = os.path.expanduser(path)
    if os.path.isdir(resolved):
        return os.path.join(resolved, "snapshot.json")
    return resolved


def load_snapshot(path: str) -> Sample:
    resolved = resolve_snapshot_path(path)
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise EvidenceError("could not read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise EvidenceError("%s is not a snapshot object" % path)
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise EvidenceError("%s has schema %r, expected %r"
                            % (path, data.get("schema"), SNAPSHOT_SCHEMA))
    return Sample(resolved, data)


def discover(series_dir: str) -> List[str]:
    """Every snapshot.json under a collection directory, in filesystem order."""
    root = os.path.expanduser(series_dir)
    found = sorted(globlib.glob(os.path.join(root, "*", "snapshot.json")))
    found += sorted(p for p in globlib.glob(os.path.join(root, "snapshot.json")))
    return found


def load_series(paths: Sequence[str]) -> List[Sample]:
    """Loads and orders a series by capture time; an undated snapshot sorts last, not silently."""
    samples = [load_snapshot(p) for p in paths]
    far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    return sorted(samples, key=lambda s: (s.captured_at or far_future, s.path))


# ---------------------------------------------------------------- helpers

def _int(value: Any) -> Optional[int]:
    return B._as_int(value)


def _hours_between(start: Optional[dt.datetime], end: Optional[dt.datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 3600.0


def _pairs(samples: Sequence[Sample]) -> List[Tuple[Sample, Sample]]:
    return list(zip(samples, samples[1:]))


def _pair_label(before: Sample, after: Sample) -> str:
    return "%s -> %s" % (before.milestone, after.milestone)


def _pruned(before: Sample, after: Sample) -> bool:
    """True when the ledger's oldest row moved forward, i.e. retention pruning really happened.

    Records may only legitimately leave the ledger through the prune worker, and pruning always
    takes the oldest rows first. If the oldest row is unchanged, nothing was pruned, and a smaller
    record count means accepted telemetry was lost.
    """
    old_before = B._parse_ts(before.block("outbox").get("oldest_record_at"))
    old_after = B._parse_ts(after.block("outbox").get("oldest_record_at"))
    if old_before is None or old_after is None:
        return False
    return old_after > old_before


# ---------------------------------------------------------------- series checks

def check_baseline(samples: Sequence[Sample], baseline: str) -> List[Check]:
    """Every snapshot must name the same authoritative baseline, and T0 must be at it."""
    checks: List[Check] = []
    declared = {s.path: s.snapshot.get("soak_baseline") for s in samples}
    undeclared = sorted(os.path.basename(os.path.dirname(p)) or p
                        for p, v in declared.items() if not v)
    wrong = {os.path.basename(os.path.dirname(p)) or p: v
             for p, v in declared.items() if v and v != baseline}
    if undeclared and not wrong:
        checks.append(Check("baseline_declared", "unknown",
                            "snapshots without a recorded baseline: %s" % undeclared))
    elif wrong:
        checks.append(Check("baseline_declared", "fail",
                            "snapshots disagree with %s: %s" % (baseline, wrong)))
    else:
        checks.append(Check("baseline_declared", "pass",
                            "all %d snapshots declare %s" % (len(samples), baseline)))

    t0 = next((s for s in samples if s.milestone == "T0"), None)
    want = B._parse_ts(baseline)
    if t0 is None:
        checks.append(Check("baseline_sample_present", "fail",
                            "the series has no T0 snapshot to compare against"))
    elif t0.captured_at is None or want is None:
        checks.append(Check("baseline_sample_present", "unknown", "T0 has no capture time"))
    else:
        drift_h = abs((t0.captured_at - want).total_seconds()) / 3600.0
        checks.append(Check(
            "baseline_sample_present", "pass" if drift_h <= TIMING_TOLERANCE_H["T0"] else "fail",
            "T0 captured %s, %.2fh from the authoritative baseline %s"
            % (t0.snapshot.get("captured_at"), drift_h, baseline)))
    return checks


def check_milestones(samples: Sequence[Sample], baseline: str,
                     required: Sequence[str]) -> List[Check]:
    """The series must be one sample per scheduled milestone, each taken when it says it was."""
    checks: List[Check] = []
    seen = [s.milestone for s in samples]
    duplicates = sorted({m for m in seen if seen.count(m) > 1})
    unknown = sorted({m for m in seen if m not in MILESTONE_ORDER})
    missing = [m for m in required if m not in seen]
    if duplicates or unknown:
        checks.append(Check("milestone_labels_distinct", "fail",
                            "duplicated=%s unrecognised=%s" % (duplicates, unknown)))
    else:
        checks.append(Check("milestone_labels_distinct", "pass",
                            "milestones %s" % ", ".join(seen)))
    checks.append(Check("required_milestones_present", "fail" if missing else "pass",
                        "missing %s" % missing if missing
                        else "have %s" % ", ".join(required or seen)))

    want = B._parse_ts(baseline)
    off: Dict[str, str] = {}
    graded = 0
    for sample in samples:
        nominal = MILESTONE_HOURS.get(sample.milestone)
        elapsed = _hours_between(want, sample.captured_at)
        if nominal is None or elapsed is None:
            continue
        graded += 1
        if abs(elapsed - nominal) > TIMING_TOLERANCE_H.get(sample.milestone, 24.0):
            off[sample.milestone] = "%.1fh after baseline, expected %.0fh" % (elapsed, nominal)
    if not graded:
        checks.append(Check("milestone_timing", "unknown", "no sample carries a capture time"))
    else:
        checks.append(Check("milestone_timing", "fail" if off else "pass",
                            "mislabelled: %s" % off if off
                            else "%d samples sit at their nominal offsets" % graded))

    order_ok = all(a.captured_at and b.captured_at and a.captured_at < b.captured_at
                   for a, b in _pairs(samples))
    milestone_ok = [MILESTONE_ORDER.index(m) for m in seen if m in MILESTONE_ORDER]
    checks.append(Check(
        "series_is_chronological",
        "pass" if order_ok and milestone_ok == sorted(milestone_ok) else "fail",
        "capture times and milestone order agree" if order_ok else
        "snapshots are out of order once sorted by capture time"))
    return checks


def check_record_conservation(samples: Sequence[Sample]) -> List[Check]:
    """Accepted records may grow, and may only leave through a prune that advanced the window.

    Three separate losses are possible and each is checked: the total, the per-node totals, and the
    cumulative cache successes. A ledger that is smaller than it was, with an unchanged oldest row,
    is telemetry that was accepted and then lost -- the one outcome the durable outbox exists to
    make impossible.
    """
    checks: List[Check] = []
    internal: Dict[str, str] = {}
    for sample in samples:
        outbox = sample.block("outbox")
        records, pending = _int(outbox.get("records")), _int(outbox.get("pending"))
        succeeded = _int(outbox.get("succeeded"))
        if records is None or pending is None:
            continue
        if pending > records:
            internal[sample.milestone] = "pending %d exceeds records %d" % (pending, records)
        elif succeeded is not None and succeeded < records - pending:
            internal[sample.milestone] = (
                "%d succeeded attempts cannot account for %d acknowledged records"
                % (succeeded, records - pending))
    checks.append(Check("record_accounting_consistent", "fail" if internal else "pass",
                        "%s" % internal if internal
                        else "records = acknowledged + pending in every snapshot"))

    lost: Dict[str, str] = {}
    for before, after in _pairs(samples):
        # Retention pruning removes the oldest rows and the cache attempts attached to them, so a
        # pair that really pruned is allowed to be smaller. `ledger_not_restarted` is what stops a
        # replaced database from hiding behind that allowance.
        if _pruned(before, after):
            continue
        b_out, a_out = before.block("outbox"), after.block("outbox")
        b_rec, a_rec = _int(b_out.get("records")), _int(a_out.get("records"))
        if b_rec is not None and a_rec is not None and a_rec < b_rec:
            lost[_pair_label(before, after)] = (
                "records fell %d -> %d with no prune (oldest row unchanged)" % (b_rec, a_rec))
        b_ok, a_ok = _int(b_out.get("succeeded")), _int(a_out.get("succeeded"))
        if b_ok is not None and a_ok is not None and a_ok < b_ok:
            lost.setdefault(_pair_label(before, after),
                            "acknowledged records fell %d -> %d with no prune" % (b_ok, a_ok))
    checks.append(Check("accepted_records_conserved", "fail" if lost else "pass",
                        "%s" % lost if lost
                        else "no snapshot lost accepted records against its predecessor"))

    regressed: Dict[str, Any] = {}
    for before, after in _pairs(samples):
        if _pruned(before, after):
            continue  # a prune legitimately removes the oldest rows of every node
        b_counts = (before.block("coverage").get("counts") or {})
        a_counts = (after.block("coverage").get("counts") or {})
        shrunk = {node: [b_counts[node], a_counts.get(node, 0)] for node in b_counts
                  if (_int(a_counts.get(node)) or 0) < (_int(b_counts.get(node)) or 0)}
        if shrunk:
            regressed[_pair_label(before, after)] = shrunk
    checks.append(Check("per_node_records_conserved", "fail" if regressed else "pass",
                        "%s" % regressed if regressed
                        else "no node's record count went backwards"))
    return checks


def check_pending_and_failed(samples: Sequence[Sample],
                             max_pending: int = DEFAULT_MAX_PENDING) -> List[Check]:
    """Pending must drain at every sample; cache failures must not grow after the baseline."""
    checks: List[Check] = []
    backlog = {s.milestone: _int(s.block("outbox").get("pending"))
               for s in samples
               if (_int(s.block("outbox").get("pending")) or 0) > max_pending}
    unknown = [s.milestone for s in samples if _int(s.block("outbox").get("pending")) is None]
    if backlog:
        checks.append(Check("pending_drained_at_every_sample", "fail",
                            "pending above %d: %s" % (max_pending, backlog)))
    elif unknown and len(unknown) == len(samples):
        checks.append(Check("pending_drained_at_every_sample", "unknown",
                            "no snapshot reports pending"))
    else:
        checks.append(Check("pending_drained_at_every_sample", "pass",
                            "pending <= %d in all %d snapshots" % (max_pending, len(samples))))

    grew: Dict[str, str] = {}
    for before, after in _pairs(samples):
        b_failed = _int(before.block("outbox").get("failed"))
        a_failed = _int(after.block("outbox").get("failed"))
        if b_failed is None or a_failed is None:
            continue
        if a_failed > b_failed:
            grew[_pair_label(before, after)] = "failed %d -> %d" % (b_failed, a_failed)
    first_failed = _int(samples[0].block("outbox").get("failed")) if samples else None
    if grew:
        checks.append(Check("cache_failures_did_not_grow", "fail",
                            "%s (a replay that never succeeded is a durability defect)" % grew))
    else:
        checks.append(Check("cache_failures_did_not_grow", "pass",
                            "failed unchanged across the series (baseline %s)" % first_failed))

    receiver_backlog = {s.milestone: _int(s.block("receiver").get("pending_records"))
                        for s in samples
                        if s.block("receiver").get("available")
                        and (_int(s.block("receiver").get("pending_records")) or 0) > max_pending}
    graded = [s for s in samples if s.block("receiver").get("available")]
    if not graded:
        checks.append(Check("receiver_pending_drained", "unknown",
                            "no snapshot carries receiver health"))
    else:
        checks.append(Check("receiver_pending_drained", "fail" if receiver_backlog else "pass",
                            "%s" % receiver_backlog if receiver_backlog
                            else "receiver ledger drained in %d snapshots" % len(graded)))
    return checks


def check_coverage(samples: Sequence[Sample], expected: Sequence[str]) -> List[Check]:
    """Five-node coverage, with the excluded node documented rather than silently missing."""
    checks: List[Check] = []
    expected = list(expected)

    redefined = {s.milestone: s.block("coverage").get("expected")
                 for s in samples
                 if list(s.block("coverage").get("expected") or []) != expected}
    checks.append(Check("expectation_stable", "fail" if redefined else "pass",
                        "snapshots expect a different fleet than %s: %s" % (expected, redefined)
                        if redefined else "every snapshot expects the same %d nodes: %s"
                        % (len(expected), ", ".join(expected))))

    gaps = {s.milestone: s.block("coverage").get("missing")
            for s in samples if s.block("coverage").get("missing")}
    graded = [s for s in samples if s.block("coverage").get("counts")]
    if not graded:
        checks.append(Check("all_expected_nodes_covered", "unknown",
                            "no snapshot carries per-node counts"))
    else:
        checks.append(Check("all_expected_nodes_covered", "fail" if gaps else "pass",
                            "missing nodes: %s" % gaps if gaps
                            else "all %d nodes present in every snapshot" % len(expected)))

    undocumented: Dict[str, Any] = {}
    excluded_seen: Dict[str, str] = {}
    for sample in samples:
        coverage = sample.block("coverage")
        declared = coverage.get("excluded")
        if declared is None:
            undocumented[sample.milestone] = "records no exclusion list"
            continue
        for node, reason in declared.items():
            if not str(reason or "").strip():
                undocumented[sample.milestone] = "%s excluded without a reason" % node
            else:
                excluded_seen[node] = str(reason)
        overlap = coverage.get("excluded_overlap") or []
        if overlap:
            undocumented[sample.milestone] = "%s is both expected and excluded" % overlap
        unexpected = coverage.get("unexpected") or []
        if unexpected:
            undocumented[sample.milestone] = "undeclared device(s) in the ledger: %s" % unexpected
    if undocumented:
        checks.append(Check("exclusions_documented", "fail", "%s" % undocumented))
    elif not excluded_seen:
        checks.append(Check("exclusions_documented", "pass",
                            "no node is excluded; coverage is the whole fleet"))
    else:
        checks.append(Check("exclusions_documented", "pass",
                            "documented exclusions: %s"
                            % "; ".join("%s (%s)" % (k, v) for k, v in sorted(excluded_seen.items()))))

    exclusion_sets = {tuple(sorted((s.block("coverage").get("excluded") or {}))) for s in samples
                      if s.block("coverage").get("excluded") is not None}
    if len(exclusion_sets) > 1:
        checks.append(Check("exclusions_stable", "fail",
                            "the excluded set changed mid-soak: %s" % sorted(exclusion_sets)))
    else:
        checks.append(Check("exclusions_stable", "pass",
                            "excluded set constant: %s"
                            % (", ".join(sorted(next(iter(exclusion_sets), ()))) or "none")))
    return checks


def check_cache_ttl(samples: Sequence[Sample]) -> List[Check]:
    """Heartbeat keys must stay volatile, inside the TTL bound, and present while a node writes.

    Because a heartbeat key lives ~30 s, seeing one alive in snapshots taken days apart is the
    evidence that the cache path kept re-arming it. A key with TTL -1 has lost its expiry, which is
    an unbounded cache; TTL -2 on a node that is still writing to the ledger means the cache write
    stopped even though ingest continued.
    """
    graded = [s for s in samples if (s.block("cache").get("ttl_seconds") or {})]
    if not graded:
        return [Check("cache_ttl_volatile", "unknown",
                      "no snapshot carries Redis TTL evidence (the cache is password-protected "
                      "and the collector holds no credential; the ledger stays authoritative)"),
                Check("cache_ttl_rearmed", "unknown", "no snapshot carries Redis TTL evidence")]

    defects: Dict[str, Any] = {}
    for sample in graded:
        cache = sample.block("cache")
        present = sample.block("coverage").get("present") or []
        problems = {}
        if cache.get("persistent"):
            problems["lost_expiry"] = cache["persistent"]
        if cache.get("over_limit"):
            problems["ttl_above_%ss" % cache.get("ttl_limit_s")] = cache["over_limit"]
        writing_but_absent = sorted(set(cache.get("absent") or []) & set(present))
        if writing_but_absent:
            problems["absent_while_writing"] = writing_but_absent
        if problems:
            defects[sample.milestone] = problems
    checks = [Check("cache_ttl_volatile", "fail" if defects else "pass",
                    "%s" % defects if defects
                    else "every heartbeat key volatile and within its TTL bound in %d snapshots"
                    % len(graded))]

    rearmed = sorted(set.intersection(*[set(s.block("cache").get("volatile") or [])
                                        for s in graded])) if graded else []
    span_h = _hours_between(graded[0].captured_at, graded[-1].captured_at)
    if len(graded) < 2 or not span_h:
        checks.append(Check("cache_ttl_rearmed", "unknown",
                            "a single sample cannot show a key being re-armed"))
    else:
        limit = _int(graded[-1].block("cache").get("ttl_limit_s")) or B.DEFAULT_HEARTBEAT_TTL_S
        ok = bool(rearmed) and span_h * 3600.0 > limit
        checks.append(Check("cache_ttl_rearmed", "pass" if ok else "fail",
                            "%d key(s) alive across %.1fh, far beyond the %ss TTL: %s"
                            % (len(rearmed), span_h, limit, ", ".join(rearmed)) if ok
                            else "no heartbeat key survived the whole series; the cache write "
                                 "path stopped re-arming"))
    return checks


def check_refusals(samples: Sequence[Sample]) -> List[Check]:
    """The refusal quarantine is capped, and lives beside the durable verdict, never inside it."""
    graded = [s for s in samples if s.block("refusals").get("available")]
    if not graded:
        return [Check("refusal_caps_hold", "unknown", "no snapshot carries the refusal surface"),
                Check("refusals_separate_from_health", "unknown",
                      "no snapshot carries the refusal surface")]
    over: Dict[str, str] = {}
    for sample in graded:
        refusals = sample.block("refusals")
        refused, cap = _int(refusals.get("refused_messages")), _int(refusals.get("max_rows"))
        if refused is None or cap is None:
            continue
        if cap > 0 and refused > cap:
            over[sample.milestone] = "%d refusals stored against a %d-row cap" % (refused, cap)
    checks = [Check("refusal_caps_hold", "fail" if over else "pass",
                    "%s" % over if over
                    else "refusals within their row cap in %d snapshots (evicted %s, suppressed %s)"
                    % (len(graded), graded[-1].block("refusals").get("evicted_refusals"),
                       graded[-1].block("refusals").get("suppressed_refusals")))]

    # Refused input never reached the outbox, so refusal volume must not move the durable numbers:
    # a snapshot with refusals and a drained ledger is exactly the shape that proves the surfaces
    # are separate.
    mixed: Dict[str, str] = {}
    for sample in graded:
        refusals, receiver = sample.block("refusals"), sample.block("receiver")
        if not refusals.get("separate_from_durable_health"):
            mixed[sample.milestone] = "refusal counters are not reported beside durable_store"
            continue
        refused = _int(refusals.get("refused_messages")) or 0
        pending = _int(receiver.get("pending_records"))
        failures = _int(receiver.get("cache_failures"))
        if refused and (pending or failures):
            mixed[sample.milestone] = (
                "%d refusals coincide with pending=%s failures=%s; refused input must not enter "
                "the durable path" % (refused, pending, failures))
    checks.append(Check("refusals_separate_from_health", "fail" if mixed else "pass",
                        "%s" % mixed if mixed
                        else "refusal counters stay on their own surface and never entered the "
                             "durable verdict"))
    return checks


def check_durable_continuity(samples: Sequence[Sample]) -> List[Check]:
    """One uninterrupted SQLite ledger: same file, same journal, same schema, never restarted."""
    checks: List[Check] = []

    def distinct(getter) -> Dict[str, Any]:
        return {s.milestone: getter(s) for s in samples if getter(s) is not None}

    paths = distinct(lambda s: s.block("outbox").get("path"))
    journals = distinct(lambda s: s.block("outbox").get("journal_mode"))
    schemas = distinct(lambda s: tuple(s.block("outbox").get("schema_versions") or ()) or None)
    tables = distinct(lambda s: tuple(sorted(s.block("outbox").get("tables") or ())) or None)
    drift = {name: values for name, values in
             (("db path", paths), ("journal mode", journals), ("schema versions", schemas),
              ("tables", tables))
             if len(set(values.values())) > 1}
    if not (paths or journals or schemas or tables):
        checks.append(Check("ledger_identity_stable", "unknown", "no outbox evidence in series"))
    else:
        checks.append(Check("ledger_identity_stable", "fail" if drift else "pass",
                            "changed mid-soak: %s" % drift if drift
                            else "path %s, journal %s, schema %s constant"
                            % (next(iter(paths.values()), "?"),
                               next(iter(journals.values()), "?"),
                               next(iter(schemas.values()), "?"))))

    resets: Dict[str, str] = {}
    for before, after in _pairs(samples):
        b_out, a_out = before.block("outbox"), after.block("outbox")
        b_new = B._parse_ts(b_out.get("newest_record_at"))
        a_old = B._parse_ts(a_out.get("oldest_record_at"))
        b_old = B._parse_ts(b_out.get("oldest_record_at"))
        if b_old is not None and a_old is not None and b_new is not None and a_old > b_new:
            resets[_pair_label(before, after)] = (
                "the ledger's oldest row (%s) is newer than everything the previous snapshot held "
                "(%s): the database was replaced, not pruned"
                % (a_out.get("oldest_record_at"), b_out.get("newest_record_at")))
    checks.append(Check("ledger_not_restarted", "fail" if resets else "pass",
                        "%s" % resets if resets
                        else "the record window advances continuously"))

    volumes = {s.milestone: (s.block("pvc").get("volume"), s.block("pvc").get("storage_class"))
               for s in samples if s.block("pvc").get("available")}
    unbound = sorted(m for m, s in ((s.milestone, s) for s in samples)
                     if s.block("pvc").get("available") and not s.block("pvc").get("bound"))
    if not volumes:
        checks.append(Check("state_volume_stable", "unknown", "no PVC evidence in series"))
    elif len(set(volumes.values())) > 1 or unbound:
        checks.append(Check("state_volume_stable", "fail",
                            "volumes %s unbound %s" % (volumes, unbound)))
    else:
        checks.append(Check("state_volume_stable", "pass",
                            "bound to %s throughout" % (next(iter(volumes.values()))[0],)))

    restarts = {s.milestone: _int(s.block("pod").get("restarts"))
                for s in samples if s.block("pod").get("available")}
    if not restarts:
        checks.append(Check("writer_never_restarted", "unknown", "no pod evidence in series"))
    else:
        bad = {m: r for m, r in restarts.items() if r}
        checks.append(Check("writer_never_restarted", "fail" if bad else "pass",
                            "restarts %s" % bad if bad
                            else "restart count 0 in all %d snapshots" % len(restarts)))
    return checks


def check_digest_stability(samples: Sequence[Sample]) -> List[Check]:
    """A changed image, ConfigMap digest or durable env means the semantics were replaced.

    That is exactly how a soak silently resets: the milestone labels keep counting while the thing
    being soaked has been swapped underneath them. When this fails, the finding is not "fix the
    tool" -- it is "record a new baseline and start the soak again".
    """
    checks: List[Check] = []
    digests = {s.milestone: s.block("configmap").get("checksum_sha256")
               for s in samples if s.block("configmap").get("available")}
    known = {m: d for m, d in digests.items() if d}
    if not known:
        checks.append(Check("code_digest_stable", "unknown", "no ConfigMap digest in series"))
    elif len(set(known.values())) > 1:
        checks.append(Check("code_digest_stable", "fail",
                            "the bridge code ConfigMap changed mid-soak: %s; the baseline is void and "
                            "a new baseline must be recorded before the soak restarts" % known))
    else:
        checks.append(Check("code_digest_stable", "pass",
                            "sha256 %s across %d snapshots"
                            % (next(iter(known.values()))[:16], len(known))))

    identity: Dict[str, Dict[str, Any]] = {}
    for field in DEPLOYMENT_IDENTITY:
        values = {s.milestone: s.block("deployment").get(field)
                  for s in samples if s.block("deployment").get("available")}
        if values and len({json.dumps(v, sort_keys=True, default=str)
                           for v in values.values()}) > 1:
            identity[field] = values
    if not any(s.block("deployment").get("available") for s in samples):
        checks.append(Check("deployment_identity_stable", "unknown",
                            "no deployment evidence in series"))
    else:
        checks.append(Check("deployment_identity_stable", "fail" if identity else "pass",
                            "changed mid-soak: %s" % identity if identity
                            else "image, strategy, host network and generation unchanged"))

    durable: Dict[str, Dict[str, Any]] = {}
    for key in DURABLE_IDENTITY:
        values = {s.milestone: (s.block("deployment").get("durable_env") or {}).get(key)
                  for s in samples if s.block("deployment").get("available")}
        if values and len(set(map(str, values.values()))) > 1:
            durable[key] = values
    drifted = {s.milestone: s.block("deployment").get("network_drift")
               for s in samples if s.block("deployment").get("network_drift")}
    if not any(s.block("deployment").get("available") for s in samples):
        checks.append(Check("durable_semantics_stable", "unknown",
                            "no deployment evidence in series"))
    elif durable or drifted:
        checks.append(Check("durable_semantics_stable", "fail",
                            "durable env drift %s; network drift %s" % (durable, drifted)))
    else:
        checks.append(Check("durable_semantics_stable", "pass",
                            "durable env and plaintext MQTT identity unchanged"))
    return checks


# ---------------------------------------------------------------- validation entry point

def validate(samples: Sequence[Sample], *, baseline: str = B.SOAK_BASELINE_T0,
             expected_nodes: Sequence[str] = B.EXPECTED_NODES,
             required_milestones: Sequence[str] = (),
             max_pending: int = DEFAULT_MAX_PENDING) -> Dict[str, Any]:
    """Grades a whole series from its snapshot files. Reads nothing else."""
    if not samples:
        return {"schema": SERIES_SCHEMA, "verdict": "incomplete", "samples": [],
                "checks": [Check("series_supplied", "fail",
                                 "no snapshot files were given").as_dict()],
                "failed": ["series_supplied"], "unknown": []}
    checks: List[Check] = []
    checks += check_baseline(samples, baseline)
    checks += check_milestones(samples, baseline, list(required_milestones))
    checks += check_record_conservation(samples)
    checks += check_pending_and_failed(samples, max_pending)
    checks += check_coverage(samples, expected_nodes)
    checks += check_cache_ttl(samples)
    checks += check_refusals(samples)
    checks += check_durable_continuity(samples)
    checks += check_digest_stability(samples)

    failed = [c.name for c in checks if c.verdict == "fail"]
    unknown = [c.name for c in checks if c.verdict == "unknown"]
    return {
        "schema": SERIES_SCHEMA,
        "baseline": baseline,
        "expected_nodes": list(expected_nodes),
        "validated_at": B.utc_now_iso(),
        "samples": [{"milestone": s.milestone, "review": REVIEW_MILESTONES.get(s.milestone),
                     "captured_at": s.snapshot.get("captured_at"), "path": s.path}
                    for s in samples],
        "checks": [c.as_dict() for c in checks],
        "failed": failed,
        "unknown": unknown,
        "verdict": "fail" if failed else ("incomplete" if unknown else "pass"),
    }


def format_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Phase 2 soak evidence series -- %s" % result.get("verdict"),
        "",
        "baseline %s (authoritative corrected durable-outbox semantics rollout)"
        % result.get("baseline"),
        "expected nodes: %s" % ", ".join(result.get("expected_nodes") or []),
        "validated offline from snapshot files at %s; no cluster, cache or database was read"
        % result.get("validated_at"),
        "",
        "## samples",
    ]
    for sample in result.get("samples") or []:
        lines.append("%-6s %-8s %s  %s" % (sample.get("milestone"), sample.get("review") or "-",
                                           sample.get("captured_at"), sample.get("path")))
    lines += ["", "## checks"]
    for check in result.get("checks") or []:
        lines.append("%-7s %-32s %s" % (check.get("verdict"), check.get("name"),
                                        check.get("detail")))
    if result.get("failed"):
        lines += ["", "failed: %s" % ", ".join(result["failed"])]
    if result.get("unknown"):
        lines += ["", "unknown (evidence missing, not a pass): %s" % ", ".join(result["unknown"])]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Validate a collected Phase 2 soak evidence series offline, from snapshot "
                    "files only.")
    ap.add_argument("snapshots", nargs="*",
                    help="snapshot.json files, or the dated directories containing them")
    ap.add_argument("--series-dir",
                    help="collection directory to discover <milestone>-<stamp>/snapshot.json in")
    ap.add_argument("--baseline", default=B.SOAK_BASELINE_T0,
                    help="authoritative soak baseline every snapshot must declare")
    ap.add_argument("--expected-nodes", default=",".join(B.EXPECTED_NODES),
                    help="comma-separated device ids expected in every snapshot")
    ap.add_argument("--require-milestone", action="append", default=[], choices=MILESTONE_ORDER,
                    help="milestone the series must contain; repeatable")
    ap.add_argument("--review", choices=("day-1", "day-7", "day-14"),
                    help="shorthand for the milestones a scheduled review requires")
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING)
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--require-pass", action="store_true",
                    help="exit 2 unless every check passes (default reports only)")
    return ap


REVIEW_REQUIREMENTS: Dict[str, Tuple[str, ...]] = {
    "day-1": ("T0", "T+24h"),
    "day-7": ("T0", "T+24h", "T+7d"),
    "day-14": ("T0", "T+24h", "T+7d", "T+14d"),
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv or []))
    paths = [p for p in args.snapshots]
    if args.series_dir:
        paths += discover(args.series_dir)
    if not paths:
        print("no snapshot files given; pass paths or --series-dir", file=sys.stderr)
        return 1
    try:
        samples = load_series(paths)
    except EvidenceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    required = list(dict.fromkeys(list(args.require_milestone)
                                  + list(REVIEW_REQUIREMENTS.get(args.review or "", ()))))
    result = validate(samples, baseline=args.baseline,
                      expected_nodes=[n.strip() for n in args.expected_nodes.split(",")
                                      if n.strip()],
                      required_milestones=required, max_pending=args.max_pending)
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(result), end="")
    if args.require_pass and result["verdict"] != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
