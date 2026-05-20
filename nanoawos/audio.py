"""MPD playlist management and GPIO PTT controller."""

import logging
import os
import signal
import subprocess
import sys
import time

from nanoawos.config import load_config

log = logging.getLogger(__name__)


def _gpio_path(pin):
    return f"/sys/class/gpio/gpio{pin}"


def init_gpio(cfg=None):
    """Initialize GPIO pin for PTT relay."""
    if cfg is None:
        cfg = load_config()
    pin = cfg["audio"]["gpio_pin"]
    gpio = _gpio_path(pin)

    if not os.path.isfile(f"{gpio}/direction"):
        with open("/sys/class/gpio/export", "w") as f:
            f.write(str(pin))
        time.sleep(0.1)
        with open(f"{gpio}/direction", "w") as f:
            f.write("out")
    log.info("GPIO %d initialized", pin)


def set_ptt(state, cfg=None):
    """Set PTT relay state (True=transmit, False=idle)."""
    if cfg is None:
        cfg = load_config()
    pin = cfg["audio"]["gpio_pin"]
    with open(f"{_gpio_path(pin)}/value", "w") as f:
        f.write("1" if state else "0")


def get_ptt(cfg=None):
    """Read current PTT state."""
    if cfg is None:
        cfg = load_config()
    pin = cfg["audio"]["gpio_pin"]
    with open(f"{_gpio_path(pin)}/value", "r") as f:
        return f.read().strip() == "1"


def wait_for_idle(cfg=None):
    """Wait until PTT is not transmitting (GPIO is low)."""
    if cfg is None:
        cfg = load_config()
    while get_ptt(cfg):
        time.sleep(1)


def _mpc(args, cfg=None):
    """Run an mpc command.

    Uses local connection (no -h flag) because MPD restricts file access
    when connecting via TCP. Local socket/loopback allows file:// paths.
    """
    cmd = ["mpc"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and result.stderr:
        log.warning("mpc %s failed: %s", args[0], result.stderr.strip())
    return result


def update_playlists(full_wav, wind_wav, cfg=None):
    """Update MPD playlists with new WAV files."""
    if cfg is None:
        cfg = load_config()

    # Remove old playlists
    _mpc(["rm", "full"], cfg)
    _mpc(["rm", "wind"], cfg)

    # Create full weather playlist
    _mpc(["clear"], cfg)
    _mpc(["add", full_wav], cfg)
    _mpc(["save", "full"], cfg)

    # Create wind-only playlist
    _mpc(["clear"], cfg)
    _mpc(["add", wind_wav], cfg)
    _mpc(["save", "wind"], cfg)

    log.info("Playlists updated: full=%s, wind=%s", full_wav, wind_wav)


def play_playlist(name, cfg=None):
    """Play a named playlist (full or wind)."""
    if cfg is None:
        cfg = load_config()
    _mpc(["clear"], cfg)
    _mpc(["load", name], cfg)
    _mpc(["crossfade", "1"], cfg)
    _mpc(["play"], cfg)
    log.info("Playing playlist: %s", name)


def play_wav(wav_path, cfg=None):
    """Play a single WAV file directly."""
    if cfg is None:
        cfg = load_config()
    _mpc(["clear"], cfg)
    _mpc(["add", wav_path], cfg)
    _mpc(["play"], cfg)
    log.info("Playing WAV: %s", wav_path)


def _get_mpd_state(cfg):
    """Get MPD play state via python-mpd2."""
    from mpd import MPDClient
    client = MPDClient()
    try:
        client.connect(cfg["audio"]["mpd_host"], cfg["audio"]["mpd_port"])
        state = client.status().get("state", "stop")
        client.disconnect()
        return state
    except Exception:
        return "stop"


def gpio_watcher_main():
    """Main loop: watch MPD state and control PTT GPIO accordingly.

    Runs as a daemon. Sets GPIO high when MPD is playing, low when stopped.
    Ignores SIGALRM to prevent being killed by NanoHatOLED binary.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()

    # Ignore SIGALRM from NanoHatOLED binary button handler
    signal.signal(signal.SIGALRM, signal.SIG_IGN)

    init_gpio(cfg)
    log.info("GPIO watcher started, monitoring MPD state")

    from mpd import MPDClient
    from socket import error as SocketError

    # Connect via localhost for local file access permissions
    mpd_host = "localhost"
    mpd_port = cfg["audio"]["mpd_port"]

    client = MPDClient()
    try:
        client.connect(mpd_host, mpd_port)
        log.info("Connected to MPD at %s:%s", mpd_host, mpd_port)
    except SocketError:
        log.error("Failed to connect to MPD")
        sys.exit(1)

    playing = False

    while True:
        try:
            state = client.status().get("state", "stop")
            if state == "play":
                if not playing:
                    set_ptt(True, cfg)
                    playing = True
            else:
                if playing:
                    set_ptt(False, cfg)
                    playing = False
            time.sleep(0.1)
        except Exception as e:
            log.warning("MPD connection lost: %s, reconnecting...", e)
            playing = False
            set_ptt(False, cfg)
            time.sleep(2)
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.connect(mpd_host, mpd_port)
                log.info("Reconnected to MPD")
            except Exception:
                pass


if __name__ == "__main__":
    gpio_watcher_main()
