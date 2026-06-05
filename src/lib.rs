use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};
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

#[derive(Clone, Copy)]
struct DensityCell {
    x: u16,
    y: u16,
    track: u32,
    velocity: u8,
    is_black: bool,
    density: u8,
}

#[derive(Clone, Copy, Eq, PartialEq)]
struct MergeCursor {
    tick: u64,
    order: u8,
    track_idx: usize,
    event_idx: usize,
}

impl Ord for MergeCursor {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .tick
            .cmp(&self.tick)
            .then_with(|| other.order.cmp(&self.order))
            .then_with(|| other.track_idx.cmp(&self.track_idx))
            .then_with(|| other.event_idx.cmp(&self.event_idx))
    }
}

impl PartialOrd for MergeCursor {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[derive(Clone, Copy)]
struct ActiveNote {
    start_tick: u64,
    start_sec: f64,
    velocity: u8,
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

    #[pyo3(signature = (t, note_lo, note_hi, play_rows, hit_row, x_scale, move_speed, length_speed, note_direction="down"))]
    fn visible_density_data(
        &self,
        t: f64,
        note_lo: u8,
        note_hi: u8,
        play_rows: usize,
        hit_row: i32,
        x_scale: usize,
        move_speed: f64,
        length_speed: f64,
        note_direction: &str,
    ) -> (
        Vec<(u16, u16, u32, u8, bool, u8)>,
        Vec<(u8, u32, u8)>,
        Vec<(u8, u32)>,
    ) {
        let cells;
        let active;
        let visible;
        (cells, active, visible) = self.build_density_data(
            t,
            note_lo,
            note_hi,
            play_rows,
            hit_row,
            x_scale,
            move_speed,
            length_speed,
            note_direction,
        );
        (
            cells
                .into_iter()
                .map(|cell| (cell.x, cell.y, cell.track, cell.velocity, cell.is_black, cell.density))
                .collect(),
            active,
            visible,
        )
    }

    fn track_ids(&self) -> Vec<u32> {
        let mut tracks = self.track.clone();
        tracks.sort_unstable();
        tracks.dedup();
        tracks
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

impl NativePackedPlaybackSource {
    #[allow(clippy::too_many_arguments)]
    fn build_density_data(
        &self,
        t: f64,
        note_lo: u8,
        note_hi: u8,
        play_rows: usize,
        hit_row: i32,
        x_scale: usize,
        move_speed: f64,
        length_speed: f64,
        note_direction: &str,
    ) -> (Vec<DensityCell>, Vec<(u8, u32, u8)>, Vec<(u8, u32)>) {
        let direction = if note_direction.eq_ignore_ascii_case("up") {
            "up"
        } else {
            "down"
        };
        let window_sec = (play_rows as f64) / move_speed.max(1e-6);
        let (t0, t1) = if direction == "up" {
            (t - window_sec - 0.05, t + 0.05)
        } else {
            (t - 0.05, t + window_sec)
        };
        let visible = self.visible_spans(t0, t1);
        let white_w = white_key_width(x_scale) as i32;
        let black_key_w = black_key_width(x_scale) as i32;
        let black_fall_w = black_fall_width(x_scale) as i32;

        let mut cells: HashMap<(u16, u16), DensityCell> = HashMap::new();
        let mut active: HashSet<(u8, u32, u8)> = HashSet::new();
        let mut visible_notes: HashSet<(u8, u32)> = HashSet::new();

        for (note, start, end, velocity, _channel, track) in visible {
            if note < note_lo || note > note_hi {
                continue;
            }
            if start <= t && t <= end {
                active.insert((note, track, velocity));
            }
            visible_notes.insert((note, track));
            let Some((y1, y2)) =
                span_bounds(start, end, t, hit_row, move_speed, length_speed, direction)
            else {
                continue;
            };
            let x = note_x_native(note, x_scale) as i32;
            if is_white_native(note) {
                for y in y1..=y2 {
                    if y < 0 || y >= play_rows as i32 {
                        continue;
                    }
                    for dx in 0..white_w {
                        let xx = x + dx;
                        if xx < 0 {
                            continue;
                        }
                        let key = (xx as u16, y as u16);
                        let entry = cells.entry(key).or_insert(DensityCell {
                            x: xx as u16,
                            y: y as u16,
                            track,
                            velocity,
                            is_black: false,
                            density: 0,
                        });
                        entry.density = entry.density.saturating_add(1).min(4);
                        if velocity >= entry.velocity {
                            entry.track = track;
                            entry.velocity = velocity;
                        }
                        entry.is_black = false;
                    }
                }
            } else {
                let x0 = x + ((black_key_w - black_fall_w).max(0) / 2);
                for y in y1..=y2 {
                    if y < 0 || y >= play_rows as i32 {
                        continue;
                    }
                    for dx in 0..black_fall_w {
                        let xx = x0 + dx;
                        if xx < 0 {
                            continue;
                        }
                        let key = (xx as u16, y as u16);
                        let entry = cells.entry(key).or_insert(DensityCell {
                            x: xx as u16,
                            y: y as u16,
                            track,
                            velocity,
                            is_black: true,
                            density: 0,
                        });
                        entry.density = entry.density.saturating_add(1).min(4);
                        if velocity >= entry.velocity {
                            entry.track = track;
                            entry.velocity = velocity;
                        }
                        entry.is_black = true;
                    }
                }
            }
        }

        let mut cell_vec: Vec<DensityCell> = cells.into_values().collect();
        cell_vec.sort_by(|a, b| a.y.cmp(&b.y).then_with(|| a.x.cmp(&b.x)));
        let mut active_vec: Vec<(u8, u32, u8)> = active.into_iter().collect();
        active_vec.sort_unstable();
        let mut visible_vec: Vec<(u8, u32)> = visible_notes.into_iter().collect();
        visible_vec.sort_unstable();
        (cell_vec, active_vec, visible_vec)
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
    let (ticks_per_qn, events) = parse_midi_file(midi_path)?;
    let (mut spans, controls, measure_times, note_diag) =
        build_native_rows_streaming(&events, ticks_per_qn, stuck_note_fix_mode);
    if midi_flip {
        apply_midi_flip(&mut spans, flip_lo, flip_hi);
    }
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

    let mut track_events: Vec<Vec<RawEvent>> = Vec::with_capacity(ntrks as usize);
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

        let mut events: Vec<RawEvent> = Vec::new();
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
        events.sort_by(|a, b| a.tick.cmp(&b.tick).then_with(|| a.order.cmp(&b.order)));
        track_events.push(events);
    }

    Ok((division, merge_track_events(track_events)))
}

fn merge_track_events(track_events: Vec<Vec<RawEvent>>) -> Vec<RawEvent> {
    let total_len: usize = track_events.iter().map(Vec::len).sum();
    let mut merged = Vec::with_capacity(total_len);
    let mut heap = BinaryHeap::new();

    for (track_idx, events) in track_events.iter().enumerate() {
        if let Some(event) = events.first() {
            heap.push(MergeCursor {
                tick: event.tick,
                order: event.order,
                track_idx,
                event_idx: 0,
            });
        }
    }

    while let Some(cursor) = heap.pop() {
        let events = &track_events[cursor.track_idx];
        merged.push(events[cursor.event_idx].clone());

        let next_idx = cursor.event_idx + 1;
        if let Some(next) = events.get(next_idx) {
            heap.push(MergeCursor {
                tick: next.tick,
                order: next.order,
                track_idx: cursor.track_idx,
                event_idx: next_idx,
            });
        }
    }

    merged
}

fn build_native_rows_streaming(
    events: &[RawEvent],
    ticks_per_qn: u16,
    stuck_note_fix_mode: &str,
) -> (Vec<NativeSpan>, Vec<NativeControl>, Vec<f64>, NoteDiagnostics) {
    if ticks_per_qn == 0 {
        return (
            Vec::new(),
            Vec::new(),
            Vec::new(),
            NoteDiagnostics {
                stuck_note_fix_mode: 0,
                overlap_fixes: 0,
                orphan_note_offs: 0,
                remaining_unmatched_note_ons: 0,
            },
        );
    }

    let mode = normalize_stuck_note_fix_mode(stuck_note_fix_mode);
    let mut spans: Vec<NativeSpan> = Vec::new();
    let mut controls: Vec<NativeControl> = Vec::new();
    let mut measure_times: Vec<f64> = vec![0.0];
    let mut on_map: HashMap<(u32, u8, u8), VecDeque<ActiveNote>> = HashMap::new();
    let mut overlap_fixes = 0u64;
    let mut orphan_note_offs = 0u64;
    let mut max_tick = 0u64;

    let mut current_tick = 0u64;
    let mut current_sec = 0.0f64;
    let mut tempo_us_per_qn = DEFAULT_TEMPO_US_PER_QN;
    let mut sec_per_tick = (tempo_us_per_qn as f64) / 1_000_000.0 / (ticks_per_qn as f64);

    let mut current_bar_ticks = bar_ticks_for_signature(4, 4, ticks_per_qn);
    let mut next_measure_tick = current_bar_ticks;

    for event in events {
        max_tick = max_tick.max(event.tick);

        while next_measure_tick <= event.tick {
            let measure_sec =
                current_sec + ((next_measure_tick.saturating_sub(current_tick)) as f64) * sec_per_tick;
            if measure_times
                .last()
                .map(|last| (last - measure_sec).abs() > 1e-12)
                .unwrap_or(true)
            {
                measure_times.push(measure_sec);
            }
            next_measure_tick = next_measure_tick.saturating_add(current_bar_ticks.max(1));
        }

        current_sec += ((event.tick.saturating_sub(current_tick)) as f64) * sec_per_tick;
        current_tick = event.tick;

        match event.kind {
            EventKind::Tempo => {
                if let Some(tempo) = event.tempo_us_per_qn {
                    tempo_us_per_qn = tempo.max(1);
                    sec_per_tick =
                        (tempo_us_per_qn as f64) / 1_000_000.0 / (ticks_per_qn as f64);
                }
            }
            EventKind::TimeSignature => {
                let numer = event.time_sig_numerator.unwrap_or(4).max(1);
                let denom = event.time_sig_denominator.unwrap_or(4).max(1);
                if measure_times
                    .last()
                    .map(|last| (last - current_sec).abs() > 1e-12)
                    .unwrap_or(true)
                {
                    measure_times.push(current_sec);
                }
                current_bar_ticks = bar_ticks_for_signature(numer, denom, ticks_per_qn);
                next_measure_tick = current_tick.saturating_add(current_bar_ticks);
            }
            EventKind::ControlChange => {
                if let (Some(channel), Some(controller), Some(value)) =
                    (event.channel, event.controller, event.value)
                {
                    controls.push(NativeControl {
                        time_sec: current_sec,
                        channel: channel.min(15),
                        controller,
                        value,
                        track: event.track,
                    });
                }
            }
            EventKind::NoteOn => {
                if let (Some(note), Some(channel), Some(velocity)) =
                    (event.note, event.channel, event.velocity)
                {
                    on_map
                        .entry((event.track, channel, note))
                        .or_default()
                        .push_back(ActiveNote {
                            start_tick: event.tick,
                            start_sec: current_sec,
                            velocity,
                        });
                }
            }
            EventKind::NoteOff => {
                if let (Some(note), Some(channel)) = (event.note, event.channel) {
                    let Some(queue) = on_map.get_mut(&(event.track, channel, note)) else {
                        orphan_note_offs += 1;
                        continue;
                    };
                    let start = if (mode == "auto" || mode == "lifo") && queue.len() > 1 {
                        overlap_fixes += 1;
                        queue.pop_back()
                    } else {
                        queue.pop_front()
                    };
                    let Some(start_note) = start else {
                        orphan_note_offs += 1;
                        continue;
                    };
                    if event.tick <= start_note.start_tick {
                        continue;
                    }
                    spans.push(NativeSpan {
                        note,
                        start: start_note.start_sec,
                        end: current_sec,
                        velocity: start_note.velocity,
                        channel,
                        track: event.track,
                    });
                }
            }
        }

    }

    while next_measure_tick <= max_tick {
        let measure_sec =
            current_sec + ((next_measure_tick.saturating_sub(current_tick)) as f64) * sec_per_tick;
        if measure_times
            .last()
            .map(|last| (last - measure_sec).abs() > 1e-12)
            .unwrap_or(true)
        {
            measure_times.push(measure_sec);
        }
        next_measure_tick = next_measure_tick.saturating_add(current_bar_ticks.max(1));
    }

    let remaining_unmatched_note_ons = on_map.values().map(|q| q.len() as u64).sum();
    spans.shrink_to_fit();
    controls.shrink_to_fit();
    measure_times.shrink_to_fit();

    (
        spans,
        controls,
        measure_times,
        NoteDiagnostics {
            stuck_note_fix_mode: match mode {
                "auto" => 1,
                "lifo" => 2,
                _ => 0,
            },
            overlap_fixes,
            orphan_note_offs,
            remaining_unmatched_note_ons,
        },
    )
}

fn bar_ticks_for_signature(numer: u32, denom: u32, ticks_per_qn: u16) -> u64 {
    (((ticks_per_qn as f64) * 4.0 * (numer as f64) / (denom as f64)).round() as u64).max(1)
}

fn normalize_stuck_note_fix_mode(value: &str) -> &'static str {
    match value.trim().to_ascii_lowercase().as_str() {
        "off" | "none" | "disable" | "disabled" => "off",
        "lifo" | "stack" | "recent" | "newest" => "lifo",
        "auto" | "on" | "enabled" | "retrigger" | "trim" | "guard" => "auto",
        _ => "off",
    }
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

fn is_white_native(note: u8) -> bool {
    matches!(note % 12, 0 | 2 | 4 | 5 | 7 | 9 | 11)
}

fn count_whites_before_native(note: u8) -> usize {
    let mut count = 0usize;
    let mut n = MIN_PIANO_NOTE;
    while n < note {
        if is_white_native(n) {
            count += 1;
        }
        n = n.saturating_add(1);
    }
    count
}

fn white_key_width(x_scale: usize) -> usize {
    (2 * x_scale).max(2)
}

fn black_key_width(x_scale: usize) -> usize {
    (2 * x_scale).max(2)
}

fn black_fall_width(x_scale: usize) -> usize {
    black_key_width(x_scale)
}

fn note_x_native(note: u8, x_scale: usize) -> usize {
    let w = white_key_width(x_scale);
    if is_white_native(note) {
        return count_whites_before_native(note) * w;
    }
    let mut left = note.saturating_sub(1);
    while left >= MIN_PIANO_NOTE && !is_white_native(left) {
        if left == 0 {
            break;
        }
        left = left.saturating_sub(1);
    }
    let base = if left < MIN_PIANO_NOTE { note } else { left };
    (count_whites_before_native(base) * w) + x_scale.max(1)
}

fn span_bounds(
    start_val: f64,
    end_val: f64,
    t: f64,
    hit_row: i32,
    move_speed: f64,
    length_speed: f64,
    direction: &str,
) -> Option<(i32, i32)> {
    let (y1, y2) = if direction == "up" {
        let grow_dur = (t.min(end_val) - start_val).max(0.0);
        let pixel_len = (grow_dur * length_speed).round().max(1.0) as i32;
        let release_shift = ((t - end_val).max(0.0) * move_speed) as f64;
        let y_bot = (f64::from(hit_row) - release_shift).floor() as i32;
        (y_bot - pixel_len + 1, y_bot)
    } else {
        let pixel_len = ((end_val - start_val) * length_speed).round().max(1.0) as i32;
        let y_bot = (f64::from(hit_row) + (t - start_val) * move_speed).floor() as i32;
        (y_bot - pixel_len + 1, y_bot)
    };
    if y2 < 0 || y1 > hit_row {
        return None;
    }
    Some((y1.max(0), y2.min(hit_row)))
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
