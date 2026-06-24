#!/usr/bin/env python3
"""
boiler_control.py – Controls the immersion heater in a CTC 1100 boiler
based on the Nordpool day-ahead spot price.

Runs every 15 minutes via cron (required since electricity prices moved
to quarter-hour resolution on 2025-10-01):
    */15 * * * * python3 /home/boiler/boiler/boiler_control.py

Fail-safe: on any error, the boiler is turned ON — you never freeze.

Sensor priority for indoor/frost temperature:
  1. DS18B20 on Shelly Add-on index 100 (wired, always-on)
  2. Shelly H&T local HTTP (only when awake)
  3. Shelly H&T cloud (last reported value)
  4. Last saved value in state file

Boiler temperature guard:
  - DS18B20 on Shelly Add-on index 101, corrected by DS18B20_BOILER_OFFSET
  - If unavailable: falls back to MAX_CONSEC_OFF_QUARTERS constraint
  - Season (summer/winter) inferred from sustained corrected boiler temp readings,
    or set explicitly via SEASON_OVERRIDE in config

Radiator feed temperature:
  - DS18B20 on Shelly Add-on index 102
  - Logged only, not used for control decisions
"""

import requests
import json
import logging
from pathlib import Path
from datetime import datetime, date

from config import (
    PRICE_ZONE, OFF_QUARTERS_TARGET, MAX_CONSEC_OFF_QUARTERS,
    BOILER_SHELLY_IP,
    HT_SHELLY_IP, HT_SHELLY_GEN,
    CLOUD_AUTH_KEY, CLOUD_SERVER, HT_DEVICE_ID,
    DS18B20_INDOOR_INDEX, DS18B20_BOILER_INDEX, DS18B20_FEED_INDEX,
    DS18B20_BOILER_OFFSET,
    SEASON_OVERRIDE, SEASON_CHANGE_READINGS, SEASON_INFERENCE_THRESHOLD_C,
    BOILER_MIN_TEMP_SUMMER_C, BOILER_MIN_TEMP_WINTER_C,
    FROST_GUARD_C, MAX_TEMP_AGE_HOURS,
    STATE_FILE, LOG_FILE, SCHEDULE_FILE,
)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)


# ── State file ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """
    Loads the full persistent state from disk.
    Returns an empty dict (safe defaults) if the file is missing or corrupt.
    Handles backward compatibility with the old 'temp'/'timestamp' key names.
    """
    p = Path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        # Backward compatibility: migrate old key names on first load
        if "temp" in data and "indoor_temp" not in data:
            data["indoor_temp"] = data.pop("temp")
        if "timestamp" in data and "indoor_temp_timestamp" not in data:
            data["indoor_temp_timestamp"] = data.pop("timestamp")
        return data
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """Saves the full persistent state to disk."""
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        logging.warning(f"Could not save state file: {e}")


# ── Spot prices ────────────────────────────────────────────────────────────────

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


def build_off_schedule(prices: list[dict], use_consec_constraint: bool) -> set[str]:
    """
    Builds an off-schedule for today's quarter-hour periods.

    When the boiler temperature sensor is available (use_consec_constraint=False):
      Pure greedy — turn off the most expensive OFF_QUARTERS_TARGET quarters.
      The boiler temp guard in main() handles hot-water safety directly.

    When the boiler temperature sensor is unavailable (use_consec_constraint=True):
      Greedy with run-length constraint — never more than MAX_CONSEC_OFF_QUARTERS
      consecutive OFF quarters. This is the fallback proxy for hot-water safety.

    NOTE: Schedule is computed per calendar day. An OFF period ending late and
    one starting early the next day are counted separately, so the real
    contiguous off-time across midnight may exceed MAX_CONSEC_OFF_QUARTERS.
    This is a known limitation of the per-day model.
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

        if use_consec_constraint:
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
                is_off[idx] = False  # too risky without temp sensor – leave ON
                continue

        off_count += 1

    return {chrono[i]["time_start"] for i in range(n) if is_off[i]}


def write_schedule_file(
    prices: list[dict],
    off_keys: set[str],
    season: str,
    boiler_min_temp: float,
    boiler_temp_available: bool,
    actuals: dict,
) -> None:
    """
    Writes today's full on/off plan to SCHEDULE_FILE, overwritten each run.

    Columns:
      time              – quarter start, HH:MM
      price_sek_per_kwh – Nordpool spot price for this quarter
      scheduled         – what the price schedule says (ON/OFF)
      actual            – what actually happened (ON/OFF), empty for future quarters

    CSV format with '#'-prefixed header lines for a quick terminal glance;
    readers that skip '#' lines (e.g. R's readr::read_csv(comment = "#"))
    get clean tabular data straight away.
    """
    chrono = sorted(prices, key=lambda p: p["time_start"])
    bar = "".join("." if p["time_start"] in off_keys else "#" for p in chrono)
    off_count = bar.count(".")

    constraint_note = (
        "boiler temp guard active"
        if boiler_temp_available
        else f"fallback: max consecutive off {MAX_CONSEC_OFF_QUARTERS}"
    )

    lines = [
        f"# Schedule generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Season: {season} | Boiler min temp: {boiler_min_temp}°C | {constraint_note}",
        f"# Off quarters: {off_count}/{len(chrono)} (target {OFF_QUARTERS_TARGET})",
        f"# {bar}",
        "time,price_sek_per_kwh,scheduled,actual",
    ]
    for p in chrono:
        scheduled = "OFF" if p["time_start"] in off_keys else "ON"
        actual    = actuals.get(p["time_start"], "")
        t         = datetime.fromisoformat(p["time_start"])
        time_str  = t.strftime("%H:%M")
        lines.append(f"{time_str},{p['SEK_per_kWh']:.4f},{scheduled},{actual}")

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


# ── DS18B20 sensors via Shelly Add-on ─────────────────────────────────────────

def fetch_addon_temp(index: int) -> float | None:
    """
    Reads a DS18B20 sensor from the Shelly Add-on attached to the boiler Shelly.
    The Add-on shares the boiler Shelly's IP; sensors appear as Temperature
    components with indices 100, 101, 102, etc.
    Returns raw °C, or None on any error.
    """
    try:
        r = requests.get(
            f"http://{BOILER_SHELLY_IP}/rpc/Temperature.GetStatus?id={index}",
            timeout=3,
        )
        r.raise_for_status()
        return float(r.json()["tC"])
    except Exception:
        return None


# ── Indoor temperature (DS18B20 → H&T local → H&T cloud → saved) ─────────────

def fetch_ht_live() -> float | None:
    """
    Tries to read directly from the H&T over HTTP.
    Only works if the device happens to be awake — None otherwise.
    """
    try:
        if HT_SHELLY_GEN == 1:
            r = requests.get(f"http://{HT_SHELLY_IP}/status", timeout=3)
            r.raise_for_status()
            return float(r.json()["tmp"]["value"])
        else:
            r = requests.get(
                f"http://{HT_SHELLY_IP}/rpc/Temperature.GetStatus?id=0",
                timeout=3,
            )
            r.raise_for_status()
            return float(r.json()["tC"])
    except Exception:
        return None


def fetch_ht_cloud() -> tuple[float, datetime] | None:
    """
    Fetches the last known temperature via Shelly Cloud.
    Works even while the H&T is asleep. Returns (temp, reading_timestamp)
    using the device's own '_updated' field so the logged time reflects
    when the H&T actually took the reading.
    """
    try:
        r = requests.get(
            f"https://{CLOUD_SERVER}/device/all_status",
            params={"auth_key": CLOUD_AUTH_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        device = data["data"]["devices_status"].get(HT_DEVICE_ID)
        if device is None:
            logging.warning(f"Device {HT_DEVICE_ID} not found in cloud response")
            return None

        temp = float(device["tmp"]["value"])
        updated_str = device.get("_updated")
        try:
            reading_time = datetime.strptime(updated_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            reading_time = datetime.now()

        return temp, reading_time
    except Exception as e:
        logging.warning(f"Cloud fetch failed: {e}")
        return None


def get_indoor_temp(
    raw_ds18b20: float | None,
    state: dict,
    now: datetime,
) -> tuple[float, datetime, bool]:
    """
    Returns (temp, reading_timestamp, is_reliable) for the indoor temperature.

    Priority chain:
      1. DS18B20 index 100 via Shelly Add-on (wired, always-on, most reliable)
      2. H&T local HTTP (only when device is awake)
      3. H&T Shelly Cloud (last reported value, regardless of sleep state)
      4. Last saved value in state file (stale if older than MAX_TEMP_AGE_HOURS)

    State is updated in-place when a fresh reading is obtained.
    """
    # 1. DS18B20 via Add-on
    if raw_ds18b20 is not None:
        state["indoor_temp"] = raw_ds18b20
        state["indoor_temp_timestamp"] = now.isoformat()
        return raw_ds18b20, now, True

    # 2. H&T local
    live = fetch_ht_live()
    if live is not None:
        state["indoor_temp"] = live
        state["indoor_temp_timestamp"] = now.isoformat()
        return live, now, True

    # 3. H&T cloud
    cloud = fetch_ht_cloud()
    if cloud is not None:
        temp, reading_time = cloud
        state["indoor_temp"] = temp
        state["indoor_temp_timestamp"] = reading_time.isoformat()
        return temp, reading_time, True

    # 4. Last saved value
    try:
        saved_temp = float(state.get("indoor_temp", 999.0))
        saved_ts   = datetime.fromisoformat(state["indoor_temp_timestamp"])
        age        = now - saved_ts
        fresh      = age.total_seconds() < MAX_TEMP_AGE_HOURS * 3600
        return saved_temp, saved_ts, fresh
    except (KeyError, ValueError, TypeError):
        return 999.0, now, False


# ── Season inference ──────────────────────────────────────────────────────────

def determine_season(corrected_boiler_temp: float, state: dict) -> str:
    """
    Determines the current heating season (summer/winter) and updates state.

    Priority:
      1. SEASON_OVERRIDE in config ("summer" / "winter") — explicit, always wins
      2. Persisted season in state file, flipped only after
         SEASON_CHANGE_READINGS consecutive readings suggesting the other season.
         This prevents a momentary boiler temp dip from incorrectly flipping
         the season mid-winter.
      3. If no history yet: infer directly from current corrected boiler temp.

    Season is inferred as "winter" when corrected boiler temp is at or above
    SEASON_INFERENCE_THRESHOLD_C, "summer" below it.
    """
    # 1. Explicit override always wins
    if SEASON_OVERRIDE is not None:
        return SEASON_OVERRIDE

    suggested = (
        "winter"
        if corrected_boiler_temp >= SEASON_INFERENCE_THRESHOLD_C
        else "summer"
    )

    current_season = state.get("season")

    # 3. No history yet — use current reading directly
    if current_season is None:
        state["season"] = suggested
        state["season_consecutive_readings"] = 0
        logging.info(f"Season initialised to '{suggested}' "
                     f"(boiler {corrected_boiler_temp:.1f}°C, "
                     f"threshold {SEASON_INFERENCE_THRESHOLD_C}°C)")
        return suggested

    # 2. Sustained-reading logic
    if suggested == current_season:
        # Reading confirms current season — reset counter
        state["season_consecutive_readings"] = 0
    else:
        # Reading suggests a flip — increment counter
        count = state.get("season_consecutive_readings", 0) + 1
        state["season_consecutive_readings"] = count
        if count >= SEASON_CHANGE_READINGS:
            state["season"] = suggested
            state["season_consecutive_readings"] = 0
            logging.info(
                f"Season changed from '{current_season}' to '{suggested}' "
                f"after {SEASON_CHANGE_READINGS} consecutive readings "
                f"(boiler {corrected_boiler_temp:.1f}°C)"
            )
            return suggested

    return current_season


# ── Boiler control ────────────────────────────────────────────────────────────

def set_boiler(on: bool) -> None:
    """Turns the boiler's immersion heater ON or OFF via Shelly's local HTTP API."""
    action = "true" if on else "false"
    url = f"http://{BOILER_SHELLY_IP}/rpc/Switch.Set?id=0&on={action}"
    r = requests.get(url, timeout=5)
    r.raise_for_status()


# ── Main logic ────────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()

    # 1. Load persistent state
    state = load_state()

    # Prune actuals when the calendar day rolls over — they're only meaningful
    # within a single day's schedule
    today_str = date.today().isoformat()
    if state.get("actuals_date") != today_str:
        state["actuals"]      = {}
        state["actuals_date"] = today_str

    # 2. Read all three DS18B20 sensors from Shelly Add-on
    raw_indoor  = fetch_addon_temp(DS18B20_INDOOR_INDEX)
    raw_boiler  = fetch_addon_temp(DS18B20_BOILER_INDEX)
    raw_feed    = fetch_addon_temp(DS18B20_FEED_INDEX)

    # Apply calibration offset to boiler sensor
    boiler_temp_available  = raw_boiler is not None
    corrected_boiler_temp  = (raw_boiler + DS18B20_BOILER_OFFSET) if boiler_temp_available else None

    # 3. Determine season and boiler minimum temperature threshold
    if boiler_temp_available:
        season = determine_season(corrected_boiler_temp, state)
    else:
        # Can't infer season without boiler temp — use persisted value,
        # default to "winter" (safer: higher minimum temp threshold)
        season = SEASON_OVERRIDE or state.get("season", "winter")
        logging.warning("Boiler temp sensor unavailable — using persisted/default season")

    boiler_min_temp = (
        BOILER_MIN_TEMP_WINTER_C if season == "winter" else BOILER_MIN_TEMP_SUMMER_C
    )

    # 4. Fetch spot prices and build off-schedule
    # With boiler temp sensor: pure greedy (temp guard handles hot-water safety)
    # Without boiler temp sensor: greedy with MAX_CONSEC_OFF_QUARTERS constraint
    try:
        prices      = get_prices()
        off_keys    = build_off_schedule(prices, use_consec_constraint=not boiler_temp_available)
        current_key = current_quarter_key(prices)
        price       = price_for_key(prices, current_key)
    except Exception as e:
        logging.warning(f"Could not fetch spot prices: {e} → fail-safe ON")
        save_state(state)
        set_boiler(True)
        return

    # 5. Get indoor temperature (DS18B20 → H&T chain → saved state)
    indoor_temp, indoor_time, indoor_fresh = get_indoor_temp(raw_indoor, state, now)

    if not indoor_fresh:
        logging.warning(
            f"Indoor temperature data missing or older than {MAX_TEMP_AGE_HOURS}h "
            f"→ fail-safe ON"
        )
        save_state(state)
        set_boiler(True)
        return

    # 6. Decide whether the boiler should be on or off
    scheduled_off      = current_key in off_keys
    frost_guard        = indoor_temp < FROST_GUARD_C
    boiler_temp_guard  = (
        boiler_temp_available and corrected_boiler_temp < boiler_min_temp
    )
    boiler_on = (not scheduled_off) or frost_guard or boiler_temp_guard

    set_boiler(boiler_on)

    # 7. Record the actual decision for this quarter, then write the schedule file.
    # Writing after the decision means the current quarter's actual is always
    # included — past quarters already have their actuals from previous runs.
    state.setdefault("actuals", {})[current_key] = "ON" if boiler_on else "OFF"
    write_schedule_file(
        prices, off_keys, season, boiler_min_temp, boiler_temp_available,
        state["actuals"],
    )

    # 8. Save state and log the decision
    save_state(state)

    reasons = []
    if frost_guard:
        reasons.append(f"frost guard ({indoor_temp:.1f}°C < {FROST_GUARD_C}°C)")
    if boiler_temp_guard:
        reasons.append(
            f"boiler temp guard ({corrected_boiler_temp:.1f}°C < {boiler_min_temp}°C)"
        )
    if not frost_guard and not boiler_temp_guard:
        reasons.append(
            "scheduled off (expensive period)" if scheduled_off else "normal operation"
        )

    boiler_str = f"{corrected_boiler_temp:.1f}°C" if boiler_temp_available else "n/a"
    feed_str   = f"{raw_feed:.1f}°C"              if raw_feed is not None  else "n/a"

    logging.info(
        f"Quarter {now.strftime('%H:%M')} | "
        f"Indoor: {indoor_temp:.1f}°C (read {indoor_time.strftime('%H:%M')}) | "
        f"Boiler: {boiler_str} | Feed: {feed_str} | "
        f"Season: {season} | {price:.3f} SEK/kWh | "
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
