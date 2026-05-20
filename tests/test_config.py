"""Tests for nanoawos/config.py."""

import yaml
import pytest

import nanoawos.config as config_mod
from nanoawos.config import load_config, save_config, get


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the module-level config cache before each test."""
    config_mod._config = None
    yield
    config_mod._config = None


# -- load_config -----------------------------------------------------------

def test_load_config_valid_yaml(tmp_path):
    """load_config reads and returns a dict from a valid YAML file."""
    cfg_data = {"station": {"id": "TST"}, "audio": {"gpio_pin": 201}}
    cfg_path = tmp_path / "nanoawos.yaml"
    cfg_path.write_text(yaml.dump(cfg_data))

    result = load_config(str(cfg_path))

    assert result["station"]["id"] == "TST"
    assert result["audio"]["gpio_pin"] == 201
    # Internal key injected by load_config
    assert result["_config_path"] == str(cfg_path)


def test_load_config_caches_result(tmp_path):
    """Calling load_config twice without a path returns the cached object."""
    cfg_data = {"station": {"id": "CACHED"}}
    cfg_path = tmp_path / "nanoawos.yaml"
    cfg_path.write_text(yaml.dump(cfg_data))

    first = load_config(str(cfg_path))
    second = load_config()  # no path -> should return cached

    assert first is second


def test_load_config_missing_file_raises():
    """load_config raises FileNotFoundError when no config file exists."""
    with pytest.raises(FileNotFoundError, match="No config found"):
        load_config("/nonexistent/path/nanoawos.yaml")


# -- save_config ------------------------------------------------------------

def test_save_config_writes_valid_yaml(tmp_path):
    """save_config writes a YAML file that can be re-loaded."""
    cfg = {"station": {"id": "SAVE"}, "audio": {"gpio_pin": 5}}
    out_path = tmp_path / "out.yaml"

    save_config(cfg, str(out_path))

    with open(out_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded == cfg


def test_save_config_excludes_internal_keys(tmp_path):
    """Keys starting with '_' must not appear in the written file."""
    cfg = {
        "station": {"id": "INT"},
        "_config_path": "/some/path",
        "_internal": True,
    }
    out_path = tmp_path / "out.yaml"

    save_config(cfg, str(out_path))

    with open(out_path) as f:
        loaded = yaml.safe_load(f)
    assert "_config_path" not in loaded
    assert "_internal" not in loaded
    assert loaded["station"]["id"] == "INT"


# -- get() ------------------------------------------------------------------

def test_get_returns_section_dict(tmp_path, sample_config):
    """get(section) with no key returns the entire section dict."""
    config_mod._config = sample_config

    section = get("station")

    assert isinstance(section, dict)
    assert section["id"] == "TEST1"
    assert section["icao"] == "ZZZZ"


def test_get_returns_specific_key(tmp_path, sample_config):
    """get(section, key) returns a single value from the section."""
    config_mod._config = sample_config

    assert get("audio", "gpio_pin") == 201
    assert get("station", "name") == "test station"


def test_get_returns_default_for_missing_key(tmp_path, sample_config):
    """get() returns the default when the key (or section) is absent."""
    config_mod._config = sample_config

    assert get("station", "nonexistent", "fallback") == "fallback"
    assert get("no_such_section", "key", 42) == 42
    # Missing key with no explicit default returns None
    assert get("station", "missing") is None
