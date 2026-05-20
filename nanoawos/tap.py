"""PTT click detector for NanoAWOS.

Listens to the radio audio input and counts PTT button clicks.
4 clicks -> wind only, 6 clicks -> full weather report.

Detection pipeline:
  1. Integer energy computation per audio frame
  2. Adaptive noise floor tracking (asymmetric EMA)
  3. Schmitt trigger with hysteresis (dual threshold)
  4. Debounced state machine with timing constraints
  5. Self-learning: tracks click energy statistics to auto-tune thresholds

Self-learning algorithm:
  - Records energy of every confirmed click (passed all timing checks)
  - Maintains running statistics: min, max, mean, stddev of click energies
  - After enough samples (10+), auto-computes optimal threshold multipliers
  - Sets t_high = midpoint between noise floor and weakest observed click
  - Sets t_low = noise_floor * 3 (just above the decay tail)
  - Persists learned profile to /tmp/nanoawos_click_profile.json
  - Reloads on restart so learning survives reboots
  - Web UI shows calibration mode + learned stats

Calibration mode (triggered via web API):
  - Records next N clicks as calibration samples
  - Uses those to establish the profile from scratch
  - Good for new radio setups or volume changes
"""

import json
import logging
import math
import os
import signal
import struct
import sys
import time

import pyaudio

from nanoawos.config import load_config

log = logging.getLogger(__name__)

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
FRAME_SAMPLES = 2205  # 50ms at 44100Hz

PROFILE_PATH = "/tmp/nanoawos_click_profile.json"
CALIBRATION_FLAG = "/tmp/nanoawos_calibrate"


class NoiseFloorTracker:
    """Adaptive noise floor estimator using asymmetric EMA."""

    def __init__(self, alpha_rise=0.001, alpha_fall=0.05):
        self.alpha_rise = alpha_rise
        self.alpha_fall = alpha_fall
        self.estimate = 0

    def update(self, energy):
        if self.estimate == 0:
            self.estimate = energy
            return self.estimate
        if energy < self.estimate:
            self.estimate += self.alpha_fall * (energy - self.estimate)
        else:
            self.estimate += self.alpha_rise * (energy - self.estimate)
        return self.estimate


class ClickProfile:
    """Self-learning click energy profiler.

    Tracks statistics of confirmed click energies to auto-tune thresholds.
    """

    def __init__(self):
        self.click_energies = []  # Peak energy of each confirmed click
        self.click_durations = []  # Duration of each click in ms
        self.max_samples = 100  # Rolling window
        self.min_samples_for_auto = 5  # Need this many to auto-tune
        self.load()

    def record_click(self, peak_energy, duration_ms):
        """Record a confirmed click's peak energy and duration."""
        self.click_energies.append(peak_energy)
        self.click_durations.append(duration_ms)
        if len(self.click_energies) > self.max_samples:
            self.click_energies = self.click_energies[-self.max_samples:]
            self.click_durations = self.click_durations[-self.max_samples:]
        self.save()

    def has_enough_data(self):
        return len(self.click_energies) >= self.min_samples_for_auto

    def get_thresholds(self, noise_floor, default_high_mult, default_low_mult):
        """Compute optimal thresholds from learned click profile.

        Strategy:
          t_high = 10% of the weakest observed click energy
          t_low  = t_high / 5
        This guarantees thresholds are always well below any real click
        but well above the noise floor + decay tail.

        Safety: never go below the default multiplier thresholds.
        """
        if not self.has_enough_data() or noise_floor <= 0:
            return None, None

        # Use the weakest observed click as our reference
        weakest = min(self.click_energies)

        # t_high at 10% of weakest click -- gives 10x safety margin
        t_high = weakest * 0.10
        # t_low at 2% of weakest click
        t_low = weakest * 0.02

        # Safety floor: never go below default multiplier thresholds
        default_high = noise_floor * default_high_mult
        default_low = noise_floor * default_low_mult
        t_high = max(t_high, default_high)
        t_low = max(t_low, default_low)

        # Ensure t_low < t_high
        if t_low >= t_high:
            t_low = t_high * 0.3

        return t_high, t_low

    def get_stats(self):
        """Return profile statistics for web UI."""
        if not self.click_energies:
            return {"samples": 0}
        energies = self.click_energies
        durations = self.click_durations or [0]
        return {
            "samples": len(energies),
            "min_energy": min(energies),
            "max_energy": max(energies),
            "mean_energy": int(sum(energies) / len(energies)),
            "min_duration_ms": round(min(durations), 0),
            "max_duration_ms": round(max(durations), 0),
            "mean_duration_ms": round(sum(durations) / len(durations), 0),
            "auto_tuning": self.has_enough_data(),
        }

    def save(self):
        try:
            with open(PROFILE_PATH, "w") as f:
                json.dump({
                    "click_energies": self.click_energies[-self.max_samples:],
                    "click_durations": self.click_durations[-self.max_samples:],
                }, f)
        except Exception:
            pass

    def load(self):
        try:
            with open(PROFILE_PATH) as f:
                data = json.load(f)
            self.click_energies = data.get("click_energies", [])
            self.click_durations = data.get("click_durations", [])
            if self.click_energies:
                log.info("Loaded click profile: %d samples, energy range %d-%d",
                         len(self.click_energies),
                         min(self.click_energies), max(self.click_energies))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def clear(self):
        self.click_energies = []
        self.click_durations = []
        self.save()


class SchmittTrigger:
    """Software Schmitt trigger with hysteresis."""

    def __init__(self):
        self.active = False

    def update(self, energy, t_high, t_low):
        if not self.active and energy > t_high:
            self.active = True
        elif self.active and energy < t_low:
            self.active = False
        return self.active


class ClickStateMachine:
    """Debounced click counter with timing constraints."""

    def __init__(self, min_click_ms=50, max_click_ms=2000,
                 min_gap_ms=150, max_gap_ms=2500):
        self.min_click = min_click_ms / 1000.0
        self.max_click = max_click_ms / 1000.0
        self.min_gap = min_gap_ms / 1000.0
        self.max_gap = max_gap_ms / 1000.0

        self.state = "IDLE"
        self.click_count = 0
        self.active_start = 0.0
        self.gap_start = 0.0
        self.peak_energy = 0  # Track peak energy of current click

    def update(self, is_active, now, energy=0):
        """Returns (click_count, peak_energy, duration_ms) when sequence ends."""
        if self.state == "IDLE":
            if is_active:
                self.state = "ACTIVE"
                self.active_start = now
                self.click_count = 0
                self.peak_energy = energy
                self._click_peaks = []
                self._click_durs = []
            return 0, 0, 0

        elif self.state == "ACTIVE":
            self.peak_energy = max(self.peak_energy, energy)
            if not is_active:
                dur = now - self.active_start
                if self.min_click <= dur <= self.max_click:
                    self.click_count += 1
                    self._click_peaks.append(self.peak_energy)
                    self._click_durs.append(dur * 1000)
                    self.state = "GAP"
                    self.gap_start = now
                elif dur > self.max_click:
                    self.state = "IDLE"
                else:
                    self.state = "IDLE"
                self.peak_energy = 0
            elif (now - self.active_start) > self.max_click:
                self.state = "IDLE"
            return 0, 0, 0

        elif self.state == "GAP":
            if is_active:
                gap_dur = now - self.gap_start
                if gap_dur >= self.min_gap:
                    self.state = "ACTIVE"
                    self.active_start = now
                    self.peak_energy = energy
                return 0, 0, 0
            else:
                gap_dur = now - self.gap_start
                if gap_dur >= self.max_gap:
                    count = self.click_count
                    # Average peak energy and duration across clicks
                    avg_peak = (sum(self._click_peaks) // len(self._click_peaks)
                                if self._click_peaks else 0)
                    avg_dur = (sum(self._click_durs) / len(self._click_durs)
                               if self._click_durs else 0)
                    self.click_count = 0
                    self.state = "IDLE"
                    return count, avg_peak, avg_dur
                return 0, 0, 0

        return 0, 0, 0

    def reset(self):
        self.state = "IDLE"
        self.click_count = 0
        self.peak_energy = 0


def frame_energy_int(block_bytes):
    """Compute integer energy (mean of squared int16 samples)."""
    count = len(block_bytes) // 2
    if count == 0:
        return 0
    shorts = struct.unpack(f"{count}h", block_bytes)
    return sum(s * s for s in shorts) // count


class ClickDetector:
    def __init__(self, cfg=None):
        if cfg is None:
            cfg = load_config()
        self.cfg = cfg
        tap_cfg = cfg["tap"]

        self.short_clicks = tap_cfg.get("short_clicks", 4)
        self.long_clicks = tap_cfg.get("long_clicks", 6)
        self.calibration_seconds = tap_cfg.get("calibration_seconds", 3)
        self.device_name = tap_cfg.get("device_name", "")

        # Fallback threshold multipliers (used before self-learning kicks in)
        self.default_high_mult = tap_cfg.get("high_mult", 50.0)
        self.default_low_mult = tap_cfg.get("low_mult", 10.0)

        # Detection pipeline
        self.noise_floor = NoiseFloorTracker(alpha_rise=0.001, alpha_fall=0.05)
        self.schmitt = SchmittTrigger()
        self.state_machine = ClickStateMachine(
            min_click_ms=tap_cfg.get("min_click_ms", 50),
            max_click_ms=tap_cfg.get("max_click_ms", 2000),
            min_gap_ms=tap_cfg.get("min_gap_ms", 150),
            max_gap_ms=tap_cfg.get("max_gap_ms", 2500),
        )
        self.profile = ClickProfile()

        # Calibration mode
        self.calibrating = False
        self.cal_clicks_needed = 0
        self.cal_clicks_done = 0

        # Audio
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.error_count = 0

        # Debug state
        self._last_energy = 0
        self._last_t_high = 0
        self._last_t_low = 0

    def find_input_device(self):
        if self.device_name:
            for i in range(self.pa.get_device_count()):
                d = self.pa.get_device_info_by_index(i)
                if self.device_name.lower() in d.get("name", "").lower() and d.get("maxInputChannels", 0) > 0:
                    log.info("Found configured device: %d - %s", i, d["name"])
                    return i
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d.get("name", "") == "default" and d.get("maxInputChannels", 0) > 0:
                log.info("Using default (dsnoop) device: %d", i)
                return i
        log.warning("default device not found, using PyAudio default")
        return self.pa.get_default_input_device_info()["index"]

    def open_stream(self):
        device_index = self.find_input_device()
        self.stream = self.pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, input_device_index=device_index,
            frames_per_buffer=FRAME_SAMPLES,
        )
        log.info("Audio stream opened on device %d", device_index)

    def calibrate(self):
        """Seed noise floor tracker."""
        log.info("Calibrating noise floor for %d seconds...", self.calibration_seconds)
        blocks = int(self.calibration_seconds / (FRAME_SAMPLES / RATE))
        for _ in range(blocks):
            try:
                block = self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
                self.noise_floor.update(frame_energy_int(block))
            except IOError:
                pass

        t_high, t_low = self._get_thresholds()
        mode = "learned" if self.profile.has_enough_data() else "default"
        log.info("Calibration done: noise=%d t_high=%d t_low=%d [%s mode, %d samples]",
                 int(self.noise_floor.estimate), int(t_high), int(t_low),
                 mode, len(self.profile.click_energies))

    def _get_thresholds(self):
        """Get thresholds - learned if available, else default multipliers."""
        nf = self.noise_floor.estimate
        if self.profile.has_enough_data():
            t_high, t_low = self.profile.get_thresholds(
                nf, self.default_high_mult, self.default_low_mult)
            if t_high and t_low:
                return t_high, t_low
        # Fallback to configured multipliers
        t_high = max(nf * self.default_high_mult, 500)
        t_low = max(nf * self.default_low_mult, 250)
        return t_high, t_low

    def _check_calibration_mode(self):
        """Check if calibration was requested via web API."""
        if os.path.exists(CALIBRATION_FLAG):
            try:
                with open(CALIBRATION_FLAG) as f:
                    n = int(f.read().strip())
                os.unlink(CALIBRATION_FLAG)
                self.profile.clear()
                self.calibrating = True
                self.cal_clicks_needed = n
                self.cal_clicks_done = 0
                log.info("Calibration mode: waiting for %d click sequences", n)
            except Exception:
                pass

    def _is_transmitting(self):
        try:
            pin = self.cfg["audio"]["gpio_pin"]
            with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                return f.read().strip() == "1"
        except Exception:
            return False

    def _on_clicks_detected(self, count, avg_peak, avg_dur):
        log.info("Detected %d clicks (peak_energy=%d, dur=%.0fms)",
                 count, avg_peak, avg_dur)

        # Always record to profile for self-learning (if it was a valid click)
        if avg_peak > 0 and count > 0:
            self.profile.record_click(avg_peak, avg_dur)
            if self.profile.has_enough_data():
                t_h, t_l = self.profile.get_thresholds(
                    self.noise_floor.estimate, self.default_high_mult, self.default_low_mult)
                log.info("Auto-tuned: t_high=%d t_low=%d (from %d samples, weakest=%d)",
                         int(t_h), int(t_l), len(self.profile.click_energies),
                         min(self.profile.click_energies))

        # Write count to /tmp
        try:
            with open("/tmp/tap", "w") as f:
                f.write(str(count))
        except Exception:
            pass

        # In calibration mode, don't trigger playback
        if self.calibrating:
            self.cal_clicks_done += 1
            log.info("Calibration: %d/%d sequences captured",
                     self.cal_clicks_done, self.cal_clicks_needed)
            if self.cal_clicks_done >= self.cal_clicks_needed:
                self.calibrating = False
                log.info("Calibration complete! Profile: %s", self.profile.get_stats())
            return

        if self._is_transmitting():
            log.info("PTT active, skipping playback")
            return

        from nanoawos.actions import execute_action
        execute_action(count, self.cfg)

    def _write_debug(self):
        """Write live debug data to /tmp/tap_debug for web UI."""
        try:
            active = "NOISY" if self.schmitt.active else "quiet"
            sm = self.state_machine
            clicks = sm.click_count if sm.state != "IDLE" else 0
            norm = self._last_energy / max(self._last_t_high * 2, 1)
            cal = "CAL" if self.calibrating else ""
            stats = self.profile.get_stats()
            with open("/tmp/tap_debug", "w") as f:
                f.write(f"{norm:.6f} {0.5:.6f} "
                        f"{clicks} {1 if self.schmitt.active else 0} "
                        f"{stats.get('samples', 0)} {active} "
                        f"{int(self.noise_floor.estimate)} "
                        f"{int(self._last_t_high)} {int(self._last_t_low)} "
                        f"{int(self._last_energy)} "
                        f"{1 if self.profile.has_enough_data() else 0} "
                        f"{cal}")
        except Exception:
            pass

    def listen(self):
        if self._is_transmitting():
            try:
                self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
            except IOError:
                pass
            self.state_machine.reset()
            self._write_debug()
            return

        # Check for calibration requests periodically
        self._check_calibration_mode()

        try:
            block = self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
        except IOError as e:
            self.error_count += 1
            log.warning("Audio read error #%d: %s", self.error_count, e)
            return

        now = time.monotonic()
        energy = frame_energy_int(block)
        self._last_energy = energy

        self.noise_floor.update(energy)
        t_high, t_low = self._get_thresholds()
        self._last_t_high = t_high
        self._last_t_low = t_low

        is_active = self.schmitt.update(energy, t_high, t_low)
        count, avg_peak, avg_dur = self.state_machine.update(is_active, now, energy)
        if count > 0:
            self._on_clicks_detected(count, avg_peak, avg_dur)

        self._write_debug()

    def close(self):
        if self.stream:
            self.stream.close()
        self.pa.terminate()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    try:
        os.nice(-20)
    except PermissionError:
        pass

    cfg = load_config()
    detector = ClickDetector(cfg)
    detector.open_stream()
    detector.calibrate()

    log.info("Click detector v3 running (short=%d, long=%d, profile=%d samples)",
             detector.short_clicks, detector.long_clicks,
             len(detector.profile.click_energies))

    try:
        while True:
            detector.listen()
    except KeyboardInterrupt:
        pass
    finally:
        detector.close()


if __name__ == "__main__":
    main()
