"""Tests for nanoawos/audiofilter.py."""

import math
import struct
import pytest
from nanoawos.audiofilter import HighPassFilter


def _generate_tone(freq_hz, duration_sec=0.1, sample_rate=44100, amplitude=10000):
    """Generate a pure sine wave as int16 bytes."""
    n = int(sample_rate * duration_sec)
    samples = [int(amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _rms(block_bytes):
    count = len(block_bytes) // 2
    shorts = struct.unpack(f"<{count}h", block_bytes)
    return math.sqrt(sum(s * s for s in shorts) / count)


class TestHighPassFilter:

    def test_attenuates_50hz_hum(self):
        """50Hz signal should be heavily attenuated."""
        hpf = HighPassFilter(cutoff_hz=150, sample_rate=44100)
        tone = _generate_tone(50, duration_sec=0.5)
        # Warm up filter
        hpf.process_block(tone)
        filtered = hpf.process_block(tone)
        assert _rms(filtered) < _rms(tone) * 0.15  # >85% reduction

    def test_passes_voice_frequencies(self):
        """1kHz signal (voice range) should pass through with minimal loss."""
        hpf = HighPassFilter(cutoff_hz=150, sample_rate=44100)
        tone = _generate_tone(1000, duration_sec=0.5)
        hpf.process_block(tone)
        filtered = hpf.process_block(tone)
        assert _rms(filtered) > _rms(tone) * 0.85  # <15% loss

    def test_attenuates_dc_offset(self):
        """DC (0Hz) should be completely removed."""
        hpf = HighPassFilter(cutoff_hz=150, sample_rate=44100)
        # Constant signal = DC
        n = 4410
        dc = struct.pack(f"<{n}h", *([5000] * n))
        hpf.process_block(dc)
        filtered = hpf.process_block(dc)
        assert _rms(filtered) < 500  # nearly zero

    def test_empty_block(self):
        hpf = HighPassFilter()
        assert hpf.process_block(b"") == b""

    def test_maintains_state_across_blocks(self):
        """Filter state persists between calls (no clicks at block boundaries)."""
        hpf = HighPassFilter(cutoff_hz=150, sample_rate=44100)
        tone = _generate_tone(1000, duration_sec=0.1)
        r1 = _rms(hpf.process_block(tone))
        r2 = _rms(hpf.process_block(tone))
        # Second block should be similar (no transient from state reset)
        assert abs(r1 - r2) / max(r1, r2) < 0.2

    def test_clamps_to_int16_range(self):
        """Output samples stay within int16 range."""
        hpf = HighPassFilter(cutoff_hz=150, sample_rate=44100)
        # Max amplitude signal
        tone = _generate_tone(500, amplitude=32000)
        filtered = hpf.process_block(tone)
        count = len(filtered) // 2
        samples = struct.unpack(f"<{count}h", filtered)
        assert all(-32768 <= s <= 32767 for s in samples)
