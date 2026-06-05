# Synthermia

Terminal Synthesia-style MIDI visualizer with SoundFont playback, live MIDI input, and deep TUI customization.

## Dependencies

Required (core app):
- Python 3.10+
- A UTF-8 terminal with ANSI color support

Required for SoundFont audio:
- FluidSynth runtime (`fluidsynth` CLI)
- A SoundFont file (`.sf2` / `.sf3`)

Required for in-process audio engine (`--audio-engine python`, default):
- `pyFluidSynth` Python module (`pip install pyfluidsynth` or distro equivalent)
- System `libfluidsynth`

Required for live MIDI input mode:
- `aseqdump` from `alsa-utils`

Optional (better auto-latency probing/logs):
- `pw-dump` (PipeWire tools)
- `pactl` (PulseAudio/PipeWire Pulse compatibility)

Optional (MP3 export from TUI):
- `ffmpeg`

Optional (native packed playback acceleration for very large MIDIs):
- Rust toolchain
- `maturin` for local extension builds

## Quick Start

Launch the interactive TUI config screen (pick MIDI, SoundFont, and settings):

```bash
python3 main.py
```

Or launch directly with a MIDI file:

```bash
python3 main.py midis/your_file.mid --soundfont Steinway_D-274.sf2
```

List live MIDI input ports:

```bash
python3 main.py --list-midi-input-ports
```

Live MIDI input mode:

```bash
python3 main.py --midi-input-mode --soundfont Steinway_D-274.sf2
```

## TUI Config Screen

Running with no arguments opens an interactive configuration screen (curses TUI) where you can:
- Pick a MIDI file and SoundFont(s)
- Configure audio engine, driver, output device, gain, and buffer settings
- Set per-track colors, instruments, and volume overrides with auto-matching to SoundFont presets
- Adjust visual parameters (rows, scales, glyphs, colors, pedal options)
- Export the current MIDI + SoundFont to MP3 (requires `ffmpeg`)
- Preview instruments with a built-in demo player (`d` key in any instrument picker)

Settings are persisted to `.synthermia_last_config.json` and restored on next launch.

## Functionalities

- Terminal piano-roll rendering with Unicode glyphs
- Multi-track parsing with tempo map, time signatures, controls, and note span generation
- Stuck-note mitigation modes (`auto`, `off`, `lifo`) for imperfect MIDI files
- Optional measure bars in playfield
- Pedal visualization (dotted hold tails) with independent pedal opacity scaling and configurable fade decay
- Velocity-based note transparency (dynamics visualization)
- Per-track color overrides, instrument overrides (SoundFont bank/program), volume overrides (track gain multipliers)
- Instrument picker demos (`d`) in every instrument selector (main, track, live-map)
- Auto-match MIDI track names/instrument names to SoundFont presets using fuzzy similarity
- Keyboard scaling controls (`rows`, `x_scale`, keyboard Y scale, black key extra rows)
- Off-piano ornamental background with toggle to plain background
- Pressed-key color modes: `split` (splits the key between multiple colors), `weighted` (calculates an average color), `off`
- SoundFont playback with two engines: Python in-process (`pyFluidSynth`) and CLI (`fluidsynth` subprocess)
- Audio backend settings (driver, output device, gain, period size, periods, sample rate)
- Optional audio logging with timestamps (`--audio-log`)
- Runtime playback controls: pause, seek, volume adjust, settings, quit
- MIDI pitch mirror (`--midi-flip`) for reversing note layout
- Live MIDI input mode (USB/ALSA) with upward-moving notes
- Live sustain pedal handling (including invert option)
- Live capture and save to MIDI (`s`) with CC64 pedal events preserved
- Live instrument hotkey mapping (change instrument while playing)
- MP3 export via TUI (FluidSynth renders to WAV, `ffmpeg` encodes to MP3)

## Runtime Controls

Standard playback:
- `Space`: pause/resume
- `e` (while paused): open settings / config screen
- `Left` / `Right`: seek -/+ 5s
- `Up` / `Down`: audio gain
- `q`: quit

Live input mode:
- `s`: save captured MIDI
- `Up` / `Down`: audio gain
- instrument hotkeys (configured in TUI): switch instrument + color
- `q`: quit

## CLI (Main)

Use `python3 main.py -h` for full options. Main groups:

- **Input**: MIDI file path, live MIDI mode + port + save dir + pedal invert + live mapping strings
- **Audio**: engine, FluidSynth binary, driver, output device (`--audio-output`), gain, period size/periods/sample rate, SoundFont (`--soundfont`), SoundFont library (`--soundfonts`), bank/program
- **Playback**: fps, rows, drop speed, speed, start, range, stuck-note-fix, midi-flip, playback-engine
- **Visual**: glyphs, dynamics threshold, pedal dots/opacity/decay, measure bars, keyboard scale, piano visibility, colors, terminal background, ascii-ui

## Optional Native Acceleration

Synthermia can run entirely in pure Python, but it now has an optional packed playback path aimed at very large MIDI files.

- `--playback-engine python`: force the legacy Python object path
- `--playback-engine packed`: use packed window queries and prefer the native extension when available
- `--playback-engine auto`: default; switches to the packed path for larger MIDI files

If the native extension is not installed, Synthermia falls back to a Python packed representation instead of failing.

To build the extension locally:

```bash
maturin develop
```
