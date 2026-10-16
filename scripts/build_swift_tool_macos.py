#!/usr/bin/env python3

import argparse
import dataclasses
import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


NOTICES = Path("bd_to_avp/resources/notices")
# Bundled Swift package executables built from pinned source, by product name.
TOOLS = {
    "fx-upscale": NOTICES / "fx-upscale-build.json",
    "spatial-media-kit-tool": NOTICES / "spatial-media-kit-build.json",
}


@dataclass(frozen=True)
class BuildProvenance:
    repository: str
    revision: str
    platform: str
    minimum_macos: str
    xcode_version: str
    xcode_build_version: str
    sdk_version: str
    unsigned_sha256: str


def run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_provenance(path: Path) -> BuildProvenance:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path.name} must be a JSON object")
    fields = [field.name for field in dataclasses.fields(BuildProvenance)]
    missing = [field for field in fields if not isinstance(data.get(field), str) or not data[field]]
    if missing:
        raise RuntimeError(f"{path.name} is missing: {', '.join(missing)}")
    unexpected = sorted(set(data) - set(fields))
    if unexpected:
        raise RuntimeError(f"{path.name} has unexpected fields: {', '.join(unexpected)}")
    return BuildProvenance(**{field: data[field] for field in fields})


def write_provenance(path: Path, provenance: BuildProvenance) -> None:
    path.write_text(json.dumps(dataclasses.asdict(provenance), indent=2) + "\n", encoding="utf-8")


def verify_toolchain(product: str, provenance: BuildProvenance) -> None:
    xcode_version = subprocess.check_output(["xcodebuild", "-version"], text=True).splitlines()
    expected = [f"Xcode {provenance.xcode_version}", f"Build version {provenance.xcode_build_version}"]
    if xcode_version[:2] != expected:
        raise RuntimeError(f"{product} requires {expected[0]} ({expected[1]}), got {'; '.join(xcode_version[:2])}")
    sdk_version = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-version"], text=True).strip()
    if sdk_version != provenance.sdk_version:
        raise RuntimeError(f"{product} requires macOS SDK {provenance.sdk_version}, got {sdk_version}")


def swift_build_command(product: str, checkout: Path, minimum_macos: str) -> list[str]:
    # Map the checkout path away and drop debug info and the linker's
    # object-file debug map, so the bytes do not depend on where it builds.
    source = str(checkout.resolve())
    return [
        "swift",
        "build",
        "--package-path",
        source,
        "--configuration",
        "release",
        # Targeting the app's own minimum macOS keeps the driver from adding a
        # back-deployment rpath into the installed Xcode, which would make the
        # bytes depend on where Xcode is.
        "--triple",
        f"arm64-apple-macosx{minimum_macos}",
        "--product",
        product,
        "--disable-automatic-resolution",
        "-Xswiftc",
        "-gnone",
        "-Xswiftc",
        "-file-prefix-map",
        "-Xswiftc",
        f"{source}=/{product}",
        "-Xcc",
        "-g0",
        "-Xcc",
        f"-ffile-prefix-map={source}=/{product}",
        "-Xlinker",
        "-S",
    ]


def verify_binary(product: str, binary: Path, provenance: BuildProvenance) -> None:
    for line in subprocess.check_output(["otool", "-L", str(binary)], text=True).splitlines()[1:]:
        library = line.strip().split(" ", 1)[0]
        if not library.startswith(("/System/Library/", "/usr/lib/")):
            raise RuntimeError(f"{product} links a non-system library: {library}")
    build_version = subprocess.check_output(["vtool", "-show-build", str(binary)], text=True)
    if f"minos {provenance.minimum_macos}" not in build_version:
        raise RuntimeError(f"{product} minimum macOS version is not {provenance.minimum_macos}")
    if "arm64" not in subprocess.check_output(["file", str(binary)], text=True):
        raise RuntimeError(f"{product} is not an arm64 executable")


def build_tool(product: str, output_path: Path, provenance: BuildProvenance, *, verify: bool = True) -> str:
    with tempfile.TemporaryDirectory(prefix=f"{product}-build-") as temp_dir:
        checkout = Path(temp_dir) / "source"
        run(["git", "clone", "--filter=blob:none", provenance.repository, str(checkout)])
        run(["git", "checkout", "--detach", provenance.revision], checkout)
        command = swift_build_command(product, checkout, provenance.minimum_macos)
        run(command)
        bin_path = subprocess.check_output([*command, "--show-bin-path"], text=True).strip()
        built_binary = Path(bin_path) / product
        verify_binary(product, built_binary, provenance)
        built_sha256 = sha256(built_binary)
        if verify and built_sha256 != provenance.unsigned_sha256:
            raise RuntimeError(f"unsigned {product} checksum does not match the provenance manifest")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_binary, output_path)
        output_path.chmod(0o755)
    return built_sha256


def resolve_revision(repository: str, revision: str) -> str:
    if len(revision) == 40 and all(character in "0123456789abcdef" for character in revision):
        return revision
    output = subprocess.check_output(["git", "ls-remote", repository, revision], text=True).split()
    if not output:
        raise RuntimeError(f"{revision} was not found in {repository}")
    return output[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a pinned arm64 macOS Swift package executable.")
    parser.add_argument("tool", choices=sorted(TOOLS), help="The bundled executable to build.")
    parser.add_argument("--output", type=Path, help="Destination for the executable; defaults to bd_to_avp/bin.")
    parser.add_argument(
        "--update",
        metavar="REVISION",
        help="Move the pin to this branch, tag or full commit SHA, then write the binary and record the "
        "revision and checksum in the provenance manifest.",
    )
    args = parser.parse_args()

    repository_root = Path(__file__).resolve().parents[1]
    provenance_path = repository_root / TOOLS[args.tool]
    provenance = load_provenance(provenance_path)
    if args.update:
        provenance = dataclasses.replace(provenance, revision=resolve_revision(provenance.repository, args.update))
    if provenance.platform != "macOS arm64":
        raise RuntimeError(f"unsupported {args.tool} build platform: {provenance.platform}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error(f"this build script requires {provenance.platform}")
    verify_toolchain(args.tool, provenance)

    output_path = (args.output or repository_root / "bd_to_avp" / "bin" / args.tool).resolve()
    built_sha256 = build_tool(args.tool, output_path, provenance, verify=not args.update)
    if args.update:
        write_provenance(provenance_path, dataclasses.replace(provenance, unsigned_sha256=built_sha256))
        print(f"Pinned {provenance.repository} at {provenance.revision}")

    print(f"Wrote {output_path}")
    print(f"Unsigned SHA-256: {built_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
