"""Static contracts for the PUC LIS3DH seismic capture path."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "puc_node" / "puc_node.ino"
DOC = ROOT / "docs" / "puc-imu-capture.md"


def _code():
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _json_literals():
    return INO.read_text().replace(r"\"", '"')


def _define_int(name):
    hits = re.findall(r"^\s*#define\s+%s\s+(-?(?:0x)?[0-9A-Fa-f]+)" % re.escape(name), INO.read_text(), re.M)
    assert hits, "%s is not defined" % name
    return int(hits[-1], 0)


def test_lis3dh_identity_and_measured_bus_are_compiled_in():
    assert _define_int("IMU_I2C_SDA") == 47
    assert _define_int("IMU_I2C_SCL") == 48
    assert _define_int("LIS3DH_ADDR") == 0x18
    assert _define_int("LIS3DH_WHO_AM_I_EXPECTED") == 0x33
    assert "WHO_AM_I mismatch" in INO.read_text()


def test_capture_rate_is_supported_phone_equivalent_400_hz():
    assert _define_int("IMU_ODR_HZ") == 400
    assert _define_int("IMU_DT_US") == 2500
    code = _code()
    assert "lis3dh_write_checked(LIS3DH_CTRL_REG1, 0x77" in code, (
        "CTRL_REG1 must request LIS3DH ODR=400 Hz with XYZ enabled")


def test_hardware_configuration_fails_closed_on_identity_and_readback():
    code = _code()
    assert code.index("lis3dh_read_reg(LIS3DH_WHO_AM_I") < code.index("lis3dh_write_checked(LIS3DH_CTRL_REG1")
    assert code.count("readback failed") >= 4
    assert "imu_ok = true" in code
    assert code.index("imu_ok = true") > code.index("LIS3DH_FIFO_CTRL_REG")


def test_fifo_bounded_burst_raw_ring_and_monotonic_clock_are_exposed():
    src = _json_literals()
    assert _define_int("IMU_FIFO_WATERMARK") == 24
    assert _define_int("IMU_FIFO_CAPACITY") == 32
    assert _define_int("IMU_BURST_MAX") == 32
    assert "LIS3DH_FIFO_SRC_REG" in src
    assert "IMU_RING_N" in src
    assert '"schema":"puc-lis3dh-raw-v1"' in src
    assert "mono_us" in src
    assert "sample-clock-reconstructed-from-burst-end-monotonic-us" in src


def test_lis3dh_full_fifo_fss_value_is_drained_as_32_samples():
    code = _code()
    assert "fss == 0x1F ? IMU_FIFO_CAPACITY : fss" in code, (
        "LIS3DH FIFO_SRC_REG FSS=0x1f means a full 32-sample FIFO, not 31")
    assert "imu_fifo_lost_min++" in code, "FIFO overrun must account at least one overwritten sample"
    assert '"fifo_lost_min"' in _json_literals()


def test_status_reports_health_rate_drops_and_fifo_overruns():
    src = _json_literals()
    for token in ('"imu":', '"odr_hz"', '"drops"', '"fifo_overruns"', '"fifo_lost_min"', '"i2c_errors"', '"short_reads"'):
        assert token in src


def test_feature_contract_marks_imu_seismic_not_microphone():
    src = _json_literals()
    assert '"schema":"phone-vibration-features-v1"' in src
    assert '"source":"imu"' in src
    assert '"seismic.rayleigh_wave"' in src
    assert '"is_microphone":false' in src
    assert '"is_seismic":true' in src


def test_disabled_feature_response_keeps_the_fail_closed_claim():
    src = _json_literals()
    feature_fn = src[src.index("static String imu_features_json()"):src.index("static String imu_samples_json")]
    disabled = feature_fn[feature_fn.index('if (!imu_ok || n < 8)'):feature_fn.index('double sum = 0')]
    for token in ('"claim"', '"is_microphone":false', '"is_seismic":true', '"provenance":"sensor"'):
        assert token in disabled


def test_phone_feature_names_are_computed_from_calibrated_accel_mag_mps2():
    src = _json_literals()
    code = _code()
    assert "LIS3DH_MPS2_PER_LSB" in code
    assert "9.80665f * 0.001f / 16.0f" in INO.read_text()
    assert '"input":"accel_mag"' in src
    assert '"units":"m/s2"' in src
    assert "sqrt(ax * ax + ay * ay + az * az)" in code
    assert "sqrt((double)s.x * s.x + (double)s.y * s.y + (double)s.z * s.z)" not in code, (
        "phone-compatible crest_factor/dc_offset must not be computed over raw LIS3DH counts")


def test_operator_doc_names_required_on_device_validation():
    doc = DOC.read_text()
    for token in ("WHO_AM_I 0x33", "SDA GPIO47", "SCL GPIO48", "400 Hz", "/imu", "fifo_overruns"):
        assert token in doc
