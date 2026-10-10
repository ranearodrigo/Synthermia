import os
import random
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synthermia import midi, packed
from synthermia.midi import load_playback_source, parse_midi, parse_midi_track_summaries, write_varlen


def midi_bytes(tracks, division=480, fmt=None):
    if fmt is None:
        fmt = 0 if len(tracks) == 1 else 1
    return b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), division) + b"".join(
        b"MTrk" + struct.pack(">I", len(track)) + track for track in tracks
    )


INVALID_FILES = {
    "short_header": b"MThd\x00",
    "extended_header": b"MThd" + struct.pack(">IHHH", 16, 0, 1, 480),
    "zero_division": midi_bytes([b""], division=0),
    "bad_format": midi_bytes([b""], fmt=3),
    "format_2": midi_bytes([b""], fmt=2),
    "no_tracks": midi_bytes([]),
    "format_0_multiple_tracks": midi_bytes([b"", b""], fmt=0),
    "oversized_track": midi_bytes([b"\x00\xff\x2f\x00"])[:-1],
    "missing_status": midi_bytes([b"\x00"]),
    "missing_meta_type": midi_bytes([b"\x00\xff"]),
    "short_meta": midi_bytes([b"\x00\xff\x03\x04ab", b"\x00\xff\x2f\x00"]),
    "short_sysex": midi_bytes([b"\x00\xf0\x03ab"]),
    "short_channel": midi_bytes([b"\x00\x90\x3c", b"\x00\xff\x2f\x00"]),
    "short_program": midi_bytes([b"\x00\xc0"]),
    "missing_running_status": midi_bytes([b"\x00\x3c\x40"]),
    "bad_data": midi_bytes([b"\x00\x90\xff\x40"]),
    "bad_short_data": midi_bytes([b"\x00\xc0\xff"]),
    "bad_status": midi_bytes([b"\x00\xf1\x00\x00"]),
    "overlong_varlen": midi_bytes([b"\x80\x80\x80\x80\x00\x90\x3c\x40"]),
    "zero_tempo": midi_bytes([b"\x00\xff\x51\x03\x00\x00\x00"]),
    "short_tempo": midi_bytes([b"\x00\xff\x51\x02\x01\x01"]),
}


class MidiFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "fixture.mid"


class MidiTests(MidiFixture):
    def test_running_status_and_short_messages(self):
        self.path.write_bytes(midi_bytes([
            b"\x00\xff\x03\x04Test\x00\xc0\x01\x00\xd0\x40"
            b"\x00\x90\x3c\x64\x83\x60\x3c\x00\x00\xff\x2f\x00"
        ]))
        spans, _, _, _ = load_playback_source(str(self.path))
        self.assertEqual([(sp.note, sp.start, sp.end) for sp in spans], [(60, 0, 0.5)])
        with patch.object(midi, "parse_midi_track_summaries_native", None):
            summaries = parse_midi_track_summaries(str(self.path))
        self.assertEqual([(s.name, s.note_ons) for s in summaries], [("Test", 1)])

    def test_python_loaders_reject_malformed_input(self):
        with patch.object(midi, "parse_midi_track_summaries_native", None):
            for name, data in INVALID_FILES.items():
                self.path.write_bytes(data)
                for loader in (parse_midi, parse_midi_track_summaries):
                    with self.subTest(name=name, loader=loader.__name__), self.assertRaisesRegex(ValueError, "Invalid MIDI"):
                        loader(str(self.path))

    def test_python_packed_fallback_requires_missing_backend(self):
        self.path.write_bytes(midi_bytes([b"\x00\x90\x3c\x64\x01\x80\x3c\x00"]))
        with patch.object(packed, "load_packed_playback_source_native", None):
            result = packed.load_selected_playback_data(str(self.path), playback_engine="packed")
            self.assertFalse(result.native_backend)
            self.assertEqual(result.packed_source.note_count, 1)
        with patch.object(packed, "load_packed_playback_source_native", side_effect=RuntimeError("backend bug")):
            with self.assertRaisesRegex(RuntimeError, "backend bug"):
                packed.load_selected_playback_data(str(self.path), playback_engine="packed")


class NativeMidiTests(MidiFixture):
    @classmethod
    def setUpClass(cls):
        if not packed.native_packed_available():
            if os.environ.get("SYNTHERMIA_REQUIRE_NATIVE") == "1":
                raise RuntimeError("Native MIDI tests require a built extension")
            raise unittest.SkipTest("Native extension is unavailable")

    def test_native_and_background_loaders_reject_malformed_input(self):
        for name, data in INVALID_FILES.items():
            self.path.write_bytes(data)
            for loader in (
                lambda path: packed.load_selected_playback_data(path, playback_engine="packed"),
                midi.parse_midi_track_summaries_native,
                lambda path: packed.start_background_packed_playback_load(path).result(),
            ):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Invalid MIDI"):
                    loader(str(self.path))

    def test_generated_valid_files_match_python(self):
        rng = random.Random(5)
        for case in range(100):
            tracks = []
            for track_idx in range(rng.randint(1, 4)):
                events = [(0, b"\xff\x03\x04Test"), (500, b"\xff\x51\x03\x06\x1a\x80"),
                          (750, b"\xff\x58\x04\x03\x02\x18\x08"),
                          (1000, b"\xb0\x40\x7f"), (2000, b"\xb0\x40\x00")]
                for _ in range(10):
                    start = rng.randint(0, 3000)
                    end = start + rng.randint(1, 1000)
                    note, channel = rng.randint(21, 108), rng.randint(0, 3)
                    events.extend([(start, bytes([0x90 | channel, note, rng.randint(1, 127)])),
                                   (end, bytes([0x80 | channel, note, 0]))])
                data = bytearray()
                previous = 0
                for tick, payload in sorted(events, key=lambda row: row[0]):
                    data.extend(write_varlen(tick - previous) + payload)
                    previous = tick
                data.extend(b"\x00\xff\x2f\x00")
                tracks.append(data)
            self.path.write_bytes(midi_bytes(tracks))
            py_spans, py_controls, py_measures, py_diag = load_playback_source(str(self.path), "auto")
            native = packed.load_selected_playback_data(str(self.path), stuck_note_fix_mode="auto", playback_engine="packed")
            self.assertTrue(native.native_backend)
            def rows(spans):
                return sorted((sp.note, round(sp.start, 8), round(sp.end, 8), sp.velocity, sp.channel, sp.track) for sp in spans)
            with self.subTest(case=case):
                self.assertEqual(rows(py_spans), rows(native.packed_source.to_note_spans()))
                self.assertEqual(py_diag, native.note_diag)
                self.assertEqual([round(t, 8) for t in py_measures], [round(t, 8) for t in native.measure_times])
                self.assertEqual([(round(c.time_sec, 8), c.channel, c.controller, c.value, c.track) for c in py_controls],
                                 [(round(c.time_sec, 8), c.channel, c.controller, c.value, c.track) for c in native.controls])
                with patch.object(midi, "parse_midi_track_summaries_native", None):
                    summaries = parse_midi_track_summaries(str(self.path))
                self.assertEqual(summaries, parse_midi_track_summaries(str(self.path)))

    def test_background_buffers_reach_complete_source(self):
        self.path.write_bytes(midi_bytes([b"\x01\x90\x3c\x64\x01\x80\x3c\x00" * 140000]))
        task = packed.start_background_packed_playback_load(str(self.path), initial_ready_sec=1)
        ready = task.wait_until_ready(1)
        self.assertGreaterEqual(ready.packed_source.duration_sec, 1)
        self.assertTrue(ready.packed_source.visible_note_tuples(0, 0.1))
        complete = task.result()
        self.assertFalse(complete.is_partial)
        self.assertEqual(complete.packed_source.note_count, 140000)
