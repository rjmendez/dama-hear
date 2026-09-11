#!/usr/bin/env python3
"""Read a node's `dets.csv` at any of the five column layouts the firmware has written.

WHY THIS IS NOT `csv.DictReader`. Five generations of `dets.csv` exist on the cards and in the
drains, and one of them writes a header that does not describe its own rows:

    G1   9 cols   utc_us..frame_hex                          the 2026-09-07 capture
    G2  11 cols   + clip, clip_why
    G3  12 cols DECLARED, 11 WRITTEN  ⚠️ header says `node,` and the writer never emits it
    G4  12 cols   node_id, ...                               fs/layout build, node_id populated
    G5  13 cols   + sketch_back before frame_hex             the window fix

⚠️G3 IS THE WHOLE REASON THIS MODULE EXISTS. `csv.DictReader` on a G3 file silently shifts every
value one column left of its name: `utc_us` gets the node name, `uptime_s` gets the timestamp,
`frame_hex` gets the clip path. Nothing raises. Reading the 2026-09-07 drain by header name
returned 0 usable rows from 730 detections and printed no error -- the rows were there, the
decoder just never saw a frame where it looked. Dispatching on the DECLARED header and then
CHECKING the row width against it is what turns that into a counted refusal.

The discriminator between G3 and G4 is the first column name and it is exact: G3 says `node`,
G4 says `node_id`. The rename was made in firmware precisely to force the file to roll, so the
name change and the writer fix travel together and a file cannot be ambiguous between them.

WHAT IT REFUSES. An unknown header, a header-less file, and an empty file are all errors and all
say which. A row whose width does not match its file's generation is a counted skip carrying its
reason, never a silently short dict -- see `DetsRead.skips`. `hear/backend` and every corpus
consumer read counts off that object rather than off `len(rows)`, because a reader that cannot
say what it dropped is how 730 detections go missing twice.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

_BASE = ("utc_us", "uptime_s", "sample", "pps_n", "us_since_pps", "trigger", "flags", "fs_hz")


@dataclass(frozen=True)
class Generation:
    """One firmware-era column layout.

    `declared` is what the header line says. `written` is what the rows actually carry, and it is
    a SEPARATE field rather than a derived one because G3's two differ -- the only honest way to
    describe a writer that disagrees with its own header.
    """
    name: str
    declared: Tuple[str, ...]
    written: Tuple[str, ...]

    @property
    def broken_header(self) -> bool:
        return self.declared != self.written


G1 = Generation("G1", _BASE + ("frame_hex",), _BASE + ("frame_hex",))
G2 = Generation("G2", _BASE + ("frame_hex", "clip", "clip_why"),
                _BASE + ("frame_hex", "clip", "clip_why"))
# ⚠️declared carries `node`; written does not. Not a guess -- measured on both nodes' 2026-09-07
# drains, where every data row is 11 wide under a 12-wide header.
G3 = Generation("G3", ("node",) + _BASE + ("frame_hex", "clip", "clip_why"),
                _BASE + ("frame_hex", "clip", "clip_why"))
G4 = Generation("G4", ("node_id",) + _BASE + ("frame_hex", "clip", "clip_why"),
                ("node_id",) + _BASE + ("frame_hex", "clip", "clip_why"))
G5 = Generation("G5", ("node_id",) + _BASE + ("sketch_back", "frame_hex", "clip", "clip_why"),
                ("node_id",) + _BASE + ("sketch_back", "frame_hex", "clip", "clip_why"))

GENERATIONS: Tuple[Generation, ...] = (G1, G2, G3, G4, G5)
LATEST = G5

# One packed v1 sketch is 172 bytes; the column holds it as hex.
FRAME_HEX_LEN = 344


class UnknownSchema(ValueError):
    """The header matches no generation. Carries the header so the caller can add one."""


@dataclass
class DetsRead:
    """Rows, and everything that did not become a row.

    `skips` is a list of (line number, reason) and `counts` tallies reasons. Both are populated
    even on a fully clean file, so a caller can assert on them rather than on their absence.
    """
    generation: Generation
    rows: List[Dict[str, Any]] = field(default_factory=list)
    skips: List[Tuple[int, str]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    header: Tuple[str, ...] = ()

    def _skip(self, line: int, reason: str) -> None:
        self.skips.append((line, reason))
        self.counts[reason] = self.counts.get(reason, 0) + 1

    def summary(self) -> Dict[str, Any]:
        return {"generation": self.generation.name, "rows": len(self.rows),
                "skipped": len(self.skips), "reasons": dict(self.counts)}


def identify(header: Sequence[str]) -> Generation:
    """Which generation wrote this file, by its declared header. Exact match only."""
    h = tuple(c.strip() for c in header)
    for g in GENERATIONS:
        if h == g.declared:
            return g
    # A file whose header starts like a known generation but carries extra trailing columns is a
    # newer firmware than this module knows. Say that, rather than truncating to fit -- a column
    # inserted BEFORE frame_hex (which is exactly what G5 did to G4) would otherwise be read as a
    # trailing addition and shift the frame.
    for g in GENERATIONS:
        if h[:len(g.declared)] == g.declared:
            raise UnknownSchema(
                "header extends %s with %r. A column APPENDED is safe, but G5 inserted "
                "`sketch_back` BEFORE frame_hex, so extension cannot be assumed harmless: add "
                "the generation to hear/detsfile.py rather than reading this file blind."
                % (g.name, list(h[len(g.declared):])))
    raise UnknownSchema("dets.csv header %r matches no known generation (%s)"
                        % (list(h), ", ".join(g.name for g in GENERATIONS)))


def read_text(text: str, default_node: Optional[str] = None) -> DetsRead:
    """Parse a dets.csv's text. Never returns [] for a file that had no header.

    `default_node` names the node for G1-G3, which do not carry one. It is recorded as
    `node_from` = "argument" so a later reader can tell a node the FILE named from one the
    operator asserted; getting that backwards is how a two-node corpus becomes a one-node corpus.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError(
            "dets.csv is empty: %d byte(s), no header and no rows. A file the node created but "
            "never wrote, and a file whose content was lost, both read like this; neither is a "
            "quiet period. An empty capture still carries its header." % len(text))
    header = tuple(c.strip() for c in next(csv.reader([lines[0]])))
    gen = identify(header)
    out = DetsRead(generation=gen, header=header)
    width = len(gen.written)

    for n, row in enumerate(csv.reader(lines[1:]), start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) != width:
            out._skip(n, "row_width_%d_expected_%d" % (len(row), width))
            continue
        d = dict(zip(gen.written, (c.strip() for c in row)))
        fh = d.get("frame_hex", "")
        if not fh:
            out._skip(n, "no_frame")
            continue
        if len(fh) != FRAME_HEX_LEN:
            out._skip(n, "frame_hex_len_%d" % len(fh))
            continue
        if "node_id" in d and d["node_id"]:
            d["node"], d["node_from"] = d["node_id"], "file"
        else:
            if not default_node:
                out._skip(n, "no_node")
                continue
            d["node"], d["node_from"] = default_node, "argument"
        d["schema"] = gen.name
        # G1-G4 have no sketch_back column. Absent means "not stated", which is NOT the same as
        # the pre-fix value -- the pre-fix builds took the window at a `back` this file cannot
        # know. None travels; nothing here invents 736.
        d.setdefault("sketch_back", None)
        out.rows.append(d)
    return out


def read_file(path: str, default_node: Optional[str] = None) -> DetsRead:
    with open(path, "r", errors="replace") as fh:
        return read_text(fh.read(), default_node=default_node)
