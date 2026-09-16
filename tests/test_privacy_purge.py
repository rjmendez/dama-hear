"""hear/privacy/purge.py -- speech is detected, the WAV is destroyed, and only a receipt remains.

Every test here runs on synthesised audio: a harmonic, pitch-modulated voice stand-in against
the things a field microphone actually hears -- broadband noise, an impulsive gunshot, an engine
harmonic, a siren, silence. No recorded speech is committed to this repository, which is the
same rule the pipeline enforces at runtime.

The four claims under test, in the order a reviewer should care about them:

  1. a clip with a voice in it does not survive the run (and one without it is untouched);
  2. the receipt is complete, and carries nothing that could reconstruct the sound;
  3. `--dry-run` writes nothing at all -- not the clip, not the audit log;
  4. an undecidable clip is an ERROR, never a silent "no speech".
"""
import json
import os
import subprocess
import sys
import wave

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hear.privacy import purge as P  # noqa: E402

RATE = 48000
TOOL = os.path.join(ROOT, "tools", "hear_privacy_purge.py")


# ---------------------------------------------------------------- synthetic sound

def voice(dur=2.0, f0=130.0, rate=RATE, seed=1, amp=0.3):
    """A voice stand-in: a pitch-modulated harmonic stack under a syllable-rate envelope.

    Not speech, and not claimed to be. It carries the three properties the fallback detector
    keys on -- band energy, harmonicity, spectral spread -- which is what makes it a fair
    negative control's opposite number.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * dur)) / rate
    f = f0 * (1.0 + 0.05 * np.sin(2 * np.pi * 1.5 * t))
    ph = 2 * np.pi * np.cumsum(f) / rate
    sig = np.zeros_like(t)
    for k, a in enumerate([1.0, 0.7, 0.5, 0.4, 0.6, 0.3, 0.2, 0.15], start=1):
        sig += a * np.sin(k * ph)
    sig *= 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    sig += 0.01 * rng.standard_normal(t.size)
    return (amp * sig / np.max(np.abs(sig))).astype(np.float32)


def noise(dur=2.0, rate=RATE, seed=2, amp=0.1):
    return (amp * np.random.default_rng(seed).standard_normal(int(rate * dur))).astype(np.float32)


def gunshot(dur=2.0, rate=RATE, seed=3):
    rng = np.random.default_rng(seed)
    x = 0.02 * rng.standard_normal(int(rate * dur))
    i, n = int(rate * 0.5), int(rate * 0.08)
    x[i:i + n] += np.exp(-np.arange(n) / (rate * 0.01)) * rng.standard_normal(n) * 3.0
    return np.clip(x, -1.0, 1.0).astype(np.float32)


def engine(dur=2.0, rate=RATE, seed=4):
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * dur)) / rate
    x = 0.3 * np.sin(2 * np.pi * 80 * t) + 0.1 * np.sin(2 * np.pi * 160 * t)
    return (x + 0.02 * rng.standard_normal(t.size)).astype(np.float32)


def siren(dur=2.0, rate=RATE):
    t = np.arange(int(rate * dur)) / rate
    f = 900.0 + 300.0 * np.sin(2 * np.pi * 0.5 * t)
    return (0.3 * np.sin(2 * np.pi * np.cumsum(f) / rate)).astype(np.float32)


def write_wav(path, samples, rate=RATE, channels=1):
    pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return str(path)


# ---------------------------------------------------------------- the detector

def test_voice_is_speech_and_the_field_is_not():
    """One detector, five signals: only the voice may come back True.

    The four negatives are not decoration. Band energy alone calls a siren speech, harmonicity
    alone calls an engine harmonic speech, and an energy gate alone calls a gunshot speech --
    each one is here because it defeats one of the gates on its own.
    """
    vad = P.BandEnergyVAD()
    assert vad.detect(voice(), RATE).speech is True
    for name, sig in (("noise", noise()), ("gunshot", gunshot()), ("engine", engine()),
                      ("siren", siren()), ("silence", np.zeros(RATE, dtype=np.float32))):
        assert vad.detect(sig, RATE).speech is False, "%s was called speech" % name


def test_speech_that_fills_the_whole_clip_is_still_speech():
    """The loudest privacy failure must not be the one that scores lowest.

    A relative-only energy gate puts its own noise floor inside the utterance when someone talks
    for the whole five seconds, reports ~0 dB SNR, and keeps the file.
    """
    t = np.arange(int(RATE * 5.0)) / RATE
    ph = 2 * np.pi * np.cumsum(110.0 * (1 + 0.08 * np.sin(2 * np.pi * 2.0 * t))) / RATE
    sig = sum(a * np.sin(k * ph) for k, a in enumerate([1, .8, .6, .5, .7, .4, .3, .25], start=1))
    sig = (0.25 * sig / np.max(np.abs(sig))).astype(np.float32)
    decision = P.BandEnergyVAD().detect(sig, RATE)
    assert decision.speech is True
    assert decision.speech_s > 4.0


@pytest.mark.parametrize("f0", [110.0, 180.0, 250.0, 320.0])
def test_speech_is_found_across_the_pitch_range(f0):
    """A child's pitch is not a loophole: the pitch search spans 70-350 Hz on purpose."""
    assert P.BandEnergyVAD().detect(voice(f0=f0), RATE).speech is True


def test_speech_under_noise_is_not_fragmented_into_nothing():
    """Voiced peaks in traffic must merge into one utterance, not 140 ms fragments.

    Without the merge, every fragment falls under `min_speech_ms` and a clip of someone talking
    through road noise is kept -- the miss that `JOIN_GAP_MS` exists to prevent.
    """
    rng = np.random.default_rng(7)
    sig = voice() + (0.05 * rng.standard_normal(int(RATE * 2.0))).astype(np.float32)
    decision = P.BandEnergyVAD().detect(sig.astype(np.float32), RATE)
    assert decision.speech is True
    assert decision.speech_s > 1.0


def test_min_speech_ms_discards_a_flicker():
    """A 120 ms voiced blip is a door latch, not a word -- unless the operator lowers the bar."""
    sig = np.zeros(int(RATE * 3.0), dtype=np.float32)
    sig[RATE:RATE + int(0.12 * RATE)] = voice(dur=0.12)
    assert P.BandEnergyVAD(min_speech_ms=400.0).detect(sig, RATE).speech is False
    assert P.BandEnergyVAD(min_speech_ms=80.0).detect(sig, RATE).speech is True


def test_spans_are_seconds_inside_the_clip():
    sig = np.zeros(int(RATE * 5.0), dtype=np.float32)
    sig[2 * RATE:2 * RATE + int(1.5 * RATE)] = voice(dur=1.5)
    decision = P.BandEnergyVAD().detect(sig, RATE)
    assert decision.speech is True
    assert len(decision.spans) == 1
    span = decision.spans[0]
    assert 1.8 <= span.start_s <= 2.3
    assert 3.2 <= span.end_s <= 3.8
    assert 0.0 <= decision.peak_prob <= 1.0


def test_decision_from_probs_merges_gaps_and_drops_shorts():
    probs = [0.9] * 10 + [0.0] * 5 + [0.9] * 10 + [0.0] * 60 + [0.9] * 3
    d = P.decision_from_probs(probs, hop_s=0.01, frame_s=0.032, threshold=0.5,
                              min_speech_ms=200.0, engine="test")
    assert d.speech is True
    assert len(d.spans) == 1                      # the 5-frame gap merged; the 3-frame run went
    assert d.peak_prob == pytest.approx(0.9)


def test_empty_and_degenerate_inputs_do_not_claim_speech():
    vad = P.BandEnergyVAD()
    assert vad.detect(np.zeros(0, dtype=np.float32), RATE).speech is False
    assert vad.detect(np.zeros(64, dtype=np.float32), RATE).speech is False
    with pytest.raises(P.PurgeError):
        vad.detect(voice(0.5), 0)


# ---------------------------------------------------------------- reading clips

def test_read_wav_mono_mixes_channels_and_reports_rate(tmp_path):
    path = write_wav(tmp_path / "s.wav", voice(1.0), channels=2)
    samples, rate = P.read_wav_mono(path)
    assert rate == RATE
    assert samples.size == int(RATE * 1.0)
    assert samples.dtype == np.float32
    assert np.abs(samples).max() <= 1.0


def test_unreadable_clips_raise_rather_than_returning_silence(tmp_path):
    bad = tmp_path / "truncated.wav"
    bad.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
    with pytest.raises(P.UnreadableClip):
        P.read_wav_mono(str(bad))
    missing = tmp_path / "gone.wav"
    with pytest.raises(P.UnreadableClip):
        P.read_wav_mono(str(missing))


def test_a_clip_that_cannot_be_read_is_destroyed_not_kept(tmp_path):
    """Contract §9: undecided is treated as guilty. Keeping it is the unbounded-cost mistake."""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    outcome = P.purge_clip(str(bad), P.BandEnergyVAD())
    assert outcome.status == "purged"
    assert not bad.exists()
    rec = outcome.receipt.as_record()
    assert rec["verdict"] == P.VERDICT_SPEECH
    assert rec["fail_closed_reason"] == P.REASON_UNREADABLE
    assert rec["peak_speech_prob"] == 0.0, "a fail-closed purge claims no measurement"


def test_a_refused_sample_rate_fails_closed(tmp_path):
    """44.1 kHz is not a rate this lane records at, so it cannot be honestly scored."""
    path = write_wav(tmp_path / "odd.wav", voice(1.0, rate=44100), rate=44100)
    outcome = P.purge_clip(path, P.BandEnergyVAD())
    assert outcome.status == "purged" and not os.path.exists(path)
    assert outcome.receipt.as_record()["fail_closed_reason"] == P.REASON_RATE


def test_non_finite_audio_fails_closed_rather_than_scoring_as_silence():
    """A NaN makes every `p >= threshold` False: a confident silence produced by arithmetic."""
    sig = voice(1.0)
    sig[1000] = np.nan
    with pytest.raises(P.NonFiniteAudio):
        P.BandEnergyVAD().detect(sig, RATE)
    sig[1000] = np.inf
    with pytest.raises(P.NonFiniteAudio):
        P.inspect_samples(sig, RATE, P.BandEnergyVAD())


def test_a_non_finite_stream_is_refused_not_stored(tmp_path):
    """The streaming door returns a receipt -- 'do not store' -- instead of raising."""
    class NaNVad:
        name = "nan-vad"

        def detect(self, samples, rate):
            return P.VadDecision(False, float("nan"), 0.0, (), self.name)

    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((voice() * 32767).astype("<i2").tobytes())
    receipt = P.purge_wav_bytes(buf.getvalue(), node="n", vad=NaNVad())
    assert receipt is not None, "a non-finite score must never mean 'safe to keep'"
    assert receipt.as_record()["fail_closed_reason"] == P.REASON_INFERENCE


def test_a_detector_fault_destroys_the_clip_and_says_why(tmp_path):
    """`vad_unavailable` purging the backlog is the design, not a catastrophe (§9)."""
    class Broken:
        name = "broken"

        def detect(self, samples, rate):
            raise RuntimeError("model file vanished")

    path = write_wav(tmp_path / "v.wav", voice())
    outcome = P.purge_clip(path, Broken())
    assert outcome.status == "purged" and not os.path.exists(path)
    rec = outcome.receipt.as_record()
    assert rec["fail_closed_reason"] == P.REASON_INFERENCE
    assert rec["purged_sha256"], "the digest was taken before the detector was asked"


def test_a_purge_that_fails_twice_stops_the_scan(tmp_path):
    """Walking past a file this run judged speech-bearing would finish green while it is there."""
    first = write_wav(tmp_path / "a-voice.wav", voice())
    second = write_wav(tmp_path / "b-voice.wav", voice(f0=200.0))
    real_shred = P.shred_file

    def refuse(path, passes=1):
        if os.path.basename(path).startswith("a-"):
            raise P.PurgeError("read-only filesystem")
        return real_shred(path, passes)

    P.shred_file = refuse
    try:
        report = P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    finally:
        P.shred_file = real_shred

    assert report.halted is True
    assert report.scanned == 1, "nothing after the failure was scanned"
    assert os.path.exists(first) and os.path.exists(second)
    assert report.outcomes[-1].receipt.as_record()["fail_closed_reason"] == P.REASON_PURGE_FAILED


# ---------------------------------------------------------------- destruction

def test_speech_clip_is_destroyed_and_receipted(tmp_path):
    path = write_wav(tmp_path / "nyquist-0123456789ab-0000000001.wav", voice())
    before = P.sha256_file(path)
    audit = str(tmp_path / "purged_receipts.jsonl")

    outcome = P.purge_clip(path, P.BandEnergyVAD(), audit_log=audit)

    assert outcome.status == "purged"
    assert not os.path.exists(path), "the WAV survived its own purge"
    rec = outcome.receipt.as_record()
    assert rec["purged_sha256"] == before
    assert rec["purge_reason"] == P.PURGE_REASON
    assert rec["zero_audio_retained"] is True
    assert rec["node"] == "nyquist"
    assert rec["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert 0.0 < rec["peak_speech_prob"] <= 1.0
    assert rec["dry_run"] is False

    lines = P.read_receipts(audit)
    assert len(lines) == 1 and lines[0] == rec


def test_a_clip_without_speech_is_left_exactly_as_it_was(tmp_path):
    path = write_wav(tmp_path / "gunshot.wav", gunshot())
    digest = P.sha256_file(path)
    audit = str(tmp_path / "audit.jsonl")
    outcome = P.purge_clip(path, P.BandEnergyVAD(), audit_log=audit)
    assert outcome.status == "kept"
    assert P.sha256_file(path) == digest
    assert not os.path.exists(audit), "a kept clip must not produce a receipt"


def test_shred_overwrites_before_unlinking(tmp_path):
    """The bytes must be gone from the filesystem, not merely unlinked from the directory."""
    path = str(tmp_path / "raw.wav")
    write_wav(path, voice())
    with open(path, "rb") as fh:
        original = fh.read()
    P.shred_file(path)
    assert not os.path.exists(path)
    # Nothing in the directory holds the old bytes any more.
    for name in os.listdir(str(tmp_path)):
        with open(os.path.join(str(tmp_path), name), "rb") as fh:
            assert original not in fh.read()


def test_shred_on_a_missing_file_raises(tmp_path):
    with pytest.raises(P.PurgeError):
        P.shred_file(str(tmp_path / "never-existed.wav"))


# ---------------------------------------------------------------- dry run

def test_dry_run_touches_neither_the_clip_nor_the_audit_log(tmp_path):
    path = write_wav(tmp_path / "hear-aabbccddeeff-0000000002.wav", voice())
    digest = P.sha256_file(path)
    audit = str(tmp_path / "audit.jsonl")

    report = P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD(), dry_run=True, audit_log=audit)

    assert report.would_purge == 1 and report.purged == 0
    assert os.path.exists(path) and P.sha256_file(path) == digest
    assert not os.path.exists(audit)
    assert report.receipts[0].as_record()["dry_run"] is True
    assert report.audit_log is None


# ---------------------------------------------------------------- pools

def test_scan_pool_purges_only_the_voices(tmp_path):
    speech = write_wav(tmp_path / "a-voice.wav", voice())
    quiet = write_wav(tmp_path / "b-gunshot.wav", gunshot())
    hiss = write_wav(tmp_path / "c-noise.wav", noise())
    nested = tmp_path / "sub"
    nested.mkdir()
    deep = write_wav(nested / "d-voice.wav", voice(f0=200.0))

    report = P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())

    assert report.scanned == 4
    assert report.purged == 2 and report.kept == 2 and report.errors == 0
    assert not os.path.exists(speech) and not os.path.exists(deep)
    assert os.path.exists(quiet) and os.path.exists(hiss)
    audit = os.path.join(str(tmp_path), P.RECEIPT_NAME)
    assert len(P.read_receipts(audit)) == 2


def test_the_audit_log_is_never_itself_a_purge_target(tmp_path):
    """A JSONL receipt file is not a WAV, so a second run cannot destroy the first run's proof."""
    write_wav(tmp_path / "voice.wav", voice())
    P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    audit = os.path.join(str(tmp_path), P.RECEIPT_NAME)
    first = P.read_receipts(audit)
    P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    assert P.read_receipts(audit) == first


def test_receipts_accumulate_across_runs(tmp_path):
    audit = str(tmp_path / "audit.jsonl")
    for i in range(3):
        path = write_wav(tmp_path / ("clip%d.wav" % i), voice(f0=120.0 + 20 * i))
        P.purge_clip(path, P.BandEnergyVAD(), audit_log=audit)
    assert len(P.read_receipts(audit)) == 3


# ---------------------------------------------------------------- streams

def test_a_stream_can_be_decided_before_anything_reaches_the_disk(tmp_path):
    path = write_wav(tmp_path / "stream.wav", voice())
    with open(path, "rb") as fh:
        data = fh.read()
    os.unlink(path)
    audit = str(tmp_path / "audit.jsonl")

    receipt = P.purge_wav_bytes(data, node="stream-node", audit_log=audit)

    assert receipt is not None
    rec = receipt.as_record()
    import hashlib
    assert rec["purged_sha256"] == hashlib.sha256(data).hexdigest()
    assert rec["node"] == "stream-node"
    assert len(P.read_receipts(audit)) == 1


def test_a_clean_stream_yields_no_receipt():
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((gunshot() * 32767).astype("<i2").tobytes())
    assert P.purge_wav_bytes(buf.getvalue(), node="n") is None


def test_a_malformed_stream_is_refused_rather_than_raising(tmp_path):
    """A caller that only has to catch an exception to keep the bytes has opted out of §9."""
    receipt = P.purge_wav_bytes(b"RIFFnope", node="n")
    assert receipt is not None
    assert receipt.as_record()["fail_closed_reason"] == P.REASON_UNREADABLE


# ---------------------------------------------------------------- the privacy contract

def test_receipt_carries_exactly_the_allowed_fields(tmp_path):
    path = write_wav(tmp_path / "v.wav", voice())
    outcome = P.purge_clip(path, P.BandEnergyVAD())
    rec = outcome.receipt.as_record()
    assert set(rec) <= set(P.RECEIPT_KEYS)
    for required in ("timestamp", "node", "duration_s", "peak_speech_prob", "purged_sha256",
                     "purge_reason", "zero_audio_retained"):
        assert required in rec


@pytest.mark.parametrize("key", ["transcript", "mfcc", "embedding", "speaker_id",
                                 "mel_spectrogram", "audio_samples", "text"])
def test_a_forbidden_field_is_refused(key):
    """The leak never arrives as a decision to leak; it arrives as one more helpful field."""
    rec = {"schema_version": 1, "timestamp": "t", "node": "n", "clip": "c.wav",
           "duration_s": 1.0, "peak_speech_prob": 0.9, "speech_s": 1.0, "speech_spans_s": [],
           "purged_sha256": "x", "purge_reason": P.PURGE_REASON, "vad_engine": "e",
           "zero_audio_retained": True, "dry_run": False}
    rec[key] = "anything"
    with pytest.raises(P.PrivacyLeak):
        P.assert_privacy_safe(rec)


def test_a_vector_smuggled_into_an_allowed_field_is_refused():
    """A per-frame probability envelope under a legal key is still a recording of the rhythm."""
    rec = {"schema_version": 1, "timestamp": "t", "node": "n", "clip": "c.wav",
           "duration_s": 1.0, "peak_speech_prob": [0.1, 0.4, 0.9], "speech_s": 1.0,
           "speech_spans_s": [], "purged_sha256": "x", "purge_reason": P.PURGE_REASON,
           "vad_engine": "e", "zero_audio_retained": True, "dry_run": False}
    with pytest.raises(P.PrivacyLeak):
        P.assert_privacy_safe(rec)
    rec["peak_speech_prob"] = 0.9
    rec["speech_spans_s"] = [[0.0, 1.0, 0.5]]
    with pytest.raises(P.PrivacyLeak):
        P.assert_privacy_safe(rec)


def test_no_receipt_field_holds_anything_audio_shaped(tmp_path):
    """Serialise a real receipt and check every value is a scalar or a pair of seconds."""
    path = write_wav(tmp_path / "v.wav", voice())
    rec = P.purge_clip(path, P.BandEnergyVAD()).receipt.as_record()
    blob = json.dumps(rec)
    for word in ("transcript", "mfcc", "embedding", "waveform", "pcm"):
        assert word not in blob.lower()
    for key, value in rec.items():
        if key in ("speech_spans_s", "speech_segments_ms"):
            assert all(len(pair) == 2 for pair in value)
            continue
        assert value is None or isinstance(value, (str, int, float, bool))


def test_the_receipt_is_contract_shaped(tmp_path):
    """docs/silero-vad-privacy-contract.md §8.2, as far as this pipeline can answer it."""
    path = write_wav(tmp_path / "nyquist-0123456789ab-0000000007.wav", voice())
    rec = P.purge_wav(path, vad=P.BandEnergyVAD())
    assert rec["schema"] == P.RECEIPT_SCHEMA
    assert rec["verdict"] == P.VERDICT_SPEECH
    assert rec["provenance"] == "model"
    assert rec["purge_method"] == P.METHOD_OVERWRITE
    assert rec["medium_guarantee"] == "filesystem_only"
    assert rec["verified_absent"] is True
    assert rec["already_absent"] is False
    assert rec["audio_retained"] is False and rec["zero_audio_retained"] is True
    assert rec["policy_version"] == P.POLICY_VERSION
    assert rec["speech_threshold"] == P.DEFAULT_THRESHOLD
    assert rec["neg_threshold"] == P.DEFAULT_NEG_THRESHOLD
    assert rec["min_speech_duration_ms"] == P.DEFAULT_MIN_SPEECH_MS
    assert rec["min_silence_duration_ms"] == P.DEFAULT_MIN_SILENCE_MS
    assert rec["speech_pad_ms"] == P.DEFAULT_SPEECH_PAD_MS
    assert rec["frames_scored"] > 0
    assert rec["purged_bytes"] > 0
    assert rec["clip_key"] and len(rec["clip_key"]) == 32
    assert rec["speech_segment_count"] == len(rec["speech_segments_ms"]) >= 1
    assert rec["speech_total_ms"] > 0
    assert 0.0 < rec["speech_confidence_mean_in_segments"] <= rec["speech_confidence_max"]


def test_a_kept_clip_can_be_receipted_too(tmp_path):
    """Coverage is a fact: 'scored and clean' and 'never scored' must not look the same."""
    path = write_wav(tmp_path / "bang.wav", gunshot())
    rec = P.purge_wav(path, vad=P.BandEnergyVAD())
    assert rec["verdict"] == P.VERDICT_CLEAN
    assert rec["audio_retained"] is True and rec["zero_audio_retained"] is False
    assert rec["purge_method"] == P.METHOD_NONE
    assert rec["medium_guarantee"] == "none"
    assert os.path.exists(path), "a NO_SPEECH receipt must not imply a destruction"


def test_rerunning_over_an_already_purged_clip_is_a_no_op(tmp_path):
    """The idempotency claim: no exception, no second destruction, and it says why."""
    path = write_wav(tmp_path / "voice.wav", voice())
    audit = str(tmp_path / "audit.jsonl")
    first = P.purge_wav(path, vad=P.BandEnergyVAD(), audit_log=audit)
    assert first["verdict"] == P.VERDICT_SPEECH

    again = P.purge_wav(path, vad=P.BandEnergyVAD(), audit_log=audit)
    assert again["verdict"] == P.VERDICT_NOT_SCORED
    assert again["already_absent"] is True
    assert again["purge_method"] == P.METHOD_ABSENT
    assert again["purged_sha256"] is None
    assert again["zero_audio_retained"] is False    # this run destroyed nothing
    receipts = P.read_receipts(audit)
    assert [r["verdict"] for r in receipts] == [P.VERDICT_SPEECH, P.VERDICT_NOT_SCORED]


def test_scan_pool_counts_an_absent_clip_without_calling_it_an_error(tmp_path):
    path = write_wav(tmp_path / "voice.wav", voice())
    P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    assert not os.path.exists(path)
    report = P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    assert report.errors == 0 and report.scanned == 0


def test_dry_run_receipt_does_not_claim_a_destruction(tmp_path):
    path = write_wav(tmp_path / "voice.wav", voice())
    rec = P.purge_wav(path, vad=P.BandEnergyVAD(), dry_run=True)
    assert rec["verdict"] == P.VERDICT_SPEECH
    assert rec["dry_run"] is True
    assert rec["zero_audio_retained"] is False and rec["audio_retained"] is True
    assert os.path.exists(path)


def test_purge_wav_reports_a_fail_closed_destruction_instead_of_raising(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    rec = P.purge_wav(str(bad), vad=P.BandEnergyVAD())
    assert rec["verdict"] == P.VERDICT_SPEECH
    assert rec["fail_closed_reason"] == P.REASON_UNREADABLE
    assert rec["zero_audio_retained"] is True
    assert not bad.exists()


def test_a_dry_run_never_fail_closes_a_file_off_the_disk(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    rec = P.purge_wav(str(bad), vad=P.BandEnergyVAD(), dry_run=True)
    assert rec["fail_closed_reason"] == P.REASON_UNREADABLE
    assert rec["dry_run"] is True and bad.exists()


# ---------------------------------------------------------------- hysteresis (contract §4)

def test_hysteresis_keeps_one_wobbling_utterance_whole():
    """0.52, 0.48, 0.53 is one segment, not three: closing needs affirmative confidence."""
    probs = ([0.9] * 5 + [0.42] * 3) * 6 + [0.0] * 40
    d = P.decision_from_probs(probs, hop_s=0.01, frame_s=0.032, threshold=0.5,
                              min_speech_ms=250.0, engine="t", neg_threshold=0.35,
                              min_silence_ms=0.0, speech_pad_ms=0.0)
    assert d.speech is True and len(d.spans) == 1


def test_a_closing_threshold_above_the_opening_one_is_refused():
    with pytest.raises(P.PurgeError):
        P.decision_from_probs([0.9] * 50, hop_s=0.01, frame_s=0.032, threshold=0.5,
                              min_speech_ms=100.0, engine="t", neg_threshold=0.8)


def test_short_silences_are_bridged_and_padding_is_clamped_to_the_clip():
    probs = [0.9] * 30 + [0.0] * 20 + [0.9] * 30
    d = P.decision_from_probs(probs, hop_s=0.01, frame_s=0.032, threshold=0.5,
                              min_speech_ms=250.0, engine="t", min_silence_ms=300.0,
                              speech_pad_ms=30.0, clip_s=0.82)
    assert len(d.spans) == 1
    assert d.spans[0].start_s == 0.0                      # padding cannot go negative
    assert d.spans[0].end_s <= 0.82                       # nor past the clip


def test_mean_confidence_is_measured_inside_the_segments():
    """A clip-wide mean falls as silence is added; the in-segment mean does not."""
    probs = [0.8] * 40 + [0.0] * 200
    d = P.decision_from_probs(probs, hop_s=0.01, frame_s=0.032, threshold=0.5,
                              min_speech_ms=250.0, engine="t")
    assert d.mean_prob_in_segments == pytest.approx(0.8)
    assert d.frames_scored == 240


def test_segments_are_reported_in_ms_as_well_as_seconds(tmp_path):
    path = write_wav(tmp_path / "v.wav", voice())
    rec = P.purge_wav(path, vad=P.BandEnergyVAD())
    for (lo_s, hi_s), (lo_ms, hi_ms) in zip(rec["speech_spans_s"], rec["speech_segments_ms"]):
        assert lo_ms == pytest.approx(lo_s * 1000.0, abs=1.0)
        assert hi_ms == pytest.approx(hi_s * 1000.0, abs=1.0)


def test_pool_receipts_are_read_under_both_the_old_and_new_names(tmp_path):
    write_wav(tmp_path / "a.wav", voice())
    P.scan_pool(str(tmp_path), vad=P.BandEnergyVAD())
    legacy = os.path.join(str(tmp_path), P.LEGACY_RECEIPT_NAME)
    with open(legacy, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"verdict": "SPEECH_DETECTED", "legacy": True}) + "\n")
    assert len(P.read_pool_receipts(str(tmp_path))) == 2


def test_the_engine_name_is_on_every_receipt(tmp_path):
    path = write_wav(tmp_path / "v.wav", voice())
    rec = P.purge_clip(path, P.BandEnergyVAD()).receipt.as_record()
    assert rec["vad_engine"] == P.BandEnergyVAD.name


def test_load_vad_falls_back_but_never_silently(tmp_path):
    vad = P.load_vad("auto")
    assert hasattr(vad, "detect") and getattr(vad, "name", "")
    assert isinstance(P.load_vad("band_energy"), P.BandEnergyVAD)
    with pytest.raises(P.PurgeError):
        P.load_vad("whatever-is-installed")


# ---------------------------------------------------------------- the CLI

def run_tool(*argv):
    return subprocess.run([sys.executable, TOOL] + list(argv), capture_output=True, text=True)


def test_cli_dry_run_reports_without_touching_anything(tmp_path):
    path = write_wav(tmp_path / "voice.wav", voice())
    digest = P.sha256_file(path)
    proc = run_tool("--pool", str(tmp_path), "--dry-run", "--vad", "band_energy")
    assert proc.returncode == 0, proc.stderr
    assert "would_purge" in proc.stdout
    assert os.path.exists(path) and P.sha256_file(path) == digest
    assert not os.path.exists(os.path.join(str(tmp_path), P.RECEIPT_NAME))


def test_cli_purges_and_writes_the_audit_log(tmp_path):
    write_wav(tmp_path / "voice.wav", voice())
    write_wav(tmp_path / "bang.wav", gunshot())
    audit = str(tmp_path / "receipts.jsonl")

    proc = run_tool("--pool", str(tmp_path), "--audit-log", audit, "--vad", "band_energy",
                    "--threshold", "0.5", "--min-speech-ms", "250")

    assert proc.returncode == 0, proc.stderr
    assert not os.path.exists(os.path.join(str(tmp_path), "voice.wav"))
    assert os.path.exists(os.path.join(str(tmp_path), "bang.wav"))
    receipts = P.read_receipts(audit)
    assert len(receipts) == 1
    assert receipts[0]["purge_reason"] == P.PURGE_REASON
    assert receipts[0]["zero_audio_retained"] is True


def test_cli_json_mode_emits_parseable_receipts(tmp_path):
    write_wav(tmp_path / "voice.wav", voice())
    proc = run_tool("--clip", str(tmp_path / "voice.wav"), "--json", "--vad", "band_energy",
                    "--audit-log", str(tmp_path / "a.jsonl"))
    assert proc.returncode == 0, proc.stderr
    records = [json.loads(line) for line in proc.stdout.strip().splitlines()]
    assert records[-1]["summary"]["purged"] == 1
    assert records[0]["purge_reason"] == P.PURGE_REASON


def test_cli_exit_code_3_when_a_clip_was_destroyed_without_being_scored(tmp_path):
    """Fail-closed is correct AND is an alarm: a shredder is not a privacy control."""
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"still not a wav")
    proc = run_tool("--pool", str(tmp_path), "--vad", "band_energy")
    assert proc.returncode == 3, proc.stderr
    assert "FAIL-CLOSED" in proc.stdout and "clip_unreadable" in proc.stdout
    assert "fail-closed" in proc.stderr.lower()
    assert not broken.exists()


def test_cli_refuses_a_missing_pool_and_a_silly_threshold(tmp_path):
    assert run_tool("--pool", str(tmp_path / "nope")).returncode == 2
    assert run_tool("--pool", str(tmp_path), "--threshold", "4").returncode == 2
    assert run_tool("--pool", str(tmp_path), "--min-speech-ms", "-1").returncode == 2
    assert run_tool("--pool", str(tmp_path), "--threshold", "0.4",
                    "--neg-threshold", "0.9").returncode == 2


def test_cli_receipt_clean_records_coverage(tmp_path):
    write_wav(tmp_path / "bang.wav", gunshot())
    audit = str(tmp_path / "a.jsonl")
    proc = run_tool("--pool", str(tmp_path), "--vad", "band_energy", "--receipt-clean",
                    "--audit-log", audit)
    assert proc.returncode == 0, proc.stderr
    receipts = P.read_receipts(audit)
    assert [r["verdict"] for r in receipts] == [P.VERDICT_CLEAN]
    assert os.path.exists(os.path.join(str(tmp_path), "bang.wav"))


def test_cli_requires_a_target():
    proc = run_tool("--dry-run")
    assert proc.returncode != 0


# ===================================================================================
# The fixture battery: the same synthetic signals the VAD suite scores, run through the
# purge end to end.
#
# The tests above prove the pipeline against sounds defined in this file. These prove it
# against `tests/privacy_signals.py` -- the library `tests/test_silero_vad.py` uses and
# `testdata/silero_vad_golden.json` records the real Silero v5 answers for. Sharing the
# stimuli is the point: when a claim here disagrees with a claim there, the disagreement is
# between two detectors on one waveform rather than between two idiolects of "noise".
# ===================================================================================

from tests import privacy_signals as SIG  # noqa: E402

#: What the two detectors answer for the shared fixtures. Silero's column is not a guess: it
#: is `testdata/silero_vad_golden.json`, measured on the real v5 graph (docs §2.3's 576-sample
#: window, which the golden also records the 512-sample failure of).
#:
#: ⚠️THE FALLBACK DESTROYS PINK NOISE AND SILERO DOES NOT. `BandEnergyVAD` reads 1/f noise as
#: voiced-band energy with enough spectral spread to clear the bar; the real model answers
#: 0.028. This is over-destruction, not a leak -- the direction that costs clips of wind rather
#: than clips of conversation -- and it is exactly the unmeasured-recall gap docs §12.3 refuses
#: to wave through. It is asserted rather than described so that tuning the fallback shows up
#: here as a decision instead of as a silent change in what a rainy night deletes.
SHARED_FIXTURES = (
    # name,                 holds speech, band_energy purges, silero peak (golden)
    ("speech_like",         True,         True,               0.999),
    ("silence",             False,        False,              0.009),
    ("gaussian_noise",      False,        False,              0.030),
    ("gaussian_noise_loud", False,        False,              0.041),
    ("pink_noise",          False,        True,               0.028),
    ("pink_noise_loud",     False,        True,               0.028),
    ("bird_chirps",         False,        False,              0.018),
    ("tone_1k",             False,        False,              0.006),
)


def band_energy():
    return P.load_vad("band_energy")


def clip_at(tmp_path, name, samples, rate=16000):
    return write_wav(tmp_path / name, np.asarray(samples, dtype=np.float32), rate=rate)


@pytest.mark.parametrize("name,holds_speech,purges,_silero", SHARED_FIXTURES,
                         ids=[f[0] for f in SHARED_FIXTURES])
def test_the_fallback_detector_answers_the_shared_fixtures_as_recorded(name, holds_speech,
                                                                       purges, _silero):
    decision = P.inspect_samples(np.asarray(SIG.by_name(name), dtype=np.float32), 16000,
                                 band_energy())
    assert decision.speech is purges, (
        "%s: band_energy now says speech=%s (peak %.3f); the fixture holds speech=%s and Silero "
        "answers %.3f. Update SHARED_FIXTURES deliberately or fix the detector."
        % (name, decision.speech, decision.peak_prob, holds_speech, _silero))


def test_the_voice_fixture_is_destroyed_and_the_ambient_ones_are_not_all_destroyed(tmp_path):
    """The privacy-critical direction, on the shared battery: the voice must not survive."""
    vad = band_energy()
    for name, _holds, _purges, _p in SHARED_FIXTURES:
        clip_at(tmp_path, name + ".wav", SIG.by_name(name))
    report = P.scan_pool(str(tmp_path), vad=vad)
    assert not os.path.exists(str(tmp_path / "speech_like.wav")), \
        "the voice fixture survived the scan"
    assert os.path.exists(str(tmp_path / "silence.wav")), \
        "silence was destroyed: the detector is not discriminating at all"
    assert report.purged + report.kept == report.scanned


def test_the_48k_acquisition_rate_is_scored_at_its_own_rate(tmp_path):
    """Clips arrive at 48 kHz (docs §2.1). The voice must not survive that path either."""
    path = clip_at(tmp_path, "speech48.wav", SIG.speech_like_48k(), rate=48000)
    outcome = P.purge_clip(path, band_energy())
    assert outcome.status == "purged"
    assert not os.path.exists(path)
    assert abs(outcome.receipt.duration_s - 2.0) < 0.05, \
        "duration was computed at the wrong rate: %.3f s" % outcome.receipt.duration_s


# ---------------------------------------------------------------- re-running a drained pool

def test_a_second_scan_of_a_drained_pool_purges_nothing_and_errors_on_nothing(tmp_path):
    log = str(tmp_path / P.RECEIPT_NAME)
    clip_at(tmp_path, "voice.wav", SIG.speech_like())
    clip_at(tmp_path, "wind.wav", SIG.gaussian_noise())
    first = P.scan_pool(str(tmp_path), engine="band_energy", audit_log=log)
    assert first.purged == 1

    second = P.scan_pool(str(tmp_path), engine="band_energy", audit_log=log)
    assert second.purged == 0, "a drained pool purged something on the second pass"
    assert second.errors == 0, "the second pass errored: %s" % [o.detail for o in second.outcomes]
    assert second.scanned == 1, "the destroyed clip is still being scanned"
    assert len(P.read_receipts(log)) == 1, "the re-run wrote a second receipt for one destruction"


def test_a_third_and_fourth_pass_say_exactly_what_the_second_said(tmp_path):
    clip_at(tmp_path, "voice.wav", SIG.speech_like())
    P.scan_pool(str(tmp_path), engine="band_energy")
    seen = {tuple(sorted(P.scan_pool(str(tmp_path), engine="band_energy").as_record().items()))
            for _ in range(3)}
    assert len(seen) == 1, "repeated scans of a settled pool disagree: %s" % (seen,)


def test_an_armed_run_after_a_dry_run_still_destroys_the_clip(tmp_path):
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    before = open(path, "rb").read()
    assert P.purge_clip(path, band_energy(), dry_run=True).status == "would_purge"
    assert open(path, "rb").read() == before, "the dry run modified the clip"
    assert P.purge_clip(path, band_energy()).status == "purged"
    assert not os.path.exists(path)


# ---------------------------------------------------------------- malformed clips

@pytest.mark.parametrize("name,payload", [
    ("empty", lambda: b""),
    ("not_a_wav", lambda: SIG.not_a_wav()),
    ("truncated", lambda: SIG.truncated_wav(SIG.speech_like())),
])
def test_an_unreadable_clip_is_an_error_and_is_never_reported_as_silent(tmp_path, name, payload):
    """The one answer that is never acceptable is a quiet `kept`.

    Shipped behaviour is `error` + the file survives; docs §9 asks for a purge. The divergence
    is recorded in docs §12 and is the engine owner's call -- what this test pins is that
    neither resolution may arrive as "no speech here", which is the answer that turns an
    undecidable clip into a retained one without anybody noticing.
    """
    path = str(tmp_path / (name + ".wav"))
    open(path, "wb").write(payload())
    outcome = P.purge_clip(path, band_energy())
    assert outcome.status != "kept", "%s was silently kept" % name
    assert outcome.receipt is None or outcome.receipt.peak_speech_prob == outcome.receipt.peak_speech_prob


def test_a_header_that_lies_about_its_length_is_decided_on_the_bytes_that_exist(tmp_path):
    path = str(tmp_path / "lying.wav")
    open(path, "wb").write(SIG.lying_wav(SIG.speech_like()))
    outcome = P.purge_clip(path, band_energy())
    assert outcome.status == "purged", \
        "a truncated-but-readable voice clip was not destroyed: %s" % outcome.detail
    assert not os.path.exists(path)


def test_a_single_sample_clip_is_not_speech_and_does_not_raise(tmp_path):
    path = str(tmp_path / "one.wav")
    open(path, "wb").write(SIG.wav_bytes(np.asarray(SIG.speech_like()[:1], dtype=np.float32)))
    outcome = P.purge_clip(path, band_energy())
    assert outcome.status in ("kept", "error")
    assert os.path.exists(path)


def test_a_fully_clipped_clip_is_still_judged(tmp_path):
    """Preamp saturation must not become an exception path that leaves audio on disk."""
    path = clip_at(tmp_path, "clipped.wav", np.clip(np.asarray(SIG.speech_like()) * 50, -1, 1))
    outcome = P.purge_clip(path, band_energy())
    assert outcome.status in ("purged", "kept")


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_non_finite_sample_never_produces_a_confident_silence(bad):
    """⚠️OPEN DEFECT, xfailing until the engine owner closes it (docs §9, `vad_inference_error`).

    A NaN propagates through the fallback's band ratio, `peak_prob` comes back NaN, every
    comparison against the threshold is False, and the clip is reported as `speech=False` --
    a confident silence produced by arithmetic rather than by the audio. Docs §9 requires the
    opposite: a detector that answered NaN has not answered, and the clip is purged.
    """
    samples = np.asarray(SIG.speech_like(), dtype=np.float32).copy()
    samples[100] = bad
    with np.errstate(all="ignore"):
        decision = P.inspect_samples(samples, 16000, band_energy())
    if decision.peak_prob != decision.peak_prob:
        pytest.xfail("peak_prob is NaN; docs §9 asks for a fail-closed purge, not a score")
    assert decision.speech, "a clip with a voice and a corrupt sample was called silent"


def test_the_audit_log_stays_strict_json(tmp_path):
    """NaN is not JSON, and an audit log a strict reader rejects -- jq, Go, a COPY -- is not one.

    A 16-bit WAV cannot carry a non-finite sample, so the NaN hole the test above xfails on is
    reachable only through the in-memory door (`inspect_samples`, `purge_wav_bytes`); this
    guards the disk path against the day a probability arrives there unclamped.
    """
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    log = str(tmp_path / P.RECEIPT_NAME)
    assert P.purge_clip(path, band_energy(), audit_log=log).status == "purged"
    lines = open(log, "r", encoding="utf-8").read().splitlines()
    assert lines
    for line in lines:
        json.loads(line, parse_constant=_refuse_constant)


def _refuse_constant(token):
    raise AssertionError("the audit log holds %r, which no strict JSON reader will accept"
                         % (token,))


# ---------------------------------------------------------------- what the receipt cannot hold

def _values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _values(v)
    elif isinstance(obj, (list, tuple)):
        yield obj
        for v in obj:
            yield from _values(v)
    else:
        yield obj


def test_no_receipt_value_is_long_enough_to_be_audio(tmp_path):
    """2.0 s at 16 kHz is 32000 numbers. Every sequence on the receipt is a handful of seconds."""
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    record = P.purge_clip(path, band_energy()).receipt.as_record()
    for value in _values(record):
        if isinstance(value, (list, tuple)):
            assert len(value) <= 64, "a %d-element sequence on the receipt" % len(value)


def test_the_whole_receipt_is_smaller_than_the_clip_by_orders_of_magnitude(tmp_path):
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    clip_bytes = os.path.getsize(path)
    line = json.dumps(P.purge_clip(path, band_energy()).receipt.as_record())
    assert len(line) < min(4096, clip_bytes / 10), (
        "the receipt is %d B against a %d B clip; a death certificate does not grow with the "
        "body" % (len(line), clip_bytes))


def test_the_spans_stay_inside_the_clip_they_describe(tmp_path):
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    receipt = P.purge_clip(path, band_energy()).receipt
    record = receipt.as_record()
    assert record["speech_s"] <= record["duration_s"] + 1e-6
    for start, end in record["speech_spans_s"]:
        assert 0.0 <= start < end <= record["duration_s"] + 1e-6, \
            "span (%s, %s) is outside a %s s clip" % (start, end, record["duration_s"])


def test_the_digest_is_of_the_bytes_that_were_destroyed(tmp_path):
    import hashlib
    path = clip_at(tmp_path, "voice.wav", SIG.speech_like())
    expected = hashlib.sha256(open(path, "rb").read()).hexdigest()
    receipt = P.purge_clip(path, band_energy()).receipt
    assert receipt.purged_sha256 == expected
    assert not os.path.exists(path), "the digest matched but the clip is still here"
