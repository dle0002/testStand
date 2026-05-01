# Load Cell Handler

ESP32-based load cell reader with HX711 amplifier, live TFT display, calibration stored in flash, and configurable serial logging. Runs on a TTGO T1 (240×135 ST7789V).

---

## Wiring

| Signal | ESP32 GPIO | Notes |
|---|---|---|
| HX711 SCK | GPIO 33 | Clock out |
| HX711 DT | GPIO 32 | Data in |

---

## Serial interface

Connect at **115200 baud, 8N1**.

### Commands

All commands are newline-terminated and case-insensitive.

| Command | Description |
|---|---|
| `TARE` | Zero the scale with no load — averages 16 samples and saves offset to flash |
| `CAL:<grams>` | Calibrate with a known weight on the scale (e.g. `CAL:100`). Run `TARE` first. Saves scale factor to flash |
| `LOG:<hz>` | Stream readings at `<hz>` Hz (e.g. `LOG:10`). `LOG:0` stops. Default: 5 Hz |
| `SYNC:<epoch_ms>` | Set the time reference — see [Time synchronisation](#time-synchronisation) |
| `?` / `HELP` | Print command summary and current settings |

### Log output format

```
[1714483234567ms]    123.45g  (raw: 4819234)
```

Before `SYNC:` is received the timestamp is raw `millis()` (small number). After sync it is Unix epoch in milliseconds (13-digit number).

Python regex:
```
r'\[(\d+)ms\]\s+([\d.\-]+)g\s+\(raw:\s*([\d\-]+)\)'
```

When not yet calibrated:
```
[1714483234567ms] raw: 4819234  (not calibrated)
```

---

## Calibration workflow

1. Ensure the scale is empty, then send `TARE` — wait ~2 s for 16-sample average
2. Place a known weight on the scale, then send `CAL:<grams>` — wait ~2 s
3. Both offset and scale factor are saved to flash and survive reboots

**Settings persist across power cycles.** To recalibrate, simply repeat the two steps above.

---

## Display layout

```
CAL OK                  LOG:5.0Hz
┌────────────────────────────────┐
│                                │
│           123.5                │
│                              g │
│ raw: 4819234                   │
├────────────────────────────────┤
│ tare: 123456  scale: 450.23    │
└────────────────────────────────┘
```

| Field | Description |
|---|---|
| Top-left badge | `CAL OK` (green) or `NO CAL` (red) |
| Top-right | Current log rate (shown when active) |
| Centre | Weight in grams — white = positive, cyan = near zero, orange = negative |
| Raw | Raw ADC value from HX711 |
| Bottom bar | Stored tare offset and scale factor (raw units per gram) |

---

## Key constants (main.cpp)

| Constant | Default | Description |
|---|---|---|
| `HX711_SCK` | 33 | Clock GPIO |
| `HX711_DT` | 32 | Data GPIO |
| `g_log_interval_ms` | 200 | Initial log interval (5 Hz) |

The HX711 gain is fixed at **Channel A, Gain 128** (25 SCK pulses per reading). Sample rate depends on the RATE pin on the HX711 module: low = 10 Hz, high = 80 Hz.

---

## Time synchronisation

All three devices (esc_handler, loadcell_handler, hall_sampler) use the same scheme so measurements can be correlated in a shared database.

Send `SYNC:<unix_epoch_ms>` to each device. Each device records its local `millis()` at the moment the command is received and uses the difference to convert all subsequent timestamps to wall-clock time.

Device replies: `OK sync epoch=<epoch> local=<millis>`

**RPi example (Python):**

```python
import serial, time

devices = [esc_ser, loadcell_ser, hall_ser]   # open serial.Serial instances

epoch_ms = int(time.time() * 1000)
cmd = f"SYNC:{epoch_ms}\n".encode()
for dev in devices:
    dev.write(cmd)
```

USB serial latency is ~1–5 ms per device so all clocks align to well within 100 ms.

**Parsing timestamps:**

```python
import re

def parse_ts(line):
    m = re.match(r'\[(\d+)ms\]', line)
    if not m:
        return None
    ts = int(m.group(1))
    # ts > 1e12  →  Unix epoch ms (after SYNC)
    # ts < 1e12  →  raw millis since boot (before SYNC)
    return ts
```
