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


# NATO/ICAO phonetic alphabet
NATO_ALPHABET = {
    "a": "alfa", "b": "bravo", "c": "charlie", "d": "delta",
    "e": "echo", "f": "foxtrot", "g": "golf", "h": "hotel",
    "i": "india", "j": "juliett", "k": "kilo", "l": "lima",
    "m": "mike", "n": "november", "o": "oscar", "p": "papa",
    "q": "quebec", "r": "romeo", "s": "sierra", "t": "tango",
    "u": "uniform", "v": "victor", "w": "whiskey", "x": "x-ray",
    "y": "yankee", "z": "zulu",
}

# Aviation digit pronunciation
AVIATION_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner",
}


def _filter_nato(value):
    """Jinja2 filter: spell out as NATO phonetic alphabet.

    Usage: {{ "ABCD" | nato }} -> "alfa bravo charlie delta"
    """
    return " ".join(NATO_ALPHABET.get(c.lower(), c) for c in str(value) if c.strip())


def _filter_digits(value):
    """Jinja2 filter: read digits individually (aviation style).

    Usage: {{ 270 | digits }} -> "two seven zero"
           {{ "1013" | digits }} -> "one zero one three"
    Passes through non-digit characters (minus sign, decimal point).
    """
    result = []
    for c in str(value):
        if c in AVIATION_DIGITS:
            result.append(AVIATION_DIGITS[c])
        elif c == "-":
            result.append("minus")
        elif c == ".":
            result.append("decimal")
        elif c.strip():
            result.append(c)
    return " ".join(result)


def _filter_avspeak(value):
    """Jinja2 filter: auto-detect and speak in aviation style.

    - Pure digits/decimals -> digit-by-digit ("270" -> "two seven zero")
    - Pure letters -> NATO phonetic ("AB" -> "alfa bravo")
    - Mixed -> each char spoken appropriately
    - Words (len > 1 with letters) -> left as-is

    Usage: {{ weather.wind | avspeak }}
           {{ station.icao | avspeak }}
    """
    s = str(value).strip()
    # If it contains spaces, treat each word separately
    if " " in s:
        return " ".join(_filter_avspeak(word) for word in s.split())
    # Short all-alpha strings (<=4 chars like ICAO codes) -> NATO
    # Long all-alpha strings (>4 chars like "temperature") -> leave as words
    if s.isalpha() and len(s) > 4:
        return s

    result = []
    for c in s:
        cl = c.lower()
        if cl in AVIATION_DIGITS:
            result.append(AVIATION_DIGITS[cl])
        elif cl in NATO_ALPHABET:
            result.append(NATO_ALPHABET[cl])
        elif c == "-":
            result.append("minus")
        elif c == ".":
            result.append("decimal")
        elif c == "@":
            result.append("at")
        elif c == "/":
            result.append("")
        elif c.strip():
            result.append(c)
    return " ".join(w for w in result if w)


def _filter_wind(value):
    """Jinja2 filter: speak wind data in aviation format.

    Usage: {{ weather.wind | wind }}
      "270@12"    -> "two seven zero at one two"
      "270@12G18" -> "two seven zero at one two gusts one eight"
      "309@7G8"   -> "three zero niner at seven gusts eight"
    """
    s = str(value).strip()
    # Parse wind format: DIR@SPDGgust or DIR@SPD
    import re as _re
    m = _re.match(r'^(\d+)@(\d+)(?:G(\d+))?$', s)
    if not m:
        return _filter_avspeak(s)
    direction, speed, gust = m.group(1), m.group(2), m.group(3)
    parts = [_filter_digits(direction), "at", _filter_digits(speed)]
    if gust:
        parts.extend(["gusts", _filter_digits(gust)])
    return " ".join(parts)


def _filter_time(value):
    """Jinja2 filter: speak time in aviation format.

    Usage: {{ time.zulu | time }}   -> "one seven zero zero zulu"
           {{ "1700Z" | time }}     -> "one seven zero zero zulu"
           {{ "0945" | time }}      -> "zero niner four five"
    Strips Z suffix and appends "zulu", digits spoken individually.
    """
    s = str(value).strip()
    if s.upper().endswith("Z"):
        s = s[:-1]
        suffix = " zulu"
    else:
        suffix = ""
    return _filter_digits(s) + suffix


def render_template(template_text, cfg=None, extra=None):
    """Render a Jinja2 template string with all data sources.

    Available filters:
      {{ "ABCD" | nato }}     -> "alfa bravo charlie delta"
      {{ 270 | digits }}      -> "two seven zero"
      {{ value | avspeak }}   -> auto-detect letters/digits
    """
    try:
        from jinja2 import Environment
    except ImportError:
        ctx = get_template_context(cfg, extra)
        try:
            return template_text.format(**ctx)
        except Exception:
            return template_text
    ctx = get_template_context(cfg, extra)
    try:
        env = Environment()
        env.filters["nato"] = _filter_nato
        env.filters["digits"] = _filter_digits
        env.filters["avspeak"] = _filter_avspeak
        env.filters["wind"] = _filter_wind
        env.filters["time"] = _filter_time
        tmpl = env.from_string(template_text)
        return tmpl.render(**ctx)
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


TTS_CACHE_FILE = "/tmp/nanoawos_tts_cache.json"
DISK_LOW_THRESHOLD_MB = 10  # Warn/clean if free space on tmpfs below this


def _load_tts_cache():
    """Load the text-hash cache: maps wav_path -> rendered_text."""
    try:
        with open(TTS_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tts_cache(cache):
    with open(TTS_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _tts_wav_path(tag, cfg):
    output_dir = cfg["tts"]["output_dir"]
    safe_tag = re.sub(r'[^a-zA-Z0-9]', '_', tag)
    return f"{output_dir}/action_{safe_tag}.wav"


TIME_VARYING_MARKERS = ("time.zulu", "time.utc", "time.utc_hhmm",
                        "time.utc_hour", "time.utc_min")


def _is_time_varying(template):
    """Check if a Jinja2 template references time fields."""
    return any(marker in template for marker in TIME_VARYING_MARKERS)


def _synthesize_with_engine(text, wav_path, engine, cfg):
    """Synthesize text to wav_path, temporarily overriding engine if needed."""
    from nanoawos.tts import synthesize
    if engine != cfg["tts"].get("engine"):
        original = cfg["tts"].get("engine")
        cfg["tts"]["engine"] = engine
        try:
            synthesize(text, wav_path, cfg)
        finally:
            cfg["tts"]["engine"] = original
    else:
        synthesize(text, wav_path, cfg)


def pregenerate_tts_actions(cfg=None):
    """Pre-generate WAV files for all TTS actions that use Piper.

    Called every 5 minutes by the weather update service.

    For static templates: generates once, skips if text unchanged.
    For time-varying templates (containing time.zulu etc.): generates
    a WAV for each minute in the next 5-minute window so clicks
    always get the exact current-minute version instantly.

    Cloud TTS actions are skipped -- fast enough at click time.
    """
    if cfg is None:
        cfg = load_config()

    cache = _load_tts_cache()
    changed = False

    all_actions = []
    for key, action in cfg.get("click_actions", {}).items():
        if action.get("type") == "tts":
            all_actions.append((f"{key}_clicks", action, None))
    for i, rule in enumerate(cfg.get("transcription_reactions", [])):
        if rule.get("type") == "tts":
            all_actions.append((f"reaction_{rule.get('label', i)}", rule, None))

    for tag, action, extra_ctx in all_actions:
        engine = action.get("tts_engine", "") or cfg["tts"].get("engine", "piper")
        if engine not in ("piper", "wav_concat"):
            continue

        template = action.get("text", "")
        if not template:
            continue

        # Render with current data and generate one WAV per template.
        # For time-varying templates, this means the time is at most 5 min
        # stale (same as weather data). Simple, no tmpfs bloat.
        rendered = render_template(template, cfg, extra_ctx)
        wav_path = _tts_wav_path(tag, cfg)

        if cache.get(wav_path) == rendered and os.path.exists(wav_path):
            log.debug("TTS cache hit for %s", tag)
            continue

        log.info("Pre-generating TTS [%s]: %s", tag, rendered[:60])
        try:
            _synthesize_with_engine(rendered, wav_path, engine, cfg)
            cache[wav_path] = rendered
            changed = True
        except Exception as e:
            log.error("Pre-generation failed for %s: %s", tag, e)

    # Clean stale cache entries (old paths, deleted files)
    cache = {k: v for k, v in cache.items() if os.path.exists(k)}

    if changed:
        _save_tts_cache(cache)
        log.info("TTS cache updated (%d entries)", len(cache))

    # Clean up orphaned timed variant WAVs from the old strategy
    output_dir = cfg.get("tts", {}).get("output_dir", "/run/nanoawos")
    import glob as _glob
    removed = 0
    for wav_path in _glob.glob(os.path.join(output_dir, "action_*_????.wav")):
        try:
            os.unlink(wav_path)
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("Cleaned up %d orphaned timed WAV files", removed)


def check_disk_space(cfg=None):
    """Check free disk space and clean up if low.

    Called by the weather update service. If free space on the TTS
    output partition drops below threshold, aggressively removes
    old WAVs and logs a warning.
    """
    if cfg is None:
        cfg = load_config()
    output_dir = cfg.get("tts", {}).get("output_dir", "/mnt/p4")

    try:
        stat = os.statvfs(output_dir)
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
    except OSError:
        return

    if free_mb < DISK_LOW_THRESHOLD_MB:
        log.warning("Low disk space: %.0fMB free on %s (threshold: %dMB)",
                     free_mb, output_dir, DISK_LOW_THRESHOLD_MB)

        # Aggressive cleanup: remove ALL minute-variant WAVs
        import glob as _glob
        removed = 0
        for wav_path in _glob.glob(os.path.join(output_dir, "action_*_????.wav")):
            try:
                os.unlink(wav_path)
                removed += 1
            except OSError:
                pass

        # Clear cache entries for removed files
        cache = _load_tts_cache()
        cache = {k: v for k, v in cache.items() if os.path.exists(k)}
        _save_tts_cache(cache)

        log.warning("Emergency cleanup: removed %d WAV files, %.0fMB now free",
                     removed, free_mb)


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
        tts_engine = action.get("tts_engine", "") or cfg["tts"].get("engine", "piper")
        is_slow_engine = tts_engine in ("piper", "wav_concat")

        # For slow engines: play pre-generated WAV if it exists
        if is_slow_engine:
            wav_path = _tts_wav_path(tag, cfg)
            if os.path.exists(wav_path):
                log.info("TTS [%s] playing cached: %s", tag, wav_path)
                from nanoawos.audio import play_wav
                play_wav(wav_path, cfg)
                return
            log.info("TTS [%s] no cached WAV, generating live...", tag)

        # Cloud engine or no cached WAV -- generate live
        text = render_template(template, cfg, extra_ctx)
        wav_path = _tts_wav_path(tag, cfg)
        log.info("TTS [%s] generating live (%s): %s", tag, tts_engine, text[:60])
        _synthesize_with_engine(text, wav_path, tts_engine, cfg)

        cache = _load_tts_cache()
        cache[wav_path] = text
        _save_tts_cache(cache)

        from nanoawos.audio import play_wav
        play_wav(wav_path, cfg)

    elif action_type == "audio_file":
        audio_path = action.get("file", "")
        if not audio_path or not os.path.exists(audio_path):
            log.warning("Audio file not found: %s", audio_path)
            return
        from nanoawos.audio import play_wav
        play_wav(audio_path, cfg)

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
    """Execute the action configured for the given click count.

    Always reloads config to pick up changes from web UI without restart.
    """
    # Force reload config to pick up web UI changes
    from nanoawos.config import load_config as _reload
    import nanoawos.config as _cfg_mod
    _cfg_mod._config = None
    cfg = _reload()

    actions = cfg.get("click_actions", {})
    # Try both int and string keys (YAML stores int, JSON uses string)
    action = actions.get(click_count) or actions.get(str(click_count))
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
