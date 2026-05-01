# hall_sampler

RPi Pico firmware that samples an analog hall effect sensor on a two-bladed propeller and reports peak voltages and RPM over USB serial. Designed for propeller speeds up to ~30,000 RPM.

## Hardware

### Sensor wiring

| Sensor pin | Pico pin |
|------------|----------|
| VCC | 3V3 OUT (pin 36) |
| GND | GND (pin 38) |
| OUT | GPIO26 / ADC0 (pin 31) |

No connection is needed on ADC_VREF (pin 35) — it is already tied to 3.3 V internally on the Pico board.

> **Note:** Check your sensor's supply voltage. If it is a 5 V part its output can swing up to 5 V, which would damage the Pico ADC input. In that case add a voltage divider (e.g. 10 kΩ / 20 kΩ) on the signal line before GPIO26.

### Signal shape

The two blades carry magnets of opposite polarity. As the propeller spins, the sensor output is roughly sinusoidal with:
- A **positive** voltage excursion above VCC/2 when the first blade passes.
- A **negative** excursion below VCC/2 when the second blade passes.
- A flat region near VCC/2 (≈ 1.65 V) when neither blade is close.

One full revolution produces one positive peak and one negative peak.

## Connecting

The Pico exposes a USB CDC serial port. Connect with any serial terminal at **115200 baud** (baud rate has no effect on USB CDC, but most tools require a value — use 115200).

The firmware waits 2 seconds after power-on before printing anything, to give the host time to open the port. Once connected you will see:

```
hall_sampler starting
sampling at 50 kHz
```

## Serial output

### Peak events (always active)

One line is printed for every detected peak:

```
[1714483234567ms] POL:+ V:3.12 RPM:2998 ts:48291820us
```

| Field | Description |
|-------|-------------|
| `[ms]` | Timestamp — Unix epoch ms after `SYNC:`, raw ms since boot before |
| `POL:+` | Peak above VCC/2 (first blade) |
| `POL:-` | Peak below VCC/2 (second blade, opposite magnet) |
| `V:` | Voltage at the peak apex in volts (2 decimal places) |
| `RPM:` | Estimated RPM, computed from the period between the two most recent same-polarity peaks. Shows `0` on the very first peak of each polarity. |
| `ts:` | Raw ISR timestamp in microseconds — always present for sub-ms relative timing |

Example output at ~3000 RPM:

```
[1714483234567ms] POL:+ V:3.12 RPM:2998 ts:48291820us
[1714483234978ms] POL:- V:0.19 RPM:3001 ts:48292231us
[1714483235567ms] POL:+ V:3.11 RPM:3000 ts:48292820us
```

Python regex:
```
r'\[(\d+)ms\] POL:([+-]) V:([\d.]+) RPM:(\d+) ts:(\d+)us'
```

### Stopped propeller

After 500 ms of silence (no peak detected), a single line is printed and the RPM history is reset:

```
[1714483240123ms] RPM:0
```

### Debug heartbeat (on by default)

When debug mode is enabled, one diagnostic line is printed every second:

```
ISR:<count> MID:<midpoint> BUF:<pending>
```

| Field | Description |
|-------|-------------|
| `ISR:` | Total number of ADC samples taken since boot (~50,000 per second expected) |
| `MID:` | Current estimated signal midpoint in ADC counts (0–4095). Should settle near 2048. |
| `BUF:` | Number of unprocessed samples sitting in the ring buffer. A consistently high value means the main loop is slightly behind the ISR; this does not affect measurement accuracy. |

## Serial commands

All commands are newline-terminated.

| Command | Action |
|---------|--------|
| `d` | Toggle debug heartbeat on/off. Responds with `debug ON` or `debug OFF`. Debug is **on** by default. Disable it when logging data at high RPM to reduce serial noise. |
| `SYNC:<epoch_ms>` | Set the time reference — see [Time synchronisation](#time-synchronisation) |

## Tuning

The following constants at the top of `src/main.cpp` can be adjusted without changing the algorithm:

| Constant | Default | Description |
|----------|---------|-------------|
| `SAMPLE_PERIOD_US` | `20` (50 kHz) | ISR interval in microseconds. Lower = faster sampling. |
| `ENTER_THRESH` | `200` | ADC counts (0–4095) above/below midpoint required to start a peak. Increase if noise triggers false peaks; decrease if real peaks are missed. ~0.16 V at 3.3 V reference. |
| `EXIT_THRESH` | `100` | ADC counts within midpoint to end a peak and record it. Should be less than `ENTER_THRESH` (hysteresis). |
| `IDLE_TIMEOUT_US` | `500000` (500 ms) | Silence duration in microseconds before `RPM:0` is printed. |

## How RPM is calculated

RPM is derived from the time between two consecutive peaks of the **same polarity** (i.e. one full revolution):

```
RPM = 60 × 1,000,000 / period_us
```

The first peak of each polarity after a stop shows `RPM:0` because there is no previous timestamp to compare against. RPM becomes valid from the second peak onward.

## Time synchronisation

All three devices (esc_handler, loadcell_handler, hall_sampler) use the same scheme so measurements can be correlated in a shared database.

Send `SYNC:<unix_epoch_ms>` to each device. Each device records its local `millis()` at the moment the command is received and uses the difference to convert all subsequent timestamps to wall-clock time.

Device replies: `OK sync epoch=<high>:<low> local=<millis>`

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
def parse_ts(line):
    import re
    m = re.match(r'\[(\d+)ms\]', line)
    if not m:
        return None
    ts = int(m.group(1))
    # ts > 1e12  →  Unix epoch ms (after SYNC)
    # ts < 1e12  →  raw millis since boot (before SYNC)
    return ts
```

The `ts:` field in peak lines always holds the raw ISR microsecond value regardless of sync state, useful for sub-millisecond relative timing within a single session.

---

## Flashing

Built with PlatformIO. Upload target is COM10 (set in `platformio.ini`). To flash:

```
pio run --target upload
```

Or use the PlatformIO IDE upload button.
