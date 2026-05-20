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
    rate = 44100

    noise_prof = "/tmp/noise.prof"
    noise_amount = cfg.get("audio", {}).get("noise_reduction", 0.3)

    import os
    has_noise_prof = os.path.exists(noise_prof)

    # Build sox effects chain
    sox_effects = ["highpass", str(cutoff)]
    if has_noise_prof:
        sox_effects += ["noisered", noise_prof, str(noise_amount)]
        log.info("Audio bridge: dsnoop -> HPF@%dHz + noisered(%.1f) -> loopback",
                 cutoff, noise_amount)
    else:
        log.info("Audio bridge: dsnoop -> HPF@%dHz -> loopback (no noise profile)",
                 cutoff)
        log.info("To enable noise reduction: capture profile with "
                 "arecord -D default -f S16_LE -r 44100 -c 1 -d 2 /tmp/noise_sample.wav "
                 "&& sox /tmp/noise_sample.wav -n noiseprof /tmp/noise.prof")

    rec = subprocess.Popen(
        ["arecord", "-D", "default", "-f", "S16_LE",
         "-r", str(rate), "-c", "1", "-t", "raw",
         "--buffer-size", "8820"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    sox = subprocess.Popen(
        ["sox", "-t", "raw", "-r", str(rate), "-e", "signed", "-b", "16", "-c", "1", "-",
         "-t", "raw", "-r", str(rate), "-e", "signed", "-b", "16", "-c", "1", "-"]
        + sox_effects,
        stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        ["aplay", "-D", "hw:1,0", "-f", "S16_LE",
         "-r", str(rate), "-c", "1", "-t", "raw",
         "--buffer-size", "8820"],
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
