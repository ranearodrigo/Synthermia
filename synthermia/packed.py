from __future__ import annotations

import os
import threading
import time
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from synthermia.midi import load_playback_source
from synthermia.models import ControlEvent, NoteSpan

try:
    from synthermia_native import (
        load_packed_playback_source_native,
        native_runtime_info,
        start_packed_playback_load_task_native,
    )
except ImportError:
    load_packed_playback_source_native = None
    native_runtime_info = None
    start_packed_playback_load_task_native = None


def resolve_playback_engine(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"", "auto", "default"}:
        return "auto"
    if mode in {"python", "py"}:
        return "python"
    if mode in {"packed", "native"}:
        return "packed"
    return "auto"


def native_packed_available() -> bool:
    return callable(load_packed_playback_source_native)


def native_packed_task_available() -> bool:
    return callable(start_packed_playback_load_task_native)


PARTIAL_PACKED_START_BUFFER_SEC = 30.0
PARTIAL_PACKED_START_BUFFER_REAL_SEC = 5.0
PARTIAL_PACKED_START_BUFFER_MAX_SEC = 70.0
PARTIAL_PACKED_START_BUFFER_HIGH_SPEED = 8.0
PARTIAL_PACKED_START_BUFFER_EXTREME_REAL_SEC = 10.5
PARTIAL_PACKED_START_BUFFER_EXTREME_MAX_SEC = 145.0
PARTIAL_PACKED_MAX_SAFE_STARTUP_SPEED = 12.0


def scaled_partial_start_buffer_sec(playback_speed: float) -> float:
    speed = max(0.1, float(playback_speed))
    target = max(
        PARTIAL_PACKED_START_BUFFER_SEC,
        min(PARTIAL_PACKED_START_BUFFER_MAX_SEC, speed * PARTIAL_PACKED_START_BUFFER_REAL_SEC),
    )
    if speed > PARTIAL_PACKED_START_BUFFER_HIGH_SPEED:
        target = max(
            target,
            min(
                PARTIAL_PACKED_START_BUFFER_EXTREME_MAX_SEC,
                speed * PARTIAL_PACKED_START_BUFFER_EXTREME_REAL_SEC,
            ),
        )
    return target


def should_auto_use_packed(midi_path: str) -> bool:
    try:
        size_bytes = os.path.getsize(midi_path)
    except OSError:
        return False
    return size_bytes >= (2 * 1024 * 1024)


def should_use_partial_packed_startup(
    midi_path: str,
    *,
    playback_engine: str,
    playback_speed: float = 1.0,
    start_sec: float = 0.0,
    soundfont_path: str = "",
    pedal_visual_dots: bool = False,
) -> bool:
    if resolve_playback_engine(playback_engine) == "python":
        return False
    if not native_packed_task_available():
        return False
    if float(start_sec) > 1e-9:
        return False
    if float(playback_speed) > PARTIAL_PACKED_MAX_SAFE_STARTUP_SPEED:
        return False
    if str(soundfont_path or "").strip():
        return False
    if bool(pedal_visual_dots):
        return False
    try:
        size_bytes = os.path.getsize(midi_path)
    except OSError:
        return False
    return size_bytes >= (8 * 1024 * 1024)


@dataclass
class LoadedPlaybackData:
    spans: Optional[List[NoteSpan]]
    controls: List[ControlEvent]
    measure_times: List[float]
    note_diag: Dict[str, int]
    packed_source: Optional["PackedPlaybackSource"]
    engine_requested: str
    engine_used: str
    native_backend: bool
    is_partial: bool = False


class BackgroundPlaybackLoadTask:
    def __init__(self, native_task: object) -> None:
        self._native_task = native_task
        self._done = False
        self._result: Optional[LoadedPlaybackData] = None
        self._partial_result: Optional[LoadedPlaybackData] = None
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._collect_result, name="synthermia-native-load-wait", daemon=True)
        self._thread.start()

    def _collect_result(self) -> None:
        try:
            native_source = self._native_task.result()
            native_controls = _coerce_control_events(native_source.control_tuples())
            native_measure_times = [float(value) for value in native_source.measure_times()]
            native_note_diag = {
                key: int(value) for key, value in dict(native_source.note_diag()).items()
            }
            packed_source = PackedPlaybackSource.from_native(
                native_source=native_source,
                controls=native_controls,
                measure_times=native_measure_times,
                note_diag=native_note_diag,
            )
            self._result = LoadedPlaybackData(
                spans=None,
                controls=native_controls,
                measure_times=native_measure_times,
                note_diag=native_note_diag,
                packed_source=packed_source,
                engine_requested="packed",
                engine_used="packed",
                native_backend=True,
                is_partial=False,
            )
        except BaseException as exc:
            self._error = exc
        finally:
            self._done = True

    def done(self) -> bool:
        return bool(self._done)

    def cancel(self) -> None:
        try:
            cancel = getattr(self._native_task, "cancel")
        except Exception:
            cancel = None
        if callable(cancel):
            cancel()

    def progress(self) -> Dict[str, Any]:
        try:
            return {str(key): value for key, value in dict(self._native_task.progress()).items()}
        except Exception:
            return {"phase": "loading", "done": self.done()}

    def seconds_ready(self) -> float:
        snapshot = self.progress()
        try:
            return float(snapshot.get("seconds_ready", 0.0) or 0.0)
        except Exception:
            return 0.0

    def published_ready_duration_sec(self) -> float:
        snapshot = self.progress()
        try:
            value = float(snapshot.get("published_ready_duration", 0.0) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return max(0.0, value)
        if self._partial_result is not None and self._partial_result.packed_source is not None:
            try:
                return max(0.0, float(self._partial_result.packed_source.duration_sec))
            except Exception:
                pass
        return 0.0

    def ready_duration_sec(self) -> float:
        published = self.published_ready_duration_sec()
        if published > 0.0:
            return published
        if self._result is not None and self._result.packed_source is not None:
            try:
                return max(0.0, float(self._result.packed_source.duration_sec))
            except Exception:
                pass
        return max(0.0, self.seconds_ready())

    def result(self) -> LoadedPlaybackData:
        self._thread.join()
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise RuntimeError("Background packed playback task produced no result")
        return self._result

    def partial_result(self) -> Optional[LoadedPlaybackData]:
        try:
            native_source = self._native_task.partial_result()
        except Exception:
            return None
        if native_source is None:
            return None
        native_duration = float(getattr(native_source, "duration_sec", 0.0) or 0.0)
        if (
            self._partial_result is not None
            and self._partial_result.packed_source is not None
            and native_duration <= (float(self._partial_result.packed_source.duration_sec) + 1e-9)
        ):
            return self._partial_result
        native_controls = _coerce_control_events(native_source.control_tuples())
        native_measure_times = [float(value) for value in native_source.measure_times()]
        native_note_diag = {
            key: int(value) for key, value in dict(native_source.note_diag()).items()
        }
        packed_source = PackedPlaybackSource.from_native(
            native_source=native_source,
            controls=native_controls,
            measure_times=native_measure_times,
            note_diag=native_note_diag,
        )
        self._partial_result = LoadedPlaybackData(
            spans=None,
            controls=native_controls,
            measure_times=native_measure_times,
            note_diag=native_note_diag,
            packed_source=packed_source,
            engine_requested="packed",
            engine_used="packed",
            native_backend=True,
            is_partial=True,
        )
        return self._partial_result

    def wait_until_ready(self, minimum_ready_sec: float, *, poll_interval: float = 0.05) -> LoadedPlaybackData:
        target = max(0.0, float(minimum_ready_sec))
        while True:
            partial = self.partial_result()
            if partial is not None and (
                target <= 0.0
                or (
                    partial.packed_source is not None
                    and float(partial.packed_source.duration_sec) >= target
                )
                or self.ready_duration_sec() >= target
            ):
                return partial
            if self.done():
                return self.result()
            time.sleep(max(0.01, float(poll_interval)))


class PackedPlaybackSource:
    def __init__(
        self,
        spans: Sequence[NoteSpan],
        controls: Sequence[ControlEvent],
        measure_times: Sequence[float],
        note_diag: Dict[str, int],
        source_kind: str,
        native_backend: bool,
        native_source: object | None = None,
        track_ids: Optional[Sequence[int]] = None,
    ) -> None:
        span_list = list(spans)
        self._spans = span_list
        self.controls = list(controls)
        self.measure_times = list(measure_times)
        self.note_diag = dict(note_diag)
        self.source_kind = str(source_kind)
        self.native_backend = bool(native_backend)
        self._native_source = native_source
        self._track_ids = sorted({int(track) for track in (track_ids or [])})
        if native_source is not None:
            self.duration_sec = float(getattr(native_source, "duration_sec", 0.0))
            self.note_count = int(getattr(native_source, "note_count", len(span_list)))
        else:
            self.duration_sec = max((float(sp.end) for sp in span_list), default=0.0)
            self.note_count = len(span_list)
            if not self._track_ids:
                self._track_ids = sorted({int(getattr(sp, "track", 0)) for sp in span_list})
        if native_source is None:
            self.start_sec = array("d", (float(sp.start) for sp in span_list))
            self.end_sec = array("d", (float(sp.end) for sp in span_list))
            self.note = array("B", (int(sp.note) for sp in span_list))
            self.velocity = array("B", (int(sp.velocity) for sp in span_list))
            self.channel = array("B", (int(sp.channel) for sp in span_list))
            self.track = array("I", (int(sp.track) for sp in span_list))
            self._by_start = sorted(range(len(span_list)), key=lambda idx: (self.start_sec[idx], self.end_sec[idx], self.note[idx], self.channel[idx]))
            self._by_end = sorted(range(len(span_list)), key=lambda idx: (self.end_sec[idx], self.start_sec[idx], self.note[idx], self.channel[idx]))
            self._start_values = [self.start_sec[idx] for idx in self._by_start]
            self._end_values = [self.end_sec[idx] for idx in self._by_end]
        else:
            self.start_sec = array("d")
            self.end_sec = array("d")
            self.note = array("B")
            self.velocity = array("B")
            self.channel = array("B")
            self.track = array("I")
            self._by_start = []
            self._by_end = []
            self._start_values = []
            self._end_values = []

    @classmethod
    def from_python(
        cls,
        spans: Sequence[NoteSpan],
        controls: Sequence[ControlEvent],
        measure_times: Sequence[float],
        note_diag: Dict[str, int],
        source_kind: str = "python-packed",
        native_backend: bool = False,
    ) -> "PackedPlaybackSource":
        return cls(
            spans=spans,
            controls=controls,
            measure_times=measure_times,
            note_diag=note_diag,
            source_kind=source_kind,
            native_backend=native_backend,
        )

    @classmethod
    def from_native(
        cls,
        native_source: object,
        controls: Sequence[ControlEvent],
        measure_times: Sequence[float],
        note_diag: Dict[str, int],
    ) -> "PackedPlaybackSource":
        return cls(
            spans=[],
            controls=controls,
            measure_times=measure_times,
            note_diag=note_diag,
            source_kind=str(getattr(native_source, "source_kind", "native-packed")),
            native_backend=bool(getattr(native_source, "native_backend", True)),
            native_source=native_source,
            track_ids=[int(track) for track in native_source.track_ids()],
        )

    def to_note_spans(self) -> List[NoteSpan]:
        if not self._spans and self._native_source is not None:
            self._spans = _coerce_note_spans(self._native_source.note_tuples())
            if not self._track_ids:
                self._track_ids = sorted({int(getattr(sp, "track", 0)) for sp in self._spans})
        return list(self._spans)

    def visible_spans(self, t0: float, t1: float) -> List[NoteSpan]:
        if self._native_source is not None:
            return [
                NoteSpan(
                    note=int(note),
                    start=float(start),
                    end=float(end),
                    velocity=int(velocity),
                    channel=int(channel),
                    track=int(track),
                )
                for note, start, end, velocity, channel, track in self._native_source.visible_spans(float(t0), float(t1))
            ]
        i1 = bisect_right(self._start_values, float(t1))
        j0 = bisect_left(self._end_values, float(t0))
        if i1 <= (len(self._by_end) - j0):
            return [self._spans[idx] for idx in self._by_start[:i1] if self.end_sec[idx] >= float(t0)]
        out = [self._spans[idx] for idx in self._by_end[j0:] if self.start_sec[idx] <= float(t1)]
        out.sort(key=lambda sp: (float(sp.start), float(sp.end), int(sp.note), int(getattr(sp, "channel", 0))))
        return out

    def visible_note_tuples(self, t0: float, t1: float) -> List[Tuple[int, float, float, int, int, int]]:
        if self._native_source is not None:
            return [
                (
                    int(note),
                    float(start),
                    float(end),
                    int(velocity),
                    int(channel),
                    int(track),
                )
                for note, start, end, velocity, channel, track in self._native_source.visible_spans(float(t0), float(t1))
            ]
        return [
            (
                int(sp.note),
                float(sp.start),
                float(sp.end),
                int(sp.velocity),
                int(getattr(sp, "channel", 0)),
                int(getattr(sp, "track", 0)),
            )
            for sp in self.visible_spans(t0, t1)
        ]

    def native_density_data(
        self,
        *,
        t: float,
        note_lo: int,
        note_hi: int,
        play_rows: int,
        hit_row: int,
        x_scale: int,
        move_speed: float,
        length_speed: float,
        note_direction: str = "down",
    ) -> Optional[Dict[str, object]]:
        if self._native_source is None:
            return None
        cells, active, visible = self._native_source.visible_density_data(
            float(t),
            int(note_lo),
            int(note_hi),
            int(play_rows),
            int(hit_row),
            int(x_scale),
            float(move_speed),
            float(length_speed),
            str(note_direction),
        )
        return {
            "cells": [
                (int(x), int(y), int(track), int(velocity), bool(is_black), int(density))
                for x, y, track, velocity, is_black, density in cells
            ],
            "active": [(int(note), int(track), int(velocity)) for note, track, velocity in active],
            "visible": [(int(note), int(track)) for note, track in visible],
        }

    def track_ids(self) -> List[int]:
        if self._track_ids:
            return list(self._track_ids)
        return sorted({int(getattr(sp, "track", 0)) for sp in self.to_note_spans()})


def _coerce_note_spans(rows: Sequence[Tuple[int, float, float, int, int, int]]) -> List[NoteSpan]:
    return [
        NoteSpan(
            note=int(note),
            start=float(start),
            end=float(end),
            velocity=int(velocity),
            channel=int(channel),
            track=int(track),
        )
        for note, start, end, velocity, channel, track in rows
    ] 


def start_background_packed_playback_load(
    midi_path: str,
    stuck_note_fix_mode: str = "off",
    midi_flip: bool = False,
    flip_bounds: Optional[Tuple[int, int]] = None,
    long_note_threshold: float = 0.0,
    long_note_duration: float = 0.0,
    initial_ready_sec: Optional[float] = None,
) -> BackgroundPlaybackLoadTask:
    if not native_packed_task_available():
        raise RuntimeError("Native packed background load task is unavailable")
    flip_lo = None
    flip_hi = None
    if flip_bounds is not None:
        flip_lo = int(flip_bounds[0])
        flip_hi = int(flip_bounds[1])
    native_task = start_packed_playback_load_task_native(
        midi_path,
        str(stuck_note_fix_mode),
        bool(midi_flip),
        flip_lo,
        flip_hi,
        max(0.0, float(long_note_threshold)),
        max(0.0, float(long_note_duration)),
        None if initial_ready_sec is None else float(initial_ready_sec),
    )
    return BackgroundPlaybackLoadTask(native_task)


def _coerce_control_events(rows: Sequence[Tuple[float, int, int, int, int]]) -> List[ControlEvent]:
    return [
        ControlEvent(
            time_sec=float(time_sec),
            channel=int(channel),
            controller=int(controller),
            value=int(value),
            track=int(track),
        )
        for time_sec, channel, controller, value, track in rows
    ]


def load_selected_playback_data(
    midi_path: str,
    stuck_note_fix_mode: str = "off",
    midi_flip: bool = False,
    flip_bounds: Optional[Tuple[int, int]] = None,
    long_note_threshold: float = 0.0,
    long_note_duration: float = 0.0,
    playback_engine: str = "auto",
) -> LoadedPlaybackData:
    engine_requested = resolve_playback_engine(playback_engine)
    use_packed = engine_requested == "packed" or (
        engine_requested == "auto" and should_auto_use_packed(midi_path)
    )
    spans: Optional[List[NoteSpan]] = None
    controls: List[ControlEvent] = []
    measure_times: List[float] = []
    note_diag: Dict[str, int] = {}

    packed_source: Optional[PackedPlaybackSource] = None
    engine_used = "python"
    native_backend = False

    if use_packed:
        if native_packed_available():
            flip_lo = None
            flip_hi = None
            if flip_bounds is not None:
                flip_lo = int(flip_bounds[0])
                flip_hi = int(flip_bounds[1])
            native_source = load_packed_playback_source_native(
                midi_path,
                str(stuck_note_fix_mode),
                bool(midi_flip),
                flip_lo,
                flip_hi,
                max(0.0, float(long_note_threshold)),
                max(0.0, float(long_note_duration)),
            )
            native_controls = _coerce_control_events(native_source.control_tuples())
            native_measure_times = [float(value) for value in native_source.measure_times()]
            native_note_diag = {
                key: int(value) for key, value in dict(native_source.note_diag()).items()
            }
            packed_source = PackedPlaybackSource.from_native(
                native_source=native_source,
                controls=native_controls,
                measure_times=native_measure_times,
                note_diag=native_note_diag,
            )
            controls = native_controls
            measure_times = native_measure_times
            note_diag = native_note_diag
            native_backend = True
        engine_used = "packed"
        if packed_source is None:
            spans, controls, measure_times, note_diag = load_playback_source(
                midi_path,
                stuck_note_fix_mode=stuck_note_fix_mode,
                midi_flip=midi_flip,
                flip_bounds=flip_bounds,
                long_note_threshold=long_note_threshold,
                long_note_duration=long_note_duration,
            )
            packed_source = PackedPlaybackSource.from_python(
                spans,
                controls,
                measure_times,
                note_diag,
                source_kind="python-packed",
                native_backend=False,
            )
    else:
        spans, controls, measure_times, note_diag = load_playback_source(
            midi_path,
            stuck_note_fix_mode=stuck_note_fix_mode,
            midi_flip=midi_flip,
            flip_bounds=flip_bounds,
            long_note_threshold=long_note_threshold,
            long_note_duration=long_note_duration,
        )

    return LoadedPlaybackData(
        spans=spans,
        controls=controls,
        measure_times=measure_times,
        note_diag=note_diag,
        packed_source=packed_source,
        engine_requested=engine_requested,
        engine_used=engine_used,
        native_backend=native_backend,
    )
