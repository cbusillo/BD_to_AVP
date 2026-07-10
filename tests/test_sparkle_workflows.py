import importlib
import unittest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
yaml = importlib.import_module("yaml")


def load_workflow(name: str) -> dict:
    with (REPO_ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


class SparkleWorkflowTests(unittest.TestCase):
    def test_release_workflow_uses_protected_appcast_job(self) -> None:
        workflow = load_workflow("briefcase.yml")
        jobs = workflow["jobs"]
        publish = jobs["publish-appcast"]
        deploy = jobs["deploy-appcast"]

        self.assertEqual(publish["environment"], "sparkle-release")
        self.assertEqual(publish["needs"], "package")
        self.assertEqual(deploy["needs"], "publish-appcast")
        self.assertEqual(deploy["environment"]["name"], "github-pages")
        self.assertEqual(publish["permissions"]["contents"], "read")
        self.assertEqual(deploy["permissions"]["pages"], "write")
        checkout = publish["steps"][0]
        self.assertEqual(checkout["name"], "Checkout protected release tooling")
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertNotIn("target_commitish", str(publish))
        self.assertIn("--release-artifact", str(publish))
        self.assertIn("--verify-distribution", str(publish))
        self.assertIn("EXPECTED_DMG_SHA256", str(publish))

    def test_private_key_is_only_exposed_to_signing_step(self) -> None:
        workflow = load_workflow("briefcase.yml")
        publish_steps = workflow["jobs"]["publish-appcast"]["steps"]
        secret_expression = "${{ secrets.SPARKLE_EDDSA_PRIVATE_KEY }}"
        matching_steps = [step for step in publish_steps if secret_expression in str(step)]

        self.assertEqual(len(matching_steps), 1)
        self.assertEqual(matching_steps[0]["name"], "Sign DMG and build appcast")
        self.assertNotIn(secret_expression, str(workflow["jobs"]["package"]))
        self.assertNotIn(secret_expression, str(workflow["jobs"]["deploy-appcast"]))

    def test_release_assets_are_immutable_and_preflighted(self) -> None:
        workflow = load_workflow("briefcase.yml")
        package = workflow["jobs"]["package"]
        release_step = next(step for step in package["steps"] if step["name"] == "Create Release")

        self.assertEqual(release_step["with"]["overwrite_files"], "false")
        self.assertIn("check-release", str(package))
        self.assertIn("gh release view", str(package))
        self.assertIn("dmg_sha256", package["outputs"])

    def test_workflow_outputs_use_random_multiline_delimiters(self) -> None:
        workflow_text = (REPO_ROOT / ".github" / "workflows" / "briefcase.yml").read_text(encoding="utf-8")

        self.assertIn("MESSAGES_$(openssl rand -hex 16)", workflow_text)
        self.assertIn("NOTES_$(openssl rand -hex 16)", workflow_text)
        self.assertNotIn("<<EOF", workflow_text)

    def test_emergency_workflow_is_manual_only_and_deploys_empty_feed(self) -> None:
        workflow = load_workflow("sparkle-pages.yml")

        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["jobs"]["build"]["environment"], "sparkle-release")
        self.assertEqual(workflow["jobs"]["build"]["if"], "github.ref == 'refs/heads/release'")
        self.assertEqual(workflow["jobs"]["deploy"]["environment"]["name"], "github-pages")
        self.assertIn("Emergency feed must be a valid empty appcast", str(workflow))
        self.assertNotIn("SPARKLE_EDDSA_PRIVATE_KEY", str(workflow))


if __name__ == "__main__":
    unittest.main()
