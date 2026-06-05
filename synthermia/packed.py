from __future__ import annotations

import os
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from synthermia.midi import load_playback_source
from synthermia.models import ControlEvent, NoteSpan

try:
    from synthermia_native import load_packed_playback_source_native, native_runtime_info
except Exception:
    load_packed_playback_source_native = None
    native_runtime_info = None


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


def should_auto_use_packed(midi_path: str) -> bool:
    try:
        size_bytes = os.path.getsize(midi_path)
    except OSError:
        return False
    return size_bytes >= (2 * 1024 * 1024)


@dataclass
class LoadedPlaybackData:
    spans: List[NoteSpan]
    controls: List[ControlEvent]
    measure_times: List[float]
    note_diag: Dict[str, int]
    packed_source: Optional["PackedPlaybackSource"]
    engine_requested: str
    engine_used: str
    native_backend: bool


class PackedPlaybackSource:
    def __init__(
        self,
        spans: Sequence[NoteSpan],
        controls: Sequence[ControlEvent],
        measure_times: Sequence[float],
        note_diag: Dict[str, int],
        source_kind: str,
        native_backend: bool,
    ) -> None:
        span_list = list(spans)
        self._spans = span_list
        self.controls = list(controls)
        self.measure_times = list(measure_times)
        self.note_diag = dict(note_diag)
        self.source_kind = str(source_kind)
        self.native_backend = bool(native_backend)
        self.duration_sec = max((float(sp.end) for sp in span_list), default=0.0)
        self.note_count = len(span_list)
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

    def to_note_spans(self) -> List[NoteSpan]:
        return list(self._spans)

    def visible_spans(self, t0: float, t1: float) -> List[NoteSpan]:
        i1 = bisect_right(self._start_values, float(t1))
        j0 = bisect_left(self._end_values, float(t0))
        if i1 <= (len(self._by_end) - j0):
            return [self._spans[idx] for idx in self._by_start[:i1] if self.end_sec[idx] >= float(t0)]
        out = [self._spans[idx] for idx in self._by_end[j0:] if self.start_sec[idx] <= float(t1)]
        out.sort(key=lambda sp: (float(sp.start), float(sp.end), int(sp.note), int(getattr(sp, "channel", 0))))
        return out


def load_selected_playback_data(
    midi_path: str,
    stuck_note_fix_mode: str = "off",
    midi_flip: bool = False,
    flip_bounds: Optional[Tuple[int, int]] = None,
    playback_engine: str = "auto",
) -> LoadedPlaybackData:
    engine_requested = resolve_playback_engine(playback_engine)
    use_packed = engine_requested == "packed" or (
        engine_requested == "auto" and should_auto_use_packed(midi_path)
    )

    spans, controls, measure_times, note_diag = load_playback_source(
        midi_path,
        stuck_note_fix_mode=stuck_note_fix_mode,
        midi_flip=midi_flip,
        flip_bounds=flip_bounds,
    )

    packed_source: Optional[PackedPlaybackSource] = None
    engine_used = "python"
    native_backend = False

    if use_packed:
        if native_packed_available():
            try:
                flip_lo = None
                flip_hi = None
                if flip_bounds is not None:
                    flip_lo = int(flip_bounds[0])
                    flip_hi = int(flip_bounds[1])
                load_packed_playback_source_native(
                    midi_path,
                    str(stuck_note_fix_mode),
                    bool(midi_flip),
                    flip_lo,
                    flip_hi,
                )
                native_backend = True
            except Exception:
                native_backend = False
        engine_used = "packed"
        packed_source = PackedPlaybackSource.from_python(
            spans,
            controls,
            measure_times,
            note_diag,
            source_kind="native-probed" if native_backend else "python-packed",
            native_backend=native_backend,
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
