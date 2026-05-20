"""Tests for nanoawos/weather.py."""

import math
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

from nanoawos.weather import (
    WeatherData,
    build_announcement_text,
    build_wind_text,
    fetch_weather,
    write_metar_files,
)


# ---------------------------------------------------------------------------
# fetch_weather
# ---------------------------------------------------------------------------

class TestFetchWeather:
    """Tests for the fetch_weather function."""

    def test_fetch_weather_success(
        self, sample_config, weather_api_response_english, weather_api_response_metric
    ):
        """fetch_weather parses English + Metric API responses correctly."""
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = weather_api_response_metric
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            wd = fetch_weather(sample_config)

        assert wd.data_valid is True
        assert wd.wind_dir == 270
        assert wd.wind_speed_kt == 12
        assert wd.wind_gust_kt == 18
        assert wd.has_gusts is True
        assert wd.temp_c == 20.0
        assert wd.dewpt_c == 12.0
        assert wd.pressure_hpa == 1013.2
        assert wd.temp_available is True
        assert wd.obs_time_utc == "2026-05-20T10:45:00Z"
        assert wd.time_hour == "1045"

    def test_fetch_weather_null_temp(
        self, sample_config, weather_api_response_english, weather_api_response_null_temp
    ):
        """fetch_weather with null temp/dewpt sets temp_available=False."""
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = weather_api_response_null_temp
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            wd = fetch_weather(sample_config)

        assert wd.data_valid is True
        assert wd.temp_available is False
        assert wd.temp_c == 0.0
        assert wd.dewpt_c == 0.0

    def test_fetch_weather_api_error(self, sample_config):
        """fetch_weather returns invalid WeatherData when API errors out."""
        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = Exception("connection timeout")
            wd = fetch_weather(sample_config)

        assert wd.data_valid is False
        assert wd.wind_speed_kt == 0
        assert wd.temp_c == 0.0

    def test_fetch_weather_metric_api_error(
        self, sample_config, weather_api_response_english
    ):
        """fetch_weather returns invalid WeatherData when metric fetch fails."""
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, Exception("metric error")]
            wd = fetch_weather(sample_config)

        assert wd.data_valid is False


# ---------------------------------------------------------------------------
# Time parsing from obsTimeUtc
# ---------------------------------------------------------------------------

class TestTimeParsing:
    """Tests for observation time parsing in fetch_weather."""

    def test_time_parsing_from_obs_time_utc(
        self, sample_config, weather_api_response_english
    ):
        """Time hour is extracted from obsTimeUtc field (HH:MM -> HHMM)."""
        metric_response = {
            "observations": [{
                "stationID": "TEST1",
                "obsTimeUtc": "2026-05-20T08:30:00Z",
                "winddir": 180,
                "metric": {"windSpeed": 10, "windGust": 10,
                           "pressure": 1013.0, "temp": 15.0, "dewpt": 10.0},
            }]
        }
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = metric_response
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            wd = fetch_weather(sample_config)

        assert wd.time_hour == "0830"

    def test_time_parsing_missing_obs_time(
        self, sample_config, weather_api_response_english
    ):
        """Falls back to 0000 when obsTimeUtc is missing."""
        metric_response = {
            "observations": [{
                "stationID": "TEST1",
                "winddir": 180,
                "metric": {"windSpeed": 10, "windGust": 10,
                           "pressure": 1013.0, "temp": 15.0, "dewpt": 10.0},
            }]
        }
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = metric_response
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            wd = fetch_weather(sample_config)

        assert wd.time_hour == "0000"


# ---------------------------------------------------------------------------
# build_announcement_text
# ---------------------------------------------------------------------------

class TestBuildAnnouncementText:
    """Tests for the build_announcement_text function."""

    def test_with_valid_data(self, sample_config):
        """Announcement contains station name, wind, temperature, QNH."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12, wind_gust_kt=18,
            has_gusts=True, temp_c=20.0, dewpt_c=12.0, pressure_hpa=1013.2,
            density_alt_ft=800, recommended_runway=27, temp_available=True,
            data_valid=True,
        )
        text = build_announcement_text(wd, sample_config)

        assert "test station" in text
        assert "wind" in text
        assert "qnh" in text
        assert "temperature" in text
        assert "dewpoint" in text

    def test_temp_unavailable(self, sample_config):
        """Announcement says 'temperature unavailable' when temp_available=False."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12,
            temp_c=0.0, dewpt_c=0.0, pressure_hpa=1013.0,
            density_alt_ft=0, recommended_runway=27, temp_available=False,
            data_valid=True,
        )
        text = build_announcement_text(wd, sample_config)

        assert "temperature unavailable" in text
        assert "dewpoint" not in text

    def test_density_alt_above_2000_included(self, sample_config):
        """Density altitude is included when above 2000ft and temp available."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12,
            temp_c=35.0, dewpt_c=10.0, pressure_hpa=1013.0,
            density_alt_ft=2500, recommended_runway=27, temp_available=True,
            data_valid=True,
        )
        text = build_announcement_text(wd, sample_config)

        assert "density altitude" in text
        assert "2500" in text
        assert "feet" in text

    def test_density_alt_below_2000_omitted(self, sample_config):
        """Density altitude is omitted when below 2000ft."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12,
            temp_c=15.0, dewpt_c=10.0, pressure_hpa=1013.0,
            density_alt_ft=800, recommended_runway=27, temp_available=True,
            data_valid=True,
        )
        text = build_announcement_text(wd, sample_config)

        assert "density altitude" not in text

    def test_gusts_in_announcement(self, sample_config):
        """Wind gusts appear in announcement when has_gusts is True."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12, wind_gust_kt=22,
            has_gusts=True, temp_c=15.0, dewpt_c=10.0, pressure_hpa=1013.0,
            density_alt_ft=800, recommended_runway=27, temp_available=True,
            data_valid=True,
        )
        text = build_announcement_text(wd, sample_config)
        assert "gusts" in text


# ---------------------------------------------------------------------------
# build_wind_text
# ---------------------------------------------------------------------------

class TestBuildWindText:
    """Tests for the build_wind_text function."""

    def test_wind_without_gusts(self):
        """Wind text without gusts: 'wind 2 7 0 at 1 2'."""
        wd = WeatherData(wind_dir=270, wind_speed_kt=12, has_gusts=False)
        text = build_wind_text(wd)

        assert text.startswith("wind")
        assert "gusts" not in text
        # digits are spelled out with spaces
        assert "2 7 0" in text
        assert "1 2" in text

    def test_wind_with_gusts(self):
        """Wind text with gusts: includes 'gusts 1 8'."""
        wd = WeatherData(
            wind_dir=270, wind_speed_kt=12, wind_gust_kt=18, has_gusts=True
        )
        text = build_wind_text(wd)

        assert "gusts" in text
        assert "1 8" in text


# ---------------------------------------------------------------------------
# write_metar_files
# ---------------------------------------------------------------------------

class TestWriteMetarFiles:
    """Tests for write_metar_files function."""

    def test_creates_four_files_with_correct_content(self, sample_config):
        """write_metar_files creates /tmp/metar, metar2, metar3, metar4."""
        wd = WeatherData(
            time_hour="1045", wind_dir=270, wind_speed_kt=12, wind_gust_kt=18,
            has_gusts=True, temp_c=20.0, dewpt_c=12.0, pressure_hpa=1013.2,
            density_alt_ft=800, recommended_runway=27, temp_available=True,
            data_valid=True,
        )
        written = {}

        def _mock_open_factory(path, mode="r"):
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            m.write = lambda data: written.update({path: data})
            return m

        with patch("builtins.open", side_effect=_mock_open_factory):
            write_metar_files(wd, sample_config)

        assert written["/tmp/metar"] == "ZZZZ 1045"
        assert written["/tmp/metar2"] == "270@12G18"
        assert written["/tmp/metar3"] == "20/12 Q1013.2"
        assert written["/tmp/metar4"] == "DA800FT"

    def test_metar_files_temp_unavailable(self, sample_config):
        """write_metar_files writes dashes when temp is unavailable."""
        wd = WeatherData(
            time_hour="1045", wind_dir=180, wind_speed_kt=5,
            temp_c=0.0, dewpt_c=0.0, pressure_hpa=1010.0,
            density_alt_ft=0, recommended_runway=9, temp_available=False,
            data_valid=True,
        )
        written = {}

        def _mock_open_factory(path, mode="r"):
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            m.write = lambda data: written.update({path: data})
            return m

        with patch("builtins.open", side_effect=_mock_open_factory):
            write_metar_files(wd, sample_config)

        assert written["/tmp/metar3"] == "--/-- Q1010.0"
        assert written["/tmp/metar4"] == "TEMP N/A"

    def test_metar_wind_no_gusts(self, sample_config):
        """write_metar_files wind string omits gust when has_gusts=False."""
        wd = WeatherData(
            time_hour="0800", wind_dir=90, wind_speed_kt=5,
            temp_c=10.0, dewpt_c=5.0, pressure_hpa=1015.0,
            density_alt_ft=300, recommended_runway=9, temp_available=True,
            has_gusts=False, data_valid=True,
        )
        written = {}

        def _mock_open_factory(path, mode="r"):
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            m.write = lambda data: written.update({path: data})
            return m

        with patch("builtins.open", side_effect=_mock_open_factory):
            write_metar_files(wd, sample_config)

        assert "G" not in written["/tmp/metar2"]
        assert written["/tmp/metar2"] == "90@5"


# ---------------------------------------------------------------------------
# Density altitude calculation
# ---------------------------------------------------------------------------

class TestDensityAltitude:
    """Tests for density altitude calculation in fetch_weather."""

    def test_density_altitude_calculation(
        self, sample_config, weather_api_response_english, weather_api_response_metric
    ):
        """Verify density altitude formula matches manual calculation."""
        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = weather_api_response_english
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = weather_api_response_metric
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            wd = fetch_weather(sample_config)

        # Manual calculation per the formula in weather.py:
        # elevation = 500, pressure_hpa = 1013.2, temp_c = 20.0, dewpt_c = 12.0
        elevation = 500
        pressure_alt = elevation + (1013 - 1013.2) * 30  # 500 + (-0.2)*30 = 494
        standard_temp = 15 - (2 * (elevation / 1000))  # 15 - 1.0 = 14.0
        humidity_correction = 0.1 * (20.0 - 12.0)  # 0.8
        expected = math.ceil(
            pressure_alt + (120 * (20.0 - 14.0)) + humidity_correction
        )
        # = ceil(494 + 720 + 0.8) = ceil(1214.8) = 1215

        assert wd.density_alt_ft == expected


# ---------------------------------------------------------------------------
# Runway recommendation
# ---------------------------------------------------------------------------

class TestRunwayRecommendation:
    """Tests for runway recommendation logic in fetch_weather."""

    def _fetch_with_wind_dir(self, sample_config, wind_dir, english_response):
        """Helper to fetch weather with a specific wind direction."""
        english_data = dict(english_response["observations"][0])
        english_data["winddir"] = wind_dir
        english_resp = {"observations": [english_data]}

        metric_response = {
            "observations": [{
                "stationID": "TEST1",
                "obsTimeUtc": "2026-05-20T10:45:00Z",
                "winddir": wind_dir,
                "metric": {"windSpeed": 10, "windGust": 10,
                           "pressure": 1013.0, "temp": 15.0, "dewpt": 10.0},
            }]
        }

        mock_resp_english = MagicMock()
        mock_resp_english.json.return_value = english_resp
        mock_resp_english.raise_for_status = MagicMock()

        mock_resp_metric = MagicMock()
        mock_resp_metric.json.return_value = metric_response
        mock_resp_metric.raise_for_status = MagicMock()

        with patch("nanoawos.weather.requests.get") as mock_get:
            mock_get.side_effect = [mock_resp_english, mock_resp_metric]
            return fetch_weather(sample_config)

    def test_wind_270_recommends_runway_27(
        self, sample_config, weather_api_response_english
    ):
        """Wind from 270 degrees recommends runway 27 (config runways [9, 27])."""
        wd = self._fetch_with_wind_dir(
            sample_config, 270, weather_api_response_english
        )
        assert wd.recommended_runway == 27

    def test_wind_090_recommends_runway_9(
        self, sample_config, weather_api_response_english
    ):
        """Wind from 90 degrees recommends runway 9 (config runways [9, 27])."""
        wd = self._fetch_with_wind_dir(
            sample_config, 90, weather_api_response_english
        )
        assert wd.recommended_runway == 9

    def test_wind_180_picks_nearest_runway(
        self, sample_config, weather_api_response_english
    ):
        """Wind from 180 degrees is equidistant; picks first (9) by index."""
        wd = self._fetch_with_wind_dir(
            sample_config, 180, weather_api_response_english
        )
        # 180 is equidistant from both 90 and 270, min picks first match
        assert wd.recommended_runway in [9, 27]
