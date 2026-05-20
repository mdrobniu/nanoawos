"""Audio high-pass filter for removing mains hum and cable buzz.

Implements a simple 2nd-order Butterworth high-pass IIR filter.
Applied to audio blocks in-place, zero-latency (no FFT needed).

Typical use: filter out 50Hz mains hum and harmonics from audio cable.
Default cutoff: 150Hz (removes all buzz, preserves voice at 300Hz+).
"""

import math
import struct


class HighPassFilter:
    """2nd-order Butterworth high-pass IIR filter for int16 audio.

    Processes audio blocks in-place with minimal CPU overhead.
    Maintains state between blocks for continuous filtering.
    """

    def __init__(self, cutoff_hz=150, sample_rate=44100):
        """Initialize filter coefficients.

        Args:
            cutoff_hz: Cutoff frequency in Hz (default 150, removes 50Hz hum)
            sample_rate: Audio sample rate in Hz
        """
        # Butterworth 2nd-order high-pass coefficients
        w0 = 2 * math.pi * cutoff_hz / sample_rate
        alpha = math.sin(w0) / (2 * 0.7071)  # Q = 0.7071 for Butterworth

        b0 = (1 + math.cos(w0)) / 2
        b1 = -(1 + math.cos(w0))
        b2 = (1 + math.cos(w0)) / 2
        a0 = 1 + alpha
        a1 = -2 * math.cos(w0)
        a2 = 1 - alpha

        # Normalize by a0
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

        # Filter state (previous samples)
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0

    def process_block(self, block_bytes):
        """Apply high-pass filter to a block of int16 audio bytes.

        Args:
            block_bytes: Raw audio bytes (int16 little-endian)

        Returns:
            Filtered audio bytes (same format)
        """
        count = len(block_bytes) // 2
        if count == 0:
            return block_bytes

        samples = list(struct.unpack(f"<{count}h", block_bytes))

        for i in range(count):
            x = float(samples[i])
            y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
                 - self.a1 * self.y1 - self.a2 * self.y2)

            self.x2 = self.x1
            self.x1 = x
            self.y2 = self.y1
            self.y1 = y

            # Clamp to int16 range
            samples[i] = max(-32768, min(32767, int(y)))

        return struct.pack(f"<{count}h", *samples)
