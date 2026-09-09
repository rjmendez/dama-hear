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

⚠️A PROFILE NAMES THE WHOLE GEOMETRY, NOT JUST THE SHAPE. (bands, frames) does not say what a
byte MEANS: two 20x8 frames at different NFFT, different hop or different band layout are the
same shape and different measurements. v1 states its rate in flags bits 8-11 and its layout in
bit 12; the v2 flags word has no spare bit for either, so a v2 frame was LESS self-describing
than the v1 it replaced. PROFILE_GEOMETRY closes that -- the id names bands, frames, NFFT, hop,
rate, layout and the band edges together, which is what 4 bits are for.

⚠️AND THE COST: profiles 0, 1 and 2 share a shape, so a single bit flip in the profile field
relabels a frame's rate without changing its length, and nothing inside the frame can catch it.
The transport CRC is the only guard. That is worse than a length mismatch and better than a frame
that cannot say what its bands mean at all; test_backend_pipeline pins it so it is not assumed
away.

This module refuses; it never masks. EVERY out-of-range input raises ValueError and nothing is
wrapped, clamped or coerced on the way to the wire -- v1's `int(node_us) & 0xFFFFFFFF`
(sketch.py:91) and its `min(int(peak), 0xFFFF)` (sketch.py:92) are precisely the bug class refused
here. That the refusals are all one exception type is contract: a caller wraps pack_v2 in
`except ValueError` and catches all of them. It imports hear/sketch.py and never edits it: v1
pack/unpack keep working untouched.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, NamedTuple, Optional, Tuple

import numpy as np

from . import sketch as SK

VERSION: int = 2
US_PER_DAY: int = 86_400_000_000        # 37 bits. Microseconds of day, the whole timestamp range.
HDR_V2: int = 13                        # bytes. 5 ts + 2 ref + 2 peak + 2 node_id + 2 flags.

# profile_id -> (bands, frames). LITERAL AND FROZEN: if SK.MEL_BANDS/SK.FRAMES were ever edited,
# reading these from them would silently change the meaning of every deployed frame. A test
# compares the two so an edit forces a NEW profile id instead of a reinterpretation.
PROFILES: Dict[int, Tuple[int, int]] = {0: (20, 8), 1: (20, 8), 2: (20, 8)}


class Geometry(NamedTuple):
    """Everything that decides what a byte of the sketch MEANS.

    ⚠️(bands, frames) IS NOT ENOUGH, WHICH IS WHY THIS EXISTS. Two 20x8 frames with different
    NFFT, different hop or different band layout are the same SHAPE and different MEASUREMENTS.
    v1 says so in its flags -- rate in bits 8-11, fixed-layout in bit 12 -- but the v2 flags word
    is fully allocated (8 seq, 3 version, 4 profile, 1 retrigger), so v2 has nowhere to put them.
    Folding them into the profile id is what 4 bits are FOR: the id then names the whole geometry
    and a v2 frame is self-describing again instead of less so than the v1 it replaced.

    `fs_hz` None means the profile does not state a rate. That is only honest for the legacy id.
    """
    bands: int
    frames: int
    nfft: int
    hop_s: float
    fs_hz: Optional[float]
    layout: str
    f_lo: float
    f_hi: float


#: profile_id -> the full geometry. ⚠️APPEND ONLY. Editing an entry reinterprets every frame ever
#: sent with that id; a changed geometry takes a NEW id. Ids are 4 bits, so there are 16.
PROFILE_GEOMETRY: Dict[int, Geometry] = {
    # 0 is LEGACY and deliberately left as it was found: 20x8 with the rate and layout UNSTATED,
    # which is what every frame already sent under it actually means. It is not selectable for a
    # new frame -- see profile_for_geometry.
    0: Geometry(20, 8, 256, 0.004, None, SK.LAYOUT_NYQUIST, 300.0, 20000.0),
    1: Geometry(20, 8, 256, 0.004, 48000.0, SK.LAYOUT_FIXED, 300.0, 20000.0),
    2: Geometry(20, 8, 256, 0.004, 16000.0, SK.LAYOUT_FIXED, 300.0, 20000.0),
}

#: Ids a NEW frame may claim. 0 is excluded because a frame that cannot say its own rate is the
#: defect, not a choice.
LEGACY_PROFILES = frozenset({0})

#: What pack_v2 uses when the caller names no profile.
#:
#: ⚠️A DECLARED CONSTANT, NOT A DERIVATION. It used to derive the id from q.shape, which is how a
#: rate-unstated frame reached the wire: 20x8 matched exactly one id and that id said nothing
#: about rate or band layout. Shape can no longer select an id at all (profile_for refuses when
#: it is ambiguous, which 20x8 now is). A caller that does not care still gets a frame that
#: STATES what its bands mean; a caller that does passes profile_id.
DEFAULT_PROFILE: int = 1

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


def profile_geometry(profile_id: int) -> Geometry:
    """The full geometry a profile id names. Raises on an id this build does not know."""
    try:
        return PROFILE_GEOMETRY[int(profile_id)]
    except KeyError:
        raise ValueError("unknown profile id %d" % int(profile_id))


def profile_for(bands: int, frames: int) -> int:
    """The profile id carrying this sketch SHAPE.

    ⚠️SHAPE ALONE NO LONGER IDENTIFIES A PROFILE, and this raises when it is ambiguous rather
    than picking one. 20x8 is now three profiles differing in rate and band layout, and choosing
    between them from the shape is exactly the guess that put unstated-rate frames on the wire in
    the first place. Use profile_for_geometry.
    """
    key = (int(bands), int(frames))
    ids = sorted(k for k, v in PROFILES.items() if v == key)
    if not ids:
        raise ValueError("no profile for %dx%d" % key)
    if len(ids) > 1:
        raise ValueError(
            "%dx%d is ambiguous across profiles %s -- they differ in rate or band layout, which "
            "the shape does not carry. Pass profile_id, or use profile_for_geometry(fs_hz=..., "
            "layout=...)." % (key[0], key[1], ids))
    return ids[0]


def profile_for_geometry(bands: int, frames: int, fs_hz: float, layout: str,
                         nfft: int = SK.NFFT, hop_s: float = SK.HOP_S,
                         f_lo: float = SK.F_LO, f_hi: float = SK.F_HI) -> int:
    """The profile id for a COMPLETE geometry. Refuses anything unlisted and never invents an id.

    Refuses a legacy profile too: a new frame must be able to say what its bands mean.
    """
    want = Geometry(int(bands), int(frames), int(nfft), float(hop_s),
                    float(fs_hz), str(layout), float(f_lo), float(f_hi))
    for k, g in sorted(PROFILE_GEOMETRY.items()):
        if g == want and k not in LEGACY_PROFILES:
            return k
    raise ValueError(
        "no profile for %r. A geometry that is not listed cannot be encoded in 4 bits; allocate "
        "a new id in PROFILE_GEOMETRY rather than reinterpreting an existing one." % (want,))


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
    `profile_id` None uses DEFAULT_PROFILE -- a declared id, not one derived from q.shape. The
    shape is still checked against it, so a mismatched sketch raises rather than being relabelled.

    Refuses, never wraps and never clamps: an out-of-range timestamp, node id, sequence, peak or
    ref_db raises ValueError rather than being masked into the field (the sketch.py:91-92 failure).
    A non-2-D `q` raises ValueError too, not the IndexError the shape lookup would give.
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
    pk = int(peak)
    if not (0 <= pk <= 0xFFFF):
        raise ValueError("peak %d outside 0..65535" % pk)
    # ref_db lands in an <h> at 0.25 dB, so the field tops out at -8192..8191.75 dB. Absurd input
    # either way -- the point is that it fails as a ValueError, not as a struct.error the caller's
    # `except ValueError` walks straight past.
    r4 = int(round(float(ref_db) * 4))
    if not (-0x8000 <= r4 <= 0x7FFF):
        raise ValueError("ref_db %r outside -8192..8191.75 dB" % (ref_db,))

    q = np.asarray(q)
    if q.ndim != 2:
        raise ValueError("q is %d-D; a sketch is 2-D (bands, frames)" % q.ndim)
    pid = DEFAULT_PROFILE if profile_id is None else int(profile_id)
    bands, frames = profile_shape(pid)
    if q.shape != (bands, frames):
        raise ValueError("q shape %r does not match profile %d (%dx%d)"
                         % (tuple(q.shape), pid, bands, frames))

    flags = ((_F_RETRIG if retrigger else 0)
             | ((pid & _F_PROFILE_MASK) << _F_PROFILE_SHIFT)
             | ((VERSION & _F_VERSION_MASK) << _F_VERSION_SHIFT)
             | ((s & _F_SEQ_MASK) << _F_SEQ_SHIFT))
    hdr = (us.to_bytes(5, "little") + struct.pack("<hHHH", r4, pk, nid, flags))
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
