#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


REPOSITORY = "https://github.com/jens-duttke/edge264-mvc.git"
REVISION = "21723ed9e712408f1083a6397e6cd0dc89ea16d7"
DEPLOYMENT_TARGET = "14.0"


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the patched arm64 macOS edge264 MVC splitter.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("bd_to_avp/bin/edge264_test"),
        help="Destination for the statically linked splitter executable.",
    )
    args = parser.parse_args()

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("this build script requires arm64 macOS")

    repository_root = Path(__file__).resolve().parents[1]
    patch_path = repository_root / "scripts/patches/edge264-mvc-stream-input.patch"
    provenance_path = repository_root / "bd_to_avp/resources/notices/edge264-mvc-build.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    output_path = args.output.resolve()
    if sha256(patch_path) != provenance["patch_sha256"]:
        raise RuntimeError("edge264 patch checksum does not match the provenance manifest")

    with tempfile.TemporaryDirectory(prefix="edge264-mvc-build-") as temp_dir:
        checkout = Path(temp_dir) / "edge264-mvc"
        run(["git", "clone", "--filter=blob:none", REPOSITORY, str(checkout)])
        run(["git", "checkout", "--detach", REVISION], checkout)
        run(["git", "apply", str(patch_path)], checkout)
        build_env = os.environ.copy()
        build_env["MACOSX_DEPLOYMENT_TARGET"] = DEPLOYMENT_TARGET
        run(["make", "STATIC=yes", "edge264_test"], checkout, build_env)
        run(["make", "STATIC=yes", "check-stream-input"], checkout, build_env)

        built_binary = checkout / "edge264_test"
        linked_libraries = subprocess.check_output(["otool", "-L", str(built_binary)], text=True)
        if "libedge264" in linked_libraries:
            raise RuntimeError("edge264_test was not linked statically against libedge264")
        build_version = subprocess.check_output(["vtool", "-show-build", str(built_binary)], text=True)
        if f"minos {DEPLOYMENT_TARGET}" not in build_version:
            raise RuntimeError(f"edge264_test minimum macOS version is not {DEPLOYMENT_TARGET}")
        architecture = subprocess.check_output(["file", str(built_binary)], text=True)
        if "arm64" not in architecture:
            raise RuntimeError("edge264_test is not an arm64 executable")
        built_sha256 = sha256(built_binary)
        if built_sha256 != provenance["sha256"]:
            raise RuntimeError("edge264_test checksum does not match the provenance manifest")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_binary, output_path)
        output_path.chmod(0o755)

    print(f"Wrote {output_path}")
    print(f"SHA-256: {built_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
