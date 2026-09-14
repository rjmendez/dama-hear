# Gold: first/reference ESP32-S3 I2S + GPS node

**Gold** is named for Robert Gold and for Gold codes in satellite communications. Its stable
node identity is `gold`; its reusable hardware profile is `esp32s3-i2s-gps`. The identity does
not copy the profile's low-level board definitions.

## Known before first power-on

The operator reports an assembled ESP32-S3 node with:

- one external mono I2S microphone;
- GPS over UART with a 1PPS signal;
- the board's onboard addressable RGB LED on GPIO48.

Optional LoRa, BME-family environment sensing, SD storage, and I2C are disabled. Their absence
must remain a supported configuration, not a boot fault. No serial port, MAC address, GPS model,
microphone model, measured sample rate, or measured pin behavior is recorded.

Gold is not connected. The statements above come from the operator's assembly and profile
description, not from a live probe. Hardware revision, module markings, solder/photo evidence,
and a measured pin-map record are pending discovery.

## Measurement gates

Gold is **not yet TDoA-eligible**. Admission requires observed 1PPS edges, audio-path calibration,
and a measured end-to-end timing path. Microphone response, gain, noise floor, sample-rate error,
GPS behavior, RGB behavior, capture latency, calibration constants, and environmental effects
are all pending measurement.

The intended first deployment batch contains three assembled nodes. Gold is node 1 and the
reference validation unit; it is not proof that its pin map applies to another board. Construction
of nodes 2 and 3 is gated on:

1. bare-board pin discovery;
2. successful Gold bring-up and timing/audio validation;
3. recording Gold's exact hardware revision/module markings;
4. preserving pin-map evidence (source document plus scan/observation receipt); and
5. matching each later board to that revision and evidence, or repeating discovery when it does
   not match.

`hear/nodeidentity.py` keeps `hardware_revision` and `pin_map_evidence` unset until those receipts
exist, so Gold cannot currently authorize replication.
