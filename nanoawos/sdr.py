"""SDR audio source using RTL-SDR for aviation AM reception.

Replaces the analog audio cable input with digital SDR reception.
Uses rtl_fm for AM demodulation, piped through sox for filtering,
then to the ALSA loopback for Icecast/tap/transcription.

Pipeline: rtl_fm (AM demod) -> sox (HPF + noise + gain) -> aplay (loopback)

The tap detector and transcription service read from the same
ALSA dsnoop/loopback device regardless of analog/SDR mode.
"""

import logging
import os
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
    sdr_cfg = cfg.get("sdr", {})
    audio_cfg = cfg.get("audio", {})

    freq_mhz = sdr_cfg.get("frequency_mhz", 122.5)
    gain = sdr_cfg.get("gain", 40)
    squelch = sdr_cfg.get("squelch", 50)
    ppm = sdr_cfg.get("ppm_correction", 0)
    sample_rate = sdr_cfg.get("sample_rate", 12000)
    device_index = sdr_cfg.get("device_index", 0)

    # Audio processing settings (reuse from analog config)
    cutoff = audio_cfg.get("filter_cutoff_hz", 300)
    noise_amount = audio_cfg.get("noise_reduction", 0.15)
    gain_db = audio_cfg.get("gain_db", 0)
    compand = audio_cfg.get("compand", False)
    noise_prof = "/tmp/noise.prof"

    # Build rtl_fm command
    freq_hz = str(int(freq_mhz * 1e6))
    rtl_cmd = [
        "rtl_fm",
        "-M", "am",
        "-f", freq_hz,
        "-s", str(sample_rate),
        "-g", str(gain),
        "-d", str(device_index),
        "-p", str(ppm),
    ]
    if squelch > 0:
        rtl_cmd += ["-l", str(squelch)]

    # Build sox effects chain (same as audiobridge)
    sox_effects = ["highpass", str(cutoff)]
    if os.path.exists(noise_prof) and noise_amount > 0:
        sox_effects += ["noisered", noise_prof, str(noise_amount)]
    if compand:
        sox_effects += ["compand", "0.3,1", "6:-70,-60,-20", "-5", "-90", "0.2"]
    if gain_db != 0:
        sox_effects += ["gain", str(gain_db)]

    effects_str = " ".join(sox_effects)
    log.info("SDR: rtl_fm AM %.3fMHz (gain=%d squelch=%d ppm=%d rate=%d)",
             freq_mhz, gain, squelch, ppm, sample_rate)
    log.info("SDR: sox [%s] -> loopback hw:1,0", effects_str)

    # Pipeline: rtl_fm -> sox -> aplay (loopback)
    rtl = subprocess.Popen(
        rtl_cmd,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    sox = subprocess.Popen(
        ["sox", "-t", "raw", "-r", str(sample_rate), "-e", "signed", "-b", "16", "-c", "1", "-",
         "-t", "raw", "-r", str(sample_rate), "-e", "signed", "-b", "16", "-c", "1", "-"]
        + sox_effects,
        stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        ["aplay", "-D", "hw:1,0", "-f", "S16_LE",
         "-r", str(sample_rate), "-c", "1", "-t", "raw",
         "--buffer-size", str(sample_rate // 5)],
        stdin=sox.stdout, stderr=subprocess.DEVNULL,
    )

    # Create FIFO for tap detector / transcription to read from
    fifo_path = "/tmp/nanoawos_sdr_audio"
    try:
        os.unlink(fifo_path)
    except FileNotFoundError:
        pass
    os.mkfifo(fifo_path)

    # Tee sox output to both loopback (for DarkIce) and FIFO (for tap/transcribe)
    tee = subprocess.Popen(
        ["tee", fifo_path],
        stdin=sox.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        ["aplay", "-D", "hw:1,0", "-f", "S16_LE",
         "-r", str(sample_rate), "-c", "1", "-t", "raw",
         "--buffer-size", str(sample_rate // 5)],
        stdin=tee.stdout, stderr=subprocess.DEVNULL,
    )

    rtl.stdout.close()
    sox.stdout.close()
    tee.stdout.close()

    log.info("SDR running (rtl=%d sox=%d tee=%d play=%d) fifo=%s",
             rtl.pid, sox.pid, tee.pid, play.pid, fifo_path)

    try:
        play.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in [rtl, sox, tee, play]:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            os.unlink(fifo_path)
        except Exception:
            pass
        log.info("SDR stopped")


if __name__ == "__main__":
    main()
