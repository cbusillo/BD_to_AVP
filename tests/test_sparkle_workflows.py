import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

from scripts.production_identity import PRODUCTION_DEVELOPER_IDENTITY, PRODUCTION_TEAM_ID


REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def load_workflow(name: str) -> dict:
    with (REPO_ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


def load_github_config() -> dict:
    with (REPO_ROOT / ".github" / "github.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def load_release_engine() -> dict:
    return load_workflow("release-engine.yml")


class ReleaseWorkflowTests(unittest.TestCase):
    def test_important_workflow_names_match_repository_metadata(self) -> None:
        paths = REPO_ROOT / ".github" / "workflows"
        names = {load_workflow(path.name)["name"] for path in [*paths.glob("*.yml"), *paths.glob("*.yaml")]}
        self.assertLessEqual(set(load_github_config()["importantWorkflows"]), names)

    def test_sparkle_bundle_uses_importable_module_entrypoint(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", "-m", "scripts.sparkle_bundle", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_certificate_install_binds_developer_id_identity_to_team_id(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        certificate_step = next(step for step in package["steps"] if step.get("id") == "signing-certificate")
        certificate_script = certificate_step["run"]

        validation_script = certificate_script[
            certificate_script.index('case "$TEAM_ID" in') : certificate_script.index("BUILD_KEYCHAIN_PASSWORD=")
        ]
        self._assert_signing_identity_validation(validation_script)

    def test_package_inspects_signed_app_and_dmg_identity_metadata(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        package_step = next(step for step in package["steps"] if step.get("id") == "package_artifact")
        package_script = package_step["run"]

        identity_validation = (
            package_script[
                package_script.index("validate_signing_identity() {") : package_script.index(
                    "git fetch --no-tags origin"
                )
            ]
            + "\nvalidate_signing_identity\n"
        )
        self._assert_signing_identity_validation(identity_validation)

        metadata_validation = package_script[
            package_script.index("verify_codesign_metadata() {") : package_script.index("validate_signing_identity() {")
        ]
        self._assert_codesign_metadata_validation(metadata_validation)

    def test_package_rejects_bad_signature_metadata_before_notarizing(self) -> None:
        package = load_release_engine()["jobs"]["package"]
        script = next(step["run"] for step in package["steps"] if step.get("id") == "package_artifact")
        for bad_target in ("", "app", "dmg"):
            with self.subTest(bad_target=bad_target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tools = root / "tools"
                tools.mkdir()
                calls = root / "calls"
                stubs = {
                    "git": 'if [ "$1" = rev-parse ]; then echo "$GITHUB_SHA"; fi',
                    "security": ":",
                    "ditto": ":",
                    "stat": "echo 7",
                    "uv": """
case "$*" in
  *"scripts/native_app.py package"*) echo package >> "$CALLS" ;;
  *"scripts.macos_release verify-app"*|*"scripts.macos_release verify-dmg"*)
    echo verify >> "$CALLS"
    printf '{"short_version":"%s","build_version":"%s"}\\n' "$PACKAGE_VERSION" "$BUILD_VERSION" ;;
  *"scripts.macos_release create-dmg"*) printf fixture > "dist/$DMG_NAME" ;;
  *"scripts.macos_release notarize"*)
    case "$*" in
      *"app-notary.json"*) echo notarize-app >> "$CALLS" ;;
      *"dmg-notary.json"*) echo notarize-dmg >> "$CALLS" ;;
    esac ;;
  *"app_tree_sha256"*) printf '%064d\\n' 0 ;;
  *) exit 99 ;;
esac
""",
                    "codesign": """
if [ "$1" = -dv ]; then
  case "$3" in
    *.app) target=app ;;
    *.dmg) target=dmg ;;
    *) exit 99 ;;
  esac
  echo "inspect-$target" >> "$CALLS"
  if [ "$BAD_TARGET" = "$target" ]; then authority=wrong; else authority="$DEV_ID"; fi
  printf 'Authority=%s\\nTeamIdentifier=%s\\n' "$authority" "$PRODUCTION_TEAM_ID"
elif [ "$1" = --force ]; then
  echo sign-dmg >> "$CALLS"
fi
""",
                }
                for command, body in stubs.items():
                    path = tools / command
                    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
                    path.chmod(0o700)
                result = subprocess.run(
                    ["/bin/bash", "-c", script],
                    cwd=root,
                    env={
                        "PATH": f"{tools}:/usr/bin:/bin",
                        "CALLS": str(calls),
                        "BAD_TARGET": bad_target,
                        "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "100",
                        "GITHUB_OUTPUT": str(root / "output"),
                        "RUNNER_TEMP": str(root),
                        "KEYCHAIN_PATH": str(root / "keychain"),
                        "BUILD_KEYCHAIN_PASSWORD": "fixture",
                        "NOTARY_PROFILE": "fixture",
                        "DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                        "PRODUCTION_DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                        "TEAM_ID": PRODUCTION_TEAM_ID,
                        "PRODUCTION_TEAM_ID": PRODUCTION_TEAM_ID,
                        "DMG_NAME": "fixture.dmg",
                        "PACKAGE_VERSION": "1.0.0",
                        "BUILD_VERSION": "1",
                    },
                    capture_output=True,
                    check=False,
                )
                events = calls.read_text(encoding="utf-8").splitlines()
                self.assertIn("package", events)
                self.assertIn("inspect-app", events)
                if bad_target:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"inspect-{bad_target}", events)
                    self.assertNotIn(f"notarize-{bad_target}", events)
                    if bad_target == "app":
                        self.assertNotIn("inspect-dmg", events)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertLess(events.index("package"), events.index("inspect-app"))
                    self.assertLess(events.index("inspect-app"), events.index("notarize-app"))
                    self.assertLess(events.index("sign-dmg"), events.index("inspect-dmg"))
                    self.assertLess(events.index("inspect-dmg"), events.index("notarize-dmg"))
                    self.assertEqual(events[-1], "verify")

    def test_keychain_search_list_empty_array_branch_is_bash_3_2_safe(self) -> None:
        shell_script = (
            'set -u; keychains=(); if [ "${#keychains[@]}" -eq 0 ]; '
            'then echo empty; else printf "%s\\n" "${keychains[@]}"; fi'
        )
        result = subprocess.run(
            ["/bin/bash", "-c", shell_script],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "empty\n")

    def test_keychain_cleanup_restores_search_list_and_removes_material(self) -> None:
        result, state, paths = self._run_keychain_cleanup(
            ["/Users/runner/Library/Keychains/login.keychain-db", "/tmp/Space Keychain"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, "/Users/runner/Library/Keychains/login.keychain-db\n/tmp/Space Keychain\n")
        self.assertFalse(paths["keychain"].exists())
        self.assertFalse(paths["certificate"].exists())
        self.assertFalse(paths["snapshot"].exists())

    def test_keychain_cleanup_restores_empty_search_list(self) -> None:
        result, state, paths = self._run_keychain_cleanup([])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state, "")
        self.assertFalse(paths["snapshot"].exists())

    def test_keychain_cleanup_failure_blocks_artifact_upload_and_preserves_snapshot(self) -> None:
        result, _, paths = self._run_keychain_cleanup(["/tmp/login.keychain-db"], restore_exit=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to restore the user keychain search list.", result.stderr)
        self.assertTrue(paths["snapshot"].exists())

    def _run_keychain_cleanup(
        self,
        keychains: list[str],
        *,
        restore_exit: int = 0,
    ) -> tuple[subprocess.CompletedProcess[str], str, dict[str, Path]]:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        cleanup_step = next(step for step in package["steps"] if step.get("id") == "signing-cleanup")
        cleanup_script = cleanup_step["run"]

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        temporary_path = Path(temporary_directory.name)
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        security_state = temporary_path / "security-state.txt"
        security_calls = temporary_path / "security-calls.txt"
        fake_security = fake_bin / "security"
        fake_security.write_text(
            """#!/bin/bash
set -eu
case "$1" in
  list-keychains)
    shift
    if [ "${1:-}" = "-d" ]; then
      shift 2
    fi
    if [ "${1:-}" = "-s" ]; then
      shift
      : > "$SECURITY_STATE_PATH"
      for keychain in "$@"; do
        printf '%s\\n' "$keychain" >> "$SECURITY_STATE_PATH"
      done
      exit "${SECURITY_RESTORE_EXIT:-0}"
    fi
    ;;
  delete-keychain)
    printf 'delete %s\\n' "$2" >> "$SECURITY_CALLS_PATH"
    rm -f "$2"
    ;;
esac
""",
            encoding="utf-8",
        )
        fake_security.chmod(0o755)

        snapshot = temporary_path / "user-keychains.txt"
        snapshot.write_text("".join(f"{keychain}\n" for keychain in keychains), encoding="utf-8")
        keychain_path = temporary_path / "ephemeral.keychain-db"
        certificate_path = temporary_path / "certificate.p12"
        keychain_path.touch()
        certificate_path.touch()
        github_env = temporary_path / "github-env.txt"
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CERTIFICATE_PATH": str(certificate_path),
            "GITHUB_ENV": str(github_env),
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "123",
            "KEYCHAIN_PATH": str(keychain_path),
            "RUNNER_TEMP": str(temporary_path),
            "SECURITY_CALLS_PATH": str(security_calls),
            "SECURITY_RESTORE_EXIT": str(restore_exit),
            "SECURITY_STATE_PATH": str(security_state),
            "USER_KEYCHAINS_PATH": str(snapshot),
        }
        result = subprocess.run(
            ["/bin/bash", "-c", cleanup_script],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        state = security_state.read_text(encoding="utf-8") if security_state.exists() else ""
        paths = {
            "certificate": certificate_path,
            "keychain": keychain_path,
            "snapshot": snapshot,
        }
        return result, state, paths

    def _assert_signing_identity_validation(self, validation_script: str) -> None:
        valid = self._run_release_shell_fragment(
            validation_script,
            {
                "DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                "PRODUCTION_DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                "PRODUCTION_TEAM_ID": PRODUCTION_TEAM_ID,
                "TEAM_ID": PRODUCTION_TEAM_ID,
            },
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

        invalid_cases = [
            ("", PRODUCTION_DEVELOPER_IDENTITY),
            ("ABCDE1234", "Developer ID Application: Shiny Computers Leasing LLC (ABCDE1234)"),
            (PRODUCTION_TEAM_ID, f"Apple Distribution: Shiny Computers Leasing LLC ({PRODUCTION_TEAM_ID})"),
            (PRODUCTION_TEAM_ID, f"Developer ID Application:  ({PRODUCTION_TEAM_ID})"),
            (PRODUCTION_TEAM_ID, "Developer ID Application: Shiny Computers Leasing LLC (ZZZZZ12345)"),
            (PRODUCTION_TEAM_ID, f"{PRODUCTION_DEVELOPER_IDENTITY} extra"),
            (PRODUCTION_TEAM_ID, f"prefix {PRODUCTION_DEVELOPER_IDENTITY}"),
            (PRODUCTION_TEAM_ID, f"{PRODUCTION_DEVELOPER_IDENTITY} (ZZZZZ12345)"),
        ]
        for team_id, dev_id in invalid_cases:
            with self.subTest(team_id=team_id, dev_id=dev_id):
                result = self._run_release_shell_fragment(
                    validation_script,
                    {
                        "DEV_ID": dev_id,
                        "PRODUCTION_DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                        "PRODUCTION_TEAM_ID": PRODUCTION_TEAM_ID,
                        "TEAM_ID": team_id,
                    },
                )
                self.assertNotEqual(result.returncode, 0)

    def _assert_codesign_metadata_validation(self, metadata_script: str) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        temporary_path = Path(temporary_directory.name)
        metadata_path = temporary_path / "codesign.txt"
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        fake_codesign = fake_bin / "codesign"
        fake_codesign.write_text(
            """#!/bin/bash
set -eu
printf '%s' "$CODESIGN_METADATA"
""",
            encoding="utf-8",
        )
        fake_codesign.chmod(0o755)
        signed_path = temporary_path / "Signed.app"
        signed_path.touch()
        invocation = f'{metadata_script}\nverify_codesign_metadata "{signed_path}" "Test app" "{metadata_path}"\n'

        valid_metadata = "".join(
            [
                "Executable=/tmp/Test.app/Contents/MacOS/Test\n",
                f"Authority={PRODUCTION_DEVELOPER_IDENTITY}\n",
                "Authority=Developer ID Certification Authority\n",
                f"TeamIdentifier={PRODUCTION_TEAM_ID}\n",
            ]
        )
        valid = self._run_release_shell_fragment(
            invocation,
            {
                "CODESIGN_METADATA": valid_metadata,
                "DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "PRODUCTION_TEAM_ID": PRODUCTION_TEAM_ID,
            },
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

        invalid_cases = [
            f"TeamIdentifier={PRODUCTION_TEAM_ID}\n",
            f"Authority={PRODUCTION_DEVELOPER_IDENTITY}\n",
            (
                f"Authority=Developer ID Application: Other Company ({PRODUCTION_TEAM_ID})\n"
                f"TeamIdentifier={PRODUCTION_TEAM_ID}\n"
            ),
            (f"Authority={PRODUCTION_DEVELOPER_IDENTITY} extra\nTeamIdentifier={PRODUCTION_TEAM_ID}\n"),
            (f"Authority={PRODUCTION_DEVELOPER_IDENTITY}\nTeamIdentifier=ZZZZZ12345\n"),
            (f"NotAuthority={PRODUCTION_DEVELOPER_IDENTITY}\nNotTeamIdentifier={PRODUCTION_TEAM_ID}\n"),
        ]
        for metadata in invalid_cases:
            with self.subTest(metadata=metadata):
                result = self._run_release_shell_fragment(
                    invocation,
                    {
                        "CODESIGN_METADATA": metadata,
                        "DEV_ID": PRODUCTION_DEVELOPER_IDENTITY,
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "PRODUCTION_TEAM_ID": PRODUCTION_TEAM_ID,
                    },
                )
                self.assertNotEqual(result.returncode, 0)

    def _run_release_shell_fragment(
        self,
        shell_script: str,
        environment_overrides: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        temporary_path = Path(temporary_directory.name)
        environment = {**os.environ, **environment_overrides}
        return subprocess.run(
            ["/bin/bash", "-c", shell_script],
            cwd=temporary_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
