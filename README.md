# NanoAWOS

**Nano Automated Weather Observing System** -- an open-source AWOS platform built on the NanoPi NEO v1.4 that broadcasts weather observations over VHF radio, triggered by PTT clicks from pilots.

Designed for small uncontrolled airfields (CTAF/ATZ) where no official AWOS/ATIS exists. Connect it to a YAESU FT-550 (or similar VHF radio), point it at a Weather Underground personal weather station, and pilots can request real-time weather reports by clicking their PTT button.

**4 clicks** = wind only | **6 clicks** = full METAR-style report

## Demo

[![NanoAWOS Demo](https://i9.ytimg.com/vi/fD7kZr5AIl4/mqdefault.jpg?v=66d61968&sqp=CMCy2LYG-oaymwEmCMACELQB8quKqQMa8AEB-AH-CYAC0AWKAgwIABABGGUgXihRMA8=&rs=AOn4CLDl5MSHxLvJRHZshJltsfTD1HXbqA)](https://youtu.be/fD7kZr5AIl4)
[![NanoAWOS Demo 2](https://i9.ytimg.com/vi/fD7kZr5AIl4/mqdefault.jpg?v=66d61968&sqp=CMCy2LYG-oaymwEmCMACELQB8quKqQMa8AEB-AH-CYAC0AWKAgwIABABGGUgXihRMA8=&rs=AOn4CLDl5MSHxLvJRHZshJltsfTD1HXbqA)](https://www.youtube.com/watch?v=trcSWJSmVco)

## Features

### Weather Broadcasting
- **Full weather report** (6 clicks): station ID, time (Zulu), wind direction/speed/gusts, temperature, dewpoint, QNH, density altitude, recommended runway
- **Wind only** (4 clicks): wind direction, speed, gusts
- **Neural TTS** via [Piper](https://github.com/rhasspy/piper) -- natural-sounding voice, runs locally on ARM
- **Cloud TTS** option via OpenAI API for higher quality
- **Legacy WAV concatenation** as offline fallback (phonetic alphabet pre-recordings)
- Weather data from [Weather Underground](https://www.wunderground.com/) Personal Weather Station API
- Updates every 5 minutes via systemd timer
- Handles sensor outages gracefully ("temperature unavailable")

### Audio Pipeline
- **Icecast/DarkIce streaming** -- live radio audio streamed as Opus over HTTP
- **ALSA dsnoop** -- shared microphone access between click detector, DarkIce, and transcription
- **MPD (Music Player Daemon)** -- manages TTS audio playlists
- **GPIO PTT relay** -- Panasonic AQY211EH silicon relay controlled via GPIO 201
- Click detection pauses during playback to prevent false triggers

### Radio Transcription (AI-powered)
- Records pilot radio transmissions, sends to **OpenAI Whisper** for speech-to-text
- Automatic language detection (English + Polish, configurable)
- **GPT-4o-mini** extracts actionable items: position reports, landing intentions, emergencies
- Live transcription log in web UI
- Configurable min/max duration, silence detection

### Web UI
- Real-time weather dashboard with play controls
- Live click detection monitor (amplitude bar, threshold, state)
- Radio transcription log with extracted actions
- Icecast audio stream player
- Service status monitoring (6 services)
- Full configuration editor (station, API keys, TTS engine, thresholds, transcription)
- Dark theme, responsive design

### OLED Display
- 128x64 NanoHat OLED with 3-button control (K1/K2/K3)
- Pages: tap count + TX state, system info, METAR display, play weather dialog
- Signal-based button handling (SIGUSR1/SIGUSR2/SIGALRM)

### Integrations
- **MQTT** to Home Assistant (PTT state, click events)
- **Flightradar/FlightAware** via Home Assistant for traffic announcements
- **Icecast** audio streaming over IP

## Architecture

```
                    +-----------+
  VHF Radio <------>| Audio     |<--- Mic (Line In)
  (YAESU FT-550)   | Jack 4pin |---> Speaker (Line Out)
                    +-----+-----+---> PTT (GPIO 201 via relay)
                          |
                 +--------+--------+
                 |  NanoPi NEO     |
                 |  Ubuntu 22.04   |
                 |                 |
                 |  +-----------+  |     +------------------+
  Weather API ---|->| weather.py|--|--->| Piper TTS        |
  (Wunderground) |  +-----------+  |    | (neural voice)   |
                 |        |        |    +--------+---------+
                 |        v        |             |
                 |  +-----------+  |    +--------v---------+
                 |  | audio.py  |<-|--->| MPD              |
                 |  | (GPIO PTT)|  |    | (playlist mgmt)  |
                 |  +-----------+  |    +------------------+
                 |                 |
                 |  +-----------+  |    +------------------+
  Mic (dsnoop)---|->| tap.py    |--|--->| Click detection  |
                 |  +-----------+  |    | 4=wind, 6=full   |
                 |                 |    +------------------+
                 |  +-----------+  |
  Mic (dsnoop)---|->|transcribe |--|--->  OpenAI Whisper
                 |  |   .py     |  |      + GPT actions
                 |  +-----------+  |
                 |                 |
                 |  +-----------+  |    +------------------+
  Mic (dsnoop)---|->| DarkIce   |--|--->| Icecast server   |
                 |  +-----------+  |    | :8000/epmy       |
                 |                 |    +------------------+
                 |  +-----------+  |
                 |  | oled.py   |  |    128x64 OLED display
                 |  +-----------+  |
                 |                 |
                 |  +-----------+  |
                 |  | web UI    |--|--->  Flask :8080
                 |  +-----------+  |
                 +--------+--------+
                          |
                     LAN (eth0)
```

## Project Structure

```
nanoawos/
  README.md
  requirements.txt              # Python dependencies
  install.sh                    # Automated deployment script
  img/                          # Hardware assembly photos & diagrams
  config/
    nanoawos.yaml               # Main configuration file
    asound.conf                 # ALSA dsnoop for shared mic access
    darkice.cfg                 # Icecast audio streamer config
    icecast.xml                 # Icecast server config
    mpd.conf                    # Music Player Daemon config
    systemd/
      nanoawos-weather.service  # Weather fetch + TTS (oneshot)
      nanoawos-weather.timer    # Triggers weather every 5 min
      nanoawos-tap.service      # Click detector daemon
      nanoawos-gpio.service     # GPIO PTT controller daemon
      nanoawos-web.service      # Flask web UI
      nanoawos-transcribe.service  # Radio transcription (optional)
  nanoawos/                     # Python package (v2.0.0)
    __init__.py
    config.py                   # YAML config loader
    weather.py                  # Weather API + announcement builder
    tts.py                      # TTS engine (Piper / OpenAI / WAV concat)
    audio.py                    # MPD playlists + GPIO PTT control
    tap.py                      # PTT click detector with auto-calibration
    oled.py                     # NanoHat OLED display driver
    transcribe.py               # Radio transcription (Whisper + GPT)
    web/
      app.py                    # Flask app with REST API
      templates/index.html      # Dashboard SPA
      static/style.css          # Dark theme
  scripts/                      # Legacy v1.0 scripts (preserved)
  tts/                          # Pre-recorded WAV phonetic alphabet
```

## Hardware Requirements

| Component | Purpose |
|-----------|---------|
| [NanoPi NEO Starter Kit](https://www.friendlyelec.com/index.php?route=product/product&product_id=190) | SBC with NanoHat OLED, audio codec, GPIO |
| [4-pin 17.5mm jack to RCA](https://pl.aliexpress.com/item/1005005704133516.html) | Extended audio connector for mic + PTT |
| Panasonic AQY211EH | Silicon relay for PTT control via GPIO |
| 100 Ohm resistor | Current limiting for relay |
| Standard 4-pin jack with RCA | Radio-side cable (to YAESU) |
| YAESU FT-550 | VHF radio (DC powered, no batteries needed) |

## Hardware Assembly

### 1. Add 4-pin support to the NanoHAT audio jack

Extend the standard 3-pin stereo jack to 4 pins using a small spring and epoxy.

**Materials:** small spring (~3mm diameter), wire, isolation tape, glue gun

1. Disassemble the NanoPi kit to access the NanoHat board
2. Solder a wire to the spring
3. Tape/isolate the board SMD resistors around the jack area
4. Insert the 17.5mm jack while holding the spring with wire in place
5. Verify all 4 pins with a multimeter
6. Glue the jack socket, wait 3 minutes, re-verify

The 4th pin connects to the microphone pins of the NanoHat:

![Mic pin location](img/micpin.png)

Spring placement (bend toward the right, away from ethernet port):

![Jack assembly](img/jack1.JPEG)

After hot glue:

![Glued jack](img/jack2.JPEG)

### 2. Remove one stereo channel for PTT

One stereo channel is repurposed for PTT relay control.

1. Remove the pin plastic cover
2. Remove the metal connector for one channel
3. Replace the plastic cover

![Removing PTT pin](img/PTT_PIn.JPEG)
![PTT pin diagram](img/ptt_pin.png)

### 3. Solder the silicon relay

Connect the Panasonic AQY211EH relay between the PTT pin and GPIO 201.

![Relay schematic](img/relayschema.png)

Reference the [NanoHat pinout](https://wiki.friendlyelec.com/wiki/index.php/NanoPi_NEO#Diagram.2C_Layout_and_Dimension) to confirm GPIO pin locations.

![Relay soldered](img/relay.JPEG)
![Final assembly](img/final.JPEG)

## Software Installation

### Prerequisites

- NanoPi NEO with Ubuntu 22.04 (FriendlyELEC image)
- MPD, Icecast2, DarkIce installed
- Python 3.10+
- Internet connection (for Weather Underground API)

### Deploy

```bash
# On your workstation
git clone https://github.com/mdrobniu/nanoawos.git
cd nanoawos

# Copy to the NanoPi
scp -r . pi@<nanopi-ip>:/tmp/nanoawos/
ssh pi@<nanopi-ip>

# On the NanoPi
sudo mkdir -p /opt/nanoawos
sudo cp -a /tmp/nanoawos/* /opt/nanoawos/
sudo /opt/nanoawos/install.sh
```

The installer will:
1. Install Python dependencies (Flask, PyYAML, requests, python-mpd2)
2. Create default configuration at `/opt/nanoawos/config/nanoawos.yaml`
3. Install and enable 5 systemd services + 1 timer
4. Disable legacy cron jobs and old services
5. Update the OLED display script
6. Start all services and trigger first weather update

### Verify

```bash
# Check all services
for svc in nanoawos-weather.timer nanoawos-tap nanoawos-gpio nanoawos-web; do
    echo "$svc: $(systemctl is-active $svc)"
done

# Web UI
curl http://localhost:8080/api/status

# Trigger weather update manually
sudo systemctl start nanoawos-weather.service

# View logs
journalctl -u nanoawos-tap -f      # Click detector
journalctl -u nanoawos-gpio -f     # PTT controller
journalctl -u nanoawos-weather -f  # Weather updates
```

## Configuration

All settings in `/opt/nanoawos/config/nanoawos.yaml` (or via web UI at `:8080`):

### Station

```yaml
station:
  id: "YOUR_STATION_ID"      # Weather Underground station ID
  icao: "XXXX"              # ICAO airport code
  name: "echo papa mike yankee"  # Spoken station identification
  elevation_ft: 230         # Field elevation (for density altitude)
  runways: [15, 33]         # Runway headings (for wind recommendation)
```

### Weather API

```yaml
weather:
  api_key: "your-wunderground-api-key"
  update_interval_sec: 300  # Fetch every 5 minutes
```

### Text-to-Speech

```yaml
tts:
  engine: "piper"           # "piper" (local), "cloud" (OpenAI), "wav_concat" (legacy)
  piper_model: "/mnt/p4/models/en_US-ryan-medium.onnx"
  # For cloud TTS:
  cloud_api_key: "sk-..."
  cloud_voice: "nova"       # alloy, echo, fable, onyx, nova, shimmer
```

### Click Detection

```yaml
tap:
  amplitude_threshold: 0.2  # Auto-calibrated on startup
  quiet_blocks: 25          # Silence before click is registered
  short_clicks: 4           # Wind only
  long_clicks: 6            # Full weather
  calibration_seconds: 2    # Startup noise floor sampling
```

### Radio Transcription (optional)

```yaml
transcribe:
  enabled: true
  openai_api_key: "sk-..."
  language: "auto"          # Auto-detect English/Polish (or force "en"/"pl")
  extract_actions: true     # GPT extracts actionable items
  action_model: "gpt-4o-mini"
  min_duration_sec: 0.5     # Ignore very short transmissions
  max_duration_sec: 60      # Max recording length
```

Enable with: `sudo systemctl enable --now nanoawos-transcribe`

## Services

| Service | Type | Purpose |
|---------|------|---------|
| `nanoawos-weather.timer` | Timer | Triggers weather fetch every 5 min |
| `nanoawos-weather.service` | Oneshot | Fetches weather, synthesizes TTS, updates playlists |
| `nanoawos-tap` | Daemon | Listens for PTT clicks, triggers playback |
| `nanoawos-gpio` | Daemon | Watches MPD state, controls PTT relay via GPIO |
| `nanoawos-web` | Daemon | Flask web UI on port 8080 |
| `nanoawos-transcribe` | Daemon | Records + transcribes radio transmissions (optional) |

Supporting services (system-managed):
- **mpd** -- Music Player Daemon (audio playback)
- **darkice** -- Streams mic audio to Icecast (Opus 128kbps)
- **icecast2** -- HTTP audio streaming server on port 8000

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Web dashboard |
| GET | `/api/weather` | Current weather data |
| GET | `/api/status` | All service statuses + weather + uptime |
| GET | `/api/tap` | Live click detector debug (amplitude, threshold, state) |
| GET | `/api/transcriptions` | Recent radio transcriptions (last 50) |
| GET | `/api/config` | Current configuration |
| PUT | `/api/config` | Update configuration |
| POST | `/api/play/full` | Play full weather report |
| POST | `/api/play/wind` | Play wind-only report |
| POST | `/api/weather/refresh` | Trigger immediate weather update |
| POST | `/api/service/<name>/<action>` | Control service (restart/stop/start) |

## Audio Architecture

```
  Radio Mic
      |
  [H3 Audio Codec hw:2,0]
      |
  [ALSA dsnoop] ---- shared capture @ 44100Hz
      |         \              \
  [tap.py]   [DarkIce]    [transcribe.py]
  click det   48kHz Opus    record + Whisper
              |
          [Icecast :8000/epmy]
              |
          [Web UI audio player]

  [weather.py] -> [Piper TTS] -> full.wav / wind.wav
                                      |
                                  [MPD playlists]
                                      |
                              [H3 Audio Codec hw:2,0]
                                      |
                                  [Radio Speaker]
                                      |
                              [GPIO 201 -> Relay -> PTT]
```

## Weather Report Format

Example full report (6 clicks):

> "Echo Papa Mike Yankee, 1 0 4 5 zulu, weather wind 3 0 9 at 7 gusts 8,
> temperature 2 1 dewpoint 1 2, QNH 1 0 1 5, density altitude 1250 feet,
> recommended runway is 3 3"

When temperature sensor is offline:

> "...temperature unavailable, QNH 1 0 1 5, recommended runway is 3 3"

Wind-only report (4 clicks):

> "wind 3 0 9 at 7 gusts 8"

## Density Altitude Calculation

```
pressure_altitude = field_elevation + (1013 - QNH) * 30
standard_temp = 15 - (2 * field_elevation / 1000)
humidity_correction = 0.1 * (temp - dewpoint)
density_altitude = pressure_altitude + 120 * (temp - standard_temp) + humidity_correction
```

Only announced when > 2000 ft and temperature data is available.

## Development

```bash
# Run modules locally (with PYTHONPATH set)
cd /opt/nanoawos
PYTHONPATH=. python3 -m nanoawos.weather    # One-shot weather update
PYTHONPATH=. python3 -m nanoawos.tap        # Click detector
PYTHONPATH=. python3 -m nanoawos.audio      # GPIO watcher
PYTHONPATH=. python3 -m nanoawos.web.app    # Web UI
PYTHONPATH=. python3 -m nanoawos.transcribe # Transcription service

# View real-time debug
cat /tmp/tap_debug          # amplitude threshold clicks noisy quiet state
cat /tmp/tap                # last click count
cat /tmp/metar              # station + time
cat /tmp/metar2             # wind
cat /tmp/metar3             # temp/dewpt + QNH
cat /tmp/metar4             # density altitude
```

## Disclaimer

This project is an open-source initiative designed for small aviation purposes. It has **not** been certified, endorsed, or approved by any aviation regulatory authority, including ICAO, FAA, or EASA.

Users are responsible for ensuring compliance with all applicable laws, regulations, and safety standards. This software is provided "as-is" without any warranties. Use at your own risk.
