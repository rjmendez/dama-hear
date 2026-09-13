"""Regression coverage for the PMTK/NMEA path in ``hear_node.ino``.

The ESP32 sketch is not host-buildable in the repository's CI image. These tests therefore
combine source-shape checks (for wiring and guards that must remain in the sketch) with a small
host executable of the GGA arithmetic. The latter follows the sketch's field layout and
conversion rules so malformed input and boundary behavior are tested without depending on an
ESP32 toolchain.
"""
import math
import pathlib
import re
from dataclasses import dataclass

import pytest


INO = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "hear_node" / "hear_node.ino"
POS_HACC_MAX_MM = 25_000


def _source():
    if not INO.exists():
        pytest.skip("hear_node.ino not in this checkout")
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _body(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def _checksum(body):
    value = 0
    for char in body:
        value ^= ord(char)
    return f"{value:02X}"


def _sentence(body, checksum=None):
    return f"${body}*{checksum or _checksum(body)}"


@dataclass
class Position:
    lat_e7: int = 0
    lon_e7: int = 0
    hell_mm: int = 0
    hmsl_mm: int = 0
    hacc_mm: int = 0
    fix: int = 0
    sats: int = 0
    pos_n: int = 0
    sum_lat: float = 0.0
    sum_lon: float = 0.0
    sum_hell: float = 0.0
    sum_hmsl: float = 0.0


def _round_mm(value):
    # The sketch rounds halves away from zero for millimetres.
    return int(value * 1000.0 + (0.5 if value >= 0 else -0.5))


def _parse_gga(sentence, position=None):
    """Host equivalent of nmea_gga_position(), including its state updates."""
    if position is None:
        position = Position()
    if not sentence.startswith("$") or "*" not in sentence:
        return False, position
    payload, supplied = sentence[1:].split("*", 1)
    if len(supplied) < 2 or supplied[:2].upper() != _checksum(payload):
        return False, position
    fields = payload.split(",")
    if len(fields) < 12:
        return False, position
    try:
        fix = int(fields[6])
        sats = int(fields[7])
        lat_raw = float(fields[2])
        lon_raw = float(fields[4])
        hdop = float(fields[8])
        hmsl_m = float(fields[9])
        geoid_m = float(fields[11])
    except (ValueError, OverflowError):
        return False, position
    if any(not value for value in (fields[2], fields[4], fields[6], fields[7], fields[8], fields[9], fields[11])):
        return False, position
    if fix < 0 or sats < 0 or not math.isfinite(lat_raw) or not math.isfinite(lon_raw):
        return False, position
    if not math.isfinite(hdop) or hdop < 0 or not math.isfinite(hmsl_m) or not math.isfinite(geoid_m):
        return False, position
    if fields[3] not in ("N", "S") or fields[5] not in ("E", "W"):
        return False, position
    lat_deg, lon_deg = int(lat_raw / 100), int(lon_raw / 100)
    lat = lat_deg + (lat_raw - lat_deg * 100) / 60
    lon = lon_deg + (lon_raw - lon_deg * 100) / 60
    if lat_deg > 90 or lon_deg > 180 or not 0 <= lat <= 90 or not 0 <= lon <= 180:
        return False, position
    lat_e7 = int(lat * 10_000_000 + 0.5)
    lon_e7 = int(lon * 10_000_000 + 0.5)
    hmsl = _round_mm(hmsl_m)
    geoid = _round_mm(geoid_m)
    hacc = int(hdop * 2500 + 0.5)
    hell = hmsl + geoid
    if not 0 <= hacc <= 0xFFFFFFFF:
        return False, position
    position.lat_e7 = -lat_e7 if fields[3] == "S" else lat_e7
    position.lon_e7 = -lon_e7 if fields[5] == "W" else lon_e7
    position.hmsl_mm = hmsl
    position.hell_mm = hell
    position.hacc_mm = hacc
    position.fix = fix
    position.sats = sats
    if fix >= 1 and hacc and hacc < POS_HACC_MAX_MM:
        position.sum_lat += position.lat_e7
        position.sum_lon += position.lon_e7
        position.sum_hell += hell
        position.sum_hmsl += hmsl
        position.pos_n += 1
    return True, position


def _gga(lat="4807.038", ns="N", lon="01131.000", ew="E", fix="1", sats="08",
         hdop="0.8", hmsl="545.4", geoid="46.9"):
    return _sentence(
        f"GPGGA,123519,{lat},{ns},{lon},{ew},{fix},{sats},{hdop},{hmsl},M,{geoid},M,,"
    )


def test_pmtk_configuration_sends_and_requires_pulse_length_command():
    src = _source()
    configure = _body(src, "static void gps_configure()", "\nstatic uint8_t ux")
    assert 'pmtk_send("PMTK285,4,100");' in configure
    assert "PMTK_CFG_REQUIRED" in src
    required = re.search(r"#define\s+PMTK_CFG_REQUIRED\s+([^\n]+)", src).group(1)
    assert "PMTK_CFG_285" in required
    assert re.search(r"case\s+285:\s*return\s+PMTK_CFG_285", src)
    assert "pmtk_cfg_ack & PMTK_CFG_REQUIRED" in src
    assert "pmtk_cfg_nak) & PMTK_CFG_REQUIRED" in src


def test_gga_converts_coordinates_hemispheres_heights_and_hdop():
    ok, position = _parse_gga(_gga(ns="S", ew="W", hmsl="100.25", geoid="-33.75", hdop="1.2"))
    assert ok
    assert position.lat_e7 == -481173000
    assert position.lon_e7 == -115166667
    assert position.hmsl_mm == 100250
    assert position.hell_mm == 66500
    assert position.hacc_mm == 3000
    assert position.pos_n == 1


def test_gga_fix_zero_updates_last_position_but_not_the_mean():
    ok, position = _parse_gga(_gga(fix="0", sats="0"))
    assert ok
    assert position.lat_e7 == 481173000
    assert position.lon_e7 == 115166667
    assert position.fix == 0 and position.sats == 0
    assert position.pos_n == 0


@pytest.mark.parametrize("mutate", [
    lambda s: s[:-2] + "00",                         # bad checksum
    lambda s: s.replace(",N,", ",X,"),              # invalid latitude hemisphere
    lambda s: s.replace(",E,", ",Q,"),              # invalid longitude hemisphere
    lambda s: s.replace(",0.8,", ",,"),             # empty HDOP
    lambda s: s.replace(",545.4,M,", ",,M,"),       # empty MSL altitude
    lambda s: s.replace(",46.9,M,", ",,M,"),        # empty geoid separation
])
def test_gga_rejects_bad_checksum_empty_fields_and_hemispheres(mutate):
    ok, position = _parse_gga(mutate(_gga()))
    assert not ok
    assert position.pos_n == 0


def test_gga_accumulates_only_plausible_fixed_positions():
    position = Position()
    for sentence in (
        _gga(lat="4807.038", fix="1", hdop="0.8"),
        _gga(lat="4807.638", fix="1", hdop="9.9"),  # 24.75 m: still below the cap
        _gga(lat="4808.038", fix="1", hdop="10.0"), # 25 m: rejected by strict cap
        _gga(lat="4808.638", fix="1", hdop="0.0"),  # zero accuracy: rejected
        _gga(lat="4809.038", fix="0", hdop="0.8"),  # no fix: last position only
    ):
        ok, position = _parse_gga(sentence, position)
        assert ok
    assert position.pos_n == 2
    assert position.sum_lat == pytest.approx(481173000 + 481273000)
    assert position.sum_hell == pytest.approx(2 * 592300)
    assert position.lat_e7 == 481506333
    assert position.sum_lat / position.pos_n == pytest.approx(481223000)


def test_source_keeps_gga_validation_before_state_and_mean_updates():
    src = _source()
    gga = _body(src, "static bool nmea_gga_position", "\nstatic void nmea_line")
    for check in (
        "if (!nmea_checksum_ok(s)) return false;",
        "if (fields < 12) return false;",
        "field[3][0] != 78 && field[3][0] != 83",
        "field[5][0] != 69 && field[5][0] != 87",
        "field[6][0]",
        "field[8][0]",
        "field[9][0]",
        "field[11][0]",
        "gps_fix >= 1 && pos_hacc_mm && pos_hacc_mm < POS_HACC_MAX_MM",
    ):
        assert check in gga, "GGA guard moved or was removed: %s" % check
    assert gga.index("pos_lat_e7 =") < gga.index("if (gps_fix >= 1")
    assert "pos_hmsl_mm = (int32_t)hmsl; pos_hell_mm = (int32_t)hell" in gga
    assert "int64_t hacc = (int64_t)(hdop * 2500.0 + 0.5)" in gga
