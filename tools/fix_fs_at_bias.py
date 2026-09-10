#!/usr/bin/env python3
"""Undo the fs_clean latch's timestamp bias in a captured dets.csv.

⚠️WHY A CAPTURE NEEDS THIS. hear_node back-dates each sample's stamp from the end of the block
it arrived in:

    back_us = (BLOCK - 1 - i) * 1e6 / fs_at        (hear_node.ino, at the gate edge)
    cap_us  = esp_timer_get_time() - back_us

`fs_at` is the node's own measured rate, and on mach it LATCHED at 22624.0 Hz -- the short-second
test compared a second against the very average it updates, so once the value drifted high every
real second looked short and the window that would recompute it was reset before it could ever
reach the 8 seconds it needed. 1124 of 1126 health rows carry 22624.0000 with fs_win_s pinned at
0. Dividing by a rate 41% too high makes back_us too SMALL, so every stamp is too LATE by

    delta = (BLOCK - 1 - (sample mod BLOCK)) * 1e6 * (1/FS_TRUE - 1/fs_at)   microseconds

which is 18.3 us per sample of block position at fs_at = 22624, capping at 255 * 18.3 = 4.67 ms
for a sample at the START of a block and zero for one at its end. The firmware is fixed; this
recovers the captures taken before it was.

⚠️IT CORRECTS ONLY WHAT IT CAN DERIVE. The bias is a function of block position, which dets.csv
records exactly (`sample`), so the correction is deterministic and not an estimate. It does NOT
touch anything else the latch inflated -- drop_samples, the WAV header rate on clips from that
boot -- and it cannot recover a row whose utc_us was 0 to begin with.
"""
from __future__ import annotations

import argparse
import csv
import sys

BLOCK = 256          # hear_node BLOCK, and MELIMP_NFFT
FS_TRUE = 16000.0    # FS_NOMINAL; the crystal is tens of ppm from it, not percent


def bias_us(sample: int, fs_at: float, fs_true: float = FS_TRUE, block: int = BLOCK) -> float:
    """How many microseconds LATE this row's stamp is. Zero when fs_at is already right."""
    if fs_at <= 0:
        return 0.0
    return (block - 1 - (int(sample) % block)) * 1e6 * (1.0 / fs_true - 1.0 / fs_at)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dets_csv")
    ap.add_argument("--out", help="write a corrected copy here")
    ap.add_argument("--fs-true", type=float, default=FS_TRUE)
    ap.add_argument("--tolerance-ppm", type=float, default=10000.0,
                    help="fs_at within this of fs-true is treated as sound and left alone")
    a = ap.parse_args(argv)

    rows = list(csv.reader(open(a.dets_csv)))
    hdr, body = rows[0], [r for r in rows[1:] if r]
    # ⚠️positional, not by name: captures written before the node_id fix declare 12 columns and
    # write 11, so every header name is off by one. Detect it rather than trust the header.
    shifted = bool(body) and len(body[0]) == len(hdr) - 1
    names = hdr[1:] if shifted else hdr
    try:
        i_s, i_fs, i_utc = names.index("sample"), names.index("fs_hz"), names.index("utc_us")
    except ValueError as e:
        print("cannot locate a required column: %s" % e, file=sys.stderr)
        return 2

    n_fixed = n_skip = n_unstamped = 0
    deltas = []
    out = [hdr]
    for r in body:
        if len(r) != len(names):
            out.append(r); n_skip += 1; continue
        try:
            fs_at = float(r[i_fs]); samp = int(r[i_s]); utc = int(r[i_utc])
        except ValueError:
            out.append(r); n_skip += 1; continue
        if not utc:
            out.append(r); n_unstamped += 1; continue
        if abs(fs_at - a.fs_true) / a.fs_true * 1e6 <= a.tolerance_ppm:
            out.append(r); n_skip += 1; continue
        d = bias_us(samp, fs_at, a.fs_true)
        deltas.append(d)
        r = list(r); r[i_utc] = str(int(round(utc - d)))
        out.append(r); n_fixed += 1

    print("%s: %d rows (%s header)" % (a.dets_csv, len(body),
                                       "SHIFTED, read positionally" if shifted else "aligned"))
    print("  corrected     %d" % n_fixed)
    print("  fs_at sound   %d" % n_skip)
    print("  never stamped %d  (utc_us = 0; nothing to correct)" % n_unstamped)
    if deltas:
        import statistics as st
        print("  bias removed: median %.0f us  min %.0f  max %.0f  (cap %.0f us)"
              % (st.median(deltas), min(deltas), max(deltas),
                 (BLOCK - 1) * 1e6 * (1.0 / a.fs_true - 1.0 / max(
                     float(r2[i_fs]) for r2 in body if len(r2) == len(names) and r2[i_fs]))))
    if a.out:
        with open(a.out, "w", newline="") as fh:
            csv.writer(fh).writerows(out)
        print("  wrote %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
