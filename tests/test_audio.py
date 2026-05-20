"""Tests for nanoawos/audio.py."""

import subprocess
from unittest.mock import patch, mock_open, call, MagicMock

import pytest

from nanoawos import audio


@pytest.fixture
def cfg(sample_config):
    """Return the sample_config dict directly (no file I/O needed)."""
    return sample_config


# -- _mpc -------------------------------------------------------------------

def test_mpc_builds_correct_command_no_h_flag(cfg):
    """_mpc must invoke ['mpc', ...args] with NO -h flag."""
    with patch("nanoawos.audio.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["mpc", "play"], returncode=0, stdout="", stderr=""
        )
        audio._mpc(["play"], cfg)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["mpc", "play"]
        assert "-h" not in cmd


def test_mpc_logs_warning_on_failure(cfg, caplog):
    """_mpc logs a warning when returncode != 0 and stderr is non-empty."""
    with patch("nanoawos.audio.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["mpc", "load", "bad"], returncode=1,
            stdout="", stderr="playlist not found"
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="nanoawos.audio"):
            audio._mpc(["load", "bad"], cfg)

        assert any("failed" in rec.message for rec in caplog.records)


# -- update_playlists -------------------------------------------------------

def test_update_playlists_calls_mpc_in_order(cfg):
    """update_playlists must call mpc rm, clear, add, save for both playlists."""
    with patch("nanoawos.audio._mpc") as mock_mpc:
        audio.update_playlists("full.wav", "wind.wav", cfg)

        expected = [
            call(["rm", "full"], cfg),
            call(["rm", "wind"], cfg),
            # full playlist
            call(["clear"], cfg),
            call(["add", "full.wav"], cfg),
            call(["save", "full"], cfg),
            # wind playlist
            call(["clear"], cfg),
            call(["add", "wind.wav"], cfg),
            call(["save", "wind"], cfg),
        ]
        assert mock_mpc.call_args_list == expected


# -- play_playlist ----------------------------------------------------------

def test_play_playlist_calls_mpc_clear_load_crossfade_play(cfg):
    """play_playlist must issue clear, load, crossfade 1, play in order."""
    with patch("nanoawos.audio._mpc") as mock_mpc:
        audio.play_playlist("full", cfg)

        expected = [
            call(["clear"], cfg),
            call(["load", "full"], cfg),
            call(["crossfade", "1"], cfg),
            call(["play"], cfg),
        ]
        assert mock_mpc.call_args_list == expected


# -- play_wav ---------------------------------------------------------------

def test_play_wav_calls_mpc_clear_add_play(cfg):
    """play_wav must issue clear, add <path>, play in order."""
    with patch("nanoawos.audio._mpc") as mock_mpc:
        audio.play_wav("/mnt/p4/audio/alert.wav", cfg)

        expected = [
            call(["clear"], cfg),
            call(["add", "/mnt/p4/audio/alert.wav"], cfg),
            call(["play"], cfg),
        ]
        assert mock_mpc.call_args_list == expected


# -- get_ptt ----------------------------------------------------------------

def test_get_ptt_reads_gpio_value_file(cfg):
    """get_ptt reads /sys/class/gpio/gpio<pin>/value and returns bool."""
    pin = cfg["audio"]["gpio_pin"]
    expected_path = f"/sys/class/gpio/gpio{pin}/value"

    m = mock_open(read_data="1\n")
    with patch("builtins.open", m):
        result = audio.get_ptt(cfg)

    m.assert_called_once_with(expected_path, "r")
    assert result is True

    # Also test the False case
    m2 = mock_open(read_data="0\n")
    with patch("builtins.open", m2):
        result2 = audio.get_ptt(cfg)

    assert result2 is False


# -- set_ptt ----------------------------------------------------------------

def test_set_ptt_writes_gpio_value_file(cfg):
    """set_ptt writes '1' or '0' to the GPIO value file."""
    pin = cfg["audio"]["gpio_pin"]
    expected_path = f"/sys/class/gpio/gpio{pin}/value"

    m = mock_open()
    with patch("builtins.open", m):
        audio.set_ptt(True, cfg)

    m.assert_called_once_with(expected_path, "w")
    m().write.assert_called_once_with("1")

    # Test setting to False
    m2 = mock_open()
    with patch("builtins.open", m2):
        audio.set_ptt(False, cfg)

    m2().write.assert_called_once_with("0")


# -- wait_for_idle -----------------------------------------------------------

def test_wait_for_idle_blocks_while_ptt_active_then_returns(cfg):
    """wait_for_idle loops while get_ptt is True, returns when False."""
    # Simulate: PTT active twice, then goes idle
    with patch("nanoawos.audio.get_ptt", side_effect=[True, True, False]) as mock_get, \
         patch("nanoawos.audio.time.sleep") as mock_sleep:
        audio.wait_for_idle(cfg)

    assert mock_get.call_count == 3
    # sleep(1) called for each True iteration
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(1)
