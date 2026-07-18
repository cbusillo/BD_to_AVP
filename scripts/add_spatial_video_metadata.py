#!/usr/bin/env python3

import argparse
import math
import struct
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MP4BOX_PATH = REPO_ROOT / "bd_to_avp" / "bin" / "MP4Box"
MV_HEVC_SAMPLE_ENTRY_PATH = "trak.mdia.minf.stbl.stsd.hvc1"


def make_box(box_type: bytes, payload: bytes) -> bytes:
    if len(box_type) != 4:
        raise ValueError("box_type must contain exactly four bytes")
    return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def make_full_box(box_type: bytes, payload: bytes) -> bytes:
    return make_box(box_type, b"\0\0\0\0" + payload)


def spatial_vexu_content(baseline_mm: float, disparity_adjustment: float) -> bytes:
    if not math.isfinite(baseline_mm) or baseline_mm <= 0:
        raise ValueError("baseline_mm must be a positive finite number")
    if not math.isfinite(disparity_adjustment) or not -1 <= disparity_adjustment <= 1:
        raise ValueError("disparity_adjustment must be between -1 and 1")

    baseline_micrometers = round(baseline_mm * 1_000)
    if baseline_micrometers > 0xFFFFFFFF:
        raise ValueError("baseline_mm is too large for Apple spatial metadata")
    encoded_disparity = round(disparity_adjustment * 10_000)

    stereo_views = make_full_box(b"stri", b"\x03")
    camera_baseline = make_box(b"cams", make_box(b"blin", struct.pack(">Q", baseline_micrometers)))
    disparity = make_box(b"cmfy", make_box(b"dadj", struct.pack(">q", encoded_disparity)))
    eyes = make_box(b"eyes", stereo_views + camera_baseline + disparity)
    projection = make_box(b"proj", make_full_box(b"prji", b"rect"))
    return eyes + projection


def spatial_metadata_patch_xml(baseline_mm: float, disparity_adjustment: float) -> str:
    content = spatial_vexu_content(baseline_mm, disparity_adjustment).hex().upper()
    return (
        '<?xml version="1.0"?>\n'
        "<GPACBOXES>\n"
        f'  <Box path="{MV_HEVC_SAMPLE_ENTRY_PATH}.vexu" trackID="1"/>\n'
        f'  <Box path="{MV_HEVC_SAMPLE_ENTRY_PATH}.lhvC+" trackID="1">\n'
        '    <BS fcc="vexu"/>\n'
        f'    <BS data="{content}"/>\n'
        "  </Box>\n"
        "</GPACBOXES>\n"
    )


def add_spatial_video_metadata(
    input_path: Path,
    output_path: Path,
    baseline_mm: float,
    disparity_adjustment: float,
    mp4box_path: Path = DEFAULT_MP4BOX_PATH,
) -> None:
    if input_path == output_path:
        raise ValueError("input_path and output_path must be different")
    if not input_path.is_file():
        raise FileNotFoundError(f"Input movie does not exist: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    patch_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="bd-to-avp-spatial-metadata-",
            suffix=".xml",
            delete=False,
        ) as patch_file:
            patch_file.write(spatial_metadata_patch_xml(baseline_mm, disparity_adjustment))
            patch_path = Path(patch_file.name)

        subprocess.run(
            [
                str(mp4box_path),
                "-patch",
                str(patch_path),
                str(input_path),
                "-out",
                str(output_path),
            ],
            check=True,
        )
    finally:
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace MV-HEVC extended-usage metadata with complete Apple spatial-video metadata."
    )
    parser.add_argument("input_path", type=Path)
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--baseline-mm", required=True, type=float)
    parser.add_argument("--disparity-adjustment", default=0.0, type=float)
    parser.add_argument("--mp4box", default=DEFAULT_MP4BOX_PATH, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    add_spatial_video_metadata(
        args.input_path,
        args.output_path,
        args.baseline_mm,
        args.disparity_adjustment,
        args.mp4box,
    )
    print(f"Added Apple spatial-video metadata: {args.output_path}")


if __name__ == "__main__":
    main()
