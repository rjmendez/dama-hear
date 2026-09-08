# Quectel L86 — the facts, from the datasheets

Written because I repeatedly guessed at this module from search results and got it wrong, at cost:
a wrong 1PPS pin, a wrong claim about a parameter range, and a wrong explanation for why the
module stopped talking. Everything below is from *L86 Hardware Design V1.3* and *L86 GNSS
Protocol Specification V1.5*, with the section noted. Nothing here is recalled or inferred.

The part on the BirdWeather PUC: **MT3333 core, firmware `AXN5.1.9`, `Quectel-L86`, 9600 baud**,
confirmed by round trip (`$PMTK605` → `$PMTK705,MT3333_AXN5.1.9_MODULE_STD_F1_P1,0002,Quectel-L86,1.0`).

## Pin map (12-pin LCC, Hardware Design Table 3)

| pin | name | I/O | note |
|---|---|---|---|
| 1 | RXD1 | I | module's receive — the node's TX goes here |
| 2 | TXD1 | O | module's transmit — the node's RX. `VOLmax 0.42 V`, `VOHnom 2.8 V` |
| 4 | VCC | I | main supply, 2.8–4.3 V, **≥100 mA** |
| 5 | V_BCKP | I | **RTC-domain backup supply**, 2–4.3 V. May be a battery or tied to VCC |
| **6** | **1PPS** | **O** | **rising-edge synchronised, 100 ms default width** |
| 7 | FORCE_ON | I | **logic high wakes the module from backup mode.** RTC domain |
| 8 | AADET_N | O | active antenna detection |
| 10 | RESET | I | **low-active**, ≥10 ms, resets the digital part |
| 11 | EX_ANT | I | **external active antenna RF input, 50 Ω** |

⚠️**1PPS is pin 6.** I told the operator it was pin 11 — pin 11 is the antenna RF input. My first
instinct had been pin 6 and I "corrected" it to 11 on the strength of a search summary. The
datasheet settles it.

## Backup mode — this is what "UART is dead but the module is alive" looks like

Hardware Design §3.4.3, quoted:

> In this mode, the module stops acquiring and tracking satellites. **UART is not accessible.** But
> the backed-up memory in RTC domain … is alive.

Two ways in, and the wake-up differs:

- `$PMTK225,4*2F` enters backup **permanently**, and *"the only way to wake up the module is to
  pull FORCE_ON to high"*.
- Cutting VCC while **V_BCKP stays powered** also enters backup. *"Provided that the VCC pin is
  powered on, the module will return to the full-on mode immediately."*

So a power cycle that drops VCC but leaves V_BCKP alive does **not** clear the RTC domain — and if
V_BCKP is tied to VCC on a given board, it does.

## Reset

Hardware Design §3.6: drive **RESET (pin 10)** low for ≥10 ms and release. *"non-volatile backup
RAM is not cleared after resetting"* — so a RESET pulse restarts the digital part but preserves
configuration. Only `PMTK104` clears configuration (see below).

## PMTK commands that change persistent state

From the Protocol Specification. **Ranges are the datasheet's, not my recollection.**

| packet | name | field | range / meaning |
|---|---|---|---|
| 101 | HOT_START | — | restart using all prior data |
| 103 | COLD_START | — | restart with no prior location, time, almanac or ephemeris |
| **104** | **FULL_COLD_START** | — | *"clears system and user configurations … reset to the factory status"*. Also restores the baud rate default (§3.16) |
| 161 | STANDBY_MODE | Type | `0` = stop mode |
| 225 | SET_PERIODIC_MODE | Type | `4` = **backup mode, permanent** — needs FORCE_ON high to leave |
| 251 | SET_NMEA_BAUDRATE | Baudrate | 4800 / 9600 (default) / 14400 / 19200 / 38400 / 57600 / 115200 |
| 255 | SET_SYNC_PPS_NMEA | Enable | fix NMEA output time behind PPS. Default **off** |
| 256 | SET_TIMING_PRODUCT | Enable | timing product mode. Default **off** |
| **285** | **SET_PPS_CONFIG** | Type | `0` disable, `1` after first fix, `2` 3D only, `3` 2D/3D, `4` **always** |
| | | PPSPulseWidth | **`2~998` ms** |
| 314 | SET_NMEA_OUTPUT | 19 fields | `0` disables a sentence. **All zeros = silent module** |
| 605 | Q_RELEASE | — | version query; answers `PMTK705` |

⚠️**`PMTK285` accepts 2–998 ms.** I sent `PMTK285,4,500`, the module went silent, and I told the
operator twice that 500 was out of range and that this was the cause. It is squarely inside the
documented range. I then wrote that wrong explanation into a code comment and a commit message.
Whatever silenced the module, the datasheet does not support the reason I gave.

## What is actually established about the silent module

Measured, not inferred:

- Module TXD1 is **driven low** — it holds against a 45 kΩ pullup, so it is not tri-stated,
  unpowered or in reset. A quiet UART idles **high**; this one is asserting a break.
- **1PPS is present at the PUC's testpoint** (operator's scope), so part of the module runs.
- `PMTK101`, `PMTK103`, `PMTK104` and `PMTK605` all produce nothing.
- A power cycle did not change it.

"UART is not accessible" while the RTC domain stays alive is precisely the datasheet's description
of **backup mode**, and it is the only documented state that matches. If that is what it is, the
documented exit is **FORCE_ON (pin 7) high**, not any command — because in backup mode nothing is
listening.

That is a hypothesis with a citation, not a conclusion. It is not tested.
