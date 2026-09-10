#!/usr/bin/env python3
"""What a row's `node` cell is allowed to be renamed to, and on what evidence.

    from hear import identity
    identity.resolve("hear-5c4c94", "file")   -> ("rankine", "alias", "hear-5c4c94")
    identity.resolve("nyquist", "file")       -> ("nyquist", "file", None)

⚠️THIS IS NOT A LOOSENING OF `pool._node_mismatch`. That guard exists because a row filed under
the wrong node is worse than a dropped one -- it puts one microphone's arrivals on another
microphone's position, and TDoA then solves for a source that was never there. The guard stays
exactly as strict. What this module adds is the ONE case the guard cannot tell apart from a
mis-flash and which is not one: a node that wrote rows before it had a name.

⚠️AN UNPROVISIONED ID IS NOT ANOTHER NODE'S NAME. firmware/night_node/night_node.ino:84-88 is
the whole mechanism:

    #ifdef NODE_ID
      snprintf(node_id, sizeof node_id, "%s", NODE_ID);
    #else
      snprintf(node_id, sizeof node_id, "hear-%02x%02x%02x", m[3], m[4], m[5]);

`NODE_ID` is a compile-time #define written by firmware/night_node/gen_secrets.py:58, so a build
made before someone ran gen_secrets.py falls back to the last three bytes of that ESP32's OWN
MAC. `hear-5c4c94` is therefore a name no operator ever chose and no second board can answer to:
it is a hardware address, and the board that emitted it is by construction the board whose MAC
it is. Relabelling it to the name that board was later given adds no claim that its own bytes did
not already make. Relabelling `nyquist` to `mach` would be an entirely different act, and the
shape of the table below is what makes it impossible rather than merely discouraged.

⚠️PROVENANCE IS KEPT, NOT OVERWRITTEN. `pool._node_mismatch`'s docstring refuses to relabel
because "the label is evidence that a node was flashed with the wrong identity; rewriting it to
the fetch's name would file the rows correctly and destroy the only trace of the flashing error."
That objection is answered rather than waived: an aliased record carries `node_from: "alias"` and
`node_alias_of: "<the raw id>"`, and the ledger entry carries an `aliased` tally beside
`node_mismatch`. The raw id survives in the record, in the ledger and on the card. Nothing is
erased; the row is merely reachable.

⚠️THE ALIAS IS A PROPERTY OF THE IDENTITY, NOT OF THE FETCH. It is applied whether or not the
caller passed a `default_node`, because the alternative makes ONE detection content-address two
ways -- `key()` hashes the node name, so the same frame ingested by the drain (which passes a
node) and by `backfill` (which may not) would land twice under two names. Resolving first and
comparing second keeps one detection one key, and leaves the mismatch guard fully armed: an
aliased row still has to agree with the fetch, so `hear-5c4c94` rows appearing on MACH's card
would resolve to `rankine`, disagree with `mach`, and be refused exactly as before.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

#: The only id shape this module will ever rename FROM: `hear-` + three MAC bytes, lower-case
#: hex, exactly as night_node.ino:88 formats them. A name outside this shape is a name a human
#: chose, and no evidence can make one human-chosen name mean another.
UNPROVISIONED = re.compile(r"^hear-[0-9a-f]{6}$")

#: raw unprovisioned id -> (the node it is, why we say so). The evidence must be checkable from
#: the pool and the cards; a line here is a claim about hardware, not a convenience.
ALIASES: Dict[str, Tuple[str, str]] = {
    "hear-5c4c94": ("rankine", (
        "MEASURED 2026-09-10. (a) The id matches night_node.ino:88's unprovisioned fallback, so "
        "it is one board's MAC tail and not a name. (b) EXCLUSIVITY: of the archived cards in "
        "the pool's raw/ tree it appears on rankine's and only rankine's -- 23 of 23 rankine "
        "dets archives and 13 scene archives, 0 of mach's and 0 of nyquist's; and rankine's dets "
        "archives carry exactly two ids, `rankine` and this one. (c) INTERLEAVING: rankine's live "
        "/dets.csv alternates the two ids across reboots within one file -- row 567 uptime 12 s "
        "as hear-5c4c94, row 596 uptime 18 s as rankine, row 640 uptime 48 s as hear-5c4c94, row "
        "853 uptime 15 s as rankine -- i.e. one card written by one board being reflashed on "
        "2026-09-10, not two boards. (d) 172.16.100.50/status answers node=rankine, "
        "class=xiao-s3-pps. The 242 rows (233 anchored, 12:58:03Z-15:57:32Z) were refused at "
        "ingest by _node_mismatch on every one of 23 drains.")),
}


def _check_table() -> None:
    """A malformed line must fail at import, not at the drain that trusts it.

    ⚠️THE SECOND ASSERT IS THE LOAD-BEARING ONE. A table that could map a provisioned name to
    another provisioned name would be the mis-filing this whole guard exists to prevent, wearing
    the word `alias`. Neither side of an entry may be a name a human chose: the left side must
    match `UNPROVISIONED`, and the right side must NOT -- and no target may itself be a key, so
    the mapping cannot chain.
    """
    for raw, (name, why) in ALIASES.items():
        assert UNPROVISIONED.match(raw), (
            "alias source %r is not an unprovisioned id -- only night_node.ino:88's "
            "hear-<mac tail> form may be renamed" % raw)
        assert not UNPROVISIONED.match(name), (
            "alias target %r is itself an unprovisioned id" % name)
        assert name not in ALIASES, "alias target %r is itself aliased -- no chaining" % name
        assert why and len(why) > 40, "alias %r -> %r states no evidence" % (raw, name)


_check_table()


def is_unprovisioned(node: Optional[str]) -> bool:
    """True for an id the firmware generated for itself because no NODE_ID was compiled in."""
    return bool(node) and bool(UNPROVISIONED.match(str(node)))


def alias_of(node: Optional[str]) -> Optional[str]:
    """The provisioned name this raw id is known to be, or None. Never guesses from the shape.

    ⚠️Shape is necessary and NOT sufficient. An unprovisioned id with no table entry stays
    exactly as it is and takes the mismatch guard's refusal, because `hear-` + a MAC tail says
    which board wrote a row and says nothing whatever about which position that board stood at.
    """
    if not is_unprovisioned(node):
        return None
    hit = ALIASES.get(str(node))
    return hit[0] if hit else None


def reason(node: Optional[str]) -> Optional[str]:
    """The evidence recorded for this alias, for a report that has to justify a rename."""
    hit = ALIASES.get(str(node))
    return hit[1] if hit else None


def resolve(node: Optional[str], node_from: Optional[str]
            ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """`(node, node_from, node_alias_of)` after any declared rename. Pure; mutates nothing.

    Returns the input unchanged when there is no alias, so a caller may apply it to every row.
    `node_alias_of` is None unless a rename happened, and is the RAW id when one did -- the field
    that keeps the flashing history readable after the row has been filed correctly.
    """
    to = alias_of(node)
    if to is None:
        return node, node_from, None
    return to, "alias", str(node)
