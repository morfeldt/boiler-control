#!/usr/bin/env python3
"""
boiler_control.py – Controls the immersion heater in a CTC 1100 boiler
based on the Nordpool day-ahead spot price and occupancy mode.

Runs every 15 minutes via cron:
    */15 * * * * python3 /home/boiler/boiler/boiler_control.py

Occupancy is set via a Homebridge virtual switch (homebridge-cmdswitch2)
that writes "home" or "away" to OCCUPANCY_FILE. Defaults to "home" if the
file is missing — so a Pi reboot or crash always returns to home mode.

Operating modes:
  Home (summer or winter)
    Price schedule + boiler temp guard + frost guard

  Away (summer or winter)
    Boiler OFF by default.
    When frost risk is detected (indoor or outside temp below threshold),
    the full Home mode system activates (price schedule + all guards).
    Season is still inferred to select the correct boiler min temp.

Sensor priority for indoor/frost temperature:
  1. DS18B20 on Shelly Add-on index 100 (wired, always-on)
  2. Shelly H&T local HTTP (only when awake)
  3. Shelly H&T cloud (last reported value)
  4. Last saved value in state file

Boiler temperature guard:
  - DS18B20 on Shelly Add-on index 101, corrected by DS18B20_BOILER_OFFSET
  - If unavailable: falls back to MAX_CONSEC_OFF_QUARTERS constraint
  - Season inferred from sustained corrected boiler temp readings,
    or set explicitly via SEASON_OVERRIDE in config

Radiator feed temperature:
  - DS18B20 on Shelly Add-on index 102
  - Logged only, not used for control decisions

Fail-safe: on any error, the boiler is turned ON — you never freeze.
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
    OCCUPANCY_FILE,
    WEATHER_LAT, WEATHER_LON,
    AWAY_WINTER_ACTIVATE_INDOOR_C, AWAY_WINTER_DEACTIVATE_INDOOR_C,
    AWAY_WINTER_ACTIVATE_OUTSIDE_C, AWAY_WINTER_DEACTIVATE_OUTSIDE_C,
    SERVER_HEARTBEAT_URL, SERVER_HEARTBEAT_TOKEN,
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
    candidates = sorted(range(n), key=lambda i: chrono[i]["SEK_per_kWh"], reverse=True)

    off_count = 0
    for idx in candidates:
        if off_count >= OFF_QUARTERS_TARGET:
            break
        is_off[idx] = True
        if use_consec_constraint:
            run = 1
            i = idx - 1
            while i >= 0 and is_off[i]:
                run += 1; i -= 1
            i = idx + 1
            while i < n and is_off[i]:
                run += 1; i += 1
            if run > MAX_CONSEC_OFF_QUARTERS:
                is_off[idx] = False
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
    occupancy: str,
    outside_temp: float | None,
) -> None:
    """
    Writes today's full on/off plan to SCHEDULE_FILE, overwritten each run.

    Columns:
      time              – quarter start, HH:MM
      price_sek_per_kwh – Nordpool spot price for this quarter
      scheduled         – what the price schedule says (ON/OFF)
      actual            – what actually happened (ON/OFF), empty for future quarters

    In Away + Summer mode, all quarters are OFF regardless of price.
    The schedule is still written for reference (prices visible), but a
    header note indicates the boiler is off due to occupancy.
    """
    chrono    = sorted(prices, key=lambda p: p["time_start"])
    bar       = "".join("." if p["time_start"] in off_keys else "#" for p in chrono)
    off_count = bar.count(".")

    constraint_note = (
        "boiler temp guard active"
        if boiler_temp_available
        else f"fallback: max consecutive off {MAX_CONSEC_OFF_QUARTERS}"
    )
    outside_str = f"{outside_temp:.1f}°C" if outside_temp is not None else "n/a"

    lines = [
        f"# Schedule generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Occupancy: {occupancy} | Season: {season} | "
        f"Outside: {outside_str} | Boiler min: {boiler_min_temp}°C | "
        f"{constraint_note}",
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
    """Tries to read directly from the H&T over HTTP. Only works when awake."""
    try:
        if HT_SHELLY_GEN == 1:
            r = requests.get(f"http://{HT_SHELLY_IP}/status", timeout=3)
            r.raise_for_status()
            return float(r.json()["tmp"]["value"])
        else:
            r = requests.get(
                f"http://{HT_SHELLY_IP}/rpc/Temperature.GetStatus?id=0", timeout=3,
            )
            r.raise_for_status()
            return float(r.json()["tC"])
    except Exception:
        return None


def fetch_ht_cloud() -> tuple[float, datetime] | None:
    """
    Fetches the last known temperature via Shelly Cloud.
    Works even while the H&T is asleep. Returns (temp, reading_timestamp).
    """
    try:
        r = requests.get(
            f"https://{CLOUD_SERVER}/device/all_status",
            params={"auth_key": CLOUD_AUTH_KEY},
            timeout=10,
        )
        r.raise_for_status()
        device = r.json()["data"]["devices_status"].get(HT_DEVICE_ID)
        if device is None:
            logging.warning(f"Device {HT_DEVICE_ID} not found in cloud response")
            return None
        temp = float(device["tmp"]["value"])
        try:
            reading_time = datetime.strptime(device.get("_updated"), "%Y-%m-%d %H:%M:%S")
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
      1. DS18B20 index 100 via Shelly Add-on
      2. H&T local HTTP
      3. H&T Shelly Cloud
      4. Last saved value in state file
    """
    if raw_ds18b20 is not None:
        state["indoor_temp"] = raw_ds18b20
        state["indoor_temp_timestamp"] = now.isoformat()
        return raw_ds18b20, now, True

    live = fetch_ht_live()
    if live is not None:
        state["indoor_temp"] = live
        state["indoor_temp_timestamp"] = now.isoformat()
        return live, now, True

    cloud = fetch_ht_cloud()
    if cloud is not None:
        temp, reading_time = cloud
        state["indoor_temp"] = temp
        state["indoor_temp_timestamp"] = reading_time.isoformat()
        return temp, reading_time, True

    try:
        saved_temp = float(state.get("indoor_temp", 999.0))
        saved_ts   = datetime.fromisoformat(state["indoor_temp_timestamp"])
        age        = now - saved_ts
        fresh      = age.total_seconds() < MAX_TEMP_AGE_HOURS * 3600
        return saved_temp, saved_ts, fresh
    except (KeyError, ValueError, TypeError):
        return 999.0, now, False


# ── Outside temperature (Open-Meteo API) ──────────────────────────────────────

def get_outside_temp() -> float | None:
    """
    Fetches current outside temperature from Open-Meteo (free, no API key).
    Only called in Away + Winter mode where it's needed for the heating guard.
    """
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  WEATHER_LAT,
                "longitude": WEATHER_LON,
                "current":   "temperature_2m",
            },
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["current"]["temperature_2m"])
    except Exception as e:
        logging.warning(f"Could not fetch outside temperature: {e}")
        return None


# ── Occupancy ─────────────────────────────────────────────────────────────────

def get_occupancy() -> str:
    """
    Reads the occupancy state written by the Homebridge virtual switch.
    Returns "home" or "away". Defaults to "home" if the file is missing
    or unreadable — so a Pi reboot or file deletion always returns to
    home mode, ensuring the boiler is not left fully off unexpectedly.
    """
    try:
        val = Path(OCCUPANCY_FILE).read_text().strip().lower()
        if val in ("home", "away"):
            return val
        logging.warning(f"Unexpected occupancy value '{val}' — defaulting to 'home'")
        return "home"
    except Exception:
        return "home"


# ── Away + Winter frost protection state ──────────────────────────────────────

def update_frost_protection(
    state: dict,
    indoor_temp: float,
    outside_temp: float | None,
) -> bool:
    """
    Updates and returns the frost protection activation state for Away+Winter mode.

    When active: the full Home mode system runs (price schedule + all guards).
    When inactive: boiler is fully OFF, same as Away+Summer.

    Activation (either condition triggers):
      - indoor_temp < AWAY_WINTER_ACTIVATE_INDOOR_C
      - outside_temp < AWAY_WINTER_ACTIVATE_OUTSIDE_C (preemptive early warning —
        the boiler needs time to heat up from low temperatures)

    Deactivation (both conditions must recover):
      - indoor_temp > AWAY_WINTER_DEACTIVATE_INDOOR_C
      - outside_temp > AWAY_WINTER_DEACTIVATE_OUTSIDE_C (or unavailable)

    State is persisted in boiler_state.json so recovery heating continues
    correctly across Pi reboots and power outages.
    """
    active = state.get("away_frost_protection_active", False)

    indoor_triggers  = indoor_temp  < AWAY_WINTER_ACTIVATE_INDOOR_C
    outside_triggers = (outside_temp is not None and
                        outside_temp < AWAY_WINTER_ACTIVATE_OUTSIDE_C)

    indoor_recovered  = indoor_temp  > AWAY_WINTER_DEACTIVATE_INDOOR_C
    # If outside temp is unavailable, don't block deactivation on its account
    outside_recovered = (outside_temp is None or
                         outside_temp > AWAY_WINTER_DEACTIVATE_OUTSIDE_C)

    if not active and (indoor_triggers or outside_triggers):
        active = True
        trigger = (
            f"indoor {indoor_temp:.1f}°C < {AWAY_WINTER_ACTIVATE_INDOOR_C}°C"
            if indoor_triggers
            else f"outside {outside_temp:.1f}°C < {AWAY_WINTER_ACTIVATE_OUTSIDE_C}°C"
        )
        logging.info(f"Away+Winter: frost protection ACTIVATED ({trigger})")

    elif active and indoor_recovered and outside_recovered:
        active = False
        outside_info = f"{outside_temp:.1f}°C" if outside_temp is not None else "n/a"
        logging.info(
            f"Away+Winter: frost protection DEACTIVATED "
            f"(indoor {indoor_temp:.1f}°C, outside {outside_info})"
        )

    state["away_frost_protection_active"] = active
    return active


# ── Season inference ──────────────────────────────────────────────────────────

def determine_season(corrected_boiler_temp: float, state: dict) -> str:
    """
    Determines the current heating season (summer/winter) and updates state.

    Priority:
      1. SEASON_OVERRIDE in config ("summer" / "winter") — explicit, always wins
      2. Persisted season in state file, flipped only after
         SEASON_CHANGE_READINGS consecutive readings suggesting the other season.
      3. If no history yet: infer directly from current corrected boiler temp.
    """
    if SEASON_OVERRIDE is not None:
        return SEASON_OVERRIDE

    suggested = (
        "winter"
        if corrected_boiler_temp >= SEASON_INFERENCE_THRESHOLD_C
        else "summer"
    )
    current_season = state.get("season")

    if current_season is None:
        state["season"] = suggested
        state["season_consecutive_readings"] = 0
        logging.info(
            f"Season initialised to '{suggested}' "
            f"(boiler {corrected_boiler_temp:.1f}°C, "
            f"threshold {SEASON_INFERENCE_THRESHOLD_C}°C)"
        )
        return suggested

    if suggested == current_season:
        state["season_consecutive_readings"] = 0
    else:
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


# ── Watchdog heartbeats ───────────────────────────────────────────────────────

def write_shelly_heartbeat() -> None:
    """
    Writes a Unix timestamp to the Shelly's KVS as a local watchdog heartbeat.
    The Shelly watchdog script reads this every minute and forces the boiler ON
    if the Pi has been silent for more than WATCHDOG_TIMEOUT_SECS.
    Works purely over the local network — not affected by internet outages.
    """
    try:
        requests.get(
            f"http://{BOILER_SHELLY_IP}/rpc/KVS.Set",
            params={
                "key":   "pi_heartbeat",
                "value": int(datetime.now().timestamp()),
            },
            timeout=5,
        )
    except Exception as e:
        logging.warning(f"Could not write Shelly KVS heartbeat: {e}")


def ping_server() -> None:
    """
    Sends a keep-alive ping to the web server so the cPanel cron watchdog
    can detect if the Pi goes down and send an email alert.
    Notification-only — failures never affect boiler control.
    """
    try:
        r = requests.get(
            SERVER_HEARTBEAT_URL,
            params={"token": SERVER_HEARTBEAT_TOKEN},
            timeout=5,
        )
        if r.status_code != 200:
            logging.warning(f"Server heartbeat returned HTTP {r.status_code}")
    except Exception as e:
        logging.warning(f"Could not ping server heartbeat: {e}")


# ── Main logic ────────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()

    # 1. Load persistent state and prune yesterday's actuals
    state     = load_state()
    today_str = date.today().isoformat()
    if state.get("actuals_date") != today_str:
        state["actuals"]      = {}
        state["actuals_date"] = today_str

    # 2. Read all three DS18B20 sensors from Shelly Add-on
    raw_indoor = fetch_addon_temp(DS18B20_INDOOR_INDEX)
    raw_boiler = fetch_addon_temp(DS18B20_BOILER_INDEX)
    raw_feed   = fetch_addon_temp(DS18B20_FEED_INDEX)

    # Apply calibration offset to boiler sensor
    boiler_temp_available = raw_boiler is not None
    corrected_boiler_temp = (raw_boiler + DS18B20_BOILER_OFFSET) if boiler_temp_available else None

    # 3. Determine season and boiler minimum temperature threshold
    if boiler_temp_available:
        season = determine_season(corrected_boiler_temp, state)
    else:
        season = SEASON_OVERRIDE or state.get("season", "winter")
        logging.warning("Boiler temp sensor unavailable — using persisted/default season")

    boiler_min_temp = (
        BOILER_MIN_TEMP_WINTER_C if season == "winter" else BOILER_MIN_TEMP_SUMMER_C
    )

    # 4. Read occupancy (written by Homebridge virtual switch)
    occupancy = get_occupancy()

    # 5. Fetch outside temperature — needed for Away mode frost guard; also logged for Home+Winter
    outside_temp = None
    if occupancy == "away" or season == "winter":
        outside_temp = get_outside_temp()

    # 6. Fetch spot prices and build schedule
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

    # Write heartbeats — Pi is alive and running normally
    write_shelly_heartbeat()
    ping_server()

    # 7. Get indoor temperature
    indoor_temp, indoor_time, indoor_fresh = get_indoor_temp(raw_indoor, state, now)

    # 8. Mode-based boiler decision
    if occupancy == "away":
        # Away mode: boiler OFF by default regardless of season.
        # When frost risk is detected, the ENTIRE Home mode system activates
        # (price schedule + boiler temp guard + frost guard). This gives the
        # boiler time to heat from cold temperatures before it can contribute
        # to frost protection — much better than reactive on/off at the threshold.
        # Hysteresis (separate activate/deactivate thresholds) prevents cycling.

        if not indoor_fresh:
            # Can't assess frost risk without temperature data → activate as fail-safe
            logging.warning(
                f"Indoor temp unavailable in away mode → "
                f"activating frost protection as fail-safe"
            )
            state["away_frost_protection_active"] = True

        frost_active = update_frost_protection(state, indoor_temp, outside_temp)

        if frost_active:
            # Run full Home mode — price schedule + all guards
            scheduled_off     = current_key in off_keys
            frost_guard       = indoor_temp < FROST_GUARD_C
            boiler_temp_guard = boiler_temp_available and corrected_boiler_temp < boiler_min_temp
            boiler_on = (not scheduled_off) or frost_guard or boiler_temp_guard

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
            reasons = [f"away mode frost protection active — {r}" for r in reasons]
        else:
            # Temperatures safe — boiler fully OFF
            boiler_on = False
            reasons   = ["away mode — temperatures safe, boiler off"]

    else:
        # Home mode (summer or winter): price schedule + all guards
        if not indoor_fresh:
            logging.warning(
                f"Indoor temperature data missing or older than {MAX_TEMP_AGE_HOURS}h "
                f"→ fail-safe ON"
            )
            save_state(state)
            set_boiler(True)
            return

        scheduled_off     = current_key in off_keys
        frost_guard       = indoor_temp < FROST_GUARD_C
        boiler_temp_guard = boiler_temp_available and corrected_boiler_temp < boiler_min_temp
        boiler_on = (not scheduled_off) or frost_guard or boiler_temp_guard

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

    set_boiler(boiler_on)

    # 9. Record actual, write schedule file, save state
    state.setdefault("actuals", {})[current_key] = "ON" if boiler_on else "OFF"
    write_schedule_file(
        prices, off_keys, season, boiler_min_temp, boiler_temp_available,
        state["actuals"], occupancy, outside_temp,
    )
    save_state(state)

    # 10. Log the decision
    boiler_str  = f"{corrected_boiler_temp:.1f}°C" if boiler_temp_available else "n/a"
    feed_str    = f"{raw_feed:.1f}°C"              if raw_feed is not None   else "n/a"
    outside_str = f"{outside_temp:.1f}°C"          if outside_temp is not None else "n/a"
    indoor_str  = (
        f"{indoor_temp:.1f}°C (read {indoor_time.strftime('%H:%M')})"
        if indoor_fresh else "n/a (stale)"
    )

    logging.info(
        f"Quarter {now.strftime('%H:%M')} | "
        f"Occupancy: {occupancy} | "
        f"Indoor: {indoor_str} | Outside: {outside_str} | "
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
