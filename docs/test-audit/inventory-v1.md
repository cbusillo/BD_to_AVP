# Test Audit Inventory v1

- Baseline reference: `b4f980642c6e36140f458af3f3eaffafc9ae14fa`
- Test files: **149**
- Support fixtures: **70**
- Test cases counted: **2584**
- This generated view is inventory evidence, not a runtime result or duration report.

## Lanes

| ID | Kind | Maintained | Command / targets | Sources |
| --- | --- | --- | --- | --- |
| `ci.macos.blu_ray_unit` | ci | true | `uv run python scripts/native_app.py test` | `.github/workflows/ci.yml`, `scripts/native_app.py`, `macos/project.yml` |
| `ci.python.unittest` | ci | true | `uv run python -m unittest discover -s tests -t .` | `.github/workflows/ci.yml` |
| `ci.support_diagnostics.vitest` | ci | true | `npm run check` | `.github/workflows/ci.yml` |
| `operator.tier3.installed_ui` | documented_operator | true | `uv run python -m scripts.tier3_clean_machine preflight  --candidate-release-receipt docs/release-evidence/<candidate>/release-receipt.json  --candidate-dmg ~/Downloads/<candidate>.dmg  --prior-release-receipt docs/release-evidence/<prior>/release-receipt.json  --prior-dmg ~/Downloads/<prior>.dmg  --qualification-root ~/Tier3-BD-to-AVP-Qualification  --route rc  --environment-class restorable-location; uv run python -m scripts.tier3_clean_machine run  --candidate-release-receipt docs/release-evidence/<candidate>/release-receipt.json  --candidate-dmg ~/Downloads/<candidate>.dmg  --prior-release-receipt docs/release-evidence/<prior>/release-receipt.json  --prior-dmg ~/Downloads/<prior>.dmg  --qualification-root ~/Tier3-BD-to-AVP-Qualification  --route rc  --environment-class restorable-location  --output-receipt /path/out/clean-machine-signed-update.json  --ui-output-receipt /path/out/installed-ui-accessibility.json  --evidence-directory /path/out/clean-machine-signed-update-evidence` | `docs/tier3-clean-machine.md`, `macos/project.yml` |
| `operator.visionos.playback_probe` | documented_device | true | `xcodebuild test  -project macos/BluRayToVisionPro.xcodeproj  -scheme SpatialPlaybackProbe  -destination 'platform=visionOS Simulator,name=Apple Vision Pro'  -derivedDataPath macos/build/SpatialPlaybackProbeDerivedData  CODE_SIGNING_ALLOWED=NO` | `docs/visionos-playback-validator.md`, `macos/project.yml` |

## Findings

- Orphan/not-in-CI lane findings: **2**.
- Unmaintained test files: **0**.
- Classifications remain `unclassified`; this slice does not recommend deletion, relaxation, or test-count targets.

## Test Files

| Path | Language | Bundle | Cases | Lanes | Requirements | Signals |
| --- | --- | --- | ---: | --- | --- | --- |
| `macos/BluRayToVisionProTests/ActivityDrawerRenderTests.swift` | Swift | `BluRayToVisionProTests` | 1 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/AppDelegateTests.swift` | Swift | `BluRayToVisionProTests` | 5 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/AppSettingsTests.swift` | Swift | `BluRayToVisionProTests` | 3 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/ConversionQueueStoreTests.swift` | Swift | `BluRayToVisionProTests` | 46 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required | filesystem_access, polling_or_waiting, hardware_or_media |
| `macos/BluRayToVisionProTests/ConversionViewModelTests.swift` | Swift | `BluRayToVisionProTests` | 115 | `ci.macos.blu_ray_unit` | physical Blu-ray device; real-media or SSIF/ISO fixture may be required; repository fixture files | filesystem_access, external_process, polling_or_waiting, hardware_or_media |
| `macos/BluRayToVisionProTests/ConversionWorkflowTests.swift` | Swift | `BluRayToVisionProTests` | 75 | `ci.macos.blu_ray_unit` | physical Blu-ray device; real-media or SSIF/ISO fixture may be required; repository fixture files | filesystem_access, hardware_or_media |
| `macos/BluRayToVisionProTests/DiagnosticBundleTests.swift` | Swift | `BluRayToVisionProTests` | 37 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required; repository fixture files | filesystem_access, external_process, polling_or_waiting |
| `macos/BluRayToVisionProTests/DiagnosticReportClientTests.swift` | Swift | `BluRayToVisionProTests` | 7 | `ci.macos.blu_ray_unit` | network access; real-media or SSIF/ISO fixture may be required | filesystem_access, polling_or_waiting, network_access |
| `macos/BluRayToVisionProTests/DiagnosticReportViewModelTests.swift` | Swift | `BluRayToVisionProTests` | 14 | `ci.macos.blu_ray_unit` | — | filesystem_access, polling_or_waiting |
| `macos/BluRayToVisionProTests/DiagnosticUserCommentTests.swift` | Swift | `BluRayToVisionProTests` | 7 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/GitHubIssueDraftTests.swift` | Swift | `BluRayToVisionProTests` | 8 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/JSONLFramerTests.swift` | Swift | `BluRayToVisionProTests` | 3 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/LanguageCatalogTests.swift` | Swift | `BluRayToVisionProTests` | 6 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/LiveObservabilityStatusTests.swift` | Swift | `BluRayToVisionProTests` | 12 | `ci.macos.blu_ray_unit` | repository fixture files | filesystem_access |
| `macos/BluRayToVisionProTests/ObservabilityEventStoreTests.swift` | Swift | `BluRayToVisionProTests` | 12 | `ci.macos.blu_ray_unit` | repository fixture files | filesystem_access, external_process, polling_or_waiting |
| `macos/BluRayToVisionProTests/ObservabilityEventTests.swift` | Swift | `BluRayToVisionProTests` | 6 | `ci.macos.blu_ray_unit` | repository fixture files | filesystem_access |
| `macos/BluRayToVisionProTests/OffPeakScheduleStoreTests.swift` | Swift | `BluRayToVisionProTests` | 8 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/PersistentQueueCommandStateTests.swift` | Swift | `BluRayToVisionProTests` | 7 | `ci.macos.blu_ray_unit` | — | hardware_or_media |
| `macos/BluRayToVisionProTests/PersistentQueueNotificationCoordinatorTests.swift` | Swift | `BluRayToVisionProTests` | 10 | `ci.macos.blu_ray_unit` | — | polling_or_waiting |
| `macos/BluRayToVisionProTests/PersistentQueueOutcomeSummaryTests.swift` | Swift | `BluRayToVisionProTests` | 2 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/PersistentQueueWorkspaceRenderTests.swift` | Swift | `BluRayToVisionProTests` | 7 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required | hardware_or_media |
| `macos/BluRayToVisionProTests/PreviewPresentationSmokeTests.swift` | Swift | `BluRayToVisionProTests` | 2 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/PreviewViewModelTests.swift` | Swift | `BluRayToVisionProTests` | 18 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required; repository fixture files | filesystem_access, polling_or_waiting, hardware_or_media |
| `macos/BluRayToVisionProTests/ProfileStoreTests.swift` | Swift | `BluRayToVisionProTests` | 31 | `ci.macos.blu_ray_unit` | repository fixture files | filesystem_access |
| `macos/BluRayToVisionProTests/QueueResolutionTests.swift` | Swift | `BluRayToVisionProTests` | 2 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/ResolutionMemoryStoreTests.swift` | Swift | `BluRayToVisionProTests` | 6 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/RouteResolutionTests.swift` | Swift | `BluRayToVisionProTests` | 9 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/SetupEditSessionTests.swift` | Swift | `BluRayToVisionProTests` | 9 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/SetupEditorRenderTests.swift` | Swift | `BluRayToVisionProTests` | 1 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/StorageForecastTests.swift` | Swift | `BluRayToVisionProTests` | 8 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/StoragePreflightViewModelTests.swift` | Swift | `BluRayToVisionProTests` | 5 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required | polling_or_waiting |
| `macos/BluRayToVisionProTests/UpdateControllerTests.swift` | Swift | `BluRayToVisionProTests` | 13 | `ci.macos.blu_ray_unit` | network access; real-media or SSIF/ISO fixture may be required | network_access |
| `macos/BluRayToVisionProTests/VideoQualityEditorRenderTests.swift` | Swift | `BluRayToVisionProTests` | 4 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/VideoQualityTests.swift` | Swift | `BluRayToVisionProTests` | 15 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/VideoRouteQualitySummaryTests.swift` | Swift | `BluRayToVisionProTests` | 5 | `ci.macos.blu_ray_unit` | — | — |
| `macos/BluRayToVisionProTests/WorkerCancellationSmokeTests.swift` | Swift | `BluRayToVisionProTests` | 2 | `ci.macos.blu_ray_unit` | — | filesystem_access |
| `macos/BluRayToVisionProTests/WorkerLaunchConfigurationTests.swift` | Swift | `BluRayToVisionProTests` | 2 | `ci.macos.blu_ray_unit` | — | environment_dependent |
| `macos/BluRayToVisionProTests/WorkerLifecycleTests.swift` | Swift | `BluRayToVisionProTests` | 29 | `ci.macos.blu_ray_unit` | repository fixture files | filesystem_access |
| `macos/BluRayToVisionProTests/WorkerProcessClientTests.swift` | Swift | `BluRayToVisionProTests` | 15 | `ci.macos.blu_ray_unit` | real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, polling_or_waiting |
| `macos/BluRayToVisionProUITests/InstalledUIAcceptanceTests.swift` | Swift | `BluRayToVisionProUITests` | 5 | `operator.tier3.installed_ui` | Tier 3 clean-machine and Accessibility environment | environment_dependent, filesystem_access, polling_or_waiting, ui_or_accessibility, hardware_or_media, skip_or_conditional |
| `macos/SpatialPlaybackProbeTests/PlaybackValidationTests.swift` | Swift | `SpatialPlaybackProbeTests` | 15 | `operator.visionos.playback_probe` | real-media or SSIF/ISO fixture may be required; visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, hardware_or_media |
| `macos/SpatialPlaybackProbeUITests/SpatialPlaybackProbeUITests.swift` | Swift | `SpatialPlaybackProbeUITests` | 2 | `operator.visionos.playback_probe` | Tier 3 clean-machine and Accessibility environment; visionOS simulator or physical Apple Vision Pro, depending on evidence | polling_or_waiting, ui_or_accessibility, hardware_or_media, skip_or_conditional |
| `support-diagnostics/test/diagnostic-service.test.ts` | TypeScript | `support-diagnostics-vitest` | 23 | `ci.support_diagnostics.vitest` | network access; real-media or SSIF/ISO fixture may be required; repository fixture files | filesystem_access, network_access |
| `tests/test_apple_vision_ocr.py` | Python | `python-unittest-discovery` | 7 | `ci.python.unittest` | — | — |
| `tests/test_audio.py` | Python | `python-unittest-discovery` | 30 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, skip_or_conditional |
| `tests/test_audio_selection.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_av1_stereo.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_beta3_recovery_evidence.py` | Python | `python-unittest-discovery` | 9 | `ci.python.unittest` | — | environment_dependent, filesystem_access, network_access |
| `tests/test_cancelled_release_attempt.py` | Python | `python-unittest-discovery` | 14 | `ci.python.unittest` | — | filesystem_access, external_process, hardware_or_media |
| `tests/test_command.py` | Python | `python-unittest-discovery` | 12 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting |
| `tests/test_config_version.py` | Python | `python-unittest-discovery` | 3 | `ci.python.unittest` | — | filesystem_access, hardware_or_media |
| `tests/test_container.py` | Python | `python-unittest-discovery` | 29 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_create_packaged_aac_layout_fixtures.py` | Python | `python-unittest-discovery` | 12 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_direct_mv_hevc_anchors.py` | Python | `python-unittest-discovery` | 23 | `ci.python.unittest` | — | filesystem_access, external_process, hardware_or_media |
| `tests/test_direct_mv_hevc_mapping_confirmation.py` | Python | `python-unittest-discovery` | 14 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_direct_mv_hevc_metalfx_mapping_confirmation.py` | Python | `python-unittest-discovery` | 9 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_direct_mv_hevc_quality_sweep.py` | Python | `python-unittest-discovery` | 23 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_direct_mv_hevc_route_integration.py` | Python | `python-unittest-discovery` | 1 | `ci.python.unittest` | — | filesystem_access, external_process, skip_or_conditional |
| `tests/test_disc.py` | Python | `python-unittest-discovery` | 20 | `ci.python.unittest` | physical Blu-ray device; real-media or SSIF/ISO fixture may be required | filesystem_access, hardware_or_media |
| `tests/test_edge264_builder.py` | Python | `python-unittest-discovery` | 9 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access |
| `tests/test_embedded_python.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access |
| `tests/test_ffmpeg_manifest_update.py` | Python | `python-unittest-discovery` | 13 | `ci.python.unittest` | network access | filesystem_access, network_access |
| `tests/test_ffmpeg_vendor.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | network access | filesystem_access, network_access |
| `tests/test_file_upscale_quality_mapping_selection.py` | Python | `python-unittest-discovery` | 36 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_file_upscale_quality_repeatability_calibration.py` | Python | `python-unittest-discovery` | 16 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_file_upscale_quality_sweep.py` | Python | `python-unittest-discovery` | 26 | `ci.python.unittest` | — | environment_dependent, filesystem_access, external_process, polling_or_waiting |
| `tests/test_generated_mv_hevc_artifacts.py` | Python | `python-unittest-discovery` | 19 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_generated_mv_hevc_calibration.py` | Python | `python-unittest-discovery` | 69 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_generated_mv_hevc_collapse.py` | Python | `python-unittest-discovery` | 15 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_github_release_run.py` | Python | `python-unittest-discovery` | 37 | `ci.python.unittest` | — | environment_dependent, filesystem_access, external_process, polling_or_waiting |
| `tests/test_gui_processing.py` | Python | `python-unittest-discovery` | 31 | `ci.python.unittest` | — | environment_dependent, filesystem_access, polling_or_waiting |
| `tests/test_installed_ui_qualification.py` | Python | `python-unittest-discovery` | 12 | `ci.python.unittest` | Tier 3 clean-machine and Accessibility environment | filesystem_access, ui_or_accessibility |
| `tests/test_languages.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_macos_release.py` | Python | `python-unittest-discovery` | 14 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access |
| `tests/test_main.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_mp4box_builder.py` | Python | `python-unittest-discovery` | 15 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access |
| `tests/test_mv_hevc_adaptive.py` | Python | `python-unittest-discovery` | 3 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_mv_hevc_corpus.py` | Python | `python-unittest-discovery` | 21 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, polling_or_waiting |
| `tests/test_mv_hevc_encoder.py` | Python | `python-unittest-discovery` | 19 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, polling_or_waiting, skip_or_conditional |
| `tests/test_mv_hevc_gpu_profile.py` | Python | `python-unittest-discovery` | 9 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_mv_hevc_metalfx_benchmark.py` | Python | `python-unittest-discovery` | 7 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting |
| `tests/test_mv_hevc_metalfx_quality.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_mv_hevc_quality_match.py` | Python | `python-unittest-discovery` | 7 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_native_app.py` | Python | `python-unittest-discovery` | 67 | `ci.python.unittest` | Tier 3 clean-machine and Accessibility environment; network access | filesystem_access, external_process, polling_or_waiting, network_access, ui_or_accessibility, hardware_or_media |
| `tests/test_native_mvc_split.py` | Python | `python-unittest-discovery` | 41 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting, hardware_or_media |
| `tests/test_native_worker.py` | Python | `python-unittest-discovery` | 129 | `ci.python.unittest` | physical Blu-ray device; real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, polling_or_waiting, hardware_or_media |
| `tests/test_observability.py` | Python | `python-unittest-discovery` | 13 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_observability_migration.py` | Python | `python-unittest-discovery` | 3 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_packaged_aac_layouts.py` | Python | `python-unittest-discovery` | 34 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_packaged_mv_hevc_routes.py` | Python | `python-unittest-discovery` | 32 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting |
| `tests/test_packaged_video_quality_routes.py` | Python | `python-unittest-discovery` | 9 | `ci.python.unittest` | — | environment_dependent, filesystem_access, external_process |
| `tests/test_pre_signing_ui.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | network access | filesystem_access, network_access, hardware_or_media |
| `tests/test_preflight.py` | Python | `python-unittest-discovery` | 24 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process |
| `tests/test_preview.py` | Python | `python-unittest-discovery` | 8 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_process_audio.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access |
| `tests/test_process_preflight.py` | Python | `python-unittest-discovery` | 28 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_process_runner.py` | Python | `python-unittest-discovery` | 45 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting |
| `tests/test_production_preflight.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | — | environment_dependent, filesystem_access |
| `tests/test_qualify_apple_aac_layouts.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_qualify_release_notes_links.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | network access; real-media or SSIF/ISO fixture may be required | filesystem_access, network_access |
| `tests/test_qualify_release_scope.py` | Python | `python-unittest-discovery` | 46 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required; visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, external_process, ui_or_accessibility, hardware_or_media |
| `tests/test_real_mvc_4k_quality.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_real_mvc_feature_qualification.py` | Python | `python-unittest-discovery` | 18 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting |
| `tests/test_real_mvc_public_evidence.py` | Python | `python-unittest-discovery` | 7 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_real_mvc_qualification_segment.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_reassess_real_mvc_feature.py` | Python | `python-unittest-discovery` | 2 | `ci.python.unittest` | — | — |
| `tests/test_release.py` | Python | `python-unittest-discovery` | 67 | `ci.python.unittest` | network access; real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, network_access, ui_or_accessibility, hardware_or_media |
| `tests/test_release_evidence_orphan_audit.py` | Python | `python-unittest-discovery` | 20 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_release_evidence_reconcile.py` | Python | `python-unittest-discovery` | 14 | `ci.python.unittest` | network access | environment_dependent, filesystem_access, external_process, network_access |
| `tests/test_release_evidence_v2.py` | Python | `python-unittest-discovery` | 29 | `ci.python.unittest` | — | filesystem_access, external_process, ui_or_accessibility, hardware_or_media |
| `tests/test_release_milestone_context.py` | Python | `python-unittest-discovery` | 57 | `ci.python.unittest` | Tier 3 clean-machine and Accessibility environment; network access | filesystem_access, external_process, network_access, ui_or_accessibility, hardware_or_media |
| `tests/test_release_qualification_apply.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | network access | environment_dependent, filesystem_access, external_process, network_access |
| `tests/test_release_qualification_artifact.py` | Python | `python-unittest-discovery` | 22 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, polling_or_waiting, ui_or_accessibility |
| `tests/test_release_qualification_controller.py` | Python | `python-unittest-discovery` | 20 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, ui_or_accessibility, hardware_or_media |
| `tests/test_release_qualification_manifest.py` | Python | `python-unittest-discovery` | 8 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access, ui_or_accessibility, hardware_or_media |
| `tests/test_release_qualification_resume.py` | Python | `python-unittest-discovery` | 43 | `ci.python.unittest` | network access | filesystem_access, polling_or_waiting, network_access |
| `tests/test_release_receipt.py` | Python | `python-unittest-discovery` | 14 | `ci.python.unittest` | network access | filesystem_access, network_access, hardware_or_media |
| `tests/test_release_recovery_records.py` | Python | `python-unittest-discovery` | 13 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_release_smoke.py` | Python | `python-unittest-discovery` | 18 | `ci.python.unittest` | — | filesystem_access, external_process, polling_or_waiting, hardware_or_media |
| `tests/test_release_workflow_policy.py` | Python | `python-unittest-discovery` | 18 | `ci.python.unittest` | network access | environment_dependent, filesystem_access, network_access |
| `tests/test_signed_artifact_receipt.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access, ui_or_accessibility, hardware_or_media |
| `tests/test_signed_artifact_ui.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | Tier 3 clean-machine and Accessibility environment | filesystem_access, ui_or_accessibility, hardware_or_media |
| `tests/test_sparkle_appcast.py` | Python | `python-unittest-discovery` | 33 | `ci.python.unittest` | network access | filesystem_access, network_access, hardware_or_media |
| `tests/test_sparkle_packaging.py` | Python | `python-unittest-discovery` | 18 | `ci.python.unittest` | network access | filesystem_access, external_process, network_access |
| `tests/test_sparkle_workflows.py` | Python | `python-unittest-discovery` | 35 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, hardware_or_media |
| `tests/test_spatial_video_metadata.py` | Python | `python-unittest-discovery` | 4 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_ssif_probe.py` | Python | `python-unittest-discovery` | 10 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access, external_process, polling_or_waiting |
| `tests/test_ssif_probe_builder.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | network access; real-media or SSIF/ISO fixture may be required | filesystem_access, network_access |
| `tests/test_ssif_probe_integration.py` | Python | `python-unittest-discovery` | 2 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, polling_or_waiting, skip_or_conditional |
| `tests/test_stable_pypi_recovery.py` | Python | `python-unittest-discovery` | 7 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_storage_capacity.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access, polling_or_waiting |
| `tests/test_subtitles.py` | Python | `python-unittest-discovery` | 46 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_support_diagnostics.py` | Python | `python-unittest-discovery` | 21 | `ci.python.unittest` | network access | filesystem_access, polling_or_waiting, network_access |
| `tests/test_test_audit_inventory.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, external_process, polling_or_waiting |
| `tests/test_tier3_clean_machine.py` | Python | `python-unittest-discovery` | 43 | `ci.python.unittest` | Tier 3 clean-machine and Accessibility environment; network access; real-media or SSIF/ISO fixture may be required | environment_dependent, filesystem_access, external_process, polling_or_waiting, network_access, ui_or_accessibility, hardware_or_media, skip_or_conditional |
| `tests/test_tier3_operator_collect.py` | Python | `python-unittest-discovery` | 26 | `ci.python.unittest` | visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, polling_or_waiting, hardware_or_media |
| `tests/test_tier3_operator_receipt.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, hardware_or_media |
| `tests/test_tier3_receipt.py` | Python | `python-unittest-discovery` | 5 | `ci.python.unittest` | visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, hardware_or_media |
| `tests/test_tool_resolution.py` | Python | `python-unittest-discovery` | 19 | `ci.python.unittest` | — | environment_dependent, filesystem_access, external_process |
| `tests/test_updater.py` | Python | `python-unittest-discovery` | 15 | `ci.python.unittest` | network access | environment_dependent, filesystem_access, network_access |
| `tests/test_vendor_pgsrip_cli.py` | Python | `python-unittest-discovery` | 1 | `ci.python.unittest` | — | — |
| `tests/test_vendor_pgsrip_edge_cases.py` | Python | `python-unittest-discovery` | 24 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required | filesystem_access |
| `tests/test_verify_app_tools.py` | Python | `python-unittest-discovery` | 12 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_verify_apple_media.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access, external_process |
| `tests/test_video_helpers.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_video_quality_ladder.py` | Python | `python-unittest-discovery` | 16 | `ci.python.unittest` | real-media or SSIF/ISO fixture may be required; visionOS simulator or physical Apple Vision Pro, depending on evidence | filesystem_access, hardware_or_media |
| `tests/test_video_quality_route_table.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_video_route.py` | Python | `python-unittest-discovery` | 27 | `ci.python.unittest` | — | filesystem_access |
| `tests/test_worker_diagnostics.py` | Python | `python-unittest-discovery` | 6 | `ci.python.unittest` | — | filesystem_access, polling_or_waiting |

## Support Fixtures

| Path | Format | Classification |
| --- | --- | --- |
| `tests/fixtures/multilingual_audio_selection_v1.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_fallback_warning_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_fallback_warning_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_fallback_warning_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_fallback_warning_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_fallback_warning_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_language_fallback_warning_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_language_fallback_warning_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_language_fallback_warning_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_audio_language_fallback_warning_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v1.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v2.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v3.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v4.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v5.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_conversion_completed_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_existing_artifact_upscale_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_existing_artifact_upscale_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_existing_artifact_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_existing_artifact_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_existing_artifact_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_generated_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_generated_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_generated_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v2.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v3.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v4.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v5.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_physical_disc_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v1.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v2.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v3.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v4.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v5.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_convert_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v3.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v4.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v5.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_preview_v9.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v10.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v11.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v12.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v4.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v5.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v8.json` | json | unclassified |
| `tests/fixtures/native_worker_stage_started_progress_v9.json` | json | unclassified |
| `tests/fixtures/observability_event_v1.json` | json | unclassified |
| `tests/fixtures/observability_left_eye_growing_v1.json` | json | unclassified |
| `tests/fixtures/observability_long_running_tool_v1.json` | json | unclassified |
| `tests/fixtures/observability_right_eye_growing_v1.json` | json | unclassified |
| `tests/fixtures/observability_stalled_tool_v1.json` | json | unclassified |
| `tests/fixtures/profile_library_v5.json` | json | unclassified |
| `tests/fixtures/profile_library_v6.json` | json | unclassified |
| `tests/fixtures/support_diagnostics_native_v1.b64` | b64 | unclassified |
