"""
Serial manager — connects to the 3 USB microcontrollers:
  hall      RPi Pico   hall_sampler firmware
  loadcell  ESP32      loadcell_handler firmware
  esc       ESP32      esc_handler firmware  (DShot300 + KISS telemetry)

ESC throttle range:
  0       → DISARM
  47-1047 → T:47 … T:1047  (THR_MIN=47, THR_MAX=1047)

Port assignment: set HALL_PORT / LOADCELL_PORT / ESC_PORT explicitly, or
leave all as None to use auto-detection (reads startup banners).
"""
import re
import queue
import threading
import time
from collections import deque

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, find_peaks
import serial
import serial.tools.list_ports

# --- Port config -------------------------------------------------------
HALL_PORT     = None   # e.g. '/dev/ttyACM0'
LOADCELL_PORT = None   # e.g. '/dev/ttyACM1'
ESC_PORT      = None   # e.g. '/dev/ttyACM2'
BAUD          = 115200

# ESC DShot throttle range
_ESC_THR_MIN = 47
_ESC_THR_MAX = 1047

# -----------------------------------------------------------------------
# Hall sensor DSP constants — identical to scope.py so calibration domains match
_HALL_FS       = 50_000
_HALL_ADC_REF  = 3.3
_HALL_ADC_MAX  = 4095
_HALL_V_MID    = _HALL_ADC_REF / 2        # 1.65 V
_HALL_LPF_HZ   = 2_000
_HALL_LPF_ORD  = 4
_HALL_WINDOW_N = int(_HALL_FS * 0.200)    # 10 000 samples = 200 ms
_hall_sos      = butter(_HALL_LPF_ORD, _HALL_LPF_HZ / (_HALL_FS / 2.0),
                        btype='low', output='sos')

# -----------------------------------------------------------------------
# Median filter window for hall RPM — filters single-sample spikes
_RPM_WINDOW_SIZE = 5
_rpm_window: deque = deque(maxlen=_RPM_WINDOW_SIZE)


def _rpm_median(raw: int) -> int:
    _rpm_window.append(raw)
    return sorted(_rpm_window)[len(_rpm_window) // 2]
_LOADCELL_RE  = re.compile(r'\[(\d+)ms\]\s+([\d.\-]+)g\s+\(raw:\s*([\d\-]+)\)')
_ESC_RE       = re.compile(
    r'\[(\d+)ms\] THR:\s*(\d+)\s+RPM:\s*(\d+)\s+([\d.]+)V\s+([\d.]+)A\s+(\d+).C\s+(\d+)mAh'
)

_cal_active      = False
_cal_samples_pos: list = []
_cal_samples_neg: list = []

_state: dict = {
    'hall': {
        'rpm': 0, 'voltage': 0.0, 'pos_voltage': 0.0, 'neg_voltage': 0.0, 'polarity': '+',
        'connected': False, 'port': None,
    },
    'loadcell': {
        'weight': 0.0, 'raw': 0,
        'connected': False, 'port': None,
    },
    'esc': {
        'throttle_raw': 0,
        'armed': False,
        'rpm': 0, 'voltage': 0.0, 'current': 0.0,
        'temp': 0, 'mah': 0,
        'connected': False, 'port': None,
    },
}
_serials: dict[str, serial.Serial] = {}
_log_queue: queue.Queue = queue.Queue(maxsize=2000)
_log_buffer: dict[str, deque] = {
    'hall':     deque(maxlen=300),
    'loadcell': deque(maxlen=300),
    'esc':      deque(maxlen=300),
}
_lock = threading.Lock()


# -----------------------------------------------------------------------
def _write(ser: serial.Serial, cmd: str):
    """Write a command with CRLF line ending (required by ESP32 firmware)."""
    ser.write((cmd + '\r\n').encode())


def _detect_hall_peaks(filt: np.ndarray):
    """Identical to scope.py detect_peaks — adaptive prominence, no hardcoded thresholds."""
    mid      = float(np.median(filt))
    pos_prom = (float(np.max(filt)) - mid) * 0.5
    neg_prom = (mid - float(np.min(filt))) * 0.5
    pos_idx, _ = find_peaks( filt, prominence=pos_prom, height=mid,  distance=50)
    neg_idx, _ = find_peaks(-filt, prominence=neg_prom, height=-mid, distance=50)
    return pos_idx, neg_idx


def _read_loop_hall_binary(ser: serial.Serial):
    """Binary ADC stream reader — same pipeline as scope.py.

    Replaces the text-mode _read_loop for the hall source so that
    pos_voltage / neg_voltage are in the identical centered+LPF-filtered
    domain used when calibration.json was created.

    Peaks are deduplicated by absolute sample index (not wall-clock time) so
    the same peak in the sliding window is never processed twice across loop
    iterations.
    """
    zi           = sosfilt_zi(_hall_sos).astype(np.float64) * 0.0
    raw_buf      = np.zeros(_HALL_WINDOW_N, dtype=np.float32)
    filt_buf     = np.zeros(_HALL_WINDOW_N, dtype=np.float32)
    head         = 0
    leftover     = b''
    sample_count = 0   # total samples received — never wraps (Python int)
    _first_batch = True
    _bad_runs    = 0   # consecutive batches with out-of-range samples

    # Rolling peak-voltage history — same smoothing as scope.py trend line:
    # medfilt(history, min(15, n|1))[-1] gives the median-filtered latest value,
    # placing pos_voltage/neg_voltage in the same domain used when calibration.json
    # was created (scope.py averages mean(medfilt(peaks_in_10s))).
    _PEAK_HIST = 30   # keeps ≥15 samples for full kernel once motor is spinning
    pos_peak_hist: deque = deque(maxlen=_PEAK_HIST)
    neg_peak_hist: deque = deque(maxlen=_PEAK_HIST)

    # Absolute sample index of the last peak recorded per polarity
    last_pos_abs  = -1
    last_neg_abs  = -1
    # Previous positive peak index — used to compute RPM interval
    prev_pos_abs  = None
    # For idle-timeout: sample index of the most recent peak of either polarity
    last_peak_abs = 0

    while True:
        try:
            data = ser.read(4096)
            if not data:
                continue

            data     = leftover + data
            n        = len(data) // 2
            if n == 0:
                leftover = data
                continue
            leftover = data[n * 2:]

            samples = np.frombuffer(data[:n * 2], dtype='<u2').astype(np.float32)

            # 12-bit ADC: valid range 0-4095. Values above that mean the stream
            # is misaligned or the firmware isn't in binary mode yet.
            if np.max(samples) > 4095:
                _bad_runs += 1
                if _bad_runs % 20 == 1:
                    print(f'[serial] hall: invalid ADC values '
                          f'(max={int(np.max(samples))}, run={_bad_runs}) — resyncing')
                # Discard 1 byte to try a different alignment
                leftover = data[1:]
                continue
            _bad_runs = 0

            if _first_batch:
                print(f'[serial] hall binary reader: first valid batch '
                      f'n={n} min={int(np.min(samples))} max={int(np.max(samples))}')
                _first_batch = False

            volts   = samples * (_HALL_ADC_REF / _HALL_ADC_MAX) - _HALL_V_MID

            filt, zi = sosfilt(_hall_sos, volts.astype(np.float64), zi=zi)
            filt     = filt.astype(np.float32)

            m   = len(volts)
            idx = np.arange(head, head + m, dtype=np.int64) % _HALL_WINDOW_N
            raw_buf [idx] = volts
            filt_buf[idx] = filt
            head          = int((head + m) % _HALL_WINDOW_N)
            sample_count += m

            # Ordered 200 ms window, oldest first.
            # Ordered index i → absolute sample index = sample_count - WINDOW_N + i
            filt_window = np.concatenate([filt_buf[head:], filt_buf[:head]])

            pos_idx, neg_idx = _detect_hall_peaks(filt_window)

            for i in pos_idx:
                abs_idx = sample_count - _HALL_WINDOW_N + int(i)
                if abs_idx <= last_pos_abs:
                    continue   # already processed this peak
                v = float(filt_window[i])
                # Accumulate in history; compute medfilt[-1] to match scope.py
                # trend-line domain (the same domain used to build calibration.json).
                # _cal_samples_pos keeps the raw peak voltage so add_cal_point can
                # apply its own medfilt+mean exactly as scope.py does.
                pos_peak_hist.append(v)
                # Running median of last min(15, n) peaks — pure Python, no numpy
                # overhead per peak. Equivalent to medfilt(arr, kernel)[-1].
                win = list(pos_peak_hist)
                n_w = min(15, len(win) | 1)
                fv  = sorted(win[-n_w:])[n_w // 2]
                if prev_pos_abs is not None:
                    interval_s = (abs_idx - prev_pos_abs) / _HALL_FS
                    if 0.002 < interval_s < 0.6:   # 100–30000 RPM
                        with _lock:
                            _state['hall']['rpm'] = _rpm_median(
                                int(round(60.0 / interval_s)))
                prev_pos_abs  = abs_idx
                last_pos_abs  = abs_idx
                last_peak_abs = sample_count
                with _lock:
                    _state['hall']['pos_voltage'] = round(fv, 4)
                    _state['hall']['voltage']     = round(fv, 4)
                    _state['hall']['polarity']    = '+'
                    if _cal_active:
                        _cal_samples_pos.append(v)
                    rpm_now = _state['hall']['rpm']
                # Keep log buffer for REST /api/logs/hall but do NOT push to
                # _log_queue — 50–100 peak events/s would flood the SSE stream
                # and saturate the browser. Hall state arrives via the 1 s status push.
                _log_buffer['hall'].append(f'POL:+ V:{fv:.4f} RPM:{rpm_now}')

            for i in neg_idx:
                abs_idx = sample_count - _HALL_WINDOW_N + int(i)
                if abs_idx <= last_neg_abs:
                    continue   # already processed this peak
                v = float(filt_window[i])
                neg_peak_hist.append(v)
                win = list(neg_peak_hist)
                n_w = min(15, len(win) | 1)
                fv  = sorted(win[-n_w:])[n_w // 2]
                last_neg_abs  = abs_idx
                last_peak_abs = sample_count
                with _lock:
                    _state['hall']['neg_voltage'] = round(fv, 4)
                    if _cal_active:
                        _cal_samples_neg.append(v)
                    rpm_now = _state['hall']['rpm']
                _log_buffer['hall'].append(f'POL:- V:{fv:.4f} RPM:{rpm_now}')

            # Idle timeout: no peaks for 0.5 s worth of samples → RPM = 0
            if (sample_count - last_peak_abs) > int(_HALL_FS * 0.5):
                _rpm_window.clear()
                with _lock:
                    _state['hall']['rpm'] = 0

        except serial.SerialException:
            with _lock:
                _state['hall']['connected'] = False
            try:
                _log_queue.put_nowait(('hall', '[disconnected: hall]'))
            except queue.Full:
                pass
            break
        except Exception as _e:
            print(f'[serial] hall binary reader error: {_e}')
            time.sleep(0.01)



def _read_loop(source: str, ser: serial.Serial):
    while True:
        try:
            raw = ser.readline()
            line = raw.decode('utf-8', errors='replace').rstrip()
            if not line:
                continue

            with _lock:
                _log_buffer[source].append(line)
            try:
                _log_queue.put_nowait((source, line))
            except queue.Full:
                pass

            if source == 'loadcell':
                m = _LOADCELL_RE.match(line)
                if m:
                    with _lock:
                        _state['loadcell']['weight'] = float(m.group(2))
                        _state['loadcell']['raw']    = int(m.group(3))

            elif source == 'esc':
                m = _ESC_RE.match(line)
                if m:
                    with _lock:
                        _state['esc']['throttle_raw'] = int(m.group(2))
                        _state['esc']['rpm']          = int(m.group(3))
                        _state['esc']['voltage']      = float(m.group(4))
                        _state['esc']['current']      = float(m.group(5))
                        _state['esc']['temp']         = int(m.group(6))
                        _state['esc']['mah']          = int(m.group(7))

        except serial.SerialException:
            with _lock:
                _state[source]['connected'] = False
            try:
                _log_queue.put_nowait((source, f'[disconnected: {source}]'))
            except queue.Full:
                pass
            break
        except Exception:
            time.sleep(0.1)


def _connect(source: str, port: str) -> bool:
    try:
        ser = serial.Serial(port, BAUD, timeout=1)
        time.sleep(2)  # let device print its banner
        with _lock:
            _serials[source] = ser
            _state[source]['connected'] = True
            _state[source]['port']      = port
        # SYNC is sent after ALL devices connect so they share one epoch value
        if source == 'esc':
            # ESC does not log by default — start telemetry at 1 Hz on connect
            time.sleep(0.1)
            _write(ser, "LOG:1")
        elif source == 'hall':
            time.sleep(0.1)
            # Identical handshake to scope.py — keep toggling until "raw stream ON"
            # is confirmed.  Handles a Pico left in binary mode by a previous
            # scope.py session (first toggle → OFF, second → ON).
            for _attempt in range(3):
                ser.reset_input_buffer()
                ser.write(b'r\n')
                ser.flush()
                resp, deadline = b'', time.time() + 3.0
                while time.time() < deadline:
                    resp += ser.read(256)
                    if b'raw stream ON' in resp:
                        break
                    if b'raw stream OFF' in resp:
                        break   # need another toggle
                    time.sleep(0.05)
                if b'raw stream ON' in resp:
                    print('[serial] hall binary streaming ON')
                    break
                print(f'[serial] hall toggle attempt {_attempt + 1}: '
                      + resp.decode('utf-8', errors='replace').strip())
            else:
                print('[serial] hall WARNING: could not confirm binary mode')
            ser.reset_input_buffer()
            threading.Thread(target=_read_loop_hall_binary, args=(ser,), daemon=True).start()
        if source != 'hall':
            threading.Thread(target=_read_loop, args=(source, ser), daemon=True).start()
        print(f"[serial] {source} connected on {port}")
        return True
    except Exception as e:
        print(f"[serial] {source} failed on {port}: {e}")
        return False


def _sniff_port(port: str) -> str | None:
    """Poke a port with device-specific wake commands then identify by response.

    LOG:1  — starts output on ESC and load cell (hall ignores it)
    r      — toggles hall raw-stream; firmware ALWAYS replies 'raw stream ON/OFF'
             regardless of current streaming state or debug mode, so this works
             even when the Pico was left in binary-stream mode from a previous
             scope.py or server session (unlike 'd', whose reply is suppressed
             while g_raw_stream is true).
             ESC and load cell ignore 'r'.
    """
    try:
        ser = serial.Serial(port, BAUD, timeout=1)
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b'LOG:1\r\n')   # wake ESC / load cell
        ser.write(b'r\r\n')       # toggle hall raw stream — always gets a text reply
        deadline = time.time() + 4
        data = b''
        while time.time() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            data += chunk
            if data.count(b'\n') >= 3:
                break
        ser.close()
        text = data.decode('utf-8', errors='replace')
        if ('hall_sampler' in text or 'sampling at' in text
                or 'POL:' in text or 'ISR:' in text
                or 'raw stream ON' in text or 'raw stream OFF' in text):
            return 'hall'
        if 'raw:' in text or 'CAL' in text or 'TARE' in text or 'not calibrated' in text:
            return 'loadcell'
        if 'THR:' in text or 'ARMED' in text or 'DShot' in text or 'DISARM' in text:
            return 'esc'
    except Exception:
        pass
    return None


def _auto_detect(needed: list[str]) -> dict[str, str]:
    ports = sorted(
        p.device for p in serial.tools.list_ports.comports()
        if 'ttyACM' in p.device or 'ttyUSB' in p.device
    )
    print(f"[serial] auto-detect scanning: {ports}")
    found: dict[str, str] = {}
    used: set[str] = set()
    for port in ports:
        if len(found) == len(needed):
            break
        source = _sniff_port(port)
        if source and source in needed and source not in found and port not in used:
            found[source] = port
            used.add(port)
    return found


# -----------------------------------------------------------------------
def start():
    """Connect to all three devices. Call once at startup."""
    explicit = {
        'hall':     HALL_PORT,
        'loadcell': LOADCELL_PORT,
        'esc':      ESC_PORT,
    }
    needed  = [s for s, p in explicit.items() if p is None]
    port_map = {s: p for s, p in explicit.items() if p is not None}

    if needed:
        detected = _auto_detect(needed)
        port_map.update(detected)

    for source, port in port_map.items():
        _connect(source, port)

    unconnected = [s for s in ('hall', 'loadcell', 'esc') if s not in port_map]
    if unconnected:
        print(f"[serial] WARNING: no port found for {unconnected}. "
              "Set HALL_PORT / LOADCELL_PORT / ESC_PORT explicitly if needed.")
    sync_all()


def reconnect() -> dict[str, bool]:
    """Re-scan for any devices that are not currently connected.
    Returns a dict of source → True if newly connected."""
    with _lock:
        missing = [s for s in ('hall', 'loadcell', 'esc') if not _state[s]['connected']]

    if not missing:
        print("[serial] reconnect: all devices already connected")
        return {}

    print(f"[serial] reconnect: scanning for {missing}")
    found = _auto_detect(missing)
    results = {}
    for source, port in found.items():
        results[source] = _connect(source, port)
    for source in missing:
        if source not in results:
            results[source] = False
    if any(results.values()):
        sync_all()
    return results


# -----------------------------------------------------------------------
def arm_esc():
    with _lock:
        ser  = _serials.get('esc')
        armed = _state['esc'].get('armed', False)
    if ser and not armed:
        _write(ser, "ARM")
        with _lock:
            _state['esc']['armed'] = True


def disarm_esc():
    with _lock:
        ser = _serials.get('esc')
        _state['esc']['throttle_raw'] = 0
        _state['esc']['armed']        = False
    if ser:
        _write(ser, "DISARM")


def set_motor_speed(speed: int):
    """Set throttle. speed: 0 = DISARM, 47-1047 = DShot value.
    0  → DISARM (motor off, also disarms)
    >0 → ARM if needed, then T:<speed>
    """
    speed = max(0, min(_ESC_THR_MAX, speed))
    if speed == 0:
        disarm_esc()
        return
    with _lock:
        ser   = _serials.get('esc')
        armed = _state['esc'].get('armed', False)
        _state['esc']['throttle_raw'] = speed
    if ser:
        if not armed:
            _write(ser, "ARM")
            with _lock:
                _state['esc']['armed'] = True
            time.sleep(0.05)
        _write(ser, f"T:{speed}")


def sync_all():
    """Send the same SYNC epoch to ESC and loadcell.
    Hall is excluded: it runs in binary streaming mode and the firmware would
    inject the text response into the ADC sample stream, corrupting it.
    """
    epoch_ms = int(time.time() * 1000)
    cmd = f"SYNC:{epoch_ms}"
    with _lock:
        serials = {s: ser for s, ser in _serials.items()
                   if s != 'hall'}          # hall excluded — binary mode
    synced = []
    for source, ser in serials.items():
        try:
            _write(ser, cmd)
            synced.append(source)
        except Exception as e:
            print(f"[serial] SYNC failed for {source}: {e}")
    if synced:
        print(f"[serial] SYNC:{epoch_ms} → {synced}")


def set_log_rate(hz: float):
    """Set telemetry logging rate on ESC and load cell.
    Hall sensor has no rate control (only a debug toggle).
    """
    hz = max(0.1, min(100.0, hz))
    cmd = f"LOG:{hz:.1f}"
    for source in ('esc', 'loadcell'):
        with _lock:
            ser = _serials.get(source)
        if ser:
            _write(ser, cmd)


def send_command(source: str, cmd: str) -> bool:
    """Send a raw serial command to a device."""
    with _lock:
        ser = _serials.get(source)
    if ser:
        _write(ser, cmd.strip())
        # Reflect arm state if user sends ARM/DISARM manually
        if source == 'esc':
            upper = cmd.strip().upper()
            if upper == 'ARM':
                with _lock:
                    _state['esc']['armed'] = True
            elif upper == 'DISARM':
                with _lock:
                    _state['esc']['armed'] = False
        return True
    return False


def get_pending_lines() -> list[tuple[str, str]]:
    lines = []
    while True:
        try:
            lines.append(_log_queue.get_nowait())
        except queue.Empty:
            break
    return lines


def get_status() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def start_cal_collection():
    global _cal_active, _cal_samples_pos, _cal_samples_neg
    with _lock:
        _cal_active      = True
        _cal_samples_pos = []
        _cal_samples_neg = []


def stop_cal_collection() -> tuple:
    """Returns (pos_samples, neg_samples)."""
    global _cal_active
    with _lock:
        _cal_active = False
        return list(_cal_samples_pos), list(_cal_samples_neg)


def get_log_history(source: str, n: int = 200) -> list[str]:
    with _lock:
        return list(_log_buffer.get(source, deque()))[-n:]
