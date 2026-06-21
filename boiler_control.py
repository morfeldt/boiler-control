#!/usr/bin/env python3
"""
boiler_control.py – Controls the immersion heater in a CTC 1100 boiler
based on the Nordpool day-ahead spot price.

Runs every 15 minutes via cron (required since electricity prices moved
to quarter-hour resolution on 2025-10-01):
    */15 * * * * python3 /home/pi/boiler/boiler_control.py

Fail-safe: on any error, the boiler is turned ON — you never freeze.
"""

import requests
import json
import logging
from pathlib import Path
from datetime import datetime, date

from config import (
    PRICE_ZONE, OFF_QUARTERS_TARGET, MAX_CONSEC_OFF_QUARTERS,
    BOILER_SHELLY_IP, BOILER_SHELLY_GEN,
    HT_SHELLY_IP, HT_SHELLY_GEN,
    CLOUD_AUTH_KEY, CLOUD_SERVER, HT_DEVICE_ID,
    FROST_GUARD_C, MAX_TEMP_AGE_HOURS,
    STATE_FILE, LOG_FILE, SCHEDULE_FILE,
)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)


# ── Spot prices ────────────────────────────────────────────────────────────

def get_prices() -> list[dict]:
    """Fetches today's quarter-hour prices from elprisetjustnu.se (free, no API key)."""
    d = date.today()
    url = (
        f"https://www.elprisetjustnu.se/api/v1/prices/"
        f"{d.year}/{d.month:02d}-{d.day:02d}_{PRICE_ZONE}.json"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def build_off_schedule(prices: list[dict]) -> set[str]:
    """
    Builds an off-schedule for today's quarter-hour periods: turns off
    the most expensive periods, but never more than MAX_CONSEC_OFF_QUARTERS
    in a row.

    Algorithm (greedy, most expensive first):
      1. Sort quarters chronologically so neighbors can be inspected.
      2. Walk through quarters in descending price order.
      3. Tentatively mark OFF. Measure the length of the contiguous
         OFF run around this quarter.
      4. If it exceeds the limit → undo (the quarter stays ON).
         Otherwise → keep it OFF and increment the count.
      5. Continue until OFF_QUARTERS_TARGET quarters are off, or no
         more can be turned off without breaking the constraint.

    The result is a "comb" pattern: long but bounded OFF blocks,
    broken up by forced ON quarters — preferentially placed at the
    most expensive point within each block.

    NOTE: This is computed per calendar day. An OFF period ending
    late in the evening and one starting early the next day are
    counted separately, so the real contiguous off-time across
    midnight could in theory exceed MAX_CONSEC_OFF_QUARTERS. This is
    a known limitation of this simple model.
    """
    chrono = sorted(prices, key=lambda p: p["time_start"])
    n = len(chrono)
    is_off = [False] * n

    # Indices sorted by price, most expensive first
    candidates = sorted(range(n), key=lambda i: chrono[i]["SEK_per_kWh"], reverse=True)

    off_count = 0
    for idx in candidates:
        if off_count >= OFF_QUARTERS_TARGET:
            break

        is_off[idx] = True

        # Measure the contiguous OFF run around idx
        run = 1
        i = idx - 1
        while i >= 0 and is_off[i]:
            run += 1
            i -= 1
        i = idx + 1
        while i < n and is_off[i]:
            run += 1
            i += 1

        if run > MAX_CONSEC_OFF_QUARTERS:
            is_off[idx] = False  # too risky – leave this quarter ON
        else:
            off_count += 1

    return {chrono[i]["time_start"] for i in range(n) if is_off[i]}


def write_schedule_file(prices: list[dict], off_keys: set[str]) -> None:
    """
    Writes today's full on/off plan to SCHEDULE_FILE, overwritten each run.
    CSV format with a '#'-prefixed visual bar in the header for a quick
    glance at the terminal; readers that skip '#' lines (e.g. R's
    readr::read_csv(comment = "#")) get clean tabular data straight away.
    """
    chrono = sorted(prices, key=lambda p: p["time_start"])
    bar = "".join("." if p["time_start"] in off_keys else "#" for p in chrono)
    off_count = bar.count(".")

    lines = [
        f"# Schedule generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Off quarters: {off_count}/{len(chrono)} "
        f"(target {OFF_QUARTERS_TARGET}, max consecutive off {MAX_CONSEC_OFF_QUARTERS})",
        f"# {bar}",
        "time,price_sek_per_kwh,status",
    ]
    for p in chrono:
        status = "OFF" if p["time_start"] in off_keys else "ON"
        lines.append(f"{p['time_start']},{p['SEK_per_kWh']:.4f},{status}")

    try:
        Path(SCHEDULE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(SCHEDULE_FILE).write_text("\n".join(lines) + "\n")
    except Exception as e:
        logging.warning(f"Could not write schedule file: {e}")


def current_quarter_key(prices: list[dict]) -> str:
    """Finds the time_start key for the quarter-hour period we're currently in."""
    now = datetime.now()
    for p in prices:
        t = datetime.fromisoformat(p["time_start"])
        if t.hour == now.hour and t.minute == (now.minute // 15) * 15:
            return p["time_start"]
    raise ValueError(f"No price period found for {now.strftime('%H:%M')}")


def price_for_key(prices: list[dict], key: str) -> float:
    for p in prices:
        if p["time_start"] == key:
            return p["SEK_per_kWh"]
    raise ValueError(f"No price found for {key}")


# ── Temperature sensor (Shelly H&T) ─────────────────────────────────────────

def fetch_temp_live() -> float | None:
    """
    Tries to read directly from the H&T over HTTP.
    Only works if the device happens to be awake (just reported) – None otherwise.
    """
    try:
        if HT_SHELLY_GEN == 1:
            # Gen1 H&T: /status returns tmp.value
            r = requests.get(f"http://{HT_SHELLY_IP}/status", timeout=3)
            r.raise_for_status()
            return float(r.json()["tmp"]["value"])
        else:
            # Gen2 Plus H&T: RPC endpoint
            r = requests.get(
                f"http://{HT_SHELLY_IP}/rpc/Temperature.GetStatus?id=0",
                timeout=3,
            )
            r.raise_for_status()
            return float(r.json()["tC"])
    except Exception:
        return None


def fetch_temp_cloud() -> float | None:
    """
    Fetches the last known temperature via Shelly Cloud instead of the
    local network. Works even while the H&T is asleep, since the cloud
    only ever shows the last received report – not a live value. That's
    exactly what we want here.
    """
    try:
        url = f"https://{CLOUD_SERVER}/device/all_status"
        r = requests.get(
            url,
            params={"auth_key": CLOUD_AUTH_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        device = data["data"]["devices_status"].get(HT_DEVICE_ID)
        if device is None:
            logging.warning(f"Device {HT_DEVICE_ID} not found in cloud response")
            return None

        # Gen1 H&T structure in the cloud response mirrors the local /status response
        return float(device["tmp"]["value"])
    except Exception as e:
        logging.warning(f"Cloud fetch failed: {e}")
        return None


def save_temp(temp: float) -> None:
    """Saves the last known temperature + timestamp to a file."""
    state = {"temp": temp, "timestamp": datetime.now().isoformat()}
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(state))


def load_temp() -> tuple[float, bool]:
    """
    Reads the last saved temperature.
    Returns (temp, is_fresh).
    is_fresh = False if the file is missing or too old → triggers fail-safe.
    """
    p = Path(STATE_FILE)
    if not p.exists():
        return 999.0, False
    try:
        data = json.loads(p.read_text())
        age = datetime.now() - datetime.fromisoformat(data["timestamp"])
        fresh = age.total_seconds() < MAX_TEMP_AGE_HOURS * 3600
        return float(data["temp"]), fresh
    except Exception:
        return 999.0, False


def get_indoor_temp() -> tuple[float, bool]:
    """
    Attempts to read the temperature in three steps, descending reliability:
      1. Local live value (requires the H&T to happen to be awake right now)
      2. Shelly Cloud (last reported value, regardless of sleep state)
      3. Last saved value locally on the Pi
    Returns (temp, is_reliable).
    """
    live = fetch_temp_live()
    if live is not None:
        save_temp(live)
        logging.info(f"Temp from local H&T: {live:.1f}°C")
        return live, True

    cloud = fetch_temp_cloud()
    if cloud is not None:
        save_temp(cloud)
        logging.info(f"Temp from Shelly Cloud: {cloud:.1f}°C")
        return cloud, True

    # Both local and cloud failed – use the last saved value
    logging.warning("Neither local H&T nor Cloud responded – using saved value")
    return load_temp()


# ── Boiler control (Shelly G3 Plus 1 / Plus 1) ──────────────────────────────

def set_boiler(on: bool) -> None:
    """Turns the boiler's immersion heater ON or OFF via Shelly's local HTTP API."""
    action = "true" if on else "false"
    # Gen2/Gen3 share the same RPC API
    url = f"http://{BOILER_SHELLY_IP}/rpc/Switch.Set?id=0&on={action}"
    r = requests.get(url, timeout=5)
    r.raise_for_status()


# ── Main logic ───────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()

    # 1. Fetch spot prices and build today's off-schedule – on error: fail-safe ON
    try:
        prices       = get_prices()
        off_keys     = build_off_schedule(prices)
        current_key  = current_quarter_key(prices)
        price        = price_for_key(prices, current_key)
    except Exception as e:
        logging.warning(f"Could not fetch spot prices: {e} → fail-safe ON")
        set_boiler(True)
        return

    write_schedule_file(prices, off_keys)

    # 2. Read indoor temperature
    temp, temp_fresh = get_indoor_temp()

    if not temp_fresh:
        logging.warning(
            f"Temperature data missing or older than {MAX_TEMP_AGE_HOURS}h "
            f"→ fail-safe ON"
        )
        set_boiler(True)
        return

    # 3. Decide whether the boiler should be on or off
    scheduled_off = current_key in off_keys
    frost_guard   = temp < FROST_GUARD_C
    boiler_on     = (not scheduled_off) or frost_guard

    set_boiler(boiler_on)

    # 4. Log what happened and why
    reasons = []
    if frost_guard:
        reasons.append(f"frost guard ({temp:.1f}°C < {FROST_GUARD_C}°C)")
    elif scheduled_off:
        reasons.append("scheduled off (expensive period)")
    else:
        reasons.append("normal operation")

    logging.info(
        f"Quarter {now.strftime('%H:%M')} | "
        f"{temp:.1f}°C | "
        f"{price:.3f} SEK/kWh | "
        f"Boiler: {'ON ' if boiler_on else 'OFF'} ({', '.join(reasons)})"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Unexpected error in main(): {e} → attempting to set boiler ON")
        try:
            set_boiler(True)
        except Exception as e2:
            logging.error(f"Could not even set boiler ON: {e2}")
