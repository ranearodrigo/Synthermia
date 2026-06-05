# Plan: Split main.py into modules

## Context

`main.py` is a single 7,700-line file containing every concern of the app: MIDI parsing,
audio synthesis, terminal rendering, interactive TUI, config persistence, and two separate
playback loops. Zero imports from any project-local module. The goal is to split it into
conceptually coherent modules — no functionality changes, no internal refactoring of logic.

The result will be a `synthermia/` package alongside the existing `main.py` entry point.

---

## Target structure

```
main.py                  # ~300 lines: parse_args() + main() orchestration only
synthermia/
  __init__.py            # empty
  models.py              # dataclasses + shared constants
  midi.py                # MIDI file parsing, timing math, note/control building
  soundfont.py           # SF2 scanning, preset matching, instrument option utils
  config.py              # JSON persistence, glyph/visual config, track maps, live maps
  render.py              # render_frame, keyboard geometry, canvas, ANSI color utils
  audio.py               # FluidSynth engine, driver detection, routing, CLI launcher
  tui.py                 # Config screen, popups, MP3 export
  playback.py            # Playback loops, span window, pedal spans, live MIDI input
```

---

## Import dependency graph (no circular imports)

```
models      ← (nothing)
midi        ← models
soundfont   ← models
config      ← models, soundfont
render      ← models, config  (needs normalize_pressed_key_color_mode)
audio       ← models, config, soundfont
tui         ← models, midi, soundfont, config, render, audio
playback    ← models, midi, config, render, audio, tui  (tui needed for 'e' key → run_config_screen)
main        ← config (parse_args), midi, audio, playback, tui
```

---

## Module contents

### `synthermia/models.py` (~150 lines, lines 44–186)
- Constants: `DEFAULT_TEMPO_US_PER_QN`, `WHITE_PCS`, `MIN_PIANO_NOTE`, `MAX_PIANO_NOTE`, `CONFIG_FILE`
- Dataclasses: `MidiHeader`, `RawEvent`, `NoteSpan`, `ControlEvent`, `MidiTrackSummary`, `_AudioEvent`, `_LiveMidiEvent`
- Regex: `_ASEQDUMP_NOTE_ON_RE`, `_ASEQDUMP_NOTE_OFF_RE`, `_ASEQDUMP_CC_RE`

### `synthermia/midi.py` (~700 lines, lines 363–856)
- Binary helpers: `read_u32_be`, `read_u16_be`, `read_varlen`, `write_varlen`
- Parsing: `parse_midi`, `parse_midi_track_summaries`
- Timing: `build_tempo_segments`, `tick_to_sec`
- Building: `normalize_stuck_note_fix_mode`, `build_note_spans`, `build_control_events`, `build_measure_times`
- Public API: `load_playback_source`, `apply_midi_flip_to_spans`

### `synthermia/soundfont.py` (~600 lines, lines 1165–1450)
- Instrument name constants: `_INSTR_NAME_STOPWORDS`, `_INSTR_NAME_ABBREVIATIONS`
- SF2 scanner: `_riff_chunks`, `scan_soundfont_presets`, `_build_soundfont_pair_map`
- Instrument option utils: `_fmt_choice`, `_clamp_instrument_pair`, `_split_instrument_option`,
  `instrument_option_soundfont`, `instrument_option_label`, `parse_instrument_spec`,
  `parse_instrument_pair`, `format_instrument_option`
- Fuzzy matching: `_instrument_option_name`, `_normalize_instrument_name_tokens`,
  `_normalized_match_text`, `_partial_ratio`, `_match_instrument_by_name`

### `synthermia/config.py` (~500 lines, lines 1048–1165, 1241–1325, 1453–1519, 3925–4041)
- Glyph config: `DEFAULT_VISUAL_GLYPHS`, `_sanitize_visual_glyph`, `normalize_visual_glyphs`, `resolve_visual_glyphs`
- Misc config utils: `normalize_pressed_key_color_mode`, `default_track_color`, `scan_files_by_ext`
- Persistence: `load_last_config`, `save_last_config`, `args_to_saved_config`
- Soundfont library: `_normalize_saved_soundfont_library`, `_soundfont_identity`
- Track maps: `_normalize_saved_track_map`, `_clamp_track_volume`, `_normalize_saved_track_volume_map`, `_format_track_volume_text`
- Live maps: `parse_live_instrument_map`, `default_live_hotkey_color`, `parse_live_color_map`,
  `format_live_color_map`, `format_live_instrument_map`, `build_live_instrument_hotkey_map`
- Note range utils: `normalize_note_range_text`, `parse_note_range_text`

### `synthermia/render.py` (~800 lines, lines 858–1046, 1107–1143, 4044–4089, 4089–4725)
- Keyboard geometry: `is_white`, `count_whites_before`, `white_key_width`, `black_fall_width`,
  `black_key_width`, `keyboard_row_counts`, `note_x`, `keyboard_width`
- Canvas: `make_canvas`, `paint_str`
- ANSI color: `ansi_color`, `ansi_fg_rgb`, `ansi_bg_rgb`, `_clamp_rgb_triplet`, `parse_rgb_triplet`,
  `rgb_triplet_to_text`, `darken_rgb`, `blend_rgb`, `_xterm256_to_rgb`, `detect_terminal_bg_rgb`,
  `resolve_terminal_bg_rgb`, `velocity_color_alpha`
- Output: `_safe_stdout_write`
- Main render: `ornamental_fill`, `render_frame`

### `synthermia/audio.py` (~1200 lines, lines 210–260, 312–360, 5017–5115, 5188–5248, 5365–5678, 7098–7458)
- System: `list_audio_output_targets`, `_ensure_pipewire_env`, `_pipewire_init_once`
- FluidSynth in-process: `_load_pyfluidsynth_module`, `_build_audio_events`, `RealtimeFluidSynthPlayer`
- Channel routing: `_build_track_program_map`, `_build_track_color_map`, `_apply_track_volume_overrides`,
  `_assign_audio_channels`, `_assign_control_channels`
- Driver/latency helpers: `_drain_stream`, `_write_audio_log_line`, all `_parse_pulse_*`,
  `_parse_pipewire_*`, `_probe_pipewire_stream_timing`, `_probe_pulse_sink_latency_seconds`,
  `_estimate_fluidsynth_output_latency_seconds`, `_pulseaudio_server_available`,
  `_resolve_audio_driver`, `_resolve_audio_output_target`, `_append_cli_audio_output_option`, `_fluidsynth_env`
- CLI launcher: `launch_soundfont_playback`, `stop_process`

### `synthermia/tui.py` (~2500 lines, lines 1582–1674, 1677–3924, 6277–6493)
- Popups: `_edit_text_popup`, `_draw_progress_popup`, `_message_popup`
- MP3 export: `_default_export_mp3_path`, `export_configured_midi_to_mp3`
  *(kept here to avoid circular import: audio.py → tui.py → audio.py)*
- Config screen: `run_config_screen` (with all its nested functions intact)

### `synthermia/playback.py` (~1700 lines, lines 189–309, 4726–4785, 4787–5016, 5250–5362, 5680–6269, 6518–7095)
- Live MIDI input: `list_midi_input_ports`, `resolve_midi_input_port`, `parse_aseqdump_event_line`
- Span window: `_SpanWindowIndex`, `_build_span_window_index`, `_select_spans_in_window`
- Range/time utils: `choose_note_range`, `resolve_midi_flip_bounds`, `playback_start_upper_bound`, `clamp_playback_start`
- Demo MIDI: `make_instrument_preview_spans`, `write_demo_midi_file`, `create_demo_midi_temp`
- Transforms: `transform_spans_for_playback`, `transform_controls_for_playback`
- Pedal visual: `build_pedal_hold_visual_spans`
- Recording: `_default_live_recording_path`, `_snapshot_live_spans`
- Main loops: `playback()`, `playback_live_midi_input()`
- **Note:** `parse_keys()` is duplicated as a nested function in both loops. Promote to a
  single module-level function in `playback.py` — the one safe micro-cleanup this refactor permits.

### `main.py` (~300 lines, was 7,700)
- `parse_args()` — kept here as the CLI contract definition
- `main()` — orchestration only; imports from `synthermia.*`

---

## Key technical notes

**No circular imports:** `tui.py` imports from `audio.py` (audio output listing, demo spawning).
`export_configured_midi_to_mp3` therefore lives in `tui.py` (not `audio.py`), since it calls
`_draw_progress_popup`. This is the one placement that defies pure-conceptual grouping but is
required for import cleanliness.

**`normalize_note_range_text` in config:** Called by both `args_to_saved_config` (config) and
`main()` — putting it in `playback.py` would force config → playback, creating a cycle. It
lives in `config.py`.

**`parse_args` stays in `main.py`:** It references `DEFAULT_VISUAL_GLYPHS` (imported from
`synthermia.config`) and stdlib only. Keeping it in `main.py` preserves the entry point as
the single place that defines the CLI surface.

**Nested functions inside `run_config_screen` stay nested.** The 23 nested functions inside the
config screen are not being promoted to module level — we are only moving the outer container.

---

## Execution order

Work leaf-to-root so each new file's imports are already satisfied before the next file needs them:

1. Create `synthermia/__init__.py` (empty)
2. Write `synthermia/models.py`
3. Write `synthermia/midi.py`
4. Write `synthermia/soundfont.py`
5. Write `synthermia/config.py`
6. Write `synthermia/render.py`
7. Write `synthermia/audio.py`
8. Write `synthermia/tui.py`
9. Write `synthermia/playback.py`
10. Rewrite `main.py` (replace entirely with thin entry point + `parse_args`)

Each file is written fresh with only the functions belonging to it, plus the correct imports.
The old `main.py` content is not touched until step 10.

---

## Verification

After step 10:
- `python3 main.py` → opens TUI config screen with no errors
- `python3 main.py --help` → prints full arg list
- `python3 main.py --list-midi-input-ports` → runs without import errors
- `python3 main.py midis/<any>.midi` → plays back (visual only, no soundfont needed)
- `python3 -c "from synthermia import models, midi, soundfont, config, render, audio, tui, playback"` → no import errors
