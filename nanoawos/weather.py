"""Weather data fetching from Weather Underground API."""

import json
import logging
import math
import subprocess
import sys
from dataclasses import dataclass, field

import requests

from nanoawos.config import load_config

log = logging.getLogger(__name__)


@dataclass
class WeatherData:
    obs_time_utc: str = ""
    time_hour: str = ""
    wind_dir: int = 0
    wind_speed_kt: int = 0
    wind_gust_kt: int = 0
    temp_c: float = 0.0
    dewpt_c: float = 0.0
    pressure_hpa: float = 1013.0
    density_alt_ft: int = 0
    recommended_runway: int = 0
    has_gusts: bool = False
    data_valid: bool = False


def fetch_weather(cfg=None):
    """Fetch current weather from Wunderground API. Returns WeatherData."""
    if cfg is None:
        cfg = load_config()

    api_key = cfg["weather"]["api_key"]
    station_id = cfg["station"]["id"]
    api_url = cfg["weather"]["api_url"]

    wd = WeatherData()

    # Fetch English units (for wind in knots)
    try:
        resp = requests.get(api_url, params={
            "stationId": station_id,
            "format": "json",
            "units": "e",
            "apiKey": api_key,
        }, timeout=15)
        resp.raise_for_status()
        english = resp.json()["observations"][0]
    except Exception as e:
        log.error("Failed to fetch English units: %s", e)
        return wd

    # Fetch Metric units (for temp, pressure)
    try:
        resp = requests.get(api_url, params={
            "stationId": station_id,
            "format": "json",
            "units": "m",
            "apiKey": api_key,
        }, timeout=15)
        resp.raise_for_status()
        metric = resp.json()["observations"][0]
    except Exception as e:
        log.error("Failed to fetch Metric units: %s", e)
        return wd

    # Parse observation time
    wd.obs_time_utc = metric.get("obsTimeUtc", "")
    try:
        time_parts = wd.obs_time_utc.split("T")[1].split(":")
        wd.time_hour = time_parts[0] + time_parts[1]
    except (IndexError, AttributeError):
        wd.time_hour = "0000"

    # Wind data (English units give knots)
    wd.wind_dir = english.get("winddir") or 0
    wd.wind_speed_kt = english.get("imperial", {}).get("windSpeed") or 0
    wind_gust = english.get("imperial", {}).get("windGust") or 0
    if wind_gust > wd.wind_speed_kt:
        wd.wind_gust_kt = wind_gust
        wd.has_gusts = True

    # Temperature and pressure (Metric)
    wd.temp_c = metric.get("metric", {}).get("temp")
    wd.dewpt_c = metric.get("metric", {}).get("dewpt")
    wd.pressure_hpa = metric.get("metric", {}).get("pressure") or 1013.0

    # Handle None temp/dewpt - use 15/5 as safe defaults
    if wd.temp_c is None:
        log.warning("Temperature is None from API, using 15C default")
        wd.temp_c = 15.0
    if wd.dewpt_c is None:
        log.warning("Dewpoint is None from API, using 5C default")
        wd.dewpt_c = 5.0

    # Density altitude calculation
    elevation = cfg["station"]["elevation_ft"]
    pressure_alt = elevation + (1013 - wd.pressure_hpa) * 30
    standard_temp = 15 - (2 * (elevation / 1000))
    humidity_correction = 0.1 * (wd.temp_c - wd.dewpt_c)
    wd.density_alt_ft = math.ceil(
        pressure_alt + (120 * (wd.temp_c - standard_temp)) + humidity_correction
    )

    # Runway recommendation
    runways = cfg["station"].get("runways", [15, 33])
    if len(runways) >= 2:
        crosswind = [abs(wd.wind_dir - r * 10) for r in runways]
        # Normalize to 0-180
        crosswind = [min(c, 360 - c) for c in crosswind]
        wd.recommended_runway = runways[crosswind.index(min(crosswind))]

    wd.data_valid = True
    log.info("Weather fetched: %s %d@%d QNH %.1f DA %dft",
             wd.time_hour, wd.wind_dir, wd.wind_speed_kt,
             wd.pressure_hpa, wd.density_alt_ft)
    return wd


def build_announcement_text(wd, cfg=None):
    """Build the full weather announcement text for TTS."""
    if cfg is None:
        cfg = load_config()

    station_name = cfg["station"]["name"]
    parts = []

    # Station ID and time
    parts.append(station_name)
    parts.append(" ".join(list(wd.time_hour)))
    parts.append("zulu,")

    # Wind
    parts.append("weather")
    parts.append("wind")
    parts.append(" ".join(list(str(wd.wind_dir))))
    parts.append("at")
    parts.append(" ".join(list(str(wd.wind_speed_kt))))
    if wd.has_gusts:
        parts.append("gusts")
        parts.append(" ".join(list(str(wd.wind_gust_kt))))
    parts.append(",")

    # Temperature
    temp_str = str(int(wd.temp_c))
    dewpt_str = str(int(wd.dewpt_c))
    parts.append("temperature")
    parts.append(" ".join(list(temp_str)))
    parts.append("dewpoint")
    parts.append(" ".join(list(dewpt_str)))
    parts.append(",")

    # QNH
    qnh = str(math.ceil(wd.pressure_hpa))
    parts.append("qnh")
    parts.append(" ".join(list(qnh)))
    parts.append(",")

    # Density altitude (only if above 2000ft)
    if wd.density_alt_ft > 2000:
        parts.append("density altitude")
        parts.append(str(wd.density_alt_ft))
        parts.append("feet")
        parts.append(",")

    # Runway recommendation
    rwy = str(wd.recommended_runway)
    parts.append(f"recommended runway is {' '.join(list(rwy))}")

    return " ".join(parts)


def build_wind_text(wd):
    """Build wind-only announcement text for TTS."""
    parts = ["wind", " ".join(list(str(wd.wind_dir))),
             "at", " ".join(list(str(wd.wind_speed_kt)))]
    if wd.has_gusts:
        parts.extend(["gusts", " ".join(list(str(wd.wind_gust_kt)))])
    return " ".join(parts)


def write_metar_files(wd, cfg=None):
    """Write METAR data to /tmp files for OLED display."""
    if cfg is None:
        cfg = load_config()

    icao = cfg["station"]["icao"]

    with open("/tmp/metar", "w") as f:
        f.write(f"{icao} {wd.time_hour}")

    wind_str = f"{wd.wind_dir}@{wd.wind_speed_kt}"
    if wd.has_gusts:
        wind_str += f"G{wd.wind_gust_kt}"
    with open("/tmp/metar2", "w") as f:
        f.write(wind_str)

    with open("/tmp/metar3", "w") as f:
        f.write(f"{int(wd.temp_c)}/{int(wd.dewpt_c)} Q{wd.pressure_hpa}")

    with open("/tmp/metar4", "w") as f:
        f.write(f"DA{wd.density_alt_ft}FT")


def main():
    """Entry point: fetch weather, synthesize TTS, update playlists."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()

    log.info("Fetching weather data...")
    wd = fetch_weather(cfg)
    if not wd.data_valid:
        log.error("Failed to get valid weather data")
        sys.exit(1)

    write_metar_files(wd, cfg)

    # TTS synthesis
    from nanoawos.tts import synthesize
    full_text = build_announcement_text(wd, cfg)
    wind_text = build_wind_text(wd)
    log.info("Full text: %s", full_text)
    log.info("Wind text: %s", wind_text)

    output_dir = cfg["tts"]["output_dir"]
    full_wav = synthesize(full_text, f"{output_dir}/full.wav", cfg)
    wind_wav = synthesize(wind_text, f"{output_dir}/wind.wav", cfg)

    # Update MPD playlists (wait if currently transmitting)
    from nanoawos.audio import wait_for_idle, update_playlists
    wait_for_idle(cfg)
    update_playlists(full_wav, wind_wav, cfg)

    log.info("Weather update complete")


if __name__ == "__main__":
    main()
