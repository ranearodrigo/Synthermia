import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synthermia.audio import RealtimeFluidSynthPlayer, _assign_audio_channels, _assign_control_channels
from synthermia.models import ControlEvent, NoteSpan


class Synth:
    def __init__(self, **kwargs):
        self.pressed = set()
        self.held = set()
        self.controllers = {}

    def setting(self, *args):
        pass

    def start(self, **kwargs):
        return 0

    def sfload(self, path):
        return 1

    def program_select(self, *args):
        return 0

    def get_cc(self, channel, controller):
        return self.controllers.get((channel, controller), {7: 100, 10: 64, 11: 127}.get(controller, 0))

    def cc(self, channel, controller, value):
        self.controllers[channel, controller] = value
        if controller == 64 and value < 64:
            self.held = {key for key in self.held if key[0] != channel}
        elif controller == 123:
            self.pressed = {key for key in self.pressed if key[0] != channel}

    def noteon(self, channel, note, velocity):
        self.pressed.add((channel, note))

    def noteoff(self, channel, note):
        self.pressed.discard((channel, note))
        if self.get_cc(channel, 64) >= 64:
            self.held.add((channel, note))

    def delete(self):
        pass


class AudioTests(unittest.TestCase):
    def make_player(self, spans, controls):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        soundfont = Path(folder.name) / "piano.sf2"
        soundfont.touch()
        with patch("synthermia.audio._load_pyfluidsynth_module", return_value=SimpleNamespace(Synth=Synth)):
            player = RealtimeFluidSynthPlayer(spans, str(soundfont), 0.6, control_events=controls)
        self.addCleanup(player.close)
        return player

    def test_seek_preserves_released_sustain_notes(self):
        player = self.make_player(
            [NoteSpan(60, 0, 1, 100)],
            [ControlEvent(0, 0, 64, 127), ControlEvent(3, 0, 64, 0)],
        )
        player.seek(2)
        self.assertFalse(player._synth.pressed)
        self.assertEqual(player._synth.held, {(0, 60)})
        player.update(3)
        self.assertFalse(player._synth.pressed | player._synth.held)

    def test_seek_keeps_pressed_notes_until_release(self):
        player = self.make_player([NoteSpan(60, 0, 3, 100)], [])
        player.seek(2)
        self.assertEqual(player._synth.pressed, {(0, 60)})
        player.update(3)
        self.assertFalse(player._synth.pressed)

    def test_rewind_restores_initial_controller_values(self):
        player = self.make_player(
            [NoteSpan(60, 0, 5, 100)],
            [ControlEvent(2, 0, 7, 0), ControlEvent(2, 0, 10, 127)],
        )
        player.update(3)
        self.assertEqual(player._synth.get_cc(0, 7), 0)
        player.seek(0)
        self.assertEqual(player._synth.get_cc(0, 7), 100)
        self.assertEqual(player._synth.get_cc(0, 10), 64)

    def test_controller_track_follows_remapped_source_channel(self):
        spans = [NoteSpan(60, 0, 2, 100, 1, 1), NoteSpan(64, 1, 2, 100, 0, 2)]
        audio, _, _, mapping = _assign_audio_channels(spans, {1: ("", 0, 0), 2: ("", 0, 0)})
        controls = _assign_control_channels([ControlEvent(1, 0, 64, 127, 0)], mapping)
        self.assertEqual(controls[0].channel, audio[1].channel)

    def test_controller_track_reaches_all_copies_of_channel(self):
        spans = [NoteSpan(60, 0, 2, 100, 0, 1), NoteSpan(64, 1, 2, 100, 0, 2)]
        _, _, _, mapping = _assign_audio_channels(spans, {1: ("", 0, 0), 2: ("", 0, 0)})
        external = _assign_control_channels([ControlEvent(1, 0, 64, 127, 0)], mapping)
        local = _assign_control_channels([ControlEvent(1, 0, 64, 127, 1)], mapping)
        self.assertEqual([ev.channel for ev in external], [0, 1])
        self.assertEqual([ev.channel for ev in local], [0])
