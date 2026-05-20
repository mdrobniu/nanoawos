"""Audio high-pass filter for removing cable buzz and mains hum.

Uses scipy.signal.sosfilt if available (fastest, C-native).
Falls back to numpy vectorized IIR (fast).
Last resort: pure Python per-sample (slow, only for small blocks).

Default cutoff: 300Hz (matches aviation VHF radio voice band 300-3400Hz).
"""

import math
import struct

import numpy as np


def _butter_highpass_sos(cutoff_hz, sample_rate, order=4):
    """Compute SOS (second-order sections) for Butterworth high-pass.

    Uses scipy if available, otherwise manual biquad computation.
    """
    try:
        from scipy.signal import butter
        sos = butter(order, cutoff_hz, btype='high', fs=sample_rate, output='sos')
        return sos
    except ImportError:
        pass

    # Manual 4th-order: two biquad sections with Butterworth Q values
    sections = []
    for Q in [0.5412, 1.3066]:
        w0 = 2 * math.pi * cutoff_hz / sample_rate
        alpha = math.sin(w0) / (2 * Q)
        cos_w0 = math.cos(w0)
        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
        sections.append([b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0])
    return np.array(sections)


class HighPassFilter:
    """4th-order Butterworth high-pass filter using numpy/scipy.

    Optimized for real-time ARM processing with vectorized operations.
    """

    def __init__(self, cutoff_hz=300, sample_rate=44100):
        self.sos = _butter_highpass_sos(cutoff_hz, sample_rate, order=4)
        # Filter state for continuity between blocks
        self._zi = np.zeros((self.sos.shape[0], 2))
        self._use_scipy = False
        try:
            from scipy.signal import sosfilt, sosfilt_zi
            # Initialize steady-state for the filter
            self._zi = sosfilt_zi(self.sos) * 0  # start from zero
            self._use_scipy = True
        except ImportError:
            pass

    def process_block(self, block_bytes):
        count = len(block_bytes) // 2
        if count == 0:
            return block_bytes

        samples = np.frombuffer(block_bytes, dtype=np.int16).astype(np.float64)

        if self._use_scipy:
            from scipy.signal import sosfilt
            filtered, self._zi = sosfilt(self.sos, samples, zi=self._zi)
        else:
            filtered = self._sosfilt_numpy(samples)

        clipped = np.clip(filtered, -32768, 32767).astype(np.int16)
        return clipped.tobytes()

    def _sosfilt_numpy(self, x):
        """Manual SOS filter using numpy (no scipy needed)."""
        y = x.copy()
        for s in range(self.sos.shape[0]):
            b0, b1, b2 = self.sos[s, 0], self.sos[s, 1], self.sos[s, 2]
            a1, a2 = self.sos[s, 4], self.sos[s, 5]
            z1, z2 = self._zi[s, 0], self._zi[s, 1]
            out = np.empty_like(y)
            for i in range(len(y)):
                xi = y[i]
                yi = b0 * xi + z1
                z1 = b1 * xi - a1 * yi + z2
                z2 = b2 * xi - a2 * yi
                out[i] = yi
            self._zi[s, 0] = z1
            self._zi[s, 1] = z2
            y = out
        return y
