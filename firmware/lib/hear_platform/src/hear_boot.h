// OTA failback: count boots in RTC memory, revert the partition when an image never proves itself.
//
// SECOND module out of the sketches, and the one with a live defect behind it. This code existed
// TWICE -- night_node.ino and puc_node.ino -- and the copies had drifted apart:
//
//   mark_healthy_once()   45.2% identical
//   boot_guard()          80.9% identical
//
// night_node's copy refuses to mark an image healthy unless the node is REACHABLE, and says why in
// a comment. puc_node's copy marks healthy after 30 s unconditionally, sets proven_ok, and thereby
// switches off its own partition revert for good -- on a node that tracks sta_ok in seven other
// places and simply does not consult it here. The warning did not travel with the copy. That is the
// argument for this file, not tidiness.
//
// Failback does NOT rely on the bootloader's rollback feature: this core ships a prebuilt
// bootloader and CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE could not be confirmed, so depending on it
// would be depending on something unverified. The app counts its own boots in RTC memory, which
// survives a reset, and flips the boot partition back itself.
//
// ⚠️WHAT IT CANNOT SAVE YOU FROM: a build that faults before setup() runs -- a bad global
// constructor -- because nothing then increments the counter. And a HANG rather than a fault: this
// counts RESETS, and a setup() that never returns produces none. rankine was lost to exactly that
// on 2026-09-10; the answer is a watchdog around whatever can block, which turns the hang into a
// reset this file already handles. See night_node's boot_wdt_arm().
#pragma once
#include <Arduino.h>

#define HEAR_BOOT_MAX_TRIES     3
#define HEAR_BOOT_HEALTHY_MS    30000
//: Boots fine but never becomes reachable -> force a reboot so the counter can advance.
#define HEAR_BOOT_UNHEALTHY_MS  90000

// FIRST statement of setup(). Increments the boot counter and, past HEAR_BOOT_MAX_TRIES without a
// healthy boot, flips to the other OTA partition and restarts. Never reverts an image that has
// already proven itself: once healthy, being unreachable means the node moved or the AP changed,
// not that the firmware is bad, and reverting then is the failback doing harm in the name of safety.
void hear_boot_guard();

// Call from loop(), every pass. `reachable` is the sketch's own answer to "can anyone reach me" --
// STA associated, typically. It is a PARAMETER because that is the fact the duplicated copy forgot
// to consult, and a signature that demands it cannot be implemented while ignoring it.
//
// Marks the running image healthy once it has been up HEAR_BOOT_HEALTHY_MS *and* is reachable, and
// force-reboots an image that is up but unreachable past HEAR_BOOT_UNHEALTHY_MS so the counter
// advances toward a revert.
void hear_boot_tick(bool reachable);

uint32_t hear_boot_try();      // boots since the last healthy one, for /status
bool     hear_boot_proven();   // has this image ever reached healthy
bool     hear_boot_marked();   // has THIS run marked it (distinct from proven across resets)
