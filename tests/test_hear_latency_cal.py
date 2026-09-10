"""tools/hear_latency_cal.py — measuring a phone's audio latency against a co-located PPS node.

⚠️THE FAILURE THIS GUARDS IS A CONFIDENT WRONG NUMBER. The table it writes is consulted by the
phone to correct every arrival, and it was hand-edited: one entry at 13.122 ms, one at 293.499 ms
that the app refuses as implausible, one missing, and an empty `by_model` so the fallback could
never fire. A tool that replaces that with an unverifiable median is not an improvement, so most
of what follows is about what it must REFUSE.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools"))
import hear_latency_cal as LC  # noqa: E402


def _pool(tmp_path, node_events, phone_events, day="2026-09-10"):
    """A minimal pool: one node file and one phone file of anchored records."""
    d = tmp_path / "records" / day
    d.mkdir(parents=True)
    with open(d / "node.jsonl", "w") as fh:
        for name, t in node_events:
            fh.write(json.dumps({"node": name, "source": "node", "ts_utc_s": t}) + "\n")
    with open(d / "phone.jsonl", "w") as fh:
        for name, t in phone_events:
            fh.write(json.dumps({"node": name, "source": "phone", "ts_utc_s": t}) + "\n")
    return str(tmp_path)


class TestMatching:
    def test_each_node_event_is_used_at_most_once(self):
        # ⚠️Otherwise one loud node event pairs with every phone event in a burst and the tool
        # reports a spread of zero from what is really a single coincidence.
        pairs = LC.match([10.000, 10.002, 10.004], [10.001], window_ms=60)
        assert len(pairs) == 1

    def test_it_takes_the_nearest_not_the_first(self):
        pairs = LC.match([10.000], [10.050, 10.001], window_ms=60)
        assert pairs[0][1] == 10.001

    def test_nothing_outside_the_window_is_paired(self):
        assert LC.match([10.0], [10.5], window_ms=60) == []


class TestTheFit:
    def test_a_clean_offset_is_recovered(self):
        # node hears it 13 ms after the phone stamps it -> offset = +13 ms
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.013 for p in phone]
        f = LC.fit(LC.match(phone, node, 60))
        assert f["n"] == 40
        assert f["median_ms"] == pytest.approx(13.0, abs=0.01)
        assert f["mad_ms"] == pytest.approx(0.0, abs=0.01)
        assert LC.verdict(f, 12, 8.0) is None

    def test_the_sign_matches_what_the_app_does_with_it(self):
        # AcousticLatencyCalibration.computeOnsetUtcCorrection: corrected = raw + offset.
        # So a phone stamping EARLY needs a POSITIVE offset to reach the true arrival.
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.013 for p in phone]           # true arrival is later than the raw stamp
        f = LC.fit(LC.match(phone, node, 60))
        assert f["median_ms"] > 0
        raw_ns, off_ns = 100 * 10**9, round(f["median_ms"] * 1e6)
        assert raw_ns + off_ns == pytest.approx(node[0] * 1e9 - 0.0, rel=1e-12)


class TestWhatItMustRefuse:
    @staticmethod
    def _f(offsets_ms, n=40):
        phone = [100.0 + i for i in range(n)]
        node = [p + offsets_ms[i % len(offsets_ms)] / 1000.0 for i, p in enumerate(phone)]
        return LC.fit(LC.match(phone, node, 200))

    def test_too_few_events_is_refused(self):
        f = self._f([13.0], n=5)
        why = LC.verdict(f, 12, 8.0)
        assert why and "not enough" in why

    def test_a_scattered_population_is_refused_rather_than_averaged(self):
        # ⚠️THE ONE THAT MATTERS. Matches spread over tens of ms are not one event seen twice;
        # their median is a number with no referent, and emitting it is the failure mode.
        f = self._f([-40.0, 0.0, 40.0])
        why = LC.verdict(f, 12, 8.0)
        assert why and "not one population" in why

    def test_a_value_the_app_itself_would_refuse_is_refused_here(self):
        # 293.499 ms is the real financialdistress entry: written, then refused on the phone as
        # OFFSET_OUT_OF_RANGE. Refusing it here means it is explained instead of silent.
        # ⚠️the window must exceed the offset or there are no matches to judge -- see fit().
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.293499 for p in phone]
        f = LC.fit(LC.match(phone, node, 400))
        why = LC.verdict(f, 12, 8.0)
        assert why and "plausibility bound" in why

    def test_one_bad_pair_does_not_condemn_a_clean_run(self):
        # MAD not stdev: a single mispaired reflection must not sink 39 good matches.
        offs = [13.0] * 39 + [90.0]
        phone = [100.0 + i for i in range(40)]
        node = [p + offs[i] / 1000.0 for i, p in enumerate(phone)]
        f = LC.fit(LC.match(phone, node, 200))
        assert LC.verdict(f, 12, 8.0) is None


class TestTheToolRefusesToGuessCoLocation:
    def test_pair_is_required(self, tmp_path, capsys):
        p = _pool(tmp_path, [("mach", 100.0)], [("phone-a", 100.0)])
        assert LC.main(["--pool", p]) == 2
        assert "cannot discover co-location" in capsys.readouterr().err

    def test_a_missing_receiver_is_named_not_silently_skipped(self, tmp_path, capsys):
        p = _pool(tmp_path, [("mach", 100.0)], [("phone-a", 100.0)])
        rc = LC.main(["--pool", p, "--pair", "phone-a=nyquist"])
        out = capsys.readouterr().out
        assert rc == 1 and "no anchored records" in out and "nyquist" in out

    def test_the_stated_separation_and_its_bias_are_printed(self, tmp_path, capsys):
        p = _pool(tmp_path, [("mach", 100.0)], [("phone-a", 100.0)])
        LC.main(["--pool", p, "--pair", "phone-a=mach", "--separation-m", "2.0"])
        out = capsys.readouterr().out
        assert "2.00 m" in out and "5.8 ms" in out          # 2.0 / 343 * 1000
        assert "NOT corrected" in out


class TestEmit:
    def _good_pool(self, tmp_path):
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.013 for p in phone]
        return _pool(tmp_path,
                     [("mach", t) for t in node],
                     [("phone-a", t) for t in phone])

    def test_it_writes_ns_and_the_provenance(self, tmp_path):
        p = self._good_pool(tmp_path)
        out = tmp_path / "cal.json"
        assert LC.main(["--pool", p, "--pair", "phone-a=mach", "--emit", str(out)]) == 0
        d = json.load(open(out))
        assert d["by_node_id"]["phone-a"] == pytest.approx(13_000_000, abs=20_000)
        prov = d["_measured"]["phone-a"]
        assert prov["reference_node"] == "mach"
        assert prov["n_events"] == 40
        # ⚠️the separation the operator accepted is recorded, not left in their memory
        assert "stated_separation_m" in prov and "uncorrected_separation_bias_ms" in prov

    def test_an_existing_entry_this_run_could_not_measure_survives(self, tmp_path):
        # the table is hand-maintained; a run that measures one phone must not drop the other
        out = tmp_path / "cal.json"
        out.write_text(json.dumps({"schema": "acoustic_latency_calibration.v1",
                                   "by_node_id": {"other-phone": 999}, "by_model": {"pixel": 5}}))
        p = self._good_pool(tmp_path)
        LC.main(["--pool", p, "--pair", "phone-a=mach", "--emit", str(out)])
        d = json.load(open(out))
        assert d["by_node_id"]["other-phone"] == 999
        assert d["by_model"] == {"pixel": 5}
        assert "phone-a" in d["by_node_id"]

    def test_a_refused_fit_writes_nothing(self, tmp_path, capsys):
        phone = [100.0 + i for i in range(40)]
        node = [p + (0.04 if i % 2 else -0.04) for i, p in enumerate(phone)]
        p = _pool(tmp_path, [("mach", t) for t in node], [("phone-a", t) for t in phone])
        out = tmp_path / "cal.json"
        assert LC.main(["--pool", p, "--pair", "phone-a=mach", "--emit", str(out)]) == 1
        assert not out.exists(), "a refused fit must not leave a file behind"
        assert "left untouched" in capsys.readouterr().out


class TestTheWindowBoundsWhatIsMeasurable:
    """⚠️A latency larger than the window yields NO matches, not a large answer."""

    def test_an_offset_beyond_the_window_finds_nothing(self):
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.293 for p in phone]
        assert LC.match(phone, node, 60) == []

    def test_fit_of_nothing_does_not_raise(self):
        f = LC.fit([])
        assert f["n"] == 0

    def test_the_caller_is_told_the_window_may_be_the_cause(self, tmp_path, capsys):
        phone = [100.0 + i for i in range(40)]
        node = [p + 0.293 for p in phone]
        p = _pool(tmp_path, [("mach", t) for t in node], [("phone-a", t) for t in phone])
        LC.main(["--pool", p, "--pair", "phone-a=mach", "--window-ms", "60"])
        assert "widen --window-ms" in capsys.readouterr().out


class TestUnanchoredRowsAreExcluded:
    def test_unanchored_records_are_not_read(self, tmp_path):
        # ⚠️they carry no UTC at all; including them would pull every fit toward zero
        d = tmp_path / "records" / "unanchored"
        d.mkdir(parents=True)
        with open(d / "phone.jsonl", "w") as fh:
            fh.write(json.dumps({"node": "phone-a", "source": "phone", "ts_utc_s": 1.0}) + "\n")
        day = tmp_path / "records" / "2026-09-10"
        day.mkdir(parents=True)
        with open(day / "node.jsonl", "w") as fh:
            fh.write(json.dumps({"node": "mach", "source": "node", "ts_utc_s": 100.0}) + "\n")
        rows = LC._load(str(tmp_path))
        assert "phone-a" not in rows
