from __future__ import annotations

import struct
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from typing import Dict, List, Optional, Sequence, Tuple

from synthermia.models import (
    DEFAULT_TEMPO_US_PER_QN,
    ControlEvent,
    MAX_PIANO_NOTE,
    MIN_PIANO_NOTE,
    MidiHeader,
    MidiTrackSummary,
    NoteSpan,
    RawEvent,
)

try:
    from synthermia_native import parse_midi_track_summaries_native
except ImportError:
    parse_midi_track_summaries_native = None


def read_u32_be(data: bytes, i: int) -> Tuple[int, int]:
    if i + 4 > len(data):
        raise ValueError("Invalid MIDI: truncated u32")
    return struct.unpack_from(">I", data, i)[0], i + 4


def read_u16_be(data: bytes, i: int) -> Tuple[int, int]:
    if i + 2 > len(data):
        raise ValueError("Invalid MIDI: truncated u16")
    return struct.unpack_from(">H", data, i)[0], i + 2


def read_varlen(data: bytes, i: int, end: Optional[int] = None) -> Tuple[int, int]:
    limit = len(data) if end is None else end
    value = 0
    for _ in range(4):
        if i >= limit:
            raise ValueError("Invalid MIDI: truncated varlen")
        b = data[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, i
    raise ValueError("Invalid MIDI: varlen exceeds four bytes")


def write_varlen(value: int) -> bytes:
    if value < 0:
        raise ValueError("Variable-length value must be non-negative")
    buf = [value & 0x7F]
    value >>= 7
    while value:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buf))


def _read_midi_file(path: str) -> Tuple[MidiHeader, bytes, List[Tuple[int, int]]]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"MThd":
        raise ValueError("Invalid MIDI: missing MThd header")
    hdr_len, i = read_u32_be(data, 4)
    if hdr_len < 6:
        raise ValueError("Invalid MIDI: bad header length")
    if i + hdr_len > len(data):
        raise ValueError("Invalid MIDI: truncated header")
    fmt, i = read_u16_be(data, i)
    ntrks, i = read_u16_be(data, i)
    division, i = read_u16_be(data, i)
    i += hdr_len - 6
    if fmt not in (0, 1):
        raise ValueError("Invalid MIDI: only formats 0 and 1 are supported")
    if ntrks == 0 or (fmt == 0 and ntrks != 1):
        raise ValueError("Invalid MIDI: bad track count")
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")
    if division == 0:
        raise ValueError("Invalid MIDI: time division must be positive")
    tracks = []
    for _ in range(ntrks):
        if data[i : i + 4] != b"MTrk":
            raise ValueError("Invalid MIDI: missing MTrk chunk")
        length, i = read_u32_be(data, i + 4)
        end = i + length
        if end > len(data):
            raise ValueError("Invalid MIDI: truncated track chunk")
        tracks.append((i, end))
        i = end
    return MidiHeader(fmt, ntrks, division), data, tracks


def _iter_track_events(data: bytes, start: int, end: int):
    i = start
    tick = 0
    running_status = None
    while i < end:
        delta, i = read_varlen(data, i, end)
        tick += delta
        if i >= end:
            raise ValueError("Invalid MIDI: truncated event")
        status = data[i]
        if status < 0x80:
            if running_status is None:
                raise ValueError("Invalid MIDI: running status missing")
            status = running_status
        else:
            i += 1
            running_status = status if status < 0xF0 else None
        meta_type = None
        if status == 0xFF:
            if i >= end:
                raise ValueError("Invalid MIDI: truncated meta event")
            meta_type = data[i]
            i += 1
            length, i = read_varlen(data, i, end)
        elif status in (0xF0, 0xF7):
            length, i = read_varlen(data, i, end)
        elif 0x80 <= status <= 0xEF:
            length = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
        else:
            raise ValueError("Invalid MIDI: unsupported status byte")
        if i + length > end:
            raise ValueError("Invalid MIDI: truncated event payload")
        payload = data[i : i + length]
        i += length
        if status < 0xF0 and any(value >= 0x80 for value in payload):
            raise ValueError("Invalid MIDI: channel data must be seven-bit")
        if meta_type == 0x51 and (length != 3 or int.from_bytes(payload, "big") == 0):
            raise ValueError("Invalid MIDI: bad tempo")
        yield tick, status, meta_type, payload


def parse_midi(path: str) -> Tuple[MidiHeader, List[RawEvent]]:
    header, data, tracks = _read_midi_file(path)
    events: List[RawEvent] = []
    for track_idx, (start, end) in enumerate(tracks):
        for tick, status, meta_type, payload in _iter_track_events(data, start, end):
            if meta_type == 0x51:
                events.append(RawEvent(tick=tick, order=0, kind="tempo", track=track_idx,
                                       tempo_us_per_qn=int.from_bytes(payload, "big")))
            elif meta_type == 0x58 and len(payload) >= 2:
                numer, denom_pow = payload[:2]
                if numer > 0:
                    events.append(RawEvent(tick=tick, order=0, kind="time_signature", track=track_idx,
                                           time_sig_numerator=numer, time_sig_denominator=1 << min(7, denom_pow)))
            elif status < 0xF0:
                msg_type = status & 0xF0
                channel = status & 0x0F
                if msg_type == 0xB0:
                    events.append(RawEvent(tick=tick, order=1, kind="control_change", channel=channel,
                                           track=track_idx, controller=payload[0], value=payload[1]))
                elif msg_type in (0x80, 0x90):
                    note_on = msg_type == 0x90 and payload[1] > 0
                    events.append(RawEvent(tick=tick, order=2 if note_on else 1,
                                           kind="note_on" if note_on else "note_off", note=payload[0],
                                           velocity=payload[1], channel=channel, track=track_idx))
    events.sort(key=lambda e: (e.tick, e.order))
    return header, events


def parse_midi_track_summaries(path: str) -> List[MidiTrackSummary]:
    if callable(parse_midi_track_summaries_native):
        return [
            MidiTrackSummary(index=int(index), name=str(name), note_ons=int(note_ons), instrument_name=str(instrument))
            for index, name, note_ons, instrument in parse_midi_track_summaries_native(path)
        ]
    _header, data, tracks = _read_midi_file(path)
    summaries = []
    for track_idx, (start, end) in enumerate(tracks):
        track_name = ""
        instrument_name = ""
        note_ons = 0
        for _tick, status, meta_type, payload in _iter_track_events(data, start, end):
            if meta_type == 0x03 and not track_name:
                track_name = _clean_midi_text(payload)
            elif meta_type == 0x04 and not instrument_name:
                instrument_name = _clean_midi_text(payload)
            elif status & 0xF0 == 0x90 and payload[1] > 0:
                note_ons += 1
        summaries.append(MidiTrackSummary(track_idx, track_name or f"Track {track_idx + 1}", note_ons, instrument_name))
    return summaries


def _clean_midi_text(payload: bytes) -> str:
    """Remove MIDI text padding that terminal drawing APIs cannot render."""
    return payload.decode("latin1", errors="ignore").replace("\x00", "").strip()


def build_tempo_segments(
    events: Sequence[RawEvent], ticks_per_qn: int
) -> List[Tuple[int, float, int]]:
    tempos: List[Tuple[int, int]] = [(0, DEFAULT_TEMPO_US_PER_QN)]
    for ev in events:
        if ev.kind == "tempo" and ev.tempo_us_per_qn is not None:
            tempos.append((ev.tick, ev.tempo_us_per_qn))

    tempos.sort(key=lambda x: x[0])

    dedup: List[Tuple[int, int]] = []
    for t, tempo in tempos:
        if dedup and dedup[-1][0] == t:
            dedup[-1] = (t, tempo)
        else:
            dedup.append((t, tempo))

    segments: List[Tuple[int, float, int]] = []
    cur_tick = 0
    cur_sec = 0.0
    cur_tempo = dedup[0][1]
    segments.append((cur_tick, cur_sec, cur_tempo))

    for nxt_tick, nxt_tempo in dedup[1:]:
        dt = nxt_tick - cur_tick
        cur_sec += dt * (cur_tempo / 1_000_000.0) / ticks_per_qn
        cur_tick = nxt_tick
        cur_tempo = nxt_tempo
        segments.append((cur_tick, cur_sec, cur_tempo))

    return segments


def tick_to_sec(tick: int, tempo_segments: Sequence[Tuple[int, float, int]], ticks_per_qn: int) -> float:
    lo = 0
    hi = len(tempo_segments) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tempo_segments[mid][0] <= tick:
            lo = mid
        else:
            hi = mid - 1

    seg_tick, seg_sec, seg_tempo = tempo_segments[lo]
    return seg_sec + (tick - seg_tick) * (seg_tempo / 1_000_000.0) / ticks_per_qn


def normalize_stuck_note_fix_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"off", "none", "disable", "disabled"}:
        return "off"
    if mode in {"lifo", "stack", "recent", "newest"}:
        return "lifo"
    if mode in {"auto", "on", "enabled", "retrigger", "trim", "guard"}:
        return "auto"
    return "off"


def build_note_spans(
    events: Sequence[RawEvent],
    ticks_per_qn: int,
    stuck_note_fix_mode: str = "off",
    long_note_threshold: float = 0.0,
    long_note_duration: float = 0.0,
    diagnostics: Optional[Dict[str, int]] = None,
) -> List[NoteSpan]:
    tempo_segments = build_tempo_segments(events, ticks_per_qn)
    mode = normalize_stuck_note_fix_mode(stuck_note_fix_mode)
    tick_sec_cache: Dict[int, float] = {}

    def tick_sec(tick: int) -> float:
        cached = tick_sec_cache.get(tick)
        if cached is not None:
            return cached
        sec = tick_to_sec(tick, tempo_segments, ticks_per_qn)
        tick_sec_cache[tick] = sec
        return sec

    on_map: Dict[Tuple[int, int, int], deque[Tuple[int, int]]] = defaultdict(deque)
    spans: List[NoteSpan] = []
    overlap_fixes = 0
    orphan_note_offs = 0
    long_note_trims = 0
    threshold = max(0.0, float(long_note_threshold))
    trimmed_duration = max(0.0, float(long_note_duration))
    trim_long_notes = threshold > 0.0 and trimmed_duration > 0.0

    for ev in events:
        if ev.kind == "note_on" and ev.note is not None and ev.channel is not None and ev.velocity is not None:
            track_idx = int(ev.track) if ev.track is not None else 0
            key = (track_idx, ev.channel, ev.note)
            q = on_map[key]
            q.append((ev.tick, ev.velocity))
        elif ev.kind == "note_off" and ev.note is not None and ev.channel is not None:
            track_idx = int(ev.track) if ev.track is not None else 0
            q = on_map.get((track_idx, ev.channel, ev.note))
            if not q:
                orphan_note_offs += 1
                continue
            if mode in {"auto", "lifo"} and len(q) > 1:
                start_tick, vel = q.pop()
                overlap_fixes += 1
            else:
                start_tick, vel = q.popleft()
            if ev.tick <= start_tick:
                continue
            start_sec = tick_sec(start_tick)
            end_sec = tick_sec(ev.tick)
            if trim_long_notes and (end_sec - start_sec) > threshold:
                capped_end = min(end_sec, start_sec + trimmed_duration)
                if capped_end < end_sec:
                    end_sec = capped_end
                    long_note_trims += 1
            spans.append(
                NoteSpan(
                    note=ev.note,
                    start=start_sec,
                    end=end_sec,
                    velocity=vel,
                    channel=ev.channel,
                    track=track_idx,
                )
            )

    if diagnostics is not None:
        diagnostics["stuck_note_fix_mode"] = 0 if mode == "off" else (1 if mode == "auto" else 2)
        diagnostics["overlap_fixes"] = int(overlap_fixes)
        diagnostics["orphan_note_offs"] = int(orphan_note_offs)
        diagnostics["long_note_trims"] = int(long_note_trims)
        diagnostics["remaining_unmatched_note_ons"] = int(sum(len(v) for v in on_map.values()))

    spans.sort(key=lambda s: s.start)
    return spans


def build_control_events(events: Sequence[RawEvent], ticks_per_qn: int) -> List[ControlEvent]:
    tempo_segments = build_tempo_segments(events, ticks_per_qn)
    controls: List[ControlEvent] = []
    for ev in events:
        if ev.kind != "control_change":
            continue
        if ev.channel is None or ev.controller is None or ev.value is None:
            continue
        controls.append(
            ControlEvent(
                time_sec=tick_to_sec(ev.tick, tempo_segments, ticks_per_qn),
                channel=max(0, min(15, int(ev.channel))),
                controller=max(0, min(127, int(ev.controller))),
                value=max(0, min(127, int(ev.value))),
                track=int(ev.track) if ev.track is not None else 0,
            )
        )
    controls.sort(key=lambda c: c.time_sec)
    return controls


def build_measure_times(events: Sequence[RawEvent], ticks_per_qn: int) -> List[float]:
    if ticks_per_qn <= 0:
        return []
    tempo_segments = build_tempo_segments(events, ticks_per_qn)
    max_tick = max((int(ev.tick) for ev in events), default=0)
    if max_tick < 0:
        max_tick = 0

    sig_changes: List[Tuple[int, int, int]] = [(0, 4, 4)]
    for ev in events:
        if ev.kind != "time_signature":
            continue
        numer = int(ev.time_sig_numerator) if ev.time_sig_numerator is not None else 4
        denom = int(ev.time_sig_denominator) if ev.time_sig_denominator is not None else 4
        if numer <= 0 or denom <= 0:
            continue
        sig_changes.append((max(0, int(ev.tick)), numer, denom))

    sig_changes.sort(key=lambda it: it[0])
    dedup: List[Tuple[int, int, int]] = []
    for tick, numer, denom in sig_changes:
        if dedup and dedup[-1][0] == tick:
            dedup[-1] = (tick, numer, denom)
        else:
            dedup.append((tick, numer, denom))

    measure_ticks: List[int] = []
    for i, (start_tick, numer, denom) in enumerate(dedup):
        end_tick = dedup[i + 1][0] if i + 1 < len(dedup) else max_tick + 1
        if end_tick <= start_tick:
            continue
        bar_ticks = max(1, int(round(float(ticks_per_qn) * 4.0 * float(numer) / float(denom))))
        tick = start_tick
        while tick < end_tick:
            measure_ticks.append(tick)
            tick += bar_ticks

    # Keep stable ordering and remove duplicates from TS changes on bar boundaries.
    measure_ticks = sorted(set(measure_ticks))
    return [tick_to_sec(int(tk), tempo_segments, ticks_per_qn) for tk in measure_ticks]


def load_playback_source(
    midi_path: str,
    stuck_note_fix_mode: str = "off",
    midi_flip: bool = False,
    flip_bounds: Optional[Tuple[int, int]] = None,
    long_note_threshold: float = 0.0,
    long_note_duration: float = 0.0,
) -> Tuple[List[NoteSpan], List[ControlEvent], List[float], Dict[str, int]]:
    header, events = parse_midi(midi_path)
    note_diag: Dict[str, int] = {}
    spans = build_note_spans(
        events,
        header.division,
        stuck_note_fix_mode=stuck_note_fix_mode,
        long_note_threshold=long_note_threshold,
        long_note_duration=long_note_duration,
        diagnostics=note_diag,
    )
    if midi_flip:
        spans = apply_midi_flip_to_spans(spans, flip_bounds)
    controls = build_control_events(events, header.division)
    measure_times = build_measure_times(events, header.division)
    return spans, controls, measure_times, note_diag


def apply_midi_flip_to_spans(
    spans: Sequence[NoteSpan],
    bounds: Optional[Tuple[int, int]] = None,
) -> List[NoteSpan]:
    lo, hi = bounds if bounds is not None else (MIN_PIANO_NOTE, MAX_PIANO_NOTE)
    lo = max(0, min(127, int(lo)))
    hi = max(0, min(127, int(hi)))
    if lo > hi:
        lo, hi = hi, lo
    out: List[NoteSpan] = []
    for sp in spans:
        flipped_note = lo + hi - int(sp.note)
        flipped_note = max(0, min(127, flipped_note))
        out.append(
            NoteSpan(
                note=flipped_note,
                start=sp.start,
                end=sp.end,
                velocity=sp.velocity,
                channel=sp.channel,
                track=sp.track,
            )
        )
    return out
