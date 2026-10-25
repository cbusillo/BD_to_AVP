# BD to AVP Mac icon

The Mac app icon is concept M3 ("Tilted Disc") from #1127, chosen by the Director on October 25, 2026. It is the Shiny 3D Cinema headset (C3, in `../shiny-3d-cinema`) in front of a tilted Blu-ray disc, so the two apps read as one family.

- `composite.png` is the 1024×1024 master: a rounded square with transparent corners and a soft shadow. `bd_to_avp/resources/app_icon.icns` is built from it at every standard size (16 to 1024) with `sips` and `iconutil`.
- `background.png`, `background-fullbleed.png`, `disc.png`, `headset.png`, `shadow.png` and `squircle-mask.png` are the separate layers, for rebuilding in Icon Composer later.
- `disc-tilt-generated.png` is the untouched generated disc.

The artwork was generated with Codex's built-in image generation; the tool doesn't report a model ID. The exact prompts are in `prompts-mac-pass.json`. There is no Apple logo.
