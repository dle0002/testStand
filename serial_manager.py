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
_HALL_RE      = re.compile(r'POL:([+-]) V:([\d.]+) RPM:(\d+)')
_RPM_ZERO_RE  = re.compile(r'RPM:0')

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

            if source == 'hall':
                m = _HALL_RE.search(line)
                if m:
                    polarity = m.group(1)
                    v = float(m.group(2))
                    with _lock:
                        _state['hall']['rpm']      = _rpm_median(int(m.group(3)))
                        _state['hall']['voltage']  = v
                        _state['hall']['polarity'] = polarity
                        if polarity == '+':
                            _state['hall']['pos_voltage'] = v
                            if _cal_active:
                                _cal_samples_pos.append(v)
                        else:
                            _state['hall']['neg_voltage'] = v
                            if _cal_active:
                                _cal_samples_neg.append(v)
                elif _RPM_ZERO_RE.search(line):
                    _rpm_window.clear()
                    with _lock:
                        _state['hall']['rpm'] = 0

            elif source == 'loadcell':
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
            # Sniff may have toggled debug to an unknown state.
            # Wait up to 1.5 s for an ISR heartbeat; if none arrives, send 'd'
            # to enable debug output.
            time.sleep(0.1)
            ser.reset_input_buffer()
            deadline = time.time() + 1.5
            got_output = False
            while time.time() < deadline:
                if ser.in_waiting:
                    chunk = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                    if 'ISR:' in chunk or 'POL:' in chunk:
                        got_output = True
                        break
                time.sleep(0.1)
            if not got_output:
                _write(ser, "d")  # enable debug heartbeat
        threading.Thread(target=_read_loop, args=(source, ser), daemon=True).start()
        print(f"[serial] {source} connected on {port}")
        return True
    except Exception as e:
        print(f"[serial] {source} failed on {port}: {e}")
        return False


def _sniff_port(port: str) -> str | None:
    """Poke a port with device-specific wake commands then identify by response.

    LOG:1  — starts output on ESC and load cell (hall ignores it)
    d      — toggles hall debug heartbeat; hall replies 'debug ON/OFF' or
             starts emitting 'ISR:' lines; ESC/loadcell ignore it
    """
    try:
        ser = serial.Serial(port, BAUD, timeout=1)
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b'LOG:1\r\n')   # wake ESC / load cell
        ser.write(b'd\r\n')       # wake hall debug heartbeat
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
                or 'debug ON' in text or 'debug OFF' in text):
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
    """Send the same SYNC epoch to every connected device back-to-back.
    All three devices must receive the same value so their timestamps are
    comparable in the CSV/plot.  Called after all devices finish connecting.
    """
    epoch_ms = int(time.time() * 1000)
    cmd = f"SYNC:{epoch_ms}"
    with _lock:
        serials = {s: ser for s, ser in _serials.items()}
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
