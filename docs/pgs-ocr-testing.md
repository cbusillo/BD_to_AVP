# Real PGS subtitle tests

The vendored command can also be invoked with
`uv run python -m bd_to_avp.vendor.pgsrip --help`. Its `--config` option requires
an existing configuration file; a missing file or directory exits with a Click
usage error (status 2) before scanning or extraction. Malformed JSON/YAML,
schema-invalid content, non-string YAML mapping keys, undecodable text and custom-file read failures also
report a usage error for `--config`. Invalid custom regexes, missing rule patterns
and invalid rule languages name the failed rule in that diagnostic. Custom replacement
templates are checked against each merged pattern for custom rules and alias-affected
inherited rules before scanning; invalid escapes or
group references name the custom file and rule, before subtitle text is cleaned. The `locale`
flag is also rejected because cleanit's text patterns cannot use it. The file is
loaded directly after path validation, so a vanished file or a FIFO cannot
silently select defaults. Custom rules retain cleanit's default/alias merging;
pre-existing default-rule failures and unexpected programming errors propagate.
Caller and option tests
in `tests/test_vendor_pgsrip_cli.py` use temporary inputs and mock extraction/OCR
boundaries; they do not qualify native Apple Vision recognition.

`tests/test_final_mux_real_tools.py` follows regular and forced PGS tracks
through real FFmpeg extraction, bitmap decoding, SRT creation and MP4Box muxing.
It checks the decoded subtitle text and requires exactly one regular and one
forced output track. The tools require macOS arm64, Xcode, the bundled MP4Box,
FFmpeg and FFprobe. CI installs the two decoders before running the suite.
The checks step sets `BD_TO_AVP_REQUIRE_REAL_PGS_TESTS=1`, so missing tools or
an unavailable MV-HEVC fixture encoder fail instead of skipping this class.

Run the full test with actual Apple Vision recognition on a supported Mac:

```sh
uv run pytest -p no:cacheprovider -rs tests/test_final_mux_real_tools.py
```

The `xcode-27` hosted checks job records
`BD_TO_AVP_HOSTED_VISION_OCR_SKIP_REASON` for the actual Apple Vision test.
The actual OCR test still runs. On a GitHub-hosted runner only, that explicit
reason permits a skip if recognition reproduces the recorded false/no-NSError
failure on both tracks, with no subtitle output and a completed rip. Other
failures remain fatal, and pytest prints the skip reason.
A recovered runner automatically resumes full OCR coverage. Without the reason,
including local and self-hosted runs, every OCR failure fails the test.

The known limitation is recorded in [#892](https://github.com/cbusillo/BD_to_AVP/issues/892):
run `37229434438`, attempts 1–4, failed both regular and forced recognition
on image `xcode-27-arm64` / `20260928.0222.1` (macOS 27.0). Vision's
`performRequests_error_` returned false without an NSError. The unchanged test
passed on Chris-Studio. These observations do not establish whether the hosted
backend lacks a model or compute prerequisite, or has an OS defect.

The separate local test-worker crash diagnosis and remaining native macOS
qualification are tracked in [#893](https://github.com/cbusillo/BD_to_AVP/issues/893).
The hosted false/no-NSError gate above remains unchanged.

CI still runs the real PGS extraction, bitmap decoding, SRT and final mux check
with recognition substituted at `AppleVisionOcr.image_to_data`. The substitute
requires an exact decoded fixture bitmap before returning its text; missing,
altered or reordered cues and lost forced flags still fail the output checks.
This test does not qualify Apple Vision itself. Actual OCR remains covered by
the default full test on a supported Mac. No production OCR behavior changes.
