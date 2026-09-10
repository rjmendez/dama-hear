# Captured node responses

Fetched live, unedited, from the two acoustic nodes on 2026-09-08 (unix 1788901678):

    curl http://172.16.100.105/status > status_nyquist.json    # nyquist, uptime 13020 s
    curl http://172.16.100.116/status > status_mach.json       # mach,    uptime 12938 s
    curl http://172.16.100.105/ls     > ls_nyquist.txt
    curl http://172.16.100.116/ls     > ls_mach.txt

They exist so `tests/test_hear_drain.py` can pin the field NAMES the drain reads out of a real
node rather than out of a hand-written dict that agrees with whatever the reader happens to
spell. A design document for this work named three of them wrong -- `fs_clean`, `drop_seconds`,
`scene.rows` under the wrong parent -- and every one of those misspellings reads back as `None`
through `dict.get`, so an audit block full of `None` would have looked like a quiet node instead
of a broken reader.

Two facts these captures happen to record, both load-bearing elsewhere:

  * `scene.short_blocks` is 0 on BOTH nodes while `acq.drop_s` is 62 s and 89 s. `short_blocks`
    is therefore not the counter for the gaps in the scene row stream, whatever its name suggests.
  * `scene.rows == scene.written` on both, so nothing is being lost between production and the
    card. What is lost is lost after that, on the fetch -- which is what the byte watermark in
    `tools/hear_drain.py` measures.

Do not regenerate them to make a test pass. If the firmware changes a name, that is the test
telling the truth.

⚠️These captures predate the 2026-09-08 acquisition-audit change, so they do NOT carry
`acq.fs_win_s`, `acq.fs_step_ppm`, `acq.fs_used_hz`, `acq.over_s`, `i2s.clean_s` or `pps.gaps`,
and their `i2s.measured_hz` (15651.2904 and 15483.2787) is the old cumulative-over-cumulative
figure that any stall poisons. That is not a reason to regenerate them -- the rule above still
holds. It is a note that the NEW names are pinned only by tests/test_firmware_csv_schema.py and
tests/test_firmware_timebase.py against the source, and are not yet pinned against a live node.
Re-capture both files the next time a node runs a build that has them, and the drain's audit
block will be pinned against hardware again.

## ls-clips-nyquist.txt -- CONSTRUCTED, NOT CAPTURED

⚠️THIS ONE IS NOT A LIVE CAPTURE AND MUST NOT BE READ AS ONE. Every other file here came off a
node; this one could not, because the `/ls?dir=` handler it exercises exists only in this
checkout and **the fleet has not been flashed**. It is built from two things that ARE measured:
the line format the handler in `firmware/hear_node/hear_node.ino` emits (dir-qualified name,
two spaces, size, ` B`), and real clip names and the real 128044 B clip size observed on nyquist
on 2026-09-09.

It mixes both shipped name shapes on purpose. `nyquist-<boot>-<sample>.wav` is what the FLASHED
fleet writes -- a prefix histogram over 370 live names is `{'ny': 370}`, i.e. no `%02u-` field at
all -- and `07-nyquist-<boot>-<sample>.wav` is what `clip_name()` in this checkout writes. A card
that has been flashed holds both, under two different boot ids, which is exactly why
`hear.clips.parse_clip_name` accepts both and why this fixture carries both.

Replace it with a real capture the first time a node runs a build that has the handler:

    curl 'http://172.16.100.105/ls?dir=/clips' > ls-clips-nyquist.txt

If that capture disagrees with this file, the capture is right.
