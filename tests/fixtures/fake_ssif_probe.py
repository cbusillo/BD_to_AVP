#!/usr/bin/env python3

import json
import struct
import sys
import time


HEADER = struct.Struct(">4sBBHQQQII")


def record(kind, sequence, pts=0, dts=0, primary=b"", secondary=b"", random_access=False):
    flags = 1 if random_access else 0
    return (
        HEADER.pack(
            b"SSFS",
            1,
            kind,
            flags,
            sequence,
            pts,
            dts,
            len(primary),
            len(secondary),
        )
        + primary
        + secondary
    )


def title(
    playlist, seconds, *, main=False, reason=None, clips=1, video_format=6, audio=((0x1100, 0x86), (0x1101, 0x83))
):
    clip = {
        "id": "00007",
        "video_streams": [{"pid": 4113, "coding_type": 27, "format": video_format, "rate": 1, "language": None}],
        "audio_streams": [
            {"pid": pid, "coding_type": coding_type, "format": 6, "rate": 12, "language": "eng"}
            for pid, coding_type in audio
        ],
    }
    return {
        "id": f"ssif:playlist:{playlist:05d}",
        "playlist": playlist,
        "duration_ticks": seconds * 90_000,
        "duration_seconds": float(seconds),
        "angle_count": 2 if reason == "multiple_angles_unsupported" else 1,
        "clip_count": clips,
        "main_feature": main,
        "eligible": reason is None,
        "unsupported_reason": reason,
        "clips": [clip] * clips,
    }


def titles_document(source_path):
    """Titles chosen by source file name; the default is a 3D disc with 2D and 3D feature playlists."""
    if "notitles" in source_path:
        titles = []
    elif "encrypted" in source_path:
        titles = [title(800, 5400, main=True, reason="encrypted_source_unsupported")]
    elif "multiangle" in source_path:
        titles = [title(800, 5400, main=True, reason="multiple_angles_unsupported")]
    elif "multiclip" in source_path:
        titles = [title(800, 5400, main=True, reason="multiple_clips_unsupported", clips=2)]
    elif "interlaced" in source_path:
        titles = [title(800, 5400, main=True, video_format=4)]
    elif "lpcm" in source_path:
        titles = [title(800, 5400, main=True, audio=((0x1100, 0x80), (0x1101, 0x83)))]
    else:
        titles = [
            title(900, 7200, reason="multiple_clips_unsupported", clips=2),
            title(5, 5400),
            title(1005, 5400, main=True, audio=((0x1100, 0x86), (0x1101, 0x83), (0x1102, 0x81))),
            title(1010, 135),
        ]
    return {"schema_version": 1, "type": "source.titles", "title_count": len(titles), "titles": titles}


arguments = sys.argv[1:]
if arguments == ["--version"]:
    sys.stdout.write("ssif_probe contract 2\n")
elif arguments and arguments[0] == "titles" and "oldhelper" not in arguments[1]:
    source = arguments[1]
    if "malformed" in source:
        sys.stdout.write('{"schema_version": 1, "type": "source.titles", "titles": [{"playlist": "1005"}]}')
    elif "brokenjson" in source:
        sys.stdout.write("not json")
    elif "unreadable" in source:
        sys.stderr.write(
            json.dumps({"schema_version": 1, "type": "error", "code": "source_open_failed", "message": "Unreadable."})
            + "\n"
        )
        raise SystemExit(1)
    else:
        sys.stdout.write(json.dumps(titles_document(source)))
elif arguments and arguments[0] == "inspect":
    source = arguments[1]
    sys.stdout.write(
        json.dumps(
            {
                "schema_version": 2,
                "type": "source.inspect",
                "title": {
                    "eligible": True,
                    "mvc_pids": {"base": 4113, "dependent": 4114},
                    "clips": [
                        {
                            "video_streams": [
                                {
                                    "pid": 4113,
                                    "coding_type": 27,
                                    "format": 4 if "interlaced" in source else 6,
                                    "rate": 1,
                                }
                            ],
                            "audio_streams": [
                                {
                                    "pid": 4352,
                                    "coding_type": 128 if "lpcm" in source else 129,
                                    "format": 6,
                                    "rate": 3,
                                    "language": "eng",
                                }
                            ],
                        }
                    ],
                },
            }
        )
    )
elif arguments and arguments[0] == "stream-service" and "bounded" in arguments[1]:
    # Two GOPs; a four-pair bound ends inside the second one.
    sys.stdout.buffer.write(record(2, 0, 90, 90, b"audio-zero"))
    sys.stdout.buffer.write(record(1, 1, 90, 90, b"base-zero", b"dependent-zero", True))
    sys.stdout.buffer.write(record(1, 2, 3843, 3843, b"base-one", b"dependent-one"))
    sys.stdout.buffer.write(record(1, 3, 7597, 7597, b"base-two", b"dependent-two", True))
    sys.stdout.buffer.write(record(1, 4, 11351, 11351, b"base-three", b"dependent-three"))
    sys.stdout.buffer.write(record(3, 5))
    sys.stdout.buffer.flush()
elif arguments and arguments[0] == "stream-service" and "long" in arguments[1]:
    # 960 frames at 24000/1001 in twelve-frame GOPs, one audio record per GOP.
    sequence = 0
    for frame in range(960):
        pts = 90 + frame * 3754
        if frame % 12 == 0:
            sys.stdout.buffer.write(record(2, sequence, pts, pts, b"audio"))
            sequence += 1
        sys.stdout.buffer.write(record(1, sequence, pts, pts, f"base-{frame}".encode(), b"d" * 2_000, frame % 12 == 0))
        sequence += 1
    sys.stdout.buffer.write(record(3, sequence))
    sys.stdout.buffer.flush()
elif arguments and arguments[0] == "stream-service" and "slow" in arguments[1]:
    # One complete GOP, then a source that keeps running without more media.
    sys.stdout.buffer.write(record(2, 0, 90, 90, b"audio-zero"))
    sys.stdout.buffer.write(record(1, 1, 90, 90, b"base-zero", b"dependent-zero", True))
    sys.stdout.buffer.write(record(1, 2, 3843, 3843, b"base-one", b"dependent-one", True))
    sys.stdout.buffer.flush()
    time.sleep(30)
elif arguments and arguments[0] == "stream-service" and "noaudio" in arguments[1]:
    sys.stdout.buffer.write(record(1, 0, 90, 90, b"base-zero", b"dependent-zero", True))
    sys.stdout.buffer.write(record(3, 1))
    sys.stdout.buffer.flush()
elif arguments and arguments[0] == "stream-service" and "nokey" in arguments[1]:
    sys.stdout.buffer.write(record(2, 0, 80, 80, b"audio-zero"))
    sys.stdout.buffer.write(record(1, 1, 90, 90, b"base-zero", b"dependent-zero"))
    sys.stdout.buffer.write(record(3, 2))
    sys.stdout.buffer.flush()
elif arguments and arguments[0] == "stream-service":
    source = arguments[1]
    sys.stdout.buffer.write(record(2, 0, 80, 80, b"audio-zero"))
    sys.stdout.buffer.write(record(1, 1, 90, 90, b"base-zero", b"dependent-zero", True))
    sys.stdout.buffer.flush()
    if "delay" in source:
        time.sleep(30)
    if "fail" in source:
        sys.stderr.write(
            json.dumps(
                {
                    "schema_version": 2,
                    "type": "error",
                    "code": "synthetic_producer_failure",
                    "message": "Synthetic producer failure.",
                }
            )
            + "\n"
        )
        raise SystemExit(1)
    sys.stdout.buffer.write(record(2, 2, 95, 95, b"audio-one"))
    sys.stdout.buffer.write(record(1, 3, 180090, 180090, b"base-two", b"dependent-two", True))
    sys.stdout.buffer.write(record(3, 4))
    sys.stdout.buffer.flush()
else:
    sys.stderr.write("Usage:\n  ssif_probe inspect <source> <playlist>\n")
    raise SystemExit(2)
