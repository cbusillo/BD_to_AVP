from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.test_audit_inventory import (
    _canonical_json,
    _lane_definitions,
    InventoryError,
    build_inventory,
    check_artifacts,
    parse_ci_workflow,
    parse_documented_lanes,
    parse_project_yml,
    render_markdown,
    write_artifacts,
)


CI = """
jobs:
  validate:
    runs-on: macos-26
    timeout-minutes: 30
    steps:
      - name: Unit smoke tests
        run: uv run python -m unittest discover -s tests -t .
      - name: macOS app tests
        run: uv run python scripts/native_app.py test
      - name: Validate support diagnostics service
        working-directory: support-diagnostics
        run: npm run check
      - name: Build BDToAVPPlayer visionOS test bundle
        env:
          DEVELOPER_DIR: /Applications/Xcode_26.5.app/Contents/Developer
        run: |
          set -euo pipefail
          cd macos
          export DEVELOPER_DIR="/Applications/Xcode_26.5.app/Contents/Developer"
          test -x "$DEVELOPER_DIR/usr/bin/xcodebuild"
          test "$(xcodebuild -version | sed -n '1p')" = "Xcode 26.5"
          xcodegen generate --spec project.yml
          xcodebuild build-for-testing \
            -scheme BDToAVPPlayer \
            -destination "generic/platform=visionOS Simulator" \
            CODE_SIGNING_ALLOWED=NO
          runtime_id="$(xcrun simctl list runtimes -j | jq -r '
            [.runtimes[]
              | select(.isAvailable == true)
              | select(.platform == "visionOS" or (.name | startswith("visionOS")))
              | select((.version | split(".")[0] | tonumber) >= 26)
              | .identifier]
            | first // empty
          ')"
          device_type_id="$(xcrun simctl list devicetypes -j | jq -r '
            [.devicetypes[] | select(.name == "Apple Vision Pro") | .identifier]
            | first // empty
          ')"
          if [[ -z "$runtime_id" || -z "$device_type_id" ]]; then
            echo "::notice::Skipping BDToAVPPlayer visionOS 26+ unit tests"
            exit 0
          fi
          simulator_udid="$(xcrun simctl create "BDToAVPPlayer CI" "$device_type_id" "$runtime_id")"
          cleanup_simulator() {
            xcrun simctl shutdown "$simulator_udid" >/dev/null 2>&1 || true
            xcrun simctl delete "$simulator_udid" >/dev/null 2>&1 || true
          }
          trap cleanup_simulator EXIT INT TERM
          xcrun simctl boot "$simulator_udid"
          xcrun simctl bootstatus "$simulator_udid" -b
          xcodebuild test-without-building \
            -scheme BDToAVPPlayer \
            -destination "platform=visionOS Simulator,id=$simulator_udid" \
            CODE_SIGNING_ALLOWED=NO
"""

PROJECT = """
targets:
  BluRayToVisionProTests:
    type: bundle.unit-test
    platform: macOS
    sources:
      - path: BluRayToVisionProTests
  SpatialPlaybackProbeTests:
    type: bundle.unit-test
    platform: auto
    sources:
      - path: SpatialPlaybackProbeTests

schemes:
  BluRayToVisionPro:
    test:
      targets:
        - BluRayToVisionProTests
  SpatialPlaybackProbe:
    test:
      targets:
        - SpatialPlaybackProbeTests
  BDToAVPPlayer:
    build:
      targets:
        BDToAVPPlayer: all
        BDToAVPPlayerTests: [test]
    test:
      targets:
        - BDToAVPPlayerTests
"""


class TestTestAuditInventory(unittest.TestCase):
    def test_committed_inventory_artifacts_are_current(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matches, differences = check_artifacts(
            root,
            root / "docs/test-audit/inventory-v1.json",
            root / "docs/test-audit/inventory-v1.md",
        )
        self.assertTrue(matches, differences)

    def test_parses_authoritative_ci_commands(self) -> None:
        parsed = parse_ci_workflow(CI)
        self.assertEqual(parsed["runner"], "macos-26")
        self.assertEqual(parsed["timeout_minutes"], 30)
        self.assertEqual(
            [item["command"] for item in parsed["commands"][:3]],
            [
                "uv run python -m unittest discover -s tests -t .",
                "uv run python scripts/native_app.py test",
                "npm run check",
            ],
        )
        self.assertEqual(parsed["commands"][2]["working_directory"], "support-diagnostics")
        player_command = parsed["commands"][3]["command"]
        self.assertIn("xcodebuild build-for-testing", player_command)
        self.assertIn('export DEVELOPER_DIR="/Applications/Xcode_26.5.app/Contents/Developer"', player_command)
        self.assertIn('test "$(xcodebuild -version | sed -n \'1p\')" = "Xcode 26.5"', player_command)
        self.assertIn("generic/platform=visionOS Simulator", player_command)
        self.assertIn("xcodebuild test-without-building", player_command)
        self.assertIn("xcrun simctl create", player_command)
        self.assertIn('select((.version | split(".")[0] | tonumber) >= 26)', player_command)
        self.assertIn("trap cleanup_simulator EXIT INT TERM", player_command)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", player_command)

    def test_requires_explicit_xcode_26_5_pin_for_player_lane(self) -> None:
        parsed = parse_ci_workflow(CI)
        unpinned_workflow = CI.replace(
            '          export DEVELOPER_DIR="/Applications/Xcode_26.5.app/Contents/Developer"\n'
            '          test -x "$DEVELOPER_DIR/usr/bin/xcodebuild"\n'
            '          test "$(xcodebuild -version | sed -n \'1p\')" = "Xcode 26.5"\n',
            "",
        )
        unpinned = parse_ci_workflow(unpinned_workflow)
        self.assertTrue(any("xcodebuild build-for-testing" in item["command"] for item in parsed["commands"]))
        lanes = _lane_definitions(unpinned, parse_project_yml(PROJECT), ())
        self.assertNotIn("ci.visionos.bd_to_avp_player", {lane["id"] for lane in lanes})

    def test_parses_project_targets_and_scheme_membership(self) -> None:
        parsed = parse_project_yml(PROJECT)
        self.assertEqual(parsed["targets"]["BluRayToVisionProTests"]["type"], "bundle.unit-test")
        self.assertEqual(parsed["schemes"]["SpatialPlaybackProbe"]["test_targets"], ["SpatialPlaybackProbeTests"])
        self.assertEqual(parsed["schemes"]["BDToAVPPlayer"]["test_targets"], ["BDToAVPPlayerTests"])

    def test_documented_lane_commands_are_extracted(self) -> None:
        tier3_document = "```sh\nuv run python -m scripts.tier3_clean_machine run \\\n  --route rc\n```"
        visionos_document = "```bash\nxcodebuild test \\\n  -scheme SpatialPlaybackProbe\n```"
        lanes = parse_documented_lanes(
            {
                "docs/tier3-clean-machine.md": tier3_document,
                "docs/visionos-playback-validator.md": visionos_document,
            }
        )
        self.assertEqual(lanes[0]["id"], "operator.tier3.installed_ui")
        self.assertIn("tier3_clean_machine run", lanes[0]["commands"][0])
        self.assertIn("xcodebuild test", lanes[1]["commands"][0])

    def test_inventory_and_markdown_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            paths = tuple(path for path in self._tracked(root) if not path.startswith("docs/test-audit"))
            first = build_inventory(root, baseline_sha="abc", paths=paths)
            second = build_inventory(root, baseline_sha="def", paths=paths)
            self.assertEqual(_canonical_json(first), _canonical_json(second))
            self.assertEqual(render_markdown(first).replace("`abc`", "`def`"), render_markdown(second))

    def test_check_ignores_baseline_and_evidence_but_detects_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            json_path = root / "inventory.json"
            markdown_path = root / "inventory.md"
            paths = tuple(path for path in self._tracked(root) if not path.startswith("docs/test-audit"))
            document = build_inventory(root, baseline_sha="baseline", paths=paths)
            write_artifacts(document, json_path, markdown_path)
            stored = json.loads(json_path.read_text())
            stored["baseline"]["sha"] = "newer-commit"
            stored["execution_evidence"]["local"]["identity"] = "different local runner"
            json_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
            self.assertEqual(check_artifacts(root, json_path, markdown_path), (True, []))
            (root / "tests/test_sample.py").write_text("def test_sample():\n    pass\n\ndef test_new():\n    pass\n")
            matches, differences = check_artifacts(root, json_path, markdown_path)
            self.assertFalse(matches)
            self.assertTrue(differences)

    def test_classifications_are_emitted_for_every_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            document = build_inventory(root, baseline_sha="baseline")
            self.assertEqual(document["summary"]["classification_counts"], {"valuable": 2})
            self.assertEqual(document["test_files"][0]["classification"], "valuable")
            self.assertEqual(document["test_files"][0]["classification_evidence_ids"], ["ci"])
            self.assertEqual(
                document["classification_summary"]["evidence_catalog"]["ci"]["description"],
                "maintained lane",
            )
            self.assertIn("Classification Evidence", render_markdown(document))

    def test_classifications_reject_missing_and_unknown_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            classification_path = root / "docs/test-audit/classifications-v1.json"
            source = json.loads(classification_path.read_text())
            source["cohorts"][0]["paths"] = ["tests/test_missing.py"]
            classification_path.write_text(json.dumps(source))
            with self.assertRaisesRegex(InventoryError, "unknown path"):
                build_inventory(root, baseline_sha="baseline")

            source["cohorts"][0]["paths"] = ["tests/test_sample.py"]
            classification_path.write_text(json.dumps(source))
            with self.assertRaisesRegex(InventoryError, "missing paths"):
                build_inventory(root, baseline_sha="baseline")

    def test_inventory_rejects_lane_references_missing_from_lane_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            (root / ".github/workflows/ci.yml").write_text("jobs:\n  validate:\n    runs-on: macos-26\n")
            with self.assertRaisesRegex(InventoryError, "undefined lanes: ci.python.unittest"):
                build_inventory(root, baseline_sha="baseline")

    def test_support_fixture_markdown_has_matching_table_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._populate_minimal_repo(root)
            markdown = render_markdown(build_inventory(root, baseline_sha="baseline"))
            self.assertIn(
                "| Path | Format | Classification | Rationale | Evidence |\n| --- | --- | --- | --- | --- |",
                markdown,
            )

    @staticmethod
    def _init_repo(root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    @staticmethod
    def _populate_minimal_repo(root: Path) -> None:
        (root / ".github/workflows").mkdir(parents=True)
        (root / "macos").mkdir()
        (root / "tests").mkdir()
        (root / "support-diagnostics/test").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / ".github/workflows/ci.yml").write_text(CI)
        (root / "macos/project.yml").write_text(PROJECT)
        (root / "docs/tier3-clean-machine.md").write_text(
            "```sh\nuv run python -m scripts.tier3_clean_machine run\n```"
        )
        (root / "docs/visionos-playback-validator.md").write_text(
            "```bash\nxcodebuild test -scheme SpatialPlaybackProbe\n```"
        )
        (root / "docs/tier3-operator-hardware.md").write_text("# hardware")
        (root / "tests/test_sample.py").write_text("def test_sample():\n    pass\n")
        (root / "support-diagnostics/test/sample.test.ts").write_text("test('sample', () => {})\n")
        (root / "docs/test-audit").mkdir()
        (root / "docs/test-audit/classifications-v1.json").write_text(
            json.dumps(
                {
                    "artifact": "test-audit-classifications",
                    "schema_version": 1,
                    "evidence_catalog": {
                        "ci": {"description": "maintained lane", "source_paths": [".github/workflows/ci.yml"]}
                    },
                    "candidate_summary": {
                        "high_confidence_candidates": [],
                        "milestone_10_disposition": {"outcome": "no action", "rationale": "test fixture"},
                    },
                    "cohorts": [
                        {
                            "id": "all",
                            "classification": "valuable",
                            "rationale": "{path} has a maintained test contract.",
                            "evidence_ids": ["ci"],
                            "paths": ["support-diagnostics/test/sample.test.ts", "tests/test_sample.py"],
                        }
                    ],
                }
            )
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)

    @staticmethod
    def _tracked(root: Path) -> tuple[str, ...]:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=False)
        return tuple(path for path in output.decode().split("\0") if path)


if __name__ == "__main__":
    unittest.main()
