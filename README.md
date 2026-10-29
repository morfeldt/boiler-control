# Boiler Control – CTC 1100 with Shelly G3 Plus 1

Controls the immersion heater in a CTC 1100 combination boiler based on
the Nordpool day-ahead spot price (Swedish bidding zones SE1–SE4).
Runs every 15 minutes on a Raspberry Pi (electricity prices have been
set per quarter-hour, not per hour, since 2025-10-01).

Built for a vacation/weekend home: visited roughly once a month, boiler
otherwise left on frost-guard. The goal is to shift the immersion
heater's runtime toward cheap price periods while protecting the monthly
peak tariff (effektavgift), ensuring the boiler never drops too cold,
and never freezing the house if anything in the chain fails.

Occupancy is set via a virtual switch in Apple Home (Homebridge +
homebridge-cmdswitch2). When away the boiler is fully off. When away
in winter, if frost risk is detected, the full price-optimised home mode
activates automatically.

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
├── config.py            ← Tunable settings — tracked in git, safe to commit
├── local.py             ← Site-specific values (IPs, keys, coordinates) — gitignored, stays on Pi
├── local.example.py     ← Template for local.py
├── boiler_control.py    ← Main Pi script
├── shelly_watchdog.js   ← Watchdog script to install on the Shelly G3 Plus 1
├── heartbeat.php        ← Upload to your web server (receives Pi pings)
├── watchdog.php         ← Upload to your web server (run via cPanel cron)
├── boiler_state.json       ← Created automatically (persisted state: temps, actuals, monthly peak)
├── slow_reheat_duty.txt    ← Created by --increment-duty; absent = use config default
├── force_emergency.txt     ← Created by --boost (transition mode); auto-deleted when boiler is warm
├── schedule.csv            ← Today's on/off plan, overwritten every 15 min
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

### 3. Clone the repo and create local.py

```bash
cd /home/boiler
sudo -u boiler git clone https://github.com/YOUR_USERNAME/boiler-control.git boiler
cd boiler
sudo -u boiler cp local.example.py local.py
```

Open `local.py` and fill in your site-specific values:

```python
BOILER_SHELLY_IP       = "192.168.1.XX"   # IP of the Shelly G3 Plus 1
HT_SHELLY_IP           = "192.168.1.YY"   # IP of the Shelly H&T
HT_SHELLY_GEN          = 1                # 1 = H&T original, 2 = Plus H&T
CLOUD_AUTH_KEY         = "your-auth-key"  # Shelly app → User Settings → Authorization cloud key
CLOUD_SERVER           = "shelly-XX-eu.shelly.cloud"
HT_DEVICE_ID           = "XXXXXX"         # Last 6 hex chars of H&T MAC, lowercase
SERVER_HEARTBEAT_URL   = "https://yoursite.com/boiler/heartbeat.php"
SERVER_HEARTBEAT_TOKEN = "your-secret-token"
WEATHER_LAT            = 0.0000           # Property latitude
WEATHER_LON            = 0.0000           # Property longitude
```

⚠️ **`local.py` is gitignored and must never be committed.** It contains
credentials and your property's location.

**Tip:** Set static IP addresses for all Shelly devices in your router's
DHCP settings (MAC reservation) to prevent IPs changing on reboot.

### 4. Tune settings (optional)

All tunable settings live in `config.py`, which is tracked in git with
sensible defaults. Edit it directly if you need to change thresholds —
for example `OFF_QUARTERS_TARGET`, `SLOW_REHEAT_TRIGGER_C`, or the
frost protection thresholds. Changes are picked up on the next
`git pull` on the Pi.

**Boiler sensor calibration** — surface-mounted sensors read lower than
actual water temperature. Measure the gap against a reference thermometer:
```python
DS18B20_BOILER_OFFSET = 0.0  # °C added to raw DS18B20 boiler reading
```

### 5. Test manually

```bash
sudo -u boiler python3 /home/boiler/boiler/boiler_control.py
LANG=en_US.UTF-8 tail -3 /home/boiler/boiler/boiler.log
```

Expected log output:
```
2026-01-15 03:00  Quarter 03:00 | Occupancy: home | Indoor: 18.5°C (read 03:00) | Outside: n/a | Boiler: 72.3°C | Feed: 45.1°C | 0.312 SEK/kWh | Boiler: OFF (scheduled off (expensive period))
2026-01-15 08:00  Quarter 08:00 [PEAK] | Occupancy: home | Indoor: 18.2°C (read 07:55) | Outside: n/a | Boiler: 62.1°C | Feed: 38.2°C | 1.854 SEK/kWh | Boiler: OFF (peak window — holding off (boiler 62.1°C ≥ 40.0°C))
2026-01-15 02:00  Quarter 02:00 | Occupancy: home | Indoor: 17.4°C (read 01:55) | Outside: n/a | Boiler: 58.3°C | Feed: 30.2°C | 0.218 SEK/kWh | Boiler: ON [slow] (slow reheat (58.3°C < 65.0°C))
2026-01-15 04:00  Quarter 04:00 | Occupancy: away | Indoor: 11.2°C (read 03:55) | Outside: 3.1°C | Boiler: 52.3°C | Feed: 28.1°C | 0.310 SEK/kWh | Boiler: OFF (away frost protection active — scheduled off (expensive period))
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

### 8. Set up web server watchdog

Upload `heartbeat.php` and `watchdog.php` to `public_html/boiler/` on
your web server. Edit `heartbeat.php` and set `SECRET_TOKEN` to the same
value as `SERVER_HEARTBEAT_TOKEN` in `config.py`.

Test the endpoint from the command line:
```bash
# Should return HTTP 200 and write a timestamp file
curl -v "https://yoursite.com/boiler/heartbeat.php?token=YOUR_TOKEN"

# Should return a Unix timestamp
curl "https://yoursite.com/boiler/last_heartbeat.txt"

# Should produce no output when heartbeat is fresh
/usr/local/bin/php ~/public_html/boiler/watchdog.php
```

Add the cPanel cron job (every 30 minutes):
```
*/30 * * * *   /usr/local/bin/php /home/YOUR_CPANEL_USERNAME/public_html/boiler/watchdog.php
```

cPanel emails the output to your account address whenever the script
produces output — i.e. only when the Pi has been silent for >30 minutes.

### 9. Install Shelly watchdog script

In the Shelly app → G3 Plus 1 → Scripts → Create new script.
Paste the contents of `shelly_watchdog.js` and enable it.

The script runs a timer every minute on the Shelly itself, reads the
Pi's heartbeat from local KVS, and forces the boiler ON if the Pi has
been silent for 45 minutes while the boiler is OFF. This works entirely
over the local network and is not affected by internet outages.

### 10. Set up Homebridge occupancy switch

The occupancy switch appears in Apple Home and controls whether the
boiler runs in Home or Away mode.

**Install the plugin** via the Homebridge UI: Plugins → search
`homebridge-cmdswitch2` → Install.

**Fix permissions** so Homebridge can write the occupancy file:
```bash
sudo usermod -a -G boiler homebridge
sudo chmod g+w /home/boiler/boiler
sudo -u boiler sh -c 'echo home > /home/boiler/boiler/occupancy.txt'
sudo chmod 664 /home/boiler/boiler/occupancy.txt
sudo systemctl restart homebridge
```

Verify with:
```bash
groups homebridge   # should include 'boiler'
sudo -u homebridge sh -c 'echo away > /home/boiler/boiler/occupancy.txt'
cat /home/boiler/boiler/occupancy.txt   # should show: away
sudo -u homebridge sh -c 'echo home > /home/boiler/boiler/occupancy.txt'
```

**Add to Homebridge `config.json`** (Config tab in Homebridge UI,
add to the `platforms` array):
```json
{
    "platform": "cmdSwitch2",
    "name": "CMD Switch",
    "switches": [
        {
            "name": "Stuga - Hemma",
            "on_cmd": "echo home > /home/boiler/boiler/occupancy.txt",
            "off_cmd": "echo away > /home/boiler/boiler/occupancy.txt",
            "state_cmd": "grep -q home /home/boiler/boiler/occupancy.txt",
            "polling": false,
            "timeout": 5
        }
    ]
}
```

In Apple Home: switch **ON** = Home (normal operation),
switch **OFF** = Away (reduced/off mode). If the file is missing,
the script defaults to "home" — so a Pi crash never accidentally leaves
the boiler in away mode.

---

## Deploying updates

`config.py` and `boiler_control.py` are both tracked in git. To deploy
any change — code fix, threshold tweak, new feature — just pull on the Pi:

```bash
sudo -u boiler git -C /home/boiler/boiler pull
```

`local.py` is gitignored and is never touched by a pull. No SCP, no
chmod/chown, no file juggling.

To migrate from the old single-file layout (gitignored `config.py`):

```bash
# On the Pi
cd /home/boiler/boiler
sudo -u boiler cp local.example.py local.py
# Copy your IPs, keys and coordinates from the old config.py into local.py
sudo -u boiler nano local.py
# Then pull the new repo structure
sudo -u boiler git pull
```

---

## How it works

### Occupancy modes

Occupancy is set via a virtual switch in Apple Home (switch ON = Home,
switch OFF = Away). The state is written to `occupancy.txt` by the
Homebridge plugin and read on every Pi run.

| Mode | Behaviour |
|---|---|
| Home | Price schedule + peak guard + slow reheat + frost guard |
| Away | Boiler **fully OFF** unless frost risk detected |

**Away mode frost protection** uses hysteresis to avoid cycling.
Full Home mode activates when either condition is met:
- Indoor temp drops below `AWAY_WINTER_ACTIVATE_INDOOR_C` (12°C), or
- Outside temp drops below `AWAY_WINTER_ACTIVATE_OUTSIDE_C` (5°C) —
  a preemptive trigger, since the boiler needs time to heat up from
  cold temperatures before it can contribute to frost protection.

Full Home mode deactivates only when both recover:
- Indoor temp above `AWAY_WINTER_DEACTIVATE_INDOOR_C` (16°C), and
- Outside temp above `AWAY_WINTER_DEACTIVATE_OUTSIDE_C` (8°C).

The activation state is persisted in the state file so recovery
heating continues correctly across Pi reboots and power outages.

Outside temperature is fetched from the
[Open-Meteo API](https://open-meteo.com) (free, no API key required)
using the property's coordinates. It is only fetched in Away mode —
in Home mode `outside` shows as `n/a` in the log.

Frost protection is driven purely by outdoor and indoor temperatures —
in summer, outdoor temperatures are high enough that frost risk never
triggers, so the boiler stays off automatically.

### Scheduling

Every run, the script fetches today's 96 quarter-hour prices from
[elprisetjustnu.se](https://www.elprisetjustnu.se) (free, open API —
Nordpool moved to quarter-hour resolution on 2025-10-01).

The boiler is **ON by default**. The script turns off the most expensive
quarters to reach `OFF_QUARTERS_TARGET` for the day, using a pure greedy
algorithm (most expensive first) when the boiler temperature sensor is
available.

### Slow reheat — protecting the monthly peak tariff

The CTC 1100 immersion heater draws 9 kW. Running it at full power into
cold radiators (e.g. on arrival at the house) can create a sustained
9 kW hourly average, directly impacting Degerfors Energi's
effektavgift (50 kr/kW/month, worst hour) and högbelastningsavgift
(+40 kr/kW/month, Nov–Mar weekday 07:00–19:00, worst hour). A single bad
hour can cost 400+ kr in monthly tariff alone.

Instead of a single on/off threshold, the boiler operates in three tiers:

| Boiler temp | Mode | Grid draw |
|---|---|---|
| ≥ 65°C (`SLOW_REHEAT_TRIGGER_C`) | Normal — follow price schedule | Full 9 kW while ON |
| 50–65°C | **Slow reheat** — price-scaled duty, always on in home mode | 0.9–3 kW average (10–33% duty) |
| < 50°C (`SLOW_REHEAT_MIN_C`) | **Emergency** — full power for the quarter | Full 9 kW |

**Slow reheat in home mode** fires every quarter regardless of the price
schedule, but the duty scales with this quarter's price rank among all
96 quarters of the day: the cheapest quarter gets `SLOW_REHEAT_DUTY`
(max, e.g. 33%), the most expensive gets `SLOW_REHEAT_MIN_DUTY` (min,
e.g. 10%), and intermediate quarters are linearly interpolated by rank.
Rank-based scaling means price outliers don't compress the useful range —
a median-priced quarter always gets roughly half the range regardless of
how extreme the cheapest or most expensive quarter is.

This keeps the boiler stable during occupied stays without requiring 9 kW
emergency spikes at expensive prices: a 10% draw (~0.9 kW average) during
an expensive quarter costs far less than a full 9 kW spike a few quarters
later once the boiler dips below `SLOW_REHEAT_MIN_C`.

**Slow reheat in away mode** (frost protection) keeps the original
scheduled-off gate — overnight cheap slots are sufficient when nobody is
home using hot water.

**Slow reheat** uses the Shelly's native `toggle_after` parameter:
the relay turns ON, the Pi sets a timer for `duty × 900` seconds
(e.g. 0.33 × 900 = 297 s ≈ 5 min), and the Shelly cuts power
automatically — without changing the Pi's 15-minute poll interval.

The log tag shows the actual duty used each quarter:
```
Quarter 14:30 | ... | Boiler: ON [slow 18%] (slow reheat (62.1°C < 65.0°C, 18% duty))
Quarter 01:15 | ... | Boiler: ON [slow 31%] (slow reheat (58.3°C < 65.0°C, 31% duty))
```

**Emergency mode** (full quarter at 9 kW) is a genuine safety measure
for a very cold boiler during normal home use, not a peak-tariff risk,
because the boiler is below 50°C only in unusual conditions.

### Peak guard (Nov–Mar weekday 07:00–19:00)

Degerfors Energi's effekttariff has two components:
- **Effektavgift** — 50 kr/kW/month, based on the single worst hourly
  average across the entire month.
- **Högbelastningsavgift** — 40 kr/kW/month (Nov–Mar only), based on
  the single worst hourly average during weekday 07:00–19:00.

During the peak window, the script suppresses all heating unless the
boiler drops below `PEAK_GUARD_TEMP_C` (40°C). Off-peak slow reheat
(typically overnight in cheap price slots) pre-heats the boiler to ≥65°C,
building a thermal buffer so it can coast through the morning peak without
the element running at all.

The log marks peak-window runs with a `[PEAK]` tag:
```
Quarter 08:15 [PEAK] | ... | Boiler: OFF (peak window — holding off (boiler 63.5°C ≥ 40.0°C))
```

### Slow-reheat duty override

The slow-reheat duty (fraction of each 15-minute slot the element runs)
can be raised from Apple Home without touching `config.py`. This is useful
when you want faster heating — e.g. arriving sooner than expected.

Each tap of the Apple Home switch steps through the presets in
`SLOW_REHEAT_DUTY_STEPS` (e.g. 33% → 50% → 67% → 100% → 50% → …). The
active duty persists in `slow_reheat_duty.txt` until the end of the billing
month, when the script auto-deletes the file and reverts to `SLOW_REHEAT_DUTY`
from `config.py`.

**Monthly peak tracking and auto-unlock:** Every run, the script records the
estimated grid draw for that quarter (0 / duty×9 / 9 kW) and computes the
worst clock-hour average for the current billing month. This is the `Peak`
field shown in the log and schedule file. If the monthly peak already exceeds
what slow reheat at the current duty would draw, limiting the duty no longer
protects the tariff — the script automatically raises the effective duty to
match, so you get faster heating for free.

### Away→home transition warm-up

The occupancy switch in Apple Home is always flipped a day ahead of
arrival. On that transition, the boiler may be cold from Away mode, but
there is no urgent demand — the house will be entered ~24 hours later.

Normally a boiler below 50°C would trigger emergency full-power mode
(9 kW for 15 min). Instead, when the script detects an away→home
transition, it sets a flag in the state file that suppresses emergency
mode. The boiler slow-reheats the whole way up from cold during cheap
overnight price slots at 33% duty (~3 kW average).

The flag is cleared once the boiler reaches 50°C
(`SLOW_REHEAT_MIN_C`), at which point normal slow reheat takes over
automatically for the 50→65°C range.

### Heating boost switch ("Stuga - Värmeboost")

One combined switch handles both manual fast-heat (transition override)
and duty boost (extra power during a stay).

**Behaviour depends on state when you tap ON:**

| Situation | What `--boost` does |
|-----------|---------------------|
| Away→home transition in progress (boiler cold) | Clears transition flag → emergency full-power (9 kW) until boiler ≥ 50°C, then auto-clears |
| Normal home mode | Raises slow-reheat duty to 100% for `BOOST_DURATION_HOURS` (default 2 h), then auto-resets |

Tapping OFF always cancels any active boost immediately.

**Note:** A duty boost raises the monthly peak — the auto-unlock logic
will keep the effective duty elevated for the rest of the billing month.

**Complete Homebridge `config.json`** (both switches; replaces the single-switch
config from step 10):
```json
{
    "platform": "cmdSwitch2",
    "name": "CMD Switch",
    "switches": [
        {
            "name": "Värmesystem (av = frostvakt)",
            "on_cmd":    "echo home > /home/boiler/boiler/occupancy.txt",
            "off_cmd":   "echo away > /home/boiler/boiler/occupancy.txt",
            "state_cmd": "grep -q home /home/boiler/boiler/occupancy.txt",
            "polling": false,
            "interval": 60,
            "timeout": 5,
            "manufacturer": "CTC",
            "model": "1100 with Boiler Control (GitHub)",
            "serial": "123456"
        },
        {
            "name": "Värmeboost",
            "on_cmd":    "python3 /home/boiler/boiler/boiler_control.py --boost && python3 /home/boiler/boiler/boiler_control.py",
            "off_cmd":   "python3 /home/boiler/boiler/boiler_control.py --clear-boost",
            "state_cmd": "test -f /home/boiler/boiler/force_emergency.txt || test -f /home/boiler/boiler/slow_reheat_duty.txt",
            "polling": false,
            "interval": 60,
            "timeout": 15,
            "manufacturer": "CTC",
            "model": "1100 with Boiler Control (GitHub)",
            "serial": "123457"
        }
    ]
}
```

- Switch **ON** — activates boost (context-aware, see table above).
- Switch **OFF** — cancels any active boost immediately.
- Switch stays ON while either `force_emergency.txt` or `slow_reheat_duty.txt` exists.

To check the current duty from the Pi:
```bash
cat /home/boiler/boiler/slow_reheat_duty.txt   # active override, or "no such file" = config default
```

### Indoor temperature and frost guard

Indoor temperature is read in four steps, descending reliability:
1. DS18B20 index 100 via Shelly Add-on (wired, always-on)
2. Shelly H&T local HTTP (only when device is awake)
3. Shelly H&T Shelly Cloud (last reported value, regardless of sleep)
4. Last saved value in state file (fail-safe if older than `MAX_TEMP_AGE_HOURS`)

If indoor temperature falls below `FROST_GUARD_C`, the boiler is forced
ON regardless of price or schedule.

### Watchdog system

Two independent watchdogs protect against Pi crashes, operating at
different layers:

**Local watchdog (Shelly script):** The Pi writes a Unix timestamp to
the Shelly's KVS on every run. A script running on the Shelly reads this
every minute. If the timestamp is stale (>45 minutes) and the boiler is
OFF, the boiler is forced ON. This works entirely over the local network
and is unaffected by internet outages — it is the primary boiler
fail-safe for a Pi crash.

**Remote watchdog (web server + cPanel cron):** The Pi also pings a PHP
endpoint on your web server on every run. A cron job runs the watchdog
script every 30 minutes; if the last ping is older than 30 minutes, an
email alert is sent. This is notification-only — it tells you the Pi is
down so you can investigate, but does not control the boiler. Internet
outages will trigger false alerts, which are safe to ignore since the
local Shelly watchdog handles the actual boiler fail-safe.

### Schedule file

`schedule.csv` is overwritten every run with today's complete plan:

```
# Schedule generated: 2026-01-15 09:00:01
# Occupancy: home | Outside: n/a | slow reheat <65°C / emergency <50°C
# Off quarters: 64/96 (target 64)
# ##...####...
time,price_sek_per_kwh,scheduled,actual
00:00,0.3120,ON,ON
00:30,0.1540,OFF,OFF
08:00,1.8540,ON,ON        ← price schedule says ON; peak guard may still suppress
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

The system is designed to never leave the house unprotected:

| Situation | What happens |
|---|---|
| Spot price API down | Boiler ON in Home mode; OFF in Away mode |
| DS18B20 indoor sensor unavailable | Falls back to H&T chain |
| H&T asleep (normal) | Tries Shelly Cloud → uses last known cloud value |
| H&T asleep AND Cloud down | Uses last saved local value (if <4h old) |
| All indoor temp sources too old | Boiler ON (Home); frost protection activates (Away) |
| DS18B20 boiler sensor unavailable | Falls back to `MAX_CONSEC_OFF_QUARTERS` constraint |
| Away mode, indoor temp unavailable | Frost protection activated as fail-safe → Boiler ON |
| Away mode, outside temp unavailable | Indoor temp alone determines frost risk |
| Unexpected program error | Boiler ON |
| Pi crashes while boiler is OFF | Shelly watchdog forces ON after 45 min |
| Pi crashes while boiler is ON | Boiler stays ON (no action needed) |
| Pi or router loses power | Shelly "Power On Default Mode" = ON |
| Pi down (notification) | Email alert after 30 min via cPanel cron |
| Internet outage (false alert) | Email sent, safe to ignore — local watchdog unaffected |
| Pi reboots, occupancy file missing | Defaults to "home" — boiler runs normally |

---

## FAQ

**How do I switch between Home and Away mode?**
Toggle the "Stuga - Hemma" switch in Apple Home (or ask Siri). ON =
Home, OFF = Away. The change takes effect on the next 15-minute Pi run.

**What happens in Away mode if it gets cold?**
When indoor temp drops below 12°C or outside temp drops below 5°C,
the full Home mode system activates automatically — price schedule,
peak guard, slow reheat, and frost guard all run as normal. It deactivates
again only when indoor recovers above 16°C AND outside above 8°C.
The activation state persists across reboots.

**How do I get faster heating from Apple Home?**
Tap the "Stuga - Uppvärmning" switch in Apple Home. Each tap steps the
slow-reheat duty up (e.g. 33% → 50% → 67% → 100%), letting the element run
longer each quarter. Tap OFF to reset to the config default immediately. The
override resets automatically at the start of each billing month.

If the monthly peak power is already high (e.g. from earlier full-power
periods), the script may have already auto-unlocked a higher duty without
you needing to press anything — check `Peak:` in the log.

**How do I adjust how long the boiler stays on?**
Change `OFF_QUARTERS_TARGET` in `config.py`. Each unit is 15 minutes;
64 ≈ 16h off per day. During cold periods, lower this value to keep
the boiler on more.

**I flipped the arrive switch too late and the house is cold — how do I get heat fast?**
Tap the "Stuga - Värmeboost" switch in Apple Home. While the script is in
away→home transition mode, it acts as fast-heat: clears the transition flag
and allows emergency full-power (9 kW) on the next Pi run (within 15 min).
The switch turns itself off automatically once the boiler reaches 50°C
(`SLOW_REHEAT_MIN_C`). Outside transition mode it instead raises the duty
ceiling to 100% for 2 h — useful for getting extra heat during a stay.

**How do I adjust the slow reheat thresholds?**
- `SLOW_REHEAT_TRIGGER_C` — raise to start slow-reheating at a higher
  temperature; lower to let the boiler coast further before intervening.
- `SLOW_REHEAT_MIN_C` — raise if you want emergency mode to kick in
  sooner; lower to trust slow reheat further down.
- `SLOW_REHEAT_DUTY` — the max duty (used for the cheapest quarters and
  as the monthly reset value); raise to heat faster during cheap periods.
- `SLOW_REHEAT_MIN_DUTY` — the min duty (used for the most expensive
  quarters); raise to heat more aggressively even at peak prices; lower
  to prioritise tariff protection over boiler stability.
- `DS18B20_BOILER_OFFSET` — adjust if the sensor reads consistently
  lower than actual water temperature (measure against a thermometer).

**What if the boiler temperature sensor fails?**
The script falls back to the `MAX_CONSEC_OFF_QUARTERS` constraint —
the same time-based approach used before the sensor was added. A warning
is logged. Slow reheat and peak guard require the boiler sensor; without
it, the script falls back to simpler on/off scheduling.

**I got a watchdog email but the boiler seems fine — what happened?**
Most likely a brief internet outage at the house. The local Shelly
watchdog handles the actual boiler fail-safe independently of the
internet. Check the Pi is running (`ssh` in and check the log) and
ignore the alert if everything looks normal.

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
- **Remote watchdog false alarms on internet outage** — the cPanel email
  watchdog cannot distinguish between a Pi crash and a home internet
  outage. The local Shelly watchdog handles actual boiler safety; the
  email alert is informational only.
