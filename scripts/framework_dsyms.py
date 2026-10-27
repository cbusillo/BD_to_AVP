"""Debug symbols for the visionOS player's prebuilt frameworks.

App Store Connect can read a crash inside an embedded framework only when the archive holds a
dSYM whose UUIDs match that framework's binary. Each framework build compiles with debug
information, writes one dSYM per xcframework library before stripping the binary, and passes
it to `xcodebuild -create-xcframework -debug-symbols`. Xcode then copies it into every archive.
"""

import plistlib
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 900


def compile_flags(work: Path) -> list[str]:
    """Debug information without the build's temporary or checkout paths, so rebuilds match."""
    return ["-g", f"-ffile-prefix-map={work}=.", f"-ffile-prefix-map={REPOSITORY_ROOT}=."]


def link_flags(work: Path) -> list[str]:
    """Records object paths relative to the work directory; the binary's UUID hashes them."""
    return [f"-Wl,-oso_prefix,{work}/"]


def write_dsym(binary: Path, work: Path, dsym: Path) -> None:
    """Writes the dSYM for an unstripped binary under `work`, linked with `link_flags(work)`."""
    # The dSYM records the binary's path as given, so give it relative to the work directory.
    completed = subprocess.run(
        ["/usr/bin/xcrun", "dsymutil", f"--oso-prepend-path={work}", str(binary.relative_to(work)), "-o", str(dsym)],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    # dsymutil exits 0 when it cannot read an object file or finds no debug information.
    if "warning" in completed.stderr:
        raise RuntimeError(f"dsymutil could not write complete symbols for {binary}:\n{completed.stderr}")


def uuids(path: Path) -> set[str]:
    """The UUID and architecture of each slice, such as `662677C1-... (arm64)`."""
    output = subprocess.run(
        ["/usr/bin/xcrun", "dwarfdump", "--uuid", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    ).stdout
    return {" ".join(line.split()[1:3]) for line in output.splitlines() if line.startswith("UUID: ")}


def verify(xcframework: Path) -> None:
    """Checks that every library in the xcframework carries a dSYM matching its binary."""
    with (xcframework / "Info.plist").open("rb") as file:
        libraries = plistlib.load(file)["AvailableLibraries"]
    for library in libraries:
        library_directory = xcframework / library["LibraryIdentifier"]
        binary = library_directory / library["BinaryPath"]
        if "DebugSymbolsPath" not in library:
            raise RuntimeError(f"{binary} has no dSYM in {xcframework.name}")
        dsym = library_directory / library["DebugSymbolsPath"] / f"{library['LibraryPath']}.dSYM"
        expected = uuids(binary)
        if not expected or uuids(dsym) != expected:
            raise RuntimeError(f"{dsym} does not match the UUIDs of {binary}")
