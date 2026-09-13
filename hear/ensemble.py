#!/usr/bin/env python3
"""Multi-node spatial consensus: fusing what several array nodes independently said about one
acoustic event into one calibrated, corroborated prediction.

A single node's classifier score is one noisy channel's opinion. `nyquist`, `mach`, `rankine`,
`gold`, `ageev` and `kasami` (or any subset of a survey) each run the same model on their own
microphone, their own self-noise and their own propagation path, so when TWO OR MORE of them
report the same class inside the array's own transit window that agreement is independent
corroborating evidence -- and a single node's spike with nothing at any other node is exactly what
uncorrelated local noise (wind hitting one capsule, a truck idling by one post) looks like. This
module is the one place that turns "several nodes each said something" into one number, instead of
leaving every caller to average scores by hand and re-invent the same three mistakes: mixing
weights that are not on a probability scale, double-counting a shared prior once per extra node,
and rewarding a lone outlier the same as three nodes that agree.

## The transit window, not a fixed one

Two arrivals of the SAME physical event can differ in time by at most `d/c`, `d` the distance
between the two nodes and `c` the speed of sound -- exactly the bound `hear.backend.associate` and
`hear.solve.consistency.physically_possible` already enforce for TDoA grouping. `transit_window_s`
reuses that formula (`hear.solve.shockwave.sound_speed`, the array's own diameter, plus a stated
margin for onset-timing and clock slop) so a 10 m array and a 300 m array are never fused against
the same fixed cutoff -- see `hear.backend.associate.max_window_s`, which this restates for a
plain diameter rather than a `Survey` object so this module has no import-time dependency on one.

## Fusion math

Each node's per-class score is read as a calibrated probability `p_i` (the sigmoid outputs
`tools/hear_tag.py`'s taggers already store in a tag row's `scores` dict). Two fusion rules are
offered:

**Log-odds / Bayes pooling** (`method="logodds"`, the default). In logit space,
`logit(p) = ln(p / (1-p))`. Treating each node's observation as conditionally independent given
the true class (physically reasonable: independent capsules, independent self-noise, independent
propagation paths), naive-Bayes fusion of `n` independent likelihood ratios against one shared
prior `p0` is

    logit(p_fused) = sum_i w_i * logit(p_i) - (sum_i w_i - w_ref) * logit(p0)

i.e. the prior is folded in once, not once per node -- summing raw logits directly would count the
prior `n` times and inflate confidence purely from adding indifferent nodes. At the neutral prior
`p0 = 0.5`, `logit(p0) = 0` and the correction vanishes; a caller with a known class base rate can
supply it. `w_i` is the node's fusion weight (see below); `w_ref` is the mean weight, so the
correction scales with how many nodes were actually pooled rather than a raw count.

**Weighted probability pooling** (`method="linear"`), the linear opinion pool
`p_fused = sum_i w_i * p_i / sum_i w_i`. Cheaper and order-independent like the log-odds rule, but
does not correct for a shared prior and saturates less sharply -- offered because some callers
want the more conservative, harder-to-drive-to-0-or-1 average instead of the Bayes-consistent one.

## SNR / RMS weighting

`w_i` is `snr_weight(snr_db)`, `10 ** (snr_db / 10)` clipped below a floor and re-normalised across
the group -- the maximal-ratio-combining weight from array signal processing, where a channel's
correct contribution to a coherent combine is proportional to its power SNR, not its amplitude nor
a flat 1/n. A node capturing a bark at 30 dB SNR is not treated the same as one capturing it at
6 dB above its own self-noise floor; RMS level (`hear.spatial.calculate_sound_level_db`) is used
verbatim as `snr_db` when a caller has no noise-floor estimate, since level above a fixed
full-scale reference already tracks SNR for a roughly constant per-node noise floor.

## Coincidence corroboration, and why it suppresses lone spikes without a special case

`group_predictions` only ever pools nodes that are candidates for ONE physical event (same class,
inside the transit window, at most one observation per node). A class only one node reported never
enters a pool with anything else -- its "fused" score IS that lone node's score, unelevated, and
`corroborated` is False. A class several nodes agree on gets both the log-odds pooling above (which
already raises confidence roughly with the count of agreeing high-probability channels, the same
maths as combining independent likelihood ratios) AND an explicit `coincidence_bonus_db`, capped,
credited only for nodes whose own score clears `agree_floor` -- a node that merely HELD the class in
its scores dict at a near-zero probability does not count toward the bonus or get to claim credit
for another node's detection. This is what keeps a single noisy node's spike from reading the same
as three independent nodes agreeing: the spike has nowhere to average against and earns no bonus,
while the agreeing three do both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .solve.shockwave import sound_speed

#: Onset-timing / survey / clock slop added to the physical d/c bound -- the same constant
#: `hear.backend.associate.MARGIN_S` uses, restated here so this module has no import-time
#: dependency on `hear.backend.associate` (which itself imports numpy and a Survey duck-type this
#: module does not need).
MARGIN_S: float = 0.030

#: Default array diameter used only when a caller gives neither a `window_s` nor a diameter --
#: 30 m is representative of the small backyard-scale deployments this project runs (see
#: hear/nodeclass.py's measured nyquist/mach/rankine separations, all under 12 m). ALWAYS prefer
#: passing the real diameter or an explicit window; this is a documented fallback, not a survey.
DEFAULT_DIAMETER_M: float = 30.0

#: Below this SNR (or RMS level) a node's weight is floored rather than driven toward zero or
#: negative -- a node that is all noise still gets a small, non-zero say instead of a divide-by-
#: near-zero weight blowing up the normalisation.
SNR_FLOOR_DB: float = -20.0

#: A node's score for a class must clear this to count as "agreeing" for the coincidence bonus.
#: 0.5 is the natural midpoint of a calibrated probability -- a node that is not itself over half
#: confident in the class does not get to corroborate another node that is.
DEFAULT_AGREE_FLOOR: float = 0.5

#: Per corroborating node beyond the first, in dB of logit-equivalent bonus -- see
#: `coincidence_bonus_db`. Capped by MAX_COINCIDENCE_BONUS_DB so a very large array cannot drive
#: the fused score to 1.0 on agreement alone.
COINCIDENCE_BONUS_PER_NODE_DB: float = 3.0
MAX_COINCIDENCE_BONUS_DB: float = 12.0

#: 1 dB of logit-equivalent bonus, converted with the same 20*log10 convention used for level.
_DB_TO_LOGIT = math.log(10.0) / 20.0

_EPS = 1e-6


def sigmoid(x: float) -> float:
    """1 / (1 + e^-x), clipped so an extreme logit never overflows."""
    if x >= 0:
        z = math.exp(-min(x, 700.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -700.0))
    return z / (1.0 + z)


def logit(p: float, eps: float = _EPS) -> float:
    """ln(p / (1-p)), with `p` clipped to (eps, 1-eps) so 0.0 and 1.0 do not diverge."""
    p = min(max(float(p), eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def transit_window_s(diameter_m: float, temp_c: float = 20.0, margin_s: float = MARGIN_S) -> float:
    """Widest defensible spread of one physical event across nodes `diameter_m` apart: d/c +
    margin. The same formula as `hear.backend.associate.max_window_s`, taking a plain diameter
    instead of a Survey so this module stays usable without one.
    """
    return float(diameter_m) / sound_speed(temp_c) + float(margin_s)


def snr_weight(snr_db: Optional[float], floor_db: float = SNR_FLOOR_DB) -> float:
    """Maximal-ratio-combining weight: `10 ** (snr_db / 10)`, power SNR, floored rather than let
    run to 0 or negative for a very quiet node.

    `None` (no SNR/RMS estimate stated) gets the floor weight -- reported as the most cautious
    honest answer rather than guessed at, the same discipline `arrival_is_usable` uses for a
    missing quality flag: absent is not "trusted", it is "worst-cased".
    """
    db = floor_db if snr_db is None else max(float(snr_db), floor_db)
    return 10.0 ** (db / 10.0)


@dataclass
class NodeObservation:
    """One node's model output for one clip: what `tools/hear_tag.py` writes to a tag row,
    trimmed to what fusion needs. `scores` is the per-class calibrated probability dict a tagger
    already produces (see `tools/hear_tag.py`'s `model_block`/`tag()` output) -- never a raw logit.
    """
    node: str
    t_utc_s: float
    scores: Dict[str, float]
    snr_db: Optional[float] = None
    rms_dbfs: Optional[float] = None

    def weight(self, floor_db: float = SNR_FLOOR_DB) -> float:
        """This node's fusion weight, from `snr_db` if stated, else `rms_dbfs` as the fallback
        level proxy (see the module docstring on RMS-as-SNR), else the floor."""
        db = self.snr_db if self.snr_db is not None else self.rms_dbfs
        return snr_weight(db, floor_db)


@dataclass
class ConsensusPrediction:
    """One fused, spatially-corroborated prediction for one class at one moment."""
    class_name: str
    t_utc_s: float
    fused_score: float
    method: str
    n_nodes: int
    nodes: Tuple[str, ...]
    per_node_scores: Dict[str, float]
    mean_snr_db: Optional[float]
    spread_s: float
    coincidence_bonus_db: float
    agreement_count: int
    corroborated: bool
    window_s: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "class": self.class_name,
            "t_utc_s": self.t_utc_s,
            "fused_score": self.fused_score,
            "method": self.method,
            "n_nodes": self.n_nodes,
            "nodes": list(self.nodes),
            "per_node_scores": dict(self.per_node_scores),
            "mean_snr_db": self.mean_snr_db,
            "spread_s": self.spread_s,
            "coincidence_bonus_db": self.coincidence_bonus_db,
            "agreement_count": self.agreement_count,
            "corroborated": self.corroborated,
            "window_s": self.window_s,
        }


def _group_within_window(obs: Sequence[NodeObservation], window_s: float
                         ) -> List[List[NodeObservation]]:
    """Chain observations (already filtered to one class) into groups where every consecutive
    pair sits within `window_s` and no node appears twice in a group.

    Sorted-and-chained rather than all-pairs: with arrivals sorted by time, a physical event's
    spread across an array is monotonic in the sort order, so a single left-to-right pass is
    enough -- the same discipline `hear.backend.associate.associate` uses, restated here for a
    node-name/class key instead of `(node_id, seq)`. A candidate whose gap from the running
    group's last member exceeds `window_s`, OR whose node already holds a slot in the running
    group, starts a NEW group rather than being dropped -- every observation ends up in exactly
    one group.
    """
    ordered = sorted(obs, key=lambda o: o.t_utc_s)
    groups: List[List[NodeObservation]] = []
    current: List[NodeObservation] = []
    seen_nodes: set = set()
    for o in ordered:
        if current and (o.t_utc_s - current[-1].t_utc_s > window_s or o.node in seen_nodes):
            groups.append(current)
            current, seen_nodes = [], set()
        current.append(o)
        seen_nodes.add(o.node)
    if current:
        groups.append(current)
    return groups


def coincidence_bonus_db(agreement_count: int, agree_floor: float = DEFAULT_AGREE_FLOOR,
                         per_node_db: float = COINCIDENCE_BONUS_PER_NODE_DB,
                         max_db: float = MAX_COINCIDENCE_BONUS_DB) -> float:
    """dB of logit-equivalent bonus for `agreement_count` nodes independently clearing
    `agree_floor` on the same class. Zero for 0 or 1 agreeing nodes -- corroboration needs at
    least two independent channels agreeing; one node cannot corroborate itself."""
    if agreement_count <= 1:
        return 0.0
    return min(per_node_db * (agreement_count - 1), max_db)


def fuse_logodds(group: Sequence[NodeObservation], class_name: str, prior: float = 0.5,
                 floor_db: float = SNR_FLOOR_DB) -> float:
    """Weighted, prior-corrected log-odds pool of `group`'s scores for `class_name`.

    `logit(p_fused) = sum_i w_i*logit(p_i) - (sum_i w_i - w_ref)*logit(prior)`, `w_ref` the mean
    weight -- see the module docstring for why the prior is folded in once rather than once per
    node. At `prior=0.5` (`logit(0.5) == 0`) the correction term is exactly zero.

    ⚠️WEIGHTS ARE RENORMALISED TO A MEAN OF 1 BEFORE THIS RUNS, NOT USED AS RAW SNR POWER. A raw
    `10 ** (snr_db / 10)` is an absolute power ratio -- 6.3 at 8 dB, 316 at 25 dB -- and using it
    unnormalised would scale even a LONE node's logit by that factor, turning a single 0.97
    reading into a near-certainty nothing else supports. Renormalising to mean 1 keeps the
    all-else-equal case (one node, or several at equal SNR) an ordinary unweighted pool, and lets
    only the RELATIVE SNR between nodes in a group shift how much each one's opinion counts.
    """
    raw_weights = [o.weight(floor_db) for o in group]
    logits = [logit(o.scores[class_name]) for o in group]
    mean_w = sum(raw_weights) / len(raw_weights)
    if mean_w <= 0.0:
        return prior
    weights = [w / mean_w for w in raw_weights]
    w_sum = sum(weights)
    weighted = sum(w * l for w, l in zip(weights, logits))
    w_ref = w_sum / len(weights)
    correction = (w_sum - w_ref) * logit(prior)
    return sigmoid(weighted - correction)


def fuse_linear(group: Sequence[NodeObservation], class_name: str,
                floor_db: float = SNR_FLOOR_DB) -> float:
    """SNR-weighted linear opinion pool: `sum_i w_i*p_i / sum_i w_i`."""
    weights = [o.weight(floor_db) for o in group]
    scores = [o.scores[class_name] for o in group]
    w_sum = sum(weights)
    if w_sum <= 0.0:
        return sum(scores) / len(scores) if scores else 0.0
    return sum(w * s for w, s in zip(weights, scores)) / w_sum


_FUSERS = {"logodds": fuse_logodds, "linear": fuse_linear}


def fuse_group(group: Sequence[NodeObservation], class_name: str, *, method: str = "logodds",
              prior: float = 0.5, agree_floor: float = DEFAULT_AGREE_FLOOR,
              floor_db: float = SNR_FLOOR_DB, window_s: float = 0.0) -> ConsensusPrediction:
    """Fuse one already-grouped set of same-class, distinct-node observations into one
    `ConsensusPrediction`. `window_s` is carried through for reporting only; grouping itself has
    already happened by the time this runs."""
    if method not in _FUSERS:
        raise ValueError("unknown fusion method %r; known: %s" % (method, ", ".join(_FUSERS)))
    kwargs = {"prior": prior} if method == "logodds" else {}
    fused = _FUSERS[method](group, class_name, floor_db=floor_db, **kwargs)

    agree = sum(1 for o in group if o.scores[class_name] >= agree_floor)
    bonus_db = coincidence_bonus_db(agree, agree_floor) if len(group) > 1 else 0.0
    if bonus_db:
        fused = sigmoid(logit(fused) + bonus_db * _DB_TO_LOGIT)

    snrs = [o.snr_db if o.snr_db is not None else o.rms_dbfs for o in group]
    snrs = [s for s in snrs if s is not None]
    mean_snr = sum(snrs) / len(snrs) if snrs else None
    times = [o.t_utc_s for o in group]
    spread = max(times) - min(times) if len(times) > 1 else 0.0

    return ConsensusPrediction(
        class_name=class_name,
        t_utc_s=sum(times) / len(times),
        fused_score=fused,
        method=method,
        n_nodes=len(group),
        nodes=tuple(sorted(o.node for o in group)),
        per_node_scores={o.node: o.scores[class_name] for o in group},
        mean_snr_db=mean_snr,
        spread_s=spread,
        coincidence_bonus_db=bonus_db,
        agreement_count=agree,
        corroborated=len(group) >= 2 and agree >= 2,
        window_s=window_s,
    )


def fuse_predictions(observations: Sequence[NodeObservation], *,
                     window_s: Optional[float] = None,
                     diameter_m: Optional[float] = None, temp_c: float = 20.0,
                     margin_s: float = MARGIN_S, method: str = "logodds", prior: float = 0.5,
                     agree_floor: float = DEFAULT_AGREE_FLOOR, floor_db: float = SNR_FLOOR_DB,
                     min_class_score: float = 0.0) -> List[ConsensusPrediction]:
    """Group `observations` per class within the array's transit window and fuse each group.

    `window_s` overrides the computed one when given (a test proving what too wide a window costs
    passes this explicitly, same discipline as `hear.backend.associate.associate`'s `window_s`
    parameter); production should pass `diameter_m` (or nothing, taking `DEFAULT_DIAMETER_M`) and
    let `transit_window_s` compute it. `min_class_score` drops a class entirely from an
    observation before grouping when neither node ever reports it above that floor -- 0.0 (off)
    keeps every class a tagger stored, however small.

    -> one `ConsensusPrediction` per (class, transit-window group), in ascending `t_utc_s` order.
    Every input observation's every qualifying class ends in exactly one group: a class only one
    node reported becomes its own one-node group rather than being dropped, so a genuine
    single-node detection is still returned (just never corroborated, and never bonused).
    """
    win = window_s if window_s is not None else transit_window_s(
        diameter_m if diameter_m is not None else DEFAULT_DIAMETER_M, temp_c, margin_s)

    by_class: Dict[str, List[NodeObservation]] = {}
    for o in observations:
        for cls, score in o.scores.items():
            if score < min_class_score:
                continue
            by_class.setdefault(cls, []).append(
                NodeObservation(o.node, o.t_utc_s, {cls: score}, o.snr_db, o.rms_dbfs))

    out: List[ConsensusPrediction] = []
    for cls, obs in by_class.items():
        for group in _group_within_window(obs, win):
            out.append(fuse_group(group, cls, method=method, prior=prior,
                                  agree_floor=agree_floor, floor_db=floor_db, window_s=win))
    out.sort(key=lambda c: c.t_utc_s)
    return out
