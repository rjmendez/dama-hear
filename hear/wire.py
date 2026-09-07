#!/usr/bin/env python3
"""v2 frame: the sketch plus the three things a solver cannot work without.

v1 (hear/sketch.py) carries no node id, no sequence number and no absolute time. Its 4 B stamp is
microseconds WITHIN the PPS second and delegates the second to the mesh clock (sketch.py:87-88),
so a one-second mislabel is 343 m of range error and nothing downstream can see it. v2 adds
node_id (2 B), microseconds-of-day (5 B, 37 bits) and an 8-bit sequence, and pays for them by
dropping v1's 2 B of bands+frames -- MEL_BANDS and FRAMES are compile-time constants
(sketch.py:26-27) re-sent on every frame forever -- in favour of a 4-bit profile id inside the
flags word v1 already spends 2 B on and uses one bit of. 173 B against the ~200 B usable in a
Meshtastic payload (sketch.py:31,107).

⚠️THE FLAGS WORD IS FULLY ALLOCATED: 8 bits seq, 3 version, 4 profile, 1 retrigger, zero spare.
A new per-detection flag (clipped, pps_locked, a class hint) costs a new profile id or a v3
frame. That is what 13 header bytes buys.

⚠️`us_of_day` IS NOT v1's `node_us`, AND THE TWO KEYS NEVER MEET. Both are microseconds; one is
sub-second, the other absolute within the day. Sharing a name is the 343 m mistake above waiting
to happen, so this module ships no alias and no compatibility shim.

⚠️A v2 FRAME CANNOT CARRY AN UNLISTED GEOMETRY. profile_for() raises rather than inventing an id.
That is the honest cost of spending 4 bits instead of 16 on the sketch shape.

This module refuses; it never masks. Out-of-range inputs raise ValueError -- v1's
`int(node_us) & 0xFFFFFFFF` (sketch.py:91) is precisely the bug class refused here. It imports
hear/sketch.py and never edits it: v1 pack/unpack keep working untouched.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, Optional, Tuple

import numpy as np

from . import sketch as SK

VERSION: int = 2
US_PER_DAY: int = 86_400_000_000        # 37 bits. Microseconds of day, the whole timestamp range.
HDR_V2: int = 13                        # bytes. 5 ts + 2 ref + 2 peak + 2 node_id + 2 flags.

# profile_id -> (bands, frames). LITERAL AND FROZEN: if SK.MEL_BANDS/SK.FRAMES were ever edited,
# reading these from them would silently change the meaning of every deployed frame. A test
# compares the two so an edit forces a NEW profile id instead of a reinterpretation.
PROFILES: Dict[int, Tuple[int, int]] = {0: (20, 8)}

# 237 B Meshtastic payload minus 37 B protobuf/portnum, per sketch.py:31,107.
MESHTASTIC_USABLE: int = 200

_SHAPES: Dict[Tuple[int, int], int] = {v: k for k, v in PROFILES.items()}

_TS_BITS = 37
_TS_MASK = (1 << _TS_BITS) - 1

_F_RETRIG = 0x0001
_F_PROFILE_SHIFT, _F_PROFILE_MASK = 1, 0x0F
_F_VERSION_SHIFT, _F_VERSION_MASK = 5, 0x07
_F_SEQ_SHIFT, _F_SEQ_MASK = 8, 0xFF

_V1_HDR = 12                            # sketch.py:97, struct "<IhHBBH" -- bands at b[8], frames at b[9]


def profile_shape(profile_id: int) -> Tuple[int, int]:
    """(bands, frames) for a profile id. Raises on an id this build does not know."""
    try:
        return PROFILES[int(profile_id)]
    except KeyError:
        raise ValueError("unknown profile id %d" % int(profile_id))


def profile_for(bands: int, frames: int) -> int:
    """The profile id carrying this sketch shape. Refuses an unlisted geometry -- it cannot be
    encoded in 4 bits without one."""
    key = (int(bands), int(frames))
    if key not in _SHAPES:
        raise ValueError("no profile for %dx%d" % key)
    return _SHAPES[key]


def wire_size_v2(profile_id: int = 0) -> int:
    bands, frames = profile_shape(profile_id)
    return HDR_V2 + bands * frames


def fits_meshtastic_v2(profile_id: int = 0, overhead: int = 37) -> bool:
    """Mirrors sketch.fits_meshtastic against the same 237 B payload."""
    return wire_size_v2(profile_id) <= SK.MESHTASTIC_PAYLOAD - overhead


def pack_v2(us_of_day: int, node_id: int, seq: int, ref_db: float, peak: int,
            q: np.ndarray, retrigger: bool = False,
            profile_id: Optional[int] = None) -> bytes:
    """13 B header + bands*frames int8.

    `us_of_day` is integer MICROSECONDS OF DAY, UTC -- not v1's sub-second `node_us`.
    `profile_id` None derives the id from q.shape.

    Refuses, never wraps: an out-of-range timestamp, node id or sequence raises rather than being
    masked into the field (the sketch.py:91 failure).
    """
    us = int(us_of_day)
    if not (0 <= us < US_PER_DAY):
        raise ValueError("us_of_day %d outside 0..86399999999" % us)
    nid = int(node_id)
    if not (0 <= nid <= 0xFFFF):
        raise ValueError("node_id %d outside 0..65535" % nid)
    s = int(seq)
    if not (0 <= s <= 0xFF):
        raise ValueError("seq %d outside 0..255" % s)

    q = np.asarray(q)
    pid = profile_for(q.shape[0], q.shape[1]) if profile_id is None else int(profile_id)
    bands, frames = profile_shape(pid)
    if q.shape != (bands, frames):
        raise ValueError("q shape %r does not match profile %d (%dx%d)"
                         % (tuple(q.shape), pid, bands, frames))

    flags = ((_F_RETRIG if retrigger else 0)
             | ((pid & _F_PROFILE_MASK) << _F_PROFILE_SHIFT)
             | ((VERSION & _F_VERSION_MASK) << _F_VERSION_SHIFT)
             | ((s & _F_SEQ_MASK) << _F_SEQ_SHIFT))
    hdr = (us.to_bytes(5, "little")
           + struct.pack("<hHHH", int(round(ref_db * 4)), min(int(peak), 0xFFFF), nid, flags))
    return hdr + q.astype(np.int8).tobytes()


def unpack_v2(b: bytes) -> Dict:
    """Decode a v2 frame. Raises on a length, version or timestamp-invariant violation rather
    than reshaping whatever arrived."""
    if len(b) < HDR_V2:
        raise ValueError("v2 frame is %d bytes, header needs %d" % (len(b), HDR_V2))
    flags = struct.unpack_from("<H", b, 11)[0]
    pid = (flags >> _F_PROFILE_SHIFT) & _F_PROFILE_MASK
    bands, frames = profile_shape(pid)
    n = HDR_V2 + bands * frames
    if len(b) != n:
        raise ValueError("v2 frame is %d bytes, profile %d needs %d" % (len(b), pid, n))
    version = (flags >> _F_VERSION_SHIFT) & _F_VERSION_MASK
    if version != VERSION:
        raise ValueError("frame version %d is not %d" % (version, VERSION))

    ts40 = int.from_bytes(b[0:5], "little")
    # Second, independent check on the timestamp: the top 3 bits of the uint40 are a strict zero
    # invariant, so a corrupted high byte cannot pass as a plausible time.
    if ts40 >> _TS_BITS:
        raise ValueError("ts40 bits 37-39 set (0x%010x): not a microseconds-of-day frame" % ts40)
    us = ts40 & _TS_MASK
    if us >= US_PER_DAY:
        raise ValueError("us_of_day %d outside 0..86399999999" % us)

    ref4, peak, node_id, _ = struct.unpack_from("<hHHH", b, 5)
    q = np.frombuffer(b[HDR_V2:n], dtype=np.int8).reshape(bands, frames)
    return {"version": VERSION, "us_of_day": us,
            "node_id": node_id, "seq": (flags >> _F_SEQ_SHIFT) & _F_SEQ_MASK,
            "ref_db": ref4 / 4.0, "peak": peak,
            "retrigger": bool(flags & _F_RETRIG), "profile_id": pid,
            "bands": bands, "frames": frames, "flags": flags,
            "q": q, "db": q.astype(float) / 2.0 + ref4 / 4.0}


def version_of(b: bytes) -> int:
    """1 or 2, by length plus self-consistency. v2 is tested FIRST and that order is contract.

    ⚠️THE DISCRIMINATION IS NOT EXACT AND NOTHING CAN MAKE IT SO: v1 has no version field to read
    (sketch.py:97). A v1 frame whose bands*frames happened to equal a v2 profile's payload and
    whose bytes 11-12 happened to spell version 2 would be misread. With MEL_BANDS 20 and
    FRAMES 8 frozen (sketch.py:26-27) that collision does not exist in this repo, which is a
    property of the constants and not of this function.
    """
    if len(b) >= HDR_V2:
        flags = struct.unpack_from("<H", b, 11)[0]
        pid = (flags >> _F_PROFILE_SHIFT) & _F_PROFILE_MASK
        version = (flags >> _F_VERSION_SHIFT) & _F_VERSION_MASK
        if version == VERSION and pid in PROFILES and len(b) == wire_size_v2(pid):
            if not (int.from_bytes(b[0:5], "little") >> _TS_BITS):
                return VERSION
    if len(b) >= _V1_HDR and 1 <= b[8] <= 64 and 1 <= b[9] <= 64 and len(b) == _V1_HDR + b[8] * b[9]:
        return 1
    raise ValueError("unrecognised frame: %d bytes" % len(b))


def decode(b: bytes) -> Dict:
    """version_of() then the matching unpacker. A v1 result gains 'version': 1 from HERE --
    sketch.py is read-only -- and keeps its own 'node_us' key. Nothing else is renamed."""
    v = version_of(b)
    if v == VERSION:
        return unpack_v2(b)
    d = SK.unpack(b)
    d["version"] = 1
    return d


def us_of_day_from_utc(t_utc_s: float) -> int:
    return int(round((float(t_utc_s) % 86400.0) * 1e6)) % US_PER_DAY


def unwrap_utc(us_of_day: int, ref_utc_s: float) -> float:
    """Absolute UTC seconds, nearest-wrap against `ref_utc_s`.

    THE MIDNIGHT RULE: `ref_utc_s` must be within 12 h of the true event time -- a frame's own
    receive time always is. A receiver clock a whole day wrong shifts every node by the SAME
    86400 s, which cancels in TDoA; the only case where the day reaches the geometry is a group
    straddling midnight, and unwrapping each frame against its own receive time handles that.
    Feeding one shared reference to a whole group is what breaks it.
    """
    ref = float(ref_utc_s)
    t = math.floor(ref / 86400.0) * 86400.0 + int(us_of_day) / 1e6
    while t - ref > 43200.0:
        t -= 86400.0
    while ref - t > 43200.0:
        t += 86400.0
    return t
