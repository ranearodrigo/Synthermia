import curses
from contextlib import ExitStack
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import parse_args
from synthermia import tui
from synthermia.config import load_config_profiles, load_last_config, save_config_profiles, save_last_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.previous_cwd = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, self.previous_cwd)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.folder / "config")})
        env.start()
        self.addCleanup(env.stop)

    def test_settings_work_from_another_directory(self):
        cfg = {"midi": "song.mid", "soundfont": "piano.sf2", "soundfonts": ["piano.sf2"],
               "instrument": "piano.sf2\t0:1 Piano", "track_instruments": {"0": "other.sf2\t0:2"}}
        save_last_config(cfg)
        save_config_profiles([{"name": "Piano", "config": cfg}])
        other = self.folder / "other"
        other.mkdir()
        os.chdir(other)
        saved = load_last_config()
        self.assertEqual(saved["midi"], str(self.folder / "song.mid"))
        self.assertEqual(saved["soundfonts"], [str(self.folder / "piano.sf2")])
        self.assertEqual(saved["track_instruments"]["0"], f"{self.folder / 'other.sf2'}\t0:2")
        self.assertEqual(load_config_profiles()[0]["config"], saved)

    def test_legacy_files_are_read_and_left_untouched(self):
        legacy = self.folder / ".synthermia_last_config.json"
        profile = self.folder / ".synthermia_profiles.json"
        contents = '{"midi": "song.mid"}'
        legacy.write_text(contents)
        profile.write_text('[{"name":"Old","config":{"midi":"song.mid"}}]')
        self.assertEqual(load_last_config()["midi"], str(self.folder / "song.mid"))
        self.assertEqual(load_config_profiles()[0]["name"], "Old")
        save_last_config({"midi": "new.mid"})
        self.assertEqual(load_last_config()["midi"], str(self.folder / "new.mid"))
        self.assertEqual(legacy.read_text(), contents)
        self.assertFalse((self.folder / "config" / "synthermia" / "profiles.json").exists())

    def test_failed_replacement_keeps_previous_settings(self):
        path = self.folder / "settings.json"
        save_last_config({"fps": 30}, str(path))
        with patch("synthermia.config.os.replace", side_effect=PermissionError("read-only destination")):
            with self.assertRaisesRegex(OSError, "Could not save settings"):
                save_last_config({"fps": 60}, str(path))
        self.assertEqual(json.loads(path.read_text()), {"fps": 30})
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_missing_files_use_defaults_but_corruption_is_reported(self):
        self.assertEqual(load_last_config(), {})
        self.assertEqual(load_config_profiles(), [])
        path = self.folder / "bad.json"
        path.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            load_last_config(str(path))

    def test_numeric_setting_save_failure_is_reported(self):
        class Screen:
            keys = iter([ord("/"), *map(ord, "Playback Speed"), 10, curses.KEY_END, 10, ord("q")])

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

        with ExitStack() as stack:
            for name, value in {
                "load_last_config": {}, "load_config_profiles": [],
                "scan_files_by_ext": [], "list_audio_output_targets": [],
                "_edit_text_popup": "2.0",
            }.items():
                stack.enter_context(patch.object(tui, name, return_value=value))
            save = stack.enter_context(patch.object(tui, "save_last_config", side_effect=OSError("disk full")))
            stack.enter_context(patch.object(tui.curses, "wrapper", side_effect=lambda fn: fn(Screen())))
            stack.enter_context(patch.object(tui.curses, "has_colors", return_value=False))
            stack.enter_context(patch.object(tui.curses, "curs_set"))
            stack.enter_context(patch.object(tui.curses, "set_escdelay"))
            with self.assertRaisesRegex(OSError, "disk full"):
                tui.run_config_screen(parse_args([]))
            self.assertEqual(save.call_args.args[0]["speed"], 2.0)
