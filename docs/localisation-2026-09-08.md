# Localisation, reframed around the rank-1 result — 2026-09-08

The operator asked dama-hear to **locate** sources. Two nodes cannot, ever. This is what the fleet
*can* produce today, what the mobile receivers would buy, and where each number came from.

## 1. The pair produces a locus, not a position

Two sensors give exactly one TDoA, so the position Fisher information is a rank-1 outer product
`J = g gᵀ / σ_τ²`, singular for any geometry, any baseline, any SNR. `hear/solve/rangediff.fisher`
computes it rather than asserting it; `tests/test_rangediff.py` runs 400 random geometries and
gets rank 1 on every one, and moving the source `1e-4 m` along a null direction changes the TDoA
by `< 1e-12 s` against `6.2e-8 s` along the observable one.

⚠️**There is therefore no GDOP, CEP, covariance or confidence to quote for a 2-node solve.** Any
code that produces one is producing an undefined quantity.

The honest product is `rangediff.cue()`:

| field | meaning |
|---|---|
| `range_difference_m` | `Δ = c·τ = \|s−P₂\| − \|s−P₁\|`, bounded by the baseline |
| `locus` | `hyperboloid_sheet` — semi-transverse `\|Δ\|/2`, focal half-distance `B/2` |
| `asymptote_angle_deg` | `arccos(−Δ/B)`, from the p1→p2 direction, 0–180° |
| `direction_sigma_terms_deg` | the four error terms **separately** |
| `fix` / `observable_dof` / `unobservable_dof` | `False` / `1` / `2` |
| `mirror_ambiguous` | `True` — the cone of revolution, not a bearing |

It carries **no** key from `rangediff.FIX_KEYS` (`east_m`, `north_m`, `up_m`, `range_m`,
`position_observable`, `dop`, `hdop`, `vdop`, `pdop`, `cep_m`, `confidence`), which is the set
`hear/backend/pipeline.to_dama_event` copies straight onto the fleet payload. A test enforces it,
and `cue()` raises on its own output if one ever appears.

## 2. ⚠️The direction is SURVEY-limited, not timing-limited, by two orders of magnitude

Measured on `survey.json` as deployed (`nyquist` σ 0.717 m, `mach` σ 0.521 m plus **1.0 m assumed**
on a **nominal** 3.0 m storey height that was never measured), B = 16.873 m:

| term | θ=30° | θ=60° | θ=90° |
|---|---|---|---|
| timing, σ_e = 19.3 µs (picker p50, high SNR) | 0.064° | 0.037° | 0.032° |
| timing, σ_e = 336.9 µs (picker p50, low SNR) | 1.117° | 0.645° | 0.559° |
| baseline **length** (σ 0.890 m along) | 5.235° | 1.745° | 0.000° |
| baseline **axis** (σ 0.997 m perpendicular) | 3.38° | 3.38° | 3.38° |
| sound speed, 1 °C | 0.175° | 0.058° | 0.000° |

So the error ordering on this pair is **receiver count and placement → node survey → sound speed →
onset picking → clock sync**. Clock sync is fifth, not third. The 2.5 m of unmeasured `mach` height
alone outweighs every clock in the fleet. Only the picker's ~20% chunk-quantised fallback
(~5.25 ms, flat across 80 dB of SNR) makes timing the binding term.

⚠️**Endfire is degenerate.** `sin θ` divides every term above, so as `|Δ| → B` the direction σ
diverges and `cue()` returns `inf` rather than a small-looking number. This is the same selection
effect `consistency.py` measures from the other side: of 205 Kinect impulses, every one that passed
the gate lay 58–117° off the array axis and none of the 19% near endfire did.

## 3. What the 2-node pair does *not* do, in code

Every entry point already refuses two nodes — this was audited, not assumed:

| call | behaviour at n=2 |
|---|---|
| `placement.dop` | raises `need >= 3 nodes` |
| `placement.dop3` | raises `need >= 4 nodes` |
| `point.solve` | raises `need >= 4 nodes for a 3D fit` (3 with `fixed_up_m`) |
| `shockwave.solve` | raises `need >= 3 nodes` |
| `associate.associate` | `min_nodes=3`, rejects as `too_few_nodes` |
| `calibrate.solve_multi` | raises, every event needs ≥ 3 nodes |

**No code path computes a GDOP, CEP or confidence for a 2-node solve.** Three related defects that
are real, though:

1. ⚠️`consistency.check_array` **raises** on 2 mics (`additivity needs >= 3 mics`), so the one
   check that *is* defined for a pair — the plane-wave bound `|τ| ≤ d/c` — is unreachable through
   the verdict API. `rangediff.cue()` calls `physically_possible` directly and refuses past it.
2. ⚠️`point._report` reported `dop` (2D, **two** unknowns) beside `pdop` (**three** unknowns) with
   nothing saying so. Measured on a 4-node ground layout with a source at (30, 30, 2): `dop` 31.59,
   `hdop` 35.83, `pdop` 45.09 — the key a consumer would naturally quote reads 12% below the
   horizontal figure and 30% below the whole one. Now carries `dop_unknowns` / `pdop_unknowns`.
3. ⚠️`dop_singular` / `dop_dof` described `dop3`, not `dop`: at three nodes `dop_singular` is
   `True` while `dop` is finite and can look excellent. Now also emitted as `dop3_singular` /
   `dop3_dof`; the old names are kept so nothing breaks.
4. ⚠️`burstassoc.associate_burst` — the live 2-node path on `nyquist`/`mach` — returns `tau_s` with
   a pair-count `margin` and a peak-to-peak `tau_spread_s`, but **no σ**. `rangediff.cue()` makes
   `sigma_tau_s` a required keyword for that reason: a delay with no σ is an assertion.

## 4. The crack-blast gate is replaced by a CRLB floor

`interval_consistent`'s `|dt_i − dt_j| ≤ 2d/c` bound is a real inequality and is kept, but it is not
a result: it scales with separation, so it cannot fail past ~0.35 m at 2 ms of onset scatter.
Measured on the 2026-09-05 phones (10.132 / 13.012 / 20.005 m), the bound is 58.7–115.9 ms and
every pair passed on every burst — including pairs whose implied ranges differed by a factor of two.

Lindgren et al. (2010, EURASIP JASP 690732) publish the MB-SW position-error floor instead:

```
RMSE(x) >= 1430 * sigma_e   [m],   sigma_e = per-microphone detection-time error [s]
```

`crackblast.position_error_floor_m()`. `interval_consistent` now also returns `discriminating`,
`tightest_bound_slack` and `position_floor_m`, and `IntervalCheck.usable_as_gate` is the
conjunction. Measured:

| set | slack (2d/c ÷ tol) | `discriminating` | floor at that σ |
|---|---|---|---|
| ESP intra-board, 38.1 mm, σ 0.25 ms | 0.21 | yes | 0.357 m |
| 2026-09-05 phones, 10.1 m, σ 0.70 ms | 19.8 | **no** | 1.001 m |

⚠️**1430 is Lindgren's array, not a universal constant.** It has units of m/s and
`1430 / 345.238 = 4.14`: it is `c` times a geometric dilution of ~4.1 for *their* layout.
`crlb_geometry_factor()` exposes that 4.14 so a caller can substitute a DOP measured on its own
geometry — ours is 35–200 (§5), so 1430 is a floor we are nowhere near, not a prediction.

## 5. Mobile receivers: what the geometry actually requires

A phone or hugbot at a **known** position at a **known** time is a third node. Measured with
`placement.dop_grid` over a 105×105 m box around the pair, step 5 m:

| third node | median DOP | box at DOP ≤ 10 | thinnest observable band |
|---|---|---|---|
| midpoint + 5 m perpendicular | 202.7 | 4% | 5.1 m |
| midpoint + 17 m perpendicular | 87.8 | 7% | 14.9 m |
| midpoint + 34 m perpendicular | 35.5 | 17% | 16.2 m |
| 17 m off the end of the baseline | 35.4 | 13% | **0.3 m** |

DOP versus range from the array centroid, bearing 45°:

| layout | 10 m | 30 m | 50 m | 80 m | 120 m |
|---|---|---|---|---|---|
| 3 nodes, apex 17 m perp | 6.8 | 146 | 175 | 327 | 634 |
| 3 nodes, apex 34 m perp | 8.3 | 33.7 | 56.2 | 111 | 219 |
| 4 nodes, apex ±17 m perp | 1.7 | 13.2 | 40.4 | 109 | 249 |
| 4 nodes, apex ±34 m perp | 1.5 | 4.1 | **9.3** | 23.1 | 51.9 |

Symmetric 4-node square of side S centred on the pair:

| S | DOP at 50 m | median over the box | box at DOP ≤ 10 |
|---|---|---|---|
| 20 m | 26.6 | 84.5 | 6% |
| 40 m | 7.9 | 21.2 | 26% |
| 60 m | 4.4 | 8.9 | 55% |
| 80 m | 1.1 | 4.8 | 90% |

**The requirement, stated plainly.**

- **Off the baseline.** `linearity` must clear `COLLINEAR_LINEARITY = 0.02`, which is only 0.288 m
  of perpendicular offset — a threshold, not a usable geometry. Usable starts around 30–40 m.
- **Not off the end.** A node on the baseline extension clears the collinearity threshold at
  similar median DOP but collapses the thinnest observable band to 0.3 m: it buys aperture in the
  direction that was already observable and nothing in the one that was not.
- **Three nodes is not enough for a yard-scale fix.** Median DOP 35–200 and DOP ≤ 10 over 4–17% of
  the box. Three ground nodes are also coplanar *by construction*, so height is unobservable and
  mirror-ambiguous whatever their heights are.
- **Four nodes at ±34 m** hold DOP ≤ 10 out to ~50 m. An 80 m aperture holds it over 90% of the box.
- ⚠️**DOP is not the whole error.** It multiplies the *range-difference* error (σ_Δ = 9.4 mm at the
  picker's high-SNR σ), but node survey error enters roughly directly. At DOP 10 and the current
  0.5–0.9 m survey, position error ≈ `hypot(0.09, 0.9) ≈ 0.9 m` — **survey-limited again**. Adding
  receivers and re-surveying are the same-priority job, not sequential ones.
- Height needs the plane broken. Four nodes at `mach` 3.0 m / phone 1.5 m / hugbot 0.7 m give
  planarity RMS 0.197 m — still `coplanar=True`, `mirror_ambiguous=True`. A node on a roof or a
  mast, not a taller tripod.

## 6. The phone chirp is the calibration instrument

A dama-gotchi chirp is the only acoustic event this fleet can generate with a **known source
position and a known emission instant**. That collapses two open problems.

### 6a. Per-handset audio-stamp offset — works today, no re-survey needed

`β = t_heard − t_emitted − d/c`. One event, no solve, no rank argument (contrast
`calibrate.solve_multi`, which needs the source to *move* over K ≥ 2 events). Four terms, from
`calibrate.known_source_offset_budget()`:

| standoff / survey | survey | pick | node clock | sound speed (1 °C) | single | ×100 chirps |
|---|---|---|---|---|---|---|
| 5 m, phone RTK 0.02 m | 58.2 µs | 20 µs | 14.5 µs | 25.7 µs | 68.3 µs | **26.5 µs** |
| 36 m, phone RTK 0.02 m | 58.2 | 20 | 14.5 | **185.0** | 195.5 µs | 185.1 µs |
| 5 m, node at survey.json σ 0.717 m | 2087.8 | 20 | 14.5 | 25.7 | 2088 µs | **210 µs** |

⚠️**Stand close.** The sound-speed term is the only one proportional to path length and the only one
repetition cannot remove (it is common-mode at a fixed path). ⚠️**Move the phone between chirps**,
or its survey error is a bias rather than noise.

Against the 16.7 ms per-device offset an independent ballistic yardstick measured once (root cause
never found), even the *worst* row above is 80× inside it. **This is measurable with the hardware
and the survey the fleet has right now.**

Separating transmit from receive path: a chirp heard by a PPS node gives `β_tx` of the emitting
handset; the same chirp heard by a second handset gives `β_tx(A) + β_rx(B)`, so `β_rx(B)` follows.
Round-robin over K handsets with ≥ 1 PPS node as the time reference determines every `β_tx` and
`β_rx`.

### 6b. Sound speed — blocked by the survey, not by the clock

Two clock-synced nodes hear one chirp: `c = (d₂ − d₁) / (t₂ − t₁)`. The emission instant and every
constant in the phone's playback path **cancel in the difference**, so this needs no `β` at all.
`soundspeed.temperature_from_separation()`:

| node survey σ_Δd | Δd = 16.9 m | Δd = 50 m | Δd = 100 m |
|---|---|---|---|
| 0.890 m (survey.json as it stands) | **29.9 °C** | 10.1 °C | 5.0 °C |
| 0.028 m (both nodes RTK) | 0.99 °C | 0.33 °C | 0.17 °C |

The timing term is 0.32 / 0.11 / 0.05 °C in every row — it never matters. ⚠️**The existing pair
cannot measure air temperature to any useful precision until it is re-surveyed**, whatever the
clock does. With RTK nodes, `separation_for_temperature(0.5 °C)` wants Δd ≥ 33.5 m, i.e. a receiver
33 m+ out on the baseline extension — the *opposite* standoff from §6a, which is why measuring `c`
and measuring `β` are two experiments and not one.

Reaching 1 °C matters because 1 °C of temperature error is 183 µs over 35 m — larger than the
entire clock-sync budget the PPS design exists to protect (fleet σ ≈ 14.5 µs ≈ 5 mm).

## 7. Open

- `burstassoc` produces no σ on its `tau`, so nothing downstream can populate `cue()`'s mandatory
  `sigma_tau_s` from live data yet. That is the next wiring step, not a new model.
- The `mach` 3.0 m height is a nominal storey and is the single largest term in the axis σ.
  Measuring it is one afternoon and one tape measure.
- `MAX_USEFUL_SLACK = 3.0` is a judgement, sized against the two measured cases it must separate
  (0.21 and 19.8). It has no published counterpart.
