#!/usr/bin/env python3
"""Build Edge264MVC.xcframework: the edge264-mvc H.264 MVC decoder for visionOS, as a dynamic framework.

The framework holds edge264-mvc at the revision the Mac app pins, plus the small wrapper in
native/edge264_mvc, and exports only the wrapper's functions. Shiny 3D Cinema uses it to decode
Blu-ray 3D video on the headset. Anyone can rebuild and replace the framework from the pinned
source with this script.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib

from pathlib import Path

from scripts import framework_dsyms

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "vendor/edge264-mvc-visionos.toml"
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


def checkout_source(pin: dict, destination: Path) -> str:
    """Clones the pinned revision and returns the version string edge264 reports."""
    run(["git", "clone", "--quiet", "--filter=blob:none", pin["repository"], str(destination)])
    run(["git", "checkout", "--quiet", "--detach", pin["revision"]], cwd=destination)
    return run(["git", "describe", "--tags", "--always"], cwd=destination)


def build_target(manifest: dict, source: Path, version: str, work: Path, sdk: str, target: str) -> Path:
    """Compiles edge264 and the wrapper for one target and returns the linked, unstripped dylib."""
    sdk_path = xcrun(sdk, "--show-sdk-path")
    compiler = xcrun(sdk, "--find", "clang")
    environment = {**os.environ, "ZERO_AR_DATE": "1"}
    common = ["-target", target, "-isysroot", sdk_path]
    debug = framework_dsyms.compile_flags(work)
    decoder_object = work / f"edge264mvc-{target}.o"
    run(
        [
            compiler,
            *common,
            *manifest["compile_flags"],
            *debug,
            f'-DEDGE264MVC_VERSION_STRING="{version}"',
            "-c",
            str(source / manifest["decoder_source"]),
            "-o",
            str(decoder_object),
        ],
        cwd=work,
        environment=environment,
    )
    header = REPOSITORY_ROOT / manifest["wrapper_header"]
    wrapper_objects = []
    for wrapper_source in manifest["wrapper_sources"]:
        wrapper_object = work / f"{Path(wrapper_source).stem}-{target}.o"
        run(
            [
                compiler,
                *common,
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                *debug,
                f"-I{source}",
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
    exported = work / f"exported-{target}.txt"
    exported.write_text(f"{manifest['exported_symbol_prefix']}*\n")
    framework_name = manifest["framework"]
    dylib = work / f"{framework_name}-{target}"
    run(
        [
            compiler,
            *common,
            "-dynamiclib",
            *wrapper_objects,
            str(decoder_object),
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


def build_slice(manifest: dict, source: Path, version: str, work: Path, slice_spec: dict) -> tuple[Path, Path]:
    """Returns the framework and its dSYM."""
    sdk = slice_spec["sdk"]
    dylibs = [build_target(manifest, source, version, work, sdk, target) for target in slice_spec["targets"]]
    framework_name = manifest["framework"]
    framework = work / f"frameworks-{sdk}" / f"{framework_name}.framework"
    (framework / "Headers").mkdir(parents=True)
    (framework / "Modules").mkdir()
    header = REPOSITORY_ROOT / manifest["wrapper_header"]
    shutil.copy2(header, framework / "Headers" / header.name)
    (framework / "Modules/module.modulemap").write_text(
        f'framework module {framework_name} {{\n    umbrella header "{header.name}"\n    export *\n}}\n'
    )
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
        "CFBundleShortVersionString": "1.0",
        "CFBundleSupportedPlatforms": [slice_spec["platform"]],
        "CFBundleVersion": "1",
        "MinimumOSVersion": manifest["minimum_visionos"],
    }
    info_path = framework / "Info.plist"
    info_path.write_text(json.dumps(info))
    run(["plutil", "-convert", "xml1", str(info_path)])
    shutil.copy2(source / manifest["license_file"], framework / "LICENSE")
    return framework, dsym


def build() -> None:
    manifest = tomllib.loads(MANIFEST_PATH.read_text())
    pin = json.loads((REPOSITORY_ROOT / manifest["pin"]).read_text())
    output = REPOSITORY_ROOT / manifest["output"]
    notices = REPOSITORY_ROOT / manifest["notices"]
    with tempfile.TemporaryDirectory(prefix="edge264-mvc-visionos-") as temporary:
        work = Path(temporary).resolve()
        source = work / "edge264-mvc"
        version = checkout_source(pin, source)
        frameworks = [build_slice(manifest, source, version, work, slice_spec) for slice_spec in manifest["slices"]]
        if output.exists():
            shutil.rmtree(output)
        command = ["xcodebuild", "-create-xcframework"]
        for framework, dsym in frameworks:
            command += ["-framework", str(framework), "-debug-symbols", str(dsym)]
        run([*command, "-output", str(output)])
        framework_dsyms.verify(output)
        notices.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / manifest["license_file"], notices / manifest["license_file"])
    binaries = sorted(output.glob(f"*/{manifest['framework']}.framework/{manifest['framework']}"))
    provenance = {
        "schema_version": 1,
        "framework": manifest["framework"],
        "repository": pin["repository"],
        "revision": pin["revision"],
        "version": version,
        "license": manifest["license"],
        "compile_flags": manifest["compile_flags"],
        "xcode": run(["xcodebuild", "-version"]).splitlines(),
        "sdks": {spec["sdk"]: xcrun(spec["sdk"], "--show-sdk-version") for spec in manifest["slices"]},
        "binaries": {str(path.relative_to(output)): sha256(path) for path in binaries},
    }
    (notices / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.parse_args()
    build()


if __name__ == "__main__":
    main()
