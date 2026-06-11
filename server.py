"""
Propeller Teststand — Flask web server.

Endpoints:
  GET  /                          → web UI
  GET  /events                    → SSE stream
  POST /api/motor/speed           → {"speed": 0-100}
  GET  /api/status                → current device state snapshot
  GET  /api/logs/<source>         → recent log lines
  POST /api/command               → {"source": "...", "command": "..."}
  POST /api/reconnect             → re-scan for missing serial devices
  POST /api/logging/rate          → {"rate": 10}  — sets Pi logging rate
  POST /api/recording/start       → begin recording
  POST /api/recording/stop        → stop recording, save CSV + plot
  GET  /recordings/<filename>     → download a saved CSV
"""
import csv
import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
from scipy.signal import medfilt

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

import serial_manager

app = Flask(__name__)

RECORDINGS_DIR   = os.path.join(os.path.dirname(__file__), 'recordings')
CALIBRATION_PATH = os.path.join(os.path.dirname(__file__), 'calibration.json')
os.makedirs(RECORDINGS_DIR, exist_ok=True)


def _interpolate_pitch(voltage: float, polarity: str, calibration: dict):
    """Return interpolated pitch (float) or None if no calibration points."""
    pts_map = calibration.get(polarity, {})
    if not pts_map:
        return None
    pts = sorted((float(v), float(k)) for k, v in pts_map.items())
    if len(pts) == 1:
        return pts[0][1]
    if voltage <= pts[0][0]:
        return pts[0][1]
    if voltage >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        v0, p0 = pts[i]
        v1, p1 = pts[i + 1]
        if v0 <= voltage <= v1:
            t = (voltage - v0) / (v1 - v0)
            return p0 + t * (p1 - p0)
    return None


def _load_calibration() -> dict:
    try:
        with open(CALIBRATION_PATH) as f:
            data = json.load(f)
        if 'positive' in data and 'negative' in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {'positive': {}, 'negative': {}}


def _save_calibration(cal: dict):
    with open(CALIBRATION_PATH, 'w') as f:
        json.dump(cal, f, indent=2)


_cal_srv_lock = threading.Lock()
_calibration: dict = _load_calibration()   # {"positive": {pitch_str: V}, "negative": {pitch_str: V}}

# -----------------------------------------------------------------------
# SSE
# -----------------------------------------------------------------------
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()


def _sse_push(payload: dict):
    msg = f"data: {json.dumps(payload)}\n\n"
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


@app.route('/events')
def sse_stream():
    q: queue.Queue = queue.Queue(maxsize=500)
    with _clients_lock:
        _clients.append(q)

    def generate():
        try:
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _clients_lock:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# -----------------------------------------------------------------------
# Recording state
# -----------------------------------------------------------------------
_rec_lock   = threading.Lock()
_recording  = False
_rec_data: list[dict] = []
_rec_rate   = 10.0          # Hz — controlled by /api/logging/rate
_rec_start  = 0.0

# -----------------------------------------------------------------------
# Closed-loop RPM controller
# -----------------------------------------------------------------------
_rpm_ctrl_lock   = threading.Lock()
_rpm_enabled     = False
_rpm_target      = 0       # target RPM (0 = disabled)
_rpm_current_thr = 0.0     # float DShot value tracked by controller

# -----------------------------------------------------------------------
# Controlled slowdown
# -----------------------------------------------------------------------
_slowdown_lock   = threading.Lock()
_slowdown_active = False
_SLOWDOWN_TARGET = 500   # RPM — disarm once below this
_SLOWDOWN_STEP   = 10    # DShot units per interval
_SLOWDOWN_INTERVAL = 0.2 # seconds between steps

_RPM_DEADBAND  = 75    # RPM — no adjustment within this band
_RPM_LOCKED_TH = 150   # RPM — "locked" display threshold
_RPM_STEP_MIN  = 5     # DShot minimum step (far from target)
_RPM_STEP_MAX  = 20    # DShot maximum step per interval
_RPM_STEP_NEAR = 2     # DShot step when inside near-zone
_RPM_NEAR_ZONE = 225   # RPM — switch to fine steps within this band (3× deadband)
_RPM_KD        = 0.4   # derivative damping: fraction to subtract when already converging
_RPM_EMA       = 0.4   # EMA alpha for RPM smoothing (higher = less smoothing)
_RPM_INTERVAL  = 0.2   # seconds between controller iterations
_RPM_MAX_THR   = 900   # DShot throttle ceiling
_RPM_MIN_THR   = 47    # DShot throttle floor (armed minimum)

# -----------------------------------------------------------------------
# Pitch angle median filter
# -----------------------------------------------------------------------
_pitch_filter_lock   = threading.Lock()
_pitch_filter_window = 5
_pitch_buf_pos: deque = deque(maxlen=5)
_pitch_buf_neg: deque = deque(maxlen=5)
_pitch_live_pos: float | None = None
_pitch_live_neg: float | None = None

# -----------------------------------------------------------------------
# Trajectory (programmable RPM waypoint sequence)
# -----------------------------------------------------------------------
_traj_lock   = threading.Lock()
_traj_active = False
_traj_data: list[dict] = []   # [{'rpm': int, 'duration_s': float}, ...]


def _generate_plot(data: list[dict], path: str):
    times    = [d['time_s']       for d in data]
    rpm_hall = [d['rpm_hall']     for d in data]
    rpm_esc  = [d['esc_rpm']      for d in data]
    thrust   = [d['weight_g']     for d in data]
    voltage  = [d['voltage_v']    for d in data]
    current  = [d['current_a']    for d in data]
    temp     = [d['temp_c']       for d in data]
    throttle = [d['throttle'] for d in data]
    pos_v    = [d.get('pos_voltage', 0.0) for d in data]
    neg_v    = [d.get('neg_voltage', 0.0) for d in data]

    def _to_float(v):
        try:    return float(v)
        except: return None

    pitch_pos_raw = [_to_float(d.get('pitch_pos')) for d in data]
    pitch_neg_raw = [_to_float(d.get('pitch_neg')) for d in data]
    pitch_enc_raw = [_to_float(d.get('pitch_enc')) for d in data]
    enc_deg_raw   = [_to_float(d.get('enc_deg'))   for d in data]
    has_pitch = any(v is not None for v in pitch_pos_raw + pitch_neg_raw + pitch_enc_raw)

    BG    = '#0d1117'
    SURF  = '#161b22'
    BORDER= '#30363d'
    TEXT  = '#e6edf3'
    MUTED = '#8b949e'

    fig, axes = plt.subplots(6, 1, figsize=(14, 16), sharex=True,
                             gridspec_kw={'hspace': 0.10})
    fig.patch.set_facecolor(BG)

    def _style(ax):
        ax.set_facecolor(SURF)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.yaxis.label.set_color(TEXT)
        ax.yaxis.label.set_size(10)
        ax.grid(color=BORDER, linewidth=0.5, linestyle='--', alpha=0.7)

    # ── RPM ──
    ax = axes[0]
    _style(ax)
    ax.plot(times, rpm_hall, color='#bc8cff', linewidth=1.5, label='Hall RPM')
    ax.plot(times, rpm_esc,  color='#f0883e', linewidth=1.5, label='ESC RPM')
    ax.plot(times, [t * (max(rpm_hall + rpm_esc) or 1) / 1047 for t in throttle],
            color='#58a6ff', linewidth=1, linestyle=':', alpha=0.6, label='Throttle (scaled)')
    ax.set_ylabel('RPM')
    ax.legend(loc='upper left', fontsize=8, facecolor=SURF, labelcolor=TEXT,
              edgecolor=BORDER, framealpha=0.8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{int(x):,}'))

    # ── Thrust ──
    ax = axes[1]
    _style(ax)
    ax.plot(times, thrust, color='#3fb950', linewidth=1.5)
    ax.set_ylabel('Thrust (g)')
    ax.axhline(0, color=MUTED, linewidth=0.5, linestyle='--')

    # ── Voltage & Current ──
    ax = axes[2]
    _style(ax)
    ax.plot(times, voltage, color='#e3b341', linewidth=1.5, label='Voltage (V)')
    ax.set_ylabel('Voltage (V)', color='#e3b341')
    ax.tick_params(axis='y', colors='#e3b341')
    ax2 = ax.twinx()
    ax2.set_facecolor(SURF)
    ax2.plot(times, current, color='#39d4cc', linewidth=1.5, label='Current (A)')
    ax2.set_ylabel('Current (A)', color='#39d4cc')
    ax2.tick_params(axis='y', colors='#39d4cc')
    ax2.spines['right'].set_color(BORDER)
    lines1, lbl1 = ax.get_legend_handles_labels()
    lines2, lbl2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, lbl1 + lbl2, loc='upper left', fontsize=8,
              facecolor=SURF, labelcolor=TEXT, edgecolor=BORDER, framealpha=0.8)

    # ── Temperature ──
    ax = axes[3]
    _style(ax)
    ax.plot(times, temp, color='#f85149', linewidth=1.5)
    ax.set_ylabel('Temp (°C)')

    # ── Hall raw voltage (pos + neg peaks) ──
    ax = axes[4]
    _style(ax)
    ax.plot(times, pos_v, color='#79c0ff', linewidth=1.2, label='V peak (+)')
    ax.plot(times, neg_v, color='#ff7b72', linewidth=1.2, label='V peak (−)')
    ax.set_ylabel('Hall V (V)')
    ax.legend(loc='upper left', fontsize=8, facecolor=SURF, labelcolor=TEXT,
              edgecolor=BORDER, framealpha=0.8)

    # ── Pitch angle (pos + neg peaks) ──
    ax = axes[5]
    _style(ax)
    if has_pitch:
        t_pp = [t for t, v in zip(times, pitch_pos_raw) if v is not None]
        v_pp = [v for v in pitch_pos_raw if v is not None]
        t_pn = [t for t, v in zip(times, pitch_neg_raw) if v is not None]
        v_pn = [v for v in pitch_neg_raw if v is not None]
        t_pe = [t for t, v in zip(times, pitch_enc_raw) if v is not None]
        v_pe = [v for v in pitch_enc_raw if v is not None]
        if t_pp:
            ax.plot(t_pp, v_pp, color='#79c0ff', linewidth=1.2, label='Pitch (+)')
        if t_pn:
            ax.plot(t_pn, v_pn, color='#ff7b72', linewidth=1.2, label='Pitch (−)')
        if t_pe:
            ax.plot(t_pe, v_pe, color='#56d364', linewidth=1.5, label='Pitch (enc)')
        ax.axhline(0, color=MUTED, linewidth=0.5, linestyle='--')
        ax.legend(loc='upper left', fontsize=8, facecolor=SURF, labelcolor=TEXT,
                  edgecolor=BORDER, framealpha=0.8)
    else:
        ax.text(0.5, 0.5, 'No pitch calibration', transform=ax.transAxes,
                ha='center', va='center', color=MUTED, fontsize=10)
    ax.set_ylabel('Pitch (°)')
    ax.set_xlabel('Time (s)', color=TEXT, fontsize=10)
    ax.tick_params(axis='x', colors=MUTED)

    fig.suptitle(f'Teststand Recording  —  {len(data)} samples',
                 color=TEXT, fontsize=12, y=0.995)
    plt.savefig(path, dpi=130, bbox_inches='tight', facecolor=BG)
    plt.close(fig)


# -----------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/motor/rpm', methods=['POST'])
def set_rpm_target():
    global _rpm_enabled, _rpm_target, _rpm_current_thr
    data   = request.get_json(force=True)
    target = max(0, int(data.get('rpm', 0)))
    enable = bool(data.get('enable', target > 0))

    with _rpm_ctrl_lock:
        _rpm_target  = target
        _rpm_enabled = enable and target > 0
        if not _rpm_enabled:
            _rpm_current_thr = 0.0
        elif _rpm_current_thr == 0.0:
            _rpm_current_thr = float(_RPM_MIN_THR)

    if not _rpm_enabled:
        serial_manager.set_motor_speed(0)

    return jsonify({'ok': True, 'rpm_target': target, 'enabled': _rpm_enabled})


@app.route('/api/motor/speed', methods=['POST'])
def set_speed():
    data = request.get_json(force=True)
    speed = max(0, min(1047, int(data.get('speed', 0))))
    serial_manager.set_motor_speed(speed)
    return jsonify({'ok': True, 'speed': speed})


def _do_slowdown():
    global _slowdown_active, _rpm_enabled, _rpm_current_thr
    # Stop RPM controller so it doesn't fight the ramp-down
    with _rpm_ctrl_lock:
        _rpm_enabled     = False
        _rpm_current_thr = 0.0

    st  = serial_manager.get_status()
    thr = float(st['esc'].get('throttle_raw', _RPM_MIN_THR))
    thr = max(float(_RPM_MIN_THR), thr)

    _sse_push({'type': 'slowdown', 'state': 'running'})

    while True:
        with _slowdown_lock:
            if not _slowdown_active:
                _sse_push({'type': 'slowdown', 'state': 'cancelled'})
                return

        st          = serial_manager.get_status()
        current_rpm = st['esc'].get('rpm') or st['hall'].get('rpm', 0) or 0

        if 0 < current_rpm < _SLOWDOWN_TARGET:
            serial_manager.set_motor_speed(0)
            with _slowdown_lock:
                _slowdown_active = False
            _sse_push({'type': 'slowdown', 'state': 'done', 'rpm': current_rpm})
            return

        thr = max(float(_RPM_MIN_THR), thr - _SLOWDOWN_STEP)
        serial_manager.set_motor_speed(round(thr))
        _sse_push({'type': 'slowdown', 'state': 'running',
                   'throttle': round(thr), 'rpm': current_rpm})

        if thr <= _RPM_MIN_THR:
            # Reached floor — give motor 2 s to coast below target then disarm
            time.sleep(2.0)
            serial_manager.set_motor_speed(0)
            with _slowdown_lock:
                _slowdown_active = False
            _sse_push({'type': 'slowdown', 'state': 'done', 'rpm': 0})
            return

        time.sleep(_SLOWDOWN_INTERVAL)


def _do_trajectory():
    global _traj_active, _rpm_enabled, _rpm_target, _rpm_current_thr
    with _traj_lock:
        waypoints = list(_traj_data)

    if not waypoints:
        with _traj_lock:
            _traj_active = False
        return

    with _rpm_ctrl_lock:
        _rpm_enabled     = True
        if _rpm_current_thr == 0.0:
            _rpm_current_thr = float(_RPM_MIN_THR)

    total_s  = sum(w['duration_s'] for w in waypoints)
    elapsed  = 0.0
    prev_rpm = 0

    _sse_push({'type': 'trajectory', 'state': 'running', 'step': 0,
               'total': len(waypoints), 'elapsed_s': 0.0,
               'total_s': total_s, 'target_rpm': 0})

    for i, wp in enumerate(waypoints):
        with _traj_lock:
            if not _traj_active:
                _sse_push({'type': 'trajectory', 'state': 'stopped'})
                return

        target_rpm = wp['rpm']
        dur        = max(0.2, wp['duration_s'])
        steps      = max(1, round(dur / 0.2))
        step_dur   = dur / steps

        for s in range(steps):
            with _traj_lock:
                if not _traj_active:
                    _sse_push({'type': 'trajectory', 'state': 'stopped'})
                    return

            t_frac   = (s + 1) / steps
            interp   = prev_rpm + (target_rpm - prev_rpm) * t_frac
            elapsed += step_dur

            with _rpm_ctrl_lock:
                _rpm_target  = round(interp)
                _rpm_enabled = True

            _sse_push({
                'type':       'trajectory',
                'state':      'running',
                'step':       i + 1,
                'total':      len(waypoints),
                'elapsed_s':  round(elapsed, 1),
                'total_s':    total_s,
                'target_rpm': round(interp),
            })
            time.sleep(step_dur)

        prev_rpm = target_rpm

    with _traj_lock:
        _traj_active = False
    _sse_push({'type': 'trajectory', 'state': 'done', 'total': len(waypoints)})


@app.route('/api/trajectory/run', methods=['POST'])
def trajectory_run():
    global _traj_active, _traj_data
    data      = request.get_json(force=True)
    waypoints = data.get('waypoints', [])
    if not waypoints:
        return jsonify({'ok': False, 'error': 'no waypoints'}), 400
    for wp in waypoints:
        if 'rpm' not in wp or 'duration_s' not in wp:
            return jsonify({'ok': False, 'error': 'each waypoint needs rpm and duration_s'}), 400
    with _traj_lock:
        if _traj_active:
            return jsonify({'ok': False, 'error': 'trajectory already running'}), 400
        _traj_data   = [{'rpm': int(w['rpm']), 'duration_s': float(w['duration_s'])}
                        for w in waypoints]
        _traj_active = True
    threading.Thread(target=_do_trajectory, daemon=True).start()
    total_s = sum(w['duration_s'] for w in _traj_data)
    return jsonify({'ok': True, 'steps': len(_traj_data), 'total_s': total_s})


@app.route('/api/trajectory/stop', methods=['POST'])
def trajectory_stop():
    global _traj_active
    with _traj_lock:
        _traj_active = False
    return jsonify({'ok': True})


@app.route('/api/motor/slowdown', methods=['POST'])
def motor_slowdown():
    global _slowdown_active
    with _slowdown_lock:
        if _slowdown_active:
            _slowdown_active = False          # signal thread to stop
            return jsonify({'ok': True, 'state': 'cancelled'})
        if not serial_manager.get_status()['esc'].get('armed', False):
            return jsonify({'ok': False, 'error': 'motor not armed'})
        _slowdown_active = True

    threading.Thread(target=_do_slowdown, daemon=True).start()
    return jsonify({'ok': True, 'state': 'started'})


@app.route('/api/status')
def status():
    return jsonify(serial_manager.get_status())


@app.route('/api/logs/<source>')
def logs(source: str):
    if source not in ('hall', 'loadcell', 'esc', 'encoder'):
        return jsonify({'error': 'unknown source'}), 400
    return jsonify({'lines': serial_manager.get_log_history(source)})


@app.route('/api/command', methods=['POST'])
def send_command():
    data   = request.get_json(force=True)
    source = data.get('source', '')
    cmd    = data.get('command', '').strip()
    if source not in ('hall', 'loadcell', 'esc', 'encoder'):
        return jsonify({'error': 'unknown source'}), 400
    ok = serial_manager.send_command(source, cmd)
    return jsonify({'ok': ok})


@app.route('/api/reconnect', methods=['POST'])
def reconnect():
    results = serial_manager.reconnect()
    return jsonify({'ok': True, 'results': results})


@app.route('/api/sync', methods=['POST'])
def sync():
    serial_manager.sync_all()
    return jsonify({'ok': True})


@app.route('/api/logging/rate', methods=['POST'])
def set_logging_rate():
    data = request.get_json(force=True)
    rate = float(data.get('rate', 10))
    rate = max(0.1, min(100.0, rate))
    global _rec_rate
    with _rec_lock:
        _rec_rate = rate
    serial_manager.set_log_rate(rate)
    return jsonify({'ok': True, 'rate': rate})


@app.route('/api/pitch/filter', methods=['POST'])
def set_pitch_filter():
    global _pitch_filter_window, _pitch_buf_pos, _pitch_buf_neg
    data = request.get_json(force=True)
    w = max(1, min(51, int(data.get('window', 5))))
    with _pitch_filter_lock:
        _pitch_filter_window = w
        _pitch_buf_pos = deque(maxlen=w)
        _pitch_buf_neg = deque(maxlen=w)
    return jsonify({'ok': True, 'window': w})


@app.route('/api/recording/start', methods=['POST'])
def recording_start():
    global _recording, _rec_data, _rec_start
    with _rec_lock:
        _recording = True
        _rec_data  = []
        _rec_start = time.time()
    _sse_push({'type': 'recording', 'state': 'started', 'cal_mode': True})
    return jsonify({'ok': True})


@app.route('/api/recording/stop', methods=['POST'])
def recording_stop():
    global _recording
    with _rec_lock:
        _recording = False
        data = list(_rec_data)

    if not data:
        _sse_push({'type': 'recording', 'state': 'stopped', 'points': 0})
        return jsonify({'ok': False, 'error': 'no data recorded'})

    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_name = f'recording_{ts}.csv'
    png_name = f'plot_{ts}.png'
    csv_path = os.path.join(RECORDINGS_DIR, csv_name)
    png_path = os.path.join(os.path.dirname(__file__), 'static', png_name)

    # Save CSV
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
        writer.writeheader()
        writer.writerows(data)

    # Generate plot in background so response is fast
    def _make_plot():
        try:
            _generate_plot(data, png_path)
            _sse_push({
                'type':     'plot_ready',
                'plot':     f'/static/{png_name}',
                'csv':      f'/recordings/{csv_name}',
                'stem':     ts,
                'points':   len(data),
                'duration': round(data[-1]['time_s'], 1) if data else 0,
            })
        except Exception as e:
            print(f'[plot] error: {e}')

    threading.Thread(target=_make_plot, daemon=True).start()

    _sse_push({'type': 'recording', 'state': 'stopped', 'points': len(data)})
    return jsonify({'ok': True, 'csv': csv_name, 'points': len(data)})


@app.route('/recordings/<filename>')
def serve_recording(filename: str):
    return send_from_directory(RECORDINGS_DIR, filename)


@app.route('/api/recording/rename', methods=['POST'])
def recording_rename():
    data     = request.get_json(force=True)
    old_stem = data.get('old_stem', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_stem or not new_name:
        return jsonify({'ok': False, 'error': 'missing parameters'}), 400
    new_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in new_name).strip('_')
    if not new_name:
        return jsonify({'ok': False, 'error': 'invalid name'}), 400

    old_csv = os.path.join(RECORDINGS_DIR, f'recording_{old_stem}.csv')
    old_png = os.path.join(os.path.dirname(__file__), 'static', f'plot_{old_stem}.png')
    new_csv = os.path.join(RECORDINGS_DIR, f'{new_name}.csv')
    new_png = os.path.join(os.path.dirname(__file__), 'static', f'{new_name}.png')

    if old_csv == new_csv:
        return jsonify({'ok': True, 'csv': f'/recordings/{new_name}.csv',
                        'plot': f'/static/{new_name}.png'})

    errors = []
    if os.path.exists(old_csv):
        os.rename(old_csv, new_csv)
    else:
        errors.append('csv not found')
    if os.path.exists(old_png):
        os.rename(old_png, new_png)
    else:
        errors.append('plot not found')

    return jsonify({'ok': not errors, 'errors': errors,
                    'csv':  f'/recordings/{new_name}.csv',
                    'plot': f'/static/{new_name}.png'})


# -----------------------------------------------------------------------
# Pitch calibration
# -----------------------------------------------------------------------
@app.route('/api/calibration')
def get_calibration():
    with _cal_srv_lock:
        return jsonify({'points': dict(_calibration)})


@app.route('/api/calibration/point', methods=['POST'])
def add_cal_point():
    data  = request.get_json(force=True)
    pitch = float(data.get('pitch_deg', 0))

    serial_manager.start_cal_collection()
    time.sleep(10.0)   # collect 10 s of peaks at steady-state, same as scope.py
    pos_samples, neg_samples = serial_manager.stop_cal_collection()

    missing = []
    if not pos_samples: missing.append('positive peaks')
    if not neg_samples: missing.append('negative peaks')
    if missing:
        return jsonify({'ok': False,
                        'error': f'no samples for {", ".join(missing)} — check hall sensor'})

    key     = f'{round(pitch, 2):.2f}'
    avg_pos = round(float(np.mean(medfilt(np.array(pos_samples),
                                          min(15, len(pos_samples) | 1)))), 4)
    avg_neg = round(float(np.mean(medfilt(np.array(neg_samples),
                                          min(15, len(neg_samples) | 1)))), 4)
    with _cal_srv_lock:
        _calibration['positive'][key] = avg_pos
        _calibration['negative'][key] = avg_neg
        _save_calibration(_calibration)

    _sse_push({'type': 'calibration_update', 'points': dict(_calibration)})
    return jsonify({'ok': True, 'pitch_deg': pitch,
                    'avg_pos_voltage': avg_pos, 'n_pos': len(pos_samples),
                    'avg_neg_voltage': avg_neg, 'n_neg': len(neg_samples)})


@app.route('/api/calibration/point', methods=['DELETE'])
def del_cal_point():
    data = request.get_json(force=True)
    key  = f'{round(float(data.get("pitch_deg", 0)), 2):.2f}'
    with _cal_srv_lock:
        removed = bool(_calibration['positive'].pop(key, None) is not None
                       or _calibration['negative'].pop(key, None) is not None)
        if removed:
            _save_calibration(_calibration)
    _sse_push({'type': 'calibration_update', 'points': dict(_calibration)})
    return jsonify({'ok': removed})


@app.route('/api/calibration/clear', methods=['POST'])
def clear_calibration():
    with _cal_srv_lock:
        _calibration['positive'].clear()
        _calibration['negative'].clear()
        _save_calibration(_calibration)
    _sse_push({'type': 'calibration_update', 'points': {'positive': {}, 'negative': {}}})
    return jsonify({'ok': True})


# -----------------------------------------------------------------------
# Background threads
# -----------------------------------------------------------------------
def _rpm_controller():
    """Closed-loop RPM controller: nudges throttle toward target RPM."""
    global _rpm_current_thr, _rpm_enabled
    prev_error = 0
    smooth_rpm = 0.0
    while True:
        with _rpm_ctrl_lock:
            enabled = _rpm_enabled
            target  = _rpm_target
            thr     = _rpm_current_thr

        if enabled and target > 0:
            st         = serial_manager.get_status()
            raw_rpm    = st['esc'].get('rpm') or st['hall'].get('rpm', 0) or 0
            smooth_rpm = _RPM_EMA * raw_rpm + (1 - _RPM_EMA) * smooth_rpm
            error      = target - smooth_rpm
            d_error    = error - prev_error   # negative when converging

            if abs(error) > _RPM_DEADBAND:
                # Fine steps near target, coarser steps when far away
                if abs(error) < _RPM_NEAR_ZONE:
                    step = _RPM_STEP_NEAR
                else:
                    raw_step = abs(error) / 100.0
                    step     = max(_RPM_STEP_MIN, min(_RPM_STEP_MAX, raw_step))

                # Derivative damping: reduce step when already converging
                converging = (error > 0 and d_error < 0) or (error < 0 and d_error > 0)
                if converging:
                    step = max(0.0, step - _RPM_KD * abs(d_error) / max(abs(error), 1))

                if step > 0:
                    if error > 0:
                        thr = min(_RPM_MAX_THR, thr + step)
                    else:
                        thr = max(_RPM_MIN_THR, thr - step)

                    # Safety: near ceiling with no RPM response → abort
                    if thr >= _RPM_MAX_THR * 0.98 and raw_rpm < 200:
                        with _rpm_ctrl_lock:
                            _rpm_enabled     = False
                            _rpm_current_thr = 0.0
                        serial_manager.set_motor_speed(0)
                        _sse_push({'type': 'rpm_ctrl_error',
                                   'error': 'Motor not responding — controller disabled'})
                        prev_error = 0
                        smooth_rpm = 0.0
                        time.sleep(_RPM_INTERVAL)
                        continue

                    with _rpm_ctrl_lock:
                        _rpm_current_thr = thr
                    serial_manager.set_motor_speed(round(thr))

            prev_error = error
        else:
            prev_error = 0
            smooth_rpm = 0.0

        time.sleep(_RPM_INTERVAL)


def _streamer():
    """Push serial lines + periodic status to SSE clients."""
    last_status = 0.0
    while True:
        for source, line in serial_manager.get_pending_lines():
            _sse_push({'type': 'log', 'source': source, 'line': line})

        now = time.time()
        if now - last_status >= 1.0:
            st = serial_manager.get_status()
            with _rpm_ctrl_lock:
                rpm_ctrl = {
                    'enabled':  _rpm_enabled,
                    'target':   _rpm_target,
                    'throttle': round(_rpm_current_thr, 1),
                }
            if _rpm_enabled and _rpm_target > 0:
                current_rpm = st['esc'].get('rpm') or st['hall'].get('rpm', 0) or 0
                rpm_ctrl['locked']      = abs(_rpm_target - current_rpm) <= _RPM_LOCKED_TH
                rpm_ctrl['current_rpm'] = current_rpm
            _sse_push({'type': 'status', **st, 'rpm_ctrl': rpm_ctrl})
            last_status = now

        time.sleep(0.04)


def _recorder():
    """Poll sensor state, maintain pitch filter, and store rows while recording."""
    global _pitch_live_pos, _pitch_live_neg
    while True:
        with _rec_lock:
            active = _recording
            rate   = _rec_rate
            start  = _rec_start

        st    = serial_manager.get_status()
        pos_v = st['hall'].get('pos_voltage', 0.0)
        neg_v = st['hall'].get('neg_voltage', 0.0)
        enc   = st.get('encoder', {})
        with _cal_srv_lock:
            cal = dict(_calibration)

        # Update median filter (always, so live display stays current)
        raw_pos = _interpolate_pitch(pos_v, 'positive', cal)
        raw_neg = _interpolate_pitch(neg_v, 'negative', cal)
        with _pitch_filter_lock:
            if raw_pos is not None:
                _pitch_buf_pos.append(raw_pos)
                _pitch_live_pos = sorted(_pitch_buf_pos)[len(_pitch_buf_pos) // 2]
            if raw_neg is not None:
                _pitch_buf_neg.append(raw_neg)
                _pitch_live_neg = sorted(_pitch_buf_neg)[len(_pitch_buf_neg) // 2]
            pitch_pos_f = _pitch_live_pos
            pitch_neg_f = _pitch_live_neg

        if active:
            _rec_data.append({
                'time_s':      round(time.time() - start, 4),
                'throttle':    st['esc'].get('throttle_raw', 0),
                'rpm_hall':    st['hall'].get('rpm', 0),
                'weight_g':    st['loadcell'].get('weight', 0.0),
                'esc_rpm':     st['esc'].get('rpm', 0),
                'voltage_v':   st['esc'].get('voltage', 0.0),
                'current_a':   st['esc'].get('current', 0.0),
                'temp_c':      st['esc'].get('temp', 0),
                'mah':         st['esc'].get('mah', 0),
                'pos_voltage': round(pos_v, 4),
                'neg_voltage': round(neg_v, 4),
                'pitch_pos':   round(pitch_pos_f, 2) if pitch_pos_f is not None else '',
                'pitch_neg':   round(pitch_neg_f, 2) if pitch_neg_f is not None else '',
                'enc_deg':     round(enc.get('enc_deg', 0.0), 3),
                'pitch_enc':   round(enc['pitch_deg'], 3) if enc.get('pitch_deg') is not None else '',
            })

        time.sleep(1.0 / rate if active else 0.1)


# -----------------------------------------------------------------------
if __name__ == '__main__':
    serial_manager.start()
    # Apply default 10 Hz logging rate on startup
    serial_manager.set_log_rate(10.0)

    threading.Thread(target=_streamer,      daemon=True).start()
    threading.Thread(target=_recorder,      daemon=True).start()
    threading.Thread(target=_rpm_controller, daemon=True).start()

    print('=' * 52)
    print('Propeller Teststand server on http://0.0.0.0:5000')
    print('=' * 52)
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
