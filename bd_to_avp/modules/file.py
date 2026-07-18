import shutil
import subprocess

from contextlib import contextmanager
from pathlib import Path

from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.command import run_command
from bd_to_avp.process_runner import CaptureOverflowPolicy


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
        files.extend(folder.glob(f"**/*{ext}", case_sensitive=False))

    if not files:
        print(f"\nNo files found in {folder} with extensions: {extensions}")
        return None

    return max(files, key=lambda x: x.stat().st_size)


def remove_folder_if_exists(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path, ignore_errors=True)
        print(f"Removed existing directory: {folder_path}")


def path_is_relative_to(path: Path, parent: Path) -> bool:
    path_pairs = (
        (path.absolute(), parent.absolute()),
        (path.resolve(), parent.resolve()),
    )
    for candidate, candidate_parent in path_pairs:
        try:
            candidate.relative_to(candidate_parent)
            return True
        except ValueError:
            continue
    return False


def output_folder_contains_source(output_path: Path) -> bool:
    return bool(
        config.source_path
        and (config.source_path.exists() or config.source_path.is_symlink())
        and path_is_relative_to(config.source_path, output_path)
    )


def remove_output_folder_if_safe(output_path: Path, *, raise_if_unsafe: bool = False) -> bool:
    if output_folder_contains_source(output_path):
        message = f"Refusing to clear folder containing source media: {config.source_path}"
        if raise_if_unsafe:
            raise ValueError(message)
        print(message)
        return False

    remove_folder_if_exists(output_path)
    return True


def prepare_output_folder_for_source(disc_name: str) -> Path:
    output_path = config.output_root_path / disc_name
    if config.start_stage == next(iter(Stage)):
        remove_output_folder_if_safe(output_path, raise_if_unsafe=True)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def move_file_to_output_root_folder(muxed_output_path: Path) -> Path:
    final_path = config.output_root_path / muxed_output_path.name
    muxed_output_path.replace(final_path)
    if not config.keep_files:
        remove_output_folder_if_safe(muxed_output_path.parent)
    return final_path


@contextmanager
def mounted_image(image_path: Path):
    mount_point = None
    existing_mounts_command = ["hdiutil", "info"]
    existing_mounts_output = run_command(
        existing_mounts_command,
        "Check mounted images",
        capture_overflow=CaptureOverflowPolicy.FAIL,
    )
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
            mount_output = run_command(
                mount_command,
                "Mount image",
                capture_overflow=CaptureOverflowPolicy.FAIL,
            )
            for line in mount_output.split("\n"):
                if "/Volumes/" in line:
                    mount_point = line.split("\t")[-1]
                    print(f"ISO mounted successfully at {mount_point}")
                    break

        if not mount_point:
            raise RuntimeError("Failed to mount ISO or find mount point.")

        yield Path(mount_point)

    except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
        print(f"Error during ISO mount handling: {e}")
        raise

    finally:
        if mount_point and "ISO is already mounted at" not in existing_mounts_output:
            umount_command = ["hdiutil", "detach", mount_point]
            run_command(umount_command, "Unmount image")
            print(f"ISO unmounted from {mount_point}")
