#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


PROVENANCE_RELATIVE_PATH = Path("bd_to_avp/resources/notices/edge264-mvc-build.json")


@dataclass(frozen=True)
class BuildProvenance:
    repository: str
    revision: str
    platform: str
    minimum_macos: str
    linkage: str
    sha256: str


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(path: Path, expected: str, description: str) -> str:
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"{description} checksum does not match the provenance manifest")
    return actual


def load_provenance(path: Path) -> BuildProvenance:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("edge264 provenance manifest must be a JSON object")
    required_fields = (
        "repository",
        "revision",
        "platform",
        "minimum_macos",
        "linkage",
        "sha256",
    )
    for field in required_fields:
        if not isinstance(data.get(field), str) or not data[field]:
            raise RuntimeError(f"edge264 provenance field is missing or invalid: {field}")
    unexpected_fields = sorted(set(data) - set(required_fields))
    if unexpected_fields:
        raise RuntimeError(f"unexpected edge264 provenance fields: {', '.join(unexpected_fields)}")
    return BuildProvenance(**{field: data[field] for field in required_fields})


def make_command(provenance: BuildProvenance, target: str) -> list[str]:
    if provenance.linkage != "static":
        raise RuntimeError(f"unsupported edge264 linkage: {provenance.linkage}")
    return ["make", "STATIC=yes", target]


def build_edge264(output_path: Path, provenance: BuildProvenance) -> str:
    with tempfile.TemporaryDirectory(prefix="edge264-mvc-build-") as temp_dir:
        checkout = Path(temp_dir) / "edge264-mvc"
        run(["git", "clone", "--filter=blob:none", provenance.repository, str(checkout)])
        run(["git", "checkout", "--detach", provenance.revision], checkout)
        build_env = os.environ.copy()
        build_env["MACOSX_DEPLOYMENT_TARGET"] = provenance.minimum_macos
        run(make_command(provenance, "edge264_test"), checkout, build_env)
        run(make_command(provenance, "check-stream-input"), checkout, build_env)
        run(make_command(provenance, "check-edge264-test-liveness"), checkout, build_env)

        built_binary = checkout / "edge264_test"
        linked_libraries = subprocess.check_output(["otool", "-L", str(built_binary)], text=True)
        if "libedge264" in linked_libraries:
            raise RuntimeError("edge264_test was not linked statically against libedge264")
        build_version = subprocess.check_output(["vtool", "-show-build", str(built_binary)], text=True)
        if f"minos {provenance.minimum_macos}" not in build_version:
            raise RuntimeError(f"edge264_test minimum macOS version is not {provenance.minimum_macos}")
        architecture = subprocess.check_output(["file", str(built_binary)], text=True)
        if "arm64" not in architecture:
            raise RuntimeError("edge264_test is not an arm64 executable")
        built_sha256 = verify_checksum(built_binary, provenance.sha256, "edge264_test")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_binary, output_path)
        output_path.chmod(0o755)

    return built_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the pinned arm64 macOS edge264 MVC splitter.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("bd_to_avp/bin/edge264_test"),
        help="Destination for the statically linked splitter executable.",
    )
    args = parser.parse_args()

    repository_root = Path(__file__).resolve().parents[1]
    provenance = load_provenance(repository_root / PROVENANCE_RELATIVE_PATH)
    if provenance.platform != "macOS arm64":
        raise RuntimeError(f"unsupported edge264 build platform: {provenance.platform}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error(f"this build script requires {provenance.platform}")

    output_path = args.output.resolve()
    built_sha256 = build_edge264(output_path, provenance)

    print(f"Wrote {output_path}")
    print(f"SHA-256: {built_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
