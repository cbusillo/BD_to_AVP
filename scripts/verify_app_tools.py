from __future__ import annotations

import argparse
import plistlib
import subprocess
from pathlib import Path

from scripts.native_app import is_mach_o, minimum_macos_versions, normalized_version


APP_PATH = Path("build/bd-to-avp/macos/app/3D Blu-ray to Vision Pro.app")
APP_TOOL_DIR = APP_PATH / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
CORE_TOOLS = {
    "ffmpeg": ["-hide_banner", "-version"],
    "ffprobe": ["-hide_banner", "-version"],
    "MP4Box": ["-version"],
}
GUI_RUNTIME_TOOLS = {
    "edge264_test": ["--help"],
    "fx-upscale": ["--help"],
    "spatial-media-kit-tool": ["--help"],
}
REQUIRED_TOOLS = CORE_TOOLS | GUI_RUNTIME_TOOLS


def run(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
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


def verify_mach_o_minimum_versions(app_path: Path) -> None:
    with (app_path / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    minimum_system_version = info.get("LSMinimumSystemVersion")
    if not isinstance(minimum_system_version, str) or not minimum_system_version.strip():
        raise RuntimeError("App Info.plist must define LSMinimumSystemVersion.")
    minimum_system_version = minimum_system_version.strip()
    expected_version = normalized_version(minimum_system_version)
    incompatible: list[str] = []
    for path in sorted(app_path.rglob("*")):
        if not path.is_file() or path.suffix in {".a", ".o"} or not is_mach_o(path):
            continue
        newer_versions = sorted(
            version for version in minimum_macos_versions(path) if normalized_version(version) > expected_version
        )
        if newer_versions:
            incompatible.append(f"{path.relative_to(app_path)}: {', '.join(newer_versions)}")
    if incompatible:
        raise RuntimeError(
            f"Packaged Mach-O requires a newer macOS version than {minimum_system_version}:\n" + "\n".join(incompatible)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify app-local command-line tools in the Briefcase app bundle.")
    parser.add_argument("--app-path", type=Path, default=APP_PATH)
    parser.add_argument("--profile", choices=["core", "release"], default="core")
    args = parser.parse_args()

    tool_dir = args.app_path / "Contents" / "Resources" / "app" / "bd_to_avp" / "bin"
    tools = CORE_TOOLS if args.profile == "core" else REQUIRED_TOOLS
    for tool_name, probe_args in tools.items():
        verify_tool(tool_dir / tool_name, probe_args)
    verify_mach_o_minimum_versions(args.app_path)
    print(f"Verified app-local tools in {tool_dir}")


if __name__ == "__main__":
    main()
