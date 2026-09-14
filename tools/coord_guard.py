#!/usr/bin/env python3
"""Fail when a decimal lat/lon pair far from survey.json's fictional origin is in the repo.

    python3 tools/coord_guard.py tree [--rev HEAD]
    python3 tools/coord_guard.py range BASE HEAD      # every blob each commit in BASE..HEAD adds
    python3 tools/coord_guard.py range '' HEAD        # the whole history of HEAD

Output names commit, path and line only, never a coordinate value, a distance or a digest: CI
logs on this repo are public. `--show-digests` exists for local allowlisting and must not be used
in CI.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass

RADIUS_KM = 25.0
MAX_BLOB_BYTES = 2 * 1024 * 1024
LINE_WINDOW = 3
EARTH_KM = 6371.0088
ZERO_SHA = "0" * 40
ALLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coord_guard_allow.txt")

_NUM = r"(-?\d{1,3}\.\d{4,})(?!\d|[eE][-+]?\d)"
FRACTION = re.compile(r"\.\d{4,}(?!\d|[eE][-+]?\d)")
DIGITS = frozenset("0123456789")
SEPARATOR = re.compile(r"[ \t]*(?:[NSns°][ \t]*)?(?:[,;][ \t]*|[ \t]+)(?:[NSEWnsew°][ \t]*)?")
# Every dash/minus look-alike Unicode offers, folded to ASCII "-": hyphen, non-breaking hyphen,
# figure dash, en/em dash, two horizontal bars, minus sign, small hyphen-minus, small em dash,
# fullwidth hyphen-minus. One code point each, so folding never shifts a later offset.
_DASHES = "‐‑‒–—―−﹘﹣－"
_UNICODE_MINUS = str.maketrans({c: "-" for c in _DASHES})
_PERCENT_SEPARATOR = re.compile(r"%(2[Cc]|3[Bb])")
_PERCENT_SEPARATOR_MAP = {"2c": ",", "3b": ";"}


def _normalize(text):
    """ASCII-fold dash/minus look-alikes and the %2C/%3B separator escapes a URL query string
    would carry, so tokenizing never has to special-case them. Called once, from candidates();
    numbers() takes text as given and never renormalizes it, so positions it returns are always
    relative to whatever text its caller passed in."""
    text = text.translate(_UNICODE_MINUS)
    return _PERCENT_SEPARATOR.sub(lambda m: _PERCENT_SEPARATOR_MAP[m.group(1).lower()], text)


KEYED = re.compile(
    r"(?<![a-z])(lat(?:itude)?|lon(?:gitude)?|lng|long)"
    r"(?:[_-]?(?:deg(?:rees)?|dd|ref|0|1|2))?[\"']?[ \t]*[:=]?[ \t]*[\"']?" + _NUM)
DIGEST = re.compile(r"[0-9a-f]{16}")
KEYED_I = re.compile(KEYED.pattern, re.IGNORECASE)
ARRAY = re.compile(r"\[\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*(?:,\s*-?\d+(?:\.\d+)?\s*)?\]")


def numbers(text):
    """(start, end, value) of each decimal with 1-3 integer digits and >= 4 fractional digits.

    A "-" directly against the digit run is always its sign, no matter what precedes the "-"
    itself (start of text, whitespace, punctuation, or an identifier character glued straight
    against it: "a-12.345", "5-12.345", "x_1-12.345" all read as negative -- three fractional
    digits here on purpose, so this docstring never itself looks like a coordinate). Earlier
    this guard tried to tell a "real" sign apart from a hyphen that merely happened to sit next
    to a number, and on the glued cases it neither consumed nor rejected the "-": it silently
    reported the unsigned value instead, turning a real far coordinate into a near-looking
    positive one. There is no reliable text-only signal for that distinction, and guessing
    "positive" is exactly the guess that hides a leak, so this no longer guesses: any adjacent
    "-" is the sign. A number glued to a letter/digit/underscore/dot with no sign in between is
    still excluded, as before (that's what keeps ordinary identifiers and version strings
    quiet). Operates on `text` exactly as given -- no normalization here; candidates() is the
    one caller and normalizes first, so positions returned are relative to its normalized text."""
    for m in FRACTION.finditer(text):
        dot = start = m.start()
        while start > 0 and dot - start < 4 and text[start - 1] in DIGITS:
            start -= 1
        if not 1 <= dot - start <= 3:
            continue
        if start > 0 and text[start - 1] == "-":
            start -= 1
        elif start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
            continue
        yield start, m.end(), float(text[start:m.end()])


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    digest: str


def escape_data(s):
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(s):
    return escape_data(s).replace(":", "%3A").replace(",", "%2C")


def pair_digest(*values):
    return hashlib.sha256(",".join("%.4f" % v for v in values).encode()).hexdigest()[:16]


def load_allow(path):
    """{path: {digest}} from lines of `path digest  # reason`."""
    allow = {}
    if not path or not os.path.exists(path):
        return allow
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            body, _, reason = raw.partition("#")
            if not body.strip():
                continue
            fields = body.split()
            if len(fields) != 2 or not DIGEST.fullmatch(fields[1]) or not reason.strip():
                raise SystemExit("coord_guard: %s:%d must be 'path digest  # reason'" % (path, n))
            allow.setdefault(fields[0], set()).add(fields[1])
    return allow


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def _orders(a, b):
    return [(x, y) for x, y in ((a, b), (b, a)) if abs(x) <= 90.0 and abs(y) <= 180.0]


class Guard:
    def __init__(self, origin, radius_km=RADIUS_KM, allow=None):
        self.lat0, self.lon0 = origin
        self.radius_km = radius_km
        self.allow = allow or {}

    def _far(self, lat, lon):
        return haversine_km(lat, lon, self.lat0, self.lon0) > self.radius_km

    @staticmethod
    def _pair_valid(a, b):
        return bool(_orders(a, b)) and max(abs(a), abs(b)) >= 1.0

    def _pair_far(self, a, b):
        return all(self._far(x, y) for x, y in _orders(a, b))

    def _lone_far(self, axis, v):
        deg_km = math.pi * EARTH_KM / 180
        if axis == "lat":
            return abs(v - self.lat0) * deg_km > self.radius_km
        dl = (v - self.lon0 + 180.0) % 360.0 - 180.0
        return abs(dl) * deg_km * math.cos(math.radians(self.lat0)) > self.radius_km

    def candidates(self, text):
        """(line, kind, digest, far) for every coordinate-shaped pair or keyed value in `text`.

        The sole normalization point: `text` is folded here, once, before anything below reads
        it (including numbers(), which itself normalizes nothing)."""
        text = _normalize(text)
        nums = list(numbers(text))
        if not nums:
            return []
        starts = []

        def line_of(pos):
            if not starts:
                starts.extend(itertools.accumulate((len(s) + 1 for s in text.split("\n")),
                                                   initial=0))
            return bisect.bisect_right(starts, pos)

        out = []
        for (s1, e1, a), (s2, _e2, b) in zip(nums, nums[1:]):
            if SEPARATOR.fullmatch(text, e1, s2) and self._pair_valid(a, b):
                out.append((line_of(s1), "inline pair", pair_digest(a, b), self._pair_far(a, b)))
        if "[" in text:
            for m in ARRAY.finditer(text):
                a, b = float(m.group(1)), float(m.group(2))
                # Same >=1.0 floor as the loose inline-pair form: a sub-degree 2-3 element
                # bracketed float array is common DSP/ML config shape, not a coordinate.
                if self._pair_valid(a, b):
                    out.append((line_of(m.start(1)), "array pair", pair_digest(a, b),
                                self._pair_far(a, b)))
        keyed = {"lat": [], "lon": []}
        lowered = text.lower() if text.isascii() else None
        for m in (KEYED.finditer(lowered) if lowered is not None else KEYED_I.finditer(text)):
            axis = "lat" if m.group(1).lower().startswith("lat") else "lon"
            keyed[axis].append((line_of(m.start(2)), float(m.group(2)), m.start(2)))
        paired = set()
        for la_line, la, la_pos in keyed["lat"]:
            for lo_line, lo, lo_pos in keyed["lon"]:
                if abs(la_line - lo_line) <= LINE_WINDOW and abs(la) <= 90.0 and abs(lo) <= 180.0:
                    paired.update((la_pos, lo_pos))
                    out.append((min(la_line, lo_line), "keyed lat/lon", pair_digest(la, lo),
                                self._far(la, lo)))
        for axis, limit in (("lat", 90.0), ("lon", 180.0)):
            for ln, v, pos in keyed[axis]:
                if pos not in paired and abs(v) <= limit:
                    out.append((ln, "keyed " + axis, pair_digest(v), self._lone_far(axis, v)))
        return out

    def findings(self, path, candidates):
        allowed = self.allow.get(path, ())
        lines = {}
        for ln, kind, digest, far in sorted(candidates):
            if far and digest not in allowed:
                lines.setdefault(ln, (kind, digest))
        return [Finding(path, ln, kind, digest) for ln, (kind, digest) in sorted(lines.items())]

    def scan_text(self, path, text):
        return self.findings(path, self.candidates(text))


def git(*args, repo=".", data=None):
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True,
                          check=True).stdout


def read_origin(repo, rev):
    try:
        origin = json.loads(git("show", "%s:survey.json" % rev, repo=repo))["origin"]
        if origin.get("fictional") is not True:
            raise SystemExit("coord_guard: survey.json at %s: origin is not marked fictional" % rev)
        try:
            lat, lon = float(origin["lat_deg"]), float(origin["lon_deg"])
        except ValueError:
            lat = lon = None
        if lat is None or not (abs(lat) <= 90.0 and abs(lon) <= 180.0):
            raise SystemExit("coord_guard: survey.json at %s: origin is not a valid lat/lon" % rev)
    except ValueError as e:
        raise SystemExit("coord_guard: cannot read survey.json origin at %s (%s)"
                         % (rev, type(e).__name__))
    except (subprocess.CalledProcessError, KeyError, TypeError, AttributeError) as e:
        raise SystemExit("coord_guard: cannot read survey.json origin at %s (%s)"
                         % (rev, type(e).__name__))
    return lat, lon


def read_blobs(repo, oids):
    """({oid: text}, {oversized oid}) for blobs among `oids`, split on MAX_BLOB_BYTES.

    Every blob at or under the size cap is decoded and scanned, full stop -- a blob is never
    excluded because it merely contains a NUL byte. That NUL-presence heuristic used to be the
    sole test for "this is binary, skip it", and it takes exactly one incidental NUL anywhere in
    the first 8KB (or, previously, a single leading one) to blind the guard to a real coordinate
    sitting right after it, with no trace in the output that anything was skipped. Decoding with
    errors="replace" costs nothing extra for genuine binaries -- the coordinate-shaped regexes
    below need a specific run of ASCII digits and a literal '.', which garbled bytes essentially
    never produce by chance -- and it closes that gap outright instead of tuning where the
    boundary sits. The size cap stays: it exists to bound memory/CPU on a huge accidental blob,
    not to classify text vs. binary; a blob over it is never scanned, and the caller fails the
    run closed over it rather than passing silently on incomplete coverage."""
    oids = list(dict.fromkeys(oids))
    if not oids:
        return {}, set()
    checks = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                 repo=repo, data=("\n".join(oids) + "\n").encode()).decode().split("\n")
    rows = [p for p in (row.split() for row in checks) if len(p) == 3 and p[1] == "blob"]
    wanted = [p[0] for p in rows if int(p[2]) <= MAX_BLOB_BYTES]
    oversized = {p[0] for p in rows if int(p[2]) > MAX_BLOB_BYTES}
    if not wanted:
        return {}, oversized
    out = git("cat-file", "--batch", repo=repo, data=("\n".join(wanted) + "\n").encode())
    blobs, pos = {}, 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        oid, _typ, size = out[pos:nl].split()
        body = out[nl + 1:nl + 1 + int(size)]
        pos = nl + 1 + int(size) + 1
        blobs[oid.decode()] = body.decode("utf-8", "replace")
    return blobs, oversized


def tree_entries(repo, rev):
    out = git("ls-tree", "-r", "-z", "--full-tree", rev, repo=repo)
    for rec in filter(None, out.split(b"\0")):
        meta, path = rec.split(b"\t", 1)
        mode, typ, oid = meta.split()
        if typ == b"blob" and mode != b"120000":
            yield None, path.decode("utf-8", "replace"), oid.decode()


def range_entries(repo, base, head):
    spec = [head] if not base or base == ZERO_SHA else ["%s..%s" % (base, head)]
    revs = git("rev-list", *spec, repo=repo)
    if not revs.strip():
        return
    out = git("diff-tree", "--stdin", "-r", "-m", "--root", "--no-renames", "-z",
              "--diff-filter=AMTC", repo=repo, data=revs)
    fields = out.split(b"\0")
    commit, i = None, 0
    while i < len(fields):
        f = fields[i]
        if f.startswith(b":"):
            meta = f[1:].split()
            if meta[1] not in (b"120000", b"160000"):
                yield commit, fields[i + 1].decode("utf-8", "replace"), meta[3].decode()
            i += 2
        else:
            commit = f.decode().strip() or commit
            i += 1


def scan(guard, repo, entries):
    """(findings, blob_count, oversized_count, oversized_hits): oversized_hits is the
    deduplicated (commit, path) pairs pointing at a blob over MAX_BLOB_BYTES -- never scanned,
    so the caller must fail the run over them rather than reporting a clean pass."""
    entries = list(entries)
    blobs, oversized = read_blobs(repo, [oid for _, _, oid in entries])
    cands = {oid: guard.candidates(text) for oid, text in blobs.items()}
    results, oversized_hits = [], []
    for commit, path, oid in entries:
        if oid in cands:
            results.extend((commit, f) for f in guard.findings(path, cands[oid]))
        elif oid in oversized:
            oversized_hits.append((commit, path))
    return (list(dict.fromkeys(results)), len(blobs), len(oversized),
            list(dict.fromkeys(oversized_hits)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--origin-rev", help="revision whose survey.json supplies the origin")
    ap.add_argument("--radius-km", type=float, default=RADIUS_KM)
    ap.add_argument("--allow", default=ALLOW_FILE)
    ap.add_argument("--show-digests", action="store_true", help="local use only, never in CI")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("tree").add_argument("--rev", default="HEAD")
    r = sub.add_parser("range")
    r.add_argument("base")
    r.add_argument("head")
    args = ap.parse_args(argv)

    if args.mode == "tree":
        origin_rev = args.origin_rev or args.rev
        entries = tree_entries(args.repo, args.rev)
        what = "tree at %s" % args.rev
    else:
        full = not args.base or args.base == ZERO_SHA
        origin_rev = args.origin_rev or (args.head if full else args.base)
        entries = range_entries(args.repo, args.base, args.head)
        what = ("every commit reachable from %s" % args.head if full
                else "every commit in %s..%s" % (args.base, args.head))
    guard = Guard(read_origin(args.repo, origin_rev), args.radius_km, load_allow(args.allow))
    results, nblobs, oversized, oversized_hits = scan(guard, args.repo, entries)
    for commit, f in results:
        at = "commit %s " % commit[:12] if commit else ""
        tail = " digest %s" % f.digest if args.show_digests else ""
        print("::error file=%s,line=%d::%s"
              % (escape_property(f.path), f.line, escape_data(
                  "%s%s:%d: %s more than %g km from the fictional origin%s"
                  % (at, f.path, f.line, f.kind, guard.radius_km, tail))))
    for commit, path in oversized_hits:
        at = "commit %s " % commit[:12] if commit else ""
        print("::error file=%s::%s"
              % (escape_property(path), escape_data(
                  "%s%s: blob over %d bytes, not scanned" % (at, path, MAX_BLOB_BYTES))))
    skip = (" (%d blob(s) over %d bytes not scanned)" % (oversized, MAX_BLOB_BYTES)
            if oversized else "")
    print("coord_guard: %s: %d blobs, %d finding(s)%s" % (what, nblobs, len(results), skip))
    if results:
        print("coord_guard: real-world coordinates must not enter this public repo. Build test "
              "values from survey.json's fictional origin; the real site comes from "
              "HEAR_SITE_ORIGIN. Values are deliberately not printed.")
    if oversized_hits:
        print("coord_guard: an unscanned blob over the size cap cannot be certified clean; "
              "raise MAX_BLOB_BYTES or add an explicit exception.")
    return 1 if results or oversized_hits else 0


if __name__ == "__main__":
    sys.exit(main())
