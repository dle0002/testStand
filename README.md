# Propeller Teststand

A real-time propeller testing platform that simultaneously measures motor RPM, thrust, voltage, current, temperature, and blade pitch angle. Three independent microcontrollers connect to a Raspberry Pi running a Flask web server that provides live monitoring, motor control, data recording, and pitch calibration.

> Sub-system documentation: [README_esc.md](README_esc.md) · [README_hallsensor.md](README_hallsensor.md) · [README_loadcell.md](README_loadcell.md)

---

## System Architecture

```
┌──────────────────────────────────────────────────┐
│            Raspberry Pi (Central Controller)      │
│                                                  │
│  Flask Web Server (:5000)                        │
│  ├── Motor control  (throttle / closed-loop RPM) │
│  ├── Live telemetry streaming (SSE)              │
│  ├── CSV recording & matplotlib plots            │
│  └── Blade pitch angle calibration              │
│                                                  │
│  serial_manager.py                               │
│  ├── Auto-detect USB serial ports               │
│  ├── Parse telemetry from all three devices     │
│  └── Route commands to the correct device       │
└──────────┬───────────────┬──────────────┬────────┘
           │ USB CDC       │ USB CDC      │ USB CDC
           ▼               ▼              ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │ ESC Handler│  │ LoadCell   │  │ Hall       │
    │  (ESP32)   │  │ Handler    │  │ Sampler    │
    │            │  │  (ESP32)   │  │ (RP Pico)  │
    │ DShot300   │  │ HX711 ADC  │  │ 50 kHz ADC │
    │ KISS telem │  │ Load cell  │  │ Hall sensor│
    │ TFT display│  │ TFT display│  │            │
    └────────────┘  └────────────┘  └────────────┘
```

---

## Hardware Components

### ESC Handler (ESP32 + TTGO T-Display)

Controls a brushless ESC and decodes its telemetry.

| Signal | GPIO | Notes |
|--------|------|-------|
| DShot300 out | 25 | → ESC signal wire |
| Telemetry RX | 26 | ← ESC TLM/TX pin (UART2 RX) |
| Telemetry TX | 27 | Not connected; required by UART2 init |

- Protocol: **DShot300**, throttle range 48–2047 (mapped from 1–100% by the server)
- Telemetry: **10-byte KISS/BLHeli_32** format over UART2 at 115200 baud
- Decoded fields: temperature (°C), voltage (V), current (A), consumption (mAh), eRPM → shaft RPM
- Shaft RPM formula: `eRPM / POLE_PAIRS` — default `POLE_PAIRS = 7` (14-pole motor)
- Live **240×135 ST7789V TFT display** shows arm state, throttle bar, and all telemetry fields

**Display layout:**
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

**Startup sequence:**
1. Holds DShot line LOW for 500 ms
2. Sends 4000 disarm frames (throttle 0) over 4 s to arm the ESC
3. Starts the UART2 telemetry listener
4. Launches DShot keepalive task on Core 0

**Key firmware constants (`main.cpp`):**

| Constant | Default | Description |
|----------|---------|-------------|
| `THR_MIN` | 48 | Lowest armed throttle value |
| `THR_MAX` | 2047 | Maximum throttle value |
| `POLE_PAIRS` | 7 | Motor poles ÷ 2 |
| `CRC_POLY` | 0x07 | Telemetry CRC-8 polynomial |

---

### Load Cell Handler (ESP32 + HX711)

Measures propeller thrust via a load cell.

| Signal | GPIO |
|--------|------|
| HX711 SCK | 33 |
| HX711 DT | 32 |

- HX711 configured for **Channel A, Gain 128** (25 SCK pulses per reading)
- Tare offset and calibration scale factor stored in **ESP32 flash** — survive power cycles
- Default logging rate: **5 Hz** (configurable via `LOG:<hz>`)
- Also has a **240×135 ST7789V TFT display**

**Calibration workflow:**
1. With nothing on the scale, send `TARE` to zero the reading
2. Place a known reference weight, send `CAL:<grams>` to set the scale factor

---

### Hall Sampler (Raspberry Pi Pico)

Measures propeller RPM from a hall effect sensor.

| Signal | Pin |
|--------|-----|
| Analog hall input | GPIO26 / ADC0 (pin 31) |

- Samples at **50 kHz** (`SAMPLE_PERIOD_US = 20`)
- Detects peaks above/below the midpoint (VCC/2 ≈ 1.65 V) using hysteresis:
  - Entry threshold: **200 ADC counts** (~0.16 V)
  - Exit threshold: **100 ADC counts**
- Two-blade propeller with opposite-polarity magnets → one positive + one negative peak per revolution
- RPM computed from the period between consecutive same-polarity peaks
- Prints `RPM:0` after **500 ms** of silence (propeller stopped)

**Key firmware constants (`src/main.cpp`):**

| Constant | Default | Description |
|----------|---------|-------------|
| `SAMPLE_PERIOD_US` | 20 | ADC sample interval → 50 kHz |
| `ENTER_THRESH` | 200 | Peak detection entry threshold (ADC counts) |
| `EXIT_THRESH` | 100 | Peak detection exit threshold |
| `IDLE_TIMEOUT_US` | 500000 | Silence before RPM:0 is emitted |

---

## Repository Structure

```
testStand/
├── server.py              # Flask web server and core business logic
├── serial_manager.py      # USB serial device management and telemetry parsing
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html         # Single-page web UI (HTML + CSS + vanilla JS)
├── start.sh               # Boot script (runs setup_ap.sh then server.py)
├── setup_ap.sh            # Creates "PropellerTeststand" WiFi hotspot if offline
├── teststand.service      # systemd unit file for auto-start on boot
├── README_esc.md          # ESC handler sub-system documentation
├── README_hallsensor.md   # Hall sampler sub-system documentation
└── README_loadcell.md     # Load cell handler sub-system documentation
```

### server.py — Flask Application

| Component | Description |
|-----------|-------------|
| `_streamer()` | Background thread — forwards incoming serial lines and periodic status snapshots to the browser via **Server-Sent Events (SSE)** |
| `_recorder()` | Background thread — polls the current device state at a configurable rate (default 10 Hz) and appends rows to an in-memory CSV buffer while recording |
| `_rpm_controller()` | Background thread — closed-loop RPM controller running at 5 Hz; reads hall/ESC RPM and adjusts throttle to track a user-set target |
| `/api/motor/*` | ARM, DISARM, set throttle %, set target RPM, emergency stop |
| `/api/recording/*` | Start/stop recording, download CSV, generate matplotlib plot |
| `/api/calibration/*` | Collect calibration point, delete point, clear all, run calibration ramp |

**RPM controller parameters:**

| Parameter | Value |
|-----------|-------|
| Update interval | 200 ms (5 Hz) |
| RPM deadband | ±75 RPM |
| Throttle step per iteration | 0.5%–2.0% |
| Throttle ceiling | 85% |
| Throttle floor | 1% |
| Safety abort | If throttle reaches 98% with no RPM response |

### serial_manager.py — Device I/O

- **Auto-detection:** Sends `LOG:1` and `d` to each USB port, waits 4 s, identifies devices by response keywords:
  - **Hall:** `"hall_sampler"`, `"POL:"`, `"ISR:"`, `"debug ON/OFF"`
  - **Load cell:** `"raw:"`, `"CAL"`, `"TARE"`, `"not calibrated"`
  - **ESC:** `"THR:"`, `"ARMED"`, `"DShot"`, `"DISARM"`
- **Reconnection:** Monitors device health and transparently reconnects dropped ports
- **Telemetry parsing:** Regex-based parsers for each device (see formats below)
- **Command routing:** Forwards server commands to the correct serial port

---

## Communication Protocol

All devices use **115200 baud, 8N1** over USB CDC virtual serial ports. Commands are ASCII strings terminated with `\r\n`.

### Commands

| Device | Command | Description |
|--------|---------|-------------|
| **ESC** | `ARM` | Arm the ESC |
| | `DISARM` | Disarm ESC, throttle to zero |
| | `T:<48–2047>` | Set DShot throttle value |
| | `LOG:<hz>` | Set telemetry streaming rate |
| | `SYNC:<epoch_ms>` | Synchronize wall-clock timestamp |
| | `?` / `HELP` | Print available commands |
| **Load cell** | `TARE` | Zero the scale |
| | `CAL:<grams>` | Calibrate with known weight |
| | `LOG:<hz>` | Set logging rate |
| | `SYNC:<epoch_ms>` | Synchronize wall-clock timestamp |
| **Hall** | `d` | Toggle debug heartbeat output |
| | `SYNC:<epoch_ms>` | Synchronize wall-clock timestamp |

### Telemetry Formats

**ESC log line:**
```
[1714483234567ms] THR:  512  RPM:  8400  14.82V  3.10A  43°C  55mAh
```
```python
r'\[(\d+)ms\] THR:\s*(\d+)\s+RPM:\s*(\d+)\s+([\d.]+)V\s+([\d.]+)A\s+(\d+).C\s+(\d+)mAh'
```

**Hall peak event:**
```
[1714483234567ms] POL:+ V:3.12 RPM:2998 ts:48291820us
[1714483240123ms] RPM:0
```
```python
r'\[(\d+)ms\] POL:([+-]) V:([\d.]+) RPM:(\d+) ts:(\d+)us'
```

**Load cell:**
```
[1714483234567ms] 123.45g (raw: 4819234)
[1714483234567ms] raw: 4819234 (not calibrated)
```
```python
r'\[(\d+)ms\]\s+([\d.\-]+)g\s+\(raw:\s*([\d\-]+)\)'
```

### Time Synchronization

All three devices share a wall-clock scheme so their readings can be correlated precisely:

1. Server sends `SYNC:<unix_epoch_ms>` to each device
2. Each device captures its local `millis()` at reception and computes: `offset = epoch_ms - local_millis`
3. All subsequent log timestamps are `local_millis + offset` (13-digit Unix epoch ms)

Before `SYNC`, timestamps are raw `millis()` (small numbers, < 1 × 10¹²). After `SYNC` they are Unix epoch milliseconds.

USB serial latency is ~1–5 ms per device, so all clocks align to within ~100 ms.

```python
import serial, time

epoch_ms = int(time.time() * 1000)
cmd = f"SYNC:{epoch_ms}\n".encode()
for dev in [esc_ser, loadcell_ser, hall_ser]:
    dev.write(cmd)
```

---

## Web Interface

Navigate to `http://<pi_ip>:5000`.

**Left panel:**
- **Motor control** — Throttle slider (0–100%) or RPM target input; ARM / DISARM / Emergency Stop buttons; switch between throttle and RPM mode
- **Live telemetry table** — RPM (hall), RPM (ESC), thrust (g), voltage (V), current (A), temperature (°C), energy (mAh), hall peak voltages (V+, V−), estimated pitch angles
- **Raw command sender** — Select device from dropdown, type any ASCII command, send directly
- **Logging rate** — Adjust recording sample rate (Hz)
- **Recording controls** — Start/stop, download CSV, view plot
- **Pitch calibration** — Collect points at known angles, delete individual points, clear all calibration data

**Right panel:**
- Six live metric tiles: RPM (hall), RPM (ESC), thrust, voltage, current, throttle %
- Three scrolling serial log panels (hall / load cell / ESC), color-coded, with per-panel clear buttons

**Plot modal:**
Six-panel matplotlib figure generated from the last recording:
1. RPM (hall vs. ESC)
2. Thrust (g)
3. Voltage and current
4. Temperature (°C)
5. Hall peak voltages (V+, V−)
6. Estimated pitch angles (from positive and negative peaks)

---

## Data Recording

When recording is active, `_recorder()` polls device state at the configured rate and builds a CSV.

| Column | Description |
|--------|-------------|
| `time_s` | Elapsed seconds since recording start |
| `throttle_pct` | Motor throttle (0–100%) |
| `rpm_hall` | RPM from hall sensor (median-filtered, window = 5) |
| `weight_g` | Thrust from load cell (grams) |
| `esc_rpm` | RPM from ESC telemetry |
| `voltage_v` | Battery voltage |
| `current_a` | Motor current |
| `temp_c` | ESC temperature |
| `mah` | Energy consumed (mAh) |
| `pos_voltage` | Hall positive peak voltage (V) |
| `neg_voltage` | Hall negative peak voltage (V) |
| `pitch_pos` | Pitch angle from positive peak (°, requires calibration) |
| `pitch_neg` | Pitch angle from negative peak (°, requires calibration) |

---

## Blade Pitch Angle Calibration

The hall sensor's analog peak voltages vary with blade pitch angle. Calibration maps voltages to degrees via linear interpolation.

**Workflow:**
1. Set the propeller to a known pitch angle (e.g. 0°, 5°, 10°, ...)
2. Click **Collect** in the UI — the server ramps throttle 5% → 15% → 5% over ~20 s
3. Positive and negative peak voltages are averaged over the ramp and stored as one calibration point
4. Repeat for each desired pitch angle
5. At runtime, live peak voltages are interpolated → estimated pitch in degrees
6. Calibration data persists in `calibration.json`

---

## Setup & Installation

### Dependencies

```bash
pip install -r requirements.txt
```

Requires: Flask 3.0+, flask-socketio 5.0+, pyserial 3.5+, matplotlib 3.8+

### Systemd Auto-Start (Raspberry Pi)

```bash
sudo cp teststand.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable teststand
sudo systemctl start teststand
```

The service runs `start.sh` as user `pi` on every boot.

### WiFi Hotspot

`setup_ap.sh` checks for internet connectivity at startup. If the Pi is offline it creates a local hotspot:

| Setting | Value |
|---------|-------|
| SSID | `PropellerTeststand` |
| Password | `teststand123` |

Connect a device to this network and open `http://192.168.4.1:5000`.

### Manual Start

```bash
python server.py
```

On startup the server auto-detects connected USB devices and sends an initial `SYNC` to all of them.

---

## Startup Sequence

1. **RPi boot** — systemd starts `start.sh`
2. **`setup_ap.sh`** — checks connectivity; creates WiFi hotspot if offline
3. **`server.py`**:
   - `serial_manager.start()` — sniffs USB ports, identifies and connects all three devices
   - Sends `SYNC:<epoch_ms>` to every device to align clocks
   - Launches three background threads: `_streamer`, `_recorder`, `_rpm_controller`
   - ESC begins telemetry at 1 Hz by default
4. **Web server** listens on port 5000

---

## Build Environments

Each microcontroller project uses PlatformIO. The ESC handler defines two environments in `platformio.ini`:

| Environment | Description |
|-------------|-------------|
| `ttgo_t_display` | Full build with TFT display (default) |
| `telem_test` | Minimal no-display build for telemetry diagnostics — prints raw byte counts, CRC stats, and hex dumps |
