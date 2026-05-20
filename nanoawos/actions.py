"""Action dispatcher for NanoAWOS.

Handles both click actions and transcription reactions using the same
action types: weather_full, weather_wind, tts, mqtt, none.

Click actions: triggered by PTT click count (4-12)
Transcription reactions: triggered by keyword/pattern match in transcribed text

Data sources available in Jinja2 templates:
  - weather: Current weather data from /tmp/metar* files
  - station: Station config (icao, name, elevation, runways)
  - time: Current UTC time fields
  - transcript: The transcribed text and extracted action (transcription reactions only)
  - Custom API integrations via data_sources config
"""

import json
import logging
import os
import re
import time as _time
from datetime import datetime, timezone

import requests

from nanoawos.config import load_config

log = logging.getLogger(__name__)

_data_cache = {}
_cache_ts = {}
CACHE_TTL = 60


def _get_weather_data():
    def _read(path, default=""):
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return default
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
    name = source_cfg.get("name", "unknown")
    url = source_cfg.get("url", "")
    if not url:
        return {}
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
        return data
    except Exception as e:
        log.warning("Failed to fetch API source '%s': %s", name, e)
        return _data_cache.get(name, {})


def get_template_context(cfg=None, extra=None):
    """Build the full Jinja2 template context."""
    if cfg is None:
        cfg = load_config()
    ctx = {
        "weather": _get_weather_data(),
        "station": cfg.get("station", {}),
        "time": _get_time_data(),
    }
    for source in cfg.get("data_sources", []):
        name = source.get("name", "")
        if name:
            ctx[name] = _fetch_api_source(source)
    if extra:
        ctx.update(extra)
    return ctx


def render_template(template_text, cfg=None, extra=None):
    """Render a Jinja2 template string with all data sources."""
    try:
        from jinja2 import Template
    except ImportError:
        ctx = get_template_context(cfg, extra)
        try:
            return template_text.format(**ctx)
        except Exception:
            return template_text
    ctx = get_template_context(cfg, extra)
    try:
        return Template(template_text).render(**ctx)
    except Exception as e:
        log.error("Template render error: %s", e)
        return template_text


def _publish_mqtt(topic, payload, cfg):
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


def _run_action(action, cfg, tag="action", extra_ctx=None):
    """Execute a single action dict. Shared by clicks and transcription reactions.

    Args:
        action: dict with type, label, text, topic, tts_engine, payload, etc.
        cfg: full config
        tag: label for logging (e.g. "4 clicks" or "reaction:landing")
        extra_ctx: extra Jinja2 context (e.g. transcript data)
    """
    action_type = action.get("type", "none")
    label = action.get("label", tag)
    log.info("Executing %s: %s (%s)", tag, label, action_type)

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
        text = render_template(template, cfg, extra_ctx)
        log.info("TTS [%s]: %s", tag, text)

        tts_engine = action.get("tts_engine", "")
        from nanoawos.tts import synthesize
        output_dir = cfg["tts"]["output_dir"]
        # Use tag hash for unique filename
        safe_tag = re.sub(r'[^a-zA-Z0-9]', '_', tag)
        wav_path = f"{output_dir}/action_{safe_tag}.wav"

        if tts_engine:
            original = cfg["tts"].get("engine")
            cfg["tts"]["engine"] = tts_engine
            try:
                synthesize(text, wav_path, cfg)
            finally:
                cfg["tts"]["engine"] = original
        else:
            synthesize(text, wav_path, cfg)

        from nanoawos.audio import play_wav
        play_wav(wav_path, cfg)

    elif action_type == "mqtt":
        topic = action.get("topic", f"nanoawos/{tag}")
        payload = action.get("payload", "")
        if not payload:
            payload = json.dumps({
                "tag": tag,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **(extra_ctx or {}),
            })
        if "{{" in payload or "{%" in payload:
            payload = render_template(payload, cfg, extra_ctx)
        _publish_mqtt(topic, payload, cfg)

    elif action_type == "none":
        pass
    else:
        log.warning("Unknown action type: %s", action_type)


def execute_action(click_count, cfg=None):
    """Execute the action configured for the given click count."""
    if cfg is None:
        cfg = load_config()
    actions = cfg.get("click_actions", {})
    action = actions.get(str(click_count)) or actions.get(click_count)
    if not action:
        log.info("No action configured for %d clicks", click_count)
        return
    _run_action(action, cfg, tag=f"{click_count}_clicks")


def execute_transcription_reactions(text, gpt_action, cfg=None):
    """Check transcription text against reaction rules and execute matches.

    Args:
        text: raw transcription text from Whisper
        gpt_action: extracted action string from GPT (or None)
        cfg: full config

    Each reaction rule has:
        match: keyword or regex to match against text (case-insensitive)
        match_field: "text" (default) or "action" (match against GPT action)
        type/label/text/topic/tts_engine: same as click actions
    """
    if cfg is None:
        cfg = load_config()

    reactions = cfg.get("transcription_reactions", [])
    if not reactions:
        return

    extra_ctx = {
        "transcript": {
            "text": text,
            "action": gpt_action or "",
        },
    }

    for rule in reactions:
        match_pattern = rule.get("match", "")
        if not match_pattern:
            continue

        match_field = rule.get("match_field", "text")
        target = text if match_field == "text" else (gpt_action or "")

        try:
            if re.search(match_pattern, target, re.IGNORECASE):
                rule_label = rule.get("label", match_pattern)
                log.info("Transcription reaction matched: '%s' on '%s'",
                         rule_label, match_pattern)
                _run_action(rule, cfg, tag=f"reaction:{rule_label}", extra_ctx=extra_ctx)
        except re.error as e:
            log.warning("Invalid regex in reaction rule '%s': %s", match_pattern, e)
