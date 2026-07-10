import importlib
import unittest

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def load_workflow(name: str) -> dict:
    with (REPO_ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_is_manual_only_and_packages_github_sha_from_main(self) -> None:
        workflow = load_workflow("briefcase.yml")
        prepare = workflow["jobs"]["prepare"]

        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertNotIn("source_ref", str(workflow))
        self.assertEqual(workflow["concurrency"]["group"], "release")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        checkout = prepare["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertIn("refs/heads/main", str(prepare))
        self.assertIn("refs/remotes/origin/main", str(prepare))
        self.assertNotIn("refs/heads/release", str(workflow))

    def test_release_metadata_is_derived_by_tested_python(self) -> None:
        workflow = load_workflow("briefcase.yml")
        prepare = workflow["jobs"]["prepare"]

        self.assertIn("python scripts/release.py metadata", str(prepare))
        self.assertNotIn("awk -v version", str(workflow))
        self.assertNotIn("release_tag_suffix", str(workflow))
        self.assertIn("publish_pypi", prepare["outputs"])
        self.assertIn("check-release", str(prepare))
        self.assertIn("live-appcast.xml", str(prepare))
        self.assertIn("LATEST_SNAPSHOT_TAG", str(prepare))

    def test_package_preserves_dmg_validation_without_write_token(self) -> None:
        workflow = load_workflow("briefcase.yml")
        package = workflow["jobs"]["package"]

        self.assertEqual(package["needs"], "prepare")
        self.assertEqual(package["permissions"]["contents"], "read")
        self.assertIn("--verify-signatures", str(package))
        self.assertIn("--verify-distribution", str(package))
        self.assertIn("dmg_sha256", package["outputs"])
        self.assertIn("dmg_size", package["outputs"])
        self.assertIn("SHA256SUMS", str(package))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(package))

    def test_release_is_draft_until_assets_are_redownloaded_and_verified(self) -> None:
        workflow = load_workflow("briefcase.yml")
        jobs = workflow["jobs"]

        self.assertEqual(set(jobs["create-draft"]["needs"]), {"prepare", "package"})
        self.assertIn("--draft", str(jobs["create-draft"]))
        self.assertIn("--target", str(jobs["create-draft"]))
        self.assertEqual(
            set(jobs["verify-draft"]["needs"]),
            {"prepare", "package", "create-draft", "publish-appcast"},
        )
        self.assertIn("gh release upload", str(jobs["verify-draft"]))
        self.assertIn("gh release download", str(jobs["verify-draft"]))
        self.assertIn("appcast.xml", str(jobs["verify-draft"]))
        self.assertIn("--verify-distribution", str(jobs["verify-draft"]))
        self.assertEqual(set(jobs["publish-release"]["needs"]), {"prepare", "package", "verify-draft"})
        self.assertIn("--draft=false", str(jobs["publish-release"]))

    def test_private_key_is_only_exposed_to_read_only_signing_step(self) -> None:
        workflow = load_workflow("briefcase.yml")
        publish = workflow["jobs"]["publish-appcast"]
        secret_expression = "${{ secrets.SPARKLE_EDDSA_PRIVATE_KEY }}"
        matching_steps = [step for step in publish["steps"] if secret_expression in str(step)]

        self.assertEqual(publish["environment"], "sparkle-release")
        self.assertEqual(publish["permissions"]["contents"], "read")
        self.assertEqual(len(matching_steps), 1)
        self.assertEqual(matching_steps[0]["name"], "Sign DMG and build appcast")
        self.assertIn("--verify --ed-key-file -", str(matching_steps[0]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["verify-draft"]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["deploy-appcast"]))

    def test_cumulative_appcast_is_a_durable_release_asset(self) -> None:
        workflow = load_workflow("briefcase.yml")
        publish = workflow["jobs"]["publish-appcast"]
        verify = workflow["jobs"]["verify-draft"]

        self.assertIn('select(any(.assets[]?; .name == "appcast.xml"))', str(publish))
        self.assertIn("LATEST_SNAPSHOT_TAG", str(publish))
        self.assertIn("signed-appcast", str(publish))
        self.assertIn("gh release upload", str(verify))
        self.assertIn("EXPECTED_APPCAST_SHA256", str(verify))
        self.assertIn("validate-snapshot", str(verify))
        self.assertIn("verify-release", str(verify))

    def test_stable_pypi_publish_uses_oidc_trusted_publishing(self) -> None:
        workflow = load_workflow("briefcase.yml")
        publish = workflow["jobs"]["publish-pypi"]

        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml").exists())
        self.assertEqual(publish["if"], "needs.prepare.outputs.publish_pypi == 'true'")
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertIn("--trusted-publishing always", str(publish))
        self.assertIn("--check-url", str(publish))
        self.assertNotIn("PYPI_TOKEN", str(workflow))

    def test_release_deploy_uses_resumable_pages_workflow(self) -> None:
        workflow = load_workflow("briefcase.yml")
        deploy = workflow["jobs"]["deploy-appcast"]

        self.assertEqual(deploy["uses"], "./.github/workflows/sparkle-pages.yml")
        self.assertEqual(deploy["with"]["operation"], "deploy")
        self.assertEqual(
            deploy["with"]["expected_appcast_sha256"],
            "${{ needs.publish-appcast.outputs.appcast_sha256 }}",
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


class SparklePagesWorkflowTests(unittest.TestCase):
    def test_pages_workflow_supports_deploy_restore_and_disable(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")
        dispatch = workflow["on"]["workflow_dispatch"]["inputs"]

        self.assertEqual(set(workflow["on"]), {"workflow_call", "workflow_dispatch"})
        self.assertEqual(dispatch["operation"]["options"], ["deploy", "restore", "disable"])
        self.assertEqual(workflow["concurrency"]["group"], "sparkle-pages")
        self.assertEqual(
            workflow["concurrency"]["cancel-in-progress"],
            "${{ inputs.operation == 'disable' }}",
        )
        self.assertIn("refs/heads/main", str(workflow["jobs"]["validate"]))

    def test_disable_is_non_destructive_and_restore_uses_release_snapshot(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")
        prepare = workflow["jobs"]["prepare"]

        self.assertEqual(prepare["environment"], "sparkle-release")
        self.assertIn("validate-empty", str(prepare))
        self.assertIn("gh release download", str(prepare))
        self.assertIn("validate-snapshot", str(prepare))
        self.assertNotIn("gh release upload", str(prepare))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(workflow))
        self.assertEqual(workflow["jobs"]["deploy"]["environment"]["name"], "github-pages")


if __name__ == "__main__":
    unittest.main()
