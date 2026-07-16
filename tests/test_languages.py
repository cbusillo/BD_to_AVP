import unittest

from bd_to_avp.modules.languages import (
    LANGUAGES,
    LanguageCodeError,
    language_alpha2,
    language_bibliographic,
    language_code_for_name,
    language_name,
    normalize_language_code,
    normalize_source_language,
)
from scripts.generate_language_catalog import DEFAULT_OUTPUT, render_catalog


class LanguageCatalogTests(unittest.TestCase):
    def test_catalog_is_complete_unique_and_current(self) -> None:
        self.assertEqual(len(LANGUAGES), 414)
        self.assertEqual(len({language.code for language in LANGUAGES}), len(LANGUAGES))
        self.assertEqual(DEFAULT_OUTPUT.read_text(encoding="utf-8"), render_catalog())

    def test_normalizes_alpha2_bibliographic_and_terminologic_codes(self) -> None:
        expected = {
            "nl": "nld",
            "dut": "nld",
            "nld": "nld",
            "ger": "deu",
            "deu": "deu",
            "fre": "fra",
            "fra": "fra",
            "chi": "zho",
            "zho": "zho",
            "ace": "ace",
        }
        for supplied, canonical in expected.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_language_code(supplied), canonical)

    def test_normalizes_case_and_ietf_region_or_script_suffixes(self) -> None:
        self.assertEqual(normalize_language_code(" NL "), "nld")
        self.assertEqual(normalize_language_code("pt-BR"), "por")
        self.assertEqual(normalize_language_code("zh_Hant"), "zho")

    def test_rejects_invalid_or_non_selectable_codes(self) -> None:
        for supplied in (None, "", "und", "mul", "mis", "zxx", "xyz", "ééé"):
            with self.subTest(supplied=supplied):
                with self.assertRaises(LanguageCodeError):
                    normalize_language_code(supplied)

    def test_source_metadata_falls_back_to_undetermined(self) -> None:
        self.assertEqual(normalize_source_language("und"), "und")
        self.assertEqual(normalize_source_language("Ger"), "deu")
        self.assertEqual(normalize_source_language("not-a-language"), "und")
        self.assertEqual(normalize_source_language(None), "und")

    def test_exposes_catalog_metadata(self) -> None:
        self.assertEqual(language_name("dut"), "Dutch")
        self.assertEqual(language_alpha2("nld"), "nl")
        self.assertIsNone(language_alpha2("ace"))
        self.assertEqual(language_bibliographic("deu"), "ger")
        self.assertEqual(language_code_for_name("Dutch"), "nld")


if __name__ == "__main__":
    unittest.main()
