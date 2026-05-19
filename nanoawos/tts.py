"""Text-to-Speech engine for NanoAWOS.

Supports three engines:
  - "piper": Local Piper neural TTS (offline, low latency)
  - "cloud": Cloud AI TTS via OpenAI API (high quality, requires internet)
  - "wav_concat": Legacy WAV file concatenation (offline, robotic)
"""

import logging
import os
import subprocess
import wave

from nanoawos.config import load_config

log = logging.getLogger(__name__)


def synthesize(text, output_path, cfg=None):
    """Synthesize text to WAV file. Returns output path on success."""
    if cfg is None:
        cfg = load_config()

    engine = cfg["tts"].get("engine", "piper")
    tmp_path = output_path + ".tmp"

    try:
        if engine == "piper":
            _synthesize_piper(text, tmp_path, cfg)
        elif engine == "cloud":
            _synthesize_cloud(text, tmp_path, cfg)
        elif engine == "wav_concat":
            _synthesize_wav_concat(text, tmp_path, cfg)
        else:
            raise ValueError(f"Unknown TTS engine: {engine}")

        # Atomic rename
        os.rename(tmp_path, output_path)
        log.info("TTS [%s]: %s -> %s", engine, text[:60], output_path)
        return output_path

    except Exception as e:
        log.error("TTS synthesis failed (%s): %s", engine, e)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _synthesize_piper(text, output_path, cfg):
    """Synthesize using local Piper TTS."""
    from piper.voice import PiperVoice

    model_path = cfg["tts"]["piper_model"]
    voice = PiperVoice.load(model_path)
    wav_file = wave.open(output_path, "w")
    voice.synthesize(text, wav_file)
    wav_file.close()


def _synthesize_cloud(text, output_path, cfg):
    """Synthesize using OpenAI TTS API.

    Config:
      tts:
        engine: "cloud"
        cloud_api_key: "sk-..."
        cloud_voice: "nova"       # alloy, echo, fable, onyx, nova, shimmer
        cloud_model: "tts-1"      # tts-1 or tts-1-hd
        cloud_api_url: "https://api.openai.com/v1/audio/speech"  # optional override
    """
    import requests

    api_key = cfg["tts"].get("cloud_api_key", "")
    if not api_key:
        raise ValueError("tts.cloud_api_key not set in config")

    voice = cfg["tts"].get("cloud_voice", "nova")
    model = cfg["tts"].get("cloud_model", "tts-1")
    api_url = cfg["tts"].get("cloud_api_url", "https://api.openai.com/v1/audio/speech")

    resp = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "wav",
        },
        timeout=30,
    )
    resp.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(resp.content)


def _synthesize_wav_concat(text, output_path, cfg):
    """Legacy WAV concatenation using pre-recorded phonetic words."""
    import struct

    tts_dir = "/usr/local/tts"
    words = text.lower().split()

    # Map special characters
    word_map = {"-": "minus", ",": None, ".": "decimal"}

    wav_data = []
    sample_rate = None
    sample_width = None
    channels = None

    for word in words:
        mapped = word_map.get(word, word)
        if mapped is None:
            continue

        wav_path = os.path.join(tts_dir, f"{mapped}.wav")
        if not os.path.exists(wav_path):
            log.warning("WAV not found: %s", wav_path)
            continue

        with wave.open(wav_path, "rb") as wf:
            if sample_rate is None:
                sample_rate = wf.getframerate()
                sample_width = wf.getsampwidth()
                channels = wf.getnchannels()
            wav_data.append(wf.readframes(wf.getnframes()))

    if not wav_data:
        raise ValueError("No WAV data produced")

    with wave.open(output_path, "w") as out:
        out.setnchannels(channels)
        out.setsampwidth(sample_width)
        out.setframerate(sample_rate)
        for chunk in wav_data:
            out.writeframes(chunk)
