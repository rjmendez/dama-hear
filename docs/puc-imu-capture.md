# PUC LIS3DH seismic capture

The PUC firmware now has a narrow LIS3DH vibration path for bench/on-device validation. It uses the measured PUC I2C bus, **SDA GPIO47 / SCL GPIO48**, and will not enable capture unless the accelerometer at **0x18** returns **WHO_AM_I 0x33** and every control-register write reads back correctly.

## Capture contract

- Sensor: LIS3DH at `0x18`, raw signed XYZ retained from `OUT_X/Y/Z`.
- Rate: **400 Hz**. This is an exact LIS3DH ODR (`CTRL_REG1=0x77`) and matches the existing phone/infrasound analysis rate closely enough that no synthetic resampling contract is needed; Nyquist is 200 Hz, above the current 0.1-100 Hz seismic feature band.
- Mode: high-resolution, +/-2 g, block-data-update enabled, FIFO stream mode with watermark 24. The INT1/INT2 route is not yet identified, so the sketch uses bounded FIFO polling (`IMU_BURST_MAX=24`) rather than guessing an interrupt GPIO.
- Clock: samples carry `mono_us` timestamps reconstructed from the monotonic burst-end time and the 2500 us sample period. This is a local sample clock, not a UTC/PPS arrival claim.
- Safety: identity failure, I2C failure, or config readback mismatch leaves `imu.ok=false`; runtime read faults are counted in health rather than hidden.

## HTTP surfaces

- `GET /status` includes `imu`: `ok`, `fault`, `odr_hz`, bus pins/rate, FIFO level, sample count, ring drops, FIFO overruns, I2C errors, short reads, and register readbacks.
- `GET /imu/status` returns the same health block.
- `GET /imu/features` returns `phone-vibration-features-v1` with `source:"imu"`, LIS3DH sensor identity, RMS/crest/z-kurtosis fields compatible with the existing phone vibration feature vocabulary, and `vibration_onset` when a high-crest impulse is present. It explicitly sets `is_microphone:false`.
- `GET /imu?limit=N` returns `puc-lis3dh-raw-v1`: recent raw XYZ samples with `seq` and `mono_us`, health, and the feature block. `limit` is capped by the firmware ring.

## Required on-device validation before field use

1. Flash only a bench image first; do not deploy to the live cluster.
2. Confirm `/status.imu.ok == true`, `who_am_i == "0x33"`, bus is GPIO47/GPIO48, `odr_hz == 400`, and control readbacks are `reg1=0x77`, `reg4=0x88`, `reg5` FIFO-enabled, `fifo=0x98`.
3. Poll `/imu?limit=128` while tapping the enclosure. Verify raw XYZ changes, `mono_us` is monotonic, and feature output remains `source:"imu"` with no microphone claim.
4. Leave it running at least 10 minutes and confirm `fifo_overruns == 0`, `i2c_errors == 0`, `short_reads == 0`, and drops grow only if the HTTP consumer does not drain the ring.
5. Record whether INT1/INT2 is routed to an ESP32 GPIO. Until that is measured, the firmware must remain FIFO-polled.
