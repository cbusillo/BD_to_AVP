import os
import shutil

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.command import run_command


def normalize_name(name: str) -> str:
    return name.lower().replace("_", " ").replace(" ", "_")


def file_exists_normalized(target_path: Path) -> bool:
    target_dir = target_path.parent
    normalized_target_name = normalize_name(target_path.name)
    for item in target_dir.iterdir():
        if normalize_name(item.name) == normalized_target_name:
            return True
    return False


def sanitize_filename(name: str) -> str:
    allowed_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"
    return "".join(c if c in allowed_chars else "" for c in name)


def find_largest_file_with_extensions(folder: Path, extensions: list[str]) -> Path | None:
    files: list[Path] = []
    for ext in extensions:
        files.extend(folder.glob(f"**/*{ext}"))

    if not files:
        print(f"\nNo files found in {folder} with extensions: {extensions}")
        return None

    return max(files, key=lambda x: x.stat().st_size)


@contextmanager
def temporary_fifo(*names: str) -> Generator[list[Path], None, None]:
    if not names:
        raise ValueError("At least one FIFO name must be provided.")
    fifos = [Path(f"/tmp/{name}") for name in names]
    try:
        for fifo in fifos:
            fifo.unlink(missing_ok=True)
            os.mkfifo(fifo)
        yield fifos
    finally:
        for fifo in fifos:
            fifo.unlink()


def remove_folder_if_exists(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path, ignore_errors=True)
        print(f"Removed existing directory: {folder_path}")


def prepare_output_folder_for_source(disc_name: str) -> Path:
    output_path = config.output_root_path / disc_name
    if config.start_stage == list(Stage)[0]:
        remove_folder_if_exists(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def move_file_to_output_root_folder(muxed_output_path: Path) -> None:
    final_path = config.output_root_path / muxed_output_path.name
    muxed_output_path.replace(final_path)
    if not config.keep_files:
        remove_folder_if_exists(muxed_output_path.parent)


@contextmanager
def mounted_image(image_path: Path):
    mount_point = None
    existing_mounts_command = ["hdiutil", "info"]
    existing_mounts_output = run_command(existing_mounts_command, "Check mounted images")
    try:
        for line in existing_mounts_output.split("\n"):
            if str(image_path) in line:
                mount_line_index = existing_mounts_output.split("\n").index(line) + 1
                while "/dev/disk" not in existing_mounts_output.split("\n")[mount_line_index]:
                    mount_line_index += 1
                mount_point = existing_mounts_output.split("\n")[mount_line_index].split("\t")[-1]
                print(f"ISO is already mounted at {mount_point}")
                break

        if not mount_point:
            mount_command = ["hdiutil", "attach", image_path]
            mount_output = run_command(mount_command, "Mount image")
            for line in mount_output.split("\n"):
                if "/Volumes/" in line:
                    mount_point = line.split("\t")[-1]
                    print(f"ISO mounted successfully at {mount_point}")
                    break

        if not mount_point:
            raise RuntimeError("Failed to mount ISO or find mount point.")

        yield Path(mount_point)

    except Exception as e:
        print(f"Error during ISO mount handling: {e}")
        raise

    finally:
        if mount_point and "ISO is already mounted at" not in existing_mounts_output:
            umount_command = ["hdiutil", "detach", mount_point]
            run_command(umount_command, "Unmount image")
            print(f"ISO unmounted from {mount_point}")
