"""The site's real longitude must not come back into this public repo.

Only a digest is held here: sha256 of the real longitude's magnitude truncated to 0.01 deg
("DD.DD"). Every tracked text file is scanned -- comments and string literals INCLUDED, because a
coordinate in a comment is published all the same -- for numbers in the spellings a position
travels in (decimal degrees with >= MIN_DECIMALS places, NMEA dddmm.mmmm, 1e-7 degree integers,
and d°m's"), and each is reduced to the same key and hashed.
"""
import hashlib
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITE_LON_DIGESTS = frozenset({
    "3508b855184127a6c6bf3b4782f45a344c7b5ca287de117e5806d2cfeb28018b",
})
MIN_DECIMALS = 3

_DECIMAL = re.compile(r"(?<![\w.])-?(\d+)\.(\d+)")
_SCALED_1E7 = re.compile(r"(?<![\w.])-?(\d{8,10})(?![\w.])")
_DMS = re.compile(r"(\d{1,3})\s*°\s*(\d{1,2}(?:\.\d+)?)\s*['′](?:\s*(\d{1,2}(?:\.\d+)?)\s*[\"″])?")


def _key(hundredths):
    return "%d.%02d" % (hundredths // 100, hundredths % 100)


def _keys(text):
    """(offset, key) for every number in `text` that could be a longitude."""
    for m in _DECIMAL.finditer(text):
        whole, frac = m.group(1).lstrip("0") or "0", m.group(2)
        if len(whole) <= 3 and len(frac) >= MIN_DECIMALS:
            yield m.start(), _key(int(whole) * 100 + int(frac[:2]))
        elif len(whole) in (4, 5):
            minutes = float(whole[-2:] + "." + frac)
            if minutes < 60.0:
                yield m.start(), _key(int((int(whole[:-2]) + minutes / 60.0) * 100))
    for m in _SCALED_1E7.finditer(text):
        yield m.start(), _key(int(m.group(1)) // 100000)
    for m in _DMS.finditer(text):
        deg = int(m.group(1)) + float(m.group(2)) / 60.0 + float(m.group(3) or 0.0) / 3600.0
        yield m.start(), _key(int(deg * 100))


def _hits(text, digests):
    return sorted({text.count("\n", 0, pos) + 1 for pos, key in _keys(text)
                   if hashlib.sha256(key.encode()).hexdigest() in digests})


def _tracked_text_files():
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    for rel in filter(None, out.decode().split("\0")):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\0" not in data:
            yield rel, data.decode("utf-8", "replace")


def test_no_tracked_file_carries_the_site_longitude():
    found = ["%s:%s" % (rel, ",".join(map(str, lines)))
             for rel, text in _tracked_text_files()
             for lines in [_hits(text, SITE_LON_DIGESTS)] if lines]
    assert not found, (
        "the site's real longitude (to 0.01 deg or better) is back in tracked files at %s. This "
        "repo is public: use a fictional longitude in tests and fixtures, and let production read "
        "the site from HEAR_SITE_ORIGIN (Secret hear-site). The value is deliberately not printed."
        % "; ".join(found))


FAKE = frozenset({hashlib.sha256(b"45.67").hexdigest()})


@pytest.mark.parametrize("spelling", [
    "lon = -45.6789", "045.67891", "W 45.678", "$GPGGA,,1234.5,N,04540.734,W",
    "lon_e7: -456789000", "45° 40' 44.0\" W", "45°40.73'"])
def test_every_spelling_is_caught(spelling):
    assert _hits("x\n" + spelling, FAKE) == [2]


@pytest.mark.parametrize("spelling", ["-45.67", "45.6", "145.678", "1.45.678", "45.66999",
                                      "4567890123456"])
def test_what_is_not_the_key_is_not_caught(spelling):
    assert _hits(spelling, FAKE) == []
