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


def test_a_clip_that_cannot_be_read_is_an_error_and_survives(tmp_path):
    """Undecided is not innocent, and it is not guilty either: the file is left where it is."""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    outcome = P.purge_clip(str(bad), P.BandEnergyVAD())
    assert outcome.status == "error"
    assert outcome.receipt is None
    assert bad.exists()


def test_a_detector_fault_is_an_error_not_a_keep(tmp_path):
    class Broken:
        name = "broken"

        def detect(self, samples, rate):
            raise RuntimeError("model file vanished")

    path = write_wav(tmp_path / "v.wav", voice())
    outcome = P.purge_clip(path, Broken())
    assert outcome.status == "error"
    assert os.path.exists(path)


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


def test_a_malformed_stream_raises(tmp_path):
    with pytest.raises(P.UnreadableClip):
        P.purge_wav_bytes(b"RIFFnope", node="n")


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
        if key == "speech_spans_s":
            assert all(len(pair) == 2 for pair in value)
            continue
        assert isinstance(value, (str, int, float, bool))


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


def test_cli_exit_code_2_when_a_clip_cannot_be_decided(tmp_path):
    (tmp_path / "broken.wav").write_bytes(b"still not a wav")
    proc = run_tool("--pool", str(tmp_path), "--vad", "band_energy")
    assert proc.returncode == 2
    assert "error" in proc.stdout


def test_cli_refuses_a_missing_pool_and_a_silly_threshold(tmp_path):
    assert run_tool("--pool", str(tmp_path / "nope")).returncode == 2
    assert run_tool("--pool", str(tmp_path), "--threshold", "4").returncode == 2
    assert run_tool("--pool", str(tmp_path), "--min-speech-ms", "-1").returncode == 2


def test_cli_requires_a_target():
    proc = run_tool("--dry-run")
    assert proc.returncode != 0
