import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synthermia.audio import _build_track_program_map_for_tracks
from synthermia.soundfont import (
    _build_soundfont_pair_map, _normalize_saved_soundfont_library,
    _soundfont_identity, format_instrument_option,
)


@unittest.skipIf(os.path.normcase("A") == os.path.normcase("a"), "case-sensitive paths required")
class SoundfontTests(unittest.TestCase):
    def test_case_distinct_files_keep_separate_presets_and_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            upper, lower = [str(Path(folder) / name) for name in ("Piano.sf2", "piano.sf2")]
            for path in (upper, lower):
                Path(path).touch()
            library = _normalize_saved_soundfont_library([upper, lower, upper])
            self.assertEqual(library, [upper, lower])
            with patch("synthermia.soundfont.scan_soundfont_presets", side_effect=lambda sf: ["0:1"] if sf == upper else ["0:2"]):
                pairs = _build_soundfont_pair_map(library)
            self.assertEqual(pairs[_soundfont_identity(upper)], {(0, 1)})
            self.assertEqual(pairs[_soundfont_identity(lower)], {(0, 2)})
            routed = _build_track_program_map_for_tracks(
                [0, 1], (0, 1), upper,
                {"1": format_instrument_option(0, 2, soundfont_path=lower)}, pairs,
            )
            self.assertEqual(routed, {0: (upper, 0, 1), 1: (lower, 0, 2)})
