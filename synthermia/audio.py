from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, IO, List, Optional, Sequence, Tuple

from synthermia.models import NoteSpan, ControlEvent, _AudioEvent
from synthermia.soundfont import (
    _clamp_instrument_pair,
    _normalize_saved_soundfont_library,
    parse_instrument_spec,
)
from synthermia.config import (
    _normalize_saved_track_map,
    _normalize_saved_track_volume_map,
    _clamp_track_volume,
)


_PW_INIT_DONE = False


# ---------------------------------------------------------------------------
# Audio output target enumeration
# ---------------------------------------------------------------------------

def list_audio_output_targets() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen: set[str] = set()

    pactl = shutil.which("pactl")
    if pactl:
        try:
            text = subprocess.check_output([pactl, "list", "short", "sinks"], text=True, stderr=subprocess.DEVNULL)
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = re.split(r"\s+", line)
                if len(parts) < 2:
                    continue
                name = parts[1].strip()
                if (not name) or (name in seen):
                    continue
                seen.add(name)
                out.append((name, name))
        except Exception:
            pass

    pw_dump = shutil.which("pw-dump")
    if pw_dump:
        try:
            raw = subprocess.check_output([pw_dump], text=True, stderr=subprocess.DEVNULL)
            objs = json.loads(raw)
            for obj in objs:
                if obj.get("type") != "PipeWire:Interface:Node":
                    continue
                info = obj.get("info") or {}
                props = info.get("props") or {}
                if not isinstance(props, dict):
                    continue
                media_class = str(props.get("media.class", "")).strip().lower()
                if "audio/sink" not in media_class:
                    continue
                name = str(props.get("node.name", "")).strip()
                desc = str(props.get("node.description", "")).strip()
                if not name:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                label = f"{name}  ({desc})" if desc else name
                out.append((name, label))
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# PipeWire init
# ---------------------------------------------------------------------------

def _ensure_pipewire_env() -> None:
    if not os.environ.get("SPA_PLUGIN_DIR"):
        spa_candidates = [
            "/usr/lib/x86_64-linux-gnu/spa-0.2",
            "/usr/lib64/spa-0.2",
            "/usr/lib/spa-0.2",
        ]
        for cand in spa_candidates:
            if os.path.isdir(cand):
                os.environ["SPA_PLUGIN_DIR"] = cand
                break
    if not os.environ.get("PIPEWIRE_MODULE_DIR"):
        mod_candidates = [
            "/usr/lib/x86_64-linux-gnu/pipewire-0.3",
            "/usr/lib64/pipewire-0.3",
            "/usr/lib/pipewire-0.3",
        ]
        for cand in mod_candidates:
            if os.path.isdir(cand):
                os.environ["PIPEWIRE_MODULE_DIR"] = cand
                break


def _pipewire_init_once() -> None:
    global _PW_INIT_DONE
    if _PW_INIT_DONE:
        return
    _ensure_pipewire_env()
    lib = None
    last_err: Optional[Exception] = None
    for name in ("libpipewire-0.3.so.0", "libpipewire-0.3.so"):
        try:
            lib = ctypes.CDLL(name)
            break
        except Exception as exc:
            last_err = exc
            continue
    if lib is None:
        if last_err is not None:
            raise RuntimeError(f"Failed to load PipeWire shared library: {last_err}")
        raise RuntimeError("Failed to load PipeWire shared library")
    try:
        pw_init = lib.pw_init
        pw_init.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p)]
        pw_init.restype = None
        pw_init(None, None)
    except Exception as exc:
        raise RuntimeError(f"Failed to call pw_init(): {exc}") from exc
    _PW_INIT_DONE = True


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------

def _build_track_program_map(
    spans: Sequence[NoteSpan],
    default_pair: Tuple[int, int],
    default_soundfont: str,
    track_instruments_raw: object,
    valid_pairs_by_sf: Optional[Dict[str, set[Tuple[int, int]]]] = None,
) -> Dict[int, Tuple[str, int, int]]:
    tracks = sorted({int(getattr(sp, "track", 0)) for sp in spans})
    return _build_track_program_map_for_tracks(
        tracks,
        default_pair,
        default_soundfont,
        track_instruments_raw,
        valid_pairs_by_sf=valid_pairs_by_sf,
    )


def _build_track_program_map_for_tracks(
    tracks: Sequence[int],
    default_pair: Tuple[int, int],
    default_soundfont: str,
    track_instruments_raw: object,
    valid_pairs_by_sf: Optional[Dict[str, set[Tuple[int, int]]]] = None,
) -> Dict[int, Tuple[str, int, int]]:
    default_bank, default_prog = _clamp_instrument_pair(default_pair[0], default_pair[1])
    default_sf = str(default_soundfont or "").strip()
    default_sf_key = os.path.abspath(default_sf).lower() if default_sf else ""

    def _resolve_sf_key(sf_txt: str) -> str:
        sf_clean = str(sf_txt or "").strip()
        return os.path.abspath(sf_clean).lower() if sf_clean else ""

    def _pick_pair_for_sf(sf_key: str, pair: Tuple[int, int]) -> Tuple[int, int]:
        b, p = _clamp_instrument_pair(pair[0], pair[1])
        if not valid_pairs_by_sf:
            return b, p
        valid_pairs = valid_pairs_by_sf.get(sf_key)
        if valid_pairs is None:
            return b, p
        if (b, p) in valid_pairs:
            return b, p
        if (default_bank, default_prog) in valid_pairs:
            return default_bank, default_prog
        if (0, 0) in valid_pairs:
            return 0, 0
        if valid_pairs:
            return sorted(valid_pairs)[0]
        return default_bank, default_prog

    default_bank, default_prog = _pick_pair_for_sf(default_sf_key, (default_bank, default_prog))
    overrides = _normalize_saved_track_map(track_instruments_raw)
    out: Dict[int, Tuple[str, int, int]] = {}
    for tr in sorted({int(track) for track in tracks}):
        override_txt = overrides.get(str(tr))
        if override_txt:
            ov_sf, b, p = parse_instrument_spec(override_txt, (default_bank, default_prog), default_sf)
            sf = str(ov_sf or default_sf).strip()
            sf_key = _resolve_sf_key(sf)
            if valid_pairs_by_sf and sf_key not in valid_pairs_by_sf and default_sf:
                sf = default_sf
                sf_key = default_sf_key
            b, p = _pick_pair_for_sf(sf_key, (b, p))
            out[tr] = (sf, b, p)
        else:
            b, p = _pick_pair_for_sf(default_sf_key, (default_bank, default_prog))
            out[tr] = (default_sf, b, p)
    return out


def _apply_track_volume_overrides(
    spans: Sequence[NoteSpan],
    track_volumes_raw: object,
) -> List[NoteSpan]:
    overrides = _normalize_saved_track_volume_map(track_volumes_raw)
    if not overrides:
        return list(spans)
    out: List[NoteSpan] = []
    for sp in spans:
        tr = int(getattr(sp, "track", 0))
        vol = _clamp_track_volume(float(overrides.get(str(tr), 1.0)))
        if vol <= 0.0:
            continue
        vel = int(round(int(sp.velocity) * vol))
        if vel <= 0:
            continue
        vel = max(1, min(127, vel))
        out.append(
            NoteSpan(
                note=sp.note,
                start=sp.start,
                end=sp.end,
                velocity=vel,
                channel=sp.channel,
                track=sp.track,
            )
        )
    return out


def _assign_audio_channels(
    spans: Sequence[NoteSpan],
    track_programs: Dict[int, Tuple[str, int, int]],
) -> Tuple[
    List[NoteSpan],
    Dict[int, Tuple[int, int]],
    Dict[int, str],
    Dict[Tuple[int, int], int],
    Dict[int, int],
]:
    channel_programs: Dict[int, Tuple[int, int]] = {}
    channel_soundfonts: Dict[int, str] = {}
    source_channels: Dict[Tuple[int, int], int] = {}
    track_default_channels: Dict[int, int] = {}
    next_channel = 0
    for sp in spans:
        tr = int(getattr(sp, "track", 0))
        src_ch = max(0, min(15, int(getattr(sp, "channel", 0))))
        key = (tr, src_ch)
        if key in source_channels:
            continue
        sf_path, bank, prog = track_programs.get(tr, ("", 0, 0))
        if next_channel > 15:
            raise ValueError("Too many active MIDI channels in tracks (max 16 channels)")
        ch = next_channel
        next_channel += 1
        channel_programs[ch] = _clamp_instrument_pair(bank, prog)
        channel_soundfonts[ch] = str(sf_path or "").strip()
        source_channels[key] = ch
        track_default_channels.setdefault(tr, ch)
    out: List[NoteSpan] = []
    for sp in spans:
        tr = int(getattr(sp, "track", 0))
        src_ch = max(0, min(15, int(getattr(sp, "channel", 0))))
        ch = source_channels.get((tr, src_ch), track_default_channels.get(tr, 0))
        out.append(
            NoteSpan(
                note=sp.note,
                start=sp.start,
                end=sp.end,
                velocity=sp.velocity,
                channel=ch,
                track=tr,
            )
        )
    return out, channel_programs, channel_soundfonts, source_channels, track_default_channels


def _assign_control_channels(
    controls: Sequence[ControlEvent],
    source_channels: Dict[Tuple[int, int], int],
    track_default_channels: Dict[int, int],
) -> List[ControlEvent]:
    out: List[ControlEvent] = []
    for ev in controls:
        tr = int(getattr(ev, "track", 0))
        src_ch = max(0, min(15, int(ev.channel)))
        ch = source_channels.get((tr, src_ch), track_default_channels.get(tr, src_ch))
        out.append(
            ControlEvent(
                time_sec=max(0.0, float(ev.time_sec)),
                channel=ch,
                controller=max(0, min(127, int(ev.controller))),
                value=max(0, min(127, int(ev.value))),
                track=tr,
            )
        )
    out.sort(key=lambda e: e.time_sec)
    return out


# ---------------------------------------------------------------------------
# In-process FluidSynth
# ---------------------------------------------------------------------------

def _load_pyfluidsynth_module() -> Any:
    try:
        import fluidsynth  # type: ignore
    except Exception as exc:
        raise ValueError(
            "In-process audio requires python module 'fluidsynth' (pyFluidSynth). "
            "Install with: pip install pyfluidsynth (or apt install python3-fluidsynth)."
        ) from exc
    return fluidsynth


def _build_audio_events(
    spans: Sequence[NoteSpan],
    controls: Sequence[ControlEvent],
) -> Tuple[List[_AudioEvent], List[Tuple[float, int]]]:
    events: List[_AudioEvent] = []
    for ev in controls:
        t = max(0.0, float(ev.time_sec))
        channel = max(0, min(15, int(ev.channel)))
        controller = max(0, min(127, int(ev.controller)))
        value = max(0, min(127, int(ev.value)))
        events.append(
            _AudioEvent(
                time_sec=t,
                order=0,
                kind="cc",
                channel=channel,
                controller=controller,
                value=value,
            )
        )
    for sp in spans:
        start = max(0.0, float(sp.start))
        end = max(start + 1e-4, float(sp.end))
        note = max(0, min(127, int(sp.note)))
        vel = max(1, min(127, int(sp.velocity)))
        channel = max(0, min(15, int(getattr(sp, "channel", 0))))
        events.append(
            _AudioEvent(
                time_sec=start,
                order=2,
                kind="note_on",
                channel=channel,
                note=note,
                velocity=vel,
            )
        )
        events.append(
            _AudioEvent(
                time_sec=end,
                order=1,
                kind="note_off",
                channel=channel,
                note=note,
                velocity=0,
            )
        )
    events.sort(key=lambda e: (e.time_sec, e.order, e.channel, e.note, e.controller))
    keys = [(e.time_sec, e.order) for e in events]
    return events, keys


class RealtimeFluidSynthPlayer:
    def __init__(
        self,
        spans: Sequence[NoteSpan],
        soundfont_path: str,
        audio_gain: float,
        soundfont_paths: Optional[Sequence[str]] = None,
        driver: str = "",
        output_target: str = "",
        samplerate: int = 0,
        period_size: int = 0,
        periods: int = 0,
        polyphony: int = 0,
        channel_programs: Optional[Dict[int, Tuple[int, int]]] = None,
        channel_soundfonts: Optional[Dict[int, str]] = None,
        control_events: Optional[Sequence[ControlEvent]] = None,
    ) -> None:
        default_soundfont = str(soundfont_path or "").strip()
        if not default_soundfont:
            raise ValueError("SoundFont file not configured.")
        sf_library = _normalize_saved_soundfont_library(soundfont_paths or [], primary_soundfont=default_soundfont)
        if not sf_library:
            raise ValueError("No valid SoundFonts configured.")
        for sf in sf_library:
            if not os.path.exists(sf):
                raise ValueError(f"SoundFont file not found: {sf}")
        self._default_soundfont = default_soundfont
        self._soundfont_ids_by_key: Dict[str, int] = {}
        self._channel_sfids: Dict[int, int] = {}

        self._fs = _load_pyfluidsynth_module()
        self._events, self._event_keys = _build_audio_events(spans, list(control_events or []))
        self._event_idx = 0
        self._active_counts: Dict[Tuple[int, int], int] = defaultdict(int)
        self._controller_state: Dict[Tuple[int, int], int] = {}
        self._driver_used = ""
        self._gain = max(0.0, float(audio_gain))

        synth_kwargs: Dict[str, Any] = {}
        if samplerate > 0:
            synth_kwargs["samplerate"] = float(samplerate)
        synth_kwargs["gain"] = float(self._gain)

        effective_period_size = int(period_size) if int(period_size) > 0 else 512
        effective_periods = int(periods) if int(periods) > 0 else 3
        effective_samplerate = int(samplerate) if int(samplerate) > 0 else 0
        effective_polyphony = int(polyphony) if int(polyphony) > 0 else 0
        output_target_txt = _resolve_audio_output_target(output_target)

        requested = str(driver).strip()
        candidates: List[str] = []
        for cand in [requested, "", "pipewire", "pulseaudio", "alsa"]:
            if cand not in candidates:
                candidates.append(cand)

        last_err: Optional[Exception] = None
        self._synth = None  # type: ignore[assignment]
        sfid = -1
        for cand in candidates:
            try:
                try:
                    synth = self._fs.Synth(**synth_kwargs)
                except TypeError:
                    synth = self._fs.Synth()
                if hasattr(synth, "setting"):
                    try:
                        synth.setting("audio.period-size", int(effective_period_size))
                    except Exception:
                        pass
                    try:
                        synth.setting("audio.periods", int(effective_periods))
                    except Exception:
                        pass
                    if effective_samplerate > 0:
                        try:
                            synth.setting("synth.sample-rate", float(effective_samplerate))
                        except Exception:
                            pass
                    if effective_polyphony > 0:
                        try:
                            synth.setting("synth.polyphony", int(effective_polyphony))
                        except Exception:
                            pass
                    if output_target_txt:
                        setting_keys: List[str] = []
                        cand_txt = str(cand or "").strip()
                        if cand_txt:
                            setting_keys.append(f"audio.{cand_txt}.device")
                        setting_keys.extend(["audio.pulseaudio.device", "audio.pipewire.device", "audio.alsa.device"])
                        seen_keys: set[str] = set()
                        for skey in setting_keys:
                            if skey in seen_keys:
                                continue
                            seen_keys.add(skey)
                            try:
                                synth.setting(skey, output_target_txt)
                            except Exception:
                                pass
                if cand == "pipewire":
                    _pipewire_init_once()
                start_ret = synth.start(driver=cand) if cand else synth.start()
                if isinstance(start_ret, (int, float)) and start_ret < 0:
                    raise RuntimeError(f"audio start failed for driver '{cand or 'default'}'")
                for sf in sf_library:
                    loaded_sfid = synth.sfload(sf)
                    if loaded_sfid < 0:
                        raise RuntimeError(f"sfload failed for: {sf}")
                    sf_key = os.path.abspath(sf).lower()
                    self._soundfont_ids_by_key[sf_key] = int(loaded_sfid)
                sfid = self._soundfont_ids_by_key.get(os.path.abspath(default_soundfont).lower(), -1)
                if sfid < 0:
                    raise RuntimeError(f"default soundfont not loaded: {default_soundfont}")
                self._synth = synth
                self._driver_used = cand
                break
            except Exception as exc:
                last_err = exc
                try:
                    if "synth" in locals() and hasattr(synth, "delete"):
                        synth.delete()
                except Exception:
                    pass
                continue

        if self._synth is None or sfid < 0:
            if last_err is None:
                raise ValueError("Failed to initialize python fluidsynth audio")
            raise ValueError(f"Failed to initialize python fluidsynth audio: {last_err}") from last_err

        self._sfid = sfid
        programs = dict(channel_programs or {0: (0, 0)})
        if not programs:
            programs = {0: (0, 0)}
        ch_sf = dict(channel_soundfonts or {})
        for ch, pair in sorted(programs.items()):
            channel = max(0, min(15, int(ch)))
            bank, program = _clamp_instrument_pair(pair[0], pair[1])
            sf_txt = str(ch_sf.get(channel, default_soundfont) or default_soundfont).strip()
            sf_key = os.path.abspath(sf_txt).lower() if sf_txt else os.path.abspath(default_soundfont).lower()
            sfid_for_ch = self._soundfont_ids_by_key.get(sf_key, self._sfid)
            self._channel_sfids[channel] = sfid_for_ch
            try:
                ret = self._synth.program_select(channel, sfid_for_ch, bank, program)
                if isinstance(ret, (int, float)) and ret < 0:
                    raise RuntimeError("program_select failed")
            except Exception:
                self._synth.program_select(channel, sfid_for_ch, 0, 0)
        self.set_gain(self._gain)

    @property
    def driver_used(self) -> str:
        return self._driver_used

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, float(gain))
        applied = False
        if hasattr(self._synth, "setting"):
            try:
                self._synth.setting("synth.gain", float(self._gain))
                applied = True
            except Exception:
                applied = False
        if (not applied) and hasattr(self._synth, "set_gain"):
            try:
                self._synth.set_gain(float(self._gain))
            except Exception:
                pass

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        ch = max(0, min(15, int(channel)))
        nn = max(0, min(127, int(note)))
        vv = max(1, min(127, int(velocity)))
        self._note_on(ch, nn, vv)

    def note_off(self, channel: int, note: int) -> None:
        ch = max(0, min(15, int(channel)))
        nn = max(0, min(127, int(note)))
        self._note_off(ch, nn)

    def cc(self, channel: int, controller: int, value: int) -> None:
        self._cc(channel, controller, value)

    def set_channel_program(self, channel: int, bank: int, program: int) -> None:
        ch = max(0, min(15, int(channel)))
        b, p = _clamp_instrument_pair(int(bank), int(program))
        sfid = self._channel_sfids.get(ch, self._sfid)
        try:
            ret = self._synth.program_select(ch, sfid, b, p)
            if isinstance(ret, (int, float)) and ret < 0:
                raise RuntimeError("program_select failed")
        except Exception:
            self._synth.program_select(ch, sfid, 0, 0)

    def _note_on(self, channel: int, note: int, velocity: int) -> None:
        self._synth.noteon(channel, note, velocity)
        self._active_counts[(channel, note)] += 1

    def _note_off(self, channel: int, note: int) -> None:
        key = (channel, note)
        cur = self._active_counts.get(key, 0)
        if cur <= 0:
            return
        self._synth.noteoff(channel, note)
        if cur == 1:
            self._active_counts.pop(key, None)
        else:
            self._active_counts[key] = cur - 1

    def _cc(self, channel: int, controller: int, value: int) -> None:
        ch = max(0, min(15, int(channel)))
        cc = max(0, min(127, int(controller)))
        vv = max(0, min(127, int(value)))
        if hasattr(self._synth, "cc"):
            try:
                self._synth.cc(ch, cc, vv)
            except Exception:
                pass
        self._controller_state[(ch, cc)] = vv

    def all_notes_off(self) -> None:
        for (channel, note), count in list(self._active_counts.items()):
            for _ in range(count):
                self._synth.noteoff(channel, note)
        self._active_counts.clear()
        if hasattr(self._synth, "cc"):
            for ch in range(16):
                try:
                    self._synth.cc(ch, 123, 0)  # all notes off
                except Exception:
                    pass
                try:
                    self._synth.cc(ch, 64, 0)  # sustain off
                except Exception:
                    pass
        self._controller_state.clear()

    def _simulate_state_until(
        self,
        event_idx: int,
    ) -> Tuple[Dict[Tuple[int, int], int], Dict[Tuple[int, int], List[int]]]:
        controller_state: Dict[Tuple[int, int], int] = {}
        sustain_down: Dict[int, bool] = defaultdict(bool)
        pressed: Dict[Tuple[int, int], deque[int]] = defaultdict(deque)
        sounding: Dict[Tuple[int, int], deque[int]] = defaultdict(deque)

        def release_sustain(channel: int) -> None:
            for key, sounding_vels in list(sounding.items()):
                if key[0] != channel:
                    continue
                pressed_len = len(pressed.get(key, ()))
                while len(sounding_vels) > pressed_len:
                    sounding_vels.popleft()
                if not sounding_vels:
                    sounding.pop(key, None)

        for i in range(max(0, min(event_idx, len(self._events)))):
            ev = self._events[i]
            if ev.kind == "cc":
                controller_state[(ev.channel, ev.controller)] = ev.value
                if ev.controller == 64:
                    was_down = bool(sustain_down.get(ev.channel, False))
                    now_down = ev.value >= 64
                    sustain_down[ev.channel] = now_down
                    if was_down and (not now_down):
                        release_sustain(ev.channel)
                continue
            key = (ev.channel, ev.note)
            if ev.kind == "note_on":
                vel = max(1, min(127, int(ev.velocity)))
                pressed[key].append(vel)
                sounding[key].append(vel)
                continue
            if ev.kind == "note_off":
                if pressed[key]:
                    pressed[key].popleft()
                if not sustain_down.get(ev.channel, False):
                    if sounding[key]:
                        sounding[key].popleft()
                    if not sounding[key]:
                        sounding.pop(key, None)

        sounding_out: Dict[Tuple[int, int], List[int]] = {}
        for key, vals in sounding.items():
            if vals:
                sounding_out[key] = list(vals)
        return controller_state, sounding_out

    def seek(self, at_time: float) -> None:
        t = max(0.0, float(at_time))
        self.all_notes_off()
        self._event_idx = bisect_left(self._event_keys, (t, 3))
        controller_state, sounding = self._simulate_state_until(self._event_idx)
        for (channel, controller), value in sorted(controller_state.items()):
            self._cc(channel, controller, value)
        for (channel, note), vels in sorted(sounding.items()):
            for vel in vels:
                self._note_on(channel, note, vel)

    def update(self, at_time: float) -> None:
        t = max(0.0, float(at_time))
        n = len(self._events)
        while self._event_idx < n and self._events[self._event_idx].time_sec <= t + 1e-9:
            ev = self._events[self._event_idx]
            if ev.kind == "cc":
                self._cc(ev.channel, ev.controller, ev.value)
            elif ev.kind == "note_off":
                self._note_off(ev.channel, ev.note)
            else:
                self._note_on(ev.channel, ev.note, ev.velocity)
            self._event_idx += 1

    def close(self) -> None:
        self.all_notes_off()
        if hasattr(self._synth, "delete"):
            try:
                self._synth.delete()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Audio driver / latency helpers
# ---------------------------------------------------------------------------

def _drain_stream(stream: Optional[IO[str]], log_fh: Optional[IO[str]], lock: Optional[threading.Lock]) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            if log_fh:
                if lock:
                    with lock:
                        log_fh.write(line)
                        log_fh.flush()
                else:
                    log_fh.write(line)
                    log_fh.flush()
    except Exception:
        return


def _write_audio_log_line(log_fh: Optional[IO[str]], log_lock: Optional[threading.Lock], msg: str) -> None:
    if not log_fh:
        return
    line = msg if msg.endswith("\n") else f"{msg}\n"
    if log_lock:
        with log_lock:
            log_fh.write(line)
            log_fh.flush()
        return
    log_fh.write(line)
    log_fh.flush()


def _parse_pulse_latency_value(block: str, label: str) -> float:
    m = re.search(rf"{label}:\s*([0-9]+(?:\.[0-9]+)?)\s*(usec|msec|ms|sec|s)", block, re.IGNORECASE)
    if not m:
        return 0.0
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "usec":
        return value / 1_000_000.0
    if unit in {"msec", "ms"}:
        return value / 1_000.0
    return value


def _parse_pulse_sink_input_state(block: str) -> str:
    m = re.search(r"State:\s*([A-Z_]+)", block)
    if not m:
        return ""
    return m.group(1).strip().upper()


def _parse_pulse_sink_input_latency_seconds(block: str) -> float:
    sink_latency = _parse_pulse_latency_value(block, "Sink Latency")
    buffer_latency = _parse_pulse_latency_value(block, "Buffer Latency")
    return max(sink_latency, buffer_latency)


def _parse_pipewire_fraction_seconds(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    m = re.match(r"^([0-9]+)\s*/\s*([0-9]+)$", text)
    if m:
        num = int(m.group(1))
        den = int(m.group(2))
        if den > 0:
            return num / float(den)
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _probe_pipewire_stream_timing(pid: int, timeout: float = 1.5) -> Tuple[Optional[float], float]:
    pw_dump = shutil.which("pw-dump")
    if not pw_dump:
        return None, 0.0
    deadline = time.perf_counter() + max(0.0, timeout)
    best_latency = 0.0
    saw_candidate = False
    while time.perf_counter() < deadline:
        try:
            out = subprocess.check_output([pw_dump], text=True, stderr=subprocess.DEVNULL)
            objects = json.loads(out)
        except Exception:
            return None, 0.0
        for obj in objects:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            info = obj.get("info") or {}
            props = info.get("props") or {}
            if not isinstance(props, dict):
                continue
            pid_match = str(props.get("application.process.id", "")).strip() == str(pid)
            if not pid_match:
                continue
            media_class = str(props.get("media.class", "")).lower()
            if media_class and ("audio" not in media_class or "output" not in media_class):
                continue
            saw_candidate = True
            node_latency = _parse_pipewire_fraction_seconds(props.get("node.latency"))
            if node_latency > best_latency:
                best_latency = node_latency
            state = str(info.get("state", "")).strip().lower()
            if state == "running":
                return time.perf_counter(), best_latency
        time.sleep(0.05)
    if saw_candidate:
        return time.perf_counter(), best_latency
    return None, 0.0


def _probe_pulse_sink_latency_seconds(pid: int, timeout: float = 0.35) -> float:
    pactl = shutil.which("pactl")
    if not pactl:
        return 0.0
    deadline = time.perf_counter() + max(0.0, timeout)
    running_latencies: List[float] = []
    seen_latencies: List[float] = []
    while time.perf_counter() < deadline:
        try:
            out = subprocess.check_output([pactl, "list", "sink-inputs"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return 0.0
        for block in out.split("Sink Input #")[1:]:
            pid_match = f'application.process.id = "{pid}"' in block
            name_match = 'application.name = "fluidsynth"' in block
            binary_match = 'application.process.binary = "fluidsynth"' in block
            if not (pid_match or name_match or binary_match):
                continue
            state = _parse_pulse_sink_input_state(block)
            latency = _parse_pulse_sink_input_latency_seconds(block)
            if latency > 0:
                seen_latencies.append(latency)
            if state == "RUNNING":
                if latency > 0:
                    running_latencies.append(latency)
        time.sleep(0.05)
    candidates = running_latencies if running_latencies else seen_latencies
    if not candidates:
        return 0.0
    sorted_vals = sorted(candidates)
    mid = len(sorted_vals) // 2
    return sorted_vals[mid] if len(sorted_vals) % 2 == 1 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def _estimate_fluidsynth_output_latency_seconds(
    args: argparse.Namespace,
    pid: int,
    *,
    skip_pipewire_probe: bool = False,
) -> float:
    driver = str(getattr(args, "_resolved_audio_driver", "") or "")
    if (driver == "pipewire") and (not skip_pipewire_probe):
        _ready_perf, pw_latency = _probe_pipewire_stream_timing(pid, timeout=0.5)
        if pw_latency > 0:
            return pw_latency
    pulse_latency = _probe_pulse_sink_latency_seconds(pid, timeout=0.35)
    if pulse_latency > 0:
        return pulse_latency
    period_size = int(getattr(args, "audio_period_size", 0) or 0)
    periods = int(getattr(args, "audio_periods", 0) or 0)
    sample_rate = int(getattr(args, "audio_samplerate", 0) or 0)
    if period_size > 0 and periods > 0:
        sr = sample_rate if sample_rate > 0 else 48_000
        return (period_size * periods) / float(sr)
    return 0.0


def _pulseaudio_server_available() -> bool:
    pactl = shutil.which("pactl")
    if not pactl:
        return False
    try:
        p = subprocess.run(
            [pactl, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return False
    return p.returncode == 0


def _resolve_audio_driver(requested_driver: str) -> str:
    if requested_driver:
        return requested_driver
    if not sys.platform.startswith("linux"):
        return ""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    has_pipewire = bool(runtime_dir and os.path.exists(os.path.join(runtime_dir, "pipewire-0")))
    has_pulse_server = _pulseaudio_server_available()
    if has_pipewire:
        return "pipewire"
    if has_pulse_server:
        return "pulseaudio"
    return ""


def _resolve_audio_output_target(value: object) -> str:
    return str(value or "").strip()


def _append_cli_audio_output_option(cmd: List[str], driver: str, output_target: str) -> None:
    target = _resolve_audio_output_target(output_target)
    if not target:
        return
    drv = str(driver or "").strip().lower()
    if drv == "alsa":
        cmd.extend(["-o", f"audio.alsa.device={target}"])
        return
    if drv == "pulseaudio":
        cmd.extend(["-o", f"audio.pulseaudio.device={target}"])
        return
    if drv == "pipewire":
        cmd.extend(["-o", f"audio.pipewire.device={target}"])
        return


def _fluidsynth_env(driver: str, output_target: str = "") -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULSE_PROP_media.role", "music")
    target = _resolve_audio_output_target(output_target)
    if target:
        env.setdefault("PULSE_SINK", target)
    if driver == "pipewire" and "PIPEWIRE_PROPS" not in env:
        props: Dict[str, object] = {"media.role": "Music", "session.suspend-timeout-seconds": 0}
        if target:
            props["target.object"] = target
        env["PIPEWIRE_PROPS"] = json.dumps(props, separators=(",", ":"))
    return env


# ---------------------------------------------------------------------------
# CLI launcher
# ---------------------------------------------------------------------------

def launch_soundfont_playback(args: argparse.Namespace) -> Tuple[Optional[subprocess.Popen], float, float]:
    if not args.soundfont:
        return None, time.perf_counter(), 0.0

    midi_path = str(getattr(args, "audio_midi", args.midi))
    if not midi_path:
        return None, time.perf_counter(), 0.0

    if not os.path.exists(args.soundfont):
        raise ValueError(f"SoundFont file not found: {args.soundfont}")

    exe = shutil.which(args.fluidsynth_bin)
    if not exe:
        raise ValueError(
            f"fluidsynth executable not found: {args.fluidsynth_bin}. "
            "Install FluidSynth and/or pass --fluidsynth-bin."
        )

    driver = _resolve_audio_driver(args.audio_driver)
    setattr(args, "_resolved_audio_driver", driver)

    cmd = [exe, "-i", "-g", str(args.audio_gain)]
    if driver:
        cmd.extend(["-a", driver])
    _append_cli_audio_output_option(cmd, driver, str(getattr(args, "audio_output", "")))
    if args.audio_period_size:
        cmd.extend(["-o", f"audio.period-size={int(args.audio_period_size)}"])
    if args.audio_periods:
        cmd.extend(["-o", f"audio.periods={int(args.audio_periods)}"])
    if args.audio_samplerate:
        cmd.extend(["-o", f"synth.sample-rate={int(args.audio_samplerate)}"])
    if int(getattr(args, "audio_polyphony", 0) or 0) > 0:
        cmd.extend(["-o", f"synth.polyphony={int(getattr(args, 'audio_polyphony', 0))}"])
    cmd.extend([args.soundfont, midi_path])
    log_path = str(getattr(args, "audio_log", "") or "")
    log_fh: Optional[IO[str]] = None
    log_lock: Optional[threading.Lock] = None
    if log_path:
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            raise ValueError(f"Audio log directory not found: {log_dir}")
        log_fh = open(log_path, "a", encoding="utf-8")
        log_lock = threading.Lock()
        ts = datetime.now().isoformat(timespec="seconds")
        _write_audio_log_line(log_fh, log_lock, f"[{ts}] CMD: {' '.join(cmd)}")
        _write_audio_log_line(log_fh, log_lock, f"[autosync] selected_driver={driver or 'default'}")

    ready_timeout = 0.25
    env = _fluidsynth_env(driver, str(getattr(args, "audio_output", "")))

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    if log_fh:
        setattr(p, "_synthermia_log_fh", log_fh)
    start = time.perf_counter()
    ready_perf: Optional[float] = None
    ready_markers = ("Using PipeWire audio driver", "Using PulseAudio audio driver", "Using ALSA audio driver")
    error_markers = ("Failed to create the audio driver", "error:")
    deadline = start + ready_timeout
    stderr = p.stderr
    if stderr is not None:
        while time.perf_counter() < deadline:
            if p.poll() is not None:
                break
            rlist, _, _ = select.select([stderr], [], [], 0.05)
            if not rlist:
                continue
            line = stderr.readline()
            if not line:
                continue
            if log_fh:
                with log_lock:
                    log_fh.write(line)
                    log_fh.flush()
            if any(m in line for m in ready_markers):
                ready_perf = time.perf_counter()
                break
            if any(m in line for m in error_markers):
                break
    t_out = threading.Thread(target=_drain_stream, args=(p.stdout, log_fh, log_lock), daemon=True)
    t_err = threading.Thread(target=_drain_stream, args=(p.stderr, log_fh, log_lock), daemon=True)
    t_out.start()
    t_err.start()
    pw_ready_perf: Optional[float] = None
    pw_latency = 0.0
    if driver == "pipewire":
        pw_ready_perf, pw_latency = _probe_pipewire_stream_timing(p.pid, timeout=1.6)
    output_latency = pw_latency if pw_latency > 0 else _estimate_fluidsynth_output_latency_seconds(
        args,
        p.pid,
        skip_pipewire_probe=(driver == "pipewire"),
    )
    resolved_ready_perf = pw_ready_perf if pw_ready_perf is not None else (ready_perf if ready_perf is not None else start)
    _write_audio_log_line(
        log_fh,
        log_lock,
        f"[autosync] ready_after={(resolved_ready_perf - start):.3f}s output_latency={output_latency:.3f}s pid={p.pid} source={'pw' if pw_ready_perf is not None else 'fs'}",
    )
    return p, resolved_ready_perf, output_latency


def stop_process(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    log_fh = getattr(proc, "_synthermia_log_fh", None)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
    if log_fh:
        try:
            log_fh.close()
        except Exception:
            pass
