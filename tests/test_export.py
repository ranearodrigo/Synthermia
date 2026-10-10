import os
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


class ExportTests(unittest.TestCase):
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
