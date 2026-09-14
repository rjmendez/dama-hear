"""Tests for nine-node acoustic fleet topology, event contract, and cross-profile correlation safety."""

import pytest
from hear import nodeidentity as ni
from hear import nodeclass as nc
from hear import event_contract as ec
from hear.backend import associate as AS
from hear.backend import survey as SV


class TestFleetTopologyWithoutDoubleCounting:
    def test_dama_acoustic_node_count_is_exactly_nine(self):
        nodes = ni.dama_acoustic_nodes()
        assert len(nodes) == 9
        node_ids = {n.node_id for n in nodes}
        expected_nine = {
            "gold", "kasami", "ageev",
            "nyquist", "mach", "rankine",
            "phone-1", "phone-2", "phone-3",
        }
        assert node_ids == expected_nine

    def test_hugbot_is_tenth_device_and_corroborator_only(self):
        hugbot = ni.get("hugbot")
        assert hugbot.node_id == "hugbot"
        assert hugbot.corroboration_only is True
        assert not hugbot.tdoa_eligible()
        # Ensure Hugbot is NOT in dama_acoustic_nodes
        dama_ids = {n.node_id for n in ni.dama_acoustic_nodes()}
        assert "hugbot" not in dama_ids

    def test_fleet_summary_reflects_role_breakdown(self):
        summary = ni.fleet_summary()
        assert summary["dama_acoustic_node_count"] == 9
        assert summary["corroborator_node_count"] == 1
        assert summary["total_device_count"] == 10
        assert summary["active_tdoa_receivers"] == 3  # Nyquist, Mach, Rankine
        assert summary["pending_reference_units"] == 3  # Gold, Kasami, Ageev
        assert summary["non_tdoa_acoustic_participants"] == 3  # Phone 1, 2, 3
        assert summary["corroborators"] == ["hugbot"]


class TestNodeProfilesAndRoleDistinctions:
    def test_gold_kasami_ageev_reference_batch_roles(self):
        gold = ni.get("gold")
        kasami = ni.get("kasami")
        ageev = ni.get("ageev")

        assert gold.hardware_profile == "esp32s3-i2s-gps"
        assert kasami.hardware_profile == "esp32s3-i2s-gps"
        assert ageev.hardware_profile == "esp32s3-i2s-gps"

        assert gold.deployment_role == "first_reference_node"
        assert kasami.deployment_role == "reference_batch_unit_2"
        assert ageev.deployment_role == "reference_batch_unit_3"

        # Unmeasured reference units are not TDoA eligible before calibration
        assert not gold.tdoa_eligible()
        assert not kasami.tdoa_eligible()
        assert not ageev.tdoa_eligible()

    def test_deployed_monitoring_nodes_are_tdoa_eligible(self):
        for name in ("nyquist", "mach", "rankine"):
            node = ni.get(name)
            assert node.hardware_profile == "xiao-s3-pps"
            assert node.deployment_role == "deployed_monitoring_node"
            assert node.tdoa_eligible() is True

    def test_dama_phones_are_acoustic_participants_but_not_tdoa_receivers(self):
        for name in ("phone-1", "phone-2", "phone-3"):
            phone = ni.get(name)
            assert phone.hardware_profile == "gotchi-phone"
            assert not phone.tdoa_eligible()
            n_cls = nc.get(phone.hardware_profile)
            assert not n_cls.contributes_arrival()

    def test_nodeclasses_registered_and_consistent(self):
        for cls_name in ("xiao-s3-pps", "esp32s3-i2s-gps", "gotchi-phone", "hugbot-corroborator"):
            ncls = nc.get(cls_name)
            assert ncls.name == cls_name
        
        # Hugbot and phone nodeclasses do NOT contribute TDoA arrivals
        assert not nc.get("gotchi-phone").contributes_arrival()
        assert not nc.get("hugbot-corroborator").contributes_arrival()
        assert nc.get("xiao-s3-pps").contributes_arrival()
        assert nc.get("esp32s3-i2s-gps").contributes_arrival()


class TestEventContractAndTDoAEligibility:
    def test_valid_nyquist_event_passes_tdoa_validation(self):
        evt = ec.AcousticEvent.create(node_id="nyquist", timestamp_s=1700000000.123456)
        assert evt.tdoa_capable is True
        assert ec.validate_event_for_tdoa(evt) is True

    def test_hugbot_event_is_refused_for_tdoa(self):
        evt = ec.AcousticEvent.create(node_id="hugbot", timestamp_s=1700000000.123456)
        assert evt.corroboration_only is True
        assert evt.tdoa_capable is False
        with pytest.raises(ec.EventContractError, match="corroboration_only"):
            ec.validate_event_for_tdoa(evt)

    def test_phone_event_is_refused_for_tdoa(self):
        evt = ec.AcousticEvent.create(node_id="phone-1", timestamp_s=1700000000.123456)
        assert evt.tdoa_capable is False
        assert evt.timestamp_domain == "android_boottime_unanchored"
        with pytest.raises(ec.EventContractError):
            ec.validate_event_for_tdoa(evt)

    def test_unmeasured_gold_reference_event_is_refused_for_tdoa(self):
        evt = ec.AcousticEvent.create(node_id="gold", timestamp_s=1700000000.123456)
        assert evt.tdoa_capable is False
        with pytest.raises(ec.EventContractError, match="not TDoA-eligible"):
            ec.validate_event_for_tdoa(evt)

    def test_non_gps_pps_timestamp_domain_is_refused(self):
        evt_dict = {
            "node_id": "nyquist",
            "node_class": "xiao-s3-pps",
            "timestamp_s": 1700000000.0,
            "timestamp_domain": "utc_ntp",
            "tdoa_capable": True,
        }
        with pytest.raises(ec.EventContractError, match="utc_ntp"):
            ec.validate_event_for_tdoa(evt_dict)

    def test_large_capture_path_bias_is_refused(self):
        evt_dict = {
            "node_id": "nyquist",
            "node_class": "xiao-s3-pps",
            "timestamp_s": 1700000000.0,
            "timestamp_domain": "utc_gps_pps",
            "tdoa_capable": True,
            "capture_path_bias_s": 0.050,  # 50 ms
        }
        with pytest.raises(ec.EventContractError, match="capture-path bias"):
            ec.validate_event_for_tdoa(evt_dict)


class TestCrossProfileCorrelationSafety:
    def test_safe_correlation_between_deployed_pps_nodes(self):
        evt_a = ec.AcousticEvent.create("nyquist", 1700000000.100)
        evt_b = ec.AcousticEvent.create("mach", 1700000000.105)
        ok, reason = ec.check_cross_profile_correlation_safety(evt_a.__dict__, evt_b.__dict__)
        assert ok is True
        assert reason == "ok"

    def test_unsafe_correlation_refused_when_phone_or_hugbot_included(self):
        evt_a = ec.AcousticEvent.create("nyquist", 1700000000.100)
        evt_phone = ec.AcousticEvent.create("phone-1", 1700000000.105)
        evt_hugbot = ec.AcousticEvent.create("hugbot", 1700000000.105)

        ok1, reason1 = ec.check_cross_profile_correlation_safety(evt_a.__dict__, evt_phone.__dict__)
        assert ok1 is False

        ok2, reason2 = ec.check_cross_profile_correlation_safety(evt_a.__dict__, evt_hugbot.__dict__)
        assert ok2 is False


class TestAssociationAndSurveyIntegration:
    def test_associate_arrival_is_usable_filters_ineligible_nodes(self):
        d_nyquist = {"node_id": "nyquist", "node_class": "xiao-s3-pps", "timestamp_domain": "utc_gps_pps"}
        d_hugbot = {"node_id": "hugbot", "corroboration_only": True}
        d_phone = {"node_id": "phone-1", "node_class": "gotchi-phone", "timestamp_domain": "android_boottime_unanchored"}

        assert AS.arrival_is_usable(d_nyquist) is True
        assert AS.arrival_is_usable(d_hugbot) is False
        assert AS.arrival_is_usable(d_phone) is False

    def test_survey_arrival_ids_excludes_corroborator_and_phone(self):
        positions = {
            1: [0.0, 0.0, 0.0],
            2: [10.0, 0.0, 0.0],
            3: [5.0, 10.0, 0.0],
            4: [2.0, 2.0, 0.0],
            5: [1.0, 1.0, 0.0],
        }
        names = {
            1: "nyquist",
            2: "mach",
            3: "rankine",
            4: "phone-1",
            5: "hugbot",
        }
        classes = {
            1: "xiao-s3-pps",
            2: "xiao-s3-pps",
            3: "xiao-s3-pps",
            4: "gotchi-phone",
            5: "hugbot-corroborator",
        }
        surv = SV.Survey(positions, names=names, classes=classes)
        arr_ids = surv.arrival_ids()
        assert 1 in arr_ids
        assert 2 in arr_ids
        assert 3 in arr_ids
        assert 4 not in arr_ids
        assert 5 not in arr_ids
