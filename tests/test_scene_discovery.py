"""The scene lane died silently once; these are the cases that would have caught it.

Firmware moved from scene.csv to scene-YYYYMMDD.csv (commit 4dbfe26) and the drain kept asking
for the old names, fetching a frozen file and ingesting nothing while reporting `ok`. A test that
only checks "the drain fetched something" cannot see that, so these check WHICH names it asks for.
"""
from tools.hear_drain import scene_names, SCENE_FILES


def test_the_dated_file_is_found_and_the_newest_one_is_the_live_one():
    ls = {"scene.csv": 20_751_993, "scene-00000000.csv": 4_096,
          "scene-20260908.csv": 9_000_000, "scene-20260909.csv": 2_003_040, "dets.csv": 10}
    names, live = scene_names(ls)
    assert live == "scene-20260909.csv", "the newest dated file is the one still growing"
    assert set(names) == {"scene.csv", "scene-00000000.csv",
                          "scene-20260909.csv", "scene-20260908.csv"}


def test_the_node_that_serves_no_legacy_file_is_still_drained():
    # rankine, measured 2026-09-09: no scene.csv at all. The old code asked for it, got a 404,
    # recorded "scene.csv absent" and drained no scene rows from that node ever.
    names, live = scene_names({"scene-00000000.csv": 4096, "scene-20260909.csv": 1_605_608})
    assert live == "scene-20260909.csv"
    assert "scene-20260909.csv" in names


def test_the_prelock_file_is_fetched_not_skipped():
    # scene-00000000.csv holds rows written before the node knew the date. They are real rows;
    # pool.py keeps them and marks them unanchored. Skipping them here would silently drop
    # every boot's first minutes.
    names, _ = scene_names({"scene-00000000.csv": 4096, "scene-20260909.csv": 10})
    assert "scene-00000000.csv" in names


def test_a_blind_run_still_fetches_something():
    # /ls failed. A drain that then asks for nothing turns a blind spot into a data outage.
    assert scene_names(None) == (SCENE_FILES, "scene.csv")
    assert scene_names({}) == (SCENE_FILES, "scene.csv")


def test_old_firmware_that_only_has_the_legacy_names_still_works():
    names, live = scene_names({"scene.csv": 100, "scene-prev.csv": 50})
    assert live == "scene.csv", "with no dated file the legacy file is still the live one"
    assert set(names) == {"scene.csv", "scene-prev.csv"}


def test_prev_is_never_mistaken_for_the_live_file():
    names, live = scene_names({"scene.csv": 100, "scene-prev.csv": 50, "scene-20260909.csv": 7})
    assert live == "scene-20260909.csv"
    assert "scene-prev.csv" in names
