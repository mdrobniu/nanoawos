"""Audio bridge: reads from mic (dsnoop), filters with sox, writes to ALSA loopback.

Uses sox for C-native high-pass filtering -- near-zero CPU on ARM.

Pipeline: arecord (dsnoop) -> sox (high-pass) -> aplay (loopback)
DarkIce reads from loopback -> clean audio -> Icecast
"""

import logging
import signal
import subprocess
import sys

from nanoawos.config import load_config

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGALRM, signal.SIG_IGN)

    cfg = load_config()
    cutoff = cfg.get("audio", {}).get("filter_cutoff_hz", 300)
    input_rate = 44100   # dsnoop captures at 44100
    output_rate = 48000  # loopback must be 48000 for DarkIce Opus (no ALSA resample in loopback)

    noise_prof = "/tmp/noise.prof"
    noise_amount = cfg.get("audio", {}).get("noise_reduction", 0.3)

    import os
    has_noise_prof = os.path.exists(noise_prof)

    gain_db = cfg.get("audio", {}).get("gain_db", 0)
    normalize = cfg.get("audio", {}).get("normalize", False)
    compand = cfg.get("audio", {}).get("compand", False)

    # Build sox effects chain
    sox_effects = ["highpass", str(cutoff)]
    if has_noise_prof:
        sox_effects += ["noisered", noise_prof, str(noise_amount)]
    if compand:
        # Compressor: boosts quiet voice, limits loud peaks
        sox_effects += ["compand", "0.3,1", "6:-70,-60,-20", "-5", "-90", "0.2"]
    if gain_db != 0:
        sox_effects += ["gain", str(gain_db)]
    if normalize:
        sox_effects += ["norm", "-1"]  # normalize to -1dB headroom

    effects_str = " ".join(sox_effects)
    log.info("Audio bridge: dsnoop@%d -> sox [%s] -> loopback@%d",
             input_rate, effects_str, output_rate)

    rec = subprocess.Popen(
        ["arecord", "-D", "default", "-f", "S16_LE",
         "-r", str(input_rate), "-c", "1", "-t", "raw",
         "--buffer-size", "8820"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    # Sox reads 44100, applies effects, outputs 48000 (resamples internally)
    sox = subprocess.Popen(
        ["sox", "-t", "raw", "-r", str(input_rate), "-e", "signed", "-b", "16", "-c", "1", "-",
         "-t", "raw", "-r", str(output_rate), "-e", "signed", "-b", "16", "-c", "1", "-"]
        + sox_effects,
        stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        ["aplay", "-D", "hw:1,0", "-f", "S16_LE",
         "-r", str(output_rate), "-c", "1", "-t", "raw",
         "--buffer-size", str(output_rate // 5)],
        stdin=sox.stdout, stderr=subprocess.DEVNULL,
    )

    # Close our copies of the pipe fds so signals propagate
    rec.stdout.close()
    sox.stdout.close()

    log.info("Audio bridge running (rec=%d sox=%d play=%d)", rec.pid, sox.pid, play.pid)

    try:
        play.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in [rec, sox, play]:
            try:
                p.terminate()
            except Exception:
                pass
        log.info("Audio bridge stopped")


if __name__ == "__main__":
    main()
