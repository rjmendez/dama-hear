"""Every CSV a node writes must have as many fields as its header declares.

⚠️THIS TEST EXISTS BECAUSE dets.csv DID NOT. Its header declared 12 columns and its writer
emitted 11: `node` was named and never written, so a consumer reading by column name got every
field shifted one left, and the column it lost was the one saying WHICH NODE the detection came
from -- in the file that exists for multi-node TDoA. Pulled from the live nodes on 2026-09-08:
147 rows on nyquist and 268 on mach, every one 11 wide under a 12-wide header. scene.csv, in the
same source file, got it right, which is what made it an oversight rather than a convention.

The check is on the SOURCE because there is no ESP32 toolchain in CI: it reads the header
constant and the snprintf format that writes the row, and counts fields. It cannot run the
firmware, but this class of defect is a mismatch between two string literals a few hundred lines
apart, and that is exactly what it compares.
"""
import pathlib
import re

import pytest

INO = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "night_node" / "night_node.ino"

#: (header constant, the literal that starts the row, trailing fields appended after the payload)
CASES = [
    ("DETS_HDR", '"%s,%lld,%lu,%lu,%lu,%ld,%d,%u,%.3f,%lu,"', 3),  # frame_hex + clip + clip_why
    ("SCENE_HDR", '"%s,%lld,%lu,%lu,%d,%d,%d,%d,%d,%lu,"', 3),   # mel_hex + f_lo_hz + f_hi_hz
]


def _source():
    if not INO.exists():
        pytest.skip("night_node.ino not in this checkout")
    return INO.read_text()


def _header_fields(src, name):
    """Join ADJACENT string literals: C concatenates them and so must this.

    ⚠️The first version read only the first literal, so wrapping the header across two lines --
    which is exactly what adding a column made necessary -- silently halved the declared column
    count and made the guard fail on a correct header.
    """
    m = re.search(re.escape(name) + r"\[\]\s*=\s*((?:\s*\"[^\"]*\")+)\s*;", src)
    assert m, "could not find %s" % name
    return "".join(re.findall(r"\"([^\"]*)\"", m.group(1))).split(",")


def _format_fields(fmt_literal):
    """Count conversion specifiers in the row's leading format string."""
    body = fmt_literal.strip('"')
    # every field is followed by a comma in these writers, so the trailing comma is the count
    return len(re.findall(r"%[-+ #0-9.]*(?:ll|l|h)?[a-zA-Z]", body))


@pytest.mark.parametrize("hdr_name,fmt,tail", CASES)
def test_header_declares_exactly_what_the_writer_emits(hdr_name, fmt, tail):
    src = _source()
    assert fmt in src, "%s: the row format literal moved; update this test" % hdr_name
    declared = len(_header_fields(src, hdr_name))
    written = _format_fields(fmt) + tail
    assert declared == written, (
        "%s declares %d columns, the writer emits %d -- a consumer reading by name gets every "
        "field shifted" % (hdr_name, declared, written))


def test_the_dets_header_names_the_node_and_the_writer_supplies_it():
    """The specific regression: `node` declared, never written."""
    src = _source()
    fields = _header_fields(src, "DETS_HDR")
    assert fields[0] == "node_id", "dets.csv must lead with the node identity"
    fmt = CASES[0][1]
    assert fmt.startswith('"%s,'), "the first written field must be the node id"
    assert "sketch_back" in fields, "a row must say where its own sketch window started"


def test_renaming_the_column_is_what_forces_the_roll():
    """csv_open() rolls a file aside only when the header STRING changes. Reverting the name to
    `node` would append 12-column rows under the same header as the 11-column ones already on
    every card in the field."""
    src = _source()
    assert '"node_id,utc_us' in src and 'sketch_back' in src
    assert '"node,utc_us,uptime_s,sample,pps_n' not in src, "the old dets header is back"
