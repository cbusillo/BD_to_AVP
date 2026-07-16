# Direct SSIF Prototype

## Status

Issue #209 owns a development-only prototype for unencrypted and
already-decrypted Blu-ray ISO and BDMV sources. This first slice proves the
unencrypted ISO path and deliberately rejects any AACS or BD+ marker;
compatibility with BDMV folders and already-decrypted images that retain those
markers remains unproven. The prototype does not replace the production MakeMKV
path, change worker protocol v4, alter the native app, or ship a new binary.

The prototype proves a narrower source contract:

1. `libbluray` and `libudfread` open a source without mounting it.
2. An explicit playlist resolves to one SSIF-backed clip.
3. The SSIF transport exposes base H.264 PID `0x1011`, dependent MVC PID
   `0x1012`, and the playlist audio/subtitle inventory.
4. Base and dependent PES units are paired by DTS and emitted base-first as one
   decode-order Annex-B MVC stream.
5. The existing patched `edge264_test` accepts that stream through stdin.

## Build

The development build requires Apple Silicon macOS, `pkg-config`, libbluray
`1.4.1`, and libudfread `1.2.0`:

```bash
brew install libbluray pkgconf
uv run python scripts/build_ssif_probe_macos.py
```

The build intentionally enforces those exact development-library versions. A
Homebrew formula update therefore fails CI until the recorded compatibility
baseline is reviewed and updated.

The binary is written to `build/ssif-probe/ssif_probe`, which is ignored and is
not included in wheels, Briefcase, or the native preview app.

## Commands

Inspect one explicit playlist:

```bash
build/ssif-probe/ssif_probe inspect /absolute/path/to/source.iso 1005
```

The JSON result reports encryption flags, 3D metadata, duration, main-feature
status, clip timing, SSIF size, and stable video/audio/PGS stream metadata. The
prototype is eligible only when the source is unencrypted, declares 3D content,
and the playlist has one angle and exactly one SSIF-backed clip that covers the
complete underlying CLPI presentation range. Trimmed or reused clip ranges fail
with `partial_clip_unsupported`. CLPI presentation bounds use a 45 kHz clock;
the helper converts them to the playlist's 90 kHz clock before comparing
`in_ticks` and `out_ticks`.

Stream a bounded number of stereo frame pairs as combined MVC Annex B:

```bash
build/ssif-probe/ssif_probe stream-mvc /absolute/path/to/source.iso 1005 116 \
  | bd_to_avp/bin/edge264_test - -Osk \
  | ffmpeg -f yuv4mpegpipe -i pipe:0 -frames:v 100 -f framemd5 first-100.framemd5
```

The bound counts paired MVC access units rather than decoded output frames. A
small decoder lookahead is therefore required when a downstream command stops
at an exact frame count. `stream-mvc` treats a downstream pipe close as normal
bounded completion.

`stream-mvc` buffers only unmatched PES units, limits each active PES to 16 MiB,
caps queued allocation capacity at 256 MiB, rejects non-monotonic DTS, and
writes base data before its matching dependent-view data. The two active PES
builders can add at most 32 MiB beyond that queue cap. One process owns one
request, so pipe closure or process-group termination remains the cancellation
boundary.

`demux-file` applies the same reconstruction to an already accessible M2TS or
SSIF sample and exists for deterministic synthetic tests:

```bash
build/ssif-probe/ssif_probe demux-file sample.m2ts 100
```

## Rainforest Evidence

The local reference source is:

```text
/Volumes/Docker-External/BD_to_AVP_artifacts/tester-iso/
Secrets of the Rainforest (2013) 3D.iso
```

Observed playlist `01005` evidence:

- duration: `3240.111867` seconds
- one clip: `00007`
- SSIF size: `16,970,784,768` bytes
- base PID: `0x1011`
- dependent PID: `0x1012`
- DTS-HD MA audio: German `0x1100`, English `0x1101`
- no AACS or BD+ markers

A 1 GiB bounded stream reconstructed 4,380 matched MVC frame pairs and completed
through `edge264_test -Osk` with no decode failure. The first 100 decoded stereo
frames exactly matched the prefix of the accepted upstream-validation fixture;
the generated framemd5 SHA-256 is
`7ce83ff76fa9998967932874364907dfd8c45482f89db9265c474cbd65c228ae`.

The reviewed helper also produced the complete first 15,000-frame framemd5 in
`231.7` seconds. The helper exited `0`, `edge264_test` exited on the expected
downstream `SIGPIPE` after FFmpeg reached the exact frame bound, and FFmpeg
exited `0`. The output contained `15,010` lines and matched the accepted
SHA-256 exactly:

```text
9ada30b0ef6e4b21c73bcb6cc92f66fee0c2b2e197c82423870536cfe6ab7103
```

That run read `17,930,748` M2TS packets, emitted `15,002` matched PES pairs
before downstream closure, and peaked at `16,646,144` bytes of unmatched buffered
data.

Set `BD_TO_AVP_RAINFOREST_ISO` to run the environment-gated real-media tests.
Ordinary CI uses synthetic M2TS packets and does not require copyrighted media.

## Deliberate Limits

- Explicit playlist selection only; automatic provider-neutral title discovery
  remains future work.
- Single clip and single angle only; seamless branching and multi-clip timing
  remain unsupported.
- No physical-disc path, keyfile, AACS, BD+, or encrypted-media support.
  Already-decrypted images that retain AACS or BD+ metadata are also rejected
  until that boundary has dedicated evidence.
- BDMV folder paths are accepted by the helper but do not yet have real-media
  parity evidence against the validated ISO.
- No audio or PGS fan-out yet; inspection records their stable metadata only.
- The demux currently requires the Blu-ray 3D primary/dependent PID pair
  `0x1011`/`0x1012`; runtime PMT discovery remains a promotion gate. A source
  without that pair fails with `mvc_pids_unavailable` rather than producing an
  empty stream.
- No crop detection, preview seeking, restart cache, subtitle OCR, final muxing,
  or production worker dispatch.
- No claim that MakeMKV can be removed from supported encrypted-disc workflows.

## Packaging And Licensing

The tested development binary dynamically links Homebrew libraries and must not
be distributed. libbluray and libudfread are LGPL-2.1-or-later; their tested
versions, source URLs, and source-archive checksums are recorded in
`vendor/ssif-probe-macos-arm64.toml`.

A distributable implementation must build arm64 dylibs from the pinned sources,
target the app's supported minimum macOS, place the replaceable libraries in the
app bundle, rewrite install names to bundle-relative paths, include LGPL notices
and source/relinking obligations, sign every Mach-O, and pass notarization and
Gatekeeper checks. The current Homebrew dylibs target macOS 26 and are unsuitable
for the production macOS 14 Briefcase line.

## Promotion Gates

Production integration remains a separate change. Completed evidence:

- accepted first-15,000-frame framemd5 SHA-256
  `9ada30b0ef6e4b21c73bcb6cc92f66fee0c2b2e197c82423870536cfe6ab7103`.

Remaining promotion gates:

- deterministic multi-clip playlist timing and seek/replay behavior;
- selected audio and PGS fan-out with language/default/forced semantics;
- cancellation and child cleanup under the worker process owner;
- a seekable or replayable source contract for crop, preview, retry, and resume;
- bundled-library licensing, deployment-target, signing, and notarization proof;
- unchanged MakeMKV fallback for encrypted and unsupported sources.
