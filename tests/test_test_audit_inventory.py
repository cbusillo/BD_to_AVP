from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.test_audit_inventory import (
    _canonical_json,
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
"""


class TestTestAuditInventory(unittest.TestCase):
    def test_parses_authoritative_ci_commands(self) -> None:
        parsed = parse_ci_workflow(CI)
        self.assertEqual(parsed["runner"], "macos-26")
        self.assertEqual(parsed["timeout_minutes"], 30)
        self.assertEqual(
            [item["command"] for item in parsed["commands"]],
            [
                "uv run python -m unittest discover -s tests -t .",
                "uv run python scripts/native_app.py test",
                "npm run check",
            ],
        )
        self.assertEqual(parsed["commands"][2]["working_directory"], "support-diagnostics")

    def test_parses_project_targets_and_scheme_membership(self) -> None:
        parsed = parse_project_yml(PROJECT)
        self.assertEqual(parsed["targets"]["BluRayToVisionProTests"]["type"], "bundle.unit-test")
        self.assertEqual(parsed["schemes"]["SpatialPlaybackProbe"]["test_targets"], ["SpatialPlaybackProbeTests"])

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

    @staticmethod
    def _init_repo(root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    def _populate_minimal_repo(self, root: Path) -> None:
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
        subprocess.run(["git", "add", "."], cwd=root, check=True)

    @staticmethod
    def _tracked(root: Path) -> tuple[str, ...]:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=False)
        return tuple(path for path in output.decode().split("\0") if path)


if __name__ == "__main__":
    unittest.main()
