# Security Review: Dama-Hear Endpoints, Firmware, and MQTT Transport

## Summary Table

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🔴 CRITICAL | firmware/hear_node/hear_node.ino | 3541-3580, 3829-3890 | Unauthenticated administrative & firmware upload endpoints (/update, /reboot, /format) | 10/10 |
| 2 | 🟠 HIGH     | firmware/hear_node/hear_node.ino | 3540, 3571-3618 | Unauthenticated sensor telemetry & audio extraction (/detections, /audio, /sd, /status) | 10/10 |
| 3 | 🟠 HIGH     | firmware/hear_node/hear_node.ino | 2463-2478 | Insecure TLS certificate verification disabled (`setInsecure()`) on push telemetry | 10/10 |
| 4 | 🟠 HIGH     | tools/hear_mqtt_bridge.py | 39-47, 125-145 | Unauthenticated plaintext MQTT bridge connection and missing mTLS client verification | 10/10 |
| 5 | 🟠 HIGH     | firmware/hear_node/hear_node.ino | 185, 3454-3463 | Hardcoded shared fallback AP credential (`damahear`) across fleet nodes | 9/10 |
| 6 | 🟠 HIGH     | firmware/hear_node/secrets.h | 4-6 | Static credentials compiled into firmware artifacts | 10/10 |
| 7 | 🟠 HIGH     | tools/hear_heartbeat_receiver.py | 214-274, 331-359 | Plaintext HTTP heartbeat ingestion with shared bearer token transmission | 9/10 |

---

## Detailed Findings

### 1. 🔴 CRITICAL: Unauthenticated Administrative Endpoints (`/update`, `/reboot`, `/format`)
- **Location:** `firmware/hear_node/hear_node.ino:3541-3580, 3829-3890, 4135-4158`
- **Description:** Port 80 HTTP server accepts unauthenticated OTA firmware updates, remote reboot requests, and SD card format operations without cryptographic signature checks, challenge-response auth, or rate limits.
- **Remediation:** Guard privileged endpoints behind compile-time debug flags, require mTLS/HMAC authorization, and enforce secure boot with cryptographic image signatures.

### 2. 🟠 HIGH: Unauthenticated Audio, Detection, and SD Access
- **Location:** `firmware/hear_node/hear_node.ino:3540, 3571-3618, 4158-4250`
- **Description:** Sensitive acoustic sensor data, raw microphone audio recordings, GPS coordinates, and arbitrary SD card paths are readable over unauthenticated plaintext HTTP.
- **Remediation:** Enforce authentication boundaries on data endpoints and restrict file access to allowlisted paths.

### 3. 🟠 HIGH: Telemetry TLS Verification Disabled (`setInsecure()`)
- **Location:** `firmware/hear_node/hear_node.ino:2463-2478`
- **Description:** Ingest client explicitly calls `client.setInsecure()`, opening outbound telemetry and bearer tokens to MITM interception and spoofing.
- **Remediation:** Embed private CA certificates with `setCACert()` and enforce mutual TLS (mTLS) with per-node client certificates.

### 4. 🟠 HIGH: Plaintext MQTT Transport Without mTLS
- **Location:** `tools/hear_mqtt_bridge.py:39-47, 125-145`, `deploy/k8s/hear-mqtt-bridge.yaml:29, 49-51`
- **Description:** Telemetry bridge connects to MQTT brokers over unencrypted plaintext ports without mutual TLS (mTLS) client verification, trusting untrusted `device_id` payloads.
- **Remediation:** Enforce TLS port 8883 with mTLS (`tls_set` using dedicated client cert/key), validate CA chains, and enforce topic ACLs matched against device certificates.

### 5. 🟠 HIGH: Shared Static Fallback AP Password
- **Location:** `firmware/hear_node/hear_node.ino:185, 3454-3463`
- **Description:** All nodes share `AP_PASS "damahear"` on Wi-Fi connection loss, granting local attackers direct access to administrative endpoints.
- **Remediation:** Provision unique per-device fallback credentials during initial flashing.

### 6. 🟠 HIGH: Hardcoded Ingest Credentials in Firmware
- **Location:** `firmware/hear_node/secrets.h:4-6`
- **Description:** Static fleet-wide push tokens are compiled directly into binary firmware images.
- **Remediation:** Move credentials to encrypted NVS storage and provision unique per-device keys.

### 7. 🟠 HIGH: Plaintext HTTP Heartbeat Receiver
- **Location:** `tools/hear_heartbeat_receiver.py:214-274, 331-359`
- **Description:** Ingest daemon serves on unencrypted HTTP and transmits bearer tokens in plaintext headers across the host network.
- **Remediation:** Place ingestion behind TLS-terminating reverse proxy or direct HTTPS with mutual certificate validation.
