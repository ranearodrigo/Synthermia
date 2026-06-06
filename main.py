#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional, Sequence

from synthermia.audio import _load_pyfluidsynth_module
from synthermia.config import (
    DEFAULT_VISUAL_GLYPHS,
    args_to_saved_config,
    clamp_playback_start,
    normalize_note_range_text,
    resolve_midi_flip_bounds,
    resolve_visual_glyphs,
    save_last_config,
)
from synthermia.midi import normalize_stuck_note_fix_mode
from synthermia.packed import (
    PARTIAL_PACKED_START_BUFFER_SEC,
    load_selected_playback_data,
    native_packed_task_available,
    resolve_playback_engine,
    should_auto_use_packed,
    should_use_partial_packed_startup,
    start_background_packed_playback_load,
)
from synthermia.playback import list_midi_input_ports, playback, playback_live_midi_input
from synthermia.soundfont import _normalize_saved_soundfont_library
from synthermia.tui import BackgroundTaskCancelled, run_background_task_popup, run_config_screen

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Terminal Synthesia-style MIDI visualizer")
    p.add_argument("midi", nargs="?", default="", help="Path to .mid/.midi file")
    p.add_argument(
        "--midi-input-mode",
        action="store_true",
        help="Live MIDI input mode from USB/ALSA input (uses upward notes, no MIDI file required)",
    )
    p.add_argument(
        "--midi-input-port",
        type=str,
        default="",
        help="ALSA MIDI input port id/name for live mode (example: 24:0); empty=auto",
    )
    p.add_argument(
        "--midi-input-save-dir",
        type=str,
        default="midis",
        help="Directory used by live mode when pressing 's' to save captured MIDI (default: midis)",
    )
    p.add_argument(
        "--midi-input-invert-pedal",
        action="store_true",
        help="Invert sustain pedal polarity in live MIDI mode (for normally-closed/inverted pedals)",
    )
    p.add_argument(
        "--midi-input-instrument-map",
        type=str,
        default="",
        help="Live mode hotkeys for instrument switching, e.g. '1=0:0,2=0:40,3=0:56'",
    )
    p.add_argument(
        "--midi-input-color-map",
        type=str,
        default="",
        help="Live mode key colors, e.g. '1=255,80,80;2=80,220,120;3=90,140,255'",
    )
    p.add_argument(
        "--list-midi-input-ports",
        action="store_true",
        help="List available MIDI input ports and exit",
    )
    p.add_argument("--soundfont", type=str, default="", help="Optional .sf2 path for FluidSynth playback")
    p.add_argument(
        "--soundfonts",
        type=str,
        default="",
        help="Optional extra SoundFonts separated by ';' (primary --soundfont is always included)",
    )
    p.add_argument(
        "--audio-engine",
        type=str,
        choices=["python", "cli"],
        default="python",
        help="Audio backend: python=pyFluidSynth in-process (default), cli=external fluidsynth process",
    )
    p.add_argument(
        "--playback-engine",
        type=str,
        choices=["auto", "python", "packed"],
        default="auto",
        help="Playback data engine: auto=use packed path for large MIDIs, python=legacy object path, packed=packed window queries",
    )
    p.add_argument("--fluidsynth-bin", type=str, default="fluidsynth", help="FluidSynth executable name/path (cli engine)")
    p.add_argument("--audio-driver", type=str, default="", help="FluidSynth audio driver (example: pipewire, alsa)")
    p.add_argument(
        "--audio-output",
        type=str,
        default="",
        help="Audio output target/device name (for example Pulse/PipeWire sink or ALSA device)",
    )
    p.add_argument("--audio-gain", type=float, default=0.6, help="FluidSynth gain (default: 0.6)")
    p.add_argument("--audio-period-size", type=int, default=512, help="FluidSynth audio period size (buffer), e.g. 512")
    p.add_argument("--audio-periods", type=int, default=3, help="FluidSynth audio periods, e.g. 3")
    p.add_argument("--audio-samplerate", type=int, default=48000, help="FluidSynth sample rate, e.g. 48000")
    p.add_argument("--instrument-bank", type=int, default=0, help="SoundFont bank number for playback (default: 0)")
    p.add_argument("--instrument-program", type=int, default=0, help="SoundFont program number for playback (default: 0)")
    p.add_argument("--audio-log", type=str, default="", help="Append FluidSynth stdout/stderr to this file")
    p.add_argument("--ascii-ui", action="store_true", help="Use ASCII-only controls in the status bar")
    p.add_argument("--fps", type=int, default=30, help="Frames per second (default: 30)")
    p.add_argument("--rows", type=int, default=35, help="Visualizer rows including keyboard (default: 35)")
    p.add_argument("--x-scale", type=int, default=1, help="Horizontal key/note width scale factor (default: 1)")
    p.add_argument("--kb-y-scale", type=int, default=1, help="Keyboard vertical scale factor (default: 1)")
    p.add_argument("--kb-black-extra", type=int, default=1, help="Extra black-key rows beyond kb-y-scale (default: 1)")
    p.add_argument(
        "--glyph-note-white",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_white"],
        help="Glyph used for white falling notes",
    )
    p.add_argument(
        "--glyph-note-pedal",
        type=str,
        default="",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--glyph-note-pedal-white",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"],
        help="Glyph used for white pedal-held continuation notes",
    )
    p.add_argument(
        "--glyph-note-pedal-black-left",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"],
        help="Left glyph for black pedal-held continuation notes",
    )
    p.add_argument(
        "--glyph-note-pedal-black-mid",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"],
        help="Center glyph for black pedal-held continuation notes",
    )
    p.add_argument(
        "--glyph-note-pedal-black-right",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"],
        help="Right glyph for black pedal-held continuation notes",
    )
    p.add_argument(
        "--glyph-note-black-left",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"],
        help="Left glyph for black falling notes",
    )
    p.add_argument(
        "--glyph-note-black-mid",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"],
        help="Center glyph for black falling notes",
    )
    p.add_argument(
        "--glyph-note-black-right",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"],
        help="Right glyph for black falling notes",
    )
    p.add_argument(
        "--glyph-piano-white",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_piano_white"],
        help="Glyph used for white piano keys in no-color mode",
    )
    p.add_argument(
        "--glyph-piano-black-left",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"],
        help="Left glyph for black piano keys",
    )
    p.add_argument(
        "--glyph-piano-black-mid",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"],
        help="Center glyph for black piano keys",
    )
    p.add_argument(
        "--glyph-piano-black-right",
        type=str,
        default=DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"],
        help="Right glyph for black piano keys",
    )
    p.add_argument(
        "--dynamics-full-velocity",
        type=float,
        default=96.0,
        help="MIDI velocity where note color reaches full opacity; lower velocities blend toward terminal background (default: 96)",
    )
    p.add_argument(
        "--terminal-bg-color",
        type=str,
        default="",
        help="Terminal background override for dynamics blending as R,G,B (empty = auto detect)",
    )
    p.add_argument(
        "--pedal-visual-dots",
        action="store_true",
        help="Show dotted continuation for notes held by sustain pedal after key release",
    )
    p.add_argument(
        "--disable-pedal",
        action="store_true",
        help="Ignore sustain pedal during playback and export",
    )
    p.add_argument(
        "--pedal-opacity-scale",
        type=float,
        default=0.5,
        help="Opacity multiplier for pedal-held notes (0.0-1.0, default: 0.5)",
    )
    p.add_argument(
        "--pedal-decay-seconds",
        type=float,
        default=10.0,
        help="Seconds for pedal shadow notes to fade to transparent (0 disables decay, default: 10.0)",
    )
    p.add_argument(
        "--measure-bars",
        action="store_true",
        help="Draw moving measure separators using '_' in the note field",
    )
    p.add_argument(
        "--drop-speed",
        type=float,
        default=30.0,
        help="Falling speed in rows/second; also controls note visual length (default: 30.0)",
    )
    p.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier (default: 1.0)")
    p.add_argument(
        "--midi-flip",
        action="store_true",
        help="Mirror note pitches vertically. Uses --range bounds when set, otherwise flips across the full 88-key piano.",
    )
    p.add_argument(
        "--stuck-note-fix",
        type=str,
        choices=["auto", "off", "lifo"],
        default="auto",
        help="Handle overlapping same-pitch note-ons in imperfect MIDI: auto, off, or lifo",
    )
    p.add_argument("--start", type=float, default=0.0, help="Start time in seconds")
    p.add_argument("--range", type=str, default="", help="Forced note range as min:max (example: 36:84)")
    p.add_argument("--hide-piano", action="store_true", help="Hide the piano canvas and show only the off-piano art")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    p.add_argument(
        "--pressed-key-color-mode",
        type=str,
        choices=["split", "weighted", "off"],
        default="split",
        help="Pressed white-key tint mode: split, weighted, or off (default: split)",
    )
    p.add_argument(
        "--no-offpiano-pattern",
        action="store_true",
        help="Disable the off-piano ornament pattern (use plain background fill)",
    )
    return p.parse_args(argv)


def _should_use_loading_popup(args: argparse.Namespace, *, from_config_screen: bool) -> bool:
    if not from_config_screen:
        return False
    midi_path = str(getattr(args, "midi", "")).strip()
    if not midi_path:
        return False
    engine = resolve_playback_engine(str(getattr(args, "playback_engine", "auto")))
    return engine == "packed" or (engine == "auto" and should_auto_use_packed(midi_path))


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parse_args(raw_argv)
    if bool(getattr(args, "list_midi_input_ports", False)):
        ports = list_midi_input_ports()
        if not ports:
            print("No MIDI input ports found (or `aseqdump` unavailable).", file=sys.stderr)
            return 2
        print("MIDI input ports:")
        for port_id, label in ports:
            print(f"  {port_id}  {label}")
        return 0
    args.stuck_note_fix = normalize_stuck_note_fix_mode(str(getattr(args, "stuck_note_fix", "auto")))
    if not hasattr(args, "track_instruments"):
        args.track_instruments = {}
    if not hasattr(args, "track_colors"):
        args.track_colors = {}
    if not hasattr(args, "track_volumes"):
        args.track_volumes = {}
    args.soundfonts = _normalize_saved_soundfont_library(
        getattr(args, "soundfonts", []),
        primary_soundfont=str(getattr(args, "soundfont", "")),
    )
    args.playback_engine = resolve_playback_engine(str(getattr(args, "playback_engine", "auto")))
    no_args_mode = len(raw_argv) == 0

    try:
        while True:
            loaded_from_config_screen = False
            if no_args_mode:
                args = run_config_screen(args)
                loaded_from_config_screen = True
            if args.drop_speed <= 0:
                raise ValueError("--drop-speed must be > 0")
            if args.x_scale <= 0:
                raise ValueError("--x-scale must be > 0")
            if args.kb_y_scale <= 0:
                raise ValueError("--kb-y-scale must be > 0")
            if args.kb_black_extra < 0:
                raise ValueError("--kb-black-extra must be >= 0")
            if float(getattr(args, "dynamics_full_velocity", 96.0)) <= 0:
                raise ValueError("--dynamics-full-velocity must be > 0")
            args.pedal_opacity_scale = max(0.0, min(1.0, float(getattr(args, "pedal_opacity_scale", 0.5))))
            args.pedal_decay_seconds = max(0.0, float(getattr(args, "pedal_decay_seconds", 10.0)))
            bg_txt = str(getattr(args, "terminal_bg_color", "")).strip()
            if bg_txt and not re.match(r"^\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*$", bg_txt):
                raise ValueError("--terminal-bg-color must be R,G,B")
            args.terminal_bg_color = bg_txt
            normalized_glyphs = resolve_visual_glyphs(args)
            for glyph_key, glyph_val in normalized_glyphs.items():
                setattr(args, glyph_key, glyph_val)
            if args.speed <= 0:
                raise ValueError("--speed must be > 0")
            if args.soundfont and (bool(getattr(args, "midi_input_mode", False)) or str(args.audio_engine).lower() == "python"):
                _load_pyfluidsynth_module()
            selected_midi_for_config = args.midi
            if bool(getattr(args, "midi_input_mode", False)):
                save_last_config(args_to_saved_config(args, selected_midi_for_config))
                playback_live_midi_input(args)
                return 0
            if not args.midi:
                print("Provide a MIDI path.", file=sys.stderr)
                return 2
            if not os.path.exists(args.midi):
                print(f"MIDI file not found: {args.midi}", file=sys.stderr)
                return 2

            def load_playback_data():
                return load_selected_playback_data(
                    str(args.midi),
                    stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "auto")),
                    midi_flip=bool(getattr(args, "midi_flip", False)),
                    flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
                    playback_engine=str(getattr(args, "playback_engine", "auto")),
                )

            pending_packed_task = None
            use_partial_start = should_use_partial_packed_startup(
                str(args.midi),
                playback_engine=str(getattr(args, "playback_engine", "auto")),
                start_sec=float(getattr(args, "start", 0.0)),
                soundfont_path=str(getattr(args, "soundfont", "")),
                pedal_visual_dots=bool(getattr(args, "pedal_visual_dots", False)),
            )
            try:
                if use_partial_start and native_packed_task_available():
                    native_task = start_background_packed_playback_load(
                        str(args.midi),
                        stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "auto")),
                        midi_flip=bool(getattr(args, "midi_flip", False)),
                        flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
                        initial_ready_sec=PARTIAL_PACKED_START_BUFFER_SEC,
                    )
                    if _should_use_loading_popup(args, from_config_screen=loaded_from_config_screen):
                        loaded = run_background_task_popup(
                            " Loading MIDI ",
                            "Preparing the first playable packed buffer.\nPress q or Esc to cancel and return to the config screen.",
                            lambda: native_task.wait_until_ready(PARTIAL_PACKED_START_BUFFER_SEC),
                            detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                            progress_fn=native_task.progress,
                            allow_cancel=True,
                            cancel_task=native_task.cancel,
                        )
                    else:
                        loaded = native_task.wait_until_ready(PARTIAL_PACKED_START_BUFFER_SEC)
                    if loaded.is_partial and not native_task.done():
                        pending_packed_task = native_task
                elif _should_use_loading_popup(args, from_config_screen=loaded_from_config_screen):
                    if native_packed_task_available():
                        native_task = start_background_packed_playback_load(
                            str(args.midi),
                            stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "auto")),
                            midi_flip=bool(getattr(args, "midi_flip", False)),
                            flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
                        )
                        loaded = run_background_task_popup(
                            " Loading MIDI ",
                            "Preparing packed playback data.\nPress q or Esc to cancel and return to the config screen.",
                            native_task.result,
                            detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                            progress_fn=native_task.progress,
                            allow_cancel=True,
                            cancel_task=native_task.cancel,
                        )
                    else:
                        loaded = run_background_task_popup(
                            " Loading MIDI ",
                            "Preparing packed playback data.\nLarge black MIDI files may take a while.",
                            load_playback_data,
                            detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                        )
                else:
                    loaded = load_playback_data()
            except BackgroundTaskCancelled:
                if no_args_mode:
                    continue
                raise KeyboardInterrupt()

            spans = loaded.spans
            controls = loaded.controls
            measure_times = loaded.measure_times
            note_diag = loaded.note_diag
            overlap_fixes = int(note_diag.get("overlap_fixes", 0))
            orphan_offs = int(note_diag.get("orphan_note_offs", 0))
            unmatched_ons = int(note_diag.get("remaining_unmatched_note_ons", 0))
            if (
                (not loaded.is_partial)
                and str(getattr(args, "stuck_note_fix", "off")) != "off"
                and (overlap_fixes > 0 or orphan_offs > 0)
            ):
                print(
                    f"MIDI cleanup: fixed {overlap_fixes} overlap(s), "
                    f"saw {orphan_offs} orphan note-off(s), {unmatched_ons} unmatched note-on(s) remain.",
                    file=sys.stderr,
                )

            args.range = normalize_note_range_text(str(getattr(args, "range", "")))
            args.start = max(0.0, float(getattr(args, "start", 0.0)))
            duration_sec = 0.0
            if spans:
                duration_sec = max(float(sp.end) for sp in spans)
            elif loaded.packed_source is not None:
                duration_sec = float(loaded.packed_source.duration_sec)
            if duration_sec > 0.0:
                clamped_start = clamp_playback_start(
                    float(args.start),
                    duration_sec,
                    float(args.speed),
                    int(getattr(args, "fps", 30)),
                )
                if clamped_start < float(args.start) - 1e-9:
                    print(
                        f"Start time clamped from {float(args.start):.3f}s to {clamped_start:.3f}s "
                        f"(MIDI duration {duration_sec:.3f}s at speed {float(args.speed):.2f}x).",
                        file=sys.stderr,
                    )
                    args.start = clamped_start

            if loaded.engine_requested == "packed" and loaded.packed_source is not None and not loaded.native_backend:
                print(
                    "Packed playback requested but the native extension is unavailable; using the Python packed fallback.",
                    file=sys.stderr,
                )

            save_last_config(args_to_saved_config(args, selected_midi_for_config))
            playback(
                spans,
                args,
                controls=controls,
                measure_times=measure_times,
                packed_source=loaded.packed_source,
                pending_packed_task=pending_packed_task,
            )
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
