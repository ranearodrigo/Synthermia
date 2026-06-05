from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


DEFAULT_TEMPO_US_PER_QN = 500_000
WHITE_PCS = {0, 2, 4, 5, 7, 9, 11}
MIN_PIANO_NOTE = 21
MAX_PIANO_NOTE = 108
CONFIG_FILE = ".synthermia_last_config.json"


@dataclass(slots=True)
class MidiHeader:
    fmt: int
    ntrks: int
    division: int


@dataclass(slots=True)
class RawEvent:
    tick: int
    order: int
    kind: str
    note: Optional[int] = None
    velocity: Optional[int] = None
    channel: Optional[int] = None
    track: Optional[int] = None
    controller: Optional[int] = None
    value: Optional[int] = None
    tempo_us_per_qn: Optional[int] = None
    time_sig_numerator: Optional[int] = None
    time_sig_denominator: Optional[int] = None


@dataclass(slots=True)
class NoteSpan:
    note: int
    start: float
    end: float
    velocity: int
    channel: int = 0
    track: int = 0


@dataclass(slots=True)
class ControlEvent:
    time_sec: float
    channel: int
    controller: int
    value: int
    track: int = 0


@dataclass(slots=True)
class MidiTrackSummary:
    index: int
    name: str
    note_ons: int
    instrument_name: str = ""


@dataclass(slots=True)
class _AudioEvent:
    time_sec: float
    order: int  # 0=CC, 1=note-off, 2=note-on
    kind: str  # "cc" | "note_off" | "note_on"
    channel: int
    note: int = 0
    velocity: int = 0
    controller: int = 0
    value: int = 0


@dataclass(slots=True)
class _LiveMidiEvent:
    time_sec: float
    kind: str  # "note_on" | "note_off" | "cc"
    channel: int
    note: int = 0
    velocity: int = 0
    controller: int = 0
    value: int = 0


_ASEQDUMP_NOTE_ON_RE = re.compile(
    r"\bNote on\b\s+(\d+),\s*note\s+(\d+),\s*velocity\s+(\d+)",
    re.IGNORECASE,
)
_ASEQDUMP_NOTE_OFF_RE = re.compile(
    r"\bNote off\b\s+(\d+),\s*note\s+(\d+),\s*velocity\s+(\d+)",
    re.IGNORECASE,
)
_ASEQDUMP_CC_RE = re.compile(
    r"\b(?:Control change|Controller)\b\s+(\d+),\s*(?:controller|param)\s+(\d+),\s*value\s+(\d+)",
    re.IGNORECASE,
)
