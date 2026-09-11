// Enrolled identity and Wi-Fi in NVS. The NVS partition is outside both OTA slots, so what
// enroll.py stores here survives every update and one release image serves every node.
#pragma once
#include <Arduino.h>
#include "hear_prov_line.h"

// True only for a complete record (version, a valid node id, at least one network). *p is zeroed
// otherwise.
bool hear_prov_load(hear_prov_t *p);

// Replaces the stored record. The version key is written last, so an interrupted save leaves no
// record rather than a partial one.
bool hear_prov_save(const hear_prov_t *p);
