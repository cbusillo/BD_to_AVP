# Native Vision PGS OCR qualification

## Runtime boundary

On GitHub's hosted macOS 27.0.1 VM (build 26A434, VirtualMac2,1, paravirtual
GPU and CPU, no Neural Engine), Vision text recognition returns false without
an NSError. A native Swift control fails the same way on the same bitmaps, so
the cause is the VM's Vision backend, not PyObjC or our request setup. A
physical Mac mini (M1) on the same 27.0.1 build passes the real-tools tests and
the full regular/forced OCR, SRT and final-mux qualification. Users on physical
Macs are not affected. Evidence is on
[#893](https://github.com/cbusillo/BD_to_AVP/issues/893) and in
[#957](https://github.com/cbusillo/BD_to_AVP/pull/957).

The hosted unit run keeps only its narrow known-failure skip for this
signature. A hosted image that passes the diagnostic matrix below is the signal
to remove it. This failure is separate from the local Qt test teardown crash
fixed in #934.

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
workarounds. CPU-only mode explicitly assigns and reads back a supported CPU
device for every compute stage using Apple's [compute-device API](https://developer.apple.com/documentation/vision/vnrequest/setcomputedevice%3Aforcomputestage%3A),
instead of relying on the deprecated CPU-only flag. Apple documents
the [recognition levels](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)
as request properties.

Output records macOS build, architecture, Python/PyObjC and selected FFmpeg
provenance, then actual request revision, languages, compute-device availability,
native success/error and recognized text. CI provenance includes source SHA,
run ID and attempt. A pass also requires observing at least one wrapped request
per fixture cue, allowing the ripper's low-confidence retries; an instrumentation
bypass cannot silently qualify a mode.
Available devices do not prove
which device performed the request. Request diagnostics wrap the native handler;
compare the ordinary uninstrumented test before attributing a failure to Vision.
The hosted matrix runs serially after the parallel unit suite has already used
Vision, and device enumeration can itself affect initialization. If its default
case passes alongside an ordinary unit-test skip in the same run, investigate
concurrency, cold state and instrumentation; that combination does not qualify
the image as recovered.

To separate the PyObjC bridge from the Vision backend, save the exact bitmaps
the default experiment passes to Vision, then recognize them from Swift:

```sh
uv run python -m scripts.qualify_vision_ocr --mode default --dump-bitmaps build/vision-ocr/bitmaps
xcrun swiftc scripts/vision_ocr_control.swift -o build/vision-ocr-control
build/vision-ocr-control build/vision-ocr/bitmaps
```

The Swift control rebuilds each grayscale image from the raw bytes, checks
their SHA-256, and applies the same accurate level, language correction and
languages. Each output line shows the Python outcome next to the Swift outcome
for one bitmap. A Swift failure on the bytes Python also failed on points at the
OS/VM backend; a Swift success there points at the bridge or request setup.
A false return without NSError appears in Swift as a thrown nil error. It
passes only when every bitmap returns text.

To compare on the current hosted image, manually dispatch `CI` on the task
branch with `vision_ocr_diagnostics=true`. The `vision-ocr-diagnostics` artifact
contains separate process logs and exit statuses. The diagnostic step records
failed experiments without replacing the existing unit-test gate; a green
workflow alone is not native qualification. The matrix is bounded to ten
minutes, and artifact upload runs after a timeout too. Ordinary CI runs do not
pay for this experiment.

Record conclusions with exact source SHA, image version, OS build and artifact
links on #893 before hosted artifacts expire. Keep any required raw logs in the
task's approved retained-evidence location under the host retention policy.
An experiment that passes only after changing recognition settings establishes
an observation, not a production fix or a boundary affecting all users on that
OS. A runtime boundary needs affected physical-Mac evidence or a demonstrated
native prerequisite; the runner label alone cannot establish it.
