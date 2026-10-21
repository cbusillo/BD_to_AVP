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


arguments = sys.argv[1:]
if arguments == ["--version"]:
    sys.stdout.write("ssif_probe contract 2\n")
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
    raise SystemExit(2)
