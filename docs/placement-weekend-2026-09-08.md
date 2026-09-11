# Where to put the extra receivers — 2026-09-08

Every number here came out of `hear/solve/placement.py` and `hear/solve/point.py` on the
survey.json geometry as deployed. Inference is marked. `nyquist` is the frame origin,
`mach` is at `(-16.602, -0.272, 3.0)`: **B = 16.873 m in 3D, 16.604 m horizontally**, and the
axis runs 269° (nyquist → mach, near enough due west), so *perpendicular* is north/south.

⚠️Median DOP depends entirely on the box it is taken over. Every table below uses ONE fixed
120 × 120 m box centred on the pair midpoint, step 5 m, so the candidates are comparable. The
CLI's default `--margin 100` box is bigger and its medians are correspondingly larger.

## 1. Three receivers break rank-1. Measured, not asserted

Rank of the marginalised position Fisher matrix `Gᵀ(I − 11ᵀ/N)G`, over 400 random geometries and
random sources each:

| receivers | 2D rank | 2D λ₂/λ₁ (median) | 3D rank | 3D λ₃/λ₁ (median) |
|---|---|---|---|---|
| 2 | **1** (max λ₂/λ₁ 8.5e-15) | 0 | **1** | −4.4e-17 |
| 3 | 2 | 0.0066 | 2 | 4.8e-16 |
| 4 | 2 | 0.0165 | **3** | 0.0058 |
| 5 | 2 | 0.0255 | 3 | 0.0156 |

**Horizontal position becomes observable at the third receiver; height at the fourth.** Nothing
else does it — the rank is set by the count, not the baseline, SNR or clock.

⚠️**Rank is not usability.** At three receivers λ₂/λ₁ is 0.005–0.014, a condition number of
70–185: the matrix is rank 2 and is still a rank-1 matrix wearing a hat. Where it becomes usable
is a geometry question, below.

## 2. ⚠️A receiver on the baseline EXTENSION buys a BETTER DOP and NO SOLUTION

The design phase said it "buys almost nothing". That is wrong in the direction that matters —
verified, at 34 m from the array centroid:

| third receiver | linearity | median DOP | thinnest band | DOP at 50 m, brg 45° | `point.solve` |
|---|---|---|---|---|---|
| on the baseline extension | **0.0000** | **30.4** | **0.69 m** | 51.6 | **refuses: `position_observable` False** |
| 34 m perpendicular | 0.4229 | 46.8 | 16.30 m | 17.3 | returns a coordinate |

It ranks **first** on median DOP and it is unsolvable. `dop()` is a *local* measure and cannot
see the discrete mirror twin a collinear array has, so it reports 30.4 for a geometry where
`point.solve` returns `east_m: None` and the note "nodes are collinear (linearity 0.0000):
position is UNOBSERVABLE". `best_addition()` now reports `linearity`/`collinear` and sorts
collinear candidates last for exactly this reason.

It also collapses the observable-offset band from 16.3 m to 0.69 m — for a supersonic track,
which is what this array actually hears, the extension node is worse than not deploying it.

## 3. How far off the baseline

Pair + one receiver, perpendicular offset from the midpoint:

| offset | linearity | median DOP | DOP ≤ 10 | thinnest band |
|---|---|---|---|---|
| 0.3 m (the `COLLINEAR_LINEARITY` threshold) | 0.021 | 182 | 2% | 0.4 m |
| 5 m | 0.348 | 263 | 2% | 5.1 m |
| 10 m | 0.695 | 193 | 3% | 10.1 m |
| 17 m | 0.846 | 113 | 6% | 14.9 m |
| **34 m** | 0.423 | **47** | 12% | **16.3 m** |
| 45 m | 0.320 | 34 | 18% | 16.4 m |
| 60 m | 0.240 | 26 | 26% | 16.5 m |

- `COLLINEAR_LINEARITY = 0.02` is cleared by **0.288 m** of offset. It is a numerical floor, not
  a usable geometry.
- The band saturates at ≈ the baseline (16.5 m) by 34 m of offset. **The observable band is the
  minimum width of the convex hull your receivers form** — that identity holds across every
  layout measured here, and it is the number a pace-out can be planned against.
- Past ~34 m the DOP keeps falling and nothing else improves. ⚠️DOP ignores whether a receiver
  can *hear* the event; read these as "best geometry among sites that all detect".

## 4. Which sides. Straddle

Pair + two receivers:

| layout | dof | median DOP | DOP ≤ 10 | band |
|---|---|---|---|---|
| **±34 m perpendicular (straddling)** | +1 | **8.41** | **55%** | 16.3 m |
| both +34 m, same side | +1 | 18.95 | 30% | 22.8 m |
| one +34 m perp, one on the extension | +1 | 14.53 | 37% | 30.2 m |
| ±17 m perpendicular | +1 | 37.11 | 16% | 14.9 m |

Straddling is 2.3× better than same-side at the same walking distance. This is the field's own
±6 m / 0.000 ms lesson stated in DOP.

If four receivers can be placed freely, ignore the existing pair's axis and lay a square:

| square side | median DOP | DOP ≤ 10 | band |
|---|---|---|---|
| 20 m | 35.1 | 15% | 20.3 m |
| 40 m | 8.76 | 55% | 40.6 m |
| **60 m** | **3.23** | **100%** | 61.0 m |
| 80 m | 1.47 | 100% | 81.3 m |

A 60 m square holds DOP ≤ 10 over the whole 120 × 120 m box. Adding the existing pair to it
(6 receivers) takes median DOP 3.23 → 2.57 and dof +1 → +3.

## 5. ⚠️What that is worth in METRES depends on which picker fired

Position σ = DOP × c × σ_t, in quadrature with the 0.5–0.9 m node survey (0.9 m used):

| layout (median DOP) | picker 19.3 µs (high SNR) | 336.9 µs (low SNR) | 5.25 ms (chunk fallback, ~20%) |
|---|---|---|---|
| 2 receivers | **no fix at any σ** | no fix | no fix |
| 3, +34 m perp (46.8) | 0.95 m | 5.49 m | 84.4 m |
| 4, ±34 m perp (8.4) | 0.90 m | 1.33 m | 15.2 m |
| 4, 60 m square (3.2) | 0.90 m | 0.97 m | 5.9 m |
| 6, pair + 60 m square (2.6) | 0.90 m | 0.95 m | 4.7 m |

**On a well-picked event the fourth receiver buys 5% of precision and 4.6× of coverage** (12% →
55% of the box solvable at all) — precision there is survey-limited and stays survey-limited from
DOP 1.5 to DOP 50. **On a badly-picked one it buys 4×, and on the ~20% chunk-quantised fallback
it buys 5.6×.** Adding receivers and re-surveying are the same-priority job, not sequential ones.

## 6. Height: do not chase it this weekend

Source at (25, 25, 0), four receivers:

| layout | planarity RMS | coplanar | mirror-ambiguous | hdop | vdop |
|---|---|---|---|---|---|
| mach 3.0 m + phones at 1.5 m / 0.7 m | 0.197 m | **True** | **True** | 7.1 | 29.2 |
| one receiver on a 10 m mast | 1.874 m | False | False | 17.9 | 46.9 |
| one receiver on a 25 m mast | 5.056 m | False | False | 117.0 | 137.2 |

Tripod heights do not break the plane. A mast does — and costs 2.5× of horizontal DOP at 10 m
and 16× at 25 m, because it spends aperture on the axis with the least to gain. 3D point-source
localisation is exact at 4 receivers and only *meaningful* at 5 (`node_counts("point", 3)`); a
roof node is a fifth-and-sixth-receiver job, not a fourth.

## 7. The layout to pace out

**Four receivers, from `nyquist` as origin, tape and compass:**

| # | what | E | N | from nyquist |
|---|---|---|---|---|
| 1 | nyquist (existing) | 0.00 | 0.00 | — |
| 2 | mach (existing) | −16.60 | −0.27 | 16.6 m, brg 269° |
| 3 | phone / hugbot | −8.86 | +33.86 | **35.0 m, brg 345°** |
| 4 | phone / hugbot | −7.74 | −34.13 | **35.0 m, brg 193°** |

i.e. pace 34 m due north of the midpoint between the two nodes, and 34 m due south of it.
Median DOP 8.41, DOP ≤ 10 over 55% of the box, dof +1 — the first layout in this fleet's history
whose residual can catch a bad pick.

**Five or six receivers:** put the extras on a 60 m square centred on the same midpoint
(±30 m north/south × ±30 m east/west). That is the first layout that is usable everywhere in the
box, and the fifth receiver alone takes median DOP 8.41 → 6.34, coverage 55% → 70%, dof +1 → +2.

⚠️Every one of these positions has to be **written down as it is paced**, or the weekend
produces the same unlabelled corpus. At DOP 8.4 the geometry contributes 56 mm and the survey
contributes 900 mm.

## 8. The phone chirp: two experiments, two opposite geometries

A phone emission has a known source position and a known emission instant. Two decoupled scalars
fall out; neither is a position, so rank-1 forbids neither.

### 8a. Sound speed alone, from a node-pair TDoA of ONE emission

`c = Δd / Δt`, `Δd = |s−P₁| − |s−P₂|`. The emission instant and every constant in the phone's
playback path cancel in the difference.

⚠️**Stand on the baseline EXTENSION, and the phone's own position stops mattering entirely.**
Measured: with the phone on the line 10, 30 or 60 m past `nyquist`, `Δd = 16.6042 m` = the node
separation **exactly**, and `|∂Δd/∂(phone position)| = 0.0000 m/m`. Broadside gives `Δd = 0` and
a gradient of 0.53. **This experiment needs no phone survey at all — only the node-pair
separation.** It is the same endfire geometry that is useless for localisation.

| configuration | σ_T | (survey term) | (timing term) |
|---|---|---|---|
| existing pair, survey.json σ (0.890 m) | **30.4 °C** | 30.4 | 0.36 |
| existing pair, both nodes RTK (0.028 m) | 1.02 °C | 0.96 | 0.36 |
| a new 50 m pair, tape-measured to 0.10 m | 1.14 °C | 1.13 | 0.12 |
| a new 50 m pair, both RTK | 0.34 °C | 0.32 | 0.12 |
| a new 100 m pair, tape-measured to 0.10 m | 0.57 °C | 0.57 | 0.06 |

⚠️**The survey term is a fixed bias on a fixed pair and does not average down over N chirps.**
Only the timing term does, and it never mattered. σ_T is the separation-error column, full stop.

⚠️**The gate rejects exact endfire.** `consistency.physically_possible(τ, B, c, tol_s=0)` passes
at `|τ| = B/c` exactly and **fails** one pick-σ past it; `rangediff.ENDFIRE_FRACTION = 0.999`
rejects it too. Stand ~5 m off the line: that costs **1%** of σ_T at either separation and clears
the bound.

**Pace-out:** two clock-synced nodes as far apart as the site allows (50 m gets 1.1 °C off a tape
measure). Tape the separation. Walk 30 m out along that line past the far node, then 5 m sideways.
20 chirps. Read `c`.

### 8b. Per-handset offset β = t_heard − t_emitted − d/c

One receiver, one chirp, no solve. Error budget over 100 chirps, µs:

| ring radius | node horiz. | Δheight | rope | pick | clock | c (1 °C) | TOTAL |
|---|---|---|---|---|---|---|---|
| 3 m | 124.7 | 0 | 87.4 | 1.9 | 14.5 | 15.4 | 153.8 |
| **8 m** | 46.8 | 0 | 87.4 | 1.9 | 14.5 | 41.1 | **108.3** |
| 20 m | 18.7 | 0 | 87.4 | 1.9 | 14.5 | 102.8 | 137.0 |

(at `nyquist`, which is the height *datum*; rope measured to 0.03 m)

⚠️**Chirping repeatedly from one spot does not average the node's survey error — it is a bias.**
Simulated, 100 chirps, node σ 0.717 m: from one spot the residual β bias is **2088 µs**; from a
60° arc, **2019 µs**; from a full 360° ring, **164 µs** — 12.7×. Walk the whole circle, 12
stations at 30°.

⚠️**A horizontal ring averages the node's HORIZONTAL error and not its VERTICAL one.** At `mach`,
whose 3.0 m is a nominal storey ±1.0 m, the height term alone is 546 µs at an 8 m ring and forces
the optimum out to 30 m for 230 µs. **Do this at `nyquist`, or tape-measure `mach`'s height
first.** Taping the height to 0.02 m and the rope to 0.01 m reaches **49 µs at a 20 m ring**.

⚠️**Order matters.** The sound-speed term is what pushes the optimum radius back in; pin `c` with
8a first (to 0.3 °C) and 8b's optimum moves to 20 m at 96 µs.

**Pace-out:** peg a rope at `nyquist`, measured once to ±1 cm. Walk a 12-station circle at 30°
intervals. 8 chirps per station. Repeat per handset. A chirp heard by a PPS node gives `β_tx`;
the same chirp heard by a second handset gives `β_tx(A) + β_rx(B)`, so a round-robin over K
handsets with ≥1 PPS node determines every `β_tx` and `β_rx`.

Against the 16.7 ms per-device offset the ballistic yardstick measured once, 108 µs is **154×
inside it**, and even the worst configuration above is 8× inside it.

## 9. What was fixed in the tooling to produce this

- `best_addition()` **raised `need >= 3 nodes`** — it could not answer the only placement question
  this fleet has ("where does the *third* receiver go?"). It now accepts a two-node base and
  reports `dop_gain`/`worst_span_gain_m` as `None` rather than a number computed from nothing.
- `best_addition()` **ranked the unsolvable collinear site first**. It now reports `linearity` and
  `collinear` and sorts collinear last.
- The CLI **exited on a two-node array**. It now states the rank-1 result and ranks candidates.
- ⚠️`worst_bearing()["observable"]` **is true by construction and is not a verdict**. Its probe
  track sits at the middle of the band, where the extremes straddle whatever the layout is, so the
  swing is set by the cone and the shift: over 200 random 3–6 node layouts it never fell below
  4.27 ms against a 0.05 ms floor, and its maximum is exactly `2·Δ·cos(θ_Mach)/c = 32.2988 ms`.
  A gate that cannot fail is not evidence — the same defect the crack-blast interval bound had.
  `span_m` is the number that carries the geometry; `swing_outside_band_ms` and `discriminating`
  are now reported beside it.
