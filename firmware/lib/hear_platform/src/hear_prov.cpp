#include "hear_prov.h"
#include <Preferences.h>

static const char *NS = "hear_prov";

bool hear_prov_load(hear_prov_t *p) {
  memset(p, 0, sizeof *p);
  Preferences pr;
  if (!pr.begin(NS, true)) return false;
  bool ok = pr.getUChar("v", 0) == 1;
  int n = pr.getUChar("n", 0);
  if (ok && n >= 1 && n <= HEAR_PROV_MAX_NETS) {
    ok = pr.getString("node", p->node, sizeof p->node) > 0;
    pr.getString("class", p->cls, sizeof p->cls);
    for (int k = 0; ok && k < n; k++) {
      char ks[16], kp[16];
      snprintf(ks, sizeof ks, "s%d", k);
      snprintf(kp, sizeof kp, "p%d", k);
      ok = pr.getString(ks, p->ssid[k], sizeof p->ssid[k]) > 0
        && pr.getString(kp, p->psk[k], sizeof p->psk[k]) > 0;
    }
    p->n = n;
  } else {
    ok = false;
  }
  pr.end();
  if (!ok || !hear_prov_id_ok(p->node) || (p->cls[0] && !hear_prov_id_ok(p->cls))) {
    memset(p, 0, sizeof *p);
    return false;
  }
  return true;
}

bool hear_prov_save(const hear_prov_t *p) {
  if (!hear_prov_id_ok(p->node) || p->n < 1 || p->n > HEAR_PROV_MAX_NETS) return false;
  Preferences pr;
  if (!pr.begin(NS, false)) return false;
  bool ok = pr.clear();
  ok = ok && pr.putString("node", p->node) == strlen(p->node);
  if (ok && p->cls[0]) ok = pr.putString("class", p->cls) == strlen(p->cls);
  for (int k = 0; ok && k < p->n; k++) {
    char ks[16], kp[16];
    snprintf(ks, sizeof ks, "s%d", k);
    snprintf(kp, sizeof kp, "p%d", k);
    ok = pr.putString(ks, p->ssid[k]) == strlen(p->ssid[k])
      && pr.putString(kp, p->psk[k]) == strlen(p->psk[k]);
  }
  ok = ok && pr.putUChar("n", (uint8_t)p->n) == 1;
  ok = ok && pr.putUChar("v", 1) == 1;
  pr.end();
  return ok;
}
