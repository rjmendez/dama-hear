"""Gold's identity states only what is known before first power-on."""
from hear import nodeidentity as ni


def test_gold_has_a_named_reusable_hardware_profile():
    gold = ni.get("gold")
    assert gold.display_name == "Gold"
    assert gold.hardware_profile == "esp32s3-i2s-gps"
    assert "Robert Gold" in gold.naming_provenance
    assert "Gold codes" in gold.naming_provenance


def test_gold_states_only_the_known_assembled_hardware():
    assert ni.GOLD.known_hardware == {
        "esp32-s3",
        "external-mono-i2s-microphone",
        "gps-uart",
        "gps-1pps",
        "onboard-addressable-rgb-led-gpio48",
    }


def test_gold_does_not_assume_optional_peripherals():
    assert ni.GOLD.disabled_optional_hardware == {"lora", "bme", "sd", "i2c"}


def test_gold_is_not_tdoa_eligible_before_measurement():
    gold = ni.GOLD
    assert not gold.pps_observed
    assert not gold.audio_path_calibrated
    assert not gold.timing_path_measured
    assert not gold.tdoa_eligible()


def test_gold_is_reference_one_not_proof_for_the_next_two():
    gold = ni.GOLD
    assert gold.deployment_role == "first_reference_node"
    assert gold.intended_batch_size == 3
    assert gold.hardware_revision is None
    assert gold.pin_map_evidence is None
    assert not gold.replication_eligible()


def test_gold_identity_contains_no_fabricated_unique_identifiers_or_part_models():
    text = repr(ni.GOLD).lower()
    for unsupported in ("serial_port", "mac_address", "gps_model", "microphone_model"):
        assert unsupported not in text
