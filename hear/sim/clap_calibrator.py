"""Near-field joint clap-geometry and capture-latency calibration.

A human claps a handful of times within arm's reach of a small node array.
Neither the clap coordinates ``x_k`` nor the emission instants ``t_{0,k}`` are
known, and the per-node hardware capture delays ``b_i`` are also unknown except
for one or more reference nodes whose delay is pinned (``b_ref = 0``).

Everything is recovered at once by non-linear least squares over

    theta = [ (x_k, y_k, z_k, t_{0,k}) for k in 1..K ] + [ b_i for uncalibrated i ]

with residuals

    r_{k,i} = t_{k,i} - b_i - ( t_{0,k} + ||x_k - x_i|| / c )

subject to ``||x_k - array_center|| <= max_clap_radius_m``.  Parameter
uncertainty comes from the Gauss-Newton covariance
``Cov(theta) = sigma_res^2 (J^T J)^-1``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

from .latency_calibrator import CalibrationError, CalibrationEstimate, CalibrationRefusal

try:  # pragma: no cover - exercised implicitly by the solver tests
    from scipy.optimize import least_squares as _scipy_least_squares
except Exception:  # pragma: no cover - scipy is a hard requirement for solving
    _scipy_least_squares = None

__all__ = [
    "ClapCalibrationError",
    "ClapCalibrationResult",
    "ClapObservation",
    "ClapSource",
    "ClapCalibrator",
]

_DEFAULT_SOUND_SPEED_MPS = 343.0
_DEFAULT_MAX_RADIUS_M = 1.0
_RADIUS_PENALTY_WEIGHT = 1.0e3


class ClapCalibrationError(CalibrationError):
    """The clap calibration problem is malformed or not identifiable."""


@dataclass(frozen=True)
class ClapObservation:
    """One clap onset timestamp as captured by one node."""

    clap_id: str
    node_id: str
    timestamp_s: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClapObservation":
        try:
            return cls(str(value["clap_id"]), str(value["node_id"]), float(value["timestamp_s"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ClapCalibrationError("clap observation is malformed") from exc


@dataclass(frozen=True)
class ClapSource:
    """Recovered clap geometry and emission time."""

    clap_id: str
    position_m: Tuple[float, ...]
    emission_time_s: float
    sigma_position_m: Tuple[float, ...]
    sigma_emission_time_s: float
    n_nodes: int
    radius_from_center_m: float = 0.0


@dataclass(frozen=True)
class ClapCalibrationResult:
    """Full joint solve output."""

    biases: Dict[str, CalibrationEstimate]
    claps: Tuple[ClapSource, ...]
    residual_rms_s: float
    sigma_residual_s: float
    covariance: np.ndarray
    parameter_names: Tuple[str, ...]
    n_observations: int
    n_parameters: int
    degrees_of_freedom: int
    converged: bool
    cost: float
    message: str = ""
    max_clap_radius_m: float = _DEFAULT_MAX_RADIUS_M
    array_center_m: Tuple[float, ...] = field(default_factory=tuple)

    def bias_s(self, node_id: str) -> float:
        return self._estimate(node_id).bias_s

    def sigma_b_s(self, node_id: str) -> float:
        return self._estimate(node_id).sigma_b_s

    def _estimate(self, node_id: str) -> CalibrationEstimate:
        try:
            return self.biases[str(node_id)]
        except KeyError as exc:
            raise ClapCalibrationError("unknown node %r" % node_id) from exc

    def is_admissible(self, node_id: str, tolerance_s: float) -> bool:
        return self._estimate(node_id).sigma_b_s <= float(tolerance_s)

    def require_arrival(self, node_id: str, tolerance_s: float) -> CalibrationEstimate:
        estimate = self._estimate(node_id)
        if estimate.sigma_b_s > float(tolerance_s):
            raise CalibrationRefusal(
                "node %r clap calibration confidence %.1f us exceeds %.1f us"
                % (node_id, estimate.sigma_b_us, float(tolerance_s) * 1e6)
            )
        return estimate

    def as_dict(self) -> Dict[str, Any]:
        return {
            "converged": self.converged,
            "residual_rms_s": self.residual_rms_s,
            "sigma_residual_s": self.sigma_residual_s,
            "n_observations": self.n_observations,
            "n_parameters": self.n_parameters,
            "degrees_of_freedom": self.degrees_of_freedom,
            "biases_s": {k: v.bias_s for k, v in self.biases.items()},
            "sigma_b_s": {k: v.sigma_b_s for k, v in self.biases.items()},
            "claps": [
                {
                    "clap_id": c.clap_id,
                    "position_m": list(c.position_m),
                    "emission_time_s": c.emission_time_s,
                    "sigma_position_m": list(c.sigma_position_m),
                    "sigma_emission_time_s": c.sigma_emission_time_s,
                    "radius_from_center_m": c.radius_from_center_m,
                }
                for c in self.claps
            ],
        }


@dataclass(frozen=True)
class _ClapProblem:
    """Assembled least-squares problem: closures, layout and initial guess."""

    residuals: Any
    jacobian: Any
    unpack: Any
    theta0: np.ndarray
    bounds: Tuple[np.ndarray, np.ndarray]
    parameter_names: Tuple[str, ...]
    clap_ids: Tuple[str, ...]
    solved_nodes: Tuple[str, ...]
    clap_of: np.ndarray
    bias_of: np.ndarray
    n_obs: int
    n_params: int
    n_claps: int
    n_free: int
    free_axes: np.ndarray
    dimension: int
    t0_offset: int
    bias_offset: int
    time_origin: float
    center: np.ndarray
    radius: float


class ClapCalibrator:
    """Joint near-field clap position / emission time / node delay solver."""

    def __init__(
        self,
        nodes: Mapping[str, Sequence[float]],
        reference_nodes: Iterable[str] = ("node-ref",),
        *,
        reference_bias_s: Optional[Mapping[str, float]] = None,
        sound_speed_mps: float = _DEFAULT_SOUND_SPEED_MPS,
        max_clap_radius_m: float = _DEFAULT_MAX_RADIUS_M,
        clap_plane_z_m: Optional[float] = None,
        admissibility_tolerance_s: float = 30e-6,
    ) -> None:
        if not nodes:
            raise ClapCalibrationError("nodes must be non-empty")
        self.nodes = {str(k): np.asarray(v, dtype=float).ravel() for k, v in nodes.items()}
        shapes = {v.shape for v in self.nodes.values()}
        if len(shapes) != 1 or next(iter(shapes)) not in ((2,), (3,)):
            raise ClapCalibrationError("node positions must all be 2D or 3D coordinates")
        if any(not np.isfinite(v).all() for v in self.nodes.values()):
            raise ClapCalibrationError("node positions must be finite")
        self.dimension = int(next(iter(self.nodes.values())).size)

        self.reference_nodes = tuple(dict.fromkeys(str(v) for v in reference_nodes))
        if not self.reference_nodes or any(v not in self.nodes for v in self.reference_nodes):
            raise ClapCalibrationError("reference_nodes must identify nodes in nodes")
        self.reference_bias_s = {v: 0.0 for v in self.reference_nodes}
        if reference_bias_s is not None:
            for key, value in reference_bias_s.items():
                key = str(key)
                if key not in self.nodes:
                    raise ClapCalibrationError("reference_bias_s names unknown node %r" % key)
                self.reference_bias_s[key] = float(value)
        if any(not np.isfinite(v) for v in self.reference_bias_s.values()):
            raise ClapCalibrationError("reference biases must be finite")

        self.sound_speed_mps = float(sound_speed_mps)
        if not np.isfinite(self.sound_speed_mps) or self.sound_speed_mps <= 0:
            raise ClapCalibrationError("sound speed must be finite and positive")
        self.max_clap_radius_m = float(max_clap_radius_m)
        if not np.isfinite(self.max_clap_radius_m) or self.max_clap_radius_m <= 0:
            raise ClapCalibrationError("max clap radius must be finite and positive")
        if clap_plane_z_m is None:
            self.clap_plane_z_m: Optional[float] = None
        else:
            self.clap_plane_z_m = float(clap_plane_z_m)
            if not np.isfinite(self.clap_plane_z_m):
                raise ClapCalibrationError("clap plane height must be finite")
        self.admissibility_tolerance_s = float(admissibility_tolerance_s)
        if not np.isfinite(self.admissibility_tolerance_s) or self.admissibility_tolerance_s <= 0:
            raise ClapCalibrationError("admissibility tolerance must be finite and positive")

        self.observations: list[ClapObservation] = []

    # -- geometry helpers -------------------------------------------------
    @property
    def array_center_m(self) -> np.ndarray:
        return np.mean(np.stack([self.nodes[k] for k in sorted(self.nodes)]), axis=0)

    @property
    def solved_node_ids(self) -> Tuple[str, ...]:
        return tuple(k for k in sorted(self.nodes) if k not in self.reference_bias_s)

    # -- ingest -----------------------------------------------------------
    def ingest(self, observations: Iterable[ClapObservation | Mapping[str, Any]]) -> None:
        for observation in observations:
            if not isinstance(observation, ClapObservation):
                observation = ClapObservation.from_mapping(observation)
            if observation.node_id not in self.nodes:
                raise ClapCalibrationError("observation names unknown node %r" % observation.node_id)
            if not np.isfinite(observation.timestamp_s):
                raise ClapCalibrationError("observation timestamp must be finite")
            self.observations.append(observation)

    def simulate_claps(
        self,
        clap_positions: Sequence[Sequence[float]],
        *,
        emission_times_s: Optional[Sequence[float]] = None,
        capture_biases_s: Optional[Mapping[str, float]] = None,
        timing_noise_s: float = 0.0,
        seed: int = 0,
        ingest: bool = True,
    ) -> list[ClapObservation]:
        """Render synthetic clap arrival timestamps for tests and rehearsals."""
        positions = [np.asarray(v, dtype=float).ravel() for v in clap_positions]
        if not positions or any(v.size != self.dimension or not np.isfinite(v).all() for v in positions):
            raise ClapCalibrationError("clap positions must match node dimensions and be finite")
        if emission_times_s is None:
            emission_times_s = [0.0] * len(positions)
        if len(emission_times_s) != len(positions):
            raise ClapCalibrationError("emission_times_s must match clap_positions")
        noise = float(timing_noise_s)
        if not np.isfinite(noise) or noise < 0:
            raise ClapCalibrationError("timing noise must be finite and non-negative")
        biases = {k: 0.0 for k in self.nodes}
        biases.update(self.reference_bias_s)
        if capture_biases_s is not None:
            for key, value in capture_biases_s.items():
                key = str(key)
                if key not in self.nodes:
                    raise ClapCalibrationError("capture_biases_s names unknown node %r" % key)
                biases[key] = float(value)
        if any(not np.isfinite(v) for v in biases.values()):
            raise ClapCalibrationError("capture biases must be finite")

        rng = np.random.default_rng(seed)
        rendered: list[ClapObservation] = []
        for index, (position, emission_time) in enumerate(zip(positions, emission_times_s)):
            emission_time = float(emission_time)
            if not np.isfinite(emission_time):
                raise ClapCalibrationError("emission times must be finite")
            for node_id in sorted(self.nodes):
                delay = float(np.linalg.norm(self.nodes[node_id] - position)) / self.sound_speed_mps
                jitter = float(rng.normal(0.0, noise)) if noise else 0.0
                rendered.append(
                    ClapObservation("clap-%d" % index, node_id, emission_time + delay + biases[node_id] + jitter)
                )
        if ingest:
            self.ingest(rendered)
        return rendered

    # -- solver -----------------------------------------------------------
    def _grouped(self) -> Dict[str, Dict[str, float]]:
        grouped: Dict[str, Dict[str, float]] = {}
        for observation in self.observations:
            bucket = grouped.setdefault(observation.clap_id, {})
            if observation.node_id in bucket:
                raise ClapCalibrationError(
                    "duplicate observation for clap %r node %r" % (observation.clap_id, observation.node_id)
                )
            bucket[observation.node_id] = observation.timestamp_s
        return grouped

    def build_problem(self) -> "_ClapProblem":
        """Assemble the residual/Jacobian closures and the initial parameter guess."""
        grouped = self._grouped()
        clap_ids = sorted(grouped)
        if not clap_ids:
            raise ClapCalibrationError("no clap observations ingested")

        solved_nodes = self.solved_node_ids
        dim = self.dimension
        center = self.array_center_m
        radius = self.max_clap_radius_m
        speed = self.sound_speed_mps

        # A pinned clap height removes one unknown per clap, which is what makes
        # four-node arrays identifiable at all (see _identifiability docstring).
        base_position = np.zeros(dim)
        if self.clap_plane_z_m is None:
            free_axes = np.arange(dim)
        else:
            base_position[dim - 1] = self.clap_plane_z_m
            free_axes = np.arange(dim - 1)
        n_free = int(free_axes.size)

        node_index = {node_id: i for i, node_id in enumerate(solved_nodes)}
        rows: list[Tuple[int, np.ndarray, float, int]] = []  # clap idx, node xyz, timestamp, bias idx (-1 fixed)
        for k, clap_id in enumerate(clap_ids):
            members = grouped[clap_id]
            if len(members) < 2:
                raise ClapCalibrationError("clap %r was heard by fewer than two nodes" % clap_id)
            for node_id in sorted(members):
                bias_slot = node_index.get(node_id, -1)
                known = self.reference_bias_s.get(node_id, 0.0)
                rows.append((k, self.nodes[node_id], float(members[node_id]) - known, bias_slot))

        n_claps = len(clap_ids)
        n_params = n_claps * (n_free + 1) + len(solved_nodes)
        n_obs = len(rows)
        if n_obs <= n_params:
            raise ClapCalibrationError(
                "under-determined clap calibration: %d observations for %d parameters; "
                "with N nodes and K claps the joint solve needs K*(N - %d) > N - %d "
                "(more nodes, more claps, or a pinned clap_plane_z_m)"
                % (n_obs, n_params, n_free + 1, len(self.reference_bias_s))
            )
        if not any(row[3] >= 0 for row in rows) and solved_nodes:
            raise ClapCalibrationError("no observations for the uncalibrated nodes")

        clap_of = np.asarray([row[0] for row in rows], dtype=int)
        node_xyz = np.stack([row[1] for row in rows]) if rows else np.zeros((0, dim))
        # internal time origin keeps the emission-time parameters near zero
        raw_times = np.asarray([row[2] for row in rows], dtype=float)
        time_origin = float(raw_times.min())
        times = raw_times - time_origin
        bias_of = np.asarray([row[3] for row in rows], dtype=int)
        has_bias = bias_of >= 0
        bias_cols = np.where(has_bias, bias_of, 0)

        pos_slice = slice(0, n_claps * n_free)
        t0_slice = slice(n_claps * n_free, n_claps * (n_free + 1))
        bias_slice = slice(n_claps * (n_free + 1), n_params)
        t0_offset = n_claps * n_free
        bias_offset = n_claps * (n_free + 1)

        def unpack(theta: np.ndarray):
            positions = np.tile(base_position, (n_claps, 1))
            if n_free:
                positions[:, free_axes] = theta[pos_slice].reshape(n_claps, n_free)
            t0 = theta[t0_slice]
            biases = theta[bias_slice]
            return positions, t0, biases

        def residuals(theta: np.ndarray) -> np.ndarray:
            positions, t0, biases = unpack(theta)
            delta = positions[clap_of] - node_xyz
            distance = np.linalg.norm(delta, axis=1)
            bias_terms = np.where(has_bias, biases[bias_cols] if biases.size else 0.0, 0.0)
            # residuals expressed in metres (seconds * c) for conditioning
            data = speed * (times - bias_terms - t0[clap_of]) - distance
            offsets = positions - center
            excess = np.linalg.norm(offsets, axis=1) - radius
            penalty = _RADIUS_PENALTY_WEIGHT * np.maximum(excess, 0.0)
            return np.concatenate([data, penalty])

        def jacobian(theta: np.ndarray) -> np.ndarray:
            positions, _t0, biases = unpack(theta)
            jac = np.zeros((n_obs + n_claps, n_params))
            delta = positions[clap_of] - node_xyz
            distance = np.linalg.norm(delta, axis=1)
            unit = delta / np.where(distance[:, None] > 0, distance[:, None], 1.0)
            for row in range(n_obs):
                k = clap_of[row]
                jac[row, k * n_free:(k + 1) * n_free] = -unit[row][free_axes]
                jac[row, t0_offset + k] = -speed
                if has_bias[row]:
                    jac[row, bias_offset + bias_cols[row]] = -speed
            offsets = positions - center
            norms = np.linalg.norm(offsets, axis=1)
            for k in range(n_claps):
                if norms[k] > radius and norms[k] > 0:
                    jac[n_obs + k, k * n_free:(k + 1) * n_free] = (
                        _RADIUS_PENALTY_WEIGHT * offsets[k][free_axes] / norms[k]
                    )
            return jac

        theta0 = np.zeros(n_params)
        lower = np.full(n_params, -np.inf)
        upper = np.full(n_params, np.inf)
        for k in range(n_claps):
            seed_position = center[free_axes] + (1e-3 if k % 2 == 0 else -1e-3) * (k + 1)
            theta0[k * n_free:(k + 1) * n_free] = seed_position
            lower[k * n_free:(k + 1) * n_free] = center[free_axes] - radius
            upper[k * n_free:(k + 1) * n_free] = center[free_axes] + radius
            mask = clap_of == k
            nominal = np.linalg.norm(node_xyz[mask] - center, axis=1) / speed
            theta0[t0_offset + k] = float(np.mean(times[mask] - nominal))

        names: list[str] = []
        axis_labels = tuple(("x", "y", "z")[a] for a in free_axes)
        for clap_id in clap_ids:
            names.extend("%s.%s" % (clap_id, axis) for axis in axis_labels)
        names.extend("%s.t0" % clap_id for clap_id in clap_ids)
        names.extend("%s.bias" % node_id for node_id in solved_nodes)

        return _ClapProblem(
            residuals=residuals,
            jacobian=jacobian,
            unpack=unpack,
            theta0=theta0,
            bounds=(lower, upper),
            parameter_names=tuple(names),
            clap_ids=tuple(clap_ids),
            solved_nodes=tuple(solved_nodes),
            clap_of=clap_of,
            bias_of=bias_of,
            n_obs=n_obs,
            n_params=n_params,
            n_claps=n_claps,
            n_free=n_free,
            free_axes=free_axes,
            dimension=dim,
            t0_offset=t0_offset,
            bias_offset=bias_offset,
            time_origin=time_origin,
            center=center,
            radius=radius,
        )

    def solve(self, *, max_nfev: int = 400, xtol: float = 1e-14, ftol: float = 1e-14) -> ClapCalibrationResult:
        if _scipy_least_squares is None:  # pragma: no cover
            raise ClapCalibrationError("scipy is required for the clap calibration solver")
        problem = self.build_problem()
        residuals = problem.residuals
        jacobian = problem.jacobian
        unpack = problem.unpack
        speed = self.sound_speed_mps
        center = problem.center
        radius = problem.radius
        dim = problem.dimension
        n_free = problem.n_free
        free_axes = problem.free_axes
        n_claps = problem.n_claps
        n_obs = problem.n_obs
        n_params = problem.n_params
        clap_ids = list(problem.clap_ids)
        solved_nodes = problem.solved_nodes
        clap_of = problem.clap_of
        bias_of = problem.bias_of
        t0_offset = problem.t0_offset
        bias_offset = problem.bias_offset
        time_origin = problem.time_origin
        lower, upper = problem.bounds

        result = _scipy_least_squares(
            residuals,
            problem.theta0,
            jac=jacobian,
            bounds=(lower, upper),
            method="trf",
            max_nfev=int(max_nfev),
            xtol=float(xtol),
            ftol=float(ftol),
            gtol=1e-14,
        )

        theta = np.asarray(result.x, dtype=float)
        positions, t0, biases = unpack(theta)
        data_residuals_m = residuals(theta)[:n_obs]
        data_residuals_s = data_residuals_m / speed
        dof = max(n_obs - n_params, 1)
        sigma_residual_s = float(np.sqrt(np.sum(data_residuals_s ** 2) / dof))
        residual_rms_s = float(np.sqrt(np.mean(data_residuals_s ** 2)))

        jac = jacobian(theta)
        # residuals are metres, so sigma is scaled by c; the resulting covariance is
        # already in native parameter units (m^2 for positions, s^2 for times/biases)
        covariance = self._covariance(jac, sigma_residual_s * speed)
        sigma_theta = np.sqrt(np.clip(np.diag(covariance), 0.0, np.inf))

        estimates: Dict[str, CalibrationEstimate] = {}
        for node_id, bias in self.reference_bias_s.items():
            estimates[node_id] = CalibrationEstimate(node_id, float(bias), 0.0, 0.0, 0)
        for i, node_id in enumerate(solved_nodes):
            column = bias_offset + i
            count = int(np.sum(bias_of == i))
            estimates[node_id] = CalibrationEstimate(
                node_id,
                float(biases[i]),
                float(sigma_theta[column]),
                residual_rms_s,
                count,
            )

        claps: list[ClapSource] = []
        for k, clap_id in enumerate(clap_ids):
            claps.append(
                ClapSource(
                    clap_id,
                    tuple(float(v) for v in positions[k]),
                    float(t0[k] + time_origin),
                    tuple(
                        float(sigma_theta[k * n_free + int(np.where(free_axes == d)[0][0])])
                        if d in free_axes
                        else 0.0
                        for d in range(dim)
                    ),
                    float(sigma_theta[t0_offset + k]),
                    int(np.sum(clap_of == k)),
                    float(np.linalg.norm(positions[k] - center)),
                )
            )

        return ClapCalibrationResult(
            biases=estimates,
            claps=tuple(claps),
            residual_rms_s=residual_rms_s,
            sigma_residual_s=sigma_residual_s,
            covariance=covariance,
            parameter_names=problem.parameter_names,
            n_observations=n_obs,
            n_parameters=n_params,
            degrees_of_freedom=n_obs - n_params,
            converged=bool(result.success),
            cost=float(result.cost),
            message=str(getattr(result, "message", "")),
            max_clap_radius_m=radius,
            array_center_m=tuple(float(v) for v in center),
        )

    @staticmethod
    def _covariance(jac: np.ndarray, sigma_residual: float) -> np.ndarray:
        """Gauss-Newton covariance ``sigma^2 (J^T J)^-1`` with a pseudo-inverse fallback."""
        jtj = jac.T @ jac
        try:
            inverse = np.linalg.inv(jtj)
            if not np.isfinite(inverse).all():
                raise np.linalg.LinAlgError("non-finite inverse")
        except np.linalg.LinAlgError:
            inverse = np.linalg.pinv(jtj, rcond=1e-12)
        return float(sigma_residual) ** 2 * inverse

    def calibrate(self, **kwargs: Any) -> ClapCalibrationResult:
        return self.solve(**kwargs)
