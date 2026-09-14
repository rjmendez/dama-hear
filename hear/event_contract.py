"""Event identity, timestamp provenance, capture-path bias, and TDoA eligibility rules.

Contract requirements across the 9 DAMA Acoustic Nodes + Hugbot Corroborator:
- Event Identity (`event_id`): Unique event identifier.
- Timestamp Domain (`timestamp_domain`):
    "utc_gps_pps"                 — GPS 1PPS disciplined hardware timer (t_sigma ~ 100 us)
    "utc_ntp"                     — NTP network time (t_sigma ~ 3 ms)
    "android_boottime_unanchored" — Android system time without AudioRecord timestamp alignment
                                    (t_sigma ~ 25.1 ms, HAL bias ~ 70 ms)
    "local_host_rtc"              — Uncalibrated host RTC (t_sigma > 50 ms)
- Capture Path Bias (`capture_path_bias_s`):
    xiao-s3-pps:        62.5 us (1 I2S block @ 16 kHz)
    esp32s3-i2s-gps:    20.8 us (1 I2S block @ 48 kHz)
    gotchi-phone:       70.0 ms (HAL buffering + hop size)
    hugbot-corroborator: uncalibrated / variable
- TDoA Eligibility Rules:
    - Events with corroboration_only=True (Hugbot) are REFUSED for TDoA arrival solving.
    - Events with tdoa_capable=False are REFUSED for TDoA arrival solving.
    - Events from gotchi-phone or non-GPS-PPS classes are REFUSED for TDoA arrival solving.
    - Events from unmeasured reference units (Gold, Kasami, Ageev) are REFUSED for TDoA arrival solving until pps_observed and timing_path_measured are True.
"""
from dataclasses import dataclass, field
import uuid
from typing import Dict, Optional, Tuple, Any

from . import nodeclass, nodeidentity


class EventContractError(ValueError):
    """Refusal when an event violates cross-profile correlation safety or TDoA eligibility."""


@dataclass
class AcousticEvent:
    event_id: str
    node_id: str
    node_class: str
    timestamp_s: float
    timestamp_domain: str = "utc_gps_pps"
    t_sigma_s: float = 100e-6
    capture_path_bias_s: float = 0.0
    tdoa_capable: bool = True
    corroboration_only: bool = False
    modality: str = "audio"
    burst_kind: str = "impulse"
    confidence: Optional[float] = None
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, node_id: str, timestamp_s: float,
               burst_kind: str = "impulse",
               confidence: Optional[float] = None,
               event_id: Optional[str] = None) -> "AcousticEvent":
        """Construct an AcousticEvent auto-populating metadata from node identity and profile."""
        eid = event_id or f"evt_{uuid.uuid4().hex[:12]}"
        try:
            ident = nodeidentity.get(node_id)
            n_class = ident.hardware_profile
            corr_only = ident.corroboration_only
            tdoa_elig = ident.tdoa_eligible()
        except KeyError:
            n_class = "xiao-s3-pps"
            corr_only = False
            tdoa_elig = True

        try:
            ncls = nodeclass.get(n_class)
            ts_domain = "utc_gps_pps" if ncls.time_source == "gps_pps" else ("android_boottime_unanchored" if n_class == "gotchi-phone" else "utc_ntp")
            t_sigma = ncls.t_sigma_s
            contrib = ncls.contributes_arrival()
        except Exception:
            ts_domain = "utc_gps_pps"
            t_sigma = 100e-6
            contrib = True

        bias_map = {
            "xiao-s3-pps": 62.5e-6,
            "esp32s3-i2s-gps": 20.8e-6,
            "gotchi-phone": 70.0e-3,
            "hugbot-corroborator": 100.0e-3,
        }
        bias_s = bias_map.get(n_class, 0.0)

        tdoa_cap = contrib and tdoa_elig and not corr_only

        return cls(
            event_id=eid,
            node_id=node_id,
            node_class=n_class,
            timestamp_s=float(timestamp_s),
            timestamp_domain=ts_domain,
            t_sigma_s=t_sigma,
            capture_path_bias_s=bias_s,
            tdoa_capable=tdoa_cap,
            corroboration_only=corr_only,
            modality="audio",
            burst_kind=burst_kind,
            confidence=confidence,
        )


def validate_event_for_tdoa(event: Dict[str, Any] | AcousticEvent) -> bool:
    """Validate that an event is eligible as an input to a TDoA arrival solver.

    Raises EventContractError if the event or source node is ineligible.
    """
    if isinstance(event, AcousticEvent):
        d = {
            "event_id": event.event_id,
            "node_id": event.node_id,
            "node_class": event.node_class,
            "timestamp_domain": event.timestamp_domain,
            "tdoa_capable": event.tdoa_capable,
            "corroboration_only": event.corroboration_only,
            "t_sigma_s": event.t_sigma_s,
            "capture_path_bias_s": event.capture_path_bias_s,
        }
    else:
        d = dict(event)

    node_id = str(d.get("node_id") or d.get("node") or "unknown")

    # Rule 1: Corroboration-only devices (e.g. Hugbot) are strictly refused
    if d.get("corroboration_only") or d.get("deployment_role") == "corroboration_only":
        raise EventContractError(
            f"Event from node {node_id!r} has corroboration_only=True; refused for TDoA arrival solving."
        )

    # Check node identity if known
    try:
        ident = nodeidentity.get(node_id)
        if ident.corroboration_only:
            raise EventContractError(
                f"Node {node_id!r} is a corroboration-only device (Hugbot); refused for TDoA arrival solving."
            )
        if not ident.tdoa_eligible():
            raise EventContractError(
                f"Node identity {node_id!r} is not TDoA-eligible (pps_observed={ident.pps_observed}, "
                f"audio_path_calibrated={ident.audio_path_calibrated}, timing_path_measured={ident.timing_path_measured})."
            )
    except KeyError:
        pass

    # Rule 2: Event tdoa_capable flag
    if d.get("tdoa_capable") is False:
        evt_id = d.get("event_id")
        raise EventContractError(
            f"Event {evt_id!r} from node {node_id!r} has tdoa_capable=False."
        )

    # Rule 3: Timestamp domain must be GPS PPS disciplined
    domain = d.get("timestamp_domain", "utc_gps_pps")
    if domain != "utc_gps_pps":
        raise EventContractError(
            f"Event from node {node_id!r} carries timestamp domain {domain!r} (expected 'utc_gps_pps')."
        )

    # Rule 4: NodeClass must contribute arrival
    n_cls = d.get("node_class")
    if n_cls:
        try:
            cls = nodeclass.get(str(n_cls))
            if not cls.contributes_arrival():
                raise EventContractError(
                    f"Node class {n_cls!r} time source is {cls.time_source!r}, not 'gps_pps'; refused for TDoA."
                )
        except nodeclass.CapabilityError as e:
            raise EventContractError(str(e))

    # Rule 5: Capture-path bias check (> 1 ms uncorrected bias is disqualified)
    bias = float(d.get("capture_path_bias_s", 0.0))
    if bias > 1.0e-3:
        raise EventContractError(
            f"Node {node_id!r} capture-path bias ({bias * 1e3:.1f} ms) exceeds TDoA tolerance."
        )

    return True


def check_cross_profile_correlation_safety(event_a: Dict[str, Any],
                                            event_b: Dict[str, Any]) -> Tuple[bool, str]:
    """Check whether two events from different node profiles can be safely correlated for TDoA."""
    try:
        validate_event_for_tdoa(event_a)
        validate_event_for_tdoa(event_b)
    except EventContractError as err:
        return False, str(err)

    # Compare uncertainties
    t_sig_a = float(event_a.get("t_sigma_s", 100e-6))
    t_sig_b = float(event_b.get("t_sigma_s", 100e-6))

    if max(t_sig_a, t_sig_b) > 1.0e-3:
        return False, f"Timing uncertainty ratio too high ({t_sig_a*1e3:.1f} ms vs {t_sig_b*1e3:.1f} ms)"

    return True, "ok"
