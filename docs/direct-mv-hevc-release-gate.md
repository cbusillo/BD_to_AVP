# Direct MV-HEVC prerelease gate

Issue #364 proved that one fixed direct MV-HEVC bitrate could not become the released Automatic route. Issue #366
reuses that exact generated-route baseline and representative corpus to qualify content-adaptive VideoToolbox quality
rate control. The July 24, 2026 gate passes: quality `0.7` preserves every gated quality, eye-order, and size limit while
adapting effective bitrate from 2.00 to 13.74 Mbps, and the exact package fixture passes physical Vision Pro playback.

## Decision

- Replace the provisional 40 Mbps Automatic policy with VideoToolbox compression quality `0.7`.
- Preserve Custom as an exact user-owned average-bitrate target.
- Report Automatic as `rate_control: quality` with `quality: 0.7`; never report a fabricated fixed bitrate.
- Keep generated MV-HEVC as the visible pre-input fallback when direct capability is unavailable.
- Do not present a duration-only size estimate for Automatic direct output because size varies with source complexity.
- Keep broader noncanonical multichannel AAC policy in #367; this gate changes only the physically proven
  `5.1(side)` case.

Protocol v10 request shape remains unchanged: Automatic still sends `{ "mode": "automatic" }` with no numeric
sentinel, while the worker owns the qualified policy. Native or streamed 4K upscaling and live processed-frame preview
remain separate future issues.

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

The historical fixed-policy corpus evidence SHA-256 is
`a7ac4a5f316da91d2dce79292058121217c01f865eb43cb4ee9ac85638c74bbd`. The exact manifest SHA-256 recorded by the
runner is `d6cfc14cb9f29e14639d40a96516fe14f47a64df850fe0928833b77b077e87b1`.

`scripts/qualify_mv_hevc_adaptive.py` binds the same manifest and generated-route evidence, then encodes quality `0.7`
three times for every case. All 21 gated runs pass the original, stricter 5% matched-output size limit:

| Gated case | Effective bitrate | Worst-eye SSIM delta | Direct/generated size |
| --- | ---: | ---: | ---: |
| Dark | 5.306578 Mbps | +0.000061 | 79.19% |
| Rain and grain | 13.742263 Mbps | +0.000349 | 87.40% |
| Snow and fine detail | 2.649030 Mbps | -0.000370 | 74.44% |
| High motion | 8.503086 Mbps | +0.000166 | 82.91% |
| Crop | 3.552806 Mbps | +0.000224 | 77.91% |
| Frame-rate override | 7.344460 Mbps | +0.000115 | 77.49% |
| Animation control | 2.004142 Mbps | +0.019105 | 89.98% |

The worst gated quality delta is -0.000370, the smallest eye-order margin is 0.027387, and the largest size ratio is
89.98%. The adaptive evidence SHA-256 is
`dc7468ebcfaeb1552ebf1c70900519faaa5773feea434f81c20443ce99ef4a6f`.

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

Both direct jobs report the same `direct_mv_hevc` route, `rate_control: quality`, and `quality: 0.7`. Both fallback jobs
report the same `generated_mv_hevc` route, `stereo_mv_hevc_encode_unavailable` reason, and `pre_input` timing. All four
outputs pass Apple passthrough with
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
| Direct, adaptive quality 0.7 | 55,517,754 bytes | `direct_mv_hevc` |
| Generated fallback | 66,773,648 bytes | `generated_mv_hevc` |

The adaptive direct output is 16.86% smaller than the generated fallback for this bounded source. The packaged evidence
SHA-256 is `d7ae4a4435646079a2bec827713db521f56ba3856118ba85a43be3b2016ca5eb`. The package-produced physical fixture is
55,517,754 bytes with SHA-256 `1c3ac6b1ca5a4fc3375ee5a0dcd6e37418f9a5779c50503fccdb6af237a264ed`.

## Physical and rollback gates

The #364 package-produced fixture was copied to the existing physical Vision Pro validator and read back byte-for-byte.
The validator identified that exact historical hash,
reports one audio option and two subtitle options, reaches ready stereoscopic screen playback, and passes fresh
beginning/middle/end seeks. Fresh wearer observations confirm audible audio, visible English subtitles, continuously
visible playback, and comfortable non-inverted depth. The schema-v3 physical report passes on visionOS 27.0 build
24M5326g with SHA-256 `d72a4fec4ad31499910c5d175ff2331ecb55af62d3ad84c4037c1add02b2afcf`.

The final #366 fixture with SHA-256 `1c3ac6b1ca5a4fc3375ee5a0dcd6e37418f9a5779c50503fccdb6af237a264ed`
is installed in the same validator. Its fresh schema-v3 report identifies that exact 55,517,754-byte file, reports four
audio options and 100 subtitle options, and passes stereo decode, player readiness, RealityKit rendering, stereoscopic
screen presentation, and beginning/middle/end seeks. The wearer confirmed continuously visible video and
three-dimensional presentation. The report passes on visionOS 27.0 build 24M5326g with SHA-256
`39f87a99b58e752aadeff83ad5318de2818880e4759b843c414ff1d588d33f66`.

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

uv run python -m scripts.qualify_mv_hevc_adaptive \
  --manifest docs/qualification/direct-mv-hevc-corpus-v1.json \
  --baseline /absolute/path/to/issue-364-corpus/evidence.json \
  --encoder build/issue-366-corpus/mv-hevc-encoder \
  --output build/issue-366-corpus/evidence.json \
  --work-directory build/issue-366-corpus/work

uv run python -m scripts.verify_packaged_mv_hevc_routes \
  --app /absolute/path/to/3D\ Blu-ray\ to\ Vision\ Pro.app \
  --source /absolute/path/to/representative-65-second.mkv \
  --output build/issue-366-packaged/evidence.json \
  --fixture-output build/issue-366-packaged/Probe.mov
```

Evidence artifacts intentionally remain build outputs because they contain fingerprints of private source segments.
The committed scripts, manifest, thresholds, and result summary are sufficient to reproduce and audit the gate without
publishing media or source paths.
