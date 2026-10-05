# Real PGS subtitle tests

`tests/test_final_mux_real_tools.py` follows regular and forced PGS tracks
through real FFmpeg extraction, bitmap decoding, SRT creation and MP4Box muxing.
It checks the decoded subtitle text and requires exactly one regular and one
forced output track. The tools require macOS arm64, Xcode, the bundled MP4Box,
FFmpeg and FFprobe. CI installs the two decoders before running the suite.

Run the full test with actual Apple Vision recognition on a supported Mac:

```sh
uv run pytest -p no:cacheprovider -rs tests/test_final_mux_real_tools.py
```

The `xcode-27` hosted checks job records
`BD_TO_AVP_HOSTED_VISION_OCR_SKIP_REASON` for the actual Apple Vision test.
On a GitHub-hosted runner only, that explicit reason skips that single test;
pytest prints the reason in the CI log. Without the reason, including local
and self-hosted runs, the actual OCR test runs and errors fail it. To probe a
future hosted image, remove that variable from the test step's environment.

The known limitation is recorded in [#892](https://github.com/cbusillo/BD_to_AVP/issues/892):
run `37229434438`, attempts 1–4, failed both regular and forced recognition
on image `xcode-27-arm64` / `20260928.0222.1` (macOS 27.0). Vision's
`performRequests_error_` returned false without an NSError. The unchanged test
passed on Chris-Studio. These observations do not establish whether the hosted
backend lacks a model or compute prerequisite, or has an OS defect.

CI still runs the real PGS extraction, bitmap decoding, SRT and final mux check
with recognition substituted at `AppleVisionOcr.image_to_data`. The substitute
requires an exact decoded fixture bitmap before returning its text; missing,
altered or reordered cues and lost forced flags still fail the output checks.
This test does not qualify Apple Vision itself. Actual OCR remains covered by
the default full test on a supported Mac. No production OCR behavior changes.
