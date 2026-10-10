"""Regenerate the original, MIT-licensed Glass Steps sample MIDI."""

from pathlib import Path
import struct

from synthermia.midi import write_varlen


def track_chunk(name, events):
    payload = bytearray(b"\x00\xff\x03" + write_varlen(len(name)) + name.encode("ascii"))
    previous = 0
    for tick, order, message in sorted(events, key=lambda event: event[:2]):
        payload.extend(write_varlen(tick - previous) + message)
        previous = tick
    payload.extend(write_varlen(16 * 960 - previous) + b"\xff\x2f\x00")
    return b"MTrk" + struct.pack(">I", len(payload)) + payload


def main():
    credit = b"Copyright 2026 Synthermia contributors. MIT license."
    melody = [(0, 0, b"\xff\x51\x03\x07\xa1\x20"),
              (0, 0, b"\xff\x58\x04\x04\x02\x18\x08"),
              (0, 0, b"\xff\x02" + write_varlen(len(credit)) + credit),
              (0, 0, b"\xc0\x00")]
    chords = [(0, 0, b"\xc1\x00")]
    roots = (60, 57, 53, 55, 60, 57, 53, 55)
    phrases = ((0, 4, 7, 12, 11, 7, 4, 2), (0, 7, 4, 2, 4, 7, 11, 12))
    for bar, root in enumerate(roots):
        start = bar * 1920
        for step, interval in enumerate(phrases[bar // 4]):
            tick = start + step * 240
            note = root + 12 + interval
            velocity = (96, 72, 82, 76)[step % 4]
            melody.extend([(tick, 2, bytes((0x90, note, velocity))),
                           (tick + 185, 1, bytes((0x80, note, 0)))])
        for interval in (0, 3 if bar % 4 == 1 else 4, 7):
            note = root - 12 + interval
            chords.extend([(start, 2, bytes((0x91, note, 66 + bar % 3 * 6))),
                           (start + 1536, 1, bytes((0x81, note, 0)))])
        chords.extend([(start, 0, b"\xb1\x40\x7f"),
                       (start + 1824, 1, b"\xb1\x40\x00")])
    path = Path(__file__).resolve().parents[1] / "examples" / "glass-steps.mid"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 1, 2, 480)
                     + track_chunk("Glass Steps - melody", melody)
                     + track_chunk("Glass Steps - chords", chords))
    print(path)


if __name__ == "__main__":
    main()
