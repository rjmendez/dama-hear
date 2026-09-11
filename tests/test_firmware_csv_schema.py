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

INO = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "hear_node" / "hear_node.ino"

#: (header constant, the literal that starts the row, trailing fields appended after the payload)
CASES = [
    # frame_hex + clip + clip_why + sync_sigma_ns
    ("DETS_HDR", '"%s,%lld,%lu,%lu,%lu,%ld,%d,%u,%.3f,%lu,"', 4),
    ("SCENE_HDR", '"%s,%lld,%lu,%lu,%d,%d,%d,%d,%d,%lu,"', 3),   # mel_hex + f_lo_hz + f_hi_hz
]


def _source():
    if not INO.exists():
        pytest.skip("hear_node.ino not in this checkout")
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


def test_a_row_states_what_its_own_stamp_is_worth():
    """⚠️`time_valid` LATCHES TRUE AND IS NEVER CLEARED, so `utc_us > 0` is not evidence that the
    GPS is still talking -- a node whose UART dies keeps stamping off a frozen anchor and
    free-running on its crystal. `sync_sigma_ns` is the only field in which that is visible, and
    it must be in the header AND supplied by the writer, which is the exact pair this file exists
    to hold together."""
    src = _source()
    fields = _header_fields(src, "DETS_HDR")
    assert fields[-1] == "sync_sigma_ns", (
        "the declared uncertainty must be the LAST column: tools/hear_bridge.py documents "
        "trailing columns as the supported growth path and hear/detsfile.py's identify() says "
        "an inserted column is not safe")
    # the writer's tail, parsed rather than grepped: the format literal and its arguments
    src_nc = _strip_comments(src)
    i = src_nc.index('",%s,%s,%s"')
    call = src_nc[i:src_nc.index(";", i)]
    assert "d.sync_sigma_ns" in call or "sg" in call, "the sigma column is declared but not written"


def test_zero_is_not_a_value_the_sigma_column_may_carry():
    """0 ns reads as a perfect clock, which hear/nodeclass.py refuses as a claim no hardware
    supports. A row with no anchor must write an EMPTY cell instead."""
    src = _strip_comments(_source())
    i = src.index("char sg[")
    blk = src[i:i + 400]
    assert 'sg[24] = ""' in blk, "the sigma buffer must default to empty"
    assert "if (d.sync_sigma_ns)" in blk, (
        "the sigma must be written only when it is non-zero, so an unstamped row carries an "
        "empty cell rather than a claim of zero uncertainty")


def test_renaming_the_column_is_what_forces_the_roll():
    """csv_open() rolls a file aside only when the header STRING changes. Reverting the name to
    `node` would append 12-column rows under the same header as the 11-column ones already on
    every card in the field."""
    src = _source()
    assert '"node_id,utc_us' in src and 'sketch_back' in src
    assert '"node,utc_us,uptime_s,sample,pps_n' not in src, "the old dets header is back"


# ---------------------------------------------------------------- health.csv + /status
# The same defect class, in the two writers the earlier version of this file did not reach.
# health.csv's format is split across five adjacent literals and its header across four, which is
# exactly the shape that hid the dets.csv mismatch; /status is not a CSV but it is 129 conversions
# against 129 arguments in one snprintf, and a shift there mislabels every field after it.


def _strip_comments(t):
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


def _join_call(block):
    """Split a printf-style call body into its joined format literal and its argument list."""
    i, fmt, in_fmt = 0, "", True
    while i < len(block):
        c = block[i]
        if c == '"':
            j = i + 1
            out = ""
            while block[j] != '"' or block[j - 1] == "\\":
                out += block[j]
                j += 1
            if in_fmt:
                fmt += out
            i = j + 1
            continue
        if c in " \t\n\r":
            i += 1
            continue
        if c == "," and in_fmt:
            in_fmt = False
            i += 1
            continue
        break
    depth, cur, args = 0, "", []
    for ch in block[i:]:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return fmt, args


SPEC = re.compile(r"%[-+ #0-9.]*(?:ll|l|h|z)?[a-zA-Z]")


def _call(src, start, end):
    i = src.index(start)
    i = src.index('"', i)
    j = src.index(end, i) + len(end)
    return _join_call(_strip_comments(src[i:j]))


def test_the_health_header_declares_exactly_what_its_writer_emits():
    """⚠️FIVE HEADER LITERALS, SEVEN FORMAT LITERALS, 63 COLUMNS. Appending a column means touching
    both, and getting one right is not getting it right. (58 until the ubx_pvt /
    dets_unlabelled / first_label_s / ubx_silent_max append; the end anchor below moves with the
    last column and is meant to.)"""
    src = _source()
    i = src.index("HEALTH_HDR[]")
    blk = _strip_comments(src[i:])
    k = blk.index("=")
    hdr = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', blk[k:blk.index(";", k)])).split(",")
    fmt, args = _call(src, 'f.printf("%s,%lld', "ubx_silent_max);")
    assert len(hdr) == len(SPEC.findall(fmt)) == len(args), (
        "health.csv declares %d columns, its format has %d conversions and %d arguments"
        % (len(hdr), len(SPEC.findall(fmt)), len(args)))


def test_health_carries_the_acquisition_columns_that_say_whether_a_row_is_usable():
    """A stamped row is only as good as the slope that stamped it. fs_used_hz says which rate
    converted it, fs_step_ppm says how coarse the estimate behind that was, and over_s/pps_gaps
    say whether the second was certified at all -- none of which could be recovered afterwards."""
    src = _source()
    i = src.index("HEALTH_HDR[]")
    blk = _strip_comments(src[i:])
    k = blk.index("=")
    hdr = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', blk[k:blk.index(";", k)])).split(",")
    for col in ("clean_s", "fs_used_hz", "fs_step_ppm", "over_s", "pps_gaps"):
        assert col in hdr, col
    # ⚠️the column whose MEANING changed was RENAMED, because csv_open rolls the file on a header
    # change and only a roll keeps the rows written under the old meaning readable.
    assert "fs_ok_hz" in hdr and "fs_cum_hz" not in hdr


def test_status_json_has_one_argument_per_conversion():
    """It is one snprintf with 129 fields. A shift here does not fail: it renames every field
    after the shift, and the JSON still parses."""
    src = _source()
    fmt, args = _call(src, '"{\\"node\\":\\"%s\\"', "i2c_found);")
    assert len(SPEC.findall(fmt)) == len(args)


def _string_arg_width(src, arg):
    """How long the string this %s carries can be, from its own declaration -- not from a guess.
    A bare identifier is looked up in the sketch's declarations (including the multi-declarator
    lines this file favours); a `cond ? "a" : "b"` bounds itself; a macro from secrets.h cannot be
    read here, so it gets a deliberately generous 64."""
    arg = arg.strip().rstrip(");")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arg):
        m = re.search(r"char\s+[^;]*\b%s\[(\d+)\]" % re.escape(arg), src)
        if m:
            return int(m.group(1))
    lits = re.findall(r'"([^"]*)"', arg)
    if lits:
        return max(len(x) for x in lits) + 1
    return 64


def test_status_json_cannot_truncate_into_invalid_json():
    """snprintf truncates silently, and a truncated /status is not a short answer -- it is invalid
    JSON, which every consumer reads as an unreachable node. The buffer has to be sized from the
    FORMAT (every conversion at the widest value its own argument can carry), because the only
    other reference is a sample of the output, and a sample is what the old 3072 was sized from
    while the format could already emit more than that."""
    src = _source()
    fmt, args = _call(src, '"{\\"node\\":\\"%s\\"', "i2c_found);")
    specs = SPEC.findall(fmt)
    assert len(specs) == len(args)
    widths = {"%lu": 10, "%llu": 20, "%lld": 20, "%ld": 11, "%d": 11, "%u": 10}
    worst = len(SPEC.sub("", fmt))
    for sp, arg in zip(specs, args):
        worst += _string_arg_width(src, arg) if sp == "%s" else widths.get(sp, 24)
    m = re.search(r"static char b\[(\d+)\];", src)
    assert m, "the /status buffer moved"
    assert int(m.group(1)) > worst, (
        "/status can emit %d bytes into a %s-byte buffer" % (worst, m.group(1)))
    # ⚠️and the bound is not academic: it is already past the 3072 this buffer used to be.
    assert worst > 3072
