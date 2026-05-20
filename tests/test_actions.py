"""Tests for nanoawos/actions.py."""

import json
import os
from unittest.mock import MagicMock, patch, call

import pytest

from nanoawos.actions import (
    execute_action,
    execute_transcription_reactions,
    pregenerate_tts_actions,
    render_template,
    _tts_wav_path,
    _filter_nato,
    _filter_digits,
    _filter_avspeak,
    _filter_time,
)


# ---------------------------------------------------------------------------
# Helper: patch config module to avoid loading from disk
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset nanoawos.config._config before each test."""
    import nanoawos.config as cfg_mod
    cfg_mod._config = None
    yield
    cfg_mod._config = None


@pytest.fixture
def mock_metar_files():
    """Patch file reads for /tmp/metar* files used by _get_weather_data."""
    file_data = {
        "/tmp/metar": "ZZZZ 1045",
        "/tmp/metar2": "270@12G18",
        "/tmp/metar3": "20/12 Q1013.2",
        "/tmp/metar4": "DA800FT",
    }

    original_open = open

    def _side_effect(path, *args, **kwargs):
        if path in file_data:
            from io import StringIO
            return StringIO(file_data[path])
        return original_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=_side_effect):
        yield file_data


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------

class TestAviationFilters:
    """Tests for NATO/aviation Jinja2 filters."""

    def test_nato_letters(self):
        assert _filter_nato("ABCZ") == "alfa bravo charlie zulu"

    def test_nato_lowercase(self):
        assert _filter_nato("epmy") == "echo papa mike yankee"

    def test_nato_single_letter(self):
        assert _filter_nato("A") == "alfa"

    def test_digits_number(self):
        assert _filter_digits(270) == "two seven zero"

    def test_digits_string(self):
        assert _filter_digits("1013") == "one zero one three"

    def test_digits_negative(self):
        assert _filter_digits("-5") == "minus five"

    def test_digits_decimal(self):
        assert _filter_digits("29.92") == "two niner decimal niner two"

    def test_digits_niner(self):
        assert _filter_digits(9) == "niner"

    def test_avspeak_pure_digits(self):
        assert _filter_avspeak("270") == "two seven zero"

    def test_avspeak_pure_letters_short(self):
        """Short letter sequences -> NATO."""
        assert _filter_avspeak("AB") == "alfa bravo"

    def test_avspeak_icao(self):
        assert _filter_avspeak("ZZZZ") == "zulu zulu zulu zulu"

    def test_avspeak_word_passthrough(self):
        """Long words (>4 chars) are not spelled out."""
        assert _filter_avspeak("temperature") == "temperature"
        assert _filter_avspeak("weather") == "weather"
        assert _filter_avspeak("gusts") == "gusts"

    def test_avspeak_short_word_nato(self):
        """Short letter strings (<=4 chars) are NATO spelled."""
        assert _filter_avspeak("wind") == "whiskey india november delta"
        assert _filter_avspeak("QNH") == "quebec november hotel"

    def test_avspeak_wind_format(self):
        """Wind like '270@12' -> spoken digits with 'at'."""
        assert _filter_avspeak("270@12") == "two seven zero at one two"

    def test_avspeak_single_letter(self):
        assert _filter_avspeak("A") == "alfa"

    def test_avspeak_mixed(self):
        assert _filter_avspeak("R27") == "romeo two seven"

    def test_filter_in_template(self, sample_config, mock_metar_files):
        """Filters work inside Jinja2 templates."""
        result = render_template('{{ "270" | digits }}', sample_config)
        assert result == "two seven zero"

    def test_nato_filter_in_template(self, sample_config, mock_metar_files):
        result = render_template('{{ "ABCD" | nato }}', sample_config)
        assert result == "alfa bravo charlie delta"

    def test_avspeak_filter_in_template(self, sample_config, mock_metar_files):
        result = render_template('{{ "R15" | avspeak }}', sample_config)
        assert result == "romeo one five"

    def test_time_with_zulu(self):
        assert _filter_time("1700Z") == "one seven zero zero zulu"

    def test_time_without_zulu(self):
        assert _filter_time("0945") == "zero niner four five"

    def test_time_lowercase_z(self):
        assert _filter_time("1030z") == "one zero three zero zulu"

    def test_time_filter_in_template(self, sample_config, mock_metar_files):
        result = render_template('{{ "1700Z" | time }}', sample_config)
        assert result == "one seven zero zero zulu"


class TestRenderTemplate:
    """Tests for the render_template function."""

    def test_simple_text_no_variables(self, sample_config, mock_metar_files):
        """Template with no Jinja2 variables is returned as-is."""
        result = render_template("hello world", sample_config)
        assert result == "hello world"

    def test_jinja2_weather_variables(self, sample_config, mock_metar_files):
        """Template renders weather.wind, time.zulu, station.name."""
        template = "Wind: {{ weather.wind }}, Station: {{ station.name }}"
        result = render_template(template, sample_config)

        assert "270@12G18" in result
        assert "test station" in result

    def test_jinja2_time_zulu(self, sample_config, mock_metar_files):
        """Template renders time.zulu as HHMMZ format."""
        template = "Time is {{ time.zulu }}"
        result = render_template(template, sample_config)

        # time.zulu should match pattern like "1234Z"
        assert result.startswith("Time is ")
        assert result.endswith("Z")

    def test_extra_context_transcript(self, sample_config, mock_metar_files):
        """Extra context (transcript.text) is available in templates."""
        extra = {"transcript": {"text": "landing runway 27", "action": "landing"}}
        template = "Heard: {{ transcript.text }}"
        result = render_template(template, sample_config, extra=extra)

        assert result == "Heard: landing runway 27"


# ---------------------------------------------------------------------------
# execute_action
# ---------------------------------------------------------------------------

class TestExecuteAction:
    """Tests for the execute_action function."""

    @patch("nanoawos.audio.play_playlist")
    def test_click_4_plays_wind(self, mock_play_playlist, sample_config):
        """4 clicks triggers weather_wind -> play_playlist('wind')."""
        with patch("nanoawos.config.load_config", return_value=sample_config):
            execute_action(4)

        mock_play_playlist.assert_called_once_with("wind", sample_config)

    @patch("nanoawos.audio.play_playlist")
    def test_click_6_plays_full(self, mock_play_playlist, sample_config):
        """6 clicks triggers weather_full -> play_playlist('full')."""
        with patch("nanoawos.config.load_config", return_value=sample_config):
            execute_action(6)

        mock_play_playlist.assert_called_once_with("full", sample_config)

    def test_unconfigured_click_count_returns(self, sample_config):
        """Unconfigured click count (e.g. 99) logs info and returns."""
        with patch("nanoawos.config.load_config", return_value=sample_config):
            # Should not raise
            execute_action(99)

    @patch("nanoawos.audio.play_wav")
    def test_tts_cached_wav_plays_cached_file(
        self, mock_play_wav, sample_config, mock_metar_files, tmp_path
    ):
        """TTS action with matching cached WAV plays it without re-synthesis."""
        # Set up config with TTS action on 5 clicks
        wav_path = _tts_wav_path("5_clicks", sample_config)

        # Create the cached WAV file
        os.makedirs(os.path.dirname(wav_path), exist_ok=True)
        with open(wav_path, "w") as f:
            f.write("fake wav data")

        # Set up TTS cache so rendered text matches
        cache = {wav_path: "hello world"}
        with patch("nanoawos.config.load_config", return_value=sample_config), \
             patch("nanoawos.actions._load_tts_cache", return_value=cache), \
             patch("nanoawos.actions.os.path.exists", return_value=True):
            execute_action(5)

        mock_play_wav.assert_called_once_with(wav_path, sample_config)

    @patch("nanoawos.actions._publish_mqtt")
    def test_mqtt_action_calls_publish(self, mock_publish, sample_config):
        """MQTT action type calls _publish_mqtt with topic and payload."""
        with patch("nanoawos.config.load_config", return_value=sample_config):
            execute_action(8)

        mock_publish.assert_called_once()
        args = mock_publish.call_args
        assert args[0][0] == "test/topic"  # topic

    @patch("nanoawos.audio.play_wav")
    def test_audio_file_action_plays_file(self, mock_play_wav, sample_config):
        """audio_file action plays the specified file path."""
        cfg = dict(sample_config)
        cfg["click_actions"] = {
            7: {"type": "audio_file", "label": "Alert", "file": "/tmp/alert.wav"},
        }
        with patch("nanoawos.config.load_config", return_value=cfg), \
             patch("nanoawos.actions.os.path.exists", return_value=True):
            execute_action(7)

        mock_play_wav.assert_called_once_with("/tmp/alert.wav", cfg)


# ---------------------------------------------------------------------------
# execute_transcription_reactions
# ---------------------------------------------------------------------------

class TestExecuteTranscriptionReactions:
    """Tests for execute_transcription_reactions function."""

    @patch("nanoawos.actions._publish_mqtt")
    def test_regex_match_triggers_action(self, mock_publish, sample_config):
        """'mayday' in text matches the emergency reaction and triggers MQTT."""
        execute_transcription_reactions("mayday mayday mayday", None, sample_config)

        mock_publish.assert_called_once()
        args = mock_publish.call_args
        assert args[0][0] == "test/emergency"

    @patch("nanoawos.actions._publish_mqtt")
    @patch("nanoawos.actions._run_action")
    def test_no_match_does_nothing(self, mock_run, mock_publish, sample_config):
        """Text with no matching pattern does not trigger any action."""
        execute_transcription_reactions(
            "weather is nice today", None, sample_config
        )

        mock_run.assert_not_called()
        mock_publish.assert_not_called()

    @patch("nanoawos.actions._publish_mqtt")
    def test_match_on_action_field(self, mock_publish, sample_config):
        """Reaction with match_field='action' matches on gpt_action string."""
        cfg = dict(sample_config)
        cfg["transcription_reactions"] = [
            {"label": "Landing", "match": "land", "match_field": "action",
             "type": "mqtt", "topic": "test/landing"},
        ]
        execute_transcription_reactions("some text", "land", cfg)

        mock_publish.assert_called_once()
        assert mock_publish.call_args[0][0] == "test/landing"

    @patch("nanoawos.actions._publish_mqtt")
    def test_match_field_action_no_match_on_text(
        self, mock_publish, sample_config
    ):
        """match_field='action' does not match against text field."""
        cfg = dict(sample_config)
        cfg["transcription_reactions"] = [
            {"label": "Landing", "match": "land", "match_field": "action",
             "type": "mqtt", "topic": "test/landing"},
        ]
        # "land" appears in text but match_field is action, gpt_action is None
        execute_transcription_reactions("landing runway 9", None, cfg)

        mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# pregenerate_tts_actions
# ---------------------------------------------------------------------------

class TestPregenerateTtsActions:
    """Tests for pregenerate_tts_actions function."""

    @patch("nanoawos.actions._save_tts_cache")
    @patch("nanoawos.tts.synthesize")
    @patch("nanoawos.actions._load_tts_cache", return_value={})
    def test_skips_cloud_engine(
        self, mock_cache, mock_synth, mock_save, sample_config, mock_metar_files
    ):
        """TTS actions using cloud engine are skipped during pre-generation."""
        cfg = dict(sample_config)
        cfg["click_actions"] = {
            5: {"type": "tts", "label": "Cloud", "text": "hello",
                "tts_engine": "cloud"},
        }
        cfg["transcription_reactions"] = []

        pregenerate_tts_actions(cfg)

        mock_synth.assert_not_called()

    @patch("nanoawos.actions._save_tts_cache")
    @patch("nanoawos.tts.synthesize")
    @patch("nanoawos.actions.os.path.exists", return_value=False)
    @patch("nanoawos.actions._load_tts_cache", return_value={})
    def test_regenerates_when_text_changes(
        self, mock_cache, mock_exists, mock_synth, mock_save,
        sample_config, mock_metar_files,
    ):
        """Piper TTS actions are regenerated when rendered text changes."""
        cfg = dict(sample_config)
        cfg["click_actions"] = {
            5: {"type": "tts", "label": "Custom", "text": "hello world",
                "tts_engine": "piper"},
        }
        cfg["transcription_reactions"] = []

        pregenerate_tts_actions(cfg)

        mock_synth.assert_called_once()
        # Check the synthesized text and output path
        synth_args = mock_synth.call_args[0]
        assert synth_args[0] == "hello world"
        assert "action_5_clicks" in synth_args[1]
        mock_save.assert_called_once()

    @patch("nanoawos.actions._save_tts_cache")
    @patch("nanoawos.tts.synthesize")
    @patch("nanoawos.actions.os.path.exists", return_value=True)
    def test_skips_when_cached_text_unchanged(
        self, mock_exists, mock_synth, mock_save,
        sample_config, mock_metar_files,
    ):
        """Pre-generation is skipped when cached text matches rendered text."""
        cfg = dict(sample_config)
        cfg["click_actions"] = {
            5: {"type": "tts", "label": "Custom", "text": "hello world",
                "tts_engine": "piper"},
        }
        cfg["transcription_reactions"] = []

        wav_path = _tts_wav_path("5_clicks", cfg)
        # Cache already has the exact same rendered text
        cache = {wav_path: "hello world"}
        with patch("nanoawos.actions._load_tts_cache", return_value=cache):
            pregenerate_tts_actions(cfg)

        mock_synth.assert_not_called()
        mock_save.assert_not_called()

    @patch("nanoawos.actions._save_tts_cache")
    @patch("nanoawos.tts.synthesize")
    @patch("nanoawos.actions.os.path.exists", return_value=True)
    def test_regenerates_when_cached_text_differs(
        self, mock_exists, mock_synth, mock_save,
        sample_config, mock_metar_files,
    ):
        """Pre-generation regenerates when cached text differs from rendered."""
        cfg = dict(sample_config)
        cfg["click_actions"] = {
            5: {"type": "tts", "label": "Custom", "text": "hello world",
                "tts_engine": "piper"},
        }
        cfg["transcription_reactions"] = []

        wav_path = _tts_wav_path("5_clicks", cfg)
        # Cache has old text that does not match
        cache = {wav_path: "old text"}
        with patch("nanoawos.actions._load_tts_cache", return_value=cache):
            pregenerate_tts_actions(cfg)

        mock_synth.assert_called_once()
        mock_save.assert_called_once()
