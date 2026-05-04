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
from datetime import datetime

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


def _generate_plot(data: list[dict], path: str):
    times    = [d['time_s']       for d in data]
    rpm_hall = [d['rpm_hall']     for d in data]
    rpm_esc  = [d['esc_rpm']      for d in data]
    thrust   = [d['weight_g']     for d in data]
    voltage  = [d['voltage_v']    for d in data]
    current  = [d['current_a']    for d in data]
    temp     = [d['temp_c']       for d in data]
    throttle = [d['throttle_pct'] for d in data]
    pos_v    = [d.get('pos_voltage', 0.0) for d in data]
    neg_v    = [d.get('neg_voltage', 0.0) for d in data]

    def _to_float(v):
        try:    return float(v)
        except: return None

    pitch_pos_raw = [_to_float(d.get('pitch_pos')) for d in data]
    pitch_neg_raw = [_to_float(d.get('pitch_neg')) for d in data]
    has_pitch = any(v is not None for v in pitch_pos_raw + pitch_neg_raw)

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
    ax.plot(times, [t * (max(rpm_hall + rpm_esc) or 1) / 100 for t in throttle],
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
        if t_pp:
            ax.plot(t_pp, v_pp, color='#79c0ff', linewidth=1.2, label='Pitch (+)')
        if t_pn:
            ax.plot(t_pn, v_pn, color='#ff7b72', linewidth=1.2, label='Pitch (−)')
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


@app.route('/api/motor/speed', methods=['POST'])
def set_speed():
    data = request.get_json(force=True)
    speed = max(0, min(100, int(data.get('speed', 0))))
    serial_manager.set_motor_speed(speed)
    return jsonify({'ok': True, 'speed': speed})


@app.route('/api/status')
def status():
    return jsonify(serial_manager.get_status())


@app.route('/api/logs/<source>')
def logs(source: str):
    if source not in ('hall', 'loadcell', 'esc'):
        return jsonify({'error': 'unknown source'}), 400
    return jsonify({'lines': serial_manager.get_log_history(source)})


@app.route('/api/command', methods=['POST'])
def send_command():
    data   = request.get_json(force=True)
    source = data.get('source', '')
    cmd    = data.get('command', '').strip()
    if source not in ('hall', 'loadcell', 'esc'):
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
                'type':   'plot_ready',
                'plot':   f'/static/{png_name}',
                'csv':    f'/recordings/{csv_name}',
                'points': len(data),
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

    THR_LOW       = 5      # % throttle at sweep start/end
    THR_HIGH      = 25     # % throttle at sweep peak
    RAMP_DURATION = 10.0   # seconds per ramp leg
    STEP_INTERVAL = 0.2    # seconds between throttle steps
    steps = int(RAMP_DURATION / STEP_INTERVAL)

    serial_manager.start_cal_collection()

    # Ramp up: THR_LOW → THR_HIGH over RAMP_DURATION seconds
    for i in range(steps + 1):
        thr = round(THR_LOW + (THR_HIGH - THR_LOW) * i / steps)
        serial_manager.set_motor_speed(thr)
        time.sleep(STEP_INTERVAL)

    # Ramp down: THR_HIGH → THR_LOW over RAMP_DURATION seconds
    for i in range(steps + 1):
        thr = round(THR_HIGH - (THR_HIGH - THR_LOW) * i / steps)
        serial_manager.set_motor_speed(thr)
        time.sleep(STEP_INTERVAL)

    # Stop motor
    serial_manager.set_motor_speed(0)

    pos_samples, neg_samples = serial_manager.stop_cal_collection()

    missing = []
    if not pos_samples: missing.append('positive peaks')
    if not neg_samples: missing.append('negative peaks')
    if missing:
        return jsonify({'ok': False,
                        'error': f'no samples for {", ".join(missing)} — check hall sensor'})

    key     = f'{round(pitch, 2):.2f}'
    avg_pos = round(sum(pos_samples) / len(pos_samples), 4)
    avg_neg = round(sum(neg_samples) / len(neg_samples), 4)
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
def _streamer():
    """Push serial lines + periodic status to SSE clients."""
    last_status = 0.0
    while True:
        for source, line in serial_manager.get_pending_lines():
            _sse_push({'type': 'log', 'source': source, 'line': line})

        now = time.time()
        if now - last_status >= 1.0:
            _sse_push({'type': 'status', **serial_manager.get_status()})
            last_status = now

        time.sleep(0.04)


def _recorder():
    """Poll sensor state and store rows while recording is active."""
    while True:
        with _rec_lock:
            active = _recording
            rate   = _rec_rate
            start  = _rec_start

        if active:
            st  = serial_manager.get_status()
            pos_v = st['hall'].get('pos_voltage', 0.0)
            neg_v = st['hall'].get('neg_voltage', 0.0)
            with _cal_srv_lock:
                cal = dict(_calibration)
            pitch_pos = _interpolate_pitch(pos_v, 'positive', cal)
            pitch_neg = _interpolate_pitch(neg_v, 'negative', cal)
            _rec_data.append({
                'time_s':       round(time.time() - start, 4),
                'throttle_pct': st['esc'].get('throttle_pct', 0),
                'rpm_hall':     st['hall'].get('rpm', 0),
                'weight_g':     st['loadcell'].get('weight', 0.0),
                'esc_rpm':      st['esc'].get('rpm', 0),
                'voltage_v':    st['esc'].get('voltage', 0.0),
                'current_a':    st['esc'].get('current', 0.0),
                'temp_c':       st['esc'].get('temp', 0),
                'mah':          st['esc'].get('mah', 0),
                'pos_voltage':  round(pos_v, 4),
                'neg_voltage':  round(neg_v, 4),
                'pitch_pos':    round(pitch_pos, 2) if pitch_pos is not None else '',
                'pitch_neg':    round(pitch_neg, 2) if pitch_neg is not None else '',
            })
            time.sleep(1.0 / rate)
        else:
            time.sleep(0.1)


# -----------------------------------------------------------------------
if __name__ == '__main__':
    serial_manager.start()
    # Apply default 10 Hz logging rate on startup
    serial_manager.set_log_rate(10.0)

    threading.Thread(target=_streamer,  daemon=True).start()
    threading.Thread(target=_recorder,  daemon=True).start()

    print('=' * 52)
    print('Propeller Teststand server on http://0.0.0.0:5000')
    print('=' * 52)
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
