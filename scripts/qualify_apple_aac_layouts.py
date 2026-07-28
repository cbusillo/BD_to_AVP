from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import platform
import re
import shutil
import subprocess
import tempfile

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bd_to_avp.modules import audio
from bd_to_avp.modules.aac_layout_policy import (
    AAC_LAYOUT_POLICIES,
    LAYOUT_CHANNELS,
    UNLISTED_LAYOUT_POLICY,
    AacLayoutPolicy,
)
from bd_to_avp.modules.config import config
from scripts.create_spatial_audio_validation_fixtures import create_channel_identity_audio
from scripts.verify_apple_media import (
    AppleMediaConversionReport,
    AppleMediaFailure,
    inspect_apple_media_conversion,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_OUTPUT = REPO_ROOT / "docs" / "qualification" / "apple-aac-layouts-v1.json"
DEFAULT_MARKDOWN_OUTPUT = REPO_ROOT / "docs" / "apple-aac-layout-policy.md"
APPLE_LPCM_PRESET = "PresetAppleProRes422LPCM"
SAMPLE_RATE = 48_000
IDENTITY_SLOT_SECONDS = 0.25
IDENTITY_BURST_SECONDS = 0.12
IDENTITY_GUARD_SECONDS = 0.25
IDENTITY_ANALYSIS_INSET_SECONDS = 0.02
IDENTITY_RELATIVE_THRESHOLD = 0.08
IDENTITY_ABSOLUTE_THRESHOLD = 0.01

PACKAGED_FIXTURE_GATE_LINES = (
    "## Packaged Fixture Gate",
    "",
    (
        "`docs/qualification/apple-aac-package-fixtures-v1.json` is the checked release fixture contract. "
        "It binds representative packaged-worker cases for canonical stereo and `5.1` preservation, "
        "`5.1(side)` remapping, `7.1` downmixing, missing/unknown/PCE fail-closed behavior, multiple "
        "languages, handler titles, and default disposition. Its channel-identity maps must remain identical "
        "to the production policy."
    ),
    "",
    (
        "Each private fixture is a short MVC MKV with the #380 time-slotted channel-identity signal and the "
        "exact stream metadata declared by the manifest. Run the verifier from the checkout that built the "
        "candidate:"
    ),
    "",
    "```bash",
    "uv run python -m scripts.verify_packaged_aac_layouts \\",
    '  --app "$BD_TO_AVP_CANDIDATE_APP" \\',
    '  --fixture-root "$BD_TO_AVP_AAC_FIXTURE_ROOT" \\',
    '  --artifacts "$BD_TO_AVP_AAC_EVIDENCE_ROOT/artifacts" \\',
    '  --output "$BD_TO_AVP_AAC_EVIDENCE_ROOT/evidence.json"',
    "```",
    "",
    (
        "The verifier rejects package-policy drift, validates each fixture before execution, runs the packaged "
        "worker with protocol v10, checks structured layout decisions and visible rejections, verifies final "
        "AAC layout and metadata, runs Apple passthrough and LPCM identity analysis, and records bounded "
        "package/source/output hashes. An ad-hoc package run is deterministic package evidence only. It does "
        "not establish Developer ID/notarization provenance, physical Vision Pro surround placement, repeated "
        "device seeks, or perceptual lip-sync; those remain exact signed-candidate gates under #382."
    ),
)


CHANNEL_FREQUENCIES = {
    "FL": 330,
    "FR": 440,
    "FC": 550,
    "LFE": 96,
    "BL": 660,
    "BR": 770,
    "BC": 880,
    "SL": 990,
    "SR": 1100,
    "FLC": 1210,
    "FRC": 1320,
}

AAC_CHANNEL_CONFIGURATION_LAYOUTS = {
    1: ("FC",),
    2: ("FL", "FR"),
    3: ("FC", "FL", "FR"),
    4: ("FC", "FL", "FR", "BC"),
    5: ("FC", "FL", "FR", "SL", "SR"),
    6: ("FC", "FL", "FR", "SL", "SR", "LFE"),
    7: ("FC", "FLC", "FRC", "FL", "FR", "SL", "SR", "LFE"),
}

CANONICAL_AAC_CONFIGURATION = {
    "mono": 1,
    "stereo": 2,
    "3.0": 3,
    "4.0": 4,
    "5.0": 5,
    "5.1": 6,
    "7.1": 7,
}

AudioLayoutCase = AacLayoutPolicy
LAYOUT_CASES = AAC_LAYOUT_POLICIES


def run(command: Sequence[str | Path], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        check=True,
        cwd=REPO_ROOT,
        text=True,
        capture_output=capture_output,
    )


def require_tools() -> None:
    missing = [path for path in (config.FFMPEG_PATH, config.FFPROBE_PATH) if not path.is_file()]
    if shutil.which("avconvert") is None:
        missing.append(Path("avconvert"))
    if missing:
        raise RuntimeError("Missing AAC qualification tools: " + ", ".join(str(path) for path in missing))


def create_black_video(path: Path, duration_seconds: float) -> None:
    run(
        [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=160x90:r=24:d={duration_seconds:g}",
            "-c:v",
            "h264_videotoolbox",
            "-tag:v",
            "avc1",
            "-an",
            "-y",
            path,
        ]
    )


def create_source_identity(path: Path, case: AudioLayoutCase) -> float:
    duration_seconds = 2 * IDENTITY_GUARD_SECONDS + len(case.source_channels) * IDENTITY_SLOT_SECONDS
    create_channel_identity_audio(
        path,
        case.source_layout,
        tuple(CHANNEL_FREQUENCIES[channel] for channel in case.source_channels),
        duration_seconds=duration_seconds,
        cycle_seconds=duration_seconds,
        slot_seconds=IDENTITY_SLOT_SECONDS,
        burst_seconds=IDENTITY_BURST_SECONDS,
        start_offset_seconds=IDENTITY_GUARD_SECONDS,
    )
    return duration_seconds


def encode_aac(source_path: Path, output_path: Path, case: AudioLayoutCase, *, apply_policy: bool) -> None:
    target_layout = case.target_layout if apply_policy else case.source_layout
    target_channels = len(LAYOUT_CHANNELS[target_layout])
    command: list[str | Path] = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_path,
    ]
    if apply_policy and case.pan_filter is not None:
        command.extend(["-af", case.pan_filter])
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            f"{max(64, target_channels * 64)}k",
            "-ar",
            str(SAMPLE_RATE),
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:0",
            f"title={case.source_layout} identity",
            "-disposition:a:0",
            "default",
            "-y",
            output_path,
        ]
    )
    run(command)


def mux_video_audio(video_path: Path, audio_path: Path, output_path: Path) -> None:
    run(
        [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            "-y",
            output_path,
        ]
    )


def probe(path: Path) -> dict[str, Any]:
    completed = run(
        [
            config.FFPROBE_PATH,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_data",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
    )
    return json.loads(completed.stdout)


def audio_streams(probe_data: dict[str, Any]) -> list[dict[str, Any]]:
    streams = probe_data.get("streams")
    if not isinstance(streams, list):
        return []
    return [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]


def parse_aac_channel_configuration(extradata: object) -> int | None:
    if not isinstance(extradata, str):
        return None
    match = re.search(r"^[0-9a-fA-F]{8}:\s+([0-9a-fA-F]{4})", extradata, flags=re.MULTILINE)
    if match is None:
        return None
    first_word = int(match.group(1), 16)
    return (first_word >> 3) & 0x0F


def aac_stream_evidence(path: Path) -> dict[str, Any]:
    streams = audio_streams(probe(path))
    if len(streams) != 1:
        return {"audio_stream_count": len(streams), "valid": False}
    stream = streams[0]
    raw_tags = stream.get("tags")
    raw_disposition = stream.get("disposition")
    tags: Mapping[str, Any] = raw_tags if isinstance(raw_tags, dict) else {}
    disposition: Mapping[str, Any] = raw_disposition if isinstance(raw_disposition, dict) else {}
    return {
        "audio_stream_count": 1,
        "valid": True,
        "codec": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "sample_rate": stream.get("sample_rate"),
        "channels": stream.get("channels"),
        "ffprobe_layout": stream.get("channel_layout"),
        "channel_configuration": parse_aac_channel_configuration(stream.get("extradata")),
        "language": tags.get("language"),
        "title": tags.get("title") or tags.get("handler_name"),
        "default": disposition.get("default"),
    }


def conversion_report(report: AppleMediaConversionReport) -> dict[str, object]:
    return {
        "preset": report.preset,
        "source_streams": dict(sorted(report.source_streams.items())),
        "output_streams": dict(sorted(report.output_streams.items())),
        "dropped_streams": dict(sorted(report.dropped_streams.items())),
        "audio_retained": report.output_streams["audio"] >= report.source_streams["audio"],
    }


def run_apple_conversion(source_path: Path, output_path: Path, preset: str) -> dict[str, Any]:
    try:
        return conversion_report(inspect_apple_media_conversion(source_path, output_path, preset=preset))
    except AppleMediaFailure as error:
        return {"preset": preset, "error": str(error), "audio_retained": False}


def decode_float_audio(source_path: Path, output_path: Path) -> int:
    streams = audio_streams(probe(source_path))
    if len(streams) != 1:
        return 0
    channels = streams[0].get("channels")
    if not isinstance(channels, int) or channels <= 0:
        return 0
    run(
        [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source_path,
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "-y",
            output_path,
        ]
    )
    return channels


def rms(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def analyze_identity_samples(
    samples: Sequence[float],
    output_channel_names: Sequence[str],
    source_channel_names: Sequence[str],
    expected_map: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, Any]:
    output_channel_count = len(output_channel_names)
    frame_count = len(samples) // output_channel_count
    observed_map: dict[str, list[str]] = {}
    observed_rms: dict[str, dict[str, float]] = {}
    mismatches: dict[str, dict[str, object]] = {}
    all_passed = expected_map is not None

    for source_index, source_channel in enumerate(source_channel_names):
        start_seconds = IDENTITY_GUARD_SECONDS + source_index * IDENTITY_SLOT_SECONDS
        start_frame = int((start_seconds + IDENTITY_ANALYSIS_INSET_SECONDS) * SAMPLE_RATE)
        end_frame = int((start_seconds + IDENTITY_BURST_SECONDS - IDENTITY_ANALYSIS_INSET_SECONDS) * SAMPLE_RATE)
        end_frame = min(end_frame, frame_count)
        channel_rms = {
            output_name: rms(
                [
                    samples[frame_index * output_channel_count + output_index]
                    for frame_index in range(start_frame, end_frame)
                ]
            )
            for output_index, output_name in enumerate(output_channel_names)
        }
        peak = max(channel_rms.values(), default=0.0)
        active_outputs = tuple(
            output_name
            for output_name, energy in channel_rms.items()
            if energy >= IDENTITY_ABSOLUTE_THRESHOLD and (peak == 0 or energy / peak >= IDENTITY_RELATIVE_THRESHOLD)
        )
        expected_outputs = expected_map.get(source_channel, ()) if expected_map is not None else ()
        missing_outputs = tuple(output for output in expected_outputs if output not in active_outputs)
        unexpected_outputs = (
            tuple(output for output in active_outputs if output not in expected_outputs)
            if expected_map is not None
            else ()
        )
        source_passed = peak >= IDENTITY_ABSOLUTE_THRESHOLD and not missing_outputs and not unexpected_outputs
        if expected_map is not None:
            all_passed = all_passed and source_passed
        observed_map[source_channel] = list(active_outputs)
        observed_rms[source_channel] = {name: round(channel_rms[name], 6) for name in active_outputs}
        if expected_map is not None and not source_passed:
            mismatches[source_channel] = {
                "expected_outputs": list(expected_outputs),
                "observed_outputs": list(active_outputs),
                "peak_rms": round(peak, 6),
                "missing_outputs": list(missing_outputs),
                "unexpected_outputs": list(unexpected_outputs),
            }

    result: dict[str, object] = {
        "status": "passed" if all_passed else ("observed" if expected_map is None else "failed"),
        "output_channel_order": list(output_channel_names),
        "observed_map": observed_map,
        "observed_rms": observed_rms,
    }
    if mismatches:
        result["mismatches"] = mismatches
    return result


def analyze_apple_identity(
    apple_lpcm_path: Path,
    raw_path: Path,
    source_channels: Sequence[str],
    channel_configuration: int | None,
    expected_map: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, object]:
    output_channel_count = decode_float_audio(apple_lpcm_path, raw_path)
    if output_channel_count == 0:
        return {"status": "audio_dropped"}
    output_channel_names = AAC_CHANNEL_CONFIGURATION_LAYOUTS.get(channel_configuration or 0)
    if output_channel_names is None:
        return {
            "status": "pce_not_evaluated",
            "output_channel_count": output_channel_count,
        }
    if len(output_channel_names) != output_channel_count:
        return {
            "status": "channel_count_mismatch",
            "expected_output_channels": list(output_channel_names),
            "actual_output_channel_count": output_channel_count,
        }
    samples = array.array("f")
    samples.frombytes(raw_path.read_bytes())
    return analyze_identity_samples(samples, output_channel_names, source_channels, expected_map)


def build_asset_evidence(
    case: AudioLayoutCase,
    audio_path: Path,
    movie_path: Path,
    video_path: Path,
    artifact_directory: Path,
    label: str,
    expected_map: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, object]:
    mux_video_audio(video_path, audio_path, movie_path)
    aac_evidence = aac_stream_evidence(audio_path)
    passthrough_path = artifact_directory / f"{label}-passthrough.mov"
    apple_lpcm_path = artifact_directory / f"{label}-apple-lpcm.mov"
    passthrough = run_apple_conversion(movie_path, passthrough_path, "PresetPassthrough")
    apple_lpcm = run_apple_conversion(movie_path, apple_lpcm_path, APPLE_LPCM_PRESET)
    identity: dict[str, Any] = {"status": "audio_dropped"}
    if apple_lpcm.get("audio_retained") is True:
        raw_channel_configuration = aac_evidence.get("channel_configuration")
        channel_configuration = raw_channel_configuration if isinstance(raw_channel_configuration, int) else None
        identity = analyze_apple_identity(
            apple_lpcm_path,
            artifact_directory / f"{label}-apple-lpcm.f32",
            case.source_channels,
            channel_configuration,
            expected_map,
        )
    return {
        "aac": aac_evidence,
        "apple_passthrough": passthrough,
        "apple_lpcm_decode": apple_lpcm,
        "identity": identity,
    }


def direct_expected_map(case: AudioLayoutCase) -> Mapping[str, tuple[str, ...]] | None:
    return case.expected_identity_map if case.action == "preserve" else None


def expected_target_configuration(case: AudioLayoutCase) -> int:
    return CANONICAL_AAC_CONFIGURATION[case.target_layout]


def candidate_passed(case: AudioLayoutCase, evidence: Mapping[str, object]) -> bool:
    aac_evidence = evidence.get("aac")
    passthrough = evidence.get("apple_passthrough")
    apple_lpcm = evidence.get("apple_lpcm_decode")
    identity = evidence.get("identity")
    if not all(isinstance(item, Mapping) for item in (aac_evidence, passthrough, apple_lpcm, identity)):
        return False
    assert isinstance(aac_evidence, Mapping)
    assert isinstance(passthrough, Mapping)
    assert isinstance(apple_lpcm, Mapping)
    assert isinstance(identity, Mapping)
    return (
        aac_evidence.get("channel_configuration") == expected_target_configuration(case)
        and aac_evidence.get("ffprobe_layout") == case.target_layout
        and passthrough.get("audio_retained") is True
        and apple_lpcm.get("audio_retained") is True
        and identity.get("status") == "passed"
    )


def qualify_case(case: AudioLayoutCase, root: Path, video_path: Path) -> dict[str, Any]:
    case_directory = root / case.id
    case_directory.mkdir()
    source_path = case_directory / "identity.wav"
    duration_seconds = create_source_identity(source_path, case)

    direct_audio_path = case_directory / "direct.m4a"
    encode_aac(source_path, direct_audio_path, case, apply_policy=False)
    direct_evidence = build_asset_evidence(
        case,
        direct_audio_path,
        case_directory / "direct.mov",
        video_path,
        case_directory,
        "direct",
        direct_expected_map(case),
    )

    if case.action == "preserve":
        candidate_evidence = direct_evidence
        candidate_report: dict[str, Any] = {"same_as_direct": True}
    else:
        candidate_audio_path = case_directory / "candidate.m4a"
        encode_aac(source_path, candidate_audio_path, case, apply_policy=True)
        candidate_evidence = build_asset_evidence(
            case,
            candidate_audio_path,
            case_directory / "candidate.mov",
            video_path,
            case_directory,
            "candidate",
            case.expected_identity_map,
        )
        candidate_report = candidate_evidence

    return {
        "id": case.id,
        "input": {
            "layout": case.source_layout,
            "channel_count": len(case.source_channels),
            "channels": list(case.source_channels),
            "identity_duration_seconds": duration_seconds,
        },
        "decision": {
            "action": case.action,
            "target_layout": case.target_layout,
            "pan_filter": case.pan_filter,
            "expected_identity_map": {source: list(outputs) for source, outputs in case.expected_identity_map.items()},
            "rationale": case.rationale,
        },
        "direct": direct_evidence,
        "candidate": candidate_report,
        "policy_passed": candidate_passed(case, candidate_evidence),
    }


def tool_version(command: Sequence[str | Path]) -> str:
    completed = run(command, capture_output=True)
    return completed.stdout.splitlines()[0] if completed.stdout else "unknown"


def policy_digest() -> str:
    payload = {
        "cases": [
            {
                **asdict(case),
                "expected_identity_map": {
                    source: list(outputs) for source, outputs in case.expected_identity_map.items()
                },
            }
            for case in LAYOUT_CASES
        ],
        "fallback": UNLISTED_LAYOUT_POLICY,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def runtime_behavior_changed() -> bool:
    expected_copy_layouts = {
        case.source_layout: len(case.source_channels) for case in LAYOUT_CASES if case.action == "preserve"
    }
    return audio.AAC_COPY_LAYOUT_CHANNELS != expected_copy_layouts


def build_report(root: Path) -> dict[str, Any]:
    maximum_duration = 2 * IDENTITY_GUARD_SECONDS + 8 * IDENTITY_SLOT_SECONDS
    video_path = root / "black-video.mov"
    create_black_video(video_path, maximum_duration)
    matrix: list[dict[str, Any]] = []
    for case in LAYOUT_CASES:
        print(f"Qualifying {case.source_layout} -> {case.target_layout} ({case.action})")
        matrix.append(qualify_case(case, root, video_path))
    action_counts = {
        action: sum(1 for case in LAYOUT_CASES if case.action == action) for action in ("preserve", "remap", "downmix")
    }
    action_counts["fail"] = int(UNLISTED_LAYOUT_POLICY["action"] == "fail")
    policy_failures = [entry["id"] for entry in matrix if not entry["policy_passed"]]
    automatic_behavior_changed = runtime_behavior_changed()
    if automatic_behavior_changed:
        policy_failures.append("runtime-behavior-changed")
    return {
        "schema_version": 1,
        "qualification_id": "apple-aac-layouts-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "toolchain": {
            "platform": platform.platform(),
            "macos": platform.mac_ver()[0],
            "architecture": platform.machine(),
            "ffmpeg": tool_version([config.FFMPEG_PATH, "-version"]),
            "ffprobe": tool_version([config.FFPROBE_PATH, "-version"]),
            "avconvert": shutil.which("avconvert"),
        },
        "policy": {
            "policy_sha256": policy_digest(),
            "automatic_behavior_changed": automatic_behavior_changed,
            "current_runtime_snapshot": {
                "aac_copy_layout_channels": audio.AAC_COPY_LAYOUT_CHANNELS,
                "aac_layout_policy_sha256": policy_digest(),
            },
            "unlisted_layout": UNLISTED_LAYOUT_POLICY,
            "action_counts": action_counts,
        },
        "matrix": matrix,
        "summary": {
            "case_count": len(matrix),
            "channel_counts_covered": sorted({len(case.source_channels) for case in LAYOUT_CASES}),
            "direct_apple_audio_retained": sum(
                1 for entry in matrix if entry["direct"]["apple_passthrough"].get("audio_retained") is True
            ),
            "policy_passed": len(matrix) - len(policy_failures),
            "policy_failures": policy_failures,
        },
    }


def render_policy_markdown(report: Mapping[str, object]) -> str:
    matrix = report["matrix"]
    assert isinstance(matrix, list)
    rows = []
    for entry in matrix:
        assert isinstance(entry, Mapping)
        input_data = entry["input"]
        decision = entry["decision"]
        direct = entry["direct"]
        assert all(isinstance(item, Mapping) for item in (input_data, decision, direct))
        direct_aac = direct["aac"]
        direct_apple = direct["apple_passthrough"]
        assert all(isinstance(item, Mapping) for item in (direct_aac, direct_apple))
        channel_configuration = direct_aac.get("channel_configuration")
        configuration_label = "PCE (0)" if channel_configuration == 0 else str(channel_configuration)
        direct_result = "retained" if direct_apple.get("audio_retained") is True else "dropped"
        candidate_result = "pass" if entry["policy_passed"] else "FAIL"
        rows.append(
            (
                "| {channels} | `{layout}` | {configuration} | {direct_result} | "
                "**{action}** | `{target}` | {candidate} |"
            ).format(
                channels=input_data["channel_count"],
                layout=input_data["layout"],
                configuration=configuration_label,
                direct_result=direct_result,
                action=decision["action"],
                target=decision["target_layout"],
                candidate=candidate_result,
            )
        )

    transforms = []
    for entry in matrix:
        decision = entry["decision"]
        if decision["pan_filter"] is not None:
            transforms.append(
                f"- `{entry['input']['layout']}` → `{decision['target_layout']}`: `{decision['pan_filter']}`"
            )

    toolchain = report["toolchain"]
    summary = report["summary"]
    assert isinstance(toolchain, Mapping)
    assert isinstance(summary, Mapping)
    generated_summary = (
        f"Generated on **{report['generated_at']}** with `{toolchain['ffmpeg']}` on macOS "
        f"`{toolchain['macos']}` ({toolchain['architecture']}). The checked-in evidence is "
        "`docs/qualification/apple-aac-layouts-v1.json`."
    )
    return (
        "\n".join(
            [
                "# Apple-Compatible AAC Channel-Layout Policy",
                "",
                "## Decision",
                "",
                (
                    "Use an explicit **preserve / remap / downmix / fail** policy. FFmpeg decode "
                    "success and audio-track count are not sufficient evidence of Apple compatibility. "
                    "Every supported output must use a canonical AAC channel configuration, survive "
                    "both Apple passthrough and Apple LPCM decode, and preserve the expected per-channel "
                    "identity map."
                ),
                "",
                (
                    "The production runtime imports this same policy for Automatic and Convert-to-AAC processing. "
                    "Signed-package and physical Vision Pro validation remains in #382."
                ),
                "",
                "## Evidence Method",
                "",
                "- Generate a unique semantic frequency and isolated time slot for every input channel.",
                (
                    "- Encode the input layout directly with FFmpeg, mux it with a bounded H.264 video "
                    "track, and inspect the AAC AudioSpecificConfig."
                ),
                "- Run Apple `PresetPassthrough` to detect tracks that AVFoundation silently drops.",
                ("- Run Apple `PresetAppleProRes422LPCM` and measure the decoded output-channel energy matrix."),
                (
                    "- Apply the explicit candidate transform and require a canonical AAC configuration, "
                    "retained Apple audio, and the expected identity map."
                ),
                "",
                generated_summary,
                "",
                "## Policy Matrix",
                "",
                "| Channels | Input | Direct AAC config | Apple direct | Policy | Target | Candidate |",
                "| ---: | --- | ---: | --- | --- | --- | --- |",
                *rows,
                "",
                (
                    "Any missing, unknown, custom, discrete, or unlisted layout **fails** until a new "
                    "matrix row and Apple identity result are reviewed."
                ),
                "",
                "## Important Findings",
                "",
                (
                    "- Canonical AAC configurations 1-6 (`mono` through `5.1`) retain audio and pass "
                    "semantic identity checks."
                ),
                (
                    "- Most FFmpeg PCE outputs (`channel_configuration=0`) become video-only after Apple "
                    "passthrough. `quad` is retained, but remains a downmix because PCE placement has no "
                    "physical qualification."
                ),
                (
                    "- FFmpeg `7.1` uses channel configuration 7 and remains visible, but Apple decodes it "
                    "in AAC wide order. The identity matrix shows front, side, and rear signals in different "
                    "semantic positions, so the policy downmixes it to `5.1` pending physical evidence."
                ),
                (
                    "- `5.0(side)` and `5.1(side)` have lossless one-to-one remaps to canonical `5.0` and "
                    "`5.1`; all uncommon same-count mappings remain forbidden."
                ),
                (
                    f"- The policy gate passed **{summary['policy_passed']} / {summary['case_count']}** "
                    "generated candidates."
                ),
                "",
                "## Explicit Transforms",
                "",
                *transforms,
                "",
                "## Runtime Boundary",
                "",
                (
                    "`bd_to_avp/modules/aac_layout_policy.py` is the shared source of truth for this table. "
                    "Automatic copies only the six qualified preserve layouts; any selected remap or downmix causes "
                    "the whole selected set to be encoded with per-output policy filters. Missing, unknown, custom, "
                    "discrete, or unlisted layouts stop before FFmpeg with a structured failure, and resumed prepared "
                    "AAC is requalified before final muxing. #382 must validate the exact signed package on Vision Pro "
                    "before this policy is considered physically validated."
                ),
                "",
                *PACKAGED_FIXTURE_GATE_LINES,
                "",
                "## Regenerate",
                "",
                "```bash",
                "uv run python scripts/qualify_apple_aac_layouts.py --force",
                "```",
            ]
        )
        + "\n"
    )


def ensure_output_available(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"Output exists; pass --force to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qualify AAC channel layouts through FFmpeg and Apple's media stack.")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--force", action="store_true", help="Replace existing report files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    json_output = args.json_output.expanduser().resolve()
    markdown_output = args.markdown_output.expanduser().resolve()
    ensure_output_available(json_output, args.force)
    ensure_output_available(markdown_output, args.force)
    require_tools()

    with tempfile.TemporaryDirectory(prefix="bd-to-avp-aac-layouts-") as temp_directory:
        report = build_report(Path(temp_directory))
    json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(render_policy_markdown(report), encoding="utf-8")
    failures = report["summary"]["policy_failures"]
    if failures:
        print(f"AAC layout qualification failed: {', '.join(failures)}")
        return 1
    print(f"AAC layout qualification passed: {json_output}")
    print(f"Policy written: {markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
