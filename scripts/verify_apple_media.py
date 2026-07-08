from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile

from pathlib import Path


class AppleMediaFailure(RuntimeError):
    pass


def run(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def verify_apple_media_compatible(media_path: Path) -> None:
    if not media_path.is_file():
        raise AppleMediaFailure(f"Missing media file: {media_path}")
    avconvert_path = shutil.which("avconvert")
    if avconvert_path is None:
        raise AppleMediaFailure("Apple media compatibility check requires avconvert on macOS")

    with tempfile.TemporaryDirectory(prefix="bd-to-avp-avconvert-") as temp_dir:
        output_path = Path(temp_dir) / "passthrough.mov"
        try:
            run(
                [
                    avconvert_path,
                    "--source",
                    media_path,
                    "--preset",
                    "PresetPassthrough",
                    "--output",
                    output_path,
                    "--replace",
                    "--disableFastStart",
                ]
            )
        except subprocess.CalledProcessError as error:
            output = error.stdout or ""
            raise AppleMediaFailure(
                f"Apple media stack could not open/pass through {media_path}:\n{output.strip()}"
            ) from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify media with Apple's avconvert passthrough before QuickTime/AVP manual testing."
    )
    parser.add_argument("media", nargs="+", type=Path, help="MOV/MP4 file(s) to verify")
    args = parser.parse_args()

    try:
        for media_path in args.media:
            verify_apple_media_compatible(media_path)
            print(f"Apple media compatibility passed: {media_path}")
    except AppleMediaFailure as error:
        print(f"Apple media compatibility failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
