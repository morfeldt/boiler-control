<?php
/**
 * heartbeat.php – Receives a keep-alive ping from the Raspberry Pi
 * boiler controller every 15 minutes. Writes the current Unix timestamp
 * to a local file so the cPanel cron watchdog can check it.
 *
 * Place at: public_html/boiler/heartbeat.php
 * Keep the token secret — it's the only thing preventing others from
 * pinging this endpoint and masking a real Pi outage.
 */

define("SECRET_TOKEN", "REPLACE_WITH_YOUR_SECRET_TOKEN");

// Validate token
if (!isset($_GET["token"]) || $_GET["token"] !== SECRET_TOKEN) {
    http_response_code(403);
    exit;
}

// Write current timestamp
file_put_contents(__DIR__ . "/last_heartbeat.txt", time());
http_response_code(200);
