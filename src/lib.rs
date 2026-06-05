use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cmp::Ordering;
use std::collections::{HashMap, VecDeque};
use std::fs;

const DEFAULT_TEMPO_US_PER_QN: u32 = 500_000;
const MIN_PIANO_NOTE: u8 = 21;
const MAX_PIANO_NOTE: u8 = 108;

#[derive(Clone, Copy, PartialEq, Eq)]
enum EventKind {
    Tempo,
    TimeSignature,
    ControlChange,
    NoteOn,
    NoteOff,
}

#[derive(Clone)]
struct RawEvent {
    tick: u64,
    order: u8,
    kind: EventKind,
    note: Option<u8>,
    velocity: Option<u8>,
    channel: Option<u8>,
    track: u32,
    controller: Option<u8>,
    value: Option<u8>,
    tempo_us_per_qn: Option<u32>,
    time_sig_numerator: Option<u32>,
    time_sig_denominator: Option<u32>,
}

#[derive(Clone, Copy)]
struct TempoSegment {
    tick: u64,
    sec: f64,
    tempo_us_per_qn: u32,
}

#[derive(Clone, Copy)]
struct NativeSpan {
    note: u8,
    start: f64,
    end: f64,
    velocity: u8,
    channel: u8,
    track: u32,
}

#[derive(Clone, Copy)]
struct NativeControl {
    time_sec: f64,
    channel: u8,
    controller: u8,
    value: u8,
    track: u32,
}

#[derive(Clone, Copy)]
struct NoteDiagnostics {
    stuck_note_fix_mode: u8,
    overlap_fixes: u64,
    orphan_note_offs: u64,
    remaining_unmatched_note_ons: u64,
}

#[pyclass(module = "synthermia_native")]
struct NativePackedPlaybackSource {
    start_sec: Vec<f64>,
    end_sec: Vec<f64>,
    note: Vec<u8>,
    velocity: Vec<u8>,
    channel: Vec<u8>,
    track: Vec<u32>,
    by_start: Vec<usize>,
    by_end: Vec<usize>,
    start_values: Vec<f64>,
    end_values: Vec<f64>,
    controls: Vec<NativeControl>,
    measure_times: Vec<f64>,
    note_diag: NoteDiagnostics,
    duration_sec: f64,
    source_kind: String,
}

impl NativePackedPlaybackSource {
    fn from_rows(
        spans: Vec<NativeSpan>,
        controls: Vec<NativeControl>,
        measure_times: Vec<f64>,
        note_diag: NoteDiagnostics,
    ) -> Self {
        let duration_sec = spans.iter().map(|sp| sp.end).fold(0.0, f64::max);
        let start_sec: Vec<f64> = spans.iter().map(|sp| sp.start).collect();
        let end_sec: Vec<f64> = spans.iter().map(|sp| sp.end).collect();
        let note: Vec<u8> = spans.iter().map(|sp| sp.note).collect();
        let velocity: Vec<u8> = spans.iter().map(|sp| sp.velocity).collect();
        let channel: Vec<u8> = spans.iter().map(|sp| sp.channel).collect();
        let track: Vec<u32> = spans.iter().map(|sp| sp.track).collect();

        let mut by_start: Vec<usize> = (0..spans.len()).collect();
        by_start.sort_by(|&a, &b| {
            total_cmp_f64(start_sec[a], start_sec[b])
                .then_with(|| total_cmp_f64(end_sec[a], end_sec[b]))
                .then_with(|| note[a].cmp(&note[b]))
                .then_with(|| channel[a].cmp(&channel[b]))
        });

        let mut by_end: Vec<usize> = (0..spans.len()).collect();
        by_end.sort_by(|&a, &b| {
            total_cmp_f64(end_sec[a], end_sec[b])
                .then_with(|| total_cmp_f64(start_sec[a], start_sec[b]))
                .then_with(|| note[a].cmp(&note[b]))
                .then_with(|| channel[a].cmp(&channel[b]))
        });

        let start_values = by_start.iter().map(|&idx| start_sec[idx]).collect();
        let end_values = by_end.iter().map(|&idx| end_sec[idx]).collect();

        Self {
            start_sec,
            end_sec,
            note,
            velocity,
            channel,
            track,
            by_start,
            by_end,
            start_values,
            end_values,
            controls,
            measure_times,
            note_diag,
            duration_sec,
            source_kind: "native-packed".to_string(),
        }
    }

    fn span_tuple(&self, idx: usize) -> (u8, f64, f64, u8, u8, u32) {
        (
            self.note[idx],
            self.start_sec[idx],
            self.end_sec[idx],
            self.velocity[idx],
            self.channel[idx],
            self.track[idx],
        )
    }
}

#[pymethods]
impl NativePackedPlaybackSource {
    #[getter]
    fn duration_sec(&self) -> f64 {
        self.duration_sec
    }

    #[getter]
    fn note_count(&self) -> usize {
        self.note.len()
    }

    #[getter]
    fn source_kind(&self) -> &str {
        &self.source_kind
    }

    #[getter]
    fn native_backend(&self) -> bool {
        true
    }

    fn visible_spans(&self, t0: f64, t1: f64) -> Vec<(u8, f64, f64, u8, u8, u32)> {
        let i1 = upper_bound(&self.start_values, t1);
        let j0 = lower_bound(&self.end_values, t0);

        if i1 <= (self.by_end.len().saturating_sub(j0)) {
            let mut out = Vec::with_capacity(i1);
            for &idx in &self.by_start[..i1] {
                if self.end_sec[idx] >= t0 {
                    out.push(self.span_tuple(idx));
                }
            }
            return out;
        }

        let mut out = Vec::with_capacity(self.by_end.len().saturating_sub(j0));
        for &idx in &self.by_end[j0..] {
            if self.start_sec[idx] <= t1 {
                out.push(self.span_tuple(idx));
            }
        }
        out.sort_by(|a, b| {
            total_cmp_f64(a.1, b.1)
                .then_with(|| total_cmp_f64(a.2, b.2))
                .then_with(|| a.0.cmp(&b.0))
                .then_with(|| a.4.cmp(&b.4))
        });
        out
    }

    fn note_tuples(&self) -> Vec<(u8, f64, f64, u8, u8, u32)> {
        (0..self.note.len()).map(|idx| self.span_tuple(idx)).collect()
    }

    fn control_tuples(&self) -> Vec<(f64, u8, u8, u8, u32)> {
        self.controls
            .iter()
            .map(|ev| (ev.time_sec, ev.channel, ev.controller, ev.value, ev.track))
            .collect()
    }

    fn measure_times(&self) -> Vec<f64> {
        self.measure_times.clone()
    }

    fn note_diag<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item(
            "stuck_note_fix_mode",
            i32::from(self.note_diag.stuck_note_fix_mode),
        )?;
        out.set_item("overlap_fixes", self.note_diag.overlap_fixes)?;
        out.set_item("orphan_note_offs", self.note_diag.orphan_note_offs)?;
        out.set_item(
            "remaining_unmatched_note_ons",
            self.note_diag.remaining_unmatched_note_ons,
        )?;
        Ok(out)
    }

    fn __repr__(&self) -> String {
        format!(
            "NativePackedPlaybackSource(note_count={}, duration_sec={:.3})",
            self.note.len(),
            self.duration_sec
        )
    }
}

#[pyfunction]
fn native_runtime_info() -> &'static str {
    "synthermia_native packed loader"
}

#[pyfunction]
#[pyo3(signature = (midi_path, stuck_note_fix_mode="off", midi_flip=false, flip_lo=None, flip_hi=None))]
fn load_packed_playback_source_native(
    midi_path: &str,
    stuck_note_fix_mode: &str,
    midi_flip: bool,
    flip_lo: Option<u8>,
    flip_hi: Option<u8>,
) -> PyResult<NativePackedPlaybackSource> {
    let (ticks_per_qn, mut events) = parse_midi_file(midi_path)?;
    events.sort_by(|a, b| a.tick.cmp(&b.tick).then_with(|| a.order.cmp(&b.order)));
    let (mut spans, note_diag) = build_note_spans(&events, ticks_per_qn, stuck_note_fix_mode);
    if midi_flip {
        apply_midi_flip(&mut spans, flip_lo, flip_hi);
    }
    let controls = build_control_events(&events, ticks_per_qn);
    let measure_times = build_measure_times(&events, ticks_per_qn);
    Ok(NativePackedPlaybackSource::from_rows(
        spans,
        controls,
        measure_times,
        note_diag,
    ))
}

fn parse_midi_file(path: &str) -> PyResult<(u16, Vec<RawEvent>)> {
    let data = fs::read(path)
        .map_err(|err| PyValueError::new_err(format!("Failed to read MIDI file: {err}")))?;
    parse_midi_bytes(&data)
}

fn parse_midi_bytes(data: &[u8]) -> PyResult<(u16, Vec<RawEvent>)> {
    let mut i = 0usize;
    ensure_chunk(data, i, b"MThd", "missing MThd header")?;
    i += 4;
    let (hdr_len, i2) = read_u32_be(data, i)?;
    i = i2;
    if hdr_len < 6 {
        return Err(PyValueError::new_err("Invalid MIDI: bad header length"));
    }

    let (_fmt, i2) = read_u16_be(data, i)?;
    i = i2;
    let (ntrks, i2) = read_u16_be(data, i)?;
    i = i2;
    let (division, i2) = read_u16_be(data, i)?;
    i = i2;
    if (division & 0x8000) != 0 {
        return Err(PyValueError::new_err(
            "SMPTE time division is not supported",
        ));
    }
    let hdr_len_usize = hdr_len as usize;
    if i + (hdr_len_usize.saturating_sub(6)) > data.len() {
        return Err(PyValueError::new_err("Invalid MIDI: truncated header"));
    }
    i += hdr_len_usize - 6;

    let mut events: Vec<RawEvent> = Vec::new();
    for track_idx in 0..ntrks {
        ensure_chunk(data, i, b"MTrk", "missing MTrk chunk")?;
        i += 4;
        let (trk_len, i2) = read_u32_be(data, i)?;
        i = i2;
        let trk_end = i
            .checked_add(trk_len as usize)
            .ok_or_else(|| PyValueError::new_err("Invalid MIDI: track length overflow"))?;
        if trk_end > data.len() {
            return Err(PyValueError::new_err("Invalid MIDI: truncated track chunk"));
        }

        let mut abs_tick: u64 = 0;
        let mut running_status: Option<u8> = None;

        while i < trk_end {
            let (delta, i2) = read_varlen(data, i)?;
            i = i2;
            abs_tick = abs_tick.saturating_add(delta);

            let status_byte = *data
                .get(i)
                .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated event"))?;
            let status: u8;
            if status_byte < 0x80 {
                status = running_status.ok_or_else(|| {
                    PyValueError::new_err("Invalid MIDI: running status missing")
                })?;
            } else {
                status = status_byte;
                i += 1;
                running_status = if status < 0xF0 { Some(status) } else { None };
            }

            if status == 0xFF {
                let meta_type = *data
                    .get(i)
                    .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated meta event"))?;
                i += 1;
                let (length, i2) = read_varlen(data, i)?;
                i = i2;
                let len = length as usize;
                if i + len > trk_end {
                    return Err(PyValueError::new_err("Invalid MIDI: truncated meta payload"));
                }
                let payload = &data[i..i + len];
                i += len;

                if meta_type == 0x51 && len == 3 {
                    let tempo =
                        ((payload[0] as u32) << 16) | ((payload[1] as u32) << 8) | payload[2] as u32;
                    events.push(RawEvent {
                        tick: abs_tick,
                        order: 0,
                        kind: EventKind::Tempo,
                        note: None,
                        velocity: None,
                        channel: None,
                        track: track_idx as u32,
                        controller: None,
                        value: None,
                        tempo_us_per_qn: Some(tempo),
                        time_sig_numerator: None,
                        time_sig_denominator: None,
                    });
                } else if meta_type == 0x58 && len >= 2 {
                    let numer = payload[0] as u32;
                    let denom_pow = payload[1] as u32;
                    let denom = 1u32 << denom_pow.min(7);
                    if numer > 0 && denom > 0 {
                        events.push(RawEvent {
                            tick: abs_tick,
                            order: 0,
                            kind: EventKind::TimeSignature,
                            note: None,
                            velocity: None,
                            channel: None,
                            track: track_idx as u32,
                            controller: None,
                            value: None,
                            tempo_us_per_qn: None,
                            time_sig_numerator: Some(numer),
                            time_sig_denominator: Some(denom),
                        });
                    }
                }
                continue;
            }

            if status == 0xF0 || status == 0xF7 {
                let (length, i2) = read_varlen(data, i)?;
                i = i2;
                let len = length as usize;
                if i + len > trk_end {
                    return Err(PyValueError::new_err("Invalid MIDI: truncated sysex payload"));
                }
                i += len;
                continue;
            }

            let msg_type = status & 0xF0;
            let channel = status & 0x0F;

            if msg_type == 0xC0 || msg_type == 0xD0 {
                if i >= trk_end {
                    return Err(PyValueError::new_err(
                        "Invalid MIDI: truncated short message",
                    ));
                }
                i += 1;
                continue;
            }

            let p1 = *data
                .get(i)
                .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated message"))?;
            let p2 = *data
                .get(i + 1)
                .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated message"))?;
            i += 2;

            match msg_type {
                0xB0 => events.push(RawEvent {
                    tick: abs_tick,
                    order: 1,
                    kind: EventKind::ControlChange,
                    note: None,
                    velocity: None,
                    channel: Some(channel),
                    track: track_idx as u32,
                    controller: Some(p1),
                    value: Some(p2),
                    tempo_us_per_qn: None,
                    time_sig_numerator: None,
                    time_sig_denominator: None,
                }),
                0x90 if p2 == 0 => events.push(RawEvent {
                    tick: abs_tick,
                    order: 1,
                    kind: EventKind::NoteOff,
                    note: Some(p1),
                    velocity: Some(p2),
                    channel: Some(channel),
                    track: track_idx as u32,
                    controller: None,
                    value: None,
                    tempo_us_per_qn: None,
                    time_sig_numerator: None,
                    time_sig_denominator: None,
                }),
                0x90 => events.push(RawEvent {
                    tick: abs_tick,
                    order: 2,
                    kind: EventKind::NoteOn,
                    note: Some(p1),
                    velocity: Some(p2),
                    channel: Some(channel),
                    track: track_idx as u32,
                    controller: None,
                    value: None,
                    tempo_us_per_qn: None,
                    time_sig_numerator: None,
                    time_sig_denominator: None,
                }),
                0x80 => events.push(RawEvent {
                    tick: abs_tick,
                    order: 1,
                    kind: EventKind::NoteOff,
                    note: Some(p1),
                    velocity: Some(p2),
                    channel: Some(channel),
                    track: track_idx as u32,
                    controller: None,
                    value: None,
                    tempo_us_per_qn: None,
                    time_sig_numerator: None,
                    time_sig_denominator: None,
                }),
                _ => {}
            }
        }

        i = trk_end;
    }

    Ok((division, events))
}

fn build_tempo_segments(events: &[RawEvent], ticks_per_qn: u16) -> Vec<TempoSegment> {
    let mut tempos: Vec<(u64, u32)> = vec![(0, DEFAULT_TEMPO_US_PER_QN)];
    for ev in events {
        if ev.kind == EventKind::Tempo {
            if let Some(tempo) = ev.tempo_us_per_qn {
                tempos.push((ev.tick, tempo));
            }
        }
    }
    tempos.sort_by(|a, b| a.0.cmp(&b.0));

    let mut dedup: Vec<(u64, u32)> = Vec::new();
    for (tick, tempo) in tempos {
        if let Some(last) = dedup.last_mut() {
            if last.0 == tick {
                *last = (tick, tempo);
                continue;
            }
        }
        dedup.push((tick, tempo));
    }

    let mut segments = Vec::with_capacity(dedup.len());
    let mut cur_tick = 0u64;
    let mut cur_sec = 0.0f64;
    let mut cur_tempo = dedup[0].1;
    segments.push(TempoSegment {
        tick: cur_tick,
        sec: cur_sec,
        tempo_us_per_qn: cur_tempo,
    });

    for (nxt_tick, nxt_tempo) in dedup.into_iter().skip(1) {
        let dt = nxt_tick.saturating_sub(cur_tick);
        cur_sec += (dt as f64) * ((cur_tempo as f64) / 1_000_000.0) / (ticks_per_qn as f64);
        cur_tick = nxt_tick;
        cur_tempo = nxt_tempo;
        segments.push(TempoSegment {
            tick: cur_tick,
            sec: cur_sec,
            tempo_us_per_qn: cur_tempo,
        });
    }
    segments
}

fn tick_to_sec(tick: u64, tempo_segments: &[TempoSegment], ticks_per_qn: u16) -> f64 {
    let mut lo = 0usize;
    let mut hi = tempo_segments.len().saturating_sub(1);
    while lo < hi {
        let mid = (lo + hi + 1) / 2;
        if tempo_segments[mid].tick <= tick {
            lo = mid;
        } else {
            hi = mid.saturating_sub(1);
        }
    }
    let seg = tempo_segments[lo];
    seg.sec
        + ((tick.saturating_sub(seg.tick)) as f64)
            * ((seg.tempo_us_per_qn as f64) / 1_000_000.0)
            / (ticks_per_qn as f64)
}

fn normalize_stuck_note_fix_mode(value: &str) -> &'static str {
    match value.trim().to_ascii_lowercase().as_str() {
        "off" | "none" | "disable" | "disabled" => "off",
        "lifo" | "stack" | "recent" | "newest" => "lifo",
        "auto" | "on" | "enabled" | "retrigger" | "trim" | "guard" => "auto",
        _ => "off",
    }
}

fn build_note_spans(
    events: &[RawEvent],
    ticks_per_qn: u16,
    stuck_note_fix_mode: &str,
) -> (Vec<NativeSpan>, NoteDiagnostics) {
    let tempo_segments = build_tempo_segments(events, ticks_per_qn);
    let mode = normalize_stuck_note_fix_mode(stuck_note_fix_mode);
    let mut tick_sec_cache: HashMap<u64, f64> = HashMap::new();
    let mut on_map: HashMap<(u32, u8, u8), VecDeque<(u64, u8)>> = HashMap::new();
    let mut spans: Vec<NativeSpan> = Vec::new();
    let mut overlap_fixes = 0u64;
    let mut orphan_note_offs = 0u64;

    for ev in events {
        match ev.kind {
            EventKind::NoteOn => {
                if let (Some(note), Some(channel), Some(velocity)) =
                    (ev.note, ev.channel, ev.velocity)
                {
                    on_map
                        .entry((ev.track, channel, note))
                        .or_default()
                        .push_back((ev.tick, velocity));
                }
            }
            EventKind::NoteOff => {
                if let (Some(note), Some(channel)) = (ev.note, ev.channel) {
                    let Some(queue) = on_map.get_mut(&(ev.track, channel, note)) else {
                        orphan_note_offs += 1;
                        continue;
                    };
                    let start = if (mode == "auto" || mode == "lifo") && queue.len() > 1 {
                        overlap_fixes += 1;
                        queue.pop_back()
                    } else {
                        queue.pop_front()
                    };
                    let Some((start_tick, velocity)) = start else {
                        orphan_note_offs += 1;
                        continue;
                    };
                    if ev.tick <= start_tick {
                        continue;
                    }
                    let start_sec = *tick_sec_cache
                        .entry(start_tick)
                        .or_insert_with(|| tick_to_sec(start_tick, &tempo_segments, ticks_per_qn));
                    let end_sec = *tick_sec_cache
                        .entry(ev.tick)
                        .or_insert_with(|| tick_to_sec(ev.tick, &tempo_segments, ticks_per_qn));
                    spans.push(NativeSpan {
                        note,
                        start: start_sec,
                        end: end_sec,
                        velocity,
                        channel,
                        track: ev.track,
                    });
                }
            }
            _ => {}
        }
    }

    spans.sort_by(|a, b| total_cmp_f64(a.start, b.start));

    let remaining_unmatched_note_ons = on_map.values().map(|q| q.len() as u64).sum();
    let note_diag = NoteDiagnostics {
        stuck_note_fix_mode: match mode {
            "auto" => 1,
            "lifo" => 2,
            _ => 0,
        },
        overlap_fixes,
        orphan_note_offs,
        remaining_unmatched_note_ons,
    };
    (spans, note_diag)
}

fn build_control_events(events: &[RawEvent], ticks_per_qn: u16) -> Vec<NativeControl> {
    let tempo_segments = build_tempo_segments(events, ticks_per_qn);
    let mut controls = Vec::new();
    for ev in events {
        if ev.kind != EventKind::ControlChange {
            continue;
        }
        let (Some(channel), Some(controller), Some(value)) = (ev.channel, ev.controller, ev.value) else {
            continue;
        };
        controls.push(NativeControl {
            time_sec: tick_to_sec(ev.tick, &tempo_segments, ticks_per_qn),
            channel: channel.min(15),
            controller,
            value,
            track: ev.track,
        });
    }
    controls.sort_by(|a, b| total_cmp_f64(a.time_sec, b.time_sec));
    controls
}

fn build_measure_times(events: &[RawEvent], ticks_per_qn: u16) -> Vec<f64> {
    if ticks_per_qn == 0 {
        return Vec::new();
    }
    let tempo_segments = build_tempo_segments(events, ticks_per_qn);
    let max_tick = events.iter().map(|ev| ev.tick).max().unwrap_or(0);

    let mut sig_changes: Vec<(u64, u32, u32)> = vec![(0, 4, 4)];
    for ev in events {
        if ev.kind != EventKind::TimeSignature {
            continue;
        }
        let numer = ev.time_sig_numerator.unwrap_or(4);
        let denom = ev.time_sig_denominator.unwrap_or(4);
        if numer > 0 && denom > 0 {
            sig_changes.push((ev.tick, numer, denom));
        }
    }
    sig_changes.sort_by(|a, b| a.0.cmp(&b.0));

    let mut dedup: Vec<(u64, u32, u32)> = Vec::new();
    for item in sig_changes {
        if let Some(last) = dedup.last_mut() {
            if last.0 == item.0 {
                *last = item;
                continue;
            }
        }
        dedup.push(item);
    }

    let mut measure_ticks: Vec<u64> = Vec::new();
    for (idx, (start_tick, numer, denom)) in dedup.iter().copied().enumerate() {
        let end_tick = dedup
            .get(idx + 1)
            .map(|item| item.0)
            .unwrap_or(max_tick.saturating_add(1));
        if end_tick <= start_tick {
            continue;
        }
        let bar_ticks = (((ticks_per_qn as f64) * 4.0 * (numer as f64) / (denom as f64)).round()
            as u64)
            .max(1);
        let mut tick = start_tick;
        while tick < end_tick {
            measure_ticks.push(tick);
            tick = tick.saturating_add(bar_ticks);
        }
    }

    measure_ticks.sort_unstable();
    measure_ticks.dedup();
    measure_ticks
        .into_iter()
        .map(|tick| tick_to_sec(tick, &tempo_segments, ticks_per_qn))
        .collect()
}

fn apply_midi_flip(spans: &mut [NativeSpan], flip_lo: Option<u8>, flip_hi: Option<u8>) {
    let mut lo = flip_lo.unwrap_or(MIN_PIANO_NOTE);
    let mut hi = flip_hi.unwrap_or(MAX_PIANO_NOTE);
    if lo > hi {
        std::mem::swap(&mut lo, &mut hi);
    }
    for span in spans.iter_mut() {
        let flipped = (lo as i16) + (hi as i16) - (span.note as i16);
        span.note = flipped.clamp(0, 127) as u8;
    }
}

fn ensure_chunk(data: &[u8], i: usize, expected: &[u8; 4], message: &str) -> PyResult<()> {
    let Some(chunk) = data.get(i..i + 4) else {
        return Err(PyValueError::new_err(format!("Invalid MIDI: {message}")));
    };
    if chunk != expected {
        return Err(PyValueError::new_err(format!("Invalid MIDI: {message}")));
    }
    Ok(())
}

fn read_u16_be(data: &[u8], i: usize) -> PyResult<(u16, usize)> {
    let bytes = data
        .get(i..i + 2)
        .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated u16"))?;
    Ok((u16::from_be_bytes([bytes[0], bytes[1]]), i + 2))
}

fn read_u32_be(data: &[u8], i: usize) -> PyResult<(u32, usize)> {
    let bytes = data
        .get(i..i + 4)
        .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated u32"))?;
    Ok((
        u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]),
        i + 4,
    ))
}

fn read_varlen(data: &[u8], mut i: usize) -> PyResult<(u64, usize)> {
    let mut value = 0u64;
    loop {
        let byte = *data
            .get(i)
            .ok_or_else(|| PyValueError::new_err("Invalid MIDI: truncated varlen"))?;
        i += 1;
        value = (value << 7) | u64::from(byte & 0x7F);
        if (byte & 0x80) == 0 {
            return Ok((value, i));
        }
    }
}

fn lower_bound(values: &[f64], needle: f64) -> usize {
    let mut lo = 0usize;
    let mut hi = values.len();
    while lo < hi {
        let mid = (lo + hi) / 2;
        if values[mid] < needle {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

fn upper_bound(values: &[f64], needle: f64) -> usize {
    let mut lo = 0usize;
    let mut hi = values.len();
    while lo < hi {
        let mid = (lo + hi) / 2;
        if values[mid] <= needle {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

fn total_cmp_f64(a: f64, b: f64) -> Ordering {
    a.total_cmp(&b)
}

#[pymodule]
fn synthermia_native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativePackedPlaybackSource>()?;
    m.add_function(wrap_pyfunction!(native_runtime_info, m)?)?;
    m.add_function(wrap_pyfunction!(load_packed_playback_source_native, m)?)?;
    Ok(())
}
