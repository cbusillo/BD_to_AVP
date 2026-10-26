"""Writes forced_captions.sup, a tiny Blu-ray PG stream with regular and forced captions.

Run ``uv run python tests/fixtures/forced_pgs.py`` to rewrite it. Each caption is a solid block
whose size names it, so a test can tell which caption reached OCR from the decoded bitmap alone.
Objects shown together share a width, because pgsrip decodes a display set's objects as one
bitmap.
"""

import struct
from pathlib import Path

FIXTURE = Path(__file__).with_name("forced_captions.sup")

FORCED = 0x40
EPOCH_START = 0x80
NORMAL_CASE = 0x00

# (name, width, height); OCR in the tests answers with the name for a bitmap of that size.
REGULAR = ("REGULAR", 20, 2)
FORCED_ALONE = ("FORCED ALONE", 22, 2)
MIXED_REGULAR = ("MIXED REGULAR", 24, 2)
MIXED_FORCED = ("MIXED FORCED", 24, 3)
EARLY_REGULAR = ("EARLY REGULAR", 28, 2)
EARLY_FORCED = ("EARLY FORCED", 28, 3)
FORCED_FIRST = ("FORCED FIRST", 32, 2)
FORCED_NEXT = ("FORCED NEXT", 34, 2)


def segment(segment_type: int, milliseconds: int, payload: bytes) -> bytes:
    timestamp = milliseconds * 90
    return b"PG" + struct.pack(">IIBH", timestamp, timestamp, segment_type, len(payload)) + payload


def composition(milliseconds: int, number: int, state: int, objects: list[tuple[int, bool]]) -> bytes:
    payload = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, number, state, 0, 0, len(objects))
    for object_id, forced in objects:
        payload += struct.pack(">HBBHH", object_id, 0, FORCED if forced else 0, 100, 900)
    return segment(0x16, milliseconds, payload)


def window(milliseconds: int) -> bytes:
    return segment(0x17, milliseconds, bytes([1, 0]) + struct.pack(">HHHH", 100, 900, 64, 8))


def palette(milliseconds: int) -> bytes:
    # Entry 0 transparent, entry 1 opaque white (Y, Cr, Cb, alpha).
    return segment(0x14, milliseconds, bytes([0, 0, 0, 16, 128, 128, 0, 1, 235, 128, 128, 255]))


def picture(milliseconds: int, object_id: int, caption: tuple[str, int, int]) -> bytes:
    _, width, height = caption
    rle = bytes([0, 0x80 | width, 1, 0, 0]) * height
    header = struct.pack(">HBB", object_id, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big")
    return segment(0x15, milliseconds, header + struct.pack(">HH", width, height) + rle)


def end(milliseconds: int) -> bytes:
    return segment(0x80, milliseconds, b"")


def display_set(milliseconds: int, number: int, state: int, objects: list[tuple[int, bool]], *extra: bytes) -> bytes:
    return b"".join(
        [composition(milliseconds, number, state, objects), window(milliseconds), *extra, end(milliseconds)]
    )


def build() -> bytes:
    return b"".join(
        [
            # A regular caption, 1 to 2 s.
            display_set(1000, 0, EPOCH_START, [(0, False)], palette(1000), picture(1000, 0, REGULAR)),
            display_set(2000, 1, NORMAL_CASE, []),
            # A forced caption on its own, 3 to 4 s.
            display_set(3000, 2, EPOCH_START, [(0, True)], palette(3000), picture(3000, 0, FORCED_ALONE)),
            display_set(4000, 3, NORMAL_CASE, []),
            # A regular and a forced caption together from 5 s; the forced one leaves at 6 s
            # without a clearing composition, the regular one at 7 s.
            display_set(
                5000,
                4,
                EPOCH_START,
                [(0, False), (1, True)],
                palette(5000),
                picture(5000, 0, MIXED_REGULAR),
                picture(5000, 1, MIXED_FORCED),
            ),
            display_set(6000, 5, NORMAL_CASE, [(0, False)]),
            display_set(7000, 6, NORMAL_CASE, []),
            # Both captions defined at 8 s, where only the regular one shows; the forced one
            # shows alone from 9 to 10 s.
            display_set(
                8000,
                7,
                EPOCH_START,
                [(0, False)],
                palette(8000),
                picture(8000, 0, EARLY_REGULAR),
                picture(8000, 1, EARLY_FORCED),
            ),
            display_set(9000, 8, NORMAL_CASE, [(1, True)]),
            display_set(10000, 9, NORMAL_CASE, []),
            # One forced caption replaced by another within the epoch, 11 to 12 and 12 to 13 s.
            display_set(11000, 10, EPOCH_START, [(0, True)], palette(11000), picture(11000, 0, FORCED_FIRST)),
            display_set(12000, 11, NORMAL_CASE, [(1, True)], picture(12000, 1, FORCED_NEXT)),
            display_set(13000, 12, NORMAL_CASE, []),
        ]
    )


if __name__ == "__main__":
    FIXTURE.write_bytes(build())
