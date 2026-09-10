#include "hear_boot.h"
#include "hear_log.h"
#include "esp_ota_ops.h"

#define HEAR_BOOT_MAGIC 0xB0074A11UL

RTC_NOINIT_ATTR static uint32_t boot_magic;
RTC_NOINIT_ATTR static uint32_t boot_try_n;
RTC_NOINIT_ATTR static uint32_t proven_ok;
static bool marked_healthy = false;
static uint32_t boot_ms = 0;

uint32_t hear_boot_try()    { return boot_try_n; }
bool     hear_boot_proven() { return proven_ok != 0; }
bool     hear_boot_marked() { return marked_healthy; }

void hear_boot_guard() {
  boot_ms = millis();
  if (boot_magic != HEAR_BOOT_MAGIC) { boot_magic = HEAR_BOOT_MAGIC; boot_try_n = 0; proven_ok = 0; }
  boot_try_n++;
  if (proven_ok) { boot_try_n = 0; return; }
  if (boot_try_n > HEAR_BOOT_MAX_TRIES) {
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    boot_try_n = 0;
    if (other && esp_ota_set_boot_partition(other) == ESP_OK) {
      hear_logf("\nBOOT GUARD: %d boots without reaching healthy -- reverting to %s\n",
                HEAR_BOOT_MAX_TRIES, other->label);
      Serial.flush(); delay(200); esp_restart();
    }
  }
}

void hear_boot_tick(bool reachable) {
  if (marked_healthy) return;
  uint32_t up = millis() - boot_ms;
  // An image that runs happily but never joins WiFi cannot be recovered over the air and will never
  // reboot on its own, so the counter never advances. Force it: unreachable for long enough IS a
  // failed boot, and HEAR_BOOT_MAX_TRIES of those revert the partition.
  if (!proven_ok && up > HEAR_BOOT_UNHEALTHY_MS && !reachable) {
    hear_logln("boot  never became reachable -- rebooting so the failback counter advances");
    Serial.flush(); delay(200); esp_restart();
  }
  if (up < HEAR_BOOT_HEALTHY_MS) return;
  // ⚠️HEALTHY MUST INCLUDE REACHABLE. Marking an unreachable image healthy sets proven_ok and
  // switches the revert off permanently, stranding a node that boots into a corner. puc_node's
  // copy of this function did exactly that -- 30 s and a flag, with no reachability test -- and
  // this parameter exists so that the omission is not expressible.
  if (!reachable) return;
  marked_healthy = true;
  boot_try_n = 0; proven_ok = 1;
  esp_ota_mark_app_valid_cancel_rollback();   // harmless if the bootloader ignores it
  hear_logln("boot  marked healthy; failback counter cleared");
}
