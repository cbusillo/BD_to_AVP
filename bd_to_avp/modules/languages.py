from __future__ import annotations

import json
import re

from dataclasses import dataclass
from importlib.resources import files
from typing import Final

CATALOG_RESOURCE: Final = "iso639_languages.json"
UNDETERMINED_LANGUAGE_CODE: Final = "und"
LANGUAGE_TAG_PATTERN: Final = re.compile(r"^[a-z]{2,3}(?:[-_](?:[a-z]{2}|[a-z]{4}|[0-9]{3})){0,2}$")


class LanguageCodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaLanguage:
    code: str
    name: str
    alpha2: str | None
    bibliographic: str


def _load_catalog() -> tuple[MediaLanguage, ...]:
    catalog_path = files("bd_to_avp.resources").joinpath(CATALOG_RESOURCE)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("languages"), list):
        raise RuntimeError("The bundled ISO 639 language catalog is invalid.")

    languages: list[MediaLanguage] = []
    for raw_language in catalog["languages"]:
        if not isinstance(raw_language, dict):
            raise RuntimeError("The bundled ISO 639 language catalog is invalid.")
        try:
            language = MediaLanguage(
                code=raw_language["code"],
                name=raw_language["name"],
                alpha2=raw_language["alpha2"],
                bibliographic=raw_language["bibliographic"],
            )
        except (KeyError, TypeError) as error:
            raise RuntimeError("The bundled ISO 639 language catalog is invalid.") from error
        languages.append(language)
    return tuple(languages)


LANGUAGES: Final = _load_catalog()
LANGUAGES_BY_CODE: Final = {language.code: language for language in LANGUAGES}
LANGUAGE_CODES_BY_NAME: Final = {language.name.casefold(): language.code for language in LANGUAGES}
LANGUAGE_CODE_ALIASES: Final = {
    alias: language.code
    for language in LANGUAGES
    for alias in {language.code, language.alpha2, language.bibliographic}
    if alias
}


def normalize_language_code(value: object, *, allow_undetermined: bool = False) -> str:
    if not isinstance(value, str):
        raise LanguageCodeError("Language codes must be strings.")

    candidate = value.strip().casefold()
    if not LANGUAGE_TAG_PATTERN.fullmatch(candidate):
        raise LanguageCodeError(f"Unsupported language code: {value!r}.")
    if candidate == UNDETERMINED_LANGUAGE_CODE:
        if allow_undetermined:
            return UNDETERMINED_LANGUAGE_CODE
        raise LanguageCodeError("Undetermined is not a selectable language.")

    primary_code = candidate.replace("_", "-").split("-", 1)[0]
    canonical_code = LANGUAGE_CODE_ALIASES.get(primary_code)
    if canonical_code is None:
        raise LanguageCodeError(f"Unsupported language code: {value!r}.")
    return canonical_code


def normalize_source_language(value: object) -> str:
    try:
        return normalize_language_code(value, allow_undetermined=True)
    except LanguageCodeError:
        return UNDETERMINED_LANGUAGE_CODE


def language_for_code(value: object) -> MediaLanguage:
    canonical_code = normalize_language_code(value)
    return LANGUAGES_BY_CODE[canonical_code]


def language_code_for_name(value: object) -> str:
    if not isinstance(value, str):
        raise LanguageCodeError("Language names must be strings.")
    canonical_code = LANGUAGE_CODES_BY_NAME.get(value.strip().casefold())
    if canonical_code is None:
        raise LanguageCodeError(f"Unsupported language name: {value!r}.")
    return canonical_code


def language_name(value: object, *, unknown_name: str = "Unknown") -> str:
    canonical_code = normalize_source_language(value)
    if canonical_code == UNDETERMINED_LANGUAGE_CODE:
        return unknown_name
    return LANGUAGES_BY_CODE[canonical_code].name


def language_alpha2(value: object) -> str | None:
    canonical_code = normalize_source_language(value)
    if canonical_code == UNDETERMINED_LANGUAGE_CODE:
        return None
    return LANGUAGES_BY_CODE[canonical_code].alpha2


def language_bibliographic(value: object) -> str:
    return language_for_code(value).bibliographic
