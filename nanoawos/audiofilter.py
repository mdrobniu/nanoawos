"""Audio high-pass filter for removing cable buzz and mains hum.

Implements a cascaded 4th-order Butterworth high-pass IIR filter
(two 2nd-order biquad sections). Steeper rolloff than a single
2nd-order for better suppression of broadband cable interference.

Default cutoff: 300Hz (matches aviation VHF radio voice band 300-3400Hz).
"""

import math
import struct


class _Biquad:
    """Single 2nd-order IIR biquad section."""

    def __init__(self, b0, b1, b2, a1, a2):
        self.b0, self.b1, self.b2 = b0, b1, b2
        self.a1, self.a2 = a1, a2
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x):
        y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
             - self.a1 * self.y1 - self.a2 * self.y2)
        self.x2 = self.x1
        self.x1 = x
        self.y2 = self.y1
        self.y1 = y
        return y


def _butterworth_hp_biquad(cutoff_hz, sample_rate, Q=0.7071):
    """Compute 2nd-order Butterworth high-pass biquad coefficients."""
    w0 = 2 * math.pi * cutoff_hz / sample_rate
    alpha = math.sin(w0) / (2 * Q)
    cos_w0 = math.cos(w0)

    b0 = (1 + cos_w0) / 2
    b1 = -(1 + cos_w0)
    b2 = (1 + cos_w0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha

    return _Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class HighPassFilter:
    """4th-order Butterworth high-pass IIR filter for int16 audio.

    Cascades two 2nd-order biquad sections for -24dB/octave rolloff.
    At 300Hz cutoff: 150Hz is attenuated ~24dB, 50Hz is attenuated ~48dB.
    """

    def __init__(self, cutoff_hz=300, sample_rate=44100):
        # 4th-order Butterworth: two biquads with specific Q values
        # Q values for 4th-order: 0.5412 and 1.3066
        self.stages = [
            _butterworth_hp_biquad(cutoff_hz, sample_rate, Q=0.5412),
            _butterworth_hp_biquad(cutoff_hz, sample_rate, Q=1.3066),
        ]

    def process_block(self, block_bytes):
        count = len(block_bytes) // 2
        if count == 0:
            return block_bytes

        samples = list(struct.unpack(f"<{count}h", block_bytes))

        for i in range(count):
            x = float(samples[i])
            for stage in self.stages:
                x = stage.process(x)
            samples[i] = max(-32768, min(32767, int(x)))

        return struct.pack(f"<{count}h", *samples)
