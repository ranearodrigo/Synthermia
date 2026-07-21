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
except Exception:
    parse_midi_track_summaries_native = None


def read_u32_be(data: bytes, i: int) -> Tuple[int, int]:
    return struct.unpack(">I", data[i : i + 4])[0], i + 4


def read_u16_be(data: bytes, i: int) -> Tuple[int, int]:
    return struct.unpack(">H", data[i : i + 2])[0], i + 2


def read_varlen(data: bytes, i: int) -> Tuple[int, int]:
    value = 0
    while True:
        b = data[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, i


def write_varlen(value: int) -> bytes:
    if value < 0:
        raise ValueError("Variable-length value must be non-negative")
    buf = [value & 0x7F]
    value >>= 7
    while value:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buf))


def parse_midi(path: str) -> Tuple[MidiHeader, List[RawEvent]]:
    with open(path, "rb") as f:
        data = f.read()

    i = 0
    if data[i : i + 4] != b"MThd":
        raise ValueError("Invalid MIDI: missing MThd header")
    i += 4
    hdr_len, i = read_u32_be(data, i)
    if hdr_len < 6:
        raise ValueError("Invalid MIDI: bad header length")

    fmt, i = read_u16_be(data, i)
    ntrks, i = read_u16_be(data, i)
    division, i = read_u16_be(data, i)
    i += hdr_len - 6

    header = MidiHeader(fmt=fmt, ntrks=ntrks, division=division)
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")

    events: List[RawEvent] = []

    for track_idx in range(ntrks):
        if data[i : i + 4] != b"MTrk":
            raise ValueError("Invalid MIDI: missing MTrk chunk")
        i += 4
        trk_len, i = read_u32_be(data, i)
        trk_end = i + trk_len

        abs_tick = 0
        running_status: Optional[int] = None

        while i < trk_end:
            delta, i = read_varlen(data, i)
            abs_tick += delta

            status = data[i]
            if status < 0x80:
                if running_status is None:
                    raise ValueError("Invalid MIDI: running status missing")
                status = running_status
            else:
                i += 1
                running_status = status if status < 0xF0 else None

            if status == 0xFF:
                meta_type = data[i]
                i += 1
                length, i = read_varlen(data, i)
                payload = data[i : i + length]
                i += length

                if meta_type == 0x51 and length == 3:
                    tempo = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                    events.append(
                        RawEvent(
                            tick=abs_tick,
                            order=0,
                            kind="tempo",
                            track=track_idx,
                            tempo_us_per_qn=tempo,
                        )
                    )
                elif meta_type == 0x58 and length >= 2:
                    numer = int(payload[0]) if len(payload) >= 1 else 4
                    denom_pow = int(payload[1]) if len(payload) >= 2 else 2
                    denom = 1 << max(0, min(7, denom_pow))
                    if numer > 0 and denom > 0:
                        events.append(
                            RawEvent(
                                tick=abs_tick,
                                order=0,
                                kind="time_signature",
                                track=track_idx,
                                time_sig_numerator=numer,
                                time_sig_denominator=denom,
                            )
                        )
                continue

            if status in (0xF0, 0xF7):
                length, i = read_varlen(data, i)
                i += length
                continue

            msg_type = status & 0xF0
            channel = status & 0x0F

            if msg_type in (0xC0, 0xD0):
                i += 1
                continue

            p1 = data[i]
            p2 = data[i + 1]
            i += 2

            if msg_type == 0xB0:
                events.append(
                    RawEvent(
                        tick=abs_tick,
                        order=1,
                        kind="control_change",
                        channel=channel,
                        track=track_idx,
                        controller=p1,
                        value=p2,
                    )
                )
            elif msg_type == 0x90:
                note = p1
                vel = p2
                if vel == 0:
                    events.append(
                        RawEvent(
                            tick=abs_tick,
                            order=1,
                            kind="note_off",
                            note=note,
                            velocity=vel,
                            channel=channel,
                            track=track_idx,
                        )
                    )
                else:
                    events.append(
                        RawEvent(
                            tick=abs_tick,
                            order=2,
                            kind="note_on",
                            note=note,
                            velocity=vel,
                            channel=channel,
                            track=track_idx,
                        )
                    )
            elif msg_type == 0x80:
                events.append(
                    RawEvent(
                        tick=abs_tick,
                        order=1,
                        kind="note_off",
                        note=p1,
                        velocity=p2,
                        channel=channel,
                        track=track_idx,
                    )
                )

        i = trk_end

    events.sort(key=lambda e: (e.tick, e.order))
    return header, events


def parse_midi_track_summaries(path: str) -> List[MidiTrackSummary]:
    if callable(parse_midi_track_summaries_native):
        try:
            return [
                MidiTrackSummary(
                    index=int(index),
                    name=str(name),
                    note_ons=int(note_ons),
                    instrument_name=str(instrument_name),
                )
                for index, name, note_ons, instrument_name in parse_midi_track_summaries_native(path)
            ]
        except Exception:
            pass

    with open(path, "rb") as f:
        data = f.read()

    i = 0
    if data[i : i + 4] != b"MThd":
        raise ValueError("Invalid MIDI: missing MThd header")
    i += 4
    hdr_len, i = read_u32_be(data, i)
    if hdr_len < 6:
        raise ValueError("Invalid MIDI: bad header length")
    _fmt, i = read_u16_be(data, i)
    ntrks, i = read_u16_be(data, i)
    division, i = read_u16_be(data, i)
    i += hdr_len - 6
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")

    summaries: List[MidiTrackSummary] = []
    for track_idx in range(ntrks):
        if data[i : i + 4] != b"MTrk":
            raise ValueError("Invalid MIDI: missing MTrk chunk")
        i += 4
        trk_len, i = read_u32_be(data, i)
        trk_end = i + trk_len
        running_status: Optional[int] = None
        note_ons = 0
        track_name = f"Track {track_idx + 1}"
        instrument_name = ""
        name_seen = False
        instrument_seen = False

        while i < trk_end:
            _delta, i = read_varlen(data, i)
            status = data[i]
            if status < 0x80:
                if running_status is None:
                    raise ValueError("Invalid MIDI: running status missing")
                status = running_status
            else:
                i += 1
                running_status = status if status < 0xF0 else None

            if status == 0xFF:
                meta_type = data[i]
                i += 1
                length, i = read_varlen(data, i)
                payload = data[i : i + length]
                i += length
                if meta_type == 0x03 and not name_seen:
                    name_txt = _clean_midi_text(payload)
                    if name_txt:
                        track_name = name_txt
                        name_seen = True
                elif meta_type == 0x04 and not instrument_seen:
                    inst_txt = _clean_midi_text(payload)
                    if inst_txt:
                        instrument_name = inst_txt
                        instrument_seen = True
                continue

            if status in (0xF0, 0xF7):
                length, i = read_varlen(data, i)
                i += length
                continue

            msg_type = status & 0xF0
            if msg_type in (0xC0, 0xD0):
                i += 1
                continue

            _p1 = data[i]
            p2 = data[i + 1]
            i += 2
            if msg_type == 0x90 and p2 > 0:
                note_ons += 1

        i = trk_end
        summaries.append(
            MidiTrackSummary(
                index=track_idx,
                name=track_name,
                note_ons=note_ons,
                instrument_name=instrument_name,
            )
        )
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
