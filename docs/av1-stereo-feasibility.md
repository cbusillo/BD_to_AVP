# AV1 Stereo Feasibility

## Decision

BD_to_AVP should not add an AV1 output mode for stereoscopic or Apple spatial
video.

AV1 can encode a frame-packed side-by-side or over-under raster, and the
packaged FFmpeg build already contains software AV1 encoders. That does not make
AV1 an interoperable replacement for MVC or MV-HEVC:

- AV1 spatial layers and operating points describe generic scalable video, not
  left-eye and right-eye views.
- The AOM AV1 ISO Base Media File Format binding does not define the advanced
  multi-track, layer-extraction, or view-selection model needed for portable
  stereoscopic playback.
- Apple's native multi-image encode and decode APIs are explicitly MV-HEVC
  APIs. Apple documents MV-HEVC as the spatial-video delivery path.
- Apple file-format metadata can describe frame-packed stereo for codecs other
  than MV-HEVC, but there is no documented Apple playback contract that makes
  frame-packed AV1 a RealityKit spatial video.
- AV1 hardware support is fragmented across the Apple devices this project
  targets, and Apple exposes no VideoToolbox AV1 encoder in the macOS 27 SDK.

MV-HEVC therefore remains the only supported 3D output. A flat-compatible AV1
side-by-side file could be revisited as a separate non-spatial export only if a
concrete user need justifies another format and its support burden.

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

### Apple metadata does not prove Apple spatial playback

Apple's [Stereo Video ISOBMFF Extensions][apple-stereo-isobmff] define
`StereoVideoInfoBox` (`svmi`). The box can identify frame-packed side-by-side or
over-under video, and the specification allows the descriptors to be used with
codecs other than MV-HEVC.

That metadata describes how two pictures are packed. It does not define AV1
view prediction, and Apple does not document AV1 as an input to
`VideoPlayerComponent`'s stereo spatial presentation. Apple's supported
conversion guidance instead converts side-by-side 3D into
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

Apple's [M2 media-engine documentation][apple-m2] lists H.264, HEVC, and ProRes
but not AV1. Apple introduced AV1 hardware decode in the
[M3 media engine][apple-m3]. Apple's [M5 Apple Vision Pro announcement][apple-vision-pro-m5-newsroom]
and [technical specifications][apple-vision-pro-m5] list AV1 hardware decode.
The original M2 Apple Vision Pro therefore cannot be assumed to have an AV1
hardware path. Software fallback availability and performance are not a safe
product contract for full-resolution stereo playback.

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

Basic AV1 encoding would not require adding another shipped codec binary, but a
product implementation would still require a new video-mode contract across
the CLI, worker protocol, profiles, both UIs, restart stages, summaries, tests,
and clean-machine release gates. That cost is not justified by a flat output
that cannot replace native spatial playback.

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

### Apple `svmi` metadata on AV1

The AV1 side-by-side MP4 was copied and patched with this GPAC box patch:

```xml
<?xml version="1.0"?>
<GPACBOXES>
  <Box path="trak.mdia.minf.stbl.stsd.av01.av1C+" trackID="1">
    <BS fcc="svmi"/>
    <BS data="000000000002"/>
  </Box>
</GPACBOXES>
```

The `svmi` payload selects frame-packed side-by-side video with the left view
first and permits a monoscopic fallback. MP4Box preserved it as a 14-byte child
of the `av01` sample entry. The trailing `+` in the patch path inserts `svmi`
after `av1C`, making the two boxes siblings within the sample entry.

AVFoundation still decoded and sought the movie as one 640x180 image. The
format description preserved `svmi` in `SampleDescriptionExtensionAtoms`, but
did not expose left-eye, right-eye, or view-packing format-description values in
the tested macOS path. This does not prove what a future or device-specific
RealityKit renderer could do; it proves only that a valid packed AV1 file plus
metadata is not equivalent to the documented MV-HEVC spatial pipeline.

### MV-HEVC control

`scripts/create_spatial_playback_fixture.sh` produced the control movie. Its
video sample entry contained `hvcC`, `lhvC`, `vexu/eyes`, and `hfov`, and
`VTIsStereoMVHEVCDecodeSupported()` returned true. That is the same native
contract exercised by `SpatialPlaybackProbe` and remains the acceptance
baseline.

The tiny synthetic output sizes and encode times are intentionally not used as
a compression-quality comparison. The codecs used different rate-control
modes, and synthetic two-second clips do not predict feature-film quality,
storage, or thermals.

## Result Matrix

| Representation | Tool result | Apple-framework result | Product meaning |
| --- | --- | --- | --- |
| One side-by-side `av01` MP4 track | Encodes and muxes | Playable and seekable as 640x180 | Flat packed video |
| Side-by-side `av01` plus `svmi` | Generic box patch works | Raw box preserved; still decoded as one packed image | Unqualified, undocumented spatial behavior |
| Two independent `av01` MP4 tracks | Encodes and muxes | Two selectable tracks; default track decoded | Alternatives, not eye views |
| AV1 WebM with `StereoMode` | Standards-based packed metadata works | AVFoundation cannot open the container | Non-Apple delivery only |
| MV-HEVC MOV | Existing pipeline works | Native stereo capability and spatial metadata | Supported Apple spatial output |

## Revisit Conditions

Reconsider an AV1 3D mode only if all applicable conditions become true:

1. Apple documents AV1 multiview or frame-packed AV1 as a native
   `VideoPlayerComponent` stereo/spatial input.
2. The minimum supported Apple Vision Pro hardware has a qualified AV1 decode
   path at the target resolution, frame rate, bit depth, and sustained thermal
   load.
3. AOM or Apple defines interoperable eye-view semantics and a production mux
   path that the packaged tools can create and validate.
4. AV1 encoding is fast enough for full feature films without adding an
   unacceptable clean-machine dependency or support burden.
5. A concrete use case needs AV1 rather than the existing MV-HEVC spatial movie
   or a conventional side-by-side archival file.

[av1-spec]: https://aomediacodec.github.io/av1-spec/av1-spec.pdf
[av1-isobmff]: https://aomediacodec.github.io/av1-isobmff/v1.3.0.html
[apple-stereo-isobmff]: https://developer.apple.com/av-foundation/Stereo-Video-ISOBMFF-Extensions.pdf
[apple-sbs-to-mvhevc]: https://developer.apple.com/documentation/avfoundation/converting-side-by-side-3d-video-to-multiview-hevc-and-spatial-video
[apple-m2]: https://www.apple.com/newsroom/2022/06/apple-unveils-m2-with-breakthrough-performance-and-capabilities/
[apple-m3]: https://www.apple.com/newsroom/2023/10/apple-unveils-m3-m3-pro-and-m3-max-the-most-advanced-chips-for-a-personal-computer/
[apple-vision-pro-m5-newsroom]: https://www.apple.com/newsroom/2025/10/apple-vision-pro-upgraded-with-the-m5-chip-and-dual-knit-band/
[apple-vision-pro-m5]: https://support.apple.com/en-us/125436
