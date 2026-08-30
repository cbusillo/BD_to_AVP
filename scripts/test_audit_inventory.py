from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_JSON = Path("docs/test-audit/inventory-v1.json")
DEFAULT_MARKDOWN = Path("docs/test-audit/inventory-v1.md")
DEFAULT_CLASSIFICATIONS = Path("docs/test-audit/classifications-v1.json")
CLASSIFICATION_SOURCE_SCHEMA_VERSION = 1
VALID_CLASSIFICATIONS = frozenset({"valuable", "accepted-cost", "replace", "consolidate", "remove"})
EXACT_HEAD_EVIDENCE_SHA = "257fd21f38e49031b9fa96a733875702313ebd5c"
TEST_GLOBS = (
    "tests/test_*.py",
    "macos/BluRayToVisionProTests/*.swift",
    "macos/BluRayToVisionProUITests/*.swift",
    "macos/SpatialPlaybackProbeTests/*.swift",
    "macos/SpatialPlaybackProbeUITests/*.swift",
    "macos/BDToAVPPlayerTests/*.swift",
    "support-diagnostics/test/*.test.ts",
)
FIXTURE_GLOBS = ("tests/fixtures/*",)
LANE_SOURCE_PATHS = (
    ".github/workflows/ci.yml",
    "macos/project.yml",
    "docs/tier3-clean-machine.md",
    "docs/visionos-playback-validator.md",
    "docs/tier3-operator-hardware.md",
)
SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("environment_dependent", r"os\.environ|os\.getenv|ProcessInfo\.processInfo\.environment|environment\["),
    ("filesystem_access", r"open\(|Path\(|FileHandle|FileManager|fixtures/|\.json|\.b64"),
    ("external_process", r"subprocess|xcodebuild|xcrun|devicectl|ChildProcess|Process\("),
    ("polling_or_waiting", r"time\.sleep|sleep\(|asyncAfter|poll|waitFor|timeout"),
    ("network_access", r"https?://|URLSession|urllib|requests\.|curl"),
    ("ui_or_accessibility", r"XCUIApplication|XCUITest|accessibility|UI test|InstalledUI"),
    ("hardware_or_media", r"/dev/disk|physical|Blu-ray|RealityDevice|visionOS|AVPlayer|AVAsset"),
    ("skip_or_conditional", r"XCTSkip|skipTest|pytest\.skip|unittest\.skip|expectedFailure"),
)


class InventoryError(RuntimeError):
    pass


def _git_output(root: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise InventoryError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def tracked_paths(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path for path in _git_output(root, ["ls-files", "-z"]).split("\0") if path))


def _read_text(root: Path, relative_path: str) -> str:
    try:
        return (root / relative_path).read_text(encoding="utf-8")
    except OSError as error:
        raise InventoryError(f"Unable to read {relative_path}: {error}") from error


def _root_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _relative_display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def load_classifications(
    root: Path, classification_path: Path, expected_paths: Sequence[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    source_path = _root_path(root, classification_path)
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        display_path = _relative_display_path(root, source_path)
        raise InventoryError(f"Unable to load classifications from {display_path}: {error}") from error
    if not isinstance(source, dict):
        raise InventoryError("Classification source must be a JSON object")
    if source.get("schema_version") != CLASSIFICATION_SOURCE_SCHEMA_VERSION:
        raise InventoryError(f"Classification source schema_version must be {CLASSIFICATION_SOURCE_SCHEMA_VERSION}")
    if source.get("artifact") != "test-audit-classifications":
        raise InventoryError("Classification source artifact must be test-audit-classifications")
    evidence_catalog = source.get("evidence_catalog")
    cohorts = source.get("cohorts")
    candidates = source.get("candidate_summary")
    if not isinstance(evidence_catalog, dict) or not isinstance(cohorts, list) or not isinstance(candidates, dict):
        raise InventoryError("Classification source requires evidence_catalog, cohorts, and candidate_summary objects")
    if not isinstance(candidates.get("high_confidence_candidates"), list) or not isinstance(
        candidates.get("milestone_10_disposition"), dict
    ):
        raise InventoryError("Classification candidate_summary must declare candidates and milestone_10_disposition")

    normalized_evidence: dict[str, dict[str, Any]] = {}
    for evidence_id, evidence in evidence_catalog.items():
        if not isinstance(evidence_id, str) or not isinstance(evidence, dict):
            raise InventoryError("Classification evidence_catalog entries must be keyed objects")
        description = evidence.get("description")
        source_paths = evidence.get("source_paths")
        if (
            not isinstance(description, str)
            or not description
            or not isinstance(source_paths, list)
            or not all(isinstance(source_path, str) and source_path for source_path in source_paths)
        ):
            raise InventoryError(f"Evidence {evidence_id!r} must have description and source_paths")
        normalized_evidence[evidence_id] = {
            "id": evidence_id,
            "description": description,
            "source_paths": source_paths,
        }

    expected = set(expected_paths)
    classifications: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        if not isinstance(cohort, dict):
            raise InventoryError("Classification cohorts must be objects")
        cohort_id = cohort.get("id")
        classification = cohort.get("classification")
        rationale = cohort.get("rationale")
        evidence_ids = cohort.get("evidence_ids")
        paths = cohort.get("paths")
        if not isinstance(cohort_id, str) or not cohort_id:
            raise InventoryError("Classification cohorts require a non-empty id")
        if classification not in VALID_CLASSIFICATIONS:
            raise InventoryError(f"Classification cohort {cohort_id!r} has invalid classification {classification!r}")
        if not isinstance(rationale, str) or not rationale:
            raise InventoryError(f"Classification cohort {cohort_id!r} requires a rationale")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(item, str) for item in evidence_ids)
        ):
            raise InventoryError(f"Classification cohort {cohort_id!r} requires non-empty evidence_ids")
        unknown_evidence = sorted(set(evidence_ids).difference(normalized_evidence))
        if unknown_evidence:
            raise InventoryError(f"Classification cohort {cohort_id!r} references unknown evidence: {unknown_evidence}")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
            raise InventoryError(f"Classification cohort {cohort_id!r} requires non-empty literal paths")
        for path in paths:
            if path in classifications:
                raise InventoryError(f"Classification path is assigned more than once: {path}")
            if path not in expected:
                raise InventoryError(f"Classification source contains unknown path: {path}")
            classifications[path] = {
                "classification": classification,
                "classification_rationale": rationale.format(path=path),
                "classification_evidence_ids": list(evidence_ids),
                "classification_cohort": cohort_id,
            }
    missing_paths = sorted(expected.difference(classifications))
    if missing_paths:
        raise InventoryError(f"Classification source is missing paths: {missing_paths}")
    return classifications, source


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _step_commands(workflow: str) -> tuple[dict[str, str], ...]:
    lines = workflow.splitlines()
    steps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        name_match = re.match(r"^\s{6}- name:\s*(.+?)\s*$", line)
        if name_match:
            if current is not None:
                steps.append(current)
            current = {"name": name_match.group(1).strip()}
            index += 1
            continue
        if current is not None:
            working_directory = re.match(r"^\s{8}working-directory:\s*(.+?)\s*$", line)
            if working_directory:
                current["working_directory"] = working_directory.group(1).strip()
            run_match = re.match(r"^\s{8}run:\s*(.*)\s*$", line)
            if run_match:
                command = run_match.group(1).strip()
                index += 1
                if command in {"|", ">", "|-", ">-", "|+", ">+"}:
                    command_lines: list[str] = []
                    while index < len(lines):
                        continuation = lines[index]
                        if continuation and len(continuation) - len(continuation.lstrip()) <= 8:
                            break
                        command_lines.append(continuation.strip())
                        index += 1
                    command = "\n".join(item for item in command_lines if item)
                current["run"] = command
                continue
        index += 1
    if current is not None:
        steps.append(current)
    return tuple(steps)


def parse_ci_workflow(workflow: str) -> dict[str, Any]:
    runner = _first_match(workflow, r"^\s{4}runs-on:\s*(.+?)\s*$")
    timeout = _first_match(workflow, r"^\s{4}timeout-minutes:\s*(\d+)\s*$")
    commands = []
    for step in _step_commands(workflow):
        command = step.get("run", "")
        if (
            "unittest discover" in command
            or "scripts/native_app.py test" in command
            or "npm run check" in command
            or "xcodebuild build-for-testing" in command
        ):
            commands.append(
                {"name": step["name"], "command": command, "working_directory": step.get("working_directory", ".")}
            )
    return {
        "job": "validate",
        "runner": runner,
        "timeout_minutes": int(timeout) if timeout else None,
        "commands": commands,
    }


def _indented_block(lines: list[str], header_pattern: str, indent: int) -> list[tuple[str, list[str]]]:
    header = re.compile(header_pattern)
    results: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines:
        match = header.match(line)
        if match:
            if current_name is not None:
                results.append((current_name, current_lines))
            current_name = match.group(1)
            current_lines = []
            continue
        if current_name is not None and (not line.strip() or len(line) - len(line.lstrip()) > indent):
            current_lines.append(line)
    if current_name is not None:
        results.append((current_name, current_lines))
    return results


def parse_project_yml(project: str) -> dict[str, Any]:
    lines = project.splitlines()
    targets_start = next((index for index, line in enumerate(lines) if line == "targets:"), None)
    schemes_start = next((index for index, line in enumerate(lines) if line == "schemes:"), None)
    if targets_start is None or schemes_start is None:
        raise InventoryError("macos/project.yml is missing targets or schemes")
    targets: dict[str, dict[str, Any]] = {}
    for name, block in _indented_block(lines[targets_start + 1 : schemes_start], r"^  ([^\s:][^:]*):\s*$", 2):
        block_text = "\n".join(block)
        targets[name] = {
            "type": _first_match(block_text, r"^    type:\s*(.+?)\s*$"),
            "platform": _first_match(block_text, r"^    platform:\s*(.+?)\s*$"),
            "supported_destinations": re.findall(
                r"^    supportedDestinations:\s*\[([^]]+)\]", block_text, re.MULTILINE
            ),
            "sources": [match.strip() for match in re.findall(r"^\s+- path:\s*(.+?)\s*$", block_text, re.MULTILINE)],
        }
    schemes: dict[str, dict[str, Any]] = {}
    for name, block in _indented_block(lines[schemes_start + 1 :], r"^  ([^\s:][^:]*):\s*$", 2):
        block_text = "\n".join(block)
        test_match = re.search(r"^    test:\s*$([\s\S]*)", block_text, re.MULTILINE)
        test_block = test_match.group(1) if test_match else ""
        targets_match = re.search(r"^      targets:\s*$([\s\S]*)", test_block, re.MULTILINE)
        test_targets = (
            re.findall(r"^        - ([^\s]+)\s*$", targets_match.group(1), re.MULTILINE) if targets_match else []
        )
        schemes[name] = {"test_targets": test_targets}
    return {"targets": targets, "schemes": schemes}


def _command_blocks(document: str) -> tuple[str, ...]:
    commands: list[str] = []
    in_block = False
    block: list[str] = []
    for line in document.splitlines():
        if line.strip().startswith("```"):
            if in_block and block:
                commands.append(" ".join(part.strip().rstrip("\\") for part in block if part.strip()))
            in_block = not in_block
            block = []
        elif in_block:
            block.append(line)
    return tuple(command for command in commands if command)


def parse_documented_lanes(documents: dict[str, str]) -> tuple[dict[str, Any], ...]:
    tier3_commands = _command_blocks(documents["docs/tier3-clean-machine.md"])
    visionos_commands = _command_blocks(documents["docs/visionos-playback-validator.md"])
    return (
        {
            "id": "operator.tier3.installed_ui",
            "name": "Tier 3 clean-machine installed UI",
            "kind": "documented_operator",
            "source_paths": ["docs/tier3-clean-machine.md", "macos/project.yml"],
            "commands": [command for command in tier3_commands if "scripts.tier3_clean_machine" in command],
            "requirements": [
                "macOS 26 arm64",
                "Accessibility control",
                "signed installed app",
                "clean-machine qualification inputs",
            ],
            "test_targets": ["BluRayToVisionProUITests"],
        },
        {
            "id": "operator.visionos.playback_probe",
            "name": "visionOS playback validator",
            "kind": "documented_device",
            "source_paths": ["docs/visionos-playback-validator.md", "macos/project.yml"],
            "commands": [command for command in visionos_commands if "xcodebuild test" in command],
            "requirements": [
                "visionOS simulator for automated checks",
                "physical Apple Vision Pro for presentation evidence",
            ],
            "test_targets": ["SpatialPlaybackProbeTests", "SpatialPlaybackProbeUITests"],
        },
    )


def _count_cases(relative_path: str, text: str) -> int:
    if relative_path.endswith(".py"):
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError as error:
            raise InventoryError(f"Unable to parse {relative_path}: {error}") from error
        return sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
            for node in ast.walk(tree)
        )
    if relative_path.endswith(".swift"):
        return len(re.findall(r"\bfunc\s+test[A-Za-z0-9_]*\s*\(", text))
    if relative_path.endswith(".ts"):
        return len(re.findall(r"\b(?:it|test)\s*\(", text))
    return 0


def _signals(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    signals = []
    for signal, pattern in SIGNAL_PATTERNS:
        occurrences = sum(bool(re.search(pattern, line)) for line in lines)
        if occurrences:
            signals.append({"signal": signal, "occurrences": occurrences})
    return signals


def _requirements(relative_path: str, text: str) -> list[str]:
    lowered = f"{relative_path}\n{text}".lower()
    requirements: set[str] = set()
    if "ssif" in lowered or "iso" in lowered:
        requirements.add("real-media or SSIF/ISO fixture may be required")
    if "/dev/disk" in lowered or "physical disc" in lowered:
        requirements.add("physical Blu-ray device")
    if "xcuiapplication" in lowered or "installedui" in lowered or "tier 3" in lowered:
        requirements.add("Tier 3 clean-machine and Accessibility environment")
    if (
        "visionos" in lowered
        or "spatialplayback" in lowered
        or "realitydevice" in lowered
        or "bdtoavpplayer" in lowered
    ):
        requirements.add("visionOS simulator or physical Apple Vision Pro, depending on evidence")
    if "urlsession" in lowered or "http://" in lowered or "https://" in lowered:
        requirements.add("network access")
    if "fixtures/" in lowered:
        requirements.add("repository fixture files")
    return sorted(requirements)


def _bundle_for_path(relative_path: str, project: dict[str, Any]) -> str:
    if relative_path.startswith("tests/"):
        return "python-unittest-discovery"
    if relative_path.startswith("support-diagnostics/"):
        return "support-diagnostics-vitest"
    for target_name, target in project["targets"].items():
        if any(relative_path.startswith(f"macos/{source}/") for source in target["sources"]):
            return target_name
    return "unmapped"


def _lane_ids(relative_path: str, bundle: str) -> list[str]:
    if relative_path.startswith("tests/"):
        return ["ci.python.unittest"]
    if relative_path.startswith("support-diagnostics/"):
        return ["ci.support_diagnostics.vitest"]
    if bundle == "BluRayToVisionProTests":
        return ["ci.macos.blu_ray_unit"]
    if bundle == "BluRayToVisionProUITests":
        return ["operator.tier3.installed_ui"]
    if bundle in {"SpatialPlaybackProbeTests", "SpatialPlaybackProbeUITests"}:
        return ["operator.visionos.playback_probe"]
    if bundle == "BDToAVPPlayerTests":
        return ["ci.visionos.bd_to_avp_player"]
    return []


def _test_rows(
    root: Path, paths: Iterable[str], project: dict[str, Any], classifications: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for relative_path in sorted(paths):
        text = _read_text(root, relative_path)
        bundle = _bundle_for_path(relative_path, project)
        rows.append(
            {
                "path": relative_path,
                "language": {".py": "Python", ".swift": "Swift", ".ts": "TypeScript"}[Path(relative_path).suffix],
                "bundle": bundle,
                "lane_ids": _lane_ids(relative_path, bundle),
                "test_case_count": _count_cases(relative_path, text),
                **classifications[relative_path],
                "special_requirements": _requirements(relative_path, text),
                "static_brittleness_signals": _signals(text),
            }
        )
    return rows


def _fixture_rows(paths: Iterable[str], classifications: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": relative_path,
            "kind": "support_fixture",
            "format": Path(relative_path).suffix.removeprefix(".") or "unknown",
            **classifications[relative_path],
        }
        for relative_path in sorted(paths)
    ]


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(Path(path).match(pattern) for pattern in patterns)


def _lane_definitions(
    ci: dict[str, Any], project: dict[str, Any], documented: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    commands = [command["command"] for command in ci["commands"]]
    runner = ci["runner"] or "unknown"
    lanes: list[dict[str, Any]] = []
    if any("unittest discover" in command for command in commands):
        lanes.append(
            {
                "id": "ci.python.unittest",
                "name": "CI Python unit discovery",
                "kind": "ci",
                "maintained": True,
                "source_paths": [".github/workflows/ci.yml"],
                "runner": runner,
                "authoritative_command": next(command for command in commands if "unittest discover" in command),
                "test_roots": ["tests/test_*.py"],
                "requirements": ["Python 3.12", "uv environment"],
            }
        )
    macos_scheme = project["schemes"].get("BluRayToVisionPro", {})
    if any(
        "scripts/native_app.py test" in command for command in commands
    ) and "BluRayToVisionProTests" in macos_scheme.get("test_targets", []):
        lanes.append(
            {
                "id": "ci.macos.blu_ray_unit",
                "name": "CI macOS application unit tests",
                "kind": "ci",
                "maintained": True,
                "source_paths": [".github/workflows/ci.yml", "scripts/native_app.py", "macos/project.yml"],
                "runner": runner,
                "authoritative_command": next(
                    command for command in commands if "scripts/native_app.py test" in command
                ),
                "underlying_scheme": "BluRayToVisionPro",
                "test_targets": macos_scheme["test_targets"],
                "requirements": ["Xcode 26.5", "macOS 26"],
            }
        )
    player_scheme = project["schemes"].get("BDToAVPPlayer", {})
    player_command = next(
        (
            command
            for command in commands
            if "xcodebuild build-for-testing" in command
            and "BDToAVPPlayer" in command
            and "generic/platform=visionOS Simulator" in command
            and "CODE_SIGNING_ALLOWED=NO" in command
            and 'DEVELOPER_DIR="/Applications/Xcode_26.5.app/Contents/Developer"' in command
            and "xcodebuild -version" in command
            and '"Xcode 26.5"' in command
        ),
        None,
    )
    if player_command and "BDToAVPPlayerTests" in player_scheme.get("test_targets", []):
        lanes.append(
            {
                "id": "ci.visionos.bd_to_avp_player",
                "name": "CI visionOS BDToAVPPlayer unit tests",
                "kind": "ci",
                "maintained": True,
                "source_paths": [".github/workflows/ci.yml", "macos/project.yml"],
                "runner": runner,
                "authoritative_command": player_command,
                "underlying_scheme": "BDToAVPPlayer",
                "test_targets": player_scheme["test_targets"],
                "requirements": [
                    "Xcode 26.5",
                    "generic visionOS Simulator build destination",
                    "available visionOS runtime and Apple Vision Pro simulator device type for unit execution",
                    "CODE_SIGNING_ALLOWED=NO",
                ],
            }
        )
    if any("npm run check" in command for command in commands):
        lanes.append(
            {
                "id": "ci.support_diagnostics.vitest",
                "name": "CI support diagnostics checks",
                "kind": "ci",
                "maintained": True,
                "source_paths": [".github/workflows/ci.yml"],
                "runner": runner,
                "authoritative_command": next(command for command in commands if "npm run check" in command),
                "working_directory": "support-diagnostics",
                "requirements": ["Node.js 24", "npm dependencies"],
            }
        )
    lanes.extend(documented)
    return sorted(lanes, key=lambda lane: lane["id"])


def build_inventory(
    root: Path,
    *,
    baseline_sha: str,
    paths: Sequence[str] | None = None,
    classifications_path: Path = DEFAULT_CLASSIFICATIONS,
) -> dict[str, Any]:
    tracked = tuple(paths) if paths is not None else tracked_paths(root)
    test_paths = [path for path in tracked if _matches_any(path, TEST_GLOBS)]
    fixture_paths = [path for path in tracked if _matches_any(path, FIXTURE_GLOBS)]
    ci = parse_ci_workflow(_read_text(root, ".github/workflows/ci.yml"))
    project = parse_project_yml(_read_text(root, "macos/project.yml"))
    documents = {path: _read_text(root, path) for path in LANE_SOURCE_PATHS if (root / path).exists()}
    documented = parse_documented_lanes(documents)
    lanes = _lane_definitions(ci, project, documented)
    classifications, classification_source = load_classifications(
        root, classifications_path, [*test_paths, *fixture_paths]
    )
    test_files = _test_rows(root, test_paths, project, classifications)
    fixtures = _fixture_rows(fixture_paths, classifications)
    defined_lane_ids = {lane["id"] for lane in lanes}
    referenced_lane_ids = {lane_id for row in test_files for lane_id in row["lane_ids"]}
    undefined_lane_ids = sorted(referenced_lane_ids - defined_lane_ids)
    if undefined_lane_ids:
        raise InventoryError(f"test files reference undefined lanes: {', '.join(undefined_lane_ids)}")
    not_in_ci_lanes = [
        {
            "lane_id": lane["id"],
            "reason": (
                "Configured test target is not part of the maintained CI validate job; "
                "coverage is documented as operator/device evidence."
            ),
            "test_targets": lane.get("test_targets", []),
            "test_case_count": sum(row["test_case_count"] for row in test_files if lane["id"] in row["lane_ids"]),
        }
        for lane in lanes
        if lane["id"] in {"operator.tier3.installed_ui", "operator.visionos.playback_probe"}
    ]
    unmaintained = [row["path"] for row in test_files if not row["lane_ids"]]
    total_cases = sum(row["test_case_count"] for row in test_files)
    ci_cases = sum(
        row["test_case_count"] for row in test_files if any(lane_id.startswith("ci.") for lane_id in row["lane_ids"])
    )
    authoritative_commands = [
        "uv run python -m unittest discover -s tests -t .",
        "uv run python scripts/native_app.py test",
        "(cd support-diagnostics && npm run check)",
    ]
    player_lane = next((lane for lane in lanes if lane["id"] == "ci.visionos.bd_to_avp_player"), None)
    if player_lane:
        authoritative_commands.append(player_lane["authoritative_command"])
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": "test-audit-inventory",
        "baseline": {
            "sha": baseline_sha,
            "repository": "cbusillo/BD_to_AVP",
            "source_snapshot": "tracked repository files at generation time",
        },
        "source_of_truth": {
            "tracked_files_command": "git ls-files -z",
            "lane_source_paths": list(LANE_SOURCE_PATHS),
            "test_globs": list(TEST_GLOBS),
            "fixture_globs": list(FIXTURE_GLOBS),
            "classification_source": _relative_display_path(root, _root_path(root, classifications_path)),
        },
        "execution_evidence": {
            "deterministic": False,
            "status": "point-in-time execution evidence; excluded from deterministic inventory checks",
            "local": {
                "captured_from_sha": "b4f980642c6e36140f458af3f3eaffafc9ae14fa",
                "platform": {"macos_version": "27.0", "build": "26A5421a", "architecture": "arm64"},
                "toolchain": {
                    "xcode": {"version": "27.0", "build": "27A5194q"},
                    "python": "3.12.11",
                    "uv": "0.12.3",
                    "node": "26.7.0",
                    "npm": "11.19.0",
                },
                "lanes": [
                    {
                        "id": "ci.python.unittest",
                        "status": "passed",
                        "test_count": 1966,
                        "skipped_count": 3,
                        "test_duration_seconds": 235.950,
                        "wall_duration_seconds": 241.08,
                    },
                    {
                        "id": "ci.macos.blu_ray_unit",
                        "status": "passed",
                        "test_count": 567,
                        "test_duration_seconds": 25.010,
                        "wall_duration_seconds": 52.17,
                    },
                    {
                        "id": "ci.support_diagnostics.vitest",
                        "status": "passed",
                        "test_count": 23,
                        "test_duration_milliseconds": 164,
                        "wall_duration_seconds": 2.19,
                    },
                ],
                "excluded_identity_fields": ["hostname", "username", "private paths", "credentials", "device IDs"],
            },
            "exact_head": {
                "captured_from_sha": EXACT_HEAD_EVIDENCE_SHA,
                "lanes": [
                    {
                        "id": "ci.python.unittest",
                        "status": "passed",
                        "test_count": 1973,
                        "skipped_count": 3,
                        "test_duration_seconds": 154.497,
                        "wall_duration_seconds": 156.10,
                    },
                    {
                        "id": "ci.macos.blu_ray_unit",
                        "status": "passed",
                        "test_count": 567,
                        "test_duration_seconds": 21.512,
                        "wall_duration_seconds": 46.10,
                    },
                    {
                        "id": "ci.support_diagnostics.vitest",
                        "status": "passed",
                        "test_count": 23,
                        "test_duration_milliseconds": 126,
                        "wall_duration_seconds": 1.20,
                    },
                ],
                "python_module_isolation": {
                    "status": "passed",
                    "module_count": 106,
                    "test_count": 1971,
                    "skipped_count": 3,
                    "wall_duration_seconds": 163.20,
                    "resolved_candidate": "process-runner-isolated-import",
                },
            },
            "ci": {
                "runner_label": ci["runner"],
                "run_id": "33264262845",
                "status": "success",
                "evidence": "matching main CI run succeeded at the captured baseline",
            },
            "comparison_caveat": "Local timings are not compared with CI runner timings.",
        },
        "authoritative_commands": authoritative_commands,
        "lanes": lanes,
        "summary": {
            "test_file_count": len(test_files),
            "support_fixture_count": len(fixtures),
            "test_case_count": total_cases,
            "ci_test_case_count": ci_cases,
            "language_file_counts": _counts(test_files, "language"),
            "bundle_file_counts": _counts(test_files, "bundle"),
            "unmapped_test_file_count": len(unmaintained),
            "classification_counts": _counts([*test_files, *fixtures], "classification"),
        },
        "classification_summary": {
            "source": _relative_display_path(root, _root_path(root, classifications_path)),
            "evidence_catalog": classification_source["evidence_catalog"],
            "high_confidence_candidates": classification_source["candidate_summary"]["high_confidence_candidates"],
            "review_observations": classification_source["candidate_summary"].get("review_observations", []),
            "milestone_10_disposition": classification_source["candidate_summary"]["milestone_10_disposition"],
        },
        "findings": {
            "not_in_ci_lanes": not_in_ci_lanes,
            "unmaintained_test_files": unmaintained,
            "excluded_from_inventory": [
                "Untracked build/DerivedData checkouts and generated Xcode output.",
                (
                    "Runtime durations, pass/fail results, hostnames, usernames, private paths, "
                    "credentials, and device serials."
                ),
                "Release/signing workflows and packaging smoke commands that do not execute test cases.",
                "Non-test support modules outside tests/fixtures; none were found in this baseline.",
            ],
        },
        "test_files": test_files,
        "support_fixtures": fixtures,
    }


def _counts(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row[key]
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _without_evidence(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result.pop("execution_evidence", None)
    result.pop("baseline", None)
    return result


def _canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(_without_evidence(document), indent=2, sort_keys=True) + "\n"


def _canonical_markdown(markdown: str) -> str:
    return re.sub(r"^- Baseline reference: `[^`]+`$", "- Baseline reference: `<ignored>`", markdown, flags=re.MULTILINE)


def render_markdown(document: dict[str, Any]) -> str:
    summary = document["summary"]
    classification_summary = document["classification_summary"]
    lines = [
        "# Test Audit Inventory v1",
        "",
        f"- Baseline reference: `{document['baseline']['sha']}`",
        f"- Test files: **{summary['test_file_count']}**",
        f"- Support fixtures: **{summary['support_fixture_count']}**",
        f"- Test cases counted: **{summary['test_case_count']}**",
        f"- Classification source: `{classification_summary['source']}`",
        "",
        "## Lanes",
        "",
        "| ID | Kind | Maintained | Command / targets | Sources |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lane in document["lanes"]:
        command = (
            lane.get("authoritative_command")
            or "; ".join(lane.get("commands", []))
            or ", ".join(lane.get("test_targets", []))
        )
        source_paths = ", ".join(f"`{path}`" for path in lane["source_paths"])
        lines.append(
            f"| `{lane['id']}` | {lane['kind']} | {str(lane.get('maintained', True)).lower()} | "
            f"`{command}` | {source_paths} |"
        )
    lines.extend(
        [
            "",
            "## Disposition Summary",
            "",
            *[
                f"- `{classification}`: **{count}** rows."
                for classification, count in summary["classification_counts"].items()
            ],
            (
                "- High-confidence implementation candidates: **none**."
                if not classification_summary["high_confidence_candidates"]
                else "- High-confidence implementation candidates are recorded in the JSON artifact."
            ),
            f"- Non-actionable review observations: **{len(classification_summary['review_observations'])}**.",
            (
                "- Milestone #10 disposition: "
                f"{classification_summary['milestone_10_disposition']['outcome'].rstrip('.')}."
            ),
            "",
            "## Execution Evidence",
            "",
            "Point-in-time execution evidence is recorded for the baseline and intentionally excluded from",
            "`--check` comparison.",
            "",
            "| Lane | Status | Tests | Skips | Test duration | Wall duration |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for lane in document["execution_evidence"]["local"]["lanes"]:
        test_duration = (
            f"{lane['test_duration_seconds']:.3f}s"
            if "test_duration_seconds" in lane
            else f"{lane['test_duration_milliseconds']}ms"
        )
        lines.append(
            f"| `{lane['id']}` | {lane['status']} | {lane['test_count']} | {lane.get('skipped_count', 0)} | "
            f"{test_duration} | {lane['wall_duration_seconds']:.2f}s |"
        )
    exact_head = document["execution_evidence"]["exact_head"]
    lines.extend(
        [
            "",
            f"Exact-head evidence captured from `{exact_head['captured_from_sha']}`:",
            "",
            "| Lane | Status | Tests | Skips | Test duration | Wall duration |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for lane in exact_head["lanes"]:
        test_duration = (
            f"{lane['test_duration_seconds']:.3f}s"
            if "test_duration_seconds" in lane
            else f"{lane['test_duration_milliseconds']}ms"
        )
        lines.append(
            f"| `{lane['id']}` | {lane['status']} | {lane['test_count']} | {lane.get('skipped_count', 0)} | "
            f"{test_duration} | {lane['wall_duration_seconds']:.2f}s |"
        )
    isolation = exact_head["python_module_isolation"]
    lines.append(
        f"- Python module isolation: {isolation['module_count']} modules, {isolation['test_count']} tests, "
        f"{isolation['skipped_count']} skips, {isolation['wall_duration_seconds']:.2f}s wall time."
    )
    lines.extend(
        [
            "",
            "## Findings",
            "",
            f"- Maintained lanes outside CI: **{len(document['findings']['not_in_ci_lanes'])}**.",
            f"- Unmaintained test files: **{len(document['findings']['unmaintained_test_files'])}**.",
            "- No row is classified as `replace`, `consolidate`, or `remove` without concrete repository evidence.",
            "",
            "## Classification Evidence",
            "",
            "| ID | Basis | Source paths |",
            "| --- | --- | --- |",
        ]
    )
    for evidence_id, item in sorted(classification_summary["evidence_catalog"].items()):
        source_paths = ", ".join(f"`{path}`" for path in item["source_paths"])
        lines.append(f"| `{evidence_id}` | {item['description']} | {source_paths} |")
    lines.extend(
        [
            "",
            "",
            "## Test Files",
            "",
            "| Path | Language | Bundle | Cases | Classification | Rationale | Evidence | Requirements | Signals |",
            "| --- | --- | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in document["test_files"]:
        requirements = "; ".join(row["special_requirements"]) or "—"
        signals = (
            ", ".join(f"{signal['signal']} ({signal['occurrences']})" for signal in row["static_brittleness_signals"])
            or "—"
        )
        evidence_ids = ", ".join(f"`{evidence_id}`" for evidence_id in row["classification_evidence_ids"])
        lines.append(
            f"| `{row['path']}` | {row['language']} | `{row['bundle']}` | {row['test_case_count']} | "
            f"`{row['classification']}` | {row['classification_rationale']} | {evidence_ids} | "
            f"{requirements} | {signals} |"
        )
    lines.extend(
        [
            "",
            "## Support Fixtures",
            "",
            "| Path | Format | Classification | Rationale | Evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in document["support_fixtures"]:
        evidence_ids = ", ".join(f"`{evidence_id}`" for evidence_id in row["classification_evidence_ids"])
        lines.append(
            f"| `{row['path']}` | {row['format']} | `{row['classification']}` | "
            f"{row['classification_rationale']} | {evidence_ids} |"
        )
    return "\n".join(lines) + "\n"


def write_artifacts(document: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(document), encoding="utf-8")


def check_artifacts(
    root: Path,
    json_path: Path,
    markdown_path: Path,
    classifications_path: Path = DEFAULT_CLASSIFICATIONS,
) -> tuple[bool, list[str]]:
    try:
        stored = json.loads(json_path.read_text(encoding="utf-8"))
        expected = build_inventory(
            root, baseline_sha=stored["baseline"]["sha"], classifications_path=classifications_path
        )
        actual_markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as error:
        return False, [f"unable to load generated artifacts: {error}"]
    differences = []
    if _canonical_json(stored) != _canonical_json(expected):
        differences.append("deterministic inventory differs from the generated JSON")
    if _canonical_markdown(actual_markdown) != _canonical_markdown(render_markdown(expected)):
        differences.append("generated Markdown differs from the deterministic inventory")
    return not differences, differences


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory tracked tests, fixtures, and maintained execution lanes.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root (default: current directory).")
    parser.add_argument(
        "--baseline-sha", default=None, help="Reference SHA recorded in the artifact (default: current HEAD)."
    )
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--classifications", type=Path, default=DEFAULT_CLASSIFICATIONS)
    parser.add_argument(
        "--check", action="store_true", help="Fail when deterministic inventory or Markdown has drifted."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    json_path = args.output_json if args.output_json.is_absolute() else root / args.output_json
    markdown_path = args.output_markdown if args.output_markdown.is_absolute() else root / args.output_markdown
    classifications_path = args.classifications if args.classifications.is_absolute() else root / args.classifications
    baseline_sha = args.baseline_sha or _git_output(root, ["rev-parse", "HEAD"]).strip()
    if args.check:
        matches, differences = check_artifacts(root, json_path, markdown_path, classifications_path)
        if not matches:
            for difference in differences:
                print(f"drift: {difference}", file=sys.stderr)
            return 1
        print("test audit inventory is up to date")
        return 0
    document = build_inventory(root, baseline_sha=baseline_sha, classifications_path=classifications_path)
    write_artifacts(document, json_path, markdown_path)
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
