from __future__ import annotations

import argparse
import contextlib
import curses
import curses.textpad
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import termios
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from synthermia.audio import (
    RealtimeFluidSynthPlayer,
    _append_cli_audio_output_option,
    _apply_track_volume_overrides,
    _assign_audio_channels,
    _assign_control_channels,
    _build_track_program_map,
    _fluidsynth_env,
    _resolve_audio_driver,
    list_audio_output_targets,
    stop_process,
)
from synthermia.config import (
    ANSI16_TRACK_COLORS,
    ANSI16_AUTO_TRACK_COLORS,
    DEFAULT_VISUAL_GLYPHS,
    _clamp_track_volume,
    _format_track_volume_text,
    _normalize_saved_track_map,
    _normalize_saved_track_volume_map,
    _sanitize_visual_glyph,
    args_to_saved_config,
    clamp_playback_start,
    default_live_hotkey_color,
    default_ansi16_track_color,
    default_track_color,
    format_live_color_map,
    format_live_instrument_map,
    load_config_profiles,
    load_last_config,
    normalize_color_mode,
    nearest_ansi16_track_color,
    normalize_pressed_key_color_mode,
    normalize_note_range_text,
    parse_live_color_map,
    parse_live_instrument_map,
    resolve_midi_flip_bounds,
    resolve_visual_glyphs,
    save_config_profiles,
    save_last_config,
    scan_files_by_ext,
)
from synthermia.midi import (
    build_note_spans,
    load_playback_source,
    normalize_stuck_note_fix_mode,
    parse_midi,
    parse_midi_track_summaries,
)
from synthermia.models import ControlEvent, MidiTrackSummary, NoteSpan
from synthermia.render import parse_rgb_triplet, rgb_triplet_to_text
from synthermia.soundfont import (
    _build_soundfont_pair_map,
    _clamp_instrument_pair,
    _fmt_choice,
    _instrument_option_name,
    _match_instrument_by_name,
    _normalize_saved_soundfont_library,
    _soundfont_identity,
    _split_instrument_option,
    format_instrument_option,
    instrument_option_label,
    instrument_option_soundfont,
    parse_instrument_pair,
    parse_instrument_spec,
    scan_soundfont_presets,
)

_TaskResultT = TypeVar("_TaskResultT")


class BackgroundTaskCancelled(Exception):
    pass


def _is_picker_navigation_key(key: int) -> bool:
    return key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE)


@contextlib.contextmanager
def _suppress_stderr_fd() -> Any:
    try:
        sys.stderr.flush()
    except Exception:
        pass
    try:
        saved_fd = os.dup(2)
    except OSError:
        yield
        return
    devnull_fd = -1
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            os.dup2(saved_fd, 2)
        except OSError:
            pass
        try:
            os.close(saved_fd)
        except OSError:
            pass
        if devnull_fd >= 0:
            try:
                os.close(devnull_fd)
            except OSError:
                pass


@contextlib.contextmanager
def _disable_terminal_flow_control() -> Any:
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
    except Exception:
        yield
        return
    new_attrs = [list(item) if isinstance(item, list) else item for item in attrs]
    try:
        new_attrs[0] &= ~termios.IXON
        if hasattr(termios, "IXOFF"):
            new_attrs[0] &= ~termios.IXOFF
        termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass

def _edit_text_popup(stdscr: "curses._CursesWindow", label: str, value: str) -> str:
    h, w = stdscr.getmaxyx()
    # Keep this wide enough for real filesystem paths.
    desired_w = max(72, len(label) + 24, len(value) + 12)
    box_w = max(20, min(desired_w, max(20, w - 4)))
    box_h = max(5, min(7, max(5, h - 2)))
    top = max(1, (h - box_h) // 2)
    left = max(1, (w - box_w) // 2)
    win = stdscr.derwin(box_h, box_w, top, left)
    win.erase()
    win.box()
    title = " Edit Value "
    try:
        win.addstr(0, max(2, (box_w - len(title)) // 2), title, curses.A_BOLD)
    except Exception:
        pass
    cur_line = f"Current: {value}"
    win.addstr(2, 2, cur_line[: max(1, box_w - 4)])
    prompt = f"{label}: "
    win.addstr(4, 2, prompt[: max(1, box_w - 4)])
    win.refresh()
    input_x = min(2 + len(prompt), max(2, box_w - 3))
    max_len = max(1, box_w - input_x - 2)
    win.move(4, input_x)
    curses.echo()
    curses.curs_set(1)
    try:
        raw = win.getstr(4, input_x, max_len)
        txt = raw.decode("utf-8", errors="ignore").strip()
        return txt
    finally:
        curses.noecho()
        curses.curs_set(0)


def _draw_progress_popup(
    stdscr: "curses._CursesWindow",
    title: str,
    message: str,
    *,
    phase: int = 0,
    detail: Optional[str] = None,
) -> None:
    lines = [ln for ln in str(message).splitlines() if ln] or [""]
    if detail:
        lines.append(str(detail))
    h, w = stdscr.getmaxyx()
    content_w = max(len(title), *(len(ln) for ln in lines), len("Working..."))
    box_w = max(34, min(content_w + 4, max(20, w - 4)))
    box_h = max(7, min(len(lines) + 5, max(7, h - 2)))
    top = max(1, (h - box_h) // 2)
    left = max(1, (w - box_w) // 2)
    win = stdscr.derwin(box_h, box_w, top, left)
    win.erase()
    win.box()
    try:
        win.addstr(0, max(2, (box_w - len(title)) // 2), title[: max(1, box_w - 4)], curses.A_BOLD)
    except Exception:
        pass
    body_h = max(1, box_h - 4)
    for i in range(min(body_h, len(lines))):
        win.addstr(1 + i, 2, lines[i][: max(1, box_w - 4)])
    spinner = "|/-\\"
    spinner_ch = spinner[phase % len(spinner)]
    bar_w = max(8, box_w - 8)
    filled = (phase % max(2, bar_w - 1)) + 1
    bar = "[" + ("=" * max(0, min(bar_w - 2, filled))) + spinner_ch
    bar = (bar + (" " * bar_w))[:bar_w] + "]"
    win.addstr(box_h - 2, 2, f"{bar} Working..."[: max(1, box_w - 4)], curses.A_DIM)
    win.refresh()


def run_background_task_popup(
    title: str,
    message: str,
    task: Callable[[], _TaskResultT],
    *,
    detail_prefix: str = "",
    poll_interval: float = 0.12,
    progress_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    allow_cancel: bool = False,
    cancel_task: Optional[Callable[[], None]] = None,
    stdscr: Optional["curses._CursesWindow"] = None,
) -> _TaskResultT:
    state: Dict[str, Any] = {
        "done": False,
        "value": None,
        "error": None,
        "cancel_requested": False,
    }

    def worker() -> None:
        try:
            state["value"] = task()
        except BaseException as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    thread = threading.Thread(target=worker, name="synthermia-background-task", daemon=True)
    thread.start()

    def _compact_count(value: int) -> str:
        amount = max(0, int(value))
        if amount >= 1_000_000_000:
            return f"{amount / 1_000_000_000:.2f}B"
        if amount >= 1_000_000:
            return f"{amount / 1_000_000:.2f}M"
        if amount >= 1_000:
            return f"{amount / 1_000:.1f}k"
        return str(amount)

    def _format_detail(started_at: float) -> str:
        elapsed = time.perf_counter() - started_at
        prefix = str(detail_prefix or "").strip()
        detail_parts: List[str] = []
        if prefix:
            detail_parts.append(prefix)
        if bool(state["cancel_requested"]):
            detail_parts.append("Cancelling...")
        if callable(progress_fn):
            try:
                snapshot = dict(progress_fn() or {})
            except Exception:
                snapshot = {}
            phase = str(snapshot.get("phase", "")).strip()
            if phase:
                detail_parts.append(f"Phase: {phase}")
            tracks_done = int(snapshot.get("tracks_done", 0) or 0)
            tracks_total = int(snapshot.get("tracks_total", 0) or 0)
            if tracks_total > 0:
                detail_parts.append(f"Tracks: {tracks_done}/{tracks_total}")
            events_processed = int(snapshot.get("events_processed", 0) or 0)
            events_total = int(snapshot.get("events_total", 0) or 0)
            if events_total > 0:
                detail_parts.append(
                    f"Events: {_compact_count(events_processed)}/{_compact_count(events_total)}"
                )
            seconds_ready = float(snapshot.get("seconds_ready", 0.0) or 0.0)
            if seconds_ready > 0.0:
                detail_parts.append(f"Ready: {seconds_ready:.1f}s")
            notes_ready = int(snapshot.get("notes_ready", 0) or 0)
            if notes_ready > 0:
                detail_parts.append(f"Notes: {_compact_count(notes_ready)}")
        detail_parts.append(f"Elapsed: {elapsed:.1f}s")
        return "  ".join(detail_parts)

    def tui(stdscr: "curses._CursesWindow") -> None:
        curses.set_escdelay(50)
        curses.curs_set(0)
        try:
            stdscr.nodelay(True)
            stdscr.timeout(max(10, int(max(0.02, poll_interval) * 1000)))
        except Exception:
            pass
        phase = 0
        started_at = time.perf_counter()
        while not bool(state["done"]):
            _draw_progress_popup(
                stdscr,
                title,
                message,
                phase=phase,
                detail=_format_detail(started_at),
            )
            phase += 1
            try:
                key = stdscr.getch()
            except Exception:
                time.sleep(poll_interval)
                continue
            if (
                allow_cancel
                and not bool(state["cancel_requested"])
                and key in (27, ord("q"), ord("Q"))
            ):
                state["cancel_requested"] = True
                if callable(cancel_task):
                    try:
                        cancel_task()
                    except Exception:
                        pass
        _draw_progress_popup(
            stdscr,
            title,
            message,
            phase=phase,
            detail=_format_detail(started_at),
        )

    if stdscr is not None:
        # Already inside an active curses session: render the popup directly on
        # the live window. Using curses.wrapper() here would tear the outer
        # session down on exit (keypad(False)/echo()/nocbreak()), which leaves
        # the caller's screen with echo on and arrow keys undecoded (^[[A).
        prev_curs = None
        try:
            prev_curs = curses.curs_set(0)
        except Exception:
            prev_curs = None
        try:
            tui(stdscr)
        finally:
            # Restore the blocking input mode the outer menu loop expects.
            try:
                stdscr.nodelay(False)
                stdscr.timeout(-1)
            except Exception:
                pass
            if prev_curs is not None:
                try:
                    curses.curs_set(prev_curs)
                except Exception:
                    pass
    else:
        curses.wrapper(tui)
    thread.join()
    if bool(state["cancel_requested"]):
        if state["error"] is not None and "cancel" not in str(state["error"]).lower():
            raise state["error"]
        raise BackgroundTaskCancelled()
    if state["error"] is not None:
        raise state["error"]
    return state["value"]


def _message_popup(stdscr: "curses._CursesWindow", title: str, message: str) -> None:
    lines = [ln for ln in str(message).splitlines() if ln] or [""]
    h, w = stdscr.getmaxyx()
    content_w = max(len(title), *(len(ln) for ln in lines), len("Press any key"))
    box_w = max(20, min(content_w + 4, max(12, w - 2)))
    box_h = max(5, min(len(lines) + 4, max(5, h - 2)))
    top = max(1, (h - box_h) // 2)
    left = max(1, (w - box_w) // 2)
    win = stdscr.derwin(box_h, box_w, top, left)
    win.erase()
    win.box()
    try:
        win.addstr(0, max(2, (box_w - len(title)) // 2), title[: max(1, box_w - 4)], curses.A_BOLD)
    except Exception:
        pass
    body_h = max(1, box_h - 3)
    for i in range(min(body_h, len(lines))):
        win.addstr(1 + i, 2, lines[i][: max(1, box_w - 4)])
    win.addstr(box_h - 2, 2, "Press any key"[: max(1, box_w - 4)], curses.A_DIM)
    win.refresh()
    win.getch()

def run_config_screen(args: argparse.Namespace) -> argparse.Namespace:
    from synthermia.playback import create_demo_midi_temp, make_instrument_preview_spans

    saved = load_last_config()
    profile_entries = load_config_profiles()
    saved_selected_profile = str(saved.get("selected_profile_name", "")).strip()
    profile_selection = -1
    if saved_selected_profile:
        for idx, item in enumerate(profile_entries):
            if str(item.get("name", "")).strip() == saved_selected_profile:
                profile_selection = idx
                break
    if profile_selection < 0 and profile_entries:
        profile_selection = 0
    saved_track_instruments = _normalize_saved_track_map(saved.get("track_instruments", {}))
    saved_track_colors = _normalize_saved_track_map(saved.get("track_colors", {}))
    saved_track_volumes = _normalize_saved_track_volume_map(
        saved.get("track_volumes", getattr(args, "track_volumes", {}))
    )
    saved_soundfont_primary = str(saved.get("soundfont", getattr(args, "soundfont", "")))
    saved_soundfonts = _normalize_saved_soundfont_library(
        saved.get("soundfonts", getattr(args, "soundfonts", [])),
        primary_soundfont=saved_soundfont_primary,
    )
    if (not saved_soundfont_primary) and saved_soundfonts:
        saved_soundfont_primary = str(saved_soundfonts[0]).strip()
    saved_instr_bank = int(saved.get("instrument_bank", getattr(args, "instrument_bank", 0)))
    saved_instr_prog = int(saved.get("instrument_program", getattr(args, "instrument_program", 0)))
    saved_instr_text = str(saved.get("instrument", f"{saved_instr_bank}:{saved_instr_prog}"))
    saved_instr_bank, saved_instr_prog = parse_instrument_pair(saved_instr_text, (saved_instr_bank, saved_instr_prog))
    saved_scroll = float(saved.get("scroll", 1.0))
    if "drop_speed" in saved and "scroll" in saved:
        initial_drop_speed = float(saved.get("drop_speed", args.drop_speed)) * saved_scroll
    elif "drop_speed" in saved:
        initial_drop_speed = float(saved.get("drop_speed", args.drop_speed))
    elif "scroll" in saved:
        initial_drop_speed = saved_scroll
    else:
        initial_drop_speed = float(args.drop_speed)
    saved_note_color_mode = normalize_color_mode(
        str(saved.get("note_color_mode", getattr(args, "note_color_mode", "truecolor")))
    )

    state: Dict[str, object] = {
        "midi_input_mode": bool(saved.get("midi_input_mode", getattr(args, "midi_input_mode", False))),
        "midi_input_port": str(saved.get("midi_input_port", getattr(args, "midi_input_port", ""))),
        "midi_input_save_dir": str(
            saved.get("midi_input_save_dir", getattr(args, "midi_input_save_dir", "midis"))
        ),
        "midi_input_invert_pedal": bool(
            saved.get("midi_input_invert_pedal", getattr(args, "midi_input_invert_pedal", False))
        ),
        "midi_input_instrument_map": str(
            saved.get("midi_input_instrument_map", getattr(args, "midi_input_instrument_map", ""))
        ),
        "midi_input_color_map": str(
            saved.get("midi_input_color_map", getattr(args, "midi_input_color_map", ""))
        ),
        "midi": str(saved.get("midi", args.midi)),
        "soundfont": saved_soundfont_primary,
        "soundfonts": list(saved_soundfonts),
        "audio_engine": str(saved.get("audio_engine", args.audio_engine)),
        "playback_engine": str(saved.get("playback_engine", getattr(args, "playback_engine", "auto"))),
        "fluidsynth_bin": str(saved.get("fluidsynth_bin", args.fluidsynth_bin)),
        "audio_driver": str(saved.get("audio_driver", args.audio_driver)),
        "audio_output": str(saved.get("audio_output", getattr(args, "audio_output", ""))),
        "audio_gain": float(saved.get("audio_gain", args.audio_gain)),
        "audio_period_size": int(saved.get("audio_period_size", args.audio_period_size)),
        "audio_periods": int(saved.get("audio_periods", args.audio_periods)),
        "audio_samplerate": int(saved.get("audio_samplerate", args.audio_samplerate)),
        "audio_polyphony": int(saved.get("audio_polyphony", getattr(args, "audio_polyphony", 2048))),
        "instrument": saved_instr_text if saved_instr_text.strip() else format_instrument_option(saved_instr_bank, saved_instr_prog),
        "track_instruments": dict(saved_track_instruments),
        "track_colors": dict(saved_track_colors),
        "track_volumes": dict(saved_track_volumes),
        "track_customization": "",
        "fps": int(saved.get("fps", args.fps)),
        "rows": int(saved.get("rows", args.rows)),
        "x_scale": int(saved.get("x_scale", args.x_scale)),
        "kb_y_scale": int(saved.get("kb_y_scale", args.kb_y_scale)),
        "kb_black_extra": int(saved.get("kb_black_extra", args.kb_black_extra)),
        "dynamics_full_velocity": float(
            saved.get("dynamics_full_velocity", getattr(args, "dynamics_full_velocity", 96.0))
        ),
        "terminal_bg_color": str(saved.get("terminal_bg_color", getattr(args, "terminal_bg_color", ""))),
        "note_color_mode": saved_note_color_mode,
        "pedal_color_mode": normalize_color_mode(
            str(saved.get("pedal_color_mode", saved_note_color_mode))
        ),
        "piano_color_mode": normalize_color_mode(
            str(saved.get("piano_color_mode", getattr(args, "piano_color_mode", "truecolor")))
        ),
        "track_color_mode": normalize_color_mode(
            str(saved.get("track_color_mode", getattr(args, "track_color_mode", "truecolor")))
        ),
        "color_rendering": "",
        "glyph_note_white": _sanitize_visual_glyph(
            saved.get("glyph_note_white", getattr(args, "glyph_note_white", DEFAULT_VISUAL_GLYPHS["glyph_note_white"])),
            DEFAULT_VISUAL_GLYPHS["glyph_note_white"],
        ),
        "glyph_note_pedal_white": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_pedal_white",
                saved.get("glyph_note_pedal", getattr(args, "glyph_note_pedal_white", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"])),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"],
        ),
        "glyph_note_pedal_black_left": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_pedal_black_left",
                saved.get(
                    "glyph_note_pedal",
                    getattr(args, "glyph_note_pedal_black_left", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"]),
                ),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"],
        ),
        "glyph_note_pedal_black_mid": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_pedal_black_mid",
                saved.get(
                    "glyph_note_pedal",
                    getattr(args, "glyph_note_pedal_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"]),
                ),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"],
        ),
        "glyph_note_pedal_black_right": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_pedal_black_right",
                saved.get(
                    "glyph_note_pedal",
                    getattr(args, "glyph_note_pedal_black_right", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"]),
                ),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"],
        ),
        "glyph_note_black_left": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_black_left",
                getattr(args, "glyph_note_black_left", DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"],
        ),
        "glyph_note_black_mid": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_black_mid",
                getattr(args, "glyph_note_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"],
        ),
        "glyph_note_black_right": _sanitize_visual_glyph(
            saved.get(
                "glyph_note_black_right",
                getattr(args, "glyph_note_black_right", DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"],
        ),
        "glyph_piano_white": _sanitize_visual_glyph(
            saved.get("glyph_piano_white", getattr(args, "glyph_piano_white", DEFAULT_VISUAL_GLYPHS["glyph_piano_white"])),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_white"],
        ),
        "glyph_piano_black_left": _sanitize_visual_glyph(
            saved.get(
                "glyph_piano_black_left",
                getattr(args, "glyph_piano_black_left", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"],
        ),
        "glyph_piano_black_mid": _sanitize_visual_glyph(
            saved.get(
                "glyph_piano_black_mid",
                getattr(args, "glyph_piano_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"],
        ),
        "glyph_piano_black_right": _sanitize_visual_glyph(
            saved.get(
                "glyph_piano_black_right",
                getattr(args, "glyph_piano_black_right", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"]),
            ),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"],
        ),
        "glyph_editor": "",
        "pedal_visual_dots": bool(saved.get("pedal_visual_dots", getattr(args, "pedal_visual_dots", False))),
        "disable_pedal": bool(saved.get("disable_pedal", getattr(args, "disable_pedal", False))),
        "pedal_opacity_scale": float(saved.get("pedal_opacity_scale", getattr(args, "pedal_opacity_scale", 0.5))),
        "pedal_decay_seconds": float(saved.get("pedal_decay_seconds", getattr(args, "pedal_decay_seconds", 10.0))),
        "measure_bars": bool(saved.get("measure_bars", getattr(args, "measure_bars", False))),
        "drop_speed": float(initial_drop_speed),
        "speed": float(saved.get("speed", args.speed)),
        "midi_flip": bool(saved.get("midi_flip", getattr(args, "midi_flip", False))),
        "stuck_note_fix": normalize_stuck_note_fix_mode(
            str(saved.get("stuck_note_fix", getattr(args, "stuck_note_fix", "auto")))
        ),
        "long_note_threshold": float(saved.get("long_note_threshold", getattr(args, "long_note_threshold", 0.0))),
        "long_note_duration": float(saved.get("long_note_duration", getattr(args, "long_note_duration", 10.0))),
        "start": float(saved.get("start", args.start)),
        "range": normalize_note_range_text(str(saved.get("range", args.range))),
        "hide_piano": bool(saved.get("hide_piano", getattr(args, "hide_piano", False))),
        "no_color": bool(saved.get("no_color", args.no_color)),
        "no_offpiano_pattern": bool(saved.get("no_offpiano_pattern", args.no_offpiano_pattern)),
        "offpiano_pattern": str(saved.get("offpiano_pattern", getattr(args, "offpiano_pattern", ""))),
        "pressed_key_color_mode": normalize_pressed_key_color_mode(
            str(saved.get("pressed_key_color_mode", getattr(args, "pressed_key_color_mode", "split")))
        ),
    }

    fields: List[Dict[str, object]] = [
        {"key": "profile_load", "label": "Load settings profile", "type": "profile_selector", "section": "Profile"},
        {"key": "profile_save", "label": "Save this profile", "type": "profile_save_button", "section": "Profile"},
        {"key": "profile_delete", "label": "Delete this profile", "type": "profile_delete_button", "section": "Profile"},
        {"key": "profile_rename", "label": "Rename this profile", "type": "profile_rename_button", "section": "Profile"},
        {"key": "midi_input_mode", "label": "Live MIDI Input", "type": "bool", "section": "Input"},
        {"key": "midi_input_port", "label": "MIDI Input Port", "type": "text", "section": "Input"},
        {"key": "midi_input_save_dir", "label": "Live Save Dir", "type": "text", "section": "Input"},
        {"key": "midi_input_invert_pedal", "label": "Invert Pedal (Live)", "type": "bool", "section": "Input"},
        {
            "key": "midi_input_instrument_map",
            "label": "Live Instrument Keys",
            "type": "live_instrument_map_browser",
            "section": "Input",
        },
        {"key": "midi", "label": "MIDI File", "type": "midi_browser", "section": "Input"},
        {"key": "soundfonts", "label": "SoundFonts", "type": "soundfont_manager_browser", "section": "Input"},
        {"key": "instrument", "label": "Instrument", "type": "instrument_browser", "options": [format_instrument_option(saved_instr_bank, saved_instr_prog)], "section": "Input"},
        {"key": "track_customization", "label": "Track Customization", "type": "track_customization_browser", "section": "Input"},
        {"key": "audio_engine", "label": "Audio Engine", "type": "choice", "options": ["python", "cli"], "section": "Audio"},
        {"key": "fluidsynth_bin", "label": "FluidSynth Bin", "type": "text", "section": "Audio"},
        {"key": "audio_driver", "label": "Audio Driver", "type": "text", "section": "Audio"},
        {"key": "audio_output", "label": "Audio Output", "type": "audio_output_browser", "section": "Audio"},
        {"key": "audio_gain", "label": "Audio Gain", "type": "float", "step": 0.05, "min": 0.0, "section": "Audio"},
        {"key": "audio_period_size", "label": "Audio Period Size", "type": "int", "step": 64, "min": 0, "section": "Audio"},
        {"key": "audio_periods", "label": "Audio Periods", "type": "int", "step": 1, "min": 0, "section": "Audio"},
        {"key": "audio_samplerate", "label": "Audio Samplerate", "type": "int", "step": 1000, "min": 0, "section": "Audio"},
        {"key": "audio_polyphony", "label": "Audio Polyphony", "type": "int", "step": 256, "min": 0, "section": "Audio"},
        {"key": "playback_engine", "label": "Playback Engine", "type": "choice", "options": ["auto", "python", "packed"], "section": "Playback"},
        {"key": "fps", "label": "FPS", "type": "int", "step": 1, "min": 1, "section": "Playback"},
        {"key": "rows", "label": "Rows", "type": "int", "step": 1, "min": 4, "section": "Playback"},
        {"key": "drop_speed", "label": "Drop Speed", "type": "float", "step": 0.5, "min": 0.1, "section": "Playback"},
        {"key": "speed", "label": "Playback Speed", "type": "float", "step": 0.1, "min": 0.1, "section": "Playback"},
        {"key": "midi_flip", "label": "MIDIflip", "type": "bool", "section": "Playback"},
        {
            "key": "stuck_note_fix",
            "label": "Stuck Note Fix",
            "type": "choice",
            "options": ["auto", "off", "lifo"],
            "section": "Playback",
        },
        {
            "key": "long_note_threshold",
            "label": "Long Note Threshold (s; 0=Off)",
            "type": "float",
            "step": 1.0,
            "min": 0.0,
            "section": "Playback",
        },
        {
            "key": "long_note_duration",
            "label": "Long Note Trim To (s)",
            "type": "float",
            "step": 1.0,
            "min": 0.0,
            "section": "Playback",
        },
        {"key": "start", "label": "Start Time (s)", "type": "float", "step": 0.5, "min": 0.0, "section": "Playback"},
        {"key": "range", "label": "Range (min:max)", "type": "text", "section": "Playback"},
        {"key": "x_scale", "label": "Horizontal Scale", "type": "int", "step": 1, "min": 1, "section": "Visual"},
        {"key": "kb_y_scale", "label": "Keyboard Y Scale", "type": "int", "step": 1, "min": 1, "section": "Visual"},
        {"key": "kb_black_extra", "label": "Black Extra Rows", "type": "int", "step": 1, "min": 0, "section": "Visual"},
        {
            "key": "dynamics_full_velocity",
            "label": "Dynamics Full Velocity",
            "type": "float",
            "step": 1.0,
            "min": 1.0,
            "section": "Visual",
        },
        {"key": "color_rendering", "label": "Color Rendering", "type": "color_rendering_editor", "section": "Visual"},
        {"key": "glyph_editor", "label": "Glyphs", "type": "glyph_editor", "section": "Visual"},
        {"key": "pedal_visual_dots", "label": "Pedal Hold Dots", "type": "bool", "section": "Visual"},
        {"key": "disable_pedal", "label": "Disable Pedal Playback", "type": "bool", "section": "Playback"},
        {
            "key": "pedal_opacity_scale",
            "label": "Pedal Opacity Scale",
            "type": "float",
            "step": 0.05,
            "min": 0.0,
            "section": "Visual",
        },
        {
            "key": "pedal_decay_seconds",
            "label": "Pedal Decay (s)",
            "type": "float",
            "step": 0.5,
            "min": 0.0,
            "section": "Visual",
        },
        {"key": "measure_bars", "label": "Measure Bars (_)", "type": "bool", "section": "Visual"},
        {
            "key": "pressed_key_color_mode",
            "label": "Pressed Key Color",
            "type": "choice",
            "options": ["split", "weighted", "off"],
            "section": "Visual",
        },
        {"key": "hide_piano", "label": "Hide Piano", "type": "bool", "section": "Visual"},
        {"key": "no_color", "label": "No Color", "type": "bool", "section": "Visual"},
        {"key": "no_offpiano_pattern", "label": "Hide Off-Piano Pattern", "type": "bool", "section": "Visual"},
        {"key": "offpiano_pattern", "label": "Background Pattern", "type": "offpiano_pattern_editor", "section": "Visual"},
        {"key": "__export_mp3__", "label": "[ Export MP3 ]", "type": "button", "section": "Action"},
        {"key": "__ok__", "label": "[  OK  ]", "type": "button", "section": "Action"},
    ]

    def profile_name_at(index: int) -> str:
        if 0 <= index < len(profile_entries):
            return str(profile_entries[index].get("name", "")).strip()
        return ""

    def unique_profile_name(base_name: str, skip_index: int = -1) -> str:
        base = str(base_name or "").strip() or "Profile"
        existing = {
            str(item.get("name", "")).strip().lower()
            for idx, item in enumerate(profile_entries)
            if idx != skip_index
        }
        if base.lower() not in existing:
            return base
        suffix = 2
        while True:
            candidate = f"{base} {suffix}"
            if candidate.lower() not in existing:
                return candidate
            suffix += 1

    def next_default_profile_name() -> str:
        n = 1
        existing = {str(item.get("name", "")).strip().lower() for item in profile_entries}
        while True:
            candidate = f"Profile {n}"
            if candidate.lower() not in existing:
                return candidate
            n += 1

    def build_saved_config_snapshot() -> Dict[str, object]:
        clamp_playback_fields()
        inst_bank, inst_prog = parse_instrument_pair(str(state["instrument"]))
        return {
            "selected_profile_name": profile_name_at(profile_selection),
            "midi_input_mode": bool(state["midi_input_mode"]),
            "midi_input_port": str(state["midi_input_port"]),
            "midi_input_save_dir": str(state["midi_input_save_dir"]),
            "midi_input_invert_pedal": bool(state["midi_input_invert_pedal"]),
            "midi_input_instrument_map": str(state["midi_input_instrument_map"]),
            "midi_input_color_map": str(state["midi_input_color_map"]),
            "midi": str(state["midi"]),
            "soundfont": str(state["soundfont"]),
            "soundfonts": list(
                _normalize_saved_soundfont_library(
                    state.get("soundfonts", []), primary_soundfont=str(state["soundfont"])
                )
            ),
            "instrument": str(state["instrument"]),
            "instrument_bank": inst_bank,
            "instrument_program": inst_prog,
            "track_instruments": dict(state["track_instruments"]),
            "track_colors": dict(state["track_colors"]),
            "track_volumes": dict(state["track_volumes"]),
            "audio_engine": str(state["audio_engine"]),
            "playback_engine": str(state["playback_engine"]),
            "fluidsynth_bin": str(state["fluidsynth_bin"]),
            "audio_driver": str(state["audio_driver"]),
            "audio_output": str(state["audio_output"]),
            "audio_gain": float(state["audio_gain"]),
            "audio_period_size": int(state["audio_period_size"]),
            "audio_periods": int(state["audio_periods"]),
            "audio_samplerate": int(state["audio_samplerate"]),
            "audio_polyphony": int(state["audio_polyphony"]),
            "fps": int(state["fps"]),
            "rows": int(state["rows"]),
            "x_scale": int(state["x_scale"]),
            "kb_y_scale": int(state["kb_y_scale"]),
            "kb_black_extra": int(state["kb_black_extra"]),
            "dynamics_full_velocity": float(state["dynamics_full_velocity"]),
            "terminal_bg_color": str(state["terminal_bg_color"]).strip(),
            "note_color_mode": normalize_color_mode(str(state["note_color_mode"])),
            "pedal_color_mode": normalize_color_mode(str(state["pedal_color_mode"])),
            "piano_color_mode": normalize_color_mode(str(state["piano_color_mode"])),
            "track_color_mode": normalize_color_mode(str(state["track_color_mode"])),
            "glyph_note_white": _sanitize_visual_glyph(state["glyph_note_white"], DEFAULT_VISUAL_GLYPHS["glyph_note_white"]),
            "glyph_note_pedal_white": _sanitize_visual_glyph(
                state["glyph_note_pedal_white"], DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"]
            ),
            "glyph_note_pedal_black_left": _sanitize_visual_glyph(
                state["glyph_note_pedal_black_left"], DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"]
            ),
            "glyph_note_pedal_black_mid": _sanitize_visual_glyph(
                state["glyph_note_pedal_black_mid"], DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"]
            ),
            "glyph_note_pedal_black_right": _sanitize_visual_glyph(
                state["glyph_note_pedal_black_right"], DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"]
            ),
            "glyph_note_black_left": _sanitize_visual_glyph(
                state["glyph_note_black_left"], DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"]
            ),
            "glyph_note_black_mid": _sanitize_visual_glyph(
                state["glyph_note_black_mid"], DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"]
            ),
            "glyph_note_black_right": _sanitize_visual_glyph(
                state["glyph_note_black_right"], DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"]
            ),
            "glyph_piano_white": _sanitize_visual_glyph(
                state["glyph_piano_white"], DEFAULT_VISUAL_GLYPHS["glyph_piano_white"]
            ),
            "glyph_piano_black_left": _sanitize_visual_glyph(
                state["glyph_piano_black_left"], DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"]
            ),
            "glyph_piano_black_mid": _sanitize_visual_glyph(
                state["glyph_piano_black_mid"], DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"]
            ),
            "glyph_piano_black_right": _sanitize_visual_glyph(
                state["glyph_piano_black_right"], DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"]
            ),
            "pedal_visual_dots": bool(state["pedal_visual_dots"]),
            "disable_pedal": bool(state["disable_pedal"]),
            "pedal_opacity_scale": max(0.0, min(1.0, float(state["pedal_opacity_scale"]))),
            "pedal_decay_seconds": max(0.0, float(state["pedal_decay_seconds"])),
            "measure_bars": bool(state["measure_bars"]),
            "drop_speed": float(state["drop_speed"]),
            "speed": float(state["speed"]),
            "midi_flip": bool(state["midi_flip"]),
            "stuck_note_fix": normalize_stuck_note_fix_mode(str(state["stuck_note_fix"])),
            "long_note_threshold": max(0.0, float(state["long_note_threshold"])),
            "long_note_duration": max(0.0, float(state["long_note_duration"])),
            "start": float(state["start"]),
            "range": str(state["range"]),
            "hide_piano": bool(state["hide_piano"]),
            "no_color": bool(state["no_color"]),
            "no_offpiano_pattern": bool(state["no_offpiano_pattern"]),
            "offpiano_pattern": str(state["offpiano_pattern"]),
            "pressed_key_color_mode": normalize_pressed_key_color_mode(str(state["pressed_key_color_mode"])),
        }

    def persist_state() -> None:
        save_last_config(build_saved_config_snapshot())

    def apply_saved_config_snapshot(snapshot: Dict[str, object]) -> None:
        nonlocal profile_selection
        if not isinstance(snapshot, dict):
            return
        for key in list(state.keys()):
            if key in snapshot:
                state[key] = snapshot[key]
        state["track_instruments"] = dict(_normalize_saved_track_map(state.get("track_instruments", {})))
        state["track_colors"] = dict(_normalize_saved_track_map(state.get("track_colors", {})))
        state["track_volumes"] = dict(_normalize_saved_track_volume_map(state.get("track_volumes", {})))
        invalidate_track_analysis()
        invalidate_instrument_options()
        refresh_midi_duration()
        selected_name = str(snapshot.get("selected_profile_name", "")).strip()
        if selected_name:
            for idx, item in enumerate(profile_entries):
                if str(item.get("name", "")).strip() == selected_name:
                    profile_selection = idx
                    break

    def choose_profile_name(stdscr: "curses._CursesWindow", current_index: int) -> Optional[int]:
        selected = max(0, min(current_index, max(0, len(profile_entries) - 1)))
        offset = 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Profile Picker "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "Up/Down move  Enter select  PgUp/PgDn page  Esc cancel"
            stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
            top = 4
            list_h = max(1, h - top - 2)
            if not profile_entries:
                stdscr.addstr(top, 2, "(no saved profiles)"[: max(1, w - 4)], curses.A_DIM)
            else:
                if selected < offset:
                    offset = selected
                if selected >= offset + list_h:
                    offset = selected - list_h + 1
                for row in range(list_h):
                    idx = offset + row
                    if idx >= len(profile_entries):
                        break
                    name = str(profile_entries[idx].get("name", "")).strip() or f"Profile {idx + 1}"
                    attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
                    stdscr.addstr(top + row, 2, name[: max(1, w - 4)], attr)
            stdscr.refresh()
            k = stdscr.getch()
            if k in (27, ord("q"), ord("Q")):
                return None
            if k == curses.KEY_UP and profile_entries:
                selected = (selected - 1) % len(profile_entries)
                continue
            if k == curses.KEY_DOWN and profile_entries:
                selected = (selected + 1) % len(profile_entries)
                continue
            if k == curses.KEY_PPAGE:
                selected = max(0, selected - list_h)
                continue
            if k == curses.KEY_NPAGE:
                selected = min(max(0, len(profile_entries) - 1), selected + list_h)
                continue
            if k in (10, 13, curses.KEY_ENTER):
                if not profile_entries:
                    return None
                return selected

    track_entries: List[MidiTrackSummary] = []
    midi_duration_sec = 0.0
    track_entries_loaded = False
    midi_duration_known = False
    instrument_options_loaded = False
    instrument_options_signature: Tuple[Tuple[str, int, int, int], ...] = ()

    def invalidate_track_analysis() -> None:
        nonlocal track_entries, midi_duration_sec, track_entries_loaded, midi_duration_known
        track_entries = []
        midi_duration_sec = 0.0
        track_entries_loaded = False
        midi_duration_known = False

    def invalidate_instrument_options() -> None:
        nonlocal instrument_options_loaded, instrument_options_signature
        instrument_options_loaded = False
        instrument_options_signature = ()

    def current_soundfont_library_signature() -> Tuple[Tuple[str, int, int, int], ...]:
        soundfont_list = _normalize_saved_soundfont_library(
            state.get("soundfonts", []),
            primary_soundfont=str(state.get("soundfont", "")),
        )
        signature: List[Tuple[str, int, int, int]] = []
        for soundfont_path in soundfont_list:
            key = _soundfont_identity(soundfont_path)
            try:
                stat = os.stat(soundfont_path)
                signature.append((key, int(stat.st_mtime_ns), int(stat.st_ctime_ns), int(stat.st_size)))
            except OSError:
                signature.append((key, -1, -1, -1))
        return tuple(signature)

    def refresh_track_entries() -> None:
        nonlocal track_entries, track_entries_loaded
        midi_path = str(state.get("midi", "")).strip()
        track_entries = []
        if midi_path and os.path.exists(midi_path) and midi_path.lower().endswith((".mid", ".midi")):
            try:
                track_entries = parse_midi_track_summaries(midi_path)
            except Exception:
                track_entries = []
        valid_keys = {str(t.index) for t in track_entries}
        track_instr = dict(state.get("track_instruments", {}))
        track_colors = dict(state.get("track_colors", {}))
        track_volumes = _normalize_saved_track_volume_map(state.get("track_volumes", {}))
        state["track_instruments"] = {k: v for k, v in track_instr.items() if k in valid_keys}
        state["track_colors"] = {k: v for k, v in track_colors.items() if k in valid_keys}
        state["track_volumes"] = {k: v for k, v in track_volumes.items() if k in valid_keys}
        track_entries_loaded = True

    def refresh_midi_duration() -> None:
        nonlocal midi_duration_sec, midi_duration_known
        midi_duration_sec = 0.0
        midi_duration_known = False
        midi_path = str(state.get("midi", "")).strip()
        if not midi_path or not os.path.exists(midi_path) or not midi_path.lower().endswith((".mid", ".midi")):
            midi_duration_known = True
            return
        try:
            header, events = parse_midi(midi_path)
            spans_for_bounds = build_note_spans(
                events,
                header.division,
                stuck_note_fix_mode=str(state.get("stuck_note_fix", "auto")),
                long_note_threshold=float(state.get("long_note_threshold", 0.0)),
                long_note_duration=float(state.get("long_note_duration", 0.0)),
            )
            if spans_for_bounds:
                midi_duration_sec = max(float(sp.end) for sp in spans_for_bounds)
        except Exception:
            midi_duration_sec = 0.0
        midi_duration_known = True

    def ensure_track_entries_loaded(stdscr: Optional["curses._CursesWindow"] = None) -> None:
        if track_entries_loaded:
            return
        midi_path = str(state.get("midi", "")).strip()
        if not midi_path or not os.path.exists(midi_path) or not midi_path.lower().endswith((".mid", ".midi")):
            refresh_track_entries()
            return
        if stdscr is None:
            refresh_track_entries()
            return
        run_background_task_popup(
            " MIDI Tracks ",
            "Reading MIDI track summaries for the selected file.",
            refresh_track_entries,
            detail_prefix=os.path.basename(midi_path),
            stdscr=stdscr,
        )

    def clamp_playback_fields() -> None:
        speed = max(0.1, float(state.get("speed", 1.0)))
        fps = max(1, int(float(state.get("fps", 30))))
        full_vel = max(1.0, min(127.0, float(state.get("dynamics_full_velocity", 96.0))))
        pedal_opacity = max(0.0, min(1.0, float(state.get("pedal_opacity_scale", 0.5))))
        pedal_decay = max(0.0, float(state.get("pedal_decay_seconds", 10.0)))
        long_note_threshold = max(0.0, float(state.get("long_note_threshold", 0.0)))
        long_note_duration = max(0.0, float(state.get("long_note_duration", 10.0)))
        state["speed"] = round(speed, 3)
        state["fps"] = int(fps)
        state["dynamics_full_velocity"] = round(full_vel, 3)
        state["pedal_opacity_scale"] = round(pedal_opacity, 3)
        state["pedal_decay_seconds"] = round(pedal_decay, 3)
        state["long_note_threshold"] = round(long_note_threshold, 3)
        state["long_note_duration"] = round(long_note_duration, 3)
        state["start"] = round(max(0.0, float(state.get("start", 0.0))), 3)
        state["range"] = normalize_note_range_text(str(state.get("range", "")))
        if midi_duration_known and midi_duration_sec > 0:
            state["start"] = round(
                clamp_playback_start(float(state["start"]), midi_duration_sec, speed, fps),
                3,
            )

    def refresh_instrument_options() -> None:
        nonlocal instrument_options_loaded, instrument_options_signature
        default_sf = str(state.get("soundfont", "")).strip()
        cur_sf, current_bank, current_prog = parse_instrument_spec(str(state["instrument"]), (0, 0), default_sf)
        current_pair = _clamp_instrument_pair(current_bank, current_prog)
        soundfont_list = _normalize_saved_soundfont_library(state.get("soundfonts", []), primary_soundfont=default_sf)
        state["soundfonts"] = list(soundfont_list)
        state["soundfont"] = soundfont_list[0] if soundfont_list else ""
        default_sf = str(state.get("soundfont", "")).strip()
        library_keys = {_soundfont_identity(sf) for sf in soundfont_list if sf}

        options: List[str] = []
        options_by_key: Dict[Tuple[str, int, int], str] = {}
        for sf in soundfont_list:
            if not sf or not os.path.exists(sf):
                continue
            for opt in scan_soundfont_presets(sf):
                _legacy_sf, b, p = parse_instrument_spec(opt, (0, 0), sf)
                label = _instrument_option_name(opt)
                full_opt = format_instrument_option(b, p, label, soundfont_path=sf)
                key = (_soundfont_identity(sf), b, p)
                if key not in options_by_key:
                    options_by_key[key] = full_opt
                    options.append(full_opt)

        cur_sf_key = _soundfont_identity(cur_sf)
        if cur_sf_key not in library_keys:
            cur_sf = default_sf
            cur_sf_key = _soundfont_identity(default_sf)
        chosen = options_by_key.get((cur_sf_key, current_pair[0], current_pair[1]))
        if not chosen:
            default_key = _soundfont_identity(default_sf)
            chosen = options_by_key.get((default_key, current_pair[0], current_pair[1]))
        if not chosen and options:
            chosen = options[0]
        if not chosen and cur_sf:
            chosen = format_instrument_option(current_pair[0], current_pair[1], soundfont_path=cur_sf)
            if _soundfont_identity(cur_sf) in library_keys:
                options = [chosen] + options
        if not options:
            fallback_sf = default_sf if default_sf else ""
            chosen = format_instrument_option(0, 0, soundfont_path=fallback_sf)
            options = [chosen]
        state["instrument"] = chosen
        for f in fields:
            if str(f.get("key", "")) == "instrument":
                f["options"] = options if options else [chosen]
                break
        track_instr = dict(state.get("track_instruments", {}))
        for tk, tv in list(track_instr.items()):
            sf, b, p = parse_instrument_spec(tv, current_pair, default_sf)
            sf_key = _soundfont_identity(sf)
            if sf_key not in library_keys:
                track_instr.pop(tk, None)
                continue
            match = options_by_key.get((sf_key, b, p))
            if match:
                track_instr[tk] = match
            else:
                track_instr.pop(tk, None)
        state["track_instruments"] = track_instr
        instrument_options_loaded = True
        instrument_options_signature = current_soundfont_library_signature()

    def ensure_instrument_options_loaded(stdscr: Optional["curses._CursesWindow"] = None) -> None:
        if instrument_options_loaded and instrument_options_signature == current_soundfont_library_signature():
            return
        soundfont_list = _normalize_saved_soundfont_library(
            state.get("soundfonts", []),
            primary_soundfont=str(state.get("soundfont", "")),
        )
        if not soundfont_list:
            refresh_instrument_options()
            return
        if stdscr is None:
            refresh_instrument_options()
            return
        run_background_task_popup(
            " SoundFonts ",
            "Scanning SoundFont presets for instrument selection.",
            refresh_instrument_options,
            detail_prefix=f"{len(soundfont_list)} file(s)",
            stdscr=stdscr,
        )

    clamp_playback_fields()

    def val_display(field: Dict[str, object]) -> str:
        k = str(field["key"])
        t = str(field["type"])
        if t == "button":
            return ""
        if t == "profile_selector":
            if not profile_entries:
                return "<none>"
            return _fmt_choice(profile_name_at(profile_selection) or f"Profile {profile_selection + 1}")
        if t in {"profile_save_button", "profile_delete_button", "profile_rename_button"}:
            return ""
        v = state[k]
        if t == "bool":
            return "ON" if bool(v) else "OFF"
        if t in {"choice", "midi_browser"}:
            return _fmt_choice(str(v))
        if t == "soundfont_manager_browser":
            items = _normalize_saved_soundfont_library(state.get("soundfonts", []))
            if not items:
                return "<none>"
            primary = os.path.basename(items[0]) if items else "<none>"
            return f"{len(items)} file(s), primary={primary}"
        if t == "instrument_browser":
            return _fmt_choice(instrument_option_label(str(v)))
        if t == "audio_output_browser":
            txt = str(v).strip()
            return txt if txt else "<default>"
        if k == "terminal_bg_color":
            txt = str(v).strip()
            return txt if txt else "auto"
        if t == "live_instrument_map_browser":
            mapping = parse_live_instrument_map(str(v))
            color_mapping = parse_live_color_map(str(state.get("midi_input_color_map", "")))
            if not mapping:
                return "<auto 1..0>"
            parts: List[str] = []
            for key in sorted(mapping.keys()):
                b, p = mapping[key]
                parts.append(f"{key}->{b}:{p}")
                if len(parts) >= 4:
                    break
            suffix = " ..." if len(mapping) > 4 else ""
            return f"{len(mapping)} keys/{len(color_mapping)} colors: {' '.join(parts)}{suffix}"
        if t == "track_customization_browser":
            inst_overrides = len(dict(state.get("track_instruments", {})))
            color_overrides = len(dict(state.get("track_colors", {})))
            vol_overrides = len(_normalize_saved_track_volume_map(state.get("track_volumes", {})))
            if track_entries_loaded:
                total_txt = str(len(track_entries))
            else:
                total_txt = "?"
            total = total_txt
            return f"{total} tracks  inst:{inst_overrides}  color:{color_overrides}  vol:{vol_overrides}"
        if t == "color_rendering_editor":
            return (
                f"Notes:{state['note_color_mode']}  "
                f"Pedals:{state['pedal_color_mode']}  Piano:{state['piano_color_mode']}"
            )
        if t == "glyph_editor":
            return (
                f"N:{state['glyph_note_white']}/{state['glyph_note_black_left']}{state['glyph_note_black_mid']}{state['glyph_note_black_right']} "
                f"P:{state['glyph_note_pedal_white']}/{state['glyph_note_pedal_black_left']}{state['glyph_note_pedal_black_mid']}{state['glyph_note_pedal_black_right']} "
                f"K:{state['glyph_piano_white']}/{state['glyph_piano_black_left']}{state['glyph_piano_black_mid']}{state['glyph_piano_black_right']}"
            )
        if t == "offpiano_pattern_editor":
            txt = str(v or "")
            if not txt.strip():
                return "default"
            lines = txt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            width = max((len(line) for line in lines), default=0)
            return f"custom {len(lines)}x{width}"
        if t == "float":
            return f"{float(v):.2f}"
        return str(v)

    def cycle_choice(key: str, options: Sequence[str], delta: int) -> None:
        cur = str(state.get(key, ""))
        if not options:
            state[key] = ""
            return
        if cur not in options:
            idx = 0
        else:
            idx = options.index(cur)
        state[key] = options[(idx + delta) % len(options)]

    def cycle_profile(delta: int) -> None:
        nonlocal profile_selection
        if not profile_entries:
            profile_selection = -1
            return
        if profile_selection < 0 or profile_selection >= len(profile_entries):
            profile_selection = 0
        else:
            profile_selection = (profile_selection + delta) % len(profile_entries)
        snapshot = profile_entries[profile_selection].get("config", {})
        if isinstance(snapshot, dict):
            apply_saved_config_snapshot(dict(snapshot))
        persist_state()

    def nudge_numeric(field: Dict[str, object], delta: int) -> None:
        key = str(field["key"])
        t = str(field["type"])
        step = float(field.get("step", 1))
        min_v = field.get("min")
        if t == "int":
            val = int(state[key]) + int(step) * delta
            if min_v is not None:
                val = max(int(min_v), val)
            state[key] = val
        elif t == "float":
            val = float(state[key]) + step * delta
            if min_v is not None:
                val = max(float(min_v), val)
            state[key] = round(val, 3)
        persist_state()

    def set_numeric_from_text(field: Dict[str, object], typed: str) -> bool:
        key = str(field["key"])
        t = str(field["type"])
        min_v = field.get("min")
        txt = typed.strip()
        if not txt:
            return False
        try:
            if t == "int":
                val = int(float(txt))
                if min_v is not None:
                    val = max(int(min_v), val)
                state[key] = val
            elif t == "float":
                val = float(txt)
                if min_v is not None:
                    val = max(float(min_v), val)
                state[key] = round(val, 3)
            else:
                return False
        except (ValueError, OverflowError):
            return False
        persist_state()
        return True

    def choose_file_by_ext(
        stdscr: "curses._CursesWindow",
        current_value: str,
        title: str,
        file_tag: str,
        extensions: Tuple[str, ...],
    ) -> Optional[str]:
        exts = tuple(e.lower() for e in extensions)
        base = os.path.abspath(".")
        if current_value:
            abs_cur = os.path.abspath(current_value)
            start_dir = abs_cur if os.path.isdir(abs_cur) else os.path.dirname(abs_cur)
            if not os.path.isdir(start_dir):
                start_dir = base
        else:
            start_dir = base
        cur_dir = start_dir
        selected = 0
        offset = 0
        filter_text = ""
        filter_editing = False
        current_abs = os.path.abspath(current_value) if str(current_value or "").strip() else ""

        def rel_display(path: str) -> str:
            try:
                rel = os.path.relpath(path, base)
                if not rel.startswith(".."):
                    return rel
            except Exception:
                pass
            return path

        def load_entries(path: str) -> List[Tuple[str, str, str]]:
            entries: List[Tuple[str, str, str]] = []
            parent = os.path.abspath(os.path.join(path, ".."))
            entries.append(("dir", "..", parent))
            dirs: List[str] = []
            files: List[str] = []
            try:
                for ent in os.scandir(path):
                    n = ent.name
                    if n.startswith("."):
                        continue
                    if ent.is_dir(follow_symlinks=False):
                        dirs.append(n)
                    elif ent.is_file(follow_symlinks=False) and ((not exts) or n.lower().endswith(exts)):
                        files.append(n)
            except Exception:
                pass
            for n in sorted(dirs, key=str.lower):
                entries.append(("dir", n + "/", os.path.join(path, n)))
            for n in sorted(files, key=str.lower):
                entries.append(("file", n, os.path.join(path, n)))
            return entries

        def entry_index_for_path(entries: Sequence[Tuple[str, str, str]], path: str) -> int:
            if not path:
                return 0
            target = os.path.abspath(path)
            for idx, (_kind, _name, full) in enumerate(entries):
                if os.path.abspath(full) == target:
                    return idx
            return 0

        selected = entry_index_for_path(load_entries(cur_dir), current_abs)

        while True:
            all_entries = load_entries(cur_dir)
            if filter_text:
                needle = filter_text.lower()
                entries = [all_entries[0]] + [e for e in all_entries[1:] if needle in e[1].lower()]
            else:
                entries = all_entries
            if selected >= len(entries):
                selected = max(0, len(entries) - 1)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            loc = f"Folder: {rel_display(cur_dir)}"
            stdscr.addstr(1, 2, loc[: max(1, w - 4)])
            help_line = "Up/Down move  Enter open/select  Left go up  / filter  c clear  Esc cancel"
            stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
            if filter_editing:
                shown_filter = f"{filter_text}_"
            else:
                shown_filter = filter_text if filter_text else "<none>"
            filter_line = f"Filter: {shown_filter}"
            stdscr.addstr(3, 2, filter_line[: max(1, w - 4)])

            list_top = 5
            list_h = max(1, h - list_top - 2)
            if selected < offset:
                offset = selected
            if selected >= offset + list_h:
                offset = selected - list_h + 1

            for row in range(list_h):
                i = offset + row
                if i >= len(entries):
                    break
                kind, name, full = entries[i]
                if kind == "dir":
                    label = f"[DIR]  {name}"
                else:
                    label = f"[{file_tag}]  {name}"
                if os.path.abspath(full) == os.path.abspath(current_value):
                    label += "  (current)"
                attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
                stdscr.addstr(list_top + row, 2, label[: max(1, w - 4)], attr)
            stdscr.refresh()

            k = stdscr.getch()
            if filter_editing and _is_picker_navigation_key(k):
                filter_editing = False
            if filter_editing:
                if k in (10, 13, curses.KEY_ENTER, curses.KEY_EXIT):
                    filter_editing = False
                    continue
                if k == 27:
                    filter_editing = False
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                    selected = 0
                    offset = 0
                    continue
                if 32 <= k <= 126:
                    filter_text += chr(k).lower()
                    selected = 0
                    offset = 0
                    continue
                continue
            if k in (27, ord("q"), ord("Q")):
                return None
            if k == ord("/"):
                filter_editing = True
                continue
            if k in (ord("c"), ord("C")):
                return ""
            if k in (curses.KEY_UP,):
                selected = (selected - 1) % max(1, len(entries))
                continue
            if k in (curses.KEY_DOWN,):
                selected = (selected + 1) % max(1, len(entries))
                continue
            if k in (curses.KEY_PPAGE,):
                selected = max(0, selected - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                selected = min(max(0, len(entries) - 1), selected + list_h)
                continue
            if k in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127):
                cur_dir = os.path.abspath(os.path.join(cur_dir, ".."))
                selected = 0
                offset = 0
                continue
            if k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                if not entries:
                    continue
                kind, _name, full = entries[selected]
                if kind == "dir":
                    if filter_text:
                        filter_text = ""
                        filter_editing = False
                        selected = 0
                        offset = 0
                    cur_dir = os.path.abspath(full)
                    selected = 0
                    offset = 0
                else:
                    return rel_display(full)

    def choose_midi_file(stdscr: "curses._CursesWindow", current_value: str) -> Optional[str]:
        return choose_file_by_ext(
            stdscr,
            current_value,
            title=" MIDI File Selector ",
            file_tag="MID",
            extensions=(".mid", ".midi"),
        )

    def choose_soundfont_file(stdscr: "curses._CursesWindow", current_value: str) -> Optional[str]:
        return choose_file_by_ext(
            stdscr,
            current_value,
            title=" SoundFont Selector ",
            file_tag="SF2",
            extensions=(".sf2", ".sf3"),
        )

    def choose_any_file(stdscr: "curses._CursesWindow", current_value: str) -> Optional[str]:
        return choose_file_by_ext(
            stdscr,
            current_value,
            title=" File Selector ",
            file_tag="FILE",
            extensions=(),
        )

    def choose_soundfont_library(
        stdscr: "curses._CursesWindow",
        primary_soundfont: str,
        current_items: Sequence[str],
    ) -> Optional[List[str]]:
        file_entries = scan_files_by_ext((".sf2", ".sf3"))
        selected_items = _normalize_saved_soundfont_library(list(current_items), primary_soundfont=primary_soundfont)
        for sf in selected_items:
            if _soundfont_identity(sf) not in {_soundfont_identity(it) for it in file_entries}:
                file_entries.append(sf)
        file_entries.sort(key=str.lower)

        top_idx = 0
        top_off = 0
        bot_idx = 0
        bot_off = 0
        active_top = True
        status_msg = ""
        filter_text = ""
        filter_editing = False

        def visible_files() -> List[str]:
            if not filter_text:
                return list(file_entries)
            needle = filter_text.lower()
            return [sf for sf in file_entries if needle in sf.lower()]

        while True:
            files = visible_files()
            if top_idx >= len(files):
                top_idx = max(0, len(files) - 1)
            if bot_idx >= len(selected_items):
                bot_idx = max(0, len(selected_items) - 1)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " SoundFonts "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "Tab switch pane  Space add/remove  p set primary  d remove(selected)  / filter  s save  Esc cancel"
            stdscr.addstr(1, 2, help_line[: max(1, w - 4)])
            shown_filter = f"{filter_text}_" if filter_editing else (filter_text if filter_text else "<none>")
            stdscr.addstr(2, 2, f"Filter: {shown_filter}"[: max(1, w - 4)])
            if status_msg:
                stdscr.addstr(3, 2, status_msg[: max(1, w - 4)], curses.A_DIM)

            split_y = max(8, h // 2)
            top_h = max(2, split_y - 6)
            bot_h = max(2, h - split_y - 3)
            stdscr.addstr(5, 2, ("[Files]" if active_top else " Files ")[: max(1, w - 4)], curses.A_BOLD)
            stdscr.addstr(split_y - 1, 2, ("[Selected]" if not active_top else " Selected ")[: max(1, w - 4)], curses.A_BOLD)

            if top_idx < top_off:
                top_off = top_idx
            if top_idx >= top_off + top_h:
                top_off = top_idx - top_h + 1
            if not files:
                stdscr.addstr(6, 2, "(no matching soundfonts)"[: max(1, w - 4)], curses.A_DIM)
            else:
                for row in range(top_h):
                    i = top_off + row
                    if i >= len(files):
                        break
                    sf = files[i]
                    included = _soundfont_identity(sf) in {_soundfont_identity(it) for it in selected_items}
                    marker = "[x]" if included else "[ ]"
                    txt = f"{marker} {sf}"
                    attr = curses.A_REVERSE if (active_top and i == top_idx) else curses.A_NORMAL
                    stdscr.addstr(6 + row, 2, txt[: max(1, w - 4)], attr)

            if bot_idx < bot_off:
                bot_off = bot_idx
            if bot_idx >= bot_off + bot_h:
                bot_off = bot_idx - bot_h + 1
            if not selected_items:
                stdscr.addstr(split_y, 2, "(no selected soundfonts)"[: max(1, w - 4)], curses.A_DIM)
            else:
                for row in range(bot_h):
                    i = bot_off + row
                    if i >= len(selected_items):
                        break
                    sf = selected_items[i]
                    tag = "(primary)" if i == 0 else ""
                    txt = f"{i + 1:02d}. {sf} {tag}".rstrip()
                    attr = curses.A_REVERSE if ((not active_top) and i == bot_idx) else curses.A_NORMAL
                    stdscr.addstr(split_y + row, 2, txt[: max(1, w - 4)], attr)

            stdscr.refresh()
            k = stdscr.getch()
            if filter_editing and _is_picker_navigation_key(k):
                filter_editing = False
            if filter_editing:
                if k in (10, 13, curses.KEY_ENTER, 27):
                    filter_editing = False
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                    top_idx = 0
                    top_off = 0
                    continue
                if 32 <= k <= 126:
                    filter_text += chr(k).lower()
                    top_idx = 0
                    top_off = 0
                    continue
                continue
            if k in (27, ord("q"), ord("Q")):
                return None
            if k == ord("/"):
                active_top = True
                filter_editing = True
                continue
            if k == 9:
                active_top = not active_top
                continue
            if k in (ord("s"), ord("S")):
                return list(selected_items)
            if k in (curses.KEY_UP,):
                if active_top:
                    if files:
                        top_idx = (top_idx - 1) % len(files)
                else:
                    if selected_items:
                        bot_idx = (bot_idx - 1) % len(selected_items)
                continue
            if k in (curses.KEY_DOWN,):
                if active_top:
                    if files:
                        top_idx = (top_idx + 1) % len(files)
                else:
                    if selected_items:
                        bot_idx = (bot_idx + 1) % len(selected_items)
                continue
            if k in (curses.KEY_PPAGE,):
                if active_top:
                    top_idx = max(0, top_idx - top_h)
                else:
                    bot_idx = max(0, bot_idx - bot_h)
                continue
            if k in (curses.KEY_NPAGE,):
                if active_top:
                    top_idx = min(max(0, len(files) - 1), top_idx + top_h)
                else:
                    bot_idx = min(max(0, len(selected_items) - 1), bot_idx + bot_h)
                continue
            if k in (ord(" "),):
                if not active_top:
                    if not selected_items:
                        status_msg = "No selected soundfonts to remove."
                        continue
                    removed = selected_items.pop(bot_idx)
                    bot_idx = min(bot_idx, max(0, len(selected_items) - 1))
                    status_msg = f"Removed {removed}"
                    continue
                if not files:
                    status_msg = "No file selected."
                    continue
                sf = files[top_idx]
                sf_key = _soundfont_identity(sf)
                found_idx = next((i for i, it in enumerate(selected_items) if _soundfont_identity(it) == sf_key), -1)
                if found_idx >= 0:
                    selected_items.pop(found_idx)
                    status_msg = f"Removed {sf}"
                else:
                    selected_items.append(sf)
                    status_msg = f"Added {sf}"
                bot_idx = min(bot_idx, max(0, len(selected_items) - 1))
                continue
            if k in (ord("d"), ord("D")):
                if not selected_items:
                    status_msg = "No selected soundfonts."
                    continue
                removed = selected_items.pop(bot_idx)
                bot_idx = min(bot_idx, max(0, len(selected_items) - 1))
                status_msg = f"Removed {removed}"
                continue
            if k in (ord("p"), ord("P")):
                if active_top:
                    if not files:
                        status_msg = "No file selected."
                        continue
                    sf = files[top_idx]
                    sf_key = _soundfont_identity(sf)
                    found_idx = next((i for i, it in enumerate(selected_items) if _soundfont_identity(it) == sf_key), -1)
                    if found_idx >= 0:
                        selected_items.pop(found_idx)
                    selected_items.insert(0, sf)
                    bot_idx = 0
                    status_msg = f"Primary set: {sf}"
                else:
                    if not selected_items:
                        status_msg = "No selected soundfonts."
                        continue
                    sf = selected_items.pop(bot_idx)
                    selected_items.insert(0, sf)
                    bot_idx = 0
                    status_msg = f"Primary set: {sf}"
                continue

    def choose_audio_output(stdscr: "curses._CursesWindow", current_value: str) -> Optional[str]:
        targets = list_audio_output_targets()
        entries: List[Tuple[str, str]] = [("", "<default> (auto routing)")]
        entries.extend(targets)
        cur = str(current_value or "").strip()
        if cur and all(val != cur for val, _lbl in entries):
            entries.append((cur, f"{cur}  (current)"))

        selected = 0
        if cur:
            for i, (val, _lbl) in enumerate(entries):
                if val == cur:
                    selected = i
                    break
        offset = 0
        filter_text = ""
        filter_editing = False

        while True:
            if filter_text:
                filtered = [it for it in entries if filter_text in it[1].lower() or filter_text in it[0].lower()]
            else:
                filtered = list(entries)
            if selected >= len(filtered):
                selected = max(0, len(filtered) - 1)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Audio Output Selector "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "Up/Down move  Enter select  PgUp/PgDn page  / filter  c default  Esc cancel"
            stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
            shown_filter = f"{filter_text}_" if filter_editing else (filter_text if filter_text else "<none>")
            stdscr.addstr(3, 2, f"Filter: {shown_filter}"[: max(1, w - 4)])

            top = 5
            list_h = max(1, h - top - 2)
            if selected < offset:
                offset = selected
            if selected >= offset + list_h:
                offset = selected - list_h + 1
            if not filtered:
                stdscr.addstr(top, 2, "(no matching outputs)"[: max(1, w - 4)], curses.A_DIM)
            else:
                for row in range(list_h):
                    i = offset + row
                    if i >= len(filtered):
                        break
                    val, label = filtered[i]
                    txt = label + ("  (current)" if val == cur and val else "")
                    attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
                    stdscr.addstr(top + row, 2, txt[: max(1, w - 4)], attr)
            stdscr.refresh()

            k = stdscr.getch()
            if filter_editing and _is_picker_navigation_key(k):
                filter_editing = False
            if filter_editing:
                if k in (10, 13, curses.KEY_ENTER, curses.KEY_EXIT, 27):
                    filter_editing = False
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                    selected = 0
                    offset = 0
                    continue
                if 32 <= k <= 126:
                    filter_text += chr(k).lower()
                    selected = 0
                    offset = 0
                    continue
                continue
            if k in (27, ord("q"), ord("Q")):
                return None
            if k == ord("/"):
                filter_editing = True
                continue
            if k in (ord("c"), ord("C")):
                return ""
            if k in (curses.KEY_UP,):
                if filtered:
                    selected = (selected - 1) % len(filtered)
                continue
            if k in (curses.KEY_DOWN,):
                if filtered:
                    selected = (selected + 1) % len(filtered)
                continue
            if k in (curses.KEY_PPAGE,):
                selected = max(0, selected - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                selected = min(max(0, len(filtered) - 1), selected + list_h)
                continue
            if k in (10, 13, curses.KEY_ENTER):
                if not filtered:
                    continue
                return filtered[selected][0]

    def edit_multiline_text(
        stdscr: "curses._CursesWindow",
        title: str,
        initial_value: str,
    ) -> Optional[str]:
        h, w = stdscr.getmaxyx()
        box_h = max(10, min(h - 2, 22))
        box_w = max(40, min(w - 2, 100))
        top = max(1, (h - box_h) // 2)
        left = max(1, (w - box_w) // 2)
        win = stdscr.derwin(box_h, box_w, top, left)
        win.erase()
        win.box()
        win.addstr(0, max(2, (box_w - len(title)) // 2), title[: max(1, box_w - 4)], curses.A_BOLD)
        help_line = "Paste/edit text. Ctrl-S/F2/Ctrl-G save, Esc cancels."
        win.addstr(1, 2, help_line[: max(1, box_w - 4)])
        frame_top = 2
        frame_left = 2
        frame_h = max(3, box_h - 4)
        frame_w = max(3, box_w - 4)
        frame_win = win.derwin(frame_h, frame_w, frame_top, frame_left)
        frame_win.erase()
        frame_win.box()
        edit_h = max(1, frame_h - 2)
        edit_w = max(1, frame_w - 2)
        edit_win = frame_win.derwin(edit_h, edit_w, 1, 1)
        edit_win.erase()
        initial_lines = str(initial_value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for row, line in enumerate(initial_lines[:edit_h]):
            try:
                edit_win.addstr(row, 0, line[:edit_w])
            except Exception:
                pass
        edit_win.move(0, 0)
        win.refresh()
        frame_win.refresh()
        edit_win.refresh()

        def validator(ch: int) -> int:
            if ch == 27:
                raise KeyboardInterrupt()
            if ch in (19, curses.KEY_F2):
                return 7
            return ch

        box = curses.textpad.Textbox(edit_win, insert_mode=True)
        try:
            curses.curs_set(1)
            with _disable_terminal_flow_control():
                result = box.edit(validator)
            normalized = str(result).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            return "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))
        except KeyboardInterrupt:
            return None
        finally:
            curses.curs_set(0)

    def choose_offpiano_pattern(stdscr: "curses._CursesWindow", current_value: str) -> Optional[str]:
        working = str(current_value or "")
        status_msg = ""
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Background Pattern "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "e edit/paste  l load file  d default  s save  Esc cancel"
            stdscr.addstr(1, 2, help_line[: max(1, w - 4)])
            if status_msg:
                stdscr.addstr(2, 2, status_msg[: max(1, w - 4)], curses.A_DIM)
            lines = str(working or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if str(working or "") else []
            if lines:
                dims = f"Custom pattern: {len(lines)} line(s), max width {max(len(line) for line in lines)}"
            else:
                dims = "Using built-in default pattern."
            stdscr.addstr(3, 2, dims[: max(1, w - 4)])
            preview_top = 5
            preview_h = max(1, h - preview_top - 2)
            preview_lines = lines if lines else ["<built-in default pattern>"]
            for row in range(min(preview_h, len(preview_lines))):
                stdscr.addstr(preview_top + row, 2, preview_lines[row][: max(1, w - 4)])
            stdscr.refresh()

            k = stdscr.getch()
            if k in (27, ord("q"), ord("Q")):
                return None
            if k in (ord("s"), ord("S"), 10, 13, curses.KEY_ENTER):
                return working
            if k in (ord("d"), ord("D"), ord("c"), ord("C")):
                working = ""
                status_msg = "Reverted to built-in default pattern."
                continue
            if k in (ord("l"), ord("L")):
                picked = choose_any_file(stdscr, "")
                path_txt = str(picked or "").strip()
                if not path_txt:
                    status_msg = "No file selected."
                    continue
                try:
                    with open(path_txt, "r", encoding="utf-8") as f:
                        working = f.read().replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                    status_msg = f"Loaded pattern from {path_txt}"
                except Exception as exc:
                    status_msg = f"Load failed: {exc}"
                continue
            if k in (ord("e"), ord("E")):
                edited = edit_multiline_text(stdscr, " Pattern Text ", working)
                if edited is not None:
                    working = edited
                    status_msg = "Updated custom pattern text."
                continue

    def start_instrument_demo(option_text: str) -> Tuple[Optional[subprocess.Popen], Optional[str], Optional[str]]:
        soundfont = instrument_option_soundfont(option_text, str(state.get("soundfont", ""))).strip()
        if not soundfont:
            return None, None, "Select a SoundFont first."
        if not os.path.exists(soundfont):
            return None, None, f"SoundFont not found: {soundfont}"
        exe_name = str(state.get("fluidsynth_bin", "fluidsynth")).strip() or "fluidsynth"
        exe = shutil.which(exe_name)
        if not exe:
            return None, None, f"fluidsynth executable not found: {exe_name}"

        bank, program = parse_instrument_pair(option_text, parse_instrument_pair(str(state["instrument"])))
        preview_spans = make_instrument_preview_spans()
        temp_midi = create_demo_midi_temp(preview_spans, channel_programs={0: (bank, program)})

        driver = _resolve_audio_driver(str(state.get("audio_driver", "")))
        cmd = [exe, "-i", "-g", str(float(state.get("audio_gain", 0.6)))]
        if driver:
            cmd.extend(["-a", driver])
        _append_cli_audio_output_option(cmd, driver, str(state.get("audio_output", "")))
        period_size = int(state.get("audio_period_size", 0))
        periods = int(state.get("audio_periods", 0))
        samplerate = int(state.get("audio_samplerate", 0))
        polyphony = int(state.get("audio_polyphony", 0))
        if period_size > 0:
            cmd.extend(["-o", f"audio.period-size={period_size}"])
        if periods > 0:
            cmd.extend(["-o", f"audio.periods={periods}"])
        if samplerate > 0:
            cmd.extend(["-o", f"synth.sample-rate={samplerate}"])
        if polyphony > 0:
            cmd.extend(["-o", f"synth.polyphony={polyphony}"])
        cmd.extend([soundfont, temp_midi])

        env = _fluidsynth_env(driver, str(state.get("audio_output", "")))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            return proc, temp_midi, None
        except Exception as exc:
            if os.path.exists(temp_midi):
                try:
                    os.unlink(temp_midi)
                except Exception:
                    pass
            return None, None, f"Demo failed: {exc}"

    def get_instrument_options() -> List[str]:
        options: List[str] = []
        for f in fields:
            if str(f.get("key", "")) == "instrument":
                options = list(f.get("options", []))
                break
        if not options:
            return [str(state["instrument"])]
        return options

    def choose_instrument(
        stdscr: "curses._CursesWindow",
        current_value: str,
        title: str = " Instrument Selector ",
        options_override: Optional[List[str]] = None,
        hotkey_map: Optional[Dict[str, Tuple[int, int]]] = None,
        hotkey_color_map: Optional[Dict[str, Tuple[int, int, int]]] = None,
        allow_mapping: bool = False,
    ) -> Optional[str]:
        options = list(options_override) if options_override is not None else get_instrument_options()
        if not options:
            return None
        selected = options.index(current_value) if current_value in options else 0
        offset = 0
        status_msg = ""
        filter_text = ""
        filter_editing = False
        mapping_ref = hotkey_map if hotkey_map is not None else {}
        color_ref = hotkey_color_map if hotkey_color_map is not None else {}
        demo_proc: Optional[subprocess.Popen] = None
        demo_temp_midi: Optional[str] = None
        demo_player: Optional[Any] = None
        demo_soundfont_key = ""
        demo_preview_spans = make_instrument_preview_spans()
        demo_duration = max((sp.end for sp in demo_preview_spans), default=0.0)
        demo_started_perf: Optional[float] = None

        def stop_demo(close_player: bool = False) -> None:
            nonlocal demo_proc, demo_temp_midi, demo_player, demo_started_perf, demo_soundfont_key
            if demo_proc is not None:
                stop_process(demo_proc)
                demo_proc = None
            if demo_temp_midi and os.path.exists(demo_temp_midi):
                try:
                    os.unlink(demo_temp_midi)
                except Exception:
                    pass
            demo_temp_midi = None
            demo_started_perf = None
            if demo_player is not None:
                try:
                    demo_player.all_notes_off()
                except Exception:
                    pass
                if close_player:
                    with _suppress_stderr_fd():
                        try:
                            demo_player.close()
                        except Exception:
                            pass
                    demo_player = None
                    demo_soundfont_key = ""

        def start_demo_python(option_text: str) -> Optional[str]:
            nonlocal demo_player, demo_started_perf, demo_soundfont_key
            soundfont = instrument_option_soundfont(option_text, str(state.get("soundfont", ""))).strip()
            if not soundfont:
                return "Select a SoundFont first."
            if not os.path.exists(soundfont):
                return f"SoundFont not found: {soundfont}"
            soundfont_key = _soundfont_identity(soundfont)
            pair = parse_instrument_pair(option_text, parse_instrument_pair(str(state["instrument"])))
            driver = _resolve_audio_driver(str(state.get("audio_driver", "")))
            gain = float(state.get("audio_gain", 0.6))
            samplerate = int(state.get("audio_samplerate", 0) or 0)
            period_size = int(state.get("audio_period_size", 0) or 0)
            periods = int(state.get("audio_periods", 0) or 0)
            polyphony = int(state.get("audio_polyphony", 0) or 0)
            try:
                with _suppress_stderr_fd():
                    if demo_player is not None and demo_soundfont_key != soundfont_key:
                        demo_player.close()
                        demo_player = None
                        demo_soundfont_key = ""
                    if demo_player is None:
                        demo_player = RealtimeFluidSynthPlayer(
                            spans=demo_preview_spans,
                            soundfont_path=soundfont,
                            audio_gain=gain,
                            driver=driver,
                            output_target=str(state.get("audio_output", "")),
                            samplerate=samplerate,
                            period_size=period_size,
                            periods=periods,
                            polyphony=polyphony,
                            channel_programs={0: pair},
                            control_events=[],
                        )
                        demo_soundfont_key = soundfont_key
                    else:
                        demo_player.set_channel_program(0, pair[0], pair[1])
                        demo_player.set_gain(gain)
                    demo_player.seek(0.0)
                demo_started_perf = time.perf_counter()
                return None
            except Exception as exc:
                if demo_player is not None:
                    with _suppress_stderr_fd():
                        try:
                            demo_player.close()
                        except Exception:
                            pass
                    demo_player = None
                demo_soundfont_key = ""
                demo_started_perf = None
                return str(exc)

        def mapped_keys_for_option(option_text: str) -> str:
            if not allow_mapping or not mapping_ref:
                return ""
            pair = parse_instrument_pair(option_text)
            keys = [k for k, v in mapping_ref.items() if v == pair]
            if not keys:
                return ""
            parts: List[str] = []
            for k in sorted(keys):
                rgb = color_ref.get(k, default_live_hotkey_color(k))
                parts.append(f"{k}:{rgb_triplet_to_text(rgb)}")
            return " ".join(parts)

        while True:
            if demo_player is not None and demo_started_perf is not None:
                elapsed = max(0.0, time.perf_counter() - demo_started_perf)
                try:
                    if elapsed <= (demo_duration + 0.10):
                        demo_player.update(elapsed)
                    else:
                        stop_demo(close_player=False)
                        status_msg = "Demo finished."
                except Exception as exc:
                    stop_demo(close_player=True)
                    status_msg = f"Demo stopped: {exc}"

            if demo_proc is not None and demo_proc.poll() is not None:
                rc = demo_proc.returncode
                stop_demo(close_player=False)
                status_msg = f"Demo finished (exit {rc})."

            if filter_text:
                filtered_options = [opt for opt in options if filter_text in instrument_option_label(opt).lower()]
            else:
                filtered_options = list(options)
            if selected >= len(filtered_options):
                selected = max(0, len(filtered_options) - 1)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            sf_count = len(_normalize_saved_soundfont_library(state.get("soundfonts", [])))
            stdscr.addstr(1, 2, f"SoundFonts: {sf_count}"[: max(1, w - 4)])
            if allow_mapping:
                help_line = "Up/Down move  Enter select  PgUp/PgDn page  / filter  d demo  m map  c color  x clear  Esc done"
            else:
                help_line = "Up/Down move  Enter select  PgUp/PgDn page  / filter  d demo  Esc cancel"
            stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
            if status_msg:
                stdscr.addstr(3, 2, status_msg[: max(1, w - 4)], curses.A_DIM)
            if filter_editing:
                shown_filter = f"{filter_text}_"
            else:
                shown_filter = filter_text if filter_text else "<none>"
            filter_line = f"Filter: {shown_filter}"
            stdscr.addstr(4, 2, filter_line[: max(1, w - 4)])

            list_top = 6
            list_h = max(1, h - list_top - 2)
            if selected < offset:
                offset = selected
            if selected >= offset + list_h:
                offset = selected - list_h + 1

            if not filtered_options:
                stdscr.addstr(list_top, 2, "(no matching instruments)"[: max(1, w - 4)], curses.A_DIM)
            else:
                for row in range(list_h):
                    i = offset + row
                    if i >= len(filtered_options):
                        break
                    opt = filtered_options[i]
                    sf_path, payload = _split_instrument_option(opt)
                    label = payload if payload else instrument_option_label(opt)
                    mapped_keys = mapped_keys_for_option(opt)
                    if mapped_keys:
                        label += f"  [map:{mapped_keys}]"
                    if opt == current_value:
                        label += "  (current)"
                    if sf_path:
                        sf_suffix = f"({os.path.basename(sf_path) or sf_path})"
                        avail = max(1, w - 4)
                        if len(label) + len(sf_suffix) + 1 <= avail:
                            label = f"{label}{' ' * (avail - len(label) - len(sf_suffix))}{sf_suffix}"
                        else:
                            label = f"{label} {sf_suffix}"
                    attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
                    stdscr.addstr(list_top + row, 2, label[: max(1, w - 4)], attr)
            stdscr.refresh()

            try:
                demo_active = (demo_proc is not None) or (demo_started_perf is not None)
                stdscr.timeout(100 if demo_active else -1)
            except Exception:
                pass
            k = stdscr.getch()
            if k == -1:
                continue
            if filter_editing and _is_picker_navigation_key(k):
                filter_editing = False
            if filter_editing:
                if k in (10, 13, curses.KEY_ENTER, curses.KEY_EXIT):
                    filter_editing = False
                    continue
                if k == 27:
                    filter_editing = False
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                    selected = 0
                    offset = 0
                    continue
                if 32 <= k <= 126:
                    filter_text += chr(k).lower()
                    selected = 0
                    offset = 0
                    continue
                continue
            if k in (27, ord("q"), ord("Q")):
                stop_demo(close_player=True)
                return None
            if k == ord("/"):
                filter_editing = True
                continue
            if k in (curses.KEY_UP,):
                if not filtered_options:
                    continue
                selected = (selected - 1) % len(filtered_options)
                continue
            if k in (curses.KEY_DOWN,):
                if not filtered_options:
                    continue
                selected = (selected + 1) % len(filtered_options)
                continue
            if k in (curses.KEY_PPAGE,):
                if not filtered_options:
                    continue
                selected = max(0, selected - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                if not filtered_options:
                    continue
                selected = min(len(filtered_options) - 1, selected + list_h)
                continue
            if k in (ord("d"), ord("D")):
                if not filtered_options:
                    status_msg = "No matching instrument to demo."
                    continue
                chosen = filtered_options[selected]
                stop_demo(close_player=False)
                status_msg = f"Starting demo: {instrument_option_label(chosen)}"
                stdscr.addstr(3, 2, status_msg[: max(1, w - 4)], curses.A_DIM)
                stdscr.refresh()
                py_err = str(start_demo_python(chosen) or "")
                if not py_err:
                    status_msg = f"Playing demo: {instrument_option_label(chosen)}"
                    continue
                proc, temp_midi, err = start_instrument_demo(chosen)
                if err:
                    status_msg = f"Demo failed (python: {py_err}; cli: {err})"
                else:
                    demo_proc = proc
                    demo_temp_midi = temp_midi
                    status_msg = f"Playing demo: {instrument_option_label(chosen)}"
                continue
            if allow_mapping and k in (ord("x"), ord("X")):
                if not filtered_options:
                    status_msg = "No matching instrument."
                    continue
                chosen = filtered_options[selected]
                pair = parse_instrument_pair(chosen)
                removed = [kk for kk, vv in list(mapping_ref.items()) if vv == pair]
                for kk in removed:
                    mapping_ref.pop(kk, None)
                    color_ref.pop(kk, None)
                status_msg = f"Cleared mapping for {instrument_option_label(chosen)} ({len(removed)} key(s))."
                continue
            if allow_mapping and k in (ord("m"), ord("M")):
                if not filtered_options:
                    status_msg = "No matching instrument."
                    continue
                chosen = filtered_options[selected]
                pair = parse_instrument_pair(chosen)
                typed = _edit_text_popup(stdscr, "Map key (single char, not q/s)", "")
                key_txt = typed.strip().lower()[:1]
                if not key_txt:
                    status_msg = "Mapping cancelled."
                    continue
                if key_txt in {"q", "s"}:
                    status_msg = "Reserved keys: q and s cannot be mapped."
                    continue
                old = mapping_ref.get(key_txt)
                mapping_ref[key_txt] = pair
                if old == pair:
                    status_msg = f"Key '{key_txt}' already mapped to {instrument_option_label(chosen)}."
                elif old is None:
                    status_msg = f"Mapped '{key_txt}' -> {instrument_option_label(chosen)}."
                else:
                    status_msg = f"Remapped '{key_txt}' -> {instrument_option_label(chosen)}."
                if key_txt not in color_ref:
                    color_ref[key_txt] = default_live_hotkey_color(key_txt)
                continue
            if allow_mapping and k in (ord("c"), ord("C")):
                if not filtered_options:
                    status_msg = "No matching instrument."
                    continue
                chosen = filtered_options[selected]
                pair = parse_instrument_pair(chosen)
                mapped_keys = sorted([kk for kk, vv in mapping_ref.items() if vv == pair])
                if not mapped_keys:
                    status_msg = "Map a key first with 'm'."
                    continue
                if len(mapped_keys) == 1:
                    key_txt = mapped_keys[0]
                else:
                    typed_key = _edit_text_popup(
                        stdscr,
                        f"Color key ({''.join(mapped_keys)})",
                        mapped_keys[0],
                    ).strip().lower()[:1]
                    if not typed_key:
                        status_msg = "Color edit cancelled."
                        continue
                    if typed_key not in mapped_keys:
                        status_msg = f"Key '{typed_key}' is not mapped to this instrument."
                        continue
                    key_txt = typed_key
                cur_rgb = color_ref.get(key_txt, default_live_hotkey_color(key_txt))
                typed_rgb = _edit_text_popup(stdscr, f"Color for key '{key_txt}' (R,G,B)", rgb_triplet_to_text(cur_rgb))
                if typed_rgb is None:
                    status_msg = "Color edit cancelled."
                    continue
                typed_rgb = typed_rgb.strip()
                if not typed_rgb:
                    color_ref[key_txt] = default_live_hotkey_color(key_txt)
                    status_msg = f"Reset key '{key_txt}' color to default."
                    continue
                color_ref[key_txt] = parse_rgb_triplet(typed_rgb, cur_rgb)
                status_msg = f"Updated key '{key_txt}' color."
                continue
            if k in (10, 13, curses.KEY_ENTER):
                if not filtered_options:
                    status_msg = "No matching instrument to select."
                    continue
                stop_demo(close_player=True)
                return filtered_options[selected]

    def choose_track_customization(stdscr: "curses._CursesWindow") -> bool:
        if not track_entries:
            return False
        instrument_options = get_instrument_options()
        track_instr = dict(state.get("track_instruments", {}))
        track_colors = dict(state.get("track_colors", {}))
        track_volumes = _normalize_saved_track_volume_map(state.get("track_volumes", {}))
        ansi_track_colors = normalize_color_mode(str(state["track_color_mode"])) == "ansi16"
        if ansi_track_colors:
            for track_idx, tr in enumerate(track_entries):
                tkey = str(tr.index)
                raw_rgb = parse_rgb_triplet(
                    track_colors.get(tkey, ""),
                    default_ansi16_track_color(track_idx),
                )
                if tkey in track_colors:
                    track_colors[tkey] = rgb_triplet_to_text(nearest_ansi16_track_color(raw_rgb))
                else:
                    track_colors[tkey] = rgb_triplet_to_text(random.choice(ANSI16_AUTO_TRACK_COLORS)[1])
        selected_line = 0
        offset_line = 0
        default_instrument = str(state["instrument"])
        status_msg = ""
        total_lines = len(track_entries) * 4
        pair_cache: Dict[Tuple[int, int], int] = {}
        next_pair = 1

        def auto_assign_track_instruments() -> Tuple[int, int]:
            named_options = [opt for opt in instrument_options if _instrument_option_name(opt)]
            if not named_options:
                return 0, 0
            matched = 0
            changed = 0
            for tr in track_entries:
                tkey = str(tr.index)
                candidates: List[str] = []
                inst_hint = str(getattr(tr, "instrument_name", "")).strip()
                if inst_hint:
                    candidates.append(inst_hint)
                name_hint = str(getattr(tr, "name", "")).strip()
                if name_hint and (name_hint.lower() != f"track {tr.index + 1}".lower()):
                    candidates.append(name_hint)

                best_opt: Optional[str] = None
                best_score = 0.0
                for query in candidates:
                    hit = _match_instrument_by_name(query, named_options)
                    if not hit:
                        continue
                    opt, score = hit
                    if score > best_score:
                        best_opt = opt
                        best_score = score
                if best_opt is None:
                    continue

                matched += 1
                before = track_instr.get(tkey)
                if best_opt == default_instrument:
                    if tkey in track_instr:
                        track_instr.pop(tkey, None)
                        changed += 1
                else:
                    if before != best_opt:
                        track_instr[tkey] = best_opt
                        changed += 1
            return matched, changed

        def randomizable_instrument_options() -> List[str]:
            default_sf = str(state.get("soundfont", "")).strip()
            soundfont_list = _normalize_saved_soundfont_library(
                state.get("soundfonts", []),
                primary_soundfont=default_sf,
            )
            valid_soundfonts = {
                _soundfont_identity(sf)
                for sf in soundfont_list
                if sf and os.path.exists(sf)
            }
            if not valid_soundfonts:
                return []

            options: List[str] = []
            seen: set[Tuple[str, int, int]] = set()
            for opt in instrument_options:
                sf, bank, program = parse_instrument_spec(opt, (0, 0), default_sf)
                sf_key = _soundfont_identity(sf)
                if not sf_key or sf_key not in valid_soundfonts:
                    continue
                ident = (sf_key, bank, program)
                if ident in seen:
                    continue
                seen.add(ident)
                options.append(opt)
            return options

        def random_assign_track_instruments() -> Tuple[int, int]:
            choices = randomizable_instrument_options()
            if not choices:
                return 0, 0

            changed = 0
            for tr in track_entries:
                tkey = str(tr.index)
                picked = random.choice(choices)
                before = track_instr.get(tkey)
                if picked == default_instrument:
                    if tkey in track_instr:
                        track_instr.pop(tkey, None)
                        changed += 1
                else:
                    if before != picked:
                        track_instr[tkey] = picked
                        changed += 1
            return len(track_entries), changed

        def rgb_to_term_color(rgb: Tuple[int, int, int]) -> int:
            r, g, b = rgb
            colors_count = int(getattr(curses, "COLORS", 0) or 0)
            if colors_count >= 256:
                if r == g == b:
                    if r < 8:
                        return 16
                    if r > 248:
                        return 231
                    return 232 + int(round(((r - 8) / 247.0) * 24))
                ri = int(round((r / 255.0) * 5))
                gi = int(round((g / 255.0) * 5))
                bi = int(round((b / 255.0) * 5))
                return 16 + (36 * ri) + (6 * gi) + bi
            # Coarse fallback for low-color terminals.
            if r >= g and r >= b:
                return curses.COLOR_RED
            if g >= r and g >= b:
                return curses.COLOR_GREEN
            if b >= r and b >= g:
                return curses.COLOR_BLUE
            return curses.COLOR_WHITE

        def color_cell_attr(rgb: Tuple[int, int, int], selected: bool) -> int:
            nonlocal next_pair
            if not curses.has_colors():
                return curses.A_BOLD if selected else curses.A_NORMAL
            colors_count = int(getattr(curses, "COLORS", 0) or 0)
            pairs_count = int(getattr(curses, "COLOR_PAIRS", 0) or 0)
            if colors_count <= 0 or pairs_count <= 1:
                return curses.A_BOLD if selected else curses.A_NORMAL
            try:
                bg = rgb_to_term_color(rgb)
                if bg >= colors_count:
                    bg = max(0, colors_count - 1)
                luminance = (0.2126 * rgb[0]) + (0.7152 * rgb[1]) + (0.0722 * rgb[2])
                fg = curses.COLOR_BLACK if luminance >= 150 else curses.COLOR_WHITE
                key = (fg, bg)
                pair_num = pair_cache.get(key)
                if pair_num is None:
                    if next_pair >= pairs_count:
                        return curses.A_BOLD if selected else curses.A_NORMAL
                    curses.init_pair(next_pair, fg, bg)
                    pair_num = next_pair
                    pair_cache[key] = pair_num
                    next_pair += 1
                attr = curses.color_pair(pair_num)
                if selected:
                    attr |= curses.A_BOLD
                return attr
            except Exception:
                return curses.A_BOLD if selected else curses.A_NORMAL

        def choose_ansi16_track_color(current_rgb: Tuple[int, int, int]) -> Optional[Tuple[int, int, int]]:
            selected = min(
                range(len(ANSI16_TRACK_COLORS)),
                key=lambda idx: sum(
                    (current_rgb[channel] - ANSI16_TRACK_COLORS[idx][1][channel]) ** 2 for channel in range(3)
                ),
            )
            while True:
                stdscr.erase()
                h, w = stdscr.getmaxyx()
                stdscr.box()
                title = " ANSI-16 Track Color "
                stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
                help_line = "Arrow keys move  Enter select  Esc cancel"
                stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
                for idx, (name, rgb) in enumerate(ANSI16_TRACK_COLORS):
                    col = idx % 2
                    row = idx // 2
                    x = 2 + col * max(18, (w - 4) // 2)
                    y = 4 + row
                    if y >= h - 1:
                        continue
                    token = f" {name:<14} "
                    attr = color_cell_attr(rgb, idx == selected)
                    if idx == selected:
                        attr |= curses.A_REVERSE
                    stdscr.addstr(y, x, token[: max(1, w - x - 2)], attr)
                stdscr.refresh()

                k = stdscr.getch()
                if k in (27, ord("q"), ord("Q")):
                    return None
                if k == curses.KEY_UP:
                    selected = (selected - 2) % len(ANSI16_TRACK_COLORS)
                    continue
                if k == curses.KEY_DOWN:
                    selected = (selected + 2) % len(ANSI16_TRACK_COLORS)
                    continue
                if k == curses.KEY_LEFT:
                    selected = selected - 1 if selected % 2 else selected + 1
                    continue
                if k == curses.KEY_RIGHT:
                    selected = selected + 1 if selected % 2 == 0 else selected - 1
                    continue
                if k in (10, 13, curses.KEY_ENTER):
                    return ANSI16_TRACK_COLORS[selected][1]

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Track Customization "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            midi_name = _fmt_choice(str(state["midi"]))
            stdscr.addstr(1, 2, f"MIDI: {midi_name}"[: max(1, w - 4)])
            help_line = "Up/Down move  Enter edit selected  Left/Right nudge volume  m auto-match  z randomize  r reset track  a reset all  s save  Esc cancel"
            stdscr.addstr(2, 2, help_line[: max(1, w - 4)])
            if status_msg:
                stdscr.addstr(3, 2, status_msg[: max(1, w - 4)], curses.A_DIM)

            list_top = 5
            list_h = max(1, h - list_top - 2)
            if selected_line < offset_line:
                offset_line = selected_line
            if selected_line >= offset_line + list_h:
                offset_line = selected_line - list_h + 1

            for row in range(list_h):
                line_idx = offset_line + row
                if line_idx >= total_lines:
                    break
                track_idx = line_idx // 4
                sub_row = line_idx % 4
                tr = track_entries[track_idx]
                key = str(tr.index)
                inst = track_instr.get(key, default_instrument)
                default_rgb = (
                    default_ansi16_track_color(track_idx)
                    if ansi_track_colors
                    else default_track_color(track_idx, len(track_entries))
                )
                color_txt = track_colors.get(key, rgb_triplet_to_text(default_rgb))
                volume = _clamp_track_volume(float(track_volumes.get(key, 1.0)))
                if sub_row == 0:
                    has_custom = (key in track_instr) or (key in track_colors) or (key in track_volumes)
                    suffix = "  *custom*" if has_custom else ""
                    inst_hint = str(getattr(tr, "instrument_name", "")).strip()
                    hint = f"  [inst: {inst_hint}]" if inst_hint else ""
                    label = f"Track {tr.index + 1:02d}: {tr.name} ({tr.note_ons} notes){hint}{suffix}"
                    attr = curses.A_REVERSE if line_idx == selected_line else curses.A_NORMAL
                    stdscr.addstr(list_top + row, 2, label[: max(1, w - 4)], attr)
                elif sub_row == 1:
                    label = f"-> Instrument: {instrument_option_label(inst)}"
                    attr = curses.A_REVERSE if line_idx == selected_line else curses.A_NORMAL
                    stdscr.addstr(list_top + row, 2, label[: max(1, w - 4)], attr)
                elif sub_row == 2:
                    selected = (line_idx == selected_line)
                    prefix = "-> Color: "
                    line_y = list_top + row
                    prefix_attr = curses.A_REVERSE if selected else curses.A_NORMAL
                    stdscr.addstr(line_y, 2, prefix[: max(1, w - 4)], prefix_attr)
                    available = max(0, (w - 4) - len(prefix))
                    if available > 0:
                        color_rgb = parse_rgb_triplet(color_txt, default_rgb)
                        token = f" {color_txt} "
                        if len(token) > available:
                            token = token[:available]
                        token_attr = color_cell_attr(color_rgb, selected)
                        stdscr.addstr(line_y, 2 + len(prefix), token, token_attr)
                else:
                    label = f"-> Volume: {_format_track_volume_text(volume)}"
                    attr = curses.A_REVERSE if line_idx == selected_line else curses.A_NORMAL
                    stdscr.addstr(list_top + row, 2, label[: max(1, w - 4)], attr)
            stdscr.refresh()

            k = stdscr.getch()
            if k in (27, ord("q"), ord("Q")):
                return False
            if k in (ord("s"), ord("S")):
                state["track_instruments"] = track_instr
                state["track_colors"] = track_colors
                state["track_volumes"] = track_volumes
                return True
            if k in (curses.KEY_UP,):
                selected_line = (selected_line - 1) % total_lines
                continue
            if k in (curses.KEY_DOWN,):
                selected_line = (selected_line + 1) % total_lines
                continue
            if k in (curses.KEY_PPAGE,):
                selected_line = max(0, selected_line - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                selected_line = min(total_lines - 1, selected_line + list_h)
                continue

            track_idx = selected_line // 4
            sub_row = selected_line % 4
            tr = track_entries[track_idx]
            tkey = str(tr.index)
            default_rgb = (
                default_ansi16_track_color(track_idx)
                if ansi_track_colors
                else default_track_color(track_idx, len(track_entries))
            )

            if k in (ord("r"), ord("R")):
                track_instr.pop(tkey, None)
                if ansi_track_colors:
                    track_colors[tkey] = rgb_triplet_to_text(random.choice(ANSI16_AUTO_TRACK_COLORS)[1])
                else:
                    track_colors.pop(tkey, None)
                track_volumes.pop(tkey, None)
                status_msg = f"Track {tr.index + 1} reset."
                continue

            if k in (ord("a"), ord("A")):
                track_instr.clear()
                if ansi_track_colors:
                    track_colors = {
                        str(track.index): rgb_triplet_to_text(random.choice(ANSI16_AUTO_TRACK_COLORS)[1])
                        for track in track_entries
                    }
                else:
                    track_colors.clear()
                track_volumes.clear()
                status_msg = "All track overrides reset."
                continue

            if k in (ord("m"), ord("M")):
                matched, changed = auto_assign_track_instruments()
                if matched <= 0:
                    status_msg = "No track instrument names matched any SoundFont preset."
                else:
                    status_msg = f"Auto-matched {matched} tracks from MIDI names ({changed} overrides changed)."
                continue

            if k in (ord("z"), ord("Z")):
                randomized, changed = random_assign_track_instruments()
                if randomized <= 0:
                    status_msg = "No selectable SoundFont presets available for random assignment."
                else:
                    status_msg = f"Randomized instruments for {randomized} tracks ({changed} overrides changed)."
                continue

            if k in (curses.KEY_LEFT, curses.KEY_RIGHT) and sub_row == 1:
                cur = track_instr.get(tkey, default_instrument)
                if cur not in instrument_options:
                    base_sf = instrument_option_soundfont(default_instrument, str(state.get("soundfont", "")))
                    cur_sf, cb, cp = parse_instrument_spec(cur, parse_instrument_pair(default_instrument), base_sf)
                    cur = format_instrument_option(cb, cp, soundfont_path=cur_sf)
                    if cur not in instrument_options:
                        instrument_options = instrument_options + [cur]
                opt_idx = instrument_options.index(cur) if cur in instrument_options else 0
                step = -1 if k == curses.KEY_LEFT else 1
                nxt = instrument_options[(opt_idx + step) % len(instrument_options)]
                if nxt == default_instrument:
                    track_instr.pop(tkey, None)
                else:
                    track_instr[tkey] = nxt
                status_msg = f"Track {tr.index + 1} instrument set."
                continue

            if k in (curses.KEY_LEFT, curses.KEY_RIGHT) and sub_row == 3:
                step = -0.05 if k == curses.KEY_LEFT else 0.05
                cur = _clamp_track_volume(float(track_volumes.get(tkey, 1.0)))
                nxt = _clamp_track_volume(cur + step)
                if abs(nxt - 1.0) <= 0.001:
                    track_volumes.pop(tkey, None)
                else:
                    track_volumes[tkey] = round(nxt, 3)
                status_msg = f"Track {tr.index + 1} volume set to {_format_track_volume_text(nxt)}."
                continue

            if k in (10, 13, curses.KEY_ENTER) and sub_row == 1:
                cur = track_instr.get(tkey, default_instrument)
                picked = choose_instrument(
                    stdscr,
                    current_value=cur,
                    title=f" Track {tr.index + 1} Instrument ",
                    options_override=instrument_options,
                )
                if picked is not None:
                    if picked == default_instrument:
                        track_instr.pop(tkey, None)
                    else:
                        track_instr[tkey] = picked
                    status_msg = f"Track {tr.index + 1} instrument updated."
                continue

            if k in (10, 13, curses.KEY_ENTER) and sub_row == 2:
                cur_txt = track_colors.get(tkey, rgb_triplet_to_text(default_rgb))
                if ansi_track_colors:
                    picked_rgb = choose_ansi16_track_color(parse_rgb_triplet(cur_txt, default_rgb))
                    if picked_rgb is not None:
                        track_colors[tkey] = rgb_triplet_to_text(picked_rgb)
                        status_msg = f"Track {tr.index + 1} ANSI color updated."
                else:
                    typed = _edit_text_popup(stdscr, f"Track {tr.index + 1} Color R,G,B", cur_txt)
                    if typed is not None:
                        typed = typed.strip()
                    if typed:
                        rgb = parse_rgb_triplet(typed, default_rgb)
                        txt = rgb_triplet_to_text(rgb)
                        if txt == rgb_triplet_to_text(default_rgb):
                            track_colors.pop(tkey, None)
                        else:
                            track_colors[tkey] = txt
                        status_msg = f"Track {tr.index + 1} color updated."
                    elif typed == "":
                        track_colors.pop(tkey, None)
                        status_msg = f"Track {tr.index + 1} color reset to default."
                continue
            if k in (10, 13, curses.KEY_ENTER) and sub_row == 3:
                cur_vol = _clamp_track_volume(float(track_volumes.get(tkey, 1.0)))
                typed = _edit_text_popup(stdscr, f"Track {tr.index + 1} Volume Multiplier", f"{cur_vol:.2f}")
                if typed is None:
                    status_msg = "Volume edit cancelled."
                    continue
                typed = typed.strip()
                if not typed:
                    track_volumes.pop(tkey, None)
                    status_msg = f"Track {tr.index + 1} volume reset to default."
                    continue
                try:
                    nxt = _clamp_track_volume(float(typed))
                except Exception:
                    status_msg = "Invalid volume value."
                    continue
                if abs(nxt - 1.0) <= 0.001:
                    track_volumes.pop(tkey, None)
                else:
                    track_volumes[tkey] = round(nxt, 3)
                status_msg = f"Track {tr.index + 1} volume set to {_format_track_volume_text(nxt)}."
                continue
            if k in (10, 13, curses.KEY_ENTER):
                status_msg = "Select an Instrument, Color, or Volume row to edit."
                continue

    def choose_glyph_editor(stdscr: "curses._CursesWindow") -> bool:
        glyph_items: List[Tuple[str, str]] = [
            ("glyph_note_white", "Note White"),
            ("glyph_note_black_left", "Note Black Left"),
            ("glyph_note_black_mid", "Note Black Mid"),
            ("glyph_note_black_right", "Note Black Right"),
            ("glyph_note_pedal_white", "Pedal White"),
            ("glyph_note_pedal_black_left", "Pedal Black Left"),
            ("glyph_note_pedal_black_mid", "Pedal Black Mid"),
            ("glyph_note_pedal_black_right", "Pedal Black Right"),
            ("glyph_piano_white", "Piano White"),
            ("glyph_piano_black_left", "Piano Black Left"),
            ("glyph_piano_black_mid", "Piano Black Mid"),
            ("glyph_piano_black_right", "Piano Black Right"),
        ]
        idx = 0
        off = 0
        changed = False
        status_msg = ""
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Glyph Configuration "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "Up/Down move  Enter edit  r reset item  a reset all  Esc back"
            stdscr.addstr(1, 2, help_line[: max(1, w - 4)])
            if status_msg:
                stdscr.addstr(2, 2, status_msg[: max(1, w - 4)], curses.A_DIM)
            top = 4
            list_h = max(1, h - top - 2)
            if idx < off:
                off = idx
            if idx >= off + list_h:
                off = idx - list_h + 1
            for row in range(list_h):
                item_idx = off + row
                if item_idx >= len(glyph_items):
                    break
                key, label = glyph_items[item_idx]
                glyph = str(state.get(key, DEFAULT_VISUAL_GLYPHS[key]))
                line = f"{label:<20} {glyph}"
                attr = curses.A_REVERSE if item_idx == idx else curses.A_NORMAL
                stdscr.addstr(top + row, 2, line[: max(1, w - 4)], attr)
            stdscr.refresh()

            k = stdscr.getch()
            if k in (27, ord("q"), ord("Q")):
                return changed
            if k == curses.KEY_UP:
                idx = (idx - 1) % len(glyph_items)
                continue
            if k == curses.KEY_DOWN:
                idx = (idx + 1) % len(glyph_items)
                continue
            if k in (curses.KEY_PPAGE,):
                idx = max(0, idx - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                idx = min(len(glyph_items) - 1, idx + list_h)
                continue
            if k in (ord("r"), ord("R")):
                key, _label = glyph_items[idx]
                state[key] = DEFAULT_VISUAL_GLYPHS[key]
                changed = True
                status_msg = "Reset selected glyph to default."
                continue
            if k in (ord("a"), ord("A")):
                for key, _label in glyph_items:
                    state[key] = DEFAULT_VISUAL_GLYPHS[key]
                changed = True
                status_msg = "Reset all glyphs to defaults."
                continue
            if k in (10, 13, curses.KEY_ENTER):
                key, label = glyph_items[idx]
                typed = _edit_text_popup(stdscr, label, str(state.get(key, DEFAULT_VISUAL_GLYPHS[key])))
                new_glyph = _sanitize_visual_glyph(typed, DEFAULT_VISUAL_GLYPHS[key])
                if str(state.get(key, "")) != new_glyph:
                    state[key] = new_glyph
                    changed = True
                    status_msg = f"Updated {label}."
                else:
                    status_msg = "No change."
                continue

    def choose_color_rendering(stdscr: "curses._CursesWindow") -> Optional[Dict[str, str]]:
        items = [
            ("note_color_mode", "Notes"),
            ("pedal_color_mode", "Pedal Tails"),
            ("piano_color_mode", "Piano"),
            ("track_color_mode", "Track Colors"),
            ("terminal_bg_color", "Terminal BG"),
        ]
        values = {
            "note_color_mode": normalize_color_mode(str(state["note_color_mode"])),
            "pedal_color_mode": normalize_color_mode(str(state["pedal_color_mode"])),
            "piano_color_mode": normalize_color_mode(str(state["piano_color_mode"])),
            "track_color_mode": normalize_color_mode(str(state["track_color_mode"])),
            "terminal_bg_color": str(state["terminal_bg_color"]).strip(),
        }
        selected = 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.box()
            title = " Color Rendering "
            stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
            help_line = "Up/Down move  Left/Right change colors  Enter change/edit  s save  Esc cancel"
            stdscr.addstr(1, 2, help_line[: max(1, w - 4)])
            stdscr.addstr(3, 2, "ANSI-16 uses your terminal theme palette."[: max(1, w - 4)], curses.A_DIM)
            for idx, (key, label) in enumerate(items):
                value = values[key]
                if key == "terminal_bg_color" and not value:
                    value = "auto"
                line = f"{label:<16} {value}"
                attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
                stdscr.addstr(5 + idx, 2, line[: max(1, w - 4)], attr)
            stdscr.refresh()

            k = stdscr.getch()
            if k in (27, ord("q"), ord("Q")):
                return None
            if k == curses.KEY_UP:
                selected = (selected - 1) % len(items)
                continue
            if k == curses.KEY_DOWN:
                selected = (selected + 1) % len(items)
                continue
            if k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                key, _label = items[selected]
                if key != "terminal_bg_color":
                    values[key] = "ansi16" if values[key] == "truecolor" else "truecolor"
                continue
            if k in (10, 13, curses.KEY_ENTER):
                key, _label = items[selected]
                if key == "terminal_bg_color":
                    values[key] = _edit_text_popup(stdscr, "Terminal BG (R,G,B; blank=auto)", values[key]).strip()
                else:
                    values[key] = "ansi16" if values[key] == "truecolor" else "truecolor"
                continue
            if k in (ord("s"), ord("S")):
                return values

    def apply_field_action(stdscr: "curses._CursesWindow", idx: int, direction: int) -> Optional[str]:
        nonlocal profile_selection
        field = fields[idx]
        key = str(field["key"])
        t = str(field["type"])
        if t == "button":
            if key == "__export_mp3__":
                persist_state()
                default_out = _default_export_mp3_path(str(state.get("midi", "")))
                typed = _edit_text_popup(stdscr, "Export MP3 Path", default_out)
                out_path = str(typed).strip() or default_out
                try:
                    export_args = argparse.Namespace(**{**vars(args), **build_saved_config_snapshot()})
                    saved_path = export_configured_midi_to_mp3(export_args, out_path, stdscr=stdscr)
                    _message_popup(stdscr, " Export MP3 ", f"Saved to:\n{saved_path}")
                except Exception as exc:
                    _message_popup(stdscr, " Export Failed ", str(exc))
                return None
            return "ok"
        if t == "profile_selector":
            if direction != 0:
                cycle_profile(direction)
                return None
            picked_idx = choose_profile_name(stdscr, profile_selection if profile_selection >= 0 else 0)
            if picked_idx is None:
                return None
            profile_selection = picked_idx
            snapshot = profile_entries[picked_idx].get("config", {})
            if isinstance(snapshot, dict):
                apply_saved_config_snapshot(dict(snapshot))
                persist_state()
            return None
        if t == "profile_save_button":
            if direction != 0:
                return None
            if 0 <= profile_selection < len(profile_entries):
                snapshot = build_saved_config_snapshot()
                profile_entries[profile_selection]["config"] = dict(snapshot)
            else:
                new_name = next_default_profile_name()
                profile_entries.append(
                    {
                        "name": new_name,
                        "config": {},
                    }
                )
                profile_selection = len(profile_entries) - 1
                snapshot = build_saved_config_snapshot()
                profile_entries[profile_selection]["config"] = dict(snapshot)
            save_config_profiles(profile_entries)
            persist_state()
            return None
        if t == "profile_delete_button":
            if direction != 0:
                return None
            if not (0 <= profile_selection < len(profile_entries)):
                return None
            profile_entries.pop(profile_selection)
            if not profile_entries:
                profile_selection = -1
            else:
                profile_selection = min(profile_selection, len(profile_entries) - 1)
            save_config_profiles(profile_entries)
            persist_state()
            return None
        if t == "profile_rename_button":
            if direction != 0:
                return None
            if not (0 <= profile_selection < len(profile_entries)):
                return None
            current_name = profile_name_at(profile_selection)
            typed = _edit_text_popup(stdscr, "Profile Name", current_name)
            new_name = str(typed).strip()
            if not new_name:
                return None
            profile_entries[profile_selection]["name"] = unique_profile_name(new_name, skip_index=profile_selection)
            snapshot = profile_entries[profile_selection].get("config", {})
            if isinstance(snapshot, dict):
                snapshot["selected_profile_name"] = profile_entries[profile_selection]["name"]
            save_config_profiles(profile_entries)
            persist_state()
            return None
        if t == "bool":
            state[key] = not bool(state[key])
            persist_state()
        elif t == "choice":
            options = list(field.get("options", []))
            prev_soundfont = str(state.get("soundfont", "")) if key == "soundfont" else ""
            cycle_choice(key, options, direction if direction else 1)
            if key == "soundfont":
                if str(state.get("soundfont", "")) != prev_soundfont:
                    # Force fresh hotkeys for the new SoundFont. An empty custom
                    # map makes live mode regenerate defaults from presets.
                    state["midi_input_instrument_map"] = ""
                    invalidate_instrument_options()
            persist_state()
        elif t == "midi_browser":
            if direction != 0:
                return None
            picked = choose_midi_file(stdscr, str(state[key]))
            if picked is not None:
                state[key] = picked
                state["start"] = 0.0
                invalidate_track_analysis()
                persist_state()
        elif t == "soundfont_manager_browser":
            if direction != 0:
                return None
            picked_lib = choose_soundfont_library(
                stdscr,
                primary_soundfont=str(state.get("soundfont", "")),
                current_items=list(_normalize_saved_soundfont_library(state.get("soundfonts", []))),
            )
            if picked_lib is not None:
                normalized = list(_normalize_saved_soundfont_library(picked_lib))
                state["soundfonts"] = normalized
                state["soundfont"] = normalized[0] if normalized else ""
                state["midi_input_instrument_map"] = ""
                invalidate_instrument_options()
                persist_state()
        elif t == "instrument_browser":
            if direction != 0:
                return None
            ensure_instrument_options_loaded(stdscr)
            picked = choose_instrument(stdscr, str(state[key]))
            if picked is not None:
                state[key] = picked
                persist_state()
        elif t == "audio_output_browser":
            if direction != 0:
                return None
            picked = choose_audio_output(stdscr, str(state.get(key, "")))
            if picked is not None:
                state[key] = picked
                persist_state()
        elif t == "live_instrument_map_browser":
            if direction != 0:
                return None
            ensure_instrument_options_loaded(stdscr)
            working_map = parse_live_instrument_map(str(state.get(key, "")))
            working_color_map = parse_live_color_map(str(state.get("midi_input_color_map", "")))
            picked = choose_instrument(
                stdscr,
                current_value=str(state.get("instrument", "")),
                title=" Live Instrument Mapping ",
                hotkey_map=working_map,
                hotkey_color_map=working_color_map,
                allow_mapping=True,
            )
            normalized_map = format_live_instrument_map(working_map)
            normalized_color_map = format_live_color_map(working_color_map)
            changed = False
            if str(state.get(key, "")) != normalized_map:
                state[key] = normalized_map
                changed = True
            if str(state.get("midi_input_color_map", "")) != normalized_color_map:
                state["midi_input_color_map"] = normalized_color_map
                changed = True
            if picked is not None and picked != str(state.get("instrument", "")):
                state["instrument"] = picked
                changed = True
            if changed:
                persist_state()
        elif t == "track_customization_browser":
            if direction != 0:
                return None
            ensure_track_entries_loaded(stdscr)
            ensure_instrument_options_loaded(stdscr)
            if choose_track_customization(stdscr):
                persist_state()
        elif t == "glyph_editor":
            if direction != 0:
                return None
            if choose_glyph_editor(stdscr):
                persist_state()
        elif t == "color_rendering_editor":
            if direction != 0:
                return None
            modes = choose_color_rendering(stdscr)
            if modes is not None:
                for mode_key, mode_value in modes.items():
                    if mode_key == "terminal_bg_color":
                        state[mode_key] = mode_value.strip()
                    else:
                        state[mode_key] = normalize_color_mode(mode_value)
                persist_state()
        elif t == "offpiano_pattern_editor":
            if direction != 0:
                return None
            picked = choose_offpiano_pattern(stdscr, str(state.get(key, "")))
            if picked is not None:
                state[key] = picked
                persist_state()
        elif t in {"int", "float"}:
            if direction != 0:
                nudge_numeric(field, direction)
            else:
                typed = _edit_text_popup(stdscr, str(field["label"]), str(state[key]))
                set_numeric_from_text(field, typed)
        elif t == "text":
            typed = _edit_text_popup(stdscr, str(field["label"]), str(state[key]))
            if key in DEFAULT_VISUAL_GLYPHS:
                state[key] = _sanitize_visual_glyph(typed, DEFAULT_VISUAL_GLYPHS[key])
            elif key == "terminal_bg_color":
                state[key] = str(typed).strip()
            else:
                state[key] = typed
            persist_state()
        return None

    collapsed_sections: set[str] = set()
    search_text = ""
    search_editing = False

    def build_menu_items() -> List[Dict[str, object]]:
        needle = search_text.casefold()
        section_order: List[str] = []
        section_fields: Dict[str, List[int]] = {}
        for field_idx, field in enumerate(fields):
            section = str(field.get("section", "")).strip() or "General"
            if section not in section_fields:
                section_fields[section] = []
                section_order.append(section)
            section_fields[section].append(field_idx)
        items: List[Dict[str, object]] = []
        for section in section_order:
            field_indices = section_fields[section]
            section_matches = bool(needle) and needle in section.casefold()
            visible_field_indices = [
                field_idx
                for field_idx in field_indices
                if not needle
                or section_matches
                or needle in str(fields[field_idx]["key"]).casefold()
                or needle in str(fields[field_idx]["label"]).casefold()
            ]
            if needle and not visible_field_indices:
                continue
            count = len(field_indices)
            if section == "Profile":
                count = len(profile_entries)
            items.append(
                {
                    "kind": "section",
                    "section": section,
                    "count": count,
                }
            )
            if section in collapsed_sections and not needle:
                continue
            for field_idx in visible_field_indices:
                items.append({"kind": "field", "field_idx": field_idx})
        if needle and not items:
            items.append({"kind": "empty"})
        return items

    def draw(
        stdscr: "curses._CursesWindow",
        menu_items: Sequence[Dict[str, object]],
        selected_idx: int,
        scroll_top: int,
    ) -> Tuple[int, int]:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.box()
        title = " Synthermia Config "
        stdscr.addstr(0, max(2, (w - len(title)) // 2), title, curses.A_BOLD)
        help_line = "Arrows: navigate  Enter: edit/toggle  /: search  p: play  q: quit"
        stdscr.addstr(1, 2, help_line[: max(1, w - 4)])
        search_value = f"{search_text}_" if search_editing else (search_text or "<all>")
        search_help = "Esc clears" if search_text else "Enter on sections collapses/expands"
        stdscr.addstr(2, 2, f"Search: {search_value}  {search_help}"[: max(1, w - 4)])
        list_top = 4
        list_h = max(1, h - list_top - 1)
        total = len(menu_items)
        if total <= 0:
            stdscr.refresh()
            return 0, list_h

        max_top = max(0, total - list_h)
        if selected_idx < scroll_top:
            scroll_top = selected_idx
        if selected_idx >= scroll_top + list_h:
            scroll_top = selected_idx - list_h + 1
        scroll_top = max(0, min(scroll_top, max_top))

        for row in range(list_h):
            menu_idx = scroll_top + row
            if menu_idx >= total:
                break
            item = menu_items[menu_idx]
            kind = str(item.get("kind", ""))
            y = list_top + row
            if kind == "section":
                section = str(item.get("section", "General"))
                count = int(item.get("count", 0))
                collapsed = section in collapsed_sections
                prefix = f"[{'+' if collapsed else '-'}] {section} ({count})"
                available = max(1, w - 4)
                if len(prefix) < available:
                    line = prefix + " " + ("-" * max(0, available - len(prefix) - 1))
                else:
                    line = prefix
                attr = curses.A_REVERSE | curses.A_BOLD if menu_idx == selected_idx else curses.A_BOLD
                stdscr.addstr(y, 2, line[: max(1, w - 4)], attr)
                continue
            if kind == "empty":
                stdscr.addstr(y, 2, "  (no settings match)"[: max(1, w - 4)], curses.A_DIM)
                continue

            field_idx = int(item.get("field_idx", 0))
            field = fields[field_idx]
            label = str(field["label"])
            t = str(field["type"])
            key = str(field["key"])
            if t == "button":
                line = f"  {label}"
            else:
                value = val_display(field)
                if key == "midi" and bool(state.get("midi_input_mode", False)):
                    value = "(ignored in current mode)"
                line = f"  {label:<20} {value}"
            attr = curses.A_REVERSE if menu_idx == selected_idx else curses.A_NORMAL
            stdscr.addstr(y, 2, line[: max(1, w - 4)], attr)

        stdscr.refresh()
        return scroll_top, list_h

    def tui(stdscr: "curses._CursesWindow") -> bool:
        nonlocal search_editing, search_text
        curses.set_escdelay(50)
        curses.curs_set(0)
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
            except curses.error:
                pass
        try:
            stdscr.keypad(True)
        except Exception:
            pass
        selected_idx = 0
        scroll_top = 0
        while True:
            menu_items = build_menu_items()
            if not menu_items:
                return False
            selected_idx = max(0, min(selected_idx, len(menu_items) - 1))
            scroll_top, list_h = draw(stdscr, menu_items, selected_idx, scroll_top)
            k = stdscr.getch()
            if search_editing:
                if k in (10, 13, curses.KEY_ENTER):
                    search_editing = False
                    continue
                if k == 27:
                    search_text = ""
                    search_editing = False
                    selected_idx = 0
                    scroll_top = 0
                    continue
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    search_text = search_text[:-1]
                    selected_idx = 0
                    scroll_top = 0
                    continue
                if 32 <= k <= 126:
                    search_text += chr(k)
                    selected_idx = 0
                    scroll_top = 0
                continue
            if k in (ord("q"), ord("Q")):
                return False
            if k == ord("/"):
                search_text = ""
                search_editing = True
                selected_idx = 0
                scroll_top = 0
                continue
            if k == 27 and search_text:
                search_text = ""
                selected_idx = 0
                scroll_top = 0
                continue
            if k in (ord("p"), ord("P")):
                persist_state()
                return True
            if k in (curses.KEY_UP,):
                selected_idx = (selected_idx - 1) % len(menu_items)
                continue
            if k in (curses.KEY_DOWN,):
                selected_idx = (selected_idx + 1) % len(menu_items)
                continue
            if k in (curses.KEY_PPAGE,):
                selected_idx = max(0, selected_idx - list_h)
                continue
            if k in (curses.KEY_NPAGE,):
                selected_idx = min(len(menu_items) - 1, selected_idx + list_h)
                continue
            if k in (curses.KEY_HOME,):
                selected_idx = 0
                continue
            if k in (curses.KEY_END,):
                selected_idx = len(menu_items) - 1
                continue
            item = menu_items[selected_idx]
            kind = str(item.get("kind", ""))
            if kind == "empty":
                continue
            if kind == "section":
                section = str(item.get("section", "General"))
                if k in (10, 13, curses.KEY_ENTER):
                    if section in collapsed_sections:
                        collapsed_sections.discard(section)
                    else:
                        collapsed_sections.add(section)
                    continue
                if k in (curses.KEY_LEFT,):
                    collapsed_sections.add(section)
                    continue
                if k in (curses.KEY_RIGHT,):
                    collapsed_sections.discard(section)
                    continue
                continue

            field_idx = int(item.get("field_idx", 0))
            if k in (curses.KEY_LEFT,):
                res = apply_field_action(stdscr, field_idx, -1)
                if res == "ok":
                    return True
                continue
            if k in (curses.KEY_RIGHT,):
                res = apply_field_action(stdscr, field_idx, 1)
                if res == "ok":
                    return True
                continue
            if k in (10, 13, curses.KEY_ENTER):
                res = apply_field_action(stdscr, field_idx, 0)
                if res == "ok":
                    return True
                continue
            if 32 <= k <= 126:
                field = fields[field_idx]
                key = str(field["key"])
                t = str(field["type"])
                ch = chr(k)
                if t == "bool" and ch in "01":
                    state[key] = (ch == "1")
                    persist_state()
                    continue
                if t in {"int", "float"} and ch in "0123456789.-":
                    typed = _edit_text_popup(stdscr, str(field["label"]), ch)
                    set_numeric_from_text(field, typed if typed else ch)
                    continue

    accepted = curses.wrapper(tui)
    if not accepted:
        raise KeyboardInterrupt()
    for key, value in build_saved_config_snapshot().items():
        if key != "selected_profile_name":
            setattr(args, key, value)
    return args



def _default_export_mp3_path(midi_path: str) -> str:
    src = str(midi_path or "").strip()
    if not src:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(".", f"synthermia_export_{ts}.mp3")
    base_dir = os.path.dirname(os.path.abspath(src)) or "."
    stem = os.path.splitext(os.path.basename(src))[0] or "synthermia_export"
    return os.path.join(base_dir, f"{stem}.mp3")


def _run_export_process(
    cmd: List[str],
    stdscr: Optional["curses._CursesWindow"],
    message: str,
    detail: str = "",
) -> None:
    # A file keeps progress polling independent of pipe buffer capacity.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            phase = 0
            while proc.poll() is None:
                if stdscr is not None:
                    _draw_progress_popup(stdscr, " Export MP3 ", message, phase=phase, detail=detail)
                phase += 1
                time.sleep(0.12)
            if proc.returncode != 0:
                log.seek(0)
                raise ValueError(f"{os.path.basename(cmd[0])} failed ({proc.returncode}): {log.read().strip()}")
        finally:
            stop_process(proc)


def export_configured_midi_to_mp3(
    args: argparse.Namespace,
    output_path: str,
    stdscr: Optional["curses._CursesWindow"] = None,
) -> str:
    from synthermia.playback import create_demo_midi_temp

    midi_path = str(getattr(args, "midi", "")).strip()
    soundfont_path = str(getattr(args, "soundfont", "")).strip()
    out_path = os.path.abspath(str(output_path or "").strip())
    if not midi_path:
        raise ValueError("Select a MIDI file first.")
    if not os.path.exists(midi_path):
        raise ValueError(f"MIDI file not found: {midi_path}")
    if not soundfont_path:
        raise ValueError("Select a SoundFont first.")
    if not os.path.exists(soundfont_path):
        raise ValueError(f"SoundFont file not found: {soundfont_path}")

    fluidsynth_bin = shutil.which(str(getattr(args, "fluidsynth_bin", "fluidsynth")))
    if not fluidsynth_bin:
        raise ValueError(f"fluidsynth executable not found: {getattr(args, 'fluidsynth_bin', 'fluidsynth')}")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise ValueError("ffmpeg executable not found.")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    spans, controls, _measure_times, _diag = load_playback_source(
        midi_path,
        stuck_note_fix_mode=str(getattr(args, "stuck_note_fix", "auto")),
        midi_flip=bool(getattr(args, "midi_flip", False)),
        flip_bounds=resolve_midi_flip_bounds(str(getattr(args, "range", ""))),
        long_note_threshold=float(getattr(args, "long_note_threshold", 0.0)),
        long_note_duration=float(getattr(args, "long_note_duration", 0.0)),
    )
    if not spans:
        raise ValueError("No playable notes found in MIDI")
    if bool(getattr(args, "disable_pedal", False)):
        controls = [ev for ev in controls if int(getattr(ev, "controller", -1)) != 64]

    default_program = _clamp_instrument_pair(
        int(getattr(args, "instrument_bank", 0) or 0),
        int(getattr(args, "instrument_program", 0) or 0),
    )
    soundfont_paths = _normalize_saved_soundfont_library(
        getattr(args, "soundfonts", []),
        primary_soundfont=soundfont_path,
    )
    valid_pairs_by_sf = _build_soundfont_pair_map(soundfont_paths)
    track_programs = _build_track_program_map(
        spans,
        default_program,
        soundfont_path,
        getattr(args, "track_instruments", {}),
        valid_pairs_by_sf=valid_pairs_by_sf,
    )
    audio_spans, channel_programs, channel_soundfonts, source_channels = _assign_audio_channels(
        spans,
        track_programs,
    )
    audio_spans = _apply_track_volume_overrides(audio_spans, getattr(args, "track_volumes", {}))
    audio_controls = _assign_control_channels(controls, source_channels)

    if stdscr is not None:
        _draw_progress_popup(
            stdscr,
            " Export MP3 ",
            "Step 1/3: Building render stems",
            phase=0,
        )

    channels_by_sf: Dict[str, set[int]] = {}
    for ch, sf in channel_soundfonts.items():
        sf_txt = str(sf or soundfont_path).strip() or soundfont_path
        channels_by_sf.setdefault(sf_txt, set()).add(int(ch))

    stem_specs: List[Tuple[str, List[NoteSpan], Dict[int, Tuple[int, int]], List[ControlEvent]]] = []
    for sf, chs in channels_by_sf.items():
        stem_spans = [sp for sp in audio_spans if int(sp.channel) in chs]
        stem_controls = [ev for ev in audio_controls if int(ev.channel) in chs]
        stem_programs = {ch: channel_programs[ch] for ch in sorted(chs) if ch in channel_programs}
        if not stem_spans and not stem_controls:
            continue
        stem_specs.append((sf, stem_spans, stem_programs, stem_controls))
    if not stem_specs:
        raise ValueError("No playable audio after track routing/volume filters.")

    temp_midis: List[str] = []
    temp_wavs: List[str] = []
    try:
        total_stems = len(stem_specs)
        for idx, (stem_sf, stem_spans, stem_programs, stem_controls) in enumerate(stem_specs, start=1):
            if stdscr is not None:
                _draw_progress_popup(
                    stdscr,
                    " Export MP3 ",
                    f"Step 2/3: Rendering stem {idx}/{total_stems}",
                    phase=idx,
                    detail=os.path.basename(stem_sf) or stem_sf,
                )
            stem_midi = create_demo_midi_temp(
                stem_spans,
                channel_programs=stem_programs,
                control_events=stem_controls,
                insert_warmup_pulses=False,
            )
            temp_midis.append(stem_midi)
            fd_wav, stem_wav = tempfile.mkstemp(prefix="synthermia_export_", suffix=".wav")
            os.close(fd_wav)
            temp_wavs.append(stem_wav)

            fs_cmd = [
                fluidsynth_bin,
                "-ni",
                "-g",
                str(float(getattr(args, "audio_gain", 0.6))),
                "-F",
                stem_wav,
                "-T",
                "wav",
            ]
            if int(getattr(args, "audio_samplerate", 0) or 0) > 0:
                fs_cmd.extend(["-r", str(int(getattr(args, "audio_samplerate", 0)))])
            if int(getattr(args, "audio_polyphony", 0) or 0) > 0:
                fs_cmd.extend(["-o", f"synth.polyphony={int(getattr(args, 'audio_polyphony', 0))}"])
            fs_cmd.extend([stem_sf, stem_midi])
            _run_export_process(
                fs_cmd, stdscr, f"Step 2/3: Rendering stem {idx}/{total_stems}",
                os.path.basename(stem_sf) or stem_sf,
            )

        if stdscr is not None:
            _draw_progress_popup(
                stdscr,
                " Export MP3 ",
                "Step 3/3: Encoding MP3",
                phase=1,
            )
        ffmpeg_cmd: List[str] = [ffmpeg_bin, "-y"]
        for wav in temp_wavs:
            ffmpeg_cmd.extend(["-i", wav])
        if len(temp_wavs) == 1:
            ffmpeg_cmd.extend(["-codec:a", "libmp3lame", "-q:a", "2", out_path])
        else:
            ffmpeg_cmd.extend(
                [
                    "-filter_complex",
                    f"amix=inputs={len(temp_wavs)}:normalize=0",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    out_path,
                ]
            )
        _run_export_process(ffmpeg_cmd, stdscr, "Step 3/3: Encoding MP3 with ffmpeg")
        if stdscr is not None:
            _draw_progress_popup(
                stdscr,
                " Export MP3 ",
                f"Done.\nSaved to:\n{out_path}",
                phase=0,
            )
        return out_path
    finally:
        for tmp_midi in temp_midis:
            if os.path.exists(tmp_midi):
                os.unlink(tmp_midi)
        for tmp_wav in temp_wavs:
            if os.path.exists(tmp_wav):
                os.unlink(tmp_wav)
