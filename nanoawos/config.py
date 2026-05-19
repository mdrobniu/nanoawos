"""Configuration loader for NanoAWOS."""

import os
import yaml

DEFAULT_CONFIG_PATHS = [
    "/opt/nanoawos/config/nanoawos.yaml",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "nanoawos.yaml"),
]

_config = None


def load_config(path=None):
    """Load YAML config from file. Caches the result."""
    global _config
    if _config is not None and path is None:
        return _config

    if path:
        paths = [path]
    else:
        paths = DEFAULT_CONFIG_PATHS

    for p in paths:
        if os.path.isfile(p):
            with open(p) as f:
                _config = yaml.safe_load(f)
            _config["_config_path"] = p
            return _config

    raise FileNotFoundError(f"No config found in: {paths}")


def save_config(cfg, path=None):
    """Write config back to YAML file."""
    path = path or cfg.get("_config_path")
    if not path:
        raise ValueError("No config path specified")
    to_write = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(path, "w") as f:
        yaml.dump(to_write, f, default_flow_style=False, sort_keys=False)


def get(section, key=None, default=None):
    """Get a config value. Returns section dict if key is None."""
    cfg = load_config()
    s = cfg.get(section, {})
    if key is None:
        return s
    return s.get(key, default)
