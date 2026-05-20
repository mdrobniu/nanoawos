"""Audio bridge: reads from mic (dsnoop), filters, writes to ALSA loopback.

This daemon sits between the microphone and DarkIce/Icecast, applying
a high-pass filter to remove 50Hz mains hum from the audio cable.

Pipeline: dsnoop (hw:2,0 shared) -> high-pass filter -> hw:1,0 (loopback write)
DarkIce reads from: hw:1,1 (loopback read) -> clean audio -> Icecast
"""

import logging
import os
import signal
import subprocess
import sys

from nanoawos.audiofilter import HighPassFilter
from nanoawos.config import load_config

log = logging.getLogger(__name__)

RATE = 44100
CHANNELS = 1
CHUNK_FRAMES = 2205  # 50ms


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGALRM, signal.SIG_IGN)

    cfg = load_config()
    cutoff = cfg.get("audio", {}).get("filter_cutoff_hz", 150)

    hpf = HighPassFilter(cutoff_hz=cutoff, sample_rate=RATE)
    chunk_bytes = CHUNK_FRAMES * 2  # 16-bit mono

    log.info("Audio bridge starting: dsnoop -> HPF@%dHz -> loopback", cutoff)

    # Use arecord/aplay piped together with Python filter in between.
    # This avoids PyAudio device enumeration issues with loopback.
    rec = subprocess.Popen(
        ["arecord", "-D", "default", "-f", "S16_LE",
         "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw",
         "--buffer-size", str(CHUNK_FRAMES * 4)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        ["aplay", "-D", "hw:1,0", "-f", "S16_LE",
         "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw",
         "--buffer-size", str(CHUNK_FRAMES * 4)],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    log.info("Audio bridge running (PID rec=%d play=%d)", rec.pid, play.pid)

    try:
        while True:
            data = rec.stdout.read(chunk_bytes)
            if not data:
                log.warning("arecord ended, restarting...")
                break
            filtered = hpf.process_block(data)
            try:
                play.stdin.write(filtered)
            except BrokenPipeError:
                log.warning("aplay ended, restarting...")
                break
    except KeyboardInterrupt:
        pass
    finally:
        rec.terminate()
        play.terminate()
        log.info("Audio bridge stopped")


if __name__ == "__main__":
    main()
