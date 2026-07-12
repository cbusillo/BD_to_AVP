import plistlib
import tempfile
import unittest

from pathlib import Path

from scripts.native_app import (
    NATIVE_APP_NAME,
    NATIVE_BUNDLE_IDENTIFIER,
    NATIVE_EXECUTABLE_NAME,
    NATIVE_PRODUCT_NAME,
    MACOS_ROOT,
    PROJECT_PATH,
    REPO_ROOT,
    SCHEME,
    validate_smoke_events,
    verify_native_binary_paths,
    verify_package_paths,
    verify_product_identity,
    verify_product_source_copy,
)


class NativeAppPackagingTests(unittest.TestCase):
    def test_uses_release_grade_product_identity(self) -> None:
        self.assertEqual(PROJECT_PATH.name, "BluRayToVisionPro.xcodeproj")
        self.assertEqual(SCHEME, "BluRayToVisionPro")
        self.assertEqual(NATIVE_APP_NAME, "3D Blu-ray to Vision Pro.app")
        self.assertEqual(NATIVE_EXECUTABLE_NAME, NATIVE_PRODUCT_NAME)
        self.assertEqual(NATIVE_BUNDLE_IDENTIFIER, "com.shinycomputers.bd-to-avp")

    def test_uses_one_native_settings_scene_and_release_grade_source_groups(self) -> None:
        project_spec = (MACOS_ROOT / "project.yml").read_text(encoding="utf-8")
        app_source = (MACOS_ROOT / "BluRayToVisionPro" / "App" / "BluRayToVisionProApp.swift").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("BDToAVPNative", project_spec)
        self.assertEqual(app_source.count('Window("Settings", id: AppWindowID.settings)'), 1)
        self.assertIn("SettingsView(", app_source)
        self.assertIn("profileStore: profileStore", app_source)
        self.assertIn("capabilities: capabilities", app_source)
        self.assertIn("CommandGroup(replacing: .appSettings)", app_source)
        self.assertIn('openWindow(id: AppWindowID.settings)', app_source)
        self.assertIn(".windowResizability(.contentMinSize)", app_source)

    def test_native_ui_keeps_discs_primary_and_original_job_controls_visible(self) -> None:
        source_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "SourceWorkspaceView.swift").read_text(
            encoding="utf-8"
        )
        setup_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "ConversionSetupView.swift").read_text(
            encoding="utf-8"
        )
        encoding_editor = (
            MACOS_ROOT / "BluRayToVisionPro" / "Views" / "EncodingOptionsEditor.swift"
        ).read_text(encoding="utf-8")
        conversion_ui = setup_view + encoding_editor

        self.assertIn("Convert a 3D Blu-ray Disc", source_view)
        self.assertIn("Import MTS or M2TS transport stream", source_view)
        self.assertLess(
            source_view.index("Convert a 3D Blu-ray Disc"),
            source_view.index("Import MTS or M2TS transport stream"),
        )
        for label in (
            "HEVC quality",
            "Left / right bitrate",
            "AI FX upscale to 2\u00d7 resolution",
            "Crop black bars",
            "Swap left and right eyes",
            "Audio handling",
            "Preferred language",
            "Start stage",
            "Keep durable stage files",
            "Continue processing after recoverable errors",
            "Use software encoder",
            "Overwrite an existing output file",
            "Remove original after success",
            "Show generated commands in activity",
        ):
            self.assertIn(label, conversion_ui)

    def test_profile_settings_remain_resizable_and_scrollable_when_read_only(self) -> None:
        settings_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "SettingsView.swift").read_text(
            encoding="utf-8"
        )
        encoding_editor = (
            MACOS_ROOT / "BluRayToVisionPro" / "Views" / "EncodingOptionsEditor.swift"
        ).read_text(encoding="utf-8")

        self.assertNotIn(".frame(width: 900, height: 600)", settings_view)
        self.assertIn("maxWidth: .infinity", settings_view)
        self.assertIn("maxHeight: .infinity", settings_view)
        self.assertIn("ProfileEncodingSummaryView(", settings_view)
        self.assertIn("ScrollView {", settings_view)
        self.assertNotIn("isEditable", encoding_editor)

    def test_product_copy_has_no_internal_labels(self) -> None:
        verify_product_source_copy()

    def test_rejects_repository_path_in_native_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable_path = Path(temporary_directory) / "native"
            executable_path.write_bytes(b"header\0" + str(REPO_ROOT).encode() + b"/Sources/App.swift\0")

            with self.assertRaisesRegex(RuntimeError, "development repository path"):
                verify_native_binary_paths(executable_path)

    def test_rejects_repository_path_anywhere_in_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            leaked_file = app_path / "Contents" / "Resources" / "generated-tool"
            leaked_file.parent.mkdir(parents=True)
            leaked_file.write_bytes(b"#!" + str(REPO_ROOT).encode() + b"/.venv/bin/python\n")

            with self.assertRaisesRegex(RuntimeError, "development repository paths"):
                verify_package_paths(app_path)

    def test_accepts_release_product_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    {
                        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
                        "CFBundleName": NATIVE_PRODUCT_NAME,
                        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
                        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
                        "MainModule": "bd_to_avp.worker",
                        "BluRayToVisionProEngineBundled": True,
                    },
                    info_file,
                )

            verify_product_identity(app_path)

    def test_rejects_development_metadata_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    {
                        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
                        "CFBundleName": NATIVE_PRODUCT_NAME,
                        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
                        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
                        "MainModule": "bd_to_avp.worker",
                        "BluRayToVisionProEngineBundled": True,
                        "BDToAVPDevelopmentRepositoryRoot": "/private/tmp/source",
                    },
                    info_file,
                )

            with self.assertRaisesRegex(RuntimeError, "development repository metadata"):
                verify_product_identity(app_path)

    def test_rejects_repository_documents_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    {
                        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
                        "CFBundleName": NATIVE_PRODUCT_NAME,
                        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
                        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
                        "MainModule": "bd_to_avp.worker",
                        "BluRayToVisionProEngineBundled": True,
                    },
                    info_file,
                )
            internal_document = app_path / "Contents" / "Resources" / "app" / "README.md"
            internal_document.parent.mkdir(parents=True)
            internal_document.write_text("internal", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "repository-only documents"):
                verify_product_identity(app_path)

    def test_accepts_complete_worker_smoke_contract(self) -> None:
        job_id = "97456c4a-f3c5-44e4-a548-0bd833ead4bb"
        events: list[object] = [
            {
                "protocol_version": 1,
                "type": "worker.ready",
                "job_id": job_id,
                "sequence": 0,
                "payload": {"process_group_id": 123},
            },
            {
                "protocol_version": 1,
                "type": "job.started",
                "job_id": job_id,
                "sequence": 1,
                "payload": {},
            },
            {
                "protocol_version": 1,
                "type": "stage.started",
                "job_id": job_id,
                "sequence": 2,
                "payload": {},
            },
            {
                "protocol_version": 1,
                "type": "job.completed",
                "job_id": job_id,
                "sequence": 3,
                "payload": {
                    "result": {
                        "resolution": "160x90",
                        "frame_rate": "24/1",
                        "interlaced": False,
                        "size_bytes": 1024,
                    }
                },
            },
        ]

        validate_smoke_events(events, job_id)

    def test_rejects_wrong_job_or_result(self) -> None:
        events: list[object] = [
            {
                "protocol_version": 1,
                "type": event_type,
                "job_id": "wrong-job",
                "sequence": sequence,
                "payload": {
                    "result": {
                        "resolution": "bad",
                        "frame_rate": "24/1",
                        "interlaced": False,
                        "size_bytes": 1,
                    }
                }
                if event_type == "job.completed"
                else {},
            }
            for sequence, event_type in enumerate(["worker.ready", "job.started", "stage.started", "job.completed"])
        ]

        with self.assertRaises(ValueError):
            validate_smoke_events(events, "expected-job")


if __name__ == "__main__":
    unittest.main()
