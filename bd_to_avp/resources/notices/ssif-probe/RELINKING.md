# SSIF Probe Relinking Notice

The bundled `ssif_probe` helper dynamically links `libbluray.3.dylib` and
`libudfread.3.dylib` from the adjacent `bd_to_avp/lib` directory. Both
libraries are distributed under LGPL-2.1-or-later.

The exact corresponding source archives, their upstream `COPYING` texts, and
build provenance are provided in this directory. You may replace either shared
library with a compatible build that keeps its documented install name:

- `@rpath/libbluray.3.dylib`
- `@rpath/libudfread.3.dylib`

The helper resolves those install names through `@loader_path/../lib`.

## Rebuild And Replace

1. Extract `libbluray-1.4.1.tar.xz` and `libudfread-1.2.0.tar.xz` from this
   directory.
2. Build the libraries for arm64 macOS 14 or later using the Meson options and
   compiler flags recorded in `build-provenance.json`.
3. Set each replacement library ID with `install_name_tool -id`:
   `@rpath/libbluray.3.dylib` and `@rpath/libudfread.3.dylib`.
4. Set libbluray's libudfread dependency with
   `install_name_tool -change <old-libudfread-path>
   @rpath/libudfread.3.dylib libbluray.3.dylib`.
5. Replace the corresponding files in `bd_to_avp/lib` in the source tree, or
   under `Contents/Resources/app/bd_to_avp/lib` in an installed app.
6. Re-sign a modified app before launching it. For local testing, use
   `codesign --force --deep --sign - "3D Blu-ray to Vision Pro.app"`.

Apple's hardened runtime invalidates the original Developer ID signature after
a library is replaced. A downstream distributor must sign the modified app with
its own identity and repeat its normal notarization process before distribution.
