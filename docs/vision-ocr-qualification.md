# Native Vision PGS OCR qualification

Issue [#893](https://github.com/cbusillo/BD_to_AVP/issues/893) tracks the
hosted Vision request returning false without an NSError. This differs from
the local Qt test teardown crash fixed in #934. A newer local macOS pass does
not qualify the affected OS or explain the hosted failure.

Run the normal product check first:

```sh
uv run pytest -p no:cacheprovider -rs tests/test_final_mux_real_tools.py
```

For a request/backend comparison, run each experiment in a fresh process:

```sh
uv run python -m scripts.qualify_vision_ocr --mode default
uv run python -m scripts.qualify_vision_ocr --mode cpu-only
uv run python -m scripts.qualify_vision_ocr --mode fast
```

Each command runs the existing actual regular/forced PGS extraction, recognition,
SRT and final-mux assertions. It removes the hosted skip and requires real tools;
exit zero means that entire case passed without skips. These commands require
the repository's development dependencies, Xcode and fixture decoders, like the
normal real-tools test. They do not install system tools or change production
OCR settings. The CPU-only and fast modes are experiments, not supported runtime
workarounds. Apple documents [CPU-only execution](https://developer.apple.com/documentation/vision/vnrequest/usescpuonly)
and [recognition levels](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)
as request properties.

Output records macOS build, architecture, Python/PyObjC and selected FFmpeg
provenance, then actual request revision, languages, compute-device availability,
native success/error and recognition results. Available devices do not prove
which device performed the request. Request diagnostics wrap the native handler;
compare the ordinary uninstrumented test before attributing a failure to Vision.

To compare on the current hosted image, manually dispatch `CI` on the task
branch with `vision_ocr_diagnostics=true`. The `vision-ocr-diagnostics` artifact
contains separate process logs and exit statuses. The diagnostic step records
failed experiments without replacing the existing unit-test gate; a green
workflow alone is not native qualification. The matrix is bounded to ten
minutes, and artifact upload runs after a timeout too. Ordinary CI runs do not
pay for this experiment.

Retain the exact source SHA, image version and OS build with any conclusion.
An experiment that passes only after changing recognition settings establishes
an observation, not a production fix or a boundary affecting all users on that
OS. A runtime boundary needs affected physical-Mac evidence or a demonstrated
native prerequisite; the runner label alone cannot establish it.
