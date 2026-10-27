#!/usr/bin/env python3
"""Build FFmpegDCA.xcframework: FFmpeg's LGPL DTS decoder for visionOS, as a dynamic framework.

The framework holds a decoder-only libavcodec and libavutil with nothing but the `dca` decoder,
plus the small wrapper in native/ffmpeg_dca. It exports only the wrapper's functions. Shiny 3D
Cinema uses it to decode the DTS core of DTS, DTS-HD High Resolution and DTS-HD Master Audio
tracks. Anyone can rebuild and replace the framework from the pinned source with this script.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

from pathlib import Path

from scripts import framework_dsyms

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "vendor/ffmpeg-dca-visionos.toml"
COMMAND_TIMEOUT_SECONDS = 900


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path | None = None, environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def xcrun(sdk: str, *arguments: str) -> str:
    return run(["xcrun", "--sdk", sdk, *arguments])


def fetch_source(manifest: dict, cache_directory: Path) -> Path:
    ffmpeg = manifest["ffmpeg"]
    cache_directory.mkdir(parents=True, exist_ok=True)
    archive = cache_directory / ffmpeg["source"]
    if not archive.exists():
        with urllib.request.urlopen(ffmpeg["url"], timeout=120) as response:
            archive.write_bytes(response.read())
    actual = sha256(archive)
    if actual != ffmpeg["sha256"]:
        raise RuntimeError(f"{archive} has SHA-256 {actual}, expected {ffmpeg['sha256']}")
    return archive


def extract_source(archive: Path, destination: Path, source_directory: str) -> Path:
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    return destination / source_directory


def rearchive_with_unique_names(build_directory: Path, library: str, destination: Path) -> Path:
    """Returns a copy of FFmpeg's static library whose members all have different names.

    libavutil.a holds two cpu.o and two crc.o, the generic and the per-architecture ones, and
    dsymutil tells archive members apart only by name and date, so it would leave one of each
    out of the dSYM. The copy holds the same objects, named by their build paths.
    """
    archive = build_directory / library / f"{library}.a"
    members = sorted(name for name in run(["ar", "-t", str(archive)]).splitlines() if not name.startswith("__.SYMDEF"))
    objects = sorted((build_directory / library).rglob("*.o"))
    if sorted(path.name for path in objects) != members:
        raise RuntimeError(f"the objects built in {library} are not the members of {archive}")
    destination.mkdir(parents=True)
    copies = []
    for path in objects:
        copy = destination / str(path.relative_to(build_directory)).replace("/", "-")
        shutil.copy2(path, copy)
        copies.append(str(copy))
    unique = destination / archive.name
    run(["ar", "rcs", str(unique), *copies], environment={**os.environ, "ZERO_AR_DATE": "1"})
    return unique


def build_architecture(manifest: dict, source: Path, work: Path, sdk: str, architecture: dict) -> Path:
    """Builds FFmpeg and the wrapper for one architecture and returns the linked, unstripped dylib."""
    target = architecture["target"]
    sdk_path = xcrun(sdk, "--show-sdk-path")
    compiler = xcrun(sdk, "--find", "clang")
    name = f"{sdk}-{architecture['arch']}"
    build_directory = work / f"build-{name}"
    prefix = work / f"prefix-{name}"
    build_directory.mkdir(parents=True)
    environment = {**os.environ, "ZERO_AR_DATE": "1"}
    run(
        [
            str(source / "configure"),
            f"--prefix={prefix}",
            "--enable-cross-compile",
            "--target-os=darwin",
            f"--arch={architecture['arch']}",
            f"--cc={compiler}",
            f"--sysroot={sdk_path}",
            f"--extra-cflags=-target {target} {' '.join(framework_dsyms.compile_flags(work))}",
            f"--extra-ldflags=-target {target}",
            *manifest["ffmpeg"]["configure_flags"],
            *architecture.get("configure_flags", []),
        ],
        cwd=build_directory,
        environment=environment,
    )
    license_line = next(
        (line for line in (build_directory / "config.h").read_text().splitlines() if "FFMPEG_LICENSE" in line),
        "",
    )
    if f'"{manifest["expected_license"]}"' not in license_line:
        raise RuntimeError(f"FFmpeg configured with an unexpected license for {name}: {license_line!r}")
    config = set((build_directory / "config.h").read_text().splitlines())
    for option in ("CONFIG_GPL", "CONFIG_VERSION3", "CONFIG_NONFREE"):
        if f"#define {option} 0" not in config:
            raise RuntimeError(f"FFmpeg configuration for {name} does not disable {option}")
    run(["make", f"-j{os.cpu_count() or 4}", "install"], cwd=build_directory, environment=environment)

    header = REPOSITORY_ROOT / manifest["wrapper_header"]
    common = ["-target", target, "-isysroot", sdk_path]
    wrapper_objects = []
    for wrapper_source in manifest["wrapper_sources"]:
        wrapper_object = work / f"{Path(wrapper_source).stem}-{name}.o"
        run(
            [
                compiler,
                *common,
                "-O2",
                *framework_dsyms.compile_flags(work),
                f"-I{prefix / 'include'}",
                f"-I{header.parent}",
                "-c",
                str(REPOSITORY_ROOT / wrapper_source),
                "-o",
                str(wrapper_object),
            ],
            cwd=work,
            environment=environment,
        )
        wrapper_objects.append(str(wrapper_object))
    exported = work / f"exported-{name}.txt"
    exported.write_text(f"{manifest['exported_symbol_prefix']}*\n")
    framework_name = manifest["framework"]
    dylib = work / f"{framework_name}-{name}"
    run(
        [
            compiler,
            *common,
            "-dynamiclib",
            *wrapper_objects,
            *[
                str(rearchive_with_unique_names(build_directory, library, work / f"objects-{name}-{library}"))
                for library in ("libavcodec", "libavutil")
            ],
            "-install_name",
            f"@rpath/{framework_name}.framework/{framework_name}",
            "-Wl,-exported_symbols_list," + str(exported),
            "-Wl,-dead_strip",
            *framework_dsyms.link_flags(work),
            "-o",
            str(dylib),
        ],
        cwd=work,
        environment=environment,
    )
    return dylib


def build_slice(manifest: dict, source: Path, work: Path, slice_spec: dict) -> tuple[Path, Path]:
    """Returns the framework and its dSYM."""
    sdk = slice_spec["sdk"]
    dylibs = [
        build_architecture(manifest, source, work, sdk, architecture) for architecture in slice_spec["architectures"]
    ]
    framework_name = manifest["framework"]
    framework = work / f"frameworks-{sdk}" / f"{framework_name}.framework"
    (framework / "Headers").mkdir(parents=True)
    (framework / "Modules").mkdir()
    header = REPOSITORY_ROOT / manifest["wrapper_header"]
    shutil.copy2(header, framework / "Headers" / header.name)
    (framework / "Modules/module.modulemap").write_text(
        f'framework module {framework_name} {{\n    umbrella header "{header.name}"\n    export *\n}}\n'
    )
    # One binary per slice; the simulator slice is universal (arm64 and x86_64).
    binary = framework / framework_name
    run(["lipo", "-create", *[str(dylib) for dylib in dylibs], "-output", str(binary)])
    dsym = work / f"dsyms-{sdk}" / f"{framework_name}.framework.dSYM"
    framework_dsyms.write_dsym(binary, work, dsym)
    run(["strip", "-x", str(binary)])
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": framework_name,
        "CFBundleIdentifier": manifest["bundle_identifier"],
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": framework_name,
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": manifest["ffmpeg"]["version"],
        "CFBundleSupportedPlatforms": [slice_spec["platform"]],
        "CFBundleVersion": manifest["ffmpeg"]["version"],
        "MinimumOSVersion": manifest["minimum_visionos"],
    }
    info_path = framework / "Info.plist"
    info_path.write_text(json.dumps(info))
    run(["plutil", "-convert", "xml1", str(info_path)])
    shutil.copy2(source / manifest["ffmpeg"]["license_file"], framework / "LICENSE")
    return framework, dsym


def build(cache_directory: Path) -> None:
    manifest = tomllib.loads(MANIFEST_PATH.read_text())
    archive = fetch_source(manifest, cache_directory)
    output = REPOSITORY_ROOT / manifest["output"]
    notices = REPOSITORY_ROOT / manifest["notices"]
    with tempfile.TemporaryDirectory(prefix="ffmpeg-dca-") as temporary:
        work = Path(temporary).resolve()
        source = extract_source(archive, work, manifest["ffmpeg"]["source_directory"])
        frameworks = [build_slice(manifest, source, work, slice_spec) for slice_spec in manifest["slices"]]
        if output.exists():
            shutil.rmtree(output)
        command = ["xcodebuild", "-create-xcframework"]
        for framework, dsym in frameworks:
            command += ["-framework", str(framework), "-debug-symbols", str(dsym)]
        run([*command, "-output", str(output)])
        framework_dsyms.verify(output)
        notices.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / manifest["ffmpeg"]["license_file"], notices / manifest["ffmpeg"]["license_file"])
    binaries = sorted(output.glob(f"*/{manifest['framework']}.framework/{manifest['framework']}"))
    provenance = {
        "schema_version": 1,
        "framework": manifest["framework"],
        "ffmpeg_version": manifest["ffmpeg"]["version"],
        "ffmpeg_url": manifest["ffmpeg"]["url"],
        "ffmpeg_sha256": manifest["ffmpeg"]["sha256"],
        "ffmpeg_license": manifest["ffmpeg"]["license"],
        "configure_flags": manifest["ffmpeg"]["configure_flags"],
        "xcode": run(["xcodebuild", "-version"]).splitlines(),
        "sdks": {spec["sdk"]: xcrun(spec["sdk"], "--show-sdk-version") for spec in manifest["slices"]},
        "binaries": {str(path.relative_to(output)): sha256(path) for path in binaries},
    }
    (notices / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--source-cache",
        type=Path,
        default=REPOSITORY_ROOT / "macos/build/ffmpeg-source",
        help="directory that keeps the downloaded FFmpeg source archive",
    )
    arguments = parser.parse_args()
    build(arguments.source_cache)


if __name__ == "__main__":
    main()
