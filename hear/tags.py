#!/usr/bin/env python3
"""The tag store: what a model said about a pooled clip, and the read-only join back to scene.csv.

⚠️A TAG IS A MODEL'S OPINION, NEVER A LABEL. `provenance` is the literal string "model" on every
row this module writes, and it is written here rather than inferred by any caller, so no code
path can promote a score into looking like something a human heard. The one thing this project
has already done wrong at scale is train on its own model's output -- all 35 dama ant models were
fitted on circular self-labels -- and the guard against repeating it is that the tag rows say, in
a machine-readable field, what they are.

⚠️IT NEVER IMPORTS hear.pool, AND THAT IS LOAD-BEARING RATHER THAN STYLE. `scene_overlap` takes
an already-constructed Pool. deploy/k8s/gen_configmap.py resolves every `hear.*` import in a
shipped module -- by an `ast.walk`, which sees a function-local import exactly as well as a
top-level one -- and would then correctly demand hear/pool.py (and numpy, and scenefile, and
corpus) in the hear-tag bundle for the sake of one analysis-only query the CronJob never calls.
Taking the Pool as an argument is what keeps that closure honest instead of dodged.

⚠️THE SAMPLE JOIN IS WEAKER THAN THE UTC JOIN AND SAYS SO IN ITS OWN RESULT. Neither scene.csv
nor dets.csv carries a boot id, and `sample` restarts at 0 on every boot, so two rows with the
same sample counter can be from different boots hours apart. `basis` is "utc" whenever the clip
is anchored; the sample basis is OFF unless a caller asks for it, and when it is used the result
carries `weak: True` and the reason. A silent match across a reboot would be indistinguishable
from a real one, which is the only kind of wrong answer this repo treats as worse than no answer.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

TAG_SCHEMA_VERSION = 1
TAGS_NAME = "tags.jsonl"

#: The clip's own geometry, from hear.clips. Repeated here as a local constant rather than
#: imported so that a bundle carrying tags.py without clips.py still parses; the two are checked
#: against each other by tests/test_hear_tag.py.
CLIP_PRE_S = 1.0
#: ⚠️FALLBACK ONLY -- see hear/clips.py. Two clip geometries are live in the corpus at once
#: (1.0+3.0 s at 16 kHz, 1.0+4.0 s at 48 kHz); `sample_window` reads the length off the row and
#: uses this only when the row has no body to read it from.
CLIP_POST_S = 4.0
FS_NOMINAL_HZ = 16000.0

#: How many scene rows one overlap query will return before it says it truncated. A 4.0 s clip
#: covers 4 or 5 rows of 1.024 s, so anything near this cap means the query matched something it
#: should not have -- it is a tripwire, not a page size.
SCENE_ROWS_CAP = 64

BASIS_UTC = "utc"
BASIS_SAMPLE = "sample"
BASIS_NONE = "none"


def tags_path(root: str) -> str:
    return os.path.join(root, "clips", TAGS_NAME)


def tag_key(clip_key: str, model_name: str, model_version: str,
            model_sha256: Optional[str] = None) -> str:
    """Identity of one (clip, model, model version, WEIGHTS DIGEST). sha256 truncated to 32 hex.

    Re-tagging with the SAME weights is a no-op duplicate the reader collapses; a NEW version or a
    NEW digest produces a DIFFERENT key and therefore a new row beside the old one. Nothing is
    overwritten, because the old row is the only record of what the previous model said about
    audio that may already have been pruned.

    ⚠️THE DIGEST IS IN THE KEY BECAUSE THE VERSION STRING IS A PROMISE AND THE DIGEST IS A
    MEASUREMENT. `model_block` already says "the sha256 IS the identity"; keying on the version
    alone meant a re-exported yamnet.tflite dropped in under the same MODEL_VERSION was treated
    as already-tagged for every clip already scored, and clips/tags.jsonl then held two models'
    scores under one version string with nothing anywhere comparing digests.
    """
    h = hashlib.sha256()
    for part in ("tag", clip_key, model_name, model_version, model_sha256 or ""):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def read_tags(root: str) -> Iterator[Dict[str, Any]]:
    """Every stored tag row. An unparseable line is skipped, not fatal: the store is append-only
    and a torn tail from a killed pod must not make the whole resume set unreadable."""
    p = tags_path(root)
    if not os.path.exists(p):
        return
    with open(p, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def read_resume(root: str) -> Tuple[Set[str], Dict[str, int]]:
    """ONE pass over the tag store -> (every tag_key held, `name/version/sha256` -> row count).

    One pass because `run()` needs both and the store is the only thing either can be read from;
    two passes over a file that grows with the corpus is the kind of cost that gets a check
    disabled. `versions_held` is the reported half, and it is now on a production path rather
    than being a function only the tests ever called.
    """
    keys: Set[str] = set()
    versions: Dict[str, int] = {}
    for r in read_tags(root):
        k = r.get("tag_key")
        if isinstance(k, str):
            keys.add(k)
        m = r.get("model") or {}
        vk = "%s/%s/%s" % (m.get("name"), m.get("version"), m.get("sha256"))
        versions[vk] = versions.get(vk, 0) + 1
    return keys, versions


def read_tagged(root: str) -> Set[str]:
    """Every tag_key already held. THE resume token, the same shape as hear_score's key rescan --
    no watermark, so there is nothing to tear and no second source of truth to disagree with."""
    return read_resume(root)[0]


def versions_held(root: str) -> Dict[str, int]:
    """`name/version/sha256` -> row count. ⚠️VERSION MIXING IS REPORTED, NEVER MERGED. Two model
    versions scoring the same clip produce two rows by construction (both the version and the
    weights digest are in the key), and a consumer that averaged or de-duplicated across them
    would be averaging two different models.

    ⚠️THE DIGEST IS IN THE REPORTED KEY, NOT ONLY IN THE ROW. Keyed on `name/version` this
    returned ONE entry for two different weight files scored under one version string -- the
    exact mixing the docstring claims is reported.
    """
    return read_resume(root)[1]


def append_tags(root: str, rows) -> int:
    p = tags_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    n = 0
    with open(p, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            n += 1
    return n


def _post_s(row: Dict[str, Any]) -> float:
    """The clip's post-roll, read off the row rather than assumed.

    ⚠️CLIP_POST_S IS THE FALLBACK, NOT THE ANSWER. The 16 kHz era wrote 1.0 + 3.0 s and the
    48 kHz firmware writes 1.0 + 4.0 s, and both are in the corpus right now -- a window built
    from one constant is 1.0 s wrong at the END for every clip of the other era. hear/clips.py
    derives `dur_s` at index time (mis-header corrected) precisely so this can be a lookup, and
    the pre-roll is 1.0 s in both eras, which is what makes the split recoverable from a total.
    """
    total = row.get("dur_s")
    if total is None:
        total = _v1_total_s(row.get("bytes"), row.get("wav_header_fs_hz"))
    if total is None or not (CLIP_PRE_S < total <= 8.0):
        return CLIP_POST_S
    return total - CLIP_PRE_S


#: The clip lengths this fleet writes and the rates it clocks. Repeated from hear/clips.py rather
#: than imported, because clips.py imports THIS module; test_hear_tag asserts the copies agree.
_GEOMETRIES_S = (4.0, 5.0)
_GEOMETRY_TOL = 0.02
_FLEET_RATES_HZ = (16000.0, 32000.0, 48000.0)


def _v1_total_s(n_bytes, header_fs) -> Optional[float]:
    """A v1 row's length, for rows written before `dur_s` existed. None when unknowable.

    ⚠️NOT bytes / header rate. v1 rows include 48 kHz clips headed 16000 Hz, which that division
    reads as 15.0 s. The header is believed only when it yields a length the fleet writes;
    otherwise exactly one fleet rate must, or nothing is claimed.
    """
    if not n_bytes or not header_fs:
        return None
    n = (int(n_bytes) - 44) / 2.0
    def is_geom(d):
        return any(abs(d - g) <= g * _GEOMETRY_TOL for g in _GEOMETRIES_S)
    at_header = n / float(header_fs)
    if is_geom(at_header):
        return at_header
    hits = [n / r for r in _FLEET_RATES_HZ if is_geom(n / r)]
    return hits[0] if len(hits) == 1 else None


def sample_window(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The clip's window in the node's own sample counter, or None when the name carried none.

    night_node.ino:1848 writes the clip from `d.sample - CLIP_PRE_SAMPLES`, and clip_name()
    embeds `d.sample` -- the TRIGGER, not the window start. Deriving the start here is what stops
    every consumer re-deriving CLIP_PRE_SAMPLES for itself and getting it 1.0 s wrong.
    """
    s = row.get("sample")
    if s is None:
        return None
    post = _post_s(row)
    return {"node": row.get("node"), "boot": row.get("boot"),
            "start_sample": int(s) - int(CLIP_PRE_S * FS_NOMINAL_HZ),
            "end_sample": int(s) + int(post * FS_NOMINAL_HZ),
            "post_s": post}


def _scene_ref(r: Dict[str, Any]) -> Dict[str, Any]:
    """The scene row's identity and window, NOT its mel payload.

    A tag row is ~5 KB of embedding already; carrying five 80-band mel vectors into it would
    triple that to restate rows the pool holds. `key` is the pool key, which is what a reader
    joins on.
    """
    return {"key": r.get("key"), "node": r.get("node"), "ts_utc_s": r.get("ts_utc_s"),
            "utc_us": r.get("utc_us"), "sample": r.get("sample"),
            "span_ms": r.get("span_ms"), "anchored": bool(r.get("anchored")),
            "bands": r.get("bands"), "slices": r.get("slices")}


def _span_s(r: Dict[str, Any]) -> Optional[float]:
    try:
        v = float(r.get("span_ms"))
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 0 else None


def _days_touched(t0: float, t1: float) -> List[str]:
    """The UTC day partitions a [t0, t1] window can land in. A 4.0 s clip at 23:59:58 straddles
    midnight, and scanning only its start day would silently drop the rows after it."""
    import datetime as _dt
    out = []
    for t in (t0, t1):
        d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d")
        if d not in out:
            out.append(d)
    return out


def scene_overlap(pl, row: Dict[str, Any], *, allow_sample_basis: bool = False,
                  max_rows: int = SCENE_ROWS_CAP) -> Dict[str, Any]:
    """READ-ONLY. The scene rows whose window overlaps this clip's.

    `pl` is a hear.pool.Pool -- taken as an argument, never imported (see the module docstring).
    `row` is one clips/index.jsonl row.

    -> {"basis": "utc"|"sample"|"none", "rows": [...], "refused": str|None, "weak": bool,
        "weakness": str|None, "truncated": bool, "scanned": int, "refused_records": int}

    ⚠️A 4.0 s clip against 1.024 s scene rows is 64000/16384 = 3.906 rows, so the answer is 4 or 5
    depending on phase and NEVER fewer. A query returning 0 on an anchored clip means the scene
    store does not hold that node/day, which is a real finding and is returned as an empty list
    with basis "utc" -- not as a refusal, and not as a silent fallback to the sample basis.

    ⚠️`refused_records` COUNTS, NEVER JUST SKIPS. A row inside a dated partition should always
    carry a usable `ts_utc_s`/`span_ms` by construction (`_scene_path`/`_day` route on exactly
    that), so one that does not is a malformed row, not a normal gap -- and this is the layer that
    handles unanchored/unusable records by refusing to use them AND saying how many, rather than
    letting them fall out of the count silently the way an unconditional `continue` would.
    """
    out: Dict[str, Any] = {"basis": BASIS_NONE, "rows": [], "refused": None, "weak": False,
                           "weakness": None, "truncated": False, "scanned": 0,
                           "refused_records": 0}
    node = row.get("node")
    if not node:
        out["refused"] = "the index row names no node, so no scene partition can be selected"
        return out

    t0, t1 = row.get("t_start_utc_s"), row.get("t_end_utc_s")
    if row.get("anchored") and t0 is not None and t1 is not None:
        out["basis"] = BASIS_UTC
        for day in _days_touched(float(t0), float(t1)):
            for r in pl.scene(node=node, day=day, anchored_only=True):
                out["scanned"] += 1
                ts, span = r.get("ts_utc_s"), _span_s(r)
                if ts is None or span is None:
                    out["refused_records"] += 1
                    continue
                if float(ts) < float(t1) and float(ts) + span > float(t0):
                    if len(out["rows"]) >= max_rows:
                        out["truncated"] = True
                        return out
                    out["rows"].append(_scene_ref(r))
        return out

    win = sample_window(row)
    if win is None or win.get("start_sample") is None:
        out["refused"] = ("the clip is unanchored and its name carried no sample counter, so it "
                          "has no window in either basis")
        return out
    if not allow_sample_basis:
        out["refused"] = ("the clip is unanchored, and the sample basis is off by default: "
                          "neither scene.csv nor dets.csv carries a boot id and `sample` resets "
                          "to 0 every boot, so this join can match a DIFFERENT boot's rows. Pass "
                          "allow_sample_basis=True to take it with that risk stated on the row")
        return out

    out["basis"] = BASIS_SAMPLE
    out["weak"] = True
    out["weakness"] = ("no boot id exists in scene.csv or dets.csv and `sample` restarts at 0 on "
                       "every boot, so these rows are only certainly the same boot if the node "
                       "has not rebooted between them -- which nothing here can check")
    s0, s1 = win["start_sample"], win["end_sample"]
    # ⚠️THE UNANCHORED PARTITION ONLY. An anchored scene row has a UTC and would have matched the
    # basis above; reaching into it on a sample counter would join a dated row to a clockless one
    # on a number neither of them promises is comparable.
    for r in pl.scene(node=node, day="unanchored"):
        out["scanned"] += 1
        if r.get("anchored"):
            out["refused_records"] += 1
            continue
        span = _span_s(r)
        try:
            start = int(r.get("sample"))
        except (TypeError, ValueError):
            out["refused_records"] += 1
            continue
        if span is None:
            out["refused_records"] += 1
            continue
        end = start + int(round(span * FS_NOMINAL_HZ))
        if start < s1 and end > s0:
            if len(out["rows"]) >= max_rows:
                out["truncated"] = True
                return out
            out["rows"].append(_scene_ref(r))
    return out


#: The sketch's own geometry, from hear.sketch (FRAMES, NFFT, HOP_S). Repeated here as local
#: constants rather than imported -- TAG_CODE in deploy/k8s/gen_configmap.py deliberately ships
#: tags.py WITHOUT hear/sketch.py, the same reason CLIP_PRE_S/CLIP_POST_S above are repeated
#: rather than imported from hear.clips. Checked against hear.sketch by test_hear_tag.py.
SKETCH_FRAMES = 8
SKETCH_NFFT = 256
SKETCH_HOP_S = 0.004
#: How far BEFORE the onset the window starts -- hear/node/detect.py SKETCH_BACK_S, a DIFFERENT
#: constant from HOP_S that happens to hold the same 0.004. Named separately here because the
#: window's start and its hop grid move for different reasons.
SKETCH_BACK_S = 0.004


def _sketch_span_s(fs_hz: Any) -> Optional[float]:
    """The sketch's own window length in seconds, at the rate IT was cut at.

    night_node.ino: `SKETCH_SPAN = NFFT + (FRAMES-1)*HOP` samples. NFFT is a SAMPLE count, so its
    duration depends on fs -- 256/16000 = 16 ms, 256/48000 = 5.33 ms -- while HOP_S is a fixed
    4 ms grid regardless of fs. That is why docs/acoustic-stack.md measures the sketch window at
    33-44 ms rather than one number: 5.33 + 7*4 = 33.3 ms at 48 kHz, 16 + 7*4 = 44 ms at 16 kHz,
    both reproduced by this formula. The node now cuts at 48 kHz like every phone, so 44 ms is
    the LEGACY case; this function still has to reproduce it for stored rows.

    ⚠️THE BACK-OFF IS NOT THE HOP, though it equalled it at 16 kHz and equals it again at 48 kHz.
    `SKETCH_BACK` is `SKETCH_BACK_S * fs` and `HOP` is `HOP_S * fs`; hear/node/detect.py's
    SKETCH_BACK_S and hear/sketch.py's HOP_S are two independent constants that both happen to be
    0.004 s. It does not enter the span either way -- the window is FRAMES hops wide wherever it
    starts -- so this formula is unaffected, and that is the only reason the coincidence was
    survivable here.
    """
    try:
        fs = float(fs_hz)
    except (TypeError, ValueError):
        return None
    if fs <= 0:
        return None
    return SKETCH_NFFT / fs + (SKETCH_FRAMES - 1) * SKETCH_HOP_S


def _sketch_ref(r: Dict[str, Any], is_trigger: bool) -> Dict[str, Any]:
    """The sketch record's identity and window, not its frame bytes -- same discipline as
    `_scene_ref`: a reader joins on `key`, it does not need the mel payload restated.

    ⚠️`sample` and `fs_hz` in this dict are in DIFFERENT domains and always have been: `sample` is
    the node's decimated counter (FS_NOMINAL_HZ) and `fs_hz` is the rate the frame was CUT at,
    48000.0 on every node frame since the sketch moved to the acquisition stream. sample/fs_hz is
    not a time."""
    return {"key": r.get("key"), "node": r.get("node"), "ts_utc_s": r.get("ts_utc_s"),
            "utc_us": r.get("utc_us"), "sample": r.get("sample"), "fs_hz": r.get("fs_hz"),
            "anchored": bool(r.get("anchored")), "trigger": r.get("trigger"),
            "clip": r.get("clip"), "is_trigger": is_trigger}


def sketch_overlap(pl, row: Dict[str, Any], *, source: str = "node",
                   allow_sample_basis: bool = False,
                   max_rows: int = SCENE_ROWS_CAP) -> Dict[str, Any]:
    """READ-ONLY. The pooled sketches (dets/detection records, hear.pool's `records` store) whose
    ~33-44 ms window overlaps this clip's 4.0 s window.

    `pl` is a hear.pool.Pool, taken as an argument, never imported -- see the module docstring;
    the same reason `scene_overlap` does it. `row` is one clips/index.jsonl row.

    -> {"basis": "utc"|"sample"|"none", "rows": [...], "refused": str|None, "weak": bool,
        "weakness": str|None, "truncated": bool, "scanned": int, "refused_records": int}

    One row of `rows` carries `is_trigger: True` when its `key` equals `row["record_key"]` -- the
    clip's OWN triggering sketch, already joined by identity and not by a window guess. Every
    other row is a DIFFERENT detection that happened to fall inside the same 4.0 s clip; on the
    live fleet a retrigger inside `CLIP_DEDUPE_SAMPLES` is common, so 2+ rows is a real finding,
    not a bug.

    ⚠️SAME REFUSAL DISCIPLINE AS scene_overlap, restated because this is a different store: an
    unanchored clip refuses the UTC basis and says why; the sample basis is opt-in and marks
    itself `weak` because neither dets.csv nor scene.csv carries a boot id and `sample` resets to
    0 every boot. A sketch record found in a dated partition that is itself unanchored, or that
    states no `fs_hz` (so its window cannot be computed), is REFUSED and COUNTED in
    `refused_records` -- never silently dropped, and never counted as "no sketches here" the way
    an uncounted `continue` would read.
    """
    out: Dict[str, Any] = {"basis": BASIS_NONE, "rows": [], "refused": None, "weak": False,
                           "weakness": None, "truncated": False, "scanned": 0,
                           "refused_records": 0}
    node = row.get("node")
    if not node:
        out["refused"] = "the index row names no node, so no sketch partition can be selected"
        return out

    trigger_key = row.get("record_key")
    t0, t1 = row.get("t_start_utc_s"), row.get("t_end_utc_s")
    if row.get("anchored") and t0 is not None and t1 is not None:
        out["basis"] = BASIS_UTC
        for day in _days_touched(float(t0), float(t1)):
            for r in pl.raw(source=source, day=day):
                if r.get("node") != node:
                    continue
                out["scanned"] += 1
                if not r.get("anchored") or r.get("ts_utc_s") is None:
                    out["refused_records"] += 1
                    continue
                span = _sketch_span_s(r.get("fs_hz"))
                if span is None:
                    out["refused_records"] += 1
                    continue
                s0 = float(r["ts_utc_s"]) - SKETCH_BACK_S
                s1 = s0 + span
                if s0 < float(t1) and s1 > float(t0):
                    if len(out["rows"]) >= max_rows:
                        out["truncated"] = True
                        return out
                    out["rows"].append(_sketch_ref(r, r.get("key") == trigger_key))
        return out

    win = sample_window(row)
    if win is None or win.get("start_sample") is None:
        out["refused"] = ("the clip is unanchored and its name carried no sample counter, so it "
                          "has no window in either basis")
        return out
    if not allow_sample_basis:
        out["refused"] = ("the clip is unanchored, and the sample basis is off by default: "
                          "neither dets.csv nor scene.csv carries a boot id and `sample` resets "
                          "to 0 every boot, so this join can match a DIFFERENT boot's rows. Pass "
                          "allow_sample_basis=True to take it with that risk stated on the row")
        return out

    out["basis"] = BASIS_SAMPLE
    out["weak"] = True
    out["weakness"] = ("no boot id exists in dets.csv or scene.csv and `sample` restarts at 0 on "
                       "every boot, so these rows are only certainly the same boot if the node "
                       "has not rebooted between them -- which nothing here can check")
    cs0, cs1 = win["start_sample"], win["end_sample"]
    for r in pl.raw(source=source, day="unanchored"):
        if r.get("node") != node:
            continue
        out["scanned"] += 1
        if r.get("anchored"):
            out["refused_records"] += 1
            continue
        try:
            trig_sample = int(r.get("sample"))
        except (TypeError, ValueError):
            out["refused_records"] += 1
            continue
        # ⚠️TWO RATES, ONE ROW. `span` is a TIME and depends on the frame's own rate (fs_hz is
        # 48000.0 for every node frame cut after the acquisition-rate move). `sample` is a
        # DECIMATED position -- night_node.ino writes `dets[idx].sample = g_samples + i` and
        # deliberately keeps it there -- so FS_NOMINAL_HZ is the only rate that turns a time into
        # this counter's samples, and it is the rate sample_window() built cs0/cs1 with. Using
        # the frame's rate here makes the window 3x too long and starts it 3x too far back.
        span = _sketch_span_s(r.get("fs_hz"))
        if span is None:
            out["refused_records"] += 1
            continue
        start = trig_sample - int(round(SKETCH_BACK_S * FS_NOMINAL_HZ))
        end = start + int(round(span * FS_NOMINAL_HZ))
        if start < cs1 and end > cs0:
            if len(out["rows"]) >= max_rows:
                out["truncated"] = True
                return out
            out["rows"].append(_sketch_ref(r, r.get("key") == trigger_key))
    return out


def joined_context(pl, tag_row: Dict[str, Any], index_row: Dict[str, Any], *,
                   allow_sample_basis: bool = False) -> Dict[str, Any]:
    """READ-ONLY. One tag (a model's opinion of a clip) joined onto the scene rows and the
    sketches whose window overlaps that clip -- LAYER 4 of the clip pipeline.

    ⚠️THIS IS A QUERY, NOT AN EXPORT. It builds one dict in memory and writes nothing; the spec
    (§12) REFUSES a training-label export or a persisted scene-row label join, because fitting
    a scene/sketch model on a tag would measure whether a 20-band descriptor can reconstruct what
    a full-fidelity model already decided -- the same shape as the 35 dama ant models trained on
    circular self-labels. `scene_overlap`/`sketch_overlap` stay the only sanctioned join, and this
    function is the two of them called together plus the tag's own claim -- nothing more.

    ⚠️A LABEL IS ALWAYS A MODEL OUTPUT, NEVER GROUND TRUTH, AND THIS IS ENFORCED HERE, NOT
    TRUSTED FROM THE CALLER. `tag_row["provenance"]` must be the literal string "model" -- a
    caller that hands this function a row it built by hand without that field gets a ValueError,
    not a silently-blessed result. The returned bundle restates `provenance`, `model` and `claim`
    at its own top level so a consumer reading only this dict, and never the tag row underneath
    it, still cannot mistake a score for something a human confirmed.

    -> {"tag_key", "clip_key", "node", "provenance": "model", "model": {...}, "claim": {...},
        "scene": scene_overlap(...), "sketch": sketch_overlap(...),
        "refused": str|None}

    `refused` is set, and both sub-joins are left at basis "none", only when the clip itself
    cannot be placed in either basis: unanchored, with no sample counter, or with
    `allow_sample_basis=False` (the default) left at that default. That is `index_row`'s own
    unanchored clip being refused and counted -- one refusal here reads as one refusal in a
    caller's tally, not two, because it is the SAME clip failing the SAME way in both stores.
    """
    if tag_row.get("provenance") != "model":
        raise ValueError("tag row %r carries provenance %r, not the literal 'model' -- refusing "
                         "to join it as though it were a label"
                         % (tag_row.get("tag_key"), tag_row.get("provenance")))
    scene = scene_overlap(pl, index_row, allow_sample_basis=allow_sample_basis)
    sketch = sketch_overlap(pl, index_row, allow_sample_basis=allow_sample_basis)
    refused = None
    if scene["basis"] == BASIS_NONE and sketch["basis"] == BASIS_NONE:
        refused = scene["refused"] or sketch["refused"]
    return {
        "tag_key": tag_row.get("tag_key"),
        "clip_key": tag_row.get("clip_key"),
        "node": index_row.get("node"),
        "provenance": "model",
        "model": tag_row.get("model"),
        "claim": tag_row.get("claim"),
        "scene": scene,
        "sketch": sketch,
        "refused": refused,
    }
