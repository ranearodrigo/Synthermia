from __future__ import annotations

import os
import re
import struct
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple


_INSTR_NAME_STOPWORDS = {
    "track", "part", "staff", "stave", "instrument", "inst", "voice",
    "channel", "ch", "midi", "port", "lh", "rh", "left", "right",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii",
}

_INSTR_NAME_ABBREVIATIONS = {
    "fr": "french", "hrn": "horn", "hrns": "horn", "hn": "horn",
    "tpt": "trumpet", "trp": "trumpet", "tbn": "trombone", "trb": "trombone",
    "cl": "clarinet", "ob": "oboe", "bn": "bassoon", "bsn": "bassoon",
    "fl": "flute", "picc": "piccolo", "pno": "piano", "pf": "piano",
    "vln": "violin", "vl": "violin", "vla": "viola",
    "vcl": "cello", "vc": "cello", "cb": "bass", "db": "bass",
}


def _fmt_choice(value: str) -> str:
    return value if value else "<none>"


def _clamp_instrument_pair(bank: int, program: int) -> Tuple[int, int]:
    b = max(0, min(16383, int(bank)))
    p = max(0, min(127, int(program)))
    return b, p


def _split_instrument_option(value: str) -> Tuple[str, str]:
    raw = str(value or "").strip()
    if "\t" in raw:
        sf_path, payload = raw.split("\t", 1)
        return str(sf_path).strip(), str(payload).strip()
    return "", raw


def instrument_option_soundfont(value: str, default_soundfont: str = "") -> str:
    sf_path, _payload = _split_instrument_option(value)
    if sf_path:
        return sf_path
    return str(default_soundfont or "").strip()


def instrument_option_label(value: str) -> str:
    sf_path, payload = _split_instrument_option(value)
    if not sf_path:
        return payload
    sf_name = os.path.basename(sf_path) or sf_path
    return f"{sf_name} | {payload}"


def parse_instrument_spec(
    value: str,
    default_pair: Tuple[int, int] = (0, 0),
    default_soundfont: str = "",
) -> Tuple[str, int, int]:
    sf_path, payload = _split_instrument_option(value)
    m = re.search(r"(\d+)\s*:\s*(\d+)", str(payload))
    if not m:
        b, p = _clamp_instrument_pair(default_pair[0], default_pair[1])
        sf = sf_path if sf_path else str(default_soundfont or "").strip()
        return sf, b, p
    b, p = _clamp_instrument_pair(int(m.group(1)), int(m.group(2)))
    sf = sf_path if sf_path else str(default_soundfont or "").strip()
    return sf, b, p


def parse_instrument_pair(value: str, default: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    _sf, b, p = parse_instrument_spec(str(value), default_pair=default, default_soundfont="")
    return b, p


def format_instrument_option(bank: int, program: int, name: str = "", soundfont_path: str = "") -> str:
    b, p = _clamp_instrument_pair(bank, program)
    spec = f"{b}:{p}"
    clean = name.strip()
    payload = f"{spec} {clean}" if clean else spec
    sf_path = str(soundfont_path or "").strip()
    return f"{sf_path}\t{payload}" if sf_path else payload


def _normalize_saved_soundfont_library(raw: object, primary_soundfont: str = "") -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    def _add_path(path_txt: str) -> None:
        txt = str(path_txt or "").strip()
        if not txt:
            return
        key = _soundfont_identity(txt)
        if key in seen:
            return
        seen.add(key)
        out.append(txt)

    _add_path(primary_soundfont)
    if isinstance(raw, str):
        for piece in re.split(r"[;\n]", raw):
            _add_path(piece)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            _add_path(str(item))
    return out


def _soundfont_identity(path_txt: str) -> str:
    txt = str(path_txt or "").strip()
    if not txt:
        return ""
    return os.path.normcase(os.path.abspath(txt))


def _riff_chunks(data: bytes, start: int, end: int) -> List[Tuple[bytes, int, int]]:
    out: List[Tuple[bytes, int, int]] = []
    i = start
    while i + 8 <= end:
        cid = data[i : i + 4]
        size = struct.unpack_from("<I", data, i + 4)[0]
        payload_start = i + 8
        payload_end = min(end, payload_start + size)
        if payload_end < payload_start:
            break
        out.append((cid, payload_start, payload_end))
        i = payload_start + size + (size & 1)
    return out


def scan_soundfont_presets(soundfont_path: str) -> List[str]:
    if not soundfont_path or not os.path.exists(soundfont_path):
        return []
    try:
        with open(soundfont_path, "rb") as f:
            data = f.read()
    except Exception:
        return []
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"sfbk":
        return []

    phdr: Optional[bytes] = None
    for cid, payload_start, payload_end in _riff_chunks(data, 12, len(data)):
        if cid != b"LIST" or payload_end - payload_start < 4:
            continue
        if data[payload_start : payload_start + 4] != b"pdta":
            continue
        for scid, sub_start, sub_end in _riff_chunks(data, payload_start + 4, payload_end):
            if scid == b"phdr":
                phdr = data[sub_start:sub_end]
                break
        break
    if not phdr:
        return []

    rec_size = 38
    nrec = len(phdr) // rec_size
    by_pair: Dict[Tuple[int, int], str] = {}
    for i in range(nrec):
        rec = phdr[i * rec_size : (i + 1) * rec_size]
        if len(rec) < rec_size:
            continue
        name = rec[:20].split(b"\x00", 1)[0].decode("latin1", errors="ignore").strip()
        program = struct.unpack_from("<H", rec, 20)[0]
        bank = struct.unpack_from("<H", rec, 22)[0]
        if name.upper() == "EOP":
            continue
        b, p = _clamp_instrument_pair(bank, program)
        by_pair.setdefault((b, p), name)

    presets = sorted(((b, p, n) for (b, p), n in by_pair.items()), key=lambda x: (x[0], x[1], x[2].lower()))
    return [format_instrument_option(b, p, n) for b, p, n in presets]


def _build_soundfont_pair_map(soundfont_paths: Sequence[str]) -> Dict[str, set[Tuple[int, int]]]:
    out: Dict[str, set[Tuple[int, int]]] = {}
    for sf in soundfont_paths:
        sf_txt = str(sf or "").strip()
        if not sf_txt or not os.path.exists(sf_txt):
            continue
        sf_key = _soundfont_identity(sf_txt)
        try:
            pairs = {
                _clamp_instrument_pair(*parse_instrument_pair(opt, (0, 0)))
                for opt in scan_soundfont_presets(sf_txt)
            }
        except Exception:
            pairs = set()
        out[sf_key] = pairs
    return out


def _instrument_option_name(option_text: str) -> str:
    _sf, payload = _split_instrument_option(option_text)
    m = re.match(r"^\s*\d+\s*:\s*\d+\s*(.*)$", str(payload))
    if not m:
        return str(payload).strip()
    return m.group(1).strip()


def _normalize_instrument_name_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for tok in re.findall(r"[a-z]+|\d+", str(text).lower()):
        if tok.isdigit():
            continue
        tok = _INSTR_NAME_ABBREVIATIONS.get(tok, tok)
        if tok in _INSTR_NAME_STOPWORDS:
            continue
        if tok.endswith("s") and len(tok) > 3:
            tok = tok[:-1]
        if tok in _INSTR_NAME_STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


def _normalized_match_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower())).strip()


def _partial_ratio(a: str, b: str) -> float:
    a = str(a).strip()
    b = str(b).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_text = (a, b) if len(a) <= len(b) else (b, a)
    if short in long_text:
        return 1.0
    matcher = SequenceMatcher(None, short, long_text)
    best = 0.0
    for block in matcher.get_matching_blocks():
        start = max(0, block[1] - block[0])
        window = long_text[start : start + len(short)]
        if not window:
            continue
        best = max(best, SequenceMatcher(None, short, window).ratio())
        if best >= 0.995:
            break
    return best


def _match_instrument_by_name(
    query_text: str,
    instrument_options: Sequence[str],
) -> Optional[Tuple[str, float]]:
    q_tokens = _normalize_instrument_name_tokens(query_text)
    if not q_tokens:
        return None
    q_join = " ".join(q_tokens)
    q_raw = _normalized_match_text(query_text) or q_join
    q_set = set(q_tokens)
    best_option: Optional[str] = None
    best_score = 0.0

    for opt in instrument_options:
        opt_name = _instrument_option_name(opt)
        c_tokens = _normalize_instrument_name_tokens(opt_name if opt_name else opt)
        if not c_tokens:
            continue
        c_join = " ".join(c_tokens)
        c_raw = _normalized_match_text(opt_name if opt_name else opt) or c_join
        c_set = set(c_tokens)
        ratio = max(
            SequenceMatcher(None, q_join, c_join).ratio(),
            SequenceMatcher(None, q_raw, c_raw).ratio(),
        )
        partial = max(_partial_ratio(q_join, c_join), _partial_ratio(q_raw, c_raw))
        overlap = (len(q_set & c_set) / float(len(q_set))) if q_set else 0.0
        jaccard = (len(q_set & c_set) / float(len(q_set | c_set))) if (q_set or c_set) else 0.0
        subset = 1.0 if q_set and q_set.issubset(c_set) else 0.0
        score = (0.30 * ratio) + (0.25 * partial) + (0.20 * overlap) + (0.10 * jaccard) + (0.10 * subset)
        if len(q_tokens) == 1 and q_tokens[0] in c_set:
            score += 0.05
        if len(q_tokens) == 1 and any(tok.startswith(q_tokens[0]) for tok in c_tokens):
            score += 0.03
        if q_join and c_join:
            if q_join in c_join or c_join in q_join:
                score += 0.08
        score = min(score, 1.0)
        if score > best_score:
            best_score = score
            best_option = opt

    if best_option is None:
        return None
    if best_score < 0.42:
        return None
    return best_option, best_score
