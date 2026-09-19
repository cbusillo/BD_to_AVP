import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import yaml

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


def git(repository: Path, *arguments: str) -> str:
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    return subprocess.check_output(["git", "-C", str(repository), *identity, *arguments], text=True).strip()


class Edge264PinUpdateTests(unittest.TestCase):
    def test_resolve_revision_names_the_commit_behind_tags_latest_and_full_shas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            upstream = Path(temp_dir)
            git(upstream, "init", "--quiet")
            commits = {}
            # Version order, not creation or alphabetical order, decides "latest".
            for tag in ("v2026.10.02", "v2026.9.30"):
                git(upstream, "commit", "--quiet", "--allow-empty", "-m", tag)
                git(upstream, "tag", "--annotate", "-m", tag, tag)
                commits[tag] = git(upstream, "rev-parse", "HEAD")
            git(upstream, "commit", "--quiet", "--allow-empty", "-m", "untagged")
            git(upstream, "tag", "lightweight")
            head = git(upstream, "rev-parse", "HEAD")

            self.assertEqual(build_edge264_macos.resolve_revision(str(upstream), "latest"), commits["v2026.10.02"])
            self.assertEqual(build_edge264_macos.resolve_revision(str(upstream), "v2026.9.30"), commits["v2026.9.30"])
            self.assertEqual(build_edge264_macos.resolve_revision(str(upstream), "lightweight"), head)
            self.assertEqual(build_edge264_macos.resolve_revision(str(upstream), "f" * 40), "f" * 40)
            with self.assertRaisesRegex(RuntimeError, "no tag matching"):
                build_edge264_macos.resolve_revision(str(upstream), "v1")

    def test_writing_the_loaded_manifest_reproduces_the_committed_file(self) -> None:
        committed = REPO_ROOT / build_edge264_macos.PROVENANCE_RELATIVE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            rewritten = Path(temp_dir) / "edge264.json"
            build_edge264_macos.write_provenance(rewritten, build_edge264_macos.load_provenance(committed))

            self.assertEqual(rewritten.read_bytes(), committed.read_bytes())


class Edge264UpstreamWatchTests(unittest.TestCase):
    """Run the watcher's shell step against a stand-in pin check and gh."""

    def run_watch(self, pending: str, open_issue: str) -> list[str]:
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/edge264-upstream-watch.yml").read_text(encoding="utf-8")
        )
        (step,) = [
            step for job in workflow["jobs"].values() for step in job["steps"] if "gh issue" in step.get("run", "")
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_directory = Path(temp_dir)
            calls = bin_directory / "calls"
            calls.touch()
            (bin_directory / "python3").write_text(f"#!/bin/sh\nprintf '%s' '{pending}'\n", encoding="utf-8")
            (bin_directory / "gh").write_text(
                f"#!/bin/sh\necho \"$2\" >> '{calls}'\n[ \"$2\" = list ] && printf '%s' '{open_issue}'\nexit 0\n",
                encoding="utf-8",
            )
            for tool in ("python3", "gh"):
                (bin_directory / tool).chmod(0o755)
            subprocess.run(
                ["/bin/bash", "-e", "-c", step["run"]],
                env={**step["env"], "PATH": f"{bin_directory}:/usr/bin:/bin", "GH_TOKEN": "unused"},
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            return calls.read_text(encoding="utf-8").split()

    def test_a_current_pin_touches_no_issue(self) -> None:
        self.assertEqual(self.run_watch(pending="", open_issue=""), [])

    def test_a_stale_pin_opens_one_issue_and_never_a_second(self) -> None:
        self.assertEqual(self.run_watch(pending="a" * 40, open_issue=""), ["list", "create"])
        self.assertEqual(self.run_watch(pending="a" * 40, open_issue="12"), ["list"])


class Edge264UpdateBranchTests(unittest.TestCase):
    """Run the update workflow's push step against a throwaway repository and remote."""

    def run_push(self, change_pin: bool) -> tuple[str, str]:
        workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/update-edge264.yml").read_text(encoding="utf-8"))
        (step,) = [
            step for job in workflow["jobs"].values() for step in job["steps"] if "git push" in step.get("run", "")
        ]
        manifest = build_edge264_macos.PROVENANCE_RELATIVE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            remote = Path(temp_dir) / "remote.git"
            checkout = Path(temp_dir) / "checkout"
            subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "--quiet", "--initial-branch", "main", str(checkout)], check=True)
            (checkout / manifest).parent.mkdir(parents=True)
            (checkout / manifest).write_text(json.dumps({"revision": "a" * 40}), encoding="utf-8")
            (checkout / "bd_to_avp/bin").mkdir(parents=True)
            (checkout / "bd_to_avp/bin/edge264_test").write_bytes(b"old")
            (checkout / "bd_to_avp/bin/edge264_test").chmod(0o755)
            git(checkout, "add", "--all")
            git(checkout, "commit", "--quiet", "-m", "pinned")
            git(checkout, "remote", "add", "origin", str(remote))
            if change_pin:
                # Artifact downloads do not keep the executable bit.
                (checkout / manifest).write_text(json.dumps({"revision": "b" * 40}), encoding="utf-8")
                (checkout / "bd_to_avp/bin/edge264_test").write_bytes(b"new")
                (checkout / "bd_to_avp/bin/edge264_test").chmod(0o644)
            summary = Path(temp_dir) / "summary"
            subprocess.run(
                ["/bin/bash", "-e", "-c", step["run"]],
                env={
                    **os.environ,
                    **step["env"],
                    "REQUESTED": "latest",
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "GITHUB_SERVER_URL": "https://example.invalid",
                    "GITHUB_REPOSITORY": "owner/repository",
                },
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            pushed = subprocess.run(
                ["git", "-C", str(remote), "ls-tree", "-r", step["env"]["BRANCH"]],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            return pushed, summary.read_text(encoding="utf-8")

    def test_a_new_pin_is_pushed_with_both_files_and_an_executable_decoder(self) -> None:
        pushed, summary = self.run_push(change_pin=True)

        self.assertRegex(pushed, r"(?m)^100755 blob \S+\tbd_to_avp/bin/edge264_test$")
        self.assertIn(str(build_edge264_macos.PROVENANCE_RELATIVE_PATH), pushed)
        self.assertIn("b" * 40, summary)

    def test_an_unchanged_pin_pushes_nothing(self) -> None:
        pushed, _ = self.run_push(change_pin=False)

        self.assertEqual(pushed, "")


if __name__ == "__main__":
    unittest.main()
