"""PTT click detector for NanoAWOS.

Listens to the radio audio input and counts PTT button clicks.
4 clicks -> wind only, 6 clicks -> full weather report.
"""

import logging
import math
import os
import signal
import struct
import subprocess
import sys
import time

import pyaudio

from nanoawos.config import load_config

log = logging.getLogger(__name__)

SHORT_NORMALIZE = 1.0 / 32768.0
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
INPUT_BLOCK_TIME = 0.05
INPUT_FRAMES_PER_BLOCK = int(RATE * INPUT_BLOCK_TIME)


def get_rms(block):
    """Calculate RMS amplitude of an audio block."""
    count = len(block) // 2
    shorts = struct.unpack(f"{count}h", block)
    sum_squares = sum((s * SHORT_NORMALIZE) ** 2 for s in shorts)
    return math.sqrt(sum_squares / count) if count > 0 else 0.0


class ClickDetector:
    def __init__(self, cfg=None):
        if cfg is None:
            cfg = load_config()
        self.cfg = cfg
        tap_cfg = cfg["tap"]

        self.threshold = tap_cfg.get("amplitude_threshold", 0.2)
        self.quiet_blocks = tap_cfg.get("quiet_blocks", 25)
        self.noisy_min_blocks = tap_cfg.get("noisy_min_blocks", 1)
        self.short_clicks = tap_cfg.get("short_clicks", 4)
        self.long_clicks = tap_cfg.get("long_clicks", 6)
        self.calibration_seconds = tap_cfg.get("calibration_seconds", 2)
        self.device_name = tap_cfg.get("device_name", "")

        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.noisy_count = 0
        self.quiet_count = 0
        self.click_count = 0
        self.error_count = 0

    def find_input_device(self):
        """Find the best audio input device.

        Uses the ALSA 'default' device (dsnoop via asound.conf) for shared
        access with DarkIce. If a specific device_name is configured, uses that.
        """
        # If a specific device name is configured, find it
        if self.device_name:
            for i in range(self.pa.get_device_count()):
                devinfo = self.pa.get_device_info_by_index(i)
                name = devinfo.get("name", "")
                if self.device_name.lower() in name.lower() and devinfo.get("maxInputChannels", 0) > 0:
                    log.info("Found configured device: %d - %s", i, name)
                    return i

        # Use the default device (dsnoop shared capture via asound.conf)
        # This allows both tap detector and DarkIce to share the mic
        for i in range(self.pa.get_device_count()):
            devinfo = self.pa.get_device_info_by_index(i)
            if devinfo.get("name", "") == "default" and devinfo.get("maxInputChannels", 0) > 0:
                log.info("Using default (dsnoop) device: %d", i)
                return i

        # Last resort
        log.warning("default device not found, using PyAudio default")
        return self.pa.get_default_input_device_info()["index"]

    def open_stream(self):
        """Open the audio input stream."""
        device_index = self.find_input_device()
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=INPUT_FRAMES_PER_BLOCK,
        )
        log.info("Audio stream opened on device %d", device_index)

    def calibrate(self):
        """Sample ambient noise to set threshold adaptively."""
        if self.calibration_seconds <= 0:
            log.info("Calibration disabled, using threshold %.3f", self.threshold)
            return

        log.info("Calibrating noise floor for %d seconds...", self.calibration_seconds)
        samples = []
        blocks = int(self.calibration_seconds / INPUT_BLOCK_TIME)

        for _ in range(blocks):
            try:
                block = self.stream.read(INPUT_FRAMES_PER_BLOCK, exception_on_overflow=False)
                rms = get_rms(block)
                samples.append(rms)
            except IOError:
                pass

        if samples:
            avg_noise = sum(samples) / len(samples)
            max_noise = max(samples)
            # Set threshold at 3x the max ambient noise, minimum 0.05
            new_threshold = max(max_noise * 3.0, 0.05)
            log.info("Calibration: avg=%.4f, max=%.4f, threshold=%.4f -> %.4f",
                     avg_noise, max_noise, self.threshold, new_threshold)
            self.threshold = new_threshold

    def _is_transmitting(self):
        """Check if PTT relay is currently active."""
        try:
            pin = self.cfg["audio"]["gpio_pin"]
            with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                return f.read().strip() == "1"
        except Exception:
            return False

    def _on_clicks_detected(self, count):
        """Handle detected click pattern."""
        log.info("Detected %d clicks", count)

        # Write click count to /tmp for OLED display
        try:
            with open("/tmp/tap", "w") as f:
                f.write(str(count))
        except Exception:
            pass

        # Don't play if currently transmitting
        if self._is_transmitting():
            log.info("PTT active, skipping playback")
            return

        from nanoawos.audio import play_playlist

        if count == self.long_clicks:
            play_playlist("full", self.cfg)
        elif count == self.short_clicks:
            play_playlist("wind", self.cfg)
        else:
            log.debug("Ignoring %d clicks (not %d or %d)",
                      count, self.short_clicks, self.long_clicks)

    def _write_debug(self, amplitude, is_noisy):
        """Write live tap debug data to /tmp/tap_debug for web UI."""
        try:
            state = "NOISY" if is_noisy else "quiet"
            with open("/tmp/tap_debug", "w") as f:
                f.write(f"{amplitude:.6f} {self.threshold:.6f} "
                        f"{self.click_count} {self.noisy_count} "
                        f"{self.quiet_count} {state}")
        except Exception:
            pass

    def listen(self):
        """Process one audio block. Call in a loop."""
        # Pause click counting during playback to avoid false triggers
        if self._is_transmitting():
            # Drain the audio buffer but don't process
            try:
                self.stream.read(INPUT_FRAMES_PER_BLOCK, exception_on_overflow=False)
            except IOError:
                pass
            # Reset state so we start fresh after playback
            self.noisy_count = 0
            self.quiet_count = 0
            self.click_count = 0
            self._write_debug(0.0, False)
            return

        try:
            block = self.stream.read(INPUT_FRAMES_PER_BLOCK, exception_on_overflow=False)
        except IOError as e:
            self.error_count += 1
            log.warning("Audio read error #%d: %s", self.error_count, e)
            self.noisy_count = 1
            return

        amplitude = get_rms(block)

        if amplitude > self.threshold:
            # Noisy block (radio signal)
            self.quiet_count = 0
            self.noisy_count += 1
            self._write_debug(amplitude, True)
        else:
            # Quiet block
            self.quiet_count += 1
            if self.noisy_count > self.noisy_min_blocks:
                self.click_count += 1
                log.debug("Click registered (total: %d)", self.click_count)

            if self.quiet_count >= self.quiet_blocks and self.click_count > 0:
                self._on_clicks_detected(self.click_count)
                self.click_count = 0

            self.noisy_count = 0
            self._write_debug(amplitude, False)

    def close(self):
        if self.stream:
            self.stream.close()
        self.pa.terminate()


def main():
    """Entry point for click detector daemon."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Ignore SIGALRM from NanoHatOLED
    signal.signal(signal.SIGALRM, signal.SIG_IGN)

    # Run at high priority for reliable audio capture
    try:
        os.nice(-20)
    except PermissionError:
        log.warning("Cannot set high priority (not root)")

    cfg = load_config()
    detector = ClickDetector(cfg)
    detector.open_stream()
    detector.calibrate()

    log.info("Click detector running (threshold=%.4f, short=%d, long=%d)",
             detector.threshold, detector.short_clicks, detector.long_clicks)

    try:
        while True:
            detector.listen()
    except KeyboardInterrupt:
        pass
    finally:
        detector.close()


if __name__ == "__main__":
    main()
