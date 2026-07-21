import importlib
import json
import plistlib
import subprocess
import tempfile
import tomllib
import unittest

from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from bd_to_avp.worker.protocol import PROTOCOL_VERSION
from scripts.native_app import (
    NATIVE_APP_NAME,
    NATIVE_BUNDLE_IDENTIFIER,
    NATIVE_BUILD_VERSION,
    NATIVE_EXECUTABLE_NAME,
    NATIVE_MINIMUM_SYSTEM_VERSION,
    NATIVE_PACKAGE_CONFIGURATION,
    NATIVE_PRODUCT_NAME,
    NATIVE_SHORT_VERSION,
    NATIVE_UPDATE_INFO,
    SUPPORT_DIAGNOSTICS_ENDPOINT_ENV,
    SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY,
    WORKER_PROTOCOL_VERSION,
    MACOS_ROOT,
    PROJECT_PATH,
    REPO_ROOT,
    SCHEME,
    native_build_settings,
    minimum_macos_versions,
    parse_args,
    sign_package,
    smoke_packaged_native_app,
    smoke_packaged_worker,
    validate_smoke_events,
    verify_native_binary_paths,
    verify_mach_o_minimum_system_versions,
    verify_package_paths,
    verify_product_identity,
    verify_product_source_copy,
)

yaml = importlib.import_module("yaml")


def canonical_ffprobe_event(job_id: str) -> dict[str, object]:
    return {
        "schema": "bd_to_avp.observability",
        "schema_version": 1,
        "emitter": "worker",
        "stream_id": job_id,
        "sequence": 0,
        "occurred_at": "2026-07-19T00:00:00.000Z",
        "kind": "tool.started",
        "severity": "info",
        "privacy": "private",
        "redaction": "raw",
        "context": {
            "correlation": {"job_id": job_id},
            "tool": {"id": "ffprobe"},
        },
        "data": {},
    }


def production_info(*, support_diagnostics_endpoint: object = "https://support.example") -> dict[str, object]:
    return {
        "CFBundleDisplayName": NATIVE_PRODUCT_NAME,
        "CFBundleName": NATIVE_PRODUCT_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE_NAME,
        "CFBundleIdentifier": NATIVE_BUNDLE_IDENTIFIER,
        "CFBundleShortVersionString": NATIVE_SHORT_VERSION,
        "CFBundleVersion": NATIVE_BUILD_VERSION,
        "LSMinimumSystemVersion": NATIVE_MINIMUM_SYSTEM_VERSION,
        "MainModule": "bd_to_avp.worker",
        "BluRayToVisionProEngineBundled": True,
        **NATIVE_UPDATE_INFO,
        SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY: support_diagnostics_endpoint,
    }


class NativeAppPackagingTests(unittest.TestCase):
    def test_worker_smoke_uses_current_protocol_version(self) -> None:
        self.assertEqual(WORKER_PROTOCOL_VERSION, PROTOCOL_VERSION)

    def test_uses_production_identity(self) -> None:
        self.assertEqual(PROJECT_PATH.name, "BluRayToVisionPro.xcodeproj")
        self.assertEqual(SCHEME, "BluRayToVisionPro")
        self.assertEqual(NATIVE_PACKAGE_CONFIGURATION, "Release")
        self.assertEqual(NATIVE_APP_NAME, "3D Blu-ray to Vision Pro.app")
        self.assertEqual(NATIVE_EXECUTABLE_NAME, NATIVE_PRODUCT_NAME)
        self.assertEqual(NATIVE_BUNDLE_IDENTIFIER, "com.shinycomputers.bd-to-avp")
        self.assertEqual(NATIVE_SHORT_VERSION, "0.3.0b4")
        self.assertEqual(NATIVE_BUILD_VERSION, "149")
        self.assertEqual(NATIVE_MINIMUM_SYSTEM_VERSION, "26.0")

    def test_uses_one_native_settings_scene_and_release_grade_source_groups(self) -> None:
        project_spec = (MACOS_ROOT / "project.yml").read_text(encoding="utf-8")
        project = yaml.load(project_spec, Loader=yaml.BaseLoader)
        target_settings = project["targets"]["BluRayToVisionPro"]["settings"]
        debug_settings = target_settings["configs"]["Debug"]
        release_settings = target_settings["configs"]["Release"]
        sparkle_manifest = tomllib.loads((REPO_ROOT / "vendor" / "sparkle-macos.toml").read_text(encoding="utf-8"))
        briefcase_info = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["briefcase"][
            "app"
        ]["bd-to-avp"]["macOS"]["info"]
        debug_info = plistlib.loads((MACOS_ROOT / "BluRayToVisionPro" / "Info.plist").read_bytes())
        release_info = plistlib.loads((MACOS_ROOT / "BluRayToVisionPro" / "Info-Release.plist").read_bytes())
        app_source = (MACOS_ROOT / "BluRayToVisionPro" / "App" / "BluRayToVisionProApp.swift").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("BDToAVPNative", project_spec)
        self.assertEqual(app_source.count('Window("Settings", id: AppWindowID.settings)'), 1)
        self.assertIn("SettingsView(", app_source)
        self.assertIn("profileStore: profileStore", app_source)
        self.assertIn("capabilities: capabilities", app_source)
        self.assertIn("updater: updater", app_source)
        self.assertIn("UpdateCommands(updater: updater)", app_source)
        self.assertIn("UpdateController(installPostponer: workCoordinator)", app_source)
        self.assertIn("CommandGroup(replacing: .appSettings)", app_source)
        self.assertIn("CommandGroup(after: .help)", app_source)
        self.assertIn('Picker("Update Channel"', app_source)
        self.assertIn("openWindow(id: AppWindowID.settings)", app_source)
        self.assertIn(".windowResizability(.contentMinSize)", app_source)
        self.assertEqual(target_settings["base"]["CURRENT_PROJECT_VERSION"], NATIVE_BUILD_VERSION)
        self.assertNotIn("CURRENT_PROJECT_VERSION", release_settings)
        self.assertEqual(project["options"]["deploymentTarget"]["macOS"], NATIVE_MINIMUM_SYSTEM_VERSION)
        self.assertEqual(project["settings"]["base"]["MACOSX_DEPLOYMENT_TARGET"], NATIVE_MINIMUM_SYSTEM_VERSION)
        self.assertEqual(target_settings["base"]["MARKETING_VERSION"], NATIVE_SHORT_VERSION)
        self.assertEqual(target_settings["base"]["PRODUCT_BUNDLE_IDENTIFIER"], NATIVE_BUNDLE_IDENTIFIER)
        self.assertEqual(target_settings["base"]["PRODUCT_NAME"], NATIVE_PRODUCT_NAME)
        self.assertEqual(debug_settings["PRODUCT_BUNDLE_IDENTIFIER"], "com.shinycomputers.bd-to-avp.development")
        self.assertEqual(debug_settings["PRODUCT_NAME"], "3D Blu-ray to Vision Pro Development")
        self.assertNotIn("Preview: release", project_spec)
        self.assertNotIn("Native Preview", project_spec)
        self.assertNotIn(".native-preview", project_spec)
        self.assertEqual(project["packages"]["Sparkle"]["exactVersion"], sparkle_manifest["version"])
        self.assertIn({"package": "Sparkle"}, project["targets"]["BluRayToVisionPro"]["dependencies"])
        update_keys = {
            "BDToAVPDistributionChannel",
            "SUAllowsAutomaticUpdates",
            "SUFeedURL",
            "SUPublicEDKey",
            "SUVerifyUpdateBeforeExtraction",
        }
        for key in update_keys:
            self.assertNotIn(key, debug_info)
            self.assertEqual(release_info[key], briefcase_info[key])
        self.assertEqual(debug_info, {key: value for key, value in release_info.items() if key not in update_keys})
        self.assertEqual(release_settings["INFOPLIST_FILE"], "BluRayToVisionPro/Info-Release.plist")
        self.assertNotIn("SUEnableAutomaticChecks", release_info)

    def test_native_ui_keeps_discs_primary_and_original_job_controls_visible(self) -> None:
        source_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "SourceWorkspaceView.swift").read_text(
            encoding="utf-8"
        )
        setup_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "ConversionSetupView.swift").read_text(
            encoding="utf-8"
        )
        encoding_editor = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "EncodingOptionsEditor.swift").read_text(
            encoding="utf-8"
        )
        language_picker = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "LanguagePickerField.swift").read_text(
            encoding="utf-8"
        )
        conversion_options = (MACOS_ROOT / "BluRayToVisionPro" / "Models" / "ConversionOptions.swift").read_text(
            encoding="utf-8"
        )
        conversion_ui = setup_view + encoding_editor + language_picker + conversion_options

        self.assertIn('.accessibilityLabel("\\(purpose.label): \\(selection.displayName)")', language_picker)
        self.assertNotIn('.accessibilityLabel("Preferred language:', language_picker)

        self.assertIn("Convert a 3D Blu-ray Disc", source_view)
        self.assertIn("Import MTS or M2TS transport stream", source_view)
        self.assertIn(".disabled(outputControlsLocked)", source_view)
        self.assertIn("state.phase.isRunning || state.phase == .decisionRequired", source_view)
        self.assertLess(
            source_view.index("Convert a 3D Blu-ray Disc"),
            source_view.index("Import MTS or M2TS transport stream"),
        )
        for label in (
            "HEVC quality",
            "Left / right bitrate",
            "Video Output",
            "AV1 quality",
            "AI FX upscale to 2\u00d7 resolution",
            "Crop black bars",
            "Swap left and right eyes",
            "Audio handling",
            "Audio languages",
            "All Languages",
            "Preferred Language Only",
            "Audio language",
            "Subtitle handling",
            "Subtitle language",
            "Start stage",
            "Keep durable stage files",
            "Continue processing after recoverable errors",
            "Use software HEVC encoder",
            "Overwrite an existing output file",
            "Remove original after success",
            "Show generated commands in activity",
        ):
            self.assertIn(label, conversion_ui)

        self.assertIn('Section("Subtitles")', encoding_editor)
        self.assertNotIn("Subtitles and Languages", encoding_editor)
        self.assertIn('LabeledContent("Video")', source_view)
        self.assertIn('LabeledContent("Audio")', source_view)
        self.assertIn('LabeledContent("Subtitles")', source_view)
        self.assertIn("Text(options.encoding.videoSummary)", source_view)
        self.assertIn("Text(options.encoding.audioSummary)", source_view)
        self.assertIn("Opens a searchable list of audio languages", language_picker)
        self.assertIn("Opens a searchable list of subtitle languages", language_picker)
        self.assertIn("Search audio languages", language_picker)
        self.assertIn("Search subtitle languages", language_picker)
        self.assertNotIn("All audio tracks from the source are included, regardless of language.", encoding_editor)
        self.assertNotIn("Subtitle language choices do not filter audio tracks.", encoding_editor)

    def test_profile_settings_remain_resizable_and_scrollable_when_read_only(self) -> None:
        settings_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "SettingsView.swift").read_text(encoding="utf-8")
        encoding_editor = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "EncodingOptionsEditor.swift").read_text(
            encoding="utf-8"
        )

        self.assertNotIn(".frame(width: 900, height: 600)", settings_view)
        self.assertIn("maxWidth: .infinity", settings_view)
        self.assertIn("maxHeight: .infinity", settings_view)
        self.assertIn("ProfileEncodingSummaryView(", settings_view)
        self.assertIn("ScrollView {", settings_view)
        self.assertNotIn("isEditable", encoding_editor)

        content_view = (MACOS_ROOT / "BluRayToVisionPro" / "Views" / "ContentView.swift").read_text(encoding="utf-8")
        self.assertIn(".onChange(of: defaultJobOptions)", content_view)
        self.assertIn("if let warningMessage = viewModel.state.warningMessage", content_view)
        self.assertIn('return "Warning: \\(warningMessage)"', content_view)

    def test_native_build_settings_support_hosted_ci_deployment_override(self) -> None:
        settings = native_build_settings(
            "Debug",
            {"BD_TO_AVP_MACOS_DEPLOYMENT_TARGET_OVERRIDE": "26.0"},
        )

        self.assertIn("MACOSX_DEPLOYMENT_TARGET=26.0", settings)
        self.assertNotIn("ARCHS=arm64", settings)

        release_settings = native_build_settings(
            "Release",
            {"BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT": "https://support.example"},
        )
        self.assertIn("ARCHS=arm64", release_settings)
        self.assertIn(f"CURRENT_PROJECT_VERSION={NATIVE_BUILD_VERSION}", release_settings)
        self.assertIn(f"MARKETING_VERSION={NATIVE_SHORT_VERSION}", release_settings)
        self.assertIn(f"PRODUCT_BUNDLE_IDENTIFIER={NATIVE_BUNDLE_IDENTIFIER}", release_settings)

    def test_native_build_settings_reject_invalid_deployment_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid macOS deployment target override"):
            native_build_settings(
                "Debug",
                {"BD_TO_AVP_MACOS_DEPLOYMENT_TARGET_OVERRIDE": "latest"},
            )

    def test_native_build_settings_inject_strict_https_support_endpoint(self) -> None:
        settings = native_build_settings(
            "Release",
            {"BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT": "https://support.example"},
        )

        self.assertIn("BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://support.example", settings)

        invalid_endpoints = (
            "http://support.example",
            "https://user:secret@support.example",
            "https://support.example/custom/path",
            "https://support.example?token=secret",
        )
        for endpoint in invalid_endpoints:
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaisesRegex(
                    ValueError,
                    "Invalid support diagnostics endpoint",
                ),
            ):
                native_build_settings(
                    "Release",
                    {"BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT": endpoint},
                )

    def test_release_build_requires_support_endpoint(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Release builds require an approved support diagnostics endpoint",
        ):
            native_build_settings("Release", {})

    def test_reads_minimum_versions_from_mach_o_build_commands(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="""
Load command 2
      cmd LC_BUILD_VERSION
 platform MACOS
    minos 11.0
Load command 3
      cmd LC_BUILD_VERSION
 platform MACOS
    minos 26.0
""",
            stderr="",
        )

        with patch("scripts.native_app.subprocess.run", return_value=completed):
            self.assertEqual(minimum_macos_versions(Path("/tmp/tool")), {"11.0", "26.0"})

    def test_rejects_packaged_mach_o_requiring_newer_macos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
            newer_library = app_path / "Contents" / "Frameworks" / "Newer.dylib"
            native_executable.parent.mkdir(parents=True)
            newer_library.parent.mkdir(parents=True)
            native_executable.write_bytes(b"\xcf\xfa\xed\xfe")
            newer_library.write_bytes(b"\xcf\xfa\xed\xfe")

            def versions(path: Path) -> set[str]:
                return {"27.0"} if path == newer_library else {NATIVE_MINIMUM_SYSTEM_VERSION}

            with (
                patch("scripts.native_app.minimum_macos_versions", side_effect=versions),
                self.assertRaisesRegex(RuntimeError, "requires a newer macOS version"),
            ):
                verify_mach_o_minimum_system_versions(app_path, native_executable)

    def test_ignores_static_archives_when_validating_packaged_mach_o(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            native_executable = app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME
            static_archive = app_path / "Contents" / "Frameworks" / "libstub.a"
            native_executable.parent.mkdir(parents=True)
            static_archive.parent.mkdir(parents=True)
            native_executable.write_bytes(b"\xcf\xfa\xed\xfe")
            static_archive.write_bytes(b"\xca\xfe\xba\xbe")

            with patch(
                "scripts.native_app.minimum_macos_versions",
                return_value={NATIVE_MINIMUM_SYSTEM_VERSION},
            ) as versions:
                verify_mach_o_minimum_system_versions(app_path, native_executable)

            versions.assert_called_once_with(native_executable)

    def test_package_accepts_an_explicit_signing_keychain(self) -> None:
        args = parse_args(
            [
                "package",
                "--sign-identity",
                "Developer ID Application: Example",
                "--sign-keychain",
                "/tmp/release.keychain-db",
            ]
        )

        self.assertEqual(args.sign_identity, "Developer ID Application: Example")
        self.assertEqual(args.sign_keychain, "/tmp/release.keychain-db")

    def test_package_signing_uses_the_explicit_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            (app_path / "Contents" / "MacOS").mkdir(parents=True)
            with patch("scripts.native_app.run") as run_mock:
                sign_package(
                    app_path,
                    "Developer ID Application: Example",
                    "/tmp/release.keychain-db",
                )

        signing_commands = [
            call.args[0]
            for call in run_mock.call_args_list
            if call.args[0][0] == "codesign" and "--sign" in call.args[0]
        ]
        self.assertGreaterEqual(len(signing_commands), 2)
        for command in signing_commands:
            self.assertIn("--keychain", command)
            self.assertIn("/tmp/release.keychain-db", command)
            self.assertIn("--options", command)
            self.assertIn("runtime", command)
            self.assertIn("--timestamp", command)

    def test_ad_hoc_package_signing_omits_hardened_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            (app_path / "Contents" / "MacOS").mkdir(parents=True)
            with patch("scripts.native_app.run") as run_mock:
                sign_package(app_path, "-")

        signing_commands = [
            call.args[0]
            for call in run_mock.call_args_list
            if call.args[0][0] == "codesign" and "--sign" in call.args[0]
        ]
        self.assertGreaterEqual(len(signing_commands), 2)
        for command in signing_commands:
            self.assertNotIn("--options", command)
            self.assertNotIn("runtime", command)
            self.assertIn("--timestamp=none", command)

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

    def test_accepts_production_product_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(production_info(), info_file)

            verify_product_identity(
                app_path,
                environment={SUPPORT_DIAGNOSTICS_ENDPOINT_ENV: "https://support.example"},
            )

    def test_rejects_missing_support_diagnostics_endpoint_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            info = production_info()
            del info[SUPPORT_DIAGNOSTICS_ENDPOINT_INFO_KEY]
            with info_path.open("wb") as info_file:
                plistlib.dump(info, info_file)

            with self.assertRaisesRegex(RuntimeError, "must be a non-empty valid HTTPS endpoint"):
                verify_product_identity(app_path, environment={})

    def test_rejects_invalid_support_diagnostics_endpoint_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    production_info(support_diagnostics_endpoint="https://support.example/diagnostics"),
                    info_file,
                )

            with self.assertRaisesRegex(RuntimeError, "must be a non-empty valid HTTPS endpoint"):
                verify_product_identity(app_path, environment={})

    def test_rejects_mismatched_support_diagnostics_endpoint_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(production_info(), info_file)

            with self.assertRaisesRegex(RuntimeError, "must exactly match the approved"):
                verify_product_identity(
                    app_path,
                    environment={SUPPORT_DIAGNOSTICS_ENDPOINT_ENV: "https://other-support.example"},
                )

    def test_rejects_development_metadata_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(
                    {
                        **production_info(),
                        "BDToAVPDevelopmentRepositoryRoot": "/private/tmp/source",
                    },
                    info_file,
                )

            with self.assertRaisesRegex(RuntimeError, "development repository metadata"):
                verify_product_identity(app_path, environment={})

    def test_rejects_repository_documents_in_release_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path = Path(temporary_directory) / NATIVE_APP_NAME
            info_path = app_path / "Contents" / "Info.plist"
            info_path.parent.mkdir(parents=True)
            with info_path.open("wb") as info_file:
                plistlib.dump(production_info(), info_file)
            internal_document = app_path / "Contents" / "Resources" / "app" / "README.md"
            internal_document.parent.mkdir(parents=True)
            internal_document.write_text("internal", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "repository-only documents"):
                verify_product_identity(app_path, environment={})

    def test_accepts_complete_worker_smoke_contract(self) -> None:
        job_id = "97456c4a-f3c5-44e4-a548-0bd833ead4bb"
        events: list[object] = [
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "worker.ready",
                "job_id": job_id,
                "sequence": 0,
                "payload": {"process_group_id": 123},
            },
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "job.started",
                "job_id": job_id,
                "sequence": 1,
                "payload": {},
            },
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "stage.started",
                "job_id": job_id,
                "sequence": 2,
                "payload": {},
            },
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "observability",
                "job_id": job_id,
                "sequence": 3,
                "payload": {"event": canonical_ffprobe_event(job_id)},
            },
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "job.completed",
                "job_id": job_id,
                "sequence": 4,
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

    def test_worker_smoke_resolves_relative_app_path_before_changing_directory(self) -> None:
        job_id = "97456c4a-f3c5-44e4-a548-0bd833ead4bb"
        event_types = ["worker.ready", "job.started", "stage.started", "observability", "job.completed"]
        events = [
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": event_type,
                "job_id": job_id,
                "sequence": sequence,
                "payload": (
                    {
                        "result": {
                            "resolution": "160x90",
                            "frame_rate": "24/1",
                            "interlaced": False,
                            "size_bytes": 1024,
                        }
                    }
                    if event_type == "job.completed"
                    else {"event": canonical_ffprobe_event(job_id)}
                    if event_type == "observability"
                    else {}
                ),
            }
            for sequence, event_type in enumerate(event_types)
        ]
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            relative_app_path = Path("package") / NATIVE_APP_NAME
            absolute_app_path = temporary_path / relative_app_path
            absolute_app_path.mkdir(parents=True)
            resolved_app_path = absolute_app_path.resolve()
            with (
                chdir(temporary_path),
                patch("scripts.native_app.run"),
                patch("scripts.native_app.uuid4", return_value=job_id),
                patch("scripts.native_app.subprocess.run", return_value=completed) as run_mock,
            ):
                smoke_packaged_worker(relative_app_path)

            worker_command = run_mock.call_args.args[0]
            self.assertEqual(
                worker_command,
                [str(resolved_app_path / "Contents" / "MacOS" / "BluRayToVisionProEngine")],
            )
            self.assertEqual(run_mock.call_args.kwargs["cwd"], resolved_app_path)

    def test_rejects_incomplete_observability_event(self) -> None:
        job_id = "97456c4a-f3c5-44e4-a548-0bd833ead4bb"
        event_types = [
            "worker.ready",
            "job.started",
            "stage.started",
            "observability",
            "observability",
            "job.completed",
        ]
        events = [
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": event_type,
                "job_id": job_id,
                "sequence": sequence,
                "payload": (
                    {
                        "result": {
                            "resolution": "160x90",
                            "frame_rate": "24/1",
                            "interlaced": False,
                            "size_bytes": 1024,
                        }
                    }
                    if event_type == "job.completed"
                    else {"event": canonical_ffprobe_event(job_id)}
                    if event_type == "observability" and sequence == 3
                    else {
                        "event": {
                            "schema": "bd_to_avp.observability",
                            "schema_version": 1,
                            "kind": "tool.completed",
                            "context": {"tool": {"id": "ffprobe"}},
                        }
                    }
                    if event_type == "observability"
                    else {}
                ),
            }
            for sequence, event_type in enumerate(event_types)
        ]

        with self.assertRaisesRegex(ValueError, "invalid canonical observability"):
            validate_smoke_events(events, job_id)

    def test_native_startup_smoke_uses_the_signed_packaged_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            relative_app_path = Path("package") / NATIVE_APP_NAME
            absolute_app_path = temporary_path / relative_app_path
            absolute_app_path.mkdir(parents=True)
            resolved_app_path = absolute_app_path.resolve()
            with chdir(temporary_path), patch("scripts.native_app.run") as run_mock:
                smoke_packaged_native_app(relative_app_path)

        run_mock.assert_called_once_with(
            [
                str(resolved_app_path / "Contents" / "MacOS" / NATIVE_EXECUTABLE_NAME),
                "--startup-smoke",
            ],
            cwd=resolved_app_path,
        )

    def test_rejects_wrong_job_or_result(self) -> None:
        events: list[object] = [
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
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

    def test_rejects_worker_smoke_without_canonical_tool_observability(self) -> None:
        job_id = "97456c4a-f3c5-44e4-a548-0bd833ead4bb"
        events: list[object] = [
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": event_type,
                "job_id": job_id,
                "sequence": sequence,
                "payload": {
                    "result": {
                        "resolution": "160x90",
                        "frame_rate": "24/1",
                        "interlaced": False,
                        "size_bytes": 1024,
                    }
                }
                if event_type == "job.completed"
                else {},
            }
            for sequence, event_type in enumerate(["worker.ready", "job.started", "stage.started", "job.completed"])
        ]

        with self.assertRaisesRegex(ValueError, "canonical FFprobe observability"):
            validate_smoke_events(events, job_id)


if __name__ == "__main__":
    unittest.main()
