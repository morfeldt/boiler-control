/**
 * shelly_watchdog.js – Boiler fail-safe watchdog for the Shelly G3 Plus 1.
 *
 * Install via: Shelly app → G3 Plus 1 → Scripts → Create new script.
 * Paste this script, save, and enable it. It runs continuously on the
 * Shelly device itself, independently of the Raspberry Pi.
 *
 * How it works:
 *   The Pi writes a Unix timestamp to the Shelly's KVS (key: "pi_heartbeat")
 *   on every 15-minute run. This script checks that timestamp every minute.
 *   If no heartbeat has been received for WATCHDOG_TIMEOUT_SECS and the
 *   boiler is currently OFF, the boiler is forced ON.
 *
 * This protects against Pi crashes where the boiler would otherwise remain
 * OFF indefinitely. It operates entirely over the local network and is not
 * affected by internet outages.
 *
 * NOTE: This watchdog only acts when the boiler is OFF. If the Pi crashes
 * while the boiler is ON, no action is needed — the boiler stays ON, which
 * is the safe state. The Shelly's "Power On Default Mode → ON" setting
 * covers the case where the Shelly itself loses power.
 */

let WATCHDOG_TIMEOUT_SECS = 45 * 60;   // 45 minutes = 3 missed Pi cycles
let CHECK_INTERVAL_MS     = 60 * 1000; // check every minute

Timer.set(CHECK_INTERVAL_MS, true, function() {

  // Step 1: get current NTP-synced system time from the Shelly
  Shelly.call("Sys.GetStatus", {}, function(sys, sys_err) {
    if (sys_err !== 0 || !sys || !sys.unixtime) {
      print("Watchdog: could not get system time (NTP not synced yet?)");
      return;
    }
    let now = sys.unixtime;

    // Step 2: read the Pi's last heartbeat from KVS
    Shelly.call("KVS.Get", {key: "pi_heartbeat"}, function(kvs, kvs_err) {
      if (kvs_err !== 0 || !kvs) {
        // No heartbeat written yet — Pi may not have run since Shelly boot.
        // Stay quiet rather than false-triggering on first install.
        print("Watchdog: no pi_heartbeat key in KVS yet — waiting for first Pi run");
        return;
      }

      let last_beat = Number(kvs.value);
      let age_secs  = now - last_beat;

      if (age_secs > WATCHDOG_TIMEOUT_SECS) {
        // Pi has been silent too long — check if the boiler is OFF
        let sw = Shelly.getComponentStatus("switch", 0);
        if (sw && !sw.output) {
          print(
            "Watchdog: Pi silent for " + age_secs + "s " +
            "(>" + WATCHDOG_TIMEOUT_SECS + "s) and boiler is OFF — forcing ON"
          );
          Shelly.call("Switch.Set", {id: 0, on: true}, null);
        }
        // If boiler is already ON, nothing to do
      }
    });
  });
});

print(
  "Boiler watchdog started — checking every " + (CHECK_INTERVAL_MS / 1000) + "s, " +
  "timeout: " + (WATCHDOG_TIMEOUT_SECS / 60) + " min"
);
