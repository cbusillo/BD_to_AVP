import io
import json
import plistlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from bd_to_avp.modules import disc, disc_image, process
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc_image import AttachedImage, SECTOR_SIZE
from bd_to_avp.worker.operations import WorkerOperationError, convert_source, resolve_job_source
from bd_to_avp.worker.ownership import WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    JobSource,
    JobSpec,
    WorkerActivityReporter,
    WorkerEventEmitter,
    WorkerSourceKind,
)
from tests.test_native_worker import conversion_request_line, decoded_events

PARTITION_START = 288
DESCRIPTOR_SEQUENCE_SECTORS = 16
MAIN_SEQUENCE_SECTOR = 32


def udf_tag(identifier: int, sector: int) -> bytes:
    tag = bytearray(struct.pack("<HHBBHHHI", identifier, 3, 0, 0, 0, 0, 0, sector))
    tag[4] = sum(tag[0:4] + tag[5:16]) % 256
    return bytes(tag)


def write_sector(path: Path, sector: int, payload: bytes) -> None:
    with path.open("r+b") as handle:
        handle.seek(sector * SECTOR_SIZE)
        handle.write(payload.ljust(SECTOR_SIZE, b"\x00"))


def build_udf_image(path: Path, *, partition_length: int, file_sectors: int) -> int:
    """Write a sparse UDF image skeleton and return the size its descriptors claim, in bytes."""
    reserve_sequence_sector = PARTITION_START + partition_length
    with path.open("wb") as handle:
        handle.truncate(file_sectors * SECTOR_SIZE)
    for offset, identifier in enumerate((b"BEA01", b"NSR03", b"TEA01")):
        write_sector(path, 16 + offset, b"\x00" + identifier + b"\x01")
    sequence_bytes = DESCRIPTOR_SEQUENCE_SECTORS * SECTOR_SIZE
    write_sector(
        path,
        256,
        udf_tag(2, 256)
        + struct.pack("<IIII", sequence_bytes, MAIN_SEQUENCE_SECTOR, sequence_bytes, reserve_sequence_sector),
    )
    partition = bytearray(udf_tag(5, MAIN_SEQUENCE_SECTOR).ljust(SECTOR_SIZE, b"\x00"))
    struct.pack_into("<II", partition, 188, PARTITION_START, partition_length)
    write_sector(path, MAIN_SEQUENCE_SECTOR, bytes(partition))
    write_sector(path, MAIN_SEQUENCE_SECTOR + 1, udf_tag(8, MAIN_SEQUENCE_SECTOR + 1))
    return (reserve_sequence_sector + DESCRIPTOR_SEQUENCE_SECTORS) * SECTOR_SIZE


def build_iso9660_image(path: Path, *, volume_blocks: int, file_bytes: int) -> int:
    with path.open("wb") as handle:
        handle.truncate(file_bytes)
    descriptor = bytearray(SECTOR_SIZE)
    descriptor[0:6] = b"\x01CD001"
    struct.pack_into("<I", descriptor, 80, volume_blocks)
    struct.pack_into("<H", descriptor, 128, SECTOR_SIZE)
    write_sector(path, 16, bytes(descriptor))
    return volume_blocks * SECTOR_SIZE


def build_mounted_volume(root: Path) -> Path:
    volume = root / "Volumes" / "Feature 3D"
    (volume / "BDMV" / "STREAM").mkdir(parents=True)
    return volume


class StopAtMakeMKV(Exception):
    pass


class IncompleteImageTests(unittest.TestCase):
    def test_truncated_udf_image_reports_missing_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "Feature.iso"
            claimed = build_udf_image(image, partition_length=4000, file_sectors=3000)

            incomplete = disc_image.find_incomplete_image(image)

            image_bytes = image.stat().st_size
            self.assertEqual(incomplete, disc_image.IncompleteImage(claimed - image_bytes, image_bytes))

    def test_complete_udf_image_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "Feature.iso"
            claimed = build_udf_image(image, partition_length=4000, file_sectors=1)
            with image.open("r+b") as handle:
                handle.truncate(claimed)

            self.assertIsNone(disc_image.find_incomplete_image(image))

    def test_truncated_iso9660_image_reports_missing_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "Feature.iso"
            claimed = build_iso9660_image(image, volume_blocks=600, file_bytes=500 * SECTOR_SIZE)

            incomplete = disc_image.find_incomplete_image(image)

            self.assertIsNotNone(incomplete)
            assert incomplete is not None
            self.assertEqual(incomplete.missing_bytes, claimed - image.stat().st_size)

    def test_unrecognized_image_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "Feature.iso"
            image.write_bytes(b"disc")

            self.assertIsNone(disc_image.find_incomplete_image(image))

    def test_truncated_image_warns_and_still_reaches_makemkv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            image = temporary_path / "Feature.iso"
            claimed = build_udf_image(image, partition_length=4000, file_sectors=3000)
            output = io.StringIO()
            activity = WorkerActivityReporter(WorkerEventEmitter(output, str(uuid4())))
            activity.set_stage_plan(["preflight", "inspect_source", "create_mkv"])
            makemkv_run = Mock(side_effect=StopAtMakeMKV)

            with (
                patch.object(config, "source_path", image),
                patch.object(config, "source_str", None),
                patch.object(config, "output_root_path", temporary_path / "output"),
                patch.object(config, "overwrite", True),
                patch.object(config, "start_stage", Stage.CREATE_MKV),
                patch.object(config, "preview_range", None),
                patch.object(config, "MAKEMKVCON_PATH", temporary_path / "makemkvcon"),
                patch("bd_to_avp.modules.process.preflight.verify_runtime_ready"),
                patch.object(disc, "run_process_capture", makemkv_run),
                self.assertRaises(StopAtMakeMKV),
            ):
                process.process_each(activity=activity, selected_title_id="makemkv:0")

            warnings = [
                event["payload"]
                for event in decoded_events(output)
                if event["type"] == "warning" and event["payload"].get("code") == "disc_image_incomplete"
            ]
            image_bytes = image.stat().st_size
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["missing_bytes"], claimed - image_bytes)
            self.assertEqual(warnings[0]["image_bytes"], image_bytes)
            makemkv_run.assert_called_once()
            self.assertIn(f"iso:{image}", makemkv_run.call_args.args[0])

    def test_cli_run_prints_incomplete_image_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "Feature.iso"
            claimed = build_udf_image(image, partition_length=4000, file_sectors=3000)
            message = Mock()

            with patch.object(config, "source_path", image), patch.object(process, "cli_message", message):
                process.report_incomplete_disc_image(None, "inspect_source", None)

            missing = disc_image.format_size(claimed - image.stat().st_size)
            message.assert_called_once()
            self.assertIn(missing, message.call_args.args[0])


class MountedImageSourceTests(unittest.TestCase):
    def test_mounted_image_folder_converts_from_backing_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            volume = build_mounted_volume(root)
            image = root / "Feature.iso"
            image.write_bytes(b"disc")
            attached = [AttachedImage(image_path=str(image), device_entries=(), mount_points=(volume,))]

            with patch.object(disc_image, "attached_disk_images", return_value=attached):
                resolved = resolve_job_source(JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=volume / "BDMV"))
                with patch.object(disc.config, "source_path", volume):
                    makemkv_source = disc.get_makemkv_source()

            self.assertEqual(resolved.kind, WorkerSourceKind.DISC_IMAGE)
            self.assertEqual(resolved.path, image)
            self.assertEqual(makemkv_source, f"iso:{image}")

    def test_mounted_image_conversion_reads_image_and_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            volume = build_mounted_volume(root)
            image = root / "Feature.iso"
            image.write_bytes(b"disc")
            destination = root / "output"
            final_path = destination / "Feature_AVP.mov"
            final_path.parent.mkdir()
            final_path.write_bytes(b"final")
            request = json.loads(
                conversion_request_line(volume, destination, source_kind=WorkerSourceKind.BLU_RAY_FOLDER.value)
            )
            request["job"]["remove_original"] = True
            job = JobSpec.from_json_line(json.dumps(request) + "\n")
            activity = WorkerActivityReporter(WorkerEventEmitter(io.StringIO(), job.job_id))
            attached = [AttachedImage(image_path=str(image), device_entries=(), mount_points=(volume,))]
            observed: dict[str, object] = {}

            def process_each(*_args: object, **_kwargs: object) -> Path:
                observed["makemkv_source"] = disc.get_makemkv_source()
                observed["remove_original"] = config.remove_original
                return final_path

            with (
                patch.object(disc_image, "attached_disk_images", return_value=attached),
                patch.object(config, "configure_tool_environment"),
                patch("bd_to_avp.modules.process.process_each", side_effect=process_each),
            ):
                convert_source(job, WorkerProcessOwner(), activity)

            self.assertEqual(observed["makemkv_source"], f"iso:{image}")
            self.assertFalse(observed["remove_original"])

    def test_ordinary_folder_still_reads_as_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = build_mounted_volume(root)
            unrelated = AttachedImage(
                image_path=str(root / "Other.iso"),
                device_entries=(),
                mount_points=(root / "Volumes" / "Other",),
            )

            with patch.object(disc_image, "attached_disk_images", return_value=[unrelated]):
                resolved = resolve_job_source(JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=folder))
                with patch.object(disc.config, "source_path", folder):
                    makemkv_source = disc.get_makemkv_source()

            self.assertEqual(resolved.kind, WorkerSourceKind.BLU_RAY_FOLDER)
            self.assertEqual(makemkv_source, f"file:{folder}")

    def test_unknown_backing_image_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            volume = build_mounted_volume(Path(temporary_directory))
            cases = {
                "no image path": [AttachedImage(image_path=None, device_entries=(), mount_points=(volume,))],
                "missing image file": [
                    AttachedImage(
                        image_path=str(Path(temporary_directory) / "gone.iso"),
                        device_entries=(),
                        mount_points=(volume,),
                    )
                ],
            }
            for name, attached in cases.items():
                with self.subTest(name), patch.object(disc_image, "attached_disk_images", return_value=attached):
                    with self.assertRaises(WorkerOperationError) as context:
                        resolve_job_source(JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=volume))
                    self.assertEqual(context.exception.code, disc_image.MountedImageBackingUnknownError.code)
                    with (
                        patch.object(disc.config, "source_path", volume),
                        self.assertRaises(disc_image.MountedImageBackingUnknownError),
                    ):
                        disc.get_makemkv_source()

    def test_unreadable_image_list_falls_back_to_volume_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            volume = build_mounted_volume(Path(temporary_directory))
            source = JobSource(kind=WorkerSourceKind.BLU_RAY_FOLDER, path=volume)

            with (
                patch.object(disc_image, "attached_disk_images", return_value=None),
                patch.object(disc_image, "volume_is_disk_image", return_value=True),
                self.assertRaises(WorkerOperationError) as context,
            ):
                resolve_job_source(source)
            self.assertEqual(context.exception.code, disc_image.MountedImageBackingUnknownError.code)

            with (
                patch.object(disc_image, "attached_disk_images", return_value=None),
                patch.object(disc_image, "volume_is_disk_image", return_value=False),
            ):
                self.assertEqual(resolve_job_source(source).kind, WorkerSourceKind.BLU_RAY_FOLDER)

    def test_hdiutil_listing_is_parsed(self) -> None:
        listing = plistlib.dumps(
            {
                "images": [
                    {
                        "image-path": "/Users/alice/Feature.iso",
                        "system-entities": [
                            {"dev-entry": "/dev/disk9"},
                            {"dev-entry": "/dev/disk9s1", "mount-point": "/Volumes/Feature 3D"},
                        ],
                    },
                    {"system-entities": [{"dev-entry": "/dev/disk10"}]},
                ]
            }
        )

        with patch.object(disc_image, "_run_disk_tool", return_value=listing):
            images = disc_image.attached_disk_images()

        self.assertEqual(
            images,
            [
                AttachedImage(
                    image_path="/Users/alice/Feature.iso",
                    device_entries=("/dev/disk9", "/dev/disk9s1"),
                    mount_points=(Path("/Volumes/Feature 3D"),),
                ),
                AttachedImage(image_path=None, device_entries=("/dev/disk10",), mount_points=()),
            ],
        )

    def test_failed_hdiutil_listing_is_unknown(self) -> None:
        for payload in (None, b"not a plist"):
            with self.subTest(payload=payload), patch.object(disc_image, "_run_disk_tool", return_value=payload):
                self.assertIsNone(disc_image.attached_disk_images())


if __name__ == "__main__":
    unittest.main()
