# Loci memory validation for spatial events

`hear.loci_validation.LociSpatialMemory` is the narrow adapter between a recalled
Loci record and `SpatialEventPipeline`. It does not make localization depend on a
live memory service: a caller recalls and validates the record, then supplies it to
the pipeline. With no record, the pipeline behavior is unchanged.

```python
memory = LociSpatialMemory.from_dict({
    "bounds": {"east_m": [0, 200], "north_m": [0, 200], "up_m": [0, 30]},
    "anchors": {"nyquist": [20, 30, 1.5]},
    "anchor_tolerance_m": 5.0,
    "arrival_slack_s": 0.005,
})
pipeline = SpatialEventPipeline(survey, loci_memory=memory)
```

The ENU frame, origin, and anchor identity must be the same survey frame used by
the pipeline. Do not use a recalled geographic coordinate without projecting it
through the survey first.

## Adversarial checks

1. **Arrival plausibility:** before a point solve, a receiver is isolated only if
   it has uniquely the most pairwise violations of `abs(dt) <= baseline / c +
   slack`. This detects PPS whole-second jumps and inconsistent NLOS arrivals
   without rewriting timestamps. A tie stays unmodified because timing cannot
   identify the faulty receiver honestly.
2. **Anchor drift:** a surveyed receiver that differs from its durable Loci anchor
   by more than `anchor_tolerance_m` is excluded. This prevents dead-reckoning
   drift from becoming a confidently wrong receiver geometry.
3. **Map plausibility:** a solved point outside recalled map bounds is not emitted.
   This rejects reflection-only/NLOS candidates that fit timing but cannot be at
   the site.

Every isolated receiver is retained in the emitted feature's
`loci_memory_rejections` metadata. A map-bound rejection intentionally emits no
feature: it is not an observed source location.

The adversarial cases live in `tests/test_loci_memory_validation.py`, including a
Nyquist-style one-second jump, a smaller echo that ordinary association permits,
Kasami-style anchor drift, and an outside-boundary NLOS solution. Existing
`tests/test_tdoa_assoc_stress.py` continues to exercise simulated clutter,
multipath, and dropped arrivals.
