# ============================================================
# config.py – Boiler controller configuration (real values)
# This file is gitignored — never commit it.
# ============================================================

# --- Electricity price and optimization ---
PRICE_ZONE  = "SE3"   # SE1, SE2, SE3, or SE4 (Swedish bidding zones)

# Scheduling: the boiler is ON by default. We turn it OFF during the
# most expensive quarters, subject to the constraints below.
OFF_QUARTERS_TARGET     = 64   # Target 15-min periods/day to turn off (64 ≈ 16h)
MAX_CONSEC_OFF_QUARTERS = 8    # Max consecutive off quarters — only used as fallback
                                # when the boiler temp sensor is unavailable (8 ≈ 2h)

# --- Shelly controlling the boiler (G3 Plus 1) ---
BOILER_SHELLY_IP  = "10.0.1.5"   # Static IP set in router
BOILER_SHELLY_GEN = 2             # 2 = Plus 1 / G3 Plus 1 (Gen2/Gen3 share the same API)

# --- Shelly Add-on DS18B20 sensor indices ---
# The Add-on is attached to the boiler Shelly and shares its IP.
DS18B20_INDOOR_INDEX = 100   # Indoor air temperature (replaces H&T as primary frost source)
DS18B20_BOILER_INDEX = 101   # Boiler water temperature (for boiler temp guard)
DS18B20_FEED_INDEX   = 102   # Radiator feed temperature (logged only, not used for control)

# Calibration offset for the boiler sensor (surface mount reads lower than water temp).
# Increase or decrease this value until the corrected reading matches a reference thermometer.
DS18B20_BOILER_OFFSET = 5.0  # °C added to raw DS18B20 boiler reading

# --- Boiler temperature guard ---
# When the corrected boiler temp falls below this threshold, the boiler is forced ON
# regardless of the price schedule — to ensure hot water doesn't get too cold.
# The active threshold is selected automatically based on the inferred season.
BOILER_MIN_TEMP_SUMMER_C = 55.0   # Active when season = "summer"
BOILER_MIN_TEMP_WINTER_C = 68.0   # Active when season = "winter"

# --- Season inference ---
# Season is inferred from the corrected boiler temperature and persisted in the
# state file. It only flips after SEASON_CHANGE_READINGS consecutive readings
# suggesting the other season, to prevent momentary dips from triggering a flip.
#
# SEASON_OVERRIDE options:
#   None      – automatic inference (recommended)
#   "summer"  – force summer mode regardless of boiler temp
#   "winter"  – force winter mode regardless of boiler temp
SEASON_OVERRIDE              = None   # None / "summer" / "winter"
SEASON_CHANGE_READINGS       = 8      # Consecutive readings needed to flip season (8 ≈ 2h)
SEASON_INFERENCE_THRESHOLD_C = 65.0   # Corrected boiler temp above which = winter

# --- Shelly H&T (temperature sensor — fallback for indoor temp) ---
HT_SHELLY_IP  = "10.0.1.6"   # Static IP set in router
HT_SHELLY_GEN = 1             # 1 = H&T original, 2 = Plus H&T

# --- Shelly Cloud (fallback when H&T is asleep / unreachable locally) ---
# Obtained via the Shelly app → User Settings → Authorization cloud key
CLOUD_AUTH_KEY = "YOUR_AUTH_KEY_HERE"           # Paste your auth_key
CLOUD_SERVER   = "shelly-XX-eu.shelly.cloud"    # Paste your server name
HT_DEVICE_ID   = "e4cfd3"                        # Last 6 hex chars of the H&T's MAC, lowercase

# --- Frost protection ---
FROST_GUARD_C      = 10.0   # Always keep boiler ON when indoor temp is below this
MAX_TEMP_AGE_HOURS = 4      # If all indoor temp sources are older than this → fail-safe ON

# --- Files ---
STATE_FILE    = "/home/boiler/boiler/boiler_state.json"
LOG_FILE      = "/home/boiler/boiler/boiler.log"
SCHEDULE_FILE = "/home/boiler/boiler/schedule.csv"
