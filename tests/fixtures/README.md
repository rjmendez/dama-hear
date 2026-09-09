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
