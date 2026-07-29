from __future__ import annotations

import hashlib
import os

from pathlib import Path


def app_tree_sha256(app_path: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(app_path.rglob("*"), key=lambda item: item.relative_to(app_path).as_posix()):
        relative = path.relative_to(app_path).as_posix().encode("utf-8")
        status = path.lstat()
        mode = status.st_mode & 0o7777
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8")
            kind = b"symlink"
        elif path.is_file():
            payload_digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    payload_digest.update(chunk)
            payload = payload_digest.digest()
            kind = b"file"
        elif path.is_dir():
            payload = b""
            kind = b"directory"
        else:
            continue
        for component in (kind, relative, f"{mode:o}".encode("ascii"), payload):
            digest.update(len(component).to_bytes(8, "big"))
            digest.update(component)
    return digest.hexdigest()
