# ESC Handler

ESP32-based ESC controller with DShot300 output, KISS/BLHeli_32 telemetry reception, and a live TFT display. Runs on a TTGO T-Display (240×135 ST7789V).

---

## Wiring

| Signal | ESP32 GPIO | Notes |
|---|---|---|
| DShot300 out | GPIO 25 | → ESC signal wire |
| Telemetry in | GPIO 26 | ← ESC TLM/TX pin (UART2 RX) |
| Telemetry TX | GPIO 27 | Not connected; required by UART2 init |

> Add a ~1 kΩ pull-up resistor from the telemetry line to 3.3 V.

---

## Serial interface

Connect at **115200 baud, 8N1** (the same port used for flashing).

### Commands

All commands are newline-terminated and case-insensitive.

| Command | Description |
|---|---|
| `ARM` | Arm the ESC — sets throttle to 48 (motor stopped, lowest armed value) |
| `DISARM` | Disarm — sets throttle to 0, motor off |
| `T:<n>` | Set throttle to `n` (range 48–2047). `T:0` is equivalent to `DISARM` |
| `LOG:<hz>` | Stream telemetry to serial at `<hz>` Hz (e.g. `LOG:10`). `LOG:0` stops logging |
| `SYNC:<epoch_ms>` | Set the time reference — see [Time synchronisation](#time-synchronisation) |
| `?` / `HELP` | Print command summary |

### Log output format

```
[1714483234567ms] THR:  512  RPM:  8400  14.82V  3.10A  43°C  55mAh
```

Before `SYNC:` is received the timestamp is raw `millis()` (small number). After sync it is Unix epoch in milliseconds (13-digit number).

Python regex to parse log lines:
```
r'\[(\d+)ms\] THR:\s*(\d+)\s+RPM:\s*(\d+)\s+([\d.]+)V\s+([\d.]+)A\s+(\d+).C\s+(\d+)mAh'
```

---

## Startup sequence

On power-on the firmware:
1. Holds the DShot line LOW for 500 ms
2. Sends 4000 disarm frames (throttle 0) over 4 s to arm the ESC
3. Starts the UART2 telemetry listener
4. Launches the DShot keepalive task on Core 0

The motor will not move until you send `ARM` followed by a `T:` command with a value above 48.

---

## Display layout

```
[ ARMED ]   1024    50.0%
████████████████░░░░░░░░
────────────────────────
RPM          VOLT
 14400        14.82V
TEMP          AMPS
  43°C         3.10A
MAH           TELEM
  55mAh        OK
────────────────────────
LOG:10.0Hz
```

| Field | Description |
|---|---|
| ARM badge | Green = armed, dark = disarmed |
| Throttle bar | Colour: green < 30 %, yellow < 70 %, red ≥ 70 % |
| TELEM | `OK` = fresh data, `STALE` = no packet for > 3 s, `NONE` = never received |

---

## Telemetry

The ESC must support the 10-byte KISS / BLHeli_32 UART telemetry format at **115200 baud**.

| Byte(s) | Field | Unit |
|---|---|---|
| 0 | Temperature | °C |
| 1–2 | Voltage (big-endian uint16) | ÷ 100 → V |
| 3–4 | Current (big-endian uint16) | ÷ 100 → A |
| 5–6 | Consumption (big-endian uint16) | mAh |
| 7–8 | eRPM (big-endian uint16) | × 100 → eRPM |
| 9 | CRC-8 over bytes 0–8 | poly **0x07** |

Shaft RPM is derived as `eRPM / POLE_PAIRS`. `POLE_PAIRS` defaults to **7** (14-pole motor) — adjust in `main.cpp` if needed.

---

## Key constants (main.cpp)

| Constant | Default | Description |
|---|---|---|
| `THR_MIN` | 48 | Lowest armed throttle value |
| `THR_MAX` | 2047 | Maximum throttle value |
| `POLE_PAIRS` | 7 | Motor poles ÷ 2 |
| `CRC_POLY` | 0x07 | Telemetry CRC polynomial |

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

---

## Build environments (platformio.ini)

| Environment | Description |
|---|---|
| `ttgo_t_display` | Full build with TFT display (default) |
| `telem_test` | Minimal no-display build for telemetry diagnostics — prints raw byte counts, CRC stats, and hex dumps to serial |
