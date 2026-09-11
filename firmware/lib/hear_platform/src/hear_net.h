// Wi-Fi join and link instrumentation for the node sketches.
#pragma once
#include <Arduino.h>
#include "hear_prov_line.h"

typedef struct {
  int      joined;      // 1-based index into the record's networks, 0 = not joined
  int      seen;        // configured networks the boot scan heard
  int      rssi_join;   // dBm at the moment of joining
  int      channel;
  uint8_t  bssid[6];
  uint32_t join_ms;     // scan + association + DHCP
} hear_net_join_t;

// Scan, then try the configured networks strongest first, each pinned to the strongest access
// point heard for it. Networks the scan missed are tried last, unpinned. Returns `joined`.
int hear_net_join(const hear_prov_t *p, uint32_t per_try_ms, hear_net_join_t *out);

// Counts link events from the Wi-Fi event task. Call once before hear_net_join(); the counters
// restart at the join, so failed attempts during it are not counted as drops.
void     hear_net_watch();
uint32_t hear_net_disconnects();
uint32_t hear_net_reconnects();
int      hear_net_last_reason();    // wifi_err_reason_t of the latest disconnect, 0 = none
uint32_t hear_net_last_disc_ms();   // millis() of it, 0 = none
