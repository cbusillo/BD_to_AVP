# Shiny 3D Cinema icon

The visionOS app icon is concept C3 ("Bold Cinema") from #1127. It was chosen by the Director on October 25, 2026.

- `background.png` is the opaque back layer, copied into `macos/BDToAVPPlayer/Assets.xcassets/AppIcon.solidimagestack/Back.solidimagestacklayer`.
- `foreground.png` is the transparent headset layer, copied into the `Front` layer. visionOS adds the parallax between the two.
- `generated-foreground-original.png` is the untouched generated headset. `foreground.png` is the same image cropped and centered at 75% of the 1024 canvas.
- `composite.png` shows both layers flattened.

The artwork was generated with Codex's built-in image generation; the tool doesn't report a model ID. The exact prompts are in `prompts-first-pass.json` (the shared background and the original C concept) and `prompts-stylized-pass.json` (C3).

There is no Apple logo. The headset is an original illustration, not traced from Apple product photography.
