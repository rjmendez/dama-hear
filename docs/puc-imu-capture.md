# PUC LIS3DH seismic capture

The PUC firmware now has a narrow LIS3DH vibration path for bench/on-device validation. It uses the measured PUC I2C bus, **SDA GPIO47 / SCL GPIO48**, and will not enable capture unless the accelerometer at **0x18** returns **WHO_AM_I 0x33** and every control-register write reads back correctly.

## Capture contract

- Sensor: LIS3DH at `0x18`, raw signed XYZ retained from `OUT_X/Y/Z`.
- Rate: **400 Hz**. This is an exact LIS3DH ODR (`CTRL_REG1=0x77`) and matches the existing phone/infrasound analysis rate closely enough that no synthetic resampling contract is needed; Nyquist is 200 Hz, above the current 0.1-100 Hz seismic feature band.
- Mode: high-resolution, +/-2 g, block-data-update enabled, FIFO stream mode with watermark 24. The INT1/INT2 route is not yet identified, so the sketch uses bounded FIFO polling rather than guessing an interrupt GPIO. A bounded drain may read the full 32-sample LIS3DH FIFO; `FSS=0x1f` is treated as 32 samples, and FIFO overrun records both an event count and a minimum overwritten-sample count.
- Clock: samples carry `mono_us` timestamps reconstructed only for samples actually read. The newest sample in a drained burst is estimated at the post-drain monotonic timestamp (`timestamp_basis:"last_sample_estimate_post_drain_us"`), then prior samples step back by 2500 us. `last_read_latency_us` reports the drain latency bound. This is a local sample clock, not a UTC/PPS arrival claim.
- Safety: identity failure, I2C failure, or config readback mismatch leaves IMU capture disabled. Runtime I2C faults are counted; one transient does not flip health, but `consecutive_i2c_errors >= 3` reports `state:"i2c_fault"` and `ok:false`. A successful burst clears the consecutive counter. If no fresh sample arrives for 1 s, health reports `state:"stale"`.

## HTTP surfaces

- `GET /status` includes `imu`: `ok`, `state`, `fault`, `odr_hz`, bus pins/rate, post-drain FIFO level (`255` means the post-drain status read failed), sample count, ring drops, FIFO overruns, `fifo_lost_min`, I2C errors, `consecutive_i2c_errors`, short reads, freshness, read latency, timestamp basis, and register readbacks.
- `GET /imu/status` returns the same health block.
- `GET /imu/features` returns `phone-vibration-features-v1` with `source:"imu"`, LIS3DH sensor identity, and `vibration_onset` when a high-crest impulse is present. `crest_factor`, `dc_offset`, and `rms` are computed over calibrated `accel_mag` in m/s² (`raw * 0.001 g / 16`, then vector magnitude), matching the phone/gotchi accelerometer contract instead of raw LIS3DH counts. Onset peak magnitude is `peak_mag_mps2`; there is no legacy consumer for the earlier misleading `peak_abs_raw` name. Disabled/not-ready responses keep the explicit `claim` block with `is_microphone:false` and `is_seismic:true`.
- `GET /imu?limit=N` returns `puc-lis3dh-raw-v1`: recent raw XYZ samples with `seq` and `mono_us`, health, and the feature block. `limit` is capped by the firmware ring.

## Required on-device validation before field use

1. Flash only a bench image first; do not deploy to the live cluster.
2. Confirm `/status.imu.ok == true`, `who_am_i == "0x33"`, bus is GPIO47/GPIO48, `odr_hz == 400`, and control readbacks are `reg1=0x77`, `reg4=0x88`, `reg5` FIFO-enabled, `fifo=0x98`.
3. Poll `/imu?limit=128` while tapping the enclosure. Verify raw XYZ changes, `mono_us` is monotonic, and feature output remains `source:"imu"` with no microphone claim.
4. Leave it running at least 10 minutes and confirm `fifo_overruns == 0`, `i2c_errors == 0`, `consecutive_i2c_errors == 0`, `short_reads == 0`, `state == "ok"`, and drops grow only if the HTTP consumer does not drain the ring.
5. Record whether INT1/INT2 is routed to an ESP32 GPIO. Until that is measured, the firmware must remain FIFO-polled.
