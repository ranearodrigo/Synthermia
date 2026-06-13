#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from synthermia.config import DEFAULT_VISUAL_GLYPHS
from synthermia.midi import load_playback_source
from synthermia.models import NoteSpan
from synthermia.playback import build_pedal_hold_visual_spans
from synthermia.render import (
    black_fall_width,
    black_key_width,
    is_white,
    keyboard_row_counts,
    note_x,
    render_frame,
    white_key_width,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class FrameSample:
    index: int
    time_sec: float
    rows: Tuple[int, ...]
    note_rows: Tuple[int, ...]
    pedal_rows: Tuple[int, ...]

    @property
    def present(self) -> bool:
        return bool(self.rows)

    @property
    def top(self) -> Optional[int]:
        return self.rows[0] if self.rows else None

    @property
    def bottom(self) -> Optional[int]:
        return self.rows[-1] if self.rows else None

    @property
    def center(self) -> Optional[float]:
        if not self.rows:
            return None
        return (float(self.rows[0]) + float(self.rows[-1])) / 2.0

    @property
    def height(self) -> int:
        return len(self.rows)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Render a MIDI time window and track one note's screen rows to find "
            "abrupt visual jumps, gaps, and collision artifacts."
        )
    )
    p.add_argument("midi", help="Path to a MIDI file")
    p.add_argument("--note", type=int, required=True, help="MIDI note number to track, e.g. 38 for low D")
    p.add_argument("--time", type=float, required=True, help="Center time in seconds")
    p.add_argument("--before", type=float, default=0.5, help="Seconds before --time to inspect")
    p.add_argument("--after", type=float, default=0.5, help="Seconds after --time to inspect")
    p.add_argument("--fps", type=int, default=30, help="Sampling/render FPS")
    p.add_argument("--rows", type=int, default=35)
    p.add_argument("--x-scale", type=int, default=1)
    p.add_argument("--kb-y-scale", type=int, default=1)
    p.add_argument("--kb-black-extra", type=int, default=1)
    p.add_argument("--drop-speed", type=float, default=30.0)
    p.add_argument("--range", dest="note_range", type=str, default="21:108")
    p.add_argument("--direction", choices=["down", "up"], default="down")
    p.add_argument("--pedal-visual-dots", action="store_true")
    p.add_argument("--pedal-decay-seconds", type=float, default=10.0)
    p.add_argument("--pedal-opacity-scale", type=float, default=0.5)
    p.add_argument("--stuck-note-fix", choices=["auto", "off", "lifo"], default="auto")
    p.add_argument("--max-row-step", type=float, default=1.5, help="Flag center jumps larger than this many rows")
    p.add_argument("--max-height-step", type=int, default=1, help="Flag height changes larger than this many rows")
    p.add_argument(
        "--component",
        choices=["bottom", "top", "largest", "all"],
        default="bottom",
        help="Which contiguous vertical component to track when multiple same-note bodies are visible",
    )
    p.add_argument("--show-clean", action="store_true", help="Print every frame, not only anomalies")
    return p.parse_args(argv)


def parse_note_range(value: str) -> Tuple[int, int]:
    parts = str(value or "").split(":", 1)
    if len(parts) != 2:
        return 21, 108
    try:
        lo = int(parts[0])
        hi = int(parts[1])
    except ValueError:
        return 21, 108
    lo = max(0, min(127, lo))
    hi = max(0, min(127, hi))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def visible_spans(spans: Sequence[NoteSpan], t0: float, t1: float) -> List[NoteSpan]:
    out: List[NoteSpan] = []
    for sp in spans:
        if float(sp.end) < t0:
            continue
        if float(sp.start) > t1:
            break
        out.append(sp)
    return out


def target_columns(note: int, x_scale: int) -> range:
    x = note_x(note, x_scale)
    if is_white(note):
        return range(x, x + white_key_width(x_scale))
    width = black_fall_width(x_scale)
    x0 = x + max(0, (black_key_width(x_scale) - width) // 2)
    return range(x0, x0 + width)


def falling_glyphs() -> set[str]:
    glyphs = set()
    for key, value in DEFAULT_VISUAL_GLYPHS.items():
        if "note" not in key:
            continue
        glyphs.update(str(value))
    glyphs.update("·░▒▓█▐▌")
    return {ch for ch in glyphs if ch and not ch.isspace()}


def normal_note_glyphs() -> set[str]:
    keys = ("glyph_note_white", "glyph_note_black_left", "glyph_note_black_mid", "glyph_note_black_right")
    return {ch for key in keys for ch in str(DEFAULT_VISUAL_GLYPHS[key]) if ch and not ch.isspace()}


def pedal_note_glyphs() -> set[str]:
    keys = (
        "glyph_note_pedal_white",
        "glyph_note_pedal_black_left",
        "glyph_note_pedal_black_mid",
        "glyph_note_pedal_black_right",
    )
    return {ch for key in keys for ch in str(DEFAULT_VISUAL_GLYPHS[key]) if ch and not ch.isspace()}


def occupied_rows(
    frame: str,
    play_rows: int,
    columns: Iterable[int],
    glyphs: set[str],
    normal_glyphs: set[str],
    pedal_glyphs: set[str],
) -> Tuple[Tuple[int, ...], Tuple[int, ...], Tuple[int, ...]]:
    lines = [ANSI_RE.sub("", line) for line in frame.splitlines()]
    rows: List[int] = []
    note_rows: List[int] = []
    pedal_rows: List[int] = []
    cols = tuple(columns)
    for row_idx, line in enumerate(lines[1 : 1 + play_rows]):
        hit = False
        note_hit = False
        pedal_hit = False
        for col in cols:
            if not (0 <= col < len(line)):
                continue
            ch = line[col]
            if ch in glyphs:
                hit = True
            if ch in normal_glyphs:
                note_hit = True
            if ch in pedal_glyphs:
                pedal_hit = True
        if hit:
            rows.append(row_idx)
        if note_hit:
            note_rows.append(row_idx)
        if pedal_hit:
            pedal_rows.append(row_idx)
    return tuple(rows), tuple(note_rows), tuple(pedal_rows)


def component_gap(note_rows: Sequence[int], pedal_rows: Sequence[int]) -> Optional[int]:
    if not note_rows or not pedal_rows:
        return None
    note_top, note_bottom = min(note_rows), max(note_rows)
    pedal_top, pedal_bottom = min(pedal_rows), max(pedal_rows)
    if pedal_bottom < note_top:
        return note_top - pedal_bottom - 1
    if note_bottom < pedal_top:
        return pedal_top - note_bottom - 1
    return 0


def contiguous_components(rows: Sequence[int]) -> List[Tuple[int, ...]]:
    if not rows:
        return []
    components: List[List[int]] = [[int(rows[0])]]
    for row in rows[1:]:
        row_i = int(row)
        if row_i == components[-1][-1] + 1:
            components[-1].append(row_i)
        else:
            components.append([row_i])
    return [tuple(component) for component in components]


def select_component(rows: Sequence[int], mode: str) -> Tuple[int, ...]:
    components = contiguous_components(rows)
    if not components:
        return ()
    if mode == "all":
        return tuple(rows)
    if mode == "top":
        return components[0]
    if mode == "largest":
        return max(components, key=lambda component: (len(component), component[-1]))
    return components[-1]


def detect_anomalies(samples: Sequence[FrameSample], max_row_step: float, max_height_step: int) -> List[str]:
    anomalies: List[str] = []
    prev: Optional[FrameSample] = None
    for sample in samples:
        if prev is None:
            prev = sample
            continue
        if prev.present and sample.present:
            prev_center = prev.center
            cur_center = sample.center
            if prev_center is not None and cur_center is not None:
                step = cur_center - prev_center
                if abs(step) > max_row_step:
                    anomalies.append(
                        f"frame {sample.index}: center jump {step:+.2f} rows "
                        f"({prev.time_sec:.4f}s -> {sample.time_sec:.4f}s)"
                    )
            height_step = sample.height - prev.height
            if abs(height_step) > max_height_step:
                anomalies.append(
                    f"frame {sample.index}: height jump {height_step:+d} rows "
                    f"({prev.height} -> {sample.height})"
                )
        elif prev.present != sample.present:
            state = "appeared" if sample.present else "disappeared"
            anomalies.append(f"frame {sample.index}: note {state}")
        gap = component_gap(sample.note_rows, sample.pedal_rows)
        if gap is not None and gap > 0:
            anomalies.append(
                f"frame {sample.index}: note/pedal vertical gap {gap} row(s) "
                f"at {sample.time_sec:.4f}s"
            )
        prev = sample
    return anomalies


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    note_lo, note_hi = parse_note_range(args.note_range)
    black_rows, white_rows, kb_rows = keyboard_row_counts(args.kb_y_scale, args.kb_black_extra)
    play_rows = args.rows - kb_rows
    if play_rows <= 0:
        raise SystemExit("rows must leave at least one play row above the keyboard")

    spans, controls, measures, _diag = load_playback_source(
        args.midi,
        stuck_note_fix_mode=args.stuck_note_fix,
    )
    pedal_spans = build_pedal_hold_visual_spans(spans, controls) if args.pedal_visual_dots else []

    start_t = max(0.0, args.time - max(0.0, args.before))
    end_t = args.time + max(0.0, args.after)
    frame_dt = 1.0 / max(1, int(args.fps))
    glyphs = falling_glyphs()
    normal_glyphs = normal_note_glyphs()
    pedal_glyphs = pedal_note_glyphs()
    cols = target_columns(args.note, args.x_scale)

    samples: List[FrameSample] = []
    frame_idx = 0
    t = start_t
    window_sec = play_rows / max(1e-6, float(args.drop_speed))
    while t <= end_t + 1e-9:
        frame = render_frame(
            spans=visible_spans(spans, t - 0.05, t + window_sec),
            pedal_spans=visible_spans(pedal_spans, t - 0.05, t + window_sec),
            t=t,
            note_lo=note_lo,
            note_hi=note_hi,
            rows=args.rows,
            x_scale=args.x_scale,
            kb_y_scale=args.kb_y_scale,
            kb_black_extra=args.kb_black_extra,
            move_speed=args.drop_speed,
            length_speed=args.drop_speed,
            use_color=False,
            title="motion-probe",
            progress=0.0,
            pedal_opacity_scale=args.pedal_opacity_scale,
            pedal_decay_seconds=args.pedal_decay_seconds,
            measure_times=None,
            note_direction=args.direction,
        )
        all_rows, all_note_rows, all_pedal_rows = occupied_rows(
            frame,
            play_rows,
            cols,
            glyphs,
            normal_glyphs,
            pedal_glyphs,
        )
        rows = select_component(all_rows, args.component)
        selected = set(rows)
        note_rows = tuple(row for row in all_note_rows if row in selected)
        pedal_rows = tuple(row for row in all_pedal_rows if row in selected)
        samples.append(FrameSample(frame_idx, t, rows, note_rows, pedal_rows))
        frame_idx += 1
        t += frame_dt

    anomalies = detect_anomalies(samples, args.max_row_step, args.max_height_step)

    print(f"midi: {args.midi}")
    print(
        f"note: {args.note}  columns: {cols.start}:{cols.stop - 1}  "
        f"play_rows: {play_rows}  component: {args.component}"
    )
    print(f"time window: {start_t:.4f}s..{end_t:.4f}s  fps: {args.fps}")
    print()
    print("frame,time,top,bottom,center,height,all_rows,note_rows,pedal_rows,gap")
    for sample in samples:
        center = sample.center
        if args.show_clean or sample.present:
            center_txt = "" if center is None else f"{center:.2f}"
            gap = component_gap(sample.note_rows, sample.pedal_rows)
            print(
                f"{sample.index},{sample.time_sec:.6f},"
                f"{'' if sample.top is None else sample.top},"
                f"{'' if sample.bottom is None else sample.bottom},"
                f"{center_txt},{sample.height},"
                f"{' '.join(str(row) for row in sample.rows)},"
                f"{' '.join(str(row) for row in sample.note_rows)},"
                f"{' '.join(str(row) for row in sample.pedal_rows)},"
                f"{'' if gap is None else gap}"
            )

    print()
    if anomalies:
        print("anomalies:")
        for item in anomalies:
            print(f"- {item}")
    else:
        print("anomalies: none")
    return 1 if anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
