# Direct MV-HEVC prerelease gate

Issue #364 validates whether the qualified direct MV-HEVC architecture can become the released Automatic route. The
answer from the July 24, 2026 gate is **not yet**: packaged routing and playback preparation pass, and every gated
content case has a defensible quality-matched direct result, but one fixed Automatic bitrate cannot preserve those size
results across different content complexity.

## Decision

- Keep the released generated route as the default.
- Do not promote or release the provisional 40 Mbps direct Automatic policy.
- Preserve the direct architecture and custom bitrate contract; their routing, packaging, preview, fallback, and media
  behavior passed.
- Continue the promotion work in #366 with content-adaptive Automatic rate control. Do not weaken the corpus
  thresholds to retain one fixed bitrate.
- Keep broader noncanonical multichannel AAC policy in #367; this gate changes only the physically proven
  `5.1(side)` case.

The current protocol-v10 implementation on `main` still selects direct for eligible Automatic jobs. It remains
unreleased evidence until #366 replaces the fixed policy and this gate is rerun.

## Representative corpus

`docs/qualification/direct-mv-hevc-corpus-v1.json` defines seven gated cases and one informational public conformance
case. Private source paths are supplied through environment variables and are never written to evidence. The committed
manifest records only stable case identifiers, transforms, and coverage tags.

The corpus covers:

- real 8-bit MVC motion, rain/grain, dark scenes, snow/fine detail, and disparity;
- per-eye crop and frame-rate override transforms;
- a deterministic animation/disparity control;
- the public ITU-T H.264.1 `MVCDS-2` MVC vector as a non-gating decoder stress control;
- explicit rejection policy for 10- and 12-bit sources, which are outside the native MVC/direct encoder contract.

The numerical gate uses the minimum decoded same-eye SSIM across both eyes. Every selected direct run must remain
within 0.002 of the generated median, preserve the case-specific same-eye-versus-cross-eye margin, and stay within 5%
of the generated median movie size. Candidate search includes the first measurement in a three-run validation set;
any quality, eye-order, or size failure disqualifies that candidate and continues the ordered search. Three generated
and three selected-direct runs record variance. A proposed Automatic policy is then encoded once against every gated
case and must stay within 10% of generated size. Required coverage is satisfied only by gated cases; the public
conformance vector cannot make a release dimension appear covered.

| Gated case | Matched direct target | Worst-eye SSIM delta | Direct/generated size |
| --- | ---: | ---: | ---: |
| Dark | 5 Mbps | -0.001620 | 70.75% |
| Rain and grain | 16 Mbps | -0.001915 | 102.88% |
| Snow and fine detail | 2 Mbps | -0.001280 | 58.21% |
| High motion | 8 Mbps | -0.001608 | 77.27% |
| Crop | 3 Mbps | -0.000802 | 72.70% |
| Frame-rate override | 4 Mbps | -0.001699 | 44.97% |
| Animation control | 0.5 Mbps | +0.005921 | 24.77% |

All seven matched quality/size gates pass. The required targets span 0.5 to 16 Mbps, which invalidates a single fixed
target as the Automatic policy.

The fixed-policy derivation selects 16 Mbps because rain/grain is the hardest case. At 16 Mbps, only rain/grain passes
the policy-size gate. The other cases become 1.55 to 3.24 times the generated size despite equal or better quality:

| Policy verification case | 16 Mbps direct/generated size |
| --- | ---: |
| Crop | 324.03% |
| Dark | 210.52% |
| Motion | 154.95% |
| Frame-rate override | 175.49% |
| Snow and fine detail | 285.57% |
| Animation control | 245.99% |

The corpus evidence SHA-256 is
`a7ac4a5f316da91d2dce79292058121217c01f865eb43cb4ee9ac85638c74bbd`. The exact manifest SHA-256 recorded by the
runner is `d6cfc14cb9f29e14639d40a96516fe14f47a64df850fe0928833b77b077e87b1`.

## Packaged route gate

`scripts/verify_packaged_mv_hevc_routes.py` runs one real 65-second MVC source through the packaged protocol-v10
worker four times:

1. supported direct full conversion;
2. supported direct finalized preview;
3. controlled valid-unavailable helper full conversion;
4. controlled valid-unavailable helper finalized preview.

The unavailable-capability package is an APFS clone of the real app with a compiled arm64, macOS-26 helper that emits
the production capability contract (`supported=false`, exit 2). The clone is ad-hoc re-signed and deep-verified before
execution. No production route flag or test-only product behavior is added.

Both direct jobs report the same `direct_mv_hevc` route. Both fallback jobs report the same `generated_mv_hevc` route,
`stereo_mv_hevc_encode_unavailable` reason, and `pre_input` timing. All four outputs pass Apple passthrough with
video/audio/subtitle track-count preservation, required spatial-box checks, beginning/middle/end seeks, and independent
stream inspection.

The first physical direct fixture exposed a release-gate defect that ordinary FFmpeg inspection missed. Its source
`5.1(side)` audio became AAC with MPEG-4 `channel_configuration=0` and a Program Config Element. FFmpeg decoded the
track, but AVFoundation exposed zero audio tracks and Vision Pro was silent. Audio preparation now disqualifies
`5.1(side)` from AAC copy and normalizes transcoded output to standard `5.1`; FFmpeg inserts the required channel remap
before AAC encoding. The Apple verifier now compares source and `avconvert` passthrough track counts, so silent track
drops fail the packaged gate. The rebuilt fixture exposes one audio option in both macOS AVFoundation and visionOS.

| Packaged full output | Size | Route |
| --- | ---: | --- |
| Direct, provisional 40 Mbps | 330,860,823 bytes | `direct_mv_hevc` |
| Generated fallback | 45,629,113 bytes | `generated_mv_hevc` |

The packaged evidence SHA-256 is
`879c433bf41f7a37a4762b83f05d1a2705bd4366a054b9fb69ccf12e07056656`. The package-produced physical fixture is
330,860,823 bytes with SHA-256 `6b161b286f21deb16b7851bdb054fb42d6c27cc080f958a90b82c193b8667e79`.

## Physical and rollback gates

The exact package-produced fixture is prepared as `build/issue-364-packaged/Probe.mov`, copied to the existing physical
Vision Pro validator, and read back byte-for-byte with the same SHA-256. The validator identifies that exact hash,
reports one audio option and two subtitle options, reaches ready stereoscopic screen playback, and passes fresh
beginning/middle/end seeks. Fresh wearer observations confirm audible audio, visible English subtitles, continuously
visible playback, and comfortable non-inverted depth. The schema-v3 physical report passes on visionOS 27.0 build
24M5326g with SHA-256 `d72a4fec4ad31499910c5d175ff2331ecb55af62d3ad84c4037c1add02b2afcf`.

Profile rollback remains additive rather than destructive. Profile document version 4 writes the current nested
MV-HEVC intent and the stable `hevcQuality`, `leftRightBitrate`, and `linkQuality` mirror keys. Native tests decode the
persisted options with the stable v4 shape and reject divergent current/mirror state.

## Reproduction

Provide the private production MVC source and the extracted ITU `MVCDS-2.264` vector without placing either in the
repository:

```bash
export BD_TO_AVP_RELEASE_MVC_SOURCE=/absolute/path/to/representative-3d-source.mkv
export BD_TO_AVP_ITU_MVC_VECTOR=/absolute/path/to/MVCDS-2.264

uv run python scripts/build_mv_hevc_encoder_macos.py \
  --output build/issue-364-corpus/mv-hevc-encoder

uv run python -m scripts.qualify_mv_hevc_corpus \
  --manifest docs/qualification/direct-mv-hevc-corpus-v1.json \
  --encoder build/issue-364-corpus/mv-hevc-encoder \
  --output build/issue-364-corpus/evidence.json

uv run python -m scripts.verify_packaged_mv_hevc_routes \
  --app /absolute/path/to/3D\ Blu-ray\ to\ Vision\ Pro.app \
  --source /absolute/path/to/representative-65-second.mkv \
  --output build/issue-364-packaged/evidence.json \
  --fixture-output build/issue-364-packaged/Probe.mov
```

Evidence artifacts intentionally remain build outputs because they contain fingerprints of private source segments.
The committed scripts, manifest, thresholds, and result summary are sufficient to reproduce and audit the gate without
publishing media or source paths.
