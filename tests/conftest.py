"""Shared test fixtures for NanoAWOS test suite."""

import os
import json
import pytest
import yaml


@pytest.fixture
def sample_config(tmp_path):
    """Minimal valid config for testing."""
    cfg = {
        "station": {"id": "TEST1", "icao": "ZZZZ", "name": "test station",
                     "elevation_ft": 500, "runways": [9, 27]},
        "weather": {"api_key": "test-key", "update_interval_sec": 300,
                     "api_url": "https://api.weather.com/v2/pws/observations/current"},
        "tts": {"engine": "piper", "piper_model": "/tmp/model.onnx", "output_dir": str(tmp_path)},
        "audio": {"mpd_host": "localhost", "mpd_port": 6600, "gpio_pin": 201, "alsa_device": "hw:2,0"},
        "tap": {"short_clicks": 4, "long_clicks": 6, "calibration_seconds": 1,
                "device_name": "", "min_click_ms": 50, "max_click_ms": 2000,
                "min_gap_ms": 150, "max_gap_ms": 2500, "high_mult": 50, "low_mult": 10},
        "click_actions": {
            4: {"type": "weather_wind", "label": "Wind"},
            6: {"type": "weather_full", "label": "Full weather"},
            5: {"type": "tts", "label": "Custom", "text": "hello world"},
            8: {"type": "mqtt", "label": "MQTT test", "topic": "test/topic"},
        },
        "transcription_reactions": [
            {"label": "Emergency", "match": "mayday|pan pan", "match_field": "text",
             "type": "mqtt", "topic": "test/emergency"},
            {"label": "Landing", "match": "final|landing", "match_field": "text",
             "type": "tts", "text": "traffic alert"},
        ],
        "data_sources": [],
        "mqtt": {"enabled": False, "broker": "localhost", "port": 1883},
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080},
        "oled": {"enabled": True},
        "transcribe": {"enabled": False, "openai_api_key": "", "model": "whisper-1",
                        "language": "auto"},
    }
    cfg_path = tmp_path / "nanoawos.yaml"
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    cfg["_config_path"] = str(cfg_path)
    return cfg


@pytest.fixture
def weather_api_response_english():
    """Mock Weather Underground API response (English/imperial units)."""
    return {"observations": [{"stationID": "TEST1", "obsTimeUtc": "2026-05-20T10:45:00Z",
            "winddir": 270, "imperial": {"windSpeed": 12, "windGust": 18,
            "pressure": 29.92, "temp": 68, "dewpt": 55}}]}


@pytest.fixture
def weather_api_response_metric():
    """Mock Weather Underground API response (Metric units)."""
    return {"observations": [{"stationID": "TEST1", "obsTimeUtc": "2026-05-20T10:45:00Z",
            "winddir": 270, "metric": {"windSpeed": 22, "windGust": 33,
            "pressure": 1013.2, "temp": 20.0, "dewpt": 12.0}}]}


@pytest.fixture
def weather_api_response_null_temp():
    """Mock API response with null temperature (sensor offline)."""
    return {"observations": [{"stationID": "TEST1", "obsTimeUtc": "2026-05-20T10:45:00Z",
            "winddir": 270, "metric": {"windSpeed": 22, "windGust": 33,
            "pressure": 1013.2, "temp": None, "dewpt": None}}]}
