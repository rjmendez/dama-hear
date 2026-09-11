"""firmware/boards/esp32s3_lora.h, checked against what the ESP32-S3 silicon forbids.

⚠️THIS REPO HAS SHIPPED A WRONG PIN TO A SOLDERING IRON TWICE, AND NEITHER TIME WAS IT CAUGHT BY
READING. The L86's 1PPS was documented on module pin 11 when its Hardware Design table says 6;
the PUC's PPS pad was named GPIO18 on the argument "reads low and is not a strapping pin", both
true and both insufficient, when GPIO18 is externally driven and GPIO17 is the pad that floats.
tests/test_puc_pps_pin.py is the comparison that would have caught the second one. This is the
same comparison for a board that does not exist yet, made BEFORE the iron rather than after.

It reads the `#define`s with a parser, never a grep, so it cannot pass by matching its own prose.

WHAT THE SILICON SAYS, and where. Paths relative to
    ~/.arduino15/packages/esp32/tools/esp32s3-libs/3.3.11/include/soc/esp32s3/include/soc/

    soc_caps.h:187    SOC_GPIO_VALID_GPIO_MASK clears BIT22..BIT25 -> GPIO 22,23,24,25 ABSENT
    soc_caps.h:189    SOC_GPIO_VALID_OUTPUT_GPIO_MASK == SOC_GPIO_VALID_GPIO_MASK -> the S3 has
                      NO input-only pins; any claim that one is input-only is an ESP32-classic
                      table read for the wrong part
    soc_caps.h:177    SOC_GPIO_PIN_COUNT 49 -> 0..48 is the whole range
    spi_pins.h:11-17  MSPI flash bus 26 CS1, 27 HD, 28 WP, 29 CS0, 30 CLK, 31 MISO, 32 MOSI
    spi_pins.h:18-22  MSPI 33 D4, 34 D5, 35 D6, 36 D7, 37 DQS -- OCTAL flash/PSRAM only
    spi_pins.h:29-34  SPI2 IO_MUX fast path HD 9, CS 10, MOSI 11, CLK 12, MISO 13, WP 14
    usb_pins.h:26-27  USBPHY_DP_NUM 20, USBPHY_DM_NUM 19 -- the native USB pads
    uart_pins.h:23-24 U0RXD 44, U0TXD 43

TestAgainstTheInstalledHeaders below re-derives the first four of those from the toolchain when
it is installed, so these constants cannot rot silently against the headers they came from. It
skips where the toolchain is absent (CI), which is why the constants are ALSO written out here.

THE PSRAM QUESTION, AND WHY THIS FILE DOES NOT HAVE TO ANSWER IT. GPIO 33..37 are free on a quad
or no-PSRAM module and are the PSRAM data bus on an octal one. The board map is built to use
none of them, so the answer cannot move a pin -- and `test_uses_no_pin_that_octal_psram_would_take`
is what keeps that property true as the map is edited.

STRAPPING PINS ARE NOT ASSERTED FROM THE TOOLCHAIN, BECAUSE THEY ARE NOT IN IT. The installed
headers name GPIO46 (esp_rom/.../rom/efuse.h:210-211), contradict themselves with GPIO8 for the
same control (soc/.../efuse_struct.h:442-444), and name gpio10 for JTAG selection
(soc/.../efuse_reg.h:502-504). {0, 3, 45, 46} is a DATASHEET claim -- ESP32-S3 datasheet, chapter
2 "Pin Definitions", section "Strapping Pins" -- that has NOT been verified here. The test below
therefore asserts only the weak, safe thing: the board uses none of them, so the answer does not
matter to this build.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HDR = os.path.join(ROOT, "firmware", "boards", "esp32s3_lora.h")

#: soc_caps.h:177. 0..48 inclusive.
PIN_COUNT = 49
#: soc_caps.h:187, the cleared bits. These pads do not exist on the die.
ABSENT_PINS = {22, 23, 24, 25}
#: spi_pins.h:11-17. The flash bus. Never usable, on any module.
FLASH_PINS = {26, 27, 28, 29, 30, 31, 32}
#: spi_pins.h:18-22. Used only when the part is OCTAL. The map must not depend on which it is.
OCTAL_PSRAM_PINS = {33, 34, 35, 36, 37}
#: usb_pins.h:26-27. The native PHY pads. Reconfiguring these cost the PUC a replug.
USB_PINS = {19, 20}
#: DATASHEET, UNVERIFIED -- see the module docstring. Not used by this board either way.
STRAPPING_PINS_UNVERIFIED = {0, 3, 45, 46}

#: Every pin-valued define in the header, grouped by the function that owns it. A pin may appear
#: in exactly one group; that is what `test_no_two_functions_share_a_pin` enforces.
FUNCTIONS = {
    "gps_uart": ("GPS_RX_PIN", "GPS_TX_PIN"),
    "pps": ("PPS_PIN",),
    "lora_spi": ("LORA_CS_PIN", "LORA_MOSI_PIN", "LORA_SCK_PIN", "LORA_MISO_PIN"),
    "lora_ctl": ("LORA_DIO0_PIN", "LORA_RST_PIN", "LORA_DIO1_PIN",
                 "LORA_RXEN_PIN", "LORA_TXEN_PIN"),
    "mic": ("MIC_CLK_PIN", "MIC_DIN_PIN"),
    "sd_spi": ("SD_SCK_PIN", "SD_MISO_PIN", "SD_MOSI_PIN", "SD_CS_PIN"),
    "i2c": ("I2C_SDA_PIN", "I2C_SCL_PIN"),
}

#: The lines Meshtastic's RF95 path cannot run without. src/mesh/RadioInterface.cpp:466 passes
#: (cs, RF95_IRQ, RF95_RESET, RF95_DIO1); src/RF95Configuration.h maps RF95_IRQ to LORA_DIO0 and
#: RF95_DIO1 to LORA_DIO1 "not really used for RF95"; src/mesh/RF95Interface.h:51 arms
#: setDio0Action and nothing else. So DIO0 and RESET are required and DIO1 is not.
LORA_REQUIRED = ("LORA_CS_PIN", "LORA_MOSI_PIN", "LORA_SCK_PIN", "LORA_MISO_PIN",
                 "LORA_DIO0_PIN", "LORA_RST_PIN")


def _src():
    with open(HDR) as fh:
        return fh.read()


def _define_int(name, src=None):
    """The value of an integer #define, as the preprocessor would see it. None if absent."""
    src = _src() if src is None else src
    hits = re.findall(r"^\s*#define\s+%s\s+(-?\d+)\b" % re.escape(name), src, re.M)
    return int(hits[-1]) if hits else None


def _define_int_list(name, src=None):
    """The value of a `#define NAME {a, b, c}` brace list. None if absent."""
    src = _src() if src is None else src
    hits = re.findall(r"^\s*#define\s+%s\s+\{([^}]*)\}" % re.escape(name), src, re.M)
    if not hits:
        return None
    return [int(t) for t in re.findall(r"-?\d+", hits[-1])]


def _assigned():
    """function name -> {pin: define} for every pin this build actually claims.

    -1 means NOT WIRED and is excluded: it is an assertion that the joint does not exist, which
    is the honest state for LORA_DIO1_PIN, and treating it as a pin would collide five ways.
    """
    src = _src()
    out = {}
    for fn, names in FUNCTIONS.items():
        pins = {}
        for n in names:
            v = _define_int(n, src)
            assert v is not None, "%s is missing from %s" % (n, os.path.basename(HDR))
            if v != -1:
                pins[v] = n
        out[fn] = pins
    return out


def _all_assigned():
    out = {}
    for fn, pins in _assigned().items():
        for pin, name in pins.items():
            out.setdefault(pin, []).append((fn, name))
    return out


class TestTheHeaderExistsAndParses:
    def test_board_name_is_stated(self):
        assert re.search(r'^\s*#define\s+BOARD_NAME\s+"esp32s3-lora-pps"', _src(), re.M)

    def test_every_named_pin_define_is_present(self):
        # _assigned() asserts on absence; calling it is the test
        assert _assigned()


class TestPinsThatDoNotExistOrCannotBeUsed:
    @pytest.mark.parametrize("fn", sorted(FUNCTIONS))
    def test_no_function_lands_on_a_pin_the_die_does_not_have(self, fn):
        bad = sorted(set(_assigned()[fn]) & ABSENT_PINS)
        assert not bad, (
            "%s uses GPIO %s. soc_caps.h:187 clears BIT22..BIT25 from SOC_GPIO_VALID_GPIO_MASK: "
            "those pads DO NOT EXIST on ESP32-S3." % (fn, bad))

    @pytest.mark.parametrize("fn", sorted(FUNCTIONS))
    def test_no_function_lands_on_the_flash_bus(self, fn):
        bad = sorted(set(_assigned()[fn]) & FLASH_PINS)
        assert not bad, (
            "%s uses GPIO %s, which is the MSPI flash bus (spi_pins.h:11-17). Driving it takes "
            "the board down at the first cache miss." % (fn, bad))

    @pytest.mark.parametrize("fn", sorted(FUNCTIONS))
    def test_no_function_lands_on_native_usb(self, fn):
        bad = sorted(set(_assigned()[fn]) & USB_PINS)
        assert not bad, (
            "%s uses GPIO %s. usb_pins.h:26-27 makes 19/20 the native USB D-/D+ pads; "
            "reconfiguring them cost the PUC a replug inside a four-second window." % (fn, bad))

    @pytest.mark.parametrize("fn", sorted(FUNCTIONS))
    def test_no_function_lands_outside_the_pin_range(self, fn):
        bad = sorted(p for p in _assigned()[fn] if not 0 <= p < PIN_COUNT)
        assert not bad, "%s uses GPIO %s; soc_caps.h:177 is SOC_GPIO_PIN_COUNT 49" % (fn, bad)

    def test_uses_no_pin_that_octal_psram_would_take(self):
        """⚠️THIS IS THE TEST THAT KEEPS THE PSRAM QUESTION FROM MATTERING.

        33..37 are free on a quad/no-PSRAM module and are the PSRAM data bus on an octal one
        (spi_pins.h:18-22). Whether THIS breakout is octal is not established. The map avoids
        them entirely, so the answer cannot move a pin -- and this is what keeps that true.
        Ten pads remain free, so relaxing it is never the cheapest fix.
        """
        bad = {p: v for p, v in _all_assigned().items() if p in OCTAL_PSRAM_PINS}
        assert not bad, (
            "GPIO %s is claimed by %s. On an OCTAL module that is the PSRAM bus. The board's "
            "PSRAM type is not established, so the map must work for both -- FREE_PADS still "
            "has spares." % (sorted(bad), sorted({f for v in bad.values() for f, _ in v})))

    def test_uses_no_pin_believed_to_be_a_strapping_pin(self):
        """{0,3,45,46} is DATASHEET and UNVERIFIED here -- see the module docstring.

        Which is exactly why the build must not depend on it. This asserts avoidance, not the
        claim itself.
        """
        bad = {p: v for p, v in _all_assigned().items() if p in STRAPPING_PINS_UNVERIFIED}
        assert not bad, (
            "GPIO %s is claimed. Those are believed to be strapping pins (ESP32-S3 datasheet, "
            "ch.2 'Strapping Pins') and that belief is NOT verified in this repo, so the build "
            "must not rest on it either way." % sorted(bad))


class TestNothingIsWiredTwice:
    def test_no_two_functions_share_a_pin(self):
        dup = {p: v for p, v in _all_assigned().items() if len(v) > 1}
        assert not dup, "these pins are claimed twice: %s" % {
            p: [n for _, n in v] for p, v in sorted(dup.items())}

    def test_the_lora_bus_does_not_collide_with_the_sd_bus(self):
        """⚠️THE COLLISION THAT WOULD NOT LOOK LIKE ONE.

        Two SPI devices on one bus is legal and it is the wrong trade here: a DIO0 edge arrives
        during an SD block write and the radio's service contends with the card's transaction at
        interrupt time. Sharing would also read as working on the bench, where nothing detects
        while the card is being written. Separate hosts, separate pins.
        """
        a = _assigned()
        shared = sorted(set(a["lora_spi"]) & set(a["sd_spi"]))
        assert not shared, "LoRa and SD share GPIO %s -- they must be separate buses" % shared
        assert _define_int("LORA_SPI_HOST") != _define_int("SD_SPI_HOST"), (
            "LoRa and SD are on the same SPI host, so they share the peripheral even with "
            "distinct pins")

    def test_lora_control_lines_do_not_sit_on_the_lora_bus(self):
        a = _assigned()
        shared = sorted(set(a["lora_ctl"]) & set(a["lora_spi"]))
        assert not shared, "LoRa DIO0/RESET on GPIO %s, which is also a bus line" % shared

    def test_free_pads_claims_nothing_the_build_uses(self):
        """⚠️puc.h's FREE_PADS lists 18 and 39 and tests/test_puc_pps_pin.py records both as
        externally DRIVEN. A free-pad list that overlaps the build is the same failure one step
        earlier, and it is the list a future wiring session reads."""
        free = _define_int_list("FREE_PADS")
        assert free, "FREE_PADS is missing"
        overlap = sorted(set(free) & set(_all_assigned()))
        assert not overlap, "FREE_PADS offers GPIO %s, which this build already uses" % overlap

    def test_free_pads_offers_no_pin_the_silicon_forbids(self):
        free = set(_define_int_list("FREE_PADS"))
        forbidden = ABSENT_PINS | FLASH_PINS | OCTAL_PSRAM_PINS | USB_PINS | \
            STRAPPING_PINS_UNVERIFIED
        bad = sorted(free & forbidden)
        assert not bad, "FREE_PADS offers GPIO %s, which is absent/flash/PSRAM/USB/strapping" % bad


class TestTheRadioHasTheLinesItsDriverActuallyArms:
    @pytest.mark.parametrize("name", LORA_REQUIRED)
    def test_a_required_lora_line_is_wired(self, name):
        v = _define_int(name)
        assert v is not None and v != -1, (
            "%s is %s. Meshtastic's RF95 path needs SPI, DIO0 and RESET: RadioInterface.cpp:466 "
            "passes them and RF95Interface.h:51 arms setDio0Action." % (name, v))

    def test_dio1_is_explicitly_not_wired_rather_than_absent(self):
        """RF95Configuration.h calls DIO1 'not really used for RF95' and RF95Interface.h:51 arms
        only DIO0. -1 states that as a decision; a missing define states nothing."""
        assert _define_int("LORA_DIO1_PIN") is not None

    def test_the_lora_bus_is_the_spi2_iomux_fast_path(self):
        """spi_pins.h:29-34 -- routing SPI2 through these pads skips the GPIO matrix. Meshtastic's
        tbeam-s3-core variant.h, a shipping ESP32-S3 + SX127x board, uses the same four."""
        a = _assigned()["lora_spi"]
        got = {a[p]: p for p in a}
        assert (got["LORA_CS_PIN"], got["LORA_MOSI_PIN"],
                got["LORA_SCK_PIN"], got["LORA_MISO_PIN"]) == (10, 11, 12, 13)


class TestThePpsPinClaimsNothingItHasNotEarned:
    def test_pps_is_not_wired_yet(self):
        """⚠️A NODE WITH AN UNPROVEN PPS MUST NOT BE TRUSTED AS A TDoA ARRIVAL SOURCE.

        puc.h says the same thing: PPS_WIRED flips to 1 only when /pps has reported edges. The
        board does not exist, so the honest value is 0 and this test fails the day somebody
        optimistically sets it before the scan.
        """
        assert _define_int("PPS_WIRED") == 0, (
            "PPS_WIRED is 1 on a board nobody has built. It means edges have ARRIVED, not that a "
            "pin was chosen.")

    def test_pps_names_a_candidate_pad_rather_than_nothing(self):
        # -1 would also be honest, but then the build doc has no pad to scan; a named candidate
        # that the scan must clear is the useful state.
        assert _define_int("PPS_PIN") != -1


class TestTheClassIsNotRegisteredUntilItsPathIsMeasured:
    """⚠️A BOARD MAY NOT CLAIM A NodeClass IT HAS NOT EARNED.

    hear/nodeclass.py refuses a class with `path_bias_s=None` as a TDoA arrival source, which is
    correct for hardware nobody has timed. The failure this guards is the transplant: copying the
    XIAO's 1/16000 across a change of microphone, sample rate and SPI load, where the number stays
    plausible and stops being true. It does NOT require the class to be absent -- registering it
    is fine, registering it with a made-up bias is not.
    """

    def test_if_the_class_exists_its_capture_path_has_been_measured(self):
        import sys
        sys.path.insert(0, ROOT)
        from hear import nodeclass as NC

        name = "esp32s3-lora-pps"
        if name not in NC.CLASSES:
            pytest.skip("class not registered yet, which is the expected state")
        assert NC.CLASSES[name].path_bias_s is not None, (
            "%s is registered with path_bias_s=None or a copied constant. Measure the capture "
            "delay against an external reference before this class contributes an arrival." % name)


class TestAgainstTheInstalledHeaders:
    """The constants above, re-derived from the toolchain when it is present.

    ⚠️THE CONSTANTS ARE STILL WRITTEN OUT ABOVE ON PURPOSE. This class skips wherever the ESP-IDF
    tree is not installed -- CI, a fresh checkout -- so it can only catch a drift, never carry the
    claim. A test that exists only where the toolchain does is a test that is usually not run.
    """

    SOC = os.path.expanduser(
        "~/.arduino15/packages/esp32/tools/esp32s3-libs/3.3.11/include/soc/esp32s3/include/soc")

    def _read(self, name):
        p = os.path.join(self.SOC, name)
        if not os.path.exists(p):
            pytest.skip("ESP-IDF headers not installed at %s" % self.SOC)
        with open(p) as fh:
            return fh.read()

    def test_absent_pins_match_soc_caps(self):
        src = self._read("soc_caps.h")
        m = re.search(r"#define\s+SOC_GPIO_VALID_GPIO_MASK\s+(.+)", src)
        assert m, "SOC_GPIO_VALID_GPIO_MASK not found"
        cleared = {int(b) for b in re.findall(r"~?\(?[^)]*?BIT(\d+)", m.group(1))}
        assert cleared == ABSENT_PINS

    def test_the_part_has_no_input_only_pins(self):
        src = self._read("soc_caps.h")
        m = re.search(r"#define\s+SOC_GPIO_VALID_OUTPUT_GPIO_MASK\s+\(([^)]*)\)", src)
        assert m and m.group(1).strip() == "SOC_GPIO_VALID_GPIO_MASK", (
            "the output mask no longer equals the input mask, so the S3 may now have input-only "
            "pins and every 'any pin can drive' argument in the board header needs re-reading")

    def test_pin_count_matches_soc_caps(self):
        src = self._read("soc_caps.h")
        assert int(re.search(r"#define\s+SOC_GPIO_PIN_COUNT\s+(\d+)", src).group(1)) == PIN_COUNT

    def test_flash_and_octal_pins_match_spi_pins(self):
        src = self._read("spi_pins.h")
        mspi = {n: int(v) for n, v in
                re.findall(r"#define\s+MSPI_IOMUX_PIN_NUM_(\w+)\s+(\d+)", src)}
        quad = {"CS1", "HD", "WP", "CS0", "CLK", "MISO", "MOSI"}
        octal = {"D4", "D5", "D6", "D7", "DQS"}
        assert {mspi[k] for k in quad} == FLASH_PINS
        assert {mspi[k] for k in octal} == OCTAL_PSRAM_PINS

    def test_usb_pins_match_usb_pins_h(self):
        src = self._read("usb_pins.h")
        dp = int(re.search(r"#define\s+USBPHY_DP_NUM\s+(\d+)", src).group(1))
        dm = int(re.search(r"#define\s+USBPHY_DM_NUM\s+(\d+)", src).group(1))
        assert {dp, dm} == USB_PINS

    def test_the_lora_bus_matches_the_spi2_iomux_block(self):
        src = self._read("spi_pins.h")
        s2 = {n: int(v) for n, v in
              re.findall(r"#define\s+SPI2_IOMUX_PIN_NUM_(\w+)\s+(\d+)\s*$", src, re.M)}
        a = _assigned()["lora_spi"]
        got = {a[p]: p for p in a}
        assert got["LORA_CS_PIN"] == s2["CS"]
        assert got["LORA_MOSI_PIN"] == s2["MOSI"]
        assert got["LORA_SCK_PIN"] == s2["CLK"]
        assert got["LORA_MISO_PIN"] == s2["MISO"]

    def test_the_gps_pair_is_the_uart0_iomux_pair(self):
        src = self._read("uart_pins.h")
        rx = int(re.search(r"#define\s+U0RXD_GPIO_NUM\s+(\d+)", src).group(1))
        tx = int(re.search(r"#define\s+U0TXD_GPIO_NUM\s+(\d+)", src).group(1))
        assert _define_int("GPS_RX_PIN") == rx
        assert _define_int("GPS_TX_PIN") == tx
