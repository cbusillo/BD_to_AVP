from __future__ import annotations

import argparse
import json

from importlib.metadata import version
from pathlib import Path

from babelfish.language import LANGUAGE_MATRIX

EXPECTED_BABELFISH_VERSION = "0.6.1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "bd_to_avp" / "resources" / "iso639_languages.json"


def build_catalog() -> dict[str, object]:
    babelfish_version = version("babelfish")
    if babelfish_version != EXPECTED_BABELFISH_VERSION:
        raise RuntimeError(
            f"Language catalog generation requires babelfish {EXPECTED_BABELFISH_VERSION}, found {babelfish_version}."
        )

    languages = [
        {
            "code": language.alpha3t,
            "name": language.name,
            "alpha2": language.alpha2 or None,
            "bibliographic": language.alpha3b,
        }
        for language in LANGUAGE_MATRIX
        if language.alpha3t and language.scope in {"I", "M"}
    ]
    languages.sort(key=lambda language: (str(language["name"]).casefold(), str(language["code"])))
    return {
        "schema_version": 1,
        "source": {
            "package": "babelfish",
            "version": babelfish_version,
            "standard": "ISO 639-2",
        },
        "languages": languages,
    }


def render_catalog() -> str:
    return json.dumps(build_catalog(), ensure_ascii=False, indent=2) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the shared ISO 639 language catalog.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Fail when the checked-in catalog is stale.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rendered = render_catalog()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"Language catalog is stale: {args.output}")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
