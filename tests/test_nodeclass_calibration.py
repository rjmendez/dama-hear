#!/usr/bin/env python3
"""Loading a calibrated capture-path bias into the nodeclass registry.

`esp32s3-speaker` and `esp32s3-box3` are registered with `path_bias_s=None` -- their capture
path has NEVER BEEN MEASURED, and `require_arrival()` refuses them for exactly that reason. These
tests prove `apply_calibration()` / `load_calibrated_biases()` are the only sanctioned way to move
that field, that they enforce the same legality rules `NodeClass.__init__` does, that they apply a
separate CONFIDENCE gate (`sigma_b_s`) on top of the class's own bias-magnitude gate, and that a
correctly-calibrated bias flips `require_arrival()` from a "NEVER BEEN MEASURED" refusal to an
admitted `NodeClass`.
"""
import json
import math

import pytest

from hear import nodeclass as nc


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _restore_bias_state():
    """Every test here mutates `path_bias_s` on shared, module-level `CLASSES` singletons.
    Snapshot and restore so tests do not leak calibration state into each other or into the rest
    of the suite."""
    before = {name: cls.path_bias_s for name, cls in nc.CLASSES.items()}
    yield
    for name, cls in nc.CLASSES.items():
        cls.path_bias_s = before[name]


# ---------------------------------------------------------------- starting state
def test_speaker_and_box3_are_registered_and_unmeasured():
    """Both classes exist in the registry (this is the precondition for calibration to have
    anything to update) and both start with a capture path that has never been measured."""
    for name in ("esp32s3-speaker", "esp32s3-box3"):
        cls = nc.get(name)
        assert cls.path_bias_s is None
        assert not cls.contributes_arrival()


def test_require_arrival_refuses_uncalibrated_speaker_and_box3():
    """The exact transition named in the task: before calibration, require_arrival() raises
    CapabilityError naming the capture path as NEVER BEEN MEASURED, for both classes."""
    for name in ("esp32s3-speaker", "esp32s3-box3"):
        with pytest.raises(nc.CapabilityError, match="NEVER BEEN MEASURED"):
            nc.require_arrival(name)


# ---------------------------------------------------------------- apply_calibration()
def test_apply_calibration_admits_a_confidently_measured_bias():
    """A bias measured with sigma_b_s inside the 30 us confidence gate, and itself inside the
    class bias-magnitude bound, is applied and require_arrival() now admits the class."""
    updated = nc.apply_calibration("esp32s3-speaker", 40e-6, 12e-6)
    assert updated.path_bias_s == pytest.approx(40e-6)

    cls = nc.require_arrival("esp32s3-speaker")
    assert cls is nc.CLASSES["esp32s3-speaker"]
    assert cls.path_bias_s == pytest.approx(40e-6)


def test_apply_calibration_admits_box3_too():
    updated = nc.apply_calibration("esp32s3-box3", -35e-6, 8e-6)
    # path_bias_s is stored as a magnitude -- see NodeClass.__init__ / the module docstring.
    assert updated.path_bias_s == pytest.approx(35e-6)
    assert nc.require_arrival("esp32s3-box3") is nc.CLASSES["esp32s3-box3"]


def test_apply_calibration_takes_the_magnitude_of_a_signed_bias():
    """A calibration solve reports a signed offset; the stored field is a magnitude, exactly as
    every hand-written registration in the module already treats it."""
    updated = nc.apply_calibration("esp32s3-box3", -35e-6, 8e-6)
    assert updated.path_bias_s > 0.0


def test_apply_calibration_refuses_a_low_confidence_measurement():
    """sigma_b_s over the 30 us confidence gate is refused even though the bias VALUE would
    otherwise be legal and small enough to admit -- the two gates are independent."""
    with pytest.raises(nc.CapabilityError, match="confidence gate"):
        nc.apply_calibration("esp32s3-speaker", 40e-6, 45e-6)
    # And the class is untouched: still unmeasured, still refused.
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s is None
    with pytest.raises(nc.CapabilityError, match="NEVER BEEN MEASURED"):
        nc.require_arrival("esp32s3-speaker")


def test_apply_calibration_custom_sigma_max_is_honoured():
    """The confidence gate is overridable per call, not just the module default."""
    nc.apply_calibration("esp32s3-speaker", 40e-6, 45e-6, sigma_max_s=50e-6)
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s == pytest.approx(40e-6)


def test_apply_calibration_measured_but_still_too_biased_is_not_a_lie():
    """A trustworthy (low sigma_b_s) measurement of a bias that is itself over
    ARRIVAL_PATH_BIAS_MAX_S is applied -- apply_calibration only judges confidence, not the
    class-level bias bound -- and require_arrival() still refuses the class, but for the BIAS
    magnitude now, not for it being unmeasured."""
    nc.apply_calibration("esp32s3-speaker", 500e-6, 5e-6)
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s == pytest.approx(500e-6)
    with pytest.raises(nc.CapabilityError) as excinfo:
        nc.require_arrival("esp32s3-speaker")
    assert "NEVER BEEN MEASURED" not in str(excinfo.value)
    assert "exceeds the" in str(excinfo.value)


@pytest.mark.parametrize("bad_sigma", [-1e-6, math.nan, math.inf])
def test_apply_calibration_rejects_illegal_sigma(bad_sigma):
    with pytest.raises(nc.CapabilityError):
        nc.apply_calibration("esp32s3-speaker", 40e-6, bad_sigma)


@pytest.mark.parametrize("bad_bias", [0.0, math.nan, math.inf, None])
def test_apply_calibration_rejects_illegal_bias(bad_bias):
    with pytest.raises(nc.CapabilityError):
        nc.apply_calibration("esp32s3-speaker", bad_bias, 10e-6)


def test_apply_calibration_unknown_class_raises():
    with pytest.raises(nc.CapabilityError, match="unknown node class"):
        nc.apply_calibration("not-a-real-class", 40e-6, 10e-6)


# ---------------------------------------------------------------- load_calibrated_biases()
def _write(tmp_path, payload):
    path = tmp_path / "calibrated_node_biases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_load_calibrated_biases_missing_file_is_a_noop(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert nc.load_calibrated_biases(path) == []
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s is None


def test_load_calibrated_biases_applies_admissible_confident_entries(tmp_path):
    payload = {
        "schema": "hear.calibrated_node_biases.v1",
        "reference_nodes": ["xiao-s3-pps"],
        "admissibility_tolerance_s": 30e-6,
        "nodes": {
            "esp32s3-speaker": {
                "path_bias_s": 40e-6, "sigma_b_s": 12e-6, "status": "admissible",
            },
            "esp32s3-box3": {
                "path_bias_s": -35e-6, "sigma_b_s": 8e-6, "status": "admissible",
            },
        },
    }
    path = _write(tmp_path, payload)

    applied = nc.load_calibrated_biases(path)

    assert applied == ["esp32s3-box3", "esp32s3-speaker"]
    assert nc.require_arrival("esp32s3-speaker") is nc.CLASSES["esp32s3-speaker"]
    assert nc.require_arrival("esp32s3-box3") is nc.CLASSES["esp32s3-box3"]
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s == pytest.approx(40e-6)
    assert nc.CLASSES["esp32s3-box3"].path_bias_s == pytest.approx(35e-6)


def test_load_calibrated_biases_skips_unregistered_node_ids(tmp_path):
    """A calibration run can include nodes (e.g. a bench-only reference) with no registered
    NodeClass at all. Those entries are ignored rather than raising."""
    payload = {
        "nodes": {
            "some-bench-only-reference": {
                "path_bias_s": 10e-6, "sigma_b_s": 5e-6, "status": "admissible",
            },
        },
    }
    path = _write(tmp_path, payload)
    assert nc.load_calibrated_biases(path) == []


def test_load_calibrated_biases_skips_refused_status(tmp_path):
    payload = {
        "nodes": {
            "esp32s3-speaker": {
                "path_bias_s": 40e-6, "sigma_b_s": 12e-6, "status": "refused",
            },
        },
    }
    path = _write(tmp_path, payload)
    assert nc.load_calibrated_biases(path) == []
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s is None


def test_load_calibrated_biases_skips_low_confidence_entries(tmp_path):
    """A node whose calibration solve did not converge tightly enough (sigma_b_s over the 30 us
    gate) is skipped even if its status says admissible -- the confidence gate here is stricter
    than, and independent of, calibrate_claps.py's own tolerance gate."""
    payload = {
        "nodes": {
            "esp32s3-speaker": {
                "path_bias_s": 40e-6, "sigma_b_s": 45e-6, "status": "admissible",
            },
        },
    }
    path = _write(tmp_path, payload)
    assert nc.load_calibrated_biases(path) == []
    assert nc.CLASSES["esp32s3-speaker"].path_bias_s is None
    with pytest.raises(nc.CapabilityError, match="NEVER BEEN MEASURED"):
        nc.require_arrival("esp32s3-speaker")


def test_load_calibrated_biases_skips_entries_missing_fields(tmp_path):
    payload = {
        "nodes": {
            "esp32s3-speaker": {"status": "admissible"},   # no path_bias_s / sigma_b_s at all
        },
    }
    path = _write(tmp_path, payload)
    assert nc.load_calibrated_biases(path) == []


def test_load_calibrated_biases_default_path_is_repo_config_file():
    """With no path given, the loader looks at config/calibrated_node_biases.json under the repo
    root. The file does not exist yet in this repo, so this must be a no-op, not an error."""
    assert nc.load_calibrated_biases() == []
