"""PTT click detector for NanoAWOS.

Listens to the radio audio input and counts PTT button clicks.
4 clicks -> wind only, 6 clicks -> full weather report.

Detection pipeline (based on FAA Pilot-Controlled Lighting approach):
  1. Integer energy computation per audio frame (no float math)
  2. Adaptive noise floor tracking (asymmetric EMA)
  3. Schmitt trigger with hysteresis (dual threshold, prevents toggling)
  4. Debounced state machine with timing constraints (rejects noise spikes,
     voice transmissions, and split-click artifacts)
"""

import logging
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


class NoiseFloorTracker:
    """Adaptive noise floor estimator using asymmetric exponential moving average.

    Tracks the minimum energy level (noise floor) by falling quickly when
    energy drops but rising slowly during signal-present periods.
    """

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

    def thresholds(self, high_mult=6.0, low_mult=3.0, minimum=500):
        """Return Schmitt trigger thresholds relative to noise floor."""
        t_high = max(self.estimate * high_mult, minimum)
        t_low = max(self.estimate * low_mult, minimum * 0.5)
        return t_high, t_low


class SchmittTrigger:
    """Software Schmitt trigger with hysteresis.

    Requires energy to exceed t_high to activate, and fall below t_low
    to deactivate. The gap between thresholds prevents rapid toggling
    when energy hovers near a single threshold.
    """

    def __init__(self):
        self.active = False

    def update(self, energy, t_high, t_low):
        if not self.active and energy > t_high:
            self.active = True
        elif self.active and energy < t_low:
            self.active = False
        return self.active


class ClickStateMachine:
    """Debounced click counter using a timing-constrained state machine.

    States: IDLE -> ACTIVE -> GAP -> (back to ACTIVE or EMIT)

    Timing constraints reject:
      - Noise spikes (duration < min_click_ms)
      - Voice transmissions (duration > max_click_ms)
      - Contact bounce (gap < min_gap_ms)
    """

    def __init__(self, min_click_ms=30, max_click_ms=1500,
                 min_gap_ms=80, max_gap_ms=2000):
        self.min_click = min_click_ms / 1000.0
        self.max_click = max_click_ms / 1000.0
        self.min_gap = min_gap_ms / 1000.0
        self.max_gap = max_gap_ms / 1000.0

        self.state = "IDLE"
        self.click_count = 0
        self.active_start = 0.0
        self.gap_start = 0.0

    def update(self, is_active, now):
        """Feed Schmitt trigger output. Returns click count when sequence ends, else 0."""
        if self.state == "IDLE":
            if is_active:
                self.state = "ACTIVE"
                self.active_start = now
                self.click_count = 0
            return 0

        elif self.state == "ACTIVE":
            if not is_active:
                # Falling edge: click ended
                dur = now - self.active_start
                if self.min_click <= dur <= self.max_click:
                    self.click_count += 1
                    self.state = "GAP"
                    self.gap_start = now
                elif dur > self.max_click:
                    # Too long - voice or continuous signal, discard
                    log.debug("Rejected: duration %.0fms > max %dms",
                              dur * 1000, self.max_click * 1000)
                    self.state = "IDLE"
                else:
                    # Too short - noise spike, discard
                    log.debug("Rejected: duration %.0fms < min %dms",
                              dur * 1000, self.min_click * 1000)
                    self.state = "IDLE"
            elif (now - self.active_start) > self.max_click:
                # Still active but exceeded max duration
                self.state = "IDLE"
            return 0

        elif self.state == "GAP":
            if is_active:
                gap_dur = now - self.gap_start
                if gap_dur >= self.min_gap:
                    # Valid gap followed by new click
                    self.state = "ACTIVE"
                    self.active_start = now
                # else: ignore, gap too short (bounce)
                return 0
            else:
                gap_dur = now - self.gap_start
                if gap_dur >= self.max_gap:
                    # Timeout: emit the count
                    count = self.click_count
                    self.click_count = 0
                    self.state = "IDLE"
                    return count
                return 0

        return 0

    def reset(self):
        self.state = "IDLE"
        self.click_count = 0


def frame_energy_int(block_bytes):
    """Compute integer energy (mean of squared int16 samples). No float math."""
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

        # Detection pipeline components
        self.noise_floor = NoiseFloorTracker(alpha_rise=0.001, alpha_fall=0.05)
        self.schmitt = SchmittTrigger()
        self.state_machine = ClickStateMachine(
            min_click_ms=tap_cfg.get("min_click_ms", 30),
            max_click_ms=tap_cfg.get("max_click_ms", 1500),
            min_gap_ms=tap_cfg.get("min_gap_ms", 80),
            max_gap_ms=tap_cfg.get("max_gap_ms", 2000),
        )

        # Audio
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.error_count = 0

        # Debug state for web UI
        self._last_energy = 0
        self._last_t_high = 0
        self._last_t_low = 0

    def find_input_device(self):
        """Find the best audio input device (ALSA default/dsnoop for shared access)."""
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
        """Seed the adaptive noise floor tracker with ambient noise samples."""
        log.info("Calibrating noise floor for %d seconds...", self.calibration_seconds)
        blocks = int(self.calibration_seconds / (FRAME_SAMPLES / RATE))

        for _ in range(blocks):
            try:
                block = self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
                energy = frame_energy_int(block)
                self.noise_floor.update(energy)
            except IOError:
                pass

        t_high, t_low = self.noise_floor.thresholds()
        log.info("Calibration complete: noise_floor=%d, t_high=%d, t_low=%d",
                 int(self.noise_floor.estimate), int(t_high), int(t_low))

    def _is_transmitting(self):
        try:
            pin = self.cfg["audio"]["gpio_pin"]
            with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                return f.read().strip() == "1"
        except Exception:
            return False

    def _on_clicks_detected(self, count):
        log.info("Detected %d clicks", count)

        try:
            with open("/tmp/tap", "w") as f:
                f.write(str(count))
        except Exception:
            pass

        if self._is_transmitting():
            log.info("PTT active, skipping playback")
            return

        from nanoawos.audio import play_playlist

        if count == self.long_clicks:
            play_playlist("full", self.cfg)
        elif count == self.short_clicks:
            play_playlist("wind", self.cfg)
        else:
            log.info("Ignoring %d clicks (expected %d or %d)",
                     count, self.short_clicks, self.long_clicks)

    def _write_debug(self):
        """Write live debug data to /tmp/tap_debug for web UI."""
        try:
            active = "NOISY" if self.schmitt.active else "quiet"
            sm = self.state_machine
            clicks = sm.click_count if sm.state != "IDLE" else 0
            # Normalize energy to 0-1 range for display (divide by t_high * 2)
            norm = self._last_energy / max(self._last_t_high * 2, 1)
            with open("/tmp/tap_debug", "w") as f:
                f.write(f"{norm:.6f} {0.5:.6f} "
                        f"{clicks} {1 if self.schmitt.active else 0} "
                        f"0 {active}")
        except Exception:
            pass

    def listen(self):
        """Process one audio frame through the full detection pipeline."""
        # Pause during our own playback
        if self._is_transmitting():
            try:
                self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
            except IOError:
                pass
            self.state_machine.reset()
            self._write_debug()
            return

        try:
            block = self.stream.read(FRAME_SAMPLES, exception_on_overflow=False)
        except IOError as e:
            self.error_count += 1
            log.warning("Audio read error #%d: %s", self.error_count, e)
            return

        now = time.monotonic()

        # Layer 1: Integer energy
        energy = frame_energy_int(block)
        self._last_energy = energy

        # Layer 2: Adaptive noise floor -> Schmitt trigger thresholds
        self.noise_floor.update(energy)
        t_high, t_low = self.noise_floor.thresholds()
        self._last_t_high = t_high
        self._last_t_low = t_low

        # Layer 3: Schmitt trigger (hysteresis)
        is_active = self.schmitt.update(energy, t_high, t_low)

        # Layer 4: Debounced click state machine
        count = self.state_machine.update(is_active, now)
        if count > 0:
            self._on_clicks_detected(count)

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
        log.warning("Cannot set high priority (not root)")

    cfg = load_config()
    detector = ClickDetector(cfg)
    detector.open_stream()
    detector.calibrate()

    log.info("Click detector v2 running (short=%d, long=%d)",
             detector.short_clicks, detector.long_clicks)

    try:
        while True:
            detector.listen()
    except KeyboardInterrupt:
        pass
    finally:
        detector.close()


if __name__ == "__main__":
    main()
