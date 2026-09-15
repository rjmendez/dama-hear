"""Read-side verification semantics.

`hear/ingest/reconcile.py` audits what the two *writers* stored. This package audits what the
two *readers* answer. Both are stdlib-only, perform no I/O, and have no authority to change
the thing they audit.
"""
from . import shadow_read  # noqa: F401
