import importlib
import json
import subprocess
import sys
import unittest

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def load_workflow(name: str) -> dict:
    with (REPO_ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


def load_github_config() -> dict:
    with (REPO_ROOT / ".github" / "github.json").open(encoding="utf-8") as handle:
        return json.load(handle)


class ReleaseWorkflowTests(unittest.TestCase):
    def test_sparkle_bundle_uses_importable_module_entrypoint(self) -> None:
        workflow = load_workflow("briefcase.yml")
        workflow_text = str(workflow)
        ci_text = str(load_workflow("ci.yml"))
        smoke_text = (REPO_ROOT / "docs" / "release-smoke.md").read_text(encoding="utf-8")

        self.assertNotIn("python scripts/sparkle_bundle.py", workflow_text)
        self.assertEqual(workflow_text.count("python -m scripts.sparkle_bundle"), 5)
        self.assertNotIn("python scripts/sparkle_bundle.py", smoke_text)
        self.assertNotIn("python scripts/briefcase_app.py", workflow_text + ci_text)
        self.assertEqual((workflow_text + ci_text).count("python -m scripts.briefcase_app"), 5)

        result = subprocess.run(
            [sys.executable, "-S", "-m", "scripts.sparkle_bundle", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_is_manual_only_and_packages_github_sha_from_main(self) -> None:
        workflow = load_workflow("briefcase.yml")
        prepare = workflow["jobs"]["prepare"]
        package = workflow["jobs"]["package"]

        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["env"]["GH_REPO"], "${{ github.repository }}")
        self.assertNotIn("source_ref", str(workflow))
        self.assertEqual(workflow["concurrency"]["group"], "release")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
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

    def test_release_metadata_is_derived_by_tested_python(self) -> None:
        workflow = load_workflow("briefcase.yml")
        prepare = workflow["jobs"]["prepare"]
        release_history = next(step for step in prepare["steps"] if step.get("id") == "release_history")

        self.assertIn("python scripts/release.py metadata", str(prepare))
        self.assertNotIn("awk -v version", str(workflow))
        self.assertNotIn("release_tag_suffix", str(workflow))
        self.assertIn("publish_pypi", prepare["outputs"])
        self.assertIn("previous_release_tag", prepare["outputs"])
        self.assertIn("python scripts/release.py notes-base", str(release_history))
        self.assertIn("check-release", str(prepare))
        self.assertNotIn("sort_by(.published_at)", str(release_history))
        self.assertNotIn("git merge-base --is-ancestor", str(release_history))
        self.assertIn("live-appcast.xml", str(prepare))
        self.assertIn("LATEST_SNAPSHOT_TAG", str(prepare))
        self.assertIn("base_snapshot_tag", prepare["outputs"])
        self.assertIn("appcast-state.json", str(prepare))

    def test_package_preserves_dmg_validation_without_write_token(self) -> None:
        workflow = load_workflow("briefcase.yml")
        package = workflow["jobs"]["package"]

        self.assertEqual(package["needs"], "prepare")
        self.assertEqual(package["environment"], "macos-signing")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertIn("--verify-signatures", str(package))
        self.assertIn("--verify-distribution", str(package))
        self.assertIn("BUILD_VERSION", str(package))
        self.assertIn(".build_version", str(package))
        self.assertIn("dmg_sha256", package["outputs"])
        self.assertIn("dmg_size", package["outputs"])
        self.assertIn("SHA256SUMS", str(package))
        self.assertIn("DMG_NAME=${ORIGINAL_DMG_NAME// /-}", str(package))
        self.assertIn("BUILD_KEYCHAIN_PASSWORD", str(package))
        self.assertIn("APPLE_APP_PASSWORD", str(package))
        self.assertIn('NOTARY_PROFILE="briefcase-macOS-$TEAM_ID"', str(package))
        self.assertNotIn("KEYCHAIN_NAME", str(package))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(package))

    def test_release_is_draft_until_assets_are_redownloaded_and_verified(self) -> None:
        workflow = load_workflow("briefcase.yml")
        jobs = workflow["jobs"]

        self.assertEqual(
            set(jobs["create-draft"]["needs"]),
            {"prepare", "package", "attest-package"},
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
        workflow = load_workflow("briefcase.yml")
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
        workflow = load_workflow("briefcase.yml")
        attest = workflow["jobs"]["attest-package"]
        verify = workflow["jobs"]["verify-draft"]

        self.assertEqual(attest["permissions"]["attestations"], "write")
        self.assertEqual(attest["permissions"]["id-token"], "write")
        self.assertIn("actions/attest@", str(attest))
        self.assertNotIn("release-package/*", str(attest))
        self.assertEqual(verify["permissions"]["attestations"], "read")
        self.assertIn("gh attestation verify", str(verify))
        self.assertIn('--source-digest "$GITHUB_SHA"', str(verify))
        self.assertIn("TOTAL_ASSET_COUNT", str(verify))

    def test_private_key_is_only_exposed_to_read_only_signing_step(self) -> None:
        workflow = load_workflow("briefcase.yml")
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
        self.assertIn("--verify --ed-key-file -", str(matching_steps[0]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["verify-draft"]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["deploy-appcast"]))

    def test_cumulative_appcast_is_a_durable_release_asset(self) -> None:
        workflow = load_workflow("briefcase.yml")
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
        workflow = load_workflow("briefcase.yml")
        build = workflow["jobs"]["build-python"]
        publish = workflow["jobs"]["publish-pypi"]

        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").exists())
        self.assertIn("needs.build-python.result == 'success'", publish["if"])
        self.assertIn("needs.publish-release.result == 'success'", publish["if"])
        self.assertIn("needs.prepare.outputs.publish_pypi == 'true'", publish["if"])
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertEqual(set(publish["needs"]), {"build-python", "prepare", "publish-release"})
        self.assertIn("uv build", str(build))
        self.assertNotIn("uv build", str(publish))
        self.assertIn("pypa/gh-action-pypi-publish@", str(publish))
        self.assertIn("attestations", str(publish))
        self.assertNotIn("PYPI_TOKEN", str(workflow))

    def test_release_deploy_uses_resumable_pages_workflow(self) -> None:
        workflow = load_workflow("briefcase.yml")
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
        workflow = load_workflow("briefcase.yml")
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
        for workflow_name in ("briefcase.yml", "sparkle-pages.yml"):
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
        self.assertIn("appcast-state.json", str(prepare))
        self.assertIn('status: "disabled"', str(prepare))
        self.assertIn('status: "enabled"', str(prepare))
        self.assertNotIn("gh release upload", str(prepare))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(workflow))
        self.assertEqual(workflow["jobs"]["deploy"]["environment"]["name"], "github-pages")
        self.assertIn("Verify the live Pages state", str(workflow["jobs"]["deploy"]))


if __name__ == "__main__":
    unittest.main()
