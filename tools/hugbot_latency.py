#!/usr/bin/env python3
"""Measure how far hugbot's ESP ring is from UTC, and refuse to call it an arrival time.

    # on hugbot (needs the live tap and ring):
    python3 tools/hugbot_latency.py probe --trials 40 --seg-s 0.25 --out trials.json
    # anywhere:
    python3 tools/hugbot_latency.py report --from-json trials.json

WHY THIS EXISTS. hugbot is the obvious fourth receiver for the acoustic array: 3 x ESP32-S3 over
USB = 6 mics at 48 kHz, it already answers `dama/colony/audio/pull`, and it is the only node whose
microphone geometry was measured with a tape rather than assumed. The brief said its one blocker
was an "unmeasured ALSA/USB capture-latency bias (stated 1-20 ms, never checked)".

⚠️THAT PREMISE IS WRONG IN BOTH HALVES, and this tool exists because the real blocker is elsewhere.

  * It was measured. `perception/capture_latency.conf` on hugbot carries 0.1784917 s +/- 0.01130,
    from `capture_latency_bench.py` by same-clock self-loopback on 2026-09-04 (two boards:
    173.77 ms sigma 5.96, 183.33 ms sigma 5.68). That is ~9x the top of the modelled 1-20 ms.
  * And it is not what stops hugbot. A CONSTANT latency, however large, is a bias you subtract.
    What actually stops hugbot is that no ESP ring frame can be dated at all (`t_anchor_ns == 0`,
    measured live), and that the offset the pull protocol uses instead is not constant: measured
    here it moves over a ~210 ms range that PortAudio's own reported backlog does not predict.

WHAT IS BEING MEASURED, and why it needs no speaker and no second microphone.

`perception/esp_tap.py` ships each block with PortAudio's `inputBufferAdcTime`, the stream clock
read in the same callback, and CLOCK_REALTIME -- three raw terms. `perception/esp_ring_feed.py`
consumes that same stream and writes the ring, DISCARDING all three (`esp_ring_feed.py:395`
unpacks them into `_adc, _cur, _wall`). So the tap and the ring carry the identical samples under
two different time axes, and cross-correlating them recovers the offset between those axes exactly
-- no emitter, no co-located reference, no acoustics. The correlation is between a signal and
itself, so a true alignment reads rho ~= 1.0 and anything less is a failure to align, not a noisy
answer. That is the discriminator this tool leans on, and `--rho-min` is where it is set.

    lag = t_pull(frame carrying tap sample 0) - t_adc(tap sample 0)

`t_pull` is the axis `perception/ring_pull_capture.py` actually serves to the fleet: it maps the
newest ring frame to the instant of the read ("THE WINDOW IS APPROXIMATE", its own docstring says).
`t_adc` is PortAudio's claim about the same sample. So `lag` is the error in the served axis
RELATIVE TO the driver's model -- one term of the total, isolated.

⚠️IT IS NOT THE WHOLE BIAS, AND THE REST IS NOT MEASURABLE FROM THE PI. The full chain is

    air -> mic -> ESP32 I2S DMA -> USB UAC -> Pi USB host -> ALSA -> PortAudio -> tap -> ring -> pull
           |______________ B_device _______________|________ B_host ________|_____ B_pull _____|

`B_pull` is what this tool measures. `B_host` is PortAudio's own claim, reported here as a
distribution because it is not a constant. `B_device` -- everything inside the ESP32 before the USB
frame -- is INVISIBLE to PortAudio by construction: the driver cannot see a buffer on the far side
of the wire. Only an acoustic measurement against an external clock separates it, which is what
`capture_latency_bench.py` (self-loopback) and `tools/hear_latency_cal.py` (co-located PPS node)
are for. This tool therefore reports a FLOOR on the total bias and says so; it never adds the three
terms into one confident number.

⚠️AND IT REPORTS A DISTRIBUTION, NEVER A POINT. A latency you can subtract is a constant. The
whole question for hugbot is whether this offset IS one, so a median with no spread beside it would
answer the wrong question. `summarise()` always carries p05/p50/p95, the MAD and the full range,
and `--max-spread-ms` refuses outright rather than emitting a middle for a distribution too wide to
have a referent.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

#: Speed of sound at 20 C, for turning a time into the distance it costs. Nominal: used to EXPLAIN
#: a magnitude to a reader, never to correct one.
C_MPS = 343.0

#: The one-way arrival budget, from docs/node-hardware.md: one degree of unmeasured air temperature
#: over the array's 35 m long baseline. `c = 331.3 + 0.606*T`, so 35 * 0.606 / 343^2 = 180 us, which
#: the doc rounds to 183. It is site-independent and is the size of an error the design already
#: tolerates, which is why it is the yardstick here rather than any one pair's geometry.
#: `hear/nodeclass.py` gains the same constant on the arrival-gate branch; when that lands this
#: should import from there rather than keep a second copy.
ARRIVAL_ONE_WAY_BUDGET_S = 183e-6

#: hugbot's measured capture-path latency, read from perception/capture_latency.conf on the robot.
#: Quoted here ONLY so this tool can state the term it does not itself measure; it is never a
#: default. A caller with no measurement gets a refusal, because a zero would assert a perfect
#: transport -- the same rule perception/esp_ring_correlate.py applies.
HUGBOT_CAPTURE_LATENCY_S = 0.1784917
HUGBOT_CAPTURE_SIGMA_S = 0.01130

#: Widest distance between any two of hugbot's six ESP microphones, computed from
#: perception/array_geometry.py (operator tape measurement, 2026-08-22) as the max pairwise
#: distance over ESP_ARRAYS: rear-array mic to right-array mic. Present only to give the measured
#: channel skew a physical scale -- a skew larger than the aperture means the columns cannot be
#: describing one wavefront at all.
HUGBOT_ESP_APERTURE_M = 0.7388

TAP_HELLO = b"ESPT2 "
TAP_FRAME_HDR = "<BIddq"           # board, nframes, adc_time, stream_time, wall_ns
TAP_FRAME_HDR_N = struct.calcsize(TAP_FRAME_HDR)
RING_HDR = "<4sIIIIIQ"             # perception/audio_ring.py: magic, ver, sr, ch, cap, dtype, wc
RING_HDR_N = struct.calcsize(RING_HDR)
RING_HDR_PAD = 64                  # audio_ring._HDR_PAD: data starts here
RING_EXT = "<qQq"                  # t_anchor_ns, wc_anchor, gap_frames
RING_EXT_OFF = RING_HDR_N


class LatencyError(Exception):
    """A refusal with a reason the operator can act on."""


# --- pure core ----------------------------------------------------------------------------------

def normalised_peak(seg, ref) -> float:
    """Pearson correlation of two equal-length, mean-removed segments.

    Normalised, because the discriminator this whole measurement rests on is "rho ~= 1.0 means
    these are literally the same samples". A raw correlation peak cannot say that: it grows with
    amplitude, so a loud misalignment outscores a quiet true one.
    """
    import numpy as np

    a = np.asarray(ref, dtype=np.float64)
    b = np.asarray(seg, dtype=np.float64)
    if a.shape != b.shape:
        raise LatencyError("normalised_peak needs equal lengths, got %r and %r"
                           % (a.shape, b.shape))
    a = a - a.mean()
    b = b - b.mean()
    den = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    if den <= 0.0:
        return 0.0
    return float((a * b).sum() / den)


def align_in_channel(tap, ring_col) -> Tuple[float, int]:
    """Best (rho, offset) placing `tap` inside `ring_col`. Offset indexes `ring_col`.

    Searches the WHOLE ring rather than a window around an expected lag. A window would be an
    assumption about the answer, and the first thing this measurement found was that the answer
    moves by more than any window worth choosing.
    """
    import numpy as np
    from scipy.signal import correlate

    a = np.asarray(tap, dtype=np.float64)
    y = np.asarray(ring_col, dtype=np.float64)
    if a.size == 0 or y.size < a.size:
        raise LatencyError("ring column (%d) must be at least the tap segment (%d)"
                           % (y.size, a.size))
    if a.std() <= 0.0 or y.std() <= 0.0:
        return 0.0, 0
    c = correlate(y - y.mean(), a - a.mean(), mode="valid")
    k = int(np.argmax(c))
    return normalised_peak(y[k:k + a.size], a), k


def best_channel(tap, ring) -> Tuple[float, int, int]:
    """(rho, channel, offset) for the ring column that best carries `tap`.

    The column is DERIVED rather than taken from esp_ring_feed's documented slot i -> columns 2i.
    Not because that mapping is wrong -- checked live on 2026-09-10 over repeated snapshots it
    holds exactly (93F8->0, 9838->2, 7C4C->4) -- but because this function is also asked about
    boards that are NOT PRESENT in the ring at that instant, and for those the argmax lands on
    whichever column happens to correlate best. That answer is meaningless, and the ONLY thing
    that distinguishes it from a real one is rho. So the column returned here is provisional and
    a caller must gate on rho before believing it; `summarise` is where that gate lives.
    """
    import numpy as np

    r = np.asarray(ring)
    if r.ndim != 2:
        raise LatencyError("ring must be [frames, channels], got %r" % (r.shape,))
    best = (0.0, -1, 0)
    for col in range(r.shape[1]):
        rho, off = align_in_channel(tap, r[:, col])
        if best[1] < 0 or rho > best[0]:
            best = (rho, col, off)
    return best


def pull_axis_lag_s(t_snap: float, write_counter: int, sample_rate: int,
                    frame_of_tap0: int, t_adc_tap0: float) -> float:
    """`t_pull(frame) - t_adc(sample)` for the frame that carries the tap's first sample.

    `t_pull` reproduces ring_pull_capture.read_window exactly: the newest frame is assumed to be
    the instant of the read, and every earlier frame is one sample period further back. Positive
    means the served axis dates the audio LATER than PortAudio says it was captured.
    """
    if sample_rate <= 0:
        raise LatencyError("sample_rate must be positive, got %r" % (sample_rate,))
    t_pull = t_snap - (int(write_counter) - int(frame_of_tap0)) / float(sample_rate)
    return float(t_pull - t_adc_tap0)


def _quantiles(vals: Sequence[float]) -> Dict[str, float]:
    import numpy as np

    v = np.asarray(sorted(vals), dtype=np.float64)
    med = float(np.median(v))
    return {
        "n": int(v.size),
        "min_ms": float(v.min()), "p05_ms": float(np.percentile(v, 5)),
        "p50_ms": med, "p95_ms": float(np.percentile(v, 95)), "max_ms": float(v.max()),
        "mad_ms": float(np.median(np.abs(v - med))),
        "spread_ms": float(np.percentile(v, 95) - np.percentile(v, 5)),
    }


def summarise(trials: Sequence[Dict], rho_min: float = 0.9, min_trials: int = 8,
              max_spread_ms: Optional[float] = None) -> Dict:
    """Per-board lag distributions, or a refusal that names what was missing.

    A refused board carries NO `lag` key at all -- not a null and not a zero. A consumer that
    forgets to check `status` gets a KeyError rather than a plausible-looking number, the same
    rule capture_latency_bench.py applies to its own refusals.
    """
    by: Dict[str, List[Dict]] = {}
    for t in trials:
        by.setdefault(str(t["serial"]), []).append(t)

    out: Dict[str, Dict] = {}
    for serial, rows in sorted(by.items()):
        good = [r for r in rows if float(r["rho"]) >= rho_min]
        cols = sorted({int(r["ring_ch"]) for r in good})
        rec: Dict = {
            "serial": serial,
            "trials": len(rows),
            "aligned": len(good),
            "aligned_frac": len(good) / float(len(rows)) if rows else 0.0,
            "rho_min": rho_min,
            "ring_columns_seen": cols,
        }
        if len(good) < min_trials:
            rec["status"] = "refused_unalignable"
            rec["reason"] = (
                "only %d of %d trials reached rho >= %.2f; the tap and the ring carry the same "
                "samples, so a true alignment is rho ~= 1.0 -- failing to find one means the ring "
                "does not hold a findable copy of this board's stream, not that the lag is noisy"
                % (len(good), len(rows), rho_min))
            out[serial] = rec
            continue
        if len(cols) > 1:
            rec["status"] = "refused_column_unstable"
            rec["reason"] = ("aligned trials landed in more than one ring column %r -- the board's "
                             "audio has no fixed home in the ring, so no per-board lag can be "
                             "attributed" % (cols,))
            out[serial] = rec
            continue
        dist = _quantiles([float(r["lag_ms"]) for r in good])
        rec["ring_column"] = cols[0]
        if max_spread_ms is not None and dist["spread_ms"] > max_spread_ms:
            rec["status"] = "refused_spread"
            rec["reason"] = ("p05..p95 spans %.1f ms, past the %.1f ms a caller was willing to "
                             "call one offset; a median here has no referent"
                             % (dist["spread_ms"], max_spread_ms))
            rec["lag_rejected"] = dist
            out[serial] = rec
            continue
        rec["status"] = "ok"
        rec["lag"] = dist
        out[serial] = rec
    return out


def channel_skew(trials: Sequence[Dict], rho_min: float = 0.9) -> Dict:
    """Pairwise inter-board skew inside one ring, from snapshots where both boards aligned.

    WHY THIS IS A SEPARATE QUESTION FROM THE LAG. The lag above asks "is hugbot's ring on UTC",
    which is what makes it a fourth RECEIVER. This asks "are the ring's six columns on the same
    clock as each other", which is what makes it an ARRAY at all -- every bearing hugbot publishes
    from esp_doa_stream, and its whole claim to measured geometry, rests on the six columns being
    simultaneous.

    Both boards are stamped in the SAME snapshot, so t_snap and write_counter cancel and the
    skew is just the difference of the two lags -- no ring-wide time axis needed. That is why
    this survives on a ring that has no anchor at all.
    """
    by_trial: Dict[int, List[Dict]] = {}
    for t in trials:
        if float(t["rho"]) >= rho_min:
            by_trial.setdefault(int(t["trial"]), []).append(t)

    pairs: Dict[str, List[float]] = {}
    for rows in by_trial.values():
        rows = sorted(rows, key=lambda r: str(r["serial"]))
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                if rows[i]["serial"] == rows[j]["serial"]:
                    # One board cannot be skewed against itself; a duplicate row would otherwise
                    # manufacture a perfect 0.0 ms pair and dilute every real one.
                    continue
                key = "%s/%s" % (rows[i]["serial"], rows[j]["serial"])
                pairs.setdefault(key, []).append(
                    float(rows[i]["lag_ms"]) - float(rows[j]["lag_ms"]))

    out: Dict[str, Dict] = {}
    for key, vals in sorted(pairs.items()):
        rec: Dict = {"pair": key, "n": len(vals)}
        if len(vals) < 3:
            rec["status"] = "refused_too_few_joint_alignments"
            rec["reason"] = ("only %d snapshot(s) aligned both boards at rho >= %.2f; a skew needs "
                             "both boards findable in the SAME ring read" % (len(vals), rho_min))
        else:
            rec["status"] = "ok"
            rec["skew"] = _quantiles(vals)
        out[key] = rec
    return out


def arrival_verdict(lag: Dict, capture_latency_s: Optional[float] = None,
                    capture_sigma_s: Optional[float] = None,
                    budget_s: float = ARRIVAL_ONE_WAY_BUDGET_S) -> Dict:
    """Can a receiver with this lag distribution contribute an arrival time?

    The test is on the SPREAD, not the median. A median is a bias and a bias is subtractable; what
    disqualifies a receiver is the part of the offset that changes between events. `capture_*` are
    optional and, when given, widen the answer -- they can never narrow it, because they describe a
    term measured somewhere else on a different day.
    """
    if capture_latency_s is None:
        raise LatencyError(
            "arrival_verdict needs the capture-path latency stated explicitly; there is no "
            "default because a zero would assert a perfect transport (see capture_latency.conf)")
    half_spread_s = 0.5 * float(lag["spread_ms"]) * 1e-3
    sigma_s = half_spread_s
    if capture_sigma_s:
        sigma_s = math.sqrt(half_spread_s ** 2 + float(capture_sigma_s) ** 2)
    return {
        "budget_s": budget_s,
        "budget_m": budget_s * C_MPS,
        "lag_half_spread_s": half_spread_s,
        "capture_latency_s": float(capture_latency_s),
        "capture_sigma_s": float(capture_sigma_s) if capture_sigma_s else None,
        "sigma_s": sigma_s,
        "sigma_m": sigma_s * C_MPS,
        "over_budget_factor": sigma_s / budget_s,
        "admissible": sigma_s <= budget_s,
        "floor_only": True,
        "floor_note": (
            "this is a FLOOR: it contains the ring/pull term measured here and the stated capture "
            "term, and excludes everything inside the ESP32 ahead of the USB frame, which no "
            "host-side measurement can see"),
    }


# --- live probes (hugbot only) ------------------------------------------------------------------

def read_ring(path: str) -> Dict:
    """One whole-file snapshot of the ESP ring, with the instant it was taken.

    Read as a single `read()` and stamped immediately after, so the write counter and the samples
    come from as near one instant as a userspace reader can manage. The ring is opened READ-ONLY:
    this tool never writes hugbot's ring header, which is pps_name_second's job.
    """
    import numpy as np

    with open(path, "rb") as fh:
        buf = fh.read()
    t_snap = time.time()
    magic, ver, sr, ch, cap, _dt, wc = struct.unpack(RING_HDR, buf[:RING_HDR_N])
    if magic != b"HARB":
        raise LatencyError("%s is not an audio ring (magic %r)" % (path, magic))
    t_anchor, wc_anchor, gap = struct.unpack_from(RING_EXT, buf, RING_EXT_OFF)
    data = np.frombuffer(buf, dtype=np.int16, count=cap * ch,
                         offset=RING_HDR_PAD).reshape(cap, ch)
    oldest_first = data[(np.arange(cap) + (wc % cap)) % cap]
    return {"sr": int(sr), "channels": int(ch), "capacity": int(cap), "write_counter": int(wc),
            "t_snap": t_snap, "data": oldest_first, "first_frame": int(wc) - int(cap),
            "t_anchor_ns": int(t_anchor), "wc_anchor": int(wc_anchor), "gap_frames": int(gap)}


def grab_tap(host: str, port: int, seconds: float, timeout: float = 30.0) -> Dict:
    """Collect `seconds` of every board from the ESP tap, with PortAudio's stamps.

    A FRESH CONNECTION PER CALL, and closed before any analysis runs. esp_tap fans out to a bounded
    per-client deque and drops a slow client's frames; correlating while still attached made it
    drop this one mid-run and then reset the connection. Reconnecting costs milliseconds and the
    tap is built for clients arriving and leaving.
    """
    import numpy as np

    s = socket.create_connection((host, port), timeout=timeout)
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise LatencyError("tap closed during handshake")
            buf += chunk
        head, buf = buf.split(b"\n", 1)
        if not head.startswith(TAP_HELLO):
            raise LatencyError("unexpected tap hello %r" % (head[:16],))
        meta = json.loads(head[len(TAP_HELLO):].decode("utf-8"))
        fs, nch = int(meta["fs"]), int(meta["channels"])
        n_boards = len(meta["boards"])

        def rd(n: int) -> bytes:
            nonlocal buf
            while len(buf) < n:
                d = s.recv(1 << 20)
                if not d:
                    raise LatencyError("tap closed mid-frame")
                buf += d
            out, buf = buf[:n], buf[n:]
            return out

        need = int(seconds * fs)
        pcm: Dict[int, List] = {i: [] for i in range(n_boards)}
        # First block per board carries the stamp the whole measurement hangs on. Blocks whose
        # tinfo was absent arrive NaN by the tap's own design; those are skipped, never zeroed.
        mark: Dict[int, Optional[Tuple[int, float, float]]] = {i: None for i in range(n_boards)}
        while min(sum(a.shape[0] for a in v) for v in pcm.values()) < need:
            idx, nf, adc, cur, wall = struct.unpack(TAP_FRAME_HDR, rd(TAP_FRAME_HDR_N))
            raw = rd(nf * nch * 2)
            if not (0 <= idx < n_boards):
                continue
            if mark[idx] is None and adc == adc and cur == cur:
                mark[idx] = (sum(a.shape[0] for a in pcm[idx]),
                             wall / 1e9 - (cur - adc), cur - adc)
            pcm[idx].append(np.frombuffer(raw, dtype="<i2").reshape(-1, nch))
    finally:
        s.close()
    return {"meta": meta, "fs": fs, "need": need,
            "pcm": {i: np.concatenate(v) for i, v in pcm.items()}, "mark": mark}


def probe(host: str, port: int, ring_path: str, trials: int, seg_s: float) -> List[Dict]:
    """Run `trials` independent tap-vs-ring alignments and return one row per board per trial."""
    import numpy as np

    rows: List[Dict] = []
    for trial in range(trials):
        try:
            tap = grab_tap(host, port, seg_s)
            ring = read_ring(ring_path)
        except (LatencyError, OSError) as exc:
            print("trial %d: %s" % (trial, exc), file=sys.stderr)
            time.sleep(1.0)
            continue
        serials = [b.get("serial") for b in tap["meta"]["boards"]]
        for slot, block in sorted(tap["pcm"].items()):
            if tap["mark"][slot] is None:
                continue
            seg = block[:tap["need"], 0].astype(np.float64)
            if seg.size < tap["need"] or seg.std() <= 0.0:
                continue
            m0, t_adc0, host_backlog = tap["mark"][slot]
            t_adc = t_adc0 - m0 / float(tap["fs"])
            rho, col, off = best_channel(seg, ring["data"])
            rows.append({
                "trial": trial, "slot": slot, "serial": serials[slot], "ring_ch": col,
                "rho": rho,
                "lag_ms": 1e3 * pull_axis_lag_s(ring["t_snap"], ring["write_counter"],
                                                ring["sr"], ring["first_frame"] + off, t_adc),
                "host_backlog_ms": 1e3 * host_backlog,
                "t_snap": ring["t_snap"], "write_counter": ring["write_counter"],
                "ring_anchored": ring["t_anchor_ns"] != 0,
                "ring_gap_frames": ring["gap_frames"],
                "rms": float(seg.std()),
            })
    return rows


# --- report -------------------------------------------------------------------------------------

def render(trials: Sequence[Dict], rho_min: float, min_trials: int,
           max_spread_ms: Optional[float]) -> str:
    lines: List[str] = []
    anchored = {bool(t.get("ring_anchored")) for t in trials}
    lines.append("ESP ring anchored (audio_ring t_anchor_ns != 0): %s"
                 % (", ".join(sorted(str(a) for a in anchored)) or "unknown"))
    if anchored == {False}:
        lines.append("  -> frame_time_ns() returns None for every frame: nothing in production")
        lines.append("     converts an ESP ring frame to UTC, so the pull axis below is the ONLY")
        lines.append("     time this audio has.")
    backlog = [float(t["host_backlog_ms"]) for t in trials if "host_backlog_ms" in t]
    if backlog:
        b = _quantiles(backlog)
        lines.append("")
        lines.append("PortAudio reported backlog (currentTime - inputBufferAdcTime), n=%d:" % b["n"])
        lines.append("  p05 %.1f  p50 %.1f  p95 %.1f  ms   [min %.1f, max %.1f]"
                     % (b["p05_ms"], b["p50_ms"], b["p95_ms"], b["min_ms"], b["max_ms"]))
        lines.append("  This is the driver's own model of how far back the block starts. It is not")
        lines.append("  a constant, so it is not a bias anyone can subtract.")

    summary = summarise(trials, rho_min=rho_min, min_trials=min_trials,
                        max_spread_ms=max_spread_ms)
    lines.append("")
    lines.append("Per board, lag = t_pull - t_adc  (the served axis minus PortAudio's claim):")
    for serial, rec in summary.items():
        lines.append("")
        lines.append("  %s  aligned %d/%d trials (%.0f%%) at rho >= %.2f, columns %r"
                     % (serial, rec["aligned"], rec["trials"], 100 * rec["aligned_frac"],
                        rec["rho_min"], rec["ring_columns_seen"]))
        if rec["status"] != "ok":
            lines.append("    %s: %s" % (rec["status"], rec["reason"]))
            continue
        d = rec["lag"]
        lines.append("    ring column %d" % rec["ring_column"])
        lines.append("    lag ms: p05 %.1f  p50 %.1f  p95 %.1f   [min %.1f, max %.1f]  MAD %.1f"
                     % (d["p05_ms"], d["p50_ms"], d["p95_ms"], d["min_ms"], d["max_ms"],
                        d["mad_ms"]))
        v = arrival_verdict(d, HUGBOT_CAPTURE_LATENCY_S, HUGBOT_CAPTURE_SIGMA_S)
        lines.append("    half-spread %.1f ms = %.1f m of range"
                     % (1e3 * v["lag_half_spread_s"], v["lag_half_spread_s"] * C_MPS))
        lines.append("    vs the %.0f us (%.1f cm) one-way arrival budget: %s, over by %.0fx"
                     % (1e6 * v["budget_s"], 100 * v["budget_m"],
                        "ADMISSIBLE" if v["admissible"] else "REFUSED", v["over_budget_factor"]))
        lines.append("    %s" % v["floor_note"])

    skew = channel_skew(trials, rho_min=rho_min)
    lines.append("")
    lines.append("Inter-board skew inside the ring (are the six columns one array?):")
    if not skew:
        lines.append("  no snapshot aligned two boards at once -- cannot say.")
    for key, rec in skew.items():
        if rec["status"] != "ok":
            lines.append("  %s: %s" % (key, rec["reason"]))
            continue
        d = rec["skew"]
        lines.append("  %s  n=%d  skew ms: p05 %.1f  p50 %.1f  p95 %.1f  [min %.1f, max %.1f]"
                     % (key, rec["n"], d["p05_ms"], d["p50_ms"], d["p95_ms"],
                        d["min_ms"], d["max_ms"]))
        # Deliberately NOT "the skew is <p50>". At these n the median is one draw from a
        # distribution spanning seconds, and quoting it would invent a constant. What the numbers
        # can support is the comparison against the only bound physics allows.
        lines.append("     any real skew between two mics of one array is bounded by the aperture:"
                     " %.3f m / %.0f m/s = %.2f ms." % (HUGBOT_ESP_APERTURE_M, C_MPS,
                                                        1e3 * HUGBOT_ESP_APERTURE_M / C_MPS))
        lines.append("     observed p05..p95 spans %.0f ms, %.0fx that bound: on this evidence the"
                     % (d["spread_ms"], d["spread_ms"] / (1e3 * HUGBOT_ESP_APERTURE_M / C_MPS)))
        lines.append("     six columns are not samples of one wavefront.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="run live against hugbot's tap and ring")
    p.add_argument("--tap-host", default="127.0.0.1")
    p.add_argument("--tap-port", type=int, default=8720)
    p.add_argument("--ring", default="/dev/shm/hugbot_esp_audio.ring")
    p.add_argument("--trials", type=int, default=40)
    p.add_argument("--seg-s", type=float, default=0.25)
    p.add_argument("--out", required=True)

    r = sub.add_parser("report", help="summarise recorded trials")
    r.add_argument("--from-json", required=True)
    for q in (p, r):
        q.add_argument("--rho-min", type=float, default=0.9)
        q.add_argument("--min-trials", type=int, default=8)
        q.add_argument("--max-spread-ms", type=float, default=None)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "probe":
            rows = probe(a.tap_host, a.tap_port, a.ring, a.trials, a.seg_s)
            if not rows:
                raise LatencyError("no trial produced a usable segment")
            with open(a.out, "w") as fh:
                json.dump(rows, fh, indent=1)
            print("wrote %d rows to %s" % (len(rows), a.out), file=sys.stderr)
        else:
            with open(a.from_json) as fh:
                rows = json.load(fh)
        print(render(rows, a.rho_min, a.min_trials, a.max_spread_ms))
    except LatencyError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
