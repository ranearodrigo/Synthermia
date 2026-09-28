from __future__ import annotations

import argparse
import errno
import fcntl
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import tempfile
import time
import tty
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from synthermia.audio import (
    RealtimeFluidSynthPlayer,
    _apply_track_volume_overrides,
    _assign_audio_channels,
    _assign_control_channels,
    _build_track_program_map,
    _build_track_program_map_for_tracks,
    _resolve_audio_driver,
    launch_soundfont_playback,
    stop_process,
)
from synthermia.config import (
    args_to_saved_config,
    build_live_instrument_hotkey_map,
    default_live_hotkey_color,
    parse_live_color_map,
    parse_note_range_text,
    resolve_midi_flip_bounds,
    resolve_visual_glyphs,
    save_last_config,
)
from synthermia.midi import (
    write_varlen,
)
from synthermia.media_controls import MediaCommand, MprisMediaControls
from synthermia.models import (
    DEFAULT_TEMPO_US_PER_QN,
    MAX_PIANO_NOTE,
    MIN_PIANO_NOTE,
    _ASEQDUMP_CC_RE,
    _ASEQDUMP_NOTE_OFF_RE,
    _ASEQDUMP_NOTE_ON_RE,
    ControlEvent,
    NoteSpan,
    _LiveMidiEvent,
)
from synthermia.packed import (
    BackgroundPlaybackLoadTask,
    PackedPlaybackSource,
    load_selected_playback_data,
    native_packed_task_available,
    resolve_playback_engine,
    scaled_partial_start_buffer_sec,
    should_auto_use_packed,
    should_use_partial_packed_startup,
    start_background_packed_playback_load,
)
from synthermia.render import (
    _safe_stdout_write,
    build_track_color_map,
    keyboard_row_counts,
    keyboard_width,
    ornamental_fill,
    render_frame,
    resolve_terminal_bg_rgb,
    rgb_triplet_to_text,
)
from synthermia.soundfont import (
    _build_soundfont_pair_map,
    _clamp_instrument_pair,
    _normalize_saved_soundfont_library,
    format_instrument_option,
    parse_instrument_pair,
    scan_soundfont_presets,
)
from synthermia.tui import run_background_task_popup, run_config_screen

PARTIAL_PROMOTE_AHEAD_MARGIN_SEC = 12.0
PARTIAL_BUFFER_LOW_WATER_SEC = 3.0
PARTIAL_BUFFER_RESUME_AHEAD_SEC = 12.0
PARTIAL_PROMOTE_AHEAD_REAL_SEC = 1.0
PARTIAL_BUFFER_LOW_WATER_REAL_SEC = 0.6
PARTIAL_BUFFER_RESUME_AHEAD_REAL_SEC = 1.5
PARTIAL_PROMOTE_AHEAD_MAX_SEC = 36.0
PARTIAL_BUFFER_LOW_WATER_MAX_SEC = 20.0
PARTIAL_BUFFER_RESUME_AHEAD_MAX_SEC = 48.0
DENSE_COLOR_FALLBACK_CELLS = 7000


def list_midi_input_ports() -> List[Tuple[str, str]]:
    exe = shutil.which("aseqdump")
    if not exe:
        return []
    try:
        out = subprocess.check_output([exe, "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    ports: List[Tuple[str, str]] = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+:\d+)\s+(.+?)\s{2,}(.+?)\s*$", line)
        if not m:
            continue
        port_id = m.group(1).strip()
        client_name = m.group(2).strip()
        port_name = m.group(3).strip()
        label = f"{port_id}  {client_name} / {port_name}"
        ports.append((port_id, label))
    return ports


def _should_use_loading_popup_for_args(args: argparse.Namespace) -> bool:
    midi_path = str(getattr(args, "midi", "")).strip()
    if not midi_path:
        return False
    engine = resolve_playback_engine(str(getattr(args, "playback_engine", "auto")))
    return engine == "packed" or (engine == "auto" and should_auto_use_packed(midi_path))

def resolve_midi_input_port(requested_port: str) -> Tuple[str, str]:
    requested = str(requested_port or "").strip()
    ports = list_midi_input_ports()
    if requested:
        for port_id, label in ports:
            if requested == port_id:
                return port_id, label
        for port_id, label in ports:
            if requested.lower() in label.lower():
                return port_id, label
        return requested, requested
    if not ports:
        raise ValueError(
            "No MIDI input ports found. Connect your keyboard and check `aseqdump -l`."
        )
    for port_id, label in ports:
        low = label.lower()
        if "through" in low or "announce" in low:
            continue
        return port_id, label
    return ports[0]


def parse_aseqdump_event_line(line: str, at_time: float) -> Optional[_LiveMidiEvent]:
    txt = str(line or "").strip()
    if not txt:
        return None
    m = _ASEQDUMP_NOTE_ON_RE.search(txt)
    if m:
        ch = max(0, min(15, int(m.group(1))))
        note = max(0, min(127, int(m.group(2))))
        vel = max(0, min(127, int(m.group(3))))
        if vel == 0:
            return _LiveMidiEvent(time_sec=at_time, kind="note_off", channel=ch, note=note, velocity=0)
        return _LiveMidiEvent(time_sec=at_time, kind="note_on", channel=ch, note=note, velocity=vel)
    m = _ASEQDUMP_NOTE_OFF_RE.search(txt)
    if m:
        ch = max(0, min(15, int(m.group(1))))
        note = max(0, min(127, int(m.group(2))))
        vel = max(0, min(127, int(m.group(3))))
        return _LiveMidiEvent(time_sec=at_time, kind="note_off", channel=ch, note=note, velocity=vel)
    m = _ASEQDUMP_CC_RE.search(txt)
    if m:
        ch = max(0, min(15, int(m.group(1))))
        cc = max(0, min(127, int(m.group(2))))
        val = max(0, min(127, int(m.group(3))))
        return _LiveMidiEvent(time_sec=at_time, kind="cc", channel=ch, controller=cc, value=val)
    return None

def choose_note_range(spans: Sequence[NoteSpan], forced: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    if forced:
        lo, hi = forced
        return max(MIN_PIANO_NOTE, lo), min(MAX_PIANO_NOTE, hi)

    # Always show the full 88-key keyboard unless user overrides --range.
    return MIN_PIANO_NOTE, MAX_PIANO_NOTE

@dataclass
class _SpanWindowIndex:
    by_start: List[NoteSpan]
    by_end: List[NoteSpan]
    start_values: List[float]
    end_values: List[float]


def _build_span_window_index(spans: Sequence[NoteSpan]) -> _SpanWindowIndex:
    by_start = sorted(
        list(spans),
        key=lambda sp: (float(sp.start), float(sp.end), int(sp.note), int(getattr(sp, "channel", 0))),
    )
    by_end = sorted(
        list(spans),
        key=lambda sp: (float(sp.end), float(sp.start), int(sp.note), int(getattr(sp, "channel", 0))),
    )
    return _SpanWindowIndex(
        by_start=by_start,
        by_end=by_end,
        start_values=[float(sp.start) for sp in by_start],
        end_values=[float(sp.end) for sp in by_end],
    )


def _select_spans_in_window(index: _SpanWindowIndex, t0: float, t1: float) -> List[NoteSpan]:
    # Return spans overlapping [t0, t1], choosing the smaller of:
    # - prefix by start<=t1, or
    # - suffix by end>=t0
    # This avoids O(N) full scans on dense files (especially pedal shadow spans).
    i1 = bisect_right(index.start_values, float(t1))
    j0 = bisect_left(index.end_values, float(t0))

    if i1 <= (len(index.by_end) - j0):
        return [sp for sp in index.by_start[:i1] if float(sp.end) >= float(t0)]

    out = [sp for sp in index.by_end[j0:] if float(sp.start) <= float(t1)]
    out.sort(key=lambda sp: (float(sp.start), float(sp.end), int(sp.note), int(getattr(sp, "channel", 0))))
    return out


def make_instrument_preview_spans() -> List[NoteSpan]:
    spans: List[NoteSpan] = []
    t = 0.0
    for n in (36, 48, 60, 72, 84):
        spans.append(NoteSpan(note=n, start=t, end=t + 0.28, velocity=100, channel=0, track=0))
        t += 0.34
    for n in (48, 55, 60):
        spans.append(NoteSpan(note=n, start=t + 0.10, end=t + 1.20, velocity=96, channel=0, track=0))
    t += 1.35
    for n in (60, 64, 67, 72):
        spans.append(NoteSpan(note=n, start=t, end=t + 1.35, velocity=92, channel=0, track=0))
    spans.sort(key=lambda s: s.start)
    return spans


def write_demo_midi_file(
    path: str,
    spans: Sequence[NoteSpan],
    ticks_per_qn: int = 480,
    instrument_bank: int = 0,
    instrument_program: int = 0,
    channel_programs: Optional[Dict[int, Tuple[int, int]]] = None,
    control_events: Optional[Sequence[ControlEvent]] = None,
    initial_controls: Optional[Dict[Tuple[int, int], int]] = None,
    insert_warmup_pulses: bool = True,
) -> None:
    tempo = DEFAULT_TEMPO_US_PER_QN
    tick_per_sec = (1_000_000.0 * ticks_per_qn) / tempo

    events: List[Tuple[int, int, bytes]] = [(0, 0, bytes([0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20]))]
    if channel_programs:
        for ch, pair in sorted(channel_programs.items()):
            channel = max(0, min(15, int(ch)))
            bank, program = _clamp_instrument_pair(pair[0], pair[1])
            bank_msb = (bank >> 7) & 0x7F
            bank_lsb = bank & 0x7F
            events.append((0, 0, bytes([0xB0 | channel, 0, bank_msb])))
            events.append((0, 0, bytes([0xB0 | channel, 32, bank_lsb])))
            events.append((0, 0, bytes([0xC0 | channel, program])))
    else:
        bank, program = _clamp_instrument_pair(instrument_bank, instrument_program)
        bank_msb = (bank >> 7) & 0x7F
        bank_lsb = bank & 0x7F
        events.append((0, 0, bytes([0xB0, 0, bank_msb])))
        events.append((0, 0, bytes([0xB0, 32, bank_lsb])))
        events.append((0, 0, bytes([0xC0, program])))
    if initial_controls:
        for (ch, controller), value in sorted(initial_controls.items()):
            channel = max(0, min(15, int(ch)))
            cc = max(0, min(127, int(controller)))
            vv = max(0, min(127, int(value)))
            events.append((0, 0, bytes([0xB0 | channel, cc, vv])))
    if control_events:
        for ev in control_events:
            tick = max(0, int(round(max(0.0, float(ev.time_sec)) * tick_per_sec)))
            ch = max(0, min(15, int(ev.channel)))
            cc = max(0, min(127, int(ev.controller)))
            vv = max(0, min(127, int(ev.value)))
            events.append((tick, 1, bytes([0xB0 | ch, cc, vv])))
    if insert_warmup_pulses and spans:
        first_start = min(max(0.0, float(sp.start)) for sp in spans)
        if first_start > 0.30:
            ch = 15
            events.append((0, 0, bytes([0xB0 | ch, 7, 1])))
            events.append((0, 0, bytes([0xB0 | ch, 11, 1])))
            pulse_step = 0.20
            pulse_dur = 0.015
            t = 0.0
            while t < first_start:
                start_tick = max(0, int(round(t * tick_per_sec)))
                end_tick = max(start_tick + 1, int(round((t + pulse_dur) * tick_per_sec)))
                events.append((start_tick, 2, bytes([0x90 | ch, 96, 1])))
                events.append((end_tick, 1, bytes([0x80 | ch, 96, 0])))
                t += pulse_step
    for sp in spans:
        start_tick = max(0, int(round(sp.start * tick_per_sec)))
        end_tick = max(start_tick + 1, int(round(sp.end * tick_per_sec)))
        velocity = max(1, min(127, int(sp.velocity)))
        note = max(0, min(127, int(sp.note)))
        channel = max(0, min(15, int(getattr(sp, "channel", 0))))
        events.append((start_tick, 2, bytes([0x90 | channel, note, velocity])))
        events.append((end_tick, 1, bytes([0x80 | channel, note, 0])))

    events.sort(key=lambda e: (e[0], e[1]))

    track = bytearray()
    prev_tick = 0
    for tick, _order, payload in events:
        delta = tick - prev_tick
        prev_tick = tick
        track.extend(write_varlen(delta))
        track.extend(payload)
    track.extend(write_varlen(0))
    track.extend(b"\xFF\x2F\x00")

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks_per_qn)
    trk = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    with open(path, "wb") as f:
        f.write(header)
        f.write(trk)


def create_demo_midi_temp(
    spans: Sequence[NoteSpan],
    instrument_bank: int = 0,
    instrument_program: int = 0,
    channel_programs: Optional[Dict[int, Tuple[int, int]]] = None,
    control_events: Optional[Sequence[ControlEvent]] = None,
    initial_controls: Optional[Dict[Tuple[int, int], int]] = None,
    insert_warmup_pulses: bool = True,
) -> str:
    fd, path = tempfile.mkstemp(prefix="synthermia_demo_", suffix=".mid")
    os.close(fd)
    write_demo_midi_file(
        path,
        spans,
        instrument_bank=instrument_bank,
        instrument_program=instrument_program,
        channel_programs=channel_programs,
        control_events=control_events,
        initial_controls=initial_controls,
        insert_warmup_pulses=insert_warmup_pulses,
    )
    return path

def transform_spans_for_playback(spans: Sequence[NoteSpan], start_sec: float, speed: float) -> List[NoteSpan]:
    if speed <= 0:
        raise ValueError("speed must be > 0")
    out: List[NoteSpan] = []
    for sp in spans:
        t_start = (sp.start - start_sec) / speed
        t_end = (sp.end - start_sec) / speed
        if t_end <= 0:
            continue
        if t_start < 0:
            t_start = 0.0
        if t_end <= t_start:
            t_end = t_start + 0.02
        out.append(
            NoteSpan(
                note=sp.note,
                start=t_start,
                end=t_end,
                velocity=sp.velocity,
                channel=int(getattr(sp, "channel", 0)),
                track=int(getattr(sp, "track", 0)),
            )
        )
    out.sort(key=lambda s: s.start)
    return out


def transform_controls_for_playback(
    controls: Sequence[ControlEvent],
    start_sec: float,
    speed: float,
) -> Tuple[List[ControlEvent], Dict[Tuple[int, int], int]]:
    if speed <= 0:
        raise ValueError("speed must be > 0")
    out: List[ControlEvent] = []
    initial_state: Dict[Tuple[int, int], int] = {}
    for ev in controls:
        t_src = float(ev.time_sec)
        key = (max(0, min(15, int(ev.channel))), max(0, min(127, int(ev.controller))))
        value = max(0, min(127, int(ev.value)))
        if t_src < start_sec:
            initial_state[key] = value
            continue
        t = (t_src - start_sec) / speed
        if t < 0:
            t = 0.0
        out.append(
            ControlEvent(
                time_sec=t,
                channel=key[0],
                controller=key[1],
                value=value,
                track=int(getattr(ev, "track", 0)),
            )
        )
    out.sort(key=lambda e: e.time_sec)
    return out, initial_state

def build_pedal_hold_visual_spans(
    spans: Sequence[NoteSpan],
    controls: Sequence[ControlEvent],
) -> List[NoteSpan]:
    if not spans or not controls:
        return []

    sustain_controls = [ev for ev in controls if int(getattr(ev, "controller", -1)) == 64]
    if not sustain_controls:
        return []

    voice_by_id: Dict[int, NoteSpan] = {}
    events: List[Tuple[float, int, str, int, int, int]] = []
    for vid, sp in enumerate(spans):
        start = max(0.0, float(sp.start))
        end = max(start + 1e-4, float(sp.end))
        note = max(0, min(127, int(sp.note)))
        channel = max(0, min(15, int(getattr(sp, "channel", 0))))
        voice_by_id[vid] = sp
        events.append((start, 2, "note_on", channel, note, vid))
        events.append((end, 1, "note_off", channel, note, vid))
    for ev in sustain_controls:
        t = max(0.0, float(ev.time_sec))
        channel = max(0, min(15, int(ev.channel)))
        value = max(0, min(127, int(ev.value)))
        events.append((t, 0, "cc64", channel, value, -1))
    events.sort(key=lambda it: (it[0], it[1], it[3], it[4], it[5]))

    sustain_down: Dict[int, bool] = defaultdict(bool)
    pressed: Dict[Tuple[int, int], deque[int]] = defaultdict(deque)
    sounding: Dict[Tuple[int, int], deque[int]] = defaultdict(deque)
    tail_start: Dict[int, float] = {}
    out: List[NoteSpan] = []

    def dequeue_remove(q: deque[int], value: int) -> bool:
        try:
            q.remove(value)
            return True
        except ValueError:
            return False

    def flush_tail(vid: int, end_t: float) -> None:
        st = tail_start.pop(vid, None)
        if st is None:
            return
        if end_t <= st + 1e-5:
            return
        src = voice_by_id.get(vid)
        if src is None:
            return
        out.append(
            NoteSpan(
                note=max(0, min(127, int(src.note))),
                start=float(st),
                end=float(end_t),
                velocity=max(1, min(127, int(src.velocity))),
                channel=max(0, min(15, int(getattr(src, "channel", 0)))),
                track=max(0, int(getattr(src, "track", 0))),
            )
        )

    def release_sustain(channel: int, at_time: float) -> None:
        for key, q_sound in list(sounding.items()):
            if key[0] != channel:
                continue
            pressed_len = len(pressed.get(key, ()))
            while len(q_sound) > pressed_len:
                vid = q_sound.popleft()
                flush_tail(vid, at_time)
            if not q_sound:
                sounding.pop(key, None)

    for t_ev, _ord, kind, channel, a, vid in events:
        if kind == "cc64":
            was_down = bool(sustain_down.get(channel, False))
            now_down = int(a) >= 64
            sustain_down[channel] = now_down
            if was_down and (not now_down):
                release_sustain(channel, t_ev)
            continue
        note = max(0, min(127, int(a)))
        key = (channel, note)
        if kind == "note_on":
            pressed[key].append(vid)
            sounding[key].append(vid)
            continue
        if kind == "note_off":
            q_pressed = pressed.get(key)
            if q_pressed:
                dequeue_remove(q_pressed, vid)
                if not q_pressed:
                    pressed.pop(key, None)
            if sustain_down.get(channel, False):
                tail_start[vid] = float(t_ev)
                continue
            q_sound = sounding.get(key)
            if q_sound:
                dequeue_remove(q_sound, vid)
                if not q_sound:
                    sounding.pop(key, None)
            flush_tail(vid, t_ev)

    final_t = 0.0
    if spans:
        final_t = max(final_t, max(float(sp.end) for sp in spans))
    if sustain_controls:
        final_t = max(final_t, max(float(ev.time_sec) for ev in sustain_controls))
    if final_t > 0.0:
        for vid in list(tail_start.keys()):
            flush_tail(vid, final_t)

    out.sort(key=lambda sp: (sp.start, sp.end, sp.note, sp.channel, sp.track))
    return out


def parse_keys(buf: str) -> Tuple[List[str], str]:
    out: List[str] = []
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch != "\x1b":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break
        if buf[i + 1] != "[":
            i += 1
            continue
        if i + 2 >= n:
            break
        code = buf[i + 2]
        if code == "A":
            out.append("UP")
        elif code == "B":
            out.append("DOWN")
        elif code == "C":
            out.append("RIGHT")
        elif code == "D":
            out.append("LEFT")
        i += 3
    return out, buf[i:]


def _playback_layout(
    args: argparse.Namespace,
    spans: Sequence[NoteSpan],
) -> Tuple[os.terminal_size, int, int, int, int, int, int]:
    term_size = shutil.get_terminal_size(fallback=(120, 36))
    # Outer border, inner frame, and the render header consume five rows.
    max_rows = max(4, term_size.lines - 5)
    _black_rows, _white_rows, kb_rows = keyboard_row_counts(args.kb_y_scale, args.kb_black_extra)
    min_rows = kb_rows + 1
    if max_rows < min_rows:
        raise ValueError(
            f"Terminal too short for --kb-y-scale {args.kb_y_scale}; need at least {min_rows + 5} terminal lines."
        )
    rows = max(min_rows, min(args.rows, max_rows))

    note_range = None
    if args.range:
        note_range = parse_note_range_text(str(args.range))
        if note_range is None:
            raise ValueError("--range must be like 21:108")
    note_lo, note_hi = choose_note_range(spans, note_range)
    max_frame_cols = max(8, max(1, term_size.columns) - 4)
    x_scale = int(args.x_scale)
    while x_scale > 1 and keyboard_width(note_lo, note_hi, x_scale) > max_frame_cols:
        x_scale -= 1
    if x_scale != args.x_scale:
        print(f"Note: --x-scale {args.x_scale} does not fit terminal width; using {x_scale}.", file=sys.stderr)
    frame_cols = keyboard_width(note_lo, note_hi, x_scale)
    return term_size, rows, rows - kb_rows, note_lo, note_hi, x_scale, frame_cols


def _prepare_audio_routing(
    args: argparse.Namespace,
    spans: Sequence[NoteSpan],
    controls: Sequence[ControlEvent],
) -> Tuple[
    List[NoteSpan], Dict[int, Tuple[int, int]], Dict[int, str], List[ControlEvent], List[str], bool
]:
    default_program = _clamp_instrument_pair(
        int(getattr(args, "instrument_bank", 0) or 0),
        int(getattr(args, "instrument_program", 0) or 0),
    )
    soundfont_paths = _normalize_saved_soundfont_library(
        getattr(args, "soundfonts", []),
        primary_soundfont=str(getattr(args, "soundfont", "")),
    )
    if not args.soundfont:
        return [], {0: default_program}, {0: ""}, list(controls), soundfont_paths, False

    valid_pairs_by_sf = _build_soundfont_pair_map(soundfont_paths)
    track_programs = _build_track_program_map(
        spans,
        default_program,
        str(args.soundfont),
        getattr(args, "track_instruments", {}),
        valid_pairs_by_sf=valid_pairs_by_sf,
    )
    audio_spans, channel_programs, channel_soundfonts, source_channels, track_default_channels = (
        _assign_audio_channels(spans, track_programs)
    )
    audio_spans = _apply_track_volume_overrides(audio_spans, getattr(args, "track_volumes", {}))
    audio_controls = _assign_control_channels(controls, source_channels, track_default_channels)
    active_soundfonts = {path.strip() for path in channel_soundfonts.values() if path.strip()}
    return audio_spans, channel_programs, channel_soundfonts, audio_controls, soundfont_paths, len(active_soundfonts) > 1


def playback(
    spans: Optional[Sequence[NoteSpan]],
    args: argparse.Namespace,
    controls: Optional[Sequence[ControlEvent]] = None,
    measure_times: Optional[Sequence[float]] = None,
    packed_source: Optional[PackedPlaybackSource] = None,
    pending_packed_task: Optional[BackgroundPlaybackLoadTask] = None,
) -> bool:
    spans_active = list(spans or [])
    if (not spans_active) and packed_source is None:
        raise ValueError("No playable notes found in MIDI")

    term_size, rows, play_rows, note_lo, note_hi, effective_x_scale, frame_cols = _playback_layout(args, spans_active)
    show_piano = not bool(getattr(args, "hide_piano", False))
    track_ids = packed_source.track_ids() if packed_source is not None else [int(getattr(sp, "track", 0)) for sp in spans_active]
    track_color_map = build_track_color_map(
        track_ids,
        getattr(args, "track_colors", {}),
        str(getattr(args, "track_color_mode", "truecolor")),
    )
    terminal_bg_rgb = resolve_terminal_bg_rgb(str(getattr(args, "terminal_bg_color", "")))
    visual_glyphs = resolve_visual_glyphs(args)
    controls_src = list(controls or [])
    if bool(getattr(args, "disable_pedal", False)):
        controls_src = [ev for ev in controls_src if int(getattr(ev, "controller", -1)) != 64]
    measure_times_src = list(measure_times or [])
    span_window_index = _build_span_window_index(spans_active) if spans_active else None
    packed_source_active = packed_source
    pedal_visual_spans: List[NoteSpan] = []
    pedal_window_index: Optional[_SpanWindowIndex] = None
    def require_all_spans() -> List[NoteSpan]:
        nonlocal spans_active, span_window_index
        if not spans_active and packed_source_active is not None:
            spans_active = packed_source_active.to_note_spans()
            if span_window_index is None:
                span_window_index = _build_span_window_index(spans_active)
        return spans_active

    if bool(getattr(args, "pedal_visual_dots", False)) and not bool(getattr(args, "disable_pedal", False)):
        pedal_visual_spans = build_pedal_hold_visual_spans(require_all_spans(), controls_src)
        if pedal_visual_spans:
            pedal_window_index = _build_span_window_index(pedal_visual_spans)

    audio_spans, channel_programs, channel_soundfonts, audio_controls, soundfont_paths, requires_python_audio = (
        _prepare_audio_routing(args, require_all_spans() if args.soundfont else spans_active, controls_src)
    )

    start_time = args.start
    end_time = float(packed_source_active.duration_sec) if packed_source_active is not None else max(sp.end for sp in spans_active)
    frame_dt = 1.0 / max(1, args.fps)
    title = os.path.basename(args.midi)
    use_offpiano_pattern = not args.no_offpiano_pattern
    custom_offpiano_pattern = str(getattr(args, "offpiano_pattern", ""))
    audio_gain = float(args.audio_gain)
    audio_engine = str(getattr(args, "audio_engine", "python")).strip().lower()
    if audio_engine not in {"python", "cli"}:
        raise ValueError("--audio-engine must be 'python' or 'cli'")
    audio_engine_active = audio_engine
    play_t = start_time
    clock_anchor_t = play_t
    clock_anchor_perf = time.perf_counter()
    paused = False
    py_audio: Optional[RealtimeFluidSynthPlayer] = None
    audio_proc: Optional[subprocess.Popen] = None
    audio_temp_midi: Optional[str] = None
    audio_cache_key: Optional[Tuple[float, float]] = None
    final_note_diag_reported = pending_packed_task is None

    def maybe_print_final_note_diag(note_diag: Optional[Dict[str, int]]) -> None:
        nonlocal final_note_diag_reported
        if final_note_diag_reported:
            return
        diag = dict(note_diag or {})
        overlap_fixes = int(diag.get("overlap_fixes", 0))
        orphan_offs = int(diag.get("orphan_note_offs", 0))
        unmatched_ons = int(diag.get("remaining_unmatched_note_ons", 0))
        long_note_trims = int(diag.get("long_note_trims", 0))
        if overlap_fixes > 0 or orphan_offs > 0 or long_note_trims > 0:
            print(
                f"MIDI cleanup: fixed {overlap_fixes} overlap(s), "
                f"trimmed {long_note_trims} long note(s), saw {orphan_offs} orphan note-off(s), "
                f"{unmatched_ons} unmatched note-on(s) remain.",
                file=sys.stderr,
            )
        final_note_diag_reported = True

    def rebuild_playback_runtime(resume_t: float, *, show_loading_popup: bool = False) -> None:
        nonlocal spans_active, controls_src, measure_times_src, term_size, rows, play_rows
        nonlocal note_lo, note_hi, effective_x_scale, frame_cols
        nonlocal show_piano, track_color_map, terminal_bg_rgb, visual_glyphs
        nonlocal span_window_index, packed_source_active, pedal_visual_spans, pedal_window_index
        nonlocal soundfont_paths, requires_python_audio
        nonlocal audio_spans, channel_programs, channel_soundfonts, audio_controls
        nonlocal start_time, end_time, frame_dt, title, use_offpiano_pattern, custom_offpiano_pattern, audio_gain
        nonlocal audio_engine, audio_engine_active, play_t, clock_anchor_t, clock_anchor_perf, paused
        nonlocal py_audio, audio_proc, audio_temp_midi, audio_cache_key, pending_packed_task

        midi_path = str(getattr(args, "midi", "")).strip()
        if not midi_path or not os.path.exists(midi_path):
            raise ValueError(f"MIDI file not found: {midi_path}")

        release_audio_runtime()

        def load_playback_data():
            return load_selected_playback_data(
                midi_path,
                stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "off")),
                midi_flip=bool(getattr(args, "midi_flip", False)),
                flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
                long_note_threshold=float(getattr(args, "long_note_threshold", 0.0)),
                long_note_duration=float(getattr(args, "long_note_duration", 0.0)),
                playback_engine=str(getattr(args, "playback_engine", "auto")),
            )

        if show_loading_popup and _should_use_loading_popup_for_args(args):
            if native_packed_task_available():
                use_partial_start = should_use_partial_packed_startup(
                    midi_path,
                    playback_engine=str(getattr(args, "playback_engine", "auto")),
                    playback_speed=float(getattr(args, "speed", 1.0)),
                    start_sec=float(resume_t),
                    soundfont_path=str(getattr(args, "soundfont", "")),
                    pedal_visual_dots=bool(getattr(args, "pedal_visual_dots", False)),
                )
                partial_start_buffer_sec = scaled_partial_start_buffer_sec(float(getattr(args, "speed", 1.0)))
                native_task = start_background_packed_playback_load(
                    midi_path,
                    stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "off")),
                    midi_flip=bool(getattr(args, "midi_flip", False)),
                    flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
                    long_note_threshold=float(getattr(args, "long_note_threshold", 0.0)),
                    long_note_duration=float(getattr(args, "long_note_duration", 0.0)),
                    initial_ready_sec=partial_start_buffer_sec if use_partial_start else None,
                )
                if use_partial_start:
                    loaded = run_background_task_popup(
                        " Loading MIDI ",
                        "Preparing the first playable packed buffer.\nThe rest of the timeline will keep building in the background.",
                        lambda: native_task.wait_until_ready(partial_start_buffer_sec),
                        detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                        progress_fn=native_task.progress,
                    )
                    pending_packed_task = native_task if loaded.is_partial and not native_task.done() else None
                else:
                    loaded = run_background_task_popup(
                        " Loading MIDI ",
                        "Refreshing packed playback data.\nLarge black MIDI files may take a while.",
                        native_task.result,
                        detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                        progress_fn=native_task.progress,
                    )
                    pending_packed_task = None
            else:
                loaded = run_background_task_popup(
                    " Loading MIDI ",
                    "Refreshing packed playback data.\nLarge black MIDI files may take a while.",
                    load_playback_data,
                    detail_prefix=f"Engine: {resolve_playback_engine(str(getattr(args, 'playback_engine', 'auto')))}",
                )
                pending_packed_task = None
        else:
            loaded = load_playback_data()
            pending_packed_task = None
        spans_active = list(loaded.spans or [])
        controls_src = loaded.controls
        measure_times_src = loaded.measure_times
        packed_source_active = loaded.packed_source
        span_window_index = _build_span_window_index(spans_active) if spans_active else None
        if (not spans_active) and packed_source_active is None:
            raise ValueError("No playable notes found in MIDI")
        if bool(getattr(args, "disable_pedal", False)):
            controls_src = [ev for ev in controls_src if int(getattr(ev, "controller", -1)) != 64]

        term_size, rows, play_rows, note_lo, note_hi, effective_x_scale, frame_cols = _playback_layout(args, spans_active)
        show_piano = not bool(getattr(args, "hide_piano", False))
        track_ids = packed_source_active.track_ids() if packed_source_active is not None else [int(getattr(sp, "track", 0)) for sp in spans_active]
        track_color_map = build_track_color_map(
            track_ids,
            getattr(args, "track_colors", {}),
            str(getattr(args, "track_color_mode", "truecolor")),
        )
        terminal_bg_rgb = resolve_terminal_bg_rgb(str(getattr(args, "terminal_bg_color", "")))
        visual_glyphs = resolve_visual_glyphs(args)
        pedal_visual_spans = []
        pedal_window_index = None
        if bool(getattr(args, "pedal_visual_dots", False)) and not bool(getattr(args, "disable_pedal", False)):
            pedal_visual_spans = build_pedal_hold_visual_spans(require_all_spans(), controls_src)
            if pedal_visual_spans:
                pedal_window_index = _build_span_window_index(pedal_visual_spans)

        audio_spans, channel_programs, channel_soundfonts, audio_controls, soundfont_paths, requires_python_audio = (
            _prepare_audio_routing(args, require_all_spans() if args.soundfont else spans_active, controls_src)
        )

        start_time = float(resume_t)
        end_time = float(packed_source_active.duration_sec) if packed_source_active is not None else max(sp.end for sp in spans_active)
        frame_dt = 1.0 / max(1, int(args.fps))
        title = os.path.basename(midi_path)
        use_offpiano_pattern = not args.no_offpiano_pattern
        custom_offpiano_pattern = str(getattr(args, "offpiano_pattern", ""))
        audio_gain = float(args.audio_gain)
        audio_engine = str(getattr(args, "audio_engine", "python")).strip().lower()
        if audio_engine not in {"python", "cli"}:
            audio_engine = "python"
        audio_engine_active = audio_engine
        play_t = min(max(0.0, float(resume_t)), end_time if end_time > 0 else max(0.0, float(resume_t)))
        clock_anchor_t = play_t
        clock_anchor_perf = time.perf_counter()
        paused = True
        args.start = float(play_t)

    def offpiano_fill(width: int, row: int = 0, phase: int = 0) -> str:
        if width <= 0:
            return ""
        if use_offpiano_pattern:
            return ornamental_fill(width, row, phase, pattern_text=custom_offpiano_pattern)
        return " " * width

    def set_play_clock(midi_time: float, perf_time: Optional[float] = None) -> None:
        nonlocal play_t, clock_anchor_t, clock_anchor_perf
        clock_anchor_t = max(0.0, float(midi_time))
        clock_anchor_perf = time.perf_counter() if perf_time is None else perf_time
        play_t = clock_anchor_t

    def update_play_time(now: float) -> None:
        nonlocal play_t
        elapsed = now - clock_anchor_perf
        if elapsed <= 0:
            play_t = clock_anchor_t
            return
        play_t = clock_anchor_t + elapsed * args.speed

    def restart_audio(at_time: float) -> None:
        nonlocal audio_proc, audio_temp_midi, py_audio, audio_engine_active
        nonlocal audio_cache_key
        if not args.soundfont:
            set_play_clock(at_time)
            return
        if audio_engine_active == "python":
            if py_audio is None:
                try:
                    driver = _resolve_audio_driver(args.audio_driver)
                    py_audio = RealtimeFluidSynthPlayer(
                        spans=audio_spans,
                        soundfont_path=str(args.soundfont),
                        soundfont_paths=soundfont_paths,
                        audio_gain=audio_gain,
                        driver=driver,
                        output_target=str(getattr(args, "audio_output", "")),
                        samplerate=int(getattr(args, "audio_samplerate", 0) or 0),
                        period_size=int(getattr(args, "audio_period_size", 0) or 0),
                        periods=int(getattr(args, "audio_periods", 0) or 0),
                        polyphony=int(getattr(args, "audio_polyphony", 0) or 0),
                        channel_programs=channel_programs,
                        channel_soundfonts=channel_soundfonts,
                        control_events=audio_controls,
                    )
                    print(
                        f"Audio: using python engine driver '{py_audio.driver_used or 'default'}'.",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    if requires_python_audio:
                        raise ValueError(
                            f"Python audio engine failed with multiple SoundFonts configured: {exc}"
                        ) from exc
                    # Fall back to CLI engine if python audio cannot open a device.
                    audio_engine_active = "cli"
                    py_audio = None
                    print(f"Audio: python engine init failed ({exc}); falling back to cli.", file=sys.stderr)
            if py_audio is not None:
                py_audio.set_gain(audio_gain)
                py_audio.seek(at_time)
                set_play_clock(at_time)
                return
        if requires_python_audio:
            raise ValueError(
                "Multiple SoundFonts are active. Use python audio engine (cli engine cannot route per-channel SoundFonts)."
            )
        stop_process(audio_proc)
        audio_proc = None
        cache_key = (at_time, args.speed)
        if audio_temp_midi and audio_cache_key == cache_key and os.path.exists(audio_temp_midi):
            args.audio_midi = audio_temp_midi
        else:
            if audio_temp_midi and os.path.exists(audio_temp_midi):
                os.unlink(audio_temp_midi)
            transformed = transform_spans_for_playback(audio_spans, at_time, args.speed)
            transformed_controls, initial_controls = transform_controls_for_playback(
                audio_controls,
                at_time,
                args.speed,
            )
            if (not transformed) and (not transformed_controls):
                set_play_clock(at_time)
                return
            audio_temp_midi = create_demo_midi_temp(
                transformed,
                channel_programs=channel_programs,
                control_events=transformed_controls,
                initial_controls=initial_controls,
            )
            audio_cache_key = cache_key
            args.audio_midi = audio_temp_midi
        args.audio_gain = audio_gain
        audio_proc, ready_perf, output_latency = launch_soundfont_playback(args)
        target_perf = ready_perf + output_latency
        now_perf = time.perf_counter()
        if target_perf > now_perf:
            target_perf = now_perf
        set_play_clock(at_time, target_perf)

    def sync_audio_to_time(at_time: float) -> None:
        if not args.soundfont:
            set_play_clock(at_time)
            return
        restart_audio(at_time)

    def set_paused(next_paused: bool) -> None:
        nonlocal paused
        next_state = bool(next_paused)
        if paused == next_state:
            return
        paused = next_state
        if paused:
            stop_audio_only()
        else:
            sync_audio_to_time(play_t)

    def seek_to(target_time: float) -> None:
        nonlocal play_t
        requested_target = max(0.0, float(target_time))
        if pending_packed_task is not None and requested_target > (end_time - 0.25):
            promote_pending_packed_source(
                force_wait=True,
                wait_target_override=requested_target + partial_buffer_resume_ahead_sec(),
            )
        seek_target = min(requested_target, float(end_time))
        play_t = min(current_ready_timeline_sec() + 1.0, seek_target)
        if not paused:
            sync_audio_to_time(play_t)
        else:
            set_play_clock(play_t)

    def set_volume(next_gain: float) -> None:
        nonlocal audio_gain
        audio_gain = max(0.0, min(2.0, float(next_gain)))
        if py_audio is not None:
            py_audio.set_gain(audio_gain)
        elif not paused and args.soundfont:
            sync_audio_to_time(play_t)

    def adjust_volume(delta: float) -> None:
        set_volume(audio_gain + float(delta))

    def apply_media_command(command: MediaCommand) -> bool:
        action = command.action
        if action == "toggle_pause":
            set_paused(not paused)
        elif action == "pause":
            set_paused(True)
        elif action == "play":
            set_paused(False)
        elif action == "seek_back":
            seek_to(play_t - 5.0)
        elif action == "seek_forward":
            seek_to(play_t + 5.0)
        elif action == "seek_relative":
            seek_to(play_t + float(command.value or 0.0))
        elif action == "seek_absolute":
            seek_to(float(command.value or 0.0))
        elif action == "set_volume":
            set_volume(float(command.value or 0.0))
        elif action == "quit":
            return False
        return True

    def stop_audio_only() -> None:
        nonlocal audio_proc
        if py_audio is not None:
            py_audio.all_notes_off()
        stop_process(audio_proc)
        audio_proc = None

    def release_audio_runtime() -> None:
        nonlocal py_audio, audio_proc, audio_temp_midi, audio_cache_key
        if py_audio is not None:
            try:
                py_audio.close()
            except Exception:
                pass
            py_audio = None
        stop_process(audio_proc)
        audio_proc = None
        if audio_temp_midi and os.path.exists(audio_temp_midi):
            os.unlink(audio_temp_midi)
        audio_temp_midi = None
        audio_cache_key = None

    def _apply_loaded_packed_source(loaded: object, *, clear_pending: bool = False) -> bool:
        nonlocal pending_packed_task, controls_src, measure_times_src, packed_source_active, end_time
        packed_source = getattr(loaded, "packed_source", None)
        if packed_source is None:
            if clear_pending:
                pending_packed_task = None
            return False
        controls_src = list(getattr(loaded, "controls", []) or [])
        if bool(getattr(args, "disable_pedal", False)):
            controls_src = [ev for ev in controls_src if int(getattr(ev, "controller", -1)) != 64]
        measure_times_src = list(getattr(loaded, "measure_times", []) or [])
        packed_source_active = packed_source
        end_time = float(packed_source_active.duration_sec)
        if clear_pending:
            maybe_print_final_note_diag(getattr(loaded, "note_diag", None))
            pending_packed_task = None
        return True

    def current_ready_timeline_sec() -> float:
        ready_end = float(end_time)
        if pending_packed_task is not None:
            try:
                ready_end = max(ready_end, float(pending_packed_task.ready_duration_sec()))
            except Exception:
                pass
        return max(0.0, ready_end)

    def scaled_partial_timeline_sec(base_sec: float, real_sec: float, max_sec: float) -> float:
        speed = max(0.1, float(getattr(args, "speed", 1.0)))
        return max(float(base_sec), min(float(max_sec), speed * float(real_sec)))

    def partial_promote_margin_sec() -> float:
        return scaled_partial_timeline_sec(
            PARTIAL_PROMOTE_AHEAD_MARGIN_SEC,
            PARTIAL_PROMOTE_AHEAD_REAL_SEC,
            PARTIAL_PROMOTE_AHEAD_MAX_SEC,
        )

    def partial_buffer_low_water_sec() -> float:
        return scaled_partial_timeline_sec(
            PARTIAL_BUFFER_LOW_WATER_SEC,
            PARTIAL_BUFFER_LOW_WATER_REAL_SEC,
            PARTIAL_BUFFER_LOW_WATER_MAX_SEC,
        )

    def partial_buffer_resume_ahead_sec() -> float:
        return scaled_partial_timeline_sec(
            PARTIAL_BUFFER_RESUME_AHEAD_SEC,
            PARTIAL_BUFFER_RESUME_AHEAD_REAL_SEC,
            PARTIAL_BUFFER_RESUME_AHEAD_MAX_SEC,
        )

    def promote_pending_packed_source(
        *,
        force_wait: bool = False,
        allow_partial_refresh: bool = True,
        wait_target_override: Optional[float] = None,
    ) -> bool:
        nonlocal pending_packed_task, controls_src, measure_times_src, packed_source_active, end_time
        if pending_packed_task is None:
            return False
        if allow_partial_refresh and not pending_packed_task.done():
            loaded_partial = pending_packed_task.partial_result()
            if (
                loaded_partial is not None
                and loaded_partial.packed_source is not None
                and (
                    packed_source_active is None
                    or float(loaded_partial.packed_source.duration_sec) > (float(packed_source_active.duration_sec) + 1e-9)
                )
            ):
                return _apply_loaded_packed_source(loaded_partial, clear_pending=False)
        if not pending_packed_task.done():
            if not force_wait:
                return False
            resume_ahead_sec = partial_buffer_resume_ahead_sec()
            wait_target = max(
                float(wait_target_override) if wait_target_override is not None else float(play_t) + resume_ahead_sec,
                float(end_time) + resume_ahead_sec,
            )
            loaded = run_background_task_popup(
                " Buffering MIDI ",
                "Building more packed playback data ahead of the playhead.",
                lambda: pending_packed_task.wait_until_ready(wait_target),
                detail_prefix=f"Need: {wait_target:.1f}s ready",
                progress_fn=pending_packed_task.progress,
            )
            if getattr(loaded, "is_partial", False):
                return _apply_loaded_packed_source(loaded, clear_pending=False)
        else:
            loaded = pending_packed_task.result()
        return _apply_loaded_packed_source(loaded, clear_pending=True)


    _safe_stdout_write("\x1b[2J\x1b[H\x1b[?25l")

    stdin_fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(stdin_fd)
    old_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
    keybuf = ""
    set_play_clock(play_t)

    def restore_terminal_input_mode() -> None:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_flags)

    def enter_playback_input_mode() -> None:
        tty.setcbreak(stdin_fd)
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)

    media_controls = MprisMediaControls(title=title)
    media_controls.start()

    try:
        enter_playback_input_mode()
        sync_audio_to_time(play_t)

        while True:
            now = time.perf_counter()
            if not paused:
                update_play_time(now)
                if py_audio is not None:
                    py_audio.update(play_t)
            media_controls.update(paused=paused, position_sec=play_t, volume=audio_gain, title=title)

            while True:
                ready, _, _ = select.select([stdin_fd], [], [], 0)
                if not ready:
                    break
                try:
                    data = os.read(stdin_fd, 128)
                except BlockingIOError:
                    break
                if not data:
                    break
                keybuf += data.decode("utf-8", errors="ignore")
            keys, keybuf = parse_keys(keybuf)
            for command in media_controls.pop_commands():
                if not apply_media_command(command):
                    return False
            for key in keys:
                if key == " ":
                    set_paused(not paused)
                elif key in {"e", "E"}:
                    resume_hint = float(play_t)
                    paused = True
                    stop_audio_only()
                    args.start = resume_hint
                    save_last_config(args_to_saved_config(args, str(getattr(args, "midi", ""))))
                    restore_terminal_input_mode()
                    args = run_config_screen(args)
                    release_audio_runtime()
                    rebuild_playback_runtime(
                        float(getattr(args, "start", resume_hint)),
                        show_loading_popup=True,
                    )
                    enter_playback_input_mode()
                    paused = False
                    sync_audio_to_time(play_t)
                    keybuf = ""
                elif key == "LEFT":
                    seek_to(play_t - 5.0)
                elif key == "RIGHT":
                    seek_to(play_t + 5.0)
                elif key == "UP":
                    adjust_volume(0.05)
                elif key == "DOWN":
                    adjust_volume(-0.05)
                elif key in {"q", "Q"}:
                    return False

            if pending_packed_task is not None:
                if pending_packed_task.done():
                    promote_pending_packed_source()
                else:
                    ready_sec = pending_packed_task.ready_duration_sec()
                    current_end = float(end_time)
                    if ready_sec >= (current_end + partial_promote_margin_sec()):
                        promote_pending_packed_source(allow_partial_refresh=True)
                if pending_packed_task is not None and (not paused) and play_t >= max(0.0, end_time - partial_buffer_low_water_sec()):
                    paused = True
                    stop_audio_only()
                    promote_pending_packed_source(force_wait=True)
                    sync_audio_to_time(play_t)
                    paused = False

            t = play_t
            if (not paused) and t > end_time + 1.0:
                break

            progress = min(1.0, t / max(0.001, end_time))
            status = "PAUSED" if paused else "PLAY"
            if pending_packed_task is not None:
                status = f"{status}|BUILD"
            window_sec = max(0.001, float(play_rows) / max(0.001, float(args.drop_speed)))
            t0 = t - 0.05
            t1 = t + window_sec
            density_data = None
            if packed_source_active is not None:
                if (
                    packed_source_active.native_backend
                    and packed_source_active.note_count >= 20000
                ):
                    density_data = packed_source_active.native_density_data(
                        t=t,
                        note_lo=note_lo,
                        note_hi=note_hi,
                        play_rows=play_rows,
                        hit_row=play_rows - 1,
                        x_scale=effective_x_scale,
                        move_speed=float(args.drop_speed),
                        length_speed=float(args.drop_speed),
                        note_direction="down",
                    )
                if density_data is not None:
                    visible_spans = []
                else:
                    visible_spans = packed_source_active.visible_note_tuples(t0, t1)
            else:
                if span_window_index is None:
                    visible_spans = []
                else:
                    visible_spans = _select_spans_in_window(span_window_index, t0, t1)
            visible_pedal_spans: List[NoteSpan] = []
            if pedal_window_index is not None:
                visible_pedal_spans = _select_spans_in_window(pedal_window_index, t0, t1)
            dense_color_fallback = False
            if density_data is not None and not bool(getattr(args, "no_color", False)):
                try:
                    dense_color_fallback = len(density_data.get("cells", ())) >= DENSE_COLOR_FALLBACK_CELLS
                except Exception:
                    dense_color_fallback = False
            if dense_color_fallback:
                status = f"{status}|MONO"
            ready_info = ""
            if pending_packed_task is not None:
                ready_end = current_ready_timeline_sec()
                ready_margin = max(0.0, ready_end - t)
                ready_info = f" ready={ready_end:.1f}s (+{ready_margin:.1f}s)"
            if getattr(args, "ascii_ui", False):
                run_title = (
                    f"{title} [{status}] vol={audio_gain:.2f}{ready_info}  "
                    "(Space pause, e settings, left/right seek, up/down volume, q quit)"
                )
            else:
                run_title = (
                    f"{title} [{status}] vol={audio_gain:.2f}{ready_info}  "
                    "(Space pause, e settings, ←/→ seek, ↑/↓ volume, q quit)"
                )
            if show_piano:
                frame = render_frame(
                    spans=visible_spans,
                    pedal_spans=visible_pedal_spans,
                    t=t,
                    note_lo=note_lo,
                    note_hi=note_hi,
                    rows=rows,
                x_scale=effective_x_scale,
                kb_y_scale=args.kb_y_scale,
                    kb_black_extra=args.kb_black_extra,
                    move_speed=args.drop_speed,
                    length_speed=args.drop_speed,
                    use_color=(not args.no_color) and (not dense_color_fallback),
                    title=run_title,
                    progress=progress,
                    track_colors=track_color_map,
                    note_color_mode=str(getattr(args, "note_color_mode", "truecolor")),
                    pedal_color_mode=str(getattr(args, "pedal_color_mode", "truecolor")),
                    piano_color_mode=str(getattr(args, "piano_color_mode", "truecolor")),
                    pressed_key_color_mode=str(getattr(args, "pressed_key_color_mode", "split")),
                    dynamics_full_velocity=float(getattr(args, "dynamics_full_velocity", 96.0)),
                    pedal_opacity_scale=float(getattr(args, "pedal_opacity_scale", 0.5)),
                    pedal_decay_seconds=float(getattr(args, "pedal_decay_seconds", 10.0)),
                    terminal_bg_rgb=terminal_bg_rgb,
                    visual_glyphs=visual_glyphs,
                    measure_times=measure_times_src if bool(getattr(args, "measure_bars", False)) else None,
                    density_data=density_data,
                )
                raw_lines = frame.splitlines()
                frame_rows = len(raw_lines) + 2
                framed_cols = frame_cols + 2
            else:
                raw_lines = []
                frame_rows = 0
                framed_cols = 0

            term_w = max(1, term_size.columns)
            term_h = max(1, term_size.lines)
            inner_w = max(1, term_w - 2)
            inner_h = max(1, term_h - 2)

            if show_piano:
                left_pad = max(0, (inner_w - framed_cols) // 2)
                top_pad = max(0, (inner_h - frame_rows) // 2)
            else:
                left_pad = 0
                top_pad = 0

            screen_lines: List[str] = []
            if term_w >= 2:
                top_inner = offpiano_fill(term_w - 2, -1, 0)
                bottom_inner = offpiano_fill(term_w - 2, inner_h, 0)
                screen_lines.append("╔" + top_inner + "╗")
            else:
                screen_lines.append(offpiano_fill(term_w, -1, 0))

            for r in range(inner_h):
                if show_piano and top_pad <= r < (top_pad + frame_rows):
                    fr = r - top_pad
                    right_len = max(0, inner_w - left_pad - framed_cols)
                    # Build one continuous tiled row across the full inner width.
                    # The piano overlays this row, so pattern continuity remains
                    # correct "behind" the hidden middle region.
                    full_row = offpiano_fill(inner_w, r, 0)
                    left = full_row[:left_pad]
                    right = full_row[left_pad + framed_cols : left_pad + framed_cols + right_len]
                    y = r + 1
                    if fr == 0:
                        src = "╭" + ("─" * frame_cols) + "╮"
                    elif fr == frame_rows - 1:
                        src = "╰" + ("─" * frame_cols) + "╯"
                    else:
                        ln = raw_lines[fr - 1]
                        src = "│" + ln + "│"
                    inner = left + src + right
                else:
                    inner = offpiano_fill(inner_w, r, 0)

                if term_w >= 2:
                    screen_lines.append("║" + inner + "║")
                else:
                    screen_lines.append(inner[:term_w])

            if term_w >= 2:
                screen_lines.append("╚" + bottom_inner + "╝")
            else:
                screen_lines.append(offpiano_fill(term_w, inner_h, 0))

            _safe_stdout_write("\x1b[H" + "\n".join(screen_lines))

            sleep_for = frame_dt - (time.perf_counter() - now)
            if sleep_for > 0:
                time.sleep(sleep_for)
        return True
    finally:
        media_controls.stop()
        if py_audio is not None:
            py_audio.close()
        stop_process(audio_proc)
        if audio_temp_midi and os.path.exists(audio_temp_midi):
            os.unlink(audio_temp_midi)
        restore_terminal_input_mode()
        _safe_stdout_write("\x1b[0m\x1b[?25h\n")


def _default_live_recording_path(save_dir: str) -> str:
    folder = str(save_dir or "").strip() or "midis"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(folder, f"synthermia_live_{ts}.mid")

def _snapshot_live_spans(
    completed: Sequence[NoteSpan],
    active_voices: Dict[Tuple[int, int], deque[Tuple[float, int, int]]],
    now_t: float,
) -> List[NoteSpan]:
    out = list(completed)
    for (channel, note), starts in active_voices.items():
        for start_t, velocity, color_track in starts:
            end_t = max(start_t + 0.01, now_t)
            out.append(
                NoteSpan(
                    note=int(note),
                    start=max(0.0, float(start_t)),
                    end=float(end_t),
                    velocity=max(1, min(127, int(velocity))),
                    channel=max(0, min(15, int(channel))),
                    track=max(0, int(color_track)),
                )
            )
    out.sort(key=lambda s: s.start)
    return out

def playback_live_midi_input(args: argparse.Namespace) -> None:
    aseqdump = shutil.which("aseqdump")
    if not aseqdump:
        raise ValueError(
            "Live MIDI input mode requires `aseqdump` (package: alsa-utils). "
            "Install it and try again."
        )

    port_id, port_label = resolve_midi_input_port(str(getattr(args, "midi_input_port", "")))
    cmd = [aseqdump, "-p", port_id]
    midi_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
    )
    if midi_proc.stdout is None:
        stop_process(midi_proc)
        raise ValueError("Failed to open MIDI input stream")
    midi_fd = midi_proc.stdout.fileno()
    midi_old_flags = fcntl.fcntl(midi_fd, fcntl.F_GETFL)
    fcntl.fcntl(midi_fd, fcntl.F_SETFL, midi_old_flags | os.O_NONBLOCK)
    midi_buf = b""

    try:
        term_size, rows, _play_rows, note_lo, note_hi, effective_x_scale, frame_cols = _playback_layout(args, [])
    except ValueError:
        stop_process(midi_proc)
        raise
    show_piano = not bool(getattr(args, "hide_piano", False))
    use_offpiano_pattern = not args.no_offpiano_pattern
    custom_offpiano_pattern = str(getattr(args, "offpiano_pattern", ""))
    track_color_map: Dict[int, Tuple[int, int, int]] = {}
    terminal_bg_rgb = resolve_terminal_bg_rgb(str(getattr(args, "terminal_bg_color", "")))
    visual_glyphs = resolve_visual_glyphs(args)

    live_audio: Optional[RealtimeFluidSynthPlayer] = None
    audio_gain = float(args.audio_gain)
    default_program = _clamp_instrument_pair(
        int(getattr(args, "instrument_bank", 0) or 0),
        int(getattr(args, "instrument_program", 0) or 0),
    )
    current_program = default_program
    instrument_hotkeys, instrument_labels = build_live_instrument_hotkey_map(args)
    valid_sf_pairs: Optional[set[Tuple[int, int]]] = None
    if args.soundfont:
        try:
            valid_sf_pairs = {
                _clamp_instrument_pair(*parse_instrument_pair(opt, (0, 0)))
                for opt in scan_soundfont_presets(str(args.soundfont))
            }
        except Exception:
            valid_sf_pairs = None
    instrument_colors = parse_live_color_map(str(getattr(args, "midi_input_color_map", "")))
    for hk in instrument_hotkeys.keys():
        if hk not in instrument_colors:
            instrument_colors[hk] = default_live_hotkey_color(hk)
    current_color = instrument_colors.get(next(iter(sorted(instrument_hotkeys.keys()))), default_live_hotkey_color("0"))
    current_color_track = 0
    track_color_map[current_color_track] = current_color
    hotkey_display = "".join(instrument_hotkeys.keys())
    if args.soundfont:
        if str(getattr(args, "audio_engine", "python")).strip().lower() != "python":
            print("Live MIDI input mode uses python audio engine; ignoring --audio-engine for live input.", file=sys.stderr)
        try:
            driver = _resolve_audio_driver(str(getattr(args, "audio_driver", "")))
            programs = {ch: default_program for ch in range(16)}
            live_audio = RealtimeFluidSynthPlayer(
                spans=[],
                soundfont_path=str(args.soundfont),
                audio_gain=audio_gain,
                driver=driver,
                output_target=str(getattr(args, "audio_output", "")),
                samplerate=int(getattr(args, "audio_samplerate", 0) or 0),
                period_size=int(getattr(args, "audio_period_size", 0) or 0),
                periods=int(getattr(args, "audio_periods", 0) or 0),
                polyphony=int(getattr(args, "audio_polyphony", 0) or 0),
                channel_programs=programs,
                control_events=[],
            )
            print(
                f"Audio: live python engine driver '{live_audio.driver_used or 'default'}'.",
                file=sys.stderr,
            )
            if instrument_hotkeys:
                print(
                    f"Live instrument hotkeys: {hotkey_display} "
                    "(press key to change instrument on-the-fly).",
                    file=sys.stderr,
                )
        except Exception as exc:
            stop_process(midi_proc)
            raise ValueError(f"Failed to initialize live audio: {exc}") from exc

    pressed_notes: Dict[Tuple[int, int], deque[Tuple[float, int, int]]] = defaultdict(deque)
    sounding_notes: Dict[Tuple[int, int], deque[Tuple[float, int, int]]] = defaultdict(deque)
    sustain_visual_tails: Dict[Tuple[int, int], deque[Tuple[float, int, int]]] = defaultdict(deque)
    sustain_down: Dict[int, bool] = defaultdict(bool)
    recording_active_notes: Dict[Tuple[int, int], deque[Tuple[float, int, int]]] = defaultdict(deque)
    recording_completed_spans: List[NoteSpan] = []
    visual_recent_spans: deque[NoteSpan] = deque()
    completed_pedal_spans: deque[NoteSpan] = deque()
    control_events: List[ControlEvent] = []
    pedal_invert = bool(getattr(args, "midi_input_invert_pedal", False))
    frame_dt = 1.0 / max(1, int(args.fps))
    last_msg = ""
    msg_until = 0.0

    black_rows, white_rows, kb_rows = keyboard_row_counts(args.kb_y_scale, args.kb_black_extra)
    play_rows = rows - kb_rows
    keep_window_sec = (play_rows / max(1.0, float(args.drop_speed))) + 1.5
    scroll_window_sec = play_rows / max(1e-6, float(args.drop_speed))
    pedal_decay_sec = max(0.0, float(getattr(args, "pedal_decay_seconds", 10.0)))
    pedal_visual_enabled = bool(getattr(args, "pedal_visual_dots", False)) and not bool(
        getattr(args, "disable_pedal", False)
    )
    title = f"LIVE {port_label}"

    def pedal_tail_expires_at(start_t: float, end_t: float) -> float:
        visible_emit_until = float(end_t)
        if pedal_decay_sec > 0.0:
            visible_emit_until = min(visible_emit_until, float(start_t) + pedal_decay_sec)
        return visible_emit_until + scroll_window_sec

    def offpiano_fill(width: int, row: int = 0, phase: int = 0) -> str:
        if width <= 0:
            return ""
        if use_offpiano_pattern:
            return ornamental_fill(width, row, phase, pattern_text=custom_offpiano_pattern)
        return " " * width


    def poll_midi_events(now_t: float) -> List[_LiveMidiEvent]:
        nonlocal midi_buf
        out: List[_LiveMidiEvent] = []
        while True:
            try:
                chunk = os.read(midi_fd, 4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                break
            if not chunk:
                break
            midi_buf += chunk
            while b"\n" in midi_buf:
                line_raw, midi_buf = midi_buf.split(b"\n", 1)
                line = line_raw.decode("utf-8", errors="ignore")
                ev = parse_aseqdump_event_line(line, now_t)
                if ev is not None:
                    out.append(ev)
        return out

    def process_live_event(ev: _LiveMidiEvent) -> None:
        def finish_one_sounding(channel: int, note: int, at_time: float) -> bool:
            key = (channel, note)
            q = sounding_notes.get(key)
            if not q:
                return False
            _start_t, _vel, _color_track = q.popleft()
            if not q:
                sounding_notes.pop(key, None)
            return True

        def finish_one_recorded(channel: int, note: int, at_time: float) -> bool:
            key = (channel, note)
            q = recording_active_notes.get(key)
            if not q:
                return False
            start_t, vel, color_track = q.popleft()
            if not q:
                recording_active_notes.pop(key, None)
            recording_completed_spans.append(
                NoteSpan(
                    note=note,
                    start=max(0.0, float(start_t)),
                    end=max(float(start_t) + 0.01, at_time),
                    velocity=max(1, min(127, int(vel))),
                    channel=channel,
                    track=max(0, int(color_track)),
                )
            )
            return True

        def release_sustain_channel(channel: int, at_time: float) -> None:
            for (ch, note), _q in list(sounding_notes.items()):
                if ch != channel:
                    continue
                key = (ch, note)
                pressed_len = len(pressed_notes.get(key, ()))
                while len(sounding_notes.get(key, ())) > pressed_len:
                    q_tail = sustain_visual_tails.get(key)
                    if q_tail:
                        tail_start, tail_vel, tail_track = q_tail.popleft()
                        completed_pedal_spans.append(
                            NoteSpan(
                                note=note,
                                start=max(0.0, float(tail_start)),
                                end=max(float(tail_start) + 0.01, at_time),
                                velocity=max(1, min(127, int(tail_vel))),
                                channel=ch,
                                track=max(0, int(tail_track)),
                            )
                        )
                        if not q_tail:
                            sustain_visual_tails.pop(key, None)
                    if not finish_one_sounding(ch, note, at_time):
                        break

        t_event = max(0.0, float(ev.time_sec))
        ch = max(0, min(15, int(ev.channel)))
        if ev.kind == "note_on":
            note = max(0, min(127, int(ev.note)))
            vel = max(1, min(127, int(ev.velocity)))
            pressed_notes[(ch, note)].append((t_event, vel, current_color_track))
            sounding_notes[(ch, note)].append((t_event, vel, current_color_track))
            recording_active_notes[(ch, note)].append((t_event, vel, current_color_track))
            if live_audio is not None:
                live_audio.note_on(ch, note, vel)
            return
        if ev.kind == "note_off":
            note = max(0, min(127, int(ev.note)))
            key = (ch, note)
            q_pressed = pressed_notes.get(key)
            released_voice: Optional[Tuple[float, int, int]] = None
            if q_pressed:
                start_t, vel, color_track = q_pressed.popleft()
                released_voice = (start_t, vel, color_track)
                vis_sp = NoteSpan(
                    note=note,
                    start=max(0.0, float(start_t)),
                    end=max(float(start_t) + 0.01, t_event),
                    velocity=max(1, min(127, int(vel))),
                    channel=ch,
                    track=max(0, int(color_track)),
                )
                visual_recent_spans.append(vis_sp)
                if not q_pressed:
                    pressed_notes.pop(key, None)
            finish_one_recorded(ch, note, t_event)
            if not sustain_down.get(ch, False):
                finish_one_sounding(ch, note, t_event)
            else:
                if released_voice is not None:
                    _start_t, rel_vel, rel_color_track = released_voice
                    sustain_visual_tails[key].append(
                        (t_event, max(1, min(127, int(rel_vel))), max(0, int(rel_color_track)))
                    )
            if live_audio is not None:
                live_audio.note_off(ch, note)
            return
        if ev.kind == "cc":
            cc = max(0, min(127, int(ev.controller)))
            val = max(0, min(127, int(ev.value)))
            if cc == 64 and pedal_invert:
                val = 127 - val
            if cc == 64 and bool(getattr(args, "disable_pedal", False)):
                return
            control_events.append(ControlEvent(time_sec=t_event, channel=ch, controller=cc, value=val, track=ch))
            if live_audio is not None:
                live_audio.cc(ch, cc, val)
            if cc == 64:
                was_down = bool(sustain_down.get(ch, False))
                now_down = val >= 64
                sustain_down[ch] = now_down
                if was_down and (not now_down):
                    release_sustain_channel(ch, t_event)

    def save_live_recording(now_t: float) -> Tuple[bool, str]:
        # Persist key press/release timing in note lengths; sustain is encoded with CC64 events.
        all_spans = _snapshot_live_spans(recording_completed_spans, recording_active_notes, now_t)
        save_controls = list(control_events)
        for ch, is_down in list(sustain_down.items()):
            if is_down:
                save_controls.append(
                    ControlEvent(time_sec=now_t, channel=ch, controller=64, value=0, track=ch)
                )
        if (not all_spans) and (not save_controls):
            return False, "Nothing captured yet."
        out_path = _default_live_recording_path(str(getattr(args, "midi_input_save_dir", "midis")))
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        save_pair = _clamp_instrument_pair(current_program[0], current_program[1])
        if valid_sf_pairs is not None and save_pair not in valid_sf_pairs:
            if valid_sf_pairs:
                if (0, 0) in valid_sf_pairs:
                    save_pair = (0, 0)
                else:
                    save_pair = sorted(valid_sf_pairs)[0]
            else:
                save_pair = (0, 0)
        used_channels = {int(sp.channel) for sp in all_spans}
        used_channels.update(int(ev.channel) for ev in save_controls)
        if not used_channels:
            used_channels = {0}
        channel_programs = {ch: save_pair for ch in sorted(used_channels)}
        write_demo_midi_file(
            out_path,
            all_spans,
            channel_programs=channel_programs,
            control_events=save_controls,
            insert_warmup_pulses=False,
        )
        return True, out_path

    _safe_stdout_write("\x1b[2J\x1b[H\x1b[?25l")

    stdin_fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(stdin_fd)
    old_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
    keybuf = ""
    start_perf = time.perf_counter()

    try:
        tty.setcbreak(stdin_fd)
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)

        while True:
            now_perf = time.perf_counter()
            play_t = max(0.0, now_perf - start_perf)

            for ev in poll_midi_events(play_t):
                process_live_event(ev)

            while True:
                ready, _, _ = select.select([stdin_fd], [], [], 0)
                if not ready:
                    break
                try:
                    data = os.read(stdin_fd, 128)
                except BlockingIOError:
                    break
                if not data:
                    break
                keybuf += data.decode("utf-8", errors="ignore")
            keys, keybuf = parse_keys(keybuf)
            for key in keys:
                if key in {"q", "Q"}:
                    return
                if key == "UP":
                    audio_gain = min(2.0, audio_gain + 0.05)
                    if live_audio is not None:
                        live_audio.set_gain(audio_gain)
                elif key == "DOWN":
                    audio_gain = max(0.0, audio_gain - 0.05)
                    if live_audio is not None:
                        live_audio.set_gain(audio_gain)
                elif key in {"s", "S"}:
                    ok, msg = save_live_recording(play_t)
                    if ok:
                        last_msg = f"Saved: {msg}"
                    else:
                        last_msg = msg
                    msg_until = now_perf + 2.5
                elif len(key) == 1:
                    hk = key.lower()
                    if hk in instrument_hotkeys:
                        pair = instrument_hotkeys[hk]
                        current_program = pair
                        current_color = instrument_colors.get(hk, default_live_hotkey_color(hk))
                        current_color_track = 1000 + ord(hk[0])
                        track_color_map[current_color_track] = current_color
                        if live_audio is not None:
                            for ch in range(16):
                                live_audio.set_channel_program(ch, pair[0], pair[1])
                        label = instrument_labels.get(hk, format_instrument_option(pair[0], pair[1]))
                        last_msg = f"Instrument: {label} (key {hk}) color={rgb_triplet_to_text(current_color)}"
                        msg_until = now_perf + 2.5

            while visual_recent_spans and visual_recent_spans[0].end < play_t - keep_window_sec:
                visual_recent_spans.popleft()
            while completed_pedal_spans and pedal_tail_expires_at(
                float(completed_pedal_spans[0].start), float(completed_pedal_spans[0].end)
            ) < play_t:
                completed_pedal_spans.popleft()
            render_spans = list(visual_recent_spans)
            pedal_render_spans: List[NoteSpan] = (
                list(completed_pedal_spans) if pedal_visual_enabled else []
            )
            for (ch, note), starts in pressed_notes.items():
                for start_t, vel, color_track in starts:
                    render_spans.append(
                        NoteSpan(
                            note=note,
                            start=start_t,
                            end=max(start_t + 0.01, play_t),
                            velocity=vel,
                            channel=ch,
                            track=max(0, int(color_track)),
                        )
                    )
            if pedal_visual_enabled:
                for (ch, note), tails in sustain_visual_tails.items():
                    for tail_start, tail_vel, tail_track in tails:
                        if pedal_tail_expires_at(float(tail_start), play_t) < play_t:
                            continue
                        pedal_render_spans.append(
                            NoteSpan(
                                note=note,
                                start=max(0.0, float(tail_start)),
                                end=max(float(tail_start) + 0.01, play_t),
                                velocity=max(1, min(127, int(tail_vel))),
                                channel=ch,
                                track=max(0, int(tail_track)),
                            )
                        )
            render_spans.sort(key=lambda s: s.start)
            pedal_render_spans.sort(key=lambda s: s.start)

            status_msg = last_msg if now_perf <= msg_until else ""
            live_notes = sum(len(v) for v in pressed_notes.values())
            sustain_channels = sum(1 for down in sustain_down.values() if down)
            inst_txt = f"{current_program[0]}:{current_program[1]}"
            col_txt = rgb_triplet_to_text(current_color)
            if getattr(args, "ascii_ui", False):
                run_title = (
                    f"{title} active={live_notes} pedal={sustain_channels} inst={inst_txt} color={col_txt} vol={audio_gain:.2f} "
                    f"(s save, up/down volume, q quit"
                )
            else:
                run_title = (
                    f"{title} active={live_notes} pedal={sustain_channels} inst={inst_txt} color={col_txt} vol={audio_gain:.2f} "
                    f"(s save, ↑/↓ volume, q quit"
                )
            if instrument_hotkeys:
                run_title += f", inst {hotkey_display}"
            run_title += ")"
            if status_msg:
                run_title = f"{run_title}  {status_msg}"

            if show_piano:
                frame = render_frame(
                    spans=render_spans,
                    pedal_spans=pedal_render_spans,
                    t=play_t,
                    note_lo=note_lo,
                    note_hi=note_hi,
                    rows=rows,
                    x_scale=effective_x_scale,
                    kb_y_scale=args.kb_y_scale,
                    kb_black_extra=args.kb_black_extra,
                    move_speed=args.drop_speed,
                    length_speed=args.drop_speed,
                    use_color=(not args.no_color),
                    title=run_title,
                    progress=0.0,
                    track_colors=track_color_map,
                    note_color_mode=str(getattr(args, "note_color_mode", "truecolor")),
                    pedal_color_mode=str(getattr(args, "pedal_color_mode", "truecolor")),
                    piano_color_mode=str(getattr(args, "piano_color_mode", "truecolor")),
                    pressed_key_color_mode=str(getattr(args, "pressed_key_color_mode", "split")),
                    dynamics_full_velocity=float(getattr(args, "dynamics_full_velocity", 96.0)),
                    pedal_opacity_scale=float(getattr(args, "pedal_opacity_scale", 0.5)),
                    pedal_decay_seconds=float(getattr(args, "pedal_decay_seconds", 10.0)),
                    terminal_bg_rgb=terminal_bg_rgb,
                    visual_glyphs=visual_glyphs,
                    note_direction="up",
                )
                raw_lines = frame.splitlines()
                frame_rows = len(raw_lines) + 2
                framed_cols = frame_cols + 2
            else:
                raw_lines = []
                frame_rows = 0
                framed_cols = 0

            term_w = max(1, term_size.columns)
            term_h = max(1, term_size.lines)
            inner_w = max(1, term_w - 2)
            inner_h = max(1, term_h - 2)

            if show_piano:
                left_pad = max(0, (inner_w - framed_cols) // 2)
                top_pad = max(0, (inner_h - frame_rows) // 2)
            else:
                left_pad = 0
                top_pad = 0

            screen_lines: List[str] = []
            if term_w >= 2:
                top_inner = offpiano_fill(term_w - 2, -1, 0)
                bottom_inner = offpiano_fill(term_w - 2, inner_h, 0)
                screen_lines.append("╔" + top_inner + "╗")
            else:
                screen_lines.append(offpiano_fill(term_w, -1, 0))

            for r in range(inner_h):
                if show_piano and top_pad <= r < (top_pad + frame_rows):
                    fr = r - top_pad
                    right_len = max(0, inner_w - left_pad - framed_cols)
                    full_row = offpiano_fill(inner_w, r, 0)
                    left = full_row[:left_pad]
                    right = full_row[left_pad + framed_cols : left_pad + framed_cols + right_len]
                    if fr == 0:
                        src = "╭" + ("─" * frame_cols) + "╮"
                    elif fr == frame_rows - 1:
                        src = "╰" + ("─" * frame_cols) + "╯"
                    else:
                        ln = raw_lines[fr - 1]
                        src = "│" + ln + "│"
                    inner = left + src + right
                else:
                    inner = offpiano_fill(inner_w, r, 0)
                if term_w >= 2:
                    screen_lines.append("║" + inner + "║")
                else:
                    screen_lines.append(inner[:term_w])

            if term_w >= 2:
                screen_lines.append("╚" + bottom_inner + "╝")
            else:
                screen_lines.append(offpiano_fill(term_w, inner_h, 0))

            _safe_stdout_write("\x1b[H" + "\n".join(screen_lines))

            sleep_for = frame_dt - (time.perf_counter() - now_perf)
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        try:
            fcntl.fcntl(midi_fd, fcntl.F_SETFL, midi_old_flags)
        except Exception:
            pass
        stop_process(midi_proc)
        if live_audio is not None:
            live_audio.close()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_flags)
        _safe_stdout_write("\x1b[0m\x1b[?25h\n")
