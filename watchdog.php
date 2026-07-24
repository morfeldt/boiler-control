<?php
/**
 * watchdog.php – Checks whether the Raspberry Pi boiler controller
 * has pinged recently. Run via cPanel cron every 30 minutes.
 *
 * Place at: public_html/boiler/watchdog.php
 *
 * cPanel cron command:
 *   /usr/local/bin/php /home/YOUR_CPANEL_USERNAME/public_html/boiler/watchdog.php
 *
 * cPanel will email the output to your account address whenever the
 * script produces any output — i.e. only when the Pi is silent.
 * No output = no email = everything is fine.
 *
 * NOTE: This is a notification-only watchdog. The actual boiler
 * fail-safe runs independently on the Shelly device via a local
 * watchdog script, and is not affected by internet outages.
 * An alert here during a brief internet outage is a false alarm
 * and can be safely ignored if the boiler is working normally.
 */

define("HEARTBEAT_FILE",    __DIR__ . "/last_heartbeat.txt");
define("WATCHDOG_TIMEOUT",  30 * 60);   // 30 minutes — one missed Pi ping

// Check if heartbeat file exists at all
if (!file_exists(HEARTBEAT_FILE)) {
    echo "ALERT: No heartbeat file found — the Pi may never have pinged, or the file was deleted.\n";
    echo "Expected location: " . HEARTBEAT_FILE . "\n";
    exit(1);
}

$last_beat = intval(file_get_contents(HEARTBEAT_FILE));
$age       = time() - $last_beat;

if ($age > WATCHDOG_TIMEOUT) {
    $mins = round($age / 60);
    echo "ALERT: Raspberry Pi boiler controller has been silent for {$mins} minutes.\n";
    echo "Last heartbeat: " . date("Y-m-d H:i:s", $last_beat) . "\n";
    echo "\n";
    echo "The boiler's local Shelly watchdog script will have forced the boiler ON\n";
    echo "if the Pi is genuinely down. Check the Pi at your earliest convenience.\n";
    echo "\n";
    echo "If there was a known internet outage, this alert can be safely ignored.\n";
    exit(1);
}

// No output = no email from cPanel cron
exit(0);
