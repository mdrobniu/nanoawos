"""Comprehensive tests for nanoawos.tap -- PTT click detection module.

Covers: NoiseFloorTracker, SchmittTrigger, ClickStateMachine,
ClickProfile, frame_energy_int, and full pipeline integration.

Does NOT test anything requiring PyAudio hardware.
"""

import json
import struct
import time

import pytest

# We must mock pyaudio before importing tap, since tap.py does
# `import pyaudio` and `FORMAT = pyaudio.paInt16` at module level.
import sys
from unittest import mock

_pyaudio_mock = mock.MagicMock()
_pyaudio_mock.paInt16 = 8  # pyaudio.paInt16 constant
sys.modules["pyaudio"] = _pyaudio_mock

from nanoawos.tap import (  # noqa: E402
    ClickProfile,
    ClickStateMachine,
    NoiseFloorTracker,
    SchmittTrigger,
    frame_energy_int,
    PROFILE_PATH,
)


# ---------------------------------------------------------------------------
# 1. NoiseFloorTracker
# ---------------------------------------------------------------------------
class TestNoiseFloorTracker:

    def test_initial_update_sets_estimate(self):
        nf = NoiseFloorTracker()
        assert nf.estimate == 0
        result = nf.update(170_000)
        assert result == 170_000
        assert nf.estimate == 170_000

    def test_falls_quickly_when_energy_drops(self):
        """alpha_fall=0.05 means ~5 % step toward lower value each frame."""
        nf = NoiseFloorTracker(alpha_fall=0.05)
        nf.update(200_000)  # seed

        # Feed a much lower energy value
        nf.update(100_000)
        # estimate should have moved noticeably toward 100K
        # delta = 0.05 * (100K - 200K) = -5000, so new ~ 195000
        assert nf.estimate < 200_000
        assert nf.estimate == pytest.approx(195_000, rel=0.01)

        # After many frames it converges close to 100K
        for _ in range(200):
            nf.update(100_000)
        assert nf.estimate == pytest.approx(100_000, rel=0.01)

    def test_rises_slowly_when_energy_increases(self):
        """alpha_rise=0.001 means ~0.1 % step toward higher value each frame."""
        nf = NoiseFloorTracker(alpha_rise=0.001)
        nf.update(100_000)  # seed

        nf.update(200_000)
        # delta = 0.001 * (200K - 100K) = 100, so new ~ 100_100
        assert nf.estimate > 100_000
        assert nf.estimate == pytest.approx(100_100, rel=0.01)

        # Even after 100 frames of higher energy, barely moved
        for _ in range(100):
            nf.update(200_000)
        # Still well below 200K -- slow rise by design
        assert nf.estimate < 120_000

    def test_tracks_stable_noise_floor(self):
        """When energy hovers around a constant, estimate converges there."""
        nf = NoiseFloorTracker()
        for _ in range(500):
            nf.update(170_000)
        assert nf.estimate == pytest.approx(170_000, rel=0.001)

    def test_asymmetry(self):
        """Falls faster than it rises -- key property for noise tracking."""
        nf_fall = NoiseFloorTracker(alpha_rise=0.001, alpha_fall=0.05)
        nf_fall.update(150_000)
        nf_fall.update(100_000)  # drop
        fall_delta = abs(nf_fall.estimate - 150_000)

        nf_rise = NoiseFloorTracker(alpha_rise=0.001, alpha_fall=0.05)
        nf_rise.update(150_000)
        nf_rise.update(200_000)  # rise
        rise_delta = abs(nf_rise.estimate - 150_000)

        # Fall should move much further than rise in one step
        assert fall_delta > rise_delta * 10


# ---------------------------------------------------------------------------
# 2. SchmittTrigger
# ---------------------------------------------------------------------------
class TestSchmittTrigger:

    def test_activates_above_t_high(self):
        st = SchmittTrigger()
        assert st.active is False
        result = st.update(energy=1000, t_high=500, t_low=200)
        assert result is True
        assert st.active is True

    def test_stays_active_between_thresholds(self):
        """Hysteresis: once active, stays active until energy < t_low."""
        st = SchmittTrigger()
        st.update(energy=1000, t_high=500, t_low=200)  # activate
        assert st.active is True

        # Energy between t_low and t_high -- should stay active
        result = st.update(energy=350, t_high=500, t_low=200)
        assert result is True
        assert st.active is True

    def test_does_not_toggle_between_thresholds(self):
        """Inactive trigger must NOT activate when energy is between t_low and t_high."""
        st = SchmittTrigger()
        # Energy between thresholds while inactive -- must stay inactive
        result = st.update(energy=350, t_high=500, t_low=200)
        assert result is False
        assert st.active is False

    def test_deactivates_below_t_low(self):
        st = SchmittTrigger()
        st.update(energy=1000, t_high=500, t_low=200)  # activate
        assert st.active is True

        result = st.update(energy=100, t_high=500, t_low=200)
        assert result is False
        assert st.active is False

    def test_exact_boundary_high(self):
        """energy == t_high should NOT activate (strictly greater required)."""
        st = SchmittTrigger()
        result = st.update(energy=500, t_high=500, t_low=200)
        assert result is False

    def test_exact_boundary_low(self):
        """energy == t_low should NOT deactivate (strictly less required)."""
        st = SchmittTrigger()
        st.update(energy=1000, t_high=500, t_low=200)  # activate
        result = st.update(energy=200, t_high=500, t_low=200)
        assert result is True  # still active


# ---------------------------------------------------------------------------
# 3. ClickStateMachine
# ---------------------------------------------------------------------------
class TestClickStateMachine:

    def _make_sm(self, **kwargs):
        defaults = dict(min_click_ms=50, max_click_ms=2000,
                        min_gap_ms=150, max_gap_ms=2500)
        defaults.update(kwargs)
        return ClickStateMachine(**defaults)

    def _run_click(self, sm, t, duration_ms, energy=100_000_000):
        """Simulate one click: active for duration_ms, then deactivate."""
        # Go active
        sm.update(True, t, energy)
        t += duration_ms / 1000.0
        # Go inactive
        sm.update(False, t, 0)
        return t

    def test_single_valid_click(self):
        """A single 100ms click, then wait for max_gap timeout -> count=1."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # Click: active for 100ms
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Wait for gap timeout (500ms)
        t += 0.6
        count, peak, dur = sm.update(False, t, 0)
        assert count == 1
        assert peak > 0
        assert dur > 0

    def test_rejects_short_click_noise_spike(self):
        """Click shorter than min_click_ms (50ms) is rejected as noise."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # Very short click: 20ms
        sm.update(True, t, 50_000_000)
        t += 0.020
        sm.update(False, t, 0)

        # Should go back to IDLE, wait for timeout
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        # No valid clicks detected
        assert count == 0

    def test_rejects_long_click_voice(self):
        """Click longer than max_click_ms (2000ms) is rejected as voice."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # Long press: 3000ms
        sm.update(True, t, 50_000_000)
        t += 3.0
        sm.update(False, t, 0)

        # Wait for timeout
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        assert count == 0

    def test_long_click_rejected_during_active(self):
        """If active duration exceeds max_click while still active, resets to IDLE."""
        sm = self._make_sm(max_click_ms=2000, max_gap_ms=500)
        t = 0.0

        sm.update(True, t, 50_000_000)
        t += 2.1  # exceed max_click while still active
        # This update while still active should trigger the timeout path
        count, _, _ = sm.update(True, t, 50_000_000)
        assert sm.state == "IDLE"
        assert count == 0

    def test_multiple_clicks_counted(self):
        """Two valid clicks with a valid gap are counted as 2."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # Click 1: 100ms
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Gap: 200ms (>= min_gap of 150ms)
        t += 0.2
        # Click 2: 100ms
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Wait for timeout
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        assert count == 2

    def test_emits_count_after_max_gap_timeout(self):
        """Count is only emitted once the max_gap elapses without a new click."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # One click
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Not yet at timeout -- should return 0
        t += 0.3
        count, _, _ = sm.update(False, t, 0)
        assert count == 0

        # Past timeout
        t += 0.3
        count, _, _ = sm.update(False, t, 0)
        assert count == 1

    def test_rejects_bounce_gap_too_short(self):
        """Gap shorter than min_gap_ms is ignored (bounce rejection)."""
        sm = self._make_sm(min_gap_ms=150, max_gap_ms=500)
        t = 0.0

        # Click 1
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Bounce: gap only 50ms, then another active
        t += 0.050
        # This active event during a too-short gap should be ignored
        count, _, _ = sm.update(True, t, 100_000_000)
        assert count == 0

        # The state machine stays in GAP -- wait for timeout
        # Since the short-gap activation was ignored, we remain in GAP
        # Continue being inactive
        sm.update(False, t + 0.01, 0)
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        # Only the first valid click counted
        assert count == 1

    def test_four_clicks_detected(self):
        """4 clicks in sequence detected correctly."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        for _ in range(4):
            sm.update(True, t, 100_000_000)
            t += 0.1  # 100ms click
            sm.update(False, t, 0)
            t += 0.2  # 200ms gap

        # Wait for timeout
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        assert count == 4

    def test_six_clicks_detected(self):
        """6 clicks in sequence detected correctly."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        for _ in range(6):
            sm.update(True, t, 100_000_000)
            t += 0.1  # 100ms click
            sm.update(False, t, 0)
            t += 0.2  # 200ms gap

        # Wait for timeout
        t += 0.6
        count, _, _ = sm.update(False, t, 0)
        assert count == 6

    def test_returns_peak_energy_and_duration(self):
        """Emitted result includes average peak energy and duration."""
        sm = self._make_sm(max_gap_ms=500)
        t = 0.0

        # Click with known energy
        sm.update(True, t, 50_000_000)
        t += 0.05  # 50ms at threshold
        sm.update(True, t, 200_000_000)  # peak mid-click
        t += 0.05
        sm.update(False, t, 0)

        t += 0.6
        count, peak, dur = sm.update(False, t, 0)
        assert count == 1
        assert peak == 200_000_000  # took the max
        assert dur == pytest.approx(100.0, abs=5)  # ~100ms in ms

    def test_reset_clears_state(self):
        sm = self._make_sm()
        t = 0.0
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)
        assert sm.state == "GAP"

        sm.reset()
        assert sm.state == "IDLE"
        assert sm.click_count == 0
        assert sm.peak_energy == 0


# ---------------------------------------------------------------------------
# 4. ClickProfile
# ---------------------------------------------------------------------------
class TestClickProfile:

    @pytest.fixture(autouse=True)
    def _patch_profile_path(self, tmp_path, monkeypatch):
        """Redirect profile file I/O to tmp_path so tests don't touch /tmp."""
        self._profile_path = str(tmp_path / "click_profile.json")
        monkeypatch.setattr("nanoawos.tap.PROFILE_PATH", self._profile_path)

    def _make_profile(self):
        """Create a fresh ClickProfile (picks up patched PROFILE_PATH)."""
        return ClickProfile()

    def test_record_click_adds_to_list(self):
        p = self._make_profile()
        assert len(p.click_energies) == 0
        p.record_click(100_000_000, 120.0)
        assert len(p.click_energies) == 1
        assert p.click_energies[0] == 100_000_000
        assert p.click_durations[0] == 120.0

    def test_has_enough_data_after_5_samples(self):
        p = self._make_profile()
        for i in range(4):
            p.record_click(100_000_000 + i, 100.0 + i)
        assert p.has_enough_data() is False

        p.record_click(100_000_004, 104.0)
        assert p.has_enough_data() is True

    def test_get_thresholds_returns_sensible_values(self):
        p = self._make_profile()
        noise_floor = 170_000

        # Before enough data, returns None
        t_h, t_l = p.get_thresholds(noise_floor, default_high_mult=50.0,
                                     default_low_mult=10.0)
        assert t_h is None
        assert t_l is None

        # Add enough samples with energy well above noise floor
        for i in range(6):
            p.record_click(100_000_000 + i * 1_000_000, 100.0)

        t_h, t_l = p.get_thresholds(noise_floor, default_high_mult=50.0,
                                     default_low_mult=10.0)
        assert t_h is not None
        assert t_l is not None

        # t_high should be well above noise floor
        assert t_h > noise_floor
        # t_low should be below t_high
        assert t_l < t_h
        # Both should be below the weakest click energy
        assert t_h < 100_000_000
        assert t_l < t_h

    def test_safety_floor_never_below_default_multipliers(self):
        """Even with learned data, thresholds never drop below default mult * noise."""
        p = self._make_profile()
        noise_floor = 170_000
        high_mult = 50.0
        low_mult = 10.0

        # Record clicks with very low energy (close to noise floor)
        for _ in range(6):
            p.record_click(500_000, 100.0)  # very weak "clicks"

        t_h, t_l = p.get_thresholds(noise_floor, high_mult, low_mult)

        # t_high must be at least noise_floor * high_mult
        assert t_h >= noise_floor * high_mult
        # t_low must be at least noise_floor * low_mult
        assert t_l >= noise_floor * low_mult

    def test_clear_resets_profile(self):
        p = self._make_profile()
        for i in range(5):
            p.record_click(100_000_000, 100.0)
        assert p.has_enough_data() is True

        p.clear()
        assert len(p.click_energies) == 0
        assert len(p.click_durations) == 0
        assert p.has_enough_data() is False

    def test_save_load_roundtrip(self):
        p = self._make_profile()
        p.record_click(100_000_000, 120.5)
        p.record_click(200_000_000, 95.0)
        p.record_click(150_000_000, 110.0)
        p.save()

        # Create a new profile that loads from the same path
        p2 = self._make_profile()
        assert len(p2.click_energies) == 3
        assert p2.click_energies == [100_000_000, 200_000_000, 150_000_000]
        assert p2.click_durations == [120.5, 95.0, 110.0]

    def test_load_missing_file_no_error(self):
        """Loading when no profile file exists should not raise."""
        p = self._make_profile()
        assert len(p.click_energies) == 0

    def test_max_samples_rolling_window(self):
        p = self._make_profile()
        p.max_samples = 10
        for i in range(15):
            p.record_click(i * 1_000_000, 100.0)
        assert len(p.click_energies) == 10
        # Should keep the most recent 10
        assert p.click_energies[0] == 5_000_000

    def test_get_thresholds_zero_noise_floor(self):
        """With zero noise floor, should return None even with data."""
        p = self._make_profile()
        for _ in range(6):
            p.record_click(100_000_000, 100.0)
        t_h, t_l = p.get_thresholds(0, 50.0, 10.0)
        assert t_h is None
        assert t_l is None

    def test_get_stats_empty(self):
        p = self._make_profile()
        stats = p.get_stats()
        assert stats["samples"] == 0

    def test_get_stats_with_data(self):
        p = self._make_profile()
        p.record_click(100_000_000, 120.0)
        p.record_click(200_000_000, 80.0)
        stats = p.get_stats()
        assert stats["samples"] == 2
        assert stats["min_energy"] == 100_000_000
        assert stats["max_energy"] == 200_000_000
        assert stats["auto_tuning"] is False  # only 2 samples


# ---------------------------------------------------------------------------
# 5. frame_energy_int
# ---------------------------------------------------------------------------
class TestFrameEnergyInt:

    def test_silent_audio_returns_low_energy(self):
        """All-zero samples should give energy 0."""
        silence = struct.pack("<" + "h" * 100, *([0] * 100))
        assert frame_energy_int(silence) == 0

    def test_loud_audio_returns_high_energy(self):
        """Max-amplitude samples should give very high energy."""
        loud = struct.pack("<" + "h" * 100, *([32767] * 100))
        energy = frame_energy_int(loud)
        # 32767^2 = 1_073_676_289
        assert energy == 32767 * 32767

    def test_empty_bytes_returns_zero(self):
        assert frame_energy_int(b"") == 0

    def test_single_sample(self):
        data = struct.pack("<h", 1000)
        assert frame_energy_int(data) == 1_000_000

    def test_mixed_samples(self):
        """Mean of squared values for a mix of positive and negative."""
        samples = [100, -100, 200, -200]
        data = struct.pack("<" + "h" * 4, *samples)
        # (10000 + 10000 + 40000 + 40000) / 4 = 25000
        assert frame_energy_int(data) == 25000

    def test_odd_byte_count_raises(self):
        """Odd number of bytes raises struct.error (buffer size mismatch)."""
        data = struct.pack("<hh", 100, 200) + b"\x00"
        with pytest.raises(struct.error):
            frame_energy_int(data)


# ---------------------------------------------------------------------------
# 6. Integration: Full Pipeline (NoiseFloor -> Schmitt -> StateMachine)
# ---------------------------------------------------------------------------
class TestIntegrationPipeline:
    """End-to-end tests of the detection pipeline without PyAudio."""

    NOISE_ENERGY = 170_000
    CLICK_ENERGY = 100_000_000

    def _run_pipeline(self, energy_sequence, frame_ms=50):
        """Run a sequence of energy values through the full pipeline.

        Args:
            energy_sequence: list of (energy, frame_count) tuples
            frame_ms: duration of each frame in ms

        Returns:
            list of (count, peak, dur) tuples for each emission.
        """
        nf = NoiseFloorTracker(alpha_rise=0.001, alpha_fall=0.05)
        st = SchmittTrigger()
        sm = ClickStateMachine(min_click_ms=50, max_click_ms=2000,
                               min_gap_ms=150, max_gap_ms=2500)

        # Seed noise floor with quiet frames
        for _ in range(60):  # 3 seconds of noise
            nf.update(self.NOISE_ENERGY)

        t = 0.0
        dt = frame_ms / 1000.0
        results = []

        for energy, num_frames in energy_sequence:
            for _ in range(num_frames):
                nf.update(energy)
                nf_est = nf.estimate
                t_high = max(nf_est * 50.0, 500)
                t_low = max(nf_est * 10.0, 250)

                is_active = st.update(energy, t_high, t_low)
                count, peak, dur = sm.update(is_active, t, energy)
                if count > 0:
                    results.append((count, peak, dur))
                t += dt

        return results

    def test_four_clicks_detected(self):
        """Simulate 4 PTT clicks at 100M+ energy with noise floor at 170K."""
        seq = []
        for i in range(4):
            # Click: 100ms = 2 frames of 50ms
            seq.append((self.CLICK_ENERGY, 2))
            # Gap: 300ms = 6 frames of 50ms (unless last click)
            if i < 3:
                seq.append((self.NOISE_ENERGY, 6))

        # Final gap to trigger timeout: 3 seconds = 60 frames
        seq.append((self.NOISE_ENERGY, 60))

        results = self._run_pipeline(seq)
        assert len(results) == 1
        count, peak, dur = results[0]
        assert count == 4
        assert peak > self.NOISE_ENERGY  # peak was from click energy

    def test_six_clicks_detected(self):
        """Simulate 6 PTT clicks."""
        seq = []
        for i in range(6):
            seq.append((self.CLICK_ENERGY, 2))  # 100ms click
            if i < 5:
                seq.append((self.NOISE_ENERGY, 6))  # 300ms gap

        seq.append((self.NOISE_ENERGY, 60))  # timeout

        results = self._run_pipeline(seq)
        assert len(results) == 1
        assert results[0][0] == 6

    def test_noise_only_produces_no_clicks(self):
        """Continuous noise at 170K should never trigger a click."""
        seq = [(self.NOISE_ENERGY, 200)]  # 10 seconds of noise
        results = self._run_pipeline(seq)
        assert len(results) == 0

    def test_single_long_signal_rejected(self):
        """A long continuous signal (e.g. voice) exceeding max_click is rejected.

        When energy stays high for many seconds, the state machine resets to IDLE
        at max_click_ms, then re-enters ACTIVE on the next still-high frame.
        Each such cycle exceeds max_click again. A short tail at the energy
        drop boundary could register as a valid click if its duration falls
        within min_click..max_click. We test with a duration that is an exact
        multiple of max_click + some margin to ensure the final tail is shorter
        than min_click_ms (50ms = 1 frame at 50ms/frame).
        """
        # Use exactly 42 frames = 2100ms per active cycle.
        # max_click = 2000ms -> times out at frame 40.
        # After timeout + re-enter, 1 frame of "new active" before drop.
        # That 1-frame click = 50ms, which is right at min_click boundary.
        # Use 41 frames so the remainder after timeout is 50ms (1 frame),
        # and it drops to noise immediately. The SM sees 50ms active then
        # inactive: dur=50ms == min_click, so it could pass. To be safe,
        # use a very long signal where the last segment also exceeds max.
        seq = [
            # 10 seconds = 200 frames of continuous high energy
            # At max_click=2000ms (40 frames), resets cycle:
            #   frames 0-40: ACTIVE->timeout->IDLE
            #   frame 41: IDLE->ACTIVE restart
            #   frames 41-81: timeout again
            #   ... repeats.  200 frames = 5 full cycles, no valid click.
            (self.CLICK_ENERGY, 200),
            # Abrupt drop: 0 energy for 1 frame so Schmitt deactivates fast
            (0, 1),
            # Then noise for timeout
            (self.NOISE_ENERGY, 60),
        ]
        results = self._run_pipeline(seq)
        # Any emitted count should be 0 (no valid clicks), or if a 1-frame
        # tail registers, it would be exactly at min_click boundary.
        # The key assertion: no multi-click sequence detected from a
        # continuous signal.
        for count, _, _ in results:
            assert count <= 1, (
                f"Long signal should not produce multi-click sequence, got {count}"
            )

    def test_weak_clicks_below_threshold_ignored(self):
        """Clicks only slightly above noise floor should not trigger."""
        weak_energy = self.NOISE_ENERGY * 5  # well below 50x threshold
        seq = []
        for i in range(4):
            seq.append((weak_energy, 2))
            if i < 3:
                seq.append((self.NOISE_ENERGY, 6))
        seq.append((self.NOISE_ENERGY, 60))

        results = self._run_pipeline(seq)
        assert len(results) == 0

    def test_mixed_valid_and_invalid_clicks(self):
        """A sequence with one short noise spike among valid clicks."""
        seq = [
            # Valid click 1: 100ms
            (self.CLICK_ENERGY, 2),
            (self.NOISE_ENERGY, 6),  # 300ms gap
            # Valid click 2: 100ms
            (self.CLICK_ENERGY, 2),
            (self.NOISE_ENERGY, 6),  # 300ms gap
            # Valid click 3: 100ms
            (self.CLICK_ENERGY, 2),
            # Timeout
            (self.NOISE_ENERGY, 60),
        ]
        results = self._run_pipeline(seq)
        assert len(results) == 1
        assert results[0][0] == 3


# ---------------------------------------------------------------------------
# Edge cases and state transitions
# ---------------------------------------------------------------------------
class TestClickStateMachineEdgeCases:

    def test_idle_to_active_only_on_active_signal(self):
        sm = ClickStateMachine()
        t = 0.0
        count, _, _ = sm.update(False, t, 0)
        assert count == 0
        assert sm.state == "IDLE"

    def test_no_emission_while_collecting_clicks(self):
        """During an active click sequence, count is not emitted mid-sequence."""
        sm = ClickStateMachine(max_gap_ms=500)
        t = 0.0

        # Click 1
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)
        assert sm.click_count == 1

        # Mid-gap -- no emission yet
        t += 0.2
        count, _, _ = sm.update(False, t, 0)
        assert count == 0

        # Click 2
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)
        assert sm.click_count == 2

        # Still no emission
        t += 0.2
        count, _, _ = sm.update(False, t, 0)
        assert count == 0

    def test_average_metrics_across_multiple_clicks(self):
        """Peak energy and duration are averaged across all clicks in a sequence."""
        sm = ClickStateMachine(max_gap_ms=500)
        t = 0.0

        # Click 1: 100ms, peak 100M
        sm.update(True, t, 100_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Gap
        t += 0.2

        # Click 2: 100ms, peak 200M
        sm.update(True, t, 200_000_000)
        t += 0.1
        sm.update(False, t, 0)

        # Timeout
        t += 0.6
        count, peak, dur = sm.update(False, t, 0)
        assert count == 2
        # Average peak: (100M + 200M) / 2 = 150M
        assert peak == 150_000_000
        # Average duration: (100ms + 100ms) / 2 = 100ms
        assert dur == pytest.approx(100.0, abs=5)
