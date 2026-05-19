"""Radio transmission transcription service for NanoAWOS.

Listens to the radio audio input, detects voice transmissions,
records them, sends to OpenAI Whisper for STT, and optionally
extracts actionable items via GPT.
"""

import io
import json
import logging
import math
import os
import signal
import struct
import sys
import time
import wave
from datetime import datetime, timezone

import pyaudio
import requests

from nanoawos.config import load_config

log = logging.getLogger(__name__)

SHORT_NORMALIZE = 1.0 / 32768.0
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
BLOCK_TIME = 0.05
FRAMES_PER_BLOCK = int(RATE * BLOCK_TIME)


def get_rms(block):
    count = len(block) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", block)
    sum_sq = sum((s * SHORT_NORMALIZE) ** 2 for s in shorts)
    return math.sqrt(sum_sq / count)


class TranscriptionService:
    def __init__(self, cfg=None):
        if cfg is None:
            cfg = load_config()
        self.cfg = cfg
        tc = cfg["transcribe"]

        self.api_key = tc["openai_api_key"]
        self.model = tc.get("model", "whisper-1")
        self.language = tc.get("language", "en")
        self.min_duration = tc.get("min_duration_sec", 0.5)
        self.max_duration = tc.get("max_duration_sec", 60)
        self.silence_blocks = tc.get("silence_blocks", 15)
        self.log_file = tc.get("log_file", "/tmp/nanoawos_transcriptions.json")
        self.max_log_entries = tc.get("max_log_entries", 200)
        self.extract_actions = tc.get("extract_actions", True)
        self.action_model = tc.get("action_model", "gpt-4o-mini")
        self.action_prompt = tc.get("action_prompt", "Extract actionable items from this radio transmission. Be brief.")

        # Audio state
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.threshold = 0.05
        self.recording = False
        self.audio_buffer = []
        self.silence_count = 0
        self.record_start = 0

    def find_default_device(self):
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d.get("name", "") == "default" and d.get("maxInputChannels", 0) > 0:
                return i
        return self.pa.get_default_input_device_info()["index"]

    def open_stream(self):
        idx = self.find_default_device()
        self.stream = self.pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, input_device_index=idx,
            frames_per_buffer=FRAMES_PER_BLOCK,
        )
        log.info("Transcription stream opened on device %d", idx)

    def calibrate(self, seconds=2):
        log.info("Calibrating noise floor...")
        samples = []
        for _ in range(int(seconds / BLOCK_TIME)):
            try:
                block = self.stream.read(FRAMES_PER_BLOCK, exception_on_overflow=False)
                samples.append(get_rms(block))
            except IOError:
                pass
        if samples:
            self.threshold = max(max(samples) * 3.0, 0.05)
            log.info("Calibrated threshold: %.4f (max ambient: %.4f)", self.threshold, max(samples))

    def _is_transmitting(self):
        """Check if our PTT is active (we're transmitting, not receiving)."""
        try:
            pin = self.cfg["audio"]["gpio_pin"]
            with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                return f.read().strip() == "1"
        except Exception:
            return False

    def _buffer_to_wav(self):
        """Convert audio buffer to WAV bytes."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(self.pa.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            for chunk in self.audio_buffer:
                wf.writeframes(chunk)
        return buf.getvalue()

    def _transcribe_audio(self, wav_bytes):
        """Send WAV to OpenAI Whisper API, return transcription text.

        If language is "auto" or empty, Whisper auto-detects the language.
        Supports mixed English/Polish radio communications.
        """
        try:
            data = {"model": self.model}
            # Only set language if explicitly configured (not "auto" or empty)
            if self.language and self.language != "auto":
                data["language"] = self.language
            resp = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": ("radio.wav", wav_bytes, "audio/wav")},
                data=data,
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            return text
        except Exception as e:
            log.error("Whisper API error: %s", e)
            return None

    def _extract_action(self, text):
        """Send transcription to GPT to extract actionable items."""
        if not self.extract_actions or not text:
            return None
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.action_model,
                    "messages": [
                        {"role": "system", "content": self.action_prompt},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 150,
                    "temperature": 0.3,
                },
                timeout=15,
            )
            resp.raise_for_status()
            action = resp.json()["choices"][0]["message"]["content"].strip()
            return action if action.lower() != "no action" else None
        except Exception as e:
            log.error("GPT action extraction error: %s", e)
            return None

    def _save_entry(self, entry):
        """Append transcription entry to log file."""
        entries = []
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, IOError):
                entries = []

        entries.append(entry)

        # Trim to max entries
        if len(entries) > self.max_log_entries:
            entries = entries[-self.max_log_entries:]

        with open(self.log_file, "w") as f:
            json.dump(entries, f, indent=2)

        # Also write latest for quick access
        with open("/tmp/nanoawos_last_transcription", "w") as f:
            json.dump(entry, f)

    def _process_recording(self):
        """Process a completed recording: transcribe and extract actions."""
        duration = time.time() - self.record_start
        if duration < self.min_duration:
            log.debug("Recording too short (%.1fs < %.1fs), skipping",
                      duration, self.min_duration)
            self.audio_buffer = []
            return

        log.info("Processing %.1fs recording...", duration)
        wav_bytes = self._buffer_to_wav()
        self.audio_buffer = []

        text = self._transcribe_audio(wav_bytes)
        if not text:
            log.info("No transcription returned")
            return

        log.info("Transcription: %s", text)

        action = self._extract_action(text)
        if action:
            log.info("Action: %s", action)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_sec": round(duration, 1),
            "text": text,
            "action": action,
        }
        self._save_entry(entry)

    def listen(self):
        """Process one audio block."""
        # Skip when we're transmitting (playing weather)
        if self._is_transmitting():
            try:
                self.stream.read(FRAMES_PER_BLOCK, exception_on_overflow=False)
            except IOError:
                pass
            if self.recording:
                self.audio_buffer = []
                self.recording = False
            return

        try:
            block = self.stream.read(FRAMES_PER_BLOCK, exception_on_overflow=False)
        except IOError:
            return

        amplitude = get_rms(block)

        if amplitude > self.threshold:
            # Voice detected
            if not self.recording:
                self.recording = True
                self.record_start = time.time()
                self.audio_buffer = []
                log.debug("Recording started")
            self.audio_buffer.append(block)
            self.silence_count = 0

            # Check max duration
            if time.time() - self.record_start > self.max_duration:
                log.info("Max recording duration reached")
                self.recording = False
                self._process_recording()
        else:
            if self.recording:
                self.audio_buffer.append(block)
                self.silence_count += 1
                if self.silence_count >= self.silence_blocks:
                    # End of transmission
                    self.recording = False
                    self._process_recording()

    def close(self):
        if self.stream:
            self.stream.close()
        self.pa.terminate()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    signal.signal(signal.SIGALRM, signal.SIG_IGN)

    cfg = load_config()
    tc = cfg.get("transcribe", {})

    if not tc.get("enabled"):
        log.error("Transcription service is disabled in config")
        sys.exit(1)

    if not tc.get("openai_api_key"):
        log.error("transcribe.openai_api_key not set in config")
        sys.exit(1)

    svc = TranscriptionService(cfg)
    svc.open_stream()
    svc.calibrate()

    log.info("Transcription service running (threshold=%.4f, min=%.1fs, max=%.1fs)",
             svc.threshold, svc.min_duration, svc.max_duration)

    try:
        while True:
            svc.listen()
    except KeyboardInterrupt:
        pass
    finally:
        svc.close()


if __name__ == "__main__":
    main()
