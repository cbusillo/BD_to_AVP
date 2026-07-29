# AV1 Stereo Feasibility

## Decision

BD_to_AVP may retain an opt-in, software-encoded AV1 stereo export only as an
experimental M5 Apple Vision Pro path while retaining MV-HEVC as the default
native Apple spatial-video output. Physical testing proves that M2 Apple Vision
Pro cannot decode the AV1 output, even at a small control resolution. Physical
M5 stereoscopic playback remains unqualified.

AV1 can encode a full-resolution side-by-side raster, and the packaged FFmpeg
build contains `libsvtav1`, `libaom-av1`, and `librav1e`. Apple's codec-agnostic
ISOBMFF metadata can identify the packed eyes on an `av01` track. This makes a
distinct AV1 stereo export viable, but it does not make AV1 an interoperable
replacement for MV-HEVC spatial delivery:

- AV1 spatial layers and operating points describe generic scalable video, not
  left-eye and right-eye views.
- The AOM AV1 ISO Base Media File Format binding does not define the advanced
  multi-track, layer-extraction, or view-selection model needed for portable
  stereoscopic playback.
- Apple's native multi-image encode and decode APIs are explicitly MV-HEVC
  APIs. Apple documents MV-HEVC as the spatial-video delivery path.
- Apple file-format metadata describes frame-packed stereo for codecs other
  than MV-HEVC. The corrected probe is recognized by AVFoundation as stereo
  video, but not as multiview-compressed or spatial video.
- AV1 hardware support is fragmented across the Apple devices this project
  targets, and Apple exposes no VideoToolbox AV1 encoder in the macOS 27 SDK.

MV-HEVC therefore remains the native Apple spatial output. AV1 is a separate,
experimental software stereo export for M5 Vision Pro or custom-playback
workflows; the UI and documentation must identify M2 Vision Pro as unsupported
and must not describe AV1 as MV-HEVC or native spatial video.

## Standards Evidence

### AV1 layers are not eye views

The [AV1 Bitstream and Decoding Process Specification][av1-spec] defines a
layer as frames sharing `spatial_id` and `temporal_id`. Operating points select
sets of those generic spatial and temporal layers. The output process normally
selects the highest spatial layer present in a temporal unit.

The specification does not assign left-eye, right-eye, disparity, camera
baseline, or stereoscopic presentation semantics to those layers. Mapping two
spatial layers to two eyes would be a private convention rather than an AV1
interoperability contract.

The [AV1 ISOBMFF binding v1.3.0][av1-isobmff] allows a scalable AV1 stream to be
stored in one `av01` track. Its overview explicitly leaves advanced multi-track
support, layer extraction, and other scalability use cases to a future version.
The binding defines `av01`, `av1C`, one temporal unit per sample, and AV1 sample
groups, but no eye-view selector or multiview track relationship comparable to
MVC or MV-HEVC.

### Apple metadata defines packed stereo independently of the codec

Apple's [Stereo Video ISOBMFF Extensions][apple-stereo-isobmff] define a
`VideoExtendedUsageBox` (`vexu`) that applies to visual sample entries without
requiring MV-HEVC. For full side-by-side video, `vexu` contains:

- `eyes/stri`, which declares that the track carries left and right eye views;
- `pack/pkin`, whose `side` value declares side-by-side packing.

That metadata describes how two pictures are packed. It does not define AV1
inter-view prediction or make the stream multiview-compressed. Apple's native
spatial-video conversion guidance still produces
[MV-HEVC spatial video][apple-sbs-to-mvhevc].

The macOS 27 SDK reinforces the distinction:

- `kCMVideoCodecType_AV1` declares ordinary AV1 media.
- `VTIsHardwareDecodeSupported(kCMVideoCodecType_AV1)` queries AV1 hardware
  decode at runtime.
- `VTIsStereoMVHEVCDecodeSupported()` and
  `VTIsStereoMVHEVCEncodeSupported()` are the stereo capability queries.
- `kVTCompressionPropertyKey_MVHEVCVideoLayerIDs`,
  `kVTCompressionPropertyKey_MVHEVCViewIDs`, and
  `kVTCompressionPropertyKey_MVHEVCLeftAndRightViewIDs` are explicitly
  MV-HEVC-specific.
- `VTCopyVideoEncoderList` exposes no AV1 encoder on the tested M4 Max system.

Apple's [M2 media-engine documentation][apple-m2] and the original
[M2 Vision Pro technical specifications][apple-vision-pro-m2] list H.264,
HEVC, and ProRes but not AV1. Apple introduced AV1 hardware decode in the
[M3 media engine][apple-m3]. Apple's [M5 Apple Vision Pro announcement][apple-vision-pro-m5-newsroom]
and [technical specifications][apple-vision-pro-m5] list AV1 hardware decode.
Physical testing below confirms that the M2 Vision Pro has neither an AV1
hardware path nor a usable AVFoundation software fallback. M5 hardware support
is documented, but its packed-stereo RealityKit behavior still requires
physical evidence.

## Repository And Toolchain Evidence

The current pipeline is intentionally MV-HEVC-specific:

1. `edge264_test` splits MVC into two decoded views.
2. FFmpeg encodes each eye as HEVC.
3. `spatial-media-kit-tool merge` combines those HEVC streams into MV-HEVC and
   writes Apple spatial metadata.
4. MP4Box imports that video into the finalized MOV with audio and subtitles.

The pinned runtime tools provide only part of a hypothetical AV1 path:

- FFmpeg 8.1.2 includes `libaom-av1`, `librav1e`, `libsvtav1`, and AV1 decode.
  All bundled AV1 encoders are software encoders; the build has no
  `av1_videotoolbox` encoder.
- FFmpeg can mux ordinary `av01` MP4 and Matroska/WebM packed-stereo metadata.
  The pinned MOV muxer can write the older `st3d` box when coded stereo side
  data reaches the muxer, but it has no Apple `svmi` or `vexu` authoring path.
  The simple side-by-side probe below carried no coded stereo side data and
  produced no stereo box.
- MP4Box 26.02.0 imports and preserves ordinary `av01` tracks. It does not have
  first-class `svmi` authoring; the probe below used GPAC's generic unknown-box
  patch mechanism.
- `spatial-media-kit-tool` accepts per-eye HEVC and produces MV-HEVC. It is not
  an AV1 multiview packager.

The implemented AV1 path adds no runtime dependency. The native splitter feeds
one FFmpeg `libsvtav1` encode, FFmpeg's `av1_metadata` bitstream filter writes
limited-range BT.709 matrix/primary/transfer signaling, MP4Box applies the
deterministic `vexu/eyes/pack` patch, and the existing MP4Box final mux adds
audio and subtitles to the completed MOV. The CLI, worker protocol, persisted
profiles, native UI, restart stages, summaries, tests, and release gates all
carry the explicit mode without changing the MV-HEVC default.

## Reproducible Probes

### Environment

The probes were run on July 16, 2026 with:

- Apple M4 Max Mac Studio
- macOS 27.0 and Xcode 27.0
- pinned FFmpeg/FFprobe 8.1.2 GPLv3 arm64 build
- bundled MP4Box 26.02.0 arm64 build

The runtime VideoToolbox checks were:

```bash
swift -e 'import CoreMedia; import VideoToolbox; print("AV1 hardware decode=\(VTIsHardwareDecodeSupported(kCMVideoCodecType_AV1))"); print("stereo MV-HEVC decode=\(VTIsStereoMVHEVCDecodeSupported())")'
```

The tested M4 Max returned `true` for both. `VTCopyVideoEncoderList` contained
H.264, HEVC, ProRes, depth, and disparity encoders, but no `av01` encoder.

### Side-by-side AV1 MP4

After vendoring the pinned FFmpeg binaries, a packed AV1 MP4 can be created with:

```bash
mkdir -p /tmp/bd-av1-probe

bd_to_avp/bin/ffmpeg -hide_banner -loglevel error \
  -f lavfi -i 'testsrc2=size=320x180:rate=24:duration=2' \
  -f lavfi -i 'smptebars=size=320x180:rate=24:duration=2' \
  -filter_complex '[0:v][1:v]hstack=inputs=2,format=yuv420p[v]' \
  -map '[v]' -an -c:v libaom-av1 -cpu-used 8 -row-mt 1 \
  -crf 36 -b:v 0 -movflags +faststart \
  /tmp/bd-av1-probe/sbs-av1.mp4
```

FFprobe and MP4Box reported one 640x180 `av01` track. AVFoundation marked the
asset playable, and `AVAssetImageGenerator` decoded beginning, middle, and end
frames at 640x180. It remained one packed image rather than two selectable eye
views.

### Two AV1 tracks

Two independently encoded tracks can also be stored in MP4:

```bash
bd_to_avp/bin/ffmpeg -hide_banner -loglevel error \
  -f lavfi -i 'testsrc2=size=320x180:rate=24:duration=2' \
  -f lavfi -i 'smptebars=size=320x180:rate=24:duration=2' \
  -map 0:v:0 -map 1:v:0 -an \
  -c:v libaom-av1 -cpu-used 8 -row-mt 1 -crf 36 -b:v 0 \
  -metadata:s:v:0 title='Left eye' \
  -metadata:s:v:1 title='Right eye' \
  -disposition:v:0 default -disposition:v:1 0 \
  /tmp/bd-av1-probe/dual-track-av1.mp4
```

AVFoundation exposed two independent 320x180 video tracks and decoded the
enabled default track. Neither AV1 nor the AOM MP4 binding assigns those tracks
stereoscopic view roles.

### WebM packed-stereo metadata

Libaom's `aomenc --stereo-mode` option is compiled only for WebM output and sets
the Matroska `StereoMode` field. With Homebrew `aomenc` 3.14.1, the probe used:

```bash
bd_to_avp/bin/ffmpeg -hide_banner -loglevel error \
  -f lavfi -i 'testsrc2=size=320x180:rate=24:duration=2' \
  -f lavfi -i 'smptebars=size=320x180:rate=24:duration=2' \
  -filter_complex '[0:v][1:v]hstack=inputs=2,format=yuv420p[v]' \
  -map '[v]' -an -f yuv4mpegpipe /tmp/bd-av1-probe/sbs.y4m

aomenc --codec=av1 --passes=1 --cpu-used=8 --threads=8 \
  --end-usage=q --cq-level=36 --webm --stereo-mode=left-right \
  -o /tmp/bd-av1-probe/sbs-av1.webm \
  /tmp/bd-av1-probe/sbs.y4m
```

FFprobe reported `stereo_mode=left_right` and `Stereo 3D: side by side`.
AVFoundation rejected the file with `AVFoundationErrorDomain -11828` because
WebM is not a supported native media container in this path.

### Apple `vexu/eyes/pack` metadata on AV1

The corrected probe patched the AV1 side-by-side MP4 with the current Apple box
structure:

```xml
<?xml version="1.0"?>
<GPACBOXES>
  <Box path="trak.mdia.minf.stbl.stsd.av01.av1C+" trackID="1">
    <BS fcc="vexu"/>
    <BS data="00000015657965730000000D737472690000000003000000187061636B00000010706B696E0000000073696465"/>
  </Box>
</GPACBOXES>
```

MP4Box 26.02 created a 53-byte `vexu` sibling after `av1C`, containing a 21-byte
`eyes` box with mandatory `stri` and a 24-byte `pack` box with mandatory
`pkin=side`. A second MP4Box pass that added the video to the final MOV preserved
the complete unknown box tree.

On macOS 27, AVFoundation exposed `HasLeftStereoEyeView = 1`,
`HasRightStereoEyeView = 1`, and `ViewPackingKind = SideBySide` on the AV1 format
description. `AVAssetPlaybackAssistant` returned
`AVAssetPlaybackConfigurationOptionStereoVideo`; the bare AV1 control returned
no playback configuration options. It did not return
`StereoMultiviewVideo` or `SpatialVideo`.

This proves an Apple-recognized packed-stereo asset contract. It does not prove
stereoscopic rendering on every Apple Vision Pro generation; that device
qualification remains separate in issue #409.

### Implemented production-path probe

On July 17, 2026, the production command generator encoded a native-splitter
Y4M stream with bundled FFmpeg 8.1.2 and SVT-AV1 3.1.2, wrote the AV1 MP4
intermediate, applied the `vexu/eyes/pack` patch with bundled MP4Box 26.02, and
imported the marked track plus AAC audio into the final MOV. Packaged FFprobe
reported one `av01` video track and one `mp4a` audio track. Beginning, middle,
and end frames decoded successfully.

The finalized AV1 stream reports limited range and BT.709 matrix, primaries,
and transfer characteristics. MP4Box's box dump shows `vexu` as a sibling of
`av1C`, containing `eyes/stri` and `pack/pkin=side`. AVFoundation reports both
eye views and `ViewPackingKind = SideBySide`; `AVAssetPlaybackAssistant`
returns only `StereoVideo`, not `StereoMultiviewVideo` or `SpatialVideo`.

### MV-HEVC control

`scripts/create_spatial_playback_fixture.sh` produced the control movie. Its
video sample entry contained `hvcC`, `lhvC`, `vexu/eyes`, and `hfov`, and
`VTIsStereoMVHEVCDecodeSupported()` returned true. That is the same native
contract exercised by the BD to AVP Playback Check target and remains the acceptance
baseline.

### Bounded default comparison

A bounded product-path comparison used the same two-second, 48-frame,
1920x1080-per-eye `testsrc2` source with a 16-pixel horizontal disparity for
the right eye and the same 128 kbps AAC audio. Timing excluded source
generation and included video encoding, mode-specific stereo finalization, and
the final audio mux. Quality was measured per eye after decoding against the
generated source, then averaged.

| Mode | Product settings | Pipeline time | Final MOV size | Mean PSNR | Mean SSIM | AVFoundation playback options |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| MV-HEVC | VideoToolbox HEVC, 20 Mbps per eye, merge quality 75 | 2.201 s | 4,005,130 bytes | 59.934823 dB | 0.999850 | `StereoVideo`, `StereoMultiviewVideo` |
| AV1 stereo | `libsvtav1`, preset 9, CRF 32, full side-by-side | 1.529 s | 3,688,383 bytes | 41.393707 dB | 0.993237 | `StereoVideo` |

This is a pipeline sanity check, not a quality-matched codec comparison. The
MV-HEVC path uses a fixed per-eye bitrate while AV1 uses CRF, the source is a
short synthetic stress pattern, and fixed setup/mux overhead dominates such a
short run. The result cannot predict feature-film speed, size, thermals, or
subjective quality. It does show that both completed defaults are decodable and
that CRF 32 trades measurable quality for the storage-oriented AV1 mode.

### Sustained encoder comparison

On July 29, 2026, a longer encoding-only comparison used the same 60-second,
24 fps, 1920x1080-per-eye synthetic source on an M4 Max Mac Studio. The AV1
route encoded one 3840x1080 packed stream with `libsvtav1`, preset 9, CRF 32.
The hardware HEVC route encoded two 1920x1080 eye streams concurrently with
`hevc_videotoolbox` at 20 Mbps per eye. Both commands processed the same total
pixel count and included the same synthetic source filters.

| Mode | Run 1 | Run 2 | Run 3 | Mean | Relative elapsed time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Software AV1 | 14.16 s | 14.65 s | 14.61 s | 14.47 s | 1.23× hardware HEVC |
| Hardware HEVC eye streams | 11.82 s | 11.82 s | 11.78 s | 11.81 s | baseline |

For this source and machine, the software AV1 encode stage was approximately
23% slower than the two concurrent hardware HEVC eye encodes. Both completed
faster than real time. This is not a quality-matched comparison and excludes
the later MV-HEVC merge, stereo metadata finalization, and final audio/subtitle
mux, so it should not be used as a universal whole-conversion estimate.

The same source was swept at preset 9 to validate the exposed CRF control:

| CRF | Encoded AV1 bytes | Mean PSNR | Mean SSIM |
| ---: | ---: | ---: | ---: |
| 20 | 6,414,286 | 43.268265 dB | 0.996338 |
| 24 | 5,704,307 | 42.900199 dB | 0.995748 |
| 28 | 4,709,320 | 42.283759 dB | 0.994744 |
| 30 | 4,226,448 | 41.908174 dB | 0.994129 |
| 32 | 3,656,352 | 41.393707 dB | 0.993237 |

CRF 32 remains the storage-conscious default; users can lower it when quality
matters more than output size. The sweep is still synthetic and is not a
feature-film recommendation by itself.

### Physical M2 Vision Pro qualification

On July 29, 2026, BD to AVP Playback Check 0.2.0 build 4 tested an immutable
60-second production-contract candidate on physical `RealityDevice14,1`
running visionOS 27.0 build `24M5326g`:

- Source/tool package commit: `1eecfcfc9bd5b64c501976592db5b0516edfb6f4`.
- Packaged app tree SHA-256:
  `c8f053793a0f7f045a35de12e60ed2b46a96118b4ea1ccb77f0069bbbd9e25ef`.
- Candidate: one 3840x1080 `av01` track, two AAC language tracks, English
  subtitles, and Apple `vexu/eyes/pack` metadata.
- Candidate SHA-256:
  `bd67dab8ad3a5f4f88a4c555d576ffbbbec9d29662681bcf7c3ed1b33ee1f3ef`.

The device reported `VTIsHardwareDecodeSupported(kCMVideoCodecType_AV1) =
false` and `VTIsStereoMVHEVCDecodeSupported() = true`. An independent
`AVAssetReader` first-frame probe failed with `Cannot Decode` before RealityKit
was involved. `AVPlayerItem` briefly became ready through a fallback route,
then the RealityKit player also failed with `Cannot Decode` before rendering.

A separate six-second 1280x360 packed AV1 control with SHA-256
`4be7607cdda287b501e245e84681e5ba6962a482235ad36592812319f229a6e6`
failed identically in both the independent AVFoundation decoder probe and the
RealityKit renderer. This rules out full-resolution load and the MV-HEVC-derived
test-player design as the cause. The tested M2 Vision Pro has no usable AV1
decode path for this product.

The M5 Vision Pro lists AV1 hardware decode in Apple's specifications, but no
physical M5 packed-stereo run has been completed. The AV1 option therefore
remains M5-targeted and experimental until an M5 wearer provides decode,
stereoscopic presentation, eye-order, seek, sustained-playback, and thermal
evidence.

## Result Matrix

| Representation | Tool result | Apple-framework result | Product meaning |
| --- | --- | --- | --- |
| One side-by-side `av01` MP4 track | Encodes and muxes | Playable and seekable as 640x180; no stereo playback option | Unmarked packed video |
| Side-by-side `av01` plus `vexu/eyes/pack` | Deterministic GPAC patch and final remux work | macOS recognizes left/right eyes and `StereoVideo`; M2 Vision Pro returns `Cannot Decode` | Experimental M5-targeted AV1 stereo asset, not spatial video |
| Two independent `av01` MP4 tracks | Encodes and muxes | Two selectable tracks; default track decoded | Alternatives, not eye views |
| AV1 WebM with `StereoMode` | Standards-based packed metadata works | AVFoundation cannot open the container | Non-Apple delivery only |
| MV-HEVC MOV | Existing pipeline works | Native stereo multiview and spatial metadata | Default Apple spatial output |

## Product Boundaries And Remaining Qualification

The first AV1 mode is deliberately narrow:

1. Software `libsvtav1` encoding with a fixed practical preset and explicit CRF.
2. One full-resolution side-by-side `av01` track in MOV with Apple packed-stereo
   metadata.
3. No AI FX upscale in the initial AV1 path.
4. No claim that AV1 is MV-HEVC, multiview-compressed, or Apple spatial video.
5. MV-HEVC remains the default and retains the `_AVP.mov` filename; AV1 uses the
   distinct `_AV1_Stereo.mov` suffix.
6. M2 Vision Pro is explicitly unsupported. The UI identifies M5 Vision Pro as
   the experimental target while physical M5 qualification remains pending.

The next physical gate is an M5 Vision Pro run using the immutable candidate or
an equivalent finalized output. Long-form compression, file-size, and
subjective-quality qualification remain separate from the bounded synthetic
comparisons above.

[av1-spec]: https://aomediacodec.github.io/av1-spec/av1-spec.pdf
[av1-isobmff]: https://aomediacodec.github.io/av1-isobmff/v1.3.0.html
[apple-stereo-isobmff]: https://developer.apple.com/av-foundation/Stereo-Video-ISOBMFF-Extensions.pdf
[apple-sbs-to-mvhevc]: https://developer.apple.com/documentation/avfoundation/converting-side-by-side-3d-video-to-multiview-hevc-and-spatial-video
[apple-m2]: https://www.apple.com/newsroom/2022/06/apple-unveils-m2-with-breakthrough-performance-and-capabilities/
[apple-m3]: https://www.apple.com/newsroom/2023/10/apple-unveils-m3-m3-pro-and-m3-max-the-most-advanced-chips-for-a-personal-computer/
[apple-vision-pro-m2]: https://support.apple.com/en-us/117810
[apple-vision-pro-m5-newsroom]: https://www.apple.com/newsroom/2025/10/apple-vision-pro-upgraded-with-the-m5-chip-and-dual-knit-band/
[apple-vision-pro-m5]: https://support.apple.com/en-us/125436
