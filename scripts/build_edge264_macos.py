#!/usr/bin/env python3

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import re
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
    xcode_version: str
    xcode_build_version: str
    sdk_version: str
    architecture_flags: str
    linkage: str
    unsigned_sha256: str


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
        "xcode_version",
        "xcode_build_version",
        "sdk_version",
        "architecture_flags",
        "linkage",
        "unsigned_sha256",
    )
    for field in required_fields:
        if not isinstance(data.get(field), str) or not data[field]:
            raise RuntimeError(f"edge264 provenance field is missing or invalid: {field}")
    if not re.fullmatch(r"[0-9a-f]{64}", data["unsigned_sha256"]):
        raise RuntimeError("edge264 provenance unsigned_sha256 must be 64 lowercase hexadecimal characters")
    unexpected_fields = sorted(set(data) - set(required_fields))
    if unexpected_fields:
        raise RuntimeError(f"unexpected edge264 provenance fields: {', '.join(unexpected_fields)}")
    return BuildProvenance(**{field: data[field] for field in required_fields})


def write_provenance(path: Path, provenance: BuildProvenance) -> None:
    path.write_text(json.dumps(dataclasses.asdict(provenance), indent=2) + "\n", encoding="utf-8")


def resolve_revision(repository: str, revision: str) -> str:
    """Return the commit a tag, "latest" (the highest version tag) or a full commit SHA names."""
    if re.fullmatch(r"[0-9a-f]{40}", revision):
        return revision
    pattern = "refs/tags/v*" if revision == "latest" else f"refs/tags/{revision}"
    listing = subprocess.check_output(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", repository, pattern, f"{pattern}^{{}}"],
        text=True,
    )
    tags = [line.split("\t") for line in listing.splitlines()]
    if not tags:
        raise RuntimeError(f"edge264 repository has no tag matching {revision}")
    # An annotated tag lists the tag object and, with ^{}, the commit it points at. Rank the tag
    # names only: a peeled "v1^{}" entry otherwise sorts above a later "v1-2".
    newest = next(name for _, name in tags if not name.endswith("^{}"))
    commits = {name: commit for commit, name in tags if name.removesuffix("^{}") == newest}
    return commits.get(f"{newest}^{{}}", commits[newest])


def verify_toolchain(provenance: BuildProvenance) -> None:
    xcode_version = subprocess.check_output(["xcodebuild", "-version"], text=True).splitlines()
    expected_xcode_version = [
        f"Xcode {provenance.xcode_version}",
        f"Build version {provenance.xcode_build_version}",
    ]
    if xcode_version[:2] != expected_xcode_version:
        raise RuntimeError(
            "edge264 requires "
            f"{expected_xcode_version[0]} ({expected_xcode_version[1]}), got {'; '.join(xcode_version[:2])}"
        )
    sdk_version = subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-version"],
        text=True,
    ).strip()
    if sdk_version != provenance.sdk_version:
        raise RuntimeError(f"edge264 requires macOS SDK {provenance.sdk_version}, got {sdk_version}")


def make_command(provenance: BuildProvenance, target: str) -> list[str]:
    if provenance.linkage != "static":
        raise RuntimeError(f"unsupported edge264 linkage: {provenance.linkage}")
    if provenance.architecture_flags != "-arch arm64":
        raise RuntimeError(f"unsupported edge264 architecture flags: {provenance.architecture_flags}")
    return [
        "make",
        "OS=macos",
        "HOST_OS=distribution",
        f"CFLAGS={provenance.architecture_flags}",
        "STATIC=yes",
        target,
    ]


def build_edge264(
    output_path: Path,
    provenance: BuildProvenance,
    *,
    verify: bool = True,
    must_descend_from: str | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="edge264-mvc-build-") as temp_dir:
        checkout = Path(temp_dir) / "edge264-mvc"
        run(["git", "clone", "--filter=blob:none", provenance.repository, str(checkout)])
        run(["git", "checkout", "--detach", provenance.revision], checkout)
        if must_descend_from:
            # "latest" may only move the pin forward; an older or unrelated commit is a resolution mistake.
            try:
                run(["git", "merge-base", "--is-ancestor", must_descend_from, provenance.revision], checkout)
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    f"latest edge264 tag {provenance.revision} does not descend from the pinned {must_descend_from}"
                ) from error
        build_env = os.environ.copy()
        build_env["MACOSX_DEPLOYMENT_TARGET"] = provenance.minimum_macos
        run(make_command(provenance, "check"), checkout, build_env)

        built_binary = checkout / "edge264_test"
        linked_libraries = subprocess.check_output(["otool", "-L", str(built_binary)], text=True)
        if "libedge264" in linked_libraries:
            raise RuntimeError("edge264_test was not linked statically against libedge264")
        build_version = subprocess.check_output(["vtool", "-show-build", str(built_binary)], text=True)
        if f"minos {provenance.minimum_macos}" not in build_version:
            raise RuntimeError(f"edge264_test minimum macOS version is not {provenance.minimum_macos}")
        if f"sdk {provenance.sdk_version}" not in build_version:
            raise RuntimeError(f"edge264_test macOS SDK version is not {provenance.sdk_version}")
        architecture = subprocess.check_output(["file", str(built_binary)], text=True)
        if "arm64" not in architecture:
            raise RuntimeError("edge264_test is not an arm64 executable")
        if verify:
            built_sha256 = verify_checksum(built_binary, provenance.unsigned_sha256, "unsigned edge264_test")
        else:
            built_sha256 = sha256(built_binary)

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
    parser.add_argument(
        "--update",
        metavar="REVISION",
        help='Move the pin: build this tag, full commit SHA or "latest" (the highest version tag), '
        "then write the binary and record the revision and checksum in the provenance manifest.",
    )
    parser.add_argument(
        "--repository",
        help="With --update, also move the pin to this repository URL.",
    )
    parser.add_argument(
        "--pending-update",
        action="store_true",
        help='Print the commit "latest" names if the pin is behind it, without building.',
    )
    args = parser.parse_args()
    if args.repository and not args.update:
        parser.error("--repository requires --update")

    repository_root = Path(__file__).resolve().parents[1]
    provenance_path = repository_root / PROVENANCE_RELATIVE_PATH
    provenance = load_provenance(provenance_path)
    if args.pending_update:
        latest = resolve_revision(provenance.repository, "latest")
        if latest != provenance.revision:
            print(latest)
        return 0
    pinned_revision = provenance.revision
    if args.update:
        repository = args.repository or provenance.repository
        provenance = dataclasses.replace(
            provenance,
            repository=repository,
            revision=resolve_revision(repository, args.update),
        )
    if provenance.platform != "macOS arm64":
        raise RuntimeError(f"unsupported edge264 build platform: {provenance.platform}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error(f"this build script requires {provenance.platform}")
    verify_toolchain(provenance)

    output_path = args.output.resolve()
    built_sha256 = build_edge264(
        output_path,
        provenance,
        verify=not args.update,
        must_descend_from=pinned_revision if args.update == "latest" and not args.repository else None,
    )
    if args.update:
        write_provenance(provenance_path, dataclasses.replace(provenance, unsigned_sha256=built_sha256))
        print(f"Pinned {provenance.repository} at {provenance.revision}")

    print(f"Wrote {output_path}")
    print(f"Unsigned SHA-256: {built_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
