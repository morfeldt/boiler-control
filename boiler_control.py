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
  Home
    Price schedule + boiler temp guard + frost guard

  Away
    Boiler fully OFF unless frost risk is detected.
    Frost protection activates when indoor or outside temp drops below
    threshold — runs full Home mode until temperatures recover.
    Frost protection is driven purely by temperature — no season tracking.

Sensor priority for indoor/frost temperature:
  1. DS18B20 on Shelly Add-on index 100 (wired, always-on)
  2. Shelly H&T local HTTP (only when awake)
  3. Shelly H&T cloud (last reported value)
  4. Last saved value in state file

Boiler temperature guard:
  - DS18B20 on Shelly Add-on index 101, corrected by DS18B20_BOILER_OFFSET
  - If unavailable: falls back to MAX_CONSEC_OFF_QUARTERS constraint

Radiator feed temperature:
  - DS18B20 on Shelly Add-on index 102
  - Logged only, not used for control decisions

Fail-safe: on any error, the boiler is turned ON — you never freeze.
Exception: Away mode keeps the boiler OFF on errors since
frost risk is assessed by temperature, not by a hardcoded season.
"""

import sys
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
    SLOW_REHEAT_TRIGGER_C, SLOW_REHEAT_MIN_C, SLOW_REHEAT_DUTY, SLOW_REHEAT_DUTY_FILE,
    SLOW_REHEAT_DUTY_STEPS, PEAK_GUARD_TEMP_C, SLOW_REHEAT_MIN_DUTY, BOOST_DURATION_HOURS,
    FEED_DUTY_MIN_C, FEED_DUTY_MAX_C,
    FORCE_EMERGENCY_FILE,
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
    boiler_temp_available: bool,
    actuals: dict,
    occupancy: str,
    outside_temp: float | None,
    monthly_peak_kw: float = 0.0,
) -> None:
    """
    Writes today's full on/off plan to SCHEDULE_FILE, overwritten each run.

    Columns:
      time              – quarter start, HH:MM
      price_sek_per_kwh – Nordpool spot price for this quarter
      scheduled         – what the price schedule says (ON/OFF)
      actual            – what actually happened (ON/OFF), empty for future quarters

    In Away mode, quarters may all be OFF if frost protection is not active.
    The schedule is still written for reference (prices visible), but a
    header note indicates the occupancy state.
    """
    chrono    = sorted(prices, key=lambda p: p["time_start"])
    bar       = "".join("." if p["time_start"] in off_keys else "#" for p in chrono)
    off_count = bar.count(".")

    sensor_note = (
        f"slow reheat <{SLOW_REHEAT_TRIGGER_C}°C / emergency <{SLOW_REHEAT_MIN_C}°C"
        if boiler_temp_available
        else f"sensor unavailable — fallback: max {MAX_CONSEC_OFF_QUARTERS} consecutive off"
    )
    outside_str = f"{outside_temp:.1f}°C" if outside_temp is not None else "n/a"
    away_note   = " | AWAY: boiler OFF unless frost protection active" if occupancy == "away" else ""

    lines = [
        f"# Schedule generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Occupancy: {occupancy} | Outside: {outside_str} | "
        f"Peak: {monthly_peak_kw:.1f} kW | {sensor_note}{away_note}",
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
    Used for Away mode frost protection and logged in Home mode for context.
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


# ── Away frost protection state ───────────────────────────────────────────────

def update_frost_protection(
    state: dict,
    indoor_temp: float,
    outside_temp: float | None,
) -> bool:
    """
    Updates and returns the frost protection activation state for Away mode.

    When active: the full Home mode system runs (price schedule + all guards).
    When inactive: boiler is fully OFF.

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
        logging.info(f"Away: frost protection ACTIVATED ({trigger})")

    elif active and indoor_recovered and outside_recovered:
        active = False
        outside_info = f"{outside_temp:.1f}°C" if outside_temp is not None else "n/a"
        logging.info(
            f"Away: frost protection DEACTIVATED "
            f"(indoor {indoor_temp:.1f}°C, outside {outside_info})"
        )

    state["away_frost_protection_active"] = active
    return active



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


# ── Peak window and slow reheat ───────────────────────────────────────────────

def is_peak_window(now: datetime) -> bool:
    """
    Returns True during the Degerfors Energi peak tariff window:
    November–March, Monday–Friday, 07:00–19:00 local time.

    Both the effektavgift (50 kr/kW/month, all hours) and the
    högbelastningsavgift (40 kr/kW/month, peak window only) are based
    on the single worst hourly average in the month, so avoiding high
    draws during this window eliminates the combined 90 kr/kW charge.
    """
    if now.month not in (11, 12, 1, 2, 3):
        return False
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return 7 <= now.hour < 19


def set_boiler_slow_reheat(duty: float) -> None:
    """
    Turns the boiler ON for `duty` fraction of the quarter via the Shelly's
    toggle_after parameter (e.g. 0.33 → 297 s ≈ 5 min ON then auto-off).
    Full-power heating (emergency or scheduled ON) uses set_boiler() directly.
    """
    timer_secs = int(duty * 900)
    try:
        r = requests.get(
            f"http://{BOILER_SHELLY_IP}/rpc/Switch.Set",
            params={"id": 0, "on": "true", "toggle_after": timer_secs},
            timeout=5,
        )
        r.raise_for_status()
    except Exception as e:
        logging.warning(f"set_boiler_slow_reheat failed: {e} — falling back to full ON")
        set_boiler(True)


# ── Slow-reheat duty override ─────────────────────────────────────────────────

def get_effective_duty(state: dict, now: datetime) -> float:
    """
    Returns the slow-reheat duty for this quarter.

    Reads SLOW_REHEAT_DUTY_FILE (written by --increment-duty / --reset-duty).
    Auto-clears the file at month rollover so the billing period always starts
    from the config default. Falls back to SLOW_REHEAT_DUTY if absent or invalid.
    """
    month_str = now.strftime("%Y-%m")
    p = Path(SLOW_REHEAT_DUTY_FILE)

    if p.exists() and state.get("duty_override_month", month_str) != month_str:
        try:
            old_str = f"{float(p.read_text().strip()):.0%}"
        except Exception:
            old_str = "unknown"
        try:
            p.unlink()
            logging.info(
                f"Monthly reset: slow-reheat duty override cleared "
                f"(was {old_str}), back to config default {SLOW_REHEAT_DUTY:.0%}"
            )
        except Exception as e:
            logging.warning(f"Could not clear duty override file: {e}")
        state.pop("duty_override_month", None)
        return SLOW_REHEAT_DUTY

    try:
        val = float(p.read_text().strip())
        if 0.0 < val <= 1.0:
            state["duty_override_month"] = month_str
            return val
    except Exception:
        pass

    return SLOW_REHEAT_DUTY


def compute_monthly_peak_kw(log: dict) -> float:
    """
    Returns the worst clock-hour average power draw from the monthly log.
    Each entry maps a quarter-hour timestamp → kW drawn that quarter.
    Unfilled quarter slots count as 0 kW, so the average is always / 4.
    """
    hourly: dict[str, float] = {}
    for ts, kw in log.items():
        hour = ts[:13]   # "2026-10-27T14"
        hourly[hour] = hourly.get(hour, 0.0) + kw
    return max((total / 4 for total in hourly.values()), default=0.0)


def record_power_draw(state: dict, now: datetime, power_kw: float) -> float:
    """
    Logs this quarter's estimated grid draw and returns the updated monthly
    peak kW (worst clock-hour average draw so far this billing month).
    """
    month_str = now.strftime("%Y-%m")
    if state.get("monthly_kw_log_month") != month_str:
        state["monthly_kw_log"]       = {}
        state["monthly_kw_log_month"] = month_str

    quarter_key = now.strftime("%Y-%m-%dT%H:") + f"{(now.minute // 15) * 15:02d}"
    state["monthly_kw_log"][quarter_key] = power_kw

    peak_kw = compute_monthly_peak_kw(state["monthly_kw_log"])
    state["monthly_peak_kw"] = peak_kw
    return peak_kw


# ── Duty override CLI helpers ─────────────────────────────────────────────────

def increment_duty() -> None:
    """
    Steps the slow-reheat duty to the next value in SLOW_REHEAT_DUTY_STEPS,
    wrapping from the last step back to the first. The new value persists in
    SLOW_REHEAT_DUTY_FILE until the month resets or --reset-duty is called.

    Usage: python3 boiler_control.py --increment-duty
    Homebridge on_cmd: python3 /home/boiler/boiler/boiler_control.py --increment-duty
    """
    p = Path(SLOW_REHEAT_DUTY_FILE)
    try:
        current = float(p.read_text().strip())
    except Exception:
        current = SLOW_REHEAT_DUTY

    steps     = sorted(SLOW_REHEAT_DUTY_STEPS)
    next_duty = next((s for s in steps if s > current + 0.001), steps[0])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{next_duty:.4f}")
    print(f"Slow-reheat duty: {next_duty:.0%}  (was {current:.0%})")


def reset_duty() -> None:
    """
    Removes the duty override file, reverting to SLOW_REHEAT_DUTY from config.

    Usage: python3 boiler_control.py --reset-duty
    Homebridge off_cmd: python3 /home/boiler/boiler/boiler_control.py --reset-duty
    """
    p = Path(SLOW_REHEAT_DUTY_FILE)
    if p.exists():
        p.unlink()
        print(f"Slow-reheat duty override cleared — back to config default {SLOW_REHEAT_DUTY:.0%}")
    else:
        print(f"No override active — duty already at config default {SLOW_REHEAT_DUTY:.0%}")


def price_scaled_duty(
    price: float,
    all_prices: list[dict],
    min_duty: float,
    max_duty: float,
) -> float:
    """
    Returns a slow-reheat duty scaled to this quarter's price rank among all today's quarters.
    Cheapest quarter → max_duty; most expensive → min_duty; linear in between.
    Rank-based (not value-based) so price outliers don't compress the useful range.
    """
    if max_duty <= min_duty:
        return max_duty
    prices_sorted = sorted(p["SEK_per_kWh"] for p in all_prices)
    n = len(prices_sorted)
    if n <= 1:
        return max_duty
    rank = sum(1 for p in prices_sorted if p <= price)   # 1 = cheapest, n = most expensive
    normalized = (rank - 1) / (n - 1)                    # 0.0 = cheapest, 1.0 = most expensive
    return max_duty - normalized * (max_duty - min_duty)


def feed_scaled_min_duty(
    feed_temp: float | None,
    min_duty: float,
    max_duty: float,
) -> float:
    """
    Returns a minimum duty floor based on radiator feed temperature.
    High feed temp means the house heating circuit is extracting heat quickly,
    so the boiler needs more power to keep up regardless of price.
    Linear interpolation between FEED_DUTY_MIN_C (→ min_duty) and FEED_DUTY_MAX_C (→ max_duty).
    Falls back to min_duty if sensor unavailable.
    """
    if feed_temp is None:
        return min_duty
    normalized = max(0.0, min(1.0, (feed_temp - FEED_DUTY_MIN_C) / (FEED_DUTY_MAX_C - FEED_DUTY_MIN_C)))
    return min_duty + normalized * (max_duty - min_duty)


def enable_fast_heat() -> None:
    """
    Activates the fast-heat override: clears the away_transition flag and creates
    FORCE_EMERGENCY_FILE so the next run allows emergency full-power mode.
    The file is auto-deleted by main() once the boiler reaches SLOW_REHEAT_MIN_C.

    Usage: python3 boiler_control.py --fast-heat
    Homebridge on_cmd: python3 /home/boiler/boiler/boiler_control.py --fast-heat
    """
    Path(FORCE_EMERGENCY_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(FORCE_EMERGENCY_FILE).touch()
    state = load_state()
    if state.get("away_transition"):
        state["away_transition"] = False
        save_state(state)
    print(
        f"Fast heat override active — emergency full-power enabled until "
        f"boiler ≥ {SLOW_REHEAT_MIN_C}°C"
    )


def disable_fast_heat() -> None:
    """
    Deactivates the fast-heat override, returning to normal transition behaviour.

    Usage: python3 boiler_control.py --clear-fast-heat
    Homebridge off_cmd: python3 /home/boiler/boiler/boiler_control.py --clear-fast-heat
    """
    p = Path(FORCE_EMERGENCY_FILE)
    if p.exists():
        p.unlink()
        print("Fast heat override cleared")
    else:
        print("Fast heat override not active")


# ── Heating boost (combined switch) ──────────────────────────────────────────

def boost() -> None:
    """
    Combined heating boost, behaviour depends on current state:

    During away→home transition (boiler cold from Away mode):
      Clears the transition flag and activates emergency full-power mode
      immediately. Auto-clears once the boiler reaches SLOW_REHEAT_MIN_C.

    Otherwise:
      Raises the duty ceiling to max for BOOST_DURATION_HOURS, then
      auto-resets. The resulting monthly peak increase may raise the
      auto-unlock duty for the rest of the billing period.

    Usage: python3 boiler_control.py --boost
    Homebridge on_cmd: python3 /home/boiler/boiler/boiler_control.py --boost
    """
    state = load_state()
    if state.get("away_transition"):
        Path(FORCE_EMERGENCY_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(FORCE_EMERGENCY_FILE).touch()
        state["away_transition"] = False
        save_state(state)
        print(
            f"Boost: transition mode — emergency full-power until boiler ≥ {SLOW_REHEAT_MIN_C}°C"
        )
    else:
        max_step = max(SLOW_REHEAT_DUTY_STEPS)
        p = Path(SLOW_REHEAT_DUTY_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{max_step:.4f}")
        expiry = datetime.now().timestamp() + BOOST_DURATION_HOURS * 3600
        state["duty_boost_expiry"] = expiry
        save_state(state)
        expiry_str = datetime.fromtimestamp(expiry).strftime("%H:%M")
        print(
            f"Boost: duty raised to {max_step:.0%} for {BOOST_DURATION_HOURS}h "
            f"(auto-resets at {expiry_str})"
        )


def clear_boost() -> None:
    """
    Cancels any active boost — clears both the duty override and the
    fast-heat override if present.

    Usage: python3 boiler_control.py --clear-boost
    Homebridge off_cmd: python3 /home/boiler/boiler/boiler_control.py --clear-boost
    """
    state = load_state()
    cleared = []

    p = Path(FORCE_EMERGENCY_FILE)
    if p.exists():
        p.unlink()
        cleared.append("fast-heat")

    p = Path(SLOW_REHEAT_DUTY_FILE)
    if p.exists():
        p.unlink()
        cleared.append("duty boost")

    state.pop("duty_boost_expiry", None)
    save_state(state)

    print(f"Boost cleared ({', '.join(cleared)})" if cleared else "No boost active")


# ── Main logic ────────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()

    # 1. Load persistent state and prune yesterday's actuals
    state     = load_state()
    today_str = date.today().isoformat()
    if state.get("actuals_date") != today_str:
        state["actuals"]      = {}
        state["actuals_date"] = today_str

    # 2. Expire duty boost if the two-hour window has passed, then compute effective duty.
    if "duty_boost_expiry" in state:
        if now.timestamp() >= state["duty_boost_expiry"]:
            p = Path(SLOW_REHEAT_DUTY_FILE)
            if p.exists():
                try:
                    p.unlink()
                    logging.info(
                        f"Duty boost expired — duty reset to config default {SLOW_REHEAT_DUTY:.0%}"
                    )
                except Exception as e:
                    logging.warning(f"Could not clear duty boost file: {e}")
            state.pop("duty_boost_expiry", None)

    effective_duty  = get_effective_duty(state, now)
    monthly_peak_kw = state.get("monthly_peak_kw", 0.0)
    auto_duty       = min(1.0, monthly_peak_kw / 9.0)
    if auto_duty > effective_duty:
        effective_duty = auto_duty

    # 3. Read all three DS18B20 sensors from Shelly Add-on
    raw_indoor = fetch_addon_temp(DS18B20_INDOOR_INDEX)
    raw_boiler = fetch_addon_temp(DS18B20_BOILER_INDEX)
    raw_feed   = fetch_addon_temp(DS18B20_FEED_INDEX)

    # Apply calibration offset to boiler sensor
    boiler_temp_available = raw_boiler is not None
    corrected_boiler_temp = (raw_boiler + DS18B20_BOILER_OFFSET) if boiler_temp_available else None

    # 3. Read occupancy and detect away→home transition.
    #    When the occupancy switch is flipped a day ahead, the boiler may be cold
    #    from Away mode. We suppress emergency full-power reheating during the
    #    transition and use slow reheat instead, letting the price schedule fill
    #    cheap overnight slots with 33%-duty heating.
    #
    #    The fast-heat override ("Snabb uppvärmning" switch) bypasses this suppression
    #    so emergency full-power can run immediately when needed. The override file
    #    is auto-deleted once the boiler reaches SLOW_REHEAT_MIN_C.
    fast_heat      = Path(FORCE_EMERGENCY_FILE).exists()
    occupancy      = get_occupancy()
    prev_occupancy = state.get("previous_occupancy")
    if prev_occupancy == "away" and occupancy == "home":
        if not state.get("away_transition") and not fast_heat:
            state["away_transition"] = True
            logging.info(
                "Away→home transition detected — slow-reheat warm-up mode active "
                f"(emergency suppressed until boiler ≥ {SLOW_REHEAT_MIN_C}°C)"
            )
    # Fast-heat override: clear transition flag so emergency mode can run immediately
    if fast_heat and state.get("away_transition"):
        state["away_transition"] = False
        logging.info("Fast heat override active — away_transition cleared, emergency mode enabled")
    # Clear transition flag once the boiler is warm enough for normal slow reheat to govern
    if state.get("away_transition") and boiler_temp_available:
        if corrected_boiler_temp >= SLOW_REHEAT_MIN_C:
            state["away_transition"] = False
            logging.info(
                f"Away→home warm-up complete "
                f"(boiler {corrected_boiler_temp:.1f}°C ≥ {SLOW_REHEAT_MIN_C}°C)"
            )
    # Auto-clear fast-heat once the boiler is past the emergency threshold
    if fast_heat and boiler_temp_available and corrected_boiler_temp >= SLOW_REHEAT_MIN_C:
        try:
            Path(FORCE_EMERGENCY_FILE).unlink()
            logging.info(
                f"Fast heat override cleared "
                f"(boiler {corrected_boiler_temp:.1f}°C ≥ {SLOW_REHEAT_MIN_C}°C)"
            )
            fast_heat = False
        except Exception as e:
            logging.warning(f"Could not clear force_emergency file: {e}")
    state["previous_occupancy"] = occupancy
    away_transition = state.get("away_transition", False)

    # 5. Fetch outside temperature — used for Away mode frost protection,
    #    also logged in Home mode for context alongside feed temperature.
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
        # Away mode: fail safe to OFF (Shelly watchdog handles actual emergencies).
        # Home mode: fail safe to ON.
        set_boiler(occupancy != "away")
        return

    # Slow-reheat duty for this quarter (home mode).
    # Boost active (and not in transition): user explicitly wants heat now — run at
    # full effective_duty regardless of price. Otherwise price-scale between
    # SLOW_REHEAT_MIN_DUTY and effective_duty, with a feed-temp demand floor.
    duty_boost_active = (
        "duty_boost_expiry" in state
        and now.timestamp() < state["duty_boost_expiry"]
        and not away_transition
    )
    if duty_boost_active:
        slow_reheat_duty = effective_duty
    else:
        price_duty       = price_scaled_duty(price, prices, SLOW_REHEAT_MIN_DUTY, effective_duty)
        feed_floor       = feed_scaled_min_duty(raw_feed, SLOW_REHEAT_MIN_DUTY, effective_duty)
        slow_reheat_duty = max(price_duty, feed_floor)

    # Write heartbeats — Pi is alive and running normally
    write_shelly_heartbeat()
    ping_server()

    # 7. Get indoor temperature
    indoor_temp, indoor_time, indoor_fresh = get_indoor_temp(raw_indoor, state, now)

    # 8. Mode-based boiler decision
    boiler_on   = False
    slow_reheat = False   # True → use set_boiler_slow_reheat() with price-scaled duty
    emergency   = False   # True → full 9 kW regardless of duty cap
    reheat_duty = effective_duty  # overridden to slow_reheat_duty in home mode slow reheat
    reasons     = []
    peak        = is_peak_window(now)

    if occupancy == "away":
        # Away mode: boiler OFF by default. The frost protection system activates
        # the full Home mode logic when temperatures suggest frost risk.
        # Season distinction is not needed here — frost protection is driven entirely
        # by outdoor and indoor temperatures.

        if not indoor_fresh:
            # Can't assess frost risk without temperature data → activate as fail-safe
            logging.warning(
                "Indoor temp unavailable in Away mode → "
                "activating frost protection as fail-safe"
            )
            state["away_frost_protection_active"] = True

        frost_active = update_frost_protection(state, indoor_temp, outside_temp)

        if frost_active:
            # Run full Home mode within Away: price schedule + all guards.
            scheduled_off = current_key in off_keys
            frost_guard   = indoor_temp < FROST_GUARD_C

            if frost_guard:
                # Indoor critically low — full power regardless
                boiler_on = True
                reasons.append(f"frost guard ({indoor_temp:.1f}°C < {FROST_GUARD_C}°C)")

            elif peak and (
                not boiler_temp_available or corrected_boiler_temp >= PEAK_GUARD_TEMP_C
            ):
                # Peak tariff window and boiler not dangerously cold: hold off
                boiler_on = False
                temp_info = (
                    "no sensor"
                    if not boiler_temp_available
                    else f"boiler {corrected_boiler_temp:.1f}°C ≥ {PEAK_GUARD_TEMP_C}°C"
                )
                reasons.append(f"peak window — holding off ({temp_info})")

            elif boiler_temp_available and corrected_boiler_temp < SLOW_REHEAT_MIN_C:
                # Boiler very cold — emergency full power
                boiler_on = True
                emergency  = True
                reasons.append(
                    f"boiler temp emergency "
                    f"({corrected_boiler_temp:.1f}°C < {SLOW_REHEAT_MIN_C}°C)"
                )

            elif boiler_temp_available and corrected_boiler_temp < SLOW_REHEAT_TRIGGER_C:
                if not scheduled_off:
                    boiler_on   = True
                    slow_reheat = True
                    reasons.append(
                        f"slow reheat ({corrected_boiler_temp:.1f}°C < {SLOW_REHEAT_TRIGGER_C}°C)"
                    )
                else:
                    boiler_on = False
                    reasons.append("scheduled off (expensive period)")

            else:
                boiler_on = not scheduled_off
                reasons.append(
                    "scheduled off (expensive period)" if scheduled_off else "normal operation"
                )

            reasons = [f"away frost protection active — {r}" for r in reasons]

        else:
            boiler_on = False
            reasons   = ["away mode — temperatures safe, boiler off"]

    else:
        # Home mode: price schedule + peak guard + slow reheat + frost guard.
        if not indoor_fresh:
            logging.warning(
                f"Indoor temperature data missing or older than {MAX_TEMP_AGE_HOURS}h "
                f"→ fail-safe ON"
            )
            save_state(state)
            set_boiler(True)
            return

        scheduled_off = current_key in off_keys
        frost_guard   = indoor_temp < FROST_GUARD_C

        if frost_guard:
            # Indoor critically low — full power, no exceptions
            boiler_on = True
            reasons.append(f"frost guard ({indoor_temp:.1f}°C < {FROST_GUARD_C}°C)")

        elif peak and (
            not boiler_temp_available or corrected_boiler_temp >= PEAK_GUARD_TEMP_C
        ):
            # Inside the peak tariff window and boiler not dangerously cold:
            # hold off completely to protect the monthly effektavgift + högbelastningsavgift.
            # Off-peak slow reheat during overnight cheap slots pre-heats the boiler
            # so it can coast through the 07:00–19:00 window without this guard firing.
            boiler_on = False
            temp_info = (
                "no sensor"
                if not boiler_temp_available
                else f"boiler {corrected_boiler_temp:.1f}°C ≥ {PEAK_GUARD_TEMP_C}°C"
            )
            reasons.append(f"peak window — holding off ({temp_info})")

        elif (
            boiler_temp_available
            and corrected_boiler_temp < SLOW_REHEAT_MIN_C
            and not away_transition
        ):
            # Boiler very cold during normal home use: emergency full power.
            # Suppressed during away→home transition — no urgent demand since the
            # switch is always flipped a day ahead; slow reheat covers the warm-up.
            boiler_on = True
            emergency  = True
            reasons.append(
                f"boiler temp emergency "
                f"({corrected_boiler_temp:.1f}°C < {SLOW_REHEAT_MIN_C}°C)"
            )

        elif boiler_temp_available and corrected_boiler_temp < SLOW_REHEAT_TRIGGER_C:
            # Slow-reheat whenever below the trigger, regardless of the price schedule.
            # Duty scales with price rank: cheapest quarter → effective_duty (max),
            # most expensive → SLOW_REHEAT_MIN_DUTY (min). A small draw at expensive
            # prices costs less than a full 9 kW emergency spike a few quarters later.
            # Peak window is already caught above; away mode keeps its scheduled_off gate.
            boiler_on   = True
            slow_reheat = True
            reheat_duty = slow_reheat_duty
            mode_label  = "transition warm-up" if away_transition else "slow reheat"
            boost_tag   = " [BOOST]" if duty_boost_active else ""
            reasons.append(
                f"{mode_label}{boost_tag} {corrected_boiler_temp:.1f}°C < {SLOW_REHEAT_TRIGGER_C}°C, "
                f"{reheat_duty:.0%} duty"
            )

        else:
            # Boiler at or above comfort temperature (or sensor unavailable):
            # follow the price schedule normally.
            boiler_on = not scheduled_off
            if scheduled_off:
                reasons.append("scheduled off (expensive period)")
            elif effective_duty < 1.0:
                reasons.append(f"normal operation ({effective_duty:.0%} duty)")
            else:
                reasons.append("normal operation")

    # 9. Apply boiler state and record power draw for monthly peak tracking
    if boiler_on and not slow_reheat and not emergency:
        # Normal operation: duty-cap to protect monthly peak, same auto-unlock as slow reheat
        power_kw = effective_duty * 9.0
        set_boiler_slow_reheat(effective_duty)
    elif slow_reheat:
        power_kw = reheat_duty * 9.0
        set_boiler_slow_reheat(reheat_duty)
    else:
        power_kw = 9.0 if boiler_on else 0.0
        set_boiler(boiler_on)
    monthly_peak_kw = record_power_draw(state, now, power_kw)

    # 10. Record actual, write schedule file, save state
    state.setdefault("actuals", {})[current_key] = "ON" if boiler_on else "OFF"
    write_schedule_file(
        prices, off_keys, boiler_temp_available,
        state["actuals"], occupancy, outside_temp, monthly_peak_kw,
    )
    save_state(state)

    # 11. Log the decision
    boiler_str  = f"{corrected_boiler_temp:.1f}°C" if boiler_temp_available else "n/a"
    feed_str    = f"{raw_feed:.1f}°C"              if raw_feed is not None   else "n/a"
    outside_str = f"{outside_temp:.1f}°C"          if outside_temp is not None else "n/a"
    indoor_str  = (
        f"{indoor_temp:.1f}°C (read {indoor_time.strftime('%H:%M')})"
        if indoor_fresh else "n/a (stale)"
    )
    peak_tag      = " [PEAK]"      if peak      else ""
    fast_heat_tag = " [FAST HEAT]" if fast_heat else ""

    logging.info(
        f"Quarter {now.strftime('%H:%M')}{peak_tag}{fast_heat_tag} | "
        f"Occupancy: {occupancy} | "
        f"Indoor: {indoor_str} | Outside: {outside_str} | "
        f"Boiler: {boiler_str} | Feed: {feed_str} | "
        f"{price:.3f} SEK/kWh | Peak: {monthly_peak_kw:.1f} kW | "
        f"Boiler: {'ON ' if boiler_on else 'OFF'} ({', '.join(reasons)})"
    )


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--increment-duty":
        increment_duty()
    elif len(sys.argv) == 2 and sys.argv[1] == "--reset-duty":
        reset_duty()
    elif len(sys.argv) == 2 and sys.argv[1] == "--fast-heat":
        enable_fast_heat()
    elif len(sys.argv) == 2 and sys.argv[1] == "--clear-fast-heat":
        disable_fast_heat()
    elif len(sys.argv) == 2 and sys.argv[1] == "--boost":
        boost()
    elif len(sys.argv) == 2 and sys.argv[1] == "--clear-boost":
        clear_boost()
    else:
        try:
            main()
        except Exception as e:
            logging.error(f"Unexpected error in main(): {e} → attempting to set boiler ON")
            try:
                set_boiler(True)
            except Exception as e2:
                logging.error(f"Could not even set boiler ON: {e2}")
