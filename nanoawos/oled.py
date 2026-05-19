#!/usr/bin/env python3
"""NanoAWOS OLED display driver for NanoHat OLED 128x64.

This script is deployed to:
  /root/NanoHatOLED/BakeBit/Software/Python/bakebit_nanohat_oled.py

It is launched by the NanoHatOLED binary which handles GPIO button
interrupts and sends SIGUSR1/SIGUSR2/SIGALRM signals for K1/K2/K3.

Pages:
  0: Tap count + TX state + time
  1: System info (IP, CPU, mem, disk, temp)
  3: Play weather? -> No selected
  4: Play weather? -> Yes selected
  5: Playing weather (triggers MPC)
  6: METAR weather data display
"""

from __future__ import print_function
import bakebit_128_64_oled as oled
from PIL import Image, ImageFont, ImageDraw
import time
import subprocess
import threading
import signal
import os
import socket

WIDTH = 128
HEIGHT = 64

pageCount = 2
pageIndex = 0
showPageIndicator = False
drawing = False
lock = threading.Lock()

oled.init()
oled.setNormalDisplay()
oled.setHorizontalMode()

image = Image.new('1', (WIDTH, HEIGHT))
draw = ImageDraw.Draw(image)
fontb24 = ImageFont.truetype('DejaVuSansMono-Bold.ttf', 24)
font14 = ImageFont.truetype('DejaVuSansMono.ttf', 14)
smartFont = ImageFont.truetype('DejaVuSansMono-Bold.ttf', 10)
fontb14 = ImageFont.truetype('DejaVuSansMono-Bold.ttf', 14)
font11 = ImageFont.truetype('DejaVuSansMono.ttf', 11)


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def _read_file(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def draw_page():
    global drawing

    lock.acquire()
    if drawing:
        lock.release()
        return
    drawing = True
    page_index = pageIndex
    lock.release()

    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)

    # Page indicator dots
    if showPageIndicator:
        dotW, dotPad = 4, 2
        dotX = WIDTH - dotW - 1
        dotTop = (HEIGHT - pageCount * dotW - (pageCount - 1) * dotPad) // 2
        for i in range(pageCount):
            fill = 255 if i == page_index else 0
            draw.rectangle((dotX, dotTop, dotX + dotW, dotTop + dotW),
                           outline=255, fill=fill)
            dotTop += dotW + dotPad

    if page_index == 0:
        # Main page: tap count, TX state, time
        tap = _read_file("/tmp/tap", "0")
        tx = _read_file("/sys/class/gpio/gpio201/value", "0")
        draw.text((2, 2), f"T:{tap} TX:{tx}", font=fontb24, fill=255)
        draw.text((2, 40), time.strftime("%X"), font=fontb24, fill=255)

    elif page_index == 1:
        # System info page
        x, top = 0, 0
        ip = get_ip()
        try:
            cpu = subprocess.check_output(
                "top -bn1 | grep load | awk '{printf \"CPU: %.2f\", $(NF-2)}'",
                shell=True).decode()
        except Exception:
            cpu = "CPU: ?"
        try:
            mem = subprocess.check_output(
                "free -m | awk 'NR==2{printf \"Mem: %s/%sMB\", $3,$2}'",
                shell=True).decode()
        except Exception:
            mem = "Mem: ?"
        try:
            disk = subprocess.check_output(
                "df -h | awk '$NF==\"/\"{printf \"Disk: %d/%dGB %s\", $3,$2,$5}'",
                shell=True).decode()
        except Exception:
            disk = "Disk: ?"
        try:
            temp_raw = int(open('/sys/class/thermal/thermal_zone0/temp').read())
            temp_c = temp_raw // 1000 if temp_raw > 1000 else temp_raw
        except Exception:
            temp_c = "?"
        draw.text((x, top + 5), f"IP: {ip}", font=smartFont, fill=255)
        draw.text((x, top + 17), cpu, font=smartFont, fill=255)
        draw.text((x, top + 29), mem, font=smartFont, fill=255)
        draw.text((x, top + 41), disk, font=smartFont, fill=255)
        draw.text((x, top + 53), f"CPU TEMP: {temp_c}C", font=smartFont, fill=255)

    elif page_index == 3:
        # Play weather? -> No selected
        draw.text((2, 2), 'Play weather?', font=fontb14, fill=255)
        draw.rectangle((2, 20, WIDTH - 4, 36), outline=0, fill=0)
        draw.text((4, 22), 'Yes', font=font11, fill=255)
        draw.rectangle((2, 38, WIDTH - 4, 54), outline=0, fill=255)
        draw.text((4, 40), 'No', font=font11, fill=0)

    elif page_index == 4:
        # Play weather? -> Yes selected
        draw.text((2, 2), 'Play weather?', font=fontb14, fill=255)
        draw.rectangle((2, 20, WIDTH - 4, 36), outline=0, fill=255)
        draw.text((4, 22), 'Yes', font=font11, fill=0)
        draw.rectangle((2, 38, WIDTH - 4, 54), outline=0, fill=0)
        draw.text((4, 40), 'No', font=font11, fill=255)

    elif page_index == 5:
        draw.text((2, 2), 'Playing weather', font=fontb14, fill=255)
        draw.text((2, 20), 'Please wait', font=font11, fill=255)

    elif page_index == 6:
        # METAR weather display
        x, top = 0, 0
        metar = _read_file("/tmp/metar", "NO DATA")
        metar2 = _read_file("/tmp/metar2", "---")
        metar3 = _read_file("/tmp/metar3", "---")
        metar4 = _read_file("/tmp/metar4", "---")
        draw.text((x, top + 5), metar, font=fontb14, fill=255)
        draw.text((x, top + 17), metar2, font=fontb14, fill=255)
        draw.text((x, top + 29), metar3, font=fontb14, fill=255)
        draw.text((x, top + 41), metar4, font=fontb14, fill=255)

    oled.drawImage(image)

    lock.acquire()
    drawing = False
    lock.release()


def update_page_index(pi):
    global pageIndex
    lock.acquire()
    pageIndex = pi
    lock.release()


def is_showing_power_msgbox():
    lock.acquire()
    pi = pageIndex
    lock.release()
    return pi in (3, 4)


def receive_signal(signum, stack):
    global pageIndex

    lock.acquire()
    page_index = pageIndex
    lock.release()

    if page_index == 5:
        return

    if signum == signal.SIGUSR1:  # K1
        if is_showing_power_msgbox():
            update_page_index(4 if page_index == 3 else 3)
        else:
            update_page_index(6)
        draw_page()

    elif signum == signal.SIGUSR2:  # K2
        if is_showing_power_msgbox():
            if page_index == 4:
                update_page_index(5)
            else:
                update_page_index(0)
        else:
            update_page_index(1)
        draw_page()

    elif signum == signal.SIGALRM:  # K3
        if is_showing_power_msgbox():
            update_page_index(0)
        else:
            update_page_index(3)
        draw_page()


# Splash screen
try:
    image0 = Image.open('nanoawos.png').convert('1')
    oled.drawImage(image0)
except Exception:
    pass
time.sleep(2)

signal.signal(signal.SIGUSR1, receive_signal)
signal.signal(signal.SIGUSR2, receive_signal)
signal.signal(signal.SIGALRM, receive_signal)

while True:
    try:
        draw_page()

        lock.acquire()
        page_index = pageIndex
        lock.release()

        if page_index == 5:
            subprocess.run(['mpc', 'clear'])
            subprocess.run(['mpc', 'load', 'full'])
            subprocess.run(['mpc', 'play'])
            time.sleep(5)
            pageIndex = 0
        elif page_index == 1:
            time.sleep(1)
        else:
            time.sleep(0.2)
    except KeyboardInterrupt:
        break
    except IOError:
        print("OLED IO Error")
