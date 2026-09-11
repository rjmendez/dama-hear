#!/usr/bin/env python3
"""Why a node's detections carry `utc_us == 0`, and which of them can honestly be given a time.

⚠️THIS MODULE EXISTS BECAUSE "mach loses 20.3% OF ITS DETECTIONS" WAS TRUE AND MEANT SOMETHING
ELSE THAN IT SOUNDED LIKE. Measured 2026-09-10 against /pool/corpus/records:

    mach     2557 node records   517 unanchored  20.2%
    nyquist  2390 node records    92 unanchored   3.8%
    rankine  1299 node records    35 unanchored   2.7%

Every node writes a couple of unanchored rows in the first ~15 s of every boot -- the gate is
armed before the first UTC label can exist. Splitting on that one fact separates the fleet:

    node      unanchored at uptime <= 15 s      unanchored at uptime > 15 s
    mach                  42                        475   (18.6% of all its rows)
    nyquist               87                          5   ( 0.2%)
    rankine               20                         15   ( 1.2%)

So mach's excess is not a boot artefact and it is not spread thinly: it is a small number of long
windows in which the node detected sound and could not time any of it. The longest in the live
card file ran from uptime 34 s to 988 s of one boot and swallowed 135 detections.

⚠️AND IT IS NOT A GPS ACQUISITION PROBLEM. The archived health.csv for those windows shows the
timepulse working perfectly throughout -- pps advancing 1:1 with uptime, pps_bad 0, pps_gaps 0,
interval spread 2-6 us -- while `tacc_ns` stayed 0. tacc_ns is assigned from EVERY NAV-PVT with a
payload of 24 B or more, outside the fix guard (hear_node.ino, right before `ubx_pvt++`), so
tacc_ns == 0 across 1016 consecutive seconds means no NAV-PVT was decoded at all. A u-blox that
is searching for sky still sends NAV-PVT, and still reports a large tAcc; it does not report 0.
Then the node went from 0 satellites to 19 with tAcc 25 ns inside a single 30 s health interval,
which is a link coming up, not a sky clearing. Across the archive:

    mach     4 of 7 boots had a NAV-PVT-silent window     2401 s   3.37% of logged uptime
    nyquist  0 of 3                                          0 s   0%
    rankine  0 of 9                                          0 s   0%

mach is the node whose GPS TX/RX pair is reversed at the module. The fix is in the firmware's
bring-up, not here. What is here is (a) the diagnosis, so the number is never again read as
"mach's GPS is bad", and (b) the only reconstruction the stored fields actually support.

WHAT CAN BE RECONSTRUCTED. A row with `utc_us == 0` still carries `pps_n` and `us_since_pps`: the
node could not NAME the edge, but it still knows WHICH edge and how far into it. Between two
anchored rows of the same boot the edges are locked, one second apart, and the arithmetic is
exact -- measured over 5175 adjacent anchored pairs on the three live cards:

    |(d_utc_edge) - (d_pps_n * 1e6)|   p50 0 us   p90 <= 50 us   p99 <= 91 us   max 2331 us

WHAT CANNOT, AND WHY THIS REFUSES IT. Two things break the arithmetic, and both are measured:

1.  ⚠️PRE-LOCK ROWS ARE NOT RECOVERABLE AT ANY PRICE, and they are 167 of the 169 unanchored rows
    on the live cards. Before the first label the timepulse is free-running on the module's own
    oscillator, not disciplined to UTC. In mach's archived health rows the ESP-vs-PPS figure
    reads 10.34-10.63 ppm through the unlocked window and settles at 9.66 ppm once the module
    locks -- a ~0.8-1.0 ppm offset that is itself drifting (5.91 -> 6.48 ppm across one 1317 s
    window). Extrapolating a label backwards across 1016 unlocked seconds therefore accumulates
    of order a millisecond -- ~35 cm of sound -- BEFORE counting the unbounded phase step the
    module takes when it switches the pulse to the locked timebase, which nothing in the record
    can bound. So: refused, and named `pre_lock`.

2.  ⚠️pps_n IS NOT A GLOBALLY RELIABLE EDGE INDEX. 34 of those 5175 adjacent pairs disagree by a
    near-whole number of seconds (-5.000 s, +-1.000 s, +-2.000 s, all within 300 us of an
    integer). An edge the ISR did not count leaves pps_n short while UTC advanced anyway, and a
    recovery that fitted one model per boot would silently spread that whole second over every
    row in between. That is why `recover_boot` brackets each row between its NEAREST anchored
    neighbours and requires those two to AGREE, rather than fitting a boot-wide line: the pair
    disagreeing IS the detection of the lost edge, and the row is refused instead of moved 343 m.

WHAT IT RECOVERS TODAY. Nothing, on the current cards -- 2 of mach's 169 unanchored rows and 9 of
nyquist's sit after a boot's first anchor, and all 11 fail the bracket because no anchored row
follows them. That is the honest answer and it is reported as such rather than rounded up to a
recovery. The module earns its place by making the refusal checkable and by being correct for the
rows the firmware fix will leave behind -- a short pre-lock window with anchored rows either side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Rows at or below this uptime are the ordinary boot settle every node shows on every boot: the
#: audio gate arms before any UTC label can exist. It is a REPORTING SPLIT, not a physical
#: boundary -- a node can perfectly well still be unlabelled at 16 s. It is 15 because on the
#: three live cards every boot of every node put its first rows at uptime 12-15 and the earliest
#: anchored row seen anywhere is 13. Callers who want a different split pass one.
SETTLE_UPTIME_S = 15

#: The bracket-agreement gate, in microseconds. Set from the measured envelope and not from its
#: middle: the worst residual over 5141 consistent adjacent pairs is 2331 us, and the SMALLEST
#: real slip is 999 946 us, so anything between the two separates them. 5000 us is 2.1x the
#: worst genuine residual and 200x below the nearest slip. It is a REFUSAL threshold -- the
#: accuracy actually achieved is `RecoveredRow.residual_us`, whose p99 is 91 us (3 cm of sound),
#: and a consumer with a tighter budget should filter on that field rather than lower this.
EDGE_TOL_US = 5000


def _int(row: Dict[str, Any], name: str) -> Optional[int]:
    """A dets field as an int, or None. `hear.detsfile` yields strings; the pool yields a mix."""
    v = row.get(name)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def is_anchored(row: Dict[str, Any]) -> bool:
    """`utc_us > 0`. The same weak question `hear.pool` asks -- a stamp exists, not that it is
    trustworthy. See the warning at the top of `hear/pool.py`."""
    u = _int(row, "utc_us")
    return bool(u and u > 0)


def edge_utc_us(row: Dict[str, Any]) -> Optional[int]:
    """UTC of the PPS edge this anchored row hangs off: its stamp less its offset into the second.

    `us_since_pps` is SIGNED -- a sample back-dated across an edge belongs to the previous second
    and the firmware decrements pps_n for it -- so this is a subtraction, not an absolute value.
    """
    u = _int(row, "utc_us")
    s = _int(row, "us_since_pps")
    if not u or u <= 0 or s is None:
        return None
    return u - s


def split_boots(rows: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Cut a dets file into per-boot runs.

    ⚠️THIS IS A HEURISTIC AND IT IS KNOWN TO UNDER-CUT. dets.csv carries no boot id -- only
    `clip`, on the minority of rows that got one, embeds the boot's hex tag -- so the only signals
    are that `uptime_s` and `pps_n` both restart at a reboot. Two boots that both wrote only their
    first-second rows are therefore merged, and that is not hypothetical: on the live nyquist card
    two segments each hold rows at uptime 13 with pps_n 11 whose stamps are 342 s apart, which is
    impossible inside one boot.

    A merge is SAFE HERE because nothing downstream trusts this cut. `recover_boot` re-derives the
    edge relation from the anchored rows and refuses any bracket whose two ends disagree, so a
    merged pair fails the bracket exactly as a lost edge does. The cut narrows the search; the
    agreement check is what makes the answer sound.
    """
    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    prev_up = prev_pps = -1
    for r in rows:
        up = _int(r, "uptime_s")
        pps = _int(r, "pps_n")
        if cur and ((up is not None and up < prev_up) or (pps is not None and pps < prev_pps)):
            out.append(cur)
            cur = []
        cur.append(r)
        if up is not None:
            prev_up = up
        if pps is not None:
            prev_pps = pps
    if cur:
        out.append(cur)
    return out


@dataclass(frozen=True)
class RecoveredRow:
    """One unanchored row given a time by its two anchored neighbours."""
    index: int              #: position in the boot run that was passed in
    pps_n: int
    utc_us: int             #: reconstructed
    residual_us: int        #: how far the two bracketing anchors disagreed. The error bar.
    span_s: int             #: edges between the two anchors -- how far the bracket reaches


@dataclass(frozen=True)
class Refusal:
    """One unanchored row that was NOT given a time, and the reason, which is never 'unknown'."""
    index: int
    pps_n: Optional[int]
    reason: str             #: pre_lock | after_last_anchor | no_pps_n | bracket_disagrees


@dataclass(frozen=True)
class BootRecovery:
    n_rows: int
    n_anchored: int
    recovered: List[RecoveredRow] = field(default_factory=list)
    refused: List[Refusal] = field(default_factory=list)

    @property
    def n_unanchored(self) -> int:
        return len(self.recovered) + len(self.refused)


def recover_boot(rows: Sequence[Dict[str, Any]], *,
                 tol_us: int = EDGE_TOL_US) -> BootRecovery:
    """Time the unanchored rows of ONE boot run that can be timed. Refuse the rest, with a reason.

    Each unanchored row is bracketed by the nearest anchored row before it and the nearest after.
    Both must exist, and they must agree about the edge counter: the UTC between them must equal
    the number of edges between them, to within `tol_us`. Only then is
    ``utc = edge_utc(before) + (pps_n - pps_n(before)) * 1e6 + us_since_pps`` written.

    A row before the run's first anchored row is `pre_lock` and is refused whatever it looks like
    -- the timepulse was not disciplined to UTC yet, so the edges behind that point are not
    seconds. See this module's docstring for the measured size of that error.
    """
    anchored: List[Tuple[int, int, int]] = []          # (index, pps_n, edge_utc_us)
    for i, r in enumerate(rows):
        if not is_anchored(r):
            continue
        k = _int(r, "pps_n")
        e = edge_utc_us(r)
        if k is None or k <= 0 or e is None:
            continue
        anchored.append((i, k, e))

    rec: List[RecoveredRow] = []
    ref: List[Refusal] = []
    for i, r in enumerate(rows):
        if is_anchored(r):
            continue
        k = _int(r, "pps_n")
        off = _int(r, "us_since_pps")
        if k is None or k <= 0 or off is None:
            # pps_n == 0 is the firmware saying there was no edge to be offset from at all
            # ("pre-lock: there is no edge to be offset FROM"), so there is nothing to place.
            ref.append(Refusal(i, k, "no_pps_n"))
            continue
        before = [a for a in anchored if a[0] < i]
        after = [a for a in anchored if a[0] > i]
        if not before:
            ref.append(Refusal(i, k, "pre_lock"))
            continue
        if not after:
            ref.append(Refusal(i, k, "after_last_anchor"))
            continue
        bi, bk, be = before[-1]
        ai, ak, ae = after[0]
        resid = (ae - be) - (ak - bk) * 1_000_000
        if abs(resid) > tol_us:
            # An edge the ISR never counted, or two boots this file cannot tell apart. Either way
            # the second between the anchors is not accounted for and the row would be placed a
            # whole second -- 343 m -- from where it happened.
            ref.append(Refusal(i, k, "bracket_disagrees"))
            continue
        rec.append(RecoveredRow(index=i, pps_n=k,
                                utc_us=be + (k - bk) * 1_000_000 + off,
                                residual_us=int(resid), span_s=ak - bk))
    return BootRecovery(n_rows=len(rows), n_anchored=len(anchored), recovered=rec, refused=ref)


@dataclass(frozen=True)
class Diagnosis:
    """What an unanchored fraction is actually made of, for one node."""
    node: str
    n_rows: int
    n_unanchored: int
    n_settle: int                    #: unanchored at uptime <= settle_s -- every boot has these
    n_late: int                      #: unanchored above it -- the ones that mean something
    late_uptimes_s: List[int] = field(default_factory=list)
    n_boots: int = 0
    n_recovered: int = 0
    refusals: Dict[str, int] = field(default_factory=dict)

    @property
    def unanchored_frac(self) -> float:
        return self.n_unanchored / self.n_rows if self.n_rows else 0.0

    @property
    def late_frac(self) -> float:
        """The figure that separates the fleet. See the table in this module's docstring."""
        return self.n_late / self.n_rows if self.n_rows else 0.0

    @property
    def longest_late_s(self) -> int:
        return max(self.late_uptimes_s) if self.late_uptimes_s else 0

    def late_quantiles(self) -> Dict[str, int]:
        """The distribution of late-unanchored uptimes, not just its maximum. Empty when there
        are none -- a node with no late rows has no distribution, and 0 is not one."""
        v = sorted(self.late_uptimes_s)
        if not v:
            return {}
        def q(p: float) -> int:
            return v[min(len(v) - 1, int(p * (len(v) - 1)))]
        return {"min": v[0], "p50": q(0.5), "p90": q(0.9), "max": v[-1]}


def diagnose(rows: Sequence[Dict[str, Any]], *, node: str = "",
             settle_s: int = SETTLE_UPTIME_S, tol_us: int = EDGE_TOL_US) -> Diagnosis:
    """Split a node's dets rows into the boot-settle unanchored rows and the ones that matter.

    Boot-settle rows are not a defect and must not be counted as one: subtract them and nyquist's
    92 unanchored records become 5. Everything above the split is a node that was running, and
    detecting, with no clock.
    """
    n_settle = 0
    late: List[int] = []
    for r in rows:
        if is_anchored(r):
            continue
        up = _int(r, "uptime_s")
        if up is not None and up <= settle_s:
            n_settle += 1
        else:
            late.append(up if up is not None else -1)

    boots = split_boots(rows)
    n_rec = 0
    refusals: Dict[str, int] = {}
    for b in boots:
        got = recover_boot(b, tol_us=tol_us)
        n_rec += len(got.recovered)
        for x in got.refused:
            refusals[x.reason] = refusals.get(x.reason, 0) + 1
    if not node and rows:
        node = str(rows[0].get("node") or rows[0].get("node_id") or "")
    return Diagnosis(node=node,
                     n_rows=len(rows), n_unanchored=n_settle + len(late),
                     n_settle=n_settle, n_late=len(late), late_uptimes_s=late,
                     n_boots=len(boots), n_recovered=n_rec, refusals=refusals)
