# Apple-Compatible AAC Channel-Layout Policy

## Decision

Use an explicit **preserve / remap / downmix / fail** policy. FFmpeg decode success and audio-track count are not sufficient evidence of Apple compatibility. Every supported output must use a canonical AAC channel configuration, survive both Apple passthrough and Apple LPCM decode, and preserve the expected per-channel identity map.

This qualification slice does **not** change Automatic runtime behavior. Production implementation belongs to #381, and signed-package/physical validation belongs to #382.

## Evidence Method

- Generate a unique semantic frequency and isolated time slot for every input channel.
- Encode the input layout directly with FFmpeg, mux it with a bounded H.264 video track, and inspect the AAC AudioSpecificConfig.
- Run Apple `PresetPassthrough` to detect tracks that AVFoundation silently drops.
- Run Apple `PresetAppleProRes422LPCM` and measure the decoded output-channel energy matrix.
- Apply the explicit candidate transform and require a canonical AAC configuration, retained Apple audio, and the expected identity map.

Generated on **2026-07-26T21:39:19.217810+00:00** with `ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers` on macOS `27.0` (arm64). The checked-in evidence is `docs/qualification/apple-aac-layouts-v1.json`.

## Policy Matrix

| Channels | Input | Direct AAC config | Apple direct | Policy | Target | Candidate |
| ---: | --- | ---: | --- | --- | --- | --- |
| 1 | `mono` | 1 | retained | **preserve** | `mono` | pass |
| 2 | `stereo` | 2 | retained | **preserve** | `stereo` | pass |
| 3 | `2.1` | PCE (0) | dropped | **downmix** | `stereo` | pass |
| 3 | `3.0` | 3 | retained | **preserve** | `3.0` | pass |
| 3 | `3.0(back)` | PCE (0) | dropped | **downmix** | `stereo` | pass |
| 4 | `4.0` | 4 | retained | **preserve** | `4.0` | pass |
| 4 | `quad` | PCE (0) | retained | **downmix** | `stereo` | pass |
| 4 | `quad(side)` | PCE (0) | dropped | **downmix** | `stereo` | pass |
| 4 | `3.1` | PCE (0) | dropped | **downmix** | `stereo` | pass |
| 5 | `5.0` | 5 | retained | **preserve** | `5.0` | pass |
| 5 | `5.0(side)` | PCE (0) | dropped | **remap** | `5.0` | pass |
| 5 | `4.1` | PCE (0) | dropped | **downmix** | `stereo` | pass |
| 6 | `5.1` | 6 | retained | **preserve** | `5.1` | pass |
| 6 | `5.1(side)` | PCE (0) | dropped | **remap** | `5.1` | pass |
| 6 | `6.0` | PCE (0) | dropped | **downmix** | `5.0` | pass |
| 6 | `6.0(front)` | PCE (0) | dropped | **downmix** | `5.0` | pass |
| 6 | `hexagonal` | PCE (0) | dropped | **downmix** | `5.0` | pass |
| 7 | `6.1` | PCE (0) | dropped | **downmix** | `5.1` | pass |
| 7 | `6.1(back)` | PCE (0) | dropped | **downmix** | `5.1` | pass |
| 7 | `6.1(front)` | PCE (0) | dropped | **downmix** | `5.1` | pass |
| 7 | `7.0` | PCE (0) | dropped | **downmix** | `5.0` | pass |
| 7 | `7.0(front)` | PCE (0) | dropped | **downmix** | `5.0` | pass |
| 8 | `7.1` | 7 | retained | **downmix** | `5.1` | pass |
| 8 | `7.1(wide)` | PCE (0) | dropped | **downmix** | `5.1` | pass |
| 8 | `7.1(wide-side)` | PCE (0) | dropped | **downmix** | `5.1` | pass |
| 8 | `octagonal` | PCE (0) | dropped | **downmix** | `5.0` | pass |

Any missing, unknown, custom, discrete, or unlisted layout **fails** until a new matrix row and Apple identity result are reviewed.

## Important Findings

- Canonical AAC configurations 1-6 (`mono` through `5.1`) retain audio and pass semantic identity checks.
- Most FFmpeg PCE outputs (`channel_configuration=0`) become video-only after Apple passthrough. `quad` is retained, but remains a downmix because PCE placement has no physical qualification.
- FFmpeg `7.1` uses channel configuration 7 and remains visible, but Apple decodes it in AAC wide order. The identity matrix shows front, side, and rear signals in different semantic positions, so the policy downmixes it to `5.1` pending physical evidence.
- `5.0(side)` and `5.1(side)` have lossless one-to-one remaps to canonical `5.0` and `5.1`; all uncommon same-count mappings remain forbidden.
- The policy gate passed **26 / 26** generated candidates.

## Explicit Transforms

- `2.1` → `stereo`: `pan=stereo|FL<FL+0.5*LFE|FR<FR+0.5*LFE`
- `3.0(back)` → `stereo`: `pan=stereo|FL<FL+0.707*BC|FR<FR+0.707*BC`
- `quad` → `stereo`: `pan=stereo|FL<FL+0.707*BL|FR<FR+0.707*BR`
- `quad(side)` → `stereo`: `pan=stereo|FL<FL+0.707*SL|FR<FR+0.707*SR`
- `3.1` → `stereo`: `pan=stereo|FL<FL+0.707*FC+0.5*LFE|FR<FR+0.707*FC+0.5*LFE`
- `5.0(side)` → `5.0`: `pan=5.0|FL=FL|FR=FR|FC=FC|BL=SL|BR=SR`
- `4.1` → `stereo`: `pan=stereo|FL<FL+0.707*FC+0.5*LFE+0.707*BC|FR<FR+0.707*FC+0.5*LFE+0.707*BC`
- `5.1(side)` → `5.1`: `pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=SL|BR=SR`
- `6.0` → `5.0`: `pan=5.0|FL=FL|FR=FR|FC=FC|BL<SL+0.707*BC|BR<SR+0.707*BC`
- `6.0(front)` → `5.0`: `pan=5.0|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC<0.5*FLC+0.5*FRC|BL=SL|BR=SR`
- `hexagonal` → `5.0`: `pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+0.707*BC|BR<BR+0.707*BC`
- `6.1` → `5.1`: `pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<SL+0.707*BC|BR<SR+0.707*BC`
- `6.1(back)` → `5.1`: `pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<BL+0.707*BC|BR<BR+0.707*BC`
- `6.1(front)` → `5.1`: `pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC<0.5*FLC+0.5*FRC|LFE=LFE|BL=SL|BR=SR`
- `7.0` → `5.0`: `pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+SL|BR<BR+SR`
- `7.0(front)` → `5.0`: `pan=5.0|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|BL=SL|BR=SR`
- `7.1` → `5.1`: `pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<BL+SL|BR<BR+SR`
- `7.1(wide)` → `5.1`: `pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|LFE=LFE|BL=BL|BR=BR`
- `7.1(wide-side)` → `5.1`: `pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|LFE=LFE|BL=SL|BR=SR`
- `octagonal` → `5.0`: `pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+SL+0.707*BC|BR<BR+SR+0.707*BC`

## Runtime Boundary

The first PR deliberately leaves `AAC_COPY_LAYOUT_CHANNELS` and `AAC_TRANSCODE_LAYOUT_NORMALIZATION` unchanged. #381 must apply this table at the shared audio-planning boundary, emit a structured warning for every remap/downmix/fail decision, reject unqualified AAC copy, and add metadata/final-MOV coverage. #382 must then validate the exact signed package on Vision Pro before Automatic support broadens.

## Regenerate

```bash
uv run python scripts/qualify_apple_aac_layouts.py --force
```
