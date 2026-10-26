#!/usr/bin/env python3
"""Build libbluray and libudfread as visionOS dynamic frameworks for Shiny 3D Cinema.

The source pins, Meson options and tool versions come from the macOS Blu-ray reader
manifest, so the Mac helper and the headset player always use the same LGPL sources.
"""

import argparse
import hashlib
import json
import plistlib
import shutil
import tempfile

from dataclasses import dataclass
from pathlib import Path

from scripts.build_bluray_reader_macos import (
    MANIFEST_PATH as READER_MANIFEST_PATH,
    NativeDependency,
    BlurayReaderManifest,
    command_output,
    load_manifest,
    meson_setup_command,
    prepare_libbluray_source,
    run,
    source_archive_path,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORKS_DIRECTORY = REPOSITORY_ROOT / "macos/Vendor"
PROVENANCE_PATH = FRAMEWORKS_DIRECTORY / "bluray-visionos-provenance.json"
MINIMUM_VISIONOS = "26.0"
LIBRARY_COMPILE_FLAGS = ("-O2", "-g0")
LIBBLURAY_HEADERS = (
    "bluray.h",
    "bluray-version.h",
    "clpi_data.h",
    "filesystem.h",
    "keys.h",
    "log_control.h",
    "meta_data.h",
    "overlay.h",
    "player_settings.h",
)


@dataclass(frozen=True)
class Slice:
    name: str
    sdk: str
    targets: tuple[str, ...]
    supported_platform: str


# Generic simulator builds include x86_64, so the simulator slice carries both architectures.
SLICES = (
    Slice("xros-arm64", "xros", (f"arm64-apple-xros{MINIMUM_VISIONOS}",), "XROS"),
    Slice(
        "xros-arm64_x86_64-simulator",
        "xrsimulator",
        (f"arm64-apple-xros{MINIMUM_VISIONOS}-simulator", f"x86_64-apple-xros{MINIMUM_VISIONOS}-simulator"),
        "XRSimulator",
    ),
)


@dataclass(frozen=True)
class Framework:
    name: str
    dependency: NativeDependency
    library_filename: str
    headers: tuple[str, ...]

    @property
    def install_name(self) -> str:
        return f"@rpath/{self.name}.framework/{self.name}"


def frameworks(manifest: BlurayReaderManifest) -> tuple[Framework, Framework]:
    return (
        Framework("libudfread", manifest.libudfread, manifest.libudfread.library_filename, ()),
        Framework("libbluray", manifest.libbluray, manifest.libbluray.library_filename, LIBBLURAY_HEADERS),
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cross_file(path: Path, slice_: Slice, target: str, source_root: Path) -> None:
    sdk_path = command_output(["/usr/bin/xcrun", "--sdk", slice_.sdk, "--show-sdk-path"]).strip()
    # A non-darwin system name keeps libbluray off its macOS-only DiskArbitration mount code;
    # visionOS has no DiskArbitration, and the player opens files, not mounted discs.
    compiler = ["/usr/bin/clang", "-target", target, "-isysroot", sdk_path]
    compile_flags = [*LIBRARY_COMPILE_FLAGS, f"-ffile-prefix-map={source_root}=."]
    path.write_text(
        "[binaries]\n"
        f"c = {compiler!r}\n"
        "ar = '/usr/bin/ar'\n"
        "strip = '/usr/bin/strip'\n"
        "[built-in options]\n"
        f"c_args = {compile_flags!r}\n"
        "c_link_args = ['-Wl,-headerpad_max_install_names']\n"
        "[host_machine]\n"
        "system = 'visionos'\n"
        "subsystem = 'visionos'\n"
        "kernel = 'xnu'\n"
        f"cpu_family = {'x86_64' if target.startswith('x86_64') else 'aarch64'!r}\n"
        f"cpu = {target.split('-')[0]!r}\n"
        "endian = 'little'\n",
        encoding="utf-8",
    )


def framework_info(framework: Framework, slice_: Slice) -> dict[str, object]:
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": framework.name,
        "CFBundleIdentifier": f"org.videolan.{framework.name}",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": framework.name,
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": framework.dependency.version,
        "CFBundleSupportedPlatforms": [slice_.supported_platform],
        "CFBundleVersion": framework.dependency.version,
        "MinimumOSVersion": MINIMUM_VISIONOS,
    }


def package_framework(
    framework: Framework, slice_: Slice, prefixes: list[Path], copying: Path, destination: Path
) -> Path:
    bundle = destination / f"{framework.name}.framework"
    bundle.mkdir(parents=True)
    binary = bundle / framework.name
    libraries = [str(prefix / "lib" / framework.library_filename) for prefix in prefixes]
    if len(libraries) == 1:
        shutil.copy2(libraries[0], binary)
    else:
        run(["/usr/bin/lipo", "-create", *libraries, "-output", str(binary)])
    prefix = prefixes[0]
    run(["/usr/bin/install_name_tool", "-id", framework.install_name, str(binary)])
    if framework.name == "libbluray":
        for dependency in macho_dependencies(binary):
            if dependency.endswith("libudfread.3.dylib"):
                run(
                    [
                        "/usr/bin/install_name_tool",
                        "-change",
                        dependency,
                        "@rpath/libudfread.framework/libudfread",
                        str(binary),
                    ]
                )
    if framework.headers:
        headers = bundle / "Headers"
        headers.mkdir()
        for header in framework.headers:
            shutil.copy2(prefix / "include" / framework.name / header, headers / header)
    shutil.copy2(copying, bundle / "COPYING")
    with (bundle / "Info.plist").open("wb") as file:
        plistlib.dump(framework_info(framework, slice_), file, fmt=plistlib.FMT_BINARY)
    return bundle


def macho_dependencies(path: Path) -> list[str]:
    # A universal binary lists each architecture's header line, ending in a colon.
    lines = command_output(["/usr/bin/otool", "-L", str(path)]).splitlines()
    return [line.strip().split(" (", 1)[0] for line in lines if line.strip() and not line.endswith(":")]


def build_slice(
    manifest: BlurayReaderManifest, slice_: Slice, temporary: Path, meson: Path, ninja: Path
) -> dict[str, Path]:
    work = temporary / slice_.name
    work.mkdir()
    bluray_source, bluray_copying, udfread_copying = prepare_libbluray_source(work, manifest)
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "NINJA": str(ninja), "HOME": str(work)}
    prefixes = []
    for target in slice_.targets:
        architecture = target.split("-")[0]
        cross = work / f"cross-{architecture}.ini"
        cross_file(cross, slice_, target, work / "sources")
        build = work / f"build-{architecture}"
        prefix = work / f"prefix-{architecture}"
        setup = meson_setup_command(str(meson), bluray_source, build, prefix, manifest)
        run([*setup, "--cross-file", str(cross)], env=environment)
        run([str(ninja), "-C", str(build), "install"], env=environment)
        prefixes.append(prefix)
    copying = {"libbluray": bluray_copying, "libudfread": udfread_copying}
    output = work / "frameworks"
    return {
        framework.name: package_framework(framework, slice_, prefixes, copying[framework.name], output)
        for framework in frameworks(manifest)
    }


def slice_binary(framework: Framework, slice_: Slice) -> Path:
    return (
        FRAMEWORKS_DIRECTORY
        / f"{framework.name}.xcframework"
        / slice_.name
        / f"{framework.name}.framework"
        / framework.name
    )


def checksums(manifest: BlurayReaderManifest) -> dict[str, str]:
    return {
        str(slice_binary(framework, slice_).relative_to(REPOSITORY_ROOT)): sha256(slice_binary(framework, slice_))
        for framework in frameworks(manifest)
        for slice_ in SLICES
    }


def verify(manifest: BlurayReaderManifest) -> None:
    provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    if provenance.get("unsigned_checksums") != checksums(manifest):
        raise RuntimeError("visionOS Blu-ray framework checksums do not match their provenance")
    for framework in frameworks(manifest):
        if not source_archive_path(framework.dependency).is_file():
            raise RuntimeError(f"corresponding source archive is missing for {framework.name}")
        for slice_ in SLICES:
            binary = slice_binary(framework, slice_)
            build = command_output(["/usr/bin/vtool", "-show-build", str(binary)])
            platform = "VISIONOSSIMULATOR" if slice_.sdk == "xrsimulator" else "VISIONOS"
            if f"platform {platform}" not in build or f"minos {MINIMUM_VISIONOS}" not in build:
                raise RuntimeError(f"{binary} is not a visionOS {MINIMUM_VISIONOS} {slice_.name} binary")
            allowed = {framework.install_name, "/usr/lib/libSystem.B.dylib"}
            if framework.name == "libbluray":
                allowed.add("@rpath/libudfread.framework/libudfread")
            if set(macho_dependencies(binary)) - allowed:
                raise RuntimeError(f"{binary} links an unapproved dependency")


def build(manifest: BlurayReaderManifest) -> None:
    tools = REPOSITORY_ROOT / ".venv/bin"
    meson, ninja = tools / "meson", tools / "ninja"
    if command_output([str(meson), "--version"]).strip() != manifest.meson_version:
        raise RuntimeError("Meson version does not match the Blu-ray reader manifest; run uv sync")
    if command_output([str(ninja), "--version"]).strip() != manifest.ninja_version:
        raise RuntimeError("Ninja version does not match the Blu-ray reader manifest; run uv sync")
    with tempfile.TemporaryDirectory(prefix="bluray-visionos-") as temporary_name:
        temporary = Path(temporary_name)
        built = {slice_.name: build_slice(manifest, slice_, temporary, meson, ninja) for slice_ in SLICES}
        FRAMEWORKS_DIRECTORY.mkdir(parents=True, exist_ok=True)
        for framework in frameworks(manifest):
            output = FRAMEWORKS_DIRECTORY / f"{framework.name}.xcframework"
            shutil.rmtree(output, ignore_errors=True)
            command = ["/usr/bin/xcrun", "xcodebuild", "-create-xcframework"]
            for slice_ in SLICES:
                command += ["-framework", str(built[slice_.name][framework.name])]
            run([*command, "-output", str(output)])
    write_provenance(manifest)
    verify(manifest)


def write_provenance(manifest: BlurayReaderManifest) -> None:
    provenance = {
        "schema_version": 1,
        "builder": "scripts/build_bluray_visionos.py",
        "source_manifest": str(READER_MANIFEST_PATH.relative_to(REPOSITORY_ROOT)),
        "minimum_visionos": MINIMUM_VISIONOS,
        "targets": [target for slice_ in SLICES for target in slice_.targets],
        "library_compile_flags": list(LIBRARY_COMPILE_FLAGS),
        "meson_options": meson_setup_command("meson", Path("libbluray"), Path("build"), Path("prefix"), manifest)[4:],
        "cross_host_system": "visionos",
        "source_archives": [
            {
                "version": framework.dependency.version,
                "filename": framework.dependency.source,
                "sha256": framework.dependency.sha256,
                "license": framework.dependency.license,
                "install_name": framework.install_name,
            }
            for framework in frameworks(manifest)
        ],
        "unsigned_checksums": checksums(manifest),
    }
    PROVENANCE_PATH.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Verify the committed frameworks.")
    args = parser.parse_args()
    manifest = load_manifest(READER_MANIFEST_PATH)
    if args.verify_only:
        verify(manifest)
        print("Verified visionOS Blu-ray frameworks")
        return 0
    build(manifest)
    print(f"Built {FRAMEWORKS_DIRECTORY.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
