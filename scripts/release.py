from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
LOCK_PATH = REPO_ROOT / "uv.lock"
MACOS_PROJECT_PATH = REPO_ROOT / "macos" / "project.yml"
BETA3_RECOVERY_EVIDENCE_PATH = REPO_ROOT / "docs" / "release-evidence" / "v0.3.0-beta.3-recovery.json"
VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:(?P<stage>a|b|rc)(?P<prerelease>[1-9][0-9]*))?$"
)
PUBLIC_TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<stage>alpha|beta|rc)\.(?P<prerelease>[1-9][0-9]*))?$"
)
LEGACY_RC_TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"rc(?P<prerelease>[1-9][0-9]*)$"
)
RETIRED_RELEASE_TAGS = frozenset({"native-ui-preview-1", "v0.3.0-beta.1", "v0.3.0-beta.2"})
DMG_NAME_PREFIX = "3D-Blu-ray-to-Vision-Pro"
INTERNAL_STAGE_NAMES = {"a": "alpha", "b": "beta", "rc": "rc"}
PUBLIC_STAGE_SUFFIXES = {"alpha": "a", "beta": "b", "rc": "rc"}
STAGE_ORDER = {"alpha": 0, "beta": 1, "rc": 2, "stable": 3}
BETA3_RECOVERY_SOURCE_VERSION = "0.3.0rc1"
BETA3_RECOVERY_SOURCE_BUILD = "147"
BETA3_RECOVERY_TARGET_VERSION = "0.3.0b3"
BETA3_RECOVERY_TARGET_BUILD = "148"
BETA3_RECOVERY_EVIDENCE: dict[str, Any] = {
    "schema": "bd_to_avp.release_recovery",
    "schema_version": 1,
    "observed_at": "2026-07-20T02:28:48Z",
    "repository": "cbusillo/BD_to_AVP",
    "transition": {
        "source": {
            "package_version": BETA3_RECOVERY_SOURCE_VERSION,
            "public_version": "0.3.0-rc.1",
            "build": BETA3_RECOVERY_SOURCE_BUILD,
        },
        "target": {
            "package_version": BETA3_RECOVERY_TARGET_VERSION,
            "public_version": "0.3.0-beta.3",
            "release_tag": "v0.3.0-beta.3",
            "build": BETA3_RECOVERY_TARGET_BUILD,
        },
    },
    "failed_run": {
        "id": 29693513480,
        "head_sha": "438991f07fa19cd2ae2df4d3ec14716bc0371f8d",
        "conclusion": "failure",
        "skipped_boundaries": {
            "package_upload": "skipped",
            "draft": "skipped",
            "appcast": "skipped",
            "pypi": "skipped",
            "release": "skipped",
            "pages": "skipped",
        },
        "artifact_count": 0,
    },
    "absent_github_state": {
        "tags": ["v0.3.0rc1", "v0.3.0-rc.1", "v0.3.0-beta.3"],
        "releases": ["v0.3.0rc1", "v0.3.0-rc.1", "v0.3.0-beta.3"],
        "drafts": ["v0.3.0rc1", "v0.3.0-rc.1", "v0.3.0-beta.3"],
    },
    "github_latest": {"release_tag": "v0.2.143"},
    "pages": {
        "status": "enabled",
        "release_tag": "v0.2.143",
        "appcast_sha256": "d231c47d69606bc6f28113bb396c0ac9d24f656cbe0930c770259e0739f9904e",
    },
    "pypi": {
        "latest_version": "0.2.143",
        "absent_versions": ["0.3.0rc1", "0.3.0b3"],
    },
    "retired_preview_releases": [
        {
            "tag": "native-ui-preview-1",
            "immutable": True,
            "release_id": 352841376,
            "target_sha": "e1e4f851096cfeae70da1c56a88acef9c5e61055",
            "assets": [
                {
                    "asset_id": 474714228,
                    "name": "3D-Blu-ray-to-Vision-Pro-Native-Preview-0.3.0-1.dmg",
                    "size": 406585971,
                    "digest": "sha256:119a86f829c0bc8962deaea5f1a8fb92ffbcd48002d062a0ac59cccffe61af71",
                },
                {
                    "asset_id": 474714389,
                    "name": "SHA256SUMS",
                    "size": 118,
                    "digest": "sha256:1b142e59fea4ee0e0f734ae5cdf34815583765d2d0895a9ec25217436d52336e",
                },
            ],
        },
        {
            "tag": "v0.3.0-beta.1",
            "immutable": True,
            "release_id": 353900273,
            "target_sha": "2378b46345185f05611f15a39658217bac7c6960",
            "assets": [
                {
                    "asset_id": 476861235,
                    "name": "3D-Blu-ray-to-Vision-Pro-Native-Preview-0.3.0-beta.1.dmg",
                    "size": 407183981,
                    "digest": "sha256:b611dca7de660efed218a0e2fc5c4ed9d6e5e652a54da938e13d40a6bd994bed",
                },
                {
                    "asset_id": 476861481,
                    "name": "SHA256SUMS",
                    "size": 123,
                    "digest": "sha256:17eead4e3faf2ffd3110a9bf200d39f0ebfba78546f40aa0fa868fd3afb53b5b",
                },
            ],
        },
        {
            "tag": "v0.3.0-beta.2",
            "immutable": True,
            "release_id": 355845653,
            "target_sha": "9e9a38c715dbbe5df97e6d3a8ba715731607db6a",
            "assets": [
                {
                    "asset_id": 480668646,
                    "name": "3D-Blu-ray-to-Vision-Pro-Native-Preview-0.3.0-beta.2.dmg",
                    "size": 408158527,
                    "digest": "sha256:e2e8a20c9fd4517076189e56598ff6b32cd1adc544c6ad0618f5a46f55dcae24",
                },
                {
                    "asset_id": 480668779,
                    "name": "SHA256SUMS",
                    "size": 123,
                    "digest": "sha256:ab387b19cef2af5923b112735a10253d42f4fc37f002e6983deb44a5a17b7fb0",
                },
            ],
        },
    ],
}


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseVersion:
    text: str
    major: int
    minor: int
    patch: int
    stage: str
    prerelease_number: int | None

    @property
    def prerelease(self) -> bool:
        return self.stage != "stable"

    @property
    def rc(self) -> int | None:
        return self.prerelease_number if self.stage == "rc" else None

    @property
    def public_version(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if not self.prerelease:
            return base
        return f"{base}-{self.stage}.{self.prerelease_number}"

    @property
    def release_tag(self) -> str:
        return f"v{self.public_version}"

    @property
    def channel(self) -> str:
        return self.stage if self.prerelease else "stable"

    @property
    def order_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.patch,
            STAGE_ORDER[self.stage],
            self.prerelease_number or 0,
        )


@dataclass(frozen=True)
class ReleaseMetadata:
    package_version: str
    public_version: str
    build_version: str
    release_tag: str
    release_name: str
    dmg_name: str
    channel: str
    prerelease: bool
    make_latest: bool
    publish_pypi: bool

    def github_outputs(self) -> dict[str, str]:
        values = asdict(self)
        return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in values.items()}


@dataclass(frozen=True)
class PublishedRelease:
    tag_name: str
    version: ReleaseVersion
    prerelease: bool


LockRunner = Callable[[Path, str], None]
TagExists = Callable[[str], bool]
AncestorCheck = Callable[[str, str], bool]


def parse_release_version(value: str) -> ReleaseVersion:
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ReleaseError("Release version must be a canonical three-part PEP 440 Stable, Alpha, Beta, or RC version.")
    internal_stage = match.group("stage")
    return ReleaseVersion(
        text=value,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        stage=INTERNAL_STAGE_NAMES[internal_stage] if internal_stage is not None else "stable",
        prerelease_number=int(match.group("prerelease")) if match.group("prerelease") is not None else None,
    )


def parse_build_version(value: str) -> int:
    if not value.isdigit() or str(int(value)) != value or int(value) <= 1:
        raise ReleaseError("CFBundleVersion must be a canonical integer greater than 1.")
    return int(value)


def parse_release_tag(value: str, *, allow_legacy_rc: bool = True) -> ReleaseVersion:
    if value in RETIRED_RELEASE_TAGS:
        raise ReleaseError(f"Release tag belongs to the retired preview identity: {value}")

    match = PUBLIC_TAG_PATTERN.fullmatch(value)
    if match is not None:
        stage = match.group("stage")
        suffix = PUBLIC_STAGE_SUFFIXES[stage] if stage is not None else ""
        prerelease = match.group("prerelease") or ""
        return parse_release_version(
            f"{match.group('major')}.{match.group('minor')}.{match.group('patch')}{suffix}{prerelease}"
        )

    if allow_legacy_rc:
        legacy_match = LEGACY_RC_TAG_PATTERN.fullmatch(value)
        if legacy_match is not None:
            return parse_release_version(
                f"{legacy_match.group('major')}.{legacy_match.group('minor')}.{legacy_match.group('patch')}"
                f"rc{legacy_match.group('prerelease')}"
            )

    raise ReleaseError("Release tag must use vX.Y.Z, vX.Y.Z-alpha.N, vX.Y.Z-beta.N, or vX.Y.Z-rc.N.")


def _validate_production_version(version: ReleaseVersion) -> None:
    if version.release_tag in RETIRED_RELEASE_TAGS:
        raise ReleaseError(f"Release version belongs to the retired preview identity: {version.text}")


def _release_records(release_history: Any) -> list[dict[str, Any]]:
    if not isinstance(release_history, list):
        raise ReleaseError("GitHub release history must be a JSON array.")
    records: list[dict[str, Any]] = []
    for item in release_history:
        page = item if isinstance(item, list) else [item]
        for record in page:
            if not isinstance(record, dict):
                raise ReleaseError("GitHub release history contains a non-object entry.")
            records.append(record)
    return records


def _published_releases(release_history: Any) -> list[PublishedRelease]:
    releases: list[PublishedRelease] = []
    seen_tags: set[str] = set()
    seen_versions: set[str] = set()
    for record in _release_records(release_history):
        if record.get("draft") is not False or not record.get("published_at"):
            continue
        tag_name = record.get("tag_name")
        if isinstance(tag_name, str) and tag_name in RETIRED_RELEASE_TAGS:
            continue
        prerelease = record.get("prerelease")
        if not isinstance(tag_name, str) or not isinstance(prerelease, bool):
            raise ReleaseError("Published GitHub release metadata is incomplete.")
        try:
            version = parse_release_tag(tag_name)
        except ReleaseError:
            continue
        if prerelease != version.prerelease:
            raise ReleaseError(f"Published GitHub Release prerelease state disagrees with tag {tag_name}.")
        if tag_name in seen_tags:
            raise ReleaseError(f"Multiple published GitHub Releases use tag {tag_name}.")
        if version.text in seen_versions:
            raise ReleaseError(f"Multiple published GitHub Releases use version {version.text}.")
        seen_tags.add(tag_name)
        seen_versions.add(version.text)
        releases.append(PublishedRelease(tag_name=tag_name, version=version, prerelease=prerelease))
    return releases


def _git_tag_exists(tag_name: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", f"refs/tags/{tag_name}^{{commit}}"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _git_tag_is_ancestor(tag_name: str, head_ref: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", f"refs/tags/{tag_name}^{{commit}}", head_ref],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseError(f"Unable to compare release tag {tag_name} with {head_ref}.")
    return result.returncode == 0


def select_release_notes_base(
    current_tag: str,
    release_history: Any,
    head_ref: str,
    *,
    tag_exists: TagExists = _git_tag_exists,
    is_ancestor: AncestorCheck = _git_tag_is_ancestor,
) -> str:
    current_version = parse_release_tag(current_tag, allow_legacy_rc=False)
    candidates = sorted(
        (
            release
            for release in _published_releases(release_history)
            if release.version.order_key < current_version.order_key
        ),
        key=lambda release: release.version.order_key,
        reverse=True,
    )
    if not current_version.prerelease:
        candidates = [release for release in candidates if not release.prerelease and not release.version.prerelease]

    for release in candidates:
        if not tag_exists(release.tag_name):
            raise ReleaseError(f"Published release tag is missing from the checkout: {release.tag_name}")
        if not current_version.prerelease or is_ancestor(release.tag_name, head_ref):
            return release.tag_name
    return ""


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"Unable to load {path}: {error}") from error


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"Recovery evidence contains duplicate key {key!r}.")
        result[key] = value
    return result


def _validate_exact_evidence_value(actual: Any, expected: Any, location: str) -> None:
    if type(actual) is not type(expected):
        raise ReleaseError(f"Recovery evidence {location} has the wrong type.")
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append(f"missing {missing}")
            if extra:
                details.append(f"unexpected {extra}")
            raise ReleaseError(f"Recovery evidence {location} has invalid keys: {', '.join(details)}.")
        for key, expected_value in expected.items():
            _validate_exact_evidence_value(actual[key], expected_value, f"{location}.{key}")
        return
    if isinstance(expected, list):
        if len(actual) != len(expected):
            raise ReleaseError(f"Recovery evidence {location} has the wrong number of entries.")
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
            _validate_exact_evidence_value(actual_value, expected_value, f"{location}[{index}]")
        return
    if actual != expected:
        raise ReleaseError(f"Recovery evidence {location} does not match the audited value.")


def validate_beta3_recovery_evidence(
    evidence_path: Path = BETA3_RECOVERY_EVIDENCE_PATH,
) -> dict[str, Any]:
    try:
        evidence = json.loads(
            evidence_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Unable to load Beta 3 recovery evidence from {evidence_path}: {error}") from error
    _validate_exact_evidence_value(evidence, BETA3_RECOVERY_EVIDENCE, "$")
    return evidence


def _locked_project_version(lock_path: Path) -> str:
    lock = _load_toml(lock_path)
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ReleaseError("uv.lock does not contain package entries.")
    project_packages = [
        package
        for package in packages
        if isinstance(package, dict)
        and str(package.get("name", "")).replace("_", "-") == "bd-to-avp"
        and package.get("source") == {"editable": "."}
    ]
    if len(project_packages) != 1:
        raise ReleaseError("uv.lock must contain exactly one editable bd-to-avp project package.")
    return str(project_packages[0].get("version", ""))


def _yaml_mapping_value(text: str, path: tuple[str, ...], key: str) -> str:
    stack: dict[int, str] = {}
    matches: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(?P<indent> *)(?P<key>[^#][^:]*):(?:[ ]*(?P<value>.*))?$", line)
        if match is None:
            continue
        indentation = len(match.group("indent"))
        for level in [level for level in stack if level >= indentation]:
            del stack[level]
        parent_path = tuple(stack[level] for level in sorted(stack))
        mapping_key = match.group("key").strip()
        raw_value = (match.group("value") or "").strip()
        if parent_path == path and mapping_key == key and raw_value:
            matches.append(raw_value.strip("\"'"))
        if not raw_value:
            stack[indentation] = mapping_key
    if len(matches) != 1:
        joined_path = ".".join((*path, key))
        raise ReleaseError(f"Expected exactly one {joined_path} entry in the macOS project; found {len(matches)}.")
    return matches[0]


def _replace_yaml_mapping_value(text: str, path: tuple[str, ...], key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    stack: dict[int, str] = {}
    matches: list[int] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<indent> *)(?P<key>[^#][^:]*):(?:[ ]*(?P<value>.*?))?(?P<newline>\n?)$", line)
        if match is None:
            continue
        indentation = len(match.group("indent"))
        for level in [level for level in stack if level >= indentation]:
            del stack[level]
        parent_path = tuple(stack[level] for level in sorted(stack))
        mapping_key = match.group("key").strip()
        raw_value = (match.group("value") or "").strip()
        if parent_path == path and mapping_key == key and raw_value:
            matches.append(index)
        if not raw_value:
            stack[indentation] = mapping_key
    if len(matches) != 1:
        joined_path = ".".join((*path, key))
        raise ReleaseError(f"Expected exactly one {joined_path} entry in the macOS project; found {len(matches)}.")
    index = matches[0]
    prefix = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{prefix}{key}: {value}{newline}"
    return "".join(lines)


def _validate_macos_project_metadata(path: Path, pyproject: dict[str, Any], version: str, build: str) -> None:
    try:
        project_text = path.read_text(encoding="utf-8")
        briefcase = pyproject["tool"]["briefcase"]
        app = briefcase["app"]["bd-to-avp"]
    except (OSError, KeyError, TypeError) as error:
        raise ReleaseError(f"Unable to load production macOS project metadata: {error}") from error
    base_path = ("targets", "BluRayToVisionPro", "settings", "base")
    release_path = ("targets", "BluRayToVisionPro", "settings", "configs", "Release")
    expected_values = {
        "CURRENT_PROJECT_VERSION": build,
        "MARKETING_VERSION": version,
        "PRODUCT_BUNDLE_IDENTIFIER": f"{briefcase['bundle']}.bd-to-avp",
        "PRODUCT_NAME": str(app["formal_name"]),
    }
    mismatches = [
        f"{key}: expected {expected!r}, found {actual!r}"
        for key, expected in expected_values.items()
        if (actual := _yaml_mapping_value(project_text, base_path, key)) != expected
    ]
    release_plist = _yaml_mapping_value(project_text, release_path, "INFOPLIST_FILE")
    if release_plist != "BluRayToVisionPro/Info-Release.plist":
        mismatches.append(f"INFOPLIST_FILE: expected 'BluRayToVisionPro/Info-Release.plist', found {release_plist!r}")
    if mismatches:
        raise ReleaseError("Production macOS project metadata is inconsistent:\n" + "\n".join(mismatches))


def load_release_metadata(
    pyproject_path: Path = PYPROJECT_PATH,
    lock_path: Path = LOCK_PATH,
    macos_project_path: Path | None = None,
) -> ReleaseMetadata:
    pyproject = _load_toml(pyproject_path)
    try:
        project = pyproject["project"]
        briefcase = pyproject["tool"]["briefcase"]
        info = briefcase["app"]["bd-to-avp"]["macOS"]["info"]
    except (KeyError, TypeError) as error:
        raise ReleaseError("pyproject.toml is missing required project or Briefcase release metadata.") from error
    if not isinstance(project, dict) or not isinstance(briefcase, dict) or not isinstance(info, dict):
        raise ReleaseError("Project and Briefcase release metadata must be TOML tables.")
    if "version" in briefcase:
        raise ReleaseError("Remove duplicate [tool.briefcase].version; Briefcase must inherit [project].version.")

    version = parse_release_version(str(project.get("version", "")))
    _validate_production_version(version)
    build_version = str(info.get("CFBundleVersion", ""))
    parse_build_version(build_version)
    locked_version = _locked_project_version(lock_path)
    if locked_version != version.text:
        raise ReleaseError(
            f"uv.lock project version {locked_version!r} does not match [project].version {version.text!r}."
        )
    if macos_project_path is None and pyproject_path == PYPROJECT_PATH and lock_path == LOCK_PATH:
        macos_project_path = MACOS_PROJECT_PATH
    if macos_project_path is not None:
        _validate_macos_project_metadata(macos_project_path, pyproject, version.text, build_version)

    return ReleaseMetadata(
        package_version=version.text,
        public_version=version.public_version,
        build_version=build_version,
        release_tag=version.release_tag,
        release_name=version.release_tag,
        dmg_name=f"{DMG_NAME_PREFIX}-{version.public_version}.dmg",
        channel=version.channel,
        prerelease=version.prerelease,
        make_latest=not version.prerelease,
        publish_pypi=not version.prerelease,
    )


def _replace_section_value(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    section_header = f"[{section}]"
    in_section = False
    matches: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == section_header
            continue
        if in_section and re.match(rf"^{re.escape(key)}\s*=", stripped):
            matches.append(index)
    if len(matches) != 1:
        raise ReleaseError(f"Expected exactly one {key} entry in {section_header}; found {len(matches)}.")
    index = matches[0]
    newline = "\n" if lines[index].endswith("\n") else ""
    indentation = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    lines[index] = f'{indentation}{key} = "{value}"{newline}'
    return "".join(lines)


def refresh_uv_lock(stage_root: Path, uv_executable: str) -> None:
    subprocess.run(
        [uv_executable, "lock", "--project", str(stage_root)],
        check=True,
        cwd=stage_root,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolve_macos_project_path(
    pyproject_path: Path,
    lock_path: Path,
    macos_project_path: Path | None,
) -> Path | None:
    if macos_project_path is None and pyproject_path == PYPROJECT_PATH and lock_path == LOCK_PATH:
        return MACOS_PROJECT_PATH
    return macos_project_path


def _write_release_metadata(
    next_version: ReleaseVersion,
    next_build: int,
    current: ReleaseMetadata,
    *,
    pyproject_path: Path,
    lock_path: Path,
    macos_project_path: Path | None,
    uv_executable: str,
    lock_runner: LockRunner,
) -> ReleaseMetadata:
    original_pyproject = pyproject_path.read_bytes()
    original_lock = lock_path.read_bytes()
    original_macos_project = macos_project_path.read_bytes() if macos_project_path is not None else None
    if load_release_metadata(pyproject_path, lock_path, macos_project_path) != current:
        raise ReleaseError("Release files changed before release preparation; no updates were applied.")

    updated_pyproject = _replace_section_value(
        original_pyproject.decode("utf-8"),
        "project",
        "version",
        next_version.text,
    )
    updated_pyproject = _replace_section_value(
        updated_pyproject,
        "tool.briefcase.app.bd-to-avp.macOS.info",
        "CFBundleVersion",
        str(next_build),
    )
    updated_macos_project: str | None = None
    if original_macos_project is not None:
        updated_macos_project = _replace_yaml_mapping_value(
            original_macos_project.decode("utf-8"),
            ("targets", "BluRayToVisionPro", "settings", "base"),
            "MARKETING_VERSION",
            next_version.text,
        )
        updated_macos_project = _replace_yaml_mapping_value(
            updated_macos_project,
            ("targets", "BluRayToVisionPro", "settings", "base"),
            "CURRENT_PROJECT_VERSION",
            str(next_build),
        )

    with tempfile.TemporaryDirectory(prefix="release-prep-", dir=pyproject_path.parent) as temporary_dir:
        stage_root = Path(temporary_dir)
        staged_pyproject = stage_root / "pyproject.toml"
        staged_lock = stage_root / "uv.lock"
        staged_macos_project = stage_root / "project.yml"
        staged_pyproject.write_text(updated_pyproject, encoding="utf-8")
        staged_lock.write_bytes(original_lock)
        if updated_macos_project is not None:
            staged_macos_project.write_text(updated_macos_project, encoding="utf-8")
        for filename in ("README.md", "LICENSE"):
            source = pyproject_path.parent / filename
            if source.is_file():
                shutil.copy2(source, stage_root / filename)
        lock_runner(stage_root, uv_executable)
        staged_metadata = load_release_metadata(
            staged_pyproject,
            staged_lock,
            staged_macos_project if updated_macos_project is not None else None,
        )
        if staged_metadata.package_version != next_version.text or staged_metadata.build_version != str(next_build):
            raise ReleaseError("Staged release metadata does not match the requested version and build.")
        staged_lock_content = staged_lock.read_bytes()

    if pyproject_path.read_bytes() != original_pyproject or lock_path.read_bytes() != original_lock:
        raise ReleaseError("Release files changed while release preparation was running; no updates were applied.")
    if macos_project_path is not None and macos_project_path.read_bytes() != original_macos_project:
        raise ReleaseError("Release files changed while release preparation was running; no updates were applied.")
    try:
        _atomic_write(pyproject_path, updated_pyproject.encode("utf-8"))
        _atomic_write(lock_path, staged_lock_content)
        if macos_project_path is not None and updated_macos_project is not None:
            _atomic_write(macos_project_path, updated_macos_project.encode("utf-8"))
        metadata = load_release_metadata(pyproject_path, lock_path, macos_project_path)
    except Exception:
        _atomic_write(pyproject_path, original_pyproject)
        _atomic_write(lock_path, original_lock)
        if macos_project_path is not None and original_macos_project is not None:
            _atomic_write(macos_project_path, original_macos_project)
        raise
    return metadata


def prepare_release(
    version_value: str,
    build_value: str,
    *,
    pyproject_path: Path = PYPROJECT_PATH,
    lock_path: Path = LOCK_PATH,
    macos_project_path: Path | None = None,
    uv_executable: str = "uv",
    lock_runner: LockRunner = refresh_uv_lock,
) -> ReleaseMetadata:
    macos_project_path = _resolve_macos_project_path(pyproject_path, lock_path, macos_project_path)
    current = load_release_metadata(pyproject_path, lock_path, macos_project_path)
    current_version = parse_release_version(current.package_version)
    next_version = parse_release_version(version_value)
    _validate_production_version(next_version)
    current_build = parse_build_version(current.build_version)
    next_build = parse_build_version(build_value)
    if next_version.order_key <= current_version.order_key:
        raise ReleaseError(f"Release version {version_value} must be newer than {current.package_version}.")
    if next_build <= current_build:
        raise ReleaseError(f"CFBundleVersion {build_value} must be greater than {current.build_version}.")
    return _write_release_metadata(
        next_version,
        next_build,
        current,
        pyproject_path=pyproject_path,
        lock_path=lock_path,
        macos_project_path=macos_project_path,
        uv_executable=uv_executable,
        lock_runner=lock_runner,
    )


def recover_beta3(
    *,
    pyproject_path: Path = PYPROJECT_PATH,
    lock_path: Path = LOCK_PATH,
    macos_project_path: Path | None = None,
    evidence_path: Path = BETA3_RECOVERY_EVIDENCE_PATH,
    uv_executable: str = "uv",
    lock_runner: LockRunner = refresh_uv_lock,
) -> ReleaseMetadata:
    validate_beta3_recovery_evidence(evidence_path)
    macos_project_path = _resolve_macos_project_path(pyproject_path, lock_path, macos_project_path)
    current = load_release_metadata(pyproject_path, lock_path, macos_project_path)
    if current.package_version != BETA3_RECOVERY_SOURCE_VERSION or current.build_version != BETA3_RECOVERY_SOURCE_BUILD:
        raise ReleaseError(
            "Beta 3 recovery requires exact source metadata "
            f"{BETA3_RECOVERY_SOURCE_VERSION} build {BETA3_RECOVERY_SOURCE_BUILD}; "
            f"found {current.package_version} build {current.build_version}."
        )
    target_version = parse_release_version(BETA3_RECOVERY_TARGET_VERSION)
    target_build = parse_build_version(BETA3_RECOVERY_TARGET_BUILD)
    return _write_release_metadata(
        target_version,
        target_build,
        current,
        pyproject_path=pyproject_path,
        lock_path=lock_path,
        macos_project_path=macos_project_path,
        uv_executable=uv_executable,
        lock_runner=lock_runner,
    )


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT_PATH)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--macos-project", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and prepare BD_to_AVP release metadata.")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Validate committed release version, build, and lock metadata.")
    _add_paths(validate)

    metadata = commands.add_parser("metadata", help="Emit derived GitHub release metadata.")
    _add_paths(metadata)
    metadata.add_argument("--github-output", type=Path)

    notes_base = commands.add_parser(
        "notes-base",
        help="Select the channel-aware base tag for generated GitHub release notes.",
    )
    notes_base.add_argument("--release-tag", required=True)
    notes_base.add_argument("--releases-json", type=Path, required=True)
    notes_base.add_argument("--head-ref", required=True)
    notes_base.add_argument("--github-output", type=Path)

    prepare = commands.add_parser("prepare", help="Atomically prepare a newer committed release version and build.")
    _add_paths(prepare)
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--build", required=True)
    prepare.add_argument("--uv", default="uv")

    recover_beta3_parser = commands.add_parser(
        "recover-beta3",
        help="Apply the audited one-time 0.3.0rc1 build 147 to 0.3.0b3 build 148 recovery.",
    )
    _add_paths(recover_beta3_parser)
    recover_beta3_parser.add_argument("--uv", default="uv")

    validate_tag = commands.add_parser("validate-tag", help="Validate a v-prefixed release tag.")
    validate_tag.add_argument("tag")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-tag":
        print(parse_release_tag(args.tag).text)
        return 0
    if args.command == "notes-base":
        try:
            release_history = json.loads(args.releases_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReleaseError(f"Unable to load GitHub release history from {args.releases_json}: {error}") from error
        previous_release_tag = select_release_notes_base(
            args.release_tag,
            release_history,
            args.head_ref,
        )
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write(f"previous_release_tag={previous_release_tag}\n")
        print(json.dumps({"previous_release_tag": previous_release_tag}, sort_keys=True))
        return 0
    if args.command == "prepare":
        metadata = prepare_release(
            args.version,
            args.build,
            pyproject_path=args.pyproject,
            lock_path=args.lock,
            macos_project_path=args.macos_project,
            uv_executable=args.uv,
        )
    elif args.command == "recover-beta3":
        metadata = recover_beta3(
            pyproject_path=args.pyproject,
            lock_path=args.lock,
            macos_project_path=args.macos_project,
            uv_executable=args.uv,
        )
    else:
        metadata = load_release_metadata(args.pyproject, args.lock, args.macos_project)
    if args.command == "metadata" and args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in metadata.github_outputs().items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(asdict(metadata), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
