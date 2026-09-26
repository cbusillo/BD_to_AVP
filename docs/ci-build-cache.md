# CI compilation cache

The Mac and visionOS CI test builds use Xcode's content-addressed compilation
cache. Each build still creates its own DerivedData, links its test products,
and runs the selected tests. A cache miss performs a normal compilation.

Only the compiler cache directory is transferred through GitHub Actions.
DerivedData, application bundles, test results, packaged tools, signing inputs,
and release artifacts are not transferred. The cache configuration is applied
through `XCODE_XCCONFIG_FILE` on the two test-build steps. The Mac packaging
smoke step and the release workflows do not load it. The bundled decoder still
rebuilds and must match its committed binary byte for byte.

## Keys and writers

Separate caches serve the Mac and visionOS lanes. Their compatibility prefix
includes the runner OS and architecture, full `xcodebuild -version` output,
project specification, and compilation-cache configuration. Changing the
toolchain or project configuration cannot restore the old cache.

The final key component is the committed `macos` source tree. A source change
creates a new key while allowing a compatible older compiler cache to be
restored. Xcode checks compilation inputs before reusing each result; unchanged
compilations can hit while changed source is compiled again.

The restore action cannot save in a post-job step. Only a successful test step
on a push to `main` can reach the explicit save action. Pull requests and manual
runs only restore. GitHub also scopes pull-request caches to their merge ref,
so another PR or `main` cannot read a cache written by a modified PR workflow.

Cache entries remain subject to the repository's existing GitHub cache quota
and eviction policy. Cache availability is an optimization, never a reason to
skip a build or a test.

## Verification

Check the restore action's `cache-hit` and `cache-matched-key` outputs to
distinguish an exact match, a compatible source revision, and a cold run.
`COMPILATION_CACHE_ENABLE_DIAGNOSTIC_REMARKS` records compiler hits and misses
in the build log. Measure cold and warm builds with fresh DerivedData and keep
simulator startup and test execution separate from compilation time. Compare
medians from repeated runs on the same hardware and toolchain.

Sources: [Apple's build settings reference](https://developer.apple.com/documentation/xcode/build-settings-reference),
[GitHub cache access and key matching](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
and the [restore-only action](https://github.com/actions/cache/tree/main/restore).
