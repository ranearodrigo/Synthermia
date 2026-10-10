"""Record the bundled sample in a real PTY and render its output as a GIF.

Requires Pillow and pyte, separately from the application's dependencies.
"""

import argparse
import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

from PIL import Image, ImageDraw, ImageFont
import pyte


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "docs" / "demo.gif")
    parser.add_argument("--font", default="/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    args = parser.parse_args()
    columns, rows = 112, 32
    font = ImageFont.truetype(args.font, 13)
    cell_width, cell_height = 8, 18
    background = (17, 21, 29)
    colors = {"default": "#d7dde8", "black": "#000000", "white": "#ffffff",
              "red": "#cc5555", "green": "#55cc55", "brown": "#cccc55",
              "blue": "#5555cc", "magenta": "#cc55cc", "cyan": "#55cccc",
              "brightblack": "#555555", "brightwhite": "#ffffff",
              "brightred": "#ff5555", "brightgreen": "#55ff55", "brightbrown": "#ffff55",
              "brightblue": "#5555ff", "brightmagenta": "#ff55ff", "brightcyan": "#55ffff"}

    def color(value, default):
        if value == "default":
            return default
        return colors[value] if value in colors else "#" + value

    screen = pyte.Screen(columns, rows)
    stream = pyte.ByteStream(screen)
    frames = []

    def snapshot():
        frame = Image.new("RGB", (columns * cell_width, rows * cell_height), background)
        draw = ImageDraw.Draw(frame)
        for y, line in screen.buffer.items():
            for x, char in line.items():
                fg = color(char.fg, colors["default"])
                bg = color(char.bg, background)
                if char.reverse:
                    fg, bg = bg, fg
                left, top = x * cell_width, y * cell_height
                draw.rectangle((left, top, left + cell_width - 1, top + cell_height - 1), fill=bg)
                if char.data.strip():
                    draw.text((left, top), char.data, font=font, fill=fg)
        frames.append(frame)

    with tempfile.TemporaryDirectory(prefix="synthermia-recording-") as folder:
        master, slave = pty.openpty()
        proc = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=folder,
                       DBUS_SESSION_BUS_ADDRESS="unix:path=" + folder + "/no-bus")
            proc = subprocess.Popen(
                [sys.executable, str(root / "main.py"), str(root / "examples" / "glass-steps.mid"),
                 "--fps", "12", "--drop-speed", "10", "--pedal-visual-dots",
                 "--no-offpiano-pattern", "--terminal-bg-color", "17,21,29"],
                stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env,
            )
            started = time.monotonic()
            next_frame = started + 0.2
            output = bytearray()
            while time.monotonic() - started < 10 and proc.poll() is None:
                if select.select([master], [], [], 0.01)[0]:
                    data = os.read(master, 65536)
                    output.extend(data)
                    stream.feed(data)
                if time.monotonic() >= next_frame:
                    snapshot()
                    next_frame += 0.1
            os.write(master, b"q")
            proc.wait(timeout=5)
            if proc.returncode or b"[PLAY]" not in output:
                raise RuntimeError(output.decode("utf-8", errors="replace"))
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
            os.close(slave)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(args.output, save_all=True, append_images=frames[1:], duration=100, loop=0, optimize=True)
    print(f"Recorded {len(frames)} frames to {args.output}")


if __name__ == "__main__":
    main()
