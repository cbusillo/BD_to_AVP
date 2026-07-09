from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


APP_PATH = Path("build/bd-to-avp/macos/app/3D Blu-ray to Vision Pro.app")
APP_TOOL_DIR = APP_PATH / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
REQUIRED_TOOLS = {
    "ffmpeg": ["-hide_banner", "-version"],
    "ffprobe": ["-hide_banner", "-version"],
    "edge264_test": ["--help"],
    "mkvextract": ["--version"],
    "mkvmerge": ["--version"],
    "MP4Box": ["-version"],
    "spatial-media-kit-tool": ["--help"],
    "tesseract": ["--version"],
}


def run(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def verify_tool(tool_path: Path, probe_args: list[str]) -> None:
    if not tool_path.is_file():
        raise FileNotFoundError(f"Missing bundled tool: {tool_path}")
    if not tool_path.stat().st_mode & 0o111:
        raise PermissionError(f"Bundled tool is not executable: {tool_path}")

    run([tool_path, *probe_args])
    linked_libraries = run(["otool", "-L", tool_path]).stdout
    for forbidden_path in ["/opt/homebrew", "/usr/local"]:
        if forbidden_path in linked_libraries:
            raise RuntimeError(f"Bundled {tool_path.name} still links to {forbidden_path}:\n{linked_libraries}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify app-local command-line tools in the Briefcase app bundle.")
    parser.add_argument("--app-path", type=Path, default=APP_PATH)
    args = parser.parse_args()

    tool_dir = args.app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
    for tool_name, probe_args in REQUIRED_TOOLS.items():
        verify_tool(tool_dir / tool_name, probe_args)
    print(f"Verified app-local tools in {tool_dir}")


if __name__ == "__main__":
    main()
