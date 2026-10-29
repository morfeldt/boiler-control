# ============================================================
# local.example.py – Site-specific configuration template
# Copy this file to local.py on the Pi and fill in your values.
# local.py is gitignored — never commit it.
# ============================================================

# --- Shelly controlling the boiler (G3 Plus 1) ---
BOILER_SHELLY_IP = "192.168.1.XX"   # Static IP set in router

# --- Shelly H&T (temperature sensor — fallback for indoor temp) ---
HT_SHELLY_IP  = "192.168.1.YY"   # Static IP set in router
HT_SHELLY_GEN = 1                 # 1 = H&T original, 2 = Plus H&T

# --- Shelly Cloud (fallback when H&T is asleep / unreachable locally) ---
# Obtained via the Shelly app → User Settings → Authorization cloud key
CLOUD_AUTH_KEY = "YOUR_AUTH_KEY_HERE"           # Paste your auth_key
CLOUD_SERVER   = "shelly-XX-eu.shelly.cloud"    # Paste your server name
HT_DEVICE_ID   = "XXXXXX"                        # Last 6 hex chars of the H&T's MAC, lowercase

# --- Remote watchdog (Pi → your web server) ---
SERVER_HEARTBEAT_URL   = "https://yoursite.com/boiler/heartbeat.php"
SERVER_HEARTBEAT_TOKEN = "REPLACE_WITH_YOUR_SECRET_TOKEN"   # must match heartbeat.php

# --- Property location (Open-Meteo API for outside temperature) ---
WEATHER_LAT = 0.0000   # Property latitude
WEATHER_LON = 0.0000   # Property longitude
