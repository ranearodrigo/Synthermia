from __future__ import annotations

import colorsys
import errno
import math
import os
import re
import select
import sys
import time
from bisect import bisect_left
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from synthermia.config import (
    normalize_pressed_key_color_mode,
    normalize_visual_glyphs,
)
from synthermia.models import MIN_PIANO_NOTE, MAX_PIANO_NOTE, WHITE_PCS, NoteSpan

PackedSpan = Tuple[int, float, float, int, int, int]
DensityCell = Tuple[int, int, int, int, bool, int]
ActiveDensityNote = Tuple[int, int, int]
VisibleDensityNote = Tuple[int, int]


# ---------------------------------------------------------------------------
# Keyboard geometry
# ---------------------------------------------------------------------------

def is_white(note: int) -> bool:
    return (note % 12) in WHITE_PCS


@lru_cache(maxsize=None)
def count_whites_before(note: int) -> int:
    c = 0
    for n in range(MIN_PIANO_NOTE, note):
        if is_white(n):
            c += 1
    return c


@lru_cache(maxsize=None)
def white_key_width(x_scale: int) -> int:
    return max(2, 2 * x_scale)


@lru_cache(maxsize=None)
def black_fall_width(x_scale: int) -> int:
    # Match falling black-note width to the black key width at all scales.
    return black_key_width(x_scale)


@lru_cache(maxsize=None)
def black_key_width(x_scale: int) -> int:
    return max(2, 2 * x_scale)


@lru_cache(maxsize=None)
def keyboard_row_counts(kb_y_scale: int, kb_black_extra: int) -> Tuple[int, int, int]:
    s = max(1, kb_y_scale)
    extra = max(0, kb_black_extra)
    black_rows = s + extra
    white_rows = 2 * s
    return black_rows, white_rows, black_rows + white_rows


@lru_cache(maxsize=None)
def note_x(note: int, x_scale: int) -> int:
    w = white_key_width(x_scale)
    if is_white(note):
        return count_whites_before(note) * w

    left = note - 1
    while left >= MIN_PIANO_NOTE and not is_white(left):
        left -= 1
    if left < MIN_PIANO_NOTE:
        left = note
    return count_whites_before(left) * w + max(1, x_scale)


@lru_cache(maxsize=None)
def keyboard_width(note_lo: int, note_hi: int, x_scale: int) -> int:
    max_x = 0
    for n in range(note_lo, note_hi + 1):
        x = note_x(n, x_scale)
        w = white_key_width(x_scale) if is_white(n) else black_key_width(x_scale)
        max_x = max(max_x, x + w - 1)
    return max_x + 1


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

def make_canvas(rows: int, cols: int) -> List[List[str]]:
    return [[" " for _ in range(cols)] for _ in range(rows)]


def paint_str(row: List[str], x: int, text: str) -> None:
    for i, ch in enumerate(text):
        xx = x + i
        if 0 <= xx < len(row):
            row[xx] = ch


def _flat_row_index(row: int, col: int, cols: int) -> int:
    return (row * cols) + col


# ---------------------------------------------------------------------------
# ANSI color utilities
# ---------------------------------------------------------------------------

def ansi_color(enabled: bool, code: str) -> str:
    if not enabled:
        return ""
    return f"\x1b[{code}m"


def ansi_fg_rgb(enabled: bool, rgb: Tuple[int, int, int]) -> str:
    if not enabled:
        return ""
    r, g, b = rgb
    return f"\x1b[38;2;{r};{g};{b}m"


def ansi_bg_rgb(enabled: bool, rgb: Tuple[int, int, int]) -> str:
    if not enabled:
        return ""
    r, g, b = rgb
    return f"\x1b[48;2;{r};{g};{b}m"


def _clamp_rgb_triplet(r: int, g: int, b: int) -> Tuple[int, int, int]:
    return (
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    )


def parse_rgb_triplet(value: str, default: Tuple[int, int, int] = (255, 64, 64)) -> Tuple[int, int, int]:
    m = re.match(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$", str(value))
    if not m:
        return _clamp_rgb_triplet(default[0], default[1], default[2])
    return _clamp_rgb_triplet(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def rgb_triplet_to_text(rgb: Tuple[int, int, int]) -> str:
    r, g, b = _clamp_rgb_triplet(rgb[0], rgb[1], rgb[2])
    return f"{r},{g},{b}"


def darken_rgb(rgb: Tuple[int, int, int], factor: float) -> Tuple[int, int, int]:
    f = max(0.0, min(1.0, float(factor)))
    return _clamp_rgb_triplet(int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))


def blend_rgb(bg_rgb: Tuple[int, int, int], fg_rgb: Tuple[int, int, int], alpha: float) -> Tuple[int, int, int]:
    a = max(0.0, min(1.0, float(alpha)))
    return _clamp_rgb_triplet(
        int(round(bg_rgb[0] * (1.0 - a) + fg_rgb[0] * a)),
        int(round(bg_rgb[1] * (1.0 - a) + fg_rgb[1] * a)),
        int(round(bg_rgb[2] * (1.0 - a) + fg_rgb[2] * a)),
    )


_ANSI16_RGB: Dict[int, Tuple[int, int, int]] = {
    0: (0, 0, 0), 1: (205, 0, 0), 2: (0, 205, 0), 3: (205, 205, 0),
    4: (0, 0, 238), 5: (205, 0, 205), 6: (0, 205, 205), 7: (229, 229, 229),
    8: (127, 127, 127), 9: (255, 0, 0), 10: (0, 255, 0), 11: (255, 255, 0),
    12: (92, 92, 255), 13: (255, 0, 255), 14: (0, 255, 255), 15: (255, 255, 255),
}


def _xterm256_to_rgb(code: int) -> Tuple[int, int, int]:
    idx = max(0, min(255, int(code)))
    if idx in _ANSI16_RGB:
        return _ANSI16_RGB[idx]
    if 16 <= idx <= 231:
        v = idx - 16
        r = v // 36
        g = (v // 6) % 6
        b = v % 6
        steps = [0, 95, 135, 175, 215, 255]
        return (steps[r], steps[g], steps[b])
    if 232 <= idx <= 255:
        gray = 8 + (idx - 232) * 10
        return (gray, gray, gray)
    return _ANSI16_RGB[0]


@lru_cache(maxsize=1)
def detect_terminal_bg_rgb() -> Tuple[int, int, int]:
    # COLORFGBG commonly exposes foreground;background color indexes.
    raw = str(os.environ.get("COLORFGBG", "")).strip()
    if not raw:
        return (0, 0, 0)
    parts = [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]
    if not parts:
        return (0, 0, 0)
    bg_token = parts[-1].lower()
    if bg_token in {"default", "none"}:
        return (0, 0, 0)
    try:
        return _xterm256_to_rgb(int(bg_token))
    except Exception:
        return (0, 0, 0)


def resolve_terminal_bg_rgb(override_text: str) -> Tuple[int, int, int]:
    txt = str(override_text or "").strip()
    if not txt:
        return detect_terminal_bg_rgb()
    return parse_rgb_triplet(txt, detect_terminal_bg_rgb())


def velocity_color_alpha(velocity: int, full_velocity: float) -> float:
    vel = max(1.0, min(127.0, float(int(velocity))))
    threshold = max(1.0, min(127.0, float(full_velocity)))
    if vel >= threshold:
        return 1.0
    return max(0.0, min(1.0, vel / threshold))


# ---------------------------------------------------------------------------
# Terminal stdout
# ---------------------------------------------------------------------------

def _safe_stdout_write(text: str) -> None:
    data = text.encode("utf-8", errors="ignore")
    if not data:
        return
    fd = sys.stdout.fileno()
    i = 0
    attempts = 0
    n = len(data)
    while i < n:
        try:
            wrote = os.write(fd, data[i:])
            if wrote <= 0:
                attempts += 1
                if attempts > 8:
                    break
                time.sleep(0.002)
                continue
            i += wrote
            attempts = 0
        except (BlockingIOError, OSError) as exc:
            err = getattr(exc, "errno", None)
            if err not in {errno.EAGAIN, errno.EWOULDBLOCK, 11}:
                raise
            attempts += 1
            if attempts > 8:
                break
            try:
                select.select([], [fd], [], 0.01)
            except Exception:
                time.sleep(0.002)


# ---------------------------------------------------------------------------
# Ornamental fill pattern
# ---------------------------------------------------------------------------

ORNAMENT_PATTERN = r"""
.    ,'    ,--'   ,'    `;-.     / \      `. o  ,--.      `.   /
 `.-'    ,'      '`-.,  /       /   `.      `--'  o `.      \ /     `-
   `.   /       /  '-..,       ;    ,-`.          ,---`.,.---'
     \ /     `-;.    ,'    ,--'   ,'    `;-.     / \      `. o  ,--.
'`.---'          `.-'    ,'      '`-.,  /       /   `.      `--'  o `.
   `. o  ,--.      `.   /       /  '-..,       ;    ,-`.          ,---
     `--'  o `.      \ /     `-;.    ,'    ,--'   ,'    `;-.     / \
.          ,---`'`.---'          `.-'    ,'      '`-.,  /       /   `.
 `;-.     / \      `. o  ,--.      `.   /       /  '-..,       ;    ,-
 /       /   `.      `--'  o `.      \ /     `-;.    ,'    ,--'   ,'
,       ;    ,-`.          ,---`.,.---'          `.-'    ,'      '`-.,
    ,--'   ,'    `;-.     / \      `. o  ,--.      `.   /       /  '-.
  ,'      '`-.,  /       /   `.      `--'  o `.      \ /     `-;.    ,
 /       /  '-..,       ;    ,-`.          ,---`'`.---'          `.-'
/     `-;.    ,'    ,--'   ,'    `;-.     / \      `. o  ,--.      `.
          `.-'    ,'      '`-.,  /       /   `.      `--'  o `.      \
  ,--.      `.   /       /  '-..,       ;    ,-`.          ,---`'`.---
-'  o `.      \ /     `-;.    ,'    ,--'   ,'    `;-.     / \      `.
    ,---`'`.---'          `.-'    ,'      '`-.,  /       /   `.      `
   / \      `. o  ,--.      `.   /       /  '-..,       ;    ,-`.
  /   `.      `--'  o `.      \ /     `-;.    ,'    ,--'   ,'    `;-.
 ;    ,-`.          ,---`'`.---'          `.-'    ,'      '`-.,  /
'   ,'    `;-.     / \      `. o  ,--.      `.   /       /  '-..,
   '`-.,  /       /   `.      `--'  o `.      \ /     `-;.    ,'    ,-
"""

PATTERN_LINES = [ln for ln in ORNAMENT_PATTERN.splitlines() if ln]
PATTERN_H = max(1, len(PATTERN_LINES))
PATTERN_W = max(1, max(len(ln) for ln in PATTERN_LINES))
PATTERN_GRID = [ln.ljust(PATTERN_W) for ln in PATTERN_LINES]


def ornamental_fill(width: int, row: int = 0, phase: int = 0) -> str:
    if width <= 0:
        return ""
    # Tile the full ASCII-art block as a rectangle, not per-line lengths.
    # This keeps horizontal structure stable even when source lines vary in length.
    y = row % PATTERN_H
    line = PATTERN_GRID[y]
    start = phase % PATTERN_W
    base = line[start:] + line[:start]
    return (base * ((width // PATTERN_W) + 2))[:width]


# ---------------------------------------------------------------------------
# Main frame renderer
# ---------------------------------------------------------------------------

def _render_density_frame(
    density_data: Dict[str, Sequence[object]],
    t: float,
    note_lo: int,
    note_hi: int,
    rows: int,
    x_scale: int,
    kb_y_scale: int,
    kb_black_extra: int,
    move_speed: float,
    use_color: bool,
    title: str,
    progress: float,
    track_colors: Optional[Dict[int, Tuple[int, int, int]]],
    pressed_key_color_mode: str,
    dynamics_full_velocity: float,
    terminal_bg_rgb: Optional[Tuple[int, int, int]],
    visual_glyphs: Optional[Dict[str, object]],
    measure_times: Optional[Sequence[float]],
) -> str:
    black_rows, white_rows, kb_rows = keyboard_row_counts(kb_y_scale, kb_black_extra)
    play_rows = rows - kb_rows
    hit_row = play_rows - 1
    cols = keyboard_width(note_lo, note_hi, x_scale)
    canvas = make_canvas(rows, cols)
    total_play_cells = play_rows * cols
    note_track = [-1] * total_play_cells
    note_velocity = [0] * total_play_cells
    black_fall_mask = bytearray(total_play_cells)
    default_white_note = (255, 80, 80)
    pressed_mode = normalize_pressed_key_color_mode(pressed_key_color_mode)
    term_bg_rgb = terminal_bg_rgb or detect_terminal_bg_rgb()
    full_vel = max(1.0, min(127.0, float(dynamics_full_velocity)))
    glyphs = normalize_visual_glyphs(visual_glyphs)
    note_white_glyph = glyphs["glyph_note_white"]
    note_pedal_black_mid = glyphs["glyph_note_pedal_black_mid"]
    note_black_mid = glyphs["glyph_note_black_mid"]
    piano_white_glyph = glyphs["glyph_piano_white"]
    piano_black_left = glyphs["glyph_piano_black_left"]
    piano_black_mid = glyphs["glyph_piano_black_mid"]
    piano_black_right = glyphs["glyph_piano_black_right"]
    white_w = white_key_width(x_scale)
    black_key_w = black_key_width(x_scale)
    palette_cache: Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = {}
    active_tracks_by_note: Dict[int, List[int]] = {}
    visible_tracks_by_note: Dict[int, List[int]] = {}
    active_weighted_colors_by_note: Dict[int, List[Tuple[Tuple[int, int, int], float]]] = {}
    active_notes = set()

    def track_white_rgb(track_idx: int) -> Tuple[int, int, int]:
        if track_colors and track_idx in track_colors:
            return track_colors[track_idx]
        return default_white_note

    def pressed_tint(rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        base = (165, 165, 165)
        alpha = 0.45
        return (
            int(round(base[0] * (1.0 - alpha) + rgb[0] * alpha)),
            int(round(base[1] * (1.0 - alpha) + rgb[1] * alpha)),
            int(round(base[2] * (1.0 - alpha) + rgb[2] * alpha)),
        )

    def blend_rgbs(colors: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
        if not colors:
            return (165, 165, 165)
        r = int(round(sum(c[0] for c in colors) / len(colors)))
        g = int(round(sum(c[1] for c in colors) / len(colors)))
        b = int(round(sum(c[2] for c in colors) / len(colors)))
        return (r, g, b)

    def blend_hue_weighted(weighted_colors: Sequence[Tuple[Tuple[int, int, int], float]]) -> Tuple[int, int, int]:
        total_w = 0.0
        hue_x = 0.0
        hue_y = 0.0
        sat_acc = 0.0
        val_acc = 0.0
        for rgb, w in weighted_colors:
            weight = max(0.0, float(w))
            if weight <= 0:
                continue
            r_f = max(0.0, min(1.0, rgb[0] / 255.0))
            g_f = max(0.0, min(1.0, rgb[1] / 255.0))
            b_f = max(0.0, min(1.0, rgb[2] / 255.0))
            h, s, v = colorsys.rgb_to_hsv(r_f, g_f, b_f)
            ang = h * (2.0 * math.pi)
            hue_weight = weight * max(0.15, s)
            hue_x += math.cos(ang) * hue_weight
            hue_y += math.sin(ang) * hue_weight
            sat_acc += s * weight
            val_acc += v * weight
            total_w += weight
        if total_w <= 0:
            return (165, 165, 165)
        sat = sat_acc / total_w
        val = val_acc / total_w
        if abs(hue_x) < 1e-9 and abs(hue_y) < 1e-9:
            den = sum(max(0.0, float(w)) for _rgb, w in weighted_colors)
            if den <= 0:
                return (165, 165, 165)
            rr = int(round(sum(rgb[0] * max(0.0, float(w)) for rgb, w in weighted_colors) / den))
            gg = int(round(sum(rgb[1] * max(0.0, float(w)) for rgb, w in weighted_colors) / den))
            bb = int(round(sum(rgb[2] * max(0.0, float(w)) for rgb, w in weighted_colors) / den))
            return _clamp_rgb_triplet(rr, gg, bb)
        hue = (math.atan2(hue_y, hue_x) / (2.0 * math.pi)) % 1.0
        r_m, g_m, b_m = colorsys.hsv_to_rgb(hue, sat, val)
        return _clamp_rgb_triplet(int(round(r_m * 255)), int(round(g_m * 255)), int(round(b_m * 255)))

    def add_unique_track(dst: Dict[int, List[int]], note: int, track_idx: int) -> None:
        arr = dst.get(note)
        if arr is None:
            dst[note] = [track_idx]
            return
        if track_idx not in arr:
            arr.append(track_idx)

    def white_density_char(level: int) -> str:
        if level <= 1:
            return "·"
        if level == 2:
            return "░"
        if level == 3:
            return "▒"
        return note_white_glyph

    def black_density_char(level: int) -> str:
        if level <= 2:
            return note_pedal_black_mid
        return note_black_mid

    for note, trk, vel in density_data.get("active", ()):
        note_i = int(note)
        trk_i = int(trk)
        active_notes.add(note_i)
        add_unique_track(active_tracks_by_note, note_i, trk_i)
        vel_w = max(1.0, float(max(1, int(vel))))
        active_weighted_colors_by_note.setdefault(note_i, []).append((track_white_rgb(trk_i), vel_w))
    for note, trk in density_data.get("visible", ()):
        add_unique_track(visible_tracks_by_note, int(note), int(trk))
    for x, y, trk, vel, is_black_cell, density in density_data.get("cells", ()):
        xx = int(x)
        yy = int(y)
        if not (0 <= xx < cols and 0 <= yy < play_rows):
            continue
        idx = _flat_row_index(yy, xx, cols)
        trk_i = int(trk)
        vel_i = max(1, min(127, int(vel)))
        note_track[idx] = trk_i
        note_velocity[idx] = vel_i
        dens = max(1, min(4, int(density)))
        if bool(is_black_cell):
            canvas[yy][xx] = black_density_char(dens)
            black_fall_mask[idx] = 1
        else:
            canvas[yy][xx] = white_density_char(dens)

    measure_rows: set[int] = set()
    if measure_times:
        window_sec = play_rows / move_speed
        bar_t0 = t - 0.15
        bar_t1 = t + window_sec + 0.1
        m0 = bisect_left(measure_times, bar_t0)
        m1 = bisect_left(measure_times, bar_t1)
        for mt in measure_times[m0:m1]:
            y_bar = int(math.floor(hit_row + (t - float(mt)) * move_speed))
            if not (0 <= y_bar < play_rows):
                continue
            measure_rows.add(y_bar)
            row = canvas[y_bar]
            for x in range(cols):
                if row[x] == " ":
                    row[x] = "_"

    key_bg_rgb = [[(245, 245, 245) for _ in range(cols)] for _ in range(kb_rows)]
    black_key_fg_rgb: List[List[Optional[Tuple[int, int, int]]]] = [
        [None for _ in range(cols)] for _ in range(black_rows)
    ]
    pressed_black_notes: List[int] = []
    black_start = play_rows
    white_start = black_start + black_rows

    def pressed_row_colors(note: int, row_count: int) -> List[Tuple[int, int, int]]:
        if row_count <= 0:
            return []
        if pressed_mode == "off":
            return [(165, 165, 165)] * row_count
        if pressed_mode == "weighted":
            weighted_colors = active_weighted_colors_by_note.get(note, [])
            if weighted_colors:
                mixed = blend_hue_weighted(weighted_colors)
                return [pressed_tint(mixed)] * row_count
            return [(165, 165, 165)] * row_count
        track_ids = visible_tracks_by_note.get(note) or active_tracks_by_note.get(note) or []
        tint_sources = [track_white_rgb(trk) for trk in track_ids] if track_ids else []
        if not tint_sources:
            return [(165, 165, 165)] * row_count
        if len(tint_sources) == 1:
            return [pressed_tint(tint_sources[0])] * row_count
        if row_count >= len(tint_sources):
            return [pressed_tint(tint_sources[(ry * len(tint_sources)) // row_count]) for ry in range(row_count)]
        return [pressed_tint(blend_rgbs(tint_sources))] * row_count

    for n in range(note_lo, note_hi + 1):
        x = note_x(n, x_scale)
        pressed = n in active_notes
        if is_white(n):
            row_colors: List[Tuple[int, int, int]] = [(245, 245, 245)] * kb_rows
            if pressed:
                row_colors = pressed_row_colors(n, kb_rows)
            for ry in range(kb_rows):
                row_rgb = row_colors[ry]
                for dx in range(white_w):
                    xx = x + dx
                    if 0 <= xx < cols:
                        key_bg_rgb[ry][xx] = row_rgb
            chunk = (("G" if pressed else "W") * white_w)
            for ry in range(white_rows):
                paint_str(canvas[white_start + ry], x, chunk)
        else:
            if black_key_w <= 1:
                base_chunk = piano_black_mid
                pressed_chunk = "P"
            elif black_key_w == 2:
                base_chunk = piano_black_left + piano_black_right
                pressed_chunk = "LR"
            else:
                base_chunk = piano_black_left + (piano_black_mid * (black_key_w - 2)) + piano_black_right
                pressed_chunk = "L" + ("P" * (black_key_w - 2)) + "R"
            chunk = pressed_chunk if pressed else base_chunk
            for ry in range(black_rows):
                paint_str(canvas[black_start + ry], x, chunk)
            if pressed:
                pressed_black_notes.append(n)

    for n in pressed_black_notes:
        x = note_x(n, x_scale)
        row_colors = pressed_row_colors(n, black_rows)
        for ry in range(black_rows):
            row_rgb = row_colors[ry]
            for dx in range(black_key_w):
                xx = x + dx
                if 0 <= xx < cols:
                    black_key_fg_rgb[ry][xx] = row_rgb

    for n in range(note_lo, note_hi + 1):
        if not is_white(n):
            continue
        x = note_x(n, x_scale)
        pressed = n in active_notes
        cap_ch = "U" if pressed else "T"
        for ry in range(black_rows):
            row = canvas[black_start + ry]
            for dx in range(white_w):
                xx = x + dx
                if 0 <= xx < cols and row[xx] == " ":
                    row[xx] = cap_ch

    header = f"{title}  {int(progress * 100):3d}%  t={t:6.2f}s"
    header = header[:cols].ljust(cols)
    reset = ansi_color(use_color, "0")
    lines: List[str] = [f"\x1b[97m{header}{reset}"] if use_color else [header]

    def track_palette(track_idx: int, velocity: int) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        key = (int(track_idx), int(velocity), 255)
        cached = palette_cache.get(key)
        if cached is not None:
            return cached
        if track_colors and track_idx in track_colors:
            base_white = track_colors[track_idx]
        else:
            base_white = default_white_note
        base_dark = darken_rgb(base_white, 0.6)
        alpha = velocity_color_alpha(int(velocity), full_vel)
        white_rgb = blend_rgb(term_bg_rgb, base_white, alpha)
        dark_rgb = blend_rgb(term_bg_rgb, base_dark, alpha)
        palette_cache[key] = (white_rgb, dark_rgb)
        return white_rgb, dark_rgb

    for y in range(play_rows):
        raw = "".join(canvas[y])
        if use_color:
            parts: List[str] = []
            current_style = reset
            clear_bg = ansi_color(use_color, "49")
            row_offset = y * cols
            for x, ch in enumerate(raw):
                idx = row_offset + x
                trk = note_track[idx]
                if black_fall_mask[idx]:
                    vel = note_velocity[idx]
                    _white_rgb, dark_rgb = track_palette(trk, vel)
                    style = ansi_fg_rgb(use_color, dark_rgb) + clear_bg
                elif trk >= 0:
                    vel = note_velocity[idx]
                    white_rgb, _dark_rgb = track_palette(trk, vel)
                    style = ansi_fg_rgb(use_color, white_rgb) + clear_bg
                elif y in measure_rows and ch == "_":
                    style = ansi_fg_rgb(use_color, (130, 130, 130)) + clear_bg
                else:
                    style = reset
                if style != current_style:
                    parts.append(style)
                    current_style = style
                parts.append(ch)
            if current_style != reset:
                parts.append(reset)
            line = "".join(parts)
        else:
            line = raw
        lines.append(line)

    if use_color:
        black_key_default = ansi_color(use_color, "30")
        for row_idx in range(black_start, black_start + black_rows):
            parts = []
            raw = "".join(canvas[row_idx])
            kb_ry = row_idx - black_start
            for x, ch in enumerate(raw):
                key_bg = ansi_bg_rgb(use_color, key_bg_rgb[kb_ry][x])
                if ch == "L":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_left)
                elif ch == "R":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_right)
                elif ch == "P":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_mid)
                elif ch in {"U", "T"}:
                    parts.append(key_bg + " ")
                elif ch in {piano_black_left, piano_black_right, piano_black_mid}:
                    parts.append(black_key_default + key_bg + ch)
                else:
                    parts.append(ch)
            lines.append("".join(parts) + reset)
        for row_idx in range(white_start, white_start + white_rows):
            parts = []
            raw = "".join(canvas[row_idx])
            kb_ry = row_idx - black_start
            for x, ch in enumerate(raw):
                if ch in {"G", "W"}:
                    key_bg = ansi_bg_rgb(use_color, key_bg_rgb[kb_ry][x])
                    parts.append(key_bg + " ")
                else:
                    parts.append(ch)
            lines.append("".join(parts) + reset)
    else:
        for row_idx in range(black_start, black_start + black_rows):
            raw = "".join(canvas[row_idx])
            raw = (
                raw.replace("L", piano_black_left)
                .replace("R", piano_black_right)
                .replace("P", piano_black_mid)
                .replace("T", piano_white_glyph)
                .replace("U", piano_white_glyph)
            )
            lines.append(raw)
        for row_idx in range(white_start, white_start + white_rows):
            raw = "".join(canvas[row_idx])
            raw = raw.replace("W", piano_white_glyph).replace("G", piano_white_glyph)
            lines.append(raw)

    return "\n".join(lines)

def render_frame(
    spans: Sequence[NoteSpan | PackedSpan],
    pedal_spans: Optional[Sequence[NoteSpan]],
    t: float,
    note_lo: int,
    note_hi: int,
    rows: int,
    x_scale: int,
    kb_y_scale: int,
    kb_black_extra: int,
    move_speed: float,
    length_speed: float,
    use_color: bool,
    title: str,
    progress: float,
    track_colors: Optional[Dict[int, Tuple[int, int, int]]] = None,
    pressed_key_color_mode: str = "split",
    dynamics_full_velocity: float = 96.0,
    pedal_opacity_scale: float = 0.5,
    pedal_decay_seconds: float = 10.0,
    terminal_bg_rgb: Optional[Tuple[int, int, int]] = None,
    visual_glyphs: Optional[Dict[str, object]] = None,
    measure_times: Optional[Sequence[float]] = None,
    note_direction: str = "down",
    density_data: Optional[Dict[str, Sequence[object]]] = None,
) -> str:
    direction = str(note_direction or "down").strip().lower()
    if direction not in {"down", "up"}:
        direction = "down"
    if density_data is not None and direction == "down" and not pedal_spans:
        return _render_density_frame(
            density_data=density_data,
            t=t,
            note_lo=note_lo,
            note_hi=note_hi,
            rows=rows,
            x_scale=x_scale,
            kb_y_scale=kb_y_scale,
            kb_black_extra=kb_black_extra,
            move_speed=move_speed,
            use_color=use_color,
            title=title,
            progress=progress,
            track_colors=track_colors,
            pressed_key_color_mode=pressed_key_color_mode,
            dynamics_full_velocity=dynamics_full_velocity,
            terminal_bg_rgb=terminal_bg_rgb,
            visual_glyphs=visual_glyphs,
            measure_times=measure_times,
        )
    black_rows, white_rows, kb_rows = keyboard_row_counts(kb_y_scale, kb_black_extra)
    play_rows = rows - kb_rows
    hit_row = play_rows - 1

    cols = keyboard_width(note_lo, note_hi, x_scale)
    canvas = make_canvas(rows, cols)
    white_fall_track = [[-1 for _ in range(cols)] for _ in range(play_rows)]
    white_fall_velocity = [[0 for _ in range(cols)] for _ in range(play_rows)]
    white_fall_alpha_scale = [[1.0 for _ in range(cols)] for _ in range(play_rows)]
    note_track = [[-1 for _ in range(cols)] for _ in range(play_rows)]
    note_velocity = [[0 for _ in range(cols)] for _ in range(play_rows)]
    black_fall_mask = [[False for _ in range(cols)] for _ in range(play_rows)]
    pedal_fall_mask = [[False for _ in range(cols)] for _ in range(play_rows)]
    pedal_black_fall_mask = [[False for _ in range(cols)] for _ in range(play_rows)]
    pedal_fall_alpha_scale = [[0.0 for _ in range(cols)] for _ in range(play_rows)]
    measure_line_mask = [[False for _ in range(cols)] for _ in range(play_rows)]
    note_density = [[0 for _ in range(cols)] for _ in range(play_rows)]
    default_white_note = (255, 80, 80)
    pressed_mode = normalize_pressed_key_color_mode(pressed_key_color_mode)
    term_bg_rgb = terminal_bg_rgb or detect_terminal_bg_rgb()
    full_vel = max(1.0, min(127.0, float(dynamics_full_velocity)))
    pedal_alpha_scale = max(0.0, min(1.0, float(pedal_opacity_scale)))
    pedal_decay = max(0.0, float(pedal_decay_seconds))
    glyphs = normalize_visual_glyphs(visual_glyphs)
    note_white_glyph = glyphs["glyph_note_white"]
    note_pedal_white = glyphs["glyph_note_pedal_white"]
    note_pedal_black_left = glyphs["glyph_note_pedal_black_left"]
    note_pedal_black_mid = glyphs["glyph_note_pedal_black_mid"]
    note_pedal_black_right = glyphs["glyph_note_pedal_black_right"]
    note_black_left = glyphs["glyph_note_black_left"]
    note_black_mid = glyphs["glyph_note_black_mid"]
    note_black_right = glyphs["glyph_note_black_right"]
    piano_white_glyph = glyphs["glyph_piano_white"]
    piano_black_left = glyphs["glyph_piano_black_left"]
    piano_black_mid = glyphs["glyph_piano_black_mid"]
    piano_black_right = glyphs["glyph_piano_black_right"]
    white_w = white_key_width(x_scale)
    black_key_w = black_key_width(x_scale)
    black_fall_w = black_fall_width(x_scale)
    palette_cache: Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = {}

    def build_pedal_alpha_lookup(pixels_per_second: float) -> Tuple[List[float], int]:
        if pedal_decay <= 0.0:
            return [pedal_alpha_scale] * (play_rows + 1), play_rows
        fade_rows = max(0.0, float(pedal_decay) * max(1e-6, float(pixels_per_second)))
        row_cap = max(0, int(math.ceil(fade_rows)) - 1)
        alpha_by_dist = [
            pedal_alpha_scale * max(0.0, 1.0 - (float(dist) / max(1e-6, fade_rows)))
            for dist in range(play_rows + 1)
        ]
        return alpha_by_dist, row_cap

    move_pedal_alpha_by_dist, move_pedal_row_cap = build_pedal_alpha_lookup(move_speed)
    length_pedal_alpha_by_dist, length_pedal_row_cap = build_pedal_alpha_lookup(length_speed)

    def pedal_visible_y1(y1: int, y2: int, row_cap: int) -> int:
        if pedal_decay <= 0.0:
            return y1
        return max(y1, y2 - row_cap)

    def pedal_cell_alpha_at_row(y: int, y2: int, alpha_by_dist: Sequence[float]) -> float:
        dist = max(0, min(len(alpha_by_dist) - 1, y2 - y))
        return float(alpha_by_dist[dist])

    def track_white_rgb(track_idx: int) -> Tuple[int, int, int]:
        if track_colors and track_idx in track_colors:
            return track_colors[track_idx]
        return default_white_note

    def pressed_tint(rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        # Keep a gray-looking pressed key, but bias hue toward the note color.
        base = (165, 165, 165)
        alpha = 0.45
        return (
            int(round(base[0] * (1.0 - alpha) + rgb[0] * alpha)),
            int(round(base[1] * (1.0 - alpha) + rgb[1] * alpha)),
            int(round(base[2] * (1.0 - alpha) + rgb[2] * alpha)),
        )

    def blend_rgbs(colors: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
        if not colors:
            return (165, 165, 165)
        r = int(round(sum(c[0] for c in colors) / len(colors)))
        g = int(round(sum(c[1] for c in colors) / len(colors)))
        b = int(round(sum(c[2] for c in colors) / len(colors)))
        return (r, g, b)

    def blend_hue_weighted(weighted_colors: Sequence[Tuple[Tuple[int, int, int], float]]) -> Tuple[int, int, int]:
        total_w = 0.0
        hue_x = 0.0
        hue_y = 0.0
        sat_acc = 0.0
        val_acc = 0.0
        for rgb, w in weighted_colors:
            weight = max(0.0, float(w))
            if weight <= 0:
                continue
            r_f = max(0.0, min(1.0, rgb[0] / 255.0))
            g_f = max(0.0, min(1.0, rgb[1] / 255.0))
            b_f = max(0.0, min(1.0, rgb[2] / 255.0))
            h, s, v = colorsys.rgb_to_hsv(r_f, g_f, b_f)
            ang = h * (2.0 * math.pi)
            hue_weight = weight * max(0.15, s)
            hue_x += math.cos(ang) * hue_weight
            hue_y += math.sin(ang) * hue_weight
            sat_acc += s * weight
            val_acc += v * weight
            total_w += weight
        if total_w <= 0:
            return (165, 165, 165)
        sat = sat_acc / total_w
        val = val_acc / total_w
        if abs(hue_x) < 1e-9 and abs(hue_y) < 1e-9:
            # Near-neutral hue vector: fall back to weighted RGB.
            rgb_w: List[Tuple[Tuple[int, int, int], float]] = [(rgb, max(0.0, float(w))) for rgb, w in weighted_colors]
            den = sum(w for _rgb, w in rgb_w)
            if den <= 0:
                return (165, 165, 165)
            rr = int(round(sum(rgb[0] * w for rgb, w in rgb_w) / den))
            gg = int(round(sum(rgb[1] * w for rgb, w in rgb_w) / den))
            bb = int(round(sum(rgb[2] * w for rgb, w in rgb_w) / den))
            return _clamp_rgb_triplet(rr, gg, bb)
        hue = (math.atan2(hue_y, hue_x) / (2.0 * math.pi)) % 1.0
        r_m, g_m, b_m = colorsys.hsv_to_rgb(hue, sat, val)
        return _clamp_rgb_triplet(int(round(r_m * 255)), int(round(g_m * 255)), int(round(b_m * 255)))

    def add_unique_track(dst: Dict[int, List[int]], note: int, track_idx: int) -> None:
        arr = dst.get(note)
        if arr is None:
            dst[note] = [track_idx]
            return
        if track_idx not in arr:
            arr.append(track_idx)

    window_sec = play_rows / move_speed
    if direction == "up":
        t0 = t - window_sec - 0.05
        t1 = t + 0.05
    else:
        t0 = t - 0.05
        t1 = t + window_sec
    active_tracks_by_note: Dict[int, List[int]] = {}
    visible_tracks_by_note: Dict[int, List[int]] = {}
    active_weighted_colors_by_note: Dict[int, List[Tuple[Tuple[int, int, int], float]]] = {}
    active_notes = set()
    packed_span_mode = bool(spans) and isinstance(spans[0], tuple)
    density_mode = density_data is not None or (
        packed_span_mode and len(spans) >= max(512, play_rows * max(12, cols // 3))
    )

    def white_density_char(level: int) -> str:
        if level <= 1:
            return "·"
        if level == 2:
            return "░"
        if level == 3:
            return "▒"
        return note_white_glyph

    def black_density_chunk(level: int, width: int) -> str:
        if level <= 1:
            mid = note_pedal_black_mid
            left = note_pedal_black_left
            right = note_pedal_black_right
        elif level == 2:
            mid = note_pedal_black_mid
            left = note_black_left
            right = note_black_right
        else:
            mid = note_black_mid
            left = note_black_left
            right = note_black_right
        if width <= 1:
            return mid
        if width == 2:
            return left + right
        return left + (mid * (width - 2)) + right

    def black_density_char(level: int) -> str:
        if level <= 1:
            return note_pedal_black_mid
        if level == 2:
            return note_pedal_black_mid
        if level == 3:
            return note_black_mid
        return note_black_mid

    def span_geometry(start_val: float, end_val: float) -> Optional[Tuple[int, int, int, int]]:
        if direction == "up":
            # For upward mode, keep active notes anchored at the piano edge and
            # only move them upward after release. This avoids base flicker.
            grow_dur = max(0.0, min(t, end_val) - start_val)
            pixel_len = max(1, int(round(grow_dur * length_speed)))
            release_shift = max(0.0, (t - end_val) * move_speed)
            y_bot = int(math.floor(hit_row - release_shift))
            y1 = y_bot - pixel_len + 1
            y2 = y_bot
        else:
            front_edge = float(hit_row) + ((float(t) - float(start_val)) * float(move_speed))
            pixel_len = max(1, int(round(max(0.0, float(end_val) - float(start_val)) * length_speed)))
            if front_edge < float(hit_row):
                y2 = int(math.floor(front_edge + 0.5))
                y1 = y2 - pixel_len + 1
            else:
                back_edge = front_edge - (max(0.0, float(end_val) - float(start_val)) * float(length_speed))
                y2 = hit_row
                y1 = max(y2 - pixel_len + 1, int(math.ceil(back_edge - 0.5)))
        if y2 < 0 or y1 > hit_row:
            return None
        return max(0, y1), min(hit_row, y2), y1, y2

    for sp in (pedal_spans or ()):
        if sp.end < t0:
            continue
        if sp.start > t1:
            break
        if not (note_lo <= sp.note <= note_hi):
            continue
        y_geom = span_geometry(float(sp.start), float(sp.end))
        if y_geom is None:
            continue
        y1, y2, raw_y1, raw_y2 = y_geom
        trk = int(getattr(sp, "track", 0))
        vel = max(1, min(127, int(getattr(sp, "velocity", 100))))
        x = note_x(sp.note, x_scale)
        if direction == "up":
            sample_t_lo = max(float(sp.start), float(t) - (float(hit_row) / max(1e-6, float(move_speed))))
            sample_t_hi = min(float(sp.end), float(t))
            if sample_t_hi < sample_t_lo:
                continue
            raw_y_top = int(
                math.ceil(float(hit_row) - ((float(t) - sample_t_lo) * float(move_speed)) - 1e-6)
            )
            raw_y_bottom = int(
                math.floor(float(hit_row) - ((float(t) - sample_t_hi) * float(move_speed)) + 1e-6)
            )
            y_top = max(0, raw_y_top)
            y_bottom = min(hit_row, raw_y_bottom)
            if y_bottom < y_top:
                continue
            y_top = max(y_top, pedal_visible_y1(raw_y_top, raw_y_bottom, move_pedal_row_cap))
            if y_bottom < y_top:
                continue
            if is_white(sp.note):
                for y in range(y_top, y_bottom + 1):
                    pedal_cell_alpha = pedal_cell_alpha_at_row(y, raw_y_bottom, move_pedal_alpha_by_dist)
                    if pedal_cell_alpha <= 0.0:
                        continue
                    for dx in range(white_w):
                        xx = x + dx
                        if 0 <= xx < cols:
                            canvas[y][xx] = note_pedal_white
                            white_fall_track[y][xx] = trk
                            white_fall_velocity[y][xx] = vel
                            white_fall_alpha_scale[y][xx] = pedal_cell_alpha
                            note_track[y][xx] = trk
                            note_velocity[y][xx] = vel
                            black_fall_mask[y][xx] = False
                            pedal_fall_mask[y][xx] = True
                            pedal_black_fall_mask[y][xx] = False
                            pedal_fall_alpha_scale[y][xx] = max(
                                pedal_fall_alpha_scale[y][xx], pedal_cell_alpha
                            )
                continue
            bw = black_fall_w
            x0 = x + max(0, (black_key_w - bw) // 2)
            if bw <= 1:
                pedal_black_chunk = note_pedal_black_mid
            elif bw == 2:
                pedal_black_chunk = note_pedal_black_left + note_pedal_black_right
            else:
                pedal_black_chunk = (
                    note_pedal_black_left
                    + (note_pedal_black_mid * (bw - 2))
                    + note_pedal_black_right
                )
            for y in range(y_top, y_bottom + 1):
                pedal_cell_alpha = pedal_cell_alpha_at_row(y, raw_y_bottom, move_pedal_alpha_by_dist)
                if pedal_cell_alpha <= 0.0:
                    continue
                for dx in range(bw):
                    xx = x0 + dx
                    if 0 <= xx < cols:
                        canvas[y][xx] = pedal_black_chunk[dx]
                        note_track[y][xx] = trk
                        note_velocity[y][xx] = vel
                        black_fall_mask[y][xx] = False
                        pedal_fall_mask[y][xx] = True
                        pedal_black_fall_mask[y][xx] = True
                        pedal_fall_alpha_scale[y][xx] = max(
                            pedal_fall_alpha_scale[y][xx], pedal_cell_alpha
                        )
            continue
        if is_white(sp.note):
            y1 = max(y1, pedal_visible_y1(raw_y1, raw_y2, length_pedal_row_cap))
            for y in range(y1, y2 + 1):
                if 0 <= y < play_rows:
                    pedal_cell_alpha = pedal_cell_alpha_at_row(y, raw_y2, length_pedal_alpha_by_dist)
                    if pedal_cell_alpha <= 0.0:
                        continue
                    for dx in range(white_w):
                        xx = x + dx
                        if 0 <= xx < cols:
                            canvas[y][xx] = note_pedal_white
                            white_fall_track[y][xx] = trk
                            white_fall_velocity[y][xx] = vel
                            white_fall_alpha_scale[y][xx] = pedal_cell_alpha
                            note_track[y][xx] = trk
                            note_velocity[y][xx] = vel
                            black_fall_mask[y][xx] = False
                            pedal_fall_mask[y][xx] = True
                            pedal_black_fall_mask[y][xx] = False
                            pedal_fall_alpha_scale[y][xx] = max(
                                pedal_fall_alpha_scale[y][xx], pedal_cell_alpha
                            )
        else:
            bw = black_fall_w
            x0 = x + max(0, (black_key_w - bw) // 2)
            if bw <= 1:
                pedal_black_chunk = note_pedal_black_mid
            elif bw == 2:
                pedal_black_chunk = note_pedal_black_left + note_pedal_black_right
            else:
                pedal_black_chunk = (
                    note_pedal_black_left
                    + (note_pedal_black_mid * (bw - 2))
                    + note_pedal_black_right
                )
            y1 = max(y1, pedal_visible_y1(raw_y1, raw_y2, length_pedal_row_cap))
            for y in range(y1, y2 + 1):
                if 0 <= y < play_rows:
                    pedal_cell_alpha = pedal_cell_alpha_at_row(y, raw_y2, length_pedal_alpha_by_dist)
                    if pedal_cell_alpha <= 0.0:
                        continue
                    for dx in range(bw):
                        xx = x0 + dx
                        if 0 <= xx < cols:
                            canvas[y][xx] = pedal_black_chunk[dx]
                            note_track[y][xx] = trk
                            note_velocity[y][xx] = vel
                            black_fall_mask[y][xx] = False
                            pedal_fall_mask[y][xx] = True
                            pedal_black_fall_mask[y][xx] = True
                            pedal_fall_alpha_scale[y][xx] = max(
                                pedal_fall_alpha_scale[y][xx], pedal_cell_alpha
                            )

    if density_data is not None:
        for note, trk, vel in density_data.get("active", ()):
            active_notes.add(int(note))
            add_unique_track(active_tracks_by_note, int(note), int(trk))
            vel_w = max(1.0, float(max(1, int(vel))))
            active_weighted_colors_by_note.setdefault(int(note), []).append((track_white_rgb(int(trk)), vel_w))
        for note, trk in density_data.get("visible", ()):
            add_unique_track(visible_tracks_by_note, int(note), int(trk))
        for x, y, trk, vel, is_black_cell, density in density_data.get("cells", ()):
            xx = int(x)
            yy = int(y)
            if not (0 <= xx < cols and 0 <= yy < play_rows):
                continue
            dens = max(1, min(4, int(density)))
            note_density[yy][xx] = dens
            trk_i = int(trk)
            vel_i = max(1, min(127, int(vel)))
            note_track[yy][xx] = trk_i
            note_velocity[yy][xx] = vel_i
            pedal_fall_mask[yy][xx] = False
            pedal_black_fall_mask[yy][xx] = False
            if bool(is_black_cell):
                canvas[yy][xx] = black_density_char(dens)
                black_fall_mask[yy][xx] = True
            else:
                canvas[yy][xx] = white_density_char(dens)
                white_fall_track[yy][xx] = trk_i
                white_fall_velocity[yy][xx] = vel_i
                white_fall_alpha_scale[yy][xx] = 1.0
                black_fall_mask[yy][xx] = False
    else:
        for sp in spans:
            if packed_span_mode:
                sp_note, sp_start, sp_end, sp_velocity, _sp_channel, sp_track = sp  # type: ignore[misc]
            else:
                sp_note = int(sp.note)  # type: ignore[union-attr]
                sp_start = float(sp.start)  # type: ignore[union-attr]
                sp_end = float(sp.end)  # type: ignore[union-attr]
                sp_velocity = int(getattr(sp, "velocity", 100))  # type: ignore[arg-type]
                sp_track = int(getattr(sp, "track", 0))  # type: ignore[arg-type]
            if sp_end < t0:
                continue
            if sp_start > t1:
                break
            if not (note_lo <= sp_note <= note_hi):
                continue
            trk = sp_track
            if sp_start <= t <= sp_end:
                active_notes.add(sp_note)
                add_unique_track(active_tracks_by_note, sp_note, trk)
                vel_w = max(1.0, float(max(1, sp_velocity)))
                active_weighted_colors_by_note.setdefault(sp_note, []).append((track_white_rgb(trk), vel_w))

            y_geom = span_geometry(sp_start, sp_end)
            if y_geom is None:
                continue
            y1, y2, _raw_y1, _raw_y2 = y_geom
            x = note_x(sp_note, x_scale)
            add_unique_track(visible_tracks_by_note, sp_note, trk)

            if is_white(sp_note):
                vel = max(1, min(127, sp_velocity))
                for y in range(y1, y2 + 1):
                    if 0 <= y < play_rows:
                        for dx in range(white_w):
                            xx = x + dx
                            if 0 <= xx < cols:
                                if density_mode:
                                    dens = min(4, note_density[y][xx] + 1)
                                    note_density[y][xx] = dens
                                    canvas[y][xx] = white_density_char(dens)
                                    if vel >= note_velocity[y][xx]:
                                        white_fall_track[y][xx] = trk
                                        white_fall_velocity[y][xx] = vel
                                        white_fall_alpha_scale[y][xx] = 1.0
                                        note_track[y][xx] = trk
                                        note_velocity[y][xx] = vel
                                else:
                                    canvas[y][xx] = note_white_glyph
                                    white_fall_track[y][xx] = trk
                                    white_fall_velocity[y][xx] = vel
                                    white_fall_alpha_scale[y][xx] = 1.0
                                    note_track[y][xx] = trk
                                    note_velocity[y][xx] = vel
                                black_fall_mask[y][xx] = False
                                pedal_fall_mask[y][xx] = False
                                pedal_black_fall_mask[y][xx] = False
            else:
                bw = black_fall_w
                x0 = x + max(0, (black_key_w - bw) // 2)
                vel = max(1, min(127, sp_velocity))
                black_chunk = black_density_chunk(4, bw)
                for y in range(y1, y2 + 1):
                    if 0 <= y < play_rows:
                        for dx in range(bw):
                            xx = x0 + dx
                            if 0 <= xx < cols:
                                if density_mode:
                                    dens = min(4, note_density[y][xx] + 1)
                                    note_density[y][xx] = dens
                                    chunk = black_density_chunk(dens, bw)
                                    canvas[y][xx] = chunk[dx]
                                    if vel >= note_velocity[y][xx]:
                                        note_track[y][xx] = trk
                                        note_velocity[y][xx] = vel
                                else:
                                    canvas[y][xx] = black_chunk[dx]
                                    note_track[y][xx] = trk
                                    note_velocity[y][xx] = vel
                                black_fall_mask[y][xx] = True
                                pedal_fall_mask[y][xx] = False
                                pedal_black_fall_mask[y][xx] = False

    # Draw moving measure separators behind notes.
    if measure_times:
        bar_t0 = t0 - 0.1
        bar_t1 = t1 + 0.1
        m0 = bisect_left(measure_times, bar_t0)
        m1 = bisect_left(measure_times, bar_t1)
        for mt in measure_times[m0:m1]:
            if direction == "up":
                y_bar = int(math.floor(hit_row - (t - float(mt)) * move_speed))
            else:
                y_bar = int(math.floor(hit_row + (t - float(mt)) * move_speed))
            if not (0 <= y_bar < play_rows):
                continue
            row = canvas[y_bar]
            row_mask = measure_line_mask[y_bar]
            for x in range(cols):
                if row[x] == " ":
                    row[x] = "_"
                    row_mask[x] = True

    black_start = play_rows
    white_start = black_start + black_rows
    key_bg_rgb = [[(245, 245, 245) for _ in range(cols)] for _ in range(kb_rows)]
    # Optional per-cell foreground tint for pressed black-key glyphs.
    black_key_fg_rgb: List[List[Optional[Tuple[int, int, int]]]] = [
        [None for _ in range(cols)] for _ in range(black_rows)
    ]
    pressed_black_notes: List[int] = []

    def pressed_row_colors(note: int, row_count: int) -> List[Tuple[int, int, int]]:
        if row_count <= 0:
            return []
        if pressed_mode == "off":
            return [(165, 165, 165)] * row_count
        if pressed_mode == "weighted":
            weighted_colors = active_weighted_colors_by_note.get(note, [])
            if weighted_colors:
                mixed = blend_hue_weighted(weighted_colors)
                return [pressed_tint(mixed)] * row_count
            return [(165, 165, 165)] * row_count
        track_ids = visible_tracks_by_note.get(note) or active_tracks_by_note.get(note) or []
        tint_sources = [track_white_rgb(trk) for trk in track_ids] if track_ids else []
        if not tint_sources:
            return [(165, 165, 165)] * row_count
        if len(tint_sources) == 1:
            return [pressed_tint(tint_sources[0])] * row_count
        if row_count >= len(tint_sources):
            return [pressed_tint(tint_sources[(ry * len(tint_sources)) // row_count]) for ry in range(row_count)]
        return [pressed_tint(blend_rgbs(tint_sources))] * row_count

    for n in range(note_lo, note_hi + 1):
        x = note_x(n, x_scale)
        pressed = n in active_notes
        if is_white(n):
            row_colors: List[Tuple[int, int, int]] = [(245, 245, 245)] * kb_rows
            if pressed:
                row_colors = pressed_row_colors(n, kb_rows)
            for ry in range(kb_rows):
                row_rgb = row_colors[ry]
                for dx in range(white_w):
                    xx = x + dx
                    if 0 <= xx < cols:
                        key_bg_rgb[ry][xx] = row_rgb
            # Use placeholders so press state can be represented by color
            # (white default -> gray when pressed) instead of glyph changes.
            chunk = (("G" if pressed else "W") * white_w)
            for ry in range(white_rows):
                paint_str(canvas[white_start + ry], x, chunk)
        else:
            # Split black keys across adjacent white-key characters.
            bw = black_key_w
            # Use one solid black-key shape for both states to avoid half-block
            # vertical striping artifacts at larger keyboard Y scales.
            if bw <= 1:
                base_chunk = piano_black_mid
                pressed_chunk = "P"
            elif bw == 2:
                base_chunk = piano_black_left + piano_black_right
                pressed_chunk = "LR"
            else:
                base_chunk = piano_black_left + (piano_black_mid * (bw - 2)) + piano_black_right
                pressed_chunk = "L" + ("P" * (bw - 2)) + "R"

            chunk = pressed_chunk if pressed else base_chunk
            for ry in range(black_rows):
                paint_str(canvas[black_start + ry], x, chunk)
            if pressed:
                pressed_black_notes.append(n)

    # Apply pressed black-key tint as foreground color after all keys are
    # laid out so adjacent white-key passes cannot overwrite half-block edges.
    for n in pressed_black_notes:
        x = note_x(n, x_scale)
        bw = black_key_width(x_scale)
        bw = black_key_w
        row_colors = pressed_row_colors(n, black_rows)
        for ry in range(black_rows):
            row_rgb = row_colors[ry]
            for dx in range(bw):
                xx = x + dx
                if 0 <= xx < cols:
                    black_key_fg_rgb[ry][xx] = row_rgb

    # Extend white keys upward into the black-key band where no black key sits.
    for n in range(note_lo, note_hi + 1):
        if not is_white(n):
            continue
        x = note_x(n, x_scale)
        pressed = n in active_notes
        cap_ch = "U" if pressed else "T"
        for ry in range(black_rows):
            row = canvas[black_start + ry]
            for dx in range(white_w):
                xx = x + dx
                if 0 <= xx < cols and row[xx] == " ":
                    row[xx] = cap_ch

    header = f"{title}  {int(progress * 100):3d}%  t={t:6.2f}s"
    header = header[:cols].ljust(cols)

    reset = ansi_color(use_color, "0")
    lines: List[str] = [f"\x1b[97m{header}{reset}"] if use_color else [header]

    def track_palette(
        track_idx: int, velocity: int, alpha_scale: float = 1.0
    ) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        alpha_q = max(0, min(255, int(round(max(0.0, min(1.0, float(alpha_scale))) * 255.0))))
        key = (int(track_idx), int(velocity), alpha_q)
        cached = palette_cache.get(key)
        if cached is not None:
            return cached
        if track_colors and track_idx in track_colors:
            base_white = track_colors[track_idx]
        else:
            base_white = default_white_note
        base_dark = darken_rgb(base_white, 0.6)
        alpha = velocity_color_alpha(int(velocity), full_vel) * (alpha_q / 255.0)
        alpha = max(0.0, min(1.0, alpha))
        white_rgb = blend_rgb(term_bg_rgb, base_white, alpha)
        dark_rgb = blend_rgb(term_bg_rgb, base_dark, alpha)
        palette_cache[key] = (white_rgb, dark_rgb)
        return white_rgb, dark_rgb

    for y in range(play_rows):
        raw = "".join(canvas[y])
        if use_color:
            parts: List[str] = []
            current_style = reset
            clear_bg = ansi_color(use_color, "49")
            for x, ch in enumerate(raw):
                if pedal_fall_mask[y][x]:
                    trk = note_track[y][x]
                    vel = note_velocity[y][x]
                    pedal_alpha = pedal_fall_alpha_scale[y][x]
                    white_rgb, dark_rgb = track_palette(trk, vel, pedal_alpha)
                    if pedal_black_fall_mask[y][x]:
                        bg_track = white_fall_track[y][x]
                        bg_vel = white_fall_velocity[y][x]
                        bg_alpha = white_fall_alpha_scale[y][x]
                        bg_rgb = track_palette(bg_track, bg_vel, bg_alpha)[0] if bg_track >= 0 else None
                        style = ansi_fg_rgb(use_color, dark_rgb) + (
                            ansi_bg_rgb(use_color, bg_rgb) if bg_rgb else clear_bg
                        )
                    else:
                        style = ansi_fg_rgb(use_color, white_rgb) + clear_bg
                elif black_fall_mask[y][x]:
                    trk = note_track[y][x]
                    vel = note_velocity[y][x]
                    _white_rgb, dark_rgb = track_palette(trk, vel)
                    bg_track = white_fall_track[y][x]
                    bg_vel = white_fall_velocity[y][x]
                    bg_alpha = white_fall_alpha_scale[y][x]
                    bg_rgb = track_palette(bg_track, bg_vel, bg_alpha)[0] if bg_track >= 0 else None
                    style = ansi_fg_rgb(use_color, dark_rgb) + (
                        ansi_bg_rgb(use_color, bg_rgb) if bg_rgb else clear_bg
                    )
                elif note_track[y][x] >= 0:
                    trk = note_track[y][x]
                    vel = note_velocity[y][x]
                    white_rgb, _dark_rgb = track_palette(trk, vel)
                    style = ansi_fg_rgb(use_color, white_rgb) + clear_bg
                elif measure_line_mask[y][x]:
                    style = ansi_fg_rgb(use_color, (130, 130, 130)) + clear_bg
                else:
                    style = reset
                if style != current_style:
                    parts.append(style)
                    current_style = style
                parts.append(ch)
            if current_style != reset:
                parts.append(reset)
            line = "".join(parts)
        else:
            line = raw
        lines.append(line)

    if use_color:
        black_key_default = ansi_color(use_color, "30")
        for row_idx in range(black_start, black_start + black_rows):
            parts = []
            raw = "".join(canvas[row_idx])
            kb_ry = row_idx - black_start
            for x, ch in enumerate(raw):
                key_bg = ansi_bg_rgb(use_color, key_bg_rgb[kb_ry][x])
                if ch == "L":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_left)
                elif ch == "R":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_right)
                elif ch == "P":
                    fg_rgb = black_key_fg_rgb[kb_ry][x] or (165, 165, 165)
                    parts.append(ansi_fg_rgb(use_color, fg_rgb) + key_bg + piano_black_mid)
                elif ch in {"U", "T"}:
                    parts.append(key_bg + " ")
                elif ch in {piano_black_left, piano_black_right, piano_black_mid}:
                    parts.append(black_key_default + key_bg + ch)
                else:
                    parts.append(ch)
            lines.append("".join(parts) + reset)
        for row_idx in range(white_start, white_start + white_rows):
            parts = []
            raw = "".join(canvas[row_idx])
            kb_ry = row_idx - black_start
            for x, ch in enumerate(raw):
                if ch in {"G", "W"}:
                    key_bg = ansi_bg_rgb(use_color, key_bg_rgb[kb_ry][x])
                    parts.append(key_bg + " ")
                else:
                    parts.append(ch)
            lines.append("".join(parts) + reset)
    else:
        for row_idx in range(black_start, black_start + black_rows):
            raw = "".join(canvas[row_idx])
            raw = (
                raw.replace("L", piano_black_left)
                .replace("R", piano_black_right)
                .replace("P", piano_black_mid)
                .replace("T", piano_white_glyph)
                .replace("U", piano_white_glyph)
            )
            lines.append(raw)
        for row_idx in range(white_start, white_start + white_rows):
            raw = "".join(canvas[row_idx])
            raw = raw.replace("W", piano_white_glyph).replace("G", piano_white_glyph)
            lines.append(raw)

    return "\n".join(lines)
