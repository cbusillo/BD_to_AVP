"""Keep MakeMKV away from damaged or mounted disc images.

Reading a truncated Blu-ray image through macOS's kernel UDF driver can leave
the Mac unable to unmount it until restart. Images are therefore always read as
plain files (``iso:``), and an image shorter than its own filesystem claims is
refused before any MakeMKV run. Nothing here mounts or unmounts anything.
"""

from __future__ import annotations

import os
import plistlib
import struct
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from bd_to_avp.modules.command import run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.process_runner import ProcessRunnerError

SECTOR_SIZE = 2048
UDF_ANCHOR_SECTOR = 256
VOLUME_RECOGNITION_START_SECTOR = 16
VOLUME_RECOGNITION_SECTOR_LIMIT = 64
VOLUME_DESCRIPTOR_SEQUENCE_SECTOR_LIMIT = 64
UDF_NSR_IDENTIFIERS = frozenset({b"NSR02", b"NSR03"})
UDF_TAG_ANCHOR = 2
UDF_TAG_PARTITION = 5
UDF_TAG_TERMINATOR = 8
DISK_TOOL_TIMEOUT_SECONDS = 20.0
HDIUTIL_PATH = Path("/usr/bin/hdiutil")
DISKUTIL_PATH = Path("/usr/sbin/diskutil")

UNKNOWN_BACKING_MESSAGE = (
    "This folder is on a mounted disc image. Choose the ISO file itself instead of the mounted disc."
)


class DiscImageSourceError(ValueError):
    code = "disc_image_unreadable"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IncompleteDiscImageError(DiscImageSourceError):
    code = "disc_image_incomplete"

    def __init__(self, missing_bytes: int) -> None:
        super().__init__(
            f"This disc image is incomplete (about {format_size(missing_bytes)} missing). Download or rip it again."
        )
        self.missing_bytes = missing_bytes


class MountedImageBackingUnknownError(DiscImageSourceError):
    code = "mounted_image_backing_unknown"

    def __init__(self) -> None:
        super().__init__(UNKNOWN_BACKING_MESSAGE)


@dataclass(frozen=True)
class AttachedImage:
    image_path: str | None
    device_entries: tuple[str, ...]
    mount_points: tuple[Path, ...]


def format_size(byte_count: int) -> str:
    if byte_count >= 100_000_000:
        return f"{byte_count / 1_000_000_000:.1f} GB"
    return f"{max(1, round(byte_count / 1_000_000))} MB"


def _read_sector(handle: BinaryIO, sector: int) -> bytes | None:
    handle.seek(sector * SECTOR_SIZE)
    data = handle.read(SECTOR_SIZE)
    return data if len(data) == SECTOR_SIZE else None


def _udf_tag(data: bytes, sector: int) -> int | None:
    identifier, _version, checksum, _reserved, _serial, _crc, _crc_length, location = struct.unpack_from(
        "<HHBBHHHI", data, 0
    )
    if sum(data[0:4] + data[5:16]) % 256 != checksum or location != sector:
        return None
    return identifier


def _has_udf_recognition_sequence(handle: BinaryIO) -> bool:
    for sector in range(
        VOLUME_RECOGNITION_START_SECTOR, VOLUME_RECOGNITION_START_SECTOR + VOLUME_RECOGNITION_SECTOR_LIMIT
    ):
        data = _read_sector(handle, sector)
        if data is None or data[1:6] == b"\x00" * 5:
            return False
        if data[1:6] in UDF_NSR_IDENTIFIERS:
            return True
    return False


def _udf_claimed_sectors(handle: BinaryIO) -> int | None:
    if not _has_udf_recognition_sequence(handle):
        return None
    anchor = _read_sector(handle, UDF_ANCHOR_SECTOR)
    if anchor is None or _udf_tag(anchor, UDF_ANCHOR_SECTOR) != UDF_TAG_ANCHOR:
        return None
    main_length, main_location, reserve_length, reserve_location = struct.unpack_from("<IIII", anchor, 16)
    claimed = max(
        main_location + main_length // SECTOR_SIZE,
        reserve_location + reserve_length // SECTOR_SIZE,
    )
    sequence_sectors = min(main_length // SECTOR_SIZE, VOLUME_DESCRIPTOR_SEQUENCE_SECTOR_LIMIT)
    for sector in range(main_location, main_location + sequence_sectors):
        descriptor = _read_sector(handle, sector)
        if descriptor is None:
            break
        tag = _udf_tag(descriptor, sector)
        if tag == UDF_TAG_TERMINATOR:
            break
        if tag == UDF_TAG_PARTITION:
            partition_start, partition_length = struct.unpack_from("<II", descriptor, 188)
            claimed = max(claimed, partition_start + partition_length)
    return claimed


def _iso9660_claimed_bytes(handle: BinaryIO) -> int | None:
    descriptor = _read_sector(handle, VOLUME_RECOGNITION_START_SECTOR)
    if descriptor is None or descriptor[0] != 1 or descriptor[1:6] != b"CD001":
        return None
    volume_space_size = struct.unpack_from("<I", descriptor, 80)[0]
    logical_block_size = struct.unpack_from("<H", descriptor, 128)[0]
    if logical_block_size == 0:
        return None
    return volume_space_size * logical_block_size


def claimed_image_size(handle: BinaryIO) -> int | None:
    """Return the size the image's own UDF or ISO 9660 descriptors claim, if recognizable."""
    claims = []
    udf_sectors = _udf_claimed_sectors(handle)
    if udf_sectors is not None:
        claims.append(udf_sectors * SECTOR_SIZE)
    iso_bytes = _iso9660_claimed_bytes(handle)
    if iso_bytes is not None:
        claims.append(iso_bytes)
    return max(claims) if claims else None


def require_complete_disc_image(image_path: Path) -> None:
    """Refuse an image that is shorter than its filesystem says, reading it only as a plain file."""
    try:
        with image_path.open("rb") as handle:
            file_size = os.fstat(handle.fileno()).st_size
            claimed_size = claimed_image_size(handle)
    except OSError:
        return
    if claimed_size is not None and file_size < claimed_size:
        raise IncompleteDiscImageError(claimed_size - file_size)


def _run_disk_tool(argv: list[Any], tool_id: str) -> bytes | None:
    try:
        result = run_process_capture(
            argv,
            f"Read {tool_id} information",
            tool_id=tool_id,
            timeout_seconds=DISK_TOOL_TIMEOUT_SECONDS,
            show_command=False,
        )
    except (OSError, subprocess.CalledProcessError, ProcessRunnerError):
        return None
    if result.returncode != 0 or result.stdout.truncated:
        return None
    return result.stdout.capture


def _parse_plist(payload: bytes | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        parsed = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def attached_disk_images() -> list[AttachedImage] | None:
    """List attached disk images from ``hdiutil info``; ``None`` when that cannot be read."""
    info = _parse_plist(_run_disk_tool([HDIUTIL_PATH, "info", "-plist"], "hdiutil"))
    if info is None:
        return None
    images = []
    for raw_image in info.get("images", []):
        if not isinstance(raw_image, dict):
            continue
        entities = [entity for entity in raw_image.get("system-entities", []) if isinstance(entity, dict)]
        image_path = raw_image.get("image-path")
        images.append(
            AttachedImage(
                image_path=image_path if isinstance(image_path, str) else None,
                device_entries=tuple(
                    entity["dev-entry"] for entity in entities if isinstance(entity.get("dev-entry"), str)
                ),
                mount_points=tuple(
                    Path(entity["mount-point"]) for entity in entities if isinstance(entity.get("mount-point"), str)
                ),
            )
        )
    return images


def _mount_point(path: Path) -> Path:
    current = path
    while not os.path.ismount(current) and current.parent != current:
        current = current.parent
    return current


def volume_is_disk_image(path: Path) -> bool:
    """Ask ``diskutil`` whether the volume holding ``path`` is backed by a disk image."""
    info = _parse_plist(_run_disk_tool([DISKUTIL_PATH, "info", "-plist", _mount_point(path)], "diskutil"))
    return info is not None and info.get("BusProtocol") == "Disk Image"


def _device_number(device_entry: str) -> int | None:
    try:
        return os.stat(device_entry).st_rdev
    except OSError:
        return None


def _image_holds_folder(image: AttachedImage, folder: Path, folder_device: int) -> bool:
    if any(folder.is_relative_to(mount_point.resolve()) for mount_point in image.mount_points):
        return True
    return any(_device_number(entry) == folder_device for entry in image.device_entries)


def _iter_matching_images(images: list[AttachedImage], folder: Path) -> Iterator[AttachedImage]:
    folder_device = folder.stat().st_dev
    for image in images:
        if _image_holds_folder(image, folder, folder_device):
            yield image


def backing_image_for_folder(folder: Path) -> Path | None:
    """Return the image file behind a folder on a mounted disc image, or ``None`` for an ordinary folder.

    Raises ``MountedImageBackingUnknownError`` when the folder is on a mounted image whose file
    cannot be determined, so the caller never reads it through the mounted filesystem.
    """
    resolved = folder.resolve()
    images = attached_disk_images()
    if images is None:
        if volume_is_disk_image(resolved):
            raise MountedImageBackingUnknownError()
        return None
    for image in _iter_matching_images(images, resolved):
        if image.image_path is None:
            raise MountedImageBackingUnknownError()
        image_path = Path(image.image_path)
        if image_path.suffix.lower() not in config.IMAGE_EXTENSIONS or not image_path.is_file():
            raise MountedImageBackingUnknownError()
        return image_path
    return None
