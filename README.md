# Boiler Control – CTC 1100 with Shelly G3 Plus 1

Controls the immersion heater in a CTC 1100 boiler based on the
Nordpool day-ahead spot price (Swedish bidding zones SE1–SE4).
Runs every 15 minutes on a Raspberry Pi (electricity prices have
been set per quarter-hour, not per hour, since 2025-10-01).

Built for a vacation/weekend home: visited roughly once a month,
boiler otherwise left on frost-guard. The goal is to shift the
immersion heater's runtime toward cheap price periods while
guaranteeing it's never off long enough to let hot water or house
heating suffer, and to never freeze the house if anything in the
chain fails.

---

## Hardware

| Component | Model | Where | Price |
|---|---|---|---|
| Smart relay for the boiler | **Shelly G3 Plus 1** | e.g. styrahem.se (SHELLY-G3-1) | ~215 SEK |
| Temperature sensor | Shelly H&T | – | – |
| Computer | Raspberry Pi (any model) | – | – |

**NOTE:** Wiring the Shelly G3 Plus 1 into the boiler's tariff input
(230V) should be done by a licensed electrician.

---

## Files

```
boiler/
├── config.example.py   ← Template – copy to config.py and fill in your values
├── config.py            ← Your actual settings (gitignored, never commit this)
├── boiler_control.py    ← Main script
├── boiler_state.json    ← Created automatically (last known indoor temperature)
└── boiler.log           ← Log of every decision
```

---

## Installation

### 1. Prepare the Pi

```bash
sudo apt update && sudo apt upgrade -y
pip3 install requests --break-system-packages
```

### 2. Copy the files

```bash
mkdir -p /home/pi/boiler
# Copy config.example.py and boiler_control.py to /home/pi/boiler/
cp config.example.py config.py
```

### 3. Configure

Open `config.py` and fill in your values:

```python
BOILER_SHELLY_IP = "192.168.1.XX"  # IP of the Shelly G3 Plus 1
HT_SHELLY_IP     = "192.168.1.YY"  # IP of your Shelly H&T
PRICE_ZONE       = "SE3"            # Stockholm = SE3

OFF_QUARTERS_TARGET     = 64  # Target 15-min periods/day to turn off (64 ≈ 16h)
MAX_CONSEC_OFF_QUARTERS = 8   # Max consecutive off quarters (8 ≈ 2 hours) – keeps hot water warm
```

**Shelly Cloud fallback (recommended for the H&T):**

The original Shelly H&T is battery-powered and sleeps between
reports to save battery. Local HTTP polling (`/status`) only works
when the device happens to be awake, which makes it unreliable for a
script running every 15 minutes. The fix is to fetch the last
reported value from Shelly Cloud instead – the cloud always holds
the latest value, regardless of whether the device is asleep.

Get your credentials:
1. Open the Shelly app → **User Settings** (your profile icon)
2. Scroll to **Authorization cloud key**
3. Tap to reveal/generate it → gives you both an `auth_key` and a server URI

Fill in `config.py`:
```python
CLOUD_AUTH_KEY = "your-auth-key-here"
CLOUD_SERVER   = "shelly-XX-eu.shelly.cloud"   # your server name
HT_DEVICE_ID   = "XXXXXX"                       # H&T's MAC, last 6 hex chars
```

`HT_DEVICE_ID` can be found in the `"mac"` field when calling
`http://[H&T-IP]/status` locally while the device is awake — use
only the last 6 characters, lowercase.

⚠️ **Treat `CLOUD_AUTH_KEY` like a password** — it grants full
control over your Shelly devices. Never commit `config.py` with
real values anywhere public (e.g. GitHub). `config.py` should be
gitignored; only `config.example.py` belongs in the repo.

**Tip:** Set static IP addresses for both Shelly devices in your
router's DHCP settings (MAC reservation), otherwise the IPs may
change on reboot.

### 4. Test manually

```bash
cd /home/pi/boiler
python3 boiler_control.py
tail boiler.log
```

Expected log output:
```
2026-01-15 03:00  Quarter 03:00 | 18.5°C | 0.312 SEK/kWh | Boiler: OFF (scheduled off (expensive period))
2026-01-15 08:00  Quarter 08:00 | 18.2°C | 1.854 SEK/kWh | Boiler: ON  (normal operation)
2026-01-15 09:00  Quarter 09:00 | 7.8°C  | 2.100 SEK/kWh | Boiler: ON  (frost guard (7.8°C < 10.0°C))
```

### 5. Add to cron

```bash
crontab -e
```

Add the following line (runs every 15 minutes – required since
electricity prices are now set per quarter-hour, not per hour):
```
*/15 * * * * python3 /home/pi/boiler/boiler_control.py
```

---

## Fail-safe behavior

The script is designed to never leave the house unprotected:

| Situation | What happens |
|---|---|
| Spot price API down | Boiler ON |
| H&T asleep (normal) | Tries Shelly Cloud → uses last known cloud value |
| H&T asleep AND Cloud down | Uses last saved local value (if <4h old) |
| H&T battery dead / all data too old | Boiler ON |
| Unexpected program error | Boiler ON |
| Pi or router loses power | Shelly's "default state" = ON (configure in the Shelly app) |

**Important Shelly setting:** Go to the Shelly app → G3 Plus 1 →
Settings → Power On Default Mode → set to **ON**. Otherwise the
boiler stays OFF if power is cut and later restored.

---

## How it works

1. Every run (every 15 minutes) the script fetches today's
   quarter-hour prices from
   [elprisetjustnu.se](https://www.elprisetjustnu.se) (free, open
   API). Since 2025-10-01, Nordpool delivers 96 prices/day (one per
   quarter-hour) instead of 24 hourly prices.
2. The script builds an **off-schedule** for the whole day: the
   boiler is ON by default, and the most expensive quarters are
   turned off — but never more than `MAX_CONSEC_OFF_QUARTERS` in a
   row. This guarantees hot water regularly gets a chance to reheat,
   even during long expensive stretches, instead of risking 16
   straight hours with no heating just because the cheapest hours
   happen to cluster overnight.

   The algorithm is greedy: it tries to turn off quarters in
   descending price order (most expensive first), but skips a
   quarter if doing so would create a contiguous off-period longer
   than allowed. The result is a "comb" pattern – long but bounded
   off-blocks, broken up by forced on-quarters.

   **Known limitation:** the schedule is built per calendar day. An
   off-period ending late in the evening and one starting early the
   next day are counted separately, so the real contiguous off-time
   across midnight could in theory exceed `MAX_CONSEC_OFF_QUARTERS`.
3. Indoor temperature is fetched in three steps, descending
   reliability:
   - **Local** – only works if the H&T happens to be awake at that moment
   - **Shelly Cloud** – last reported value, regardless of sleep state
   - **Last saved value** on the Pi – last resort before fail-safe
4. If the indoor temperature is below `FROST_GUARD_C` degrees, the
   boiler runs regardless of price or schedule.
5. The decision, source, and price are logged to `boiler.log`.

**NOTE:** The API only returns the spot price (Nordpool day-ahead).
Grid fees and electricity tax (~0.46 SEK/kWh) always apply on top
and can't be optimized away.

**NOTE 2:** Since prices are now set per quarter-hour, the script
must run every 15 minutes (not every hour) for the schedule to
switch at the right granularity — otherwise quarters that don't
fall on the hour get missed.

---

## FAQ

**Why does the script sometimes not read the H&T live?**
The H&T is battery-powered and sleeps between reports. It only wakes
on a temperature change (>0.5°C) or a scheduled report. The script
then uses the last saved value, which is sufficient for frost
guarding.

**How do I adjust how long the boiler stays on?**
Two dials in `config.py`, both in units of 15 minutes:
- `OFF_QUARTERS_TARGET` – total quarters/day to try to turn off.
  64 ≈ 16h off, 8h on. Higher = more savings, but more time off.
- `MAX_CONSEC_OFF_QUARTERS` – max consecutive quarters the boiler
  may be off. 8 ≈ 2h. Lower = hot water stays warm more often, but
  you lose some savings potential since more expensive quarters get
  forced on.

During particularly cold periods, you can temporarily lower
`OFF_QUARTERS_TARGET` to keep the boiler on more often.

**Can I view the logs nicely?**
```bash
# Last 24 lines
tail -24 /home/pi/boiler/boiler.log

# Follow the log live
tail -f /home/pi/boiler/boiler.log
```

---

## Known limitations / possible future improvements

- **Midnight boundary effect** – the off-schedule is computed per
  calendar day, so the consecutive-off constraint doesn't see across
  midnight. Could be fixed with a rolling 24h window (fetch
  yesterday + today + tomorrow, trim to `[now-4h, now+20h]`) instead
  of a fixed calendar-day window.
- **No direct hot water temperature monitoring** – the script
  optimizes purely on price + a consecutive-off cap, as a proxy for
  "hot water won't get too cold." Direct monitoring of the boiler's
  water temperature isn't feasible with this hardware.
- **H&T cloud sync delay** – the cloud value can lag the sensor's
  actual state by up to its reporting interval (varies, but can be
  several hours if temperature doesn't change much), since the
  device only reports on a schedule or on a >0.5°C change.
