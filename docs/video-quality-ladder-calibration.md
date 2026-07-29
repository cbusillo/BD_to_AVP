# Video Quality Ladder Calibration

The route-relative quality ladder is intentionally a calibration contract before it is a user-facing control. Its
seven ordered steps are `Space Saver`, `Compact`, `Efficient`, `Balanced`, `Detailed`, `High Detail`, and
`Maximum Detail`. `Custom` remains separate and preserves exact route controls.

The checked contract is `docs/qualification/video-quality-ladder-v1.json`. It currently records only production
defaults at `Balanced`:

- direct MV-HEVC quality `0.7`
- direct MV-HEVC with MetalFX 2x quality `0.6`
- generated MV-HEVC at `20` Mbps per eye and merge quality `75`
- file-based upscale quality `75`
- AV1 CRF `32`, retained as `Custom · CRF` while physical M5 evidence remains pending in #409

Every other numeric mapping is `needs_calibration`; the manifest rejects values attached to that status. This keeps
the UI and profile model from shipping guessed mappings.

## Validation

Run the structural and production-anchor check with:

```bash
uv run python scripts/validate_video_quality_ladder.py
```

The normal command exits successfully when the contract is valid, even while calibration is incomplete. It emits a
review receipt with the manifest and corpus hashes, source Git state, per-target status counts, blockers, and deferred
AV1 work. CI runs this mode to prevent contract drift.

Use the completion gate only when evaluating whether the mappings are ready to expose:

```bash
uv run python scripts/validate_video_quality_ladder.py \
  --require-complete \
  --output build/qualification/video-quality-ladder-receipt.json
```

`--require-complete` fails until every non-AV1 target has all seven qualified mappings and ladder exposure is enabled.
AV1 may remain exact-CRF-only; enabling its ladder instead requires all seven qualified mappings and physical anchor
evidence.

## Evidence Rules

Measured mappings must include immutable evidence, source, and fixture SHA-256 identities plus quality delta, output
size ratio, encode time, and an acceptance rationale. Values and measured quality/storage must remain monotonic within
each target. Adjacent generated mappings may hold one control steady only when the other changes in the correct
direction.

`Space Saver`, `Balanced`, and `Maximum Detail` become qualified only with physical Vision Pro evidence for stereo
presentation, beginning/middle/end seeks, and sustained playback. `Balanced` must continue to match production
defaults exactly and defines quality delta `0.0` and output-size ratio `1.0`.
