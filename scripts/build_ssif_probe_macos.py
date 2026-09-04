#!/usr/bin/env python3

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "vendor/ssif-probe-macos-arm64.toml"
SOURCE_PATH = REPOSITORY_ROOT / "native/ssif_probe/ssif_probe.c"
ARTIFACT_ROOT = REPOSITORY_ROOT / "bd_to_avp"
BIN_PATH = ARTIFACT_ROOT / "bin/ssif_probe"
LIBRARY_DIRECTORY = ARTIFACT_ROOT / "lib"
NOTICE_DIRECTORY = ARTIFACT_ROOT / "resources/notices/ssif-probe"
PROVENANCE_PATH = NOTICE_DIRECTORY / "build-provenance.json"
RELINKING_NOTICE_PATH = NOTICE_DIRECTORY / "RELINKING.md"
COMMAND_TIMEOUT_SECONDS = 300
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class NativeDependency:
    version: str
    source: str
    url: str
    sha256: str
    license: str
    source_directory: str
    library_filename: str
    install_name: str


@dataclass(frozen=True)
class BuildOptions:
    default_library: str
    embed_udfread: bool
    enable_docs: bool
    enable_tools: bool
    enable_examples: bool
    bdj_jar: str
    freetype: str
    fontconfig: str
    libxml2: str


@dataclass(frozen=True)
class SsifProbeManifest:
    schema_version: int
    platform: str
    architecture: str
    minimum_macos: str
    linkage: str
    rpath: str
    meson_version: str
    ninja_version: str
    probe_compile_flags: tuple[str, ...]
    system_link_allowlist: tuple[str, ...]
    build_options: BuildOptions
    libbluray: NativeDependency
    libudfread: NativeDependency
    filenames: dict[str, str]
    unsigned_checksums: dict[str, str]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_table(value: object, description: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{description} must be a table")
    unexpected_fields = sorted(set(value) - fields)
    if unexpected_fields:
        raise RuntimeError(f"unexpected {description} fields: {', '.join(unexpected_fields)}")
    missing_fields = sorted(fields - set(value))
    if missing_fields:
        raise RuntimeError(f"{description} fields are missing: {', '.join(missing_fields)}")
    return value


def require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{description} is missing or invalid")
    return value


def require_bool(value: object, description: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{description} is missing or invalid")
    return value


def require_int(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{description} is missing or invalid")
    return value


def parse_dependency(value: object, name: str) -> NativeDependency:
    fields = {"version", "source", "url", "sha256", "license", "source_directory", "library_filename", "install_name"}
    data = require_table(value, f"{name} manifest section", fields)
    dependency = NativeDependency(**{field: require_string(data[field], f"{name} {field}") for field in fields})
    if not SHA256_PATTERN.fullmatch(dependency.sha256):
        raise RuntimeError(f"{name} sha256 must be 64 lowercase hexadecimal characters")
    if not dependency.install_name.startswith("@rpath/"):
        raise RuntimeError(f"{name} install_name must use @rpath")
    return dependency


def parse_build_options(value: object) -> BuildOptions:
    fields = {
        "default_library",
        "embed_udfread",
        "enable_docs",
        "enable_tools",
        "enable_examples",
        "bdj_jar",
        "freetype",
        "fontconfig",
        "libxml2",
    }
    data = require_table(value, "build_options", fields)
    for field in fields - {"embed_udfread", "enable_docs", "enable_tools", "enable_examples"}:
        require_string(data[field], f"build_options {field}")
    return BuildOptions(
        default_library=require_string(data["default_library"], "build_options default_library"),
        embed_udfread=require_bool(data["embed_udfread"], "build_options embed_udfread"),
        enable_docs=require_bool(data["enable_docs"], "build_options enable_docs"),
        enable_tools=require_bool(data["enable_tools"], "build_options enable_tools"),
        enable_examples=require_bool(data["enable_examples"], "build_options enable_examples"),
        bdj_jar=require_string(data["bdj_jar"], "build_options bdj_jar"),
        freetype=require_string(data["freetype"], "build_options freetype"),
        fontconfig=require_string(data["fontconfig"], "build_options fontconfig"),
        libxml2=require_string(data["libxml2"], "build_options libxml2"),
    )


def load_manifest(path: Path) -> SsifProbeManifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    fields = {
        "schema_version",
        "platform",
        "architecture",
        "minimum_macos",
        "linkage",
        "rpath",
        "meson_version",
        "ninja_version",
        "probe_compile_flags",
        "system_link_allowlist",
        "build_options",
        "libbluray",
        "libudfread",
        "filenames",
        "unsigned_checksums",
    }
    data = require_table(data, "SSIF probe manifest", fields)
    schema_version = require_int(data["schema_version"], "schema_version")
    if schema_version != 3:
        raise RuntimeError("unsupported SSIF probe manifest schema")
    allowlist = data["system_link_allowlist"]
    if not isinstance(allowlist, list) or not all(isinstance(item, str) and item for item in allowlist):
        raise RuntimeError("system_link_allowlist is missing or invalid")
    probe_compile_flags = data["probe_compile_flags"]
    if (
        not isinstance(probe_compile_flags, list)
        or not probe_compile_flags
        or not all(isinstance(item, str) and item for item in probe_compile_flags)
    ):
        raise RuntimeError("probe_compile_flags is missing or invalid")
    filenames = require_table(data["filenames"], "filenames", {"probe", "libbluray", "libudfread"})
    checksums = require_table(
        data["unsigned_checksums"],
        "unsigned_checksums",
        {"bd_to_avp/bin/ssif_probe", "bd_to_avp/lib/libbluray.3.dylib", "bd_to_avp/lib/libudfread.3.dylib"},
    )
    for relative_path, checksum in checksums.items():
        if not SHA256_PATTERN.fullmatch(require_string(checksum, f"unsigned checksum for {relative_path}")):
            raise RuntimeError(f"unsigned checksum for {relative_path} must be 64 lowercase hexadecimal characters")
    manifest = SsifProbeManifest(
        schema_version=schema_version,
        platform=require_string(data["platform"], "platform"),
        architecture=require_string(data["architecture"], "architecture"),
        minimum_macos=require_string(data["minimum_macos"], "minimum_macos"),
        linkage=require_string(data["linkage"], "linkage"),
        rpath=require_string(data["rpath"], "rpath"),
        meson_version=require_string(data["meson_version"], "meson_version"),
        ninja_version=require_string(data["ninja_version"], "ninja_version"),
        probe_compile_flags=tuple(probe_compile_flags),
        system_link_allowlist=tuple(allowlist),
        build_options=parse_build_options(data["build_options"]),
        libbluray=parse_dependency(data["libbluray"], "libbluray"),
        libudfread=parse_dependency(data["libudfread"], "libudfread"),
        filenames={field: require_string(value, f"filenames {field}") for field, value in filenames.items()},
        unsigned_checksums={
            relative_path: require_string(checksum, relative_path) for relative_path, checksum in checksums.items()
        },
    )
    if manifest.linkage != "private-shared":
        raise RuntimeError(f"unsupported SSIF probe linkage: {manifest.linkage}")
    if manifest.rpath != "@loader_path/../lib":
        raise RuntimeError("SSIF probe rpath must be @loader_path/../lib")
    if manifest.architecture != "arm64":
        raise RuntimeError("SSIF probe architecture must be arm64")
    if manifest.build_options.default_library != "shared" or manifest.build_options.embed_udfread:
        raise RuntimeError("SSIF probe must use shared libraries and a non-embedded libudfread fallback")
    return manifest


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, cwd=cwd, env=env, timeout=COMMAND_TIMEOUT_SECONDS)


def command_output(command: list[str]) -> str:
    return subprocess.check_output(command, text=True, timeout=COMMAND_TIMEOUT_SECONDS)


def build_environment(
    temporary_directory: Path,
    meson_path: str,
    ninja_path: str,
    manifest: SsifProbeManifest,
) -> dict[str, str]:
    compiler_flags = f"-arch {manifest.architecture} -O2 -g0 -mmacosx-version-min={manifest.minimum_macos}"
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(temporary_directory / "home"),
        "TMPDIR": str(temporary_directory / "tmp"),
        "CC": "/usr/bin/clang",
        "MACOSX_DEPLOYMENT_TARGET": manifest.minimum_macos,
        "MESON": meson_path,
        "NINJA": ninja_path,
        "PKG_CONFIG_PATH": "",
        "PKG_CONFIG_LIBDIR": "",
        "PKG_CONFIG": "/usr/bin/false",
        "CFLAGS": compiler_flags,
        "CPPFLAGS": "",
        "CXXFLAGS": compiler_flags,
        "LDFLAGS": (
            f"-arch {manifest.architecture} -mmacosx-version-min={manifest.minimum_macos} "
            "-Wl,-headerpad_max_install_names"
        ),
        "CPATH": "",
        "LIBRARY_PATH": "",
        "DYLD_LIBRARY_PATH": "",
        "DYLD_FALLBACK_LIBRARY_PATH": "",
    }


def notice_directory(artifact_root: Path = ARTIFACT_ROOT) -> Path:
    return artifact_root / "resources/notices/ssif-probe"


def source_archive_path(dependency: NativeDependency, artifact_root: Path = ARTIFACT_ROOT) -> Path:
    return notice_directory(artifact_root) / dependency.source


def download_source_archive(dependency: NativeDependency) -> Path:
    archive_path = source_archive_path(dependency)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists() and sha256(archive_path) == dependency.sha256:
        return archive_path
    with tempfile.TemporaryDirectory(prefix="ssif-probe-download-") as temporary_directory:
        downloaded_path = Path(temporary_directory) / dependency.source
        run(["/usr/bin/curl", "--fail", "--location", "--retry", "3", "--output", str(downloaded_path), dependency.url])
        if sha256(downloaded_path) != dependency.sha256:
            raise RuntimeError(f"{dependency.source} checksum does not match the provenance manifest")
        shutil.copy2(downloaded_path, archive_path)
    return archive_path


def extract_source_archive(archive_path: Path, destination: Path, dependency: NativeDependency) -> Path:
    with tarfile.open(archive_path, mode="r:xz") as archive:
        archive.extractall(destination, filter="data")
    source_path = destination / dependency.source_directory
    if not source_path.is_dir():
        raise RuntimeError(f"{dependency.source} did not contain {dependency.source_directory}")
    return source_path


def prepare_libbluray_source(temporary_directory: Path, manifest: SsifProbeManifest) -> tuple[Path, Path, Path]:
    bluray_archive = download_source_archive(manifest.libbluray)
    udfread_archive = download_source_archive(manifest.libudfread)
    sources_directory = temporary_directory / "sources"
    sources_directory.mkdir()
    bluray_source = extract_source_archive(bluray_archive, sources_directory, manifest.libbluray)
    udfread_source = extract_source_archive(udfread_archive, sources_directory, manifest.libudfread)
    fallback_source = bluray_source / "subprojects/libudfread"
    fallback_source.parent.mkdir(parents=True)
    shutil.copytree(udfread_source, fallback_source)
    meson_build = bluray_source / "meson.build"
    original_meson = meson_build.read_text(encoding="utf-8")
    rewritten_meson = original_meson.replace("subproject_dir: 'contrib'", "subproject_dir: 'subprojects'", 1)
    if rewritten_meson == original_meson:
        raise RuntimeError("libbluray Meson project did not declare the expected subproject directory")
    meson_build.write_text(rewritten_meson, encoding="utf-8")
    return bluray_source, bluray_source / "COPYING", udfread_source / "COPYING"


def meson_setup_command(
    meson_path: str,
    source_directory: Path,
    build_directory: Path,
    prefix: Path,
    manifest: SsifProbeManifest,
) -> list[str]:
    options = manifest.build_options
    return [
        meson_path,
        "setup",
        str(build_directory),
        str(source_directory),
        "--prefix",
        str(prefix),
        "--libdir",
        "lib",
        "--buildtype",
        "release",
        "--wrap-mode",
        "forcefallback",
        f"-Ddefault_library={options.default_library}",
        f"-Dembed_udfread={'true' if options.embed_udfread else 'false'}",
        f"-Denable_docs={'true' if options.enable_docs else 'false'}",
        f"-Denable_tools={'true' if options.enable_tools else 'false'}",
        f"-Denable_examples={'true' if options.enable_examples else 'false'}",
        f"-Dbdj_jar={options.bdj_jar}",
        f"-Dfreetype={options.freetype}",
        f"-Dfontconfig={options.fontconfig}",
        f"-Dlibxml2={options.libxml2}",
    ]


def macho_dependencies(path: Path) -> list[str]:
    lines = command_output(["/usr/bin/otool", "-L", str(path)]).splitlines()[1:]
    return [line.strip().split(" (", 1)[0] for line in lines if line.strip()]


def macho_rpaths(path: Path) -> list[str]:
    lines = command_output(["/usr/bin/otool", "-l", str(path)]).splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 5]:
            match = re.match(r"\s*path (.+) \(offset \d+\)", candidate)
            if match:
                rpaths.append(match.group(1))
                break
    return rpaths


def rewrite_install_names(prefix: Path, manifest: SsifProbeManifest) -> tuple[Path, Path]:
    udfread_path = prefix / "lib" / manifest.libudfread.library_filename
    bluray_path = prefix / "lib" / manifest.libbluray.library_filename
    run(["/usr/bin/install_name_tool", "-id", manifest.libudfread.install_name, str(udfread_path)])
    run(["/usr/bin/install_name_tool", "-id", manifest.libbluray.install_name, str(bluray_path)])
    for dependency in macho_dependencies(bluray_path):
        if dependency.endswith(manifest.libudfread.library_filename) and dependency != manifest.libudfread.install_name:
            run(
                [
                    "/usr/bin/install_name_tool",
                    "-change",
                    dependency,
                    manifest.libudfread.install_name,
                    str(bluray_path),
                ]
            )
    return bluray_path, udfread_path


def compile_probe(prefix: Path, output_path: Path, manifest: SsifProbeManifest) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "/usr/bin/clang",
        *manifest.probe_compile_flags,
        f"-mmacosx-version-min={manifest.minimum_macos}",
        f"-I{prefix / 'include'}",
        str(SOURCE_PATH),
        "-o",
        str(output_path),
        f"-L{prefix / 'lib'}",
        "-lbluray",
        "-Wl,-rpath,@loader_path/../lib",
        "-Wl,-headerpad_max_install_names",
    ]
    run(command)


def artifact_paths(manifest: SsifProbeManifest, artifact_root: Path = ARTIFACT_ROOT) -> dict[str, Path]:
    return {
        "bd_to_avp/bin/ssif_probe": artifact_root / "bin" / manifest.filenames["probe"],
        "bd_to_avp/lib/libbluray.3.dylib": artifact_root / "lib" / manifest.filenames["libbluray"],
        "bd_to_avp/lib/libudfread.3.dylib": artifact_root / "lib" / manifest.filenames["libudfread"],
    }


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path)


def verify_no_foreign_paths(path: Path) -> None:
    output = command_output(["/usr/bin/strings", "-a", str(path)])
    prohibited = ("/opt/homebrew", "/opt/local", "/Users/")
    if any(fragment in output for fragment in prohibited):
        raise RuntimeError(f"{display_path(path)} contains a Homebrew, MacPorts, or user path")


def verify_macho_artifact(path: Path, expected_architecture: str, minimum_macos: str) -> None:
    architecture = command_output(["/usr/bin/lipo", "-archs", str(path)]).strip().split()
    if architecture != [expected_architecture]:
        raise RuntimeError(f"{display_path(path)} is not an arm64-only Mach-O artifact")
    build_version = command_output(["/usr/bin/vtool", "-show-build", str(path)])
    if f"minos {minimum_macos}" not in build_version:
        raise RuntimeError(f"{display_path(path)} minimum macOS version is not {minimum_macos}")
    verify_no_foreign_paths(path)


def verify_artifacts(
    manifest: SsifProbeManifest,
    *,
    verify_checksums: bool,
    require_provenance: bool = True,
    artifact_root: Path = ARTIFACT_ROOT,
) -> dict[str, str]:
    artifacts = artifact_paths(manifest, artifact_root)
    probe_path = artifacts["bd_to_avp/bin/ssif_probe"]
    for relative_path, path in artifacts.items():
        if not path.is_file():
            raise RuntimeError(f"required SSIF probe artifact is missing: {relative_path}")
        verify_macho_artifact(path, "arm64", manifest.minimum_macos)
    expected_dependencies = {
        probe_path: {manifest.libbluray.install_name},
        artifacts["bd_to_avp/lib/libbluray.3.dylib"]: {manifest.libudfread.install_name},
        artifacts["bd_to_avp/lib/libudfread.3.dylib"]: set(),
    }
    allowed_dependencies = set(manifest.system_link_allowlist) | {
        manifest.libbluray.install_name,
        manifest.libudfread.install_name,
    }
    for path, required in expected_dependencies.items():
        dependencies = set(macho_dependencies(path))
        if not required <= dependencies:
            raise RuntimeError(f"{display_path(path)} is missing required private dylib dependencies")
        if dependencies - allowed_dependencies:
            raise RuntimeError(f"{display_path(path)} links an unapproved dependency")
    if command_output(["/usr/bin/otool", "-D", str(artifacts["bd_to_avp/lib/libbluray.3.dylib"])]).splitlines()[1:] != [
        manifest.libbluray.install_name
    ]:
        raise RuntimeError("libbluray does not have the required private install name")
    udfread_install_names = command_output(
        ["/usr/bin/otool", "-D", str(artifacts["bd_to_avp/lib/libudfread.3.dylib"])]
    ).splitlines()[1:]
    if udfread_install_names != [manifest.libudfread.install_name]:
        raise RuntimeError("libudfread does not have the required private install name")
    if macho_rpaths(probe_path) != [manifest.rpath]:
        raise RuntimeError("ssif_probe does not have the required private-library rpath")
    for dependency in (manifest.libbluray, manifest.libudfread):
        archive_path = source_archive_path(dependency, artifact_root)
        if not archive_path.is_file() or sha256(archive_path) != dependency.sha256:
            raise RuntimeError(f"source archive verification failed for {dependency.source}")
    required_notices = [
        notice_directory(artifact_root) / "RELINKING.md",
        notice_directory(artifact_root) / "libbluray-COPYING",
        notice_directory(artifact_root) / "libudfread-COPYING",
    ]
    if require_provenance:
        required_notices.append(notice_directory(artifact_root) / "build-provenance.json")
    if any(not path.is_file() or path.stat().st_size == 0 for path in required_notices):
        raise RuntimeError("SSIF probe notices or provenance are missing")
    actual_checksums = {relative_path: sha256(path) for relative_path, path in artifacts.items()}
    if verify_checksums:
        for relative_path, checksum in actual_checksums.items():
            if checksum != manifest.unsigned_checksums[relative_path]:
                raise RuntimeError(f"unsigned checksum does not match for {relative_path}")
    verify_runtime(probe_path)
    if require_provenance:
        verify_provenance(notice_directory(artifact_root) / "build-provenance.json", manifest)
    return actual_checksums


def verify_runtime(probe_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="ssif-probe-runtime-") as temporary_directory:
        environment = {
            "HOME": temporary_directory,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": temporary_directory,
        }
        completed = subprocess.run(
            [str(probe_path), "--version"],
            cwd=temporary_directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    if completed.returncode != 0 or completed.stdout != "ssif_probe contract 2\n" or completed.stderr:
        raise RuntimeError("ssif_probe could not resolve its private libraries at runtime")


def verify_provenance(path: Path, manifest: SsifProbeManifest) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("SSIF probe build provenance is invalid") from error
    if (
        data.get("schema_version") != 1
        or data.get("architecture") != manifest.architecture
        or data.get("minimum_macos") != manifest.minimum_macos
        or data.get("meson_version") != manifest.meson_version
        or data.get("ninja_version") != manifest.ninja_version
        or data.get("probe_compile_flags") != list(manifest.probe_compile_flags)
        or data.get("unsigned_checksums") != manifest.unsigned_checksums
    ):
        raise RuntimeError("SSIF probe build provenance does not match the manifest")


def write_provenance(manifest: SsifProbeManifest, checksums: dict[str, str]) -> None:
    data = {
        "schema_version": 1,
        "builder": "scripts/build_ssif_probe_macos.py",
        "platform": manifest.platform,
        "architecture": manifest.architecture,
        "minimum_macos": manifest.minimum_macos,
        "linkage": manifest.linkage,
        "meson_version": manifest.meson_version,
        "ninja_version": manifest.ninja_version,
        "probe_compile_flags": list(manifest.probe_compile_flags),
        "meson_options": meson_setup_command("meson", Path("libbluray"), Path("build"), Path("prefix"), manifest)[4:],
        "source_archives": [
            {
                "version": dependency.version,
                "filename": dependency.source,
                "url": dependency.url,
                "sha256": dependency.sha256,
                "license": dependency.license,
            }
            for dependency in (manifest.libbluray, manifest.libudfread)
        ],
        "install_names": {"libbluray": manifest.libbluray.install_name, "libudfread": manifest.libudfread.install_name},
        "probe_rpath": manifest.rpath,
        "system_link_allowlist": list(manifest.system_link_allowlist),
        "unsigned_checksums": checksums,
    }
    PROVENANCE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_ssif_probe(manifest: SsifProbeManifest) -> None:
    tool_directory = Path(sys.executable).parent
    meson_path = tool_directory / "meson"
    ninja_path = tool_directory / "ninja"
    if not meson_path.is_file() or not ninja_path.is_file():
        raise RuntimeError("Pinned Meson and Ninja tools are missing; run uv sync before building")
    if command_output([str(meson_path), "--version"]).strip() != manifest.meson_version:
        raise RuntimeError("Meson version does not match the SSIF probe manifest")
    if command_output([str(ninja_path), "--version"]).strip() != manifest.ninja_version:
        raise RuntimeError("Ninja version does not match the SSIF probe manifest")
    with tempfile.TemporaryDirectory(prefix="ssif-probe-build-") as temporary_directory_name:
        temporary_directory = Path(temporary_directory_name)
        environment = build_environment(temporary_directory, str(meson_path), str(ninja_path), manifest)
        for directory in (Path(environment["HOME"]), Path(environment["TMPDIR"])):
            directory.mkdir()
        bluray_source, bluray_copying, udfread_copying = prepare_libbluray_source(temporary_directory, manifest)
        build_directory = temporary_directory / "build"
        prefix = temporary_directory / "prefix"
        run(meson_setup_command(str(meson_path), bluray_source, build_directory, prefix, manifest), env=environment)
        run([str(ninja_path), "-C", str(build_directory), "install"], env=environment)
        bluray_path, udfread_path = rewrite_install_names(prefix, manifest)
        staged_probe = temporary_directory / manifest.filenames["probe"]
        compile_probe(prefix, staged_probe, manifest)
        BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIBRARY_DIRECTORY.mkdir(parents=True, exist_ok=True)
        NOTICE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_probe, BIN_PATH)
        shutil.copy2(bluray_path, LIBRARY_DIRECTORY / manifest.filenames["libbluray"])
        shutil.copy2(udfread_path, LIBRARY_DIRECTORY / manifest.filenames["libudfread"])
        shutil.copy2(bluray_copying, NOTICE_DIRECTORY / "libbluray-COPYING")
        shutil.copy2(udfread_copying, NOTICE_DIRECTORY / "libudfread-COPYING")
    checksums = verify_artifacts(manifest, verify_checksums=False, require_provenance=False)
    write_provenance(manifest, checksums)


def record_checksums(manifest: SsifProbeManifest) -> dict[str, str]:
    checksums = verify_artifacts(manifest, verify_checksums=False, require_provenance=False)
    manifest_text = MANIFEST_PATH.read_text(encoding="utf-8")
    for relative_path, checksum in checksums.items():
        expression = re.compile(rf'^("{re.escape(relative_path)}"\s*=\s*")[0-9a-f]{{64}}("\s*)$', re.MULTILINE)
        manifest_text, replacements = expression.subn(rf"\g<1>{checksum}\g<2>", manifest_text)
        if replacements != 1:
            raise RuntimeError(f"could not record unsigned checksum for {relative_path}")
    MANIFEST_PATH.write_text(manifest_text, encoding="utf-8")
    write_provenance(manifest, checksums)
    return checksums


def require_macos_arm64(parser: argparse.ArgumentParser, manifest: SsifProbeManifest) -> None:
    if manifest.platform != "macOS arm64":
        raise RuntimeError(f"unsupported SSIF probe platform: {manifest.platform}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error(f"this build script requires {manifest.platform}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and verify the bundled macOS SSIF probe.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify committed bundled artifacts without building.",
    )
    modes.add_argument(
        "--record-checksums",
        action="store_true",
        help="Record checksums for already-built unsigned artifacts.",
    )
    args = parser.parse_args()
    manifest = load_manifest(MANIFEST_PATH)
    require_macos_arm64(parser, manifest)
    if args.verify_only:
        verify_artifacts(manifest, verify_checksums=True)
        print("Verified bundled SSIF probe artifacts")
        return 0
    if args.record_checksums:
        checksums = record_checksums(manifest)
        print(json.dumps(checksums, sort_keys=True))
        return 0
    build_ssif_probe(manifest)
    print(f"Built {BIN_PATH.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
