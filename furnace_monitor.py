#!/usr/bin/env python3
"""
furnace_monitor.py — Samples furnace power LEDs every 30s via go2rtc snapshot.

Writes actual power draw to FURNACE_POWER_FILE so boiler_control.py can
retroactively correct its duty-based peak estimates with real measurements.

Run as a systemd service — see furnace-monitor.service.

LED positions (in -90° rotated TAPO C200 frame, calibrated 2026-11-14):
  9 kW:    (524, 1149)
  4.5 kW:  (549, 1159)
  2.25 kW: (571, 1170)

Calibration thresholds (R channel, 6-pixel radius sample):
  ON:  R ≈ 213  |  OFF: R ≈ 84–92  |  Threshold: 150
"""

import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

from config import FURNACE_POWER_FILE

GO2RTC_URL   = "http://localhost:1984/api/frame.jpeg?src=furnace"
LOG_FILE     = "/home/boiler/boiler/furnace_monitor.log"
SAMPLE_SECS  = 30
LED_RADIUS   = 6
ON_THRESHOLD = 150

# (label, center_x, center_y, kW) — ordered highest to lowest so the first
# lit LED found wins (boiler steps: only one element active at a time).
LEDS = [
    ("9kW",    524, 1149, 9.0),
    ("4.5kW",  549, 1159, 4.5),
    ("2.25kW", 571, 1170, 2.25),
]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)


def grab_frame() -> Image.Image:
    r = requests.get(GO2RTC_URL, timeout=10)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).rotate(-90, expand=True)


def read_power_kw(img: Image.Image) -> float:
    for _label, cx, cy, kw in LEDS:
        region  = img.crop((cx - LED_RADIUS, cy - LED_RADIUS, cx + LED_RADIUS, cy + LED_RADIUS))
        pixels  = list(region.getdata())
        avg_r   = sum(p[0] for p in pixels) / len(pixels)
        if avg_r > ON_THRESHOLD:
            return kw
    return 0.0


def quarter_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:") + f"{(dt.minute // 15) * 15:02d}"


def load_state() -> dict:
    try:
        return json.loads(Path(FURNACE_POWER_FILE).read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    Path(FURNACE_POWER_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(FURNACE_POWER_FILE).write_text(json.dumps(state, indent=2))


def main() -> None:
    logging.info("furnace_monitor starting")
    while True:
        try:
            now      = datetime.now()
            img      = grab_frame()
            power_kw = read_power_kw(img)
            qkey     = quarter_key(now)

            state = load_state()

            if state.get("current_quarter_key") != qkey:
                # Archive completed quarter before resetting
                if state.get("current_quarter_key"):
                    state["prev_quarter_key"]    = state["current_quarter_key"]
                    state["prev_quarter_avg_kw"] = state.get("current_quarter_avg_kw", 0.0)
                state["current_quarter_key"]     = qkey
                state["current_quarter_samples"] = []

            state["current_quarter_samples"].append(power_kw)
            samples                        = state["current_quarter_samples"]
            state["current_quarter_avg_kw"] = sum(samples) / len(samples)
            state["current_kw"]            = power_kw
            state["current_time"]          = now.isoformat()

            save_state(state)
            logging.info(
                f"{power_kw:.2f} kW  "
                f"(quarter avg {state['current_quarter_avg_kw']:.2f} kW, {len(samples)} samples)"
            )

        except Exception as e:
            logging.warning(f"Sample failed: {e}")

        time.sleep(SAMPLE_SECS)


if __name__ == "__main__":
    main()
