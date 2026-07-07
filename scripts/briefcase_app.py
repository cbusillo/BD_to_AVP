from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = REPO_ROOT / ".briefcase-wheelhouse"
WHEELHOUSE_REQUIREMENTS = ["pysrt==1.1.2"]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=REPO_ROOT)


def build_wheelhouse() -> None:
    WHEELHOUSE.mkdir(exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(WHEELHOUSE),
            *WHEELHOUSE_REQUIREMENTS,
        ]
    )


def briefcase_config_override() -> str:
    wheelhouse_path = WHEELHOUSE.resolve().as_posix()
    return f'requirement_installer_args=["--find-links", "{wheelhouse_path}"]'


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Briefcase with repo-local packaging fixes.")
    parser.add_argument("briefcase_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if not args.briefcase_args:
        parser.error("provide a Briefcase command, for example: create --no-input")

    build_wheelhouse()
    run(
        [
            sys.executable,
            "-m",
            "briefcase",
            *args.briefcase_args,
            "-C",
            briefcase_config_override(),
        ]
    )


if __name__ == "__main__":
    main()
