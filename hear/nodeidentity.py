"""Named physical-node identities, kept separate from reusable hardware profiles.

A hardware profile describes a wiring design. An identity describes one assembled unit and the
evidence that permits that unit to use the design. Keeping those separate prevents measurements
from the first board from silently becoming claims about later boards.

Fleet Topology (9 DAMA Acoustic Nodes + 1 Hugbot Corroborator):
- New ESP32-S3-WROOM reference batch (3 nodes): Gold, Kasami, Ageev
- Deployed monitoring nodes (3 nodes): Nyquist, Mach, Rankine
- DAMA handset participants (3 nodes): Phone 1, Phone 2, Phone 3
- Hugbot mobile corroborator (1 tenth device): Hugbot (corroboration-only)
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    display_name: str
    hardware_profile: str
    naming_provenance: str
    deployment_role: str
    intended_batch_size: int
    known_hardware: FrozenSet[str]
    disabled_optional_hardware: FrozenSet[str]
    hardware_evidence: str
    hardware_revision: Optional[str] = None
    pin_map_evidence: Optional[str] = None
    pps_observed: bool = False
    audio_path_calibrated: bool = False
    timing_path_measured: bool = False
    corroboration_only: bool = False

    def tdoa_eligible(self) -> bool:
        """Whether this physical unit has earned admission as a TDoA arrival source."""
        if self.corroboration_only:
            return False
        return self.pps_observed and self.audio_path_calibrated and self.timing_path_measured

    def replication_eligible(self) -> bool:
        """Whether this unit can be used as evidence for constructing matching copies."""
        return (
            self.hardware_revision is not None
            and self.pin_map_evidence is not None
            and self.pps_observed
            and self.audio_path_calibrated
        )


GOLD = NodeIdentity(
    node_id="gold",
    display_name="Gold",
    hardware_profile="esp32s3-i2s-gps",
    naming_provenance=(
        "Named for Robert Gold and Gold codes used in satellite communications."
    ),
    deployment_role="first_reference_node",
    intended_batch_size=3,
    known_hardware=frozenset({
        "esp32-s3",
        "external-mono-i2s-microphone",
        "gps-uart",
        "gps-1pps",
        "onboard-addressable-rgb-led-gpio48",
    }),
    disabled_optional_hardware=frozenset({"lora", "bme", "sd", "i2c"}),
    hardware_evidence=(
        "Operator-provided assembly: ESP32-S3-WROOM, Adafruit Ultimate GPS v3 (GPIO15/16 UART, GPIO4 PPS), "
        "ICS-43434 I2S mic (GPIO41/42/1), RGB GPIO48."
    ),
)

KASAMI = NodeIdentity(
    node_id="kasami",
    display_name="Kasami",
    hardware_profile="esp32s3-i2s-gps",
    naming_provenance=(
        "Named for Tadao Kasami and Kasami sequences used in CDMA cross-correlation."
    ),
    deployment_role="reference_batch_unit_2",
    intended_batch_size=3,
    known_hardware=frozenset({
        "esp32-s3",
        "external-mono-i2s-microphone",
        "gps-uart",
        "gps-1pps",
        "onboard-addressable-rgb-led-gpio48",
    }),
    disabled_optional_hardware=frozenset({"lora", "bme", "sd", "i2c"}),
    hardware_evidence=(
        "Operator-provided assembly matching Gold reference unit: ESP32-S3-WROOM, Adafruit Ultimate GPS v3, "
        "ICS-43434 I2S mic, GPS UART 15/16, PPS 4, I2S 41/42/1, RGB 48."
    ),
)

AGEEV = NodeIdentity(
    node_id="ageev",
    display_name="Ageev",
    hardware_profile="esp32s3-i2s-gps",
    naming_provenance=(
        "Named for Mikhail Ageev, pioneer in underwater autonomous vehicle acoustic navigation."
    ),
    deployment_role="reference_batch_unit_3",
    intended_batch_size=3,
    known_hardware=frozenset({
        "esp32-s3",
        "external-mono-i2s-microphone",
        "gps-uart",
        "gps-1pps",
        "onboard-addressable-rgb-led-gpio48",
    }),
    disabled_optional_hardware=frozenset({"lora", "bme", "sd", "i2c"}),
    hardware_evidence=(
        "Operator-provided assembly matching Gold reference unit: ESP32-S3-WROOM, Adafruit Ultimate GPS v3, "
        "ICS-43434 I2S mic, GPS UART 15/16, PPS 4, I2S 41/42/1, RGB 48."
    ),
)

NYQUIST = NodeIdentity(
    node_id="nyquist",
    display_name="Nyquist",
    hardware_profile="xiao-s3-pps",
    naming_provenance=(
        "Named for Harry Nyquist and the Nyquist-Shannon sampling theorem."
    ),
    deployment_role="deployed_monitoring_node",
    intended_batch_size=3,
    known_hardware=frozenset({
        "xiao-esp32-s3",
        "pdm-mems-mic",
        "gps-uart",
        "gps-1pps",
        "bmp280",
    }),
    disabled_optional_hardware=frozenset({"lora"}),
    hardware_evidence=(
        "Deployed monitoring node; bench and field telemetry captured since 2026-09-07."
    ),
    pps_observed=True,
    audio_path_calibrated=True,
    timing_path_measured=True,
)

MACH = NodeIdentity(
    node_id="mach",
    display_name="Mach",
    hardware_profile="xiao-s3-pps",
    naming_provenance=(
        "Named for Ernst Mach and Mach number."
    ),
    deployment_role="deployed_monitoring_node",
    intended_batch_size=3,
    known_hardware=frozenset({
        "xiao-esp32-s3",
        "pdm-mems-mic",
        "gps-uart",
        "gps-1pps",
        "bmp280",
    }),
    disabled_optional_hardware=frozenset({"lora"}),
    hardware_evidence=(
        "Deployed monitoring node; bench and field telemetry captured since 2026-09-07."
    ),
    pps_observed=True,
    audio_path_calibrated=True,
    timing_path_measured=True,
)

RANKINE = NodeIdentity(
    node_id="rankine",
    display_name="Rankine",
    hardware_profile="xiao-s3-pps",
    naming_provenance=(
        "Named for William John Macquorn Rankine and Rankine-Hugoniot shock relations."
    ),
    deployment_role="deployed_monitoring_node",
    intended_batch_size=3,
    known_hardware=frozenset({
        "xiao-esp32-s3",
        "pdm-mems-mic",
        "gps-uart",
        "gps-1pps",
        "bmp280",
    }),
    disabled_optional_hardware=frozenset({"lora"}),
    hardware_evidence=(
        "Deployed monitoring node; bench and field telemetry captured since 2026-09-07."
    ),
    pps_observed=True,
    audio_path_calibrated=True,
    timing_path_measured=True,
)

PHONE_1 = NodeIdentity(
    node_id="phone-1",
    display_name="DAMA Phone 1",
    hardware_profile="gotchi-phone",
    naming_provenance="DAMA handset participant 1 running dama-gotchi AcousticAntCollector.",
    deployment_role="mobile_acoustic_participant",
    intended_batch_size=3,
    known_hardware=frozenset({"android-handset", "internal-mic", "gps-location", "cellular-wifi"}),
    disabled_optional_hardware=frozenset({"gps-1pps-hardware-line"}),
    hardware_evidence="Mobile Android participant running dama-gotchi APK.",
    pps_observed=False,
    audio_path_calibrated=False,
    timing_path_measured=False,
)

PHONE_2 = NodeIdentity(
    node_id="phone-2",
    display_name="DAMA Phone 2",
    hardware_profile="gotchi-phone",
    naming_provenance="DAMA handset participant 2 running dama-gotchi AcousticAntCollector.",
    deployment_role="mobile_acoustic_participant",
    intended_batch_size=3,
    known_hardware=frozenset({"android-handset", "internal-mic", "gps-location", "cellular-wifi"}),
    disabled_optional_hardware=frozenset({"gps-1pps-hardware-line"}),
    hardware_evidence="Mobile Android participant running dama-gotchi APK.",
    pps_observed=False,
    audio_path_calibrated=False,
    timing_path_measured=False,
)

PHONE_3 = NodeIdentity(
    node_id="phone-3",
    display_name="DAMA Phone 3",
    hardware_profile="gotchi-phone",
    naming_provenance="DAMA handset participant 3 running dama-gotchi AcousticAntCollector.",
    deployment_role="mobile_acoustic_participant",
    intended_batch_size=3,
    known_hardware=frozenset({"android-handset", "internal-mic", "gps-location", "cellular-wifi"}),
    disabled_optional_hardware=frozenset({"gps-1pps-hardware-line"}),
    hardware_evidence="Mobile Android participant running dama-gotchi APK.",
    pps_observed=False,
    audio_path_calibrated=False,
    timing_path_measured=False,
)

HUGBOT = NodeIdentity(
    node_id="hugbot",
    display_name="Hugbot Corroborator",
    hardware_profile="hugbot-corroborator",
    naming_provenance="Named for Hugbot mobile robotic platform.",
    deployment_role="corroboration_only",
    intended_batch_size=1,
    known_hardware=frozenset({"robotic-platform", "acoustic-sensor", "host-mcu"}),
    disabled_optional_hardware=frozenset({"gps-1pps-hardware-line"}),
    hardware_evidence="External ground-truth / presence corroborator; arrival timing uncalibrated.",
    pps_observed=False,
    audio_path_calibrated=False,
    timing_path_measured=False,
    corroboration_only=True,
)


IDENTITIES: Dict[str, NodeIdentity] = {
    GOLD.node_id: GOLD,
    KASAMI.node_id: KASAMI,
    AGEEV.node_id: AGEEV,
    NYQUIST.node_id: NYQUIST,
    MACH.node_id: MACH,
    RANKINE.node_id: RANKINE,
    PHONE_1.node_id: PHONE_1,
    PHONE_2.node_id: PHONE_2,
    PHONE_3.node_id: PHONE_3,
    HUGBOT.node_id: HUGBOT,
}


def get(node_id: str) -> NodeIdentity:
    try:
        return IDENTITIES[node_id]
    except KeyError:
        raise KeyError("unknown node identity %r (have %s)"
                       % (node_id, ", ".join(sorted(IDENTITIES))))


def dama_acoustic_nodes() -> List[NodeIdentity]:
    """Return the 9 DAMA acoustic nodes (excluding Hugbot corroborator)."""
    return [
        GOLD, KASAMI, AGEEV,
        NYQUIST, MACH, RANKINE,
        PHONE_1, PHONE_2, PHONE_3,
    ]


def corroborator_nodes() -> List[NodeIdentity]:
    """Return corroboration-only nodes (Hugbot)."""
    return [HUGBOT]


def tdoa_capable_identities() -> List[NodeIdentity]:
    """Return node identities that are currently eligible as TDoA arrival sources."""
    return [i for i in IDENTITIES.values() if i.tdoa_eligible()]


def fleet_summary() -> Dict[str, object]:
    """Return structured overview of fleet topology, roles, and TDoA capability."""
    dama_nodes = dama_acoustic_nodes()
    corroborators = corroborator_nodes()
    return {
        "dama_acoustic_node_count": len(dama_nodes),
        "corroborator_node_count": len(corroborators),
        "total_device_count": len(dama_nodes) + len(corroborators),
        "active_tdoa_receivers": sum(1 for n in dama_nodes if n.tdoa_eligible()),
        "pending_reference_units": sum(1 for n in dama_nodes if n.hardware_profile == "esp32s3-i2s-gps" and not n.tdoa_eligible()),
        "non_tdoa_acoustic_participants": sum(1 for n in dama_nodes if n.hardware_profile == "gotchi-phone"),
        "corroborators": [c.node_id for c in corroborators],
    }
