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
    cfg = load_config()
    host = cfg["audio"]["mpd_host"]
    subprocess.run(["mpc", "-h", host, "clear"])
    subprocess.run(["mpc", "-h", host, "load", name])
    subprocess.run(["mpc", "-h", host, "crossfade", "1"])
    subprocess.run(["mpc", "-h", host, "play"])
    return jsonify({"status": "playing", "playlist": name})


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
    # Remove internal keys
    return jsonify({k: v for k, v in cfg.items() if not k.startswith("_")})


@app.route("/api/config", methods=["PUT"])
def api_config_put():
    new_cfg = request.get_json()
    if not new_cfg:
        return jsonify({"error": "No JSON body"}), 400

    cfg = load_config()
    # Merge sections
    for section, values in new_cfg.items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict) and section in cfg:
            cfg[section].update(values)
        else:
            cfg[section] = values

    save_config(cfg)
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
