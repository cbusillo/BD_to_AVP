import hashlib
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

    def test_release_evidence_direct_push_publishes_exact_actor_bound_capture(self) -> None:
        workflow = load_workflow("release-evidence.yml")
        publish_steps = workflow["jobs"]["publish-evidence-branch"]["steps"]
        prepare_script = next(step["run"] for step in publish_steps if step.get("id") == "evidence-parent")
        publish_script = next(step["run"] for step in publish_steps if step.get("id") == "publish")
        release_tag = "v1.0.0"
        evidence_ref = f"automation/release-evidence-{release_tag}"
        actor_id = "1875516"
        actor_login = "cbusillo"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            remote = root / "remote.git"
            worktree = root / "worktree"
            prepared = root / "prepared"
            runner_temp = root / "runner-temp"
            output_path = root / "github-output"
            worktree.mkdir()
            prepared.mkdir()
            runner_temp.mkdir()
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=worktree, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=worktree, check=True)
            subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=worktree, check=True)
            main_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            bundle = prepared / "docs" / "release-evidence" / release_tag
            bundle.mkdir(parents=True)
            receipt_bytes = b'{"release":"fixture"}\n'
            signed_receipt_digest = "4" * 64
            signed_archive_bytes = b"signed-ui-archive"
            manifest_digest = "5" * 64
            (bundle / "capture-v2.json").write_text(
                json.dumps({"release_tag": release_tag, "state": "CAPTURED"}) + "\n",
                encoding="utf-8",
            )
            (bundle / "qualification-manifest.json").write_text(
                json.dumps({"manifest_sha256": manifest_digest}) + "\n",
                encoding="utf-8",
            )
            (bundle / "release-receipt.json").write_bytes(receipt_bytes)
            (bundle / "signed-artifact-ui-receipt.json").write_text(
                json.dumps({"receipt_sha256": signed_receipt_digest}) + "\n",
                encoding="utf-8",
            )
            (bundle / "signed-artifact-ui.zip").write_bytes(signed_archive_bytes)
            environment = {
                **os.environ,
                "EVIDENCE_ACTOR_ID": actor_id,
                "EVIDENCE_ACTOR_LOGIN": actor_login,
                "EVIDENCE_REF": evidence_ref,
                "EXPECTED_EXISTING_BRANCH_SHA": "",
                "EXPECTED_MAIN_SHA": main_sha,
                "RUNNER_TEMP": str(runner_temp),
            }
            subprocess.run(["bash", "-c", prepare_script], cwd=worktree, env=environment, check=True)
            environment.update(
                {
                    "EXPECTED_MANIFEST_SHA256": manifest_digest,
                    "EXPECTED_RECEIPT_FILE_SHA256": hashlib.sha256(receipt_bytes).hexdigest(),
                    "EXPECTED_SIGNED_UI_RECEIPT_SHA256": signed_receipt_digest,
                    "EXPECTED_SIGNED_UI_ARTIFACT_SHA256": hashlib.sha256(signed_archive_bytes).hexdigest(),
                    "GITHUB_OUTPUT": str(output_path),
                    "PREPARED_ROOT": str(prepared),
                    "RELEASE_TAG": release_tag,
                }
            )
            subprocess.run(["bash", "-c", publish_script], cwd=worktree, env=environment, check=True)
            published_sha = subprocess.run(
                ["git", "ls-remote", remote, f"refs/heads/{evidence_ref}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()[0]

            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                published_sha,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "show", "-s", "--format=%an <%ae>|%cn <%ce>", published_sha],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
                (
                    f"{actor_login} <{actor_id}+{actor_login}@users.noreply.github.com>|"
                    f"{actor_login} <{actor_id}+{actor_login}@users.noreply.github.com>"
                ),
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "diff", "--name-only", f"{main_sha}...{published_sha}"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.splitlines(),
                sorted(path.relative_to(prepared).as_posix() for path in bundle.iterdir()),
            )

    def test_release_evidence_stage_is_noop_for_current_evidence_branch(self) -> None:
        workflow = load_workflow("release-evidence.yml")
        stage_script = next(
            step["run"] for step in workflow["jobs"]["validate-and-prepare"]["steps"] if step.get("id") == "stage"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_path = root / "github-output"
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=root, check=True)
            base_path = root / "docs/base.md"
            base_path.parent.mkdir(parents=True)
            base_path.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-qc", "automation/release-evidence-v1.0.0"], cwd=root, check=True)
            evidence_path = root / "docs/release-evidence/v1.0.0/capture-v2.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text('{"state":"CAPTURED"}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "captured evidence"], cwd=root, check=True)

            branch_only_paths = subprocess.run(
                ["git", "diff", "--name-only", "main...HEAD", "--", "docs"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertEqual(branch_only_paths, ["docs/release-evidence/v1.0.0/capture-v2.json"])

            subprocess.run(
                ["bash", "-c", stage_script],
                cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
                check=True,
            )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "has_changes=false\n")
            self.assertFalse(any((root / "prepared-evidence").rglob("*")))

            output_path.unlink()
            subprocess.run(
                ["bash", "-c", stage_script],
                cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output_path), "REFRESHED": "true"},
                check=True,
            )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "has_changes=true\n")
            self.assertEqual(
                sorted(
                    path.relative_to(root / "prepared-evidence").as_posix()
                    for path in (root / "prepared-evidence").rglob("*.json")
                ),
                branch_only_paths,
            )


if __name__ == "__main__":
    unittest.main()
