"""The run ledger: an append-only JSONL in the work dir that is the ONLY authority for resume.

⚠️THE LEDGER MAY LAG THE STORE BUT NEVER LEAD IT. Every row is written *after* the operation it
records returned success. A crash between a pointer commit and its ledger row therefore leaves a
task that will be re-attempted, and the re-attempt's conditional put answers "already there with
the same blob" -> recorded as `published` with `idempotent_replay: true`. Written the other way
round -- ledger first -- a crash would mark an object published that does not exist, and the next
run would skip it forever. That is the difference between a resumable import and a lost object.

⚠️RESUME READS THIS FILE, NOT THE BUCKET. Listing a bucket to find out what is done costs money,
is eventually consistent on several stores, and gets slower exactly as the import gets bigger. The
happy path performs no listing at all; `list_prefix` exists for the end-of-run cross-check (does
the ledger's published set equal what is in the store), where a disagreement is a hard failure and
not a warning.

⚠️A DEFERRAL IS COUNTED, NEVER SILENT. `hear/pool.py` says it plainly: a reader that could not say
what it dropped has twice returned a confident wrong answer here. Every task ends as published,
failed, or deferred with a reason, and the counters are part of the run close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Set

ROW_TYPES = frozenset({
    "run_open", "claim", "staged", "verified", "published", "failed", "deferred",
    "checkpoint", "run_close",
})


@dataclass
class ResumeState:
    """What a replay of the ledger says about a run, without touching the object store."""

    run_id: Optional[str] = None
    published: Set[str] = field(default_factory=set)
    failed_final: Set[str] = field(default_factory=set)
    deferred: Set[str] = field(default_factory=set)
    claimed: Set[str] = field(default_factory=set)
    counters: Dict[str, int] = field(default_factory=dict)
    closed: Optional[str] = None

    def is_done(self, task_id: str) -> bool:
        """Done means "this run will not attempt it again", which includes a final failure."""
        return task_id in self.published or task_id in self.failed_final


class RunLedger:
    """Append-only JSONL plus a tmp+replace checkpoint pointer, per the drain's durability rule."""

    def __init__(self, path: str, run_id: str) -> None:
        self.path = os.path.abspath(path)
        self.run_id = run_id
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.rows_written = 0

    def append(self, row_type: str, **fields: Any) -> Dict[str, Any]:
        if row_type not in ROW_TYPES:
            raise ValueError("unknown ledger row type: %r" % (row_type,))
        row = dict(fields)
        row["type"] = row_type
        row["run_id"] = self.run_id
        line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        with open(self.path, "a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self.rows_written += 1
        return row

    def checkpoint(self, counters: Dict[str, int], last_task_id: Optional[str]) -> None:
        self.append("checkpoint", counters=dict(counters), last_task_id=last_task_id)
        pointer = os.path.join(os.path.dirname(self.path), "checkpoint.json")
        tmp = pointer + ".tmp"
        doc = {"run_id": self.run_id, "counters": dict(counters), "last_task_id": last_task_id}
        with open(tmp, "w") as fh:
            fh.write(json.dumps(doc, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, pointer)

    def rows(self) -> Iterator[Dict[str, Any]]:
        yield from read_rows(self.path)


def read_rows(path: str) -> Iterator[Dict[str, Any]]:
    """Read complete rows only. A torn final line is the crash, not a parse error."""
    if not os.path.isfile(path):
        return
    with open(path, "rb") as fh:
        body = fh.read()
    cut = body.rfind(b"\n")
    if cut < 0:
        return
    for line in body[:cut].split(b"\n"):
        if not line.strip():
            continue
        yield json.loads(line.decode("utf-8"))


def replay(path: str) -> ResumeState:
    state = ResumeState()
    for row in read_rows(path):
        kind = row.get("type")
        state.run_id = row.get("run_id", state.run_id)
        task = row.get("task_id")
        if kind == "claim" and task:
            state.claimed.add(task)
        elif kind == "published" and task:
            state.published.add(task)
            state.deferred.discard(task)
            state.failed_final.discard(task)
        elif kind == "failed" and task and not row.get("retryable", False):
            state.failed_final.add(task)
        elif kind == "deferred" and task:
            state.deferred.add(task)
        elif kind == "checkpoint":
            state.counters = dict(row.get("counters", {}))
        elif kind == "run_close":
            state.closed = row.get("outcome")
    return state
