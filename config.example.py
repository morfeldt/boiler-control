# ============================================================
# config.py – Boiler controller configuration
# Copy this file to config.py and fill in your own values.
# config.py is gitignored — never commit it with real values.
# ============================================================

# --- Electricity price and optimization ---
PRICE_ZONE  = "SE3"   # SE1, SE2, SE3, or SE4 (Swedish bidding zones)

# Scheduling: the boiler is ON by default. We turn it OFF during the
# most expensive quarters, but never long enough in a row that hot
# water gets too cold or house heating suffers.
OFF_QUARTERS_TARGET      = 64   # Target number of 15-min periods/day to turn off (64 ≈ 16h)
MAX_CONSEC_OFF_QUARTERS  = 8    # Max consecutive off quarters allowed (8 ≈ 2 hours)

# --- Shelly controlling the boiler (G3 Plus 1 or Plus 1) ---
BOILER_SHELLY_IP  = "192.168.1.XX"   # Set a static IP in your router
BOILER_SHELLY_GEN = 2                 # 2 = Plus 1 / G3 Plus 1 (Gen2/Gen3 share the same API)

# --- Shelly H&T (temperature sensor) ---
HT_SHELLY_IP  = "192.168.1.YY"   # Set a static IP in your router
HT_SHELLY_GEN = 1                 # 1 = H&T original, 2 = Plus H&T

# --- Shelly Cloud (fallback when H&T is asleep / unreachable locally) ---
# Obtained via the Shelly app → User Settings → Authorization cloud key
CLOUD_AUTH_KEY = "YOUR_AUTH_KEY_HERE"           # Paste your auth_key
CLOUD_SERVER   = "shelly-XX-eu.shelly.cloud"    # Paste your server name
HT_DEVICE_ID   = "XXXXXX"                        # Last 6 hex chars of the H&T's MAC, lowercase
                                                  # (matches the key under devices_status in the cloud response)

# --- Frost protection ---
FROST_GUARD_C      = 10.0   # Degrees — always keep the boiler ON below this temperature
MAX_TEMP_AGE_HOURS = 4      # If the latest temperature reading is older than this → fail-safe ON

# --- Files ---
STATE_FILE    = "/home/boiler/boiler/boiler_state.json"
LOG_FILE      = "/home/boiler/boiler/boiler.log"
SCHEDULE_FILE = "/home/boiler/boiler/schedule.csv"   # Overwritten each run – today's full on/off plan
