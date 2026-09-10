"""hear-tag: the consumer that finally opens a clip, and everything it refuses to open.

⚠️THE NORMALISATION TEST IS THE ONE THAT MATTERS. Measured on two real nyquist clips pulled off
the card 2026-09-09: at their recorded -56.9 and -62.1 dBFS YAMNet answers `Silence` (0.406,
0.723) and nothing else; RMS-normalised to -20 dBFS the same bytes return Animal/Cricket/Speech
and Animal/Wild animals/Bird. A tagger that skips `normalise` exits 0 forever and tags every
clip Silence, so `TestNormalisationIsNotAnOptimisation` drives a stub model with exactly that
level dependence -- it FAILS if the call is removed, which no assertion on the output shape can.

⚠️THE MODEL IS STUBBED EVERYWHERE EXCEPT ONE OPT-IN TEST. onnxruntime and the 24 MB weights
are not in this checkout and must not be a test dependency: the assertions here are about
normalisation, rate refusal, the refusal census, the accounting invariant and the store, none of
which are properties of YAMNet. `TestAgainstTheRealModel` runs only when both are present.

⚠️NOTHING HERE WRITES OUTSIDE tmp_path.
"""
import json
import os
import re
import subprocess
import sys
import textwrap

import numpy as np
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hear import clips as CLIPS                                 # noqa: E402
from hear import pool as P                                      # noqa: E402
from hear import tags as TAGS                                   # noqa: E402
from tools import hear_tag as HT                                # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "deploy", "k8s"))
import gen_configmap as GC                                      # noqa: E402

MANIFEST = os.path.join(ROOT, "deploy", "k8s", "hear-tag.yaml")

CLIP_SAMPLES = (CLIPS.CLIP_BYTES_16K_4S - 44) // 2


# ----------------------------------------------------------------- fixtures

def wav_bytes(pcm: np.ndarray, fs: int = 16000) -> bytes:
    """The canonical 44-byte header the firmware writes, plus int16 samples.

    Built here rather than with `wave` so a test can state a header rate that disagrees with the
    audio -- which is exactly the mach condition being asserted against.
    """
    import struct
    d = np.clip(np.asarray(pcm) * 32767.0, -32768, 32767).astype("<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(d)) + b"WAVEfmt " + struct.pack("<I", 16)
            + struct.pack("<HHIIHH", 1, 1, fs, fs * 2, 2, 16)
            + b"data" + struct.pack("<I", len(d)) + d)


def quiet_noise(dbfs=-57.0, seed=0, n=CLIP_SAMPLES):
    x = np.random.default_rng(seed).normal(0, 1, n)
    x = x / np.sqrt(np.mean(x ** 2))
    return x * (10.0 ** (dbfs / 20.0))


class StubTagger:
    """A model with a stated, level-dependent answer, so the pipeline's own steps are testable.

    ⚠️IT RETURNS `Silence` BELOW -30 dBFS. That is not decoration: it is the measured behaviour of
    the real model on the real corpus, reproduced in miniature so that removing `normalise` turns
    a green test red. A stub that answered the same at every level would make the normalisation
    step untestable, which is how it comes to be dropped.
    """

    def __init__(self, tail=(0.009, 0.004)):
        self.calls = []
        self.tail = tail

    def tag(self, pcm, floor=HT.SCORE_FLOOR):
        self.calls.append(np.asarray(pcm))
        db = HT.dbfs(pcm)
        scores = ({"Silence": 0.91, "Animal": 0.02} if db < -30.0
                  else {"Animal": 0.31, "Cricket": 0.20, "Speech": 0.19})
        keep = {k: v for k, v in scores.items() if v >= floor}
        dropped = [v for v in scores.values() if v < floor] + list(self.tail)
        return {"scores": keep, "max_unstored_score": max(dropped) if dropped else 0.0,
                "n_classes_scored": HT.MODEL_CLASSES, "n_passes": HT.MODEL_PASSES,
                "embedding": [0.0] * HT.MODEL_EMBED_DIM,
                "embedding_dim": HT.MODEL_EMBED_DIM}


VERIFIED = {"ok": True, "model_sha256": HT.MODEL_SHA256, "class_map_sha256": HT.CLASSMAP_SHA256,
            "model_bytes": HT.MODEL_BYTES, "class_map_bytes": HT.CLASSMAP_BYTES, "problems": []}


def store_clip(root, *, node="nyquist", boot="db21acd5", sample=1082421378, ts=1788997850.8,
               pcm=None, fs=16000, outcome="stored", anchored=True, write_audio=True,
               fs_csv=16000.169, extra=None):
    """One index row plus (optionally) its WAV, laid out exactly as hear-drain leaves them."""
    base = "%s-%s-%010d.wav" % (node, boot, sample)
    clip = "/clips/" + base
    parts = CLIPS.parse_clip_name(clip)
    body = None
    rel = None
    if outcome == "stored":
        body = wav_bytes(quiet_noise() if pcm is None else pcm, fs=fs)
        day = CLIPS._day(ts if anchored else None)
        p = CLIPS.store_path(str(root), day, node, base)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if write_audio:
            with open(p, "wb") as fh:
                fh.write(body)
        rel = os.path.relpath(p, str(root))
    row = CLIPS.index_row(clip=clip, parts=parts, node=node, body=body,
                          probe=CLIPS.wav_probe(body) if body else None,
                          dets={"utc_us": int(ts * 1e6) if anchored else 0,
                                "ts_utc_s": ts if anchored else None, "anchored": anchored,
                                "uptime_s": 70875, "fs_hz": fs_csv, "trigger": "lf",
                                "clip_why": "ok", "dets_origin": "%s:/dets.csv" % node,
                                "record_key": "aa" * 16},
                          path=rel, outcome=outcome, fetched_at=1788998000.0)
    if extra:
        row.update(extra)
    CLIPS.append_index(str(root), [row])
    return row


def run(root, tagger, **kw):
    kw.setdefault("model_dir", "/nonexistent")
    return HT.run(str(root), tagger=tagger, verified=VERIFIED, **kw)


# ----------------------------------------------------------------- the weights

class TestTheWeightsAreProvenOrTheRunStops:
    """⚠️A TAGGER THAT CANNOT NAME ITS WEIGHTS MUST NOT RUN. The alternative is a store full of
    rows attributing scores to a model that did not produce them, which no later reader can
    detect. Every one of these is `verify_weights` HASHING A FILE -- the existing numpy guard in
    hear-drain.yaml tested only that a directory existed, which enforces nothing."""

    def test_absent_weights_are_refused_and_name_the_url(self, tmp_path):
        v = HT.verify_weights(str(tmp_path))
        assert not v["ok"]
        assert any("is absent" in p and "EfficientAT" in p for p in v["problems"]), v["problems"]

    def test_the_right_size_and_the_wrong_bytes_is_still_refused(self, tmp_path):
        """The size check alone would pass this. sha256 is what separates the pinned artifact from
        the next 16 MB file somebody drops in the same directory."""
        mp, cp = HT.weights_paths(str(tmp_path))
        with open(mp, "wb") as fh:
            fh.write(b"\0" * HT.MODEL_BYTES)
        with open(cp, "wb") as fh:
            fh.write(b"\0" * HT.CLASSMAP_BYTES)
        v = HT.verify_weights(str(tmp_path))
        assert not v["ok"]
        assert all("sha256" in p for p in v["problems"]), v["problems"]

    def test_run_refuses_loudly_rather_than_tagging_nothing(self, tmp_path):
        store_clip(tmp_path)
        with pytest.raises(HT.WeightsRefused):
            HT.run(str(tmp_path), model_dir=str(tmp_path), write=False)

    def test_the_cli_exits_2_not_1_so_a_monitor_can_tell_them_apart(self, tmp_path, capsys):
        store_clip(tmp_path)
        assert HT.main(["--pool", str(tmp_path), "--model-dir", str(tmp_path)]) == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_verify_weights_alone_exits_2(self, tmp_path):
        assert HT.main(["--model-dir", str(tmp_path), "--verify-weights"]) == 2

    def test_the_upstream_checkpoint_is_pinned_too_not_only_the_export(self):
        """⚠️THE CHAIN HAS THREE LINKS. Upstream ships PyTorch, this repo ships an export script,
        the pod loads ONNX. Pinning only the ONNX would make the digest a record of what was
        built rather than a check on what it was built FROM."""
        assert re.fullmatch(r"[0-9a-f]{64}", HT.UPSTREAM_SHA256)
        assert HT.UPSTREAM_BYTES == 19708753
        assert "EfficientAT" in HT.UPSTREAM_URL and "v0.0.1" in HT.UPSTREAM_URL
        assert "mn10_as_mAP_471" in HT.UPSTREAM_URL, "the mAP is in the filename; eleven assets \
in that release are all called mn10_as and differ only in mel bins and hop"
        assert "export_mn10_onnx" in HT.MODEL_URL

    def test_the_class_map_is_pinned_to_a_release_tag_not_a_branch(self):
        assert "/v0.0.1/" in HT.CLASSMAP_URL
        assert "/main/" not in HT.CLASSMAP_URL and "/master/" not in HT.CLASSMAP_URL

    def test_the_pinned_digests_are_hex_of_the_right_length(self):
        # A placeholder left in the source ("<pin the 24 MB .onnx>") would make every
        # verification fail closed, but silently -- and nobody would know the pin was never done.
        for s in (HT.MODEL_SHA256, HT.CLASSMAP_SHA256):
            assert re.fullmatch(r"[0-9a-f]{64}", s), s

# ----------------------------------------------------------------- normalisation

class TestNormalisationIsNotAnOptimisation:
    """⚠️MEASURED, NOT ASSUMED. nyquist-db21acd5-1082421378 at its recorded -56.9 dBFS returns
    `Silence 0.406`; the same bytes at -20 dBFS return `Animal 0.307 | Cricket 0.204`. The clips
    are recorded at -49 to -62 dBFS, so this is the whole corpus, not an edge case."""

    def test_silence_at_recorded_levels_is_the_normalisation_bug(self, tmp_path):
        store_clip(tmp_path, pcm=quiet_noise(-55.0))
        st = StubTagger()
        t = run(tmp_path, st, write=False)
        assert t["tagged"] == 1
        assert HT.dbfs(st.calls[0]) == pytest.approx(HT.TARGET_DBFS, abs=0.5), (
            "the model was fed audio at %.1f dBFS. The clip was recorded at -55; without "
            "normalise() the model sees that level and answers Silence for every clip while the "
            "job exits 0." % HT.dbfs(st.calls[0]))
        assert t["silence_top"] == 0

    def test_the_pre_normalisation_level_is_recorded_not_discarded(self, tmp_path):
        store_clip(tmp_path, pcm=quiet_noise(-55.0))
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["pre_norm_dbfs"] == pytest.approx(-55.0, abs=0.5)

    def test_normalise_returns_the_level_it_came_from(self):
        y, pre = HT.normalise(quiet_noise(-48.0))
        assert pre == pytest.approx(-48.0, abs=0.3)
        assert HT.dbfs(y) == pytest.approx(HT.TARGET_DBFS, abs=0.3)

    def test_it_does_not_clip_the_loudest_real_clip(self):
        y, _ = HT.normalise(quiet_noise(-20.0))
        assert float(np.max(np.abs(y))) <= 1.0

    def test_digital_silence_is_refused_rather_than_scaled(self, tmp_path):
        """⚠️Its gain is undefined, and any finite gain produces `Silence` -- which would make a
        producer defect indistinguishable from the bug normalise() exists to prevent."""
        store_clip(tmp_path, pcm=np.zeros(CLIP_SAMPLES))
        t = run(tmp_path, StubTagger(), write=False)
        assert t["refused"] == 1
        assert t["by_reason"] == {HT.R_DIGITAL_SILENCE: 1}


# ----------------------------------------------------------------- the rate

class TestTheRateIsSnappedOrRefused:
    """⚠️A SHIPPED CONDITION, NOT A HYPOTHETICAL. mach headed a whole boot 22624/22848 Hz over
    16 kHz audio.

    ⚠️THE RULE CHANGED ONCE AND THE DISTINCTION IS THE WHOLE POINT. The original rule was "a rate
    nobody configured is refused, because snapping it to the nearest would hide a firmware defect
    behind a confident answer". Snapping to the nearest is a GUESS and stays refused. Recovering
    the rate from the sample count is a PROOF: exactly one rate this fleet clocks turns 64000
    samples into a length this fleet writes, and the node's own fs_hz estimate has to agree. When
    the proof closes the clip is scored and the row carries `header_rate_suspect` saying what was
    corrected and why; when it does not close, the refusal is exactly as it was.
    """

    def test_a_22624_header_whose_length_proves_16k_is_recovered_not_discarded(self, tmp_path):
        """The audio is real 16 kHz audio and the header is the only broken thing about it.
        Throwing it away loses a real detection to a header bug we can prove and correct."""
        store_clip(tmp_path, fs=22624, fs_csv=16000.169)
        t = run(tmp_path, StubTagger(), write=False)
        assert t["tagged"] == 1 and t["refused"] == 0

    def test_the_recovery_is_recorded_on_the_row_never_silent(self, tmp_path):
        row = store_clip(tmp_path, fs=22624, fs_csv=16000.169)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"], got
        fix = got["row"]["header_rate_suspect"]
        assert fix["true_fs_hz"] == 16000.0 and fix["header_fs_hz"] == 22624.0
        assert got["row"]["wav_header_fs_hz"] == 22624, "the lying header is KEPT"
        assert "never clocks" in fix["why"]

    def test_a_bad_rate_the_length_does_NOT_explain_is_still_refused(self, tmp_path):
        """⚠️The refusal did not go away. A clip that is neither a known length at a known rate
        nor recoverable is thrown out, into its own counted bucket."""
        store_clip(tmp_path, fs=22624, pcm=quiet_noise(n=12345), fs_csv=16000.169)
        t = run(tmp_path, StubTagger(), write=False)
        assert t["tagged"] == 0 and t["refused"] == 1
        assert t["by_reason"] == {HT.R_RATE_REFUSED: 1}

    def test_the_nodes_own_estimate_can_veto_the_recovery(self, tmp_path):
        """fs_hz and wav_header_fs_hz are kept side by side because they once disagreed by
        6,624 Hz. When they disagree about the RECOVERED rate too, nothing is recovered."""
        store_clip(tmp_path, fs=22624, fs_csv=48000.0)
        t = run(tmp_path, StubTagger(), write=False)
        assert t["refused"] == 1 and t["by_reason"] == {HT.R_RATE_REFUSED: 1}

    def test_the_measured_15986_spread_is_inside_the_tolerance(self, tmp_path):
        store_clip(tmp_path, fs=15986)
        assert run(tmp_path, StubTagger(), write=False)["tagged"] == 1

    def test_the_refusal_names_both_rates(self, tmp_path):
        store_clip(tmp_path, fs=22624, pcm=quiet_noise(n=12345), fs_csv=16000.169)
        got = HT.tag_one(StubTagger(), CLIPS.read_index(str(tmp_path)).popitem()[1],
                         str(tmp_path), HT.model_block(VERIFIED))
        assert not got["ok"]
        assert "22624" in got["detail"] and "16000.169" in got["detail"]

    def test_resampling_is_deliberate_and_labelled_not_incidental(self, tmp_path):
        """This test used to assert no resampler existed at all. One does now -- the model is
        32 kHz and the fleet is not -- so the invariant moved to what every row must SAY."""
        row = store_clip(tmp_path)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"], got
        for field in ("band_limit_hz", "fs_source_hz", "upsampled"):
            assert field in got["row"], "%s must ride on every tag row" % field


# ----------------------------------------------------------------- the score picture

class TestTheWholeScorePictureIsStored:

    def test_the_discarded_tail_is_quantified_not_invisible(self, tmp_path):
        """⚠️Without max_unstored_score, a floor that is too high and a clip that genuinely
        scored nothing are the same empty dict."""
        store_clip(tmp_path)
        st = StubTagger(tail=(0.0093, 0.0012))
        run(tmp_path, st)
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["max_unstored_score"] == pytest.approx(0.0093)
        assert all(v >= HT.SCORE_FLOOR for v in row["scores"].values())

    def test_the_embedding_ships_at_full_width(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert len(row["embedding"]) == HT.MODEL_EMBED_DIM

    def test_no_hard_top_one_is_stored(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert len(row["scores"]) > 1, "a top-1 would throw away the picture the pool exists for"
        assert "top" not in row and "label" not in row

    def test_the_floor_is_documented_as_storage_not_an_operating_point(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        card = json.load(open(HT.card_path(str(tmp_path))))
        assert "not a decision threshold" in card["score_floor_note"]


# ----------------------------------------------------------------- identity and provenance

class TestEveryRowNamesItsModelAndItsStatus:
    """The same discipline hear_score.py applies to P: a field called `scores` next to a field
    called `node` will be read as "what was heard", so each row says what it is not."""

    def test_the_sha256_is_on_every_row(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["model"]["sha256"] == HT.MODEL_SHA256
        assert row["model"]["class_map_sha256"] == HT.CLASSMAP_SHA256
        assert row["model"]["name"] == HT.MODEL_NAME and row["model"]["version"] == HT.MODEL_VERSION

    def test_provenance_is_a_literal_never_an_inference(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["provenance"] == "model"
        assert row["claim"]["human_verified"] is False

    def test_a_tag_is_not_offered_as_a_training_label(self, tmp_path):
        """§12.1. All 35 dama ant models were trained on circular self-labels; the refusal has to
        travel with the data, not only in a document nobody reads at training time."""
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["claim"]["usable_as_training_label"] is False
        card = json.load(open(HT.card_path(str(tmp_path))))
        assert card["usable_as_training_label"] is False
        assert "circular" in card["why_not_a_training_label"]

    def test_both_rates_travel_side_by_side(self, tmp_path):
        store_clip(tmp_path, fs=15990, fs_csv=16000.169)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["wav_header_fs_hz"] == 15990 and row["csv_fs_hz"] == 16000.169

    def test_the_card_is_written_once_and_referenced_by_the_rows(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["model"]["card"] == "tag_model_card.json"
        assert os.path.exists(HT.card_path(str(tmp_path)))

    def test_every_row_is_json_serialisable(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        for row in TAGS.read_tags(str(tmp_path)):
            json.loads(json.dumps(row, sort_keys=True))


# ----------------------------------------------------------------- the seam to hear/clips.py

class TestTheTaggerReadsOnlyWhatClipsDeclares:
    """Seam S5. The producer is `hear.clips.index_row`; the consumer is `hear_tag.tag_one`. If
    the tagger reaches for a field hear-drain happens to write today but does not declare, the
    two drift the first time the drain changes."""

    DECLARED = {"schema_version", "clip_key", "clip", "basename", "node", "boot", "sample",
                "prio", "outcome", "reason", "path", "bytes", "sha256", "utc_us", "ts_utc_s",
                "anchored", "t_start_utc_s", "t_end_utc_s", "uptime_s", "fs_hz",
                "wav_header_fs_hz", "trigger", "clip_why", "dets_origin", "record_key",
                "fetched_at", "audio_pruned_at", "probe_404s",
                # schema 2: the clip's own length (mis-header corrected) and, when its header
                # rate is provably wrong, what it really is. Two clip geometries and one
                # wrong-rate firmware build made "how long is this clip" stop being a constant.
                "dur_s", "header_rate_suspect"}

    def test_index_row_declares_exactly_these_fields(self, tmp_path):
        row = store_clip(tmp_path)
        assert set(row) <= self.DECLARED, set(row) - self.DECLARED

    def test_the_tagger_reads_only_fields_clips_declares(self, tmp_path):
        store_clip(tmp_path, extra={"a_field_hear_drain_might_add_later": 1})
        index = CLIPS.read_index(str(tmp_path))
        stripped = {k: {f: v for f, v in r.items() if f in self.DECLARED}
                    for k, r in index.items()}
        got = HT.tag_one(StubTagger(), list(stripped.values())[0], str(tmp_path),
                         HT.model_block(VERIFIED))
        assert got["ok"], got

    def test_the_two_wav_readers_agree_about_the_same_bytes(self, tmp_path):
        """`clips.wav_probe` validated this header at drain time and `hear_tag.read_wav` reads it
        again. Two parsers of one format is one more than necessary; that they agree is checked
        rather than assumed."""
        row = store_clip(tmp_path)
        pcm, fs = HT.read_wav(os.path.join(str(tmp_path), row["path"]))
        assert fs == row["wav_header_fs_hz"]
        assert len(pcm) * 2 + 44 == row["bytes"] == CLIPS.CLIP_BYTES_16K_4S


# ----------------------------------------------------------------- the refusal census

class TestNothingFallsIntoADefault:
    """§14's discipline applied to the tag lane: every way a clip can fail to be tagged has its
    own counter, and the totals are asserted rather than hoped for."""

    def test_a_clip_the_node_destroyed_is_refused_by_name(self, tmp_path):
        store_clip(tmp_path, outcome="evicted_before_fetch")
        t = run(tmp_path, StubTagger(), write=False)
        assert t["by_reason"] == {HT.R_NO_AUDIO: 1}

    def test_a_deferred_clip_is_refused_by_the_same_name_and_retried_later(self, tmp_path):
        store_clip(tmp_path, outcome="deferred_by_cap")
        assert run(tmp_path, StubTagger(), write=False)["by_reason"] == {HT.R_NO_AUDIO: 1}

    def test_a_pruned_wav_is_told_apart_from_a_missing_one(self, tmp_path):
        """⚠️DIFFERENT OPERATOR ACTIONS. A pruned WAV is the byte cap working; a missing one with
        no prune recorded is a bug. One counter for both would hide the second inside the first."""
        store_clip(tmp_path, sample=1, write_audio=False, extra={"audio_pruned_at": 1.0})
        store_clip(tmp_path, sample=2, write_audio=False)
        t = run(tmp_path, StubTagger(), write=False)
        assert t["by_reason"] == {HT.R_AUDIO_PRUNED: 1, HT.R_AUDIO_MISSING: 1}

    def test_a_truncated_wav_is_a_counted_reason_not_a_crash(self, tmp_path):
        row = store_clip(tmp_path)
        p = os.path.join(str(tmp_path), row["path"])
        with open(p, "r+b") as fh:
            fh.truncate(44 + 1000)
        t = run(tmp_path, StubTagger(), write=False)
        assert t["refused"] == 1 and HT.R_WAV_SAMPLES in t["by_reason"]

    def test_an_unreadable_wav_is_a_counted_reason(self, tmp_path):
        row = store_clip(tmp_path)
        with open(os.path.join(str(tmp_path), row["path"]), "wb") as fh:
            fh.write(b"not a riff file at all")
        t = run(tmp_path, StubTagger(), write=False)
        assert t["refused"] == 1 and HT.R_WAV_UNREADABLE in t["by_reason"]

    def test_a_model_that_raises_is_a_counted_reason(self, tmp_path):
        store_clip(tmp_path)

        class Boom:
            def tag(self, pcm, floor=0.0):
                raise RuntimeError("interpreter died")

        t = run(tmp_path, Boom(), write=False)
        assert t["by_reason"] == {HT.R_MODEL_ERROR: 1}

    def test_refusals_are_broken_out_by_node_and_day(self, tmp_path):
        # A bad rate the length cannot explain: recoverable ones are now tagged, not refused.
        store_clip(tmp_path, node="mach", fs=22624, pcm=quiet_noise(n=12345))
        store_clip(tmp_path, node="nyquist", outcome="evicted_before_fetch")
        t = run(tmp_path, StubTagger(), write=False)
        assert sorted(t["by_node_day_reason"]) == [
            "mach|2026-09-09|rate_out_of_tolerance", "nyquist|2026-09-09|no_audio_stored"]

    def test_every_reason_this_module_can_emit_is_in_the_vocabulary(self, tmp_path):
        store_clip(tmp_path, node="mach", fs=22624)
        store_clip(tmp_path, sample=3, outcome="evicted_before_fetch")
        store_clip(tmp_path, sample=4, pcm=np.zeros(CLIP_SAMPLES))
        t = run(tmp_path, StubTagger(), write=False)
        assert set(t["by_reason"]) <= set(HT.REFUSAL_REASONS), set(t["by_reason"])


class TestTheAccountingInvariant:

    def test_every_index_row_lands_in_exactly_one_bucket(self, tmp_path):
        store_clip(tmp_path, sample=1)
        store_clip(tmp_path, sample=2, fs=22624)
        store_clip(tmp_path, sample=3, outcome="evicted_before_fetch")
        t = run(tmp_path, StubTagger(), write=False)
        assert t["index_keys"] == 3
        assert t["tagged"] + t["refused"] + t["already_tagged"] + t["deferred"] == 3
        assert t["conservation_ok"]

    def test_a_superseded_line_is_counted_not_double_tagged(self, tmp_path):
        """`prune()` appends a second line for the same clip_key. `read_index` keeps the last; the
        first must show up as `superseded` rather than vanishing from the ledger."""
        row = store_clip(tmp_path)
        CLIPS.append_index(str(tmp_path), [dict(row, audio_pruned_at=1.0)])
        t = run(tmp_path, StubTagger(), write=False)
        assert (t["index_lines"], t["index_keys"], t["superseded"]) == (2, 1, 1)
        assert t["conservation_ok"]

    def test_a_torn_index_line_is_counted_and_not_fatal(self, tmp_path):
        store_clip(tmp_path)
        with open(CLIPS.index_path(str(tmp_path)), "a") as fh:
            fh.write('{"clip_key": "hal\n')
        t = run(tmp_path, StubTagger(), write=False)
        assert t["unparseable"] == 1 and t["conservation_ok"]

    def test_the_invariant_can_actually_break(self, tmp_path, monkeypatch):
        """⚠️Otherwise it restates the loop. `index_census` walks the FILE while the dispatch
        loop walks `read_index`'s collapsed dict, so making the two disagree flips the flag --
        which is what makes it a check and not a restatement of the loop."""
        store_clip(tmp_path)
        assert run(tmp_path, StubTagger(), write=False)["conservation_ok"]
        monkeypatch.setattr(HT, "index_census",
                            lambda root: {"lines": 99, "keys": 99, "superseded": 0,
                                          "unparseable": 0})
        assert not run(tmp_path, StubTagger(), write=False)["conservation_ok"]


# ----------------------------------------------------------------- resume and caps

class TestResumeIsTheStoreItself:

    def test_a_second_run_tags_nothing_new(self, tmp_path):
        store_clip(tmp_path)
        assert run(tmp_path, StubTagger())["tagged"] == 1
        t = run(tmp_path, StubTagger())
        assert (t["tagged"], t["already_tagged"]) == (0, 1)
        assert sum(1 for _ in TAGS.read_tags(str(tmp_path))) == 1

    def test_only_the_new_clip_is_tagged(self, tmp_path):
        store_clip(tmp_path, sample=1)
        run(tmp_path, StubTagger())
        store_clip(tmp_path, sample=2)
        t = run(tmp_path, StubTagger())
        assert (t["tagged"], t["already_tagged"]) == (1, 1)

    def test_a_new_model_version_re_tags_beside_the_old_row(self, tmp_path, monkeypatch):
        """⚠️NOT AN OVERWRITE. The old row is the only record of what the previous model said
        about audio the byte cap may already have deleted."""
        store_clip(tmp_path)
        run(tmp_path, StubTagger())
        monkeypatch.setattr(HT, "MODEL_VERSION", "onnx-2")
        assert run(tmp_path, StubTagger())["tagged"] == 1
        # ⚠️KEYED ON THE DIGEST TOO. Pre-change this returned `name/version` only, so two
        # different weight files scored under one version string reported as ONE model -- the
        # mixing the docstring says is reported.
        held = TAGS.versions_held(str(tmp_path))
        assert sorted(k.rsplit("/", 1)[0] for k in held) == [
            "efficientat-mn10_as/onnx-1", "efficientat-mn10_as/onnx-2"]
        assert all(len(k.rsplit("/", 1)[1]) == 64 for k in held), held

    def test_the_limit_defers_rather_than_dropping(self, tmp_path):
        for i in range(5):
            store_clip(tmp_path, sample=i + 1)
        t = run(tmp_path, StubTagger(), limit=2, write=False)
        assert (t["tagged"], t["deferred"], t["stop_reason"]) == (2, 3, "limit")
        assert t["cap_hit"] and t["conservation_ok"]

    def test_a_deferred_clip_is_picked_up_next_run(self, tmp_path):
        for i in range(3):
            store_clip(tmp_path, sample=i + 1)
        run(tmp_path, StubTagger(), limit=1)
        assert run(tmp_path, StubTagger(), limit=99)["tagged"] == 2

    def test_the_oldest_day_is_tagged_first(self, tmp_path):
        """⚠️ORDER IS BY PRUNE RISK. clips.prune() deletes whole days oldest-first at the byte
        cap, so the oldest untagged clip is the one whose audio disappears next."""
        store_clip(tmp_path, sample=2, ts=1789084250.0)          # 2026-09-10
        store_clip(tmp_path, sample=1, ts=1788997850.0)          # 2026-09-09
        run(tmp_path, StubTagger(), limit=1)
        row = next(iter(TAGS.read_tags(str(tmp_path))))
        assert row["day"] == "2026-09-09"


# ----------------------------------------------------------------- the scene join (seam S7)

def _scene_pool(tmp_path, node="nyquist", n=12, t0=1788997840.0, start=1082300000,
                anchored=True):
    """A pool whose scene store holds `n` consecutive 1.024 s rows, written the way
    Pool.ingest_scene would leave them. Written directly because building a scene.csv here would
    test hear.scenefile, which has its own suite."""
    import gzip
    pl = P.Pool(str(tmp_path))
    day = CLIPS._day(t0 if anchored else None)
    d = os.path.join(pl.scene_dir, day)
    os.makedirs(d, exist_ok=True)
    rows = []
    for i in range(n):
        ts = t0 + i * 1.024
        rows.append(json.dumps({
            "key": "k%02d" % i, "source": "scene", "node": node,
            "utc_us": int(ts * 1e6) if anchored else 0, "anchored": anchored,
            "ts_utc_s": ts if anchored else None, "sample": str(start + i * 16384),
            "span_ms": 1024, "bands": 20, "slices": 4, "frames_summed": 64}))
    with gzip.open(os.path.join(d, "%s.jsonl.gz" % node), "at") as fh:
        fh.write("\n".join(rows) + "\n")
    return pl


def _scene_row(tmp_path, *, node="nyquist", ts, span_ms=1024, key="edge"):
    """One scene row at an exact ts/span, for the half-open-boundary tests -- `_scene_pool`
    only offers a fixed 1.024 s cadence starting at a chosen t0, which cannot place a row's
    edge exactly on the clip window without back-solving t0 for every case."""
    import gzip
    pl = P.Pool(str(tmp_path))
    day = CLIPS._day(ts)
    d = os.path.join(pl.scene_dir, day)
    os.makedirs(d, exist_ok=True)
    row = {"key": key, "source": "scene", "node": node, "utc_us": int(ts * 1e6),
          "anchored": True, "ts_utc_s": ts, "sample": "0", "span_ms": span_ms,
          "bands": 20, "slices": 4, "frames_summed": 64}
    with gzip.open(os.path.join(d, "%s.jsonl.gz" % node), "at") as fh:
        fh.write(json.dumps(row) + "\n")
    return pl


class TestTheSceneJoinIsReadOnlyAndSaysHowStrongItIs:

    def test_a_clip_overlaps_four_or_five_scene_rows_never_fewer(self, tmp_path):
        """64000 samples against 16384 = 3.906 rows, so 4 or 5 depending on phase. Both extremes
        are checked, because a half-open/closed slip shows up at exactly one of them."""
        pl = _scene_pool(tmp_path)
        for offset in (0.0, 0.512, 1.023):
            row = store_clip(tmp_path, sample=int(1e6) + int(offset * 1000),
                             ts=1788997845.0 + offset)
            got = TAGS.scene_overlap(pl, row)
            assert got["basis"] == "utc"
            assert 4 <= len(got["rows"]) <= 5, (offset, len(got["rows"]))
            assert got["refused"] is None and got["weak"] is False

    def test_a_clip_straddling_midnight_is_not_half_lost(self, tmp_path):
        """A 4.0 s window at 23:59:58 lands in two day partitions; scanning only the start day
        would silently drop the rows after the roll."""
        assert TAGS._days_touched(1788911998.0, 1788912002.0) == ["2026-09-08", "2026-09-09"]

    def test_an_unanchored_clip_refuses_the_utc_basis_and_says_so(self, tmp_path):
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, anchored=False)
        got = TAGS.scene_overlap(pl, row)
        assert got["basis"] == "none" and got["rows"] == []
        assert "boot" in got["refused"] and "resets to 0" in got["refused"]

    def test_the_sample_basis_is_opt_in_and_marks_itself_weak(self, tmp_path):
        pl = _scene_pool(tmp_path, anchored=False, t0=None if False else 0.0)
        row = store_clip(tmp_path, anchored=False, sample=1082300000 + 16384 * 3)
        got = TAGS.scene_overlap(pl, row, allow_sample_basis=True)
        assert got["basis"] == "sample" and got["weak"] is True
        assert "boot id" in got["weakness"]
        assert 4 <= len(got["rows"]) <= 5

    def test_an_anchored_clip_with_no_scene_rows_is_an_empty_answer_not_a_fallback(self, tmp_path):
        """⚠️0 rows on an anchored clip is a real finding -- the scene store does not hold that
        node/day. Silently retrying on the weak sample basis would turn it into a wrong answer."""
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path)
        got = TAGS.scene_overlap(pl, row)
        assert got["basis"] == "utc" and got["rows"] == [] and got["refused"] is None

    def test_the_join_carries_the_scene_key_not_the_mel_payload(self, tmp_path):
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, ts=1788997845.0)
        got = TAGS.scene_overlap(pl, row)
        assert got["rows"] and "mel_b64" not in got["rows"][0]
        assert "key" in got["rows"][0]

    def test_the_sample_window_derives_the_pre_roll_so_no_caller_has_to(self, tmp_path):
        row = store_clip(tmp_path, sample=1082421378)
        w = TAGS.sample_window(row)
        assert w["start_sample"] == 1082421378 - 16000
        assert w["end_sample"] == 1082421378 + 48000
        assert w["end_sample"] - w["start_sample"] == CLIP_SAMPLES

    def test_the_clip_geometry_in_tags_matches_hear_clips(self):
        """tags.py restates CLIP_PRE_S so it can ship without clips.py. The two must agree."""
        assert (TAGS.CLIP_PRE_S, TAGS.CLIP_POST_S) == (CLIPS.CLIP_PRE_S, CLIPS.CLIP_POST_S)
        assert TAGS.FS_NOMINAL_HZ == CLIPS.FS_NOMINAL_HZ

    def test_scene_overlap_writes_nothing(self, tmp_path):
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, ts=1788997845.0)
        before = sorted(os.walk(str(tmp_path)))
        TAGS.scene_overlap(pl, row)
        assert sorted(os.walk(str(tmp_path))) == before

    def test_a_malformed_scene_row_is_refused_and_counted_not_silently_skipped(self, tmp_path):
        """A row inside a dated partition with no ts/span is a malformed row, not a normal gap --
        it must be counted, never fall out of the tally the way a bare `continue` would."""
        import gzip
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, ts=1788997845.0)
        d = os.path.join(str(tmp_path), "scene", "2026-09-09")
        with gzip.open(os.path.join(d, "nyquist.jsonl.gz"), "at") as fh:
            fh.write(json.dumps({"key": "bad1", "node": "nyquist", "anchored": True,
                                 "ts_utc_s": None, "span_ms": 1024}) + "\n")
        got = TAGS.scene_overlap(pl, row)
        assert got["refused_records"] == 1

    def test_a_scene_row_ending_exactly_at_the_clip_start_does_not_overlap(self, tmp_path):
        """ts + span == t0 exactly: the half-open window [t0, t1) is not touched. Mirrors
        TestTheSketchJoinIsReadOnlyAndSaysHowStrongItIs's edge tests for the other store."""
        row = store_clip(tmp_path, ts=1788997850.8)          # t0 = 1788997849.8
        pl = _scene_row(tmp_path, ts=1788997848.776)         # 1788997848.776 + 1.024 == t0
        got = TAGS.scene_overlap(pl, row)
        assert got["rows"] == []

    def test_a_scene_row_one_ms_inside_the_clip_start_does_overlap(self, tmp_path):
        row = store_clip(tmp_path, ts=1788997850.8)
        pl = _scene_row(tmp_path, ts=1788997848.777, key="edge-in-start")
        got = TAGS.scene_overlap(pl, row)
        assert [r["key"] for r in got["rows"]] == ["edge-in-start"]

    def test_a_scene_row_starting_exactly_at_the_clip_end_does_not_overlap(self, tmp_path):
        """ts == t1 exactly."""
        row = store_clip(tmp_path, ts=1788997850.8)          # t1 = 1788997853.8
        pl = _scene_row(tmp_path, ts=1788997853.8)
        got = TAGS.scene_overlap(pl, row)
        assert got["rows"] == []

    def test_a_scene_row_one_ms_inside_the_clip_end_does_overlap(self, tmp_path):
        row = store_clip(tmp_path, ts=1788997850.8)
        pl = _scene_row(tmp_path, ts=1788997853.799, key="edge-in-end")
        got = TAGS.scene_overlap(pl, row)
        assert [r["key"] for r in got["rows"]] == ["edge-in-end"]


# --------------------------------------------------------- the sketch join

def store_sketch(root, *, node="nyquist", ts=None, sample=1082421378, fs_hz=16000.0,
                 trigger="lf", clip=None, key=None, anchored=True, source="node"):
    """One raw pool record laid out exactly as `hear.pool.Pool._append` leaves it -- day-
    partitioned under records/<day>/<source>.jsonl -- with only the fields `sketch_overlap`
    declares it reads. The mel payload is irrelevant to a window join and is not written."""
    utc_us = int(ts * 1e6) if (anchored and ts) else 0
    row = {"schema_version": 1, "key": key or ("sk-%s-%s" % (node, sample)), "source": source,
          "node": node, "utc_us": utc_us, "anchored": bool(anchored and ts),
          "ts_utc_s": ts if (anchored and ts) else None, "fs_hz": fs_hz, "sample": sample,
          "trigger": trigger, "clip": clip}
    day = P._day(row["ts_utc_s"])
    d = os.path.join(str(root), "records", day)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "%s.jsonl" % source), "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


class TestTheSketchJoinIsReadOnlyAndSaysHowStrongItIs:
    """Mirrors TestTheSceneJoinIsReadOnlyAndSaysHowStrongItIs -- same discipline, a different
    store. The sketch window at 16 kHz is NFFT/fs + (FRAMES-1)*HOP_S = 256/16000 + 7*0.004 =
    0.044 s, starting one hop (0.004 s) before the record's own `ts_utc_s`."""

    def test_the_own_trigger_sketch_is_flagged_and_others_are_not(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        store_sketch(tmp_path, ts=1788997850.8, sample=1082421378, key="aa" * 16)
        store_sketch(tmp_path, ts=1788997851.2, sample=1082427698, key="bb" * 16)
        got = TAGS.sketch_overlap(pl, row)
        assert got["basis"] == "utc" and got["refused"] is None
        by_key = {r["key"]: r for r in got["rows"]}
        assert by_key["aa" * 16]["is_trigger"] is True
        assert by_key["bb" * 16]["is_trigger"] is False

    def test_a_sketch_ending_exactly_at_the_clip_start_does_not_overlap(self, tmp_path):
        """s1 = ts + 0.04 == t0 exactly: the half-open window [t0, t1) is not touched."""
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)          # t0 = 1788997849.8
        store_sketch(tmp_path, ts=1788997849.76)             # s1 == t0 exactly
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == []

    def test_a_sketch_one_ms_inside_the_clip_start_does_overlap(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        store_sketch(tmp_path, ts=1788997849.761, key="edge-in-start")
        got = TAGS.sketch_overlap(pl, row)
        assert [r["key"] for r in got["rows"]] == ["edge-in-start"]

    def test_a_sketch_starting_exactly_at_the_clip_end_does_not_overlap(self, tmp_path):
        """s0 = ts - 0.004 == t1 exactly."""
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)          # t1 = 1788997853.8
        store_sketch(tmp_path, ts=1788997853.804)
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == []

    def test_a_sketch_one_ms_inside_the_clip_end_does_overlap(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        store_sketch(tmp_path, ts=1788997853.803, key="edge-in-end")
        got = TAGS.sketch_overlap(pl, row)
        assert [r["key"] for r in got["rows"]] == ["edge-in-end"]

    def test_an_unanchored_clip_refuses_the_utc_basis_and_says_so(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, anchored=False)
        got = TAGS.sketch_overlap(pl, row)
        assert got["basis"] == "none" and got["rows"] == []
        assert "boot" in got["refused"] and "resets to 0" in got["refused"]

    def test_the_sample_basis_is_opt_in_and_marks_itself_weak(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, anchored=False, sample=1082421378)
        store_sketch(tmp_path, anchored=False, sample=1082421378 + 100, key="unanchored-sk")
        got = TAGS.sketch_overlap(pl, row, allow_sample_basis=True)
        assert got["basis"] == "sample" and got["weak"] is True
        assert "boot id" in got["weakness"]
        assert [r["key"] for r in got["rows"]] == ["unanchored-sk"]
        assert TAGS.sketch_overlap(pl, row)["refused"] is not None  # off by default

    def test_a_48khz_phone_sketch_window_is_33ms_not_44(self, tmp_path):
        """docs/acoustic-stack.md measures 33-44 ms because NFFT is a SAMPLE count: 256/48000 is
        narrower than 256/16000. A record that states fs_hz=48000 must use the narrower window,
        or a phone sketch would be joined into clip windows it never actually reached."""
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)          # t0 = 1788997849.8
        # At 48 kHz the window is 256/48000 + 7*0.004 = 0.03333 s, one hop (0.004 s) before ts.
        # ts = 1788997849.767 -> s0 = 1788997849.763, s1 = 1788997849.79633 < t0: NOT an overlap
        # at 48 kHz, but WOULD be at the 16 kHz formula (s1 = ts + 0.04 = 1788997849.807 > t0).
        store_sketch(tmp_path, ts=1788997849.767, fs_hz=48000.0, key="phone-narrow")
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == []

    def test_an_unanchored_sketch_record_inside_a_dated_partition_is_refused_and_counted(
            self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        day = P._day(1788997850.8)
        d = os.path.join(str(tmp_path), "records", day)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "node.jsonl"), "a") as fh:
            fh.write(json.dumps({"key": "malformed", "node": "nyquist", "anchored": False,
                                 "ts_utc_s": None, "fs_hz": 16000.0}) + "\n")
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == [] and got["refused_records"] == 1

    def test_a_sketch_with_no_stated_fs_is_refused_and_counted_not_dropped(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        store_sketch(tmp_path, ts=1788997850.9, fs_hz=None, key="no-fs")
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == [] and got["refused_records"] == 1

    def test_a_different_node_is_not_joined(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, node="nyquist", ts=1788997850.8)
        store_sketch(tmp_path, node="mach", ts=1788997850.8, key="wrong-node")
        got = TAGS.sketch_overlap(pl, row)
        assert got["rows"] == []

    def test_sketch_overlap_writes_nothing(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        store_sketch(tmp_path, ts=1788997850.8)
        before = sorted(os.walk(str(tmp_path)))
        TAGS.sketch_overlap(pl, row)
        assert sorted(os.walk(str(tmp_path))) == before

    def test_the_sketch_geometry_matches_hear_sketch(self):
        """tags.py restates FRAMES/NFFT/HOP_S so it can ship without hear/sketch.py (TAG_CODE
        excludes it deliberately). The two must agree."""
        from hear import sketch as SK
        from hear.node import detect as DET
        assert TAGS.SKETCH_FRAMES == SK.FRAMES
        assert TAGS.SKETCH_NFFT == SK.NFFT
        assert TAGS.SKETCH_HOP_S == SK.HOP_S
        # a THIRD constant, equal to the hop by coincidence at every rate so far
        assert TAGS.SKETCH_BACK_S == DET.SKETCH_BACK_S

    @pytest.mark.parametrize("fs_hz", [16000.0, 48000.0, 32000.0])
    def test_the_sample_basis_window_is_in_the_decimated_counter_at_every_frame_rate(
            self, tmp_path, fs_hz):
        """The pool row pairs `sample` -- night_node's DECIMATED counter -- with `fs_hz`, the rate
        the frame was CUT at, 48000.0 on every node frame since the sketch moved to the
        acquisition stream. Only FS_NOMINAL_HZ indexes the counter sample_window() built cs0/cs1
        in; the frame's own rate gives the window's LENGTH IN SECONDS and nothing else.

        Both edges are recovered by probing rather than recomputed, so this fails against
        `start = trig - round(SKETCH_HOP_S * fs)` / `end = start + round(span * fs)`: at
        fs_hz=48000 that form measures back=192 and length=1600 decimated samples (12.0 ms and
        100.0 ms) where the frame really covers 4.0 ms and 33.3 ms. 32000.0 is here because a
        guard aimed at the one rate that broke goes blind at the next one."""
        pl = P.Pool(str(tmp_path))
        s = 1082421378
        row = store_clip(tmp_path, anchored=False, sample=s)
        # ⚠️THE ROW, NOT A BARE {"sample": s}. sample_window() reads the clip's LENGTH off the
        # row now, because the 16 kHz era wrote 1.0+3.0 s and the 48 kHz firmware writes
        # 1.0+4.0 s. A synthetic dict takes the fallback post-roll, so this compared a window
        # built from the fallback against rows joined with the row-derived one.
        win = TAGS.sample_window(row)
        cs0, cs1 = win["start_sample"], win["end_sample"]
        probes = list(range(cs0 - 2000, cs0 + 50)) + list(range(cs1 - 50, cs1 + 2000))
        for t in probes:
            store_sketch(tmp_path, anchored=False, sample=t, fs_hz=fs_hz, key="p%d" % t)
        got = TAGS.sketch_overlap(pl, row, allow_sample_basis=True, max_rows=10 ** 6)
        hit = sorted(int(r["sample"]) for r in got["rows"])
        assert hit and not got["truncated"]
        assert probes[0] < hit[0] and hit[-1] < probes[-1], \
            "the window ran off the probed range, so its edges were not measured"
        # overlap is `start < cs1 and end > cs0`, so the two extreme hits name both edges exactly
        back = hit[-1] + 1 - cs1
        length = cs0 + back - hit[0] + 1
        assert back == int(round(TAGS.SKETCH_BACK_S * TAGS.FS_NOMINAL_HZ)), back
        assert length == int(round(TAGS._sketch_span_s(fs_hz) * TAGS.FS_NOMINAL_HZ)), length


class TestTheJoinedContextNeverPresentsAModelScoreAsGroundTruth:
    """joined_context is LAYER 4: a tag joined onto both scene and sketch in one read-only
    bundle. Every assertion here is about the guard, not the join arithmetic already proven by
    the two overlap test classes above."""

    def _tag_row(self, **over):
        row = {"tag_key": "t1", "clip_key": "c1", "provenance": "model",
              "model": {"name": "efficientat-mn10_as", "version": "onnx-1"},
              "claim": {"usable_as_training_label": False, "human_verified": False}}
        row.update(over)
        return row

    def test_a_tag_row_without_model_provenance_is_refused_not_joined(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        with pytest.raises(ValueError, match="provenance"):
            TAGS.joined_context(pl, self._tag_row(provenance="human"), row)

    def test_a_tag_row_with_no_provenance_at_all_is_refused(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, ts=1788997850.8)
        bad = self._tag_row()
        del bad["provenance"]
        with pytest.raises(ValueError):
            TAGS.joined_context(pl, bad, row)

    def test_the_bundle_restates_provenance_model_and_claim_at_its_own_top_level(self, tmp_path):
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, ts=1788997845.0)
        got = TAGS.joined_context(pl, self._tag_row(), row)
        assert got["provenance"] == "model"
        assert got["model"] == {"name": "efficientat-mn10_as", "version": "onnx-1"}
        assert got["claim"]["usable_as_training_label"] is False
        assert got["tag_key"] == "t1" and got["clip_key"] == "c1"
        assert got["scene"]["basis"] == "utc"
        assert got["sketch"]["basis"] == "utc"
        assert got["refused"] is None

    def test_an_unanchored_clip_is_refused_once_not_twice(self, tmp_path):
        """The same clip failing the same way in both stores is one refusal in the bundle, not
        two contradictory ones a caller would have to reconcile."""
        pl = P.Pool(str(tmp_path))
        row = store_clip(tmp_path, anchored=False)
        got = TAGS.joined_context(pl, self._tag_row(), row)
        assert got["refused"] is not None
        assert got["scene"]["basis"] == "none" and got["sketch"]["basis"] == "none"

    def test_joined_context_writes_nothing(self, tmp_path):
        pl = _scene_pool(tmp_path)
        row = store_clip(tmp_path, ts=1788997845.0)
        before = sorted(os.walk(str(tmp_path)))
        TAGS.joined_context(pl, self._tag_row(), row)
        assert sorted(os.walk(str(tmp_path))) == before


# ----------------------------------------------------------------- the gate

class TestTheCheckCanActuallyFail:
    """Every gate here is one that can fire, and the absolute ones come first: an empty read must
    not pass vacuously. hear_drain.check()'s `if not sensors: return 1` is the same shape."""

    def _beat(self, tmp_path, **kw):
        base = {"index_lines": 1, "index_keys": 1, "superseded": 0, "unparseable": 0,
                "tagged": 1, "refused": 0, "already_tagged": 0, "deferred": 0,
                "cap_hit": False, "stop_reason": None, "conservation_ok": True,
                "weights_ok": True, "silence_top": 0, "by_reason": {}, "by_node": {},
                "by_node_day_reason": {}, "model": {}, "silence_frac": 0.0,
                "observation_not_health": {}}
        base.update(kw)
        HT.write_heartbeat(str(tmp_path), base, now=kw.pop("now", 1000.0))
        return base

    def test_no_heartbeat_at_all_fails(self, tmp_path):
        assert HT.check_tags(str(tmp_path))[0] == 1

    def test_a_heartbeat_with_no_runs_fails(self, tmp_path):
        HT._write_json_atomic(HT.heartbeat_path(str(tmp_path)), {"runs": []})
        assert HT.check_tags(str(tmp_path))[0] == 1

    def test_an_unreadable_heartbeat_fails(self, tmp_path):
        os.makedirs(HT.state_dir(str(tmp_path)), exist_ok=True)
        with open(HT.heartbeat_path(str(tmp_path)), "w") as fh:
            fh.write("{not json")
        assert HT.check_tags(str(tmp_path))[0] == 1

    def test_an_empty_read_fails_rather_than_passing_vacuously(self, tmp_path):
        """⚠️Point --pool at /pool instead of /pool/corpus and every growth-conditioned gate is
        trivially satisfied: 0 rows, 0 refusals, 0 == 0 accounting."""
        self._beat(tmp_path, index_keys=0, tagged=0)
        code, lines = HT.check_tags(str(tmp_path), now=1000.0)
        assert code == 1 and any("EMPTY" in l for l in lines)

    def test_a_stale_tagger_fails_whatever_the_store_looks_like(self, tmp_path):
        self._beat(tmp_path)
        code, lines = HT.check_tags(str(tmp_path), now=1000.0 + 99999)
        assert code == 1 and any("STALE" in l for l in lines)

    def test_unverified_weights_fail_the_gate(self, tmp_path):
        self._beat(tmp_path, weights_ok=False)
        code, lines = HT.check_tags(str(tmp_path), now=1000.0)
        assert code == 1 and any("weights" in l and "REFUSED" in l for l in lines)

    def test_a_broken_accounting_invariant_fails(self, tmp_path):
        self._beat(tmp_path, conservation_ok=False)
        assert HT.check_tags(str(tmp_path), now=1000.0)[0] == 1

    def test_a_torn_index_line_fails(self, tmp_path):
        self._beat(tmp_path, unparseable=2)
        assert HT.check_tags(str(tmp_path), now=1000.0)[0] == 1

    def test_a_store_that_grew_with_nothing_tagged_fails(self, tmp_path):
        self._beat(tmp_path, index_keys=1, tagged=0, now=1000.0)
        self._beat(tmp_path, index_keys=40, tagged=0, now=1100.0)
        code, lines = HT.check_tags(str(tmp_path), now=1200.0)
        assert code == 1 and any("STUCK" in l for l in lines)

    def test_a_refusal_bucket_appearing_where_it_never_had_fails(self, tmp_path):
        """⚠️A REASON IN A NEW BUCKET IS AN EVENT, NOT A RATE. One boot's fs latch flips a whole
        node from 0% refused to 100% with nothing in between, so a percentage never warns."""
        self._beat(tmp_path, by_node_day_reason={}, now=1000.0)
        self._beat(tmp_path, by_node_day_reason={"mach|2026-09-09|rate_out_of_tolerance": 9},
                   now=1100.0)
        code, lines = HT.check_tags(str(tmp_path), now=1200.0)
        assert code == 1 and any("refusals NEW" in l for l in lines)

    def test_the_first_run_is_exempt_from_the_new_bucket_gate(self, tmp_path):
        self._beat(tmp_path, by_node_day_reason={"mach|2026-09-09|rate_out_of_tolerance": 9},
                   now=1000.0)
        assert HT.check_tags(str(tmp_path), now=1000.0)[0] == 0

    def test_a_binding_cap_reaches_the_gate(self, tmp_path):
        self._beat(tmp_path, cap_hit=True, stop_reason="deadline", deferred=200, now=1000.0)
        code, lines = HT.check_tags(str(tmp_path), now=1000.0)
        assert code == 1 and any("BINDING" in l for l in lines)

    def test_the_silence_gate_is_report_only_by_default_and_says_so(self, tmp_path):
        self._beat(tmp_path, tagged=10, silence_top=10, now=1000.0)
        code, lines = HT.check_tags(str(tmp_path), now=1000.0)
        assert code == 0
        line = [l for l in lines if l.startswith("silence")][0]
        assert "REPORT" in line and "NOT GATED" in line and "1.000" in line

    def test_it_does_fire_once_a_threshold_is_set(self, tmp_path):
        self._beat(tmp_path, tagged=10, silence_top=10, now=1000.0)
        code, lines = HT.check_tags(str(tmp_path), max_silence_frac=0.5, now=1000.0)
        assert code == 1 and any("HIGH" in l for l in lines)

    def test_a_quiet_night_is_not_a_failure(self, tmp_path):
        """⚠️A gate keyed on 'did anything score high' fires on a correct result. Nothing here
        reads the class distribution."""
        self._beat(tmp_path, tagged=50, silence_top=0, now=1000.0)
        assert HT.check_tags(str(tmp_path), now=1000.0)[0] == 0

    def test_the_check_reads_a_window_of_the_ring_not_only_the_last_run(self, tmp_path):
        self._beat(tmp_path, unparseable=3, now=1000.0)
        self._beat(tmp_path, now=1100.0)
        assert HT.check_tags(str(tmp_path), window_s=7200, now=1200.0)[0] == 1
        assert HT.check_tags(str(tmp_path), window_s=10, now=1200.0)[0] == 0


# ----------------------------------------------------------------- the manifest (seam S9)

def _containers(text):
    out, name, buf, grabbing = {}, None, [], False
    for line in text.splitlines():
        m = re.match(r"\s*- name: (\S+)\s*$", line)
        if m and not grabbing:
            name = m.group(1)
        if re.match(r"\s*args:\s*$", line) and name:
            grabbing, buf = True, []
            continue
        if grabbing:
            if re.match(r"\s*(volumeMounts|resources|env|image|command):", line):
                out[name] = "\n".join(buf)
                grabbing, name = False, None
                continue
            buf.append(line)
    if grabbing and name:
        out[name] = "\n".join(buf)
    return out


def _mounts(text):
    out, name = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*- name: (\S+)\s*$", line)
        if m:
            name = m.group(1)
            out.setdefault(name, set())
            continue
        m = re.search(r"name: code,.*subPath: (\S+?)\s*\}", line)
        if m and name:
            out[name].add(m.group(1))
    return out


@pytest.fixture(scope="module")
def manifest():
    with open(MANIFEST) as fh:
        return fh.read()


@pytest.fixture(scope="module")
def docs(manifest):
    return [d for d in yaml.safe_load_all(manifest) if d]


class TestTheTagJobIsSuspendedUntilAHumanHasListened:
    """§7. Nine clips left a node before this branch and nobody has listened to any of them.
    Three models agreeing on "Dog" is corroboration, not ground truth."""

    def test_both_cronjobs_ship_suspended(self, docs):
        assert [d["metadata"]["name"] for d in docs] == ["hear-tag", "hear-tag-check"]
        for d in docs:
            assert d["spec"]["suspend"] is True, d["metadata"]["name"]

    def test_the_unsuspend_condition_is_recorded_on_the_object(self, docs):
        a = docs[0]["metadata"]["annotations"]["dama-hear/unsuspend-gate"]
        assert "clip-calibration" in a and "mach" in a

    def test_the_check_ships_the_report_only_threshold(self, manifest):
        blocks = _containers(manifest)
        body = "\n".join(l for l in blocks["check"].splitlines() if not l.strip().startswith("#"))
        assert "--max-silence-frac -1" in body, (
            "the silence gate must ship report-only: its threshold comes from a measured "
            "calibration set that does not exist yet")

    def test_it_never_reaches_a_node(self, manifest):
        """⚠️COMMENTS STRIPPED FIRST -- the comment explaining this rule names a node IP, and a
        guard that matched its own prose would pass on a manifest that really did reach one."""
        body = "\n".join(l for l in manifest.splitlines() if not l.strip().startswith("#"))
        assert "172.16.100." not in body, (
            "the ESP32 serves ONE client at a time and refuses the rest; a second workload "
            "touching a node converts hear-drain's slow run into a refused one")


class TestTheWeightsAreVerifiedBeforeAnythingIsTagged:

    def test_the_preamble_verifies_unconditionally_before_the_exec(self, manifest):
        """⚠️COMMENTS STRIPPED FIRST. A grep over the whole block would match the comment
        explaining the check; this repo shipped that bug twice."""
        blocks = _containers(manifest)
        body = "\n".join(l for l in blocks["tag"].splitlines() if not l.strip().startswith("#"))
        assert "--verify-weights" in body
        last = body.rindex("--verify-weights")
        assert last < body.index("exec python"), (
            "the unconditional verify must run BEFORE the tagging exec, not after it")
        # ⚠️THE INVARIANT IS "VERIFY GATES THE EXEC", NOT "VERIFY IS CALLED TWICE". Under YAMNet
        # the model was fetchable, so the shape was a probe, a fetch, then a gate -- and the test
        # counted calls. mn10_as is staged out of band (upstream ships PyTorch; converting it in
        # the pod would mean 3 GB of torch in a 5 Gi PVC), so there is one call and it exits.
        # Counting calls would now fail a preamble that is strictly stricter than the old one.
        gate = body[last:body.index("exec python")]
        assert "exit 1" in gate, (
            "the verify must FAIL the job, not warn: no path may reach the exec unverified")
        assert "|| true" not in body.split("exec python")[0], (
            "a verify swallowed by `|| true` gates nothing")

    def test_the_preamble_is_valid_shell(self, docs, tmp_path):
        s = docs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
        p = tmp_path / "pre.sh"
        p.write_text(s)
        assert subprocess.run(["/bin/sh", "-n", str(p)]).returncode == 0

    def test_it_does_not_shell_out_to_wget_or_curl(self, manifest):
        """Neither exists in python:*-slim. A loader that shells out to wget is one of the
        documented reasons PANNs was refused; repeating it here would be the same defect."""
        blocks = _containers(manifest)
        body = "\n".join(l for l in blocks["tag"].splitlines() if not l.strip().startswith("#"))
        assert "wget" not in body and "curl" not in body

    def test_the_venv_is_not_the_drains(self, manifest):
        blocks = _containers(manifest)
        body = "\n".join(l for l in blocks["tag"].splitlines() if not l.strip().startswith("#"))
        assert "/pool/pylib-tag" in body and "--target \"$LIB\"" in body
        assert "/pool/pylib\"" not in body, (
            "the tag lane must not resolve its dependencies into the dir the drain imports from")


class TestTheCheckJobCanActuallyFail:
    """⚠️Run it. hear-drain shipped a check job whose script ended on a command that always
    succeeds, so the shell returned 0 whatever the gate found. Only the shell can prove it gone."""

    def _run(self, script, tmp_path, check_rc):
        stub = tmp_path / "python"
        stub.write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  [ \"$a\" = \"--check\" ] && exit %d\n"
            "  [ \"$a\" = \"--verify-weights\" ] && { echo WEIGHTS_RAN; exit 0; }\n"
            "done\nexit 0\n" % check_rc)
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        return subprocess.run(["/bin/sh", "-lc", script], capture_output=True, text=True, env=env)

    def _script(self, docs):
        s = docs[1]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
        return textwrap.dedent(s)

    def test_a_failing_check_fails_the_job(self, docs, tmp_path):
        assert self._run(self._script(docs), tmp_path, check_rc=1).returncode == 1

    def test_a_passing_check_passes_the_job(self, docs, tmp_path):
        assert self._run(self._script(docs), tmp_path, check_rc=0).returncode == 0

    def test_the_weight_summary_still_prints_when_the_check_failed(self, docs, tmp_path):
        r = self._run(self._script(docs), tmp_path, check_rc=1)
        assert "WEIGHTS_RAN" in r.stdout

    def test_the_summary_cannot_mask_a_failing_check(self, docs, tmp_path):
        stub = tmp_path / "python"
        stub.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = \"--check\" ] && exit 1; "
                        "done\nexit 7\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        r = subprocess.run(["/bin/sh", "-lc", self._script(docs)], capture_output=True, text=True,
                           env=env)
        assert r.returncode == 1, (
            "exit code must be --check's (1), not the summary's (%d)" % r.returncode)


class TestEveryBundleKeyIsMounted:
    """⚠️THE SEAM THAT BREAKS ONLY IN THE CLUSTER. test_configmap_sync.py proves the ConfigMap
    matches the checkout and gen_configmap.check() proves the import closure is complete; neither
    looks at the pod. A file in TAG_CODE without a volumeMount is a green generation, a green sync
    test and a ModuleNotFoundError every half hour."""

    def test_every_bundle_key_is_mounted_by_every_container(self, manifest):
        keys = {k for k, _rel in GC.BUNDLES["hear-tag-code"][1]}
        mounts = _mounts(manifest)
        for container in ("tag", "check"):
            assert mounts.get(container) == keys, (
                "container %r mounts %r but bundle hear-tag-code ships %r"
                % (container, sorted(mounts.get(container) or []), sorted(keys)))

    def test_the_mount_path_matches_the_bundle_path(self, manifest):
        by_key = dict(GC.BUNDLES["hear-tag-code"][1])
        seen = 0
        for line in manifest.splitlines():
            m = re.search(r"mountPath: (\S+?),\s*subPath: (\S+?)\s*\}", line)
            if not m:
                continue
            path, key = m.groups()
            assert key in by_key, "%r is mounted but is in no bundle" % key
            assert path == "/app/" + by_key[key]
            seen += 1
        assert seen == 2 * len(by_key)

    def test_the_bundle_carries_no_weights(self):
        """16,096,668 B against a 1 MiB ConfigMap limit. The digest ships instead, in the code
        that enforces it."""
        for _key, rel in GC.BUNDLES["hear-tag-code"][1]:
            assert not rel.endswith((".onnx", ".tflite", ".csv")), rel
        assert GC.BUNDLES["hear-tag-code"][2] == []

    def test_the_bundle_does_not_drag_in_the_pool(self):
        """hear/tags.py takes a Pool as an argument rather than importing one -- gen_configmap's
        audit is an ast.walk and would find a function-local import just as well."""
        rels = {rel for _k, rel in GC.BUNDLES["hear-tag-code"][1]}
        assert "hear/pool.py" not in rels
        # ⚠️THE AST, NOT A grep. tags.py's own docstring explains that it must not import
        # hear.pool, so a text scan would match its explanation and pass on a file that did.
        import ast
        tree = ast.parse(open(os.path.join(ROOT, "hear", "tags.py")).read())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                assert not (n.module or "").startswith("hear"), ast.dump(n)
            if isinstance(n, ast.Import):
                assert not any(a.name.startswith("hear") for a in n.names), ast.dump(n)

    def test_the_closure_check_passes_for_this_bundle(self):
        app, code, data = GC.BUNDLES["hear-tag-code"]
        GC.check(code, data)          # SystemExit on an incomplete closure


# ----------------------------------------------------------------- the real model, if present

_MODEL_DIR = os.environ.get("HEAR_TAG_MODEL_DIR", "")


def _have_real_model():
    if not _MODEL_DIR:
        return False
    try:
        import onnxruntime                                      # noqa: F401
    except Exception:
        return False
    return HT.verify_weights(_MODEL_DIR)["ok"]


@pytest.mark.skipif(not _have_real_model(),
                    reason="set HEAR_TAG_MODEL_DIR to a directory holding the pinned "
                           "mn10_as.onnx + audioset_class_labels_indices.csv, with "
                           "onnxruntime installed")
class TestAgainstTheRealModel:
    """⚠️OPT-IN. The weights are 16 MB and are not in this checkout; the assertions above are
    about the pipeline, not about YAMNet. What this class proves is the one thing a stub cannot:
    that the interpreter's three same-named outputs are identified correctly."""

    def test_the_three_outputs_are_found_by_width(self):
        mp, cp = HT.weights_paths(_MODEL_DIR)
        t = HT.Tagger(mp, cp)
        got = t.tag(np.zeros(CLIP_SAMPLES, dtype=np.float32))
        assert got["n_classes_scored"] == HT.MODEL_CLASSES
        assert got["n_passes"] == HT.MODEL_PASSES
        assert len(got["embedding"]) == HT.MODEL_EMBED_DIM

    def test_the_model_is_level_sensitive_which_is_why_normalise_exists(self):
        """⚠️THE CLAIM ASSERTED HERE IS ONLY WHAT THIS TEST CAN REPRODUCE. The Silence flip was
        measured on REAL clips -- nyquist-db21acd5-1082421378 at -56.9 dBFS gives
        `Silence 0.406 | Speech 0.183 | Animal 0.147` raw and `Animal 0.307 | Cricket 0.204 |
        Speech 0.198` normalised -- and those clips are not in this checkout, so it cannot be
        asserted here. Synthetic white noise does NOT flip to Silence (measured: `White noise
        0.325 | Snake 0.318` at -57 dBFS), so asserting that it does would be a false claim that
        happened to be checkable. What IS reproducible without the corpus is the property the
        whole normalisation step rests on: the same waveform at two levels gets materially
        different scores from a model that neither states nor corrects for level."""
        mp, cp = HT.weights_paths(_MODEL_DIR)
        t = HT.Tagger(mp, cp)
        x = quiet_noise(-57.0).astype(np.float32)
        quiet = t.tag(x, floor=0.0)["scores"]
        y, pre = HT.normalise(x)
        assert pre == pytest.approx(-57.0, abs=0.3)
        assert HT.dbfs(y) == pytest.approx(HT.TARGET_DBFS, abs=0.3)
        loud = t.tag(y, floor=0.0)["scores"]
        shared = set(quiet) & set(loud)
        assert shared, "the two score dicts share no class at all; the comparison is meaningless"
        assert max(abs(quiet[k] - loud[k]) for k in shared) > 0.05, (
            "the model gave the same answer at -57 and -20 dBFS, so normalise() would be "
            "pointless -- and the measured Silence-for-every-clip failure could not happen")


class TestAModelThatScoredNothingIsNotAQuietNight:
    """⚠️Pre-change `scored_any` was tallied and then dropped: not in the run report's heartbeat
    entry, not read by `check_tags`. A tagger returning `{}` for every clip reported
    silence_frac 0.000 -- the BEST possible value -- and passed the Phase-3 gate more easily than
    any real night can. `max_unstored_score` was quantified per row and never aggregated."""

    class Mute:
        """An interpreter that loaded and is fed or read wrong; or --score-floor past the top."""

        def tag(self, pcm, floor=HT.SCORE_FLOOR):
            return {"scores": {}, "max_unstored_score": 0.0,
                    "n_classes_scored": HT.MODEL_CLASSES, "n_passes": HT.MODEL_PASSES,
                    "embedding_dim": HT.MODEL_EMBED_DIM,
                    "embedding": [0.0] * HT.MODEL_EMBED_DIM}

    def _report(self, tmp_path, tagger, n=3):
        for i in range(n):
            store_clip(tmp_path, sample=1082421378 + i)
        return run(tmp_path, tagger, now=1789000000.0)

    def test_the_run_that_scored_nothing_fails_the_gate(self, tmp_path):
        t = self._report(tmp_path, self.Mute())
        assert t["tagged"] == 3 and t["scored_any"] == 0 and t["silence_frac"] == 0.0
        code, lines = HT.check_tags(str(tmp_path), now=1789000010.0)
        text = "\n".join(lines)
        assert code == 1, text
        assert "scores   NONE" in text, text
        # ⚠️And the silence line still reports the best possible value on the same run -- which
        # is exactly why the absolute gate has to exist and be read first.
        assert "0.000" in text, text

    def test_a_run_that_scored_passes_and_says_how_many(self, tmp_path):
        t = self._report(tmp_path, StubTagger())
        assert t["scored_any"] == 3
        code, lines = HT.check_tags(str(tmp_path), now=1789000010.0)
        text = "\n".join(lines)
        assert code == 0, text
        assert "scores   ok" in text and "3 of 3" in text, text

    def test_the_discarded_tail_is_aggregated_not_only_per_row(self, tmp_path):
        t = self._report(tmp_path, StubTagger())
        d = t["observation_not_health"]["max_unstored_score"]
        assert d["n"] == 3, d
        assert d["median"] > 0.0, d


class TestOneVersionStringMeansOneWeightsFile:
    """⚠️`model_block` says the sha256 IS the identity, but `tag_key` keyed on MODEL_VERSION
    alone: a re-exported mn10_as.onnx under an unchanged version string was treated as
    already-tagged for every clip already scored, and nothing anywhere compared digests."""

    def test_new_weights_under_the_same_version_still_re_tag(self, tmp_path, monkeypatch):
        store_clip(tmp_path)
        assert run(tmp_path, StubTagger())["tagged"] == 1
        assert run(tmp_path, StubTagger())["already_tagged"] == 1
        other = dict(VERIFIED, model_sha256="ff" * 32)
        t = HT.run(str(tmp_path), model_dir="/nonexistent", tagger=StubTagger(), verified=other)
        assert t["tagged"] == 1, (
            "a different weights file under the same version string was treated as done")

    def test_two_digests_under_one_version_fail_the_gate(self, tmp_path):
        store_clip(tmp_path)
        run(tmp_path, StubTagger(), now=1789000000.0)
        other = dict(VERIFIED, model_sha256="ff" * 32)
        HT.run(str(tmp_path), model_dir="/nonexistent", tagger=StubTagger(), verified=other,
               now=1789000100.0)
        code, lines = HT.check_tags(str(tmp_path), now=1789000110.0)
        text = "\n".join(lines)
        assert code == 1, text
        assert "weights  MIXED" in text, text


# ----------------------------------------------------------------- the model swap

class TestTheClipReachesTheModelAtTheModelsRate:
    """mn10_as is 32 kHz and the fleet is not. What crosses, what is refused, what is labelled."""

    def test_a_48k_clip_is_tagged_rather_than_refused_for_its_size(self, tmp_path):
        """⚠️REGRESSION. tag_one tested `len(pcm) * 2 + 44 != CLIP_BYTES_16K_4S` and so refused
        every clip the 48 kHz firmware writes -- the identical magic number, in the identical
        shape, that hear/clips.py had already been fixed for. A constant copied between modules
        keeps its number and loses its meaning."""
        row = store_clip(tmp_path, pcm=quiet_noise(n=240000), fs=48000, fs_csv=48000.0)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"], got
        assert got["row"]["fs_source_nominal_hz"] == 48000.0
        assert got["row"]["fs_model_hz"] == HT.MODEL_FS_HZ

    def test_the_16k_clip_says_which_band_is_real(self, tmp_path):
        """The model's Nyquist is 16 kHz; a 16 kHz clip carries measurement to 8. A reader that
        cannot tell the difference will treat the interpolation filter as the night."""
        row = store_clip(tmp_path)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"], got
        assert got["row"]["upsampled"] is True
        assert got["row"]["band_limit_hz"] == 8000.0
        assert got["row"]["resample_L"] == 2 and got["row"]["resample_M"] == 1

    def test_the_48k_clip_is_not_flagged_as_upsampled(self, tmp_path):
        row = store_clip(tmp_path, pcm=quiet_noise(n=240000), fs=48000, fs_csv=48000.0)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["row"]["upsampled"] is False
        assert got["row"]["band_limit_hz"] == 16000.0
        assert (got["row"]["resample_L"], got["row"]["resample_M"]) == (2, 3)

    def test_an_unrecoverable_bad_rate_is_refused_AS_A_RATE_PROBLEM(self, tmp_path):
        """⚠️THE REASON HAS TO STAY RIGHT. Checked after the duration test, a bad-rate clip came
        back `wav_sample_count`: 64000 samples read as 2.83 s and refused for the wrong length.
        True, and useless. The rate is the defect and the length is a symptom of it."""
        row = store_clip(tmp_path, fs=22624, pcm=quiet_noise(n=12345), fs_csv=16000.0)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert not got["ok"]
        assert got["reason"] == HT.R_RATE_REFUSED, got
        assert "22624" in got["detail"]

    def test_a_clip_of_no_known_length_is_refused_by_duration_not_by_bytes(self, tmp_path):
        row = store_clip(tmp_path, pcm=quiet_noise(n=16000))       # 1.0 s at 16 kHz
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert not got["ok"] and got["reason"] == HT.R_WAV_SAMPLES
        assert "1.000 s" in got["detail"] and "4.0/5.0" in got["detail"]

    def test_a_misheaded_48k_clip_is_scored_at_48k_and_says_so(self, tmp_path):
        """240000 samples headed 16000 Hz is the FS_NOMINAL-stamped 48 kHz clip bug. Scored at
        the header's rate it is 15 s of audio an octave and a half low."""
        row = store_clip(tmp_path, pcm=quiet_noise(n=240000), fs=16000, fs_csv=16000.0)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["ok"], got
        assert got["row"]["header_rate_suspect"]["true_fs_hz"] == 48000.0
        assert got["row"]["fs_source_nominal_hz"] == 48000.0
        assert got["row"]["wav_header_fs_hz"] == 16000, "the lying header is KEPT beside the correction"


class TestTheEmbeddingWidthCannotBeConfused:

    def test_the_width_is_960_and_travels_on_every_row(self, tmp_path):
        """⚠️hear_bridge.py has four consumers that hard-code len(e) == 1024; three drop a
        mismatch SILENTLY and audio_anomaly_score.py returns 0.0 / "not anomalous", which its own
        docstring calls worse than dropping. YAMNet's 1024 and BirdNET's 1024 are mutually
        confusable there. 960 is not either of them, and the row says which it is."""
        row = store_clip(tmp_path)
        got = HT.tag_one(StubTagger(), row, str(tmp_path), HT.model_block(VERIFIED))
        assert got["row"]["embedding_dim"] == 960 == HT.MODEL_EMBED_DIM
        assert len(got["row"]["embedding"]) == 960
        assert HT.MODEL_EMBED_DIM != 1024, "the whole point of recording the width"

    def test_the_model_block_carries_the_width_and_the_rate(self, tmp_path):
        mb = HT.model_block(VERIFIED)
        assert mb["embed_dim"] == 960 and mb["n_classes"] == 527
        assert mb["input_fs_hz"] == 32000 and mb["runtime"] == "onnxruntime"


class TestTheModelIdentityIsTheWholeChain:

    def test_the_card_names_the_upstream_checkpoint_and_the_export_script(self):
        card = HT.model_card(VERIFIED)
        assert card["upstream_sha256"] == HT.UPSTREAM_SHA256
        assert card["export_script"] == "tools/export_mn10_onnx.py"
        assert card["upstream_licence"] == "MIT"

    def test_the_card_states_the_measured_upsampling_penalty_rather_than_a_hope(self):
        card = HT.model_card(VERIFIED)
        t = card["upsampling_penalty_measured"]
        assert "cosine" in t and "0.90" in t
        assert "Perch" in t, "the measurement is for THIS model and must not be read as Perch's"

    def test_the_export_script_exists_and_pins_what_hear_tag_pins(self):
        import pathlib
        p = pathlib.Path(__file__).resolve().parents[1] / "tools" / "export_mn10_onnx.py"
        src = p.read_text()
        assert HT.UPSTREAM_SHA256 in src, "the export names the checkpoint it converts"
        assert HT.MODEL_SHA256 in src, "and the digest of what it produces"
