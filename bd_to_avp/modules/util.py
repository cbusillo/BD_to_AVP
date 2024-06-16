import tomllib
from datetime import datetime, timedelta

from pathlib import Path

import humanize


def sorted_files_by_creation(path: Path) -> list[Path]:
    return sorted(Path(path).iterdir(), key=lambda f: f.stat().st_ctime)


def sorted_files_by_creation_filtered_on_suffix(path: Path, suffix: str) -> list[Path]:
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    sorted_files = sorted_files_by_creation(path)
    return [file for file in sorted_files if file.suffix == suffix]


def get_pyproject_data() -> tuple[dict[str, str], dict[str, str]]:
    pyproject_data = load_data_from_pyproject()
    if not pyproject_data:
        raise FileNotFoundError("pyproject.toml not found")

    tool = pyproject_data.get("tool", {})
    poetry = tool.get("poetry", {})
    briefcase = tool.get("briefcase", {})

    return poetry, briefcase


def load_data_from_pyproject() -> dict[str, dict] | None:
    project_root = Path(__file__).parent.parent.parent
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    with open(pyproject_path, "rb") as pyproject_file:
        pyproject_data = tomllib.load(pyproject_file)
        return pyproject_data


def get_common_language_options() -> list[str]:
    common_languages = [
        "English",
        "Spanish",
        "French",
        "German",
        "Chinese",
        "Japanese",
        "Portuguese",
        "Russian",
        "Italian",
        "Korean",
    ]
    return common_languages


def format_timestamp(timestamp: datetime) -> str:
    return timestamp.strftime("%I:%M:%S %p on %Y-%m-%d")


def format_timedelta(seconds_elapsed: int) -> str:
    time_difference = timedelta(seconds=seconds_elapsed)
    return humanize.naturaldelta(time_difference)


def formatted_time_elapsed(start_time: datetime) -> str:
    return format_timedelta(int((datetime.now() - start_time).total_seconds()))
