#include "hear_net.h"
#include "hear_log.h"
#include <WiFi.h>

static volatile uint32_t disc_n = 0, reconn_n = 0, last_disc_ms = 0;
static volatile int last_reason = 0;
static volatile bool joined_once = false;

uint32_t hear_net_disconnects()  { return disc_n; }
uint32_t hear_net_reconnects()   { return reconn_n; }
int      hear_net_last_reason()  { return last_reason; }
uint32_t hear_net_last_disc_ms() { return last_disc_ms; }

void hear_net_watch() {
  WiFi.onEvent([](arduino_event_id_t e, arduino_event_info_t info) {
    if (e == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      disc_n = disc_n + 1;
      last_reason = info.wifi_sta_disconnected.reason;
      last_disc_ms = millis();
    } else if (e == ARDUINO_EVENT_WIFI_STA_GOT_IP && joined_once) {
      reconn_n = reconn_n + 1;
    }
  });
}

int hear_net_join(const hear_prov_t *p, uint32_t per_try_ms, hear_net_join_t *out) {
  memset(out, 0, sizeof *out);
  if (p->n < 1) return 0;
  uint32_t t0 = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  int best[HEAR_PROV_MAX_NETS], ch[HEAR_PROV_MAX_NETS];
  uint8_t bs[HEAR_PROV_MAX_NETS][6];
  for (int k = 0; k < p->n; k++) { best[k] = -127; ch[k] = 0; memset(bs[k], 0, 6); }
  int found = WiFi.scanNetworks(false, true);
  for (int i = 0; i < found; i++) {
    String s = WiFi.SSID(i);
    int r = WiFi.RSSI(i);
    for (int k = 0; k < p->n; k++) {
      if (s == p->ssid[k] && r > best[k]) {
        best[k] = r;
        ch[k] = WiFi.channel(i);
        const uint8_t *b = WiFi.BSSID(i);
        if (b) memcpy(bs[k], b, 6);
      }
    }
  }
  WiFi.scanDelete();

  int order[HEAR_PROV_MAX_NETS];
  for (int k = 0; k < p->n; k++) {
    order[k] = k;
    if (best[k] > -127) out->seen++;
  }
  for (int a = 1; a < p->n; a++)          // strongest first; stable, so ties keep the record order
    for (int b = a; b > 0 && best[order[b]] > best[order[b - 1]]; b--) {
      int t = order[b]; order[b] = order[b - 1]; order[b - 1] = t;
    }
  hear_logf("wifi  scan heard %d of %d configured networks (%d access points)\n",
            out->seen, p->n, found < 0 ? 0 : found);

  for (int j = 0; j < p->n; j++) {
    int k = order[j];
    bool pinned = best[k] > -127;
    if (pinned) hear_logf("wifi  trying network %d/%d  rssi %d ch %d\n", k + 1, p->n, best[k], ch[k]);
    else        hear_logf("wifi  trying network %d/%d  not heard by the scan\n", k + 1, p->n);
    if (pinned) WiFi.begin(p->ssid[k], p->psk[k], ch[k], bs[k]);
    else        WiFi.begin(p->ssid[k], p->psk[k]);
    uint32_t t1 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t1 < per_try_ms) delay(100);
    if (WiFi.status() == WL_CONNECTED) {
      out->joined = k + 1;
      out->rssi_join = WiFi.RSSI();
      out->channel = WiFi.channel();
      const uint8_t *b = WiFi.BSSID();
      if (b) memcpy(out->bssid, b, 6);
      out->join_ms = millis() - t0;
      disc_n = 0; reconn_n = 0; last_reason = 0; last_disc_ms = 0;
      joined_once = true;
      return out->joined;
    }
    WiFi.disconnect();
  }
  out->join_ms = millis() - t0;
  return 0;
}
