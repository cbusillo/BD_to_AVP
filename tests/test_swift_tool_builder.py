import dataclasses
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import build_swift_tool_macos as builder


REPO_ROOT = Path(__file__).resolve().parents[1]


def committed_provenance(tool: str) -> builder.BuildProvenance:
    return builder.load_provenance(REPO_ROOT / builder.TOOLS[tool])


class SwiftToolBuilderTests(unittest.TestCase):
    def test_committed_binaries_match_provenance_checksums(self) -> None:
        for tool in builder.TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(
                    builder.sha256(REPO_ROOT / "bd_to_avp" / "bin" / tool),
                    committed_provenance(tool).unsigned_sha256,
                )

    def test_minimum_macos_matches_the_app(self) -> None:
        project = yaml.safe_load((REPO_ROOT / "macos" / "project.yml").read_text(encoding="utf-8"))
        for tool in builder.TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(
                    committed_provenance(tool).minimum_macos,
                    project["options"]["deploymentTarget"]["macOS"],
                )

    def test_load_provenance_rejects_missing_and_unexpected_fields(self) -> None:
        manifest = dataclasses.asdict(committed_provenance("spatial-media-kit-tool"))
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "build.json"

            manifest_path.write_text(json.dumps({**manifest, "revision": ""}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing: revision"):
                builder.load_provenance(manifest_path)

            manifest_path.write_text(json.dumps({**manifest, "linkage": "static"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected fields: linkage"):
                builder.load_provenance(manifest_path)

    def test_verify_binary_rejects_non_system_library(self) -> None:
        provenance = committed_provenance("fx-upscale")
        otool = "tool:\n\t/usr/lib/libSystem.B.dylib (compatibility)\n\t/opt/homebrew/lib/libx.dylib (compatibility)\n"

        def check_output(command: list[str], **_: object) -> str:
            outputs = {
                "otool": otool,
                "vtool": f"minos {provenance.minimum_macos}\n",
                "file": "Mach-O 64-bit executable arm64\n",
            }
            return outputs[command[0]]

        with patch.object(builder.subprocess, "check_output", side_effect=check_output):
            with self.assertRaisesRegex(RuntimeError, "non-system library: /opt/homebrew/lib/libx.dylib"):
                builder.verify_binary("fx-upscale", Path("tool"), provenance)


if __name__ == "__main__":
    unittest.main()
