#!/usr/bin/env python3

#Python Script for brightness control and i3blocks

import sys
import math

with open("/sys/class/backlight/intel_backlight/max_brightness",  "r") as rd:
    max_brightness = rd.read()
with open("/sys/class/backlight/intel_backlight/actual_brightness",  "r") as rd:
    actual_brightness = rd.read()

max_brightness = int(max_brightness.strip())
actual_brightness = int(actual_brightness.strip())

current_brightness = math.floor(((actual_brightness+10) / max_brightness) * 100)
print(f'<span color="#00ef00"> {current_brightness}</span>')

if len(sys.argv) > 1:
    current_brightness += int(sys.argv[1])

    if current_brightness >= max_brightness:
        print(f'<span color="#0000cc"> {current_brightness}</span>')
        print(f'<span color="#00ef00"> {current_brightness}</span>')
    else:
        with open("/sys/class/backlight/intel_backlight/brightness", "w") as wr:
            wr.write(str(current_brightness))
        if current_brightness < 50:
            print(f'<span color="#e6e600"> {current_brightness}</span>')
        elif current_brightness < 10:
            print(f'<span color="#ff0000"> {current_brightness}</span>')
        else:
            print(f'<span color="#00ef00"> {current_brightness}</span>')