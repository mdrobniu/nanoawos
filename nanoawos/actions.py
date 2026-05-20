"""Click action dispatcher for NanoAWOS.

Maps click counts to configurable actions:
  - weather_full: Play full weather TTS
  - weather_wind: Play wind-only TTS
  - tts: Speak custom Jinja2 template text
  - mqtt: Publish MQTT event
  - none: Do nothing

Data sources available in Jinja2 templates:
  - weather: Current weather data (wind_dir, wind_speed, temp, qnh, etc.)
  - station: Station config (icao, name, elevation, runways)
  - time: Current UTC time fields
  - Custom API integrations via data_sources config
"""

import json
import logging
import os
import time as _time
from datetime import datetime, timezone

import requests

from nanoawos.config import load_config

log = logging.getLogger(__name__)

# Cache for data sources
_data_cache = {}
_cache_ts = {}
CACHE_TTL = 60  # seconds


def _get_weather_data():
    """Read current weather from /tmp/metar* files."""
    def _read(path, default=""):
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return default

    # Also try to read the full weather JSON if available
    metar = _read("/tmp/metar")
    parts = metar.split() if metar else []
    return {
        "metar": metar,
        "wind": _read("/tmp/metar2"),
        "temp_qnh": _read("/tmp/metar3"),
        "density_alt": _read("/tmp/metar4"),
        "icao": parts[0] if len(parts) > 0 else "",
        "time_zulu": parts[1] if len(parts) > 1 else "",
    }


def _get_time_data():
    """Current time in various formats."""
    now = datetime.now(timezone.utc)
    return {
        "utc": now.strftime("%H:%M"),
        "utc_hour": now.strftime("%H"),
        "utc_min": now.strftime("%M"),
        "utc_hhmm": now.strftime("%H%M"),
        "date": now.strftime("%Y-%m-%d"),
        "zulu": now.strftime("%H%M") + "Z",
    }


def _fetch_api_source(source_cfg):
    """Fetch data from a custom API source with caching."""
    name = source_cfg.get("name", "unknown")
    url = source_cfg.get("url", "")
    if not url:
        return {}

    # Check cache
    now = _time.time()
    ttl = source_cfg.get("cache_ttl", CACHE_TTL)
    if name in _data_cache and (now - _cache_ts.get(name, 0)) < ttl:
        return _data_cache[name]

    try:
        headers = source_cfg.get("headers", {})
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _data_cache[name] = data
        _cache_ts[name] = now
        log.debug("Fetched API source '%s': %d bytes", name, len(resp.text))
        return data
    except Exception as e:
        log.warning("Failed to fetch API source '%s': %s", name, e)
        return _data_cache.get(name, {})


def get_template_context(cfg=None):
    """Build the full Jinja2 template context from all data sources."""
    if cfg is None:
        cfg = load_config()

    ctx = {
        "weather": _get_weather_data(),
        "station": cfg.get("station", {}),
        "time": _get_time_data(),
    }

    # Custom API sources
    for source in cfg.get("data_sources", []):
        name = source.get("name", "")
        if name:
            ctx[name] = _fetch_api_source(source)

    return ctx


def render_template(template_text, cfg=None):
    """Render a Jinja2 template string with all data sources."""
    try:
        from jinja2 import Template
    except ImportError:
        # Fallback: simple string format
        ctx = get_template_context(cfg)
        try:
            return template_text.format(**ctx)
        except Exception:
            return template_text

    ctx = get_template_context(cfg)
    try:
        tmpl = Template(template_text)
        return tmpl.render(**ctx)
    except Exception as e:
        log.error("Template render error: %s", e)
        return template_text


def _publish_mqtt(topic, payload, cfg):
    """Publish an MQTT message."""
    mqtt_cfg = cfg.get("mqtt", {})
    if not mqtt_cfg.get("enabled"):
        log.info("MQTT disabled, would publish: %s = %s", topic, payload)
        return

    try:
        import paho.mqtt.client as mqtt
        client = mqtt.Client()
        user = mqtt_cfg.get("username")
        if user:
            client.username_pw_set(user, mqtt_cfg.get("password", ""))
        client.connect(mqtt_cfg.get("broker", "localhost"),
                       mqtt_cfg.get("port", 1883), 60)
        client.publish(topic, payload)
        client.disconnect()
        log.info("MQTT published: %s = %s", topic, payload)
    except Exception as e:
        log.error("MQTT publish failed: %s", e)


def execute_action(click_count, cfg=None):
    """Execute the action configured for the given click count."""
    if cfg is None:
        cfg = load_config()

    actions = cfg.get("click_actions", {})
    action = actions.get(str(click_count)) or actions.get(click_count)

    if not action:
        log.info("No action configured for %d clicks", click_count)
        return

    action_type = action.get("type", "none")
    label = action.get("label", f"{click_count} clicks")

    log.info("Executing action for %d clicks: %s (%s)", click_count, label, action_type)

    if action_type == "weather_full":
        from nanoawos.audio import play_playlist
        play_playlist("full", cfg)

    elif action_type == "weather_wind":
        from nanoawos.audio import play_playlist
        play_playlist("wind", cfg)

    elif action_type == "tts":
        template = action.get("text", "")
        if not template:
            log.warning("TTS action has no text template")
            return
        text = render_template(template, cfg)
        log.info("TTS: %s", text)

        # Per-action TTS engine override (piper, cloud, wav_concat)
        tts_engine = action.get("tts_engine", "")

        from nanoawos.tts import synthesize
        output_dir = cfg["tts"]["output_dir"]
        wav_path = f"{output_dir}/custom_{click_count}.wav"

        if tts_engine:
            # Temporarily override the TTS engine for this action
            original_engine = cfg["tts"].get("engine")
            cfg["tts"]["engine"] = tts_engine
            try:
                synthesize(text, wav_path, cfg)
            finally:
                cfg["tts"]["engine"] = original_engine
        else:
            synthesize(text, wav_path, cfg)

        from nanoawos.audio import play_wav
        play_wav(wav_path, cfg)

    elif action_type == "mqtt":
        topic = action.get("topic", f"nanoawos/clicks/{click_count}")
        payload = action.get("payload", json.dumps({
            "clicks": click_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
        if "{{" in payload or "{%" in payload:
            payload = render_template(payload, cfg)
        _publish_mqtt(topic, payload, cfg)

    elif action_type == "none":
        pass

    else:
        log.warning("Unknown action type: %s", action_type)
