# Boiler Control – CTC 1100 with Shelly G3 Plus 1

Controls the immersion heater in a CTC 1100 combination boiler based on
the Nordpool day-ahead spot price (Swedish bidding zones SE1–SE4).
Runs every 15 minutes on a Raspberry Pi (electricity prices have been
set per quarter-hour, not per hour, since 2025-10-01).

Built for a vacation/weekend home: visited roughly once a month, boiler
otherwise left on frost-guard. The goal is to shift the immersion
heater's runtime toward cheap price periods while guaranteeing the boiler
water temperature never drops too low, and to never freeze the house if
anything in the chain fails.

---

## Hardware

| Component | Model | Notes |
|---|---|---|
| Smart relay for boiler | **Shelly G3 Plus 1** | Wired into boiler's 230V tariff input |
| Sensor expansion | **Shelly Plus Add-on** | Clips into the G3 Plus 1, adds DS18B20 support |
| Boiler water temp | DS18B20 probe | Surface-mounted on boiler casing, calibrated via offset |
| Indoor air temp | DS18B20 probe | Primary frost-guard sensor |
| Radiator feed temp | DS18B20 probe | Logged only, not used for control decisions |
| Indoor temp fallback | Shelly H&T | Used if DS18B20 indoor sensor is unavailable |
| Computer | Raspberry Pi | Any model with WiFi |

**NOTE:** Wiring the Shelly G3 Plus 1 into the boiler's tariff input
(230V) must be done by a licensed electrician.

**NOTE:** The Shelly Plus Add-on clips into an internal header inside the
G3 Plus 1 enclosure. Power must be off when installing it.

---

## Files

```
boiler/
├── config.example.py    ← Template – copy to config.py and fill in your values
├── config.py            ← Your actual settings (gitignored, never commit this)
├── boiler_control.py    ← Main script
├── boiler_state.json    ← Created automatically (persisted state: temps, season)
├── schedule.csv         ← Today's on/off plan, overwritten every 15 min
└── boiler.log           ← Log of every decision
```

---

## Installation

### 1. Create a dedicated service user

```bash
sudo useradd -r -s /usr/sbin/nologin -m -d /home/boiler boiler
sudo mkdir -p /home/boiler/boiler
```

### 2. Install dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo pip3 install requests --break-system-packages
```

### 3. Copy the files

```bash
sudo cp config.example.py boiler_control.py /home/boiler/boiler/
sudo cp config.example.py /home/boiler/boiler/config.py
sudo chown -R boiler:boiler /home/boiler/boiler
```

### 4. Configure

Open `/home/boiler/boiler/config.py` and fill in your values.

**Essential settings:**
```python
BOILER_SHELLY_IP = "192.168.1.XX"  # IP of the Shelly G3 Plus 1 (also hosts the Add-on)
PRICE_ZONE       = "SE3"            # SE1, SE2, SE3, or SE4

OFF_QUARTERS_TARGET = 64            # Target 15-min periods/day to turn off (64 ≈ 16h)
```

**DS18B20 sensor indices** (as they appear on the Shelly Add-on):
```python
DS18B20_INDOOR_INDEX = 100   # Indoor air temperature
DS18B20_BOILER_INDEX = 101   # Boiler water temperature
DS18B20_FEED_INDEX   = 102   # Radiator feed temperature
```

**Boiler sensor calibration** — surface-mounted sensors read lower than
actual water temperature. Measure the gap against a reference thermometer
and set the offset accordingly:
```python
DS18B20_BOILER_OFFSET = 5.0  # °C added to raw boiler sensor reading
```

**Season inference** — the script infers heating season from the corrected
boiler temperature and applies a different minimum temperature threshold
for each season. The season only flips after 8 consecutive readings
pointing the other way, preventing momentary dips from triggering a switch.
You can override it manually if needed:
```python
SEASON_OVERRIDE              = None    # None (auto), "summer", or "winter"
SEASON_INFERENCE_THRESHOLD_C = 65.0   # Corrected boiler temp above which = winter
BOILER_MIN_TEMP_SUMMER_C     = 55.0   # Boiler forced ON below this in summer
BOILER_MIN_TEMP_WINTER_C     = 68.0   # Boiler forced ON below this in winter
```

**Shelly H&T fallback** (used if DS18B20 indoor sensor is unavailable):

The original Shelly H&T sleeps between reports to save battery, making
local HTTP polling unreliable. The script falls back to Shelly Cloud,
which always holds the last reported value regardless of sleep state.

Get your credentials via the Shelly app → User Settings →
Authorization cloud key, then fill in:
```python
HT_SHELLY_IP   = "192.168.1.YY"
CLOUD_AUTH_KEY = "your-auth-key-here"
CLOUD_SERVER   = "shelly-XX-eu.shelly.cloud"
HT_DEVICE_ID   = "XXXXXX"              # Last 6 hex chars of H&T MAC, lowercase
```

⚠️ **Treat `CLOUD_AUTH_KEY` like a password.** Never commit `config.py`
with real values. Only `config.example.py` belongs in the repo.

**Tip:** Set static IP addresses for all Shelly devices in your router's
DHCP settings (MAC reservation) to prevent IPs changing on reboot.

### 5. Test manually

```bash
sudo -u boiler python3 /home/boiler/boiler/boiler_control.py
LANG=en_US.UTF-8 tail -3 /home/boiler/boiler/boiler.log
```

Expected log output:
```
2026-01-15 03:00  Quarter 03:00 | Indoor: 18.5°C (read 03:00) | Boiler: 72.3°C | Feed: 45.1°C | Season: winter | 0.312 SEK/kWh | Boiler: OFF (scheduled off (expensive period))
2026-01-15 08:00  Quarter 08:00 | Indoor: 18.2°C (read 07:55) | Boiler: 65.8°C | Feed: 38.2°C | Season: winter | 1.854 SEK/kWh | Boiler: ON  (boiler temp guard (65.8°C < 68.0°C))
2026-01-15 09:00  Quarter 09:00 | Indoor: 7.8°C  (read 09:00) | Boiler: 71.2°C | Feed: 42.0°C | Season: winter | 2.100 SEK/kWh | Boiler: ON  (frost guard (7.8°C < 10.0°C))
```

### 6. Add to cron

```bash
sudo crontab -u boiler -e
```

Add this line (runs every 15 minutes):
```
*/15 * * * * python3 /home/boiler/boiler/boiler_control.py
```

Verify it's saved:
```bash
sudo crontab -u boiler -l
```

### 7. Configure Shelly fail-safe

In the Shelly app → G3 Plus 1 → Settings → Power On Default Mode →
set to **ON**. This ensures the boiler turns on if the Shelly loses
power and restores, regardless of what state it was in before.

---

## How it works

### Scheduling

Every run, the script fetches today's 96 quarter-hour prices from
[elprisetjustnu.se](https://www.elprisetjustnu.se) (free, open API —
Nordpool moved to quarter-hour resolution on 2025-10-01).

The boiler is **ON by default**. The script turns off the most expensive
quarters to reach `OFF_QUARTERS_TARGET` for the day, using a pure greedy
algorithm (most expensive first) when the boiler temperature sensor is
available. This produces the maximum savings while the boiler temp guard
directly ensures hot water safety.

### Boiler temperature guard

The corrected boiler temperature (raw DS18B20 + `DS18B20_BOILER_OFFSET`)
is checked every run. If it falls below the season's minimum threshold,
the boiler is forced ON regardless of the price schedule. This replaces
a time-based consecutive-off limit with a direct physical measurement,
adapting automatically to actual heat demand rather than guessing.

### Season inference

Summer and winter modes apply different boiler minimum temperature
thresholds. The season is inferred from the corrected boiler temperature
(above `SEASON_INFERENCE_THRESHOLD_C` = winter, below = summer) and
persisted in the state file. It only flips after
`SEASON_CHANGE_READINGS` consecutive readings suggesting the other
season, preventing a temporary dip from triggering an incorrect switch.

### Indoor temperature and frost guard

Indoor temperature is read in four steps, descending reliability:
1. DS18B20 index 100 via Shelly Add-on (wired, always-on)
2. Shelly H&T local HTTP (only when device is awake)
3. Shelly H&T Shelly Cloud (last reported value, regardless of sleep)
4. Last saved value in state file (fail-safe if older than `MAX_TEMP_AGE_HOURS`)

If indoor temperature falls below `FROST_GUARD_C`, the boiler is forced
ON regardless of price or schedule.

### Schedule file

`schedule.csv` is overwritten every run with today's complete plan:

```
# Schedule generated: 2026-01-15 09:00:01
# Season: winter | Boiler min temp: 68.0°C | boiler temp guard active
# Off quarters: 64/96 (target 64)
# ##...####...
time,price_sek_per_kwh,scheduled,actual
00:00,0.3120,ON,ON
00:30,0.1540,OFF,OFF
08:00,1.8540,ON,ON        ← boiler temp guard forced ON despite schedule
...
14:00,0.2100,OFF,         ← future quarter, actual not yet known
```

The `actual` column fills in as the day progresses. Discrepancies
between `scheduled` and `actual` show where guards overrode the price
schedule — useful for tuning thresholds.

**NOTE:** The API only returns the Nordpool spot price. Grid fees and
electricity tax (~0.46 SEK/kWh) always apply on top and cannot be
optimized away.

---

## Fail-safe behavior

The script is designed to never leave the house unprotected:

| Situation | What happens |
|---|---|
| Spot price API down | Boiler ON |
| DS18B20 indoor sensor unavailable | Falls back to H&T chain |
| H&T asleep (normal) | Tries Shelly Cloud → uses last known cloud value |
| H&T asleep AND Cloud down | Uses last saved local value (if <4h old) |
| All indoor temp sources too old | Boiler ON |
| DS18B20 boiler sensor unavailable | Falls back to `MAX_CONSEC_OFF_QUARTERS` constraint |
| Unexpected program error | Boiler ON |
| Pi or router loses power | Shelly "Power On Default Mode" = ON |

---

## FAQ

**How do I adjust how long the boiler stays on?**
Change `OFF_QUARTERS_TARGET` in `config.py`. Each unit is 15 minutes;
64 ≈ 16h off per day. During cold periods, lower this value to keep
the boiler on more.

**How do I adjust the hot water minimum temperature?**
Change `BOILER_MIN_TEMP_SUMMER_C` or `BOILER_MIN_TEMP_WINTER_C`.
If the boiler temp sensor reads consistently lower than actual water
temperature, adjust `DS18B20_BOILER_OFFSET` first.

**How do I force a specific season?**
Set `SEASON_OVERRIDE = "summer"` or `SEASON_OVERRIDE = "winter"` in
`config.py`. Takes effect on the next 15-minute run. Set back to `None`
to resume automatic inference.

**What if the boiler temperature sensor fails?**
The script falls back to the `MAX_CONSEC_OFF_QUARTERS` constraint —
the same time-based approach used before the sensor was added. A warning
is logged and the season defaults to the last known value (or "winter"
if unknown, which applies the safer higher threshold).

**Can I view the logs nicely?**
```bash
# Latest decisions
LANG=en_US.UTF-8 tail -24 /home/boiler/boiler/boiler.log

# Follow live
LANG=en_US.UTF-8 tail -f /home/boiler/boiler/boiler.log

# Today's schedule at a glance
cat /home/boiler/boiler/schedule.csv
```

---

## Known limitations / possible future improvements

- **Midnight boundary effect (fallback only)** — when the boiler temp
  sensor is unavailable and the script falls back to
  `MAX_CONSEC_OFF_QUARTERS`, the constraint is applied per calendar day.
  An off-period ending late and one starting early the next day are
  counted separately, so the real combined off-time across midnight could
  exceed the limit. This only applies in the fallback scenario; normal
  operation uses the boiler temp guard which is not affected by calendar
  boundaries.
- **DS18B20 calibration is a fixed offset** — `DS18B20_BOILER_OFFSET`
  assumes a constant gap between surface and water temperature. In
  practice the gap may vary slightly with boiler temperature or flow
  conditions. Periodic verification against a reference thermometer is
  recommended.
- **Cloud logging not yet implemented** — currently all data is stored
  locally on the Pi. Future improvement: ship log entries to a time-series
  database (e.g. InfluxDB Cloud) for long-term analysis and visualisation
  without relying on the Pi's SD card.
- **Radiator feed temperature is logged but unused** — the feed sensor
  provides useful context for understanding how quickly the boiler cools
  under different heat demand conditions. After a full season of data,
  this could inform more sophisticated control logic.
