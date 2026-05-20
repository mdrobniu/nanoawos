"""Tests for nanoawos/web/app.py."""

import json
import os
import subprocess
from unittest.mock import patch, mock_open, MagicMock

import pytest

import nanoawos.config as config_mod


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset config cache before/after each test."""
    config_mod._config = None
    yield
    config_mod._config = None


@pytest.fixture
def client(sample_config, tmp_path):
    """Create a Flask test client with sample_config pre-loaded."""
    config_mod._config = sample_config

    from nanoawos.web.app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _mock_read_file(path, default=""):
    """Mock replacement for _read_file in the web module."""
    files = {
        "/tmp/metar": "ZZZZ 201045Z 27012G18KT 9999 FEW030 20/12 Q1013",
        "/tmp/metar2": "Wind 270/12G18",
        "/tmp/metar3": "T20 QNH1013",
        "/tmp/metar4": "DA+200ft",
        "/tmp/tap": "4",
        "/proc/uptime": "86400.00 172800.00",
        "/tmp/tap_debug": "1500 2000 3 1 10 click 200 80 40 900 1",
    }
    return files.get(path, default)


def _mock_service_status(name):
    return "active"


# -- GET / ------------------------------------------------------------------

def test_index_returns_200(client):
    """GET / should return 200 with the web UI page."""
    with patch("nanoawos.web.app._read_file", side_effect=_mock_read_file), \
         patch("nanoawos.web.app._service_status", return_value="active"), \
         patch("nanoawos.web.app._list_tts_models", return_value=[]):
        resp = client.get("/")

    assert resp.status_code == 200


# -- GET /api/weather -------------------------------------------------------

def test_api_weather_returns_json_with_metar_fields(client):
    """GET /api/weather returns JSON containing metar, wind, temp_qnh, etc."""
    with patch("nanoawos.web.app._read_file", side_effect=_mock_read_file):
        resp = client.get("/api/weather")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "metar" in data
    assert "wind" in data
    assert "temp_qnh" in data
    assert "density_alt" in data
    assert "tap_count" in data
    assert data["metar"] == "ZZZZ 201045Z 27012G18KT 9999 FEW030 20/12 Q1013"


# -- GET /api/status --------------------------------------------------------

def test_api_status_returns_json_with_services(client):
    """GET /api/status returns JSON with services dict and weather data."""
    with patch("nanoawos.web.app._read_file", side_effect=_mock_read_file), \
         patch("nanoawos.web.app._service_status", return_value="active"):
        resp = client.get("/api/status")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "services" in data
    assert "weather" in data
    assert "uptime" in data
    # All six services present
    for svc in ("nanoawos-weather.timer", "nanoawos-tap", "nanoawos-gpio",
                "mpd", "darkice", "icecast2"):
        assert svc in data["services"]


# -- GET /api/tap -----------------------------------------------------------

def test_api_tap_returns_json_with_tap_fields(client):
    """GET /api/tap returns JSON with amplitude, threshold, clicks, etc."""
    with patch("nanoawos.web.app._read_file", side_effect=_mock_read_file):
        resp = client.get("/api/tap")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "amplitude" in data
    assert "threshold" in data
    assert "clicks" in data
    assert "active" in data
    assert "state" in data
    assert "last_tap" in data
    assert data["amplitude"] == 1500.0
    assert data["clicks"] == 3
    assert data["state"] == "click"


# -- POST /api/play/<name> -------------------------------------------------

def test_api_play_full_calls_mpc_commands(client):
    """POST /api/play/full calls mpc clear, load, crossfade, play."""
    with patch("nanoawos.web.app.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        resp = client.post("/api/play/full")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "playing"
    assert data["playlist"] == "full"

    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert ["mpc", "clear"] in cmds
    assert ["mpc", "load", "full"] in cmds
    assert ["mpc", "crossfade", "1"] in cmds
    assert ["mpc", "play"] in cmds


def test_api_play_invalid_returns_400(client):
    """POST /api/play/invalid returns 400 with error message."""
    resp = client.post("/api/play/invalid")

    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# -- GET /api/config --------------------------------------------------------

def test_api_config_get_returns_config_without_internal_keys(client, sample_config):
    """GET /api/config returns the config but omits keys starting with '_'."""
    resp = client.get("/api/config")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "_config_path" not in data
    assert "station" in data
    assert "audio" in data


# -- PUT /api/config --------------------------------------------------------

def test_api_config_put_merges_sections(client, sample_config, tmp_path):
    """PUT /api/config merges dict sections into existing config."""
    update = {"station": {"name": "updated station"}}

    with patch("nanoawos.web.app.save_config") as mock_save:
        resp = client.put("/api/config",
                          data=json.dumps(update),
                          content_type="application/json")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "saved"

    # Verify save_config was called and the station section was merged
    saved_cfg = mock_save.call_args[0][0]
    assert saved_cfg["station"]["name"] == "updated station"
    # Original keys preserved via merge
    assert saved_cfg["station"]["id"] == "TEST1"


def test_api_config_put_normalizes_click_actions_keys_to_int(client, sample_config):
    """PUT /api/config converts click_actions string keys to int."""
    update = {
        "click_actions": {
            "4": {"type": "weather_wind", "label": "Wind update"},
            "7": {"type": "tts", "label": "Custom", "text": "test"},
        }
    }

    with patch("nanoawos.web.app.save_config") as mock_save:
        resp = client.put("/api/config",
                          data=json.dumps(update),
                          content_type="application/json")

    assert resp.status_code == 200
    saved_cfg = mock_save.call_args[0][0]
    # Keys must be int, not str
    assert 4 in saved_cfg["click_actions"]
    assert 7 in saved_cfg["click_actions"]
    assert "4" not in saved_cfg["click_actions"]


# -- GET /api/audio/list ----------------------------------------------------

def test_api_audio_list_returns_empty_list_when_no_files(client):
    """GET /api/audio/list returns [] when the upload dir does not exist."""
    with patch("nanoawos.web.app.os.path.isdir", return_value=False):
        resp = client.get("/api/audio/list")

    assert resp.status_code == 200
    assert resp.get_json() == []


# -- POST /api/tap/calibrate ------------------------------------------------

def test_api_tap_calibrate_creates_flag_file(client, tmp_path):
    """POST /api/tap/calibrate writes a calibration flag to /tmp."""
    m = mock_open()
    with patch("builtins.open", m):
        resp = client.post("/api/tap/calibrate",
                           data=json.dumps({"clicks": 3}),
                           content_type="application/json")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "calibrating"
    assert data["sequences"] == 3
    m.assert_called_once_with("/tmp/nanoawos_calibrate", "w")
    m().write.assert_called_once_with("3")


# -- GET /api/transcriptions ------------------------------------------------

def test_api_transcriptions_returns_empty_list_when_no_log(client):
    """GET /api/transcriptions returns [] when the log file does not exist."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        resp = client.get("/api/transcriptions")

    assert resp.status_code == 200
    assert resp.get_json() == []


# -- DELETE /api/tap/profile -------------------------------------------------

def test_api_tap_profile_delete_removes_file(client):
    """DELETE /api/tap/profile calls os.unlink on the profile file."""
    with patch("nanoawos.web.app.os.unlink") as mock_unlink:
        resp = client.delete("/api/tap/profile")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "cleared"
    mock_unlink.assert_called_once_with("/tmp/nanoawos_click_profile.json")
