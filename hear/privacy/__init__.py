"""Privacy enforcement for audio that was captured for acoustics and may contain a voice.

The node records 5.0 s WAVs so gunshots, claps and engines can be located. A microphone that
hears a gunshot also hears the person standing next to it, and a clip of a conversation is not
evidence this project is allowed to keep. This package is the mechanism that makes "we do not
retain speech" a thing the code does rather than a thing the README says.

`purge.py` is the pipeline: detect speech, hash the bytes, destroy the file, write a receipt
that proves the destruction and carries nothing of what was said. It is stdlib + numpy, opens
no socket, and refuses to emit any field it does not recognise.
"""
from . import purge  # noqa: F401
