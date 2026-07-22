import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts import build_edge264_macos


REPO_ROOT = Path(__file__).resolve().parents[1]


class Edge264BuilderTests(unittest.TestCase):
    def test_load_provenance_reads_all_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "edge264.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "repository": "https://example.invalid/edge264.git",
                        "revision": "a" * 40,
                        "platform": "macOS arm64",
                        "minimum_macos": "14.0",
                        "xcode_version": "26.5",
                        "xcode_build_version": "17F42",
                        "sdk_version": "26.5",
                        "architecture_flags": "-arch arm64",
                        "linkage": "static",
                        "unsigned_sha256": "c" * 64,
                    }
                ),
                encoding="utf-8",
            )

            provenance = build_edge264_macos.load_provenance(manifest_path)

        self.assertEqual(provenance.repository, "https://example.invalid/edge264.git")
        self.assertEqual(provenance.revision, "a" * 40)
        self.assertEqual(provenance.minimum_macos, "14.0")
        self.assertEqual(provenance.xcode_version, "26.5")
        self.assertEqual(provenance.sdk_version, "26.5")
        self.assertEqual(provenance.architecture_flags, "-arch arm64")

    def test_load_provenance_rejects_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "edge264.json"
            manifest_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "repository"):
                build_edge264_macos.load_provenance(manifest_path)

    def test_load_provenance_rejects_unexpected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "edge264.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "repository": "https://example.invalid/edge264.git",
                        "revision": "a" * 40,
                        "platform": "macOS arm64",
                        "minimum_macos": "14.0",
                        "xcode_version": "26.5",
                        "xcode_build_version": "17F42",
                        "sdk_version": "26.5",
                        "architecture_flags": "-arch arm64",
                        "linkage": "static",
                        "unsigned_sha256": "c" * 64,
                        "patch": "obsolete.patch",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected edge264 provenance fields: patch"):
                build_edge264_macos.load_provenance(manifest_path)

    def test_committed_binary_matches_provenance_checksum(self) -> None:
        provenance = build_edge264_macos.load_provenance(REPO_ROOT / build_edge264_macos.PROVENANCE_RELATIVE_PATH)

        self.assertEqual(
            build_edge264_macos.sha256(REPO_ROOT / "bd_to_avp" / "bin" / "edge264_test"),
            provenance.unsigned_sha256,
        )

    def test_load_provenance_rejects_invalid_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "edge264.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "repository": "https://example.invalid/edge264.git",
                        "revision": "a" * 40,
                        "platform": "macOS arm64",
                        "minimum_macos": "14.0",
                        "xcode_version": "26.5",
                        "xcode_build_version": "17F42",
                        "sdk_version": "26.5",
                        "architecture_flags": "-arch arm64",
                        "linkage": "static",
                        "unsigned_sha256": "ABC123",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "64 lowercase hexadecimal"):
                build_edge264_macos.load_provenance(manifest_path)

    def test_verify_checksum_rejects_mismatch(self) -> None:
        with tempfile.NamedTemporaryFile() as binary_file:
            binary_path = Path(binary_file.name)
            binary_path.write_bytes(b"binary")

            with self.assertRaisesRegex(RuntimeError, "edge264_test checksum"):
                build_edge264_macos.verify_checksum(binary_path, "0" * 64, "edge264_test")

    def test_build_edge264_uses_manifest_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository_root = Path(temp_dir)
            output_path = repository_root / "bin" / "edge264_test"
            binary_sha256 = hashlib.sha256(b"binary").hexdigest()
            provenance = build_edge264_macos.BuildProvenance(
                repository="https://example.invalid/edge264.git",
                revision="a" * 40,
                platform="macOS arm64",
                minimum_macos="15.0",
                xcode_version="26.5",
                xcode_build_version="17F42",
                sdk_version="26.5",
                architecture_flags="-arch arm64",
                linkage="static",
                unsigned_sha256=binary_sha256,
            )
            commands: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

            def fake_run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
                commands.append((command, cwd, env))
                if command[:3] == ["git", "clone", "--filter=blob:none"]:
                    Path(command[-1]).mkdir(parents=True)
                if command == build_edge264_macos.make_command(provenance, "check") and cwd:
                    (cwd / "edge264_test").write_bytes(b"binary")

            def fake_check_output(command: list[str], text: bool) -> str:
                self.assertTrue(text)
                if command[0] == "otool":
                    return "edge264_test:\n\t/usr/lib/libSystem.B.dylib\n"
                if command[0] == "vtool":
                    return "platform macos\nminos 15.0\nsdk 26.5\n"
                return "Mach-O 64-bit executable arm64"

            with (
                patch.object(build_edge264_macos, "run", side_effect=fake_run),
                patch.object(build_edge264_macos.subprocess, "check_output", side_effect=fake_check_output),
            ):
                actual_sha256 = build_edge264_macos.build_edge264(output_path, provenance)
            output_bytes = output_path.read_bytes()

        self.assertEqual(actual_sha256, binary_sha256)
        self.assertEqual(output_bytes, b"binary")
        self.assertTrue(
            any(
                command[:4] == ["git", "clone", "--filter=blob:none", provenance.repository]
                for command, _, _ in commands
            )
        )
        self.assertTrue(
            any(command == ["git", "checkout", "--detach", provenance.revision] for command, _, _ in commands)
        )
        check_target = build_edge264_macos.make_command(provenance, "check")
        build_command = next(item for item in commands if item[0] == check_target)
        self.assertEqual(
            check_target[:5],
            ["make", "OS=macos", "HOST_OS=distribution", "CFLAGS=-arch arm64", "STATIC=yes"],
        )
        self.assertFalse(any(command[:2] == ["git", "apply"] for command, _, _ in commands))
        build_env = build_command[2]
        self.assertIsNotNone(build_env)
        assert build_env is not None
        self.assertEqual(build_env["MACOSX_DEPLOYMENT_TARGET"], provenance.minimum_macos)

    def test_verify_toolchain_accepts_pinned_xcode_and_sdk(self) -> None:
        provenance = build_edge264_macos.BuildProvenance(
            repository="https://example.invalid/edge264.git",
            revision="a" * 40,
            platform="macOS arm64",
            minimum_macos="14.0",
            xcode_version="26.5",
            xcode_build_version="17F42",
            sdk_version="26.5",
            architecture_flags="-arch arm64",
            linkage="static",
            unsigned_sha256="c" * 64,
        )

        with patch.object(
            build_edge264_macos.subprocess,
            "check_output",
            side_effect=["Xcode 26.5\nBuild version 17F42\n", "26.5\n"],
        ):
            build_edge264_macos.verify_toolchain(provenance)

    def test_verify_toolchain_rejects_other_xcode(self) -> None:
        provenance = build_edge264_macos.BuildProvenance(
            repository="https://example.invalid/edge264.git",
            revision="a" * 40,
            platform="macOS arm64",
            minimum_macos="14.0",
            xcode_version="26.5",
            xcode_build_version="17F42",
            sdk_version="26.5",
            architecture_flags="-arch arm64",
            linkage="static",
            unsigned_sha256="c" * 64,
        )

        with (
            patch.object(
                build_edge264_macos.subprocess,
                "check_output",
                return_value="Xcode 27.0\nBuild version 27A5194q\n",
            ),
            self.assertRaisesRegex(RuntimeError, "Xcode 26.5"),
        ):
            build_edge264_macos.verify_toolchain(provenance)


if __name__ == "__main__":
    unittest.main()
