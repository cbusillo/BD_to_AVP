from __future__ import annotations

import argparse
import base64
import binascii
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from scripts.beta3_recovery_evidence import (
    BETA3_RECOVERY_EVIDENCE_PATH,
    Beta3RecoveryEvidenceError,
    load_beta3_recovery_evidence,
    verify_beta3_remote_state,
)
from scripts.production_identity import (
    PRODUCTION_BUNDLE_IDENTIFIER,
    PRODUCTION_DISTRIBUTION_CHANNEL,
    PRODUCTION_FEED_URL,
    PRODUCTION_PRODUCT_NAME,
    PRODUCTION_SPARKLE_PUBLIC_KEY,
    validate_production_public_key,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
LOCK_PATH = REPO_ROOT / "uv.lock"
MACOS_PROJECT_PATH = REPO_ROOT / "macos" / "project.yml"
PUBLIC_KEY_PATH = REPO_ROOT / "sparkle-public-ed-key.txt"
TRANSACTION_JOURNAL_NAME = ".bd-to-avp-release-transaction.json"
TRANSACTION_SCHEMA = "bd_to_avp.release_metadata_transaction"
TRANSACTION_SCHEMA_VERSION = 1
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
RemoteEvidenceVerifier = Callable[[Mapping[str, Any]], None]
TransactionObserver = Callable[[str, Path], None]
PrecommitValidator = Callable[[], None]
TagExists = Callable[[str], bool]
AncestorCheck = Callable[[str, str], bool]


@dataclass(frozen=True)
class TransactionFile:
    path: Path
    original: bytes
    target: bytes


@dataclass(frozen=True)
class JournalFile:
    path: Path
    original: bytes
    target_sha256: str


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


def validate_beta3_recovery_evidence(
    evidence_path: Path = BETA3_RECOVERY_EVIDENCE_PATH,
) -> dict[str, Any]:
    try:
        return load_beta3_recovery_evidence(evidence_path)
    except Beta3RecoveryEvidenceError as error:
        raise ReleaseError(str(error)) from error


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


def _validate_beta3_recovery_source_identity(
    pyproject_path: Path,
    lock_path: Path,
    macos_project_path: Path,
    evidence: Mapping[str, Any],
) -> dict[Path, str] | None:
    operated_paths = (pyproject_path, lock_path, macos_project_path)
    for path in operated_paths:
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"Beta 3 recovery requires a regular non-symlink source file: {path}")
    pyproject = _load_toml(pyproject_path)
    try:
        project = pyproject["project"]
        briefcase = pyproject["tool"]["briefcase"]
        app = briefcase["app"]["bd-to-avp"]
        info = app["macOS"]["info"]
    except (KeyError, TypeError) as error:
        raise ReleaseError("Beta 3 recovery source is missing production identity metadata.") from error
    expected_values = {
        "project.name": (project.get("name"), "bd_to_avp"),
        "tool.briefcase.project_name": (briefcase.get("project_name"), PRODUCTION_PRODUCT_NAME),
        "tool.briefcase.bundle": (briefcase.get("bundle"), "com.shinycomputers"),
        "tool.briefcase.app.bd-to-avp.formal_name": (app.get("formal_name"), PRODUCTION_PRODUCT_NAME),
        "BDToAVPDistributionChannel": (
            info.get("BDToAVPDistributionChannel"),
            PRODUCTION_DISTRIBUTION_CHANNEL,
        ),
        "SUFeedURL": (info.get("SUFeedURL"), PRODUCTION_FEED_URL),
        "SUPublicEDKey": (info.get("SUPublicEDKey"), PRODUCTION_SPARKLE_PUBLIC_KEY),
        "SUAllowsAutomaticUpdates": (info.get("SUAllowsAutomaticUpdates"), False),
        "SUVerifyUpdateBeforeExtraction": (info.get("SUVerifyUpdateBeforeExtraction"), True),
    }
    mismatches = [
        f"{name}: expected {expected!r}, found {actual!r}"
        for name, (actual, expected) in expected_values.items()
        if actual != expected
    ]
    if "SUEnableAutomaticChecks" in info:
        mismatches.append("SUEnableAutomaticChecks must remain unset")
    public_key_path = pyproject_path.parent / PUBLIC_KEY_PATH.name
    try:
        public_key = public_key_path.read_text(encoding="utf-8").strip()
        validate_production_public_key(public_key)
    except (OSError, ValueError) as error:
        mismatches.append(f"production Sparkle public key: {error}")
    try:
        project_text = macos_project_path.read_text(encoding="utf-8")
        base_path = ("targets", "BluRayToVisionPro", "settings", "base")
        release_path = ("targets", "BluRayToVisionPro", "settings", "configs", "Release")
        project_expectations = {
            "PRODUCT_BUNDLE_IDENTIFIER": PRODUCTION_BUNDLE_IDENTIFIER,
            "PRODUCT_NAME": PRODUCTION_PRODUCT_NAME,
            "BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT": "",
        }
        for key, expected in project_expectations.items():
            actual = _yaml_mapping_value(project_text, base_path, key)
            if actual != expected:
                mismatches.append(f"{key}: expected {expected!r}, found {actual!r}")
        expected_release_plist = "BluRayToVisionPro/Info-Release.plist"
        release_plist = _yaml_mapping_value(project_text, release_path, "INFOPLIST_FILE")
        if release_plist != expected_release_plist:
            mismatches.append(f"INFOPLIST_FILE: expected {expected_release_plist!r}, found {release_plist!r}")
    except (OSError, UnicodeDecodeError, ReleaseError) as error:
        mismatches.append(f"production macOS project identity: {error}")
    canonical_paths = tuple(path.resolve() for path in operated_paths) == (
        PYPROJECT_PATH.resolve(),
        LOCK_PATH.resolve(),
        MACOS_PROJECT_PATH.resolve(),
    )
    expected_original_digests: dict[Path, str] | None = None
    if canonical_paths:
        source_identity = evidence.get("source_identity")
        if not isinstance(source_identity, Mapping):
            mismatches.append("reviewed source identity is missing")
        else:
            source_files = source_identity.get("files")
            if not isinstance(source_files, Mapping):
                mismatches.append("reviewed source file digests are missing")
            else:
                expected_original_digests = {}
                for relative_path, path in (
                    ("pyproject.toml", pyproject_path),
                    ("uv.lock", lock_path),
                    ("macos/project.yml", macos_project_path),
                ):
                    expected_digest = source_files.get(relative_path)
                    actual_digest = _sha256(path.read_bytes())
                    if expected_digest != actual_digest:
                        mismatches.append(
                            f"{relative_path} source digest: expected {expected_digest!r}, found {actual_digest!r}"
                        )
                    if isinstance(expected_digest, str):
                        expected_original_digests[path.resolve()] = expected_digest
            base_commit = source_identity.get("base_commit")
            expected_tree = source_identity.get("tree")
            try:
                repository_root = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    check=True,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                origin_url = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    check=True,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                actual_tree = subprocess.run(
                    ["git", "show", "-s", "--format=%T", str(base_commit)],
                    check=True,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", str(base_commit), "HEAD"],
                    check=True,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                )
                source_status = subprocess.run(
                    [
                        "git",
                        "status",
                        "--porcelain",
                        "--",
                        "pyproject.toml",
                        "uv.lock",
                        "macos/project.yml",
                        "sparkle-public-ed-key.txt",
                    ],
                    check=True,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                ).stdout
            except (OSError, subprocess.CalledProcessError) as error:
                mismatches.append(f"reviewed Git source identity: {error}")
            else:
                normalized_origin = origin_url.removesuffix(".git").rstrip("/")
                if Path(repository_root).resolve() != REPO_ROOT.resolve():
                    mismatches.append(f"Git top-level must be {REPO_ROOT}, found {repository_root!r}")
                if normalized_origin != "https://github.com/cbusillo/BD_to_AVP":
                    mismatches.append(f"origin must be the production GitHub repository, found {origin_url!r}")
                if actual_tree != expected_tree:
                    mismatches.append(f"reviewed source tree: expected {expected_tree!r}, found {actual_tree!r}")
                if source_status:
                    mismatches.append("reviewed source files must be clean before recovery")
    if mismatches:
        raise ReleaseError("Beta 3 recovery source identity is invalid:\n" + "\n".join(mismatches))
    if expected_original_digests is None:
        expected_original_digests = {path.resolve(): _sha256(path.read_bytes()) for path in operated_paths}
    return expected_original_digests


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


def _load_toml_bytes(content: bytes, description: str) -> dict[str, Any]:
    try:
        return tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"Unable to load {description}: {error}") from error


def _editable_project_package(lock: dict[str, Any], description: str) -> dict[str, Any]:
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ReleaseError(f"{description} does not contain package entries.")
    matches = [
        package
        for package in packages
        if isinstance(package, dict)
        and str(package.get("name", "")).replace("_", "-") == "bd-to-avp"
        and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ReleaseError(f"{description} must contain exactly one editable bd-to-avp package.")
    return matches[0]


def _validate_lock_refresh(
    original_content: bytes,
    staged_content: bytes,
    *,
    current_version: str,
    next_version: str,
) -> None:
    original = _load_toml_bytes(original_content, "original uv.lock")
    staged = _load_toml_bytes(staged_content, "staged uv.lock")
    original_package = _editable_project_package(original, "Original uv.lock")
    staged_package = _editable_project_package(staged, "Staged uv.lock")
    if original_package.get("version") != current_version:
        raise ReleaseError("Original uv.lock project version changed before release preparation.")
    if staged_package.get("version") != next_version:
        raise ReleaseError("Staged uv.lock project version does not match the requested release version.")
    normalized_staged = copy.deepcopy(staged)
    _editable_project_package(normalized_staged, "Staged uv.lock")["version"] = current_version
    if normalized_staged != original:
        raise ReleaseError("uv lock refresh changed data other than the editable project version.")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _remove_file_durably(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _transaction_journal_path(pyproject_path: Path) -> Path:
    return pyproject_path.parent / TRANSACTION_JOURNAL_NAME


def _release_lock_path(pyproject_path: Path) -> Path:
    root_digest = hashlib.sha256(str(pyproject_path.parent.resolve()).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"bd-to-avp-release-{root_digest}.lock"


@contextmanager
def _release_metadata_lock(pyproject_path: Path) -> Iterator[None]:
    lock_path = _release_lock_path(pyproject_path)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError("Another release metadata operation is already running for this checkout.") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _transaction_payload(files: Sequence[TransactionFile], state: str) -> dict[str, Any]:
    return {
        "schema": TRANSACTION_SCHEMA,
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "state": state,
        "files": [
            {
                "path": str(file.path.resolve()),
                "original_sha256": _sha256(file.original),
                "original_base64": base64.b64encode(file.original).decode("ascii"),
                "target_sha256": _sha256(file.target),
            }
            for file in files
        ],
    }


def _write_transaction_journal(path: Path, payload: Mapping[str, Any]) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(path, content)


def _load_transaction_files(path: Path, expected_paths: Sequence[Path]) -> tuple[str, list[JournalFile]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Release metadata transaction journal is invalid: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseError("Release metadata transaction journal must be a JSON object.")
    if payload.get("schema") != TRANSACTION_SCHEMA or payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise ReleaseError("Release metadata transaction journal has an unsupported schema.")
    state = payload.get("state")
    if state not in {"prepared", "committed"}:
        raise ReleaseError("Release metadata transaction journal has an invalid state.")
    entries = payload.get("files")
    if not isinstance(entries, list) or len(entries) != len(expected_paths):
        raise ReleaseError("Release metadata transaction journal has an invalid file set.")
    expected_resolved = [path.resolve() for path in expected_paths]
    files: list[JournalFile] = []
    for entry, expected_path in zip(entries, expected_resolved, strict=True):
        if not isinstance(entry, dict) or entry.get("path") != str(expected_path):
            raise ReleaseError("Release metadata transaction journal path does not match this checkout.")
        try:
            original = base64.b64decode(str(entry["original_base64"]), validate=True)
        except (KeyError, ValueError, binascii.Error) as error:
            raise ReleaseError("Release metadata transaction journal contains invalid backup data.") from error
        if _sha256(original) != entry.get("original_sha256"):
            raise ReleaseError("Release metadata transaction journal backup digest is invalid.")
        target_digest = entry.get("target_sha256")
        if not isinstance(target_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", target_digest):
            raise ReleaseError("Release metadata transaction journal target digest is invalid.")
        files.append(JournalFile(path=expected_path, original=original, target_sha256=target_digest))
    return str(state), files


def _recover_interrupted_transaction(journal_path: Path, expected_paths: Sequence[Path]) -> None:
    if not journal_path.exists():
        return
    state, journal_files = _load_transaction_files(journal_path, expected_paths)
    if state == "committed":
        for file in journal_files:
            if _sha256(file.path.read_bytes()) != file.target_sha256:
                raise ReleaseError("Committed release metadata transaction does not match its target digest.")
        _remove_file_durably(journal_path)
        return
    for file in journal_files:
        current_digest = _sha256(file.path.read_bytes())
        if current_digest not in {_sha256(file.original), file.target_sha256}:
            raise ReleaseError(f"Interrupted release transaction cannot restore externally changed file: {file.path}")
    for file in journal_files:
        _atomic_write(file.path, file.original)
    _remove_file_durably(journal_path)


def _apply_metadata_transaction(
    files: Sequence[TransactionFile],
    journal_path: Path,
    validate: Callable[[], ReleaseMetadata],
    observer: TransactionObserver | None = None,
    precommit_validator: PrecommitValidator | None = None,
) -> ReleaseMetadata:
    expected_paths = [file.path for file in files]
    prepared = _transaction_payload(files, "prepared")
    _write_transaction_journal(journal_path, prepared)
    if observer is not None:
        observer("journal-prepared", journal_path)
    try:
        for file in files:
            if file.path.read_bytes() != file.original:
                raise ReleaseError(f"Release file changed immediately before replacement: {file.path}")
            _atomic_write(file.path, file.target)
            if observer is not None:
                observer("file-applied", file.path)
        metadata = validate()
        if precommit_validator is not None:
            precommit_validator()
        _write_transaction_journal(journal_path, _transaction_payload(files, "committed"))
        if observer is not None:
            observer("journal-committed", journal_path)
        _remove_file_durably(journal_path)
        return metadata
    except BaseException:
        try:
            _recover_interrupted_transaction(journal_path, expected_paths)
        except Exception as rollback_error:
            raise ReleaseError(
                f"Release metadata transaction could not be recovered; journal retained at {journal_path}."
            ) from rollback_error
        raise


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
    transaction_observer: TransactionObserver | None = None,
    precommit_validator: PrecommitValidator | None = None,
    expected_original_digests: Mapping[Path, str] | None = None,
) -> ReleaseMetadata:
    original_pyproject = pyproject_path.read_bytes()
    original_lock = lock_path.read_bytes()
    original_macos_project = macos_project_path.read_bytes() if macos_project_path is not None else None
    original_contents = {
        pyproject_path.resolve(): original_pyproject,
        lock_path.resolve(): original_lock,
    }
    if macos_project_path is not None and original_macos_project is not None:
        original_contents[macos_project_path.resolve()] = original_macos_project
    if expected_original_digests is not None:
        if set(expected_original_digests) != set(original_contents):
            raise ReleaseError("Beta 3 recovery source digest set does not match the operated release files.")
        for path, content in original_contents.items():
            if _sha256(content) != expected_original_digests[path]:
                raise ReleaseError(f"Beta 3 recovery source changed after identity validation: {path}")
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
        _validate_lock_refresh(
            original_lock,
            staged_lock_content,
            current_version=current.package_version,
            next_version=next_version.text,
        )

    if pyproject_path.read_bytes() != original_pyproject or lock_path.read_bytes() != original_lock:
        raise ReleaseError("Release files changed while release preparation was running; no updates were applied.")
    if macos_project_path is not None and macos_project_path.read_bytes() != original_macos_project:
        raise ReleaseError("Release files changed while release preparation was running; no updates were applied.")
    files = [
        TransactionFile(
            path=pyproject_path,
            original=original_pyproject,
            target=updated_pyproject.encode("utf-8"),
        ),
        TransactionFile(path=lock_path, original=original_lock, target=staged_lock_content),
    ]
    if macos_project_path is not None and updated_macos_project is not None and original_macos_project is not None:
        files.append(
            TransactionFile(
                path=macos_project_path,
                original=original_macos_project,
                target=updated_macos_project.encode("utf-8"),
            )
        )
    return _apply_metadata_transaction(
        files,
        _transaction_journal_path(pyproject_path),
        lambda: load_release_metadata(pyproject_path, lock_path, macos_project_path),
        transaction_observer,
        precommit_validator,
    )


def prepare_release(
    version_value: str,
    build_value: str,
    *,
    pyproject_path: Path = PYPROJECT_PATH,
    lock_path: Path = LOCK_PATH,
    macos_project_path: Path | None = None,
    uv_executable: str = "uv",
    lock_runner: LockRunner = refresh_uv_lock,
    transaction_observer: TransactionObserver | None = None,
) -> ReleaseMetadata:
    macos_project_path = _resolve_macos_project_path(pyproject_path, lock_path, macos_project_path)
    expected_paths = [pyproject_path, lock_path, *([macos_project_path] if macos_project_path is not None else [])]
    with _release_metadata_lock(pyproject_path):
        _recover_interrupted_transaction(_transaction_journal_path(pyproject_path), expected_paths)
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
            transaction_observer=transaction_observer,
        )


def recover_beta3(
    *,
    pyproject_path: Path = PYPROJECT_PATH,
    lock_path: Path = LOCK_PATH,
    macos_project_path: Path | None = None,
    evidence_path: Path = BETA3_RECOVERY_EVIDENCE_PATH,
    lock_runner: LockRunner = refresh_uv_lock,
    remote_verifier: RemoteEvidenceVerifier = verify_beta3_remote_state,
    transaction_observer: TransactionObserver | None = None,
) -> ReleaseMetadata:
    evidence = validate_beta3_recovery_evidence(evidence_path)
    macos_project_path = _resolve_macos_project_path(pyproject_path, lock_path, macos_project_path)
    if macos_project_path is None:
        raise ReleaseError("Beta 3 recovery requires the production macOS project metadata.")
    expected_paths = [pyproject_path, lock_path, macos_project_path]
    with _release_metadata_lock(pyproject_path):
        _recover_interrupted_transaction(_transaction_journal_path(pyproject_path), expected_paths)
        expected_original_digests = _validate_beta3_recovery_source_identity(
            pyproject_path,
            lock_path,
            macos_project_path,
            evidence,
        )
        current = load_release_metadata(pyproject_path, lock_path, macos_project_path)
        if (
            current.package_version != BETA3_RECOVERY_SOURCE_VERSION
            or current.build_version != BETA3_RECOVERY_SOURCE_BUILD
        ):
            raise ReleaseError(
                "Beta 3 recovery requires exact source metadata "
                f"{BETA3_RECOVERY_SOURCE_VERSION} build {BETA3_RECOVERY_SOURCE_BUILD}; "
                f"found {current.package_version} build {current.build_version}."
            )

        def verify_remote_premise() -> None:
            try:
                remote_verifier(evidence)
            except Beta3RecoveryEvidenceError as error:
                raise ReleaseError(str(error)) from error

        verify_remote_premise()
        target_version = parse_release_version(BETA3_RECOVERY_TARGET_VERSION)
        target_build = parse_build_version(BETA3_RECOVERY_TARGET_BUILD)
        return _write_release_metadata(
            target_version,
            target_build,
            current,
            pyproject_path=pyproject_path,
            lock_path=lock_path,
            macos_project_path=macos_project_path,
            uv_executable="uv",
            lock_runner=lock_runner,
            transaction_observer=transaction_observer,
            precommit_validator=verify_remote_premise,
            expected_original_digests=expected_original_digests,
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
    recover_beta3_parser.add_argument("--evidence", type=Path, default=BETA3_RECOVERY_EVIDENCE_PATH)

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
        metadata = recover_beta3(evidence_path=args.evidence)
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
