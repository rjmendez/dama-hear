#!/usr/bin/env python3
"""Read a node's `scene.csv` -- the CONTINUOUS descriptor, not the gated one.

⚠️THIS IS THE CORPUS, AND `dets.csv` IS THE EXCEPTION. A detection row exists only when the
impulse gate fired, so a pool built from `dets.csv` alone can hold nothing but impulsive events
and structurally cannot represent the acoustic world the gate is silent for -- which is nearly
all of it. `scene.csv` carries one row every ~1.024 s whether or not anything triggered. Cicadas,
traffic, aircraft, rain, machinery, the diurnal cycle and the room's own noise floor are all
HERE and nowhere else. dama-hear is not a gunshot project; the sketch is deliberately
uninterpreted so the central side can train new classifiers on the same bytes forever, and this
is the file that makes that true rather than aspirational.

⚠️A SCENE ROW IS NOT A WIRE FRAME. `mel_hex` is a BARE `bands x slices` int8 array, band-major
(index `b*slices + s`), each cell a 0.5 dB step below the row's OWN `ref_db4` reference in
quarter-dB. There is no v1/v2 header on it and `hear.wire.decode` would reject it. The geometry
comes from the row's `bands` and `slices` columns -- which is why they are columns -- and the
length is CHECKED against them, because a truncated line is otherwise indistinguishable from a
smaller descriptor.

⚠️THE SECOND AXIS IS `slices`, NOT `frames`. The row's `frames` column is how many FFT frames the
node summed into each slice; it says how much averaging is behind a cell and it is NOT the shape.
Conflating them silently changes what a feature vector means.

The generations, all with headers that describe their own rows (unlike `dets.csv` G3):

    S0  10 cols   utc_us..mel_hex                      the shape hear_bridge.SCENE_COLUMNS pins
    S1  12 cols   + f_lo_hz, f_hi_hz                   the band edges stated per row
    S2  13 cols   node, ...                            node named by the file
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

_BASE = ("utc_us", "uptime_s", "sample", "bands", "slices", "span_ms", "ref_db4", "frames",
         "fft_us", "mel_hex")


@dataclass(frozen=True)
class Generation:
    name: str
    declared: Tuple[str, ...]
    written: Tuple[str, ...]

    @property
    def broken_header(self) -> bool:
        return self.declared != self.written


S0 = Generation("S0", _BASE, _BASE)
S1 = Generation("S1", _BASE + ("f_lo_hz", "f_hi_hz"), _BASE + ("f_lo_hz", "f_hi_hz"))
S2 = Generation("S2", ("node",) + _BASE + ("f_lo_hz", "f_hi_hz"),
                ("node",) + _BASE + ("f_lo_hz", "f_hi_hz"))

GENERATIONS: Tuple[Generation, ...] = (S0, S1, S2)
LATEST = S2


class UnknownSchema(ValueError):
    """The header matches no generation. Carries the header so the caller can add one."""


@dataclass
class SceneRead:
    generation: Generation
    rows: List[Dict[str, Any]] = field(default_factory=list)
    skips: List[Tuple[int, str]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    header: Tuple[str, ...] = ()
    partial_first_line: bool = False

    def _skip(self, line: int, reason: str) -> None:
        self.skips.append((line, reason))
        self.counts[reason] = self.counts.get(reason, 0) + 1

    def summary(self) -> Dict[str, Any]:
        return {"generation": self.generation.name, "rows": len(self.rows),
                "skipped": len(self.skips), "reasons": dict(self.counts),
                "partial_first_line": self.partial_first_line}


def identify(header: Sequence[str]) -> Generation:
    h = tuple(c.strip() for c in header)
    for g in GENERATIONS:
        if h == g.declared:
            return g
    for g in GENERATIONS:
        if h[:len(g.declared)] == g.declared:
            raise UnknownSchema(
                "scene.csv header extends %s with %r. S2 prepended a column and S1 appended two, "
                "so neither end is safe to assume: add the generation to hear/scenefile.py."
                % (g.name, list(h[len(g.declared):])))
    raise UnknownSchema("scene.csv header %r matches no known generation (%s)"
                        % (list(h), ", ".join(g.name for g in GENERATIONS)))


def decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One row's `mel_hex` -> {q, ref_db, bands, slices, span_ms, frames_summed}.

    Raises ValueError with the reason. Every geometry field is read from the ROW; nothing here
    pins a shape, because `SCENE_SLICES` and `SCENE_FRAMES_PER_SLICE` are firmware's to change.
    """
    def _int(k):
        v = row.get(k)
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    bands, slices = _int("bands"), _int("slices")
    if not bands or not slices or bands < 1 or slices < 1:
        raise ValueError("bands=%r slices=%r; both required and positive"
                         % (row.get("bands"), row.get("slices")))
    s = str(row.get("mel_hex") or "").strip()
    if not s:
        raise ValueError("empty mel_hex")
    if len(s) != 2 * bands * slices:
        raise ValueError("mel_hex is %d hex chars, %dx%d needs %d"
                         % (len(s), bands, slices, 2 * bands * slices))
    try:
        b = bytes.fromhex(s)
    except ValueError as e:
        raise ValueError("mel_hex is not hex: %s" % e)
    ref4 = _int("ref_db4")
    if ref4 is None:
        raise ValueError("no ref_db4, so the cells have no reference level")
    span = row.get("span_ms")
    if span in (None, ""):
        raise ValueError("no span_ms, so the window this row describes is unknown")
    try:
        span = float(span)
    except (TypeError, ValueError):
        raise ValueError("span_ms is %r, not a number" % (span,))
    if span <= 0:
        raise ValueError("span_ms is %r; a row covers a positive interval" % (span,))
    return {"q": np.frombuffer(b, dtype=np.int8).reshape(bands, slices),
            "ref_db": ref4 / 4.0, "bands": bands, "slices": slices,
            "span_ms": span, "frames_summed": _int("frames")}


def read_text(text: str, default_node: Optional[str] = None,
              allow_partial_first_line: bool = False) -> SceneRead:
    """Parse a scene.csv's text.

    `allow_partial_first_line` is for a BYTE-RANGE fetch. `GET /sd?file=scene.csv&tail=N` starts
    mid-line and carries no header, so the caller states the generation is known and the first
    fragment is dropped rather than parsed into a plausible wrong row -- the failure `dets.csv`
    G3 already taught this project. It is off by default: a whole file that begins mid-row is a
    corrupt file, not a partial fetch, and the two must not read the same.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError(
            "scene.csv is empty: %d byte(s), no header and no rows. A file the node created but "
            "never wrote, and one whose content was lost, both read like this; neither is a "
            "quiet interval, which still carries the header and its rows." % len(text))

    header = tuple(c.strip() for c in next(csv.reader([lines[0]])))
    partial = False
    if allow_partial_first_line:
        try:
            gen = identify(header)
            body = lines[1:]
        except UnknownSchema:
            # No header: a tail fetch. Infer the generation from the row width, drop the leading
            # fragment, and record that we did -- an unreported drop is the bug this file exists
            # to not repeat.
            rows = [r for r in csv.reader(lines[1:]) if r]
            if not rows:
                raise ValueError("tail fetch carried no complete row")
            widths = {}
            for r in rows:
                widths[len(r)] = widths.get(len(r), 0) + 1
            width = max(widths, key=widths.get)
            match = [g for g in GENERATIONS if len(g.written) == width]
            if not match:
                raise UnknownSchema(
                    "tail fetch rows are %d wide, which matches no generation (%s)"
                    % (width, ", ".join("%s=%d" % (g.name, len(g.written)) for g in GENERATIONS)))
            gen = match[0]
            body = lines[1:]
            partial = True
    else:
        gen = identify(header)
        body = lines[1:]

    out = SceneRead(generation=gen, header=header, partial_first_line=partial)
    width = len(gen.written)
    if partial:
        # ⚠️COUNTED, not merely flagged. The fragment is a line this read threw away, and a drop
        # that lives only in a boolean is a drop nobody totals. It lands in skip_reasons beside
        # every other refusal, so `rows == added + duplicate + skipped` still closes upstream.
        out._skip(1, "partial_first_line")
    for n, row in enumerate(csv.reader(body), start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) != width:
            out._skip(n, "row_width_%d_expected_%d" % (len(row), width))
            continue
        d = dict(zip(gen.written, (c.strip() for c in row)))
        if d.get("node"):
            d["node_from"] = "file"
        else:
            if not default_node:
                out._skip(n, "no_node")
                continue
            d["node"], d["node_from"] = default_node, "argument"
        d["schema"] = gen.name
        out.rows.append(d)
    return out


def read_file(path: str, default_node: Optional[str] = None) -> SceneRead:
    with open(path, "r", errors="replace") as fh:
        return read_text(fh.read(), default_node=default_node)
