from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from synthermia.models import CONFIG_FILE, MIN_PIANO_NOTE, MAX_PIANO_NOTE
from synthermia.soundfont import (
    _clamp_instrument_pair,
    _normalize_saved_soundfont_library,
    format_instrument_option,
    parse_instrument_pair,
    scan_soundfont_presets,
)


def _clamp_rgb_triplet(r: int, g: int, b: int) -> Tuple[int, int, int]:
    return (
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    )


DEFAULT_VISUAL_GLYPHS: Dict[str, str] = {
    "glyph_note_white": "█",
    "glyph_note_pedal_white": "▒",
    "glyph_note_pedal_black_left": "▐",
    "glyph_note_pedal_black_mid": "▒",
    "glyph_note_pedal_black_right": "▌",
    "glyph_note_black_left": "▐",
    "glyph_note_black_mid": "█",
    "glyph_note_black_right": "▌",
    "glyph_piano_white": "█",
    "glyph_piano_black_left": "▐",
    "glyph_piano_black_mid": "█",
    "glyph_piano_black_right": "▌",
}


def _sanitize_visual_glyph(value: object, default: str) -> str:
    txt = str(value or "")
    if not txt:
        return default
    return txt[0]


def normalize_visual_glyphs(values: Optional[Dict[str, object]]) -> Dict[str, str]:
    src = values or {}
    out: Dict[str, str] = {}
    for key, default in DEFAULT_VISUAL_GLYPHS.items():
        out[key] = _sanitize_visual_glyph(src.get(key, default), default)
    legacy = _sanitize_visual_glyph(src.get("glyph_note_pedal", ""), "")
    for key in (
        "glyph_note_pedal_white",
        "glyph_note_pedal_black_left",
        "glyph_note_pedal_black_mid",
        "glyph_note_pedal_black_right",
    ):
        raw = str(src.get(key, "") or "").strip()
        if legacy and not raw:
            out[key] = legacy
    return out


def resolve_visual_glyphs(args: argparse.Namespace) -> Dict[str, str]:
    raw = {key: getattr(args, key, default) for key, default in DEFAULT_VISUAL_GLYPHS.items()}
    raw["glyph_note_pedal"] = getattr(args, "glyph_note_pedal", "")
    return normalize_visual_glyphs(raw)


def normalize_pressed_key_color_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"off", "none", "disable", "disabled"}:
        return "off"
    if mode in {"weighted", "mix", "dynamic", "dynamics", "velocity"}:
        return "weighted"
    return "split"


def _normalize_saved_stuck_note_fix_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"off", "none", "disable", "disabled"}:
        return "off"
    if mode in {"lifo", "stack", "recent", "newest"}:
        return "lifo"
    if mode in {"auto", "on", "enabled", "retrigger", "trim", "guard"}:
        return "auto"
    return "off"


def default_track_color(track_idx: int, total_tracks: int) -> Tuple[int, int, int]:
    total = max(1, int(total_tracks))
    h = (track_idx % total) / float(total)
    r_f, g_f, b_f = colorsys.hsv_to_rgb(h, 0.75, 0.96)
    return _clamp_rgb_triplet(int(r_f * 255), int(g_f * 255), int(b_f * 255))


def scan_files_by_ext(extensions: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__"}]
        for name in files:
            lower = name.lower()
            if lower.endswith(extensions):
                rel = os.path.relpath(os.path.join(root, name), ".")
                out.append(rel)
    out.sort(key=lambda s: s.lower())
    return out


def load_last_config(path: str = CONFIG_FILE) -> Dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_last_config(cfg: Dict[str, object], path: str = CONFIG_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _normalize_saved_track_map(raw: object) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            idx = int(str(k).strip())
        except Exception:
            continue
        if idx < 0:
            continue
        val = str(v).strip()
        if not val:
            continue
        out[str(idx)] = val
    return out


def _clamp_track_volume(value: float) -> float:
    try:
        v = float(value)
    except Exception:
        v = 1.0
    if not math.isfinite(v):
        v = 1.0
    return max(0.0, min(4.0, v))


def _normalize_saved_track_volume_map(raw: object) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            idx = int(str(k).strip())
        except Exception:
            continue
        if idx < 0:
            continue
        vol = _clamp_track_volume(float(v))
        if abs(vol - 1.0) <= 1e-6:
            continue
        out[str(idx)] = round(vol, 3)
    return out


def _format_track_volume_text(volume: float) -> str:
    vol = _clamp_track_volume(volume)
    pct = int(round(vol * 100.0))
    return f"{vol:.2f}x ({pct}%)"


def parse_live_instrument_map(value: str) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    txt = str(value or "").strip()
    if not txt:
        return out
    for part in txt.split(","):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        lhs, rhs = chunk.split("=", 1)
        key = lhs.strip()
        if len(key) != 1:
            continue
        pair = parse_instrument_pair(rhs.strip(), (0, 0))
        out[key.lower()] = pair
    return out


def default_live_hotkey_color(key: str) -> Tuple[int, int, int]:
    ch = (str(key).strip().lower() or "?")[0]
    h = ((ord(ch) * 0.61803398875) % 1.0)
    r_f, g_f, b_f = colorsys.hsv_to_rgb(h, 0.72, 0.96)
    return _clamp_rgb_triplet(int(r_f * 255), int(g_f * 255), int(b_f * 255))


def parse_live_color_map(value: str) -> Dict[str, Tuple[int, int, int]]:
    out: Dict[str, Tuple[int, int, int]] = {}
    txt = str(value or "").strip()
    if not txt:
        return out
    pat = re.compile(r"([^\s=,;])\s*=\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
    for m in pat.finditer(txt):
        key = m.group(1).strip().lower()
        out[key] = _clamp_rgb_triplet(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return out


def format_live_color_map(value: Dict[str, Tuple[int, int, int]]) -> str:
    parts: List[str] = []
    for key in sorted(value.keys()):
        rgb = value[key]
        r, g, b = _clamp_rgb_triplet(rgb[0], rgb[1], rgb[2])
        parts.append(f"{key}={r},{g},{b}")
    return ";".join(parts)


def format_live_instrument_map(value: Dict[str, Tuple[int, int]]) -> str:
    parts: List[str] = []
    for key in sorted(value.keys()):
        pair = value[key]
        b, p = _clamp_instrument_pair(pair[0], pair[1])
        parts.append(f"{key}={b}:{p}")
    return ",".join(parts)


def build_live_instrument_hotkey_map(args: argparse.Namespace) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, str]]:
    custom = parse_live_instrument_map(str(getattr(args, "midi_input_instrument_map", "")))
    labels: Dict[str, str] = {}
    if custom:
        for key, pair in custom.items():
            labels[key] = format_instrument_option(pair[0], pair[1])
        return custom, labels

    soundfont = str(getattr(args, "soundfont", "")).strip()
    if not soundfont:
        return {}, {}
    try:
        presets = scan_soundfont_presets(soundfont)
    except Exception:
        return {}, {}
    if not presets:
        return {}, {}
    hotkeys = "1234567890"
    out: Dict[str, Tuple[int, int]] = {}
    for key, opt in zip(hotkeys, presets):
        b, p = parse_instrument_pair(opt, (0, 0))
        out[key] = (b, p)
        labels[key] = opt
    return out, labels


def normalize_note_range_text(value: str) -> str:
    txt = str(value or "").strip()
    if not txt:
        return ""
    parts = txt.split(":", 1)
    if len(parts) != 2:
        return ""
    try:
        lo = int(float(parts[0].strip()))
        hi = int(float(parts[1].strip()))
    except Exception:
        return ""
    lo = max(MIN_PIANO_NOTE, min(MAX_PIANO_NOTE, lo))
    hi = max(MIN_PIANO_NOTE, min(MAX_PIANO_NOTE, hi))
    if lo > hi:
        lo, hi = hi, lo
    return f"{lo}:{hi}"


def parse_note_range_text(value: str) -> Optional[Tuple[int, int]]:
    normalized = normalize_note_range_text(value)
    if not normalized:
        return None
    lo_txt, hi_txt = normalized.split(":", 1)
    return int(lo_txt), int(hi_txt)


def resolve_midi_flip_bounds(range_text: str) -> Tuple[int, int]:
    forced = parse_note_range_text(range_text)
    if forced is not None:
        return forced
    return MIN_PIANO_NOTE, MAX_PIANO_NOTE


def playback_start_upper_bound(duration_sec: float, speed: float, fps: int) -> float:
    dur = max(0.0, float(duration_sec))
    spd = max(0.1, float(speed))
    frame_rate = max(1, int(fps))
    source_frame_step = max(0.001, spd / float(frame_rate))
    return max(0.0, dur - source_frame_step)


def clamp_playback_start(start_sec: float, duration_sec: float, speed: float, fps: int) -> float:
    start = max(0.0, float(start_sec))
    max_start = playback_start_upper_bound(duration_sec, speed, fps)
    if start > max_start:
        return max_start
    return start


def args_to_saved_config(args: argparse.Namespace, selected_midi: str) -> Dict[str, object]:
    inst_bank, inst_prog = _clamp_instrument_pair(
        int(getattr(args, "instrument_bank", 0)),
        int(getattr(args, "instrument_program", 0)),
    )
    instrument_text = str(getattr(args, "instrument", "")).strip()
    if not instrument_text:
        instrument_text = format_instrument_option(
            inst_bank,
            inst_prog,
            soundfont_path=str(getattr(args, "soundfont", "")),
        )
    saved_track_instruments = _normalize_saved_track_map(getattr(args, "track_instruments", {}))
    saved_track_colors = _normalize_saved_track_map(getattr(args, "track_colors", {}))
    saved_track_volumes = _normalize_saved_track_volume_map(getattr(args, "track_volumes", {}))
    saved_soundfonts = _normalize_saved_soundfont_library(
        getattr(args, "soundfonts", []),
        primary_soundfont=str(getattr(args, "soundfont", "")),
    )
    return {
        "midi_input_mode": bool(getattr(args, "midi_input_mode", False)),
        "midi_input_port": str(getattr(args, "midi_input_port", "")),
        "midi_input_save_dir": str(getattr(args, "midi_input_save_dir", "midis")),
        "midi_input_invert_pedal": bool(getattr(args, "midi_input_invert_pedal", False)),
        "midi_input_instrument_map": str(getattr(args, "midi_input_instrument_map", "")),
        "midi_input_color_map": str(getattr(args, "midi_input_color_map", "")),
        "midi": selected_midi,
        "soundfont": args.soundfont,
        "soundfonts": saved_soundfonts,
        "instrument": instrument_text,
        "instrument_bank": inst_bank,
        "instrument_program": inst_prog,
        "track_instruments": saved_track_instruments,
        "track_colors": saved_track_colors,
        "track_volumes": saved_track_volumes,
        "audio_engine": args.audio_engine,
        "playback_engine": str(getattr(args, "playback_engine", "auto")),
        "fluidsynth_bin": args.fluidsynth_bin,
        "audio_driver": args.audio_driver,
        "audio_output": str(getattr(args, "audio_output", "")),
        "audio_gain": args.audio_gain,
        "audio_period_size": args.audio_period_size,
        "audio_periods": args.audio_periods,
        "audio_samplerate": args.audio_samplerate,
        "fps": args.fps,
        "rows": args.rows,
        "x_scale": args.x_scale,
        "kb_y_scale": args.kb_y_scale,
        "kb_black_extra": args.kb_black_extra,
        "dynamics_full_velocity": float(getattr(args, "dynamics_full_velocity", 96.0)),
        "terminal_bg_color": str(getattr(args, "terminal_bg_color", "")).strip(),
        "glyph_note_white": _sanitize_visual_glyph(
            getattr(args, "glyph_note_white", DEFAULT_VISUAL_GLYPHS["glyph_note_white"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_white"],
        ),
        "glyph_note_pedal_white": _sanitize_visual_glyph(
            getattr(args, "glyph_note_pedal_white", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_white"],
        ),
        "glyph_note_pedal_black_left": _sanitize_visual_glyph(
            getattr(args, "glyph_note_pedal_black_left", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_left"],
        ),
        "glyph_note_pedal_black_mid": _sanitize_visual_glyph(
            getattr(args, "glyph_note_pedal_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_mid"],
        ),
        "glyph_note_pedal_black_right": _sanitize_visual_glyph(
            getattr(args, "glyph_note_pedal_black_right", DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_pedal_black_right"],
        ),
        "glyph_note_black_left": _sanitize_visual_glyph(
            getattr(args, "glyph_note_black_left", DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_left"],
        ),
        "glyph_note_black_mid": _sanitize_visual_glyph(
            getattr(args, "glyph_note_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_mid"],
        ),
        "glyph_note_black_right": _sanitize_visual_glyph(
            getattr(args, "glyph_note_black_right", DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"]),
            DEFAULT_VISUAL_GLYPHS["glyph_note_black_right"],
        ),
        "glyph_piano_white": _sanitize_visual_glyph(
            getattr(args, "glyph_piano_white", DEFAULT_VISUAL_GLYPHS["glyph_piano_white"]),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_white"],
        ),
        "glyph_piano_black_left": _sanitize_visual_glyph(
            getattr(args, "glyph_piano_black_left", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"]),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_left"],
        ),
        "glyph_piano_black_mid": _sanitize_visual_glyph(
            getattr(args, "glyph_piano_black_mid", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"]),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_mid"],
        ),
        "glyph_piano_black_right": _sanitize_visual_glyph(
            getattr(args, "glyph_piano_black_right", DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"]),
            DEFAULT_VISUAL_GLYPHS["glyph_piano_black_right"],
        ),
        "pedal_visual_dots": bool(getattr(args, "pedal_visual_dots", False)),
        "disable_pedal": bool(getattr(args, "disable_pedal", False)),
        "pedal_opacity_scale": max(0.0, min(1.0, float(getattr(args, "pedal_opacity_scale", 0.5)))),
        "pedal_decay_seconds": max(0.0, float(getattr(args, "pedal_decay_seconds", 10.0))),
        "measure_bars": bool(getattr(args, "measure_bars", False)),
        "drop_speed": args.drop_speed,
        "speed": args.speed,
        "midi_flip": bool(getattr(args, "midi_flip", False)),
        "stuck_note_fix": _normalize_saved_stuck_note_fix_mode(str(getattr(args, "stuck_note_fix", "auto"))),
        "start": args.start,
        "range": normalize_note_range_text(str(args.range)),
        "hide_piano": bool(getattr(args, "hide_piano", False)),
        "no_color": bool(args.no_color),
        "no_offpiano_pattern": bool(args.no_offpiano_pattern),
        "pressed_key_color_mode": normalize_pressed_key_color_mode(
            str(getattr(args, "pressed_key_color_mode", "split"))
        ),
    }
