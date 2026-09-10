#!/usr/bin/env python3
"""Stage a stratified sample of pooled clips a human can actually play, and the sheet to fill in.

    python3 tools/hear_listen.py --pool /pool --out /tmp/listen --n 30
    python3 tools/hear_listen.py --pool /pool --out /tmp/listen --node mach --n 10

WHY THIS EXISTS. `deploy/k8s/hear-tag.yaml` holds the tagger suspended behind a gate whose second
condition is "a human has listened to >= 30 clips". Nothing in this repo could produce audio a
human could listen to, so that condition was unmeetable by construction. This file is the missing
half of the gate, not a convenience.

⚠️THE RAW CLIPS ARE INAUDIBLE AND THAT IS THE REASON, NOT THE TOOL. Measured on the pool
2026-09-10: these WAVs sit at -49 to -62 dBFS. At -57 dBFS an int16 sample peaks near 45 counts
of 32767. Played straight into any normal desktop chain that is silence -- not quiet, silence.
`hear_tag.py` already documents the same fact from the model's side (un-normalised, YAMNet answers
Silence for every clip). Anyone who "listened" to the raw files and heard nothing would have been
hearing the level, not the site.

⚠️THE GAIN IS RECORDED, AND THE THRESHOLD COMES FROM THE ORIGINAL. Each staged clip is written
twice: `<basename>.wav` byte-identical to the pool, and `<basename>.loud.wav` gain-adjusted for
the ear. `--max-silence-frac` (gate condition 3) must be derived from `rms_dbfs_orig` in
manifest.json, NEVER from the staged loud copy -- normalising and then measuring the normalised
thing is the circularity this repo names elsewhere.

⚠️RMS GAIN IS CLAMPED BY PEAK HEADROOM. Impulsive clips (a crack against a quiet floor) have an
RMS 40 dB under their peak; taking RMS to -20 dBFS would clip the transient, which is exactly the
part worth hearing. Gain is min(rms_target, peak_target) and the manifest says which bound bit.

⚠️IT READS THE POOL AND WRITES ONLY --out. No node is contacted, index.jsonl is not rewritten,
and nothing is pruned.
"""

import argparse
import array
import collections
import json
import math
import os
import random
import shutil
import struct
import sys
import wave

#: Target for the listening copy. -20 dBFS RMS matches what hear_tag.py normalises to, so the ear
#: and the model are hearing the same level.
RMS_TARGET_DBFS = -20.0
#: Peak ceiling for the listening copy. -1 dBFS leaves a sample of headroom against rounding.
PEAK_TARGET_DBFS = -1.0
#: int16 full scale is 32768, matching hear_tag's /32768.0. 32767 put a -32768 sample above 0 dBFS.
FULL_SCALE = 32768.0
#: Silence floor for the per-clip silence fraction, in dBFS over 20 ms frames. Reported only --
#: this file does not set a threshold, it produces the distribution one can be set from.
SILENCE_FRAME_DBFS = -60.0
SILENCE_FRAME_MS = 20.0


def dbfs(x):
    return -math.inf if x <= 0 else 20.0 * math.log10(x / FULL_SCALE)


def read_pcm(path):
    """Return (samples, fs_hz, channels). int16 mono only -- anything else is a real anomaly."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("%s: %d-byte samples, expected int16" % (path, w.getsampwidth()))
        if w.getnchannels() != 1:
            raise ValueError("%s: %d channels, expected mono" % (path, w.getnchannels()))
        n = w.getnframes()
        raw = w.readframes(n)
        fs = w.getframerate()
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    return a, fs, 1


def measure(samples, fs):
    n = len(samples)
    if n == 0:
        return {"n": 0}
    acc = 0
    peak = 0
    for s in samples:
        acc += s * s
        if s > peak:
            peak = s
        elif -s > peak:
            peak = -s
    rms = math.sqrt(acc / n)
    step = max(1, int(fs * SILENCE_FRAME_MS / 1000.0))
    frames = 0
    silent = 0
    for i in range(0, n - step + 1, step):
        fa = 0
        for s in samples[i:i + step]:
            fa += s * s
        frames += 1
        if dbfs(math.sqrt(fa / step)) < SILENCE_FRAME_DBFS:
            silent += 1
    return {
        "n": n,
        "dur_s": n / float(fs),
        "rms_dbfs_orig": round(dbfs(rms), 2),
        "peak_dbfs_orig": round(dbfs(peak), 2),
        "crest_db": round(dbfs(peak) - dbfs(rms), 2) if rms > 0 and peak > 0 else None,
        "silence_frac": round(silent / float(frames), 4) if frames else None,
        "silence_frames": frames,
    }


def gain_for(m):
    """min(rms-to-target, peak-to-ceiling), and which bound bit."""
    if m.get("n", 0) == 0 or m["rms_dbfs_orig"] == -math.inf:
        return 1.0, "none"
    g_rms = 10.0 ** ((RMS_TARGET_DBFS - m["rms_dbfs_orig"]) / 20.0)
    g_pk = 10.0 ** ((PEAK_TARGET_DBFS - m["peak_dbfs_orig"]) / 20.0)
    return (g_rms, "rms") if g_rms <= g_pk else (g_pk, "peak")


def write_loud(samples, fs, gain, out_path):
    a = array.array("h")
    clipped = 0
    for s in samples:
        v = int(round(s * gain))
        if v > 32767:
            v, clipped = 32767, clipped + 1
        elif v < -32768:
            v, clipped = -32768, clipped + 1
        a.append(v)
    if sys.byteorder == "big":
        a.byteswap()
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(round(fs)))
        w.writeframes(a.tobytes())
    return clipped


def load_index(pool):
    p = os.path.join(pool, "corpus", "clips", "index.jsonl")
    if not os.path.exists(p):
        raise SystemExit("no clip index at %s -- has hear-drain run?" % p)
    rows = []
    malformed = 0
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(r, dict) or r.get("outcome") != "stored":
                continue
            # A torn or older-schema row must cost one clip, not the whole staging run.
            if not all(isinstance(r.get(k), str) and r.get(k) for k in ("path", "node", "clip_key")):
                malformed += 1
                continue
            rows.append(r)
    if malformed:
        print("skipped %d malformed index row(s)" % malformed)
    return rows


def stratify(rows, n, rng, require_nodes):
    """Round-robin NODES, and within a node round-robin its days.

    ⚠️Balancing on (node, day) cells directly would hand the sample to whichever node has been
    up longest -- mach has three day-cells to rankine's one, which took 16 of 30 on the first
    run. The node is the unit that matters here: the gate exists to hear what each node hears.
    """
    per_node = collections.OrderedDict()
    for r in rows:
        parts = (r.get("path") or "").split("/")
        day = parts[1] if len(parts) > 2 else "?"
        per_node.setdefault(r.get("node"), collections.OrderedDict()).setdefault(day, []).append(r)
    for days in per_node.values():
        for v in days.values():
            rng.shuffle(v)

    def take(node):
        days = per_node.get(node) or {}
        order = [d for d in days if days[d]]
        if not order:
            return None
        return days[rng.choice(order)].pop()

    picked = []
    seen = set()
    for node in require_nodes:
        r = take(node)
        if r is not None:
            picked.append(r)
            seen.add(r["clip_key"])
    nodes = [nd for nd in per_node if any(per_node[nd].values())]
    rng.shuffle(nodes)
    i = 0
    while len(picked) < n and nodes:
        nd = nodes[i % len(nodes)]
        r = take(nd)
        if r is None:
            nodes.remove(nd)
            continue
        if r["clip_key"] not in seen:
            picked.append(r)
            seen.add(r["clip_key"])
        i += 1
    return picked


def sheet(picked, staged, out, day_tag):
    lines = []
    lines.append("# Clip calibration — %s" % day_tag)
    lines.append("")
    lines.append("Gate condition 2 of `dama-hear/unsuspend-gate` on `hear-tag`. Play the "
                 "`.loud.wav` copy; write what you HEARD, not what you expect.")
    lines.append("")
    lines.append("⚠️`rms_dbfs` and `silence_frac` below are measured on the ORIGINAL, not on the "
                 "loud copy. `--max-silence-frac` (condition 3) is set from the `silence_frac` "
                 "column of the clips you marked as containing nothing — from the envelope of "
                 "that distribution, never from one clip.")
    lines.append("")
    lines.append("`gain_db` is how much this clip had to be lifted to be audible. It is a "
                 "property of the recording, not of the event.")
    lines.append("")
    lines.append("| # | node | UTC | rms dBFS | crest dB | silence frac | gain dB | heard | "
                 "confident? | notes |")
    lines.append("|---|------|-----|----------|----------|--------------|---------|-------|"
                 "------------|-------|")
    import datetime
    for i, (r, m) in enumerate(zip(picked, staged), 1):
        ts = r.get("ts_utc_s")
        when = (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                .strftime("%m-%d %H:%M:%S") if isinstance(ts, (int, float)) else "unanchored")
        lines.append("| %d | %s | %s | %.1f | %s | %s | %+.1f |  |  |  |" % (
            i, r.get("node"), when, m["rms_dbfs_orig"],
            "%.1f" % m["crest_db"] if m.get("crest_db") is not None else "—",
            "%.3f" % m["silence_frac"] if m.get("silence_frac") is not None else "—",
            20.0 * math.log10(m["gain"]) if m["gain"] > 0 else 0.0))
    lines.append("")
    lines.append("## What the corpus cannot tell you")
    lines.append("")
    lines.append("- A clip is 4.0 s: 1.0 s before the trigger and 3.0 s after. If the event is at "
                 "the very start you are hearing its tail only.")
    lines.append("- The tagger has never run on these. Nothing here is a model's opinion; that is "
                 "the point.")
    lines.append("")
    open(os.path.join(out, "sheet.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", default="/pool", help="pool root holding corpus/clips/")
    ap.add_argument("--out", required=True,
                    help="directory to stage into; created, and refused if it already holds files")
    ap.add_argument("--force", action="store_true",
                    help="stage into a non-empty --out anyway (earlier runs' files stay mixed in)")
    ap.add_argument("--n", type=int, default=30, help="clips to stage (gate wants >= 30)")
    ap.add_argument("--node", action="append", default=None,
                    help="restrict to this node; repeatable")
    ap.add_argument("--require-node", action="append", default=["mach"],
                    help="guarantee at least one clip from this node; repeatable "
                         "(gate names mach)")
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the sample. Omit for a fresh draw; record it for a repeatable one")
    args = ap.parse_args()

    rows = load_index(args.pool)
    if args.node:
        rows = [r for r in rows if r.get("node") in set(args.node)]
    if not rows:
        raise SystemExit("no stored clips matched")

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    require = [nd for nd in (args.require_node or [])
               if any(r.get("node") == nd for r in rows)]
    missing = [nd for nd in (args.require_node or []) if nd not in require]
    picked = stratify(rows, args.n, rng, require)

    if os.path.isdir(args.out) and os.listdir(args.out) and not args.force:
        raise SystemExit("%s already holds files; staging into it would mix this run's clips "
                         "and sheet with an earlier run's. Use an empty directory, or --force."
                         % args.out)
    os.makedirs(args.out, exist_ok=True)
    staged = []
    kept = []
    skipped = []
    for r in picked:
        src = os.path.join(args.pool, "corpus", r["path"])
        if not os.path.exists(src):
            skipped.append({"path": r["path"], "why": "indexed but not on disk (pruned)"})
            continue
        try:
            samples, fs, _ = read_pcm(src)
        except (wave.Error, ValueError) as e:
            skipped.append({"path": r["path"], "why": str(e)})
            continue
        m = measure(samples, fs)
        g, bound = gain_for(m)
        base = os.path.basename(r["path"])
        shutil.copy2(src, os.path.join(args.out, base))
        loud = base[:-4] + ".loud.wav" if base.endswith(".wav") else base + ".loud.wav"
        clipped = write_loud(samples, fs, g, os.path.join(args.out, loud))
        m.update({"gain": g, "gain_db": round(20.0 * math.log10(g), 2) if g > 0 else 0.0,
                  "gain_bound_by": bound, "clipped_samples": clipped,
                  "wav_fs_hz": fs, "loud": loud, "orig": base})
        staged.append(m)
        kept.append(r)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "pool": os.path.abspath(args.pool),
            "seed": seed,
            "requested": args.n,
            "staged": len(kept),
            "skipped": skipped,
            "require_node_unavailable": missing,
            "rms_target_dbfs": RMS_TARGET_DBFS,
            "peak_target_dbfs": PEAK_TARGET_DBFS,
            "silence_frame_dbfs": SILENCE_FRAME_DBFS,
            "silence_frame_ms": SILENCE_FRAME_MS,
            "threshold_source": ("--max-silence-frac must come from rms_dbfs_orig/silence_frac "
                                 "below, which are measured on the ORIGINAL clip; the .loud.wav "
                                 "copies exist only to be audible"),
            "clips": [dict(m, **{k: r.get(k) for k in
                                 ("node", "path", "clip_key", "ts_utc_s", "anchored", "trigger",
                                  "wav_header_fs_hz", "fs_hz", "boot")})
                      for r, m in zip(kept, staged)],
        }, f, indent=2, sort_keys=True)

    with open(os.path.join(args.out, "listen.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r, m in zip(kept, staged):
            f.write("#EXTINF:%d,%s %s %+.0f dB\n" % (
                int(m["dur_s"]), r.get("node"), m["orig"], m["gain_db"]))
            f.write(m["loud"] + "\n")

    import datetime
    sheet(kept, staged, args.out, datetime.date.today().isoformat())

    print("staged %d clip(s) into %s (seed %d)" % (len(kept), args.out, seed))
    by_node = collections.Counter(r.get("node") for r in kept)
    print("  per node: %s" % dict(by_node))
    if missing:
        print("  ⚠️no clips available from required node(s): %s" % ", ".join(missing))
    if len(kept) < args.n:
        print("  ⚠️asked for %d, staged %d (%d skipped)" % (args.n, len(kept), len(skipped)))
    for s in skipped:
        print("    skipped %s: %s" % (s["path"], s["why"]))
    lv = [m["rms_dbfs_orig"] for m in staged if m["rms_dbfs_orig"] != -math.inf]
    if lv:
        print("  original level: %.1f to %.1f dBFS -- raw playback of these is silence, use "
              "the .loud.wav copies" % (min(lv), max(lv)))
    print("  sheet.md + manifest.json + listen.m3u written alongside")


if __name__ == "__main__":
    main()
