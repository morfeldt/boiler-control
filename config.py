# ============================================================
# config.py – Boiler controller tunable settings
# Tracked in git — safe to commit (no credentials).
# Site-specific values (IPs, keys, coordinates) live in local.py,
# which is gitignored and stays on the Pi.
# ============================================================

# --- Electricity price and optimization ---
PRICE_ZONE  = "SE3"   # SE1, SE2, SE3, or SE4 (Swedish bidding zones)

# Scheduling: the boiler is ON by default. We turn it OFF during the
# most expensive quarters, subject to the constraints below.
OFF_QUARTERS_TARGET     = 64   # Target 15-min periods/day to turn off (64 ≈ 16h)
MAX_CONSEC_OFF_QUARTERS = 2    # Max consecutive off quarters — only used as fallback
                                # when the boiler temp sensor is unavailable (2 ≈ 30 min)

# --- Shelly Add-on DS18B20 sensor indices ---
# The Add-on is attached to the boiler Shelly and shares its IP.
DS18B20_INDOOR_INDEX = 100   # Indoor air temperature (replaces H&T as primary frost source)
DS18B20_BOILER_INDEX = 101   # Boiler water temperature (for boiler temp guard)
DS18B20_FEED_INDEX   = 102   # Radiator feed temperature (logged only, not used for control)

# Calibration offset for the boiler sensor (surface mount reads lower than water temp).
# Increase or decrease this value until the corrected reading matches a reference thermometer.
DS18B20_BOILER_OFFSET = 0.0  # °C added to raw DS18B20 boiler reading

# --- Boiler temperature and slow reheat ---
# The boiler is heated in three tiers depending on temperature:
#
#   Normal operation  (boiler ≥ SLOW_REHEAT_TRIGGER_C)
#     Follows the price schedule: ON during the cheapest quarters, OFF during
#     the most expensive. The boiler's own thermostat (~73°C) is the ceiling.
#
#   Slow reheat  (SLOW_REHEAT_MIN_C ≤ boiler < SLOW_REHEAT_TRIGGER_C)
#     Always heats in home mode regardless of the price schedule.
#     Duty cycles with price rank: cheapest quarter → SLOW_REHEAT_DUTY (max),
#     most expensive → SLOW_REHEAT_MIN_DUTY (min), linear in between.
#     Away mode keeps the scheduled_off gate — overnight cheap slots suffice
#     when nobody is home using hot water.
#
#   Emergency mode  (boiler < SLOW_REHEAT_MIN_C)
#     Element runs for the full quarter at 9 kW — genuine safety measure.
#     Suppressed during away→home transition (switch is flipped a day ahead).
#
#   Peak window  (Nov–Mar, weekday, 07:00–19:00)
#     No heating at all unless boiler drops below PEAK_GUARD_TEMP_C.
#     Off-peak slow reheat builds the thermal buffer for this window.
#
# Note on Legionella: SLOW_REHEAT_TRIGGER_C keeps the boiler above 55°C
# as a matter of course, so the boiler never sits in the 25–50°C growth
# range for extended periods.
SLOW_REHEAT_TRIGGER_C  = 65.0         # Below this, price-scaled slow reheat (home mode always)
SLOW_REHEAT_MIN_C      = 50.0         # Below this, emergency full-power (except during away→home transition)
SLOW_REHEAT_DUTY       = 0.33         # Max duty (cheapest quarters); also the monthly reset value
SLOW_REHEAT_MIN_DUTY   = 0.10         # Min duty (most expensive quarters) in price-scaled slow reheat
SLOW_REHEAT_DUTY_STEPS = [0.50, 0.67, 1.00]  # Steps cycled through by --increment-duty (Apple Home switch)
SLOW_REHEAT_DUTY_FILE  = "/home/boiler/boiler/slow_reheat_duty.txt"  # Override file written by Homebridge
PEAK_GUARD_TEMP_C      = 40.0         # Nov–Mar weekday 07:00–19:00: only heat below this

# --- Local watchdog and frost guard ---
FROST_GUARD_C      = 6     # Always keep boiler ON when indoor temp is below this
MAX_TEMP_AGE_HOURS = 4     # If all indoor temp sources are older than this → fail-safe ON

# --- Occupancy ---
# Written by the Homebridge virtual switch (homebridge-cmdswitch2).
# "home" = normal operation, "away" = reduced/off mode.
# Defaults to "home" if the file is missing — Pi reboot always returns to home mode.
OCCUPANCY_FILE = "/home/boiler/boiler/occupancy.txt"

# Thresholds for Away mode frost protection.
# When frost risk is detected, the full Home mode system activates (price
# schedule + all guards). When temperatures recover, it deactivates back to
# fully OFF. Hysteresis prevents cycling: separate activate/deactivate thresholds.
#
# Activation (either triggers full Home mode):
#   - indoor < ACTIVATE_INDOOR_C  (house is getting cold)
#   - outside < ACTIVATE_OUTSIDE_C (preemptive — boiler needs time to heat up)
#
# Deactivation (both must recover before turning OFF again):
#   - indoor > DEACTIVATE_INDOOR_C
#   - outside > DEACTIVATE_OUTSIDE_C
AWAY_WINTER_ACTIVATE_INDOOR_C    = 7.0
AWAY_WINTER_DEACTIVATE_INDOOR_C  = 10.0
AWAY_WINTER_ACTIVATE_OUTSIDE_C   = 2.0
AWAY_WINTER_DEACTIVATE_OUTSIDE_C = 10.0

# --- Files ---
STATE_FILE           = "/home/boiler/boiler/boiler_state.json"
LOG_FILE             = "/home/boiler/boiler/boiler.log"
SCHEDULE_FILE        = "/home/boiler/boiler/schedule.csv"
FORCE_EMERGENCY_FILE = "/home/boiler/boiler/force_emergency.txt"  # Created by --fast-heat

# --- Site-specific settings (IPs, credentials, location) ---
# Imported from local.py which is gitignored and stays on the Pi.
from local import (
    BOILER_SHELLY_IP,
    HT_SHELLY_IP,
    HT_SHELLY_GEN,
    CLOUD_AUTH_KEY,
    CLOUD_SERVER,
    HT_DEVICE_ID,
    SERVER_HEARTBEAT_URL,
    SERVER_HEARTBEAT_TOKEN,
    WEATHER_LAT,
    WEATHER_LON,
)
