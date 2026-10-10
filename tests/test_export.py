import os
import curses
from contextlib import ExitStack
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import parse_args
from synthermia.midi import load_playback_source
from synthermia.models import ControlEvent, NoteSpan
from synthermia.playback import write_demo_midi_file
from synthermia.tui import _run_export_process, export_configured_midi_to_mp3
from synthermia import tui


class ExportTests(unittest.TestCase):
    def test_config_screen_exports_current_snapshot(self):
        class Screen:
            def __init__(self):
                self.keys = iter([curses.KEY_END, curses.KEY_UP, 10, ord("p")])

            def getch(self):
                return next(self.keys)

            def getmaxyx(self):
                return 40, 120

            def erase(self):
                pass

            def box(self):
                pass

            def addstr(self, *args):
                pass

            def refresh(self):
                pass

            def keypad(self, value):
                pass

        settings = {"midi_flip": True, "disable_pedal": True, "range": "36:84", "speed": 2.0}
        with ExitStack() as stack:
            for name, value in {
                "load_last_config": settings, "load_config_profiles": [],
                "scan_files_by_ext": [], "list_audio_output_targets": [],
                "save_last_config": None, "_edit_text_popup": "/tmp/example.mp3",
                "_message_popup": None,
            }.items():
                stack.enter_context(patch.object(tui, name, return_value=value))
            stack.enter_context(patch.object(tui.curses, "wrapper", side_effect=lambda fn: fn(Screen())))
            stack.enter_context(patch.object(tui.curses, "has_colors", return_value=False))
            stack.enter_context(patch.object(tui.curses, "curs_set"))
            stack.enter_context(patch.object(tui.curses, "set_escdelay"))
            export = stack.enter_context(patch.object(tui, "export_configured_midi_to_mp3", return_value="/tmp/example.mp3"))
            tui.run_config_screen(parse_args([]))
            self.assertEqual(export.call_count, 1)
            exported = export.call_args.args[0]
            for key, value in settings.items():
                self.assertEqual(getattr(exported, key), value)

    def test_noisy_child_completes(self):
        _run_export_process(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 1000000)"],
            None, "Rendering",
        )

    def test_child_failure_reports_output(self):
        with self.assertRaisesRegex(ValueError, "broken soundfont"):
            _run_export_process(
                [sys.executable, "-c", "import sys; print('broken soundfont'); sys.exit(2)"],
                None, "Rendering",
            )

    def test_interruption_stops_child(self):
        real_popen = subprocess.Popen
        children = []

        def spawn(cmd, **kwargs):
            child = real_popen(cmd, **kwargs)
            children.append(child)
            return child

        with patch("synthermia.tui.subprocess.Popen", side_effect=spawn), patch(
            "synthermia.tui._draw_progress_popup", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                _run_export_process([sys.executable, "-c", "import time; time.sleep(30)"], object(), "Rendering")
        self.assertIsNotNone(children[0].poll())

    def test_export_applies_flip_and_disabled_pedal_and_removes_temps(self):
        with tempfile.TemporaryDirectory() as folder:
            midi = Path(folder) / "source.mid"
            soundfont = Path(folder) / "piano.sf2"
            soundfont.touch()
            write_demo_midi_file(
                str(midi), [NoteSpan(60, 0, 1, 100)],
                control_events=[ControlEvent(0, 0, 64, 127)], insert_warmup_pulses=False,
            )
            args = parse_args([str(midi), "--soundfont", str(soundfont), "--midi-flip", "--disable-pedal"])
            temp_paths = []

            def render(cmd, *unused):
                if "-F" in cmd:
                    temp_paths.extend([cmd[-1], cmd[cmd.index("-F") + 1]])
                    spans, controls, _, _ = load_playback_source(cmd[-1])
                    self.assertEqual([sp.note for sp in spans], [69])
                    self.assertFalse(any(ev.controller == 64 for ev in controls))
                else:
                    Path(cmd[-1]).write_bytes(b"encoded")

            output = str(Path(folder) / "out.mp3")
            with patch("synthermia.tui.shutil.which", side_effect=lambda name: name), patch(
                "synthermia.tui._run_export_process", side_effect=render
            ):
                self.assertEqual(export_configured_midi_to_mp3(args, output), output)
            self.assertEqual(Path(output).read_bytes(), b"encoded")
            self.assertTrue(all(not os.path.exists(path) for path in temp_paths))
