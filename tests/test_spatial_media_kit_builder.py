import dataclasses
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import build_spatial_media_kit_macos as builder


REPO_ROOT = Path(__file__).resolve().parents[1]


def committed_provenance() -> builder.BuildProvenance:
    return builder.load_provenance(REPO_ROOT / builder.PROVENANCE_RELATIVE_PATH)


class SpatialMediaKitBuilderTests(unittest.TestCase):
    def test_committed_binary_matches_provenance_checksum(self) -> None:
        self.assertEqual(
            builder.sha256(REPO_ROOT / "bd_to_avp" / "bin" / builder.PRODUCT),
            committed_provenance().unsigned_sha256,
        )

    def test_load_provenance_rejects_missing_and_unexpected_fields(self) -> None:
        manifest = dataclasses.asdict(committed_provenance())
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "build.json"

            manifest_path.write_text(json.dumps({**manifest, "revision": ""}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing: revision"):
                builder.load_provenance(manifest_path)

            manifest_path.write_text(json.dumps({**manifest, "linkage": "static"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected fields: linkage"):
                builder.load_provenance(manifest_path)

    def test_verify_binary_rejects_non_system_library(self) -> None:
        provenance = committed_provenance()
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
                builder.verify_binary(Path("tool"), provenance)


if __name__ == "__main__":
    unittest.main()
