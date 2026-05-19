#!/bin/bash
set -e

INSTALL_DIR="/opt/nanoawos"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== NanoAWOS Installer ==="
echo "Source: $SCRIPT_DIR"
echo "Target: $INSTALL_DIR"

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (use sudo)"
    exit 1
fi

# --- Copy project files ---
echo "[1/7] Copying project files..."
# When source == target (deployed via tar), skip copy
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
    cp -a "$SCRIPT_DIR/nanoawos" "$INSTALL_DIR/"
    cp -a "$SCRIPT_DIR/config" "$INSTALL_DIR/"
    cp -a "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
    cp -a "$SCRIPT_DIR/install.sh" "$INSTALL_DIR/"
fi

# --- Install Python dependencies ---
echo "[2/7] Installing Python dependencies..."
pip3 install -q flask pyyaml requests python-mpd2 2>/dev/null || true

# --- Copy config if not exists ---
echo "[3/7] Setting up configuration..."
if [ ! -f "$INSTALL_DIR/config/nanoawos.yaml" ]; then
    cp "$SCRIPT_DIR/config/nanoawos.yaml" "$INSTALL_DIR/config/nanoawos.yaml"
    echo "  Created default config"
else
    echo "  Config already exists, keeping current"
fi

# --- Install systemd services ---
echo "[4/7] Installing systemd services..."
for svc in nanoawos-weather.service nanoawos-weather.timer \
           nanoawos-tap.service nanoawos-gpio.service nanoawos-web.service; do
    cp "$INSTALL_DIR/config/systemd/$svc" "/etc/systemd/system/$svc"
    echo "  Installed $svc"
done

# --- Stop old services ---
echo "[5/7] Stopping old services..."
systemctl stop tap.service 2>/dev/null || true
systemctl disable tap.service 2>/dev/null || true
systemctl stop mpd_watch_gpio.service 2>/dev/null || true
systemctl disable mpd_watch_gpio.service 2>/dev/null || true

# Remove old cron entry
crontab -l 2>/dev/null | grep -v '/sbin/wunderground.py' | crontab - 2>/dev/null || true
echo "  Removed old cron job and services"

# --- Update OLED script ---
echo "[6/7] Updating OLED display script..."
OLED_DIR="/root/NanoHatOLED/BakeBit/Software/Python"
if [ -d "$OLED_DIR" ]; then
    cp "$OLED_DIR/bakebit_nanohat_oled.py" "$OLED_DIR/bakebit_nanohat_oled.py.bak"
    cp "$INSTALL_DIR/nanoawos/oled.py" "$OLED_DIR/bakebit_nanohat_oled.py"
    echo "  OLED script updated (backup at .bak)"
    # Restart OLED by restarting the binary
    pkill -f NanoHatOLED 2>/dev/null || true
    sleep 1
    /usr/local/bin/oled-start &
    echo "  OLED restarted"
else
    echo "  WARN: NanoHatOLED directory not found, skipping OLED update"
fi

# --- Enable and start new services ---
echo "[7/7] Enabling and starting services..."
systemctl daemon-reload

systemctl enable nanoawos-weather.timer
systemctl start nanoawos-weather.timer
echo "  Weather timer enabled (every 5 min)"

systemctl enable nanoawos-tap.service
systemctl restart nanoawos-tap.service
echo "  Click detector started"

systemctl enable nanoawos-gpio.service
systemctl restart nanoawos-gpio.service
echo "  GPIO PTT controller started"

systemctl enable nanoawos-web.service
systemctl restart nanoawos-web.service
echo "  Web UI started"

# Trigger first weather update
systemctl start nanoawos-weather.service &

echo ""
echo "=== NanoAWOS installed successfully ==="
echo "  Web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "  Config: $INSTALL_DIR/config/nanoawos.yaml"
echo ""
echo "Services:"
for svc in nanoawos-weather.timer nanoawos-tap nanoawos-gpio nanoawos-web; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
    echo "  $svc: $status"
done
