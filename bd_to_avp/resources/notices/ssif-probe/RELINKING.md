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
