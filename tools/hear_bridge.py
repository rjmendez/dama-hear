#!/usr/bin/env python3
"""Bridge: hear-node feature records -> the telem_*.jsonl shards hugbot already forwards.

WHY A BRIDGE AND NOT A PORT. hugbot5000 already runs embedding -> anomaly -> clustering
(audio_embed_archiver.py, npu-audio/audio_anomaly_train.py, npu-audio/audio_anomaly_score.py,
npu-audio/cluster_audio_embed.py) and log_forwarder.py already ships completed shards to the
batch-ingest API with store-and-forward and per-chunk idempotency. That protocol is only correct
because there is one implementation of it; a second copy living here would drift. This module
writes that chain's INPUT and nothing else -- one JSON object per line, `telem_<bucket>.jsonl`.

⚠️THESE RECORDS DO NOT REACH hugbot's MODELS AS THOSE MODELS ARE WRITTEN TODAY, AND THAT IS THE
FIRST THING TO KNOW ABOUT THIS TOOL. Every consumer of the shard stream hard-codes YAMNet's width:

    npu-audio/cluster_audio_embed.py:33-34   e = d.get("embed"); if e and len(e) == 1024:
    npu-audio/audio_anomaly_train.py:44-45   the same two lines
    npu-audio/correlate_audio_sources.py:36-37  the same two lines
    npu-audio/audio_anomaly_score.py:32-33   wrong width -> return 0.0, False

The first three CONTINUE past anything else -- the record is dropped, not routed -- and the fourth
scores it 0.0, "not anomalous", which is worse than dropping because it looks like an answer. A
hear node's impulse sketch is 20x8 = 160 wide and its scene descriptor is 20x4 = 80, so as of
today 100% of what this tool emits is discarded or scored-as-normal by the pipeline it feeds.
`embed_dim` is therefore a LABEL, not a route: it is how a human tells the two hear features apart
in a shard, and how the 1024-guard tells them from YAMNet. Making them consumable needs a decision
on the far side (a per-dim model, or a width check that dispatches instead of dropping) and no
amount of care on this side substitutes for it. main() prints that warning on every run --
consumer_note() -- rather than leaving it in this docstring for someone to not read.

⚠️THE TIMESTAMP IS THE PRODUCT AND IT IS NOT ROUNDED. hugbot's own producer ships
`round(t, 3)` (perception/audio_ant.py:222) because a YAMNet patch spans 0.96 s and a millisecond
is free. A hear node's stamp cost 11.33 h of GPS discipline to earn, measured across the capture's
1360 health rows: fix 3 in every one of them, tAcc 22-26 ns, 40817 PPS edges with 0 rejected and a
per-row spread of 2-15 us (median 10). Rounding that to a millisecond throws away four orders of
magnitude: at 343 m/s one microsecond is 343 MICROMETRES of range, so the 911 us a
round-to-milliseconds would discard from the capture's first anchored detection is 911 x 343 um,
312 mm. So the integer `utc_us` from the node is the authoritative field and travels verbatim.
`ts` is emitted beside it as float seconds ONLY because the ingest API quarantines a message
without one (audio_embed_archiver.py:25-26) and cluster_audio_embed.py:38 reads `ts` with no `t`
fallback. Float64 at this epoch (1.789e9 s) has 238 ns of spacing, so `ts` round-trips an integer
microsecond exactly but cannot carry the 22 ns the receiver actually had. Read `utc_us`.

⚠️NO RAW AUDIO LEAVES HERE, AND THIS MODULE MAKES NO NETWORK CALLS AT ALL. Not to the node, not
to the ingest API. It reads files (or stdin) and writes files. Instead of samples it emits a
RETRIEVAL POINTER: node, UTC range, sample range and the /audio URL that would return that window
if someone asks for it. The policy "don't push raw, provide it if asked" then lives in the data
where a consumer can act on it, rather than in a convention someone has to remember. The URL is
the node's documented form -- `/audio?from=<utc_us>&dur=<seconds>` -- and audio_pointer() is
written so that it dereferences: see that function for what `from` is and is not.

THE TWO FEATURES, AND THE TWO FILES THEY COME FROM. They are different shapes from different
firmware paths and they are NOT one code path:

  dets.csv  frame_hex  the IMPULSE sketch, 20 bands x 8 frames = 160 cells over 44 ms, gated,
                       packed in the v1/v2 wire header (hear/sketch.py, hear/wire.py) and decoded
                       through hear.wire.decode.
  scene.csv mel_hex    the SCENE descriptor, 20 bands x 4 slices = 80 cells over 1.024 s, written
                       every row whether or not anything triggered. It is a BARE quantised array
                       -- 80 int8 half-dB steps relative to the row's own ref_db4/4, band-major,
                       with NO wire header at all -- so it does NOT decode through hear.wire and
                       has its own parser here (parse_scene_csv, decode_scene_row). Its geometry
                       and reference level come from the row's OWN columns, never from a constant.

PURE vs I/O. Everything above `----- I/O -----` is pure: it takes text or dicts and returns dicts,
and tests/test_bridge.py covers it. Below that line is file reading, shard writing and argv.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import wire as WR  # noqa: E402

SHARD_PREFIX = "telem"
SHARD_S = 60                    # audio_embed_archiver.py:51,104; log_forwarder.py:52 globs telem_*.jsonl

# The one width every downstream consumer accepts; see the module docstring. Nothing this tool
# emits is this wide, which is the point of consumer_note().
CONSUMER_EMBED_DIM = 1024

# The nominal rate, used only where a row's own fs_hz is missing or implausible. The true rate,
# PPS-disciplined, is the capture's last health row: fs_clean_hz 16000.169 over fs_win_s 3030 s.
# That is +10.6 ppm, which over a 2 s retrieval window is 21 us -- a third of one sample at
# 62.5 us -- so the fallback does not move a pointer. It is NOT the cumulative fs_cum_hz of
# 15991.4821 in the same row, which is poisoned by boot loss.
NOMINAL_FS = 16000.0

# The node's impulse geometry (firmware/night_node/mel16.h: MEL16_BANDS, MEL16_FRAMES, MEL16_HOP,
# MEL16_NFFT). Held here so a frame of a DIFFERENT shape is recognised as different rather than
# silently measured with these.
NODE_BANDS, NODE_FRAMES = 20, 8
NODE_HOP, NODE_NFFT = 64, 256

# The node caps `dur` at AUDIO_MAX_S (night_node.ino, the "/audio" handler: `if (dur >
# (float)AUDIO_MAX_S) dur = (float)AUDIO_MAX_S;`). A pointer asking for more would silently come
# back short, so audio_pointer clamps to it here and says so in the record instead.
NODE_MAX_DUR_S = 30.0

# v1 FLAGS ONLY. hear/sketch.py's 2-byte flags word is free-form and the firmware (night_node.ino,
# in the gate's detection branch) uses exactly these two bits: bit 0 retrigger, bit 1 "the ring had
# not filled behind the trigger". ⚠️A v2 frame's flags word means something else entirely --
# seq<<8 | version<<5 | profile<<1 | retrigger (hear/wire.py: _F_SEQ_SHIFT, _F_VERSION_SHIFT,
# _F_PROFILE_SHIFT, _F_RETRIG) -- so bit 1 there is profile-id bit 0, and masking it as
# "no context" tags a perfectly good frame. Read flags through frame_flags(), never directly.
V1_FLAG_RETRIGGER = 0x0001
V1_FLAG_NO_CONTEXT = 0x0002

# The literal header the firmware writes (night_node.ino: DETS_HDR). Checked rather than trusted:
# File::size() returns uninitialised memory on a file SD.open() has just created, so a node can
# and does produce a dets.csv with NO header at all. Parsed with csv.DictReader, a headerless
# file turns its first detection into the column names and every later row into plausible
# nonsense. Refusing is the only safe reading.
DETS_COLUMNS = ["utc_us", "uptime_s", "sample", "pps_n", "us_since_pps",
                "trigger", "flags", "fs_hz", "frame_hex"]

# The literal header the firmware writes for the scene feature (night_node.ino: SCENE_HDR),
# checked for the same reason.
SCENE_COLUMNS = ["utc_us", "uptime_s", "sample", "bands", "slices", "span_ms",
                 "ref_db4", "frames", "fft_us", "mel_hex"]

# Columns that hold a feature. One row carries at most one of these in practice -- they come from
# different files -- but convert() counts features rather than rows so that stays true by
# construction rather than by assumption.
FEATURE_KEYS = ("frame_hex", "mel_hex")

# Quartiles of the trigger peak over the 48 clock-anchored detections of the 11.33 h capture:
# min 796, p25 848.5, median 953, p75 1252.25, max 2709 (numpy linear percentiles over
# abs(int(trigger)); tests/test_bridge.py recomputes them from the capture and fails if this
# drifts).
#
# ⚠️THESE ARE NOT A FLOOR AND THEY ARE NOT AMBIENT. Two things it would be easy to assume and
# both are wrong:
#   * "every detection is above the gate floor of 800". It is not: the measured minimum is 796.
#     The floor applies to a DIFFERENT quantity -- gate_thr() is max(8 x ambient, FLOOR=800) and
#     it thresholds `e`, the mean of |s| over the last 16 DC-blocked samples, while the `trigger`
#     column stores the single DC-blocked sample at the crossing. An envelope over 800 does not
#     imply that sample is. (The threshold is not fixed either: gate_thr ran 800-1715 over the
#     capture's 1360 health rows.)
#   * ambient percentiles would do instead. They would not: env peak per health row has median
#     368.9 and p95 692.0, both below every detection here, so banding on them puts all 48 in one
#     bucket -- measured, not feared -- which is the all-one-name failure these tags exist to
#     avoid. The detections' own quartiles split them 12/12/12/12.
DET_PEAK_QUARTILES = (848.5, 953.0, 1252.25)

REASONS: frozenset = frozenset({
    "unanchored_time", "no_feature", "undecodable_feature", "malformed_row"})


class Reject(ValueError):
    """A row that must not be shipped, carrying the vocabulary term that says why.

    Derived from ValueError so a caller's `except ValueError` still catches everything this
    module refuses on, but TYPED so convert() routes on the class instead of sniffing words out
    of the message -- which is how a reworded error message silently becomes a mislabelled
    rejection.
    """

    def __init__(self, reason: str, why: str):
        if reason not in REASONS:
            raise ValueError("unknown rejection reason %r" % reason)
        super().__init__("%s: %s" % (reason, why))
        self.reason = reason
        self.why = why


def _row(row: Dict, reason: str, why: str) -> Dict:
    if reason not in REASONS:
        raise ValueError("unknown rejection reason %r" % reason)
    return {"reason": reason, "why": why, "row": row}


def _int_or_none(row: Dict, key: str) -> Optional[int]:
    """int(row[key]) or None when the node did not write one. Never a substitute: a missing
    quantity stays missing, the rule hear/node/telemetry.py:55-59 states for a missing sensor.
    Note that 0 is a VALUE here (sample 0 is the first sample of the boot), not an absence."""
    v = row.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        raise Reject("malformed_row", "%s is %r, which is not an integer" % (key, v))


# ------------------------------------------------------------------ input normalisation

def parse_dets_csv(text: str) -> List[Dict]:
    """Rows from a dets.csv pulled off the card over /sd. Pure: takes the file's text.

    Refuses a file whose header is missing or reordered rather than guessing at the columns --
    see DETS_COLUMNS. Extra trailing columns are allowed and carried, so a future column added to
    the firmware arrives here without a change.
    """
    return _parse_csv(text, DETS_COLUMNS, "dets.csv")


def parse_scene_csv(text: str) -> List[Dict]:
    """Rows from a /scene.csv pulled off the card over /sd (`GET /sd?file=/scene.csv`).

    Same header discipline as dets.csv, against SCENE_COLUMNS. The rows are continuous -- one
    every 1.024 s whether or not anything triggered -- so a night is tens of thousands of them,
    and every one with utc_us 0 (pre-PPS-lock) is refused downstream exactly as a detection is.
    """
    return _parse_csv(text, SCENE_COLUMNS, "scene.csv")


def _parse_csv(text: str, columns: Sequence[str], src: str) -> List[Dict]:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    head = [c.strip() for c in lines[0].split(",")]
    if head[:len(columns)] != list(columns):
        raise ValueError(
            "%s header is %r, expected it to start %r. A node that created the file on this "
            "boot can write it headerless; prepend the header rather than parsing it blind."
            % (src, head[:len(columns)], list(columns)))
    out = []
    for d in csv.DictReader(io.StringIO("\n".join(lines))):
        d = {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items() if k is not None}
        d["src"] = src
        out.append(d)
    return out


def rows_from_detections(obj: Sequence[Dict]) -> List[Dict]:
    """Rows from the node's /detections JSON, normalised to the dets.csv column names.

    /detections is the live ring (night_node.ino: h_dets()): the newest MAXDET=128, oldest first,
    in RAM. It carries `frame` and `frame_len` where the card carries `frame_hex`; frame_len is
    checked against the hex it came with, because a truncated JSON body is otherwise
    indistinguishable from a short frame and would decode as a smaller sketch.

    There is no scene feature here: /scene.csv has no live-ring endpoint, it is read off the card.
    """
    out = []
    for d in obj:
        r = {k: d[k] for k in DETS_COLUMNS[:-1] if k in d}
        hexs = d.get("frame", d.get("frame_hex", ""))
        n = d.get("frame_len")
        if n is not None and len(hexs) != 2 * int(n):
            raise ValueError("detection i=%r declares frame_len %r but carries %d hex chars"
                             % (d.get("i"), n, len(hexs)))
        r["frame_hex"] = hexs
        r["i"] = d.get("i")
        r["src"] = "/detections"
        out.append(r)
    return out


# ------------------------------------------------------------------ feature reshaping

def decode_feature(frame_hex: str) -> Dict:
    """Decode one packed IMPULSE sketch. Delegates to hear.wire.decode so there is one frame
    parser. Accepts v1 (the 172 B frame the node writes today) and v2 alike, and any v1 geometry.

    This is the dets.csv path only. The scene feature is not a wire frame -- see decode_scene_row.
    """
    s = (frame_hex or "").strip()
    if not s:
        raise ValueError("empty frame")
    if len(s) % 2:
        raise ValueError("frame hex has an odd length (%d chars)" % len(s))
    try:
        b = bytes.fromhex(s)
    except ValueError as e:
        raise ValueError("frame hex is not hex: %s" % e)
    return WR.decode(b)


def decode_scene_row(row: Dict) -> Dict:
    """Decode one /scene.csv row's `mel_hex` into the same {q, ref_db, bands, frames} shape a
    decoded wire frame has, so feature_vector() sees one kind of thing.

    THE FORMAT, as the firmware writes it (night_node.ino: scene_emit()): mel_hex is a BARE array
    of bands x slices int8 values, hex-encoded, band-major (index b*slices + s), each a 0.5 dB
    step below the row's own reference -- `roundf((scene_db[i] - ref) * 2.0f)` clamped to int8 --
    and the reference is the row's `ref_db4` column in quarter-dB. There is NO v1/v2 header on it
    and hear.wire.decode would reject it; geometry comes from the row's `bands` and `slices`
    columns, which is why they are columns at all.

    The length is checked against those columns rather than assumed, for the same reason
    /detections' frame_len is: a truncated line is otherwise indistinguishable from a smaller
    descriptor, and a smaller descriptor is a different embed_dim.
    """
    bands = _int_or_none(row, "bands")
    slices = _int_or_none(row, "slices")
    if not bands or not slices or bands < 1 or slices < 1:
        raise ValueError("scene row has bands=%r slices=%r; both are required and positive"
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
    ref4 = _int_or_none(row, "ref_db4")
    if ref4 is None:
        raise ValueError("scene row has no ref_db4, so its cells have no reference level")
    span_ms = row.get("span_ms")
    if span_ms in (None, ""):
        raise ValueError("scene row has no span_ms, so the window it describes is unknown")
    try:
        span_ms = float(span_ms)
    except (TypeError, ValueError):
        raise ValueError("span_ms is %r, which is not a number" % (span_ms,))
    if span_ms <= 0:
        raise ValueError("span_ms is %r; a row covers a positive interval" % (span_ms,))
    return {
        "q": np.frombuffer(b, dtype=np.int8).reshape(bands, slices),
        "ref_db": ref4 / 4.0,
        "bands": bands,
        "frames": slices,
        # The node's own measurement of the row's span (SCENE_FRAMES x MEL16_NFFT / FS_NOMINAL,
        # computed on the node and written into the row), not a number derived here. Required,
        # because it IS the retrieval window: a scene record with no span would emit a pointer
        # with dur=0, which the node reads as "no dur given" and answers with a default 5 s.
        "span_ms": span_ms,
        # The row's `frames` column: FFT frames summed into the slices (64 of them, 16 per slice
        # at the shipped SCENE_FRAMES_PER_SLICE). Carried because it is the only thing that says
        # how much averaging is behind each cell; it is NOT the descriptor's shape.
        "frames_summed": _int_or_none(row, "frames"),
    }


def frame_flags(dec: Dict) -> Tuple[bool, Optional[bool]]:
    """(retrigger, context_ok) from a DECODED frame, by wire version.

    ⚠️THE FLAGS WORD IS NOT ONE FORMAT. v1 (hear/sketch.py) hands its 16 bits to the firmware,
    which spends two of them: bit 0 retrigger, bit 1 "the ring had not filled behind the trigger"
    (night_node.ino, the gate's detection branch). v2 (hear/wire.py) packs the SAME word as
    seq<<8 | version<<5 | profile<<1 | retrigger, so bit 1 is profile-id bit 0: masking
    V1_FLAG_NO_CONTEXT against a v2 frame carrying profile 1 (flags 0x0342) reports "no context"
    for a frame that has full context. Reproduced in tests/test_bridge.py.

    v2 has no context bit to read -- its flags word is fully allocated (hear/wire.py's header
    warning) -- so context_ok is None, "not knowable from this frame", not False. unpack_v2
    already decodes `retrigger` correctly, so that is taken from the decoder rather than
    re-derived here.
    """
    if int(dec.get("version", 1)) >= 2:
        return bool(dec.get("retrigger", False)), None
    flags = int(dec.get("flags", 0) or 0)
    return bool(flags & V1_FLAG_RETRIGGER), not bool(flags & V1_FLAG_NO_CONTEXT)


def feature_vector(dec: Dict) -> List[float]:
    """The embedding hugbot's models read: the sketch's SHAPE, L2-normalised, band-major.

    Two decisions, both forced.

    L2, because npu-audio/audio_anomaly_train.py's EPS = 1e-3 ridge (:31) and its p99.5 threshold
    (:32) are calibrated for unit vectors, and cluster_audio_embed.py scores with cosine. An
    un-normalised log-mel column would still invert but neither constant would transfer.

    SHAPE, i.e. db - ref_db, which is exactly the quantised int8 halved. The absolute level does
    not go into the vector: it is the single strongest feature this project has measured and it
    would dominate every cosine distance, collapsing the clusters onto loudness. It travels
    instead as `feature.ref_db` and `feature.peak`, which is the same split the wire format
    already makes (hear/sketch.py:13-16). ⚠️THE COST IS REAL: audio_anomaly_train.py:44-46 reads
    `embed` and nothing else, so level does not reach that model at all. Anyone who wants it there
    must widen the vector deliberately.

    A frame whose cells are all equal has a zero shape and normalises to zeros; that is a real
    sketch of silence, not an error, and for a v1 detection the `no_context` tag is what marks it.
    """
    v = np.asarray(dec["q"], dtype=float).reshape(-1) / 2.0     # int8 half-dB steps -> dB below ref
    n = float(np.linalg.norm(v))
    if n > 0:
        v = v / n
    # 6 dp matches perception/audio_ant.py:215,228 and holds a unit vector to well under any
    # distance that changes a cluster assignment, at roughly a third of the bytes of a full repr.
    return [round(float(x), 6) for x in v]


def feature_span_ms(frames: int, hop: int = NODE_HOP, nfft: int = NODE_NFFT,
                    fs: float = NOMINAL_FS) -> float:
    """Milliseconds of audio one IMPULSE sketch covers. 20x8 at hop 64, nfft 256, 16 kHz =
    256 + 7x64 = 704 samples, 44.0 ms. The firmware fetches `MEL16_NFFT + (MEL16_FRAMES - 1) *
    MEL16_HOP + 32` = 736 samples (night_node.ino, sketch_frame's caller); the extra 32 is guard,
    not span. A scene row does not go through here: it carries its own measured span_ms."""
    return (nfft + (int(frames) - 1) * hop) / float(fs) * 1000.0


def _bucket_name(value: float, edges: Sequence[float], names: Sequence[str]) -> str:
    i = sum(1 for e in edges if float(value) >= float(e))
    return names[i] if i < len(names) else names[-1]


def level_tags(peak: float, retrigger: bool = False, context_ok: Optional[bool] = True,
               edges: Sequence[float] = DET_PEAK_QUARTILES) -> List[Dict]:
    """The symbolic label a DETECTION record carries, built only from what the node computed.

    cluster_audio_embed.py is not decoration-tolerant here: it names a cluster by
    top_tags[0]["name"] (:37, :93-95) and sorts anything named "?" or "Silence" into
    silence/ambient (:103-110). A stream with no tags yields all-"?" clusters at purity 1.0 and
    the report means nothing. The node has no classifier and inventing class names it cannot
    defend would be worse, so the label is an ORDERING BUCKET against the night's own trigger-peak
    distribution, named after its boundary so it cannot be mistaken for a posterior.

    ⚠️These order detections against one night at one site; they do not measure anything. Pass
    `edges` from your own capture -- `--peak-edges` on the command line -- rather than carrying
    this site's quartiles to another.

    `no_context` leads when the frame says so, because a v1 sketch taken before the ring filled is
    all-equal bands (night_node.ino, the gate's detection branch sets bit 1 when
    `aring_total < back`) and its shape is an artefact -- "the vector is meaningless" is the
    honest dominant label, and it puts those rows in their own cluster instead of seeding a false
    one. `context_ok=None` (a v2 frame, which has no such bit) adds no tag either way: it is
    unknown, not good and not bad.

    `retrigger`/`context_ok` are booleans decoded by frame_flags(), NOT a raw flags word: the two
    wire versions pack that word differently and only the decoder knows which is in hand.
    """
    tags: List[str] = []
    if context_ok is False:
        tags.append("no_context")
    tags.append(_bucket_name(peak, edges,
                             ("lvl_p0_p25", "lvl_p25_p50", "lvl_p50_p75", "lvl_p75_up")))
    if retrigger:
        tags.append("retrigger")
    # 1.0 on every tag: these are deterministic buckets, not classifier confidences. A score that
    # varied would be read as a posterior and there is no model behind it.
    return [{"name": t, "score": 1.0} for t in tags]


def scene_tags(ref_db: float, edges: Optional[Sequence[float]] = None) -> List[Dict]:
    """The symbolic label a SCENE record carries.

    A scene row has no trigger and no peak -- it is the background, sampled every 1.024 s -- so
    the detection quartiles do not apply to it and reusing them would be a fabrication. What it
    does have is its own reference level, `ref_db4/4`, and bucketing that would order the rows the
    way DET_PEAK_QUARTILES orders detections. There are no edges here for it: this repo has no
    scene capture to measure them from (the 2026-09-07 night predates /scene.csv), and inventing
    four numbers is exactly what the house rule forbids. Pass `--scene-ref-edges` once a night of
    scene rows exists and the bucket LEADS the tag list, so cluster_audio_embed has something that
    varies to name a cluster with; until then every scene record is tagged "scene", which will
    cluster into one name and should be read as "unlabelled", not as a finding.
    """
    if not edges:
        return [{"name": "scene", "score": 1.0}]
    name = _bucket_name(ref_db, edges,
                        ("ref_p0_p25", "ref_p25_p50", "ref_p50_p75", "ref_p75_up"))
    return [{"name": name, "score": 1.0}, {"name": "scene", "score": 1.0}]


def audio_pointer(node: str, utc_us: int, sample: Optional[int], fs_hz: float,
                  base_url: Optional[str] = None, pre_s: float = 1.0, post_s: float = 1.0,
                  retention_s: Optional[float] = None,
                  max_dur_s: float = NODE_MAX_DUR_S) -> Dict:
    """Where the raw audio for this record IS, not the audio itself.

    ⚠️THE URL IS THE POINT AND IT MUST DEREFERENCE. The node's contract is

        GET /audio                             -> JSON: ring span, fill, from/to in utc_us
        GET /audio?from=<utc_us>&dur=<seconds> -> WAV

    `from` is MICROSECONDS SINCE THE UNIX EPOCH -- the node maps it back to a ring index itself --
    and `dur` is SECONDS, default 5.0. There is no `n` parameter and a sample index passed as
    `from` dereferences to 1970 and comes back 400. So the URL is built from `utc_us_from` and
    `dur_s`, and every other field here is derived from the SAME two numbers rather than computed
    beside them: the UTC range and the sample range cannot disagree, because the sample range is
    the UTC range times fs.

    The node answers with headers that say what it actually served -- X-Audio-Clipped:
    none|head|tail|both, X-Audio-From-Utc-Us, X-Audio-Samples, X-Audio-Fs-Hz and the ring's own
    X-Audio-Ring-From-Utc-Us / -To-Utc-Us -- and 416 with X-Audio-Clipped: all when none of the
    window is still held. A caller that ignores those headers can still believe it has audio
    either side of an event when it has one side.

    `sample` is the node's g_samples counter at the event, which is the ring's WRITE INDEX. It is
    carried because it is exact where a time-to-index mapping is only as good as the anchor -- and
    because a negative index is how you know the window reaches back past boot, which is the one
    clamp this side can make. It is optional: a row that did not carry one gets None for
    `sample_from` and `clamped_to_boot`, and the URL is unaffected, since the URL is addressed by
    time.

    ⚠️`sample` RESETS ON BOOT and the ring is RAM. A pointer is valid only to the node still up on
    the boot that made it -- which is also the only boot whose ring still holds the audio, so the
    two limits coincide rather than compound. `uptime_s` travels in `clock` to make the boot
    identifiable.

    `retention_s` is how long the ring holds a window before overwriting it, and it has NO
    default. The ring does now exist in the firmware, but its size is decided at boot: the node
    asks for 240 s and steps down through 180/120/60/30 until one fits the largest contiguous free
    PSRAM block (240 s of int16 at 16 kHz is 7.68 MB), so only that boot's /status -- `audio.raw.
    span_s` -- says which it got. A number invented here would be read as a promise.
    """
    fs = float(fs_hz) if float(fs_hz) > 1000.0 else NOMINAL_FS
    pre = max(int(round(float(pre_s) * fs)), 0)
    post = max(int(round(float(post_s) * fs)), 0)

    clamped_boot = None
    if sample is not None:
        clamped_boot = int(sample) - pre < 0
        if clamped_boot:
            pre = int(sample)           # there is no audio from before the first sample of a boot

    clamped_dur = False
    max_n = int(round(float(max_dur_s) * fs)) if max_dur_s else 0
    n = pre + post
    if max_n and n > max_n:
        clamped_dur = True              # the node would truncate silently; truncate visibly here
        pre = min(pre, max_n)
        post = max_n - pre
        n = pre + post

    utc_from = int(utc_us) - int(round(pre / fs * 1e6))
    # One rounding, used by the record, the URL and the UTC end alike, so the three cannot say
    # three slightly different things about one window.
    dur_s = round(n / fs, 6)
    out = {
        "pushed": False,
        "policy": "raw audio is retrieved on request, never pushed",
        "node": node,
        "utc_us_from": utc_from,
        "utc_us_to": utc_from + int(round(dur_s * 1e6)),
        "dur_s": dur_s,
        "sample_from": None if sample is None else int(sample) - pre,
        "n_samples": n,
        "bytes": n * 2,                 # int16 on the card and on the wire
        "fs_hz": fs,
        "clamped_to_boot": clamped_boot,
        "clamped_to_max_dur": clamped_dur,
        "url": None if not base_url else "%s/audio?from=%d&dur=%.6f" % (
            base_url.rstrip("/"), utc_from, dur_s),
        "retention_s": None if retention_s is None else float(retention_s),
        "expires_utc_us": (None if retention_s is None
                           else utc_from + int(round((dur_s + float(retention_s)) * 1e6))),
    }
    return out


def to_record(row: Dict, node: str, node_id: Optional[int] = None,
              base_url: Optional[str] = None, pre_s: float = 1.0, post_s: float = 1.0,
              retention_s: Optional[float] = None, frame_key: str = "frame_hex",
              peak_edges: Sequence[float] = DET_PEAK_QUARTILES,
              scene_ref_edges: Optional[Sequence[float]] = None) -> Dict:
    """One shard line's worth of dict. Raises Reject (a ValueError) on a row that must not ship.

    Missing quantities are None, never a plausible substitute -- the rule
    hear/node/telemetry.py:55-59 states for a missing sensor, applied to a missing geometry. In
    particular a row with no `sample` yields `clock.sample: None` and a pointer with no sample
    range, NOT sample 0, which is a real position in the ring and would point at the audio from
    the instant the node booted.
    """
    utc_us = _int_or_none(row, "utc_us")
    if utc_us is None:
        raise Reject("malformed_row", "row has no utc_us column; it cannot be placed in time")
    if utc_us <= 0:
        # The firmware writes 0 when the GPS anchor was not trusted at that instant (night_node
        # .ino: `dets[idx].utc_us = tok ? t : 0;`, and scene_emit's `sample_to_utc` returning 0).
        # Zero is not "unknown" to anything downstream: the archiver buckets it into
        # telem_0.jsonl and cluster_audio_embed's coverage footprint spans it against the rest of
        # the night, which turns 11 boot rows into a 56-year event. 11 of the 59 rows in the
        # retrieved capture.
        raise Reject("unanchored_time",
                     "utc_us is %d: the GPS anchor was not trusted for this row" % utc_us)

    scene = (frame_key == "mel_hex")
    try:
        dec = decode_scene_row(row) if scene else decode_feature(row.get(frame_key, ""))
    except Reject:
        raise
    except ValueError as e:
        raise Reject("undecodable_feature", str(e))

    embed = feature_vector(dec)
    bands, frames = int(dec["q"].shape[0]), int(dec["q"].shape[1])
    fs_hz = float(row.get("fs_hz") or 0.0)

    if scene:
        span_ms = dec["span_ms"]
        feature = {
            "kind": "mel_scene",
            "wire_version": None,           # not a wire frame: a bare array, see decode_scene_row
            "bands": bands,
            # Slices: 4, each covering span_ms/4 = 256 ms. Named `frames` to match the sketch's
            # second axis so one embed_dim arithmetic works for both.
            "frames": frames,
            "frames_summed": dec["frames_summed"],
            "span_ms": span_ms,
            "ref_db": float(dec["ref_db"]),
            "peak": None,                   # a scene row is not gated; there is no trigger peak
            "retrigger": None,
            "context_ok": None,
            "normalisation": "l2_of_db_below_ref",
        }
        tags = scene_tags(dec["ref_db"], scene_ref_edges)
        # The row's window IS the feature: it starts at `sample`/`utc_us` and runs span_ms. No
        # pre-roll, because nothing here is an event to have a run-up to.
        win = (0.0, span_ms / 1000.0)
    else:
        known = (bands, frames) == (NODE_BANDS, NODE_FRAMES)
        retrigger, context_ok = frame_flags(dec)
        feature = {
            "kind": "mel_sketch",
            "wire_version": int(dec.get("version", 1)),
            "bands": bands,
            "frames": frames,
            # None, not a guess: hop is not on the wire, so span is only knowable for a geometry
            # this build recognises. A frame of an unknown shape gets an honest null.
            "span_ms": round(feature_span_ms(frames), 3) if known else None,
            "ref_db": float(dec.get("ref_db", 0.0)),
            "peak": int(dec.get("peak", 0)),
            "retrigger": retrigger,
            "context_ok": context_ok,
            "normalisation": "l2_of_db_below_ref",
        }
        tags = level_tags(dec.get("peak", 0), retrigger, context_ok, edges=peak_edges)
        win = (float(pre_s), float(post_s))

    rec = {
        # `ts` first only because that is the field the ingest API validates on.
        "ts": utc_us / 1e6,
        "utc_us": utc_us,
        "node": node,
        "node_id": node_id,
        "embed": embed,
        "embed_dim": len(embed),
        "top_tags": tags,
        "feature": feature,
        "clock": {
            # Everything needed to re-derive the stamp, so a disagreement downstream is arguable
            # rather than a mystery. A scene row carries no PPS columns; those stay None.
            "source": "gps_pps",
            "pps_n": _int_or_none(row, "pps_n"),
            "us_since_pps": _int_or_none(row, "us_since_pps"),
            "fs_hz": fs_hz if fs_hz > 1000.0 else None,
            "sample": _int_or_none(row, "sample"),
            "uptime_s": _int_or_none(row, "uptime_s"),
        },
        "src": row.get("src"),
    }
    rec["audio"] = audio_pointer(node, utc_us, rec["clock"]["sample"], fs_hz,
                                 base_url=base_url, pre_s=win[0], post_s=win[1],
                                 retention_s=retention_s)
    return rec


def convert(rows: Sequence[Dict], node: str, node_id: Optional[int] = None,
            base_url: Optional[str] = None, pre_s: float = 1.0, post_s: float = 1.0,
            retention_s: Optional[float] = None,
            peak_edges: Sequence[float] = DET_PEAK_QUARTILES,
            scene_ref_edges: Optional[Sequence[float]] = None) -> Dict:
    """Rows in, records + rejections out. Nothing is dropped silently, and nothing aborts the run.

    Every input FEATURE ends in exactly one of `records` or `rejected`, and the caller can assert
    `len(records) + len(rejected) == n_features` -- the same conservation
    hear/backend/associate.py:19 holds itself to, for the same reason: a converter that quietly
    discards is indistinguishable from a quiet night. The quantity is features and not rows
    because a row carrying two features would yield TWO records sharing one `utc_us`, not one
    record with two vectors: a record can only have one `embed`, which is all that
    npu-audio/audio_anomaly_score.py:60 and npu-audio/cluster_audio_embed.py:33 read. A row with
    no feature at all is one rejection, so it counts as one.

    That contract is why the loop's backstop catches every exception and not just Reject: one
    malformed line out of a night's ~40000 scene rows (11.33 h at one per
    1.024 s) must cost that line, not the night. Anything unexpected
    is still named -- reason `malformed_row`, with the exception type in `why` -- so a surprise is
    loud in the summary rather than absent from it.
    """
    records: List[Dict] = []
    rejected: List[Dict] = []
    n_features = 0
    for row in rows:
        keys = [k for k in FEATURE_KEYS if str(row.get(k) or "").strip()]
        n_features += len(keys) or 1
        if not keys:
            rejected.append(_row(row, "no_feature",
                                 "row carries neither %s" % " nor ".join(FEATURE_KEYS)))
            continue
        for k in keys:
            try:
                records.append(to_record(row, node, node_id=node_id, base_url=base_url,
                                         pre_s=pre_s, post_s=post_s,
                                         retention_s=retention_s, frame_key=k,
                                         peak_edges=peak_edges,
                                         scene_ref_edges=scene_ref_edges))
            except Reject as e:
                rejected.append(_row(row, e.reason, "%s: %s" % (k, e.why)))
            except Exception as e:                                  # noqa: BLE001 -- see docstring
                rejected.append(_row(row, "malformed_row",
                                     "%s: %s: %s" % (k, type(e).__name__, e)))
    return {"records": records, "rejected": rejected,
            "n_input": len(rows), "n_features": n_features}


def consumer_note(dims: Iterable[int]) -> Optional[str]:
    """The warning main() prints when what was written cannot be consumed as things stand.

    None when every record is CONSUMER_EMBED_DIM wide. Otherwise a message naming the guard that
    will drop them, because a shard directory that looks full is otherwise indistinguishable from
    one that will be read.
    """
    bad = sorted({int(d) for d in dims if int(d) != CONSUMER_EMBED_DIM})
    if not bad:
        return None
    return ("WARNING: embed_dim %s is not %d. cluster_audio_embed.py:33-34, "
            "audio_anomaly_train.py:44-45 and correlate_audio_sources.py:36-37 keep only "
            "len(embed) == %d and DISCARD everything else; audio_anomaly_score.py:32-33 returns "
            "(0.0, not-anomalous) for any other width. These records will be written and "
            "forwarded, and then ignored, until a consumer dispatches on embed_dim instead of "
            "dropping." % (", ".join(str(b) for b in bad), CONSUMER_EMBED_DIM,
                           CONSUMER_EMBED_DIM))


# ------------------------------------------------------------------ shard shaping

def shard_bucket(ts: float, shard_s: int = SHARD_S) -> int:
    """audio_embed_archiver.py:62-63, including the max(1, ...) guard it applies at :54 against a
    zero divisor."""
    s = max(1, int(shard_s))
    return int(float(ts) // s) * s


def shard_name(bucket: int, prefix: str = SHARD_PREFIX) -> str:
    return "%s_%d.jsonl" % (prefix, int(bucket))


def to_shard_line(rec: Dict) -> str:
    """One compact JSON object, one newline. Separators match audio_embed_archiver.py:35 so a
    line written here is byte-comparable with one the archiver wrote."""
    return json.dumps(rec, separators=(",", ":")) + "\n"


def group_into_shards(records: Sequence[Dict], shard_s: int = SHARD_S,
                      prefix: str = SHARD_PREFIX) -> Dict[str, List[str]]:
    """filename -> lines, time-ordered within each shard.

    The live archiver buckets on ARRIVAL time because it is a subscriber. This is a batch
    converter over a capture that is already over, so it buckets on each record's own `ts`; a
    night that took 11.33 h to record therefore lands in the shards it would have had if it had
    been streamed, instead of one enormous shard stamped with the hour someone ran this.
    """
    out: Dict[str, List[Dict]] = {}
    for r in records:
        out.setdefault(shard_name(shard_bucket(r["ts"], shard_s), prefix), []).append(r)
    return {k: [to_shard_line(r) for r in sorted(v, key=lambda r: (r["ts"], r["embed_dim"]))]
            for k, v in sorted(out.items())}


# ------------------------------------------------------------------ I/O -----------------------

def write_shards(out_dir: str, shards: Dict[str, List[str]]) -> List[str]:
    """Write each shard via `.partial` then rename, so log_forwarder.py:52 -- which globs
    telem_*.jsonl and posts whatever it finds -- can never pick up a half-written file.

    Refuses to overwrite an existing shard. The archiver sidesteps a collision with a pid suffix
    (audio_embed_archiver.py:84-85) because it is a daemon that cannot stop; a batch tool can
    stop, and a silent second copy of a night is worse than a failed run.
    """
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, lines in shards.items():
        final = os.path.join(out_dir, name)
        if os.path.exists(final):
            raise FileExistsError(
                "%s already exists; use a fresh --out or remove it. Re-running into a forwarded "
                "directory would post the same night twice under new idempotency keys." % final)
        part = final + ".partial"
        with open(part, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(part, final)
        written.append(final)
    return written


def read_rows(dets_csv: Optional[str], detections_json: Optional[str],
              scene_csv: Optional[str] = None) -> List[Dict]:
    """Read any of the three sources off disk or stdin. This tool NEVER fetches: pipe the node's
    response in yourself (`curl .../detections > dets.json`, `curl '.../sd?file=/scene.csv' >
    scene.csv`) so the fetch is the operator's, on their terms."""
    rows: List[Dict] = []
    if dets_csv:
        with open(dets_csv, encoding="utf-8", errors="replace") as fh:
            rows += parse_dets_csv(fh.read())
    if scene_csv:
        with open(scene_csv, encoding="utf-8", errors="replace") as fh:
            rows += parse_scene_csv(fh.read())
    if detections_json:
        text = sys.stdin.read() if detections_json == "-" else open(
            detections_json, encoding="utf-8").read()
        rows += rows_from_detections(json.loads(text))
    return rows


def _edges(s: Optional[str], flag: str) -> Optional[List[float]]:
    """Parse a --*-edges value: ascending, comma-separated, at least one."""
    if not s:
        return None
    try:
        e = [float(x) for x in s.split(",") if x.strip() != ""]
    except ValueError:
        raise ValueError("%s takes comma-separated numbers, got %r" % (flag, s))
    if not e:
        raise ValueError("%s got no numbers" % flag)
    if any(b <= a for a, b in zip(e, e[1:])):
        raise ValueError("%s must be strictly ascending, got %r" % (flag, e))
    return e


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Convert hear-node detections and scene rows into telem_*.jsonl shards for "
                    "log_forwarder.py. Makes no network calls.")
    ap.add_argument("--dets-csv", help="dets.csv pulled off the card over /sd")
    ap.add_argument("--scene-csv", help="scene.csv pulled off the card over /sd")
    ap.add_argument("--detections-json", help="saved /detections body, or - for stdin")
    ap.add_argument("--out", required=True, help="shard directory, must not already hold these shards")
    ap.add_argument("--node", required=True, help="node name, free-form, e.g. hear-01")
    ap.add_argument("--node-id", type=int, help="uint16 survey id, if this node has one")
    ap.add_argument("--node-url", help="node root, e.g. http://172.16.100.105 -- written into the "
                                       "audio pointer as a URL, never requested")
    ap.add_argument("--pre-s", type=float, default=1.0, help="audio pointer window before the trigger")
    ap.add_argument("--post-s", type=float, default=1.0, help="audio pointer window after it")
    ap.add_argument("--retention-s", type=float, help="how long the node's ring holds a window; "
                                                      "omit unless /status says which span this "
                                                      "boot got")
    ap.add_argument("--peak-edges", help="comma-separated ascending trigger-peak edges for the "
                                         "lvl_* tags, measured on YOUR capture. Default is this "
                                         "repo's 2026-09-07 night: %s -- one night at one site, "
                                         "which orders nothing anywhere else."
                                         % ",".join(str(x) for x in DET_PEAK_QUARTILES))
    ap.add_argument("--scene-ref-edges", help="comma-separated ascending ref_db edges for scene "
                                              "rows. No default: no scene capture has been "
                                              "measured, and without this every scene record is "
                                              "tagged 'scene' and will not cluster by name.")
    ap.add_argument("--shard-s", type=int, default=SHARD_S)
    a = ap.parse_args(argv)
    if not a.dets_csv and not a.detections_json and not a.scene_csv:
        ap.error("give --dets-csv, --scene-csv, --detections-json, or any combination")
    try:
        peak_edges = _edges(a.peak_edges, "--peak-edges") or list(DET_PEAK_QUARTILES)
        scene_ref_edges = _edges(a.scene_ref_edges, "--scene-ref-edges")
    except ValueError as e:
        ap.error(str(e))

    rows = read_rows(a.dets_csv, a.detections_json, a.scene_csv)
    res = convert(rows, a.node, node_id=a.node_id, base_url=a.node_url,
                  pre_s=a.pre_s, post_s=a.post_s, retention_s=a.retention_s,
                  peak_edges=peak_edges, scene_ref_edges=scene_ref_edges)
    shards = group_into_shards(res["records"], a.shard_s)
    files = write_shards(a.out, shards)
    dims = sorted({r["embed_dim"] for r in res["records"]})
    print("%d row(s) -> %d record(s) in %d shard(s); dims %s; %d rejected"
          % (res["n_input"], len(res["records"]), len(files), dims, len(res["rejected"])))
    # Grouped, because 11 identical lines say less than one line and a count -- but every reason
    # that occurred is still named, since a silent rejection is the failure this whole return
    # shape exists to prevent.
    for reason in sorted({r["reason"] for r in res["rejected"]}):
        same = [r for r in res["rejected"] if r["reason"] == reason]
        print("  rejected %d (%s) e.g. %s" % (len(same), reason, same[0]["why"]))
    note = consumer_note(dims)
    if note:
        # stderr, and every run: this is the difference between "the shards are written" and "the
        # shards will be read", and it is not a detail the operator can be assumed to remember.
        print(note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
