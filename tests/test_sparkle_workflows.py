import importlib
import json
import os
import re
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
    def test_ci_fetches_full_history_for_recovery_provenance(self) -> None:
        workflow = load_workflow("ci.yml")
        checkout = workflow["jobs"]["validate"]["steps"][0]

        self.assertEqual(checkout["uses"], "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0")
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_sparkle_bundle_uses_importable_module_entrypoint(self) -> None:
        workflow = load_release_engine()
        workflow_text = str(workflow)
        ci_text = str(load_workflow("ci.yml"))
        smoke_text = (REPO_ROOT / "docs" / "release-smoke.md").read_text(encoding="utf-8")

        self.assertNotIn("python scripts/sparkle_bundle.py", workflow_text)
        self.assertEqual(workflow_text.count("python -m scripts.sparkle_bundle"), 2)
        self.assertNotIn("python scripts/sparkle_bundle.py", smoke_text)
        self.assertNotIn("python scripts/briefcase_app.py", workflow_text + ci_text)
        self.assertEqual((workflow_text + ci_text).count("python -m scripts.briefcase_app"), 2)
        self.assertIn("python scripts/native_app.py package", workflow_text)
        self.assertIn("python -m scripts.macos_release", workflow_text)

        result = subprocess.run(
            [sys.executable, "-S", "-m", "scripts.sparkle_bundle", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_is_manual_only_and_packages_github_sha_from_main(self) -> None:
        operators = {
            "Stable": load_workflow("briefcase.yml"),
            "Prerelease": load_workflow("prerelease.yml"),
        }
        workflow = load_release_engine()
        prepare = workflow["jobs"]["prepare"]
        package = workflow["jobs"]["package"]

        self.assertEqual({operator["name"] for operator in operators.values()}, set(operators))
        for name, operator in operators.items():
            with self.subTest(operator=name):
                self.assertEqual(set(operator["on"]), {"workflow_dispatch"})
                self.assertEqual(set(operator["on"]["workflow_dispatch"]["inputs"]), {"release_notes"})
                self.assertEqual(operator["concurrency"]["group"], "release")
                self.assertEqual(operator["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(workflow["env"]["GH_REPO"], "${{ github.repository }}")
        self.assertNotIn("source_ref", str(operators) + str(workflow))
        self.assertNotIn("concurrency", workflow)
        self.assertEqual(
            sum(str(operator.get("concurrency", {}).get("group")) == "release" for operator in operators.values()),
            2,
        )
        checkout = prepare["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertIn("refs/heads/main", str(prepare))
        self.assertIn("refs/remotes/origin/main", str(prepare))
        self.assertIn("refs/remotes/origin/main", str(package))
        self.assertIn("moved after signing approval", str(package))
        self.assertGreaterEqual(str(package).count("refs/remotes/origin/main"), 4)
        self.assertIn("moved immediately before certificate use", str(package))
        self.assertIn("moved immediately before notarization credential use", str(package))
        self.assertIn("moved immediately before package signing", str(package))
        self.assertIn("refs/tags/$RELEASE_TAG^{}", str(prepare))
        self.assertIn("jq -r .name", str(prepare))
        self.assertNotIn("refs/heads/release", str(workflow))

    def test_reusable_engine_rejects_direct_invocation_and_policy_bypass(self) -> None:
        stable = load_workflow("briefcase.yml")
        prerelease = load_workflow("prerelease.yml")
        workflow = load_release_engine()
        release = stable["jobs"]["release"]
        call = workflow["on"]["workflow_call"]
        configured_environments = load_github_config()["releaseEnvironments"]
        macos_secret_names = set(configured_environments["macos-signing"]["secrets"])
        sparkle_secret_names = set(configured_environments["sparkle-release"]["secrets"])
        declared_secret_names = set(call["secrets"])
        workflow_sources = [
            (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for name in ("briefcase.yml", "prerelease.yml", "release-engine.yml")
        ]
        referenced_secret_names = set(
            re.findall(
                r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}",
                (REPO_ROOT / ".github" / "workflows" / "release-engine.yml").read_text(encoding="utf-8"),
            )
        )
        policy = workflow["jobs"]["policy"]
        entry_guard = policy["steps"][0]
        policy_checkout = policy["steps"][1]
        policy_step = next(step for step in policy["steps"] if step.get("id") == "policy")

        self.assertEqual(set(stable["jobs"]), {"release", "publish-pypi"})
        self.assertEqual(set(prerelease["jobs"]), {"release"})
        self.assertEqual(prerelease["jobs"]["release"], release)
        self.assertEqual(release["uses"], "./.github/workflows/release-engine.yml")
        self.assertEqual(declared_secret_names, macos_secret_names | sparkle_secret_names)
        self.assertEqual(declared_secret_names, referenced_secret_names)
        self.assertTrue(all(definition["required"] == "false" for definition in call["secrets"].values()))
        self.assertEqual(
            release["secrets"],
            {name: f"${{{{ secrets.{name} }}}}" for name in declared_secret_names},
        )
        self.assertTrue(all("secrets: inherit" not in source for source in workflow_sources))
        self.assertEqual(
            set(
                re.findall(
                    r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}",
                    json.dumps(workflow["jobs"]["package"]),
                )
            ),
            macos_secret_names,
        )
        self.assertEqual(
            set(
                re.findall(
                    r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}",
                    json.dumps(workflow["jobs"]["publish-appcast"]),
                )
            ),
            sparkle_secret_names,
        )
        for job_name, job in workflow["jobs"].items():
            if job_name not in {"package", "publish-appcast"}:
                self.assertNotIn("secrets.", json.dumps(job))
        self.assertEqual(
            release["permissions"],
            {
                "actions": "read",
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
                "pages": "write",
            },
        )
        self.assertEqual(workflow["permissions"], {"actions": "read", "contents": "read"})
        self.assertEqual(release["with"]["release_sha"], "${{ github.sha }}")
        self.assertEqual(release["with"]["operator_workflow_ref"], "${{ github.workflow_ref }}")
        self.assertEqual(release["with"]["operator_workflow_sha"], "${{ github.workflow_sha }}")
        self.assertEqual(release["with"]["operator_actor"], "${{ github.actor }}")
        self.assertEqual(release["with"]["operator_triggering_actor"], "${{ github.triggering_actor }}")
        self.assertEqual(
            {name for name, definition in call["inputs"].items() if definition.get("required") == "true"},
            {
                "release_sha",
                "operator_workflow_ref",
                "operator_workflow_sha",
                "operator_run_id",
                "operator_run_attempt",
                "operator_actor",
                "operator_triggering_actor",
            },
        )
        self.assertFalse({"route", "mode", "stage", "prerelease", "publish_pypi"} & set(call["inputs"]))
        for operator in (stable, prerelease):
            dispatch_inputs = operator["on"]["workflow_dispatch"]["inputs"]
            self.assertFalse({"route", "mode", "stage", "prerelease", "publish_pypi"} & set(dispatch_inputs))
        self.assertEqual(policy["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(entry_guard["name"], "Require a protected release operator context")
        self.assertNotIn("uses", entry_guard)
        self.assertIn("cbusillo/BD_to_AVP/.github/workflows/briefcase.yml@refs/heads/main", entry_guard["run"])
        self.assertIn("cbusillo/BD_to_AVP/.github/workflows/prerelease.yml@refs/heads/main", entry_guard["run"])
        self.assertIn('test "$ACTUAL_REF" = "refs/heads/main"', entry_guard["run"])
        self.assertIn('test "$ACTUAL_ACTOR" = "shiny-code-bot"', entry_guard["run"])
        self.assertEqual(policy_checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(policy_checkout["with"]["persist-credentials"], "false")
        self.assertIn("Reject direct invocation or operator policy bypass", str(policy))
        self.assertEqual(policy_step["env"]["RELEASE_OPERATOR_WORKFLOW_REF"], "${{ github.workflow_ref }}")
        self.assertNotIn("RELEASE_ENGINE_WORKFLOW_REF", policy_step["env"])
        self.assertNotIn("RELEASE_ENGINE_WORKFLOW_SHA", policy_step["env"])
        self.assertIn("release_workflow_policy.py engine", policy_step["run"])
        self.assertIn("release_route", policy["outputs"])
        self.assertIn("operator_workflow_path", policy["outputs"])
        self.assertEqual(workflow["jobs"]["prepare"]["needs"], "policy")
        self.assertEqual(set(workflow["jobs"]["package"]["needs"]), {"policy", "prepare"})
        self.assertIn("--expected-fingerprint", str(workflow["jobs"]["package"]))
        self.assertIn("needs.policy.outputs.engine_workflow_ref", str(workflow["jobs"]["package"]))

    def test_release_metadata_is_derived_by_tested_python(self) -> None:
        workflow = load_release_engine()
        prepare = workflow["jobs"]["prepare"]
        release_history = next(step for step in prepare["steps"] if step.get("id") == "release_history")
        route_validation = next(
            step for step in prepare["steps"] if step["name"] == "Validate route and summarize publication effects"
        )

        self.assertIn("python -m scripts.release metadata", str(prepare))
        self.assertEqual(prepare["permissions"], {"actions": "read", "contents": "write"})
        self.assertIn("release_workflow_policy.py metadata", route_validation["run"])
        self.assertIn("$GITHUB_STEP_SUMMARY", route_validation["run"])
        self.assertEqual(
            route_validation["env"]["RELEASE_OPERATOR_WORKFLOW_REF"],
            "${{ needs.policy.outputs.operator_workflow_ref }}",
        )
        self.assertEqual(route_validation["env"]["RELEASE_TAG"], "${{ steps.metadata.outputs.release_tag }}")
        self.assertEqual(route_validation["env"]["RELEASE_CHANNEL"], "${{ steps.metadata.outputs.channel }}")
        self.assertNotIn("RELEASE_ROUTE", route_validation["env"])
        self.assertNotIn("awk -v version", str(workflow))
        self.assertNotIn("release_tag_suffix", str(workflow))
        self.assertIn("public_version", prepare["outputs"])
        self.assertIn("dmg_name", prepare["outputs"])
        self.assertIn("publish_pypi", prepare["outputs"])
        self.assertIn("previous_release_tag", prepare["outputs"])
        self.assertIn("python -m scripts.release notes-base", str(release_history))
        self.assertIn("check-release", str(prepare))
        self.assertNotIn("sort_by(.published_at)", str(release_history))
        self.assertNotIn("git merge-base --is-ancestor", str(release_history))
        self.assertIn("live-appcast.xml", str(prepare))
        self.assertIn("LATEST_SNAPSHOT_TAG", str(prepare))
        self.assertIn("base_snapshot_tag", prepare["outputs"])
        self.assertIn("appcast-state.json", str(prepare))
        self.assertNotIn("${BASE_SNAPSHOT_TAG#v}", str(prepare))
        self.assertNotIn("${LATEST_SNAPSHOT_TAG#v}", str(prepare))
        self.assertIn('--release-tag "$BASE_SNAPSHOT_TAG"', str(prepare))
        self.assertIn('--release-tag "$LATEST_SNAPSHOT_TAG"', str(prepare))

    def test_beta4_freeze_retains_release_preflight_guards(self) -> None:
        workflow = load_release_engine()
        prepare_steps = workflow["jobs"]["prepare"]["steps"]
        step_names = [step["name"] for step in prepare_steps]
        freeze_policy = json.loads((REPO_ROOT / ".github" / "release-freezes.json").read_text(encoding="utf-8"))

        self.assertEqual(freeze_policy["schema"], "bd_to_avp.release_freezes")
        self.assertEqual(
            freeze_policy["frozen_release_tags"],
            {
                "v0.3.0-beta.4": {
                    "issue": 316,
                    "reason": (
                        "Beta 4 signing, publication, appcast mutation, and field qualification require explicit "
                        "authorization after the production diagnostics admission gate is deployed."
                    ),
                }
            },
        )
        self.assertLess(
            step_names.index("Validate route and summarize publication effects"),
            step_names.index("Reject existing release identity"),
        )
        recovery_step = next(step for step in prepare_steps if step["name"] == "Revalidate Beta 3 recovery premise")
        self.assertEqual(recovery_step["if"], "steps.metadata.outputs.release_tag == 'v0.3.0-beta.3'")
        self.assertEqual(recovery_step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn("--allow-beta3-draft", recovery_step["run"])
        self.assertIn("--allow-github-actions-contents-write-token", recovery_step["run"])
        self.assertIn('--expected-sha "$GITHUB_SHA"', recovery_step["run"])

    def test_every_release_artifact_inspection_pins_the_diagnostics_endpoint(self) -> None:
        workflow = load_release_engine()
        inspections = (
            (
                workflow["jobs"]["package"],
                "Package application for GitHub",
            ),
            (
                workflow["jobs"]["compatibility"],
                "Validate the packaged app on macOS 26",
            ),
            (
                workflow["jobs"]["publish-appcast"],
                "Inspect draft DMG",
            ),
            (
                workflow["jobs"]["verify-draft"],
                "Re-download and verify every release boundary",
            ),
        )
        for job, step_name in inspections:
            with self.subTest(step=step_name):
                step = next(step for step in job["steps"] if step["name"] == step_name)
                self.assertEqual(
                    step["env"]["BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT"],
                    "${{ vars.SUPPORT_DIAGNOSTICS_ENDPOINT }}",
                )

    def test_package_preserves_dmg_validation_without_write_token(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        certificate_step = next(
            step for step in package["steps"] if step["name"] == "Install signing certificate in an ephemeral keychain"
        )
        certificate_script = certificate_step["run"]
        cleanup_step = next(step for step in package["steps"] if step["name"] == "Remove temporary signing material")
        cleanup_script = cleanup_step["run"]
        package_step = next(step for step in package["steps"] if step["name"] == "Package application for GitHub")

        self.assertEqual(set(package["needs"]), {"policy", "prepare"})
        self.assertEqual(package["environment"], "macos-signing")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertEqual(package["runs-on"], "macos-26")
        self.assertNotIn("self-hosted", str(package))
        self.assertEqual(workflow["env"]["XCODE_VERSION"], "26.5")
        self.assertIn("Xcode_${XCODE_VERSION}.app", str(package))
        self.assertIn("Build version $XCODE_BUILD_VERSION", str(package))
        self.assertIn("xcodegen.zip", str(package))
        self.assertIn("XCODEGEN_SHA256", str(package))
        self.assertIn("--verify-signatures", str(package))
        self.assertIn("--verify-distribution", str(package))
        self.assertIn("BUILD_VERSION", str(package))
        self.assertIn(".build_version", str(package))
        self.assertIn("dmg_sha256", package["outputs"])
        self.assertIn("dmg_size", package["outputs"])
        self.assertIn("SHA256SUMS", str(package))
        self.assertEqual(package_step["env"]["DMG_NAME"], "${{ needs.prepare.outputs.dmg_name }}")
        self.assertNotIn('DMG_NAME="3D-Blu-ray-to-Vision-Pro-$PACKAGE_VERSION.dmg"', str(package))
        self.assertIn("BUILD_KEYCHAIN_PASSWORD", str(package))
        self.assertIn("USER_KEYCHAINS_PATH=", certificate_script)
        self.assertIn('echo "USER_KEYCHAINS_PATH=$USER_KEYCHAINS_PATH"', certificate_script)
        self.assertIn('USER_KEYCHAINS_TMP_PATH="${USER_KEYCHAINS_PATH}.tmp"', certificate_script)
        self.assertIn('> "$USER_KEYCHAINS_TMP_PATH"', certificate_script)
        self.assertIn('mv "$USER_KEYCHAINS_TMP_PATH" "$USER_KEYCHAINS_PATH"', certificate_script)
        self.assertIn("USER_KEYCHAINS=()", certificate_script)
        self.assertIn('if [ "${#USER_KEYCHAINS[@]}" -eq 0 ]; then', certificate_script)
        self.assertIn('security list-keychains -d user -s "$KEYCHAIN_PATH"', certificate_script)
        self.assertIn('security list-keychains -d user -s "$KEYCHAIN_PATH" "${USER_KEYCHAINS[@]}"', certificate_script)
        self.assertLess(
            certificate_script.index('mv "$USER_KEYCHAINS_TMP_PATH" "$USER_KEYCHAINS_PATH"'),
            certificate_script.index('security create-keychain -p "$BUILD_KEYCHAIN_PASSWORD"'),
        )
        self.assertLess(
            certificate_script.index('security list-keychains -d user -s "$KEYCHAIN_PATH"'),
            certificate_script.index("security find-identity"),
        )
        self.assertIn("restore_user_keychains", certificate_script)
        self.assertIn("restore_user_keychains >/dev/null 2>&1 || restore_status=$?", certificate_script)
        self.assertIn("restore_user_keychains", cleanup_script)
        self.assertIn('if [ "${#RESTORE_KEYCHAINS[@]}" -eq 0 ]; then', cleanup_script)
        self.assertIn("security list-keychains -d user -s", cleanup_script)
        self.assertNotIn("restore_user_keychains >/dev/null 2>&1 || true", cleanup_script)
        self.assertLess(
            cleanup_script.index("if ! restore_user_keychains >/dev/null 2>&1; then"),
            cleanup_script.index('security delete-keychain "$KEYCHAIN_PATH"'),
        )
        self.assertIn('security delete-keychain "$KEYCHAIN_PATH"', cleanup_script)
        self.assertIn('exit "$cleanup_status"', cleanup_script)
        self.assertIn("APPLE_APP_PASSWORD", str(package))
        self.assertIn('NOTARY_PROFILE="bd-to-avp-release-$TEAM_ID-$GITHUB_RUN_ID"', str(package))
        notarization_step = next(
            step
            for step in package["steps"]
            if step["name"] == "Store notarization credentials in the ephemeral keychain"
        )
        self.assertIn("Missing required macos-signing notarization secrets:", notarization_step["run"])
        self.assertIn('MISSING_SECRETS+=("KEYCHAIN_PASSWORD")', notarization_step["run"])
        self.assertNotIn('MISSING_SECRETS+=("APPLE_APP_PASSWORD")', notarization_step["run"])
        self.assertIn("python scripts/native_app.py package", str(package))
        self.assertIn("python -m scripts.macos_release", str(package))
        self.assertNotIn("python -m scripts.briefcase_app package", str(package))
        self.assertNotIn("CERTIFICATE_INSTALLER", str(package))
        self.assertNotIn("default-keychain", str(package))
        self.assertNotIn("KEYCHAIN_NAME", str(package))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(package))

    def test_certificate_install_binds_developer_id_identity_to_team_id(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        certificate_step = next(
            step for step in package["steps"] if step["name"] == "Install signing certificate in an ephemeral keychain"
        )
        certificate_script = certificate_step["run"]

        self.assertEqual(certificate_step["env"]["TEAM_ID"], "${{ secrets.TEAM_ID }}")
        self.assertEqual(certificate_step["env"]["PRODUCTION_TEAM_ID"], PRODUCTION_TEAM_ID)
        self.assertEqual(certificate_step["env"]["PRODUCTION_DEV_ID"], PRODUCTION_DEVELOPER_IDENTITY)
        self.assertIn("Missing required macos-signing certificate secrets:", certificate_script)
        self.assertIn('case "$TEAM_ID" in', certificate_script)
        self.assertIn('if [ "${#TEAM_ID}" -ne 10 ]; then', certificate_script)
        self.assertIn('DEV_ID_PREFIX="Developer ID Application: "', certificate_script)
        self.assertIn('DEV_ID_SUFFIX=" ($TEAM_ID)"', certificate_script)
        self.assertIn('DEV_ID_NAME=${DEV_ID#"$DEV_ID_PREFIX"}', certificate_script)
        self.assertIn('DEV_ID_NAME=${DEV_ID_NAME%"$DEV_ID_SUFFIX"}', certificate_script)
        self.assertIn('if [ -z "$DEV_ID_NAME" ]; then', certificate_script)
        self.assertIn("$2 == identity", certificate_script)
        self.assertNotIn('grep -F "$DEV_ID"', certificate_script)
        self.assertLess(certificate_script.index('case "$TEAM_ID" in'), certificate_script.index("security import"))
        self.assertLess(
            certificate_script.index('DEV_ID_SUFFIX=" ($TEAM_ID)"'), certificate_script.index("security import")
        )
        self.assertLess(certificate_script.index("security import"), certificate_script.index("security find-identity"))

        validation_script = certificate_script[
            certificate_script.index('case "$TEAM_ID" in') : certificate_script.index("BUILD_KEYCHAIN_PASSWORD=")
        ]
        self._assert_signing_identity_validation(validation_script)

    def test_package_inspects_signed_app_and_dmg_identity_metadata(self) -> None:
        workflow = load_release_engine()
        package = workflow["jobs"]["package"]
        package_step = next(step for step in package["steps"] if step["name"] == "Package application for GitHub")
        package_script = package_step["run"]

        self.assertEqual(package_step["env"]["TEAM_ID"], "${{ secrets.TEAM_ID }}")
        self.assertEqual(package_step["env"]["PRODUCTION_TEAM_ID"], PRODUCTION_TEAM_ID)
        self.assertEqual(package_step["env"]["PRODUCTION_DEV_ID"], PRODUCTION_DEVELOPER_IDENTITY)
        self.assertIn("verify_codesign_metadata()", package_script)
        self.assertIn('codesign -dv --verbose=4 "$signed_path"', package_script)
        self.assertIn('$1 == "Authority"', package_script)
        self.assertIn('$1 == "TeamIdentifier"', package_script)
        self.assertIn('[ "$signing_authority" != "$DEV_ID" ]', package_script)
        self.assertIn('[ "$team_identifier" != "$PRODUCTION_TEAM_ID" ]', package_script)
        self.assertIn("validate_signing_identity()", package_script)
        self.assertLess(
            package_script.index("git fetch --no-tags origin"), package_script.rindex("validate_signing_identity")
        )
        self.assertLess(
            package_script.rindex("validate_signing_identity"), package_script.index("security unlock-keychain")
        )

        package_command_index = package_script.index("uv run python scripts/native_app.py package")
        app_metadata_index = package_script.index('"$RUNNER_TEMP/bd-to-avp-app-codesign-$GITHUB_RUN_ID.txt"')
        app_notary_index = package_script.index("--log dist/notary/app-notary.json")
        dmg_sign_index = package_script.index('codesign --force --timestamp --sign "$DEV_ID"')
        dmg_metadata_index = package_script.index('"$RUNNER_TEMP/bd-to-avp-dmg-codesign-$GITHUB_RUN_ID.txt"')
        dmg_notary_index = package_script.index("--log dist/notary/dmg-notary.json")
        self.assertLess(package_command_index, app_metadata_index)
        self.assertLess(app_metadata_index, app_notary_index)
        self.assertLess(dmg_sign_index, dmg_metadata_index)
        self.assertLess(dmg_metadata_index, dmg_notary_index)

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
        cleanup_step = next(step for step in package["steps"] if step["name"] == "Remove temporary signing material")
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

    def test_release_is_draft_until_assets_are_redownloaded_and_verified(self) -> None:
        workflow = load_release_engine()
        jobs = workflow["jobs"]

        self.assertEqual(
            set(jobs["create-draft"]["needs"]),
            {"prepare", "package", "attest-package", "compatibility"},
        )
        self.assertIn("release_id", jobs["create-draft"]["outputs"])
        self.assertIn("release_created_at", jobs["create-draft"]["outputs"])
        self.assertIn("--draft", str(jobs["create-draft"]))
        self.assertIn("--target", str(jobs["create-draft"]))
        self.assertIn("--notes-start-tag", str(jobs["create-draft"]))
        self.assertIn("jq -r .name", str(jobs["create-draft"]))
        self.assertNotIn("--fail-on-no-commits", str(jobs["create-draft"]))
        self.assertIn("select(.tag_name == $tag)", str(jobs["create-draft"]))
        self.assertNotIn("--json databaseId", str(jobs["create-draft"]))
        self.assertEqual(
            set(jobs["verify-draft"]["needs"]),
            {"prepare", "package", "create-draft", "publish-appcast"},
        )
        self.assertIn("uploads.github.com", str(jobs["verify-draft"]))
        self.assertIn("releases/assets/$APPCAST_ASSET_ID", str(jobs["verify-draft"]))
        self.assertIn("appcast.xml", str(jobs["verify-draft"]))
        self.assertIn("--verify-distribution", str(jobs["verify-draft"]))
        self.assertEqual(
            set(jobs["publish-release"]["needs"]),
            {"build-python", "create-draft", "prepare", "package", "verify-draft"},
        )
        self.assertIn("needs.create-draft.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("needs.build-python.result == 'success'", jobs["publish-release"]["if"])
        self.assertIn("draft: false", str(jobs["publish-release"]))
        self.assertIn("--method PATCH", str(jobs["publish-release"]))
        self.assertNotIn("DELETE", str(workflow))
        self.assertNotIn("release delete", str(workflow))
        self.assertIn('case "$PRERELEASE:$MAKE_LATEST"', str(jobs["publish-release"]))
        self.assertIn("make_latest: $make_latest", str(jobs["publish-release"]))
        self.assertIn("prerelease: $prerelease", str(jobs["publish-release"]))
        self.assertIn("Published release title does not match", str(jobs["publish-release"]))
        publish_script = jobs["publish-release"]["steps"][0]["run"]
        payload_index = publish_script.index("}' > publish-release.json")
        freshness_index = publish_script.index(
            'MAIN_SHA=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq .object.sha)'
        )
        publication_index = publish_script.index("gh api --method PATCH")
        self.assertLess(payload_index, freshness_index)
        self.assertLess(freshness_index, publication_index)
        self.assertIn("main advanced immediately before publication", publish_script)
        self.assertNotIn("releases/tags/$RELEASE_TAG", str(workflow))
        self.assertIn("releases/$RELEASE_ID", str(workflow))
        self.assertIn("uploads.github.com", str(workflow))
        self.assertIn("releases/assets/$ASSET_ID", str(workflow))
        self.assertIn("releases/download/$RELEASE_TAG/$EXPECTED_DMG_NAME", str(workflow))
        self.assertNotIn('gh release upload "$RELEASE_TAG"', str(workflow))
        self.assertNotIn('gh release download "$RELEASE_TAG"', str(workflow))
        self.assertNotIn('gh release edit "$RELEASE_TAG"', str(workflow))
        self.assertIn("Draft release did not become visible through the API", str(workflow))
        self.assertIn('[ "$TOTAL_ASSET_COUNT" = "3" ]', str(jobs["verify-draft"]))

    def test_release_notes_are_frozen_embedded_and_reverified(self) -> None:
        workflow = load_release_engine()
        jobs = workflow["jobs"]
        create_draft = jobs["create-draft"]
        publish_appcast = jobs["publish-appcast"]
        verify_draft = jobs["verify-draft"]
        publish_release = jobs["publish-release"]

        self.assertIn("release_notes_sha256", create_draft["outputs"])
        self.assertIn("release-notes-source", str(create_draft))
        self.assertIn("jq -j", str(create_draft))
        self.assertIn("draft-release-notes.md", str(create_draft))
        self.assertIn("APPCAST_ASSET_COUNT", str(create_draft))
        self.assertIn("at most one appcast asset", str(create_draft))

        self.assertIn("release-notes-source", str(publish_appcast))
        self.assertIn("Draft release notes artifact digest mismatch", str(publish_appcast))
        self.assertIn("--release-notes-file", str(publish_appcast))
        self.assertIn("--full-release-notes-url", str(publish_appcast))
        self.assertNotIn("--release-notes-url", str(publish_appcast))

        self.assertIn("Draft release notes changed after appcast construction", str(verify_draft))
        self.assertIn("--release-notes-file verified-assets/release-notes.md", str(verify_draft))
        self.assertIn("--full-release-notes-url", str(verify_draft))

        self.assertIn("verify_release_notes_body", str(publish_release))
        self.assertIn("Release notes changed after appcast verification", str(publish_release))
        self.assertIn("--rawfile body release-notes.md", str(publish_release))
        self.assertIn("body: $body", str(publish_release))

    def test_release_package_provenance_is_attested_and_verified(self) -> None:
        workflow = load_release_engine()
        attest = workflow["jobs"]["attest-package"]
        verify = workflow["jobs"]["verify-draft"]

        self.assertEqual(attest["permissions"]["attestations"], "write")
        self.assertEqual(attest["permissions"]["id-token"], "write")
        self.assertIn("actions/attest@", str(attest))
        self.assertNotIn("release-package/*", str(attest))
        self.assertEqual(verify["permissions"]["attestations"], "read")
        self.assertIn("gh attestation verify", str(verify))
        self.assertIn(
            '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release-engine.yml"',
            str(verify),
        )
        self.assertIn('--signer-digest "$GITHUB_SHA"', str(verify))
        self.assertNotIn(
            '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/briefcase.yml"',
            str(verify),
        )
        self.assertIn('--source-digest "$GITHUB_SHA"', str(verify))
        self.assertIn("--deny-self-hosted-runners", str(verify))
        self.assertIn("TOTAL_ASSET_COUNT", str(verify))

    def test_private_key_is_only_exposed_to_read_only_signing_step(self) -> None:
        workflow = load_release_engine()
        publish = workflow["jobs"]["publish-appcast"]
        secret_expression = "${{ secrets.SPARKLE_EDDSA_PRIVATE_KEY }}"
        matching_steps = [step for step in publish["steps"] if secret_expression in str(step)]

        self.assertEqual(publish["environment"], "sparkle-release")
        self.assertEqual(publish["permissions"]["contents"], "read")
        self.assertIn("release-package", str(publish))
        self.assertIn("release_created_at", str(publish))
        self.assertIn("Draft release creation time is missing", str(publish))
        self.assertNotIn("GH_TOKEN", str(publish))
        self.assertNotIn("RELEASE_ID", str(publish))
        self.assertEqual(len(matching_steps), 1)
        self.assertEqual(matching_steps[0]["name"], "Sign DMG and build appcast")
        self.assertIn("Missing required sparkle-release secret: SPARKLE_EDDSA_PRIVATE_KEY", matching_steps[0]["run"])
        self.assertIn("--verify --ed-key-file -", str(matching_steps[0]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["verify-draft"]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["deploy-appcast"]))

    def test_cumulative_appcast_is_a_durable_release_asset(self) -> None:
        workflow = load_release_engine()
        publish = workflow["jobs"]["publish-appcast"]
        verify = workflow["jobs"]["verify-draft"]

        self.assertIn("BASE_SNAPSHOT_TAG", str(publish))
        self.assertIn("bootstrap", str(publish))
        self.assertIn("signed-appcast", str(publish))
        self.assertIn("name=appcast.xml", str(verify))
        self.assertIn("EXPECTED_APPCAST_SHA256", str(verify))
        self.assertIn("validate-snapshot", str(verify))
        self.assertIn("verify-release", str(verify))

    def test_stable_pypi_publish_uses_oidc_trusted_publishing(self) -> None:
        operator = load_workflow("briefcase.yml")
        prerelease = load_workflow("prerelease.yml")
        workflow = load_release_engine()
        build = workflow["jobs"]["build-python"]
        publish = operator["jobs"]["publish-pypi"]
        release_operations = load_github_config()["releaseOperations"]

        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").exists())
        self.assertEqual(publish["needs"], "release")
        self.assertIn("needs.release.result == 'success'", publish["if"])
        self.assertIn("needs.release.outputs.publish_pypi == 'true'", publish["if"])
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["actions"], "read")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertIn("uv build", str(build))
        self.assertNotIn("uv build", str(publish))
        self.assertIn("pypa/gh-action-pypi-publish@", str(publish))
        self.assertIn("attestations", str(publish))
        self.assertEqual(
            next(step for step in publish["steps"] if "pypa/gh-action-pypi-publish@" in step.get("uses", ""))["with"][
                "packages-dir"
            ],
            "python-distributions/dist",
        )
        self.assertIn("SHA256SUMS", str(build))
        self.assertIn("artifact_digest", build["outputs"])
        self.assertIn("artifact_id", build["outputs"])
        self.assertIn("Verify Python distribution transfer", str(publish))
        self.assertIn("shasum -a 256 --check SHA256SUMS", str(publish))
        self.assertIn("PYTHON_ARTIFACT_DIGEST", str(publish))
        self.assertIn("git/ref/heads/main", str(publish))
        self.assertIn("Protected main moved before PyPI publication", str(publish))
        self.assertIn('test "$RELEASE_ROUTE" = "stable"', str(publish))
        self.assertIn('test "$OPERATOR_WORKFLOW_PATH" = ".github/workflows/briefcase.yml"', str(publish))
        self.assertIn("actions/artifacts/$PYTHON_ARTIFACT_ID", str(publish))
        self.assertIn("sha256:$PYTHON_ARTIFACT_DIGEST", str(publish))
        steps = publish["steps"]
        transfer_index = next(
            index for index, step in enumerate(steps) if step["name"] == "Verify Python distribution transfer"
        )
        freshness_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "Reconfirm protected main before PyPI publication"
        )
        publisher_index = next(
            index for index, step in enumerate(steps) if "pypa/gh-action-pypi-publish@" in step.get("uses", "")
        )
        self.assertLess(transfer_index, freshness_index)
        self.assertEqual(freshness_index + 1, publisher_index)
        self.assertIn("Protected main moved immediately before PyPI publication", str(steps[freshness_index]))
        download = next(step for step in publish["steps"] if "actions/download-artifact@" in step.get("uses", ""))
        self.assertEqual(download["with"]["artifact-ids"], "${{ needs.release.outputs.python_artifact_id }}")
        self.assertEqual(download["with"]["merge-multiple"], "true")
        self.assertNotIn("publish-pypi", workflow["jobs"])
        self.assertNotIn("publish-pypi", prerelease["jobs"])
        self.assertNotIn("pypa/gh-action-pypi-publish@", str(prerelease))
        self.assertNotIn("pypa/gh-action-pypi-publish@", str(workflow))
        self.assertNotIn("PYPI_TOKEN", str(operator) + str(prerelease) + str(workflow))
        self.assertEqual(
            release_operations["workflows"]["Stable"]["path"],
            ".github/workflows/briefcase.yml",
        )
        self.assertEqual(
            release_operations["workflows"]["Prerelease"],
            {"path": ".github/workflows/prerelease.yml", "route": "prerelease"},
        )
        self.assertEqual(release_operations["engineWorkflowPath"], ".github/workflows/release-engine.yml")

    def test_release_deploy_uses_resumable_pages_workflow(self) -> None:
        workflow = load_release_engine()
        deploy = workflow["jobs"]["deploy-appcast"]

        self.assertEqual(set(deploy["needs"]), {"prepare", "publish-appcast", "publish-release"})
        self.assertIn("!cancelled()", deploy["if"])
        self.assertIn("needs.prepare.result == 'success'", deploy["if"])
        self.assertIn("needs.publish-appcast.result == 'success'", deploy["if"])
        self.assertIn("needs.publish-release.result == 'success'", deploy["if"])
        self.assertNotIn("build-python", deploy["if"])
        self.assertNotIn("publish_pypi", deploy["if"])
        self.assertEqual(deploy["uses"], "./.github/workflows/sparkle-pages.yml")
        self.assertEqual(deploy["with"]["operation"], "deploy")
        self.assertEqual(
            deploy["with"]["expected_appcast_sha256"],
            "${{ needs.publish-appcast.outputs.appcast_sha256 }}",
        )
        self.assertEqual(
            deploy["with"]["expected_base_release_tag"],
            "${{ needs.prepare.outputs.base_snapshot_tag }}",
        )

    def test_all_release_checkouts_use_dispatch_sha(self) -> None:
        workflow = load_release_engine()
        checkouts = [
            step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("actions/checkout@")
        ]

        self.assertGreaterEqual(len(checkouts), 4)
        for checkout in checkouts:
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_release_actions_are_pinned_to_commit_shas(self) -> None:
        for workflow_name in ("briefcase.yml", "release-engine.yml", "sparkle-pages.yml"):
            workflow = load_workflow(workflow_name)
            action_uses = [
                step["uses"]
                for job in workflow["jobs"].values()
                for step in job.get("steps", [])
                if "uses" in step and not step["uses"].startswith("./")
            ]
            self.assertTrue(action_uses)
            for action in action_uses:
                with self.subTest(workflow=workflow_name, action=action):
                    self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_release_environment_contract_preserves_scoped_secrets(self) -> None:
        environments = load_github_config()["releaseEnvironments"]

        self.assertEqual(
            set(environments),
            {"macos-signing", "sparkle-release", "sparkle-feed-ops", "pypi", "github-pages"},
        )
        self.assertTrue(all(environment["branches"] == ["main"] for environment in environments.values()))
        self.assertEqual(
            {name for name, environment in environments.items() if environment["requiredReview"]},
            {"macos-signing", "sparkle-feed-ops"},
        )
        self.assertEqual(environments["sparkle-release"]["secrets"], ["SPARKLE_EDDSA_PRIVATE_KEY"])
        self.assertEqual(environments["sparkle-feed-ops"]["secrets"], [])
        self.assertEqual(environments["pypi"]["secrets"], [])
        self.assertEqual(environments["github-pages"]["secrets"], [])
        self.assertTrue(
            set(environments["macos-signing"]["secrets"]).isdisjoint(environments["sparkle-release"]["secrets"])
        )


class SparklePagesWorkflowTests(unittest.TestCase):
    def test_pages_workflow_supports_deploy_restore_and_disable(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")
        manual = load_workflow("manage-sparkle-pages.yml")
        dispatch = manual["on"]["workflow_dispatch"]["inputs"]

        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(set(manual["on"]), {"workflow_dispatch"})
        self.assertEqual(manual.get("permissions"), {})
        self.assertEqual(dispatch["operation"]["options"], ["deploy", "restore", "disable"])
        self.assertEqual(manual["jobs"]["approve"]["environment"], "sparkle-feed-ops")
        self.assertEqual(manual["jobs"]["approve"].get("permissions"), {})
        self.assertEqual(manual["jobs"]["manage"]["needs"], "approve")
        self.assertEqual(manual["jobs"]["manage"]["uses"], "./.github/workflows/sparkle-pages.yml")
        self.assertEqual(manual["jobs"]["manage"]["with"]["operation"], "${{ inputs.operation }}")
        self.assertEqual(manual["jobs"]["manage"]["with"]["release_tag"], "${{ inputs.release_tag }}")
        self.assertEqual(
            manual["jobs"]["manage"]["permissions"],
            {"contents": "read", "id-token": "write", "pages": "write"},
        )
        self.assertEqual(workflow["concurrency"]["group"], "sparkle-pages")
        self.assertEqual(
            workflow["concurrency"]["cancel-in-progress"],
            "${{ inputs.operation == 'disable' }}",
        )
        stale_guard = next(
            step
            for step in workflow["jobs"]["deploy"]["steps"]
            if step.get("name") == "Refuse a stale or disabled release deployment"
        )
        self.assertEqual(stale_guard["if"], "inputs.operation == 'deploy'")
        self.assertIn("refs/heads/main", str(workflow["jobs"]["validate"]))

    def test_disable_is_non_destructive_and_restore_uses_release_snapshot(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")
        prepare = workflow["jobs"]["prepare"]

        self.assertEqual(prepare["environment"], "sparkle-release")
        self.assertIn("validate-empty", str(prepare))
        self.assertIn("gh release download", str(prepare))
        self.assertIn("validate-snapshot", str(prepare))
        self.assertIn('--release-tag "$RELEASE_TAG"', str(prepare))
        self.assertNotIn("${RELEASE_TAG#v}", str(prepare))
        self.assertIn("appcast-state.json", str(prepare))
        self.assertIn('status: "disabled"', str(prepare))
        self.assertIn('status: "enabled"', str(prepare))
        self.assertNotIn("gh release upload", str(prepare))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(workflow))
        self.assertEqual(workflow["jobs"]["deploy"]["environment"]["name"], "github-pages")
        self.assertIn("Verify the live Pages state", str(workflow["jobs"]["deploy"]))


if __name__ == "__main__":
    unittest.main()
