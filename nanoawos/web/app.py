"""NanoAWOS Web UI - Flask application."""

import glob
import json
import logging
import os
import subprocess
import sys

from flask import Flask, render_template, request, jsonify

# Add parent to path so we can import nanoawos
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from nanoawos.config import load_config, save_config

log = logging.getLogger(__name__)
app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "static"))


def _read_file(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def _service_status(name):
    """Check if a systemd service is active."""
    r = subprocess.run(["systemctl", "is-active", name],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _get_weather_data():
    """Read current weather from /tmp/metar* files."""
    return {
        "metar": _read_file("/tmp/metar", "NO DATA"),
        "wind": _read_file("/tmp/metar2", "---"),
        "temp_qnh": _read_file("/tmp/metar3", "---"),
        "density_alt": _read_file("/tmp/metar4", "---"),
        "tap_count": _read_file("/tmp/tap", "0"),
    }


def _list_tts_models():
    """List available Piper TTS models."""
    models = []
    model_dir = "/mnt/p4/models"
    if os.path.isdir(model_dir):
        for f in sorted(glob.glob(os.path.join(model_dir, "*.onnx"))):
            name = os.path.basename(f)
            if name.startswith("en_"):
                models.insert(0, f)
            else:
                models.append(f)
    return models


@app.route("/")
def index():
    cfg = load_config()
    weather = _get_weather_data()
    models = _list_tts_models()
    services = {
        "weather_timer": _service_status("nanoawos-weather.timer"),
        "tap": _service_status("nanoawos-tap"),
        "gpio": _service_status("nanoawos-gpio"),
        "mpd": _service_status("mpd"),
        "darkice": _service_status("darkice"),
        "icecast": _service_status("icecast2"),
    }
    uptime = _read_file("/proc/uptime", "0").split()[0]
    return render_template("index.html",
                           cfg=cfg, weather=weather, services=services,
                           models=models, uptime=float(uptime))


@app.route("/api/weather")
def api_weather():
    return jsonify(_get_weather_data())


@app.route("/api/play/<name>", methods=["POST"])
def api_play(name):
    if name not in ("full", "wind"):
        return jsonify({"error": "Invalid playlist"}), 400
    subprocess.run(["mpc", "clear"])
    subprocess.run(["mpc", "load", name])
    subprocess.run(["mpc", "crossfade", "1"])
    subprocess.run(["mpc", "play"])
    return jsonify({"status": "playing", "playlist": name})


@app.route("/api/tap/calibrate", methods=["POST"])
def api_tap_calibrate():
    """Start calibration mode. POST with {"clicks": N} to capture N sequences."""
    data = request.get_json() or {}
    n = data.get("clicks", 5)
    with open("/tmp/nanoawos_calibrate", "w") as f:
        f.write(str(n))
    return jsonify({"status": "calibrating", "sequences": n})


@app.route("/api/tap/profile")
def api_tap_profile():
    """Return the learned click profile."""
    try:
        with open("/tmp/nanoawos_click_profile.json") as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"click_energies": [], "click_durations": []})


@app.route("/api/tap/profile", methods=["DELETE"])
def api_tap_profile_clear():
    """Clear the learned profile to reset self-learning."""
    try:
        os.unlink("/tmp/nanoawos_click_profile.json")
    except FileNotFoundError:
        pass
    return jsonify({"status": "cleared"})


@app.route("/api/audio/upload", methods=["POST"])
def api_audio_upload():
    """Upload an audio file (wav/mp3/ogg) for use in click/transcription actions."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    # Sanitize filename
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', f.filename)
    upload_dir = "/mnt/p4/audio"
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, safe_name)
    f.save(path)
    log.info("Audio uploaded: %s (%d bytes)", path, os.path.getsize(path))
    return jsonify({"status": "uploaded", "path": path, "name": safe_name})


@app.route("/api/audio/list")
def api_audio_list():
    """List uploaded audio files."""
    upload_dir = "/mnt/p4/audio"
    files = []
    if os.path.isdir(upload_dir):
        for f in sorted(os.listdir(upload_dir)):
            fp = os.path.join(upload_dir, f)
            if os.path.isfile(fp):
                files.append({"name": f, "path": fp, "size": os.path.getsize(fp)})
    return jsonify(files)


@app.route("/api/audio/delete/<name>", methods=["DELETE"])
def api_audio_delete(name):
    """Delete an uploaded audio file."""
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    path = os.path.join("/mnt/p4/audio", safe_name)
    if os.path.exists(path):
        os.unlink(path)
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Not found"}), 404


@app.route("/api/logs")
def api_logs():
    """Return recent log lines from tap and weather services."""
    lines = []
    for svc in ["nanoawos-tap", "nanoawos-weather.service", "nanoawos-gpio"]:
        try:
            r = subprocess.run(
                ["journalctl", "-u", svc, "--since", "10 min ago",
                 "--no-pager", "-o", "short-iso", "--no-hostname"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().split("\n"):
                if line and "ALSA" not in line and "pcm" not in line and "conf.c" not in line:
                    lines.append(line)
        except Exception:
            pass
    # Sort by timestamp and return last 50
    lines.sort()
    return jsonify(lines[-50:])


@app.route("/api/transcriptions")
def api_transcriptions():
    """Return recent transcriptions."""
    log_file = "/tmp/nanoawos_transcriptions.json"
    try:
        with open(log_file) as f:
            entries = json.load(f)
        return jsonify(entries[-50:])  # Last 50
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify([])


@app.route("/api/tap")
def api_tap():
    """Live tap detector debug data."""
    raw = _read_file("/tmp/tap_debug", "0 0.5 0 0 0 quiet 0 0 0 0 0")
    parts = raw.split()
    try:
        return jsonify({
            "amplitude": float(parts[0]),
            "threshold": float(parts[1]),
            "clicks": int(parts[2]),
            "active": int(parts[3]),
            "profile_samples": int(parts[4]),
            "state": parts[5],
            "noise_floor": int(parts[6]) if len(parts) > 6 else 0,
            "t_high": int(parts[7]) if len(parts) > 7 else 0,
            "t_low": int(parts[8]) if len(parts) > 8 else 0,
            "energy": int(parts[9]) if len(parts) > 9 else 0,
            "auto_tuning": bool(int(parts[10])) if len(parts) > 10 else False,
            "calibrating": parts[11] == "CAL" if len(parts) > 11 else False,
            "last_tap": _read_file("/tmp/tap", "0"),
        })
    except (IndexError, ValueError):
        return jsonify({"amplitude": 0, "threshold": 0.5, "clicks": 0,
                        "active": 0, "profile_samples": 0,
                        "state": "error", "last_tap": "0"})


@app.route("/api/status")
def api_status():
    services = {
        "nanoawos-weather.timer": _service_status("nanoawos-weather.timer"),
        "nanoawos-tap": _service_status("nanoawos-tap"),
        "nanoawos-gpio": _service_status("nanoawos-gpio"),
        "mpd": _service_status("mpd"),
        "darkice": _service_status("darkice"),
        "icecast2": _service_status("icecast2"),
    }
    return jsonify({
        "services": services,
        "weather": _get_weather_data(),
        "uptime": _read_file("/proc/uptime", "0").split()[0],
    })


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = load_config()
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    # Normalize click_actions keys to strings for JSON
    if "click_actions" in out:
        out["click_actions"] = {str(k): v for k, v in out["click_actions"].items()}
    return jsonify(out)


@app.route("/api/config", methods=["PUT"])
def api_config_put():
    new_cfg = request.get_json()
    if not new_cfg:
        return jsonify({"error": "No JSON body"}), 400

    cfg = load_config()
    # Sections that should be fully replaced (not merged)
    replace_sections = {"click_actions", "transcription_reactions", "data_sources"}
    for section, values in new_cfg.items():
        if section.startswith("_"):
            continue
        if section == "click_actions" and isinstance(values, dict):
            # Normalize keys to int (JS sends "4", YAML needs 4)
            cfg[section] = {int(k): v for k, v in values.items()}
        elif section in replace_sections:
            cfg[section] = values
        elif isinstance(values, dict) and section in cfg and isinstance(cfg[section], dict):
            cfg[section].update(values)
        else:
            cfg[section] = values

    save_config(cfg)
    # Bust the config cache so services pick up changes
    global _config
    from nanoawos import config as _cfg_mod
    _cfg_mod._config = None
    return jsonify({"status": "saved"})


@app.route("/api/service/<name>/<action>", methods=["POST"])
def api_service_action(name, action):
    allowed_services = [
        "nanoawos-weather.timer", "nanoawos-tap", "nanoawos-gpio",
        "nanoawos-web", "darkice", "icecast2", "mpd",
    ]
    if name not in allowed_services:
        return jsonify({"error": "Service not allowed"}), 403
    if action not in ("restart", "stop", "start"):
        return jsonify({"error": "Invalid action"}), 400
    r = subprocess.run(["sudo", "systemctl", action, name],
                       capture_output=True, text=True)
    return jsonify({"status": r.returncode == 0, "output": r.stderr})


@app.route("/api/weather/refresh", methods=["POST"])
def api_weather_refresh():
    """Trigger an immediate weather update."""
    r = subprocess.run(
        ["sudo", "systemctl", "start", "nanoawos-weather.service"],
        capture_output=True, text=True
    )
    return jsonify({"status": r.returncode == 0})


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    web_cfg = cfg.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 8080)
    log.info("Starting NanoAWOS Web UI on %s:%d", host, port)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
