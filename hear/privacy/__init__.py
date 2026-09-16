"""Privacy machinery: what the fleet is allowed to keep, and what has to be destroyed.

The nodes acquire at 48 kHz for acoustic events, which also means a microphone that can hear a
conversation. `silero_vad` is the detector that decides whether a buffer contains speech; it is
kept separate from the acoustic pipeline so the purge decision has one implementation and one
place to audit.
"""

from hear.privacy.silero_vad import (
    CHUNK_SAMPLES,
    SUPPORTED_RATES,
    SileroVAD,
    SileroVADError,
    SyntheticVADEngine,
    UnsupportedSampleRate,
    decimate_48k_to_16k,
    get_speech_timestamps,
    is_speech,
)

__all__ = [
    "CHUNK_SAMPLES",
    "SUPPORTED_RATES",
    "SileroVAD",
    "SileroVADError",
    "SyntheticVADEngine",
    "UnsupportedSampleRate",
    "decimate_48k_to_16k",
    "get_speech_timestamps",
    "is_speech",
]
