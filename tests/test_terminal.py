import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from main import parse_args
from synthermia.models import NoteSpan
from synthermia.playback import _playback_layout, write_demo_midi_file


class TerminalTests(unittest.TestCase):
    def test_narrow_terminal_reports_required_width(self):
        with patch("synthermia.playback.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            with self.assertRaisesRegex(ValueError, "need at least 108 columns"):
                _playback_layout(parse_args([]), [])
            self.assertLessEqual(_playback_layout(parse_args(["--range", "21:60"]), [])[-1] + 4, 80)
            _playback_layout(parse_args(["--hide-piano"]), [])

    def test_scale_shrinks_to_fit(self):
        with patch("synthermia.playback.shutil.get_terminal_size", return_value=os.terminal_size((120, 40))):
            layout = _playback_layout(parse_args(["--x-scale", "3"]), [])
        self.assertEqual(layout[-2], 1)
        self.assertLessEqual(layout[-1] + 4, 120)

    def test_playback_detects_resize_and_restores_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            midi = Path(folder) / "demo.mid"
            write_demo_midi_file(str(midi), [NoteSpan(60, 0, 30, 100)], insert_warmup_pulses=False)
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            before = termios.tcgetattr(slave)
            env = dict(os.environ, TERM="xterm-256color", DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/synthermia-test-no-bus")
            env["XDG_CONFIG_HOME"] = folder
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), str(midi),
                 "--no-color", "--no-offpiano-pattern", "--terminal-bg-color", "0,0,0"],
                stdin=slave, stdout=slave, stderr=slave, cwd=folder, env=env,
            )
            output = bytearray()
            try:
                deadline = time.monotonic() + 5
                while b"[PLAY]" not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                self.assertIn(b"[PLAY]", output)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                while proc.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                self.assertEqual(proc.wait(timeout=2), 1)
                while select.select([master], [], [], 0)[0]:
                    output.extend(os.read(master, 65536))
                self.assertIn(b"Terminal too narrow", output)
                self.assertIn(b"\x1b[?25h", output)
                self.assertEqual(termios.tcgetattr(slave), before)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(slave)
